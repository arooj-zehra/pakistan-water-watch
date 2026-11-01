"""Superseded by backfill_news, which handles APP and every other outlet.

    python -m pipeline.backfill_app    # same as: python -m pipeline.backfill_news --outlets app
"""
from __future__ import annotations

import sys

from .backfill_news import main as _news_main


def main(argv: list[str] | None = None) -> int:
    return _news_main(["--outlets", "app"] + list(argv or []))


if __name__ == "__main__":
    sys.exit(main())
