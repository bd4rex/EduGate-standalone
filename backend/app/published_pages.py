from __future__ import annotations

import json
import mimetypes
import os
import re
import shutil
import stat
import threading
import time
import uuid
import zipfile
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any

from fastapi import HTTPException, status


PAGE_ID_PATTERN = re.compile(r"page-[a-f0-9]{16}")
ALLOWED_ASSET_SUFFIXES = {
    ".css",
    ".js",
    ".json",
    ".txt",
    ".csv",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".ico",
    ".woff",
    ".woff2",
    ".ttf",
    ".otf",
    ".mp3",
    ".wav",
    ".mp4",
    ".webm",
}
MAX_ARCHIVE_FILES = 1000
MAX_EXPANDED_MULTIPLIER = 4


class PublishedPageStore:
    def __init__(self, root: str | Path, *, max_upload_bytes: int) -> None:
        self.root = Path(root)
        self.index_path = self.root / "index.json"
        self.max_upload_bytes = max_upload_bytes
        self._lock = threading.RLock()

    def list_pages(self) -> dict[str, Any]:
        with self._lock:
            index = self._load_index_locked()
            active_id = index.get("active_page_id")
            pages = sorted(index.get("pages", []), key=lambda page: page.get("created_at", 0), reverse=True)
            return {
                "active_page_id": active_id,
                "pages": [{**page, "active": page.get("id") == active_id} for page in pages],
            }

    def publish(self, *, filename: str, title: str, content: bytes, activate: bool = True) -> dict[str, Any]:
        if not content:
            raise HTTPException(status_code=400, detail="网页文件不能为空。")
        if len(content) > self.max_upload_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"网页文件超过 {self.max_upload_bytes} 字节上限。",
            )
        safe_filename = Path(filename or "page.html").name
        suffix = Path(safe_filename).suffix.lower()
        if suffix not in {".html", ".htm", ".zip"}:
            raise HTTPException(status_code=400, detail="仅支持单个 HTML 文件或包含 index.html 的 ZIP。")

        page_id = f"page-{uuid.uuid4().hex[:16]}"
        self.root.mkdir(parents=True, exist_ok=True)
        staging = self.root / f".staging-{page_id}"
        target = self.root / page_id
        staging.mkdir()
        try:
            if suffix == ".zip":
                self._extract_zip(content, staging)
            else:
                self._validate_html(content)
                (staging / "index.html").write_bytes(content)
            index_html = staging / "index.html"
            self._validate_html(index_html.read_bytes())
            files = [path for path in staging.rglob("*") if path.is_file()]
            page = {
                "id": page_id,
                "title": self._clean_title(title, safe_filename),
                "filename": safe_filename,
                "created_at": int(time.time()),
                "asset_count": max(0, len(files) - 1),
                "size_bytes": sum(path.stat().st_size for path in files),
            }
            os.replace(staging, target)
            with self._lock:
                index = self._load_index_locked()
                index.setdefault("pages", []).append(page)
                if activate:
                    index["active_page_id"] = page_id
                self._save_index_locked(index)
            return {**page, "active": activate}
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            raise

    def activate(self, page_id: str | None) -> dict[str, Any]:
        with self._lock:
            index = self._load_index_locked()
            if page_id is not None and not any(page.get("id") == page_id for page in index.get("pages", [])):
                raise HTTPException(status_code=404, detail="找不到已发布网页。")
            index["active_page_id"] = page_id
            self._save_index_locked(index)
        return self.list_pages()

    def delete(self, page_id: str) -> dict[str, Any]:
        with self._lock:
            index = self._load_index_locked()
            pages = index.get("pages", [])
            if not any(page.get("id") == page_id for page in pages):
                raise HTTPException(status_code=404, detail="找不到已发布网页。")
            index["pages"] = [page for page in pages if page.get("id") != page_id]
            if index.get("active_page_id") == page_id:
                index["active_page_id"] = None
            self._save_index_locked(index)
            target = self._page_dir(page_id)
            if target.exists():
                shutil.rmtree(target)
        return self.list_pages()

    def get_active_document(self, page_id: str) -> dict[str, str]:
        with self._lock:
            index = self._load_index_locked()
            if index.get("active_page_id") != page_id:
                raise HTTPException(status_code=404, detail="该网页当前未发布到学生入口。")
            page = next((item for item in index.get("pages", []) if item.get("id") == page_id), None)
            if page is None:
                raise HTTPException(status_code=404, detail="找不到已发布网页。")
            document = self._page_dir(page_id) / "index.html"
            if not document.is_file():
                raise HTTPException(status_code=404, detail="已发布网页缺少 index.html。")
            return {
                "id": page_id,
                "title": str(page.get("title") or "课堂网页"),
                "html": document.read_text(encoding="utf-8-sig"),
            }

    def asset_path(self, page_id: str, asset_path: str) -> tuple[Path, str]:
        page_dir = self._page_dir(page_id).resolve()
        candidate = (page_dir / asset_path).resolve()
        try:
            relative = candidate.relative_to(page_dir)
        except ValueError as error:
            raise HTTPException(status_code=404, detail="找不到网页资源。") from error
        if not candidate.is_file() or relative.as_posix().lower() == "index.html":
            raise HTTPException(status_code=404, detail="找不到网页资源。")
        if candidate.suffix.lower() not in ALLOWED_ASSET_SUFFIXES:
            raise HTTPException(status_code=404, detail="不支持该网页资源类型。")
        media_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        return candidate, media_type

    def _page_dir(self, page_id: str) -> Path:
        if PAGE_ID_PATTERN.fullmatch(page_id) is None:
            raise HTTPException(status_code=404, detail="找不到已发布网页。")
        return self.root / page_id

    def _load_index_locked(self) -> dict[str, Any]:
        if not self.index_path.exists():
            return {"version": 1, "active_page_id": None, "pages": []}
        try:
            payload = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise HTTPException(status_code=500, detail="网页发布索引损坏。") from error
        if not isinstance(payload, dict) or not isinstance(payload.get("pages", []), list):
            raise HTTPException(status_code=500, detail="网页发布索引格式无效。")
        return payload

    def _save_index_locked(self, payload: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        temp = self.index_path.with_suffix(".tmp")
        serialized = json.dumps(payload, ensure_ascii=False, indent=2)
        with temp.open("w", encoding="utf-8") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, self.index_path)

    def _extract_zip(self, content: bytes, staging: Path) -> None:
        try:
            archive = zipfile.ZipFile(BytesIO(content))
        except zipfile.BadZipFile as error:
            raise HTTPException(status_code=400, detail="网页 ZIP 文件无效。") from error
        with archive:
            members = [
                member
                for member in archive.infolist()
                if not member.is_dir()
                and not member.filename.startswith("__MACOSX/")
                and not member.filename.endswith("/.DS_Store")
                and not member.filename.endswith(".DS_Store")
            ]
            if not members or len(members) > MAX_ARCHIVE_FILES:
                raise HTTPException(status_code=400, detail="网页 ZIP 文件为空或文件数量过多。")
            if any(member.flag_bits & 0x1 for member in members):
                raise HTTPException(status_code=400, detail="不支持加密 ZIP。")
            if any(stat.S_ISLNK(member.external_attr >> 16) for member in members):
                raise HTTPException(status_code=400, detail="网页 ZIP 不允许包含符号链接。")
            expanded_size = sum(member.file_size for member in members)
            if expanded_size > self.max_upload_bytes * MAX_EXPANDED_MULTIPLIER:
                raise HTTPException(status_code=400, detail="网页 ZIP 解压后体积过大。")

            paths = [self._safe_archive_path(member.filename) for member in members]
            index_paths = [path for path in paths if path.name.lower() == "index.html"]
            if len(index_paths) != 1:
                raise HTTPException(status_code=400, detail="网页 ZIP 必须且只能包含一个 index.html。")
            prefix = index_paths[0].parent
            if prefix != PurePosixPath(".") and not all(path.is_relative_to(prefix) for path in paths):
                raise HTTPException(status_code=400, detail="index.html 必须位于 ZIP 根目录或唯一的顶层文件夹中。")

            for member, archive_path in zip(members, paths, strict=True):
                relative = archive_path.relative_to(prefix) if prefix != PurePosixPath(".") else archive_path
                suffix = relative.suffix.lower()
                if relative.name.lower() == "index.html":
                    pass
                elif suffix in {".html", ".htm"} or suffix not in ALLOWED_ASSET_SUFFIXES:
                    raise HTTPException(status_code=400, detail=f"不支持的网页资源：{relative.as_posix()}")
                target = staging.joinpath(*relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, target.open("wb") as destination:
                    shutil.copyfileobj(source, destination)

    @staticmethod
    def _safe_archive_path(value: str) -> PurePosixPath:
        path = PurePosixPath(value.replace("\\", "/"))
        forbidden = '<>:"|?*'
        windows_reserved = {
            "CON",
            "PRN",
            "AUX",
            "NUL",
            *(f"COM{index}" for index in range(1, 10)),
            *(f"LPT{index}" for index in range(1, 10)),
        }
        if (
            path.is_absolute()
            or ".." in path.parts
            or len(path.as_posix()) > 240
            or any(not part or part in {".", ".."} for part in path.parts)
            or any(any(character in forbidden or ord(character) < 32 for character in part) for part in path.parts)
            or any(part.endswith((" ", ".")) for part in path.parts)
            or any(part.split(".", 1)[0].upper() in windows_reserved for part in path.parts)
        ):
            raise HTTPException(status_code=400, detail=f"网页 ZIP 包含不安全路径：{value}")
        return path

    @staticmethod
    def _validate_html(content: bytes) -> None:
        try:
            html = content.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise HTTPException(status_code=400, detail="HTML 必须使用 UTF-8 编码。") from error
        if "<html" not in html.lower() and "<!doctype html" not in html.lower():
            raise HTTPException(status_code=400, detail="上传内容不是完整 HTML 页面。")

    @staticmethod
    def _clean_title(title: str, filename: str) -> str:
        cleaned = " ".join((title or "").split())[:120]
        return cleaned or Path(filename).stem[:120] or "课堂网页"
