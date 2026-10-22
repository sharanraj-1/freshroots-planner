"""Load the SAP flat-file exports and check them before anything is planned.

Errors stop the run; warnings are logged with it.
"""

import csv
from datetime import date, datetime
from pathlib import Path

from planner.config import ROOT

SEED_DIR = ROOT / "data" / "seed"


def read_csv(data_dir, name):
    with open(Path(data_dir) / name, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def iso_week_monday(week):  # "2026-W42" -> Monday of that ISO week
    return datetime.strptime(week + "-1", "%G-W%V-%u").date()


def load_and_validate(cfg, data_dir=SEED_DIR):
    issues, run_date = [], date.fromisoformat(cfg["run_date"])
    skus = {r["SKU_ID"]: r for r in read_csv(data_dir, "sku_master.csv")}
    dists = {r["Distributor_ID"]: r for r in read_csv(data_dir, "distributor_master.csv")}
    inv = {r["SKU_ID"]: r for r in read_csv(data_dir, "current_inventory.csv")}
    cal = {
        r["Year_Month"]: r["Is_Festival_Month"].strip().upper() == "TRUE"
        for r in read_csv(data_dir, "calendar.csv")
    }
    sales = {}
    for r in read_csv(data_dir, "weekly_sales.csv"):
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
        gaps = [w for a, w in zip(weeks, weeks[1:], strict=False) if (w - a).days != 7]
        if gaps:
            issues.append(("warn", f"{sku}: missing weeks before {gaps}"))
    off_region = sorted({d["Region"] for d in dists.values()} - {"Maharashtra", "Goa"})
    if off_region:
        issues.append(("warn", f"distributor regions outside Maharashtra/Goa: {off_region}"))
    issues.append(("warn", "no open-PO / in-transit data: cover uses on-hand stock only"))
    issues.append(("warn", "no supplier lead times or supplier IDs: POs need a vendor before release"))
    return skus, dists, inv, cal, series, issues
