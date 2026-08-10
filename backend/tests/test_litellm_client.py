from app.litellm_client import _openai_url


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
