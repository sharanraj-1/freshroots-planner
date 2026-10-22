"""Raghav Kulkarni's documented reorder rules as pure functions (no I/O).

Thresholds come from config.json; judgment cases are flagged for review, never decided here.
"""

import math
import statistics
from datetime import date


def decide(cover, lo, hi, high_demand):
    if high_demand and cover > hi:
        return "EXCEPTION"  # rules conflict: rising demand but overstocked
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
        r = dict(
            sku_id=sku,
            sku_name=m["SKU_Name"],
            category=m["Category"],
            perishability=m["Perishability"],
            distributor=dists.get(m["Distributor_ID"], {}).get("Distributor_Name", m["Distributor_ID"]),
            stock_on_hand=stock,
            standard_qty=std,
            min_cover=lo,
            max_cover=hi,
            festival=is_festival,
            history_weeks=len(units),
            weeks=weeks,
            units=units,
        )
        flags, exc = [], []

        if len(units) < rf["min_history_weeks"]:
            exc.append(f"Only {len(units)} weeks of history (need {rf['min_history_weeks']})")
            r.update(decision="EXCEPTION", recommended_qty=0)
            r["exception_reasons"], r["review_flags"] = exc, flags
            rows.append(r)
            continue

        base = statistics.mean(units[-nb:])
        trend = statistics.mean(units[-nt:])
        if base == 0:
            exc.append("Zero sales across the baseline window")
            r.update(decision="EXCEPTION", recommended_qty=0, avg_12w=0, avg_4w=trend)
            r["exception_reasons"], r["review_flags"] = exc, flags
            rows.append(r)
            continue

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
            flags.append(
                f"Cover {cover:.2f} wks is within {rf['near_threshold_pct']:.0%} of a threshold ({lo}/{hi})"
            )
        if decide(cover_unadj, lo, hi, high) != decision:
            flags.append(
                f"Decision depends on festival uplift: without it, {decide(cover_unadj, lo, hi, high)}"
            )
        if post_cover is not None and post_cover < lo:
            flags.append(
                f"Standard qty leaves only {post_cover:.2f} wks cover; "
                f"{qty_to_min} units needed to reach {lo} wks"
            )
        cv = statistics.pstdev(units[-nb:]) / base
        if cv > rf["volatility_cv"]:
            flags.append(f"Volatile demand (CV {cv:.2f}) - check for a level shift")

        r.update(
            avg_12w=round(base, 2),
            avg_4w=round(trend, 2),
            adjusted_weekly=round(adj, 2),
            trend_ratio=round(trend_ratio, 3),
            high_demand=high,
            weeks_cover=round(cover, 2),
            weeks_cover_unadjusted=round(cover_unadj, 2),
            decision=decision,
            recommended_qty=qty,
            post_order_cover=round(post_cover, 2) if post_cover is not None else None,
            qty_to_min_cover=qty_to_min,
            qty_to_mid_cover=qty_to_mid,
            cv_12w=round(cv, 3),
            exception_reasons=exc,
            review_flags=flags,
            rule_trace=trace(decision, cover, lo, hi, high, trend_ratio, uplift, std, qty),
        )
        rows.append(r)
    return rows, is_festival, uplift


def trace(decision, cover, lo, hi, high, ratio, uplift, std, qty):
    t = [
        f"Festival uplift x{uplift:.2f} applied to 12-wk average" if uplift > 1 else "No festival uplift",
        f"4-wk vs baseline: {ratio:.2f}x -> {'HIGH DEMAND' if high else 'normal'}",
        f"Weeks of cover {cover:.2f} vs window {lo}-{hi}",
    ]
    t.append(
        {
            "REORDER": f"Below {lo} wks -> reorder {qty} (= {std}{' x 1.5' if high else ''})",
            "PAUSE": f"Above {hi} wks -> pause next order",
            "HOLD": "Inside window -> no action",
            "EXCEPTION": "Rules conflict -> planner decides",
        }[decision]
    )
    return t
