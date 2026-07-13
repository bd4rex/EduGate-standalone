from __future__ import annotations

import threading
import time
from collections.abc import Callable


class SystemControl:
    def __init__(self) -> None:
        self.started_at = time.time()
        self._callback: Callable[[str], None] | None = None
        self._lock = threading.RLock()

    def bind(self, callback: Callable[[str], None]) -> None:
        with self._lock:
            self._callback = callback

    def unbind(self) -> None:
        with self._lock:
            self._callback = None

    @property
    def supervised(self) -> bool:
        with self._lock:
            return self._callback is not None

    def request(self, action: str) -> bool:
        with self._lock:
            callback = self._callback
        if callback is None:
            return False
        threading.Timer(0.5, callback, args=(action,)).start()
        return True


system_control = SystemControl()
