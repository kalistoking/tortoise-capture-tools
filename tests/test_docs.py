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


def test_a_docstring_names_only_private_helpers_that_exist():
    """behaviour.py and play_sound.py both pointed at `_sound_for`; the method is
    `_sound_attribution` and nothing of the old name was left to find. A backticked
    `_name` in a docstring must be defined, assigned or used somewhere in the code
    proper (strings and docstrings do not count)."""
    import ast

    sources = {path: ast.parse(path.read_text(encoding="utf-8"))
               for path in sorted((ROOT / "src").rglob("*.py"))}
    code_names = set()
    for tree in sources.values():
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                code_names.add(node.id)
            elif isinstance(node, ast.Attribute):
                code_names.add(node.attr)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                code_names.add(node.name)
            elif isinstance(node, ast.arg):
                code_names.add(node.arg)
            elif isinstance(node, ast.alias):
                code_names.add((node.asname or node.name).split(".")[0])
    missing = []
    for path, tree in sources.items():
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                doc = ast.get_docstring(node, clean=False) or ""
                missing += [(path.name, name) for name in re.findall(r"`(_[a-z]\w*)`", doc)
                            if name not in code_names]
    assert not missing, missing
