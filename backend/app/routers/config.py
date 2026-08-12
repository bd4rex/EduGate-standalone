from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from app.core import (
    _public_model_catalog,
    _validate_model_selection,
    knowledge_store,
    require_admin,
    runtime_config,
    settings,
)
from app.schemas import (
    AIEnableRequest,
    ConfigResponse,
    ModelSwitchRequest,
    ScenarioUpdateRequest,
    TeachingScenario,
)

router = APIRouter()


def _config_response(current_teacher: dict[str, Any]) -> ConfigResponse:
    sources = knowledge_store.list_sources()
    scenario = runtime_config.get_scenario("default")
    return ConfigResponse(
        scenarios={"default": scenario},
        model_catalog=_public_model_catalog(),
        litellm_base_url=settings.litellm_base_url,
        litellm_api_prefix=settings.litellm_api_prefix,
        upstream_provider=settings.upstream_provider,
        upstream_base_url=settings.upstream_base_url,
        knowledge_sources=sources,
    )


@router.get("/config", response_model=ConfigResponse)
async def get_config(current_teacher: dict[str, Any] = Depends(require_admin)) -> ConfigResponse:
    return _config_response(current_teacher)


@router.post("/config/model", response_model=ConfigResponse)
async def switch_default_model(
    request: ModelSwitchRequest,
    current_teacher: dict[str, Any] = Depends(require_admin),
) -> ConfigResponse:
    _validate_model_selection(request.model)
    runtime_config.update_scenario("default", ScenarioUpdateRequest(model=request.model))
    return _config_response(current_teacher)


@router.post("/config/ai", response_model=ConfigResponse)
async def set_ai_enabled(
    request: AIEnableRequest,
    current_teacher: dict[str, Any] = Depends(require_admin),
) -> ConfigResponse:
    runtime_config.update_scenario("default", ScenarioUpdateRequest(ai_enabled=request.enabled))
    return _config_response(current_teacher)


@router.put("/config/scenarios/{scenario_id}", response_model=TeachingScenario)
async def update_scenario(
    scenario_id: str,
    request: ScenarioUpdateRequest,
    current_teacher: dict[str, Any] = Depends(require_admin),
) -> TeachingScenario:
    if request.knowledge_source_id:
        knowledge_store.get_source(request.knowledge_source_id)
    return runtime_config.update_scenario(scenario_id, request)
