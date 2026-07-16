"""Resilient I/O wrapper for the local Google Drive (``G:``) mount.

WHY THIS EXISTS
---------------
Twice in two days (2026-07-14 and 2026-07-15/16) the ``G:`` Google-Drive-for-Desktop
mount blipped -- an unmount followed ~30s later by a remount -- and the always-on Cora
bot process froze: last heartbeat at 23:51:10, the next never came, no traceback, no
shutdown line, no auto-recovery for ~9.5h. The bot reads its per-request context
(entity/founder ``CLAUDE.md``, ``_brain/`` known-answers) and several ledgers off ``G:``.

A raw ``pathlib.Path.read_text()`` against a vanished Windows mount can either raise
(``WinError 21`` "device not ready", ``53``/``67`` bad net path/name, or a bare
``FileNotFoundError``) OR *block* the calling thread. A blocking filesystem call can
freeze the entire interpreter -- and with it the heartbeat thread -- which is exactly
what an interpreter-wide freeze looks like (the heartbeat file + logs live on ``C:``,
so the heartbeat thread itself never touches ``G:``; the only way it stops is if the
whole interpreter stops).

THE CONTRACT THIS MODULE PROVIDES
---------------------------------
Every ``G:`` touch routed through here is:

1. **Timeout-bounded.** The underlying op runs on a disposable daemon worker thread
   joined with a per-attempt deadline. A hung call can therefore never block the
   *caller* for more than the timeout -- and because CPython releases the GIL during
   blocking I/O, a worker stuck in a kernel read does not freeze the caller or the
   heartbeat thread. (If a pathological FS call blocked WITHOUT releasing the GIL,
   nothing in-process could help; the external heartbeat watchdog -- ``cora-watchdog``,
   shipped 2026-07-16 -- is the recovery net for that residual. This module is the
   prevention half.)

2. **Retried with backoff over a bounded window,** so a transient ~30s remount is
   ridden over transparently (used by scheduled jobs). The interactive request path
   passes a SHORT window so a user never waits on a flaky mount -- it degrades to
   cached context fast.

3. **Fail-typed.** A confirmed mount-gone condition raises :class:`DriveUnavailable`
   (an ``OSError`` subclass) so callers degrade gracefully -- serve cache, emit a
   user-facing "briefly unavailable" notice, or exit a batch job cleanly -- instead of
   hanging or dying.

A short process-wide **circuit breaker** fast-fails during a known outage, so a
sustained multi-hour outage cannot spawn (and leak) one worker thread per request.

HAPPY-PATH INVARIANT
--------------------
When the mount is healthy every helper returns exactly what the underlying ``pathlib``
call returns. A genuine "file does not exist" while the mount is UP raises the ordinary
``FileNotFoundError`` (NOT ``DriveUnavailable``) and ``exists()`` returns a real
``False`` -- so callers that branch on existence keep their exact prior behavior. The
mount-gone-vs-genuine-absence decision is made by probing the mount anchor, never by
guessing from the error alone.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable, TypeVar

log = logging.getLogger(__name__)

_T = TypeVar("_T")


# ── configuration (env-overridable) ──────────────────────────────────────────

def _env_float(name: str, default: float) -> float:
    try:
        raw = os.environ.get(name)
        return float(raw) if raw is not None and raw.strip() else default
    except (TypeError, ValueError):
        return default


# Per-attempt worker-thread join deadline. A single healthy G: read is sub-second;
# this only bounds a *hung* call.
TIMEOUT_SECONDS = _env_float("DRIVE_IO_TIMEOUT_SECONDS", 10.0)

# Total retry window for the DEFAULT (scheduled-job) budget -- long enough to ride
# over a typical ~30s unmount/remount. The interactive request path overrides this
# with a short value (see context_loader) so a user never waits this long.
RETRY_SECONDS = _env_float("DRIVE_IO_RETRY_SECONDS", 90.0)

# Sleep between retry attempts within a window.
BACKOFF_SECONDS = _env_float("DRIVE_IO_BACKOFF_SECONDS", 1.0)

# After a window fully exhausts (outage confirmed), the circuit breaker stays open
# this long -- every read fast-fails as DriveUnavailable during the window, so a
# sustained outage triggers at most one full retry-window per breaker period rather
# than one per request.
BREAKER_SECONDS = _env_float("DRIVE_IO_BREAKER_SECONDS", 30.0)

# The mount reachability anchor -- probed to disambiguate "mount gone" from "file
# genuinely absent". Points at the Founder-OS root under G: by default.
MOUNT_ANCHOR = Path(
    os.environ.get("DRIVE_IO_MOUNT_ANCHOR") or r"G:\My Drive\HJR-Founder-OS"
)

# Windows error codes that unambiguously mean "the volume/mount is not there".
#   21 = ERROR_NOT_READY      (device not ready -- the classic unmounted-drive code)
#   53 = ERROR_BAD_NETPATH    (network path not found)
#   67 = ERROR_BAD_NET_NAME   (network name cannot be found)
_MOUNT_GONE_WINERRORS = frozenset({21, 53, 67})


class DriveUnavailable(OSError):
    """The local Google Drive (``G:``) mount is unreachable (unmounted / remounting).

    An ``OSError`` subclass so existing ``except OSError`` / ``except Exception``
    handlers already catch it -- a naked hang becomes a catchable raise.
    """


class _WorkerTimeout(Exception):
    """Internal sentinel: the worker thread did not finish within the deadline."""


# ── circuit breaker ───────────────────────────────────────────────────────────

_breaker_lock = threading.Lock()
_breaker_open_until = 0.0  # monotonic deadline; breaker is open while now < this


def _breaker_is_open() -> bool:
    with _breaker_lock:
        return time.monotonic() < _breaker_open_until


def _breaker_trip(hold_seconds: float = 0.0) -> None:
    """Open the breaker for at least BREAKER_SECONDS, and at least ``hold_seconds``
    (the retry window of the call that just confirmed the outage). Holding it at least
    as long as the tripping call's window stops a sustained hang-mode outage from
    starting a fresh full-length retry window on every breaker cycle -- which, on the
    scheduled path (retry=90 >> breaker=30), would otherwise leave the breaker open
    only ~25% of the time and undercut its worker-spawn-limiting purpose (D-051 Q5)."""
    global _breaker_open_until
    with _breaker_lock:
        _breaker_open_until = time.monotonic() + max(BREAKER_SECONDS, hold_seconds)


def _breaker_reset() -> None:
    global _breaker_open_until
    with _breaker_lock:
        _breaker_open_until = 0.0


def reset_state_for_tests() -> None:
    """Clear the circuit breaker. Test-only helper (state is process-global)."""
    _breaker_reset()


# ── bounded execution ───────────────────────────────────────────────────────

def _run_bounded(op: Callable[[], _T], timeout: float) -> _T:
    """Run ``op()`` on a disposable daemon worker; join with ``timeout``.

    Returns the value on success. Re-raises whatever ``op`` raised. Raises
    :class:`_WorkerTimeout` if the worker is still running after ``timeout``
    (the caller regains control; the worker is abandoned -- it is a daemon and,
    for blocking I/O, is parked in the kernel with the GIL released).
    """
    box: dict[str, object] = {}

    def _target() -> None:
        try:
            box["value"] = op()
        except BaseException as exc:  # noqa: BLE001 -- ferry every failure to the caller
            box["exc"] = exc

    worker = threading.Thread(target=_target, name="drive-io", daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise _WorkerTimeout()
    if "exc" in box:
        raise box["exc"]  # type: ignore[misc]
    return box["value"]  # type: ignore[return-value]


def _mount_reachable(timeout: float = TIMEOUT_SECONDS) -> bool:
    """Bounded probe of the mount anchor. True only if the anchor is reachable.

    A timeout or any error is treated as NOT reachable (fail toward "mount gone").
    Never raises. Does NOT consult or modify the breaker. ``timeout`` is threaded from
    the caller so a short request-path op's classification probe is bounded by that
    same short budget, not the longer module default (D-051 Q6).
    """
    try:
        return bool(_run_bounded(lambda: os.path.exists(MOUNT_ANCHOR), timeout))
    except _WorkerTimeout:
        return False
    except Exception:  # noqa: BLE001 -- any probe error => treat as unreachable
        return False


def is_mount_available() -> bool:
    """Public bounded reachability check for the ``G:`` mount (Slice 4 breadcrumb).

    Returns True iff the mount anchor is reachable within the timeout. Never raises.
    Independent of the circuit breaker so a breadcrumb probe reflects true current
    reachability, not the cached breaker verdict.
    """
    return _mount_reachable()


def _classify_mount_gone(exc: BaseException, timeout: float = TIMEOUT_SECONDS) -> bool:
    """Decide whether ``exc`` means the mount is gone (vs a genuine file-level error).

    - A worker timeout is always mount-gone (a healthy read never hangs).
    - A known device/network WinError code is always mount-gone.
    - A ``FileNotFoundError`` / other ``OSError`` is ambiguous (the file may simply
      not exist while the mount is UP) -- disambiguate by probing the mount anchor
      (bounded by the caller's ``timeout``).
      Mount reachable => genuine error (NOT mount-gone). Mount unreachable => mount-gone.
    - Anything else (e.g. a ``UnicodeDecodeError``) is NOT mount-gone.
    """
    if isinstance(exc, _WorkerTimeout):
        return True
    winerror = getattr(exc, "winerror", None)
    if winerror in _MOUNT_GONE_WINERRORS:
        return True
    if isinstance(exc, OSError):
        return not _mount_reachable(timeout)
    return False


def _guarded(
    op: Callable[[], _T],
    *,
    what: str,
    timeout: float,
    retry_seconds: float,
) -> _T:
    """Execute ``op`` under the full resilience contract.

    Happy path: returns ``op()``'s value; resets the breaker on success.
    Genuine error (mount UP): re-raises the original exception unchanged.
    Mount gone: retries over ``retry_seconds`` with backoff, then trips the breaker
    and raises :class:`DriveUnavailable`. If the breaker is already open, fast-fails
    with :class:`DriveUnavailable` WITHOUT spawning a worker.
    """
    if _breaker_is_open():
        raise DriveUnavailable(
            f"G: mount known-unavailable (circuit breaker open) while {what}"
        )

    deadline = time.monotonic() + max(0.0, retry_seconds)
    last_exc: BaseException | None = None
    while True:
        try:
            value = _run_bounded(op, timeout)
        except (_WorkerTimeout, OSError) as exc:
            if not _classify_mount_gone(exc, timeout):
                raise  # genuine file-level error with the mount up -- preserve behavior
            last_exc = exc
        else:
            _breaker_reset()
            return value

        if time.monotonic() >= deadline:
            break
        # Floor the backoff so a misconfigured 0/negative DRIVE_IO_BACKOFF_SECONDS can
        # neither raise (negative -> ValueError) nor hot-spin a worker-spawn storm (0).
        time.sleep(max(0.05, BACKOFF_SECONDS))

    # Hold the breaker at least as long as this call's retry window (D-051 Q5).
    _breaker_trip(retry_seconds)
    raise DriveUnavailable(f"G: mount unreachable while {what}") from last_exc


# ── public read/write helpers ────────────────────────────────────────────────

def read_text(
    path: str | os.PathLike[str],
    *,
    encoding: str = "utf-8",
    timeout: float = TIMEOUT_SECONDS,
    retry_seconds: float = RETRY_SECONDS,
) -> str:
    """Resilient ``Path.read_text``. Raises :class:`DriveUnavailable` if the mount
    is gone; re-raises ``FileNotFoundError`` when the mount is UP but the file is
    genuinely absent.

    Classification edge: if the mount ANCHOR itself is unreachable (e.g. a box with no
    G: at all), a missing file cannot be distinguished from a gone mount and so
    reclassifies to :class:`DriveUnavailable`. On the production host G: is always
    present, so absence surfaces as ``FileNotFoundError`` as expected -- but a future
    caller that branches specifically on ``FileNotFoundError`` should ALSO handle
    ``DriveUnavailable`` (both are ``OSError``)."""
    p = Path(path)
    return _guarded(
        lambda: p.read_text(encoding=encoding),
        what=f"reading {p}",
        timeout=timeout,
        retry_seconds=retry_seconds,
    )


def read_bytes(
    path: str | os.PathLike[str],
    *,
    timeout: float = TIMEOUT_SECONDS,
    retry_seconds: float = RETRY_SECONDS,
) -> bytes:
    """Resilient ``Path.read_bytes``."""
    p = Path(path)
    return _guarded(
        lambda: p.read_bytes(),
        what=f"reading {p}",
        timeout=timeout,
        retry_seconds=retry_seconds,
    )


def exists(
    path: str | os.PathLike[str],
    *,
    timeout: float = TIMEOUT_SECONDS,
    retry_seconds: float = RETRY_SECONDS,
) -> bool:
    """Resilient ``Path.exists``. Returns a real bool when the mount is reachable;
    raises :class:`DriveUnavailable` when the mount is gone (the caller genuinely
    cannot answer the existence question, and must not treat "gone" as "absent")."""
    p = Path(path)
    return _guarded(
        lambda: p.exists(),
        what=f"stat {p}",
        timeout=timeout,
        retry_seconds=retry_seconds,
    )


def stat_mtime(
    path: str | os.PathLike[str],
    *,
    timeout: float = TIMEOUT_SECONDS,
    retry_seconds: float = RETRY_SECONDS,
) -> float | None:
    """Resilient modification-time read. Returns the ``st_mtime`` float, or ``None``
    if the file does not exist while the mount is UP. Raises :class:`DriveUnavailable`
    when the mount is gone. Used by the per-request known-answers cache-validity check,
    so callers typically pass ``retry_seconds=0`` for a single fast attempt."""
    p = Path(path)

    def _op() -> float | None:
        try:
            return p.stat().st_mtime
        except FileNotFoundError:
            return None

    return _guarded(_op, what=f"stat {p}", timeout=timeout, retry_seconds=retry_seconds)


def glob(
    directory: str | os.PathLike[str],
    pattern: str,
    *,
    timeout: float = TIMEOUT_SECONDS,
    retry_seconds: float = RETRY_SECONDS,
) -> list[Path]:
    """Resilient directory glob. Returns a materialized list (the generator is drained
    inside the bounded worker so iteration can't hang the caller later). Raises
    :class:`DriveUnavailable` when the mount is gone."""
    d = Path(directory)
    return _guarded(
        lambda: list(d.glob(pattern)),
        what=f"globbing {d}/{pattern}",
        timeout=timeout,
        retry_seconds=retry_seconds,
    )


def write_text_atomic(
    path: str | os.PathLike[str],
    text: str,
    *,
    encoding: str = "utf-8",
    make_parents: bool = True,
    timeout: float = TIMEOUT_SECONDS,
    retry_seconds: float = RETRY_SECONDS,
) -> None:
    """Resilient atomic text write (temp file + ``replace``). Optionally creates parent
    dirs. Raises :class:`DriveUnavailable` when the mount is gone."""
    _write_atomic(
        path, text.encode(encoding), make_parents=make_parents,
        timeout=timeout, retry_seconds=retry_seconds,
    )


def write_bytes_atomic(
    path: str | os.PathLike[str],
    data: bytes,
    *,
    make_parents: bool = True,
    timeout: float = TIMEOUT_SECONDS,
    retry_seconds: float = RETRY_SECONDS,
) -> None:
    """Resilient atomic byte write (temp file + ``replace``)."""
    _write_atomic(
        path, data, make_parents=make_parents,
        timeout=timeout, retry_seconds=retry_seconds,
    )


def append_text(
    path: str | os.PathLike[str],
    text: str,
    *,
    encoding: str = "utf-8",
    make_parents: bool = True,
    timeout: float = TIMEOUT_SECONDS,
    retry_seconds: float = 0.0,
) -> None:
    """Resilient APPEND (``open('a')`` + write) for append-only ledgers.

    Defaults to ``retry_seconds=0`` (a single bounded attempt, NO retry): a retry
    after a slow-but-eventually-successful first write would double-append a line,
    which -- unlike an atomic replace -- is not idempotent. Callers that must record
    accept "skip on outage" (fail-open) over a possible duplicate row. Raises
    :class:`DriveUnavailable` when the mount is gone."""
    p = Path(path)

    def _op() -> None:
        if make_parents:
            p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding=encoding) as fh:
            fh.write(text)

    _guarded(_op, what=f"appending {p}", timeout=timeout, retry_seconds=retry_seconds)


def _write_atomic(
    path: str | os.PathLike[str],
    data: bytes,
    *,
    make_parents: bool,
    timeout: float,
    retry_seconds: float,
) -> None:
    p = Path(path)
    tmp = p.with_suffix(p.suffix + ".drivetmp")

    def _op() -> None:
        if make_parents:
            p.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(data)
        tmp.replace(p)

    _guarded(_op, what=f"writing {p}", timeout=timeout, retry_seconds=retry_seconds)
