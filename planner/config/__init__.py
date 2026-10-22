"""Business-rule settings. Every threshold lives in config.json at the repo root."""

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = Path(os.environ.get("PLANNER_CONFIG", ROOT / "config.json"))


def load_config(path=None, run_date=None, trend_basis=None):
    cfg = json.loads(Path(path or CONFIG_PATH).read_text(encoding="utf-8"))
    if run_date:
        cfg["run_date"] = run_date
    if trend_basis:
        cfg["trend_test_basis"] = trend_basis
    return cfg
