"""Business-rule settings. Every threshold lives in config.json at the repo root.

The file is checked against RulesConfig on load, so a typo or a missing threshold stops
the run instead of silently changing a rule.
"""

import json
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = Path(os.environ.get("PLANNER_CONFIG", ROOT / "config.json"))


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CoverWindow(_Strict):
    min_weeks: int | float = Field(gt=0)
    max_weeks: int | float = Field(gt=0)


class ReviewFlags(_Strict):
    near_threshold_pct: float = Field(ge=0)
    volatility_cv: float = Field(ge=0)
    min_history_weeks: int = Field(ge=1)


class DataFreshness(_Strict):
    max_inventory_age_days: int = Field(ge=0)
    max_sales_age_days: int = Field(ge=0)


class RulesConfig(_Strict):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    note: str = Field("", alias="_note")
    run_date: str
    baseline_weeks: int = Field(ge=1)
    trend_weeks: int = Field(ge=1)
    high_demand_threshold: float = Field(ge=0)
    high_demand_multiplier: float = Field(ge=1)
    festival_uplift: float = Field(ge=0)
    festival_basis: Literal["run_month"]
    trend_test_basis: Literal["unadjusted", "adjusted"]
    cover_windows: dict[str, CoverWindow]
    cover_includes_open_pos: bool
    review_flags: ReviewFlags
    data_freshness: DataFreshness
    home_regions: list[str]
    decision_reasons: list[str] = Field(min_length=1)  # required for every edit and reject


def load_config(path=None, run_date=None, trend_basis=None):
    cfg = json.loads(Path(path or CONFIG_PATH).read_text(encoding="utf-8"))
    if run_date:
        cfg["run_date"] = str(run_date)
    if trend_basis:
        cfg["trend_test_basis"] = trend_basis
    return RulesConfig.model_validate(cfg).model_dump(by_alias=True)
