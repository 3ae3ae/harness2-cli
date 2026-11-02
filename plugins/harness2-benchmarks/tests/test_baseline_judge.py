# Copyright 2026 Google LLC
# SPDX-License-Identifier: Apache-2.0
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

"""Actual summary producer -> LAB cached score reader -> judge CLI."""

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from harness2_benchmarks.lab import LabAdapter

from harness2 import settings
from harness2.cli import judge, main
from harness2.core.schedule import Namespace, TaskResult, _write_summary
from harness2.core.selection import Selection


class BaselineJudgeTests(unittest.TestCase):
    def judge_cached(self, payload, *, bases=True):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".harness2").mkdir()
            run = root / "local-run"
            run.mkdir()
            (run / "scores.json").write_text(json.dumps(payload), encoding="utf-8")
            namespace = Namespace("lab", "oc", "agg", 1)
            ns_dir = root / "lab/adapt" / str(namespace)
            task_dir = namespace.task_dir(root / "lab/adapt", "contracts/task-1")
            task_dir.mkdir(parents=True)
            selected = Selection(
                task_id="contracts/task-1",
                mode="agg",
                k=1,
                chosen_label="agg1",
                chosen_run_id="adapted-1",
                ok=True,
            ).to_dict()
            selected["chosen_run_dir"] = str(run)
            result = TaskResult(
                task_id="contracts/task-1",
                domain="contracts",
                k=1,
                status="ok",
                modes={"agg": {"ok": True}},
                base=[{"label": "base1", "run_id": "reference-1", "run_dir": str(run)}],
                selections={"agg": selected},
            )
            _write_summary(task_dir / "summary.json", result)
            run_root = root / "lab"
            (run_root / "run.json").write_text(
                json.dumps(
                    {
                        "name": "local-run",
                        "suite": "lab",
                        "executor": "opencode",
                        "stream": [["contracts", "contracts/task-1"]],
                        "settings": {
                            **{
                                f"improver.{k}": v for k, v in settings.DEFAULTS["improver"].items()
                            },
                            "judge.model": "fixture/judge",
                        },
                        "schedule": {"k": 1, "modes": "parallel", "seed": None},
                    }
                )
            )
            adapter = LabAdapter(runs_root=run_root, model="fixture/executor")
            stdout, stderr = io.StringIO(), io.StringIO()
            with (
                patch.dict(os.environ, {"XDG_CONFIG_HOME": str(root / "config")}),
                patch.object(judge, "open_run", return_value=(adapter, run_root)),
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                status = main.main(
                    ["-C", str(root), "judge", "local-run"] + (["--bases"] if bases else [])
                )
            process = SimpleNamespace(
                returncode=status, stdout=stdout.getvalue(), stderr=stderr.getvalue()
            )
            cache = ns_dir / ("base_judged.json" if bases else "judged.json")
            return process, json.loads(cache.read_text())["rows"] if cache.is_file() else None

    def test_missing_or_nonpositive_criteria_are_ungraded(self):
        for criteria in (None, 0, -1):
            with self.subTest(criteria=criteria):
                result, rows = self.judge_cached(
                    {"n_passed": 0, "n_criteria": criteria, "all_pass": False}
                )
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertFalse(rows[0]["graded"])
                self.assertIsNone(rows[0]["frac"])
                self.assertIn("Graded 0 of 1", result.stdout)

    def test_positive_criteria_preserve_zero_partial_and_full_scores(self):
        for passed in (0, 1, 2):
            with self.subTest(passed=passed):
                payload = {"n_passed": passed, "n_criteria": 2, "all_pass": passed == 2}
                result, rows = self.judge_cached(payload)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(rows[0]["raw"], payload)
                self.assertTrue(rows[0]["graded"])
                self.assertIn("Graded 1 of 1", result.stdout)

    def test_selected_run_uses_the_same_criteria_contract(self):
        for criteria, status in ((None, 1), (0, 1), (2, 0)):
            with self.subTest(criteria=criteria):
                result, rows = self.judge_cached(
                    {"n_passed": 0, "n_criteria": criteria, "all_pass": False}, bases=False
                )
                self.assertEqual(result.returncode, status, result.stdout + result.stderr)
                self.assertEqual(rows[0]["graded"], criteria == 2)

    def test_absent_grade_fields_do_not_count_as_a_completed_grade(self):
        result, rows = self.judge_cached({})
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertFalse(rows[0]["graded"])

    def test_missing_numerator_is_ungraded_for_baseline_and_selected_runs(self):
        for bases in (False, True):
            with self.subTest(bases=bases):
                result, rows = self.judge_cached({"n_criteria": 2, "all_pass": False}, bases=bases)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                if bases:
                    self.assertFalse(rows[0]["graded"])
                else:
                    self.assertFalse(rows[0]["graded"])
                    self.assertIsNone(rows[0]["frac"])


if __name__ == "__main__":
    unittest.main()
