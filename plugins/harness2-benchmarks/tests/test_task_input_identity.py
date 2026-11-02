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

"""Task inputs are part of a rollout's identity, at both cache layers.

A rollout answers the task it was given. When the instruction or the source material changes
under the same task id, the saved answer is no longer an answer to the current question, so
neither the task summary nor the rollout marker may be reused.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness2_benchmarks import config
from harness2_benchmarks.lab import LabAdapter


class RolloutReuseTests(unittest.TestCase):
    """The reported symptom, end to end through `LabAdapter.execute`.

    The executor is replaced by a deterministic function that writes the current document into
    the run directory: no model call, no downloaded benchmark, no shell.
    """

    task_id = "example/task-1"
    run_id = "lab-cx-agg1/example__task-1/base1"

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.lab = self.root / "lab"
        self.task = self.lab / "tasks" / self.task_id
        (self.task / "documents").mkdir(parents=True)
        self.task_json = self.task / "task.json"
        self.document = self.task / "documents" / "source.txt"
        self.task_json.write_text(json.dumps({"instructions": "Use version one."}))
        self.document.write_text("version-one")

    def _adapter(self):
        lab_repo = patch.object(config, "lab_repo", return_value=self.lab)
        lab_repo.start()
        self.addCleanup(lab_repo.stop)
        return LabAdapter(runs_root=self.root / "runs", substrate="cx")

    def _runner(self, adapter):
        def runner(task_id, install, run_ids, timeout):
            out = adapter._run_dir(run_ids[0])
            (out / "output").mkdir(parents=True, exist_ok=True)
            (out / "output" / "answer.txt").write_text(self.document.read_text())
            (out / "metrics.json").write_text("{}")
            return [out]

        return runner

    def _answer(self, run_dir):
        return (Path(run_dir) / "output" / "answer.txt").read_text()

    def test_unchanged_inputs_reuse_the_rollout(self):
        adapter = self._adapter()
        with patch.object(adapter.substrate, "execute", side_effect=self._runner(adapter)) as execs:
            first = adapter.execute(self.task_id, None, self.run_id)
            second = adapter.execute(self.task_id, None, self.run_id)
            self.assertEqual(execs.call_count, 1)
            self.assertEqual(self._answer(first), "version-one")
            self.assertEqual(Path(second), Path(first))

    def test_changed_source_files_re_roll_the_rollout(self):
        adapter = self._adapter()
        with patch.object(adapter.substrate, "execute", side_effect=self._runner(adapter)) as execs:
            adapter.execute(self.task_id, None, self.run_id)
            self.document.write_text("version-two")
            out = adapter.execute(self.task_id, None, self.run_id)
            self.assertEqual(execs.call_count, 2)
            self.assertEqual(self._answer(out), "version-two")

    def test_changed_instructions_re_roll_the_rollout(self):
        adapter = self._adapter()
        with patch.object(adapter.substrate, "execute", side_effect=self._runner(adapter)) as execs:
            adapter.execute(self.task_id, None, self.run_id)
            self.task_json.write_text(json.dumps({"instructions": "Use version two."}))
            out = adapter.execute(self.task_id, None, self.run_id)
            self.assertEqual(execs.call_count, 2)
            self.assertIn("version two", adapter.task_prompt(self.task_id))
            # The source material is unchanged, so the fresh answer still matches it.
            self.assertEqual(self._answer(out), "version-one")
