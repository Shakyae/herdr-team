#!/usr/bin/env python3
"""Herdr Team Manager — local supervised agent orchestration primitives.

No LLM calls. The invoking Lead agent supplies decisions at workflow gates.
No shell=True. All unsafe/reconciliation states are preserved for inspection.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import datetime as dt
import fcntl
import hashlib
import json
import os
import getpass
import stat
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import uuid

try:
    import yaml
except ImportError:
    yaml = None

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config" / "team.yaml"
CONFIG = Path(os.environ.get("HTM_CONFIG", str(Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "herdr-team" / "team.yaml"))).expanduser()
STATE = Path(os.environ.get("HTM_STATE_DIR", "~/.local/state/herdr-team")).expanduser()
# Herdr public ids use a 32-char Crockford-style alphabet (src/workspace.rs PUBLIC_ID_ALPHABET):
# 123456789ABCDEFGHJKMNPQRSTVWXYZ0 — no I/L/O/U, uppercase only.
ID_ALPHABET = "123456789ABCDEFGHJKMNPQRSTVWXYZ0"
WORKSPACE_ID_RE = re.compile(rf"w[{re.escape(ID_ALPHABET)}]+\Z")
TAB_ID_RE = re.compile(rf"w[{re.escape(ID_ALPHABET)}]+:t[{re.escape(ID_ALPHABET)}]+\Z")
PANE_ID_RE = re.compile(rf"w[{re.escape(ID_ALPHABET)}]+:p[{re.escape(ID_ALPHABET)}]+\Z")
NAME_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")
SLUG_RE = re.compile(r"[a-z][a-z0-9-]{0,39}\Z")
ENUM = {
    "governance.autonomy": {"read-only", "workspace-write", "approved-actions"},
    "governance.escalate": {"critical", "important", "all"},
    "governance.approval": {"boundary", "changes", "every-step"},
    "governance.report": {"final", "milestones", "verbose"},
    "layout.mode": {"side", "tab"},
    "layout.arrange": {"rows", "cols", "grid", "auto"},
    "execution.worktree": {"on", "off", "auto"},
}
INTS = {"orchestration.max_rounds": (1, 30), "orchestration.max_retries": (0, 5),
        "orchestration.max_parallel": (1, 8), "orchestration.task_timeout_seconds": (15, 300)}
ALLOWED_CATEGORIES = {"development", "research", "audit", "guided-execution", "custom"}
KINDS = {"claude", "codex", "hermes", "opencode", "gemini", "pi", "qwen", "kimi", "copilot"}


class Error(Exception):
    pass


def assert_keys(obj, allowed, where):
    extra = set(obj) - set(allowed)
    if extra:
        raise Error(f"{where}: unknown field(s): {', '.join(sorted(map(str, extra)))}")


def mapping(v, where):
    if not isinstance(v, dict):
        raise Error(f"{where} must be a mapping")
    return v


def read_cfg(path=None):
    path = Path(path or (CONFIG if CONFIG.is_file() else DEFAULT_CONFIG))
    try:
        txt = path.read_text(encoding="utf-8")
        data = yaml.safe_load(txt) if yaml else json.loads(txt)
    except (OSError, ValueError, TypeError, json.JSONDecodeError, *([yaml.YAMLError] if yaml else [])) as e:
        raise Error(f"Unable to load {path}: {e}" ) from e
    return validate(mapping(data, "config")), path


def deepmerge(base, override):
    ret = copy.deepcopy(base)
    for k, v in override.items():
        if k in ret and isinstance(ret[k], dict) and isinstance(v, dict):
            ret[k] = deepmerge(ret[k], v)
        else:
            ret[k] = copy.deepcopy(v)
    return ret


def select_path(d, key):
    v = d
    for p in key.split("."):
        if not isinstance(v, dict) or p not in v:
            raise Error(f"Missing required setting: {key}")
        v = v[p]
    return v


def check_settings(s, prefix):
    mapping(s, prefix)
    assert_keys(s, ["orchestration", "governance", "layout", "execution"], prefix)
    allowed = {"orchestration": {"max_rounds", "max_retries", "max_parallel", "task_timeout_seconds"},
               "governance": {"autonomy", "escalate", "approval", "report"},
               "layout": {"mode", "arrange"}, "execution": {"worktree"}}
    for k, v in s.items():
        mapping(v, f"{prefix}.{k}")
        assert_keys(v, allowed[k], f"{prefix}.{k}")
    for key, options in ENUM.items():
        try:
            val = select_path(s, key)
        except Error:
            if prefix != "defaults":
                continue
            raise
        if not isinstance(val, str) or val not in options:
            raise Error(f"{prefix}.{key} must be one of: {', '.join(sorted(options))}")
    for key, (lo, hi) in INTS.items():
        try:
            val = select_path(s, key)
        except Error:
            if prefix != "defaults":
                continue
            raise
        if type(val) is not int or not lo <= val <= hi:
            raise Error(f"{prefix}.{key} must be integer {lo}..{hi}")


def validate(data):
    assert_keys(data, ["version", "defaults", "agents", "stages", "templates"], "config")
    if type(data.get("version")) is not int or data["version"] != 2:
        raise Error("version must equal 2")
    check_settings(mapping(data.get("defaults"), "defaults"), "defaults")
    agents = mapping(data.get("agents"), "agents")
    for k, v in agents.items():
        if not isinstance(k, str) or not NAME_RE.fullmatch(k):
            raise Error(f"invalid agent registry key: {k}")
        mapping(v, f"agents.{k}")
        assert_keys(v, ["kind", "profile", "launch_args"], f"agents.{k}")
        if v.get("kind") not in KINDS:
            raise Error(f"agents.{k}: unsupported kind")
        if v["kind"] == "hermes":
            profile = v.get("profile")
            if not isinstance(profile, str) or not NAME_RE.fullmatch(profile):
                raise Error(f"agents.{k}: Hermes needs explicit valid profile")
        elif "profile" in v:
            raise Error(f"agents.{k}: profile only supported for Hermes")
        args = v.get("launch_args", [])
        if not isinstance(args, list) or any(not isinstance(x, str) or len(x) > 256 for x in args):
            raise Error(f"agents.{k}.launch_args must be a list of strings")
        # Hermes argument parsing has aliases and = forms. Rather than maintain
        # an incomplete denylist, allow only the documented chat subcommand.
        if v["kind"] == "hermes" and args not in ([], ["chat"]):
            raise Error(f"agents.{k}: Hermes launch_args must be [] or ['chat']; profile is injected automatically")
    stages = mapping(data.get("stages"), "stages")
    for k, stage in stages.items():
        if not isinstance(k, str) or not NAME_RE.fullmatch(k):
            raise Error(f"Invalid stage name: {k}")
        mapping(stage, f"stages.{k}")
        assert_keys(stage, ["actor", "gate"], f"stages.{k}")
        if not isinstance(stage.get("actor"), str) or not stage["actor"]:
            raise Error(f"stages.{k}.actor missing")
        if not isinstance(stage.get("gate"), str) or not stage["gate"]:
            raise Error(f"stages.{k}.gate missing")
    templates = mapping(data.get("templates"), "templates")
    if not templates:
        raise Error("At least one template is required")
    for k, v in templates.items():
        if not isinstance(k, str) or not SLUG_RE.fullmatch(k):
            raise Error(f"Invalid template name: {k}")
        mapping(v, f"templates.{k}")
        assert_keys(v, ["extends", "category", "description", "roles", "workflow", "orchestration", "governance", "layout", "execution"], f"templates.{k}")
        if "extends" in v and v["extends"] not in templates:
            raise Error(f"templates.{k} extends unknown template")
        if "roles" in v:
            mapping(v["roles"], f"templates.{k}.roles")
            for role, agent_name in v["roles"].items():
                if not isinstance(role, str) or not NAME_RE.fullmatch(role) or not isinstance(agent_name, str) or agent_name not in agents:
                    raise Error(f"templates.{k} invalid role/agent: {role} -> {agent_name}")
        if "workflow" in v:
            if not isinstance(v["workflow"], list) or not v["workflow"] or any(x not in stages for x in v["workflow"]):
                raise Error(f"templates.{k}.workflow must list configured stages")
        check_settings({x: v[x] for x in ("orchestration", "governance", "layout", "execution") if x in v}, f"templates.{k}")
    # Detect inherit cycles and stage actors unresolved after merging.
    for k in templates:
        resolved = template(data, k)
        roles = mapping(resolved.get("roles"), f"templates.{k}.roles")
        if not (1 <= len(roles) <= 8):
            raise Error(f"templates.{k} requires 1..8 roles")
        workflow = resolved.get("workflow")
        if not isinstance(workflow, list) or not workflow:
            raise Error(f"templates.{k}.workflow missing")
        for sid in workflow:
            actor = stages[sid]["actor"]
            if actor != "lead" and actor not in roles:
                raise Error(f"templates.{k}: stage {sid} requires missing role {actor}")
        category = resolved.get("category")
        if category not in ALLOWED_CATEGORIES:
            raise Error(f"templates.{k}: invalid category {category}")
    return data


def template(cfg, name, seen=None):
    all_templates = cfg["templates"]
    if name not in all_templates:
        raise Error(f"Unknown template {name}")
    seen = seen or set()
    if name in seen:
        raise Error(f"Template inheritance cycle at {name}")
    item = copy.deepcopy(all_templates[name]); parent = item.pop("extends", None)
    return deepmerge(template(cfg, parent, seen | {name}), item) if parent else item


def set_override(settings, spec):
    if "=" not in spec:
        raise Error(f"Override must be path=value: {spec}")
    key, raw = spec.split("=", 1)
    keys = key.split(".")
    if len(keys) != 2 or keys[0] not in ("orchestration", "governance", "layout", "execution"):
        raise Error(f"Unknown override path: {key}")
    group = settings[keys[0]]
    if keys[1] not in group:
        raise Error(f"Unknown override path: {key}")
    parsed = json.loads(raw) if raw.lstrip("-+").isdigit() else raw
    group[keys[1]] = parsed


def resolve(cfg, name, options=None):
    item = template(cfg, name)
    config = deepmerge(cfg["defaults"], {k: item[k] for k in ("orchestration", "governance", "layout", "execution") if k in item})
    for opt in options or []:
        set_override(config, opt)
    check_settings(config, "defaults")
    return item, config


def context(require=True):
    if require and os.environ.get("HERDR_ENV") != "1":
        raise Error("Must run inside Herdr: HERDR_ENV=1")
    ws = os.environ.get("HERDR_WORKSPACE_ID", "")
    pane = os.environ.get("HERDR_PANE_ID", "")
    tab = os.environ.get("HERDR_TAB_ID", "")
    if require and not (ws and pane and tab):
        raise Error("Missing Herdr pane/workspace/tab environment")
    if require:
        for label, value, pattern in (("workspace", ws, WORKSPACE_ID_RE),
                                      ("tab", tab, TAB_ID_RE), ("pane", pane, PANE_ID_RE)):
            if not pattern.fullmatch(value):
                raise Error(f"Invalid Herdr {label} id: {value!r}")
        if tab.split(":")[0] != ws or pane.split(":")[0] != ws:
            raise Error(f"Tab/Pane does not belong to workspace {ws}: tab={tab!r} pane={pane!r}")
    return {"workspace": ws, "lead_pane": pane, "lead_tab": tab}


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def state_file(ctx):
    key = hashlib.sha256(f"{ctx['workspace']}|{ctx['lead_pane']}".encode()).hexdigest()[:16]
    return STATE / "teams" / (key + ".json")


def run_file(runid):
    if not re.fullmatch(r"[a-f0-9]{12}", runid):
        raise Error("Invalid Run ID")
    return STATE / "runs" / (runid + ".json")


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix=".htm-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def load(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise Error(f"Cannot read state {path}: {e}") from e


@contextlib.contextmanager
def locked():
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(STATE / ".lock", os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def process(argv, timeout=40, raw=False):
    try:
        r = subprocess.run(argv, text=True, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise Error(f"Command unavailable/timed out ({argv[:3]}): {e}") from e
    if r.returncode:
        raise Error(f"Command failed ({r.returncode}): {' '.join(argv[:3])}: {(r.stderr or r.stdout)[-900:]}")
    if raw:
        return r.stdout
    try:
        out = json.loads(r.stdout)
    except json.JSONDecodeError as e:
        raise Error(f"Expected Herdr JSON from {' '.join(argv[:3])}: {r.stdout[-600:]}") from e
    if not isinstance(out, dict) or out.get("status") in ("error", "failed") or out.get("error"):
        raise Error(f"Herdr error: {str(out)[:900]}")
    return out.get("result", out)


def herdr(*args, timeout=40):
    return process(["herdr", *args], timeout=timeout)


def pane_shell_ready(pane_id):
    try:
        result = herdr("pane", "process-info", "--pane", pane_id)
    except Error:
        return False
    info = result.get("process_info") if isinstance(result, dict) else None
    if not isinstance(info, dict):
        return False
    processes = info.get("foreground_processes")
    shell_pid = info.get("shell_pid")
    pgid = info.get("foreground_process_group_id")
    if (not isinstance(processes, list) or type(shell_pid) is not int or
            type(pgid) is not int or pgid != shell_pid):
        return False
    # Herdr 0.9.5 实测：pgid == shell_pid 表示前台属于 shell；前台任务和 agent 的 pgid 不同。
    # 多进程前台也可能属于 shell，不能仅凭进程数量判断。
    for process_info in processes:
        if not isinstance(process_info, dict) or type(process_info.get("pid")) is not int or process_info["pid"] != shell_pid:
            continue
        argv0 = process_info.get("argv0")
        if isinstance(argv0, str):
            token = os.path.basename(argv0).lstrip("-")
            if token in {"zsh", "bash", "sh", "dash", "fish", "ksh", "tcsh"}:
                return True
    return False


def start_agent_when_ready(name, kind, pane_id, workspace, created_panes,
                           launch_args=(), attempts=8, interval=0.5):
    stats = {"shell_polls": 0, "busy_retries": 0, "attempts": 0}
    for attempt in range(1, attempts + 1):
        stats["attempts"] = attempt
        if pane_id not in created_panes:
            raise Error(f"Pane {pane_id} was not created by this init; refusing agent start")
        result = herdr("pane", "get", pane_id)
        pane = result.get("pane") if isinstance(result, dict) else None
        if not isinstance(pane, dict) or pane.get("workspace_id") != workspace:
            raise Error(f"Pane {pane_id} is missing or belongs to a different workspace")
        if pane_shell_ready(pane_id):
            try:
                result = herdr("agent", "start", name, "--kind", kind, "--pane", pane_id,
                               *launch_args, timeout=60)
                return result, stats
            except Error as e:
                if "agent_pane_busy" not in str(e):
                    raise
                stats["busy_retries"] += 1
        else:
            stats["shell_polls"] += 1
        if attempt < attempts:
            time.sleep(interval)
    raise Error(f"Agent pane {pane_id} stayed busy after {attempts} readiness checks; refusing to retry blindly")


def agent_data(obj):
    # Herdr wraps the Agent object as {"agent": {...}} for `agent get`.
    # An Agent object itself also carries the string field "agent" (the kind),
    # so only unwrap when the value is actually a mapping. Idempotent.
    if isinstance(obj, dict) and isinstance(obj.get("agent"), dict):
        return obj["agent"]
    return obj


def agent_kind_of(obj):
    """Agent kind: Herdr 0.9.5 `agent`, falling back to legacy `kind`/`agent_kind`.

    Returns None when no field is present (callers must fail closed).
    """
    obj = agent_data(obj)
    if not isinstance(obj, dict):
        return None
    kind = obj.get("agent")
    if isinstance(kind, str) and kind:
        return kind
    return obj.get("kind") or obj.get("agent_kind")


def agent_status_of(obj):
    """Agent status: Herdr 0.9.5 `agent_status`, falling back to legacy `status`.

    Missing/blank status is reported as "unknown" (callers treat that as
    uncertain, never as success).
    """
    obj = agent_data(obj)
    if not isinstance(obj, dict):
        return "unknown"
    status = obj.get("agent_status")
    if not (isinstance(status, str) and status):
        status = obj.get("status")
    return status if isinstance(status, str) and status else "unknown"


def lookup_agent(name):
    try:
        return agent_data(herdr("agent", "get", name))
    except Error:
        return None


def verify_existing(team_role):
    found = lookup_agent(team_role["agent_name"])
    if not found or not isinstance(found, dict):
        return False
    pane = found.get("pane")
    observed_pane = found.get("pane_id") or (pane.get("pane_id") if isinstance(pane, dict) else None)
    if not observed_pane:
        raise Error(f"Agent {team_role['agent_name']} has no verifiable pane ID; refusing reuse")
    if observed_pane != team_role["pane_id"]:
        raise Error(f"Agent {team_role['agent_name']} is live in unexpected pane {observed_pane}; refusing reuse")
    kind = agent_kind_of(found)
    if not kind:
        raise Error(f"Agent {team_role['agent_name']} has no verifiable kind")
    if kind != team_role["kind"]:
        raise Error(f"Agent {team_role['agent_name']} has unexpected kind {kind}")
    if "agent_session" in team_role:
        expected = team_role["agent_session"].get("value")
        session = found.get("agent_session")
        actual = session.get("value") if isinstance(session, dict) else None
        if actual != expected:
            raise Error(f"Agent {team_role['agent_name']} session identity changed (expected {expected}, got {actual}); refusing reuse")
    if found.get("name") and found["name"] != team_role["agent_name"]:
        raise Error("Agent name mismatch")
    if found.get("cwd") and str(Path(found["cwd"]).resolve()) != team_role.get("cwd"):
        raise Error("Agent CWD mismatch; cannot reuse in a different checkout")
    if team_role["kind"] == "hermes" and found.get("profile") and found["profile"] != team_role["profile"]:
        raise Error("Hermes profile mismatch")
    return True


def profile_check(name):
    if not NAME_RE.fullmatch(name):
        raise Error("Invalid Hermes profile name")
    try:
        return process(["hermes", "profile", "show", name], timeout=15, raw=True).strip()
    except Error as e:
        raise Error(f"Hermes profile '{name}' not confirmed by 'hermes profile show': {e}") from e


def layout_ops(count, arrange):
    """Return split operations: (index-of-existing-pane, direction, index-of-new-pane)."""
    if not 1 <= count <= 8:
        raise Error("Team size must be 1..8")
    if arrange == "auto":
        arrange = "rows" if count == 2 else "grid"
    if count == 1:
        return []
    if arrange in ("rows", "cols"):
        direc = "down" if arrange == "rows" else "right"
        return [(i-1, direc, i) for i in range(1, count)]
    # Grid: recursively bisect the region, vertical split for up to two tiles,
    # horizontal split for 3+; the resulting pane indices are in spatial order.
    if count == 2:
        return [(0, "right", 1)]
    if count == 3:
        return [(0, "down", 1), (1, "right", 2)]
    if count == 4:
        return [(0, "down", 2), (0, "right", 1), (2, "right", 3)]
    # 5..8: first 2 x N, then fill successive rows.
    # Index 0 top-left, index 1 top-right, index 2 next row left, etc.
    ops = [(0, "right", 1)]
    for i in range(2, count):
        ops.append((i-2, "down", i))
    return ops


def initial_plan(cfg, name, opts):
    item, settings = resolve(cfg, name, opts.set)
    if opts.layout: settings["layout"]["mode"] = opts.layout
    if opts.arrange: settings["layout"]["arrange"] = opts.arrange
    if opts.worktree: settings["execution"]["worktree"] = opts.worktree
    check_settings(settings, "defaults")
    role_names = list(item["roles"])
    roles = []
    for r in role_names:
        a = cfg["agents"][item["roles"][r]]
        launch = (["-p", a["profile"]] if a["kind"] == "hermes" else []) + a.get("launch_args", [])
        roles.append({"role": r, "registry": item["roles"][r], "kind": a["kind"], "profile": a.get("profile"), "launch_args": launch})
    resolved_arrange = settings["layout"]["arrange"]
    if resolved_arrange == "auto": resolved_arrange = "rows" if len(roles) == 2 else "grid"
    return {"template": name, "category": item["category"], "workflow": item["workflow"], "stages": cfg["stages"],
            "roles": roles, "settings": settings, "layout_steps": layout_ops(len(roles), resolved_arrange),
            "layout_arrange_resolved": resolved_arrange}


def git_root(repo):
    return Path(process(["git", "-C", str(repo), "rev-parse", "--show-toplevel"], raw=True).strip()).resolve()


def worktree_details(plan, args, apply, allow_exists=False):
    requested = plan["settings"]["execution"]["worktree"]
    if requested == "off":
        return None
    if requested == "auto" and (plan["category"] != "development" or not args.task or not args.repo):
        return None
    if not args.repo or not args.task:
        raise Error("Worktree requires both --repo PATH and --task lowercase-slug; use --worktree=off to skip")
    if not SLUG_RE.fullmatch(args.task):
        raise Error("Invalid --task (must be a lowercase slug)")
    repo = Path(args.repo).expanduser().resolve()
    root = git_root(repo)
    if repo != root:
        raise Error("--repo must point to an exact Git root; cross-subdirectory worktree assumptions are unsafe")
    if not allow_exists:
        dirty = process(["git", "-C", str(root), "status", "--porcelain", "--untracked-files=normal"], raw=True)
        if dirty.strip():
            raise Error("Source Git repo is dirty or contains untracked files. Commit/stash explicitly before building a HEAD-only worktree")
    digest = hashlib.sha256(str(root).encode()).hexdigest()[:10]
    branch = f"htm/{args.task}"
    wt = Path(os.environ.get("HTM_WORKTREE_DIR", "~/.local/share/herdr-team/worktrees")).expanduser() / digest / args.task
    if wt.exists() and not allow_exists:
        raise Error(f"Worktree path already exists; do not overwrite: {wt}")
    detail = {"repo": str(root), "path": str(wt), "branch": branch}
    if apply:
        wt.parent.mkdir(parents=True, exist_ok=True)
        process(["git", "-C", str(root), "worktree", "add", "-b", branch, str(wt), "HEAD"], timeout=60, raw=True)
    return detail


def init(cfg, args):
    plan = initial_plan(cfg, args.template, args)
    cwd = Path(args.cwd or os.getcwd()).expanduser().resolve()
    if not cwd.is_dir(): raise Error(f"CWD does not exist: {cwd}")
    if args.dry_run:
        wt = worktree_details(plan, args, False)
        return {"dry_run": True, "no_mutations": True, "cwd": str(cwd), "worktree": wt,
                "plan": plan, "requires_herdr": True}
    ctx = context()
    for role in plan["roles"]:
        if role["kind"] == "hermes":
            profile_check(role["profile"])
    path = state_file(ctx)
    with locked():
        requested_wt = worktree_details(plan, args, False, allow_exists=path.exists())
        if path.exists():
            old = load(path)
            if old.get("version") != 3:
                raise Error("Legacy v4.1 team state cannot be automatically adopted; inspect/close existing sessions and migrate manually")
            if (old.get("template") != args.template or
                    json.dumps(old.get("plan"), sort_keys=True) != json.dumps(plan, sort_keys=True) or
                    old.get("cwd") != str(cwd) or old.get("worktree") != requested_wt):
                raise Error("Existing team has a different resolved config, worktree, or cwd; refusing silent reuse. Explicit migration required")
            if old.get("status") == "partial":
                raise Error("Previous init is partial. Inspect team state and orphan panes before retrying; no automatic duplicate creation.")
            for role in old["roles"]:
                if not verify_existing(role):
                    raise Error(f"Registered Agent {role['agent_name']} not live. Do not silently replace; investigate or create a new team.")
            return {"reused": True, "team": old}
        # Hermes can share mutable profile/session state across terminals.
        # Detect other HTM-managed teams using the same profile. External
        # Hermes processes remain invisible; owner must inspect them manually.
        new_profiles = {r["profile"] for r in plan["roles"] if r["kind"] == "hermes"}
        for other_path in (STATE / "teams").glob("*.json"):
            other = load(other_path)
            if other.get("status") not in ("ready", "partial"):
                continue
            other_profiles = {r.get("profile") for r in other.get("roles", []) if r.get("kind") == "hermes"}
            if new_profiles & other_profiles:
                raise Error(f"Hermes profile already reserved by another managed team {other_path}; use dedicated test profiles")
        wt = requested_wt
        work_cwd = str(wt["path"]) if wt else str(cwd)
        digest = hashlib.sha256((ctx["workspace"]+":"+ctx["lead_pane"]).encode()).hexdigest()[:6]
        roles = []
        team = {"version": 3, "template": args.template, "context": ctx, "created_at": now(),
                "status": "partial", "roles": roles, "plan": plan, "cwd": str(cwd), "worktree": wt,
                "resources": [], "pending_operation": None}
        save(path, team)
        def operate(operation, detail, func):
            team["pending_operation"] = {"operation": operation, "detail": detail, "at": now()}
            save(path, team)
            result = func()
            team["resources"].append({"operation": operation, "detail": detail, "result": result, "at": now()})
            team["pending_operation"] = None
            save(path, team)
            return result
        try:
            if wt:
                operate("git_worktree_add", wt, lambda: worktree_details(plan, args, True))
            if plan["settings"]["layout"]["mode"] == "tab":
                r = operate("tab_create", {"cwd": work_cwd},
                            lambda: herdr("tab", "create", "--workspace", ctx["workspace"], "--cwd", work_cwd,
                                          "--label", "htm-"+args.template, "--no-focus"))
                root_pane = r["root_pane"]["pane_id"]
            else:
                r = operate("pane_split", {"source": ctx["lead_pane"], "cwd": work_cwd},
                            lambda: herdr("pane", "split", ctx["lead_pane"], "--direction", "right", "--cwd", work_cwd, "--no-focus"))
                root_pane = r["pane"]["pane_id"]
            pane_ids = [root_pane] + [None]*(len(plan["roles"])-1)
            for src, direction, target in plan["layout_steps"]:
                r = operate("pane_split", {"source": pane_ids[src], "direction": direction, "cwd": work_cwd},
                            lambda: herdr("pane", "split", pane_ids[src], "--direction", direction, "--cwd", work_cwd, "--no-focus"))
                pane_ids[target] = r["pane"]["pane_id"]
            for idx, role in enumerate(plan["roles"]):
                name = f"ht{digest}-{idx+1}-{role['role'][:9]}"
                name = name[:32]
                info = {**role, "pane_id": pane_ids[idx], "agent_name": name, "cwd": work_cwd}
                roles.append(info)
                save(path, team)
                detail = {"name": name, "pane": pane_ids[idx], "kind": role["kind"]}
                def start():
                    launch_args = ["--", *role["launch_args"]] if role["launch_args"] else []
                    result, stats = start_agent_when_ready(
                        name, role["kind"], pane_ids[idx], ctx["workspace"], pane_ids, launch_args)
                    detail["readiness_attempts"] = stats["attempts"]
                    detail["readiness_shell_polls"] = stats["shell_polls"]
                    detail["readiness_busy_retries"] = stats["busy_retries"]
                    return result
                operate("agent_start", detail, start)
            team["status"] = "ready"
            save(path, team)
        except (Error, KeyError, TypeError) as e:
            team["error"] = str(e)
            save(path, team)
            raise Error(f"Initialization partially applied; inspect state {path} and Herdr panes: {e}") from e
        return {"reused": False, "team": team}


def adopt(cfg, args):
    ctx = context()
    path = state_file(ctx)
    def check_fresh():
        if path.exists() or path.is_symlink():
            raise Error(f"A team is already registered for this Lead pane ({path}); adopt only registers a fresh team. Use a different Lead pane.")
    check_fresh()
    opts = argparse.Namespace(set=args.set, layout=None, arrange=None, worktree=None)
    plan = initial_plan(cfg, args.template, opts)
    assignments = {}
    expected_roles = {r["role"] for r in plan["roles"]}
    for spec in args.role:
        if "=" not in spec:
            raise Error(f"Role must be role=agent-name: {spec}")
        role, name = spec.split("=", 1)
        if role not in expected_roles:
            raise Error(f"Unknown role {role}")
        if role in assignments or not name:
            raise Error(f"Duplicate role or empty agent name: {role}")
        if name in assignments.values():
            raise Error(f"Agent {name} assigned to multiple roles")
        assignments[role] = name
    if set(assignments) != expected_roles:
        raise Error(f"All template roles are required; missing: {', '.join(sorted(expected_roles - set(assignments)))}")

    def check_reservations(roles):
        adopted_panes = {x["pane_id"] for x in roles}
        for run_path in (STATE / "runs").glob("*.json"):
            run = load(run_path)
            if run.get("status") in ("finished", "cancelled"):
                continue
            # pane_id carries its workspace prefix and is globally unique; do not gate
            # this check on run.context.workspace, which can be stale or inconsistent.
            if adopted_panes & {r.get("pane_id") for r in run.get("roles", []) if r.get("pane_id")}:
                raise Error(f"Worker reserved by unfinished Run {run.get('id')}")
        for other_path in (STATE / "teams").glob("*.json"):
            if other_path == path:
                continue
            other = load(other_path)
            for existing in other.get("roles", []):
                if any(existing.get("pane_id") == r["pane_id"] or existing.get("agent_name") == r["agent_name"] for r in roles):
                    raise Error(f"Worker already registered by team {other_path}")
                if other.get("status") in ("ready", "partial") and any(
                        r["kind"] == "hermes" and existing.get("kind") == "hermes" and
                        existing.get("profile") == r["profile"] for r in roles):
                    raise Error(f"Hermes profile already reserved by another managed team {other_path}; use dedicated test profiles")

    roles = []
    requested_cwd = str(Path(args.cwd).expanduser().resolve()) if args.cwd else None
    for role in plan["roles"]:
        name = assignments[role["role"]]
        found = lookup_agent(name)
        if not isinstance(found, dict) or not found:
            raise Error(f"Agent {name} is not live; cannot adopt")
        kind = agent_kind_of(found)
        if not kind:
            raise Error(f"Agent {name} has no verifiable kind")
        if kind != role["kind"]:
            raise Error(f"Agent {name} has unexpected kind {kind}")
        status = agent_status_of(found)
        if status not in ("idle", "done"):
            raise Error(f"Agent {name} is {status}; adopt requires idle or done")
        pane = found.get("pane")
        pane_id = found.get("pane_id") or (pane.get("pane_id") if isinstance(pane, dict) else None)
        if not isinstance(pane_id, str) or not PANE_ID_RE.fullmatch(pane_id):
            raise Error(f"Agent {name} has no valid pane ID")
        if pane_id == ctx["lead_pane"]:
            raise Error(f"Agent {name} occupies the Lead pane {pane_id}; adopt requires a separate worker pane")
        if pane_id.split(":")[0] != ctx["workspace"]:
            raise Error(f"Agent {name} pane belongs to a different workspace")
        if any(r["pane_id"] == pane_id for r in roles):
            raise Error(f"Pane {pane_id} assigned to multiple roles")
        result = herdr("pane", "get", pane_id)
        actual_pane = result.get("pane") if isinstance(result, dict) else None
        if not isinstance(actual_pane, dict) or actual_pane.get("workspace_id") != ctx["workspace"]:
            raise Error(f"Pane {pane_id} is missing or belongs to a different workspace")
        raw_cwd = found.get("cwd")
        if not isinstance(raw_cwd, str) or not raw_cwd or not Path(raw_cwd).is_dir():
            raise Error(f"Agent {name} has no valid cwd directory")
        cwd = str(Path(raw_cwd).resolve())
        if (requested_cwd and cwd != requested_cwd) or (roles and cwd != roles[0]["cwd"]):
            raise Error(f"Agent {name} cwd mismatch")
        session = found.get("agent_session")
        if (not isinstance(session, dict) or
                any(not isinstance(session.get(k), str) or not session[k].strip() for k in ("kind", "source", "value")) or
                len(session["value"]) < 8):
            raise Error(f"Agent {name} has no stable session identity; cannot adopt")
        info = {**role, "pane_id": pane_id, "agent_name": name, "cwd": cwd, "adopted": True,
                "agent_session": {k: session[k] for k in ("kind", "source", "value")}}
        check_reservations([info])
        if kind == "hermes":
            if found.get("profile") and found["profile"] != role["profile"]:
                raise Error("Hermes profile mismatch")
            profile_check(role["profile"])
        roles.append(info)
    team = {"version": 3, "template": args.template, "context": ctx, "created_at": now(),
            "status": "ready", "roles": roles, "plan": plan, "cwd": roles[0]["cwd"],
            "worktree": None, "resources": [], "pending_operation": None, "adopted": True}
    if args.dry_run:
        return {"dry_run": True, "no_mutations": True, "team": team}
    with locked():
        check_fresh()
        check_reservations(roles)
        for role in roles:
            found = lookup_agent(role["agent_name"])
            pane = found.get("pane") if isinstance(found, dict) else None
            pane_id = (found.get("pane_id") or (pane.get("pane_id") if isinstance(pane, dict) else None)) if isinstance(found, dict) else None
            session = found.get("agent_session") if isinstance(found, dict) else None
            if (pane_id != role["pane_id"] or agent_kind_of(found) != role["kind"] or
                    not isinstance(session, dict) or session.get("value") != role["agent_session"]["value"]):
                raise Error(f"Agent {role['agent_name']} changed during adopt; refusing registration")
        save(path, team)
    return {"adopted": True, "team": team}


def current_team():
    ctx = context()
    path = state_file(ctx)
    if not path.exists():
        raise Error("No registered team for this Lead pane; use init first")
    return load(path)


def require_run(runid):
    data = load(run_file(runid))
    if data.get("version") != 3:
        raise Error("Legacy Run state is not compatible with v4.2 result protocol; do not resend tasks automatically")
    ctx = context()
    if data.get("context") != ctx:
        raise Error("Run belongs to a different Lead pane/session")
    return data


def run_start(cfg, args):
    with locked():
        team = current_team()
        if team["status"] != "ready": raise Error("Team is not ready")
        for role in team["roles"]:
            if not verify_existing(role): raise Error(f"Agent {role['agent_name']} not available")
        names = {(team["context"]["workspace"], r["pane_id"]) for r in team["roles"]}
        for existing_path in (STATE / "runs").glob("*.json"):
            other = load(existing_path)
            if other.get("status") in ("finished", "cancelled"):
                continue
            used = {(other.get("context", {}).get("workspace"), r.get("pane_id")) for r in other.get("roles", [])}
            if names & used:
                raise Error(f"Worker(s) reserved by unfinished Run {other.get('id')}; inspect/resume it, do not start a conflicting Run")
        runid = uuid.uuid4().hex[:12]
        run = {"version": 3, "id": runid, "team": team["template"], "context": team["context"], "goal": args.goal,
               "status": "active", "created_at": now(), "stage_index": 0, "stage_attempt": 1, "round": 1,
               "stages": team["plan"]["workflow"], "stage_definitions": team["plan"]["stages"],
               "roles": team["roles"], "settings": copy.deepcopy(team["plan"]["settings"]),
               "dispatches": [], "events": [], "approval_tokens": []}
        save(run_file(runid), run)
    return {"run_id": runid, "stage": run["stages"][0], "status": "active", "goal": args.goal}


def stage_info(run):
    idx = run["stage_index"]
    if idx >= len(run["stages"]): return {"stage": None, "status": "ready_to_finish"}
    name = run["stages"][idx]
    return {"stage": name, "actor": run["stage_definitions"][name]["actor"],
            "gate": run["stage_definitions"][name]["gate"], "round": run["round"],
            "max_rounds": run["settings"]["orchestration"]["max_rounds"], "stage_attempt": run["stage_attempt"]}


def get_role(run, name):
    for role in run["roles"]:
        if role["role"] == name: return role
    raise Error(f"Unknown worker role: {name}")


def needs_approval(run, effect):
    gov = run["settings"]["governance"]
    if gov["autonomy"] == "read-only" and effect != "read":
        raise Error("autonomy=read-only prohibits write/sensitive dispatch (policy check, not an OS sandbox)")
    if effect == "sensitive" and gov["autonomy"] != "approved-actions":
        raise Error("sensitive actions require autonomy=approved-actions plus explicit approval")
    p = gov["approval"]
    return effect == "sensitive" or p == "every-step" or (p == "changes" and effect == "write")


def run_approve(args):
    # A tty prompt controlled by a tool-enabled Lead is not owner authentication.
    # Fail closed until an independent owner authorization service exists.
    raise Error("Owner approval is disabled: same-user TTY confirmation cannot enforce an Owner boundary. Use an external authorization mechanism in a later version")


def result_path(runid, dispatchid):
    if not re.fullmatch(r"[a-f0-9]{12}", runid) or not re.fullmatch(r"[a-f0-9]{10}", dispatchid):
        raise Error("Invalid result identity")
    return STATE / "artifacts" / runid / (dispatchid + ".json")


def read_result(run, rec):
    """Read ONLY a dispatch-bound result, never a stale terminal scrollback.

    This is an identity/shape check, NOT a signature or OS security boundary:
    an agent running as the same user may still fabricate a JSON report.
    """
    path = result_path(run["id"], rec["id"])
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
            raise Error("Result must be a regular file smaller than 1 MiB (no symlinks)")
        content = path.read_bytes()
        doc = json.loads(content)
    except FileNotFoundError:
        raise Error(f"Result artifact not found for dispatch {rec['id']}")
    except (OSError, ValueError, TypeError) as e:
        raise Error(f"Unreadable result artifact for {rec['id']}: {e}") from e
    if not isinstance(doc, dict):
        raise Error("Result must be a JSON mapping")
    expected = {"run_id": run["id"], "dispatch_id": rec["id"], "role": rec["role"],
                "stage": run["stages"][rec["stage_index"]], "stage_attempt": rec["stage_attempt"]}
    for key, val in expected.items():
        if doc.get(key) != val:
            raise Error(f"Result identity mismatch: {key}")
    if doc.get("status") not in ("complete", "blocked") or doc.get("verdict") not in ("pass", "fail", "needs-work"):
        raise Error("Result must contain status complete|blocked and verdict pass|fail|needs-work")
    if not isinstance(doc.get("summary"), str) or len(doc["summary"].strip()) < 20:
        raise Error("Result must contain a meaningful summary (20+ chars)")
    evidence = doc.get("evidence")
    if not isinstance(evidence, list) or not evidence or any(not isinstance(s, str) or len(s.strip()) < 8 for s in evidence):
        raise Error("Result needs a nonempty list of evidence strings")
    tests = doc.get("tests", [])
    if not isinstance(tests, list) or any(not isinstance(t, dict) or
          not isinstance(t.get("command"), str) or type(t.get("exit_code")) is not int for t in tests):
        raise Error("tests must contain {command, exit_code} entries")
    return doc, hashlib.sha256(content).hexdigest()


def reconcile_result(run, rec, observed_status):
    """Fail closed on unknown status or missing/invalid report."""
    data = {"observed_status": observed_status, "collected_at": now()}
    if observed_status not in ("idle", "done", "blocked"):
        data["status"] = "uncertain"
        data["error"] = f"Unexpected agent status: {observed_status}"
        return data
    try:
        report, digest = read_result(run, rec)
    except Error as e:
        data["status"] = "uncertain" if observed_status != "blocked" else "blocked"
        data["error"] = str(e)
        return data
    data.update({"status": "blocked" if observed_status == "blocked" or report["status"] == "blocked" else "settled",
                 "report": report, "artifact_sha256": digest, "result_file": str(result_path(run["id"], rec["id"]))})
    data["error"] = None
    return data


def run_dispatch(args):
    run = require_run(args.run_id)
    if run["status"] != "active": raise Error("Run is not active")
    stage = stage_info(run)
    expected_index = run["stage_index"]
    if stage["actor"] != args.role and not args.remediation:
        raise Error(f"Current stage '{stage['stage']}' expects {stage['actor']}; use --remediation for another Worker")
    role = get_role(run, args.role)
    if args.effect not in ("read", "write", "sensitive"):
        raise Error("--effect must be read, write, or sensitive")
    approval_required = needs_approval(run, args.effect)
    # An unresolved submission is never automatically re-sent; delivery may have occurred.
    uncertain = [x for x in run["dispatches"] if x["stage_index"] == run["stage_index"]
                 and x["role"] == args.role and x["status"] in ("submission_uncertain", "submitted_unsettled", "uncertain")]
    if uncertain:
        raise Error(f"Pending/uncertain dispatch {uncertain[-1]['id']}; use run collect, never blindly resend")
    attempts = [x for x in run["dispatches"] if x["stage_index"] == run["stage_index"]
                and x.get("stage_attempt", 1) == run["stage_attempt"] and x["role"] == args.role]
    if len(attempts) >= 1 + run["settings"]["orchestration"]["max_retries"]:
        raise Error("max_retries limit reached for this role/stage attempt; use a documented gate revision")
    pending = [x for x in run["dispatches"] if x["status"] in ("submission_uncertain", "submitted_unsettled", "uncertain")]
    if len(pending) >= run["settings"]["orchestration"]["max_parallel"]:
        raise Error("max_parallel limit reached; collect pending dispatches first")
    approvals = [x for x in run["approval_tokens"] if x["role"] == args.role and x["effect"] == args.effect
                 and x["stage_index"] == run["stage_index"]]
    if approval_required and not approvals:
        raise Error(f"Approval required: run approve --run-id {args.run_id} --role {args.role} --effect {args.effect} --reason <user-authorized-reference>")
    if not verify_existing(role):
        raise Error(f"Agent {role['agent_name']} is not live; do not send to an unknown terminal")
    live = lookup_agent(role["agent_name"])
    live_status = agent_status_of(live)
    if live_status not in ("idle", "done"):
        raise Error(f"Agent {role['agent_name']} is {live_status}; inspect before sending a new task")
    stamp = uuid.uuid4().hex[:10]
    artifact = result_path(args.run_id, stamp)
    text = (f"[Herdr Team Manager task {stamp} / run {args.run_id} / stage {stage['stage']}]\n"
            f"Role: {args.role}. Goal: {run['goal']}\n"
            f"Follow your role scope. Do not make security-sensitive or protected changes without the Owner's approval.\n"
            f"You MUST write a new UTF-8 JSON report at exactly: {artifact}\n"
            f"Required fields: run_id='{args.run_id}', dispatch_id='{stamp}', role='{args.role}', "
            f"stage='{stage['stage']}', stage_attempt={run['stage_attempt']}, "
            f"status='complete'|'blocked', verdict='pass'|'fail'|'needs-work', "
            f"summary (20+ chars), evidence (list of concrete strings, 8+ chars each), "
            f"tests (optional list of {{command, exit_code}}).\n"
            f"Create the report as a temporary file in the SAME directory, then atomically rename it to that path. "
            f"A terminal message alone is not a report. This JSON is a worker assertion, not independent proof. "
            f"List any unverified claims and blockers. Never falsify test results.\n\n{args.message}")
    with locked():
        run = require_run(args.run_id)
        if run["status"] != "active" or run["stage_index"] != expected_index:
            raise Error("Run changed before dispatch; inspect status")
        if any(x["role"] == args.role and x["stage_index"] == run["stage_index"]
               and x["status"] in ("submission_uncertain", "submitted_unsettled", "uncertain") for x in run["dispatches"]):
            raise Error("Concurrent dispatch already pending for this Worker")
        if sum(x["role"] == args.role and x["stage_index"] == run["stage_index"]
               and x.get("stage_attempt", 1) == run["stage_attempt"] for x in run["dispatches"]) >= 1 + run["settings"]["orchestration"]["max_retries"]:
            raise Error("max_retries limit reached due to concurrent dispatch")
        if sum(x["status"] in ("submission_uncertain", "submitted_unsettled", "uncertain")
               for x in run["dispatches"]) >= run["settings"]["orchestration"]["max_parallel"]:
            raise Error("Concurrent dispatch exceeds max_parallel")
        artifact.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        artifact.parent.chmod(0o700)
        if artifact.exists() or artifact.is_symlink():
            raise Error("Dispatch artifact path already exists; refusing overwrite")
        item = {"id": stamp, "stage_index": run["stage_index"], "stage_attempt": run["stage_attempt"],
                "remediation": args.remediation, "role": args.role, "effect": args.effect,
                "status": "submission_uncertain", "created_at": now(), "prompt": text,
                "result_file": str(artifact)}
        run["dispatches"].append(item)
        if approvals:
            fresh = [x for x in run["approval_tokens"] if x["role"] == args.role and x["effect"] == args.effect
                     and x["stage_index"] == run["stage_index"]]
            if not fresh:
                raise Error("Approval token has already been used")
            token = fresh[0]
            run["approval_tokens"].remove(token)
            item["approval_reference"] = token["reason"]
        save(run_file(args.run_id), run)
    timeout_ms = run["settings"]["orchestration"]["task_timeout_seconds"]*1000
    cmd = ["agent", "prompt", role["agent_name"], text]
    if not args.no_wait:
        cmd += ["--wait", "--until", "idle", "--until", "done", "--until", "blocked", "--timeout", str(timeout_ms)]
    try:
        result = herdr(*cmd, timeout=timeout_ms/1000+15)
        if args.no_wait:
            with locked():
                run = require_run(args.run_id)
                rec = next(x for x in run["dispatches"] if x["id"] == stamp)
                rec["status"] = "submitted_unsettled"
                save(run_file(args.run_id), run)
            return {"dispatch_id": stamp, "status": "submitted_unsettled",
                    "next": "run collect --run-id ... --dispatch-id ... before retry or passing the stage"}
        # A successful wait is only a lifecycle observation. Report identity is
        # independently verified before settling.
        st = agent_status_of(result)
        with locked():
            run = require_run(args.run_id)
            rec = next(x for x in run["dispatches"] if x["id"] == stamp)
            rec.update(reconcile_result(run, rec, st))
            save(run_file(args.run_id), run)
        return {"dispatch_id": stamp, "status": rec["status"], "agent_state": st,
                "report": rec.get("report"), "error": rec.get("error"),
                "warning": "Worker JSON is not independent verification; Lead must check evidence and tests"}
    except Error as e:
        with locked():
            run = require_run(args.run_id)
            rec = next(x for x in run["dispatches"] if x["id"] == stamp)
            rec["error"] = str(e); save(run_file(args.run_id), run)
        raise Error(f"Dispatch {stamp} uncertain; input might have been sent. Run collect before retry. {e}") from e


def herdr_read(name):
    # agent read deliberately prints plain text instead of JSON
    return process(["herdr", "agent", "read", name, "--source", "recent-unwrapped", "--lines", "160"],
                   timeout=30, raw=True)[-50000:]


def run_collect(args):
    run = require_run(args.run_id)
    rec = next((x for x in reversed(run["dispatches"]) if x["id"] == args.dispatch_id), None)
    if rec is None: raise Error("Unknown dispatch ID")
    role = get_role(run, rec["role"])
    if not verify_existing(role): raise Error("Registered Agent is no longer live")
    agent = lookup_agent(role["agent_name"])
    st = agent_status_of(agent)
    with locked():
        run = require_run(args.run_id)
        rec = next(x for x in run["dispatches"] if x["id"] == args.dispatch_id)
        if rec["status"] != "settled":
            rec.update(reconcile_result(run, rec, st))
        save(run_file(args.run_id), run)
    return {"dispatch_id": args.dispatch_id, "status": rec["status"], "agent_state": st,
            "report": rec.get("report"), "error": rec.get("error"),
            "warning": "Structured self-report must be independently reviewed; no automatic retry"}


def run_gate(args):
    with locked():
        run = require_run(args.run_id)
        if run["status"] != "active": raise Error("Run is not active")
        stage = stage_info(run)
        actor = stage["actor"]
        if args.decision in ("pass", "revise"):
            pending = [x for x in run["dispatches"] if x["stage_index"] == run["stage_index"] and
                       x.get("stage_attempt", 1) == run["stage_attempt"] and
                       x["status"] != "settled"]
            if pending:
                raise Error(f"Cannot {args.decision} stage with unresolved/blocked dispatches: {[x['id'] for x in pending]}")
        if args.decision == "pass" and actor != "lead":
            all_recs = [x for x in run["dispatches"] if x["stage_index"] == run["stage_index"] and
                        x.get("stage_attempt", 1) == run["stage_attempt"] and x["role"] == actor]
            if not all_recs:
                raise Error(f"Cannot pass stage {stage['stage']}: no bound positive artifact from {actor}")
            good = all_recs[-1]  # A later failing review overrides an earlier pass.
            if good["status"] != "settled" or good.get("report", {}).get("verdict") != "pass" or not good.get("artifact_sha256"):
                raise Error(f"Latest {actor} artifact is not an approved result")
            for role_name in set(x["role"] for x in run["dispatches"] if x["stage_index"] == run["stage_index"] and
                                 x.get("stage_attempt", 1) == run["stage_attempt"]):
                latest_role = [x for x in run["dispatches"] if x["stage_index"] == run["stage_index"] and
                               x.get("stage_attempt", 1) == run["stage_attempt"] and x["role"] == role_name][-1]
                if latest_role.get("report", {}).get("verdict") != "pass":
                    raise Error(f"Latest remediation/worker artifact for {role_name} is not pass")
            # Pin evidence to the bytes collected. If Worker changes its report
            # after collection, an old gate cannot silently endorse new bytes.
            _report, digest = read_result(run, good)
            if digest != good["artifact_sha256"]:
                raise Error("Worker artifact changed after collection")
            if stage["stage"] in ("self_test", "independent_test"):
                tests = good["report"].get("tests", [])
                if not tests or any(t["exit_code"] != 0 for t in tests):
                    raise Error("Test stage requires explicit passing test records with exit_code=0")
        if args.decision == "pass" and actor == "lead":
            # Lead attestation remains human/agent reasoning, not an external proof.
            pass
        if len(args.evidence.strip()) < 12:
            raise Error("Gate requires specific evidence (at least 12 characters)")
        run["events"].append({"at": now(), "type": "gate", "stage": stage["stage"],
                              "stage_index": run["stage_index"], "stage_attempt": run["stage_attempt"],
                              "decision": args.decision, "evidence": args.evidence,
                              "artifact_sha256": good["artifact_sha256"] if args.decision == "pass" and actor != "lead" else None})
        if args.decision == "pass":
            run["stage_index"] += 1
            run["stage_attempt"] = 1
        elif args.decision == "revise":
            run["round"] += 1
            run["stage_attempt"] += 1
            if run["round"] > run["settings"]["orchestration"]["max_rounds"]:
                run["status"] = "blocked_round_limit"
        else: run["status"] = "blocked"
        save(run_file(args.run_id), run)
    return {"decision": args.decision, "status": run["status"], **stage_info(run)}


def run_finish(args):
    with locked():
        run = require_run(args.run_id)
        if run["status"] != "active": raise Error(f"Run status {run['status']}; cannot finish")
        if run["stage_index"] != len(run["stages"]):
            raise Error(f"Cannot finish; next required stage is {stage_info(run)['stage']}")
        if any(x["status"] in ("submission_uncertain", "submitted_unsettled", "uncertain", "blocked") for x in run["dispatches"]):
            raise Error("Cannot finish while some dispatched work is unresolved or blocked")
        passed = {e.get("stage_index") for e in run["events"] if e.get("type") == "gate" and e.get("decision") == "pass"}
        if passed != set(range(len(run["stages"]))):
            raise Error("Some mandatory gates have no recorded pass event")
        run["status"] = "finished"; run["finished_at"] = now()
        save(run_file(args.run_id), run)
    return {"finished": True, "run_id": args.run_id, "events": len(run["events"])}


def run_pause(args, paused):
    with locked():
        run = require_run(args.run_id)
        if paused:
            if run["status"] != "active": raise Error("Only active run can be paused")
            run["status"] = "paused"
        else:
            if run["status"] not in ("paused", "blocked", "blocked_round_limit"):
                raise Error("Only paused/blocked run can resume")
            if run["status"] == "blocked_round_limit" and not args.extend_rounds:
                raise Error("Reached round limit. Owner must explicitly supply --extend-rounds N")
            if args.extend_rounds:
                raise Error("Round-limit extension disabled until independent Owner authentication is available")
            run["status"] = "active"
        run["events"].append({"at": now(), "type": "pause" if paused else "resume"})
        save(run_file(args.run_id), run)
    return {"status": run["status"], "run_id": args.run_id}


def run_list():
    """Discover orphaned/paused work without performing any new dispatch."""
    ctx = context()
    result = []
    with locked():
        for p in sorted((STATE / "runs").glob("*.json")):
            run = load(p)
            if run.get("context") != ctx:
                continue
            pending = [d["id"] for d in run.get("dispatches", []) if d["status"] not in ("settled",)]
            result.append({"run_id": run["id"], "status": run["status"],
                           "stage": stage_info(run)["stage"], "pending_dispatches": pending})
    return {"runs": result, "unfinished": [x for x in result if x["status"] not in ("finished", "cancelled")]}


def run_recover(args):
    """Read-only reconciliation hint. Never resend uncertain prompts."""
    with locked():
        run = require_run(args.run_id)
        pending = [d for d in run.get("dispatches", []) if d["status"] not in ("settled",)]
        return {"run_id": run["id"], "run_status": run["status"], "current_stage": stage_info(run),
                "pending_dispatches": [{"id": d["id"], "role": d["role"],
                                       "status": d["status"], "report_path": d.get("result_file"),
                                       "next": f"run collect --run-id {run['id']} --dispatch-id {d['id']}"}
                                      for d in pending],
                "next": "Inspect bound artifacts and run collect for every unsettled dispatch; NEVER blindly resend. Then resume an explicitly paused Run."}


def run_cancel(args):
    """Explicitly mark a Run abandoned; never claim completion or delete workers."""
    with locked():
        run = require_run(args.run_id)
        if run["status"] in ("finished", "cancelled"):
            raise Error("Run is already terminal")
        if args.confirm_run_id != args.run_id or len(args.reason.strip()) < 12:
            raise Error("Cancellation requires --confirm-run-id matching --run-id and specific --reason")
        pending = [d["id"] for d in run["dispatches"] if d["status"] not in ("settled", "blocked")]
        if pending:
            raise Error(f"Cannot cancel while dispatches are unresolved: {pending}. Inspect and reconcile safely")
        for role in run["roles"]:
            if not verify_existing(role):
                raise Error("Worker disappeared. Do not release its reservation before manual reconciliation")
            info = lookup_agent(role["agent_name"])
            status = agent_status_of(info)
            if status not in ("idle", "done", "blocked"):
                raise Error(f"Worker {role['agent_name']} is {status}; cannot safely release reservation")
        run["status"] = "cancelled"
        run["events"].append({"at": now(), "type": "cancel", "reason": args.reason})
        save(run_file(args.run_id), run)
    return {"run_id": args.run_id, "status": "cancelled", "finished": False}


def emit(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def add_common(p):
    p.add_argument("--config", type=Path)


def main():
    p = argparse.ArgumentParser(description="Herdr Team Manager v4.2 — conservative local Herdr orchestration primitives")
    p.add_argument("--config", type=Path, help="Alternate team.yaml")
    sp = p.add_subparsers(dest="cmd", required=True)
    sp.add_parser("templates")
    sp.add_parser("validate")
    plan = sp.add_parser("plan")
    plan.add_argument("template"); plan.add_argument("--set", action="append", default=[])
    plan.add_argument("--layout", choices=("side", "tab")); plan.add_argument("--arrange", choices=("auto", "rows", "cols", "grid"))
    plan.add_argument("--worktree", choices=("on", "off", "auto"))
    initp = sp.add_parser("init")
    initp.add_argument("template"); initp.add_argument("--set", action="append", default=[])
    initp.add_argument("--layout", choices=("side", "tab")); initp.add_argument("--arrange", choices=("auto", "rows", "cols", "grid"))
    initp.add_argument("--worktree", choices=("on", "off", "auto")); initp.add_argument("--cwd")
    initp.add_argument("--repo"); initp.add_argument("--task"); initp.add_argument("--dry-run", action="store_true")
    adoptp = sp.add_parser("adopt")
    adoptp.add_argument("template"); adoptp.add_argument("--role", action="append", default=[])
    adoptp.add_argument("--set", action="append", default=[]); adoptp.add_argument("--cwd")
    adoptp.add_argument("--dry-run", action="store_true")
    sp.add_parser("status")
    runp = sp.add_parser("run")
    rsp = runp.add_subparsers(dest="run_cmd", required=True)
    start = rsp.add_parser("start"); start.add_argument("--goal", required=True)
    rsp.add_parser("list")
    for cmd in ("status", "stage", "finish", "pause", "resume", "approve", "dispatch", "collect", "gate", "recover", "cancel"):
        sub = rsp.add_parser(cmd)
        sub.add_argument("--run-id", required=True)
        if cmd == "cancel":
            sub.add_argument("--confirm-run-id", required=True)
            sub.add_argument("--reason", required=True)
        if cmd == "resume": sub.add_argument("--extend-rounds", type=int, default=0)
        if cmd == "approve":
            sub.add_argument("--role", required=True); sub.add_argument("--effect", required=True, choices=("read", "write", "sensitive")); sub.add_argument("--reason", required=True)
        if cmd == "dispatch":
            sub.add_argument("--role", required=True); sub.add_argument("--message", required=True)
            sub.add_argument("--effect", default="read", choices=("read", "write", "sensitive"))
            sub.add_argument("--remediation", action="store_true", help="delegate a corrective subtask to a different role while keeping the current gate")
            sub.add_argument("--no-wait", action="store_true", help="submit without waiting; explicit run collect required")
        if cmd == "collect": sub.add_argument("--dispatch-id", required=True)
        if cmd == "gate":
            sub.add_argument("--decision", required=True, choices=("pass", "revise", "blocked")); sub.add_argument("--evidence", required=True)
    args = p.parse_args()
    try:
        cfg, path = read_cfg(args.config)
        if args.cmd == "validate":
            emit({"valid": True, "config": str(path), "templates": list(cfg["templates"])}); return 0
        if args.cmd == "templates":
            emit({k: {"roles": list(template(cfg,k)["roles"]), "workflow": template(cfg,k)["workflow"]}
                  for k in cfg["templates"]}); return 0
        if args.cmd == "plan":
            emit(initial_plan(cfg, args.template, args)); return 0
        if args.cmd == "init": emit(init(cfg, args)); return 0
        if args.cmd == "adopt": emit(adopt(cfg, args)); return 0
        if args.cmd == "status":
            team = current_team()
            live = []
            for r in team["roles"]:
                actual = lookup_agent(r["agent_name"])
                live.append({"role": r["role"], "agent_name": r["agent_name"],
                             "registered_pane": r["pane_id"], "live": bool(actual),
                             "observed_state": agent_status_of(actual) if isinstance(actual, dict) else "missing"})
            emit({"template": team["template"], "stored_status": team["status"], "agents": live,
                  "worktree": team.get("worktree")}); return 0
        if args.cmd == "run":
            if args.run_cmd == "start": emit(run_start(cfg, args)); return 0
            if args.run_cmd == "list": emit(run_list()); return 0
            if args.run_cmd in ("status", "stage"):
                run = require_run(args.run_id)
                emit(run if args.run_cmd == "status" else stage_info(run)); return 0
            action = {"approve": run_approve, "dispatch": run_dispatch, "collect": run_collect, "gate": run_gate,
                      "finish": run_finish, "pause": lambda a: run_pause(a,True),
                      "resume": lambda a: run_pause(a,False), "recover": run_recover,
                      "cancel": run_cancel}[args.run_cmd]
            emit(action(args)); return 0
    except (Error, TypeError, KeyError, ValueError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    return 0

if __name__ == "__main__":
    sys.exit(main())
