from __future__ import annotations

from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException

from app.core import (
    _discover_provider_models,
    _public_model,
    _validate_model_selection,
    client,
    langfuse,
    require_admin,
    runtime_config,
    secret_store,
    settings,
)
from app.routers.config import _config_response
from app.schemas import (
    ConfigResponse,
    ModelBatchImportRequest,
    ModelCatalogItem,
    ModelCatalogPublicItem,
    ModelProviderConnectionRequest,
    ScenarioUpdateRequest,
    _model_catalog_id,
    _provider_catalog_id,
)


router = APIRouter()


@router.get("/admin/models", response_model=list[ModelCatalogPublicItem], dependencies=[Depends(require_admin)])
async def admin_models() -> list[ModelCatalogPublicItem]:
    return [_public_model(model) for model in runtime_config.data.model_catalog.values()]


@router.post("/admin/models/discover", dependencies=[Depends(require_admin)])
async def admin_discover_models(request: ModelProviderConnectionRequest) -> dict[str, Any]:
    models, _, used_saved_key = await _discover_provider_models(request)
    provider_id = request.provider_id or _provider_catalog_id(request.provider, request.base_url)
    return {
        "models": models,
        "model_count": len(models),
        "provider_id": provider_id,
        "used_saved_api_key": used_saved_key,
    }


@router.post("/admin/models/batch-import", dependencies=[Depends(require_admin)])
async def admin_batch_import_models(request: ModelBatchImportRequest) -> dict[str, Any]:
    discovered, api_key, used_saved_key = await _discover_provider_models(request)
    available_ids = {item["id"] for item in discovered}
    selected_ids = list(dict.fromkeys(model_id.strip() for model_id in request.model_ids if model_id.strip()))
    if not selected_ids:
        raise HTTPException(status_code=400, detail="请至少选择一个要导入的模型。")
    unknown_ids = [model_id for model_id in selected_ids if model_id not in available_ids]
    if unknown_ids:
        raise HTTPException(
            status_code=400,
            detail=f"上游模型列表中不存在：{', '.join(unknown_ids[:10])}",
        )
    description = request.description.strip() or "从上游 /models 批量导入"
    provider_id = request.provider_id or _provider_catalog_id(request.provider, request.base_url)
    model_requests: list[ModelCatalogItem] = []
    for upstream_model_id in selected_ids:
        existing = runtime_config.find_provider_model(provider_id, upstream_model_id)
        model_requests.append(
            ModelCatalogItem(
                id=existing.id if existing else _model_catalog_id(provider_id, upstream_model_id),
                name=(
                    " ".join(request.display_names.get(upstream_model_id, "").split())[:120]
                    or upstream_model_id
                ),
                provider=request.provider.strip(),
                provider_id=provider_id,
                upstream_model_id=upstream_model_id,
                description=description,
                source="openai_compatible",
                base_url=request.base_url.strip(),
                api_key=api_key,
            )
        )
    models = runtime_config.upsert_models(model_requests)
    return {
        "status": "imported",
        "imported_count": len(models),
        "provider_id": provider_id,
        "used_saved_api_key": used_saved_key,
        "models": [_public_model(model).model_dump() for model in models],
    }


@router.post("/admin/models", response_model=ModelCatalogPublicItem, dependencies=[Depends(require_admin)])
async def admin_upsert_model(request: ModelCatalogItem) -> ModelCatalogPublicItem:
    return _public_model(runtime_config.upsert_model(request))


@router.patch("/admin/models/{model_id}", response_model=ModelCatalogPublicItem, dependencies=[Depends(require_admin)])
async def admin_patch_model(model_id: str, request: ModelCatalogItem) -> ModelCatalogPublicItem:
    current = runtime_config.data.model_catalog.get(model_id)
    if current is None:
        raise HTTPException(status_code=404, detail=f"找不到模型：{model_id}")
    return _public_model(
        runtime_config.upsert_model(
            request.model_copy(
                update={
                    "id": model_id,
                    "provider_id": current.provider_id,
                    "upstream_model_id": current.upstream_model_id or current.id,
                }
            )
        )
    )


@router.post("/admin/models/{model_id}/set-default", response_model=ConfigResponse, dependencies=[Depends(require_admin)])
async def admin_set_default_model(
    model_id: str,
    current_teacher: dict[str, Any] = Depends(require_admin),
) -> ConfigResponse:
    _validate_model_selection(model_id)
    runtime_config.update_scenario("default", ScenarioUpdateRequest(model=model_id))
    return _config_response(current_teacher)


@router.get("/admin/providers", dependencies=[Depends(require_admin)])
async def admin_providers() -> list[dict[str, Any]]:
    if settings.deployment_mode == "standalone":
        direct_models = [
            model for model in runtime_config.data.model_catalog.values()
            if model.source == "openai_compatible"
        ]
        provider_groups: dict[str, list[ModelCatalogItem]] = {}
        for model in direct_models:
            provider_id = model.provider_id or _provider_catalog_id(model.provider, model.base_url)
            provider_groups.setdefault(provider_id, []).append(model)
        providers = []
        for provider_id, models_for_provider in provider_groups.items():
            configured_count = sum(
                1
                for model in models_for_provider
                if model.base_url and secret_store.has(model.credential_id)
            )
            representative = models_for_provider[0]
            providers.append(
                {
                    "id": provider_id,
                    "name": representative.provider,
                    "status": "configured" if configured_count else "needs_configuration",
                    "base_url": representative.base_url,
                    "model_count": len(models_for_provider),
                    "configured_model_count": configured_count,
                }
            )
        providers.sort(key=lambda item: (item["name"].casefold(), item["id"]))
        return [
            *providers,
            {
                "id": "langfuse",
                "name": "langfuse",
                "status": "configured" if langfuse.enabled else "not_configured",
                "base_url": settings.langfuse_base_url,
            },
        ]
    try:
        data = await client.list_models()
        available = True
        model_count = len(data.get("data", []))
    except httpx.HTTPError:
        available = False
        model_count = 0
    return [
        {
            "name": "litellm",
            "status": "online" if available else "offline",
            "model_count": model_count,
            "base_url": settings.litellm_base_url,
        },
        {
            "name": "langfuse",
            "status": "configured" if langfuse.enabled else "not_configured",
            "base_url": settings.langfuse_base_url,
        },
    ]


@router.delete("/admin/providers/{provider_id}", dependencies=[Depends(require_admin)])
async def admin_delete_provider(
    provider_id: str,
    replacement_model_id: str | None = None,
) -> dict[str, Any]:
    deleted_model_ids, references = runtime_config.delete_provider(
        provider_id,
        replacement_model_id=replacement_model_id,
    )
    if not deleted_model_ids:
        raise HTTPException(status_code=404, detail=f"找不到供应商：{provider_id}")
    return {
        "status": "deleted",
        "provider_id": provider_id,
        "deleted_model_ids": deleted_model_ids,
        "deleted_model_count": len(deleted_model_ids),
        "replacement_model_id": replacement_model_id if references else None,
        "replaced_references": references,
    }


@router.post("/admin/providers/{name}/test", dependencies=[Depends(require_admin)])
async def admin_test_provider(name: str) -> dict[str, Any]:
    if settings.deployment_mode == "standalone":
        model = runtime_config.data.model_catalog.get(name)
        if model is None:
            model = next(
                (
                    item
                    for item in runtime_config.data.model_catalog.values()
                    if item.provider_id == name
                ),
                None,
            )
        if name.lower() == "openai_compatible":
            direct_models = [
                item for item in runtime_config.data.model_catalog.values()
                if item.source == "openai_compatible"
            ]
            configured = [
                item.id for item in direct_models
                if item.base_url and secret_store.has(item.credential_id)
            ]
            return {
                "name": name,
                "ok": bool(configured),
                "configured_models": configured,
                "model_count": len(direct_models),
            }
        if model and model.source == "openai_compatible":
            api_key = secret_store.get(model.credential_id)
            if not model.base_url or not api_key:
                return {"name": name, "ok": False, "error": "Base URL or API Key is missing"}
            try:
                result = await client.probe_openai_provider(base_url=model.base_url, api_key=api_key)
                return {"name": name, "base_url": model.base_url, **result}
            except httpx.HTTPStatusError as error:
                return {
                    "name": name,
                    "ok": False,
                    "status_code": error.response.status_code,
                    "error": error.response.text[:500] or error.response.reason_phrase,
                }
            except httpx.TimeoutException:
                return {"name": name, "ok": False, "error": "Provider request timed out"}
            except httpx.HTTPError as error:
                return {"name": name, "ok": False, "error": str(error)}
        if name.lower() == "langfuse":
            return {"name": name, "ok": langfuse.enabled}
        raise HTTPException(status_code=404, detail=f"Unknown provider or model: {name}")
    if name.lower() == "litellm":
        try:
            data = await client.list_models()
            return {"name": name, "ok": True, "model_count": len(data.get("data", []))}
        except httpx.HTTPError as error:
            return {"name": name, "ok": False, "error": str(error)}
    if name.lower() == "langfuse":
        return {"name": name, "ok": langfuse.enabled}
    raise HTTPException(status_code=404, detail=f"Unknown provider: {name}")

@router.delete("/admin/models/{model_id}", dependencies=[Depends(require_admin)])
async def delete_model_catalog_item(
    model_id: str,
    replacement_model_id: str | None = None,
) -> dict[str, Any]:
    if model_id not in runtime_config.data.model_catalog:
        raise HTTPException(status_code=404, detail=f"找不到模型：{model_id}")
    references = runtime_config.delete_model(
        model_id,
        replacement_model_id=replacement_model_id,
    )
    return {
        "status": "deleted",
        "replacement_model_id": replacement_model_id if references else None,
        "replaced_references": references,
    }
