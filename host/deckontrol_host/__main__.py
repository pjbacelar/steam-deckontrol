"""CLI entry point: ``python -m deckontrol_host`` or the ``deckontrol-host`` script."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from . import __version__
from .config import HostConfig
from .daemon import run


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="deckontrol-host",
        description="Receive Steam Deck input over the network and inject a virtual controller.",
    )
    parser.add_argument("--port", type=int, help="control/input UDP port")
    parser.add_argument("--name", help="advertised host name")
    parser.add_argument("--token", help="override the pairing token")
    parser.add_argument("--no-mouse", action="store_true", help="disable trackpad-as-mouse")
    parser.add_argument("--print-token", action="store_true", help="print the pairing token and exit")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)

    cfg = HostConfig.load()
    if args.port:
        cfg.port = args.port
    if args.name:
        cfg.name = args.name
    if args.token:
        cfg.token = args.token
    if args.no_mouse:
        cfg.enable_mouse = False

    if args.print_token:
        print(cfg.token)
        return 0

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    try:
        asyncio.run(run(cfg))
    except KeyboardInterrupt:
        return 0
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
