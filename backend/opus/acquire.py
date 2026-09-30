"""The one address the library acquires through.

Every channel in both halves is a client of OPUS · Downloads — the search, the
grab and the tracking. The library no longer knows that Prowlarr, SABnzbd,
qBittorrent or slskd exist, which is the point: the workers are shared
with the other OPUS modules and keeping N apps × M workers matched by hand is
what OPUS · Downloads was built to end.

What the library keeps is everything OPUS refuses to decide: which release is
worth taking, whether its subtitles satisfy the policy or its edition is the
right one, and what the file is called once it lands.

This module is the transport and nothing else. It knows how to reach OPUS and
how to fail loudly when it cannot; what a release *means* is read by the channel
that asked for it, because a peer's folder of FLACs and a usenet post of a film
are not the same answer to the same question."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import uuid

import httpx
import opus_auth

# who the caller is, as OPUS records it on every job. One name, because the two
# halves stopped being two apps.
APP = "library"

# how long OPUS may leave a job unaccounted for before the row waiting on it is
# failed: right after a grab there is a window where the job is not visible yet
UNACCOUNTED_GRACE = timedelta(minutes=10)


class AcquireError(Exception):
    """Raised on any failure to acquire. Never swallowed — fail loud."""


@dataclass
class JobStatus:
    # 'unknown' means OPUS cannot account for the job at all — neither running
    # nor finished — and the caller decides how long to keep waiting
    state: str  # queued | downloading | complete | failed | unknown
    progress: float = 0.0
    detail: str = ""
    files: list[dict] = field(default_factory=list)
    # the exact folder in OPUS's view of the shared landing tree; each half
    # swaps one mount point for the other rather than searching for it by name
    directory: str | None = None


_clients: dict[tuple[str, str], httpx.AsyncClient] = {}


async def _client(config) -> httpx.AsyncClient:
    url = config.get("opus_url").rstrip("/")
    if not url:
        raise AcquireError("no OPUS url is configured")
    token = config.get("opus_token")
    key = (url, token)
    http = _clients.get(key)
    if http is None or http.is_closed:
        for old in [k for k in _clients if k != key]:
            await _clients.pop(old).aclose()
        _clients[key] = http = httpx.AsyncClient(
            base_url=f"{url}/api", headers={opus_auth.TOKEN_HEADER: token} if token else {})
    return http


async def close() -> None:
    while _clients:
        _, http = _clients.popitem()
        await http.aclose()


async def search(config, query: str, kind: str, engine_names: list[str],
                 timeout: float = 90) -> list[dict]:
    """Normalized releases, straight from OPUS. Naming the engines keeps the
    fan-out down to the ones whose answers this caller can actually read."""
    http = await _client(config)
    try:
        resp = await http.post("/search", timeout=timeout, json={
            "query": query, "type": kind, "app": APP, "engines": engine_names,
        })
        resp.raise_for_status()
        answer = resp.json()
        releases = answer["releases"]
        errors = answer["errors"]
        if errors:
            details = "; ".join(f"{error['engine']}: {error['detail']}" for error in errors)
            raise AcquireError(f"OPUS search failed: {details}")
        if not isinstance(releases, list):
            raise TypeError("OPUS search releases are not a list")
        return releases
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise AcquireError(f"OPUS search failed: {exc}") from exc


async def inspect(config, grab_ref: dict) -> dict:
    """What a release DECLARES it holds, before anything is fetched."""
    http = await _client(config)
    try:
        resp = await http.post("/inspect", timeout=60, json={"grab_ref": grab_ref})
        resp.raise_for_status()
        return resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise AcquireError(f"OPUS inspect failed: {exc}") from exc


async def grab(config, grab_ref: dict, namespace: str, *, idempotency_key: str | None = None) -> str:
    """Start one acquire request, retrying a lost transport response safely.

    Downloads records this key before it asks an engine to fetch anything.  A
    retry therefore receives that job instead of sending the engine a second
    copy of the same release.
    """
    http = await _client(config)
    key = idempotency_key or uuid.uuid4().hex
    try:
        for attempt in range(2):
            try:
                resp = await http.post("/grab", timeout=45, headers={
                    "Idempotency-Key": key,
                }, json={"grab_ref": grab_ref, "namespace": namespace, "app": APP})
                break
            except httpx.TransportError:
                if attempt:
                    raise
        resp.raise_for_status()
        return resp.json()["job_id"]
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise AcquireError(f"OPUS refused the grab: {exc!r}") from exc


async def job_status(config, job_ref: dict) -> JobStatus:
    """The job as OPUS sees it. A job OPUS knows nothing of is 'unknown', which
    is not the same as a failure."""
    job_id = job_ref.get("job")
    if not job_id:
        return JobStatus("unknown", detail="the job was never handed to OPUS")
    http = await _client(config)
    try:
        resp = await http.get(f"/jobs/{job_id}", timeout=30)
        if resp.status_code == 404:
            return JobStatus("unknown", detail="OPUS has no such job")
        resp.raise_for_status()
        job = resp.json()
        return JobStatus(
            state=job["state"],
            progress=job.get("progress") or 0.0,
            detail=job.get("error") or job.get("detail") or "",
            directory=job.get("landing_path") or None,
        )
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise AcquireError(f"OPUS status failed: {exc!r}") from exc


def unaccounted_for(status: JobStatus, since: datetime) -> bool:
    return (status.state == "unknown"
            and datetime.now(timezone.utc) - since > UNACCOUNTED_GRACE)


async def drop(config, job_ref: dict) -> None:
    """Stop the download if it is running, discard what it fetched, forget it."""
    job_id = job_ref.get("job")
    if not job_id:
        return
    http = await _client(config)
    try:
        resp = await http.delete(f"/jobs/{job_id}", timeout=45)
    except httpx.HTTPError as exc:
        raise AcquireError(f"OPUS unreachable dropping job {job_id}: {exc}") from exc
    if resp.status_code not in (204, 404):
        raise AcquireError(f"OPUS would not drop job {job_id}: {resp.text}")
