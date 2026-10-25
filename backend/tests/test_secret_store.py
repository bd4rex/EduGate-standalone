from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from app.secret_store import SecretStore


def test_portable_secret_store_survives_copying_the_folder(tmp_path: Path) -> None:
    original = tmp_path / "original" / "data" / "secrets.json"
    copied = tmp_path / "copied" / "data" / "secrets.json"
    store = SecretStore(str(original), mode="portable")
    store.set("model:deepseek", "sk-portable-classroom")

    payload = json.loads(original.read_text(encoding="utf-8"))
    assert payload["format"] == "portable-plain-v1"
    assert payload["secrets"]["model:deepseek"] == "sk-portable-classroom"
    if os.name != "nt":
        assert original.stat().st_mode & 0o777 == 0o600

    copied.parent.mkdir(parents=True)
    shutil.copy2(original, copied)
    assert SecretStore(str(copied), mode="portable").get("model:deepseek") == "sk-portable-classroom"


def test_portable_secret_store_ignores_malformed_top_level_data(tmp_path: Path) -> None:
    path = tmp_path / "secrets.json"
    path.write_text("[]", encoding="utf-8")

    assert SecretStore(str(path), mode="portable").get("missing") is None


def test_portable_secret_store_has_and_delete_lifecycle(tmp_path: Path) -> None:
    path = tmp_path / "secrets.json"
    store = SecretStore(str(path), mode="portable")

    assert store.get(None) is None
    assert store.has("model:test") is False
    store.set("model:test", "temporary-secret")
    assert store.has("model:test") is True
    store.delete(None)
    store.delete("missing")
    store.delete("model:test")

    assert store.has("model:test") is False
    assert store.get("model:test") is None


def test_portable_secret_store_ignores_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "secrets.json"
    path.write_text("{invalid", encoding="utf-8")

    assert SecretStore(str(path), mode="portable").get("missing") is None


def test_default_platform_key_is_short_persistent_and_respects_existing_values(tmp_path: Path) -> None:
    path = str(tmp_path / "secrets.json")
    store = SecretStore(path, mode="portable")
    store.ensure_platform_key()
    original = store.get("system:platform_api_key")
    assert original and original.startswith("eg_") and len(original) == 25
    reloaded = SecretStore(path, mode="portable")
    reloaded.ensure_platform_key("changed-environment-key")
    assert reloaded.get("system:platform_api_key") == original
    for existing in ("my-existing-long-key-must-not-be-shortened", ""):
        reloaded.set("system:platform_api_key", existing)
        reloaded = SecretStore(path, mode="portable")
        reloaded.ensure_platform_key("environment-key")
        assert reloaded.get("system:platform_api_key") == (existing or None)

    other = SecretStore(str(tmp_path / "other.json"), mode="portable")
    other.ensure_platform_key("environment-key")
    assert other.get("system:platform_api_key") == "environment-key"
