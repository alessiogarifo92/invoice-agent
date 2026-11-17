"""The worker thread in the web process: off by default, started and stopped by the lifespan."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine

from app.agent.triage import TriageAgent
from app.jobs.worker import Worker, build_worker
from app.llm.gateway import GatewayConfigError
from app.llm.types import Limits
from app.main import create_app
from tests.jobs_support import make_settings, scalar, sql_gateway
from tests.llm_support import FAKE_KEY, FakeProvider
from tests.support import ScratchDatabase


def test_the_worker_is_off_by_default() -> None:
    settings = make_settings()
    assert settings.worker_enabled is False
    app = create_app(settings)
    with TestClient(app) as client:
        assert app.state.worker is None
        assert client.get("/healthz").status_code == 200


def test_an_enabled_worker_without_an_llm_key_is_not_started(
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = make_settings(worker_enabled=True, gemini_api_key=None)
    app = create_app(settings)
    with TestClient(app) as client:
        assert app.state.worker is None
        assert client.get("/healthz").status_code == 200
    assert "worker not started" in capsys.readouterr().out


def test_build_worker_refuses_settings_without_an_llm_key() -> None:
    with pytest.raises(GatewayConfigError):
        build_worker(make_settings(gemini_api_key=None))


def test_build_worker_takes_its_timings_from_the_settings() -> None:
    settings = make_settings(
        gemini_api_key=FAKE_KEY,
        worker_lease_s=30,
        worker_heartbeat_s=5,
        worker_poll_interval_s=1.5,
        worker_max_attempts=4,
        worker_retry_backoff_s=12,
    )
    worker = build_worker(settings)
    try:
        assert (worker.lease_s, worker.heartbeat_s, worker.poll_s) == (30, 5, 1.5)
        assert (worker.max_attempts, worker.retry_backoff_s) == (4, 12)
    finally:
        worker.engine.dispose()


def test_build_worker_wires_the_triage_agent_with_its_limits() -> None:
    worker = build_worker(
        make_settings(gemini_api_key=FAKE_KEY, agent_max_tool_calls=5, agent_timeout_s=42)
    )
    try:
        assert isinstance(worker.agent, TriageAgent)
        assert worker.agent.limits == Limits(max_tool_calls=5, timeout_s=42)
    finally:
        worker.engine.dispose()


def test_the_agent_limits_default_to_phase_3() -> None:
    settings = make_settings()
    assert (settings.agent_max_tool_calls, settings.agent_timeout_s) == (8, 120.0)


def test_the_lifespan_starts_and_stops_the_worker(
    test_db: ScratchDatabase, app_engine: Engine
) -> None:
    # An empty queue: the thread sweeps and polls, and never calls a provider.
    settings = make_settings(
        worker_enabled=True,
        worker_poll_interval_s=0.05,
        database_url=test_db.app_sqlalchemy_url,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        worker: Worker = app.state.worker
        assert worker is not None and worker.is_alive()
        assert client.get("/healthz").status_code == 200
    assert not worker.is_alive()
    assert scalar(app_engine, "SELECT count(*) FROM llm_calls") == 0


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"lease_s": 10.0, "heartbeat_s": 10.0}, "heartbeat"),
        ({"heartbeat_s": 0.0}, "heartbeat"),
        ({"max_attempts": 0}, "max_attempts"),
    ],
)
def test_the_worker_rejects_unsafe_timings(options: dict[str, float], message: str) -> None:
    # The engine never connects: Worker() itself touches no database.
    engine = create_engine("postgresql+psycopg://nobody@localhost:1/none")
    with pytest.raises(ValueError, match=message):
        Worker(engine, sql_gateway(engine, FakeProvider("gemini")), **options)  # type: ignore[arg-type]
