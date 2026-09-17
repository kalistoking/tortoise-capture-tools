"""pytest setup: make src/ and the helpers importable, and keep logs quiet.

The same files run without pytest via tests/run_tests.py.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
for path in (HERE, HERE.parent / "src"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

# Several tests exercise the error paths on purpose; their log output is not
# the assertion and would only make the run unreadable.
_root = logging.getLogger("tct")
_root.addHandler(logging.NullHandler())
_root.propagate = False
