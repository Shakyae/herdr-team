# Changelog

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
