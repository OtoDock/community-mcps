"""Background renders: ``render_composition(background=true)`` answers at once
with a job, ``render_status`` says how far it is, ``cancel_render`` stops it.

A job lives in this container's memory: a restart, an update or a stop of the
tool ends every job, and its id then answers ``lost``. One job encodes at a
time, a few wait; a job's partial file sits beside its output under a hidden
name and lands on the output's name only when its encode succeeded. From
then on the render is done: a cancel no longer stops it. The tool's
``/busy`` route answers whether a job waits or runs, so the platform keeps
it awake.

A job is the caller's to read or stop only while the caller's own session
resolves the job's output to the same place: read for its status, write to
cancel it. An unknown id and a job of another session's place answer alike.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import time
from dataclasses import dataclass, field

import renderer
from shared import _notify_file_written, _resolve_path, _to_agents_relative, logger

MAX_WAITING = 4
MAX_KEPT = 32
# A job's own ceiling, at the platform's bound on the tool's busy answer
# (the manifest's tool_timeout_sec): a stuck render ends, and the tool says
# it is free again.
JOB_MAX_S = 3600
KEEP_FINISHED_S = 6 * 3600
HOST_CACHE = ".remote-host-cache/"

LOST = ("This render is not running. The tool restarted, or the render ended "
        "more than six hours ago.")
FULL = (f"{MAX_WAITING} renders already wait their turn. Ask again when one has "
        "finished.")
ON_A_MACHINE = ("A render in the background writes on the platform, and this output is on "
                "a computer. Render it without background.")
TOO_LONG = "The render ran for an hour and was stopped. Render it in parts, or as a preview."
TOO_LONG_LANDED = ("The render ran for an hour and was stopped after its file was written. "
                   "The file is whole, but the render's report was not finished.")
LANDED = ("The render had already written its file, so it was not stopped. "
          "Ask render_status for its report.")
NOT_SENT = ("The file is in the agent's folder on the platform. The render ended after its "
            "chat did, so it was not sent to a computer that chat ran on.")


@dataclass
class Job:
    id: str
    output: str          # the container path the render ends at
    place: str           # its agents-relative answer, the job's identity
    state: str = "waiting"
    percent: float = 0.0
    result: dict | None = None
    error: str = ""
    started: float = field(default_factory=time.monotonic)
    finished: float = 0.0
    cancelled: bool = False
    task: asyncio.Task | None = None
    # Whether the platform took the finished file's notice (it sends the file
    # to the computer a chat runs on): the call's session may have ended.
    sent: bool = False
    # The render's file has landed on its output: the render is done, and a
    # cancel no longer stops it.
    landed: bool = False


_jobs: dict[str, Job] = {}
_turn = asyncio.Lock()


def busy() -> bool:
    """A job waits or runs."""
    return any(j.state in ("waiting", "running") for j in _jobs.values())


def _forget_old(now: float) -> None:
    done = sorted((j for j in _jobs.values() if j.finished), key=lambda j: j.finished)
    for j in done:
        if now - j.finished > KEEP_FINISHED_S or len(_jobs) > MAX_KEPT:
            _jobs.pop(j.id, None)


def _sandbox_form(place: str) -> str:
    """The job's place as a session names it: the agents-relative answer
    without its agent's segment."""
    return "/" + place.split("/", 1)[1] if "/" in place else place


async def _owned(job: Job, *, writing: bool) -> bool:
    """The caller's own session resolves the job's output to the job's place."""
    try:
        answer = await _resolve_path(_sandbox_form(job.place), writing=writing)
    except ValueError:
        return False
    return _to_agents_relative(answer) == job.place


async def start(plan: renderer.RenderPlan, report) -> Job:
    """Queue the heavy half of a planned render. ``report(result)`` turns the
    finished result into the answer ``render_status`` gives. Refuses a fifth
    waiting job and an output on a machine (it would never be pushed back
    once the call's session ended)."""
    now = time.monotonic()
    _forget_old(now)
    place = _to_agents_relative(plan.out)
    if place.startswith(HOST_CACHE):
        renderer.discard(plan)
        raise ValueError(ON_A_MACHINE)
    if sum(1 for j in _jobs.values() if j.state == "waiting") >= MAX_WAITING:
        renderer.discard(plan)
        raise ValueError(FULL)
    job = Job(id=secrets.token_hex(16), output=plan.out, place=place)
    _jobs[job.id] = job

    def on_progress(percent: float) -> None:
        job.percent = round(percent, 1)

    def on_landing() -> None:
        job.landed = True

    async def run() -> None:
        try:
            async with _turn:
                if job.cancelled:
                    raise asyncio.CancelledError()
                job.state = "running"
                result = await asyncio.wait_for(renderer.run_render(
                    plan, progress=on_progress,
                    cancelled=lambda: job.cancelled, landing=on_landing), JOB_MAX_S)
            job.result = {**result, "report": report(result)}
            job.state, job.percent = "done", 100.0
            with contextlib.suppress(Exception):
                job.sent = await _notify_file_written(result["output"])
        except asyncio.CancelledError:
            job.state = "cancelled"
            renderer.discard(plan)
        except asyncio.TimeoutError:
            job.state, job.error = "failed", TOO_LONG_LANDED if job.landed else TOO_LONG
            renderer.discard(plan)
        except Exception as exc:  # noqa: BLE001  # the job's answer carries every failure
            logger.warning("background render %s failed: %s", job.id[:8], exc)
            job.state, job.error = "failed", str(exc)
            renderer.discard(plan)
        finally:
            job.finished = time.monotonic()

    # The task copies this request's context: its session answers the
    # judge for every file the render writes on its own.
    job.task = asyncio.get_running_loop().create_task(run())
    return job


async def status(job_id: str) -> dict:
    """``{job_id, state, percent, output, report | error}``; ``lost`` for an
    unknown id and for a job the caller's session does not reach."""
    job = _jobs.get(job_id)
    if job is None or not await _owned(job, writing=False):
        return {"job_id": job_id, "state": "lost", "message": LOST}
    out: dict = {"job_id": job.id, "state": job.state, "percent": job.percent,
                 "output": job.place}
    if job.state == "done" and job.result:
        out["report"] = job.result["report"]
        if not job.sent:
            out["note"] = NOT_SENT
    if job.state == "failed":
        out["error"] = job.error
    return out


async def cancel(job_id: str) -> dict:
    """Stop a waiting or running job: its encode is killed and its temp file
    removed, so the output keeps the file it had. A job whose file has landed
    is done: it is not stopped, it finishes its report, and the answer says
    so (``LANDED``). Needs the caller's session to write the job's output."""
    job = _jobs.get(job_id)
    if job is None or not await _owned(job, writing=True):
        return {"job_id": job_id, "state": "lost", "message": LOST}
    if job.state in ("waiting", "running") and job.landed:
        return {"job_id": job.id, "state": job.state, "output": job.place, "message": LANDED}
    if job.state in ("waiting", "running"):
        job.cancelled = True
        if job.task is not None:
            job.task.cancel()
        job.state = "cancelled"
    return {"job_id": job.id, "state": job.state, "output": job.place}


def reset_for_tests() -> None:
    _jobs.clear()
