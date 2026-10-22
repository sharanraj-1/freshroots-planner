"""Golden results on the seed data (run_date 2026-10-19), as signed off in CLAUDE.md.

The files in tests/golden/ were produced by the original prototype engine.py; the restructured
package must reproduce them exactly for both trend bases.
"""

import csv
import json
from pathlib import Path

import pytest

from planner.jobs.run import run

GOLDEN = Path(__file__).parent / "golden"
REORDER = {"SKU001": 750, "SKU003": 300, "SKU004": 150, "SKU011": 500, "SKU013": 120}
CASES = {
    "unadjusted": (REORDER, 1820),
    "adjusted": (REORDER | {"SKU001": 500}, 1570),
}


@pytest.fixture(scope="module", params=CASES)
def result(request, tmp_path_factory):
    out = tmp_path_factory.mktemp(request.param)
    rows, summary, _, _ = run(run_date="2026-10-19", trend_basis=request.param, outdir=out)
    return request.param, rows, summary, out


def test_decisions(result):
    basis, rows, summary, _ = result
    reorder, po_units = CASES[basis]
    by = {r["sku_id"]: r for r in rows}
    assert {s: r["recommended_qty"] for s, r in by.items() if r["decision"] == "REORDER"} == reorder
    assert {s for s, r in by.items() if r["decision"] == "PAUSE"} == {"SKU006", "SKU007"}
    assert summary == {
        "REORDER": 5,
        "PAUSE": 2,
        "HOLD": 8,
        "EXCEPTION": 0,
        "needs_review": 5,
        "po_units": po_units,
    }


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.reader(f))


@pytest.mark.parametrize("name", ["recommendations.csv", "zoho_draft_po.csv"])
def test_csv_matches_prototype(result, name):
    basis, _, _, out = result
    assert read_csv(out / name) == read_csv(GOLDEN / basis / name)


def test_review_queue_matches_prototype(result):
    basis, _, _, out = result
    actual = json.loads((out / "review_queue.json").read_text(encoding="utf-8"))
    assert actual == json.loads((GOLDEN / basis / "review_queue.json").read_text(encoding="utf-8"))
