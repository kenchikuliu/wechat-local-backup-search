#!/usr/bin/env python3
"""Background backup agent for local WeChat backup/search."""

from __future__ import annotations

import os
import signal
import sys
import threading
import time

import backup_search_app as core


def _stop_handler(_signum, _frame):
    core._append_log("background agent stop requested")
    core.scheduler_stop.set()


def _heartbeat_loop():
    while not core.scheduler_stop.wait(15):
        core._write_background_state(
            pid=os.getpid(),
            mode="background",
            status="running",
            heartbeat_at=core._now_text(),
        )


def main() -> int:
    core._ensure_dirs()
    core.scheduler_stop.clear()
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _stop_handler)
    signal.signal(signal.SIGINT, _stop_handler)
    core._write_background_state(
        pid=os.getpid(),
        mode="background",
        started_at=core._now_text(),
        status="running",
    )
    core._append_log("background agent started")
    threading.Thread(target=_heartbeat_loop, daemon=True).start()
    try:
        core.scheduler_loop()
    except Exception as exc:
        core._append_log(f"background agent failed: {exc}")
        core._write_background_state(
            pid=os.getpid(),
            mode="background",
            status="failed",
            error=str(exc),
        )
        raise
    finally:
        core._append_log("background agent stopped")
        core._clear_background_state()
    return 0


if __name__ == "__main__":
    sys.exit(main())
