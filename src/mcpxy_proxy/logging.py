"""Logging setup utilities."""

from __future__ import annotations

import logging


def configure_logging(level: str = "INFO") -> None:
    """Configure basic application logging."""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
