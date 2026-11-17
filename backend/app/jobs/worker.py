"""The worker: a background thread in the web process that claims jobs and runs the pipeline.

Phase 3, A2. One thread per process (Render Free has one instance and 512 MB), started by
the FastAPI lifespan when WORKER_ENABLED is set, off by default (tests, local runs).

- The expired-lease sweep runs lazily: on the worker's first loop, never during app startup
  (/healthz and startup must not touch the database), then every `sweep_interval_s`.
- While a job runs, a heartbeat thread extends its lease. A heartbeat that finds the lease
  gone makes the pipeline stop at the next stage boundary.
- QuotaUnavailable parks the invoice in `awaiting_quota` and requeues the job for the reset
  time, without using up an attempt (phase 2 amendment).
- Any other error is retried with a linear backoff, `max_attempts` attempts in all; then the
  job and the invoice are `failed`.
- On stop, a job between stages is handed back to the queue at once; one in the middle of a
  stage finishes it, or its lease expires and another worker resumes it.
"""

import os
import secrets
import socket
import threading
import time
from datetime import datetime
from types import TracebackType
from uuid import UUID

from sqlalchemy import Connection, Engine, create_engine, text

from app.agent import Agent, triage
from app.agent.triage import TriageAgent
from app.audit import append_event
from app.config import Settings
from app.db import normalize_url
from app.jobs import queue
from app.jobs.pipeline import Stopping, run_pipeline
from app.llm.errors import QuotaUnavailable
from app.llm.gateway import LLMGateway, build_gateway, mark_awaiting_quota
from app.llm.ledger import SqlLedger
from app.llm.types import Limits

ACTOR = queue.ACTOR


def _log(message: str) -> None:
    print(f"worker: {message}", flush=True)


def default_owner() -> str:
    """Unique per worker instance: host, process and a random suffix."""
    return f"{socket.gethostname()}:{os.getpid()}:{secrets.token_hex(3)}"


def quota_retry_at(conn: Connection, providers: list[str]) -> datetime:
    """When a parked job may try again: the earliest time any of `providers` is back.

    A provider with budget_state.exhausted_until ahead is back then; one without (soft cap,
    an unconfirmed quota error, the per-demo-token cap) at the next 00:00 UTC, when the
    ledger's daily counts restart.
    """
    midnight = "(date_trunc('day', now() AT TIME ZONE 'UTC') + interval '1 day') AT TIME ZONE 'UTC'"
    retry_at: datetime = conn.execute(
        text(
            "SELECT COALESCE(min(CASE WHEN b.exhausted_until > now() THEN b.exhausted_until "
            f"ELSE {midnight} END), {midnight}) "
            "FROM unnest(CAST(:providers AS text[])) AS p(provider) "
            "LEFT JOIN budget_state b ON b.provider = p.provider"
        ),
        {"providers": providers},
    ).scalar_one()
    return retry_at


class _Heartbeat:
    """Extends a job's lease every `interval_s` until the job ends or the lease is lost."""

    def __init__(self, engine: Engine, job_id: UUID, owner: str, lease_s: float, interval_s: float):
        self.engine = engine
        self.job_id = job_id
        self.owner = owner
        self.lease_s = lease_s
        self.interval_s = interval_s
        self.lost = threading.Event()
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"heartbeat-{job_id}", daemon=True)

    def __enter__(self) -> "_Heartbeat":
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._done.set()
        self._thread.join()

    def _run(self) -> None:
        while not self._done.wait(self.interval_s):
            try:
                with self.engine.begin() as conn:
                    alive = queue.heartbeat(conn, self.job_id, self.owner, self.lease_s)
            except Exception as exc:  # the next beat may get through; the lease has slack
                _log(f"heartbeat for job {self.job_id} failed: {queue.error_text(exc)}")
                continue
            if not alive:
                self.lost.set()
                return


class Worker:
    def __init__(
        self,
        engine: Engine,
        gateway: LLMGateway,
        *,
        owner: str | None = None,
        # The no-op agent unless one is given: build_worker passes the TriageAgent.
        agent: Agent = triage,
        lease_s: float = 60.0,
        heartbeat_s: float = 15.0,
        poll_s: float = 2.0,
        max_attempts: int = 3,
        retry_backoff_s: float = 30.0,
        sweep_interval_s: float = 60.0,
    ):
        if not 0 < heartbeat_s < lease_s:
            raise ValueError("the heartbeat interval must be positive and shorter than the lease")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self.engine = engine
        self.gateway = gateway
        self.owner = owner or default_owner()
        self.agent = agent
        self.lease_s = lease_s
        self.heartbeat_s = heartbeat_s
        self.poll_s = poll_s
        self.max_attempts = max_attempts
        self.retry_backoff_s = retry_backoff_s
        self.sweep_interval_s = sweep_interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_sweep: float | None = None

    # --- one step ------------------------------------------------------------------------

    def sweep(self) -> queue.Sweep:
        with self.engine.begin() as conn:
            result = queue.sweep_expired(conn, self.max_attempts)
        self._last_sweep = time.monotonic()
        for job in result.requeued:
            _log(f"requeued job {job.id}: lease of {job.lease_owner} expired")
        return result

    def run_once(self) -> bool:
        """Sweep if due, then claim and process one job. False when nothing was due."""
        if self._last_sweep is None or time.monotonic() - self._last_sweep >= self.sweep_interval_s:
            self.sweep()
        with self.engine.begin() as conn:
            job = queue.claim(conn, self.owner, self.lease_s)
        if job is None:
            return False
        self.process(job)
        return True

    def process(self, job: queue.Job) -> None:
        with _Heartbeat(self.engine, job.id, self.owner, self.lease_s, self.heartbeat_s) as beat:

            def check() -> None:
                if beat.lost.is_set():
                    raise queue.LeaseLost(job.id)
                if self._stop.is_set():
                    raise Stopping()

            try:
                run_pipeline(self.engine, job, self.owner, self.gateway, self.agent, check)
            except queue.LeaseLost:
                _log(f"job {job.id}: lease lost, another worker owns it now")
            except Stopping:
                self._hand_back(job)
            except QuotaUnavailable as exc:
                self._park(job, exc)
            except Exception as exc:
                self._retry_or_fail(job, exc)

    def _hand_back(self, job: queue.Job) -> None:
        with self.engine.begin() as conn:
            try:
                queue.lock_owned(conn, job.id, self.owner)
            except queue.LeaseLost:
                return
            queue.requeue(conn, job.id, refund_attempt=True)
        _log(f"job {job.id}: handed back on shutdown")

    def _park(self, job: queue.Job, exc: QuotaUnavailable) -> None:
        with self.engine.begin() as conn:
            try:
                current = queue.lock_owned(conn, job.id, self.owner)
            except queue.LeaseLost:
                return
            retry_at = quota_retry_at(conn, exc.providers)
            due = queue.requeue(
                conn,
                job.id,
                run_after=retry_at,
                error=queue.error_text(exc),
                refund_attempt=True,
            )
            mark_awaiting_quota(conn, job.invoice_id)
            append_event(
                conn,
                ACTOR,
                "invoice.awaiting_quota",
                "invoice",
                str(job.invoice_id),
                {
                    "job_id": str(job.id),
                    "stage": current.stage,
                    "providers": exc.providers,
                    "reason": exc.reason,
                    "retry_at": due.isoformat(),
                },
            )
        _log(f"job {job.id}: no LLM quota, parked until {due.isoformat()}")

    def _retry_or_fail(self, job: queue.Job, exc: Exception) -> None:
        error = queue.error_text(exc)
        with self.engine.begin() as conn:
            try:
                current = queue.lock_owned(conn, job.id, self.owner)
            except queue.LeaseLost:
                return
            if current.attempts >= self.max_attempts:
                queue.fail_job_and_invoice(conn, current, error, type(exc).__name__)
                _log(f"job {job.id}: failed after {current.attempts} attempts: {error}")
                return
            due = queue.requeue(
                conn, job.id, delay_s=self.retry_backoff_s * current.attempts, error=error
            )
            append_event(
                conn,
                ACTOR,
                "job.retry_scheduled",
                "invoice",
                str(job.invoice_id),
                {
                    "job_id": str(job.id),
                    "stage": current.stage,
                    "attempt": current.attempts,
                    "retry_at": due.isoformat(),
                    # The class only: exception text stays in jobs.last_error, out of the chain.
                    "error": type(exc).__name__,
                },
            )
        _log(f"job {job.id}: attempt {current.attempts} failed ({error}), retry at {due}")

    # --- the thread ----------------------------------------------------------------------

    def _loop(self) -> None:
        _log(f"{self.owner} started")
        while not self._stop.is_set():
            try:
                ran = self.run_once()
            except Exception as exc:  # e.g. the database is unreachable: wait, then retry
                _log(f"loop error: {queue.error_text(exc)}")
                ran = False
            if not ran:
                self._stop.wait(self.poll_s)
        _log(f"{self.owner} stopped")

    def start(self) -> None:
        if self.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="invoice-worker", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        """Ask the loop to stop and wait up to `timeout` for the current job's stage."""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()


def build_worker(settings: Settings) -> Worker:
    """A worker on its own engine for settings.database_url (the restricted app role), with
    the triage agent on the same gateway and the agent limits from the settings.

    Raises GatewayConfigError when no LLM provider can be built (e.g. the key is missing).
    """
    engine = create_engine(normalize_url(settings.database_url), pool_pre_ping=True)
    try:
        gateway = build_gateway(settings, ledger=SqlLedger(engine))
    except Exception:
        engine.dispose()
        raise
    limits = Limits(
        max_tool_calls=settings.agent_max_tool_calls, timeout_s=settings.agent_timeout_s
    )
    return Worker(
        engine,
        gateway,
        agent=TriageAgent(engine, gateway, limits),
        lease_s=settings.worker_lease_s,
        heartbeat_s=settings.worker_heartbeat_s,
        poll_s=settings.worker_poll_interval_s,
        max_attempts=settings.worker_max_attempts,
        retry_backoff_s=settings.worker_retry_backoff_s,
    )
