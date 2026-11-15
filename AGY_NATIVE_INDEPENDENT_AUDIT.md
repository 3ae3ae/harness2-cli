# Native Antigravity (`agy`) Adapter: Independent Code Audit

**Repository:** `/home/alteixeira20/Projects/harness2-cli-agy`
**Branch:** `feat/agy-native-adapter`
**Audited HEAD SHA:** `0933e3fd4d38a550cb840b230aa496ed4b7ec2e0` (`docs(agy): add native Antigravity integration report`)
**Implementation SHA:** `8ca913e5821da0fae567bfd5e3bec1a05b36007f` (`feat(agy): add native Antigravity CLI executor, improver channel, telemetry, and tests`)
**Upstream Baseline SHA:** `0c811344a4edfe34960b2fc8f286369d7e81fb27` (`main`)
**Audit Mode:** Independent verification prior to remediation and Synapse integration
**Initial Pre-Remediation Verdict:** **CHANGES REQUIRED**
**Post-Remediation Status (Commit `9ffd2a0`):** **READY FOR SYNAPSE INTEGRATION** (all confirmed P1 and P2 defects remediated and regression-tested; see `AGY_NATIVE_REMEDIATION_REPORT.md`)

---

## 1. Executive Summary

The native Antigravity (`agy`) integration in commits `8ca913e` and `0933e3f` is well structured overall, cleanly isolates credentials and `gemini` API-key runs, uses real POSIX session/process-group ownership (`harness2/substrate/_proc.py`), avoids fabricating zero costs or inventing missing token counts, and passes all 171 existing core unit tests, 9 offline AGY unit tests, 13 benchmark plugin tests, and `ruff` lint/format checks prior to edits.

However, the audit identified **two confirmed P1 defects** that block direct adoption on real repositories such as Odysseus and Synapse:

1. **`AGY_SURFACE` `script_dir="scripts"` collision (P1 - Data Integrity):** `AgySubstrate` defaults `script_dir` to `"scripts"` and `import_tree()` imports `"scripts/*.py"`, while `GenericSuite` runs `AgySubstrate.strip_native(ws)` on rollout workspaces (`harness2/suite.py:435`). On any target repository that contains its own `scripts/*.py` files (verified in both `/home/alteixeira20/Projects/odysseus/scripts` and `/home/alteixeira20/Projects/Synapse/scripts`), `strip_native` deletes the target repository's `scripts/*.py` files, and `NATIVE_PATHS` omits `"scripts"` so `workspace.diff_since` misclassifies harness scripts as project changes.
2. **Stream `text_delta` and `state="ERROR"` tool step loss (P1 - Telemetry & Trajectory Accuracy):** Real `agy 1.5.9` `--output-format stream-json` streams `agent_response` chunks in top-level `step_update.text_delta` across `ACTIVE` frames with an empty delta on the final `DONE` frame, and omits `agent_response.response`. Because `parse_events()` overwrites prior frames for a `(conversation_id, step_index)` and `render_events()`/`final_message()` only inspect `step_update["agent_response"]`, all intermediate `[ASSISTANT]` turns and incomplete-stream `agent_response` text are dropped. Additionally, tool steps ending with `state == "ERROR"` are skipped by `extract_telemetry()` and `render_events()`.

In addition, three P2 defects and several P3/coverage gaps were identified and documented below.

---

## 2. Architecture Assessment

- **Substrate contract conformance:** `AgySubstrate` (`harness2/substrate/agy.py:641-808`) implements the required `Substrate` methods (`surface`, `import_tree`, `export_tree`, `strip_native`, `materialise`, `install_into`, `execute`, `read_trajectory`, `final_message`, `cost_usd`, `preflight`), plus `argv()` and `env_and_settings()`. It raises cleanly if `install_mode != "agy_root"` (`agy.py:674-677`) or when constructed without a launcher (`agy.py:692-696`).
- **Channel and proposer integration:** `CHANNELS` in `harness2/core/spec.py:33` includes `"agy"` and `INSTALL_MODES` (`spec.py:101`) includes `"agy_root"`. `Proposer._agy` (`harness2/core/proposer.py:391-450`) builds a temporary metadata directory via `AgySubstrate.env_and_settings`, invokes `run_managed`, records `telemetry` in `self._telemetry_by_cwd[cwd]`, checks `rate_limited`, and surfaces non-empty replies cleanly.
- **Configuration cohesion:** Additions to `harness2/settings.py:77` (default `agy-login` provider), `harness2/providers.py:29-38,205-232,255-260`, `harness2/cli/doctor.py:87-92,154-175`, `harness2/cli/init.py`, and `harness2/cli/chat.py` follow existing `claude`/`codex` conventions without creating parallel config systems.
- **No benchmark-specific leakage:** `harness2/substrate/agy.py` and `harness2/providers.py` contain zero Synapse-specific or Odysseus-specific paths, MCP server definitions, or grading logic.
- **Surface leak / P1 defect:** `AGY_SURFACE` (`agy.py:38-56`) relies on `SurfaceSpec.script_dir = "scripts"` (`harness2/core/tree.py:60`) instead of an isolated dot-directory such as `.agents/scripts` (used by `CODEX_SURFACE` in `harness2/substrate/codex.py:74`), causing the project-workspace collision detailed in Section 10.

---

## 3. Security and Isolation Assessment

- **No credential leakage in logs or telemetry:** `extract_telemetry()` (`agy.py:266-536`), `meta.json`, and audit files store `configured_model`, `model`, `effort`, `conversation_id`, token metrics, and sanitized CLI error summaries. Neither full process `env` dicts nor API keys are serialized.
- **Provider isolation (`harness2/providers.py:43-56,209-231` and `agy.py:740-783`):**
  - `env_and_settings()` strips all keys in `providers.STRIP_ENV` (including `GEMINI_API_KEY`, `GOOGLE_GEMINI_BASE_URL`, `ANTHROPIC_API_KEY`, `OPENAI_BASE_URL`, `CLAUDE_CODE_USE_*`) before applying provider-specific env vars. Verified offline: `GEMINI_API_KEY` from an ambient shell is stripped from `agy-login` runs. Note: `OPENAI_API_KEY` is intentionally not in `STRIP_ENV` (matching `codex`/`claude`), and for `provider.type == "gemini"`, `providers.agy_env_and_settings()` reads `os.environ.get(p.api_key_env, "")` rather than `base_env` (P2 finding).
  - For `provider.type == "gemini"`, `env_and_settings()` redirects `HOME`, `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `XDG_STATE_HOME`, and `XDG_CACHE_HOME` to a per-run `run_dir/agy-home` and sets `"modelProvider": "gemini"` in the session `--settings` file (`agy.py:765-783`). It writes `{"mcpServers": {}}` to `.gemini/config/mcp_config.json`, though `.gemini/antigravity-cli/mcp_config.json` is not also seeded (P2 finding).
  - Session settings are passed via `--settings <run_dir>/agy-settings.json` (never modifying `~/.gemini/settings.json`). `agy 1.5.9` accepts `--settings` as a session-only file or inline JSON without persisting it.
- **Login profile isolation (`agy-login`):** Because `agy 1.5.9` has no `--strict-mcp-config` flag, `agy-login` relies on the configured `provider.home` (`HOME` / `XDG_*`) or an external isolated wrapper binary (such as `agy06`). `harness2 doctor` inspects custom `agy-login` homes and warns if personal `GEMINI.md`, `AGENTS.md`, `config/hooks.json`, or `config/mcp_config.json` exist (`doctor.py:154-175`), though it misses `.gemini/antigravity-cli/mcp_config.json` (P3 finding).
- **Important host-wrapper operational constraint (verified in `/home/alteixeira20/.local/bin/agy06` and `/home/alteixeira20/.local/bin/agy06-profile-start`):** `agy06` invokes `agy06-profile-start`, which launches a persistent per-profile `dbus-daemon` and `gnome-keyring-daemon` via `&` if not already alive. If those background daemons are first started inside a `_proc.run_managed` child session, `run_managed`'s `finally: terminate_process_group(pgid)` will kill the profile's keyring when that task ends, disrupting concurrent runs on that profile. Profile keyring sessions (`<profile>-profile-start`) must therefore be started outside `run_managed` before concurrent benchmark execution.
- **Sandbox & workspace boundary:** Session settings include `"trustedWorkspaces": [resolved_cwd]`, `"artifactReviewPolicy": "always-proceed"`, `"toolPermission": "always-proceed"`, and `"showFeedbackSurvey": False`. No code weakens `normalize_rel()` or workspace diff exclusion except the `AGY_SURFACE` `scripts/` omission noted under P1. Note that `--dangerously-skip-permissions` is passed to headless CLI calls (`agy.py:723`) intentionally for unattended rollouts and improver calls inside ephemeral/git workspaces, which matches `claude.py` and `codex.py`.
- **Read-only mode check:** No silent read-only-to-writable coercion was introduced by the AGY adapter; `Harness` editing permissions remain controlled by `Proposer` (`context/harness/`) and `GenericSuite` (`deliverable.patch` / read-only tasks). However, `Proposer._agy` uses `--dangerously-skip-permissions` in temporary `harness2-prop-*` directories populated only with `context/`, matching `Proposer._claude` and `Proposer._codex`.

---

## 4. Process Lifecycle and Concurrency Assessment

Reviewed `harness2/substrate/_proc.py:1-226` and `harness2/core/events.py:1-121` and executed deterministic fake-process checks:

- **Process-group ownership (`_proc.py:142-148`):** On POSIX, `run_managed()` spawns `subprocess.Popen` with `start_new_session=True` so `pgid = proc.pid` defines a new session and process group isolated from the parent coordinator, user terminal sessions, and external `agy` processes.
- **External process isolation verified:** In deterministic offline runs, spawning an unrelated process group (`start_new_session=True`), adding two active process groups to `_ACTIVE_PGIDS`, and triggering `events.request_stop("multi")` terminated both tracked groups (`returncode=-15`) while leaving the unrelated process group running untouched.
- **SIGTERM -> SIGKILL escalation & grandchild cleanup verified:** A fake parent + grandchild both configured with `signal.SIG_IGN` on `SIGTERM` were terminated via `SIGKILL` upon `timeout=0.3, grace_seconds=0.2`; after return, `os.kill(gc_pid, 0)` confirmed the grandchild was dead and `_proc.active_process_groups()` was empty `()`.
- **Timeout, repeated cancellation, and launcher failure:** Repeated `terminate_process_group` and `terminate_active` calls are idempotent no-ops when groups are already dead. Missing binary raises `FileNotFoundError` cleanly with `_ACTIVE_PGIDS` empty (though `Proposer._agy` lacks `except OSError` around `run_managed`, unlike `suite.py:506`).
- **Process-global cancellation caveat (`events.py:98-110`):** `_stopped` and `_ACTIVE_PGIDS` are coordinator-process globals. Cancelling one `harness2` CLI invocation (`events.request_stop`) stops all active children of that `harness2` process and never touches external processes; however, running two independent experiments concurrently inside the *same* Python interpreter process would share `_stopped` and `_ACTIVE_PGIDS`.
- **Confirmed lifecycle defect (P2):** `_proc.run_managed()` does not check `cancel_check()` before `subprocess.Popen()` (`_proc.py:145`), so a call invoked while stop is already requested still spawns the child process before killing it in iteration 1. Additionally, `terminate_active()` (`_proc.py:85-88`) terminates groups sequentially (`0.5s` grace wait per PGID) rather than fanning out `SIGTERM` across all active PGIDs first.
- **Residual PID/PGID reuse note:** On normal exit (`_proc.py:183`), `proc.wait(timeout=step)` reaps the session leader PID before `terminate_process_group(pgid)` (`_proc.py:192`) and `_ACTIVE_PGIDS.discard(pgid)` (`_proc.py:197`). If no other members survive in the PGID, a theoretical kernel PID wrap-around race exists during the microsecond window before `_ACTIVE_PGIDS.discard(pgid)`.

---

## 5. Telemetry and NDJSON Parsing Assessment

Reviewed `parse_events()`, `extract_telemetry()`, `_transcript_usage()`, `_find_transcript()`, `render_events()`, `final_message()`, and `cost_usd()` in `harness2/substrate/agy.py:90-639` against real `agy 1.5.9` streams in `/home/alteixeira20/Projects/Synapse-competitive-benchmark/results/*/stream.jsonl` and real transcripts in `/home/alteixeira20/.local/share/agy03-profile/home/.gemini/antigravity-cli/brain/`.

- **Double-counting prevention:** Confirmed for tokens and tool counts when `step_type == "tool"` finishes in `DONE` and `result.usage` is present. `parse_events()` folds multiple `step_update` records sharing `(conversation_id, step_index)` instead of summing streaming deltas, and `extract_telemetry()` prefers `result.usage` over per-step sums.
- **Cached vs uncached input tokens:** Properly separated into `input_tokens` and `cached_input_tokens` (reading `cache_read_tokens` or `cached_input_tokens`) and `reasoning_tokens` (reading `thinking_tokens` or `reasoning_tokens`).
- **Unknown vs zero preservation:** Confirmed that missing `cost_usd` remains `None` in telemetry (`cost_usd=null`), and missing worker transcripts yield `usage_available=False` and `None` token fields rather than `0`. (Note: `agy.cost_usd()` helper returns `0.0` solely for `harness2` budget ledger arithmetic as documented.)
- **Failed / incomplete / malformed detection:** When a stream ends after `step_update` with no `result` event and trailing malformed JSON (`"{malformed json"`), verified `ok=False`, `incomplete=True`, `malformed_lines=1`, `status="ERROR"`, `cost_usd=None`, while partial `agent_response` `DONE` usage tokens remain surfaced (`input_tokens=100, output_tokens=20, total_tokens=120`).
- **Confirmed real-stream schema mismatches (P1 & P2):**
  1. **P1 - Lost `text_delta` and `state="ERROR"` tool steps:** Real `agy 1.5.9` `step_update` events place streamed assistant text in top-level `step_update["text_delta"]` across `ACTIVE` updates (often empty on `DONE`) and do not include a nested `agent_response` object. `parse_events()` overwrites `raw_events[prev_pos]` instead of accumulating `text_delta`, and `render_events()`/`final_message()` ignore top-level `text_delta`. Separately, `extract_telemetry()` (`agy.py:317`) and `render_events()` (`agy.py:575`) check `su.get("state") == "DONE"`, ignoring tool steps with `state == "ERROR"`.
  2. **P2 - Nested wrapper detection & transcript fallback:** `_nested_cli_ids()` (`agy.py:175`) checks `re.search(r"\bagy\b", cmd)`, missing wrapper binaries such as `agy06` or `/home/.../agy03` (verified offline: `nested_workers.invocations == 0`). `_find_transcript()` (`agy.py:253`) only reads `transcript_full.jsonl` and ignores `transcript.jsonl`, contrary to `docs/antigravity.md:112`.
  3. **Real `transcript_full.jsonl` field observation:** Inspected completed `agy03` transcripts and verified that real `MODEL` entries (`PLANNER_RESPONSE`) carry `input_tokens`, `output_tokens`, `cache_read_tokens`, and `tool_calls`, while `model` is omitted and `thinking_tokens` is absent (`worker_reasoning_tokens` remains `None` and `worker_models` stays `[]`). `extract_telemetry()` handles those missing fields accurately without fabricating zeros.
- **Scoped transcript lookup:** `_find_transcript` only searches `brain_dirs` derived from the active `env["AGY_BRAIN"]` or `env["HOME"] / ".gemini/antigravity-cli/brain"` for `conversation_id` UUIDs specifically observed in `run_command` or `subagent` tool events of the current stream, preventing scanning of unrelated user sessions.

---

## 6. Antigravity (`agy` 1.5.9) CLI Compatibility

Verified directly against installed `/home/alteixeira20/.local/bin/agy` (`1.5.9`) without paid model benchmarks (plus one tiny `hi` probe during effort validation and binary string/tag inspection):

| Capability | Expected / Adapter Usage | Verified Installed CLI `1.5.9` Behavior | Status |
|---|---|---|---|
| `--model` | `gemini-3.8-flash-high`, `gemini-4-hydrogen-high`, etc. | `agy models` lists exact slug IDs; unknown model exits `1` with JSON `result` `ERROR` and `finished_reason="error"` (`duration_seconds=0`). Display names fail with `--effort`. | Compatible (slug names required) |
| `--effort` | `EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")` | Invalid value yields CLI error `(valid: low, medium, high, xhigh, max)`; `--model gemini-3.8-flash-high --effort high` succeeded; `--model gemini-3.8-flash-low --effort max` exited `1` locally (`conflicts with --effort=max`). | Compatible |
| `-p` prompt | Small prompts (`<= 65536` UTF-8 bytes) | Supported (`-p` / `--print` alias). | Compatible |
| `--input-format stream-json` | Prompts `> 65536` bytes with `output_format="stream-json"` emitting `{"event":"user","message":{"role":"user","content":prompt}}` | CLI `--help` and binary `third_party/jetski/cli/printmode/streaminput.go` confirm NDJSON expects `"event": "user"`, `"message": {"content": ...}`, and requires `--output-format stream-json`. | Compatible (verified via binary schema; live execution untested) |
| `--output-format` | `stream-json`, `json`, `text` | Supported (`init`, `step_update`, `result` NDJSON events). | Compatible |
| `--settings` | Path to per-run `agy-settings.json` | Accepts file path or inline JSON; non-persistently scoped to session. Missing path fails cleanly. | Compatible |
| `--json-schema` | Supported in `AgySubstrate.argv` | Confirmed present in `agy --help`. | Compatible |
| `--dangerously-skip-permissions` | Passed on CLI calls | Confirmed present in `agy --help`. | Compatible |
| `--strict-mcp-config` | Not passed | Confirmed absent from `agy --help`. | Correctly omitted |
| `AGY_ERROR:` stderr JSON | Parsed by `_parse_stderr_error` (`status`, `http_status`, `short_error`) | Confirmed in CLI `changelog` / binary: headless failures emit `AGY_ERROR: {...}` JSON line on stderr and exit `3`. | Compatible |
| `finished_reason` | Checked for `"print_timeout"`, `"interrupted"` | Confirmed in CLI changelog (`print_timeout`, `interrupted`, `error`, or omitted on success) and observed in historical `pilot` / `timeout` `stream.jsonl` files. | Compatible |

---

## 7. Test Verification & Backward Compatibility

Executed prior to any repository modifications (under Python 3.12 `.venv`):

- `.venv/bin/python -B -m unittest tests/test_agy.py -v`: **9 tests passed** in `2.489s`.
- `.venv/bin/python -B -m unittest discover -s tests`: **171 tests passed** in `43.400s`.
- `.venv/bin/python -B -m unittest discover -s plugins/harness2-benchmarks/tests`: **13 tests passed** in `0.937s`.
- `ruff check harness2 tests plugins && ruff format --check harness2 tests plugins`: **All checks passed; 96 files already formatted**.

**Backward compatibility:** Existing substrates (`ClaudeSubstrate`, `CodexSubstrate`, `OpenCodeSubstrate`, `command` executor) and all 162 pre-existing core unit tests + 13 plugin tests remain passing without interface regressions. `claude`/`codex`/`opencode` still cleanly reject `agy-login` in cross-channel checks (`tests/test_agy.py:305-315`).

---

## 8. Live Verification Assessment (`AGY_NATIVE_INTEGRATION_REPORT.md`)

`AGY_NATIVE_INTEGRATION_REPORT.md` records two live smoke runs (`eval` and `run` under `agy06` with `gemini-3.8-flash-low`):

- **What those smoke tests actually establish:**
  - **Executor validation (partial):** Confirmed `agy06` (`gemini-3.8-flash-low`) completed baseline/candidate executor rollouts on a synthetic fixture task via `AgySubstrate` and wrote `stream.jsonl` and `telemetry.json`.
  - **Improver validation (partial / wrong model tier):** Confirmed `Proposer._agy` mechanically completed `improver`/`selector` calls on `gemini-3.8-flash-low`, **not** the target Gemini 4 Hydrogen High improver model (`gemini-4-hydrogen-high`).
  - **Real model identity:** Only `gemini-3.8-flash-low` was observed live.
  - **Telemetry validation:** Only coordinator token counts (`gemini-3.8-flash-low`) were exercised; nested worker invocation telemetry, `text_delta` multi-turn rendering, and `gemini` API-key mode were **not** exercised live.
  - **Cancellation & timeout validation:** Not exercised live; covered only by offline `tests/test_agy.py` fakes (`stop_requested` + fake sleeper subprocess).
  - **Profile isolation validation:** Tested `agy06` login isolation only, not `provider.type == "gemini"` live API-key isolation.
- **Is a Gemini 4 Hydrogen High smoke test required before Synapse?** Not strictly blocking for Stage C/D/E **integration** (where `AGY` is the benchmark executor on frozen tasks without Harness² optimization), **provided** the confirmed P1 defects (`scripts/` collision and `text_delta`/`ERROR` tool handling) are fixed and verified offline first, and a single safe end-to-end smoke of the integrated benchmark path passes. A dedicated Gemini 4 Hydrogen High improver smoke **will** be needed before launching future Harness² instruction-optimization runs.

---

## 9. Synapse Integration Readiness

Inspected `/home/alteixeira20/Projects/Synapse-competitive-benchmark/` read-only:

- **Repository ownership finding:** `Synapse-competitive-benchmark/` is **not** a Git repository (`git rev-parse --git-dir` fails). Directories `odysseus-vanilla/`, `odysseus-synapse/`, and `odysseus-serena/` are linked Git worktrees attached to `/home/alteixeira20/Projects/odysseus/` at commit `0276e15f6e2e7a4a692d130243f2a02c3f57ffe0`.
- **Exposed integration seam:** `AgySubstrate` (`argv`, `env_and_settings`, `extract_telemetry`, `final_message`, `read_trajectory`) and `harness2.substrate._proc.run_managed` provide clean library seams that `Synapse-competitive-benchmark` can invoke directly from an opt-in adapter module alongside `GenericSuite` compatibility later, **once the confirmed P1 defects below are fixed** (`scripts/` collision in particular would directly break `odysseus` worktrees if `AgySubstrate.strip_native` were ever run on them, since `/home/alteixeira20/Projects/odysseus/scripts` contains >60 `.py` files!).

---

## 10. Confirmed Findings

### 10.1 Confirmed Blockers (P0 / P1)

#### Finding 1 (P1): `AGY_SURFACE` `script_dir="scripts"` collides with project `scripts/` directories and deletes repo files during rollouts
- **Problem:** `AGY_SURFACE` (`harness2/substrate/agy.py:38-56`) leaves `script_dir` unset, inheriting `SurfaceSpec`'s default `"scripts"` (`harness2/core/tree.py:60`), and `AgySubstrate.import_tree()` (`agy.py:655`) explicitly scans `"scripts"`. Simultaneously, `NATIVE_PATHS` is `("GEMINI.md", ".agents", ".gemini")` (`agy.py:58`), omitting `"scripts"`.
- **Impact:** In `GenericSuite.rollout()` (`harness2/suite.py:396, 435, 559`), `strip_native(ws)` deletes pre-existing project `scripts/*.py` files from target repositories (including both `/home/alteixeira20/Projects/odysseus/scripts` and `/home/alteixeira20/Projects/Synapse/scripts`), and `workspace.diff_since` misattributes harness script edits as deliverable project diffs.
- **Exact location:** `harness2/substrate/agy.py:38-58, 654-662`; `harness2/core/tree.py:60`; `harness2/core/prompts.py:725, 746, 845-855`.
- **Supporting evidence:** Verified via Python test in a temporary workspace containing `GEMINI.md` + `scripts/build.py` + `scripts/run.sh`: `AgySubstrate().import_tree(root)` imported `scripts/build.py`, and `AgySubstrate().strip_native(root)` deleted `scripts/build.py` (`exists: False`) while leaving `scripts/run.sh`. Also verified `/home/alteixeira20/Projects/odysseus/scripts` and `/home/alteixeira20/Projects/Synapse/scripts` contain dozens of `.py` files. Both sibling project-root substrates (`ClaudeSubstrate` and `CodexSubstrate`) avoid `"scripts"` by using `".claude/scripts"` and `".agents/scripts"`.
- **Minimal correction:** Set `script_dir=".agents/scripts"` on `AGY_SURFACE`, scan `("GEMINI.md", ".agents/skills", ".agents/scripts")` in `AgySubstrate.import_tree()`, update `prompts.py` AGY surface strings (`_SURFACE_AGY_HEAD`, `_TTS_SURFACE_HEAD_AGY`, `_build_surface_texts()["agy"]`) and `docs/antigravity.md`, update `tests/test_agy.py`, and add a regression test confirming project `scripts/*.py` files survive `strip_native`/`install_into`/`diff_since`.

#### Finding 2 (P1): `parse_events`, `render_events`, and `final_message` drop real `agy 1.5.9` `text_delta` content and `state="ERROR"` tool steps
- **Problem:**
  1. Real `agy 1.5.9` `--output-format stream-json` emits `step_update` `agent_response` frames with top-level `"text_delta"` across `"ACTIVE"` events and an empty string on `"DONE"` (with no `"agent_response"` sub-dict). `parse_events()` (`agy.py:134`) replaces `raw_events[prev_pos] = obj`, discarding earlier `text_delta` chunks, and `render_events()` (`agy.py:571-572`) / `final_message()` (`agy.py:622`) only check `su.get("agent_response")`.
  2. `extract_telemetry()` (`agy.py:317`) and `render_events()` (`agy.py:575`) filter tools with `su.get("state") == "DONE"`, dropping any tool step that ends with `state == "ERROR"` (or other terminal non-`ACTIVE` failure state).
- **Impact:** Improver trajectory rendering (`read_trajectory`) loses assistant reasoning/text between tool calls on real `agy 1.5.9` runs; `final_message()` returns `""` on streams truncated before `result`; and tool failures with `state="ERROR"` are omitted from `tool_calls`, `tool_errors`, `tool_counts`, and trajectories.
- **Exact location:** `harness2/substrate/agy.py:122-137, 317, 570-582, 618-625`.
- **Supporting evidence:** Inspected real `agy 1.5.9` output (`task-106` and `/home/alteixeira20/Projects/Synapse-competitive-benchmark/results/auth-sec-01_synapse120rc1_20261114_205651/stream.jsonl`): `ACTIVE` updates carry `"text_delta"`, while `DONE` `agent_response` has keys `['conversation_id', 'duration_seconds', 'state', 'step_index', 'step_type', 'usage']` with no `"agent_response"` object. Offline test with `step_type="tool", state="ERROR", status="ERROR"` produced `tool_calls=0, tool_errors=0, tool_counts={}`.
- **Minimal correction:** Accumulate `text_delta` segments across folded `step_update` records in `parse_events()` while keeping latest fields and `"DONE"` monotonicity; read top-level accumulated `text_delta` / `text` as fallback in `render_events()` and `final_message()`; count/render tool steps for terminal states `!= "ACTIVE"` (or `("DONE", "ERROR", "FAILED")`) without counting streaming `"ACTIVE"` updates twice.

### 10.2 Confirmed Nonblocking Defects (P2 / P3)

#### Finding 3 (P2): `_proc.run_managed` spawns child processes even when already cancelled, and `terminate_active` waits sequentially
- **Problem:** `run_managed()` (`harness2/substrate/_proc.py:145-175`) invokes `subprocess.Popen` before checking `cancel_check()`, and `terminate_active()` (`_proc.py:85-88`) calls `terminate_process_group(pgid, grace_seconds=grace_seconds)` sequentially per PGID.
- **Impact:** Calling `run_managed` after `events.stop_requested()` is set briefly spawns an unwanted child process before killing it; stopping `N` stubborn child groups waits up to `N * grace_seconds`.
- **Exact location:** `harness2/substrate/_proc.py:85-88, 145-175`.
- **Supporting evidence:** Code inspection plus offline `terminate_active()` timing (`1.005s` for two `sleep(30)` processes).
- **Minimal correction:** Check `cancel_check()` at entry to `run_managed()` before spawning `Popen` (returning `ProcessResult(returncode=-1, stdout="", stderr="", duration_seconds=0.0, cancelled=True)`), and in `terminate_active()` send `SIGTERM` to all active PGIDs up front before waiting up to one shared `grace_seconds` deadline and escalating survivors to `SIGKILL`.

#### Finding 4 (P2): `_nested_cli_ids` misses profile wrapper binaries (`agy06`, `agy03`) and `_find_transcript` skips `transcript.jsonl`
- **Problem:** `_nested_cli_ids()` (`agy.py:175`) requires `re.search(r"\bagy\b", cmd)`, and `_find_transcript()` (`agy.py:253`) only inspects `transcript_full.jsonl`.
- **Impact:** Nested worker invocations run through profile wrappers such as `agy06 -p ...` or `/home/.../agy03 -p ...` report `nested_workers.invocations == 0`, and sessions where only `transcript.jsonl` exists fail transcript attribution even though `docs/antigravity.md:112` documents fallback to both files.
- **Exact location:** `harness2/substrate/agy.py:167-178, 249-257`.
- **Supporting evidence:** Offline test with `CommandLine: "agy06 -p hi --output-format json"` produced `nested_workers: {'invocations': 0, ...}`. Verified on 482 real sessions under `agy03-profile` that `transcript.jsonl` carries the same token fields (`input_tokens`, `output_tokens`, `cache_read_tokens`) as `transcript_full.jsonl`.
- **Minimal correction:** Match wrapper binary names like `agy\d*` (`re.search(r"(?:^|[/\s\"'=])(?:agy\d*|agy)(?:\b|$)", cmd)` / basename match) in `_nested_cli_ids()`, and fall back to `transcript.jsonl` when `transcript_full.jsonl` is absent or empty in `_find_transcript()`.

#### Finding 5 (P2): `providers.agy_env_and_settings` ignores `base_env` for `GEMINI_API_KEY` and `env_and_settings` omits `.gemini/antigravity-cli/mcp_config.json`
- **Problem:** `AgySubstrate.env_and_settings(..., base_env=...)` delegates to `providers.agy_env_and_settings(provider)`, which reads `os.environ.get(p.api_key_env, "")` (`harness2/providers.py:225`) regardless of `base_env`, and sets `env["GEMINI_API_KEY"] = ""` if missing in `os.environ`. Also, for `provider.type == "gemini"`, `agy.py:768-771` writes `mcp_config.json` in `.gemini/config/` but leaves `.gemini/antigravity-cli/mcp_config.json` uncreated.
- **Impact:** Callers passing custom `base_env` (such as tests or isolated runners) cannot supply `api_key_env` via `base_env`, and `gemini` API-key runs do not explicitly blank `.gemini/antigravity-cli/mcp_config.json`.
- **Exact location:** `harness2/providers.py:222-228`; `harness2/substrate/agy.py:750-780`.
- **Supporting evidence:** Verified offline: calling `sub.env_and_settings(Provider("gemini", "gemini", api_key_env="GEMINI_API_KEY"), ..., base_env={"GEMINI_API_KEY": "secret"})` produced `GEMINI_API_KEY == ""`.
- **Minimal correction:** Resolve `p.api_key_env` from `(os.environ if base_env is None else base_env)` when present (or preserve stripped value cleanly via `env_and_settings`) and write `{"mcpServers": {}}\n` to both `.gemini/config/mcp_config.json` and `.gemini/antigravity-cli/mcp_config.json` for `provider.type == "gemini"`.

#### Finding 6 (P3): `doctor` Antigravity home check misses `.gemini/antigravity-cli/mcp_config.json` and `Proposer._agy` lacks `except OSError` around `run_managed`
- **Problem:** `harness2/cli/doctor.py:159-165` checks `.gemini/config/mcp_config.json` but not `.gemini/antigravity-cli/mcp_config.json`. Separately, `Proposer._agy` (`harness2/core/proposer.py:405`) does not catch `OSError` from `run_managed`, causing `Proposer.call` to retry missing-binary failures with backoff sleeps instead of failing immediately with error telemetry.
- **Exact location:** `harness2/cli/doctor.py:159-165`; `harness2/core/proposer.py:405`.
- **Minimal correction:** Add `gemini_dir / "antigravity-cli/mcp_config.json"` to `doctor.py` personal-file checks and catch `OSError` cleanly in `Proposer._agy` (or raise `ProposerError` without sleeping on `FileNotFoundError`).

### 10.3 Missing Test Coverage

1. **Workspace `scripts/` collision regression:** No test in `tests/test_agy.py` places pre-existing project `scripts/*.py` files in a workspace and verifies `AgySubstrate.strip_native`, `install_into`, and `workspace.diff_since` leave them intact.
2. **Process-group stress tests (`tests/test_agy.py`):** No dedicated test covers multiple concurrent active PGIDs under `terminate_active()`, `SIGTERM`-ignoring grandchild escalation to `SIGKILL`, `cancel_check` true at startup, repeated idempotent cancellation, or unrelated external PGID survival.
3. **Real `agy 1.5.9` stream shapes:** `tests/test_agy.py` tests `agent_response` as a nested dict rather than top-level streaming `text_delta` updates across `ACTIVE`/`DONE` frames, and does not test `tool` steps with `state="ERROR"`, `agy06` wrapper nested CLI IDs, or `transcript.jsonl` fallback.
4. **Live Gemini 4 Hydrogen High improver & `gemini` API-key mode:** Neither has been exercised live.

### 10.4 Unverified Assumptions

1. Live execution of `--input-format stream-json` (`LARGE_PROMPT_BYTES > 65536`) against `agy 1.5.9`: payload JSON schema (`{"event":"user","message":{"role":"user","content":...}}`) was confirmed in the installed Go binary strings (`printmode.StreamInputUserMessage`), not by sending a `>64 KiB` live prompt to the API.
2. Live execution of `provider.type == "gemini"` (`GEMINI_API_KEY` + `"modelProvider": "gemini"` settings + isolated `agy-home`): tested only against fake executables in `tests/test_agy.py`.
3. Live `Gemini 4 Hydrogen High` improver/selector behavior in `harness2 run`.

---

## 11. Residual Risks

1. **External profile keyring daemon lifetime (`agy06-profile-start`):** If a wrapper starts `gnome-keyring-daemon` inside a `run_managed` child session, cleanup on task completion will terminate the shared profile keyring. Callers using wrapper profiles should ensure `<profile>-profile-start` runs before launching worker pools.
2. **Process-global cancellation (`events.py`):** Running multiple independent Harness² `Suite` instances concurrently within a single Python interpreter process shares global `_stopped` and `_ACTIVE_PGIDS` state. Separate OS processes remain fully isolated.
3. **Kernel PID wrap-around window (`_proc.py:183-197`):** Between reaping `proc.pid` at `proc.wait()` and discarding `pgid` from `_ACTIVE_PGIDS`, extreme kernel PID wrap-around could theoretically collide; negligible in standard 22-bit Linux PID spaces but worth keeping in mind.
4. **Heuristic `_tool_failed` text matching (`agy.py:164`):** Searching `"Encountered error"` or `"Command exited with code [1-9]"` in tool output could misclassify a successful `view_file` or `grep_search` whose inspected text happens to contain those substrings.

---

## 12. Recommended Next Action

**Remediate Findings 1 through 5 (and optionally 6) in `/home/alteixeira20/Projects/harness2-cli-agy` on `feat/agy-native-adapter`, add regression tests for all fixed behaviors and `_proc` stress cases, re-verify the full test and lint suite, write `AGY_NATIVE_REMEDIATION_REPORT.md`, commit locally, and then proceed to Synapse Competitive Benchmark Git initialization and adapter integration.**
