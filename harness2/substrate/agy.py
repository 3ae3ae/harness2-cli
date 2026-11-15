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

"""Antigravity (`agy`) CLI substrate, event parser, and coordinator/worker telemetry."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .. import providers
from ..core import runmark as RM
from ..core import tree as HT
from ..core.tree import SurfaceSpec
from ..providers import Provider
from ..settings import ConfigError
from ._native import check_cli, cli_argv, export_files, native_tree
from .base import Install, Launcher, RunRequest

AGY_SURFACE = SurfaceSpec(
    name="agy",
    always_on=("GEMINI.md",),
    always_on_max=10000,
    always_on_cap_reason="every line enters the Antigravity system prompt on every turn",
    skill_dir=".agents/skills",
    skill_name_rule="optional_match",
    trigger_hint=(
        "Cite its `.agents/skills/{name}/SKILL.md` path in GEMINI.md with a "
        "'when to read it' sentence."
    ),
    autotrigger_file="GEMINI.md",
    trigger_line="- When {desc}, read `.agents/skills/{name}/SKILL.md`.",
    subagent_requires_mode=False,
    plugin_dir=None,
    hooks_file=None,
    compile_skill_scripts=False,
    reject_empty_always_on=False,
)

NATIVE_PATHS = ("GEMINI.md", ".agents", ".gemini")
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
LARGE_PROMPT_BYTES = 65536
_STDERR_ERROR_RE = re.compile(r"^AGY_ERROR:\s*(\{.*\})\s*$", re.M)
_CMD_EXIT_FAIL_RE = re.compile(r"Command exited with code ([1-9]\d*)")
_CONV_UUID_RE = re.compile(r'"conversation_id"\s*:\s*"([0-9a-f-]{36})"')
_AUTH_ERROR_RE = re.compile(
    r"(?i)unauthenticated|permission_denied|not authenticated|authentication failed|"
    r"auth_revoked|credential store|missing GEMINI_API_KEY|api key not valid"
)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else f"{text[:limit]}\n... (+{len(text) - limit} chars)"


def _opt_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    return None


def _opt_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def parse_events(text_or_path: Path | str) -> list[dict]:
    """Parse Antigravity JSONL or single-JSON output while flagging malformed lines.

    Multiple ``step_update`` records with the same ``(conversation_id, step_index)`` are
    folded to the latest state (preserving ``DONE`` once seen) so streaming deltas are never
    double-counted.
    """
    if isinstance(text_or_path, Path):
        if not text_or_path.is_file():
            return []
        text = text_or_path.read_text(encoding="utf-8", errors="replace").replace("\x00", "")
    else:
        text = text_or_path.replace("\x00", "")

    raw_events: list[dict] = []
    steps: dict[tuple[str, Any], int] = {}
    default_conv = ""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            raw_events.append({"event": "_malformed", "raw": stripped})
            continue
        if not isinstance(obj, dict):
            raw_events.append({"event": "_malformed", "raw": stripped})
            continue
        ev_name = obj.get("event")
        if ev_name in ("init", "result") and not default_conv:
            default_conv = str(obj.get("conversation_id") or "")
        if ev_name == "step_update" and isinstance(obj.get("step_update"), dict):
            su = obj["step_update"]
            step_idx = su.get("step_index")
            if step_idx is not None:
                conv_id = str(su.get("conversation_id") or default_conv)
                key = (conv_id, step_idx)
                prev_pos = steps.get(key)
                if prev_pos is not None:
                    prev_state = raw_events[prev_pos]["step_update"].get("state")
                    new_state = su.get("state")
                    if prev_state == "DONE" and new_state != "DONE":
                        continue
                    raw_events[prev_pos] = obj
                    continue
                steps[key] = len(raw_events)
        raw_events.append(obj)
    return raw_events


def _parse_stderr_error(stderr_text: str) -> dict | None:
    if not stderr_text:
        return None
    matches = _STDERR_ERROR_RE.findall(stderr_text)
    for raw in reversed(matches):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _tool_failed(su: dict) -> bool:
    if str(su.get("status") or "").upper() == "ERROR":
        return True
    info = su.get("tool_info")
    if not isinstance(info, dict):
        return False
    if info.get("is_error") is True or info.get("error"):
        return True
    out = str(info.get("output") or "")
    return "Encountered error" in out or bool(_CMD_EXIT_FAIL_RE.search(out))


def _nested_cli_ids(su: dict) -> list[str]:
    if su.get("tool_name") != "run_command":
        return []
    info = su.get("tool_info")
    if not isinstance(info, dict):
        return []
    params = info.get("parameters")
    cmd = str(params.get("CommandLine") or "") if isinstance(params, dict) else ""
    if not re.search(r"\bagy\b", cmd):
        return []
    out = str(info.get("output") or "")
    return list(dict.fromkeys(_CONV_UUID_RE.findall(out)))


def _transcript_usage(path: Path) -> dict | None:
    if not path.is_file():
        return None
    records: dict[tuple[Any, str], dict] = {}
    ordered: list[dict] = []
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not raw_line.strip():
            continue
        try:
            rec = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict) or rec.get("source") != "MODEL":
            continue
        idx = rec.get("step_index")
        if idx is not None:
            key = (idx, str(rec.get("source") or ""))
            records[key] = rec
        else:
            ordered.append(rec)
    final_records = list(records.values()) + ordered
    if not final_records:
        return None
    models: set[str] = set()
    in_tok = 0
    cached_tok = 0
    out_tok = 0
    reason_tok = 0
    has_in = has_cached = has_out = has_reason = False
    tool_calls = 0
    for rec in final_records:
        if rec.get("model"):
            models.add(str(rec["model"]))
        v_in = _opt_int(rec.get("input_tokens"))
        if v_in is not None:
            in_tok += v_in
            has_in = True
        v_cache = _opt_int(
            rec.get("cache_read_tokens")
            if "cache_read_tokens" in rec
            else rec.get("cached_input_tokens")
        )
        if v_cache is not None:
            cached_tok += v_cache
            has_cached = True
        v_out = _opt_int(rec.get("output_tokens"))
        if v_out is not None:
            out_tok += v_out
            has_out = True
        v_reason = _opt_int(
            rec.get("thinking_tokens") if "thinking_tokens" in rec else rec.get("reasoning_tokens")
        )
        if v_reason is not None:
            reason_tok += v_reason
            has_reason = True
        calls = rec.get("tool_calls")
        if isinstance(calls, list):
            tool_calls += len(calls)
    return {
        "models": sorted(models),
        "input_tokens": in_tok if has_in else None,
        "cached_input_tokens": cached_tok if has_cached else None,
        "output_tokens": out_tok if has_out else None,
        "reasoning_tokens": reason_tok if has_reason else None,
        "tool_calls": tool_calls,
    }


def _find_transcript(conv_id: str, brain_dirs: list[Path]) -> dict | None:
    if not conv_id:
        return None
    for brain_dir in brain_dirs:
        candidate = brain_dir / conv_id / ".system_generated/logs/transcript_full.jsonl"
        parsed = _transcript_usage(candidate)
        if parsed is not None:
            return parsed
    return None


def _sum_opt(values: list[int | None]) -> int | None:
    if not values or any(v is None for v in values):
        return None
    return sum(v for v in values if v is not None)


def extract_telemetry(
    stream_text: str,
    *,
    brain_dirs: list[Path] | None = None,
    configured_model: str = "",
    effort: str = "",
    wall_seconds: float | None = None,
    returncode: int | None = None,
    timed_out: bool = False,
    cancelled: bool = False,
    stderr_text: str = "",
    output_artifact: str = "",
    role: str = "executor",
) -> dict[str, Any]:
    """Extract normalized coordinator and worker telemetry without inventing missing fields."""
    events = parse_events(stream_text)
    malformed_lines = sum(1 for ev in events if ev.get("event") == "_malformed")
    init_ev: dict = {}
    init_conv_id = ""
    result_ev: dict = {}
    has_result = False
    done_steps: list[dict] = []

    for ev in events:
        name = ev.get("event")
        if name == "_malformed":
            continue
        if name == "init" and isinstance(ev.get("init"), dict):
            init_ev = ev["init"]
            init_conv_id = str(ev.get("conversation_id") or "")
        elif name == "result" and isinstance(ev.get("result"), dict):
            result_ev = ev["result"]
            has_result = True
        elif not name and ("status" in ev or "response" in ev) and "event" not in ev:
            result_ev = ev
            has_result = True
        elif name == "step_update" and isinstance(ev.get("step_update"), dict):
            done_steps.append(ev["step_update"])

    tool_calls = 0
    tool_errors = 0
    tool_counts: Counter[str] = Counter()
    workers_seen: dict[str, dict[str, Any]] = {}
    step_in: list[int | None] = []
    step_cache: list[int | None] = []
    step_out: list[int | None] = []
    step_reason: list[int | None] = []
    step_total: list[int | None] = []

    for su in done_steps:
        stype = su.get("step_type")
        if stype == "tool" and su.get("state") == "DONE":
            tname = str(su.get("tool_name") or "tool")
            tool_calls += 1
            tool_counts[tname] += 1
            if _tool_failed(su):
                tool_errors += 1
            for cid in _nested_cli_ids(su):
                workers_seen.setdefault(
                    cid, {"conversation_id": cid, "kind": "nested-cli", "role": ""}
                )
        elif stype == "subagent":
            sub_info = su.get("subagent_info")
            subs = sub_info.get("subagents") if isinstance(sub_info, dict) else None
            if isinstance(subs, list):
                for sub in subs:
                    if not isinstance(sub, dict):
                        continue
                    cid = str(sub.get("conversation_id") or "")
                    if cid:
                        workers_seen.setdefault(
                            cid,
                            {
                                "conversation_id": cid,
                                "kind": str(sub.get("type_name") or "subagent"),
                                "role": str(sub.get("role") or ""),
                            },
                        )
        elif stype == "agent_response" and su.get("state") == "DONE":
            u = su.get("usage")
            if isinstance(u, dict):
                step_in.append(_opt_int(u.get("input_tokens")))
                step_cache.append(
                    _opt_int(
                        u.get("cache_read_tokens")
                        if "cache_read_tokens" in u
                        else u.get("cached_input_tokens")
                    )
                )
                step_out.append(_opt_int(u.get("output_tokens")))
                step_reason.append(
                    _opt_int(
                        u.get("thinking_tokens")
                        if "thinking_tokens" in u
                        else u.get("reasoning_tokens")
                    )
                )
                step_total.append(_opt_int(u.get("total_tokens")))

    result_usage = result_ev.get("usage") if isinstance(result_ev.get("usage"), dict) else None
    if result_usage is not None:
        in_tok = _opt_int(result_usage.get("input_tokens"))
        cached_tok = _opt_int(
            result_usage.get("cache_read_tokens")
            if "cache_read_tokens" in result_usage
            else result_usage.get("cached_input_tokens")
        )
        out_tok = _opt_int(result_usage.get("output_tokens"))
        reason_tok = _opt_int(
            result_usage.get("thinking_tokens")
            if "thinking_tokens" in result_usage
            else result_usage.get("reasoning_tokens")
        )
        total_tok = _opt_int(result_usage.get("total_tokens"))
    elif step_in or step_out:
        in_tok = _sum_opt(step_in)
        cached_tok = _sum_opt(step_cache)
        out_tok = _sum_opt(step_out)
        reason_tok = _sum_opt(step_reason)
        total_tok = _sum_opt(step_total)
    else:
        in_tok = cached_tok = out_tok = reason_tok = total_tok = None

    search_brains = list(brain_dirs or [])
    worker_records: list[dict[str, Any]] = []
    worker_models: set[str] = set()
    for w in workers_seen.values():
        parsed_tr = _find_transcript(w["conversation_id"], search_brains)
        entry: dict[str, Any] = {
            **w,
            "usage_available": parsed_tr is not None,
            "models": parsed_tr["models"] if parsed_tr else [],
            "input_tokens": parsed_tr["input_tokens"] if parsed_tr else None,
            "cached_input_tokens": parsed_tr["cached_input_tokens"] if parsed_tr else None,
            "output_tokens": parsed_tr["output_tokens"] if parsed_tr else None,
            "reasoning_tokens": parsed_tr["reasoning_tokens"] if parsed_tr else None,
            "tool_calls": parsed_tr["tool_calls"] if parsed_tr else None,
        }
        worker_models.update(entry["models"])
        worker_records.append(entry)

    all_workers_available = bool(worker_records) and all(
        w["usage_available"] for w in worker_records
    )
    worker_in = (
        _sum_opt([w["input_tokens"] for w in worker_records]) if all_workers_available else None
    )
    worker_cached = (
        _sum_opt([w["cached_input_tokens"] for w in worker_records])
        if all_workers_available
        else None
    )
    worker_out = (
        _sum_opt([w["output_tokens"] for w in worker_records]) if all_workers_available else None
    )
    worker_reason = (
        _sum_opt([w["reasoning_tokens"] for w in worker_records]) if all_workers_available else None
    )
    worker_tools = (
        _sum_opt([w["tool_calls"] for w in worker_records]) if all_workers_available else None
    )

    agy_error = _parse_stderr_error(stderr_text)
    result_status = str(result_ev.get("status") or "").upper() or None
    result_error = str(result_ev.get("error") or "")
    stderr_summary = ""
    if agy_error:
        stderr_summary = str(
            agy_error.get("short_error")
            or agy_error.get("message")
            or agy_error.get("status")
            or ""
        )
    elif stderr_text.strip():
        stderr_summary = stderr_text.strip().splitlines()[-1][:300]

    error_text = result_error or stderr_summary
    finished_reason = str(
        result_ev.get("finished_reason") or result_ev.get("termination_reason") or ""
    )
    incomplete = (not has_result) or (finished_reason in ("print_timeout", "interrupted"))
    malformed = malformed_lines > 0 or (
        bool(stream_text.strip()) and not init_ev and not has_result and not done_steps
    )
    combined_err = "\n".join(filter(None, [result_error, stderr_text, json.dumps(agy_error or {})]))
    rate_limited = bool(providers.USAGE_LIMIT.search(combined_err)) or (
        bool(agy_error)
        and (
            agy_error.get("http_status") == 429
            or str(agy_error.get("status") or "").upper() == "RESOURCE_EXHAUSTED"
        )
    )
    auth_error = bool(_AUTH_ERROR_RE.search(combined_err)) or (
        bool(agy_error)
        and str(agy_error.get("status") or "").upper() in ("UNAUTHENTICATED", "PERMISSION_DENIED")
    )

    if cancelled:
        status = "cancelled"
        termination_reason: str | None = "cancelled"
    elif timed_out:
        status = "timed_out"
        termination_reason = "timed_out"
    else:
        status = result_status or (
            "ERROR" if (malformed or incomplete or (returncode or 0) != 0) else None
        )
        termination_reason = finished_reason or None

    ok = bool(
        not cancelled
        and not timed_out
        and (returncode is None or returncode == 0)
        and not malformed
        and not incomplete
        and not rate_limited
        and not auth_error
        and not agy_error
        and result_status == "SUCCESS"
    )

    reported_model = str(init_ev.get("model") or "") or None
    duration = _opt_float(result_ev.get("duration_seconds"))
    cost = _opt_float(
        result_ev.get("total_cost_usd")
        if "total_cost_usd" in result_ev
        else result_ev.get("cost_usd")
    )

    return {
        "role": role,
        "conversation_id": str(result_ev.get("conversation_id") or init_conv_id) or None,
        "configured_model": configured_model or None,
        "model": reported_model,
        "effort": effort or None,
        "status": status,
        "ok": ok,
        "returncode": returncode,
        "timed_out": timed_out,
        "cancelled": cancelled,
        "incomplete": incomplete,
        "malformed_lines": malformed_lines,
        "rate_limited": rate_limited,
        "auth_error": auth_error,
        "termination_reason": termination_reason,
        "wall_seconds": wall_seconds,
        "duration_seconds": duration,
        "cost_usd": cost,
        "input_tokens": in_tok,
        "cached_input_tokens": cached_tok,
        "output_tokens": out_tok,
        "reasoning_tokens": reason_tok,
        "total_tokens": total_tok,
        "tool_calls": tool_calls,
        "tool_errors": tool_errors,
        "tool_counts": dict(tool_counts),
        "nested_workers": {
            "invocations": len(worker_records),
            "worker_models": sorted(worker_models),
            "usage_available": all_workers_available,
            "input_tokens": worker_in,
            "cached_input_tokens": worker_cached,
            "output_tokens": worker_out,
            "reasoning_tokens": worker_reason,
            "tool_calls": worker_tools,
            "workers": worker_records,
        },
        "error": error_text or None,
        "agy_error": agy_error,
        "output_artifact": output_artifact or None,
    }


def render_events(
    events_or_path: list[dict] | Path | str,
    *,
    obs_trunc: int = 4000,
    max_chars: int = 200_000,
    args_trunc: int = 2000,
) -> str:
    """Render parsed Antigravity events into readable trajectory text."""
    events = events_or_path if isinstance(events_or_path, list) else parse_events(events_or_path)
    lines: list[str] = []
    result_ev: dict = {}
    for ev in events:
        name = ev.get("event")
        if name == "_malformed":
            continue
        if name == "init" and isinstance(ev.get("init"), dict):
            model = ev["init"].get("model")
            if model:
                lines.append(f"[INIT] model={model}")
            continue
        if name == "result" and isinstance(ev.get("result"), dict):
            result_ev = ev["result"]
            continue
        if name != "step_update" or not isinstance(ev.get("step_update"), dict):
            continue
        su = ev["step_update"]
        stype = su.get("step_type")
        if stype == "user_input":
            txt = str((su.get("user_input") or {}).get("text") or "").strip()
            if txt:
                lines.append(f"[USER]\n{_clip(txt, obs_trunc)}")
        elif stype == "agent_response":
            resp = su.get("agent_response") or {}
            txt = str(resp.get("response") or resp.get("text_delta") or "").strip()
            if txt:
                lines.append(f"[ASSISTANT]\n{_clip(txt, obs_trunc)}")
        elif stype == "tool" and su.get("state") == "DONE":
            tname = su.get("tool_name") or "tool"
            info = su.get("tool_info") or {}
            params = json.dumps(info.get("parameters") or {}, ensure_ascii=False)
            lines.append(f"[ACTION] {tname}: {_clip(params, args_trunc)}")
            out = str(info.get("output") or "").strip()
            if out:
                lines.append(f"[OBSERVATION] {_clip(out, obs_trunc)}")
        elif stype == "subagent":
            subs = (su.get("subagent_info") or {}).get("subagents") or []
            for sub in subs:
                lines.append(
                    f"[SUBAGENT] {sub.get('type_name') or 'subagent'} "
                    f"({sub.get('role') or sub.get('conversation_id') or ''})"
                )
    if result_ev:
        resp = str(result_ev.get("response") or "").strip()
        if resp:
            lines.append(f"[FINAL]\n{_clip(resp, obs_trunc)}")
        usage = result_ev.get("usage")
        if isinstance(usage, dict):
            lines.append(
                f"[TOKENS] in={usage.get('input_tokens')} "
                f"cached={usage.get('cache_read_tokens')} "
                f"out={usage.get('output_tokens')} "
                f"thinking={usage.get('thinking_tokens')}"
            )
        if result_ev.get("error"):
            lines.append(f"[ERROR] {result_ev['error']}")
    rendered = "\n\n".join(lines)
    return _clip(rendered, max_chars) if max_chars and len(rendered) > max_chars else rendered


def final_message(events_or_path: list[dict] | Path | str) -> str:
    """Extract the final coordinator response from parsed Antigravity events."""
    events = events_or_path if isinstance(events_or_path, list) else parse_events(events_or_path)
    for ev in reversed(events):
        if ev.get("event") == "result" and isinstance(ev.get("result"), dict):
            resp = ev["result"].get("response")
            if isinstance(resp, str) and resp.strip():
                return resp.strip()
        if not ev.get("event") and isinstance(ev.get("response"), str) and ev["response"].strip():
            return ev["response"].strip()
    for ev in reversed(events):
        if ev.get("event") == "step_update" and isinstance(ev.get("step_update"), dict):
            su = ev["step_update"]
            if su.get("step_type") == "agent_response":
                resp = (su.get("agent_response") or {}).get("response")
                if isinstance(resp, str) and resp.strip():
                    return resp.strip()
    return ""


def cost_usd(events_or_path: list[dict] | Path | str) -> float:
    """Return cost in USD when reported by the stream, else 0.0 for ledger accounting."""
    events = events_or_path if isinstance(events_or_path, list) else parse_events(events_or_path)
    for ev in reversed(events):
        if ev.get("event") == "result" and isinstance(ev.get("result"), dict):
            val = ev["result"].get("total_cost_usd")
            if val is None:
                val = ev["result"].get("cost_usd")
            if isinstance(val, (int, float)):
                return float(val)
    return 0.0


class AgySubstrate:
    """Executor substrate backed by the Antigravity (`agy`) CLI."""

    name = "agy"
    NATIVE_PATHS = NATIVE_PATHS

    def __init__(self, bin: str = "", launcher: Launcher | None = None):
        self.bin = bin
        self.launcher = launcher

    def surface(self) -> SurfaceSpec:
        return AGY_SURFACE

    def import_tree(self, root: Path, *, rejected: list[str] | None = None) -> HT.Tree:
        return native_tree(AGY_SURFACE, root, ("GEMINI.md", ".agents/skills", "scripts"), rejected)

    def export_tree(self, tree: HT.Tree, root: Path, *, base: HT.Tree) -> dict[str, list[str]]:
        return export_files(root, tree, base)

    def strip_native(self, workspace: Path) -> None:
        self.export_tree({}, workspace, base=self.import_tree(workspace))
        state = Path(workspace) / ".gemini"
        if state.is_symlink() or state.is_file():
            state.unlink()
        elif state.is_dir():
            shutil.rmtree(state, ignore_errors=True)
        agents_dir = Path(workspace) / ".agents"
        if agents_dir.is_dir() and not any(
            p.is_file() or p.is_symlink() for p in agents_dir.rglob("*")
        ):
            shutil.rmtree(agents_dir, ignore_errors=True)

    def materialise(self, tree_dir: Path, spec, ctx: Mapping[str, str]) -> Install:
        if spec.install_mode != "agy_root":
            raise ValueError(
                f"AgySubstrate needs install_mode 'agy_root', got {spec.install_mode!r}"
            )
        staged = Path(tempfile.mkdtemp(prefix="harness2-agy-"))
        try:
            self.export_tree(HT.read_tree(AGY_SURFACE, tree_dir), staged, base={})
            return Install(staged, RM.fingerprint(tree_dir), {"install_mode": spec.install_mode})
        except BaseException:
            shutil.rmtree(staged, ignore_errors=True)
            raise

    def install_into(self, install: Install, workspace: Path) -> None:
        self.export_tree(self.import_tree(install.staging_dir), workspace, base={})

    def execute(
        self, task_ref: str, install: Install, run_ids: list[str], timeout: int
    ) -> list[Path]:
        if self.launcher is None:
            raise RuntimeError(
                "AgySubstrate was constructed without a launcher body; pass launcher=... "
                "or run rollouts through GenericAdapter."
            )
        return self.launcher(RunRequest(task_ref, tuple(run_ids), timeout), install)

    def argv(
        self,
        prompt: str,
        model: str,
        *,
        effort: str = "",
        output_format: str = "stream-json",
        settings_file: Path | str | None = None,
        json_schema: Path | str | None = None,
    ) -> tuple[list[str], str]:
        if not model or not model.strip():
            raise ConfigError("Antigravity requires a non-empty model name.")
        if effort and effort not in EFFORT_LEVELS:
            raise ConfigError(
                f"invalid Antigravity reasoning effort {effort!r}; use one of {EFFORT_LEVELS}"
            )
        if output_format not in ("text", "json", "stream-json"):
            raise ConfigError(f"unsupported Antigravity output format {output_format!r}")
        cmd = [
            *cli_argv(self.bin, "agy"),
            "--model",
            model,
            "--output-format",
            output_format,
            "--dangerously-skip-permissions",
        ]
        if effort:
            cmd.extend(["--effort", effort])
        if settings_file is not None:
            cmd.extend(["--settings", str(settings_file)])
        if json_schema is not None:
            cmd.extend(["--json-schema", str(json_schema)])
        if output_format == "stream-json" and len(prompt.encode("utf-8")) > LARGE_PROMPT_BYTES:
            cmd.extend(["--input-format", "stream-json"])
            stdin_payload = (
                json.dumps({"event": "user", "message": {"role": "user", "content": prompt}}) + "\n"
            )
            return cmd, stdin_payload
        cmd.extend(["-p", prompt])
        return cmd, ""

    def env_and_settings(
        self,
        provider: Provider,
        run_dir: Path | str,
        cwd: Path | str,
        *,
        base_env: Mapping[str, str] | None = None,
        home_subdir: str = "agy-home",
        settings_filename: str = "agy-settings.json",
    ) -> tuple[dict[str, str], Path]:
        env = dict(os.environ if base_env is None else base_env)
        for key in providers.STRIP_ENV:
            env.pop(key, None)
        provider_env, extra_settings = providers.agy_env_and_settings(provider)
        env.update(provider_env)
        run_path = Path(run_dir)
        run_path.mkdir(parents=True, exist_ok=True)
        resolved_cwd = str(Path(cwd).resolve())
        settings_doc: dict[str, object] = {
            "artifactReviewPolicy": "always-proceed",
            "toolPermission": "always-proceed",
            "showFeedbackSurvey": False,
            "trustedWorkspaces": [resolved_cwd],
            **extra_settings,
        }
        if provider.type == "gemini":
            agy_home = run_path / home_subdir
            (agy_home / ".gemini/config").mkdir(parents=True, exist_ok=True)
            (agy_home / ".gemini/antigravity-cli").mkdir(parents=True, exist_ok=True)
            (agy_home / ".gemini/config/mcp_config.json").write_text(
                '{"mcpServers": {}}\n', encoding="utf-8"
            )
            env.update(
                {
                    "HOME": str(agy_home),
                    "XDG_CONFIG_HOME": str(agy_home / ".config"),
                    "XDG_DATA_HOME": str(agy_home / ".local/share"),
                    "XDG_STATE_HOME": str(agy_home / ".local/state"),
                    "XDG_CACHE_HOME": str(agy_home / ".cache"),
                }
            )
        settings_path = run_path / settings_filename
        settings_path.write_text(json.dumps(settings_doc, indent=2) + "\n", encoding="utf-8")
        return env, settings_path

    def read_trajectory(
        self,
        stream_path: Path,
        *,
        obs_trunc: int = 4000,
        max_chars: int = 200_000,
        args_trunc: int = 2000,
    ) -> str:
        target = Path(stream_path)
        log_file = target if target.is_file() else target / "stream.jsonl"
        if not log_file.is_file():
            return ""
        return render_events(
            log_file, obs_trunc=obs_trunc, max_chars=max_chars, args_trunc=args_trunc
        )

    def final_message(self, stream_path: Path | str) -> str:
        return final_message(stream_path)

    def cost_usd(self, stream_path: Path | str) -> float:
        return cost_usd(stream_path)

    def preflight(self) -> None:
        check_cli(self.bin, "agy")
