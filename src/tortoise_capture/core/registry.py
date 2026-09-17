"""The registry: opcode -> module lookup, and module discovery.

Adding support for an opcode means dropping a file into `modules/` with a
`@module(...)` decorator on it. Nothing else in the codebase names it. The
registry imports that package *by string* at runtime (never a static import),
which is what keeps the independence rule of ARCHITECTURE.md section 7.1
mechanically true.

Modules declare opcodes by symbol (`"SMSG_MONSTER_MOVE"`). Numbers are
resolved against the table parsed from the server checkout, so a fork that
renumbers an opcode keeps working, and a symbol that no longer exists is
reported instead of silently decoding the wrong payload.
"""

from __future__ import annotations

import importlib
import pkgutil
from dataclasses import dataclass, field
from typing import Any, Iterator, Sequence

from .. import log as _log
from .contracts import Tables

MODULES_PACKAGE = "tortoise_capture.modules"  # imported dynamically, never statically
ANALYZE_PACKAGE = "tortoise_capture.analyze"  # likewise
AUTHOR_PACKAGE = "tortoise_capture.author"    # likewise

_logger = _log.get_logger("registry")


def _import_submodules(package: str, logger) -> int:
    """Imports every submodule so its decorators run. Discovery, by string."""
    pkg = importlib.import_module(package)
    found = 0
    for info in pkgutil.iter_modules(pkg.__path__):
        if info.name.startswith("_"):
            continue
        importlib.import_module(f"{package}.{info.name}")
        found += 1
    logger.debug("discovered %d file(s) in %s", found, package)
    return found


@dataclass(slots=True)
class Registration:
    id: str
    opcodes: tuple[str | int, ...]
    order: int
    instance: Any
    resolved: tuple[int, ...] = field(default_factory=tuple)


class Registry:
    """Holds every known module and the opcode -> module mapping."""

    def __init__(self) -> None:
        self._regs: dict[str, Registration] = {}
        self._by_opcode: dict[int, list[Registration]] = {}
        self._resolved = False

    # -- registration ------------------------------------------------------

    def add(self, reg: Registration) -> None:
        if reg.id in self._regs:
            raise ValueError(f"duplicate module id {reg.id!r}")
        self._regs[reg.id] = reg
        self._resolved = False

    def discover(self, package: str = MODULES_PACKAGE) -> int:
        """Imports every submodule of `package`; decorators do the rest."""
        return _import_submodules(package, _logger)

    # -- resolution --------------------------------------------------------

    def resolve(self, tables: Tables) -> None:
        """Turns declared opcode symbols into numbers. Idempotent."""
        self._by_opcode.clear()
        for reg in self._ordered():
            numbers: list[int] = []
            for decl in reg.opcodes:
                if isinstance(decl, int):
                    numbers.append(decl)
                    continue
                number = tables.opcodes.by_name.get(decl)
                if number is None:
                    _logger.warning(
                        "module %s declares opcode %s, which this checkout does not define "
                        "-- that opcode stays uncovered", reg.id, decl)
                    continue
                numbers.append(number)
            reg.resolved = tuple(numbers)
            for number in numbers:
                self._by_opcode.setdefault(number, []).append(reg)
        self._resolved = True
        _logger.debug("resolved %d module(s) onto %d opcode(s)", len(self._regs), len(self._by_opcode))

    def _ordered(self) -> list[Registration]:
        return sorted(self._regs.values(), key=lambda r: (r.order, r.id))

    # -- lookup ------------------------------------------------------------

    def for_opcode(self, opcode: int) -> Sequence[Any]:
        """The modules handling this opcode, in section order. Usually 0 or 1."""
        return [reg.instance for reg in self._by_opcode.get(opcode, ())]

    def modules(self) -> list[Any]:
        return [reg.instance for reg in self._ordered()]

    def registrations(self) -> list[Registration]:
        return self._ordered()

    def coverage(self) -> dict[int, list[str]]:
        """opcode number -> ids of the modules covering it."""
        return {op: [r.id for r in regs] for op, regs in sorted(self._by_opcode.items())}

    def __len__(self) -> int:
        return len(self._regs)

    def __iter__(self) -> Iterator[Registration]:
        return iter(self._ordered())


class AnalyzerRegistry:
    """Analyzers in declared order. No opcode key -- every one sees everything.

    Kept apart from the opcode Registry because the lookup is different in
    kind: modules are selected by what a packet is, analyzers all run.
    """

    def __init__(self) -> None:
        self._regs: dict[str, tuple[int, Any]] = {}

    def add(self, id: str, order: int, instance: Any) -> None:
        if id in self._regs:
            raise ValueError(f"duplicate analyzer id {id!r}")
        self._regs[id] = (order, instance)

    def discover(self, package: str = ANALYZE_PACKAGE) -> int:
        return _import_submodules(package, _logger)

    def all(self) -> list[Any]:
        return [inst for _, (order, inst) in sorted(self._regs.items(), key=lambda kv: (kv[1][0], kv[0]))]

    def __len__(self) -> int:
        return len(self._regs)


REGISTRY = Registry()
ANALYZERS = AnalyzerRegistry()
AUTHOR_RULES = AnalyzerRegistry()      # same shape: everything registered, in order


def module(*, id: str, opcodes: Sequence[str | int], order: int = 100):
    """Class decorator that registers one opcode module.

    `order` only controls where the module's section appears in grouped text
    output; it has no effect on decoding.
    """

    def decorate(cls):
        cls.id = id
        cls.opcodes = tuple(opcodes)
        cls.order = order
        REGISTRY.add(Registration(id=id, opcodes=tuple(opcodes), order=order, instance=cls()))
        return cls

    return decorate


def analyzer(*, id: str, order: int = 100):
    """Class decorator that registers one analyzer.

    `order` fixes the order findings appear in, nothing else: analyzers are
    independent and never see each other's output.
    """

    def decorate(cls):
        cls.id = id
        cls.order = order
        ANALYZERS.add(id, order, cls())
        return cls

    return decorate


def load(tables: Tables, package: str = MODULES_PACKAGE) -> Registry:
    """Discovers modules once and resolves them against the opcode table."""
    if not REGISTRY._regs:
        REGISTRY.discover(package)
    REGISTRY.resolve(tables)
    return REGISTRY


def author_rule(*, id: str, table: str, order: int = 100):
    """Class decorator that registers one authoring rule.

    `order` is the section order in the generated migration, which must
    satisfy the target tables' own dependencies -- scripts before the events
    that reference them, and so on.
    """

    def decorate(cls):
        cls.id = id
        cls.table = table
        cls.order = order
        AUTHOR_RULES.add(id, order, cls())
        return cls

    return decorate


def load_analyzers(package: str = ANALYZE_PACKAGE) -> AnalyzerRegistry:
    """Discovers analyzers once."""
    if not len(ANALYZERS):
        ANALYZERS.discover(package)
    return ANALYZERS


def load_author_rules(package: str = AUTHOR_PACKAGE) -> AnalyzerRegistry:
    """Discovers authoring rules once."""
    if not len(AUTHOR_RULES):
        AUTHOR_RULES.discover(package)
    return AUTHOR_RULES
