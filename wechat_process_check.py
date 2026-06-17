#!/usr/bin/env python3
"""Check whether supported WeChat process names are visible.

This does not read process memory or WeChat databases. It only lists process
IDs found by the Windows tasklist-based detector.
"""

from __future__ import annotations

import json
import sys


def main() -> int:
    try:
        from find_all_keys_windows import get_pids

        pids = get_pids()
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps({"ok": True, "pids": pids}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
