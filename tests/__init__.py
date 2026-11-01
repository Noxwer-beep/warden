"""Warden test suite (stdlib unittest).

The package lives in src/, so the tests put it on the path themselves — they run
from a fresh checkout without an install step.
"""

import pathlib
import sys

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
