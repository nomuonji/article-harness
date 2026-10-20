# Article Harness development

For ChatGPT Work or Codex Cloud execution, read `docs/native-parent-prompt.md`
and `docs/NATIVE_MODE.md`. Use the environment's native subagent tools, not a
nested `codex exec`. `article_harness.native` is the state/artifact helper; it
never starts a model. The parent must actually delegate and verify returned
artifacts. Native work does not assume CLI authentication or automatic MCP
connection transfer. Model availability, tools and real shared claims are
runtime-specific. Never claim an untested cloud deployment succeeded.

Python 3.11+ standard library, Linux/WSL. Run from this directory:

```bash
python3 -m unittest discover -s tests -v
python3 -m article_harness --help
```

The harness owns only its local queue, isolated Git worktrees and patch receipts.
Do not silently change scheduled Site Operator jobs, fabricate a shared claim,
push/merge/publish, or describe a reviewed patch as deployed. External reservation
adapters must use actual shared ownership and persist reservation identifiers.

Keep model execution in `runner.py`, scheduling/state/Git checks in `core.py`,
CLI wiring in `__main__.py`. Use separate fresh read-only reviewer sessions.
Never reset attempt budgets or replay refused work through another transport.
Protect immutable task scope and reviewed artifact hashes when adding recovery.

Tests should exercise observable orchestration and artifact integrity with temp
repositories/fake subprocesses. Do not add tests that merely copy implementation.
The offline demo uses fictional shop policies. Real editorial quality and live
Codex authentication require an explicit live run; do not claim them from mocks.
