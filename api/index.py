"""Vercel entry point. Without DATABASE_URL it runs the seeded demo (see planner/demo.py)."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("PLANNER_SKIP_DOTENV", "1")
os.environ.setdefault("LOG_FORMAT", "json")
if "DATABASE_URL" not in os.environ:
    os.environ["DATABASE_URL"] = "sqlite:////tmp/freshroots_demo.db"
    os.environ["PLANNER_DEMO"] = "1"
    from planner.demo import bootstrap

    bootstrap()

from planner.api.main import app  # noqa: E402,F401  (Vercel serves this ASGI app)
