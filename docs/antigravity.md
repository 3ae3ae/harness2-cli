# Antigravity (`agy`) Integration

Harness² supports the Antigravity CLI (`agy`) natively as both a coding-agent **executor**
(`executor.name = "agy"`) and an **improver / selector / reflector / judge channel**
(`channel = "agy"`).

## 1. Executor and improver configuration

Configure Antigravity in `.harness2/config.toml` (or see
[`examples/agy-project.toml`](../examples/agy-project.toml)):

```toml
[executor]
name = "agy"
model = "agy-login/gemini-3.8-flash-high"
bin = "agy"          # or an isolated profile wrapper script
effort = "high"      # low | medium | high | xhigh | max
timeout = 1800       # per-rollout limit (passed as execution timeout, not a global session cap)

[improver]
channel = "agy"
model = "agy-login/gemini-3.8-flash-high"
bin = "agy"
effort = "high"
timeout = 3600

[selector]
channel = "agy"
model = "agy-login/gemini-3.8-flash-low"
effort = "low"

[reflector]
channel = "agy"
model = "agy-login/gemini-3.8-flash-low"
effort = "low"

[judge]
channel = "agy"
model = "agy-login/gemini-3.8-flash-low"
effort = "low"
```

When using an API key instead of a subscription login:

```toml
[executor]
name = "agy"
model = "gemini/gemini-3.8-flash-high"
effort = "high"
```

with `GEMINI_API_KEY` set in `.harness2/.env` or your environment.

## 2. Model and profile selection

Model choice (`<provider>/<model>`) is independent of profile selection (`bin` / `home`):

- `agy-login/<model>` preserves Antigravity subscription / login authentication without
  copying credentials or keyrings. Set `executor.bin` / `improver.bin` to an isolated
  profile launcher script, or configure `[providers.agy-login] home = "/path/to/profile/home"`.
  Ambient `GEMINI_API_KEY` and `GOOGLE_GEMINI_BASE_URL` environment variables are stripped on
  `agy-login` runs so subscription sessions do not silently fall back to an API key.
- `gemini/<model>` sets `"modelProvider": "gemini"` in the per-session settings JSON and
  supplies `GEMINI_API_KEY` (and optional `GOOGLE_GEMINI_BASE_URL`). Ambient subscription or
  other provider API keys (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, etc.) are stripped.

## 3. Authentication and settings isolation

Each execution writes an ephemeral per-session settings file (`agy-settings.json` inside the
run or audit workspace) passed via `--settings <path>`. User settings files on disk inside
profiles are never modified.

- For `gemini` API-key runs, Harness² creates a dedicated per-session `HOME` (`agy-home`)
  containing `.gemini/config/mcp_config.json` set to `{"mcpServers": {}}` and clean XDG paths,
  preventing personal `GEMINI.md`, hooks, or user MCP servers from loading.
- Because the inspected `agy` CLI (`1.5.9`) has no `--strict-mcp-config` flag, `agy-login`
  relies on the configured profile `home` or isolated wrapper binary for MCP/user-rule
  isolation. `harness2 doctor` inspects custom `agy-login` homes and reports any present
  `.gemini/GEMINI.md`, `AGENTS.md`, `hooks.json`, or `mcp_config.json`.

## 4. Editable instruction surface

The Antigravity substrate (`AGY_SURFACE`, `install_mode = "agy_root"`) manages:

- `GEMINI.md` (always-on instructions, 10,000-char cap)
- `.agents/skills/<name>/SKILL.md` (on-demand skills; `SKILL.md` paths should be referenced
  in `GEMINI.md`)
- `scripts/` (helper scripts)

Native `.gemini/` runtime state created during task execution is excluded from task diffs and
stripped when workspaces are cleaned. Unlike Claude Code, top-level `hooks` JSON keys are
not materialised for Antigravity.

## 5. Structured telemetry and nested workers

Each executor rollout writes `telemetry.json` alongside `meta.json` (and copies it to
`meta.json["telemetry"]`). Each `agy` improver/selector/reflector/judge call records telemetry
in `adapt/_audit/*.meta.json["telemetry"]`.

Captured fields include:

- `role`, `conversation_id`, `configured_model`, `model` (from stream `init.model`), `effort`
- `status`, `ok`, `returncode`, `timed_out`, `cancelled`, `incomplete`, `malformed_lines`
- `rate_limited`, `auth_error`, `termination_reason` (`finished_reason` when reported)
- `wall_seconds`, `duration_seconds`
- `input_tokens`, `cached_input_tokens`, `output_tokens`, `reasoning_tokens`, `total_tokens`
  (sourced from `result.usage` when present, falling back to `DONE` `agent_response` steps,
  never double-counting streaming deltas; missing fields stay `null`)
- `cost_usd` (always `null` unless the CLI reports a cost field)
- `tool_calls`, `tool_errors`, `tool_counts`, `output_artifact`
- `nested_workers`: detects delegated `subagent` steps and nested CLI `run_command`
  invocations referencing `agy`, resolves worker transcripts (`transcript_full.jsonl` /
  `transcript.jsonl`) when local brain directories are provided and readable, deduplicates
  updated `step_index` records (last update wins), and explicitly sets `usage_available = false`
  and `null` token fields when worker transcripts are unavailable.

Authentication secrets and full process environments are never logged in telemetry or meta
files.

## 6. Process lifecycle and cancellation

Subprocesses launched through `harness2.substrate._proc.run_managed` run in a dedicated POSIX
session (`start_new_session=True`) tracked by the coordinator runtime. On normal completion,
per-task timeout expiration, or user cancellation (`events.request_stop`), Harness² signals
the owned process group (`SIGTERM`, grace wait, then `SIGKILL`) so spawned subshells, sandboxed
helpers, or nested workers cannot linger, without ever signalling unrelated user processes.
Cancelled or rate-limited executor runs discard incomplete rollout markers so re-running
resumes cleanly.

## 7. Prompt delivery and known CLI limits

- Prompts up to 64 KiB are passed via `-p <prompt>`. Larger prompts switch automatically to
  `--input-format stream-json --output-format stream-json` with an NDJSON `user` message on
  stdin to avoid OS `ARG_MAX` limits.
- Invocations pass `--model`, `--output-format stream-json`, `--dangerously-skip-permissions`,
  optional `--effort`, and per-session `--settings`.
- `--print-timeout` is intentionally omitted by default so CLI sessions are governed by
  Harness² per-rollout/per-session timeouts rather than a hidden global CLI deadline.
- `harness2 doctor --json` checks the configured binary (`--version`), provider readiness,
  effort validity, and custom `agy-login` home instruction/MCP files.

## 8. Verified commands

```bash
harness2 init --executor agy --yes
harness2 doctor
harness2 run --only-tasks <task_id> --max-calls 4
harness2 eval
harness2 apply --dry-run
```

## 9. Migrating from command wrappers

If you previously used `executor.name = "command"` with a shell script calling `agy`:

1. Keep your isolated profile launcher wrapper if needed and set `executor.bin` (and
   `improver.bin`) to that wrapper command, or set `bin = "agy"` with `[providers.agy-login]
   home = "..."` or `gemini/<model>`.
2. Set `executor.name = "agy"`, `executor.model = "<provider>/<model>"`, and
   `executor.effort = "<level>"`.
3. Remove ad-hoc stream-json parsing, `GEMINI.md`/`.agents/` diff filtering, and process-group
   kill wrappers from the suite side: `AgySubstrate`, `extract_telemetry`, and `run_managed`
   handle these directly and emit `telemetry.json`.

## 10. External benchmark integration

External benchmark runners (such as Synapse benchmark suites) can consume native AGY support
without embedding benchmark-specific code inside Harness² by:

- Using a custom external suite adapter built on `AgySubstrate` (`materialise`, `install_into`,
  `strip_native`, `argv`, `env_and_settings`, `read_trajectory`, `extract_telemetry`), or
- Configuring `.harness2/config.toml` with `executor.name = "agy"` and running standard
  Harness² suite workflows.

Keep benchmark-specific MCP registration, profile cloning/restoration, and grading scripts in
the external benchmark repository rather than inside `harness2`.
