from app.litellm_client import _normalize_openai_models, _openai_url


def test_openai_url_accepts_base_or_full_chat_endpoint() -> None:
    assert _openai_url("https://api.example.com", "/chat/completions") == (
        "https://api.example.com/v1/chat/completions"
    )
    assert _openai_url("https://api.example.com/custom/v1", "/models") == (
        "https://api.example.com/custom/v1/models"
    )
    assert _openai_url(
        "https://api.example.com/custom/v1/chat/completions",
        "/chat/completions",
    ) == "https://api.example.com/custom/v1/chat/completions"
    assert _openai_url(
        "https://api.example.com/custom/v1/chat/completions",
        "/models",
    ) == "https://api.example.com/custom/v1/models"


def test_openai_model_list_is_normalized_sorted_and_deduplicated() -> None:
    assert _normalize_openai_models(
        {
            "data": [
                {"id": "qwen-plus", "owned_by": "aliyun"},
                {"model_name": "deepseek-v4-flash", "provider": "aliyun"},
                {"id": "qwen-plus", "owned_by": "updated-owner"},
                "glm-5",
                {"name": ""},
            ]
        }
    ) == [
        {"id": "deepseek-v4-flash", "owned_by": "aliyun"},
        {"id": "glm-5", "owned_by": ""},
        {"id": "qwen-plus", "owned_by": "updated-owner"},
    ]
