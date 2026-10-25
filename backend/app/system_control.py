from __future__ import annotations

import threading
import time
from collections.abc import Callable


class SystemControl:
    def __init__(self) -> None:
        self.started_at = time.time()
        self._callback: Callable[[str], None] | None = None
        self._prepare: Callable[[str], None] | None = None
        self.pending_action: str | None = None
        self.shutdown_complete = False
        self.shutdown_error = ""
        self._lock = threading.RLock()

    def bind(self, callback: Callable[[str], None], prepare: Callable[[str], None] | None = None) -> None:
        with self._lock:
            self._callback = callback
            self._prepare = prepare
            self.pending_action = None
            self.shutdown_complete = False
            self.shutdown_error = ""

    def unbind(self) -> None:
        with self._lock:
            self._callback = None
            self._prepare = None
            self.pending_action = None

    def finish_shutdown(self, error: str = "") -> None:
        self.shutdown_error = error or self.shutdown_error
        self.shutdown_complete = not bool(self.shutdown_error)

    @property
    def supervised(self) -> bool:
        with self._lock:
            return self._callback is not None

    def request(self, action: str) -> bool:
        with self._lock:
            callback = self._callback
            if callback is None or self.pending_action:
                return False
            self.pending_action = action
        try:
            if self._prepare:
                self._prepare(action)
        except Exception:
            self.pending_action = None
            raise
        threading.Timer(0.5, callback, args=(action,)).start()
        return True


system_control = SystemControl()
