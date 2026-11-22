"""Sanitized model-routing configuration snapshot for authenticated status APIs."""
from __future__ import annotations

import os

from . import embeddings, model_endpoint, query_rewrite, semantic_audit, weknora


def snapshot() -> dict:
    """Report routing readiness without returning URLs, keys, or model prompts."""
    semantic_error = False
    try:
        semantic = semantic_audit.load_config()
    except (TypeError, ValueError):
        semantic = None
        semantic_error = True
    rewrite = query_rewrite.config_from_env()
    weknora_error = False
    try:
        knowledge = weknora.config_from_env()
    except (TypeError, ValueError):
        knowledge = None
        weknora_error = True
    try:
        weknora_required = weknora.required()
    except (TypeError, ValueError):
        weknora_required = True
        weknora_error = True

    embedding_url = os.getenv("P0_EMBEDDING_URL", "http://127.0.0.1:11434").strip()
    embedding_enabled = embedding_url.lower() not in {"off", "none", "disabled"}
    embedding_endpoint = embedding_url

    def route(enabled: bool, configured: bool, endpoint: str, missing_state: str, invalid: bool = False) -> dict:
        allowed = model_endpoint.endpoint_allowed(endpoint)
        invalid_endpoint = bool(endpoint) and not model_endpoint.is_valid_endpoint(endpoint)
        if invalid or invalid_endpoint:
            state = "invalid_configuration"
        elif not enabled:
            state = "disabled"
        elif not configured and not endpoint:
            state = missing_state
        elif not allowed:
            state = "blocked_non_local_endpoint"
        elif not configured:
            state = missing_state
        else:
            state = "configured"
        return {
            "enabled": enabled,
            "configured": configured,
            "endpoint_allowed": allowed,
            "state": state,
            "runtime_verified": False,
        }

    weknora_route = route(
        bool(knowledge) or weknora_required or any(os.getenv(key, "").strip() for key in (
            "P0_WEKNORA_BASE_URL", "P0_WEKNORA_API_KEY", "P0_WEKNORA_KB_ID",
        )), bool(knowledge),
        knowledge.base_url if knowledge else os.getenv("P0_WEKNORA_BASE_URL", "").strip(),
        "missing_configuration", weknora_error,
    )
    weknora_route["required"] = weknora_required

    return {
        "local_only": model_endpoint.local_only(),
        "semantic_review": route(
            semantic.enabled if semantic else os.getenv("P0_SEMANTIC_ENABLED", "0") == "1",
            bool(semantic and semantic.api_key),
            semantic.base_url if semantic else os.getenv("P0_SEMANTIC_BASE_URL", "https://api.deepseek.com"),
            "missing_credentials", semantic_error,
        ),
        "query_rewrite": route(rewrite.enabled, bool(rewrite.api_key), rewrite.base_url, "missing_credentials"),
        "weknora": weknora_route,
        "embeddings": {
            "enabled": embedding_enabled,
            "endpoint_configured": model_endpoint.is_valid_endpoint(embedding_endpoint) and embedding_enabled,
            "endpoint_allowed": model_endpoint.endpoint_allowed(embedding_endpoint),
            "state": (
                "disabled" if not embedding_enabled else
                "missing_configuration" if not embedding_endpoint else
                "invalid_configuration" if not model_endpoint.is_valid_endpoint(embedding_endpoint) else
                "endpoint_configured" if model_endpoint.endpoint_allowed(embedding_endpoint) else
                "blocked_non_local_endpoint"
            ),
            "runtime_verified": False,
        },
    }
