"""The call ledger (llm_calls) and quota state (budget_state).

llm_calls is the source of truth for quota and spend (phase 3, "Data model"): one row per
provider request, success or failure. The SQL ledger writes each row in its own short
transaction, so a row survives whatever the caller's transaction does next.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from sqlalchemy import Engine, text


@dataclass(frozen=True)
class LLMCall:
    provider: str
    model: str
    purpose: str
    status: Literal["ok", "error"]
    created_at: datetime
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int | None = None
    error_class: str | None = None
    invoice_id: str | None = None
    demo_token_hash: str | None = None


class Ledger(Protocol):
    def record(self, call: LLMCall) -> None: ...

    def count_calls(
        self,
        *,
        since: datetime,
        until: datetime,
        provider: str | None = None,
        demo_token_hash: str | None = None,
    ) -> int:
        """Rows with since <= created_at < until, filtered by provider and/or demo token."""
        ...

    def exhausted_until(self, provider: str) -> datetime | None: ...

    def set_exhausted_until(self, provider: str, until: datetime) -> None: ...


class InMemoryLedger:
    """For unit tests and local experiments."""

    def __init__(self) -> None:
        self.calls: list[LLMCall] = []
        self.budget: dict[str, datetime] = {}

    def record(self, call: LLMCall) -> None:
        self.calls.append(call)

    def count_calls(
        self,
        *,
        since: datetime,
        until: datetime,
        provider: str | None = None,
        demo_token_hash: str | None = None,
    ) -> int:
        return sum(
            1
            for call in self.calls
            if since <= call.created_at < until
            and (provider is None or call.provider == provider)
            and (demo_token_hash is None or call.demo_token_hash == demo_token_hash)
        )

    def exhausted_until(self, provider: str) -> datetime | None:
        return self.budget.get(provider)

    def set_exhausted_until(self, provider: str, until: datetime) -> None:
        self.budget[provider] = until


class SqlLedger:
    def __init__(self, engine: Engine):
        self._engine = engine

    def record(self, call: LLMCall) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO llm_calls (invoice_id, provider, model, purpose, input_tokens, "
                    "output_tokens, latency_ms, status, error_class, demo_token_hash, created_at) "
                    "VALUES (CAST(:invoice_id AS uuid), :provider, :model, :purpose, "
                    ":input_tokens, :output_tokens, :latency_ms, :status, :error_class, "
                    ":demo_token_hash, :created_at)"
                ),
                {
                    "invoice_id": call.invoice_id,
                    "provider": call.provider,
                    "model": call.model,
                    "purpose": call.purpose,
                    "input_tokens": call.input_tokens,
                    "output_tokens": call.output_tokens,
                    "latency_ms": call.latency_ms,
                    "status": call.status,
                    "error_class": call.error_class,
                    "demo_token_hash": call.demo_token_hash,
                    "created_at": call.created_at,
                },
            )

    def count_calls(
        self,
        *,
        since: datetime,
        until: datetime,
        provider: str | None = None,
        demo_token_hash: str | None = None,
    ) -> int:
        # Fixed SQL per filter combination (no "IS NULL OR" tricks), so each query can use
        # its (provider, created_at) or (demo_token_hash, created_at) index.
        where = ["created_at >= :since", "created_at < :until"]
        params: dict[str, object] = {"since": since, "until": until}
        if provider is not None:
            where.append("provider = :provider")
            params["provider"] = provider
        if demo_token_hash is not None:
            where.append("demo_token_hash = :token")
            params["token"] = demo_token_hash
        with self._engine.connect() as conn:
            count: int = conn.execute(
                text("SELECT count(*) FROM llm_calls WHERE " + " AND ".join(where)), params
            ).scalar_one()
        return count

    def exhausted_until(self, provider: str) -> datetime | None:
        with self._engine.connect() as conn:
            until: datetime | None = conn.execute(
                text("SELECT exhausted_until FROM budget_state WHERE provider = :provider"),
                {"provider": provider},
            ).scalar_one_or_none()
        return until

    def set_exhausted_until(self, provider: str, until: datetime) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO budget_state (provider, exhausted_until, updated_at) "
                    "VALUES (:provider, :until, now()) "
                    "ON CONFLICT (provider) DO UPDATE "
                    "SET exhausted_until = EXCLUDED.exhausted_until, updated_at = now()"
                ),
                {"provider": provider, "until": until},
            )
