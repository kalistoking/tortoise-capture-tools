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


PLUGINS = ("modules", "analyze")
ROOT_PACKAGE = "tortoise_capture"


def _is_plugin(parts: list[str]) -> bool:
    return len(parts) >= 2 and parts[0] == ROOT_PACKAGE and parts[1] in PLUGINS


def _package_of(path: Path) -> list[str]:
    """The package a file sits in, as the dotted parts a relative import counts from."""
    return [ROOT_PACKAGE, *path.relative_to(SRC).parts[:-1]]


def _static_plugin_imports(source: str, package: list[str]) -> list[str]:
    """Every import statement in `source` -- at the top or inside a function -- that
    names a plugin package, a relative one resolved against `package`, and a
    `from <package> import <plugin>` as much as `from <plugin> import <name>`."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            base = package[:len(package) - (node.level - 1)] if node.level else []
            where = [*base, *(node.module.split(".") if node.module else [])]
            for alias in node.names:
                if _is_plugin(where) or _is_plugin([*where, alias.name]):
                    found.append(f"from {'.' * node.level}{node.module or ''} import {alias.name}")
        elif isinstance(node, ast.Import):
            found += [f"import {alias.name}" for alias in node.names
                      if _is_plugin(alias.name.split("."))]
    return found


def _dynamic_imports(source: str) -> list[str]:
    """Calls that import by a string: importlib.import_module(...), __import__(...)."""
    return [f"{ast.unparse(node.func)}(...)" for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call)
            and ast.unparse(node.func).split(".")[-1] in ("import_module", "__import__")]


def test_the_check_sees_every_static_way_to_import_a_plugin():
    """The runner's check read `node.module` alone: `from .. import analyze` has
    none, and `from tortoise_capture import modules` has no plugin in it. An import
    inside a function was always seen (ast.walk goes into bodies); that is pinned too."""
    core = [ROOT_PACKAGE, "core"]
    for source in ("from tortoise_capture.modules.world import X",
                   "from tortoise_capture import modules",
                   "from tortoise_capture import analyze as a",
                   "from .. import analyze",
                   "from ..modules import x",
                   "from ..analyze.behaviour import y",
                   "import tortoise_capture.analyze.behaviour as b",
                   "def f():\n    from ..modules import x"):
        assert _static_plugin_imports(source, core), source
    for source in ("from .contracts import Event",
                   "from . import registry",
                   "from tortoise_capture.core import registry",
                   "from .. import log",
                   "import json",
                   "from analysis import x"):
        assert not _static_plugin_imports(source, core), source
    assert _static_plugin_imports("from .modules import x", [ROOT_PACKAGE]), "cli.py"
    assert not _static_plugin_imports("from .modules import x", core), "core/modules is not it"


def test_nothing_outside_modules_imports_modules_or_analyzers():
    """Both plugin packages are reached by string at runtime, never imported."""
    offenders = []
    for path in _python_files():
        for statement in _static_plugin_imports(path.read_text(encoding="utf-8"),
                                                _package_of(path)):
            offenders.append(f"{path.name}: {statement}")
    assert not offenders, ("the runner must not import opcode modules or analyzers: "
                           + "; ".join(offenders))


def test_only_the_registry_imports_by_a_string():
    """The registry's string import is the one door to the plugins; another file
    calling importlib or __import__ could open a second one the check above cannot see."""
    callers = {}
    for path in _python_files():
        if calls := _dynamic_imports(path.read_text(encoding="utf-8")):
            callers[path.name] = calls
    assert set(callers) == {"registry.py"}, f"imports by a string outside the registry: {callers}"


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
