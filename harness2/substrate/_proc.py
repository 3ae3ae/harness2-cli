# Copyright 2026 The Harness2 Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Process-group-owned subprocess execution with deterministic cancellation and cleanup."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

_LOCK = threading.Lock()
_ACTIVE_PGIDS: set[int] = set()


@dataclass(frozen=True)
class ProcessResult:
    """Outcome of a process-group-managed subprocess execution."""

    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False
    cancelled: bool = False


def active_process_groups() -> tuple[int, ...]:
    """Return active child process-group IDs currently tracked by this process."""
    with _LOCK:
        return tuple(sorted(_ACTIVE_PGIDS))


def _pgid_alive(pgid: int) -> bool:
    if pgid <= 0 or os.name != "posix":
        return False
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def terminate_process_group(pgid: int, *, grace_seconds: float = 1.0) -> None:
    """Terminate an owned session/process group cleanly without targeting unrelated processes."""
    if pgid <= 0 or os.name != "posix":
        return
    if not _pgid_alive(pgid):
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError:
        pass
    deadline = time.monotonic() + max(0.0, grace_seconds)
    while time.monotonic() < deadline:
        if not _pgid_alive(pgid):
            return
        time.sleep(0.02)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        return


def terminate_active(*, grace_seconds: float = 0.5) -> None:
    """Terminate all active child process groups tracked by this coordinator process."""
    pgids = active_process_groups()
    if not pgids:
        return
    if os.name != "posix":
        for pgid in pgids:
            terminate_process_group(pgid, grace_seconds=grace_seconds)
        return
    targets: list[int] = []
    for pgid in pgids:
        if pgid <= 0 or not _pgid_alive(pgid):
            continue
        try:
            os.killpg(pgid, signal.SIGTERM)
            targets.append(pgid)
        except ProcessLookupError:
            continue
        except PermissionError:
            targets.append(pgid)
    if not targets:
        return
    deadline = time.monotonic() + max(0.0, grace_seconds)
    while time.monotonic() < deadline:
        targets = [pgid for pgid in targets if _pgid_alive(pgid)]
        if not targets:
            return
        time.sleep(0.02)
    for pgid in targets:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            continue


def _drain_stream(stream, sink: list[str]) -> None:
    try:
        if stream is not None:
            sink.append(stream.read())
    except OSError:
        pass
    finally:
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass


def _write_stdin(stream, text: str) -> None:
    try:
        if stream is not None and text:
            stream.write(text)
            stream.flush()
    except OSError:
        pass
    finally:
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass


def run_managed(
    argv: Sequence[str],
    *,
    cwd: str | Path,
    env: Mapping[str, str],
    stdin_text: str = "",
    timeout: float | None = None,
    cancel_check: Callable[[], bool] | None = None,
    grace_seconds: float = 1.0,
) -> ProcessResult:
    """Run ``argv`` in its own session/process group and clean up all descendants on exit."""
    if cancel_check is not None and cancel_check():
        return ProcessResult(
            returncode=-1,
            stdout="",
            stderr="",
            duration_seconds=0.0,
            timed_out=False,
            cancelled=True,
        )
    started = time.monotonic()
    deadline = (started + float(timeout)) if timeout is not None else None
    popen_kwargs: dict = {
        "cwd": str(cwd),
        "env": dict(env),
        "stdin": subprocess.PIPE if stdin_text else subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "errors": "replace",
    }
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True

    proc = subprocess.Popen(list(argv), **popen_kwargs)
    pgid = proc.pid
    with _LOCK:
        _ACTIVE_PGIDS.add(pgid)

    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    out_thread = threading.Thread(
        target=_drain_stream, args=(proc.stdout, stdout_parts), daemon=True
    )
    err_thread = threading.Thread(
        target=_drain_stream, args=(proc.stderr, stderr_parts), daemon=True
    )
    out_thread.start()
    err_thread.start()

    in_thread = None
    if stdin_text and proc.stdin is not None:
        in_thread = threading.Thread(
            target=_write_stdin, args=(proc.stdin, stdin_text), daemon=True
        )
        in_thread.start()

    timed_out = False
    cancelled = False
    try:
        while True:
            if cancel_check is not None and cancel_check():
                cancelled = True
                break
            now = time.monotonic()
            if deadline is not None and now >= deadline:
                timed_out = True
                break
            step = 0.05
            if deadline is not None:
                step = min(step, max(0.005, deadline - now))
            try:
                proc.wait(timeout=step)
                if cancel_check is not None and cancel_check():
                    cancelled = True
                break
            except subprocess.TimeoutExpired:
                continue
    finally:
        try:
            if os.name == "posix":
                terminate_process_group(pgid, grace_seconds=grace_seconds)
            elif proc.poll() is None:
                proc.kill()
        finally:
            with _LOCK:
                _ACTIVE_PGIDS.discard(pgid)
        try:
            proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            if os.name == "posix":
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except OSError:
                    pass
                try:
                    proc.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    pass
        if in_thread is not None:
            in_thread.join(timeout=0.5)
        out_thread.join(timeout=1.0)
        err_thread.join(timeout=1.0)

    duration = round(time.monotonic() - started, 3)
    rc = proc.returncode if proc.returncode is not None else -1
    if timed_out or cancelled:
        rc = -1
    return ProcessResult(
        returncode=rc,
        stdout="".join(stdout_parts),
        stderr="".join(stderr_parts),
        duration_seconds=duration,
        timed_out=timed_out,
        cancelled=cancelled,
    )
