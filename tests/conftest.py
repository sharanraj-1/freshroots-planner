import csv
import shutil
from pathlib import Path

import pytest

from planner.config import ROOT, load_config
from planner.control.switches import install_defaults
from planner.db import make_engine, make_session_factory
from planner.models import Base

SEED = ROOT / "data" / "seed"
RUN_DATE = "2026-10-19"


@pytest.fixture
def cfg():
    return load_config(run_date=RUN_DATE)


@pytest.fixture
def engine():
    engine = make_engine("sqlite://")
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def session(engine):
    with make_session_factory(engine)() as s:
        install_defaults(s)
        yield s


@pytest.fixture
def seed_dir(tmp_path):
    """A writable copy of the seed exports."""
    d = tmp_path / "inbound"
    shutil.copytree(SEED, d)
    return d


class Csv:
    """Edit one CSV in place: rows are dicts keyed by the exact column names."""

    def __init__(self, path):
        self.path = Path(path)
        with open(self.path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            self.columns, self.rows = list(reader.fieldnames), list(reader)

    def save(self):
        with open(self.path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=self.columns)
            w.writeheader()
            w.writerows(self.rows)


@pytest.fixture
def edit(seed_dir):
    def _edit(name, fn):
        c = Csv(seed_dir / name)
        fn(c)
        c.save()

    return _edit


def write_csv(path, columns, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=columns)
        w.writeheader()
        w.writerows(rows)
