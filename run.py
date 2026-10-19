#!/usr/bin/env python3
"""One command: replay the trigger script, run the supplied simulator, judge outcomes and write all reports.

    python run.py                 # same as: python -m stage_agent run   (no install, no API key, no network)
    python run.py run --seeds oua-practice,my-seed
    python run.py inventory
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from stage_agent.cli import main  # noqa: E402

if __name__ == "__main__":
    args = sys.argv[1:]
    if not args or args[0].startswith("-"):
        args = ["run", *args]
    raise SystemExit(main(args))
