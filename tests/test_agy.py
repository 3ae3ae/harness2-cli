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

"""Offline unit and integration tests for the native Antigravity (`agy`) adapter."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from harness2 import providers, registry, suite, workspace
from harness2.cli.main import main as cli_main
from harness2.core import events, prompts, runmark, tree
from harness2.core.proposer import Proposer, ProposerError
from harness2.core.spec import CHANNELS, INSTALL_MODES, BenchSpec, ChannelSpec
from harness2.providers import Provider
from harness2.settings import ConfigError, load
from harness2.substrate import AGY_SURFACE, AgySubstrate
from harness2.substrate.agy import (
    LARGE_PROMPT_BYTES,
    extract_telemetry,
    final_message,
    parse_events,
)

FAKE_AGY = r"""import json
import os
import subprocess
import sys
import time
from pathlib import Path

args = sys.argv[1:]
if "--version" in args:
    print("1.5.9-fake")
    sys.exit(0)

model = args[args.index("--model") + 1] if "--model" in args else ""
effort = args[args.index("--effort") + 1] if "--effort" in args else ""
output_format = args[args.index("--output-format") + 1] if "--output-format" in args else "stream-json"
input_format = args[args.index("--input-format") + 1] if "--input-format" in args else "text"
settings_arg = args[args.index("--settings") + 1] if "--settings" in args else ""

if input_format == "stream-json":
    raw_in = sys.stdin.readline()
    payload = json.loads(raw_in)
    prompt = payload["message"]["content"]
elif "-p" in args:
    prompt = args[args.index("-p") + 1]
else:
    prompt = ""

cwd = Path.cwd()
record = {
    "cwd": str(cwd),
    "argv": args,
    "model": model,
    "effort": effort,
    "output_format": output_format,
    "input_format": input_format,
    "prompt_len": len(prompt),
    "prompt": prompt[:500],
    "home": os.environ.get("HOME"),
    "gemini_key": os.environ.get("GEMINI_API_KEY"),
    "gemini_base": os.environ.get("GOOGLE_GEMINI_BASE_URL"),
    "anthropic_key": os.environ.get("ANTHROPIC_API_KEY"),
    "settings": json.loads(Path(settings_arg).read_text()) if settings_arg and Path(settings_arg).is_file() else None,
    "harness": {
        "GEMINI.md": (cwd / "GEMINI.md").read_text() if (cwd / "GEMINI.md").is_file() else None,
        "skill": (cwd / ".agents/skills/check/SKILL.md").read_text()
        if (cwd / ".agents/skills/check/SKILL.md").is_file()
        else None,
    },
}
if os.environ.get("FAKE_RECORD"):
    with open(os.environ["FAKE_RECORD"], "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")

mode = os.environ.get("FAKE_MODE", "success")

if os.environ.get("FAKE_SPAWN_CHILD_PID_FILE"):
    pid_file = Path(os.environ["FAKE_SPAWN_CHILD_PID_FILE"])
    child = subprocess.Popen(
        [sys.executable, "-c",
         "import os, pathlib, sys, time\n"
         "gc = __import__('subprocess').Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
         "pathlib.Path(sys.argv[1]).write_text(f'{os.getpid()}\\n{gc.pid}\\n')\n"
         "time.sleep(60)\n",
         str(pid_file)],
    )
    deadline = time.monotonic() + 5.0
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    if os.environ.get("FAKE_EXIT_IMMEDIATELY") == "1":
        print(json.dumps({"event": "init", "conversation_id": "conv-coord-1", "init": {"model": model}}), flush=True)
        print(json.dumps({"event": "result", "result": {"conversation_id": "conv-coord-1", "status": "SUCCESS", "response": "done", "usage": {"input_tokens": 5, "output_tokens": 2}}}), flush=True)
        sys.exit(0)
    time.sleep(float(os.environ.get("FAKE_SLEEP", "60")))

if mode == "sleep":
    time.sleep(float(os.environ.get("FAKE_SLEEP", "5")))
    sys.exit(0)
if mode == "rate_limit":
    print('AGY_ERROR: {"status":"RESOURCE_EXHAUSTED","http_status":429,"short_error":"Quota exhausted"}', file=sys.stderr, flush=True)
    print(json.dumps({"event": "result", "result": {"conversation_id": "conv-rl", "status": "ERROR", "error": "429 RESOURCE_EXHAUSTED"}}), flush=True)
    sys.exit(3)
if mode == "auth_error":
    print('AGY_ERROR: {"status":"UNAUTHENTICATED","http_status":401,"short_error":"Error: authentication failed"}', file=sys.stderr, flush=True)
    print(json.dumps({"event": "result", "result": {"conversation_id": "conv-auth", "status": "ERROR", "error": "authentication failed"}}), flush=True)
    sys.exit(3)
if mode == "cli_error":
    print('AGY_ERROR: {"status":"INVALID_ARGUMENT","http_status":400,"short_error":"Unknown model"}', file=sys.stderr, flush=True)
    print(json.dumps({"event": "result", "result": {"conversation_id": "conv-err", "status": "ERROR", "error": "Unknown model"}}), flush=True)
    sys.exit(3)
if mode == "malformed":
    print(json.dumps({"event": "init", "conversation_id": "conv-bad", "init": {"model": model}}), flush=True)
    print("NOT VALID JSON LINE", flush=True)
    print(json.dumps({"event": "result", "result": {"conversation_id": "conv-bad", "status": "SUCCESS", "response": "ok"}}), flush=True)
    sys.exit(0)
if mode == "incomplete":
    print(json.dumps({"event": "init", "conversation_id": "conv-inc", "init": {"model": model}}), flush=True)
    print(json.dumps({"event": "step_update", "step_update": {"conversation_id": "conv-inc", "step_index": 1, "state": "ACTIVE", "step_type": "agent_response", "agent_response": {"text_delta": "partial"}}}), flush=True)
    sys.exit(0)
if mode == "print_timeout":
    print(json.dumps({"event": "init", "conversation_id": "conv-pt", "init": {"model": model}}), flush=True)
    print(json.dumps({"event": "result", "result": {"conversation_id": "conv-pt", "status": "ERROR", "response": "partial", "finished_reason": "print_timeout"}}), flush=True)
    sys.exit(3)

# Improver / selector context detection
context = cwd / "context"
if context.is_dir():
    if (context / "harness/GEMINI.md").is_file():
        cur = (context / "harness/GEMINI.md").read_text()
        if "Verify code.txt" not in cur:
            (context / "harness/GEMINI.md").write_text(cur + "Verify code.txt before finishing.\n")
        resp = "<notes>Added verification step to GEMINI.md.</notes>"
    elif (context / "candidates").is_dir():
        chosen = sorted((context / "candidates").glob("*.md"))[0].stem
        resp = "<why>Best</why>\n```choice\n" + chosen + "\n```"
    else:
        resp = "NO_ENTRY"
else:
    assert not (cwd / ".harness2").exists(), "project state leaked"
    assert not (cwd / "check").exists(), "check leaked"
    assert not (cwd / "rubric.md").exists(), "rubric leaked"
    if (cwd / "code.txt").is_file():
        (cwd / "code.txt").write_text("after\n")
    if (cwd / "GEMINI.md").is_file():
        (cwd / "GEMINI.md").write_text("agent mutated GEMINI.md\n")
    (cwd / ".gemini/scratch").mkdir(parents=True, exist_ok=True)
    (cwd / ".gemini/scratch/state.txt").write_text("internal state\n")
    (cwd / "output").mkdir(exist_ok=True)
    (cwd / "output/deliverable.txt").write_text("deliverable body\n")
    resp = "Finished AGY task cleanly."

conv_id = os.environ.get("FAKE_CONV_ID", "conv-coord-100")
worker_id = os.environ.get("FAKE_WORKER_ID", "")
print(json.dumps({"event": "init", "conversation_id": conv_id, "init": {"model": model, "cwd": str(cwd)}}), flush=True)
print(json.dumps({"event": "step_update", "step_update": {"conversation_id": conv_id, "step_index": 0, "state": "DONE", "step_type": "user_input", "user_input": {"text": prompt}}}), flush=True)
print(json.dumps({"event": "step_update", "step_update": {"conversation_id": conv_id, "step_index": 1, "state": "ACTIVE", "step_type": "tool", "tool_name": "run_command", "tool_info": {"parameters": {"CommandLine": "pytest"}}}}), flush=True)
print(json.dumps({"event": "step_update", "step_update": {"conversation_id": conv_id, "step_index": 1, "state": "DONE", "step_type": "tool", "tool_name": "run_command", "tool_info": {"parameters": {"CommandLine": "pytest"}, "output": "Command exited with code 0.\\nok\\n"}}}), flush=True)
if worker_id:
    print(json.dumps({"event": "step_update", "conversation_id": conv_id, "step_index": 2, "state": "DONE", "step_type": "subagent", "subagent_info": {"subagents": [{"conversation_id": worker_id, "type_name": "self", "role": "Helper Worker"}]}}), flush=True)
print(json.dumps({"event": "step_update", "conversation_id": conv_id, "step_index": 3, "state": "ACTIVE", "step_type": "agent_response", "agent_response": {"text_delta": "Working..."}, "usage": {"input_tokens": 50, "output_tokens": 10}}), flush=True)
print(json.dumps({"event": "step_update", "conversation_id": conv_id, "step_index": 3, "state": "DONE", "step_type": "agent_response", "agent_response": {"response": resp}, "usage": {"input_tokens": 120, "cache_read_tokens": 40, "output_tokens": 35, "thinking_tokens": 15, "total_tokens": 170}}), flush=True)
print(json.dumps({
    "event": "result",
    "result": {
        "conversation_id": conv_id,
        "status": "SUCCESS",
        "response": resp,
        "duration_seconds": 1.25,
        "num_turns": 2,
        "finished_reason": "stop",
        "usage": {
            "input_tokens": 150,
            "cache_read_tokens": 60,
            "output_tokens": 45,
            "thinking_tokens": 20,
            "total_tokens": 215,
        },
    },
}), flush=True)
sys.exit(0)
"""


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class AgyAdapterTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.project = self.root / "project"
        self.project.mkdir()
        (self.project / ".harness2/tasks").mkdir(parents=True)
        (self.project / "code.txt").write_text("before\n")
        (self.project / "GEMINI.md").write_text("Follow repository conventions.\n")
        skill_dir = self.project / ".agents/skills/check"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: check\ndescription: verify edited files\n---\nRun checks.\n"
        )
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        self.agy_bin = self.bin_dir / "agy"
        self.agy_bin.write_text(f"#!{sys.executable}\n" + FAKE_AGY, encoding="utf-8")
        self.agy_bin.chmod(0o755)
        self.record = self.root / "records.jsonl"
        env = {k: v for k, v in os.environ.items() if not k.startswith(("HARNESS2_", "FAKE_"))}
        env.update(
            PATH=f"{self.bin_dir}{os.pathsep}{env.get('PATH', '')}",
            FAKE_RECORD=str(self.record),
            HOME=str(self.root / "user-home"),
            XDG_CONFIG_HOME=str(self.root / "config"),
            XDG_STATE_HOME=str(self.root / "state"),
            GEMINI_API_KEY="secret-gemini-key",
            ANTHROPIC_API_KEY="ambient-anthropic-secret",
        )
        (self.root / "user-home").mkdir(parents=True, exist_ok=True)
        patcher = patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(events.start, None)
        events.start(None)
        task_dir = suite.new_task(
            self.project / ".harness2/tasks", "one", files=False, domain="coding", check=None
        )
        (task_dir / "task.md").write_text("Update code.txt to say after.\n")
        workspace.commit_all(self.project, "initial")

    def _context(self, **overrides):
        values = {
            "executor.name": "agy",
            "executor.model": "gemini/gemini-3.8-flash-high",
            "executor.effort": "high",
            "improver.channel": "agy",
            "improver.model": "gemini/gemini-3.8-flash-high",
        }
        values.update(overrides)
        settings = load(
            self.project,
            overrides={k: (v, "test") for k, v in values.items()},
        )
        return registry.SuiteContext(settings, self.project, self.project / ".harness2/runs/test")

    def _records(self):
        if not self.record.is_file():
            return []
        return [json.loads(line) for line in self.record.read_text().splitlines() if line.strip()]

    def test_constants_surface_and_config_validation(self):
        self.assertIn("agy", CHANNELS)
        self.assertIn("agy_root", INSTALL_MODES)
        self.assertEqual(AGY_SURFACE.always_on, ("GEMINI.md",))
        self.assertEqual(AGY_SURFACE.skill_dir, ".agents/skills")
        self.assertIsNotNone(prompts._st(AGY_SURFACE))

        # Valid configuration passes check()
        ctx = self._context()
        self.assertEqual(suite.check(ctx), [])

        # Invalid model format and invalid effort fail check() cleanly
        bad_model = self._context(**{"executor.model": "bare-model-without-provider"})
        self.assertTrue(any("executor:" in p for p in suite.check(bad_model)))

        bad_effort = self._context(**{"executor.effort": "ultra"})
        self.assertTrue(any("effort" in p for p in suite.check(bad_effort)))

        # Incompatible provider (e.g. openai for agy) fails cleanly
        bad_provider = self._context(**{"executor.model": "openai/gpt-5"})
        self.assertTrue(any("agy-login or gemini" in p for p in suite.check(bad_provider)))

        # Cross-channel safety: claude/codex/opencode reject agy-login
        agy_login = Provider("agy-login", "agy-login")
        for fn, args in (
            (providers.claude_env, ()),
            (providers.codex_config, ("model", "high")),
            (providers.opencode_config, ("model",)),
            (providers.litellm_params, ("model",)),
        ):
            with self.subTest(fn=fn.__name__), self.assertRaises(ConfigError):
                fn(agy_login, *args)

    def test_argv_prompt_delivery_and_invalid_inputs(self):
        sub = AgySubstrate(bin="agy")
        cmd, stdin_text = sub.argv("short prompt", "gemini-3.8-flash-high", effort="xhigh")
        self.assertIn("-p", cmd)
        self.assertIn("short prompt", cmd)
        self.assertNotIn("--input-format", cmd)
        self.assertEqual(stdin_text, "")
        self.assertEqual(cmd[cmd.index("--effort") + 1], "xhigh")

        big = "x" * (LARGE_PROMPT_BYTES + 100)
        cmd_big, stdin_big = sub.argv(big, "gemini-3.8-flash-high")
        self.assertIn("--input-format", cmd_big)
        self.assertEqual(cmd_big[cmd_big.index("--input-format") + 1], "stream-json")
        self.assertNotIn("-p", cmd_big)
        self.assertEqual(json.loads(stdin_big)["message"]["content"], big)

        with self.assertRaises(ConfigError):
            sub.argv("hi", "")
        with self.assertRaises(ConfigError):
            sub.argv("hi", "gemini-3.8-flash-high", effort="invalid")

    def test_harness_materialisation_isolation_and_diff_exclusion(self):
        sub = AgySubstrate(bin="agy")
        harness_src = self.root / "src-harness"
        tree.write_tree(
            AGY_SURFACE,
            {
                "GEMINI.md": "Always read check skill.\n",
                ".agents/skills/check/SKILL.md": "---\ndescription: run checks\n---\nBody\n",
                "scripts/helper.py": "print('ok')\n",
            },
            harness_src,
        )
        spec = BenchSpec(
            name="test",
            runs_root=self.root / "runs",
            install_mode="agy_root",
            prompts=suite.PROJECT_PROMPTS,
            channels={
                "proposer": ChannelSpec("agy", "gemini/gemini-3.8-flash-high"),
                "selector": ChannelSpec("agy", "gemini/gemini-3.8-flash-high"),
                "reflector": ChannelSpec("agy", "gemini/gemini-3.8-flash-high"),
            },
        )
        install = sub.materialise(harness_src, spec, {})
        try:
            ws = self.root / "ws"
            ws.mkdir()
            (ws / "app.py").write_text("print(1)\n")
            sub.install_into(install, ws)
            self.assertEqual((ws / "GEMINI.md").read_text(), "Always read check skill.\n")
            self.assertTrue((ws / ".agents/skills/check/SKILL.md").is_file())
            self.assertTrue((ws / "scripts/helper.py").is_file())
            self.assertTrue((ws / "app.py").is_file())

            (ws / ".gemini/tmp").mkdir(parents=True)
            (ws / ".gemini/tmp/file").write_text("state")
            sub.strip_native(ws)
            self.assertFalse((ws / "GEMINI.md").exists())
            self.assertFalse((ws / ".agents").exists())
            self.assertFalse((ws / ".gemini").exists())
            self.assertTrue((ws / "app.py").exists())
        finally:
            import shutil

            shutil.rmtree(install.staging_dir, ignore_errors=True)

    def test_executor_success_isolation_telemetry_and_resume(self):
        adapter = suite.make_adapter(self._context())
        run_dir = adapter.execute("one", None, "agy-r1")

        self.assertTrue(runmark.is_complete(run_dir, harness_dir=adapter.base_harness()))
        self.assertTrue(adapter.delivered(run_dir))
        patch_text = adapter.deliverable_files(run_dir)["deliverable.patch"]
        self.assertIn("+after", patch_text)
        self.assertNotIn("GEMINI.md", patch_text)
        self.assertNotIn(".gemini", patch_text)
        self.assertNotIn(".harness2", patch_text)
        self.assertIn("Finished AGY task cleanly.", adapter.deliverable_text(run_dir))
        self.assertIn("[ACTION] run_command:", adapter.trajectory(run_dir, obs_trunc=4000))

        meta = json.loads((run_dir / "meta.json").read_text())
        self.assertTrue(meta["ok"])
        self.assertEqual(meta["exit_code"], 0)
        telemetry = json.loads((run_dir / "telemetry.json").read_text())
        # Coordinator usage comes from result.usage (150, 60, 45, 20) -- NOT double counted with step_update (120)
        self.assertEqual(telemetry["model"], "gemini-3.8-flash-high")
        self.assertEqual(telemetry["effort"], "high")
        self.assertEqual(telemetry["input_tokens"], 150)
        self.assertEqual(telemetry["cached_input_tokens"], 60)
        self.assertEqual(telemetry["output_tokens"], 45)
        self.assertEqual(telemetry["reasoning_tokens"], 20)
        self.assertEqual(telemetry["tool_calls"], 1)
        self.assertEqual(telemetry["tool_errors"], 0)
        self.assertEqual(telemetry["termination_reason"], "stop")
        self.assertEqual(telemetry["output_artifact"], "deliverable.patch")
        self.assertIsNone(telemetry["cost_usd"])

        # Isolated gemini API home with empty MCP config and ambient Anthropic stripped
        rec = self._records()[0]
        self.assertEqual(rec["home"], str(run_dir / "agy-home"))
        self.assertIsNone(rec["anthropic_key"])
        self.assertEqual(rec["gemini_key"], "secret-gemini-key")
        mcp = json.loads((run_dir / "agy-home/.gemini/config/mcp_config.json").read_text())
        self.assertEqual(mcp, {"mcpServers": {}})
        self.assertEqual(rec["settings"]["modelProvider"], "gemini")
        self.assertEqual(rec["settings"]["artifactReviewPolicy"], "always-proceed")

        # Second execute resumes without running CLI again
        self.assertEqual(adapter.execute("one", None, "agy-r1"), run_dir)
        self.assertEqual(len(self._records()), 1)

    def test_profile_isolation_preserves_custom_home_and_doctor_reports_personal_files(self):
        profile_home = self.root / "isolated-agy-profile"
        (profile_home / ".gemini/config").mkdir(parents=True)
        (profile_home / ".gemini/GEMINI.md").write_text("personal rule\n")
        (profile_home / ".gemini/config/mcp_config.json").write_text('{"mcpServers": {}}\n')

        ctx = self._context(
            **{
                "executor.model": "agy-login/gemini-3.8-flash-high",
                "improver.model": "agy-login/gemini-3.8-flash-high",
                "providers.agy-login.home": str(profile_home),
            }
        )
        adapter = suite.make_adapter(ctx)
        adapter.execute("one", None, "profile-r1")
        rec = self._records()[0]
        self.assertEqual(rec["home"], str(profile_home))
        # Ambient GEMINI_API_KEY must be stripped from agy-login runs!
        self.assertIsNone(rec["gemini_key"])
        # Settings file must not mutate profile settings.json on disk
        self.assertFalse((profile_home / ".gemini/antigravity-cli/settings.json").exists())

        # Doctor detects personal GEMINI.md/mcp_config.json cleanly without failing
        (self.project / ".harness2").mkdir(parents=True, exist_ok=True)
        (self.project / ".harness2/config.toml").write_text(
            "\n".join(
                [
                    "[executor]",
                    'name = "agy"',
                    'model = "agy-login/gemini-3.8-flash-high"',
                    f'bin = "{self.agy_bin}"',
                    "[improver]",
                    'channel = "agy"',
                    'model = "agy-login/gemini-3.8-flash-high"',
                    "[providers.agy-login]",
                    f'home = "{profile_home}"',
                    "",
                ]
            ),
            encoding="utf-8",
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch.object(registry, "load", return_value=suite):
            code = cli_main(["-C", str(self.project), "--json", "doctor"])
        self.assertEqual(code, 0)
        rows = {row["check"]: row for row in json.loads(out.getvalue())}
        self.assertIn("Antigravity home", rows)
        self.assertTrue(rows["Antigravity home"]["ok"])
        self.assertIn("GEMINI.md", rows["Antigravity home"]["detail"])
        self.assertIn("mcp_config.json", rows["Antigravity home"]["detail"])

    def test_telemetry_worker_transcripts_missing_and_dedup(self):
        brain = self.root / "brain"
        worker_id = "11111111-2222-3333-4444-555555555555"
        tr_dir = brain / worker_id / ".system_generated/logs"
        tr_dir.mkdir(parents=True)
        (tr_dir / "transcript_full.jsonl").write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "step_index": 1,
                            "source": "MODEL",
                            "model": "gemini-3.8-flash-low",
                            "input_tokens": 80,
                            "cache_read_tokens": 20,
                            "output_tokens": 15,
                            "tool_calls": [{"name": "view_file"}],
                        }
                    ),
                    # Duplicate step_index update in transcript should replace, not add
                    json.dumps(
                        {
                            "step_index": 1,
                            "source": "MODEL",
                            "model": "gemini-3.8-flash-low",
                            "input_tokens": 90,
                            "cache_read_tokens": 25,
                            "output_tokens": 18,
                            "tool_calls": [{"name": "view_file"}, {"name": "grep_search"}],
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        stream = "\n".join(
            [
                json.dumps(
                    {
                        "event": "init",
                        "conversation_id": "c-main",
                        "init": {"model": "gemini-3.8-flash-high"},
                    }
                ),
                json.dumps(
                    {
                        "event": "step_update",
                        "step_update": {
                            "conversation_id": "c-main",
                            "step_index": 1,
                            "state": "ACTIVE",
                            "step_type": "tool",
                            "tool_name": "run_command",
                        },
                    }
                ),
                json.dumps(
                    {
                        "event": "step_update",
                        "step_update": {
                            "conversation_id": "c-main",
                            "step_index": 1,
                            "state": "DONE",
                            "step_type": "tool",
                            "tool_name": "run_command",
                            "tool_info": {"output": "Command exited with code 2.\nfail"},
                        },
                    }
                ),
                json.dumps(
                    {
                        "event": "step_update",
                        "step_update": {
                            "conversation_id": "c-main",
                            "step_index": 2,
                            "state": "DONE",
                            "step_type": "subagent",
                            "subagent_info": {
                                "subagents": [
                                    {
                                        "conversation_id": worker_id,
                                        "type_name": "self",
                                        "role": "Worker",
                                    }
                                ]
                            },
                        },
                    }
                ),
                json.dumps(
                    {
                        "event": "result",
                        "result": {
                            "conversation_id": "c-main",
                            "status": "SUCCESS",
                            "response": "All done",
                            "usage": {"input_tokens": 300, "output_tokens": 60},
                        },
                    }
                ),
            ]
        )
        tel = extract_telemetry(
            stream, brain_dirs=[brain], configured_model="gemini-3.8-flash-high"
        )
        self.assertTrue(tel["ok"])
        self.assertEqual(tel["tool_calls"], 1)
        self.assertEqual(tel["tool_errors"], 1)
        self.assertIsNone(tel["cached_input_tokens"])
        self.assertIsNone(tel["reasoning_tokens"])
        nw = tel["nested_workers"]
        self.assertEqual(nw["invocations"], 1)
        self.assertTrue(nw["usage_available"])
        self.assertEqual(nw["worker_models"], ["gemini-3.8-flash-low"])
        self.assertEqual(nw["input_tokens"], 90)
        self.assertEqual(nw["cached_input_tokens"], 25)
        self.assertEqual(nw["output_tokens"], 18)
        self.assertIsNone(nw["reasoning_tokens"])
        self.assertEqual(nw["tool_calls"], 2)

        # Missing worker brain dir records unavailable explicitly (never fabricated 0)
        tel_missing = extract_telemetry(stream, brain_dirs=[self.root / "no-brain"])
        self.assertEqual(tel_missing["nested_workers"]["invocations"], 1)
        self.assertFalse(tel_missing["nested_workers"]["usage_available"])
        self.assertIsNone(tel_missing["nested_workers"]["input_tokens"])

    def test_failure_classifications_cli_auth_malformed_incomplete_rate_limit(self):
        adapter = suite.make_adapter(self._context())

        for mode, expected_field in (
            ("cli_error", "error"),
            ("auth_error", "auth_error"),
            ("malformed", "malformed_lines"),
            ("incomplete", "incomplete"),
            ("print_timeout", "incomplete"),
        ):
            with self.subTest(mode=mode), patch.dict(os.environ, {"FAKE_MODE": mode}):
                run_dir = adapter.execute("one", None, f"fail-{mode}")
                meta = json.loads((run_dir / "meta.json").read_text())
                tel = json.loads((run_dir / "telemetry.json").read_text())
                self.assertFalse(meta["ok"])
                self.assertFalse(tel["ok"])
                self.assertTrue(tel[expected_field])
                if mode == "incomplete":
                    self.assertEqual(
                        final_message(parse_events((run_dir / "stream.jsonl").read_text())), ""
                    )

        # Rate limit triggers clean events.Stopped("usage_limit") and discards partial rollout dir
        with patch.dict(os.environ, {"FAKE_MODE": "rate_limit"}):
            with self.assertRaises(events.Stopped) as caught:
                adapter.execute("one", None, "rl-run")
            self.assertEqual(str(caught.exception), "usage_limit")
            self.assertFalse((self.project / ".harness2/runs/test/rollouts/rl-run").exists())

    def test_timeout_and_cancellation_clean_up_nested_children_without_touching_peers(self):
        # Unrelated process that must never be touched by cancellation/timeout
        unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(lambda: (unrelated.kill(), unrelated.wait()))

        # 1. Timeout cleans up entire spawned subtree (parent + child + grandchild)
        pid_file_to = self.root / "to_pids.txt"
        with patch.dict(
            os.environ, {"FAKE_SPAWN_CHILD_PID_FILE": str(pid_file_to), "FAKE_SLEEP": "60"}
        ):
            adapter = suite.make_adapter(self._context())
            run_dir = adapter.execute("one", None, "to-run", timeout=0.35)
        pids_to = [int(line) for line in pid_file_to.read_text().splitlines() if line.strip()]
        self.assertEqual(len(pids_to), 2)
        for pid in pids_to:
            self.assertFalse(_pid_alive(pid), f"descendant pid {pid} survived timeout")
        meta_to = json.loads((run_dir / "meta.json").read_text())
        self.assertFalse(meta_to["ok"])
        self.assertTrue(meta_to["timed_out"])
        self.assertEqual(meta_to["exit_code"], -1)
        self.assertTrue(_pid_alive(unrelated.pid))

        # 2. Normal completion also reaps any orphaned background child inside the owned pgid
        pid_file_norm = self.root / "norm_pids.txt"
        with patch.dict(
            os.environ,
            {"FAKE_SPAWN_CHILD_PID_FILE": str(pid_file_norm), "FAKE_EXIT_IMMEDIATELY": "1"},
        ):
            run_norm = adapter.execute("one", None, "norm-run", timeout=5)
        self.assertTrue(json.loads((run_norm / "meta.json").read_text())["ok"])
        for pid in [int(x) for x in pid_file_norm.read_text().splitlines() if x.strip()]:
            self.assertFalse(_pid_alive(pid), f"background child {pid} survived normal completion")

        # 3. Cancellation terminates process group, drops incomplete rollout, and allows resumption
        pid_file_cancel = self.root / "cancel_pids.txt"

        def cancel_soon():
            deadline = time.monotonic() + 5.0
            while not pid_file_cancel.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            time.sleep(0.05)
            events.request_stop("signal")

        t = threading.Thread(target=cancel_soon, daemon=True)
        t.start()
        with patch.dict(
            os.environ, {"FAKE_SPAWN_CHILD_PID_FILE": str(pid_file_cancel), "FAKE_SLEEP": "60"}
        ):
            with self.assertRaises(events.Stopped):
                adapter.execute("one", None, "cancel-run", timeout=10)
        t.join(timeout=2)
        for pid in [int(x) for x in pid_file_cancel.read_text().splitlines() if x.strip()]:
            self.assertFalse(_pid_alive(pid), f"descendant pid {pid} survived cancellation")
        self.assertTrue(_pid_alive(unrelated.pid))
        self.assertFalse((self.project / ".harness2/runs/test/rollouts/cancel-run").exists())

        # Clear stop and resume the cancelled run cleanly
        events.start(None)
        resumed = adapter.execute("one", None, "cancel-run")
        self.assertTrue(json.loads((resumed / "meta.json").read_text())["ok"])
        self.assertTrue(runmark.is_complete(resumed, harness_dir=adapter.base_harness()))

    def test_agy_improver_proposer_selector_and_cancellation(self):
        audit = self.root / "audit"
        ctx_dir = self.root / "ctx"
        (ctx_dir / "harness").mkdir(parents=True)
        (ctx_dir / "harness/GEMINI.md").write_text("Base instructions.\n")

        prop = Proposer(
            ChannelSpec(
                "agy",
                "gemini/gemini-3.8-flash-high",
                effort="medium",
                provider=Provider("gemini", "gemini", api_key_env="GEMINI_API_KEY"),
            ),
            audit_dir=audit,
            attempts=1,
        )
        reply = prop.call(
            "Improve the harness.",
            ctx_dir,
            "propose-1",
            system="You own harness edits.",
            harvest="context/harness",
        )
        self.assertTrue(reply.ok)
        self.assertIn("Added verification step", reply.text)
        self.assertIsNotNone(reply.workspace)
        self.assertIn("Verify code.txt", (reply.workspace / "GEMINI.md").read_text())
        self.assertIsNotNone(reply.telemetry)
        self.assertEqual(reply.telemetry["model"], "gemini-3.8-flash-high")
        self.assertEqual(reply.telemetry["input_tokens"], 150)

        meta_files = list(audit.glob("propose-1.*.meta.json"))
        self.assertEqual(len(meta_files), 1)
        audit_meta = json.loads(meta_files[0].read_text())
        self.assertEqual(audit_meta["telemetry"]["output_tokens"], 45)

        # CLI failure on proposer raises ProposerError instead of returning empty reply
        with patch.dict(os.environ, {"FAKE_MODE": "cli_error"}):
            with self.assertRaises(ProposerError):
                prop.call("Fail cleanly", ctx_dir, "fail-prop")

        # Proposer rate limit raises events.Stopped("usage_limit")
        with patch.dict(os.environ, {"FAKE_MODE": "rate_limit"}):
            with self.assertRaises(events.Stopped):
                prop.call("Hit limit", ctx_dir, "rl-prop")


if __name__ == "__main__":
    unittest.main()
