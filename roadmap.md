# Ralphzilla — Roadmap

**Status**: M1 ✓ M2 ✓ M3 ✓ — M4 in progress
**Last updated**: 2026-04-21

---

## Vision

A self-contained AI sprint runner that takes a `prd.json` backlog, executes tasks end-to-end via AI agents, handles failures autonomously, and delivers merged PRs — without requiring human intervention for recoverable errors.

Rival to [get-shit-done](https://github.com/gsd-build/get-shit-done/) on completeness and maturity. Differentiated by: deeper failure recovery, typed exception hierarchy, explicit Scrum Master supervision layer, and framework-agnostic per-epic prompt context.

---

## Milestone 1 — Core Loop (MVP)

**Goal**: `ralph.py` exists and runs end-to-end on a real project.

Deliver the full architecture from the rewrite plan as a single working Python file. No Scrum Master yet. No parallelism. Just a solid, reliable sequential sprint executor.

### Deliverables

- [x] `SubprocessRunner` — subprocess wrapper, `env_removals`, no `shell=True`
- [x] `RalphLogger` — dual-stream, fixed-width level prefix
- [x] `TaskTracker` — sole owner of `prd.json` / `progress.txt`; fresh load on every write
- [x] `PlanChecker` — structural validation (Tier 1); raises `PlanInvalidError` on schema violations
- [x] `BranchManager` — git operations; SSH-only enforcement; `reset --hard` sync
- [x] `PRManager` — `gh pr` operations; retry on fresh-PR race condition
- [x] `PRDGuard` — pre-merge diff check; closes PR and aborts on any `prd.json` mutation
- [x] `PromptBuilder` — all prompt templates as stateless static methods; `epic_addenda` support
- [x] `AIRunner` — claude / gemini / opencode backends; nested-Claude session detection
- [x] `PreCommitGate` — runs pre-commit, coder fix loop, warns after max rounds
- [x] `TestRunner` — runs `quality_checks` from `prd.json`; coder fix loop
- [x] `ReviewLoop` — reviewer + coder fix loop; `CHANGES REQUESTED` parsing
- [x] `CIPoller` — JSON CI parsing; run-ID pinning to avoid stale-data race; coder fix loop
- [x] `Orchestrator` — main loop; `_preflight()`; per-task state machine
- [x] CLI — all flags from the rewrite plan (`--skip-review`, `--resume`, `--task`, `--dry-run`, etc.)
- [x] `CODER_INSTRUCTIONS.md` template — shipped with Ralphzilla, projects copy and customise
- [x] `REVIEWER_INSTRUCTIONS.md` template — same

### Success criteria

- Runs to completion on a real `prd.json` with ≥5 tasks
- Handles at least one CI failure and one reviewer CHANGES REQUESTED autonomously
- `--dry-run` produces a readable plan with no side effects
- No `shell=True` anywhere; no grep-based CI parsing

---

## Milestone 2 — Quality & Reliability

**Goal**: Ralphzilla is testable, maintainable, and confident to run unattended.

### Deliverables

- [x] Unit tests for all classes — `TaskTracker`, `PlanChecker`, `BranchManager`, `PRDGuard`, `PromptBuilder`, `CIPoller`
- [x] Integration tests — dry-run smoke test against a fixture `prd.json`
- [x] ≥70% line coverage (matching GSD's bar)
- [x] `--validate-plan` flag — `PlanChecker` Tier 2 AI sanity check; warns on untestable ACs, non-atomic tasks
- [x] `LoopSupervisor` clean-exit verification — cross-checks log for `CLEAN_EXIT_MARKERS` after every run
- [x] Sprint summary report — markdown output at end of run (tasks completed, PRs, CI results, escalations)
- [x] `progress.txt` human-readable format — easy to read at a glance post-sprint

### Success criteria

- `pytest` passes; coverage report ≥70%
- `--validate-plan` catches at least: empty ACs, unresolved `depends_on`, non-atomic tasks
- Sprint summary is parseable by the Scrum Master layer (Milestone 4)

---

## Milestone 3 — Distribution & Developer Experience

**Goal**: Someone who hasn't read the plan can get Ralphzilla running in under 5 minutes.

### Deliverables

- [x] `ralph init` command — interactive setup via `DiscoveryWizard` (6-question grill-me lite) that produces:
  - `prd.json` scaffold (with `epic_addenda`, `quality_checks`, empty task list)
  - `CODER_INSTRUCTIONS.md` customised to the project's stack
  - `REVIEWER_INSTRUCTIONS.md`
  - Git hook that blocks commits directly to main
- [x] `DiscoveryWizard` — 6-question interactive I/O class; no AI; produces `ProjectSpec` dataclass
- [x] `PrdValidator` — 4-rule validation (description length, file-path AC, no credentials, valid depends_on); shared by `PlanChecker` and `PrdGenerator`
- [x] `ralph add` command — `PrdGenerator` class; converts free-text spec or GitHub issue URL → validated tasks appended to `prd.json`
- [x] `ralph plan` command — `PlanConsensus` class; Planner + Critic loop (max 3 iter); writes `ralph-plan.md`
- [x] `pipx`-installable package — `pipx install ralphzilla` → `ralph` available globally
- [x] README — quickstart, `prd.json` schema reference, CLI flag reference, worked example
- [x] Schema validation on `prd.json` load — clear error messages for malformed files
- [x] `ralph status` command — shows pending tasks, current branch state, last sprint result
- [x] Conflict pre-detection — warn at sprint start if two pending tasks share files in their `files` field

### Success criteria

- `pipx install ralphzilla && ralph init` produces a working project scaffold
- A new user can run their first sprint without reading the rewrite plan
- `ralph --help` is self-explanatory

---

## Milestone 4 — Scrum Master Layer

**Goal**: Ralph runs unattended across multi-sprint projects; recoverable failures are handled without human intervention.

Implement the `ScrumMaster` layer described in the rewrite plan above `ralph.py`.

### Deliverables

- [ ] `LoopSupervisor` — runs `ralph.py` as subprocess, monitors exit + log
- [ ] `BlockerAnalyser` — classifies exit cause into `BlockerKind` enum
- [ ] `UnblockStrategy` — per-blocker fix strategies (merge conflict, CI fatal, PRD guard, reviewer unavailable)
- [ ] `BacklogManager` — creates FIX tickets from APPROVED-with-comments reviews; never touches in-flight tasks
- [ ] `EscalationManager` — circuit breakers; escalates to human when stuck
- [ ] `ScrumMaster._post_sprint_cleanup()` — deletes stale ralph branches after sprint
- [ ] `ralph scrum` command (or standalone `scrum.py`) — runs the Scrum Master loop

### Success criteria

- Scrum Master runs a 10-task sprint to completion with zero human intervention
- Handles reviewer timeout → auto-downgrade to `--skip-review` without dying
- Handles CI failure → creates FIX task → re-runs → merges fix → resumes original sprint
- Circuit breakers prevent infinite loops (same blocker 3× in a row → escalate)
- Branch cleanup runs automatically post-sprint

---

## Milestone 5 — Parallelism & Scale

**Goal**: Independent tasks run in parallel; large backlogs complete significantly faster.

### Deliverables

- [ ] Dependency graph — build DAG from `depends_on` fields; topological sort
- [ ] Wave executor — group tasks with no shared dependencies into waves; run each wave in parallel via `multiprocessing` or `asyncio`
- [ ] Branch isolation — each parallel task gets its own worktree (`git worktree add`)
- [ ] Conflict detection — pre-wave check: two tasks in the same wave must not share files in `files` field
- [ ] Workstream namespacing — `--workstream NAME` flag; scopes `prd.json` task selection and branch naming
- [ ] Wave summary — after each wave, report which tasks succeeded/failed before starting the next

### Success criteria

- 4 independent tasks run in parallel and all merge cleanly
- Conflicting tasks are correctly serialised (not parallelised)
- `--workstream` correctly scopes execution to a named subset of tasks

---

## Milestone 6 — Ecosystem & Maturity

**Goal**: Ralphzilla is a general-purpose tool, not a Playchitect-specific one. Multi-runtime, verified delivery, experimental paths.

### Deliverables

- [ ] Multi-runtime support — extend `AIRunner` to support Aider, Cursor (via CLI), Cline, Codex
- [ ] `ralph verify` command — post-sprint acceptance criteria check: sends task ACs + code to AI, asks whether they are satisfied
- [ ] Spike mode — `ralph spike TASK_ID` runs a task on a throwaway branch, reports what it produced, does not open a PR
- [ ] Prompt injection detection — sanitise AI output before it is used as input to subsequent prompts
- [ ] Architectural decision conflict detection — warn when a new task contradicts a completed task's stated design decision
- [ ] `ralph doctor` — validates environment (git, gh, AI CLIs, SSH remote, Python version) with actionable fix suggestions
- [ ] TUI presentation layer — maybe integrate [Ralphy](https://github.com/thenomadcode/ralphy) (Go / Bubble Tea) as the real-time dashboard; it watches `prd.json` and streams `opencode` output — would need adapter to consume ralphzilla's log stream and state instead

### Success criteria

- At least 3 AI runtimes beyond the original 3 are supported and tested
- `ralph verify` correctly identifies at least one case where ACs are not met
- `ralph doctor` catches and explains every common setup mistake

---

## Milestone order rationale

```
M1 Core Loop        ← nothing works without this
M2 Quality          ← confidence to run unattended
M3 DX / Distribution ← usable by anyone, not just James
M4 Scrum Master     ← autonomous multi-sprint operation
M5 Parallelism      ← throughput at scale
M6 Ecosystem        ← breadth and maturity
```

M3 (DX) is deliberately before M4 (Scrum Master) because without a clean init story, the Scrum Master is hard to test across projects. M5 and M6 are independent and could be partially parallelised once M4 is stable.

---

## GSD parity tracker

| GSD capability | Ralphzilla milestone |
|---|---|
| Sequential task execution | M1 |
| Plan validation | M1 (structural) + M2 (AI) |
| Failure recovery | M1 (CI/review) + M4 (Scrum Master) |
| Installation / `init` | M3 |
| Spec-driven planning (Discuss/Plan phase) | M3 (`ralph plan` + `ralph add`) |
| Documentation | M3 |
| Tests | M2 |
| Sprint supervision | M4 |
| Wave / parallel execution | M5 |
| Multi-runtime support | M6 |
| Verify phase | M6 |
| Spike mode | M6 |
| Prompt injection detection | M6 |
