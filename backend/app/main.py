import asyncio
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from opentelemetry.sdk.trace import TracerProvider

from app import llm_ping
from app.api import audit, invoices
from app.config import Settings, get_settings
from app.jobs.worker import Worker, build_worker
from app.llm.gateway import GatewayConfigError
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


def start_worker(settings: Settings) -> Worker | None:
    """The pipeline worker thread, when WORKER_ENABLED is set and an LLM provider is configured.

    Starting it connects to nothing: its first loop, in its own thread, runs the lazy sweep.
    """
    if not settings.worker_enabled:
        return None
    try:
        worker = build_worker(settings)
    except GatewayConfigError as exc:
        print(f"worker not started: {exc}", flush=True)
        return None
    worker.start()
    return worker


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
        worker = start_worker(settings)
        app.state.worker = worker
        yield
        if worker is not None:
            # Joining waits for the current stage at most; off the event loop.
            await asyncio.to_thread(worker.stop)
            worker.engine.dispose()
        if provider is not None:
            provider.shutdown()

    app = FastAPI(title="invoice-agent", version=settings.app_version, lifespan=lifespan)
    # Routers read the settings this app was built with (tests pass their own).
    app.state.settings = settings
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

    app.include_router(audit.router)
    app.include_router(invoices.router)
    return app


app = create_app()
