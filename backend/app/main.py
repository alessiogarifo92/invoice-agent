import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from opentelemetry.sdk.trace import TracerProvider

from app import llm_ping
from app.config import Settings, get_settings
from app.observability import build_tracer_provider


def run_startup_ping(settings: Settings, provider: TracerProvider) -> llm_ping.PingResult:
    """One traced Gemini call, flushed to Langfuse straight away (T0 walking skeleton)."""
    with httpx.Client() as client:
        result = llm_ping.ping(settings, client, provider.get_tracer("invoice-agent.startup"))
    provider.force_flush()
    print(
        f"startup llm ping: ok={result.ok} model={result.model} "
        f"latency_ms={result.latency_ms} {result.error}".rstrip(),
        flush=True,
    )
    return result


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        provider = build_tracer_provider(settings)
        app.state.tracer_provider = provider
        app.state.startup_ping_thread = None
        if settings.llm_ping_on_startup and provider is not None:
            # Off the event loop and non-blocking: startup and /healthz never wait on it.
            thread = threading.Thread(
                target=run_startup_ping, args=(settings, provider), daemon=True
            )
            thread.start()
            app.state.startup_ping_thread = thread
        yield
        if provider is not None:
            provider.shutdown()

    app = FastAPI(title="invoice-agent", version=settings.app_version, lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        # Liveness only: must never touch the database (Neon scales to zero,
        # and Render health checks would otherwise wake it up).
        return {"status": "ok", "version": settings.app_version}

    return app


app = create_app()
