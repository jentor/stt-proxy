#!/usr/bin/env python
"""Print how the current .env would configure the proxy."""

from __future__ import annotations

import sys

from app.config import Settings


def main() -> int:
    try:
        s = Settings()
    except SystemExit:
        return 2

    print(f"  yandex_enabled: {s.yandex_enabled}")
    print(f"  salute_enabled: {s.salute_enabled}")
    print(f"  providers:      {s.enabled_providers}")
    print(f"  default:        {s.effective_default_provider()}")
    print(f"  host:port:      {s.host}:{s.port}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
