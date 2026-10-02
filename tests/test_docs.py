"""What the documents say of the project, held to what the project says of itself."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_the_readme_asks_for_the_python_the_project_asks_for():
    """README said 3.14 where pyproject.toml has `>=3.12`: a 3.12 install was told it
    would not do."""
    floor = re.search(r'requires-python\s*=\s*">=\s*(\d+\.\d+)"',
                      (ROOT / "pyproject.toml").read_text(encoding="utf-8")).group(1)
    stated = re.search(r"^- Python (\d+\.\d+)", (ROOT / "README.md").read_text(encoding="utf-8"),
                       re.MULTILINE).group(1)
    assert stated == floor
