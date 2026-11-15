# Native Antigravity (`agy`) Adapter: Remediation Report

**Repository:** `/home/alteixeira20/Projects/harness2-cli-agy`
**Branch:** `feat/agy-native-adapter`
**Audited Baseline Commits:** `8ca913e5821da0fae567bfd5e3bec1a05b36007f`, `0933e3fd4d38a550cb840b230aa496ed4b7ec2e0`
**Code Remediation Commit SHA:** `9ffd2a01b3800adc844e48793e6d0efdebed4ebf`
**Post-Remediation Readiness:** **READY FOR SYNAPSE INTEGRATION**

---

## 1. Original Audit Summary

An independent audit (`AGY_NATIVE_INDEPENDENT_AUDIT.md`) reviewed commits `8ca913e` and `0933e3f` against upstream `0c81134`, `agi` (`1.5.9`) installed CLI behavior, real benchmark `stream.jsonl` and `transcript_full.jsonl` artifacts, and deterministic lifecycle/isolation probes. While core architecture, credential stripping, POSIX process-group isolation, and unknown-metric preservation were sound, the audit identified two P1 blockers, three P2 defects, and one P3 improvement.

---

## 2. Confirmed Defects & Corrections Implemented

### 2.1 Finding 1 (P1): `AGY_SURFACE` `scripts/` workspace collision
- **Problem:** `AGY_SURFACE` defaulted `script_dir` to `"scripts"` and `AgySubstrate.import_tree()` imported `scripts/*.py`, while `NATIVE_PATHS` omitted `"scripts"`. Calling `strip_native(ws)` on target repositories with existing `scripts/*.py` files (including `/home/alteixeira20/Projects/odysseus/scripts` and `/home/alteixeira20/Projects/Synapse/scripts`) deleted target repository scripts.
- **Correction:**
  - `harness2/substrate/agy.py`: Set `script_dir=".agents/scripts"` on `AGY_SURFACE` and updated `AgySubstrate.import_tree()` to scan `("GEMINI.md", ".agents/skills", ".agents/scripts")` (covered by `NATIVE_PATHS = ("GEMINI.md", ".agents", ".gemini")`).
  - `harness2/core/prompts.py`: Updated AGY surface tables and levers (`_SURFACE_AGY_HEAD`, `_TTS_SURFACE_HEAD_AGY`, `_build_surface_texts()["agy"]`) to reference `.agents/scripts/`.
  - `docs/antigravity.md`: Updated Section 4 documented script surface to `.agents/scripts/`.

### 2.2 Finding 2 (P1): Real `agy 1.5.9` `text_delta` stream loss and `state="ERROR"` tool omission
- **Problem:** `agy 1.5.9` streams `agent_response` text via top-level `step_update.text_delta` across `ACTIVE` frames and typically sends an empty `text_delta` on `DONE` without a nested `agent_response` dict. `parse_events()` overwrote prior frames for a `(conversation_id, step_index)`, and `render_events()`/`final_message()` ignored top-level `text_delta`. Separately, `extract_telemetry()` and `render_events()` skipped `step_type == "tool"` events ending in `state == "ERROR"`.
- **Correction (`harness2/substrate/agy.py`):**
  - Updated `parse_events()` to accumulate string `text_delta` chunks across folded `step_update` records while maintaining `"DONE"` monotonicity.
  - Updated `render_events()` and `final_message()` to fall back to `su.get("text_delta")` alongside nested `agent_response` fields.
  - Added `_is_terminal_tool(su)` covering terminal states `DONE`, `ERROR`, `FAILED`, `CANCELLED` (or non-`ACTIVE` with `status == "ERROR"`), applied consistently in `extract_telemetry()` and `render_events()`.

### 2.3 Finding 3 (P2): Pre-cancelled subprocess launch and sequential `terminate_active`
- **Problem:** `_proc.run_managed()` spawned `subprocess.Popen` before checking `cancel_check()`, and `_proc.terminate_active()` terminated active PGIDs sequentially.
- **Correction (`harness2/substrate/_proc.py`):**
  - Added an early `cancel_check()` guard before `subprocess.Popen()` returning `ProcessResult(returncode=-1, stdout="", stderr="", duration_seconds=0.0, timed_out=False, cancelled=True)`.
  - Updated `terminate_active()` to deliver `SIGTERM` across all live tracked PGIDs first, wait up to one shared `grace_seconds` deadline, and escalate remaining live PGIDs to `SIGKILL`.

### 2.4 Finding 4 (P2): Wrapper CLI nested worker detection and `transcript.jsonl` fallback
- **Problem:** `_nested_cli_ids()` only matched `\bagy\b` (missing wrappers like `agy06` or `agy03`), and `_find_transcript()` only checked `transcript_full.jsonl` while ignoring `transcript.jsonl`.
- **Correction (`harness2/substrate/agy.py`):**
  - Replaced `\bagy\b` with `_AGY_CMD_RE` matching `agy` and numbered wrappers such as `agy06` / `/path/to/agy03`.
  - Updated `_find_transcript()` to try `transcript_full.jsonl` then `transcript.jsonl`.

### 2.5 Finding 5 (P2): `base_env` API key forwarding and `antigravity-cli/mcp_config.json` isolation
- **Problem:** `providers.agy_env_and_settings()` always read `os.environ` instead of `base_env` for `gemini` API-key providers, and `AgySubstrate.env_and_settings()` omitted `.gemini/antigravity-cli/mcp_config.json` when initializing isolated `gemini` homes.
- **Correction (`harness2/providers.py`, `harness2/substrate/agy.py`):**
  - Added optional `base_env: Mapping[str, str] | None = None` to `providers.agy_env_and_settings()` without changing single-argument callers, only populating `GEMINI_API_KEY` when non-empty.
  - Passed `source_env` from `AgySubstrate.env_and_settings()` and seeded `{"mcpServers": {}}\n` in both `.gemini/config/mcp_config.json` and `.gemini/antigravity-cli/mcp_config.json`.

### 2.6 Finding 6 (P3): `doctor` MCP inspection and `Proposer._agy` `OSError` telemetry capture
- **Correction (`harness2/cli/doctor.py`, `harness2/core/proposer.py`):**
  - Added `.gemini/antigravity-cli/mcp_config.json` to `doctor`'s `agy-login` home check.
  - Wrapped `run_managed()` in `Proposer._agy` with `try ... except OSError` to populate failed `telemetry` (`returncode=-1`) cleanly before `ProposerError`.

---

## 3. Regression Tests Added

Updated `tests/test_agy.py` (total AGY suite expanded from 9 to 11 tests):
- Updated `test_constants_surface_and_config_validation` and `test_harness_materialisation_isolation_and_diff_exclusion` to assert `.agents/scripts` while verifying that pre-existing target project `scripts/project_tool.py` files are excluded from `import_tree()` and untouched by `strip_native()`.
- Updated `test_executor_success_isolation_telemetry_and_resume` to verify `.gemini/antigravity-cli/mcp_config.json` contains `{"mcpServers": {}}`.
- Added `test_stream_text_delta_tool_error_wrapper_and_transcript_fallback`: validates real `agy 1.5.9` `ACTIVE`/`DONE` `text_delta` accumulation, truncated `final_message` and `render_events` output, `state="ERROR"` tool error accounting, `agy06` wrapper nested-worker matching, and compact `transcript.jsonl` fallback with missing `thinking_tokens` preserved as `None`.
- Added `test_base_env_gemini_isolation_and_proc_lifecycle_safeguards`: validates `base_env` forwarding, pre-cancelled `run_managed` zero-spawn guard, parallel `terminate_active()`, `SIGTERM`-ignoring grandchild escalation to `SIGKILL`, unrelated external process-group survival, and `Proposer._agy` broken-shebang `OSError` telemetry handling.

---

## 4. Validation Results

All checks ran cleanly after the final code/test edit on `9ffd2a0`:

- `ruff format --check harness2 tests plugins`: `96 files already formatted` (exit 0)
- `ruff check harness2 tests plugins`: `All checks passed!` (exit 0)
- `.venv/bin/python -B -m unittest tests/test_agy.py -v`: `Ran 11 tests in 4.858s` -> `OK`
- `.venv/bin/python -B -m unittest discover -s tests`: `Ran 173 tests in 60.950s` -> `OK`
- `.venv/bin/python -B -m unittest discover -s plugins/harness2-benchmarks/tests`: `Ran 13 tests in 1.509s` -> `OK`

Backward compatibility with `claude`, `codex`, `opencode`, and `command` substrates is preserved with zero regressions across all 162 non-AGY core tests and 13 plugin tests.

---

## 5. Remaining Risks & Operational Notes

1. **Profile keyring helper lifetime (`<profile>-profile-start`):** Wrapper profiles such as `agy06` spawn persistent per-profile `dbus-daemon` and `gnome-keyring-daemon` instances if not already running. Ensure `<profile>-profile-start` runs before invoking concurrent `run_managed` worker pools so a finishing rollout never terminates a shared keyring session started inside its PGID.
2. **Process-global stop state (`harness2.core.events`):** Multiple concurrent `Suite` instances within one Python interpreter process share `_stopped` and `_ACTIVE_PGIDS`; separate OS processes remain isolated.
3. **Live high-tier improver validation:** Offline schemas and CLI compatibility are verified against installed `agy 1.5.9`, while live `gemini-4-hydrogen-high` improver runs remain deferred until future instruction optimization after benchmark integration.

---

## 6. Commit Summary (`feat/agy-native-adapter`)

- `8ca913e5821da0fae567bfd5e3bec1a05b36007f` - Initial native Antigravity (`agy`) executor, improver channel, telemetry, and tests
- `0933e3fd4d38a550cb840b230aa496ed4b7ec2e0` - Integration report
- `9ffd2a01b3800adc844e48793e6d0efdebed4ebf` - Audit remediation fixes and regression tests
- Final audit/remediation documentation commit recorded in Git log right after this report
