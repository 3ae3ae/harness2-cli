# Copyright 2026 Google LLC
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

"""Regressions for keeping runner and host secrets out of agent subprocesses."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from harness2 import providers
from harness2.core.proposer import Proposer
from harness2.demo import DemoAdapter, DemoProposer


class SubprocessEnvTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.adapter = DemoAdapter(self.root)
        self.proposer = DemoProposer(self.adapter.spec.channels["proposer"])

    def test_scrub_env_across_launchers(self):
        from harness2.substrate.codex import CodexSubstrate

        cx = CodexSubstrate()
        prop_inst = Proposer(self.proposer.spec, audit_dir=self.root / "audit_env")
        host = {
            "HARNESS2_JUDGE_API_KEY": "secret-judge",
            "HARNESS2_BRIDGE_KEY": "secret-bridge",
            "HARNESS2_CODEX_API_KEY": "secret-codex",
            "OPENAI_API_KEY": "sk-judge",
            "ANTHROPIC_API_KEY": "sk-ant",
            "ANTHROPIC_AUTH_TOKEN": "tok-ant",
            "ANTHROPIC_BASE_URL": "https://proxy.example/v1",
            "GITHUB_TOKEN": "ghp_secret",
            "AWS_ACCESS_KEY_ID": "AKIA_TEST",
            "AWS_SECRET_ACCESS_KEY": "aws_secret",
            "AWS_SESSION_TOKEN": "aws_sess",
            "AWS_BEARER_TOKEN_BEDROCK": "bedrock-tok",
            "GOOGLE_API_KEY": "g-key",
            "GOOGLE_GENERATIVE_AI_API_KEY": "g-genai-key",
            "GOOGLE_APPLICATION_CREDENTIALS": "/adc.json",
        }
        # A Vertex solver keeps no provider key, whatever the developer's .env configures.
        with patch.dict("os.environ", host):
            launchers = {
                "scrub_env": providers.scrub_env(),
                "proposer": prop_inst.env(),
                "opencode": self.adapter.substrate.run_env(providers.os.environ, self.root),
                "codex": cx.exec_env(providers.os.environ, codex_home=self.root),
            }
            for name, env_map in launchers.items():
                with self.subTest(launcher=name):
                    for key in (
                        "HARNESS2_JUDGE_API_KEY",
                        "HARNESS2_BRIDGE_KEY",
                        "HARNESS2_CODEX_API_KEY",
                        "OPENAI_API_KEY",
                        "ANTHROPIC_API_KEY",
                        "ANTHROPIC_AUTH_TOKEN",
                        "ANTHROPIC_BASE_URL",
                        "GITHUB_TOKEN",
                        "AWS_ACCESS_KEY_ID",
                        "AWS_SECRET_ACCESS_KEY",
                        "AWS_SESSION_TOKEN",
                        "AWS_BEARER_TOKEN_BEDROCK",
                        "GOOGLE_API_KEY",
                        "GOOGLE_GENERATIVE_AI_API_KEY",
                    ):
                        self.assertNotIn(key, set(env_map))
                    self.assertEqual(env_map["GOOGLE_APPLICATION_CREDENTIALS"], "/adc.json")
            with patch("harness2.demo.subprocess.run") as demo_run:
                demo_run.return_value = Mock(stdout="42\n", stderr="")
                self.adapter.execute(
                    "ledger-1", self.adapter.base_harness(), "env_check", timeout=5
                )
                demo_env = demo_run.call_args.kwargs["env"]
                self.assertNotIn("HARNESS2_JUDGE_API_KEY", set(demo_env))
                self.assertNotIn("GITHUB_TOKEN", set(demo_env))

    def test_configured_provider_keeps_its_key(self):
        from dataclasses import replace

        from harness2.substrate.opencode import OpencodeSubstrate

        host = {
            "OPENAI_API_KEY": "sk-openai",
            "ANTHROPIC_API_KEY": "sk-ant",
            "ANTHROPIC_BASE_URL": "https://proxy.example/v1",
            "GEMINI_API_KEY": "gm-key",
            "GOOGLE_API_KEY": "g-key",
            "GOOGLE_GENERATIVE_AI_API_KEY": "g-genai-key",
            "AWS_ACCESS_KEY_ID": "AKIA_TEST",
            "AWS_SECRET_ACCESS_KEY": "aws_secret",
            "AWS_BEARER_TOKEN_BEDROCK": "bedrock-tok",
            "GITHUB_TOKEN": "ghp_secret",
            "HARNESS2_JUDGE_API_KEY": "secret-judge",
        }
        cases = {
            "openai/gpt-test": {"OPENAI_API_KEY"},
            "anthropic/claude-test": {"ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"},
            "google/gemini-test": {
                "GOOGLE_API_KEY",
                "GOOGLE_GENERATIVE_AI_API_KEY",
                "GEMINI_API_KEY",
            },
            "amazon-bedrock/claude-test": {
                "AWS_ACCESS_KEY_ID",
                "AWS_SECRET_ACCESS_KEY",
                "AWS_BEARER_TOKEN_BEDROCK",
            },
            "google-vertex/gemini-test": set(),
        }
        with patch.dict("os.environ", host):
            for model, kept in cases.items():
                with self.subTest(model=model):
                    executor = OpencodeSubstrate().run_env(providers.os.environ, self.root)
                    explicit = OpencodeSubstrate().run_env(
                        providers.os.environ, self.root, model=model
                    )
                    improver = Proposer(
                        replace(self.proposer.spec, kind="opencode", model=model),
                        audit_dir=self.root / "audit_keep",
                    ).env()
                    self.assertEqual(set(host) & set(executor), set())
                    self.assertEqual(set(host) & set(explicit), kept)
                    self.assertEqual(set(host) & set(improver), kept - set(providers.STRIP_ENV))

    def test_session_env_keeps_only_its_provider_credentials(self):
        aws = providers.provider_keys("amazon-bedrock/model")
        host = {
            **dict.fromkeys(providers.SCRUB_ENV | set(providers.STRIP_ENV), "host-secret"),
            "PATH": "/usr/bin",
        }
        for provider, kept in (
            (providers.Provider("custom", "openai", api_key_env="SESSION_KEY"), {"SESSION_KEY"}),
            (providers.Provider("aws", "bedrock"), set(aws)),
        ):
            with self.subTest(provider=provider.type):
                source = host | ({"SESSION_KEY": "session-secret"} if provider.api_key_env else {})
                env = providers.session_env(source, provider)
                self.assertEqual(env, {k: source[k] for k in kept | {"PATH"}})
                self.assertTrue(all(k not in env for k in providers.STRIP_ENV))
                self.assertNotIn("GITHUB_TOKEN", env)
                for key in kept:
                    self.assertEqual(env[key], source[key])

    def test_claude_and_codex_executor_envs_drop_host_tokens(self):
        from harness2.substrate.claude import ClaudeSubstrate
        from harness2.substrate.codex import CodexSubstrate

        host = {
            **dict.fromkeys(providers.SCRUB_ENV | set(providers.STRIP_ENV), "host-secret"),
            "SESSION_KEY": "session-secret",
            "CODEX_API_KEY": "ambient-codex-key",
            "PATH": "/usr/bin",
        }
        provider = providers.Provider("custom", "anthropic", api_key_env="SESSION_KEY")
        with patch.dict(providers.os.environ, host, clear=True):
            claude = ClaudeSubstrate().env(host, provider, self.root / "claude-home")
        codex = CodexSubstrate().env(host, self.root / "codex-home", provider=provider)
        for env in (claude, codex):
            self.assertEqual(env["SESSION_KEY"], "session-secret")
            for key in providers.SCRUB_ENV - {"ANTHROPIC_API_KEY"}:
                self.assertNotIn(key, env)
            for key in providers.STRIP_ENV:
                if key != "ANTHROPIC_API_KEY":
                    self.assertNotIn(key, env)
        self.assertEqual(claude["ANTHROPIC_API_KEY"], "session-secret")
        self.assertNotIn("ANTHROPIC_API_KEY", codex)
        login = CodexSubstrate().env(
            host, None, provider=providers.Provider("login", "chatgpt-login")
        )
        self.assertNotIn("OPENAI_API_KEY", login)
        self.assertNotIn("CODEX_API_KEY", login)


if __name__ == "__main__":
    unittest.main()
