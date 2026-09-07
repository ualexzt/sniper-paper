from __future__ import annotations

import argparse
from pathlib import Path

from .app import run


def main() -> None:
    parser = argparse.ArgumentParser(description="Public-data-only Bybit paper strategy")
    parser.add_argument("--database", type=Path, default=Path("runtime/paper.db"))
    parser.add_argument("--dashboard-host", default="127.0.0.1")
    parser.add_argument("--dashboard-port", type=int, default=8080)
    parser.add_argument("--allow-nonloopback-dashboard", action="store_true")
    args = parser.parse_args()
    run(
        args.database,
        args.dashboard_host,
        args.dashboard_port,
        allow_nonloopback_dashboard=args.allow_nonloopback_dashboard,
    )


if __name__ == "__main__":
    main()
