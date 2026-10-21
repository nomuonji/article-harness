# Validation — 2026-10-21 JST / v0.2

- Operator-minimal update: **54 tests passed**. Three added behavior tests cover offline reviewed proposals releasing a repository for the next article, just-in-time scoped delivery claims with renewal, and expired-claim rejection at handoff. The underlying Operator databases and schedules were not migrated or changed.

- Full suite: **51 passed** (23 local orchestration + 8 Codex subprocess adapter + 20 native state/artifact integration).
- Actual ChatGPT Work native Worker and separate Reviewer completed a fictional source-based article repair. The real review covered all three fixed criteria and passed; native state reached `ready_for_handoff`, exported to ZIP and restored with the same digest/status. Artifact packet: `examples/native-fixture/`.
- Native mode does not require or spawn Codex CLI. Codex Cloud product execution remains untested; runtime-specific subagent/MCP capabilities must be checked there. Real Site Operator shared claims and production article delivery are not tested by the fixture.

- `python3 -m unittest discover -s tests -v`: **31 passed** (23 orchestration/Git/queue integration tests + 8 Codex subprocess adapter tests).
- `python3 examples/demo.py --output <fresh-directory>`: offline simulation, 4 tasks across 4 fixture repositories; 3 ready_for_handoff, 1 blocked; one task revised and reviewed a second time. Source checkouts remained unchanged.
- Codex CLI is not installed in the build environment. Adapter tests launch a fake executable and verify arguments, stdin, schema/output handling, role sandboxes, timeout and process-group cleanup. They do not prove live Codex authentication or editorial quality.
- Read-only independent code review found reservation checkpoint loss, inside-repo default state, mutable worktree acknowledgement, and applied-item planning exclusion issues. These were corrected and covered by regression tests.
- No scheduled jobs, managed sites, external reservations or publication settings were changed. Actual Site Operator reservation/writing adapters remain parent-owned. Native GitHub delivery readback is implemented separately (see below).

The deliverable includes a local CLI controller and a native-subagent state/artifact helper with parent instructions. It is not a deployed continuous service, and reviewed artifacts are not published articles.

## Native GitHub delivery verification

- 65 tests pass, including remote original mismatch, stale article, unrelated HEAD changes, overlapping/renamed PR files, unexpected additions/deletions/modes, wrong commit parent/body, blob integrity, truncated trees, HTTP 429, expired claims during slow reads, and continued draft work beside pending delivery.
- Independent code review found claim revalidation after remote HEAD reads and accepted-uppercase-SHA compatibility issues; both fixed with regression tests.
- Live unauthenticated official GitHub REST reads succeeded for main commit `16ba5ef3af4590290ad433bdae43f00ca862a9c7`, tree `fb93d930da538325441fe135f95d8752ea299ca8`, and README blob integrity (10,526 bytes).
- Managed-site live delivery remains untested: `seo_agent_context({role: 'executor'})` returned `INVALID_ARGUMENT`, so no shared claim was assumed or managed article published.
