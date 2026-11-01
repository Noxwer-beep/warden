#!/usr/bin/env python3
"""Launcher: run Warden straight from the checkout."""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("PYTHONPATH", str(ROOT))

from warden.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
