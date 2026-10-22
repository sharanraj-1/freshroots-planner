"""Load one folder of SAP exports into the database.

Usage:  python -m planner.ingest [--dir PATH] [--date YYYY-MM-DD]
        --dir defaults to $SAP_INBOUND_DIR; --date (the data's as-of date) defaults to today.
"""

import argparse
import os
import sys
from datetime import date

from planner.config import load_config
from planner.db import make_session_factory
from planner.ingest.load import ingest_folder


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dir", default=os.environ.get("SAP_INBOUND_DIR"))
    ap.add_argument("--date", default=date.today().isoformat())
    a = ap.parse_args(argv)
    if not a.dir:
        ap.error("no --dir given and SAP_INBOUND_DIR is not set")
    with make_session_factory()() as session:
        result = ingest_folder(session, a.dir, load_config(), a.date)
    for i in result.issues:
        print(
            f"{i.level.upper():5} {i.file or '-'}: {i.message}",
            file=sys.stderr if i.level == "error" else sys.stdout,
        )
    print(f"batch {result.batch_id}: {'loaded' if result.loaded else 'REJECTED - nothing loaded'}")
    return 0 if result.loaded else 1


if __name__ == "__main__":
    sys.exit(main())
