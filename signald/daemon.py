"""Daemon supervision: single-instance PID lock + heartbeat (plan §4.1).

Only one daemon per data directory (a reference bot once ran two instances
that traded against each other in dev). The lock file is created O_EXCL; a
stale lock (dead PID) is taken over. A heartbeat file is touched every cycle
so a separate watchdog process can page on ``heartbeat_loss`` (M1).
"""

from __future__ import annotations

import os
import time
from pathlib import Path

_LOCK_TIMEOUT_S = 5.0


class AlreadyRunning(RuntimeError):
    pass


class DaemonLock:
    def __init__(self, pid_file: str | Path) -> None:
        self.pid_file = Path(pid_file)
        self._held = False

    def acquire(self) -> None:
        self.pid_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(str(self.pid_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            pid = self._read_pid()
            if pid and _pid_alive(pid):
                raise AlreadyRunning(
                    f"signald already running (pid {pid}) — lock {self.pid_file}"
                ) from None
            # stale lock: dead process -> take over
            self.pid_file.unlink(missing_ok=True)
            fd = os.open(str(self.pid_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(f"{os.getpid()}\n")
        self._held = True

    def release(self) -> None:
        if self._held:
            self.pid_file.unlink(missing_ok=True)
            self._held = False

    def _read_pid(self) -> int | None:
        try:
            txt = self.pid_file.read_text(encoding="utf-8").strip()
            return int(txt) if txt else None
        except (OSError, ValueError):
            return None

    def __enter__(self) -> DaemonLock:
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:  # noqa: ANN002
        self.release()


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def touch_heartbeat(path: str | Path) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(str(int(time.time())), encoding="utf-8")
