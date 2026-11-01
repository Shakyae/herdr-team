# Changelog

## v4.2.0-beta.2 (2026-11-01) — Real Herdr end-to-end acceptance

This pre-release records the first real Herdr end-to-end acceptance runs. It remains a pre-release: Hermes, OS sandbox isolation and exactly-once delivery are still unverified, and it is not suitable for unattended production use.

### Real Herdr end-to-end runs

Two independent Runs were executed in an isolated workspace `wF` with a real Herdr instance and real Claude Code Workers; both finished.

**P1 (Run `99f5b2f7d020`, finished) — automatic remediation loop**

- Real byte injection: `beta: 2` → `beta: 3`; OUT.md sha `c42d28f9…` → `e00c1be7…`.
- A real Reviewer returned `needs-work` with a self-contained diff.
- The driver issued `gate revise` automatically; the event cluster (`driver_started` → `gate` → `driver_stopped`) completed in 1.21 ms, with matching `drive-*.stdout.steps` records.
- A `remediation=true` dispatch was sent to the developer automatically.
- After the fix the Run returned to the oracle; the second review dispatch under the reviewer role re-verified the fix and returned `pass`. (The ledger records the reviewer role's agent and session once; the two dispatch records do not each carry their own agent field, so "the same agent instance" is a reasonable inference, not proven per-dispatch.)
- Lead accept + `run finish` → finished.

**P2 (Run `3db2cf1cc33d`, finished) — crash recovery**

- The kill point was hit: SIGKILL delivered to pid 52866, confirmed by `returncode=-9`.
- Without `--resume` the driver was refused with `resume_required`, and the ledger was byte-identical before and after (sha `e32fd557…`).
- The first `--resume` step was a `collect` on the **same** dispatch; no new dispatch was created.
- Final accept + finish → finished.

### Boundaries

- SIGKILL terminated the driver only, **not** the Claude worker. The artifact mtime is 80.5 seconds after the kill point: the worker kept working after the driver died and wrote its report. What was verified is that recovery collects the real artifact, **not** that work completed before the crash was preserved.
- Not verified: external exactly-once delivery, OS sandbox isolation, atomicity at arbitrary crash points.
- The first `--resume` collect found the worker still `working`, returned `uncertain` and exited `stuck_unsettled`. This is fail-closed design, not a failure.

Nothing in this section changes functional code or runtime behaviour; it only records real end-to-end acceptance conclusions.

## v4.2.0-beta.1 (2026-10-30) — First public Beta

This pre-release packages the existing v4.2 reliability work. It is not Stable: real Herdr/Hermes end-to-end acceptance has not been completed.

- Publishes a single root-level `herdr-team` Skill for Skills CLI discovery and Claude Code / Codex installation.
- Adds MIT License (2026 Shakyae), documented online/source installation, configuration, contribution and safety guidance.
- Declares Python 3.9+ and optional PyYAML support in `pyproject.toml`, with an isolated uv environment and lockfile. Default JSON-compatible YAML still requires only the standard library.
- Includes release hygiene exclusions and offline installation of license and dependency metadata; no scheduler business logic changes.

### v4.2 reliability changes

- Prevents a Team from being silently reused under different resolved governance/workflow/worktree settings. Refuses automatic reuse of v4.1 state.
- Enforces exclusive reservation of Worker panes for unfinished Runs under the state directory's process lock; paused/blocked Runs remain reserved.
- Replaces terminal-snapshot acceptance with per-dispatch JSON artifacts bound to Run ID / Dispatch ID / role / stage / stage attempt; missing or mismatched artifacts and unknown Herdr states stay uncertain.
- Requires all dispatches to settle before pass/revise, validates Worker verdict and bound artifact digest at gate, requires explicit passing test records for test stages, and prevents later failing reviews being overridden by an earlier positive review.
- Adds read-only `run list` / `run recover`, explicit guarded `run cancel`, and init resource/pending-operation journal; no blind resend or automatic deletion.
- Tightens agent reuse (must verify pane and kind), Hermes `launch_args` allowlist, and conflict detection for Hermes profiles held by another managed Team. External sessions are not discoverable.
- Refuses creating a HEAD-only Git Worktree if source repo is dirty/untracked; requires exact Git repo root.
- Converts invalid YAML syntax into a friendly CLI error.
- Removes insecure same-user TTY owner approval and unverified round extension from executable workflows (fail-closed; not a substitute for future external auth).
- Adds offline transactional upgrade with old-release backup and explicit rollback; preserves user config and existing links.
- Adds adversarial regressions for v4.1 P0/P1 failures; existing 18 tests updated for the bound artifact protocol.

**Not fixed / intentionally out of scope:** independent model-driven background supervisor, real OS-enforced sandbox, independent Owner approval service, true verification of every self-reported test, genuine Herdr/Hermes integration, DSH/Compound engineering memory. `escalate` and `report` remain Lead guidance, not background notification hooks. Do not claim production readiness.

## v4.1

Initial executable Python-based Herdr Team Manager, seven templates, saved Run ledger, workflow gates, Task/Profile/Worktree planning, local Skill package. Identified as unsuitable for autonomous production use in the independent audit.
