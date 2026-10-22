"""Regression tests: run with `python test_engine.py`. Each case pins one documented rule."""
from engine import decide, run

def test_rules():
    assert decide(2.0, 3, 4, False) == "REORDER"      # perishable below min
    assert decide(4.1, 3, 4, False) == "PAUSE"        # perishable above max
    assert decide(10, 6, 14, False) == "HOLD"
    assert decide(15, 6, 14, True) == "EXCEPTION"     # rising demand + overstock -> planner

def test_seed_run():
    rows, s, _, _ = run(outdir="out_test")
    by = {r["sku_id"]: r for r in rows}
    assert by["SKU001"]["high_demand"] and by["SKU001"]["recommended_qty"] == 750   # 500 x 1.5
    assert by["SKU004"]["decision"] == "REORDER"       # perishable, 1.8 wks
    assert by["SKU006"]["decision"] == "PAUSE"         # 22 wks cover
    assert by["SKU002"]["decision"] == "HOLD" and by["SKU002"]["review_flags"]   # uplift flips it
    assert s["po_units"] == 1820

if __name__ == "__main__":
    test_rules(); test_seed_run(); print("all tests passed")
