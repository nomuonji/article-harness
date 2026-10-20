"""Exercise the real subprocess adapter against a local fake Codex executable.

These tests verify transport/recovery behavior, not editorial quality or a live
Codex service. All subprocesses and artifacts are confined to temporary dirs.
"""

import copy
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import time
import unittest

from article_harness.runner import AgentError, CodexRunner


FAKE_CLI = r'''
import json
import os
from pathlib import Path
import subprocess
import sys
import time

args = sys.argv[1:]
run_dir = Path(args[args.index('--output-schema') + 1]).parent
recipe = json.loads((run_dir / 'recipe.json').read_text())
prompt = sys.stdin.read()
(run_dir / 'capture.json').write_text(json.dumps({
    'args': args, 'stdin': prompt, 'cwd': os.getcwd(),
    'schema': json.loads(Path(args[args.index('--output-schema') + 1]).read_text()),
    'pid': os.getpid(), 'pgid': os.getpgrp(),
}))
print(json.dumps({'type': 'thread.started', 'thread_id': 'fake-session'}), flush=True)
print('fake CLI stderr', file=sys.stderr, flush=True)
if recipe.get('mode') == 'timeout':
    ready = run_dir / 'child-ready'
    child = subprocess.Popen([sys.executable, '-c',
        "import os, pathlib, signal, sys, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)",
        str(ready)])
    while not ready.exists():
        time.sleep(0.005)
    (run_dir / 'child.json').write_text(json.dumps({'pid': child.pid, 'pgid': os.getpgid(child.pid)}))
    time.sleep(60)
if recipe.get('mode') == 'nonzero':
    sys.exit(17)
answer = Path(args[args.index('--output-last-message') + 1])
if recipe.get('mode') == 'malformed':
    answer.write_text('{not json')
elif recipe.get('mode') != 'missing':
    answer.write_text(json.dumps(recipe['answer']))
'''


@unittest.skipUnless(os.name == "posix", "Fake executable/process-group tests target Linux/WSL")
class CodexRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace with spaces"
        self.workspace.mkdir()
        self.binary = self.root / "fake codex"
        self.binary.write_text("#!" + sys.executable + "\n" + FAKE_CLI, encoding="utf-8")
        self.binary.chmod(0o755)
        self.runner = CodexRunner(str(self.binary), model="test-model")
        self.task = {
            "id": "correct-article", "files": ["article.md"],
            "brief": "Check the source's exact scope.",
            "reader_job": "Understand the eligibility rule.",
            "acceptance_criteria": ["Explain the eligibility rule"],
            "sources": ["https://example.invalid/official"],
            "policy_files": ["docs/EDITORIAL.md"],
        }
        self.feedback = [{"summary": "日本語の前回指摘", "findings": ["Read the original source"]}]
        self.counter = 0

    def run_fake(self, role="worker", *, answer=None, mode=None, timeout=5, task=None):
        self.counter += 1
        run_dir = self.root / f"run {self.counter}"
        run_dir.mkdir()
        recipe = {"answer": answer if answer is not None else {
            "status": "done", "summary": "Corrected the rule.",
            "evidence": [{"claim": "Eligibility", "source": "official section 2", "support": "Exact condition"}],
        }}
        if mode:
            recipe["mode"] = mode
        (run_dir / "recipe.json").write_text(json.dumps(recipe), encoding="utf-8")
        # Retain for assertions after the expected exceptions as well.
        self.last_run = run_dir
        result = self.runner.run(role, task or self.task, self.workspace,
                                 run_dir, self.feedback, timeout)
        return result, run_dir

    def review(self):
        return {"verdict": "pass", "summary": "Source and article checked.", "findings": [],
                "criteria": [{"criterion": "Explain the eligibility rule", "status": "pass",
                              "evidence": "article.md:8 and official source section 2"}]}

    def test_worker_cli_schema_stdin_and_persistent_logs(self):
        result, run_dir = self.run_fake()
        captured = json.loads((run_dir / "capture.json").read_text())
        args = captured["args"]
        self.assertEqual(args[:3], ["-a", "never", "exec"])
        self.assertEqual(args[args.index("--sandbox") + 1], "workspace-write")
        self.assertEqual(args[args.index("-C") + 1], str(self.workspace))
        self.assertEqual(args[args.index("--model") + 1], "test-model")
        self.assertEqual(args[-1], "-")
        self.assertIn("--json", args)
        self.assertEqual(captured["cwd"], str(self.workspace))
        self.assertIn(self.task["brief"], captured["stdin"])
        self.assertIn("日本語の前回指摘", captured["stdin"])
        self.assertIn("AGENTS.md", captured["stdin"])
        self.assertEqual(captured["stdin"], (run_dir / "prompt.txt").read_text())
        self.assertEqual(captured["schema"]["properties"]["status"]["enum"], ["done", "blocked", "refused"])
        self.assertFalse(captured["schema"]["additionalProperties"])
        self.assertEqual(result["status"], "done")
        self.assertEqual(json.loads((run_dir / "answer.json").read_text()), result)
        self.assertEqual(json.loads((run_dir / "command.json").read_text()), [str(self.binary)] + args)
        self.assertEqual(json.loads((run_dir / "events.jsonl").read_text())["thread_id"], "fake-session")
        self.assertIn("fake CLI stderr", (run_dir / "stderr.log").read_text())
        self.assertTrue((run_dir / ".invocation").exists())

    def test_reviewer_and_planner_run_read_only(self):
        planner_task = {"inventory": ["article.md"], "max_tasks": 1,
                        "brief": "Inspect actual defects", "sources": self.task["sources"],
                        "policy_files": self.task["policy_files"]}
        planned = {"id": "fix-rule", "files": ["article.md"],
                   "brief": "Remove an unsupported blanket exemption.",
                   "reader_job": "Check actual eligibility.",
                   "acceptance_criteria": ["State all official conditions"],
                   "sources": ["https://example.invalid/official"]}
        cases = [
            ("reviewer", self.task, self.review()),
            ("planner", planner_task, {"summary": "One source-backed defect", "tasks": [planned]}),
        ]
        for role, task, answer in cases:
            with self.subTest(role=role):
                result, run_dir = self.run_fake(role, task=task, answer=answer)
                captured = json.loads((run_dir / "capture.json").read_text())
                args = captured["args"]
                self.assertEqual(args[args.index("--sandbox") + 1], "read-only")
                self.assertEqual(result, answer)

    def test_passing_review_rejects_unknown_failed_or_uncovered_criteria(self):
        variants = []
        for status in ("unknown", "fail"):
            receipt = self.review()
            receipt["criteria"][0]["status"] = status
            variants.append(receipt)
        receipt = self.review()
        receipt["criteria"] = []
        variants.append(receipt)
        receipt = self.review()
        receipt["criteria"].append(copy.deepcopy(receipt["criteria"][0]))
        variants.append(receipt)
        receipt = self.review()
        receipt["findings"] = [{"path": "article.md", "detail": "Material contradiction remains."}]
        variants.append(receipt)
        receipt = self.review()
        receipt["criteria"][0]["criterion"] = "A different, weaker criterion"
        variants.append(receipt)
        for receipt in variants:
            with self.subTest(receipt=receipt):
                with self.assertRaises(AgentError) as caught:
                    self.run_fake("reviewer", answer=receipt)
                self.assertEqual(caught.exception.kind, "invalid_output")

    def test_malformed_missing_and_wrong_shape_receipts_are_rejected(self):
        cases = [("malformed", None), ("missing", None), (None, {"status": "done"}),
                 (None, {"status": "done", "summary": 7, "evidence": []}),
                 (None, {"status": "unknown", "summary": "No result", "evidence": []})]
        for mode, answer in cases:
            with self.subTest(mode=mode, answer=answer):
                with self.assertRaises(AgentError) as caught:
                    self.run_fake(mode=mode, answer=answer)
                self.assertEqual(caught.exception.kind, "invalid_output")

    def test_reused_directory_cannot_replay_a_previous_answer(self):
        _, run_dir = self.run_fake()
        captured_before = (run_dir / "capture.json").read_bytes()
        with self.assertRaises(AgentError) as caught:
            self.runner.run("worker", self.task, self.workspace, run_dir, [], 5)
        self.assertEqual(caught.exception.kind, "run_dir_in_use")
        self.assertEqual((run_dir / "capture.json").read_bytes(), captured_before)

    def test_stale_answer_without_invocation_marker_is_not_accepted(self):
        run_dir = self.root / "stale"
        run_dir.mkdir()
        (run_dir / "answer.json").write_text('{"status":"done"}')
        with self.assertRaises(AgentError) as caught:
            self.runner.run("worker", self.task, self.workspace, run_dir, [], 5)
        self.assertEqual(caught.exception.kind, "run_dir_in_use")
        self.assertFalse((run_dir / "capture.json").exists())

    def test_nonzero_exit_is_failure_even_without_json(self):
        with self.assertRaises(AgentError) as caught:
            self.run_fake(mode="nonzero")
        self.assertEqual(caught.exception.kind, "process_failed")
        self.assertIn("17", caught.exception.detail)
        self.assertIn("fake CLI stderr", (self.last_run / "stderr.log").read_text())

    @staticmethod
    def running(pid):
        try:
            status = Path(f"/proc/{pid}/stat").read_text()
            return status.rsplit(")", 1)[1].split()[0] not in ("Z", "X")
        except FileNotFoundError:
            return False

    @unittest.skipUnless(Path("/proc/self/stat").exists(), "Child liveness assertion requires Linux procfs")
    def test_timeout_kills_entire_process_group_including_term_ignoring_child(self):
        try:
            started = time.monotonic()
            with self.assertRaises(AgentError) as caught:
                self.run_fake(mode="timeout", timeout=1.0)
            self.assertEqual(caught.exception.kind, "timeout")
            self.assertLess(time.monotonic() - started, 8)
            leader = json.loads((self.last_run / "capture.json").read_text())
            child = json.loads((self.last_run / "child.json").read_text())
            self.assertEqual(leader["pid"], leader["pgid"])
            self.assertEqual(child["pgid"], leader["pgid"])
            deadline = time.monotonic() + 2
            while self.running(child["pid"]) and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertFalse(self.running(leader["pid"]), "Invocation leader is still running")
            self.assertFalse(self.running(child["pid"]), "Invocation descendant survived timeout")
        finally:
            # Ensure a failed assertion cannot leak the fake process tree.
            capture = getattr(self, "last_run", self.root) / "capture.json"
            if capture.exists():
                group = json.loads(capture.read_text())["pgid"]
                try:
                    os.killpg(group, signal.SIGKILL)
                except ProcessLookupError:
                    pass


if __name__ == "__main__":
    unittest.main()
