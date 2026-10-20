# Validation — 2026-10-21 JST / v0.2

- Full suite: **51 passed** (23 local orchestration + 8 Codex subprocess adapter + 20 native state/artifact integration).
- Actual ChatGPT Work native Worker and separate Reviewer completed a fictional source-based article repair. The real review covered all three fixed criteria and passed; native state reached `ready_for_handoff`, exported to ZIP and restored with the same digest/status. Artifact packet: `examples/native-fixture/`.
- Native mode does not require or spawn Codex CLI. Codex Cloud product execution remains untested; runtime-specific subagent/MCP capabilities must be checked there. Real Site Operator shared claims and production article delivery are not tested by the fixture.

- `python3 -m unittest discover -s tests -v`: **31 passed** (23 orchestration/Git/queue integration tests + 8 Codex subprocess adapter tests).
- `python3 examples/demo.py --output <fresh-directory>`: offline simulation, 4 tasks across 4 fixture repositories; 3 ready_for_handoff, 1 blocked; one task revised and reviewed a second time. Source checkouts remained unchanged.
- Codex CLI is not installed in the build environment. Adapter tests launch a fake executable and verify arguments, stdin, schema/output handling, role sandboxes, timeout and process-group cleanup. They do not prove live Codex authentication or editorial quality.
- Read-only independent code review found reservation checkpoint loss, inside-repo default state, mutable worktree acknowledgement, and applied-item planning exclusion issues. These were corrected and covered by regression tests.
- No scheduled jobs, managed sites, external reservations, production code or publication settings were changed. Actual Site Operator reservation/delivery adapters are not implemented in this version.

The deliverable includes a local CLI controller and a native-subagent state/artifact helper with parent instructions. It is not a deployed continuous service, and reviewed artifacts are not published articles.
