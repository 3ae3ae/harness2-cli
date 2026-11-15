# Antigravity (`agy`) Native Integration Report

## 1. Starting repository revision

- **Repository**: `/home/alteixeira20/Projects/harness2-cli-agy/`
- **Branch**: `feat/agy-native-adapter`
- **Starting commit SHA**: `0c811344a4edfe34960b2fc8f286369d7e81fb27` (`0c81134`)
- **Upstream remote**: `https://github.com/3ae3ae/harness2-cli.git`
- **Package version**: `0.1.0` (`harness2-cli`, editable install in local `.venv` alongside `harness2-benchmarks`)
- **Starting working tree**: clean before edits; pre-edit baseline passed (`162` tests in `tests/`, `13` tests in `plugins/harness2-benchmarks/tests/`, `ruff check` and `ruff format --check` clean on `93` files).
- **Installed AGY CLI inspected**: `/home/alteixeira20/.local/bin/agy`, version `1.5.9`.

## 2. Existing architecture findings

Inspection of `harness2` established clean separation across seven boundaries:

1. **Executor substrate (`harness2/substrate/base.py`, `harness2/substrate/_native.py`)**: `Substrate` owns `surface()`, `import_tree()`, `export_tree()`, `strip_native()`, `materialise()`, `install_into()`, `execute()`, `read_trajectory()`, `final_message()`, `cost_usd()`, and `preflight()`. `SurfaceSpec` (`harness2/core/tree.py`) validates editable harness files (`always_on`, `skill_dir`, `hooks_file`, `plugin_dir`, script compilation, character caps).
2. **Suite orchestration (`harness2/suite.py`)**: `GenericAdapter` wires `Substrate` implementations (`claude`, `codex`, `command`) to `workspace` git worktree/archive isolation, `runmark` resumability, prompt delivery, `stream.jsonl`/`meta.json` output, and rate-limit/stop handling.
3. **Improver / selector / reflector / judge channels (`harness2/core/proposer.py`, `harness2/core/spec.py`, `harness2/core/prompts.py`)**: `Proposer` runs isolated candidate generation/selection/reflection sessions in a prepared context workspace and records audit metadata under `adapt/_audit/*.meta.json`. `ChannelSpec` and `INSTALL_MODES` define valid channel names and harness installation modes.
4. **Providers (`harness2/providers.py`)**: `PROVIDER_TYPES` separates subscription logins (`claude-login`, `chatgpt-login`) from API providers (`anthropic`, `openai`, `vertex`, `bedrock`) and strips cross-provider secret env vars (`STRIP_ENV`).
5. **Lifecycle and cancellation (`harness2/core/events.py`)**: Cancellation previously set an in-memory `_stopped` flagchecked at coarse intervals; `harness2/suite.py` used `subprocess.run` without process-group termination on user cancellation.
6. **Workspace isolation (`harness2/workspace.py`)**: Isolated worktrees and deliverable patch/output extraction depend on `substrate.strip_native()` removing native harness and runtime files before computing diffs.
7. **Installed `agy 1.5.9` CLI contract**: Supports `--model`, `--effort` (`low|medium|high|xhigh|max`), `--output-format` (`text|json|stream-json`), `--input-format stream-json`, `-p <prompt>`, `--dangerously-skip-permissions`, `--settings <json-or-path>`, `--json-schema`, `--print-timeout` (default `0s`). Discovers `GEMINI.md`/`AGENTS.md` and `.agents/skills/<name>/SKILL.md`. Does **not** expose a `--strict-mcp-config` flag; reads MCP config from `$HOME/.gemini/config/mcp_config.json`.

## 3. Reusable previous AGY implementation

Inspection of `/home/alteixeira20/Projects/agy00-harness2-lab/` (`h2/bin/agy_exec.sh`, `campaign2/bin/summarize_run.py`, `v1.1/bin/verify_cancellation_pg.sh`, `v1.1/bin/verify_cancellation.sh`, `AGY_WORKHORSE_V1_1_HYDROGEN_REPORT.md`) identified proven patterns to adopt and ad-hoc workarounds to discard:

- **Reused**:
  - `start_new_session=True` process-group ownership and `os.killpg(..., SIGTERM/SIGKILL)` cleanup for nested CLI/sandbox process trees.
  - NDJSON `stream-json` parsing, preferring terminal `result.usage` over intermediate streaming `step_update` deltas to avoid double-counting tokens.
  - Per-step deduplication (`step_index`) when parsing worker transcript JSONL logs under `brain/<conversation_id>/.system_generated/logs/`.
  - Recording missing cost (`cost_usd = None`) and unavailable nested-worker token usage (`usage_available = False`, `None` fields) explicitly without fabricating zeroes.
  - Per-session `--settings` JSON passing `artifactReviewPolicy` / `toolPermission` / `trustedWorkspaces` / `showFeedbackSurvey` rather than mutating profile `settings.json`.
  - Removing global `--print-timeout` constraints in production sessions (`0s` default) while keeping explicit per-run timeouts.
- **Removed workarounds**:
  - Custom `executor.name = "command"` shell wrappers (`agy_exec.sh`), manual diff stripping in bash, and hardcoded profile/lab paths or sleep-polling cleanup scripts.

## 4. Implemented changes

- **Added `harness2/substrate/_proc.py`**: Process-group-owned subprocess runner (`run_managed`, `ManagedResult`, `active_process_groups`, `terminate_process_group`, `terminate_active`) spawning children with `start_new_session=True`, streaming stdout/stderr via daemon reader threads, tracking active PGIDs under a mutex, tearing down the entire process group on normal exit, timeout, or cancellation (`events.stop_requested()`), and fixing cancellation wakeup races by checking `cancel_check()` immediately after `proc.wait()` returns.
- **Added `harness2/substrate/agy.py`**:
  - `AGY_SURFACE` (`GEMINI.md`, `.agents/skills`, `scripts/`, `install_mode = "agy_root"`, no `hooks_file`).
  - `AgySubstrate` implementing `Substrate` plus `import_tree`, `export_tree`, `strip_native`, `materialise`, `install_into`, `execute`, `argv`, `env_and_settings`, `read_trajectory`, `final_message`, `cost_usd`, `preflight`.
  - `parse_events`, `extract_telemetry`, `render_events`, `final_message`, `cost_usd`.
- **Updated `harness2/substrate/__init__.py`**: Exported `AGY_SURFACE` and `AgySubstrate`.
- **Updated `harness2/core/events.py`**: Added `stop_requested()` and wired `request_stop()` to call `terminate_active()`.
- **Updated `harness2/providers.py`**: Added `"agy-login"` and `"gemini"` to `PROVIDER_TYPES` and built-in provider map; added `resource_exhausted` to `USAGE_LIMIT`; added `"GEMINI_API_KEY"` and `"GOOGLE_GEMINI_BASE_URL"` to `STRIP_ENV`; added `agy_login_home()`, `agy_env_and_settings()`, Gemini support in `litellm_params()`, and `agy-login` checks in `check()`.
- **Updated `harness2/settings.py`**: Registered `agy` channel/executor defaults, validation against allowed effort levels (`low`, `medium`, `high`, `xhigh`, `max`), and channel provider checks.
- **Updated `harness2/core/spec.py`**: Added `"agy"` to `CHANNELS` and `"agy_root"` to `INSTALL_MODES`.
- **Updated `harness2/core/prompts.py`**: Added `_SURFACE_AGY_HEAD` and `_TTS_SURFACE_HEAD_AGY` surface instructions and registered `"agy"` in `_build_surface_texts()`.
- **Updated `harness2/core/proposer.py`**: Added `_agy()` execution path using `AgySubstrate`, `run_managed`, isolated workspace cleanup of `.gemini`, `telemetry` attachment on `Reply`, and `telemetry` persistence in `_write_audit()`.
- **Updated `harness2/suite.py`**: Added `AgySubstrate` execution path in `GenericAdapter` with `install_mode="agy_root"`, `run_managed` execution, `telemetry.json` + `meta.json["telemetry"]` writing, rate-limit `Stopped("usage_limit")` handling, cancellation rollout cleanup, and `check()` validation for `agy` model/provider/effort.
- **Updated CLI (`harness2/cli/doctor.py`, `harness2/cli/init.py`, `harness2/cli/run.py`, `harness2/cli/chat.py`)**: Added `--executor agy`, `--with agy`, doctor health/personal-file checks (`Antigravity home`), and non-destructive `chat` injection.
- **Updated docs and examples**: Added `docs/antigravity.md` and `examples/agy-project.toml`; updated `README.md`, `docs/configuration.md`, and `docs/cli.md`.
- **Added and updated offline tests**: Added `tests/test_agy.py` (`9` tests); updated tuple assertions in `tests/test_providers.py` and `tests/test_runtime.py`.

## 5. Exact interface contracts

- **`AgySubstrate` (`harness2/substrate/agy.py`)**:
  - `surface() -> SurfaceSpec`: returns `AGY_SURFACE` (`name="agy"`, `always_on=("GEMINI.md",)`, `always_on_max=10000`, `skill_dir=".agents/skills"`, `hooks_file=None`).
  - `argv(prompt, model, *, effort="", output_format="stream-json", settings_file=None, json_schema=None) -> tuple[list[str], str]`: validates non-empty `model`, `effort` in `EFFORT_LEVELS`, `output_format` in `("text", "json", "stream-json")`; emits `-p <prompt>` for prompts up to `LARGE_PROMPT_BYTES` (`65536` bytes) or `--input-format stream-json` NDJSON stdin payload above `65536` bytes.
  - `env_and_settings(provider, run_dir, cwd, *, base_env=None, home_subdir="agy-home", settings_filename="agy-settings.json") -> tuple[dict[str, str], Path]`: strips `providers.STRIP_ENV`, applies `providers.agy_env_and_settings(provider)`, isolates `HOME`/XDG + empty `mcp_config.json` for `gemini`, writes session settings JSON in `run_dir`.
  - `strip_native(workspace) -> None`: removes harness files via `export_tree({}, workspace, base=import_tree(workspace))`, deletes `.gemini`, and removes empty `.agents` directory trees.
  - `extract_telemetry(events_or_path, *, role="executor", configured_model="", effort="", returncode=None, timed_out=False, cancelled=False, wall_seconds=None, stderr="", output_artifact=None, brain_dirs=None) -> dict[str, Any]`.
- **`run_managed` (`harness2/substrate/_proc.py`)**:
  - `run_managed(cmd, *, cwd, env, stdin_data="", timeout=None, cancel_check=None, on_stdout_line=None, grace_seconds=3.0) -> ManagedResult(returncode, stdout, stderr, timed_out, cancelled, wall_seconds, pgid)`.
- **`Proposer` (`harness2/core/proposer.py`)**:
  - Supports `ChannelSpec("agy", model, effort=..., timeout=..., bin=..., provider=...)` across improver, selector, reflector, and judge channels; populates `Reply.telemetry` and audit JSON `telemetry`.

## 6. New configuration options

- `executor.name = "agy"`
- `executor.model = "agy-login/<model>"` or `"gemini/<model>"`
- `executor.bin = "<binary-or-wrapper>"`
- `executor.effort = "low" | "medium" | "high" | "xhigh" | "max"`
- `improver.channel = "agy"`, `selector.channel = "agy"`, `reflector.channel = "agy"`, `judge.channel = "agy"` (with corresponding `model`, `bin`, `effort`, `timeout` keys)
- `[providers.agy-login]` (`type = "agy-login"`, optional `home = "/path/to/isolated/home"`)
- `[providers.gemini]` (built-in `type = "gemini"`, `api_key_env = "GEMINI_API_KEY"`, optional `base_url`)

## 7. Test coverage

New offline test suite in `tests/test_agy.py` (`9` unit/integration tests using a deterministic fake `agy` executable):

1. `test_constants_surface_and_config_validation`: `CHANNELS`, `INSTALL_MODES`, `AGY_SURFACE`, valid/invalid model formats, effort values, incompatible providers, cross-channel rejection of `agy-login`.
2. `test_argv_prompt_delivery_and_invalid_inputs`: `-p` short prompts, `--input-format stream-json` for >64 KiB prompts, empty model / invalid effort / unsupported output format rejections.
3. `test_harness_materialisation_isolation_and_diff_exclusion`: `GEMINI.md`, `.agents/skills/`, `scripts/` materialisation, `.gemini/` creation, and clean removal via `strip_native`.
4. `test_executor_success_isolation_telemetry_and_resume`: full `GenericAdapter.execute` rollout, deliverable diff excluding `GEMINI.md`/`.agents/`/`.gemini/`/`.harness2/`, `meta.json` & `telemetry.json`, non-double-counted tokens, isolated `agy-home` + empty `mcp_config.json`, ambient key stripping, `runmark` resume with `0` extra calls.
5. `test_profile_isolation_preserves_custom_home_and_doctor_reports_personal_files`: `agy-login` with custom `home`, stripping ambient `GEMINI_API_KEY`, zero writes to profile `settings.json`, `harness2 doctor --json` reporting `Antigravity home` files with exit `0`.
6. `test_telemetry_worker_transcripts_missing_and_dedup`: `subagent` + nested `agy` CLI detection, brain transcript lookup, `step_index` deduplication (last update wins), explicit `usage_available = False` / `None` when transcripts are absent, tool error detection.
7. `test_failure_classifications_cli_auth_malformed_incomplete_rate_limit`: CLI errors, `UNAUTHENTICATED` auth errors, malformed NDJSON lines, incomplete streams without `result`, `print_timeout`, and `RESOURCE_EXHAUSTED` raising `events.Stopped("usage_limit")` with incomplete rollout cleanup.
8. `test_timeout_and_cancellation_clean_up_nested_children_without_touching_peers`: deterministic multi-process child + grandchild fixtures verifying zero surviving descendants after timeout, normal exit, and cancellation while peer processes remain alive and cancelled runs resume cleanly.
9. `test_agy_improver_proposer_selector_and_cancellation`: `Proposer` mission execution creating inspectable candidate `GEMINI.md` artifacts, extracting telemetry, propagating `ProposerError`, and raising `events.Stopped("usage_limit")` on rate limit.

## 8. Offline validation

Executed after final implementation and documentation edits:

- `.venv/bin/python -B -m unittest tests/test_agy.py -v`: `9` tests passed (`OK`).
- `.venv/bin/python -B -m unittest discover -s tests`: `171` tests passed in `43.835s` (`OK`).
- `.venv/bin/python -B -m unittest discover -s plugins/harness2-benchmarks/tests`: `13` tests passed in `0.954s` (`OK`).
- `ruff check harness2 tests plugins`: `All checks passed!`.
- `ruff format --check harness2 tests plugins`: `96 files already formatted`.
- Verified zero em dashes in modified diff lines and newly created files.

## 9. Live smoke results

Controlled live smoke test executed in a temporary workspace (removed after run) using isolated wrapper `/home/alteixeira20/.local/bin/agy06` with `agy-login/gemini-3.8-flash-low` and `effort = "low"`:

- **Executor (`GenericAdapter.execute`)**:
  - `delivered = True`, `meta.ok = True`, `exit_code = 0`.
  - Produced `result.txt` (`+SMOKE_OK` in `deliverable.patch`); `GEMINI.md` and `.agents/` excluded from patch.
  - Telemetry (`telemetry.json`): `configured_model = "gemini-3.8-flash-low"`, `model = "gemini-3.8-flash-low"`, `effort = "low"`, `status = "SUCCESS"`, `wall_seconds = 20.872`, `duration_seconds = 17.508869719`, `input_tokens = 22239`, `cached_input_tokens = 41226`, `output_tokens = 159`, `reasoning_tokens = 0`, `total_tokens = 22398`, `tool_calls = 4` (`read_file: 3`, `write_file: 1`), `tool_errors = 0`, `cost_usd = null`, `output_artifact = "deliverable.patch"`.
- **Improver (`Proposer.call`)**:
  - `ok = True`, `text = "<notes>added rule 2</notes>"`.
  - Updated `context/harness/GEMINI.md` cleanly (`"Rule 1.\nRule 2: verify result.txt.\n"`).
  - Telemetry (`Reply.telemetry` and audit meta): `configured_model = "gemini-3.8-flash-low"`, `model = "gemini-3.8-flash-low"`, `effort = "low"`, `status = "SUCCESS"`, `wall_seconds = 10.629`, `duration_seconds = 7.013345918`, `input_tokens = 19848`, `cached_input_tokens = 27032`, `output_tokens = 110`, `reasoning_tokens = 0`, `total_tokens = 19958`, `tool_calls = 2` (`run_command: 1`, `read_file: 1`), `tool_errors = 0`, `cost_usd = null`.
- **Cleanup and leak audit**: zero leftover `agy` worker processes (`ps` showed only pre-existing persistent `gnome-keyring-daemon` for `agy06`); zero secret patterns (`AIza`, `ya29.`, `Bearer `, `GEMINI_API_KEY`, `OAUTH`) in `stream.jsonl`, `meta.json`, or `telemetry.json`.

## 10. Profile isolation evidence

- Before-and-after `(st_mtime_ns, st_size)` snapshots of `/home/alteixeira20/.local/share/agy06-profile/home/.gemini/{antigravity-cli/settings.json, config/mcp_config.json, GEMINI.md}` remained identical (`profile_watched_unchanged = True`).
- Production profiles `agy00` through `agy07`, Codex homes, Claude configs, and MCP registrations were not modified.
- Offline test `test_profile_isolation_preserves_custom_home_and_doctor_reports_personal_files` verified custom `home` preservation without credential copying, ambient `GEMINI_API_KEY` stripping on `agy-login`, zero profile `settings.json` writes, and `harness2 doctor --json` reporting `Antigravity home` files cleanly.
- Offline test `test_executor_success_isolation_telemetry_and_resume` verified `gemini` API-key runs isolate `HOME` to `<run_dir>/agy-home` with empty `{"mcpServers": {}}` and strip `ANTHROPIC_API_KEY`.

## 11. Telemetry correctness

- Coordinator model identity (`model`) is taken from stream `init.model`, never inferred solely from CLI arguments.
- Token counts read terminal `result.usage` when present and only aggregate `DONE` `agent_response` steps otherwise, eliminating double-counting from intermediate `ACTIVE` streaming deltas.
- Nested worker detection captures both `subagent` steps and `run_command` outputs referencing `agy`, resolves local brain transcripts with last-write deduplication per `step_index`, and leaves `usage_available = False` and token totals as `None` whenever transcripts are missing.
- Live `agy 1.5.9` stream JSON did not emit `finished_reason` on normal `SUCCESS` completion or USD cost fields; `termination_reason` and `cost_usd` recorded `None` rather than fabricated defaults.

## 12. Cancellation behavior

- Every `agy` subprocess runs in its own POSIX session (`start_new_session=True`), tracked in `_ACTIVE_PGIDS`.
- `events.request_stop()` invokes `terminate_active()`, sending `SIGTERM` to owned PGIDs immediately and escalating to `SIGKILL` after grace timeout; `_proc.run_managed()` also cleans up any lingering child/grandchild processes on normal completion and per-run timeout in a `finally` block.
- `run_managed()` checks `cancel_check()` immediately after `proc.wait()` returns so signal-induced exits (`-15`) classify accurately as `cancelled = True`.
- Cancelled or rate-limited executor rollouts remove incomplete `runmark` artifacts so re-running resumes cleanly (`test_timeout_and_cancellation_clean_up_nested_children_without_touching_peers`).

## 13. Backward compatibility

- All existing Claude (`claude`), Codex (`codex`), OpenCode (`opencode`), and command executors and improver channels remain intact (`171` core tests + `13` benchmark plugin tests passing).
- Existing `Proposer` subprocess mocking in `tests/test_workflow.py` remains undisturbed by keeping `_claude`, `_codex`, and `_opencode` improver methods on `subprocess.run` while `AgySubstrate` / `_agy` and suite execution use `run_managed`.

## 14. Known limitations

- **No CLI `--strict-mcp-config` flag in `agy 1.5.9`**: `agy-login` isolation depends on the configured profile `home` or isolated launcher wrapper (`bin`) rather than a CLI flag; `gemini` API-key mode avoids this completely via a fresh per-session `agy-home`.
- **Cost reporting**: `agy 1.5.9` `result` streams do not include USD pricing (`cost_usd` stays `None` / ledger `0.0`).
- **Nested worker transcripts**: worker token attribution is available only when the local Antigravity brain directory for the active profile is supplied/accessible; otherwise worker invocation count is reported with `usage_available = False`.

## 15. Required follow-up work

1. Wire `/home/alteixeira20/Projects/Synapse-competitive-benchmark/` in a dedicated, explicitly approved follow-up step without mutating Odysseus fixture worktrees.
2. Add optional `--brain-dir` / profile brain-dir plumbing in benchmark runner consumers when evaluating multi-worker runs where subagent token breakdown is desired.
3. Periodically re-verify `agy --help` schema if `agy` is upgraded past `1.5.9` (for example if a native `--strict-mcp-config` or cost field is added upstream).

## 16. Proposed Synapse benchmark integration

Read-only inspection of `/home/alteixeira20/Projects/Synapse-competitive-benchmark/` (`SYNAPSE_HARNESS2_INTEGRATION_AUDIT.md` and directory state) confirmed:

- **Version control ownership**: `/home/alteixeira20/Projects/Synapse-competitive-benchmark/` is **not** a Git repository (`git rev-parse --git-dir` failed: `not a git repository`). Its subdirectories `odysseus-serena`, `odysseus-synapse`, and `odysseus-vanilla` are Odysseus fixture worktrees containing task state. **Do not** run `git init` at the benchmark root or commit benchmark runner changes inside any Odysseus worktree.
- **Narrowest supported integration mechanism**: Keep all Synapse MCP configuration (`mcp_config.json`), Odysseus task repos, and grading scripts inside `Synapse-competitive-benchmark`. Point its Harness² configuration / runner environment at this fork's `AgySubstrate` (or `.harness2/config.toml` with `executor.name = "agy"`, `executor.bin = "<isolated profile wrapper>"`, `executor.model = "agy-login/<model>"`), consume `telemetry.json` and `AgySubstrate.extract_telemetry(..., brain_dirs=[...])`, and remove custom `command`-mode `agy_exec.sh` / diff-stripping wrappers from the benchmark harness once switched over.

## 17. Local commit SHAs

- `8ca913e5821da0fae567bfd5e3bec1a05b36007f` (`feat(agy): add native Antigravity CLI executor, improver channel, telemetry, and tests`) on branch `feat/agy-native-adapter`.
- Final commit adding `AGY_NATIVE_INTEGRATION_REPORT.md` created immediately following this report on `feat/agy-native-adapter`.

## 18. Commands for independent reproduction

```bash
cd /home/alteixeira20/Projects/harness2-cli-agy

# Lint and formatting checks
ruff check harness2 tests plugins
ruff format --check harness2 tests plugins

# Offline AGY tests and full regression suite
.venv/bin/python -B -m unittest tests/test_agy.py -v
.venv/bin/python -B -m unittest discover -s tests
.venv/bin/python -B -m unittest discover -s plugins/harness2-benchmarks/tests
```

**Final status**: READY FOR BENCHMARK INTEGRATION
