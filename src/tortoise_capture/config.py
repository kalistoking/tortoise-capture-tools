"""Run configuration, resolved once from CLI arguments and environment.

Everything below this module reads a RunConfig; nothing reads os.environ or
argparse directly, so a run is reproducible from one printable object.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ENV_REPO = "TCT_REPO"          # tortoise-wow checkout, for opcode/field tables
ENV_PORT = "TCT_PORT"          # world server port of the recorded session
ENV_SERVER_IP = "TCT_SERVER_IP"

DEFAULT_PORT = 8090            # this fork's WorldServerPort
DEFAULT_SERVER_IP = "127.0.0.1"


@dataclass(frozen=True, slots=True)
class RunConfig:
    repo: Path | None                 # tortoise-wow checkout; None -> numeric-only tables
    port: int = DEFAULT_PORT
    server_ip: str = DEFAULT_SERVER_IP
    debug: bool = False
    out_dir: Path = field(default_factory=lambda: Path("out"))
    log_dir: Path | None = field(default_factory=lambda: Path("logs"))
    cache_dir: Path = field(default_factory=lambda: Path(".cache"))

    @classmethod
    def from_args(cls, args) -> "RunConfig":
        repo = args.repo or os.environ.get(ENV_REPO)
        port = args.port or os.environ.get(ENV_PORT) or DEFAULT_PORT
        server_ip = args.server_ip or os.environ.get(ENV_SERVER_IP) or DEFAULT_SERVER_IP
        return cls(
            repo=Path(repo) if repo else None,
            port=int(port),
            server_ip=server_ip,
            debug=bool(args.debug),
            out_dir=Path(args.out_dir),
            log_dir=None if args.no_log_file else Path(args.log_dir),
            cache_dir=Path(args.cache_dir),
        )

    def log_file(self, stem: str) -> Path | None:
        return None if self.log_dir is None else self.log_dir / f"{stem}.log"
