"""Provider-agnostic LLM gateway (phase 2 amendment): Gemini, Groq, optional Anthropic."""

from app.llm.errors import (
    AuthError,
    InvalidRequest,
    LLMError,
    QuotaExhausted,
    QuotaUnavailable,
    RateLimited,
    SchemaError,
    TransientError,
)
from app.llm.gateway import GatewayConfigError, LLMGateway, build_gateway, mark_awaiting_quota
from app.llm.ledger import InMemoryLedger, Ledger, LLMCall, SqlLedger
from app.llm.provider import Provider
from app.llm.types import (
    Limits,
    StructuredResult,
    ToolCall,
    ToolCallRecord,
    ToolLoopResult,
    ToolSpec,
    Usage,
)

__all__ = [
    "AuthError",
    "GatewayConfigError",
    "InMemoryLedger",
    "InvalidRequest",
    "LLMCall",
    "LLMError",
    "LLMGateway",
    "Ledger",
    "Limits",
    "Provider",
    "QuotaExhausted",
    "QuotaUnavailable",
    "RateLimited",
    "SchemaError",
    "SqlLedger",
    "StructuredResult",
    "ToolCall",
    "ToolCallRecord",
    "ToolLoopResult",
    "ToolSpec",
    "TransientError",
    "Usage",
    "build_gateway",
    "mark_awaiting_quota",
]
