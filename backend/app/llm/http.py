"""One JSON POST with our own error classification (no SDK retries: we own HTTP)."""

from collections.abc import Callable, Mapping
from typing import Any

import httpx

from app.llm.errors import LLMError, TransientError, network_error

Classifier = Callable[[int, Mapping[str, str], Any], LLMError]

DEFAULT_TIMEOUT_S = 60.0


def post_json(
    client: httpx.Client,
    provider: str,
    url: str,
    *,
    headers: dict[str, str],
    body: dict[str, Any],
    classify: Classifier,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> dict[str, Any]:
    try:
        response = client.post(url, headers=headers, json=body, timeout=timeout_s)
    except httpx.HTTPError as exc:
        raise network_error(provider, exc) from None
    if response.status_code != 200:
        try:
            error_body: Any = response.json()
        except ValueError:
            error_body = None
        raise classify(response.status_code, response.headers, error_body)
    try:
        payload = response.json()
    except ValueError:
        raise TransientError(provider, status=200, cause="invalid_body") from None
    if not isinstance(payload, dict):
        raise TransientError(provider, status=200, cause="invalid_body")
    return payload
