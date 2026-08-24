from __future__ import annotations

import zipfile
from io import BytesIO
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.published_pages import PublishedPageStore


def _zip(entries: dict[str, bytes | str]) -> bytes:
    output = BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return output.getvalue()


def test_single_html_can_be_published_activated_and_deleted(tmp_path: Path) -> None:
    store = PublishedPageStore(tmp_path / "pages", max_upload_bytes=1024 * 1024)

    published = store.publish(
        filename="lesson.html",
        title="  光的   反射  ",
        content=b"<!doctype html><html><body>lesson</body></html>",
    )

    assert published["active"] is True
    assert published["title"] == "光的 反射"
    assert store.list_pages()["active_page_id"] == published["id"]
    assert store.get_active_document(published["id"])["html"].endswith("</html>")

    store.activate(None)
    with pytest.raises(HTTPException, match="当前未发布"):
        store.get_active_document(published["id"])
    assert store.delete(published["id"])["pages"] == []


def test_zip_page_supports_one_top_folder_and_safe_assets(tmp_path: Path) -> None:
    store = PublishedPageStore(tmp_path / "pages", max_upload_bytes=1024 * 1024)
    content = _zip({
        "lesson/index.html": "<!doctype html><html><link rel='stylesheet' href='style.css'></html>",
        "lesson/style.css": "body { color: navy; }",
        "lesson/app.js": "window.lessonReady = true;",
    })

    published = store.publish(filename="lesson.zip", title="网页课件", content=content)
    asset, media_type = store.asset_path(published["id"], "style.css")

    assert asset.read_text(encoding="utf-8") == "body { color: navy; }"
    assert media_type == "text/css"
    assert published["asset_count"] == 2


@pytest.mark.parametrize(
    ("entries", "message"),
    [
        ({"../index.html": "<!doctype html><html></html>"}, "不安全路径"),
        ({"index.html": "<!doctype html><html></html>", "second.html": "<html></html>"}, "不支持的网页资源"),
        ({"index.html": "<!doctype html><html></html>", "icon.svg": "<svg></svg>"}, "不支持的网页资源"),
        ({"index.html": "<!doctype html><html></html>", "NUL.css": "body{}"}, "不安全路径"),
        ({"readme.txt": "missing"}, "必须且只能包含一个 index.html"),
    ],
)
def test_zip_page_rejects_unsafe_or_executable_content(
    tmp_path: Path,
    entries: dict[str, str],
    message: str,
) -> None:
    store = PublishedPageStore(tmp_path / "pages", max_upload_bytes=1024 * 1024)

    with pytest.raises(HTTPException, match=message):
        store.publish(filename="unsafe.zip", title="unsafe", content=_zip(entries))
