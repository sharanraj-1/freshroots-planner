"""Data-quality checks on a Dataset. Errors stop the run; warnings are logged with it."""

from dataclasses import asdict, dataclass
from datetime import date


@dataclass(frozen=True)
class Issue:
    level: str  # error | warn
    message: str
    file: str = ""

    def as_dict(self):
        return asdict(self)


def errors(issues):
    return [i for i in issues if i.level == "error"]


def validate(ds, cfg, run_date):
    run_date = date.fromisoformat(str(run_date))
    fresh = cfg["data_freshness"]
    issues = []

    def err(msg, file):
        issues.append(Issue("error", msg, file))

    def warn(msg, file=""):
        issues.append(Issue("warn", msg, file))

    skus = ds.sku_map()
    dists = ds.distributor_map()

    seen = set()
    for r in ds.sales:
        if r.units_sold < 0:
            err(f"{r.sku_id} {r.week}: negative sales", "weekly_sales.csv")
        key = (r.sku_id, r.week)
        if key in seen:
            err(f"{key}: duplicate sales row", "weekly_sales.csv")
        seen.add(key)
        if r.sku_id not in skus:
            err(f"{r.sku_id} {r.week}: SKU not in sku_master", "weekly_sales.csv")
    seen = set()
    for r in ds.inventory:
        key = (r.sku_id, r.location, r.as_of_date)
        if key in seen:
            err(f"{key}: duplicate inventory row", "current_inventory.csv")
        seen.add(key)
        if r.sku_id not in skus:
            err(f"{r.sku_id}: inventory for a SKU not in sku_master", "current_inventory.csv")
    if len({s.sku_id for s in ds.skus}) != len(ds.skus):
        err("duplicate SKU_ID in sku_master", "sku_master.csv")

    series = ds.series()
    inventory = ds.latest_inventory()
    for sku, m in skus.items():
        if sku not in inventory:
            err(f"{sku}: no inventory record", "current_inventory.csv")
        if sku not in series:
            err(f"{sku}: no sales history", "weekly_sales.csv")
        if m.perishability not in cfg["cover_windows"]:
            err(f"{sku}: unknown perishability '{m.perishability}'", "sku_master.csv")
        if m.distributor_id not in dists:
            warn(f"{sku}: distributor {m.distributor_id} not in master", "sku_master.csv")

    for sku, (_, as_of) in inventory.items():
        age = (run_date - as_of).days
        if age < 0:
            err(f"{sku}: inventory dated {as_of}, after the run date {run_date}", "current_inventory.csv")
        elif age > fresh["max_inventory_age_days"]:
            err(
                f"{sku}: inventory is {age} days old (limit {fresh['max_inventory_age_days']})",
                "current_inventory.csv",
            )

    if not series:
        err("no sales rows", "weekly_sales.csv")
    else:
        latest = max(d for s in series.values() for d, _, _ in s)
        if (run_date - latest).days > fresh["max_sales_age_days"]:
            err(f"latest sales week starts {latest}; data is stale", "weekly_sales.csv")
        for sku, s in series.items():
            weeks = [d for d, _, _ in s]
            gaps = [w for a, w in zip(weeks, weeks[1:], strict=False) if (w - a).days != 7]
            if gaps:
                warn(f"{sku}: missing weeks before {gaps}", "weekly_sales.csv")
            if weeks[-1] < latest:
                warn(
                    f"{sku}: no sales rows after week starting {weeks[-1]} (latest is {latest})",
                    "weekly_sales.csv",
                )

    off_region = sorted({d.region for d in ds.distributors} - set(cfg["home_regions"]))
    if off_region:
        warn(
            f"distributor regions outside {'/'.join(cfg['home_regions'])}: {off_region}",
            "distributor_master.csv",
        )

    if ds.open_pos is None:
        warn("no open-PO / in-transit data: cover uses on-hand stock only")
    else:
        for p in ds.open_pos:
            if p.sku_id not in skus:
                err(f"{p.po_number}: open PO for {p.sku_id}, not in sku_master", "open_po.csv")
        if not cfg["cover_includes_open_pos"]:
            warn("open-PO data present but cover_includes_open_pos is false: cover uses on-hand only")

    if ds.suppliers is None:
        warn("no supplier lead times or supplier IDs: POs need a vendor before release")
    else:
        by_sku = {s.sku_id: s for s in ds.suppliers}
        for s in ds.suppliers:
            if s.sku_id not in skus:
                warn(f"supplier row for {s.sku_id}, not in sku_master", "supplier_master.csv")
        missing = sorted(k for k in skus if not (k in by_sku and by_sku[k].zoho_vendor_id))
        if missing:
            warn(f"no Zoho vendor for {missing}: their POs stay in the CSV export", "supplier_master.csv")
    return issues
