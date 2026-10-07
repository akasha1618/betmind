"""Logging setup for BetMind. Enable with DEBUG=1 in .env."""

from __future__ import annotations

import logging
import os
import re
import sys

_SECRET_QUERY = re.compile(r"((?:api_?key|apikey|token|key)=)[^&\s\"']+", re.IGNORECASE)


class RedactSecretsFilter(logging.Filter):
    """httpx logheaza URL-ul complet; OddsPapi pune cheia in query string."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        redacted = _SECRET_QUERY.sub(r"\1***", message)
        if redacted != message:
            record.msg, record.args = redacted, None
        return True


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

    for handler in logging.getLogger().handlers:
        handler.addFilter(RedactSecretsFilter())

    if debug:
        os.environ.setdefault("ANTHROPIC_LOG", "debug")
        logging.getLogger("anthropic").setLevel(logging.DEBUG)
        logging.getLogger("httpx").setLevel(logging.DEBUG)

    return logging.getLogger("betmind")
