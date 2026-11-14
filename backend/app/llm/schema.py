"""JSON-schema helpers for providers with a strict structured-output mode."""

import copy
from typing import Any


def strict_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Return a copy that strict modes accept (Groq strict json_schema, Anthropic).

    Strict decoding requires every object to set `additionalProperties: false` and to list
    every property as required. Optional Pydantic fields stay nullable (`anyOf ... null`), so
    the model must output them, possibly as null, and the Pydantic validation is unchanged.
    `default` is dropped: strict modes do not take it.
    """
    result = copy.deepcopy(schema)
    _close(result)
    return result


def _close(node: Any) -> None:
    if isinstance(node, list):
        for item in node:
            _close(item)
        return
    if not isinstance(node, dict):
        return
    node.pop("default", None)
    if node.get("type") == "object" and isinstance(node.get("properties"), dict):
        node["additionalProperties"] = False
        node["required"] = list(node["properties"])
    for value in node.values():
        _close(value)
