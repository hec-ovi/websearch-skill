"""Detached-process session control shared by the local bring-ups (SearXNG, Tor).

Both servers are spawned in their own session with a pidfile beside their state, so
liveness and shutdown are one rule: a pid whose process answers signal 0 is alive, and
stopping signals the whole process group so worker children die with the parent.
"""

from __future__ import annotations

import os
import signal
import time
from pathlib import Path


def running_pid(pidfile: Path) -> int | None:
    """The pid from the pidfile when that process is still alive, else None."""
    try:
        pid = int(pidfile.read_text().strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        return pid
    return pid


def stop(pidfile: Path, timeout_s: float = 10.0) -> bool:
    """Signal the whole session and wait for it to go. False when nothing was running."""
    pid = running_pid(pidfile)
    if pid is None:
        pidfile.unlink(missing_ok=True)
        return False
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(os.getpgid(pid), sig)
        except (ProcessLookupError, PermissionError):
            break
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if running_pid(pidfile) is None:
                pidfile.unlink(missing_ok=True)
                return True
            time.sleep(0.2)
    pidfile.unlink(missing_ok=True)
    return True
