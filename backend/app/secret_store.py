from __future__ import annotations

import base64
import ctypes
import json
import os
import threading
from ctypes import wintypes
from pathlib import Path


class SecretStore:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._data = self._load()

    def set(self, key: str, value: str) -> None:
        with self._lock:
            self._data[key] = _protect(value.encode("utf-8"))
            self._save_locked()

    def get(self, key: str | None) -> str | None:
        if not key:
            return None
        with self._lock:
            encoded = self._data.get(key)
        if not encoded:
            return None
        return _unprotect(encoded).decode("utf-8")

    def has(self, key: str | None) -> bool:
        with self._lock:
            return bool(key and key in self._data)

    def delete(self, key: str | None) -> None:
        if not key:
            return
        with self._lock:
            if self._data.pop(key, None) is not None:
                self._save_locked()

    def _load(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {str(key): str(value) for key, value in data.items()}

    def _save_locked(self) -> None:
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = json.dumps(self._data, ensure_ascii=False, indent=2)
        with temp.open("w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, self.path)


if os.name == "nt":
    class _DataBlob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


    def _protect(raw: bytes) -> str:
        buffer = ctypes.create_string_buffer(raw)
        input_blob = _DataBlob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
        output_blob = _DataBlob()
        if not ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(input_blob), None, None, None, None, 0, ctypes.byref(output_blob)
        ):
            raise ctypes.WinError()
        try:
            encrypted = ctypes.string_at(output_blob.pbData, output_blob.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(output_blob.pbData)
        return base64.b64encode(encrypted).decode("ascii")


    def _unprotect(encoded: str) -> bytes:
        encrypted = base64.b64decode(encoded.encode("ascii"))
        buffer = ctypes.create_string_buffer(encrypted)
        input_blob = _DataBlob(len(encrypted), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
        output_blob = _DataBlob()
        if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(input_blob), None, None, None, None, 0, ctypes.byref(output_blob)
        ):
            raise ctypes.WinError()
        try:
            return ctypes.string_at(output_blob.pbData, output_blob.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(output_blob.pbData)
else:
    def _protect(raw: bytes) -> str:
        raise RuntimeError("EduGate secret storage requires Windows DPAPI")


    def _unprotect(encoded: str) -> bytes:
        raise RuntimeError("EduGate secret storage requires Windows DPAPI")
