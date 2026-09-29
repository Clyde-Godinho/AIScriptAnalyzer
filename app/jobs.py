"""In-process async job registry.

Sync and analyze are minutes-long, I/O-bound operations, so the API returns a
job id immediately and the caller polls ``/api/jobs/{job_id}``. The registry is
deliberately in-memory (single-process deployments); for a horizontally scaled
deployment swap it for Redis + RQ/Arq/Celery - the call sites only use
:meth:`JobRegistry.submit` and :meth:`JobRegistry.get`.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from app.models import JobState, JobStatus

logger = logging.getLogger(__name__)

JobKind = Literal["sync", "analyze"]


@dataclass
class _Job:
    job_id: str
    kind: JobKind
    state: JobState = JobState.QUEUED
    submitted_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    task: asyncio.Task | None = None

    def snapshot(self) -> JobStatus:
        return JobStatus(
            job_id=self.job_id,
            kind=self.kind,
            state=self.state,
            submitted_at=self.submitted_at,
            finished_at=self.finished_at,
            result=self.result,
            error=self.error,
        )


class JobRegistry:
    """Bounded, in-memory registry of background jobs."""

    def __init__(self, max_jobs: int = 200) -> None:
        self._jobs: OrderedDict[str, _Job] = OrderedDict()
        self._max_jobs = max_jobs

    # ------------------------------------------------------------------ #
    def submit(
        self,
        kind: JobKind,
        factory: Callable[[], Awaitable[dict[str, Any] | None]],
    ) -> _Job:
        """Schedule ``factory()`` and return the tracked job immediately."""
        job = _Job(job_id=uuid.uuid4().hex[:16], kind=kind)
        self._jobs[job.job_id] = job
        self._evict()
        job.task = asyncio.create_task(self._run(job, factory))
        return job

    async def _run(
        self, job: _Job, factory: Callable[[], Awaitable[dict[str, Any] | None]]
    ) -> None:
        job.state = JobState.RUNNING
        try:
            job.result = await factory()
            job.state = JobState.SUCCEEDED
        except asyncio.CancelledError:  # pragma: no cover - shutdown path
            job.state = JobState.FAILED
            job.error = "cancelled"
            raise
        except Exception as exc:  # noqa: BLE001 - reported through the job API
            logger.exception("job %s (%s) failed", job.job_id, job.kind)
            job.state = JobState.FAILED
            job.error = str(exc)
        finally:
            job.finished_at = datetime.now(timezone.utc)

    def _evict(self) -> None:
        while len(self._jobs) > self._max_jobs:
            job_id, job = next(iter(self._jobs.items()))
            if job.state in (JobState.QUEUED, JobState.RUNNING):
                break
            self._jobs.pop(job_id, None)

    # ------------------------------------------------------------------ #
    def get(self, job_id: str) -> JobStatus | None:
        job = self._jobs.get(job_id)
        return job.snapshot() if job else None

    def list(self, limit: int = 20) -> list[JobStatus]:
        return [job.snapshot() for job in list(self._jobs.values())[-limit:]]

    def has_running(self, kind: JobKind | None = None) -> bool:
        return any(
            job.state in (JobState.QUEUED, JobState.RUNNING) and (kind is None or job.kind == kind)
            for job in self._jobs.values()
        )

    def running_of(self, kind: JobKind) -> JobStatus | None:
        for job in self._jobs.values():
            if job.kind == kind and job.state in (JobState.QUEUED, JobState.RUNNING):
                return job.snapshot()
        return None

    async def shutdown(self) -> None:
        tasks = [job.task for job in self._jobs.values() if job.task and not job.task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


__all__ = ["JobKind", "JobRegistry"]
