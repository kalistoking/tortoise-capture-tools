"""Command line entry point.

    tct key     <capture>        recover the 40-byte session key
    tct dump    <capture>        decrypt and frame the session into JSONL
    tct decode  <capture|jsonl>  run every registered module over the packets
    tct author  <capture|jsonl>  propose world-database rows for one creature
    tct opcodes                  show the opcode table and module coverage

Nothing here knows any module: it loads the registry, hands it to the runner,
and collects the result. That is the independence rule of ARCHITECTURE.md
section 7.1 in practice.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import log as _log
from .config import CONFIG_NAME, RunConfig
from .core import pipeline, registry as registry_mod
from .core.contracts import AuthorContext, DecodeContext, Tables
from .core.dispatch import Filters, Runner
from . import dbc, world as world_db
from .author import existing
from .emit import jsonl as jsonl_emit
from .emit.author_json import AuthorJsonWriter, author_json_name
from .emit.migration import MigrationWriter, migration_name
from .emit.sql import SqlSink
from .emit.text import TextSink
from .fields import tables as field_tables
from .wire import crypt, opcodes as opcode_tables, pcap, slim

EXIT_OK, EXIT_FATAL, EXIT_WITH_ERRORS = 0, 1, 2

_logger = _log.get_logger("cli")


# --------------------------------------------------------------------------
# argument parsing
# --------------------------------------------------------------------------

_NO_SLIM = ("do not leave a slim copy (only the WoW conversation) beside a capture "
            "that holds more")


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", help=f"config file (default: ./{CONFIG_NAME}); env TCT_CONFIG")
    common.add_argument("--repo", help="tortoise-wow checkout (opcode and field tables); env TCT_REPO")
    common.add_argument("--port", type=int, help="world server port of the capture (default 8090)")
    common.add_argument("--server-ip", help="server address in the capture (default 127.0.0.1)")
    common.add_argument("--logon-port", type=int,
                        help="logon server port of the capture (default 3724); env TCT_LOGON_PORT")
    common.add_argument("--logon-ip", help="logon server address (default: any); env TCT_LOGON_IP")
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
    p_dump.add_argument("--no-slim", action="store_true", help=_NO_SLIM)
    p_dump.add_argument("--out", help="output .jsonl (default: <out-dir>/<capture stem>.jsonl)")
    p_dump.add_argument("--session-key", help="hex key, skips recovery")

    p_dec = sub.add_parser("decode", parents=[common], help="run the opcode modules")
    p_dec.add_argument("source", help="a capture, or a .jsonl produced by `tct dump`")
    p_dec.add_argument("--session-key", help="hex key, skips recovery (captures only)")
    p_dec.add_argument("--no-slim", action="store_true", help=_NO_SLIM)
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

    p_auth = sub.add_parser("author", parents=[common],
                            help="propose world-database rows for one creature")
    p_auth.add_argument("source", help="a capture, or a .jsonl produced by `tct dump`")
    p_auth.add_argument("--entry", type=int, required=True,
                        help="the creature_template entry to author")
    p_auth.add_argument("--session-key", help="hex key, skips recovery (captures only)")
    p_auth.add_argument("--no-slim", action="store_true", help=_NO_SLIM)
    p_auth.add_argument("--out", help="output file (default: <out-dir>/<timestamp>_world.<ext>)")
    p_auth.add_argument("--format", choices=("sql", "json"), default="sql",
                        help="sql (commented migration) or json (default: sql)")
    p_auth.add_argument("--no-db", action="store_true",
                        help="skip database lookups and diffing even if one is configured")

    p_slim = sub.add_parser("slim", parents=[common],
                            help="keep only the WoW conversation of a capture, beside it")
    p_slim.add_argument("capture")
    p_slim.add_argument("--out", help="output capture (default: <capture stem>.wow<suffix>, beside it)")
    p_slim.add_argument("--replace", action="store_true",
                        help="replace the original with the verified slim copy -- it is the only "
                             "copy of a session that cannot be recorded again")
    p_slim.add_argument("--session-key", help="hex key, skips recovery")

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


def _packet_source(args, cfg: RunConfig, registry, ctx):
    """Packets from a dump or straight from a capture. (None, stem) if no key."""
    source = Path(args.source)
    if source.suffix.lower() == ".jsonl":
        return jsonl_emit.read_packets(source), source.stem
    session = pcap.read_session(source, *_world(cfg, source))
    key = _session_key(session, args.session_key)
    if key is None:
        return None, source.stem
    _slim_beside(source, session, key, cfg, registry, ctx, args)
    return pipeline.packets(session, key, registry, ctx), source.stem


def _logon(cfg: RunConfig) -> slim.Endpoint:
    return slim.Endpoint(cfg.logon_ip, cfg.logon_port)


def _named_world(cfg: RunConfig) -> slim.Endpoint | None:
    """The world server as named, or None to read it from the realm list -- a
    default port is not a name, and must not win over what the capture says."""
    if "port" not in cfg.named:
        return None
    return slim.Endpoint(cfg.server_ip if "server_ip" in cfg.named else None, cfg.port)


def _world(cfg: RunConfig, capture: Path) -> tuple[str, int]:
    """The world server to decode: as named; else as the capture's own realm
    list names it, so a server off 8090 needs no --port; else the default."""
    if "port" in cfg.named:
        return cfg.server_ip, cfg.port
    try:
        return slim.world_server(capture, _logon(cfg))
    except slim.SlimError as exc:
        _logger.debug("world server not read from the capture (%s); using %s:%d",
                      exc, cfg.server_ip, cfg.port)
        return cfg.server_ip, cfg.port


def _decoded(session, key, registry, ctx) -> list[tuple]:
    return [(p.seq, p.t, p.direction, p.opcode, p.name, p.body, p.via)
            for p in pipeline.packets(session, key, registry, ctx)]


def _write_verified(plan: slim.SlimPlan, out: Path, session, key, registry, ctx,
                    failed=_logger.error) -> bool:
    """Writes the slim copy, and keeps it only if it decodes exactly as the
    original does: the same session key, the same packet records, times included.
    `failed` reports a copy that does not; an error unless the copy was a
    by-product of some other command."""
    partial = out.with_name(out.name + ".partial")
    slim.write(plan, partial)
    try:
        copy = pcap.read_session(partial, *plan.world_server)
        same_key = (crypt.recover_session_key(copy.c2s.segments)
                    == crypt.recover_session_key(session.c2s.segments))
        original, slimmed = _decoded(session, key, registry, ctx), _decoded(copy, key, registry, ctx)
        if not same_key or original != slimmed:
            failed("the slim copy of %s does not decode as the original does "
                   "(%d packet record(s) against %d); not kept",
                   plan.source, len(slimmed), len(original))
            partial.unlink()
            return False
        os.replace(partial, out)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    _logger.info("wrote %s (%d bytes), which decodes identically: %d packet record(s), "
                 "the same session key", out, plan.kept_bytes, len(original))
    return True


def _slim_beside(capture: Path, session, key, cfg: RunConfig, registry, ctx, args) -> None:
    """Leaves a verified slim copy beside a capture that holds more than its WoW
    conversation -- once, and never touching the original. The copy is a
    by-product: nothing about it may cost the command its real work."""
    out = slim.slim_name(capture)
    if getattr(args, "no_slim", False) or capture.stem.endswith(".wow") or out.exists():
        return
    try:
        plan = slim.plan(capture, _logon(cfg), slim.Endpoint(*session.server))
    except slim.SlimError as exc:
        _logger.warning("not slimmed: %s", exc)
        return
    if not plan.has_foreign:
        return
    for line in slim.describe(plan):
        _logger.info("%s", line)
    try:
        _write_verified(plan, out, session, key, registry, ctx, failed=_logger.warning)
    except OSError as exc:
        _logger.warning("slim copy not written beside %s: %s", capture, exc)


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_key(args, cfg: RunConfig) -> int:
    session = pcap.read_session(Path(args.capture), *_world(cfg, Path(args.capture)))
    key = crypt.recover_session_key(session.c2s.segments)
    if key is None:
        return EXIT_WITH_ERRORS
    print(key.hex())          # stdout contract: the key, on its own line
    return EXIT_OK


def cmd_dump(args, cfg: RunConfig) -> int:
    tables = _load_tables(cfg)
    registry = registry_mod.load(tables)
    ctx = _context(tables, cfg)

    session = pcap.read_session(Path(args.capture), *_world(cfg, Path(args.capture)))
    key = _session_key(session, args.session_key)
    if key is None:
        return EXIT_WITH_ERRORS
    _slim_beside(Path(args.capture), session, key, cfg, registry, ctx, args)

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

    packets, stem = _packet_source(args, cfg, registry, ctx)
    if packets is None:
        return EXIT_WITH_ERRORS

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


def cmd_author(args, cfg: RunConfig) -> int:
    """Proposes world rows for one creature -- a file to review, never applied.

    Authoring rules are sinks, so they see the decoded events and the analyzer
    findings in the same stream the text and SQL outputs see. That is why this
    command is the decode pipeline with a different set of sinks, not a
    separate traversal of the capture.
    """
    tables = _load_tables(cfg)
    registry = registry_mod.load(tables)
    ctx = _context(tables, cfg, entry=args.entry)

    packets, stem = _packet_source(args, cfg, registry, ctx)
    if packets is None:
        return EXIT_WITH_ERRORS

    world = None if args.no_db else world_db.from_config(cfg.database)
    # Only a database's stored scale of 0 needs the model's, so no database, no read.
    displays = dbc.display_scales(cfg.dbc_dir) if world is not None else None
    rules = registry_mod.load_author_rules().all()
    analyzers = registry_mod.load_analyzers().all()

    runner = Runner(registry, ctx, rules, Filters(entry=args.entry), analyzers=analyzers)
    stats = runner.run(packets)

    as_json = args.format == "json"
    out_path = Path(args.out) if args.out else cfg.out_dir / (
        author_json_name() if as_json else migration_name())
    if as_json:
        writer = AuthorJsonWriter(out_path, capture_id=stem, entry=args.entry)
    else:
        writer = MigrationWriter(out_path, capture_id=stem, entry=args.entry,
                                 dialect=cfg.sql_dialect)
    author_ctx = AuthorContext(capture_id=stem, entry=args.entry,
                               log=_log.get_logger("author"), world=world,
                               displays=displays, fields=tables.fields)
    for rule in rules:
        try:
            # Rows first: a rule can learn what to report while producing them.
            proposed, held = existing.only_new(rule.rows(author_ctx), world)
            writer.add(proposed)
            writer.add_gaps(rule.gaps(author_ctx))
            writer.add_gaps(held)
        except Exception as exc:
            _logger.error("authoring rule %s failed: %s: %s",
                          rule.id, type(exc).__name__, exc)

    if writer.write() is None:
        _logger.warning("entry %d produced no rows -- was it in this capture? "
                        "(%d event(s) matched)", args.entry, stats.emitted)
        return EXIT_WITH_ERRORS
    _logger.info("review the file before applying it to any database")
    return EXIT_OK


def cmd_slim(args, cfg: RunConfig) -> int:
    capture = Path(args.capture)
    try:
        plan = slim.plan(capture, _logon(cfg), _named_world(cfg))
    except slim.SlimError as exc:
        _logger.error("%s", exc)
        return EXIT_WITH_ERRORS
    for line in slim.describe(plan):
        _logger.info("%s", line)
    if not plan.has_foreign:
        _logger.info("%s holds nothing but the WoW conversation; nothing to drop", capture)
        return EXIT_OK

    out = slim.slim_name(capture) if args.replace or not args.out else Path(args.out)
    if out.exists():
        _logger.error("%s already exists and is not overwritten", out)
        return EXIT_WITH_ERRORS
    tables = _load_tables(cfg)
    registry = registry_mod.load(tables)
    ctx = _context(tables, cfg)
    session = pcap.read_session(capture, *plan.world_server)
    key = _session_key(session, args.session_key)
    if key is None or not _write_verified(plan, out, session, key, registry, ctx):
        return EXIT_WITH_ERRORS
    if args.replace:
        os.replace(out, capture)
        _logger.info("replaced %s with its slim copy, as asked", capture)
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


_COMMANDS = {"key": cmd_key, "dump": cmd_dump, "decode": cmd_decode,
             "author": cmd_author, "slim": cmd_slim, "opcodes": cmd_opcodes}


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
