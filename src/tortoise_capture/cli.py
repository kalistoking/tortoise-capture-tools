"""Command line entry point.

    tct key     <capture>        recover the 40-byte session key
    tct dump    <capture>        decrypt and frame the session into JSONL
    tct decode  <capture|jsonl>  run every registered module over the packets
    tct opcodes                  show the opcode table and module coverage

Nothing here knows any module: it loads the registry, hands it to the runner,
and collects the result. That is the independence rule of ARCHITECTURE.md
section 7.1 in practice.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import log as _log
from .config import CONFIG_NAME, RunConfig
from .core import pipeline, registry as registry_mod
from .core.contracts import DecodeContext, Tables
from .core.dispatch import Filters, Runner
from .emit import jsonl as jsonl_emit
from .emit.sql import SqlSink
from .emit.text import TextSink
from .fields import tables as field_tables
from .wire import crypt, opcodes as opcode_tables, pcap

EXIT_OK, EXIT_FATAL, EXIT_WITH_ERRORS = 0, 1, 2

_logger = _log.get_logger("cli")


# --------------------------------------------------------------------------
# argument parsing
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", help=f"config file (default: ./{CONFIG_NAME}); env TCT_CONFIG")
    common.add_argument("--repo", help="tortoise-wow checkout (opcode and field tables); env TCT_REPO")
    common.add_argument("--port", type=int, help="world server port of the capture (default 8090)")
    common.add_argument("--server-ip", help="server address in the capture (default 127.0.0.1)")
    common.add_argument("--log-level", choices=_log.LEVEL_NAMES,
                        help="console verbosity (default: info, or [log] level in the config)")
    common.add_argument("--debug", action="store_true", help="shortcut for --log-level debug")
    common.add_argument("--out-dir", help="where generated files go (default: out)")
    common.add_argument("--log-dir", help="where run logs go (default: logs)")
    common.add_argument("--no-log-file", action="store_true", help="console logging only")
    common.add_argument("--cache-dir", help="parsed table cache (default: .cache)")

    ap = argparse.ArgumentParser(prog="tct", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)

    p_key = sub.add_parser("key", parents=[common], help="recover the session key")
    p_key.add_argument("capture")
    p_key.add_argument("--key-only", action="store_true",
                       help="print nothing but the hex key (for scripting)")

    p_dump = sub.add_parser("dump", parents=[common], help="decrypt and frame into JSONL")
    p_dump.add_argument("capture")
    p_dump.add_argument("--out", help="output .jsonl (default: <out-dir>/<capture stem>.jsonl)")
    p_dump.add_argument("--session-key", help="hex key, skips recovery")

    p_dec = sub.add_parser("decode", parents=[common], help="run the opcode modules")
    p_dec.add_argument("source", help="a capture, or a .jsonl produced by `tct dump`")
    p_dec.add_argument("--session-key", help="hex key, skips recovery (captures only)")
    p_dec.add_argument("--format", default="text", help="text,sql,jsonl (default: text)")
    p_dec.add_argument("--entry", type=int, help="only events about this creature_template entry")
    p_dec.add_argument("--guid", type=lambda v: int(v, 0), help="only events about this wire GUID")
    p_dec.add_argument("--only", help="comma-separated module ids to decode with")
    p_dec.add_argument("--no-analyze", action="store_true",
                       help="skip the analyzers (patrol reconstruction, behaviour correlation)")
    p_dec.add_argument("--text-layout", choices=("grouped", "stream"),
                       help="default: grouped, or [output] text_layout in the config")
    p_dec.add_argument("--text-out", help="write the text report to a file instead of stdout")
    p_dec.add_argument("--sql-dialect", choices=("mysql", "sqlite"),
                       help="default: mysql, or [output] sql_dialect in the config")
    p_dec.add_argument("--report", action="store_true", help="print the coverage summary at the end")

    p_ops = sub.add_parser("opcodes", parents=[common], help="opcode table and coverage")
    p_ops.add_argument("--coverage", action="store_true", help="show which opcodes have a module")
    p_ops.add_argument("--grep", help="only opcodes whose name contains this text")

    return ap


# --------------------------------------------------------------------------
# shared setup
# --------------------------------------------------------------------------

def _load_tables(cfg: RunConfig) -> Tables:
    return Tables(opcodes=opcode_tables.load(cfg.repo, cfg.cache_dir),
                  fields=field_tables.load(cfg.repo, cfg.cache_dir))


def _context(tables: Tables, cfg: RunConfig, **options) -> DecodeContext:
    return DecodeContext(tables=tables, log=_log.get_logger("decode"), options=options)


def _session_key(session: pcap.CaptureSession, given: str | None) -> bytes | None:
    if given:
        return bytes.fromhex(given)
    return crypt.recover_session_key(session.c2s.segments)


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_key(args, cfg: RunConfig) -> int:
    session = pcap.read_session(Path(args.capture), cfg.server_ip, cfg.port)
    key = crypt.recover_session_key(session.c2s.segments)
    if key is None:
        return EXIT_WITH_ERRORS
    print(key.hex())          # stdout contract: the key, on its own line
    return EXIT_OK


def cmd_dump(args, cfg: RunConfig) -> int:
    tables = _load_tables(cfg)
    registry = registry_mod.load(tables)
    ctx = _context(tables, cfg)

    session = pcap.read_session(Path(args.capture), cfg.server_ip, cfg.port)
    key = _session_key(session, args.session_key)
    if key is None:
        return EXIT_WITH_ERRORS

    out_path = Path(args.out) if args.out else cfg.out_dir / f"{Path(args.capture).stem}.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="\n") as fp:
        sink = jsonl_emit.PacketSink(fp)
        for pkt in pipeline.packets(session, key, registry, ctx):
            sink.handle(pkt)
        sink.close()
    _logger.info("wrote %d packet record(s) to %s", sink.count, out_path)
    return EXIT_OK


def cmd_decode(args, cfg: RunConfig) -> int:
    tables = _load_tables(cfg)
    registry = registry_mod.load(tables)
    ctx = _context(tables, cfg, entry=args.entry, guid=args.guid)

    source = Path(args.source)
    stem = source.stem
    if source.suffix.lower() == ".jsonl":
        packets = jsonl_emit.read_packets(source)
    else:
        session = pcap.read_session(source, cfg.server_ip, cfg.port)
        key = _session_key(session, args.session_key)
        if key is None:
            return EXIT_WITH_ERRORS
        packets = pipeline.packets(session, key, registry, ctx)

    only = {mid.strip() for mid in args.only.split(",")} if args.only else None
    formats = {f.strip() for f in args.format.split(",") if f.strip()}
    analyzers = [] if args.no_analyze else registry_mod.load_analyzers().all()
    # Analyzers render their findings through the same sinks as modules, so the
    # text sink needs their sections too.
    modules = [m for m in registry.modules() if only is None or m.id in only] + analyzers

    open_files, sinks = [], []
    if "text" in formats:
        if args.text_out:
            Path(args.text_out).parent.mkdir(parents=True, exist_ok=True)
            handle = open(args.text_out, "w", encoding="utf-8", newline="\n")
            open_files.append(handle)
        else:
            handle = sys.stdout
        sinks.append(TextSink(handle, modules, layout=args.text_layout or cfg.text_layout,
                              highlight_entry=args.entry))
    if "sql" in formats:
        sinks.append(SqlSink(cfg.out_dir / f"{stem}.sql", capture_id=stem,
                             dialect=args.sql_dialect or cfg.sql_dialect))
    if "jsonl" in formats:
        cfg.out_dir.mkdir(parents=True, exist_ok=True)
        handle = open(cfg.out_dir / f"{stem}.events.jsonl", "w", encoding="utf-8", newline="\n")
        open_files.append(handle)
        sinks.append(jsonl_emit.EventSink(handle))
    if not sinks:
        _logger.error("no usable --format given (want text, sql or jsonl)")
        return EXIT_WITH_ERRORS

    runner = Runner(registry, ctx, sinks, Filters(entry=args.entry, guid=args.guid),
                    only=only, analyzers=analyzers)
    try:
        stats = runner.run(packets)
    finally:
        for handle in open_files:
            handle.close()

    if args.report:
        for line in stats.report(tables.opcodes):
            _logger.info("%s", line)
    return EXIT_OK


def cmd_opcodes(args, cfg: RunConfig) -> int:
    tables = _load_tables(cfg)
    registry = registry_mod.load(tables)
    coverage = registry.coverage()

    rows = sorted(tables.opcodes.by_value.items())
    if args.grep:
        needle = args.grep.upper()
        rows = [(value, name) for value, name in rows if needle in name.upper()]

    if args.coverage:
        rows = [(value, name) for value, name in rows if value in coverage]
        print(f"{len(rows)} opcode(s) covered by {len(registry)} module(s):")
    for value, name in rows:
        mods = ",".join(coverage.get(value, ()))
        print(f"0x{value:04X}  {name:<45} {mods}")
    if not args.coverage:
        print(f"\n{len(tables.opcodes)} opcodes known, {len(coverage)} covered "
              f"by {len(registry)} module(s)")
    return EXIT_OK


_COMMANDS = {"key": cmd_key, "dump": cmd_dump, "decode": cmd_decode, "opcodes": cmd_opcodes}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = RunConfig.resolve(args)
    except (OSError, ValueError) as exc:          # unreadable or malformed config file
        print(f"ERROR config: {exc}", file=sys.stderr)
        return EXIT_FATAL

    target = getattr(args, "capture", None) or getattr(args, "source", None) or args.command
    _log.setup(level=cfg.log_level, file_level=cfg.file_log_level, quiet=cfg.quiet,
               module_levels=cfg.module_levels,
               log_file=cfg.log_file(Path(target).stem),
               command_line=" ".join([sys.executable, *sys.argv]))
    # Config problems are collected before logging exists -- replay them now.
    for level, message in cfg.issues:
        _logger.log(_log.level_value(level) or 30, "%s", message)
    _logger.debug("%s", cfg.describe())

    try:
        code = _COMMANDS[args.command](args, cfg)
    except KeyboardInterrupt:
        _logger.error("interrupted")
        return EXIT_FATAL
    except Exception as exc:
        _logger.error("%s: %s", type(exc).__name__, exc)
        if cfg.log_level == "debug":
            raise
        return EXIT_FATAL

    if code == EXIT_OK and _log.error_count():
        return EXIT_WITH_ERRORS
    return code


if __name__ == "__main__":       # pragma: no cover
    sys.exit(main())
