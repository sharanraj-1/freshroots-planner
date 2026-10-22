"""Read SAP flat-file exports from a folder and parse them against the contracts."""

import csv
import hashlib
import io
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from planner.ingest.contracts import FILES, Dataset, columns
from planner.ingest.validate import Issue


@dataclass
class ParsedFile:
    name: str
    rows: list = field(default_factory=list)
    checksum: str = ""
    issues: list = field(default_factory=list)


def parse_file(path, model):
    name = Path(path).name
    raw = Path(path).read_bytes()
    pf = ParsedFile(name, checksum=hashlib.sha256(raw).hexdigest())
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline=""))
    expected, got = set(columns(model)), set(reader.fieldnames or [])
    if got != expected:
        missing, extra = sorted(expected - got), sorted(got - expected)
        pf.issues.append(Issue("error", f"header mismatch: missing {missing}, unexpected {extra}", name))
        return pf
    for line, record in enumerate(reader, start=2):
        try:
            pf.rows.append(model.model_validate(record))
        except ValidationError as e:
            for d in e.errors():
                where = ".".join(str(x) for x in d["loc"])
                pf.issues.append(Issue("error", f"line {line}: {where}: {d['msg']}", name))
    return pf


def read_folder(folder):
    """Parse every known file in the folder. Returns (Dataset or None, {name: ParsedFile}, issues)."""
    folder = Path(folder)
    parsed, issues = {}, []
    for name, (model, required) in FILES.items():
        path = folder / name
        if not path.exists():
            if required:
                issues.append(Issue("error", "required file missing", name))
            continue
        parsed[name] = parse_file(path, model)
        issues += parsed[name].issues
    if any(i.level == "error" for i in issues):
        return None, parsed, issues

    def rows(name):
        return parsed[name].rows if name in parsed else None

    ds = Dataset(
        skus=rows("sku_master.csv"),
        distributors=rows("distributor_master.csv"),
        calendar=rows("calendar.csv"),
        sales=rows("weekly_sales.csv"),
        inventory=rows("current_inventory.csv"),
        open_pos=rows("open_po.csv"),
        suppliers=rows("supplier_master.csv"),
        daily_orders=rows("daily_orders.csv"),
    )
    return ds, parsed, issues
