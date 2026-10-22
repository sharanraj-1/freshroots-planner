"""
Freshroots demand-planning rules engine (prototype on seed data).

Runs Raghav Kulkarni's documented reorder logic deterministically, routes
judgment cases to a review queue, and writes:
  out/recommendations.csv   one row per SKU, with the full calculation trace
  out/zoho_draft_po.csv     REORDER lines only, status Draft (never released)
  out/review_queue.json     structured payloads for the AI exception assistant
  out/run_log.json          data-quality checks, config used, summary

Usage:  python engine.py [--trend-basis unadjusted|adjusted] [--out out]
"""
import argparse, csv, json, math, os, statistics
from datetime import date, datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")


# ---------- loading & validation ----------
def read_csv(name):
    with open(os.path.join(DATA, name), newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def iso_week_monday(week):            # "2026-W42" -> Monday of that ISO week
    return datetime.strptime(week + "-1", "%G-W%V-%u").date()


def load_and_validate(cfg):
    issues, run_date = [], date.fromisoformat(cfg["run_date"])
    skus = {r["SKU_ID"]: r for r in read_csv("sku_master.csv")}
    dists = {r["Distributor_ID"]: r for r in read_csv("distributor_master.csv")}
    inv = {r["SKU_ID"]: r for r in read_csv("current_inventory.csv")}
    cal = {r["Year_Month"]: r["Is_Festival_Month"].strip().upper() == "TRUE"
           for r in read_csv("calendar.csv")}
    sales = {}
    for r in read_csv("weekly_sales.csv"):
        u = int(r["Units_Sold"])
        if u < 0:
            issues.append(("error", f"{r['SKU_ID']} {r['Week']}: negative sales"))
        key = (r["SKU_ID"], r["Week"])
        if key in sales:
            issues.append(("error", f"{key}: duplicate sales row"))
        sales[key] = u

    series = {}
    for (sku, wk), u in sales.items():
        series.setdefault(sku, []).append((iso_week_monday(wk), wk, u))
    for sku in series:
        series[sku].sort()

    for sku, m in skus.items():
        if sku not in inv:
            issues.append(("error", f"{sku}: no inventory record"))
        if sku not in series:
            issues.append(("error", f"{sku}: no sales history"))
        if m["Perishability"] not in cfg["cover_windows"]:
            issues.append(("error", f"{sku}: unknown perishability '{m['Perishability']}'"))
        if m["Distributor_ID"] not in dists:
            issues.append(("warn", f"{sku}: distributor {m['Distributor_ID']} not in master"))
    for sku, r in inv.items():
        age = (run_date - date.fromisoformat(r["As_Of_Date"])).days
        if age > 1:
            issues.append(("error", f"{sku}: inventory is {age} days old (limit 1)"))
    latest = max(d for s in series.values() for d, _, _ in s)
    if (run_date - latest).days > 13:
        issues.append(("error", f"latest sales week starts {latest}; data is stale"))
    for sku, s in series.items():
        weeks = [d for d, _, _ in s]
        gaps = [w for a, w in zip(weeks, weeks[1:]) if (w - a).days != 7]
        if gaps:
            issues.append(("warn", f"{sku}: missing weeks before {gaps}"))
    off_region = sorted({d["Region"] for d in dists.values()} - {"Maharashtra", "Goa"})
    if off_region:
        issues.append(("warn", f"distributor regions outside Maharashtra/Goa: {off_region}"))
    issues.append(("warn", "no open-PO / in-transit data: cover uses on-hand stock only"))
    issues.append(("warn", "no supplier lead times or supplier IDs: POs need a vendor before release"))
    return skus, dists, inv, cal, series, issues


# ---------- rules ----------
def decide(cover, lo, hi, high_demand):
    if high_demand and cover > hi:
        return "EXCEPTION"                       # rules conflict: rising demand but overstocked
    if cover > hi:
        return "PAUSE"
    if cover < lo:
        return "REORDER"
    return "HOLD"


def evaluate(cfg, skus, dists, inv, cal, series):
    run_date = date.fromisoformat(cfg["run_date"])
    is_festival = cal.get(run_date.strftime("%Y-%m"), False)
    uplift = 1 + cfg["festival_uplift"] if is_festival else 1.0
    nb, nt = cfg["baseline_weeks"], cfg["trend_weeks"]
    rf = cfg["review_flags"]
    rows = []
    for sku, m in skus.items():
        units = [u for _, _, u in series.get(sku, [])]
        weeks = [w for _, w, _ in series.get(sku, [])]
        lo = cfg["cover_windows"][m["Perishability"]]["min_weeks"]
        hi = cfg["cover_windows"][m["Perishability"]]["max_weeks"]
        stock = int(inv[sku]["Units_On_Hand"])
        std = int(m["Standard_Reorder_Qty"])
        r = dict(sku_id=sku, sku_name=m["SKU_Name"], category=m["Category"],
                 perishability=m["Perishability"], distributor=dists.get(m["Distributor_ID"], {}).get("Distributor_Name", m["Distributor_ID"]),
                 stock_on_hand=stock, standard_qty=std, min_cover=lo, max_cover=hi,
                 festival=is_festival, history_weeks=len(units), weeks=weeks, units=units)
        flags, exc = [], []

        if len(units) < rf["min_history_weeks"]:
            exc.append(f"Only {len(units)} weeks of history (need {rf['min_history_weeks']})")
            r.update(decision="EXCEPTION", recommended_qty=0)
            r["exception_reasons"], r["review_flags"] = exc, flags
            rows.append(r); continue

        base = statistics.mean(units[-nb:])
        trend = statistics.mean(units[-nt:])
        if base == 0:
            exc.append("Zero sales across the baseline window")
            r.update(decision="EXCEPTION", recommended_qty=0, avg_12w=0, avg_4w=trend)
            r["exception_reasons"], r["review_flags"] = exc, flags
            rows.append(r); continue

        adj = base * uplift
        trend_ref = adj if cfg["trend_test_basis"] == "adjusted" else base
        trend_ratio = trend / trend_ref
        high = trend_ratio > 1 + cfg["high_demand_threshold"]
        cover = stock / adj
        cover_unadj = stock / base
        decision = decide(cover, lo, hi, high)

        qty = 0
        if decision == "REORDER":
            qty = math.ceil(std * (cfg["high_demand_multiplier"] if high else 1))
        post_cover = (stock + qty) / adj if decision == "REORDER" else None
        qty_to_min = max(0, math.ceil(lo * adj - stock))
        qty_to_mid = max(0, math.ceil(((lo + hi) / 2) * adj - stock))

        if decision == "EXCEPTION":
            exc.append(f"Demand up {trend_ratio - 1:.0%} but stock covers {cover:.1f} weeks (max {hi})")
        # soft review flags: rule result stands, Raghav should look
        if high and is_festival and cfg["trend_test_basis"] == "unadjusted":
            flags.append("Festival uplift and high-demand multiplier both applied: possible double count")
        if min(abs(cover - lo) / lo, abs(cover - hi) / hi) <= rf["near_threshold_pct"]:
            flags.append(f"Cover {cover:.2f} wks is within {rf['near_threshold_pct']:.0%} of a threshold ({lo}/{hi})")
        if decide(cover_unadj, lo, hi, high) != decision:
            flags.append(f"Decision depends on festival uplift: without it, {decide(cover_unadj, lo, hi, high)}")
        if post_cover is not None and post_cover < lo:
            flags.append(f"Standard qty leaves only {post_cover:.2f} wks cover; {qty_to_min} units needed to reach {lo} wks")
        cv = statistics.pstdev(units[-nb:]) / base
        if cv > rf["volatility_cv"]:
            flags.append(f"Volatile demand (CV {cv:.2f}) - check for a level shift")

        r.update(avg_12w=round(base, 2), avg_4w=round(trend, 2), adjusted_weekly=round(adj, 2),
                 trend_ratio=round(trend_ratio, 3), high_demand=high, weeks_cover=round(cover, 2),
                 weeks_cover_unadjusted=round(cover_unadj, 2), decision=decision, recommended_qty=qty,
                 post_order_cover=round(post_cover, 2) if post_cover is not None else None,
                 qty_to_min_cover=qty_to_min, qty_to_mid_cover=qty_to_mid, cv_12w=round(cv, 3),
                 exception_reasons=exc, review_flags=flags,
                 rule_trace=trace(decision, cover, lo, hi, high, trend_ratio, uplift, std, qty))
        rows.append(r)
    return rows, is_festival, uplift


def trace(decision, cover, lo, hi, high, ratio, uplift, std, qty):
    t = [f"Festival uplift x{uplift:.2f} applied to 12-wk average" if uplift > 1 else "No festival uplift",
         f"4-wk vs baseline: {ratio:.2f}x -> {'HIGH DEMAND' if high else 'normal'}",
         f"Weeks of cover {cover:.2f} vs window {lo}-{hi}"]
    t.append({"REORDER": f"Below {lo} wks -> reorder {qty} (= {std}{' x 1.5' if high else ''})",
              "PAUSE": f"Above {hi} wks -> pause next order",
              "HOLD": "Inside window -> no action",
              "EXCEPTION": "Rules conflict -> planner decides"}[decision])
    return t


# ---------- outputs ----------
def write_outputs(rows, issues, cfg, is_festival, outdir):
    os.makedirs(outdir, exist_ok=True)
    cols = ["sku_id", "sku_name", "perishability", "stock_on_hand", "avg_12w", "avg_4w", "adjusted_weekly",
            "trend_ratio", "high_demand", "weeks_cover", "min_cover", "max_cover", "decision",
            "recommended_qty", "post_order_cover", "qty_to_min_cover", "needs_review", "notes"]
    with open(os.path.join(outdir, "recommendations.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore"); w.writeheader()
        for r in rows:
            w.writerow({**r, "needs_review": bool(r["review_flags"] or r["exception_reasons"]),
                        "notes": " | ".join(r["exception_reasons"] + r["review_flags"])})
    with open(os.path.join(outdir, "zoho_draft_po.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Item SKU", "Item Name", "Quantity", "Vendor", "Status", "Reason", "Needs Review"])
        for r in rows:
            if r["decision"] == "REORDER":
                w.writerow([r["sku_id"], r["sku_name"], r["recommended_qty"], "TO BE ASSIGNED", "Draft",
                            r["rule_trace"][-1], "Yes" if r["review_flags"] else "No"])
    queue = [{k: r.get(k) for k in ("sku_id", "sku_name", "perishability", "decision", "recommended_qty",
                                    "exception_reasons", "review_flags", "stock_on_hand", "standard_qty",
                                    "avg_12w", "avg_4w", "adjusted_weekly", "weeks_cover", "min_cover",
                                    "max_cover", "post_order_cover", "qty_to_min_cover", "festival")}
             | {"weekly_sales_16w": dict(zip(r["weeks"], r["units"]))}
             for r in rows if r["review_flags"] or r["exception_reasons"]]
    json.dump(queue, open(os.path.join(outdir, "review_queue.json"), "w"), indent=2)
    summary = {d: sum(r["decision"] == d for r in rows) for d in ("REORDER", "PAUSE", "HOLD", "EXCEPTION")}
    summary["needs_review"] = len(queue)
    summary["po_units"] = sum(r["recommended_qty"] for r in rows)
    log = {"run_at": datetime.now().isoformat(timespec="seconds"), "config": cfg,
           "festival_month": is_festival, "summary": summary,
           "data_checks": [{"level": l, "message": m} for l, m in issues]}
    json.dump(log, open(os.path.join(outdir, "run_log.json"), "w"), indent=2)
    return summary


def run(trend_basis=None, outdir="out"):
    cfg = json.load(open(os.path.join(HERE, "config.json")))
    if trend_basis:
        cfg["trend_test_basis"] = trend_basis
    skus, dists, inv, cal, series, issues = load_and_validate(cfg)
    if any(l == "error" for l, _ in issues):
        raise SystemExit("Run stopped - data errors:\n" + "\n".join(m for l, m in issues if l == "error"))
    rows, fest, uplift = evaluate(cfg, skus, dists, inv, cal, series)
    summary = write_outputs(rows, issues, cfg, fest, os.path.join(HERE, outdir))
    return rows, summary, issues, cfg


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--trend-basis", choices=["unadjusted", "adjusted"])
    ap.add_argument("--out", default="out")
    a = ap.parse_args()
    rows, summary, issues, _ = run(a.trend_basis, a.out)
    print(json.dumps(summary))
    for r in rows:
        print(f"{r['sku_id']} {r['decision']:<9} qty={r['recommended_qty']:<4} cover={r.get('weeks_cover')}  "
              + "; ".join(r["exception_reasons"] + r["review_flags"]))
