"""The independence rule: the runner must not depend on any module.

ARCHITECTURE.md section 7.1. The registry reaches `modules/` by importing a
string at runtime, which is deliberate and is what this test allows -- a
static import from anywhere else would make the launcher depend on a module's
implementation, which is exactly what the design forbids.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "tortoise_capture"
GUARDED = ("core", "wire", "emit", "fields")


def _python_files():
    yield SRC / "cli.py"
    yield SRC / "log.py"
    yield SRC / "config.py"
    for package in GUARDED:
        yield from sorted((SRC / package).glob("*.py"))


def test_nothing_outside_modules_imports_modules_or_analyzers():
    """Both plugin packages are reached by string at runtime, never imported."""
    offenders = []
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if any(p in node.module for p in ("modules", "analyze")):
                    offenders.append(f"{path.name}: from {node.module}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if any(f"tortoise_capture.{p}" in alias.name for p in ("modules", "analyze")):
                        offenders.append(f"{path.name}: import {alias.name}")
    assert not offenders, ("the runner must not import opcode modules or analyzers: "
                           + "; ".join(offenders))


def test_only_framing_names_an_opcode_outside_modules():
    """Two bootstrap opcodes in wire/framing.py are the documented exception."""
    offenders = []
    for path in _python_files():
        if path.name == "framing.py":
            continue
        text = path.read_text(encoding="utf-8")
        for token in ("SMSG_", "CMSG_", "MSG_MOVE"):
            # A string literal naming an opcode is fine in a docstring; an
            # assignment of one to a constant is the thing worth catching.
            for line in text.splitlines():
                stripped = line.strip()
                if stripped.startswith(token) and "=" in stripped:
                    offenders.append(f"{path.name}: {stripped}")
    assert not offenders, "opcode constants outside modules/: " + "; ".join(offenders)
