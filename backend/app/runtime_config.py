from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from fastapi import HTTPException, status

from app.config import settings
from app.secret_store import SecretStore
from app.schemas import (
    ModelCatalogItem,
    RuntimeConfigData,
    ScenarioUpdateRequest,
    TeachingScenario,
    _provider_catalog_id,
)


class RuntimeConfig:
    def __init__(self, path: str, *, secret_store: SecretStore | None = None) -> None:
        self._path = Path(path)
        self._secret_store = secret_store or SecretStore(settings.secret_store_path, mode=settings.secret_store_mode)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._legacy_teacher_policy_migrated = False
        self.data = self._load()
        self._migrate_plaintext_secrets()
        needs_save = not self.data.legacy_runtime_migration_complete or self._legacy_teacher_policy_migrated
        self._ensure_standalone_default_model()
        if needs_save:
            self.data.legacy_runtime_migration_complete = True
            self.save()
        self._migrate_model_identities()

    def _load(self) -> RuntimeConfigData:
        if not self._path.exists():
            return RuntimeConfigData()
        raw = json.loads(self._path.read_text(encoding="utf-8"))
        legacy_policies = raw.pop("teacher_policies", {})
        data = RuntimeConfigData.model_validate(raw)
        admin_policy = legacy_policies.get(settings.admin_username.strip().lower())
        if admin_policy:
            data.scenarios["default"] = TeachingScenario.model_validate(admin_policy)
            self._legacy_teacher_policy_migrated = True
        return data

    def _ensure_standalone_default_model(self) -> None:
        if settings.deployment_mode != "standalone":
            return
        if not settings.upstream_base_url and not settings.upstream_api_key:
            return
        current = self.data.model_catalog.get(settings.default_model)
        if current is not None:
            if current.description == "Local classroom default upstream model. Edit base_url and api_key before live use.":
                self.data.model_catalog[settings.default_model] = current.model_copy(
                    update={"description": "本地课堂默认上游模型，上课前请填写接口地址和 API 密钥。"}
                )
            return
        credential_id = f"model:{settings.default_model}"
        if settings.upstream_api_key:
            self._secret_store.set(credential_id, settings.upstream_api_key)
        self.data.model_catalog[settings.default_model] = ModelCatalogItem(
            id=settings.default_model,
            name=settings.default_model,
            provider=settings.upstream_provider,
            description="本地课堂默认上游模型，上课前请填写接口地址和 API 密钥。",
            source="openai_compatible",
            base_url=settings.upstream_base_url or None,
            credential_id=credential_id,
        )

    def _migrate_plaintext_secrets(self) -> None:
        changed = False
        for model_id, model in list(self.data.model_catalog.items()):
            credential_id = model.credential_id or f"model:{model_id}"
            if model.api_key:
                self._secret_store.set(credential_id, model.api_key)
                changed = True
            if model.credential_id != credential_id or model.api_key is not None:
                self.data.model_catalog[model_id] = model.model_copy(
                    update={"credential_id": credential_id, "api_key": None}
                )
                changed = True
        if changed:
            self.save()

    def _migrate_model_identities(self) -> None:
        changed = False
        for model_id, model in list(self.data.model_catalog.items()):
            provider_id = model.provider_id or _provider_catalog_id(model.provider, model.base_url)
            upstream_model_id = model.upstream_model_id or model.id
            if model.provider_id != provider_id or model.upstream_model_id != upstream_model_id:
                self.data.model_catalog[model_id] = model.model_copy(
                    update={
                        "provider_id": provider_id,
                        "upstream_model_id": upstream_model_id,
                    }
                )
                changed = True
        if changed:
            self.save()

    def save(self) -> None:
        with self._lock:
            temp = self._path.with_suffix(self._path.suffix + ".tmp")
            payload = self.data.model_dump_json(indent=2)
            with temp.open("w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self._path)

    def get_scenario(self, scenario_id: str) -> TeachingScenario:
        scenario = self.data.scenarios.get(scenario_id)
        if scenario is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Unknown teaching scenario: {scenario_id}",
            )
        return scenario

    def update_scenario(self, scenario_id: str, request: ScenarioUpdateRequest) -> TeachingScenario:
        with self._lock:
            current = self.data.scenarios.get(scenario_id, TeachingScenario())
            changes = request.model_dump(exclude_unset=True)
            updated = current.model_copy(update=changes)
            self.data.scenarios[scenario_id] = updated
            self.save()
            return updated

    def upsert_model(self, request: ModelCatalogItem) -> ModelCatalogItem:
        if request.source == "openai_compatible" and not request.base_url:
            raise HTTPException(status_code=400, detail="base_url is required for OpenAI-compatible models")
        request = request.model_copy(
            update={
                "provider_id": request.provider_id or _provider_catalog_id(request.provider, request.base_url),
                "upstream_model_id": request.upstream_model_id or request.id,
            }
        )
        with self._lock:
            current = self.data.model_catalog.get(request.id)
            credential_id = (current.credential_id if current else None) or f"model:{request.id}"
            if request.api_key:
                self._secret_store.set(credential_id, request.api_key)
            request = request.model_copy(update={"credential_id": credential_id, "api_key": None})
            self.data.model_catalog[request.id] = request
            self.save()
            return request

    def upsert_models(self, requests: list[ModelCatalogItem]) -> list[ModelCatalogItem]:
        for request in requests:
            if request.source == "openai_compatible" and not request.base_url:
                raise HTTPException(status_code=400, detail="base_url is required for OpenAI-compatible models")
        with self._lock:
            models: list[ModelCatalogItem] = []
            for request in requests:
                request = request.model_copy(
                    update={
                        "provider_id": request.provider_id or _provider_catalog_id(request.provider, request.base_url),
                        "upstream_model_id": request.upstream_model_id or request.id,
                    }
                )
                current = self.data.model_catalog.get(request.id)
                credential_id = (current.credential_id if current else None) or f"model:{request.id}"
                if request.api_key:
                    self._secret_store.set(credential_id, request.api_key)
                model = request.model_copy(update={"credential_id": credential_id, "api_key": None})
                self.data.model_catalog[model.id] = model
                models.append(model)
            self.save()
            return models

    def find_provider_model(self, provider_id: str, upstream_model_id: str) -> ModelCatalogItem | None:
        normalized_upstream_id = upstream_model_id.strip()
        with self._lock:
            return next(
                (
                    model
                    for model in self.data.model_catalog.values()
                    if model.provider_id == provider_id
                    and (model.upstream_model_id or model.id) == normalized_upstream_id
                ),
                None,
            )

    def delete_model(self, model_id: str, *, replacement_model_id: str | None = None) -> list[str]:
        with self._lock:
            model = self.data.model_catalog.get(model_id)
            if model is None:
                return []
            references = [
                scenario_id
                for scenario_id, scenario in self.data.scenarios.items()
                if scenario.model == model_id
            ]
            if references and not replacement_model_id:
                raise HTTPException(
                    status_code=409,
                    detail="该模型仍被课堂配置使用。请先选择或导入另一个可用模型，再删除。",
                )
            if references:
                replacement = self.data.model_catalog.get(replacement_model_id or "")
                if replacement is None or replacement.id == model_id:
                    raise HTTPException(status_code=400, detail="替代模型不存在或与待删除模型相同。")
                for scenario_id, scenario in list(self.data.scenarios.items()):
                    if scenario.model == model_id:
                        self.data.scenarios[scenario_id] = scenario.model_copy(
                            update={"model": replacement.id}
                        )
            self.data.model_catalog.pop(model_id)
            self.save()
            self._secret_store.delete(model.credential_id)
            return references

    def delete_provider(
        self,
        provider_id: str,
        *,
        replacement_model_id: str | None = None,
    ) -> tuple[list[str], list[str]]:
        with self._lock:
            provider_models = [
                model
                for model in self.data.model_catalog.values()
                if model.source == "openai_compatible"
                and (model.provider_id or _provider_catalog_id(model.provider, model.base_url)) == provider_id
            ]
            if not provider_models:
                return [], []

            model_ids = {model.id for model in provider_models}
            references = [
                scenario_id
                for scenario_id, scenario in self.data.scenarios.items()
                if scenario.model in model_ids
            ]
            replacement = None
            if replacement_model_id:
                replacement = self.data.model_catalog.get(replacement_model_id)
                if replacement is None or replacement.id in model_ids:
                    raise HTTPException(
                        status_code=400,
                        detail="替代模型不存在，或仍属于待删除的供应商。",
                    )
            if references and replacement is None:
                raise HTTPException(
                    status_code=409,
                    detail="该供应商仍有模型被课堂配置使用。请先添加其他供应商的可用模型，再删除。",
                )
            if references and replacement is not None:
                for scenario_id, scenario in list(self.data.scenarios.items()):
                    if scenario.model in model_ids:
                        self.data.scenarios[scenario_id] = scenario.model_copy(
                            update={"model": replacement.id}
                        )
            credential_ids = {model.credential_id for model in provider_models if model.credential_id}
            for model_id in model_ids:
                self.data.model_catalog.pop(model_id, None)
            self.save()
            for credential_id in credential_ids:
                self._secret_store.delete(credential_id)
            return sorted(model_ids), references
