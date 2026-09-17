"""Run the suite without pytest: `python tests/run_tests.py`.

pytest runs the same files unchanged (`pip install -e ".[dev]" && pytest`);
this exists so the suite is runnable on a machine that has neither pytest nor
permission to install it.
"""

from __future__ import annotations

import importlib
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
for path in (HERE, HERE.parent / "src"):
    sys.path.insert(0, str(path))

import conftest  # noqa: E402,F401 - path and logging setup, shared with pytest


def main() -> int:
    passed, failures = 0, []
    for path in sorted(HERE.glob("test_*.py")):
        module = importlib.import_module(path.stem)
        for name in sorted(vars(module)):
            if not name.startswith("test_"):
                continue
            func = getattr(module, name)
            if not callable(func):
                continue
            try:
                func()
                passed += 1
            except Exception:
                failures.append((path.stem, name, traceback.format_exc()))

    for stem, name, trace in failures:
        print(f"\nFAIL {stem}.{name}\n{trace}")
    print(f"\n{passed} passed, {len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
