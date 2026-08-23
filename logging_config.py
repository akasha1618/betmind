"""Logging setup for BetMind. Enable with DEBUG=1 in .env."""

from __future__ import annotations

import logging
import os
import sys


def setup_logging() -> logging.Logger:
    debug = os.environ.get("DEBUG", "").strip().lower() in ("1", "true", "yes")
    level = logging.DEBUG if debug else logging.INFO

    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
        force=True,
    )

    if debug:
        os.environ.setdefault("ANTHROPIC_LOG", "debug")
        logging.getLogger("anthropic").setLevel(logging.DEBUG)
        logging.getLogger("httpx").setLevel(logging.DEBUG)

    return logging.getLogger("betmind")
