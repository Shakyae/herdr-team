# Worktree policy

Use `--worktree=off|auto|on` at `init` (or configure in YAML). No automatic commit/push/merge or worktree deletion is performed.

- `off`: use the project `--cwd` for all Workers.
- `auto`: for development templates, create a worktree only when `--repo PATH` and `--task slug` are explicitly supplied; otherwise use `--cwd`. For research/audit/guided-execution, do not create a worktree.
- `on`: require both `--repo` and `--task`, then run `git worktree add -b htm/<task> <managed-path> HEAD` using a subprocess argument list (no shell). Branch name and path are constrained. Existing target branch/path fails closed, not overwritten.

**v4.2 additional check:** `--repo` must be the exact Git repository root. Before creating a new Worktree, the runner checks `git status --porcelain --untracked-files=normal` and **refuses dirty repositories or untracked files**. Commit/stash/relocate content explicitly rather than assuming the Worker sees local modifications. The preflight is read-only; no automatic stash, commit, or clean takes place.

Managed path defaults to `~/.local/share/herdr-team/worktrees/<repo-hash>/<task>`. Override the parent with `HTM_WORKTREE_DIR` during tests. Worker terminals launch with `--cwd` set to the worktree; Lead stays in the main directory. This creates *one shared task Worktree*, not one Worktree per Worker: therefore avoid simultaneous mutating tasks against the same files and allow read-only Reviewer/Tester stages after Developer finishes.

Git Worktree only contains tracked committed state, not private raw data, ignored files, uncommitted local changes or live database content. Never infer that a worktree contains the full production environment; require separate isolated fixtures for tests. For projects with several independent repositories, select the actual Git root, not their shared parent directory.

A partially failed init can leave a worktree or Pane. The runner intentionally keeps it for inspection rather than deleting it. Remove only after confirming no uncommitted work and user authorization. Reuse and merge are left to explicit human-approved Git actions.

The state record now contains a `resources` history and `pending_operation` marker to help inspect partially completed operations. Because a process can crash after an external command succeeds but before the local journal is saved, this is a **recovery aid, not transactional rollback**. Do not blindly retry init on a partial Team.
