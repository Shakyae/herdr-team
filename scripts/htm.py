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


class HerdrError(Error):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


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


def socket_session(socket):
    """Return the unique named session whose current socket_path is exactly `socket`."""
    result = herdr("session", "list", "--json")
    sessions = result.get("sessions") if isinstance(result, dict) else None
    if not isinstance(sessions, list):
        raise Error("Invalid Herdr session list")
    matches = [item for item in sessions if isinstance(item, dict) and item.get("socket_path") == socket]
    if len(matches) != 1:
        raise Error("Herdr socket does not identify exactly one session")
    name = matches[0].get("name")
    if not isinstance(name, str) or not name.strip():
        raise Error("Herdr socket does not identify exactly one named session")
    return name


def lead_identity(require=True):
    if require and "HERDR_ENV" not in os.environ:
        first = pane_of_current_process()
        if first != pane_of_current_process():
            raise Error("Herdr pane 身份不唯一，拒绝猜测（两次探测不一致）")
        return {"context": {"workspace": first["workspace"], "lead_pane": first["pane"], "lead_tab": first["tab"]}, "session": first["session"]}
    if require and os.environ.get("HERDR_ENV") != "1":
        raise Error("Must run inside Herdr: HERDR_ENV=1")
    ws = os.environ.get("HERDR_WORKSPACE_ID", "")
    pane = os.environ.get("HERDR_PANE_ID", "")
    tab = os.environ.get("HERDR_TAB_ID", "")
    if require and not (ws and pane and tab):
        raise Error("Missing Herdr pane/workspace/tab environment")
    session = ""
    if require:
        for label, value, pattern in (("workspace", ws, WORKSPACE_ID_RE),
                                      ("tab", tab, TAB_ID_RE), ("pane", pane, PANE_ID_RE)):
            if not pattern.fullmatch(value):
                raise Error(f"Invalid Herdr {label} id: {value!r}")
        if tab.split(":")[0] != ws or pane.split(":")[0] != ws:
            raise Error(f"Tab/Pane does not belong to workspace {ws}: tab={tab!r} pane={pane!r}")
        # An inherited HERDR_* environment is not proof of ownership: it can be forged or
        # stale. Prove the calling process actually lives in the pane before trusting it.
        first = pane_of_current_process()
        if first != pane_of_current_process():
            raise Error("Herdr pane 身份不唯一，拒绝猜测（两次探测不一致）")
        if any(first[key] != value for key, value in (("workspace", ws), ("tab", tab), ("pane", pane))):
            raise Error(f"Herdr 进程归属与 HERDR_* 身份不一致: probe={first!r}")
        session = first["session"]
        if not re.fullmatch(r"[A-Za-z0-9._-]+", session):
            raise Error(f"Invalid Herdr session name: {session!r}")
        env_session = os.environ.get("HERDR_SESSION", "")
        if env_session and env_session != session:
            raise Error(f"Herdr 进程归属 session 与环境不一致: {env_session!r} != {session!r}")
        socket = os.environ.get("HERDR_SOCKET_PATH", "")
        if socket and socket_session(socket) != session:
            raise Error(f"Herdr socket 与进程归属不一致: {socket!r} != {session!r}")
    ctx = {"workspace": ws, "lead_pane": pane, "lead_tab": tab}
    return {"context": ctx, "session": session}


def context(require=True):
    return lead_identity(require)["context"]


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def state_file(ctx):
    key = hashlib.sha256(f"{ctx['workspace']}|{ctx['lead_pane']}".encode()).hexdigest()[:16]
    return STATE / "teams" / (key + ".json")


def run_file(runid):
    if not re.fullmatch(r"[a-f0-9]{12}", runid):
        raise Error("Invalid Run ID")
    return STATE / "runs" / (runid + ".json")


def ledger_ownership(data, session, path):
    """Classify ledger ownership: 'verified' | 'legacy'; raise when it belongs elsewhere.

    The recorded session name is a weak signal -- a session can be stopped and
    recreated under the same name with the same IDs but different agents. It is
    therefore only used to reject ledgers that clearly belong to another session.
    """
    recorded = data.get("session")
    if recorded is None:
        return "legacy"
    if recorded != session:
        raise Error(f"账本 {path} 属于 session {recorded!r}，当前 session 为 {session!r}；"
                    f"拒绝跨 session 操作。多 session 场景请为每个 session 设置独立的 HTM_STATE_DIR")
    return "verified"


# `verify_existing` raises for two very different reasons, and the difference
# decides whether a ledger is rejected or merely tolerated:
#
#   AgentUnobservable -- the agent could not be looked at at all. Workers
#                        legitimately die, so this is tolerable.
#   Error             -- the agent WAS observed and contradicts the ledger
#                        (identity, pane, kind, name, cwd). Never tolerable.
#
# A message-substring whitelist cannot make this call: every contradiction that
# is not on the list (a name mismatch, a cwd mismatch) silently degrades into
# "unobservable" and lets a foreign ledger through. The distinction is therefore
# carried by the exception type raised at the observation site.
class AgentUnobservable(Error):
    """The agent could not be observed. Not evidence of anything."""


class LedgerIdentityError(Error):
    """The ledger itself was refused on ownership grounds.

    A distinct type so `run drive` can label the stop reason precisely instead
    of pattern-matching Chinese error text -- a message rewrite must never
    silently reclassify a refusal as a generic error.
    """


def _inspect_roles(data, session):
    """Classify each recorded Worker identity against the live Herdr state.

    Returns (verified, total, conflicts, unavailable):
      verified    -- UUID matched live
      total       -- roles that recorded an agent_session at all
      conflicts   -- identity contradictions (never tolerated)
      unavailable -- role could not be observed (tolerable, workers die)
    """
    verified = total = 0
    conflicts, unavailable = [], []
    for role in data.get("roles", []):
        if not (role.get("agent_session") or {}).get("value"):
            continue
        total += 1
        try:
            if verify_existing(role, session=session):
                verified += 1
        except AgentUnobservable as e:
            unavailable.append(f"Agent {role.get('agent_name')}: {e}")
        except Error as e:
            # Fail closed: anything else verify_existing raises means it looked
            # at the agent and found a contradiction.
            conflicts.append(f"Agent {role.get('agent_name')}: {e}")
    return verified, total, conflicts, unavailable


def require_roles_owned(data, session, path, require_anchor=True):
    """Prove ledger ownership through Worker agent_session UUIDs.

    A session name alone is weak evidence: a session can be stopped and recreated
    under the same name with the same IDs but different agents. Callers therefore
    invoke this for tier C regardless of whether the ledger recorded a session.

    Two separate obligations, deliberately not collapsed:

      contradictions -- an observed Worker that contradicts the ledger. This
                        means the ledger is demonstrably *not* these agents, so
                        it rejects unconditionally. `require_anchor` never
                        relaxes it.
      anchor         -- positive proof that the ledger is ours (>=1 live UUID).
                        Workers legitimately die; the operations that must work
                        *because* they died (diagnose, abandon, recover-attempt)
                        cannot demand a survivor or they would reintroduce the
                        Issue #2 lock-up. Those pass require_anchor=False and
                        rely on the session tag, the pane context and the
                        contradiction check instead.
    """
    verified, total, conflicts, unavailable = _inspect_roles(data, session)
    if conflicts:
        raise LedgerIdentityError(f"账本 {path} 存在角色身份冲突，拒绝执行：" + "；".join(conflicts))
    if not require_anchor:
        return verified, unavailable
    if total == 0:
        raise LedgerIdentityError(f"账本 {path} 未记录任何 Worker 身份，无法证明归属；拒绝执行会产生副作用的操作")
    if verified == 0:
        detail = ("；不可观察：" + "；".join(unavailable)) if unavailable else ""
        raise LedgerIdentityError(f"账本 {path} 的 Worker 均无法在当前 session 内验证身份；无法证明该账本属于当前 session。"
                    f"如确认应归属当前 session，请检查 Worker 是否仍在运行{detail}")
    return verified, unavailable


def require_target_role(data, session, role_name, path):
    """Verify the specific Worker a dispatch/collect is about to act on.

    Ledger-level anchoring is not enough: another role matching says nothing
    about whether *this* Worker is the one the ledger named.
    """
    role = next((r for r in data.get("roles", []) if r.get("role") == role_name), None)
    if role is None:
        # An unknown role is a caller error with its own established message
        # (get_role). Identity checking has nothing to add: a role that is not
        # in the ledger cannot be dispatched anyway.
        return None
    if not (role.get("agent_session") or {}).get("value"):
        raise LedgerIdentityError(f"账本 {path} 的角色 {role_name} 未记录 Worker 身份；无法验证目标 Worker，拒绝执行")
    if not verify_existing(role, session=session):
        raise LedgerIdentityError(f"目标 Worker {role.get('agent_name')} 不在当前 session 内，拒绝针对它的操作")
    return role


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


def process(argv, timeout=40, raw=False, env=None):
    try:
        r = subprocess.run(argv, text=True, capture_output=True, timeout=timeout, check=False, env=env)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise Error(f"Command unavailable/timed out ({argv[:3]}): {e}") from e
    if r.returncode:
        message = f"Command failed ({r.returncode}): {' '.join(argv[:3])}: {(r.stderr or r.stdout)[-900:]}"
        for output in (r.stderr, r.stdout):
            try:
                parsed = json.loads(output)
            except (json.JSONDecodeError, TypeError):
                continue
            err = parsed.get("error") if isinstance(parsed, dict) else None
            if isinstance(err, dict):
                raise HerdrError(message, code=err.get("code"))
        raise Error(message)
    if raw:
        return r.stdout
    try:
        out = json.loads(r.stdout)
    except json.JSONDecodeError as e:
        raise Error(f"Expected Herdr JSON from {' '.join(argv[:3])}: {r.stdout[-600:]}") from e
    if not isinstance(out, dict) or out.get("status") in ("error", "failed") or out.get("error"):
        err = out.get("error") if isinstance(out, dict) else None
        raise HerdrError(f"Herdr error: {str(out)[:900]}", code=err.get("code") if isinstance(err, dict) else None)
    return out.get("result", out)


def herdr(*args, timeout=40, session=None):
    if session is not None:
        if not isinstance(session, str):
            raise Error("Invalid Herdr session name")
        if not session.strip():
            session = None
        elif not re.fullmatch(r"[A-Za-z0-9._-]+", session):
            raise Error("Invalid Herdr session name")
    return process(["herdr", *(["--session", session] if session else []), *args], timeout=timeout)


def pane_of_current_process():
    try:
        sessions = herdr("session", "list", "--json")["sessions"]
        if not isinstance(sessions, list):
            raise ValueError("invalid sessions")
        ancestors = set()
        pid = os.getpid()
        for _ in range(64):
            if pid in ancestors:
                raise ValueError("process ancestry cycle")
            ancestors.add(pid)
            pid = int(process(["/bin/ps", "-o", "ppid=", "-p", str(pid)], raw=True).strip())
            if pid <= 1:
                break
        matches = []
        seen_panes, seen_tabs = {}, {}
        for session in sessions:
            if not isinstance(session, dict):
                raise ValueError("invalid session")
            if session.get("running") is not True:
                continue
            name = session.get("name")
            if not isinstance(name, str) or not name.strip():
                raise Error("session 信息不完整，无法确认身份唯一性")
            panes = herdr("pane", "list", session=name)["panes"]
            if not isinstance(panes, list):
                raise ValueError("invalid panes")
            for pane in panes:
                pane_id = pane["pane_id"]
                workspace, tab_id = pane["workspace_id"], pane["tab_id"]
                for seen, key, label in ((seen_panes, (workspace, pane_id), "Pane"),
                                         (seen_tabs, (workspace, tab_id), "Tab")):
                    if key in seen and seen[key] != name:
                        raise Error(f"跨 session {label} ID 冲突: {key}")
                    seen[key] = name
                info = herdr("pane", "process-info", "--pane", pane_id, session=name)["process_info"]
                shell_pid = info["shell_pid"]
                foreground = info["foreground_processes"]
                if type(shell_pid) is not int or not isinstance(foreground, list):
                    raise ValueError("invalid process info")
                pids = {shell_pid}
                for item in foreground:
                    if type(item["pid"]) is not int:
                        raise ValueError("invalid process pid")
                    pids.add(item["pid"])
                if ancestors & pids:
                    matches.append({"session": name, "workspace": pane["workspace_id"],
                                    "tab": pane["tab_id"], "pane": pane_id})
        if len(matches) > 1:
            raise Error("Herdr pane 身份不唯一，拒绝猜测")
        if not matches:
            raise ValueError("no matching pane")
        match = matches[0]
        for label, pattern in (("workspace", WORKSPACE_ID_RE), ("tab", TAB_ID_RE), ("pane", PANE_ID_RE)):
            if not isinstance(match[label], str) or not pattern.fullmatch(match[label]):
                raise ValueError(f"invalid {label} id")
        if match["tab"].split(":")[0] != match["workspace"] or match["pane"].split(":")[0] != match["workspace"]:
            raise ValueError("pane/tab workspace mismatch")
        return match
    except (Error, KeyError, TypeError, ValueError, AttributeError) as e:
        if isinstance(e, Error) and "身份不唯一，拒绝猜测" in str(e):
            raise
        raise Error(f"无法定位 Herdr pane: {e}") from e


def pane_shell_ready(pane_id, session=None):
    try:
        result = herdr("pane", "process-info", "--pane", pane_id, session=session)
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
                           launch_args=(), attempts=8, interval=0.5, herdr_session=None):
    stats = {"shell_polls": 0, "busy_retries": 0, "attempts": 0}
    for attempt in range(1, attempts + 1):
        stats["attempts"] = attempt
        if pane_id not in created_panes:
            raise Error(f"Pane {pane_id} was not created by this init; refusing agent start")
        result = herdr("pane", "get", pane_id, session=herdr_session)
        pane = result.get("pane") if isinstance(result, dict) else None
        if not isinstance(pane, dict) or pane.get("workspace_id") != workspace:
            raise Error(f"Pane {pane_id} is missing or belongs to a different workspace")
        if pane_shell_ready(pane_id, session=herdr_session):
            try:
                result = herdr("agent", "start", name, "--kind", kind, "--pane", pane_id,
                               *launch_args, timeout=60, session=herdr_session)
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


def lookup_agent(name, session=None):
    try:
        return agent_data(herdr("agent", "get", name, session=session))
    except Error:
        return None


def agent_name_for(role, pane_id):
    if not isinstance(pane_id, str) or not PANE_ID_RE.fullmatch(pane_id):
        raise Error(f"Invalid pane ID for agent name: {pane_id!r}")
    name = f"{role}-{pane_id.replace(':', '')}".lower()
    if not NAME_RE.fullmatch(name):
        raise Error(f"Invalid derived agent name: {name!r}")
    return name


def require_name_available(name, team_path, roles=(), session=None):
    if any(r.get("agent_name") == name for r in roles):
        raise Error(f"Agent name already registered in this team: {name}")
    for other_path in (STATE / "teams").glob("*.json"):
        if other_path == team_path:
            continue
        other = load(other_path)
        if any(r.get("agent_name") == name for r in other.get("roles", [])):
            raise Error(f"Agent name {name} already registered by team {other_path}")
    try:
        found = herdr("agent", "get", name, session=session)
    except Error as e:
        if isinstance(e, HerdrError) and e.code == "agent_not_found":
            return
        raise Error(f"Cannot verify agent name availability for {name}: {e}") from e
    if found is not None:
        raise Error(f"Agent name already occupied: {name}")
    raise Error(f"Cannot verify agent name availability for {name}")


def pending_plan(plan):
    result = copy.deepcopy(plan)
    for role in result["roles"]:
        role.update({"agent_name": None, "pending": True})
    return result


def verify_existing(team_role, session=None):
    # `lookup_agent` swallows every Error and returns None, so an unobservable
    # agent and a genuinely absent one arrive here identically as a falsy value.
    # Ownership checks must not treat "could not look" as "does not match"; the
    # caller in _inspect_roles separates the two by exception type.
    try:
        found = lookup_agent(team_role["agent_name"], session=session)
    except Error as e:
        raise AgentUnobservable(str(e)) from e
    if not found or not isinstance(found, dict):
        return False
    pane = found.get("pane")
    observed_pane = found.get("pane_id") or (pane.get("pane_id") if isinstance(pane, dict) else None)
    # Incomplete observations say nothing about WHO the agent is; a live agent
    # contradicting the ledger does. Only the latter rejects an ownership claim.
    if not observed_pane:
        raise AgentUnobservable(f"Agent {team_role['agent_name']} has no verifiable pane ID")
    if observed_pane != team_role["pane_id"]:
        raise Error(f"Agent {team_role['agent_name']} is live in unexpected pane {observed_pane}; refusing reuse")
    kind = agent_kind_of(found)
    if not kind:
        raise AgentUnobservable(f"Agent {team_role['agent_name']} has no verifiable kind")
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


def agent_presence(role, session=None):
    """Three-state observation of a bound Worker. Never proof that it stopped.

    Rules (design §3): confirmed_absent requires BOTH `agent get` returning
    error.code == agent_not_found AND `pane get` on the bound pane returning
    error.code == pane_not_found. Exit codes are never consulted. Timeouts,
    unknown codes, unparseable data and identity drift are all `unavailable`.
    """
    code = None
    try:
        # The Worker binding must be read from ITS session. Without this, a
        # named session's call would query the default server, and a live
        # Worker could produce agent_not_found + pane_not_found there -- i.e.
        # `confirmed_absent` -- which is exactly what the abandon fence trusts.
        found = herdr("agent", "get", role["agent_name"], session=session)
    except HerdrError as e:
        code = e.code
        if code != "agent_not_found":
            return {"state": "unavailable", "code": code or "agent_error", "detail": str(e)[:300]}
        found = None
    except Error as e:
        return {"state": "unavailable", "code": "herdr_unavailable", "detail": str(e)[:300]}
    if found is not None:
        found = agent_data(found)
        if not isinstance(found, dict):
            return {"state": "unavailable", "code": "invalid_agent_data", "detail": "agent get returned no mapping"}
        pane = found.get("pane")
        observed_pane = found.get("pane_id") or (pane.get("pane_id") if isinstance(pane, dict) else None)
        session = found.get("agent_session")
        expected = role.get("agent_session") or {}
        same_session = isinstance(session, dict) and all(
            isinstance(expected.get(k), str) and expected.get(k).strip() and session.get(k) == expected.get(k)
            for k in ("kind", "source", "value"))
        reasons = []
        if not observed_pane or observed_pane != role["pane_id"]:
            reasons.append("pane")
        if agent_kind_of(found) != role["kind"]:
            reasons.append("kind")
        if not same_session:
            reasons.append("session")
        if found.get("name") and found["name"] != role["agent_name"]:
            reasons.append("name")
        if found.get("cwd") and str(Path(found["cwd"]).resolve()) != str(Path(role["cwd"]).resolve()):
            reasons.append("cwd")
        if role["kind"] == "hermes" and found.get("profile") and found["profile"] != role.get("profile"):
            reasons.append("profile")
        if reasons:
            return {"state": "unavailable", "code": "identity_mismatch",
                    "detail": "bound identity does not match: " + ",".join(reasons)}
        return {"state": "present", "code": None, "status": agent_status_of(found)}
    # agent_not_found: only a matching pane_not_found proves absence.
    try:
        herdr("pane", "get", role["pane_id"], session=session)
    except HerdrError as e:
        if e.code == "pane_not_found":
            return {"state": "confirmed_absent", "code": "agent_not_found"}
        return {"state": "unavailable", "code": e.code or "pane_error", "detail": str(e)[:300]}
    except Error as e:
        return {"state": "unavailable", "code": "herdr_unavailable", "detail": str(e)[:300]}
    return {"state": "unavailable", "code": "pane_exists", "detail": "bound pane still exists"}


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


def clean_git_env():
    # Strip variables that retarget git at another repository, plus variables
    # whose removal only restores a tighter default (GIT_DISCOVERY_ACROSS_
    # FILESYSTEM defaults to crossing-disabled). GIT_CEILING_DIRECTORIES is
    # deliberately kept: stripping it would delete the caller's search bound
    # and let discovery escape into an unrelated parent repository.
    removed = {"GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
               "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
               "GIT_NAMESPACE", "GIT_PREFIX", "GIT_IMPLICIT_WORK_TREE",
               "GIT_SHALLOW_FILE", "GIT_GRAFT_FILE", "GIT_REPLACE_REF_BASE",
               "GIT_NO_REPLACE_OBJECTS", "GIT_CONFIG", "GIT_CONFIG_PARAMETERS",
               "GIT_CONFIG_COUNT", "GIT_DISCOVERY_ACROSS_FILESYSTEM"}
    return {key: value for key, value in os.environ.items()
            if key not in removed and not key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))}


def git_root(repo):
    return Path(process(["git", "-C", str(repo), "rev-parse", "--show-toplevel"],
                        raw=True, env=clean_git_env()).strip()).resolve()


def detect_git_root(cwd):
    cwd = Path(cwd).expanduser().resolve()
    try:
        result = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
                                text=True, capture_output=True, timeout=40, check=False, env=clean_git_env())
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Error(f"Unable to invoke git for {cwd}: {exc}") from exc
    out = (result.stdout or "").strip()
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()[-1000:]
        if "not a git repository" in stderr.lower():
            raise Error(f"{cwd} is not inside a Git repository; pass --repo or run from a Git working tree")
        if "cannot change to" in stderr.lower():
            raise Error(f"{cwd} does not exist or is inaccessible; pass --repo explicitly")
        raise Error(f"Unable to inspect Git repository at {cwd}: {stderr}")
    if not out or not Path(out).is_absolute():
        raise Error(f"Git returned no usable repository root for {cwd}")
    return Path(out).resolve()


def resolve_worktree_repo(explicit, cwd):
    if explicit:
        repo = Path(explicit).expanduser().resolve()
        root = git_root(repo)
        if repo != root:
            raise Error("--repo must point to an exact Git root; cross-subdirectory worktree assumptions are unsafe")
        return root
    return detect_git_root(cwd)


def worktree_details(plan, args, apply, allow_exists=False, cwd=None, expected=None):
    requested = plan["settings"]["execution"]["worktree"]
    mode_auto = requested == "auto" and plan["category"] == "development"
    if requested != "on" and not mode_auto:
        return None
    if mode_auto and not args.task:
        return None
    if not args.task:
        raise Error("Worktree requires --task lowercase-slug")
    if not SLUG_RE.fullmatch(args.task):
        raise Error("Invalid --task (must be a lowercase slug)")
    if cwd is None:
        try:
            cwd = os.getcwd()
        except OSError as exc:
            raise Error(f"Cannot determine current working directory: {exc}; pass --cwd explicitly") from exc
    root = resolve_worktree_repo(args.repo, cwd)
    if not allow_exists:
        dirty = process(["git", "-C", str(root), "status", "--porcelain", "--untracked-files=normal"],
                        raw=True, env=clean_git_env())
        if dirty.strip():
            raise Error("Source Git repo is dirty or contains untracked files. Commit/stash explicitly before building a HEAD-only worktree")
    digest = hashlib.sha256(str(root).encode()).hexdigest()[:10]
    branch = f"htm/{args.task}"
    wt = Path(os.environ.get("HTM_WORKTREE_DIR", "~/.local/share/herdr-team/worktrees")).expanduser() / digest / args.task
    if wt.exists() and not allow_exists:
        raise Error(f"Worktree path already exists; do not overwrite: {wt}")
    detail = {"repo": str(root), "path": str(wt), "branch": branch}
    if expected is not None and detail != expected:
        raise Error("Worktree identity changed since preflight; refusing to create a different resource")
    if apply:
        wt.parent.mkdir(parents=True, exist_ok=True)
        process(["git", "-C", str(root), "worktree", "add", "-b", branch, str(wt), "HEAD"],
                timeout=60, raw=True, env=clean_git_env())
    return detail


def init(cfg, args):
    plan = initial_plan(cfg, args.template, args)
    try:
        cwd = Path(args.cwd or os.getcwd()).expanduser().resolve()
    except OSError as exc:
        raise Error(f"Cannot determine current working directory: {exc}; pass --cwd explicitly") from exc
    if not cwd.is_dir(): raise Error(f"CWD does not exist: {cwd}")
    if args.dry_run:
        wt = worktree_details(plan, args, False, cwd=cwd)
        return {"dry_run": True, "no_mutations": True, "cwd": str(cwd), "worktree": wt,
                "plan": pending_plan(plan), "requires_herdr": True}
    identity = lead_identity()
    ctx, herdr_session = identity["context"], identity["session"]
    for role in plan["roles"]:
        if role["kind"] == "hermes":
            profile_check(role["profile"])
    path = state_file(ctx)
    with locked():
        requested_wt = worktree_details(plan, args, False, allow_exists=path.exists(), cwd=cwd)
        if path.exists():
            old = load(path)
            if old.get("version") != 3:
                raise Error("Legacy v4.1 team state cannot be automatically adopted; inspect/close existing sessions and migrate manually")
            # Reusing a team is a state-changing action: a ledger that belongs to
            # another session must never be adopted silently, even if its config
            # happens to match.
            ledger_ownership(old, herdr_session, path)
            if (old.get("template") != args.template or
                    json.dumps(old.get("plan"), sort_keys=True) != json.dumps(plan, sort_keys=True) or
                    old.get("cwd") != str(cwd) or old.get("worktree") != requested_wt):
                raise Error("Existing team has a different resolved config, worktree, or cwd; refusing silent reuse. Explicit migration required")
            if old.get("status") == "partial":
                raise Error("Previous init is partial. Inspect team state and orphan panes before retrying; no automatic duplicate creation.")
            for role in old["roles"]:
                if not verify_existing(role, session=herdr_session):
                    raise Error(f"Registered Agent {role['agent_name']} not live. Do not silently replace; investigate or create a new team.")
            return {"reused": True, "team": old}
        for role in plan["roles"]:
            # 保守上界：pane 后缀可能不止一位（如 w1:p10），按两位估算；
            # 宁可提前拒绝合法边缘长度，也不要建完 pane 才失败。真实 pane id 仍做最终 NAME_RE 校验。
            if len(role["role"]) > 28 - len(ctx["workspace"]):
                raise Error(f"role 名过长，无法保证派生出合法 agent 名（上限按两位 pane 后缀保守估算）: {role['role']}")
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
        roles = []
        team = {"version": 3, "template": args.template, "context": ctx, "session": herdr_session,
                "created_at": now(),
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
                operate("git_worktree_add", wt, lambda: worktree_details(plan, args, True, cwd=cwd, expected=requested_wt))
            if plan["settings"]["layout"]["mode"] == "tab":
                r = operate("tab_create", {"cwd": work_cwd},
                            lambda: herdr("tab", "create", "--workspace", ctx["workspace"], "--cwd", work_cwd,
                                          "--label", "htm-"+args.template, "--no-focus", session=herdr_session))
                root_pane = r["root_pane"]["pane_id"]
            else:
                r = operate("pane_split", {"source": ctx["lead_pane"], "cwd": work_cwd},
                            lambda: herdr("pane", "split", ctx["lead_pane"], "--direction", "right", "--cwd", work_cwd, "--no-focus", session=herdr_session))
                root_pane = r["pane"]["pane_id"]
            pane_ids = [root_pane] + [None]*(len(plan["roles"])-1)
            for src, direction, target in plan["layout_steps"]:
                r = operate("pane_split", {"source": pane_ids[src], "direction": direction, "cwd": work_cwd},
                            lambda: herdr("pane", "split", pane_ids[src], "--direction", direction, "--cwd", work_cwd, "--no-focus", session=herdr_session))
                pane_ids[target] = r["pane"]["pane_id"]
            for idx, role in enumerate(plan["roles"]):
                try:
                    name = agent_name_for(role["role"], pane_ids[idx])
                    require_name_available(name, path, roles, session=herdr_session)
                except Error as e:
                    raise Error(f"Pane {pane_ids[idx]} already created; agent not named or started: {e}") from e
                info = {**role, "pane_id": pane_ids[idx], "agent_name": name, "cwd": work_cwd}
                roles.append(info)
                # Persist the pane/name binding before attempting agent start.
                save(path, team)
                detail = {"name": name, "pane": pane_ids[idx], "kind": role["kind"]}
                def start():
                    launch_args = ["--", *role["launch_args"]] if role["launch_args"] else []
                    result, stats = start_agent_when_ready(
                        name, role["kind"], pane_ids[idx], ctx["workspace"], pane_ids, launch_args, herdr_session=herdr_session)
                    detail["readiness_attempts"] = stats["attempts"]
                    detail["readiness_shell_polls"] = stats["shell_polls"]
                    detail["readiness_busy_retries"] = stats["busy_retries"]
                    return result
                operate("agent_start", detail, start)
                found = lookup_agent(name, session=herdr_session)
                session = found.get("agent_session") if isinstance(found, dict) else None
                if (isinstance(session, dict) and
                        all(isinstance(session.get(k), str) and session[k].strip() for k in ("kind", "source", "value")) and
                        len(session["value"].strip()) >= 8):
                    info["agent_session"] = copy.deepcopy(session)
                    save(path, team)
            team["status"] = "ready"
            save(path, team)
        except (Error, KeyError, TypeError) as e:
            team["error"] = str(e)
            save(path, team)
            raise Error(f"Initialization partially applied; inspect state {path} and Herdr panes: {e}") from e
        return {"reused": False, "team": team}


def adopt(cfg, args):
    identity = lead_identity()
    ctx, herdr_session = identity["context"], identity["session"]
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
        if not NAME_RE.fullmatch(name):
            raise Error(f"Invalid agent name: {name!r}")
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
            if run_path.name.endswith(".driver.json"):
                continue
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
        found = lookup_agent(name, session=herdr_session)
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
        result = herdr("pane", "get", pane_id, session=herdr_session)
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
    team = {"version": 3, "template": args.template, "context": ctx, "session": herdr_session,
            "created_at": now(),
            "status": "ready", "roles": roles, "plan": plan, "cwd": roles[0]["cwd"],
            "worktree": None, "resources": [], "pending_operation": None, "adopted": True}
    if args.dry_run:
        return {"dry_run": True, "no_mutations": True, "team": team}
    with locked():
        check_fresh()
        check_reservations(roles)
        for role in roles:
            found = lookup_agent(role["agent_name"], session=herdr_session)
            pane = found.get("pane") if isinstance(found, dict) else None
            pane_id = (found.get("pane_id") or (pane.get("pane_id") if isinstance(pane, dict) else None)) if isinstance(found, dict) else None
            session = found.get("agent_session") if isinstance(found, dict) else None
            if (pane_id != role["pane_id"] or agent_kind_of(found) != role["kind"] or
                    not isinstance(session, dict) or session.get("value") != role["agent_session"]["value"]):
                raise Error(f"Agent {role['agent_name']} changed during adopt; refusing registration")
        save(path, team)
    return {"adopted": True, "team": team}


def _current_team_with_session(tier="C"):
    identity = lead_identity()
    ctx = identity["context"]
    path = state_file(ctx)
    if not path.exists():
        raise Error("No registered team for this Lead pane; use init first")
    team = load(path)
    ownership = ledger_ownership(team, identity["session"], path)
    if tier == "C":
        require_roles_owned(team, identity["session"], path)
    return team, identity["session"], ownership


def current_team(tier="C"):
    return _current_team_with_session(tier=tier)[0]


def name_lead(args):
    expected = getattr(args, "expect_session", None)
    if args.apply and (not isinstance(expected, str) or not expected.strip()):
        raise Error("先跑一次预览，把输出里的 agent_session.value 传回 --expect-session")
    identity = lead_identity()
    ctx, herdr_session = identity["context"], identity["session"]
    path = state_file(ctx)
    team = load(path) if path.exists() else None
    if team is not None and (team.get("context") != ctx or team.get("status") != "ready"):
        raise Error("Team context mismatch or team is not ready")
    pane_id = ctx["lead_pane"]
    name = agent_name_for("lead", pane_id)
    pane_result = herdr("pane", "get", pane_id, session=herdr_session)
    pane = pane_result.get("pane") if isinstance(pane_result, dict) else None
    if not isinstance(pane, dict) or pane.get("pane_id") != pane_id or pane.get("workspace_id") != ctx["workspace"] or not pane.get("agent"):
        raise Error("Lead pane has no verifiable agent")
    found = agent_data(herdr("agent", "get", pane_id, session=herdr_session))
    if not isinstance(found, dict) or found.get("pane_id") != pane_id:
        raise Error("Lead agent identity or pane cannot be verified")
    kind = agent_kind_of(pane)
    pane_session = pane.get("agent_session")
    session = found.get("agent_session")
    value = session.get("value") if isinstance(session, dict) else None
    if (not kind or kind != agent_kind_of(found) or not isinstance(pane_session, dict) or
            not isinstance(value, str) or not value.strip() or len(value) < 8 or
            pane_session.get("value") != value):
        raise Error("Lead agent session identity or kind cannot be verified")
    if expected is not None and expected != value:
        raise Error("Expected session does not match lead pane")
    current = found.get("name")
    if current is not None and (not isinstance(current, str) or not NAME_RE.fullmatch(current)):
        raise Error("Lead agent name is invalid")
    if args.current and args.current != current:
        raise Error("Specified current agent does not match lead pane")
    if current == name:
        raise Error("Lead agent already has the derived name")
    require_name_available(name, path, team.get("roles", []) if team is not None else (), session=herdr_session)
    if args.apply:
        def same_identity():
            actual = agent_data(herdr("agent", "get", pane_id, session=herdr_session))
            actual_session = actual.get("agent_session") if isinstance(actual, dict) else None
            return (isinstance(actual, dict) and actual.get("pane_id") == pane_id and
                    agent_kind_of(actual) == kind and isinstance(actual_session, dict) and
                    actual_session.get("value") == value)
        with locked():
            latest = load(path) if path.exists() else None
            if ((latest is None) != (team is None) or
                    (latest is not None and (latest.get("context") != ctx or latest.get("status") != "ready"))):
                raise Error("Team context mismatch or team is not ready")
            if not same_identity():
                raise Error("Lead agent identity changed before rename")
            herdr("agent", "rename", pane_id, name, session=herdr_session)
            try:
                verified_after = same_identity()
            except Error as e:
                raise Error("rename 后身份无法核验，需人工核对") from e
            if not verified_after:
                raise Error("rename 后身份已变化，需人工核对")
    return {"dry_run": not args.apply, "current": current, "new": name, "pane_id": pane_id,
            "team": str(path) if team is not None else None,
            "agent_session": {k: session.get(k) for k in ("kind", "source", "value")}, "verified": True}


IDENTITY_TIERS = ("A", "B", "C-", "C")


def _require_run_with_session(runid, tier="C", target_role=None):
    """Load a Run and enforce ledger ownership for the caller's tier.

    Tiers, by what the caller is about to do:

      'A'  read-only, no state written. No ownership check.
      'B'  fail-safe stop (pause). Tolerates an unprovable ledger.
      'C-' writes that exist *because* Workers died (diagnose, abandon,
           recover-attempt). Rejects any identity contradiction, but does not
           require a live UUID anchor -- demanding a survivor here would make
           recovery impossible exactly when it is needed.
      'C'  state-changing operations whose execution depends on the Workers
           being who the ledger says (dispatch, collect, gate, finish, cancel
           of a Run with execution history, resume). Requires a live UUID
           anchor as well as no contradiction.

    A matching session name is never sufficient on its own at any tier, since
    the session may have been recreated under the same name.
    """
    path = run_file(runid)
    data = load(path)
    if data.get("version") != 3:
        raise Error("Legacy Run state is not compatible with v4.2 result protocol; do not resend tasks automatically")
    identity = lead_identity()
    ctx = identity["context"]
    if data.get("context") != ctx:
        raise Error("Run belongs to a different Lead pane/session")
    ownership = ledger_ownership(data, identity["session"], path)
    if tier in ("C", "C-"):
        require_roles_owned(data, identity["session"], path,
                            require_anchor=(tier == "C"))
        if target_role is not None:
            require_target_role(data, identity["session"], target_role, path)
    return data, identity["session"], ownership


def require_run(runid, tier="A", target_role=None):
    """Load a Run for callers that do not need the Herdr session.

    Defaults to read-only tier so that purely informational paths never gain
    state-changing authority by accident.
    """
    return _require_run_with_session(runid, tier=tier, target_role=target_role)[0]


def run_start(cfg, args):
    with locked():
        team, herdr_session, _own = _current_team_with_session(tier="C")
        if team["status"] != "ready": raise Error("Team is not ready")
        for role in team["roles"]:
            if not verify_existing(role, session=herdr_session): raise Error(f"Agent {role['agent_name']} not available")
        names = {(team["context"]["workspace"], r["pane_id"]) for r in team["roles"]}
        for existing_path in (STATE / "runs").glob("*.json"):
            if existing_path.name.endswith(".driver.json"):
                continue
            other = load(existing_path)
            if other.get("status") in ("finished", "cancelled"):
                continue
            used = {(other.get("context", {}).get("workspace"), r.get("pane_id")) for r in other.get("roles", [])}
            if names & used:
                raise Error(f"Worker(s) reserved by unfinished Run {other.get('id')}; inspect/resume it, do not start a conflicting Run")
        runid = uuid.uuid4().hex[:12]
        run = {"version": 3, "id": runid, "team": team["template"], "context": team["context"],
               "session": herdr_session, "goal": args.goal,
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
        # Unknown/unclassifiable observation: never settles, even with a valid report.
        data["status"] = "uncertain"
        data["error"] = f"Unexpected agent status: {observed_status}"
        return data
    try:
        report, digest = read_result(run, rec)
    except Error as e:
        # An observed blocked with no valid business report is NOT a business
        # blocked result: it is unresolved. Storing `blocked` here was the
        # permanent lock described in Issue #2 (neither abandonable nor
        # gateable). Keep the observation, classify as uncertain.
        data["status"] = "uncertain"
        data["error"] = str(e)
        if observed_status == "blocked":
            data["diagnosis"] = "blocked_without_valid_report"
        return data
    data.update({"status": "blocked" if observed_status == "blocked" or report["status"] == "blocked" else "settled",
                 "report": report, "artifact_sha256": digest, "result_file": str(result_path(run["id"], rec["id"]))})
    data["error"] = None
    return data


# --- Issue #2: evidence retention, diagnosis, controlled abandon -------------
#
# First release scope: only the `active` Run state is covered. Every path that
# needs an external trusted execution-fence receipt, independent Owner
# authorization or a human authentication service is NOT implemented and fails
# closed with a stable reason code (see REASON_CODES / check_transition).

REASON_CODES = (
    "run_terminal", "stale_snapshot", "run_state_forbidden", "history_unresolved",
    "fault_group_open", "dispatch_unresolved", "recovery_authorization_invalid",
    "recovery_budget_exhausted", "identity_mismatch", "execution_not_fenced",
    "side_effects_unknown", "approval_required", "artifact_invalid",
    "parallel_limit", "policy_forbidden",
)
UNRESOLVED = ("submission_uncertain", "submitted_unsettled", "uncertain")
DECIDED = ("settled", "blocked", "abandoned")
STATUS_HINTS = {
    "run_terminal": "Run is already finished/cancelled; inspect only",
    "stale_snapshot": "Ledger changed; re-read and re-run the same command",
    "run_state_forbidden": "This operation is not allowed in the current Run status",
    "history_unresolved": "A historical attempt is unresolved; diagnose it before finishing",
    "fault_group_open": "A recovery fault group is open; resolve it as a whole",
    "dispatch_unresolved": "A dispatch for the current attempt is unresolved",
    "recovery_authorization_invalid": "No valid recovery authorization (first release: never issued)",
    "recovery_budget_exhausted": "Recovery budget is exhausted; escalate to the Owner",
    "identity_mismatch": "Worker identity no longer matches the ledger binding",
    "execution_not_fenced": "No trusted stop/isolation receipt; escalate (first release: unsupported)",
    "side_effects_unknown": "Side effects or idempotency cannot be proven; escalate",
    "approval_required": "Independent Owner approval is required (first release: disabled)",
    "artifact_invalid": "Artifact identity, hash or verdict does not satisfy the gate",
    "parallel_limit": "max_parallel reached; collect pending dispatches first",
    "policy_forbidden": "Policy/effect/confirmation check failed",
}


class GuardError(Error):
    """Stable, machine-readable refusal. Not a success and never a retry hint."""

    def __init__(self, code, detail=""):
        if code not in REASON_CODES:
            raise AssertionError(f"unknown reason code {code}")
        self.code = code
        self.detail = detail
        super().__init__(f"[{code}] {detail or STATUS_HINTS[code]}")


def is_current(run, rec):
    return (rec.get("stage_index") == run.get("stage_index") and
            rec.get("stage_attempt", 1) == run.get("stage_attempt", 1))


def pending_dispatches(run, statuses=UNRESOLVED):
    """Current-attempt dispatches only. Historical ones are decided records."""
    return [d for d in run.get("dispatches", []) if is_current(run, d) and d.get("status") in statuses]


def check_transition(run, operation, rec=None, allow_history=False):
    """Shared pre-check used by every write entry point.

    Returns normally when the operation may proceed; raises GuardError with a
    stable reason code otherwise. Read-only commands call the same function and
    catch GuardError to report a diagnosis, never to obtain write permission.

    Deliberately conservative: anything requiring an external fence, an
    independent authorization or a human attestation is refused here so that no
    entry point can accidentally bypass it.
    """
    if run.get("status") in ("finished", "cancelled"):
        raise GuardError("run_terminal")
    if operation == "cancel" and run.get("status") not in (
            "active", "blocked", "blocked_round_limit", "paused", "recovery_active"):
        raise GuardError("run_state_forbidden",
                         f"Run status {run.get('status')!r} cannot be cancelled")
    if operation == "recover-attempt":
        # T7: only an active Run, or a blocked_round_limit Run whose technical
        # over-limit is externally verifiable (it is not in this release), may
        # enter recovery. recovery_active may not open a second chain.
        if run.get("status") not in ("active", "blocked_round_limit"):
            raise GuardError("run_state_forbidden",
                             f"Run status {run.get('status')!r} cannot enter recovery")
    elif operation not in ("diagnose", "list", "recover", "cancel"):
        # `cancel` is the designed escape hatch for EVERY non-terminal Run
        # (design T17). Without this exemption, blocked / paused /
        # blocked_round_limit Runs cannot be closed at all — exactly the
        # lock-up Issue #2 exists to remove. run_cancel still enforces its own
        # unresolved-dispatch and Worker identity/fencing checks, so safety is
        # not weakened here.
        if run.get("status") not in ("active", "recovery_active"):
            raise GuardError("run_state_forbidden",
                             f"Run status {run.get('status')}; this operation requires an active Run")
    if rec is not None:
        if operation == "abandon" and not is_current(run, rec):
            raise GuardError("history_unresolved",
                             "historical dispatches are decided records; diagnose and escalate, "
                             "never re-abandon")
        if not allow_history and not is_current(run, rec):
            raise GuardError("stale_snapshot",
                             f"dispatch {rec.get('id')} belongs to stage {rec.get('stage_index')}"
                             f"/attempt {rec.get('stage_attempt', 1)}, not the current attempt")


def result_snapshot(run, rec):
    """Describe the bound artifact without trusting it. Never follows symlinks."""
    path = result_path(run["id"], rec["id"])
    info = {"path": str(path), "exists": False, "kind": None, "size": None,
            "sha256": None, "parsed": False, "identity": None, "error": None,
            "valid": False, "verdict": None, "report_status": None}
    try:
        st = path.lstat()
    except FileNotFoundError:
        info["error"] = "missing"
        return info
    except OSError as e:
        info["error"] = f"lstat failed: {e}"
        return info
    info["exists"] = True
    info["size"] = st.st_size
    if stat.S_ISLNK(st.st_mode):
        info["kind"] = "symlink"
        info["error"] = "symlink is not followed"
        return info
    if not stat.S_ISREG(st.st_mode):
        info["kind"] = "special"
        info["error"] = "not a regular file"
        return info
    info["kind"] = "regular"
    try:
        content = path.read_bytes()
    except OSError as e:
        info["error"] = f"unreadable: {e}"
        return info
    info["sha256"] = hashlib.sha256(content).hexdigest()
    if st.st_size > 1024 * 1024:
        info["error"] = "larger than 1 MiB"
        return info
    try:
        doc = json.loads(content)
    except ValueError as e:
        info["error"] = f"invalid JSON: {e}"
        return info
    info["parsed"] = True
    if isinstance(doc, dict):
        info["identity"] = {k: doc.get(k) for k in
                            ("run_id", "dispatch_id", "role", "stage", "stage_attempt", "status", "verdict")}
    try:
        report, digest = read_result(run, rec)
    except Error as e:
        info["error"] = str(e)
        return info
    info.update({"valid": True, "verdict": report.get("verdict"), "report_status": report.get("status"),
                 "sha256": digest, "error": None})
    return info


def classify_diagnosis(presence, snapshot):
    """Four-way diagnosis from observable evidence only (design §5.1).

    No trusted execution receipt exists in this release, so `completed_report_lost`
    and `not_executed` can never be asserted: a missing artifact next to an
    absent OR present Worker, and a corrupt artifact next to a present Worker,
    all remain `undetermined`. Guessing would let an unverifiable observation
    become recovery evidence.
    """
    if snapshot.get("valid"):
        return "report_present"
    return "undetermined"


def collect_evidence(run, rec, presence, snapshot, trigger, git_summary=None):
    """Persist an immutable evidence snapshot. Original artifact is never moved."""
    root = STATE / "evidence" / run["id"] / rec["id"]
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    snapshot_id = now().replace(":", "").replace("-", "")[:15] + "-" + uuid.uuid4().hex[:6]
    dest = root / snapshot_id
    dest.mkdir(mode=0o700)  # exclusive: never reuses an existing evidence directory
    artifact_copy = None
    if snapshot.get("exists") and snapshot.get("kind") == "regular":
        src = result_path(run["id"], rec["id"])
        try:
            with open(src, "rb") as f:
                raw = f.read()
            artifact_copy = dest / "artifact.raw"
            fd = os.open(artifact_copy, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(raw)
                f.flush(); os.fsync(f.fileno())
            if hashlib.sha256(artifact_copy.read_bytes()).hexdigest() != hashlib.sha256(raw).hexdigest():
                raise Error("evidence copy failed digest re-read")
        except (OSError, Error) as e:
            artifact_copy = None
            snapshot = dict(snapshot, copy_error=str(e))
    manifest = {
        "run_id": run["id"], "dispatch_id": rec["id"], "snapshot_id": snapshot_id,
        "captured_at": now(), "trigger": trigger,
        "role": rec.get("role"), "stage_index": rec.get("stage_index"),
        "stage_attempt": rec.get("stage_attempt", 1), "dispatch_status": rec.get("status"),
        "prompt": rec.get("prompt"),
        "presence": presence, "artifact": snapshot, "artifact_copy": str(artifact_copy) if artifact_copy else None,
        "git": git_summary,
        "limits": "First release: no trusted execution fence, no independent Owner authorization, "
                  "no human authentication service. Evidence proves what was observed, not that work stopped.",
    }
    fd = os.open(dest / "manifest.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
        f.write("\n")
        f.flush(); os.fsync(f.fileno())
    return {"snapshot_id": snapshot_id, "dir": str(dest),
            "manifest_sha256": hashlib.sha256((dest / "manifest.json").read_bytes()).hexdigest(),
            "artifact_copy": str(artifact_copy) if artifact_copy else None}


def git_evidence(rec):
    """Read-only diff summary for the bound worktree. Never resets or cleans."""
    cwd = rec.get("worktree") or rec.get("cwd")
    if not cwd or not Path(cwd).is_dir():
        return {"available": False, "reason": "worktree/cwd unknown or missing"}
    result = {}
    for name, argv in (("head", ["rev-parse", "HEAD"]),
                       ("status", ["status", "--porcelain"]),
                       ("diffstat", ["diff", "--stat"])):
        try:
            out = process(["git", "-C", str(cwd), *argv], raw=True, env=clean_git_env(), timeout=20)
        except Error as e:
            result[name] = f"unavailable: {e}"
            continue
        result[name] = out[-4000:]
    return {"available": True, "cwd": str(cwd), **result}


def diagnose_dispatch(run_id, dispatch_id):
    """Read-only: classify one unresolved dispatch and record evidence.

    Tier A on purpose. Diagnose is the tool you reach for precisely when an agent
    has been replaced or is unreachable, so refusing it on an identity
    contradiction would remove the only way to inspect such a ledger. It cannot
    change Run state, and the snapshot it writes is not self-authorising:
    `abandon` re-validates the target under tier C- before consuming it.
    """
    with locked():
        run = require_run(run_id, tier="A")
        rec = next((x for x in reversed(run.get("dispatches", [])) if x["id"] == dispatch_id), None)
        if rec is None:
            raise Error("Unknown dispatch ID")
        check_transition(run, "diagnose", rec, allow_history=True)
        role = get_role(run, rec["role"])
        presence = agent_presence(role, session=run.get("session"))
        snapshot = result_snapshot(run, rec)
        classification = classify_diagnosis(presence, snapshot)
        git = git_evidence(rec)
        evidence = collect_evidence(run, rec, presence, snapshot, "diagnose", git)
        return {"dispatch_id": dispatch_id, "status": rec["status"], "stage_index": rec.get("stage_index"),
                "stage_attempt": rec.get("stage_attempt", 1), "is_current": is_current(run, rec),
                "presence": presence, "artifact": snapshot, "diagnosis": classification,
                "evidence": evidence,
                "note": "Read-only diagnosis: no ledger state changed. Trusted fence and owner "
                        "authorization are unavailable in this release."}


def validate_abandon_target(run, rec, quarantine_invalid):
    """Return (presence, snapshot) or raise GuardError. Caller holds the lock."""
    if not is_current(run, rec):
        raise GuardError("history_unresolved",
                         "historical dispatches are decided records; run diagnose and escalate")
    if rec.get("status") not in UNRESOLVED:
        raise GuardError("dispatch_unresolved", f"dispatch is already {rec.get('status')}")
    snapshot = result_snapshot(run, rec)
    if snapshot.get("valid"):
        raise GuardError("artifact_invalid",
                         "a valid identity-bound report exists; collect and gate it instead of abandoning")
    if snapshot.get("exists") and snapshot.get("kind") == "regular":
        if not quarantine_invalid:
            raise GuardError("artifact_invalid",
                             "invalid artifact present; pass --quarantine-invalid to retain bytes and abandon")
    elif snapshot.get("exists"):
        raise GuardError("artifact_invalid",
                         f"artifact is a {snapshot.get('kind')}; special files and symlinks fail closed")
    return snapshot


def run_abandon(args):
    """Explicit irreversible abandonment of ONE unresolved dispatch's result.

    Requires a prior `run diagnose` snapshot (`--evidence`). Refuses whenever a
    valid report exists. Does not dispatch, approve or claim success.
    """
    with locked():
        # Tier C-: abandon is irreversible and consumes a diagnose snapshot. It
        # must work when the Worker is gone, so it rejects contradictions but
        # does not demand a live UUID.
        run = require_run(args.run_id, tier="C-")
        rec = next((x for x in reversed(run.get("dispatches", [])) if x["id"] == args.dispatch_id), None)
        if rec is None:
            raise Error("Unknown dispatch ID")
        check_transition(run, "abandon", rec)
        if len(args.reason.strip()) < 12:
            raise GuardError("policy_forbidden", "abandon requires a specific --reason (12+ chars)")
        role = get_role(run, rec["role"])
        presence = agent_presence(role, session=run.get("session"))
        snapshot = validate_abandon_target(run, rec, args.quarantine_invalid)
        evidence_dir = STATE / "evidence" / run["id"] / rec["id"] / args.evidence
        if not (evidence_dir / "manifest.json").is_file():
            raise GuardError("policy_forbidden", f"unknown evidence snapshot {args.evidence!r}")
        if presence["state"] == "present" and presence.get("status") == "working":
            raise GuardError("execution_not_fenced", "Worker is present and working; cannot abandon while it is working")
        if presence["state"] != "confirmed_absent" and not args.confirm_untracked:
            raise GuardError("execution_not_fenced",
                             "Worker presence is not proven absent; trusted execution isolation is "
                             "unavailable in this release. Pass --confirm-untracked to record an "
                             "explicit, audited risk acceptance — the ledger will mark it as such")
        evidence = collect_evidence(run, rec, presence, snapshot, "abandon", git_evidence(rec))
        before = {k: rec.get(k) for k in ("status", "observed_status", "error", "diagnosis")}
        rec.update({"status": "abandoned", "abandoned_at": now(), "abandon_reason": args.reason,
                    "abandon_evidence": evidence["dir"], "abandon_manifest_sha256": evidence["manifest_sha256"],
                    "abandon_presence": presence, "abandon_fence_verified": False,
                    "abandon_owner_authorized": False,
                    "abandon_risk_accepted": bool(args.confirm_untracked)})
        run["events"].append({
            "at": now(), "type": "abandon", "dispatch_id": rec["id"], "stage": run["stages"][rec["stage_index"]],
            "stage_index": rec["stage_index"], "stage_attempt": rec.get("stage_attempt", 1),
            "operator": _local_operator(), "reason": args.reason,
            "evidence_dir": evidence["dir"], "manifest_sha256": evidence["manifest_sha256"],
            "presence": presence, "observed_status": rec.get("observed_status"),
            "artifact": snapshot, "before": before,
            "fence_verified": False, "owner_authorized": False,
            "risk_accepted": bool(args.confirm_untracked),
            "note": "Result eligibility terminated. Not a success and not proof the Worker stopped.",
        })
        save(run_file(args.run_id), run)
    return {"dispatch_id": rec["id"], "status": "abandoned", "stage_index": rec["stage_index"],
            "stage_attempt": rec.get("stage_attempt", 1), "evidence": evidence,
            "recovery_authorization": "unavailable",
            "next": "Redispatch requires an independent recovery authorization (not implemented in "
                    "this release). Escalate to the Owner; do not re-send blindly.",
            "warning": "abandoned is final for this dispatch and is not positive evidence"}


def _run_diagnose(args):
    return diagnose_dispatch(args.run_id, args.dispatch_id)


def _local_operator():
    """Best-effort local operator label. NOT a verified identity."""
    try:
        return {"source": "local_process", "user": getpass.getuser()}
    except Exception:  # noqa: BLE001 - operator label must never break the command
        return {"source": "local_process", "user": None}


def abandoned_predecessors(run):
    """Abandoned dispatches in the CURRENT stage_attempt — the reason a plain
    redispatch must not silently consume the remaining retry slot."""
    return [d for d in run["dispatches"] if is_current(run, d) and d.get("status") == "abandoned"]


# --- Issue #2 M2: fault groups, per-role budgets, recovery authorization ------
#
# Scope of this release: fault groups, the R1 per-logical-root role budget, the
# one-shot recovery authorization with per-role slots, and the restricted
# recovery_active transitions. Every path that needs an external trusted
# execution control plane stays unavailable in production: TRUSTED_CONTROL is
# None here and only an isolated test may install a fake. CLI flags, local
# JSON, booleans or risk declarations are never accepted as trusted proof, and
# an abandon risk acceptance is never a recovery credential.


TRUSTED_CONTROL = None  # Production stays None: no verified stop/isolation exists.
RECOVERY_ACTIONS = ("recover-attempt", "recovery-dispatch", "recovery-gate")


def trusted_control():
    """Return the isolated test control plane, or fail closed.

    There is no trusted execution control plane in production, so Worker
    identity, isolation and side-effect receipts cannot be verified. Only an
    isolated test replaces this hook with a fake; a fake proves the guards run,
    not that real isolation exists.
    """
    if not callable(TRUSTED_CONTROL):
        raise GuardError("execution_not_fenced",
                         "no trusted execution control plane: stop/isolation, identity and "
                         "side-effect receipts cannot be verified; automatic recovery is unavailable")
    return TRUSTED_CONTROL


def has_valid_report(rec):
    return bool(rec.get("report")) and bool(rec.get("artifact_sha256"))


def role_identity(role):
    """The identity pins that a recovery attempt must re-verify for a role."""
    session = role.get("agent_session") if isinstance(role, dict) else None
    pins = {"agent_name": role.get("agent_name") if isinstance(role, dict) else None,
            "pane_id": role.get("pane_id") if isinstance(role, dict) else None,
            "kind": role.get("kind") if isinstance(role, dict) else None,
            "cwd": role.get("cwd") if isinstance(role, dict) else None,
            "agent_session": session if isinstance(session, dict) else None}
    return pins


def live_identity_problems(role, session=None):
    """Strict observation of the LIVE Agent. The ledger echo is never proof.

    Recovery authorization and slot use must verify the actual Agent: a
    non-empty name, pane/kind, all three session fields, a resolvable cwd and
    (for Hermes) a matching profile. Missing or drifted fields fail closed.
    """
    try:
        found = agent_data(herdr("agent", "get", role["agent_name"], session=session))
    except HerdrError as e:
        return [f"agent get failed: {e.code or 'error'}"]
    except Error as e:
        return [f"herdr unavailable: {e}"]
    if not isinstance(found, dict):
        return ["agent get returned no mapping"]
    problems = []
    if not isinstance(found.get("name"), str) or not found["name"].strip() \
            or found["name"] != role["agent_name"]:
        problems.append("name")
    pane = found.get("pane")
    observed_pane = found.get("pane_id") or (pane.get("pane_id") if isinstance(pane, dict) else None)
    if not observed_pane or observed_pane != role["pane_id"]:
        problems.append("pane")
    if not agent_kind_of(found) or agent_kind_of(found) != role["kind"]:
        problems.append("kind")
    session = found.get("agent_session")
    expected = role.get("agent_session") or {}
    if not (isinstance(session, dict) and all(
            isinstance(expected.get(k), str) and expected[k].strip()
            and session.get(k) == expected[k] for k in ("kind", "source", "value"))):
        problems.append("session")
    cwd = found.get("cwd")
    if not isinstance(cwd, str) or not cwd.strip():
        problems.append("cwd_missing")
    else:
        try:
            if str(Path(cwd).resolve()) != str(Path(role.get("cwd") or "").resolve()):
                problems.append("cwd")
        except (OSError, RuntimeError):
            problems.append("cwd_unresolvable")
    if role.get("kind") == "hermes":
        profile = found.get("profile")
        if not isinstance(profile, str) or not profile.strip() or profile != (role.get("profile") or ""):
            problems.append("profile")
    return problems


def require_live_identity(run, role_name):
    """Raise identity_mismatch unless the live Agent matches its binding."""
    role = get_role(run, role_name)
    if not identity_pins_complete(role):
        raise GuardError("identity_mismatch",
                         f"role {role_name} has incomplete identity pins in the ledger")
    problems = live_identity_problems(role, session=run.get("session"))
    if problems:
        raise GuardError("identity_mismatch",
                         f"role {role_name} live identity does not match: {','.join(problems)}")
    return role


def identity_pins_complete(role):
    pins = role_identity(role)
    if not all(isinstance(pins[k], str) and pins[k].strip()
               for k in ("agent_name", "pane_id", "kind", "cwd")):
        return False
    session = pins["agent_session"]
    return bool(isinstance(session, dict) and
                all(isinstance(session.get(k), str) and session[k].strip()
                    for k in ("kind", "source", "value")))


def logical_root(run):
    """Attempt number at which the CURRENT logical root started.

    Derived from real recovery evidence, not from a field any writer could
    leave stale: an attempt created by `recover-attempt` keeps the root of the
    authorization that targets it; every other attempt is its own root. So a
    business revise starts a new root while a technical retry accumulates on
    the same one, and the R1 budget never refunds.
    """
    for auth in reversed(run.get("recovery_authorizations", [])):
        if auth.get("stage_index") == run["stage_index"] \
                and auth.get("target_attempt") == run["stage_attempt"]:
            return auth["root_attempt"]
    return run["stage_attempt"]


def dispatch_root(rec):
    return rec.get("root_attempt") or rec.get("stage_attempt", 1)


def root_members(run, stage_index=None, root=None):
    stage_index = run["stage_index"] if stage_index is None else stage_index
    root = logical_root(run) if root is None else root
    return [d for d in run["dispatches"]
            if d.get("stage_index") == stage_index and dispatch_root(d) == root]


def member_digest_entry(rec):
    """What a member contributes to the immutable authorization snapshot."""
    report = rec.get("report") if isinstance(rec.get("report"), dict) else {}
    return [rec["id"], rec.get("role"), rec.get("status"), rec.get("stage_attempt", 1),
            rec.get("artifact_sha256"), report.get("verdict"), report.get("status")]


def group_digest(stage_index, root, members):
    payload = json.dumps({"stage_index": stage_index, "root_attempt": root,
                          "members": sorted(member_digest_entry(d) for d in members)},
                         sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def fault_group_for(run, stage_index=None, root=None, open_only=False):
    stage_index = run["stage_index"] if stage_index is None else stage_index
    root = logical_root(run) if root is None else root
    for group in run.get("fault_groups", []):
        if (group.get("stage_index") == stage_index and group.get("root_attempt") == root
                and (not open_only or group.get("status") == "open")):
            return group
    return None


def group_triggered(members):
    """A fault group opens on a decided fault, never on a normal in-flight submission.

    Triggers: an abandoned member, a blocked member with no valid report, or an
    uncertain member that has already been observed/collected into a technical
    fault. A plain `submission_uncertain`/`submitted_unsettled` still awaiting
    its first collect is in flight, not a group.
    """
    for d in members:
        if d.get("status") == "abandoned":
            return True
        if d.get("status") == "blocked" and not has_valid_report(d):
            return True
        if d.get("status") == "uncertain" and (d.get("collected_at") or d.get("error")
                                               or d.get("diagnosis")):
            return True
    return False


def sync_group_members(run, group):
    """Refresh a group's dynamic member list from the root. Caller holds the lock.

    `authorized_members` is the immutable source snapshot an authorization is
    bound to; `members` is the live membership and always includes every
    recovery dispatch added afterwards, so a new fault can never be forgotten.
    """
    if not group.get("recorded", True):
        return group
    members = root_members(run, group["stage_index"], group["root_attempt"])
    ids = [d["id"] for d in members]
    fresh = [i for i in ids if i not in group.get("members", [])]
    if fresh:
        group.setdefault("members", []).extend(fresh)
        group.setdefault("events", []).append(
            {"at": now(), "type": "member_added", "dispatch_ids": fresh})
    if "authorized_members" not in group:
        group["authorized_members"] = list(group.get("members", []))
    return group


def current_group(run):
    """The open fault group for the current logical root, without mutating.

    Used by every read/precheck path so that merely looking at a Run never
    records a group. A triggered root without a persisted group is reported as
    a transient group; only a write path may persist it.
    """
    stage_index, root = run["stage_index"], logical_root(run)
    recorded = fault_group_for(run, stage_index, root, open_only=True)
    if recorded is not None:
        # The dynamic view always includes later recovery dispatches. This is
        # an in-memory refresh: only a write path persists it.
        return sync_group_members(run, recorded)
    members = root_members(run, stage_index, root)
    if not group_triggered(members):
        return None
    return {"id": f"unrecorded-{stage_index}-{root}", "stage_index": stage_index,
            "root_attempt": root, "status": "open", "recorded": False,
            "members": [d["id"] for d in members],
            "authorized_members": [d["id"] for d in members],
            "member_snapshot_sha256": group_digest(stage_index, root, members),
            "events": []}


def record_group(run, group):
    """Persist a transient fault group. Callers only call this on a write path."""
    existing = fault_group_for(run, group["stage_index"], group["root_attempt"])
    if existing is not None:
        return sync_group_members(run, existing)
    members = root_members(run, group["stage_index"], group["root_attempt"])
    persisted = {k: v for k, v in group.items() if k != "recorded"}
    persisted.update({"id": uuid.uuid4().hex[:12], "status": "open", "opened_at": now(),
                      "members": [d["id"] for d in members],
                      "authorized_members": list(group.get("authorized_members", [d["id"] for d in members])),
                      "member_snapshot_sha256": group_digest(
                          group["stage_index"], group["root_attempt"], members),
                      "events": [{"at": now(), "type": "opened",
                                  "reason": sorted({d.get("status") for d in members
                                                    if d.get("status") in ("uncertain", "abandoned")})}]})
    run.setdefault("fault_groups", []).append(persisted)
    return persisted


def group_has_abandoned(run, group):
    by_id = {d["id"]: d for d in run["dispatches"]}
    return any(by_id.get(i, {}).get("status") == "abandoned" for i in group.get("members", []))


def assert_members_classified(run, group):
    """Every live member must be a decided record before a group may be used.

    Delivery-unknown records are never closed by a timeout: they must be
    collected into a valid report or safely abandoned first.
    """
    by_id = {d["id"]: d for d in run["dispatches"]}
    open_ids = [i for i in group.get("members", [])
                if by_id.get(i, {}).get("status") not in ("settled", "abandoned")
                and not (by_id.get(i, {}).get("status") == "blocked" and has_valid_report(by_id.get(i, {})))]
    if open_ids:
        raise GuardError("dispatch_unresolved",
                         f"fault group still has unclassified members: {open_ids}")


def required_roles(run, group):
    """stage actor (never lead) + every role dispatched in the root + policy roles."""
    roles = set()
    stage = stage_info(run)
    if stage.get("actor") and stage["actor"] != "lead":
        roles.add(stage["actor"])
    by_id = {d["id"]: d for d in run["dispatches"]}
    for member in group.get("members", []):
        rec = by_id.get(member)
        if rec and rec.get("role"):
            roles.add(rec["role"])
    for role in run["settings"].get("recovery", {}).get("required_roles", []):
        roles.add(role)
    for role in run.get("recovery_policy_roles", []):
        roles.add(role)
    return sorted(roles)


def role_budget(run, stage_index, root, role):
    """R1: 1 + max_retries dispatches per logical root per role, never refunded.

    Cumulative across every attempt of the logical root, including recovery
    attempts, so a technical retry can never hand a role a fresh allowance.
    """
    limit = 1 + run["settings"]["orchestration"]["max_retries"]
    used = sum(1 for d in run["dispatches"]
               if d.get("stage_index") == stage_index and dispatch_root(d) == root
               and d.get("role") == role)
    return {"limit": limit, "used": used, "remaining": max(0, limit - used)}


def budget_admits(run, stage_index, root, role, exclude_id=None):
    """True when one MORE dispatch for this role still fits the R1 budget.

    `exclude_id` omits the dispatch being admitted: the slot is consumed after
    the record is appended, and that record must not count against itself.
    """
    limit = 1 + run["settings"]["orchestration"]["max_retries"]
    used = sum(1 for d in run["dispatches"]
               if d["id"] != exclude_id and d.get("stage_index") == stage_index
               and dispatch_root(d) == root and d.get("role") == role)
    return used < limit


def approval_ok(approval):
    """An independent approval record: identity, scope, receipt, timestamps."""
    if not isinstance(approval, dict):
        return False
    if not all(isinstance(approval.get(k), str) and approval[k].strip()
               for k in ("approver", "scope", "receipt", "issued_at", "expires_at")):
        return False
    issued, expires = parse_iso(approval["issued_at"]), parse_iso(approval["expires_at"])
    stamp = parse_iso(now())
    if issued is None or expires is None or stamp is None:
        return False
    if issued > stamp or expires <= issued or expires <= stamp:
        return False
    return True


def parse_iso(value):
    try:
        parsed = dt.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def receipt_failures(receipt, expected, receipt_id=None):
    """Structural + binding check of a trusted control-plane receipt.

    `expected` is the exact binding the receipt must match. Every field is
    compared by value, never by "is a nonempty string": a receipt that names a
    different Run, stage, root, group or action scope is rejected outright.
    """
    if not isinstance(receipt, dict):
        return "trusted control plane returned no record"
    for key in ("receipt_id", "issued_at", "expires_at", "idempotency_key"):
        if not isinstance(receipt.get(key), str) or not receipt[key].strip():
            return f"trusted receipt missing {key}"
    if receipt_id is not None and receipt.get("receipt_id") != receipt_id:
        return "trusted source returned another receipt"
    for key in ("run_id", "stage_index", "root_attempt", "target_attempt", "group_id",
                "group_sha256", "group_members_sha256", "action", "action_fingerprint",
                "effect", "identity_pins_sha256", "idempotency_key", "message_sha256"):
        if receipt.get(key) != expected.get(key):
            return f"trusted receipt does not bind {key}"
    if receipt.get("isolation") != "confirmed":
        return "trusted isolation is not confirmed"
    if receipt.get("side_effects") != "known":
        return "side-effect receipts are not known"
    if receipt.get("revoked") not in (False, None):
        return "trusted receipt was revoked"
    issued, expires = parse_iso(receipt.get("issued_at")), parse_iso(receipt.get("expires_at"))
    stamp = parse_iso(now())
    if issued is None or expires is None or stamp is None:
        return "trusted receipt timestamps are invalid"
    if issued > stamp:
        return "trusted receipt is not yet valid (future issued_at)"
    if expires <= issued or expires <= stamp:
        return "trusted receipt is expired"
    if not approval_ok(receipt.get("approval")):
        return "trusted receipt lacks a valid independent approval"
    if receipt["approval"].get("scope") != expected.get("action"):
        return "trusted approval scope does not cover this action"
    roles = receipt.get("required_roles")
    if not isinstance(roles, list) or sorted(roles) != sorted(expected["required_roles"]):
        return "trusted receipt role set mismatch"
    budgets = receipt.get("role_budgets")
    if not isinstance(budgets, dict) or set(budgets) != set(expected["required_roles"]):
        return "trusted receipt role budgets mismatch"
    for role, want in expected["role_budgets"].items():
        got = budgets.get(role)
        if not isinstance(got, dict) or got.get("remaining") != want["remaining"] \
                or got.get("used") != want["used"] or got.get("limit") != want["limit"]:
            return f"role budget drift for {role}"
    actions = receipt.get("role_actions")
    if not isinstance(actions, dict) or set(actions) != set(expected["required_roles"]):
        return "trusted receipt role actions mismatch"
    for role in expected["required_roles"]:
        want = expected["role_actions"][role]
        got = actions.get(role)
        if not isinstance(got, dict) or got.get("effect") != want["effect"] \
                or got.get("fingerprint") != want["fingerprint"] \
                or got.get("action") != want.get("action") \
                or got.get("action_key") != want.get("action_key"):
            return f"role action binding mismatch for {role}"
    return None


def recovery_logical_message(run, role_name):
    """The message a recovery dispatch must carry for this role.

    Bound by the authorization rather than chosen at dispatch time, so an old
    authorization can never be spent on different parameters.
    """
    return (f"Recovery attempt for stage {run['stages'][run['stage_index']]}: redo the required "
            f"work for role {role_name} and write a fresh bound report.")


def recovery_role_action(run, role_name):
    effect = "write" if role_name == "developer" else "read"
    message = recovery_logical_message(run, role_name)
    role = get_role(run, role_name)
    return {"action": "recovery-dispatch", "effect": effect, "message": message,
            "action_key": action_key(run, role_name, "recovery-dispatch"),
            "fingerprint": action_fingerprint("recovery-dispatch", run, role_name, effect,
                                              role, message=message)}


def root_action_key(run, required):
    """One stable key for the logical recovery action of this root.

    Reused by every attempt of the same root, so a downstream that de-duplicates
    by this key sees one logical action. A different root gets a different key.
    """
    payload = json.dumps({"run_id": run["id"], "stage": run["stages"][run["stage_index"]],
                          "logical_root": logical_root(run), "action": "recovery-redo",
                          "roles": sorted(required)}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def action_key(run, role_name, action):
    """Stable downstream idempotency key: logical action, NOT an attempt or id.

    A recovered redo reuses this key so a downstream that de-duplicates sees one
    logical action, while a different attempt does not mint a fresh identity.
    """
    payload = json.dumps({"run_id": run["id"], "stage": run["stages"][run["stage_index"]],
                          "logical_root": logical_root(run), "role": role_name,
                          "action": action}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def action_fingerprint(action, run, role_name, effect, role, message=None, task=None):
    """Fingerprint the ACTUAL action: parameters, message, effect and identity.

    Contains no stage_attempt: the same logical action keeps the same
    fingerprint across a technical retry, so an old authorization can never be
    reused for different parameters.
    """
    payload = json.dumps({"action": action, "run_id": run["id"],
                          "stage": run["stages"][run["stage_index"]],
                          "logical_root": logical_root(run), "role": role_name,
                          "effect": effect, "message": message,
                          "task": task, "identity": role_identity(role)},
                         sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def pins_digest(pins):
    return hashlib.sha256(json.dumps(pins, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def recovery_binding(run, group, required, action="recover-attempt", target_attempt=None,
                     role_actions=None):
    """The exact binding a trusted receipt must match. One source of truth."""
    target_attempt = run["stage_attempt"] + 1 if target_attempt is None else target_attempt
    root = logical_root(run)
    by_id = {d["id"]: d for d in run["dispatches"]}
    authorized = [by_id[i] for i in group.get("authorized_members", group.get("members", []))
                  if i in by_id]
    role_actions = role_actions or {}
    pins = {}
    for name in required:
        try:
            pins[name] = pins_digest(role_identity(get_role(run, name)))
        except Error:
            pins[name] = None
    message_sha256 = (role_actions or {}).get("__message_sha256__")
    if isinstance(role_actions, dict):
        role_actions = {k: v for k, v in role_actions.items() if k != "__message_sha256__"}
    return {"action": action, "run_id": run["id"], "stage_index": run["stage_index"],
            "root_attempt": root, "target_attempt": target_attempt,
            "group_id": group["id"], "group_sha256": group.get("member_snapshot_sha256"),
            "group_members_sha256": group_digest(run["stage_index"], root, authorized),
            "idempotency_key": root_action_key(run, required),
            "message_sha256": message_sha256,
            "required_roles": list(required),
            "role_budgets": {r: role_budget(run, run["stage_index"], root, r) for r in required},
            "role_actions": role_actions,
            "effect": sorted({a.get("effect") for a in role_actions.values() if a}),
            "identity_pins_sha256": hashlib.sha256(
                json.dumps(pins, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()}


def request_trusted_receipt(run, group, required, action="recover-attempt", target_attempt=None,
                            role_actions=None):
    """Ask the trusted control plane for one bound receipt, then verify it fully.

    Local declarations, booleans and nonempty strings are never proof: the
    returned record is checked field by field against the exact binding, and
    any mismatch, missing field, wrong scope, future issue time, expiry or
    revocation fails closed.
    """
    control = trusted_control()
    expected = recovery_binding(run, group, required, action, target_attempt, role_actions)
    try:
        receipt = control(dict(expected, type="request"))
    except GuardError:
        raise
    except Exception as e:  # noqa: BLE001 - any control-plane failure is a refusal
        raise GuardError("execution_not_fenced", f"trusted control plane unavailable: {e}") from e
    problem = receipt_failures(receipt, expected)
    if problem:
        code = ("approval_required" if problem.startswith("trusted receipt lacks")
                or problem.startswith("trusted approval") else
                "side_effects_unknown" if "side-effect" in problem else
                "execution_not_fenced" if "isolation" in problem or "control plane" in problem else
                "recovery_authorization_invalid")
        raise GuardError(code, problem)
    return receipt, expected


def verify_receipt_still_valid(run, auth, action, role=None, effect=None, role_actions=None):
    """Re-verify the trusted source at the moment of use, not only at issue time."""
    receipt = auth.get("receipt")
    if not isinstance(receipt, dict):
        raise GuardError("recovery_authorization_invalid", "authorization has no trusted receipt")
    expected = dict(auth.get("binding") or {})
    if not expected:
        raise GuardError("recovery_authorization_invalid", "authorization has no bound expectation")
    if action != "recover-attempt":
        # The authorized action set is fixed at issue time. A caller cannot
        # substitute its own role_actions here.
        if role_actions is not None and role_actions != auth.get("role_actions"):
            raise GuardError("recovery_authorization_invalid",
                             "role actions may not be replaced at use time")
        expected = dict(expected, action=action,
                        stage_attempt=run["stage_attempt"],
                        target_attempt=run["stage_attempt"],
                        role_actions=auth.get("role_actions"),
                        effect=sorted({a.get("effect") for a in (auth.get("role_actions") or {}).values()}))
        expected["role_budgets"] = {r: role_budget(run, auth["stage_index"], auth["root_attempt"], r)
                                    for r in expected["required_roles"]}
        if role is not None:
            try:
                expected["identity_pins_sha256"] = hashlib.sha256(json.dumps(
                    {r: pins_digest(role_identity(get_role(run, r)))
                     for r in expected["required_roles"]},
                    sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
            except Error as e:
                raise GuardError("identity_mismatch", str(e)) from e
    control = trusted_control()
    try:
        rechecked = control(dict(expected, type="verify", receipt_id=receipt.get("receipt_id")))
    except GuardError:
        raise
    except Exception as e:  # noqa: BLE001
        raise GuardError("execution_not_fenced", f"trusted control plane unavailable: {e}") from e
    problem = receipt_failures(rechecked, expected, receipt_id=receipt.get("receipt_id"))
    if problem:
        code = ("approval_required" if problem.startswith("trusted receipt lacks")
                or problem.startswith("trusted approval") else "recovery_authorization_invalid")
        raise GuardError(code, problem)
    if rechecked.get("receipt_id") != receipt.get("receipt_id"):
        raise GuardError("recovery_authorization_invalid", "trusted source returned another receipt")
    return rechecked


def recovery_authorization_for(run, authorization_id=None, attempt=None):
    """Return the live authorization for a stage attempt, or None."""
    attempt = run["stage_attempt"] if attempt is None else attempt
    for auth in run.get("recovery_authorizations", []):
        if auth.get("run_id") == run["id"] and auth.get("stage_index") == run["stage_index"] \
                and auth.get("target_attempt") == attempt \
                and auth.get("status") == "active" \
                and (authorization_id is None or auth.get("id") == authorization_id):
            return auth
    return None


def validate_recovery_authorization(run, auth, role=None, dispatch_id=None):
    """Lock-in re-check of every binding. Any mismatch fails closed."""
    if not isinstance(auth, dict):
        raise GuardError("recovery_authorization_invalid", "no recovery authorization")
    if auth.get("run_id") != run["id"] or auth.get("stage_index") != run["stage_index"]:
        raise GuardError("recovery_authorization_invalid", "authorization bound to another Run/stage")
    if auth.get("target_attempt") != run["stage_attempt"]:
        raise GuardError("recovery_authorization_invalid", "authorization bound to another attempt")
    if auth.get("root_attempt") != logical_root(run):
        raise GuardError("recovery_authorization_invalid", "authorization bound to another logical root")
    group = fault_group_for(run, auth["stage_index"], auth["root_attempt"])
    if group is None or group.get("id") != auth.get("group_id"):
        raise GuardError("recovery_authorization_invalid", "fault group changed since authorization")
    if group.get("member_snapshot_sha256") != auth.get("group_sha256"):
        raise GuardError("recovery_authorization_invalid",
                         "fault group members changed since authorization; re-issue recovery")
    if set(required_roles(run, group)) != set(auth.get("required_roles", [])):
        raise GuardError("recovery_authorization_invalid", "required role set changed")
    expires = parse_iso(auth.get("expires_at"))
    stamp = parse_iso(now())
    if expires is None or stamp is None or expires <= stamp:
        raise GuardError("recovery_authorization_invalid", "authorization expired")
    if auth.get("revoked"):
        raise GuardError("recovery_authorization_invalid", "authorization was revoked")
    if role is not None:
        slot = (auth.get("slots") or {}).get(role)
        if not isinstance(slot, dict):
            raise GuardError("recovery_authorization_invalid", f"no recovery slot for role {role}")
        if slot.get("status") == "consumed" and slot.get("dispatch_id") != dispatch_id:
            raise GuardError("recovery_authorization_invalid",
                             f"recovery slot for {role} already consumed by another dispatch")
    return auth


def consume_recovery_slot(run, auth, role, dispatch_id, effect):
    """Atomically occupy one role slot. Caller holds the lock."""
    slot = auth["slots"][role]
    if slot.get("status") == "consumed":
        if slot.get("dispatch_id") == dispatch_id:
            return slot  # idempotent re-entry of the SAME dispatch only
        raise GuardError("recovery_authorization_invalid",
                         f"recovery slot for {role} already consumed")
    want_effect = (auth.get("role_actions") or {}).get(role, {}).get("effect")
    if want_effect is None or want_effect != effect:
        # A read slot can never be spent on a write (or the reverse).
        raise GuardError("recovery_authorization_invalid",
                         f"recovery slot for {role} is bound to effect {want_effect!r}, not {effect!r}")
    if not budget_admits(run, auth["stage_index"], auth["root_attempt"], role, exclude_id=dispatch_id):
        raise GuardError("recovery_budget_exhausted",
                         f"role {role} has no remaining recovery budget on this logical root")
    slot.update({"status": "consumed", "dispatch_id": dispatch_id, "consumed_at": now(),
                 "effect": effect})
    auth.setdefault("events", []).append(
        {"at": now(), "type": "slot_consumed", "role": role, "dispatch_id": dispatch_id,
         "effect": effect})
    return slot


def revoke_slots(auth):
    for slot in (auth.get("slots") or {}).values():
        if slot.get("status") == "unused":
            slot["status"] = "revoked"
    auth["status"] = "revoked"
    auth.setdefault("events", []).append({"at": now(), "type": "revoked"})


def close_fault_group(run, group, proof, auth=None):
    """Close a group and retire EVERY authorization it ever issued.

    A group may own several attempts (each T7 mints its own authorization). On
    a T10 pass all of them must be revoked, otherwise a stale active
    authorization blocks finish forever.
    """
    group["status"] = "closed"
    group["closed_at"] = now()
    group["closure"] = proof
    group.setdefault("events", []).append({"at": now(), "type": "closed", "proof": proof})
    retired = []
    for candidate in run.get("recovery_authorizations", []):
        if candidate.get("group_id") != group.get("id"):
            continue
        if candidate.get("status") == "active":
            revoke_slots(candidate)
        retired.append(candidate.get("id"))
    if auth is not None and auth.get("id") not in retired:
        revoke_slots(auth)
        retired.append(auth.get("id"))
    group["closed_authorization_ids"] = retired
    group["closed_authorization_id"] = retired[-1] if retired else None
    return group


def closure_problems(run, group):
    """A closure is only valid when it names real, still-matching evidence.

    Checked against the ledger, not just against itself: the recorded hashes
    must match the members' actual collected reports, the named gate event must
    exist, and no authorized slot may still be unconsumed.
    """
    proof = group.get("closure")
    if not isinstance(proof, dict):
        return ["missing closure record"]
    problems = []
    for key in ("by", "decision", "group_sha256", "stage_index", "stage_attempt", "evidence",
                "at", "member_hashes_sha256", "gate_event_index"):
        value = proof.get(key)
        if not isinstance(value, str if key not in ("stage_index", "stage_attempt",
                                                    "gate_event_index") else int):
            problems.append(f"closure missing {key}")
    if problems:
        return problems
    if proof["group_sha256"] != group.get("member_snapshot_sha256"):
        problems.append("closure does not match the authorized member snapshot")
    by_id = {d["id"]: d for d in run["dispatches"]}
    members = [by_id[i] for i in group.get("members", []) if i in by_id]
    if group_digest(group["stage_index"], group["root_attempt"], members) != proof["member_hashes_sha256"]:
        problems.append("closure does not match the members' current report hashes")
    index = proof["gate_event_index"]
    events = run.get("events", [])
    if not isinstance(index, int) or index < 0 or index >= len(events):
        problems.append("closure names no gate event")
    else:
        event = events[index]
        if event.get("type") != "gate" or event.get("decision") != proof["decision"] \
                or event.get("stage_index") != proof["stage_index"] \
                or event.get("stage_attempt") != proof["stage_attempt"]:
            problems.append("closure does not match the recorded gate event")
    group_auths = [a for a in run.get("recovery_authorizations", [])
                   if a.get("group_id") == group.get("id")]
    for auth in group_auths:
        if auth.get("status") == "active":
            problems.append(f"closure left recovery authorization {auth.get('id')} active")
        if any(slot.get("status") == "unused" for slot in (auth.get("slots") or {}).values()):
            problems.append(f"closure left an unconsumed slot on {auth.get('id')}")
    return problems


def attempt_authorization_source(run, requested):
    """Resolve an exact same-request authorization before any history refusal.

    R1: a retry of the SAME request must return the same authorization, the
    same slot and the same target attempt — it must never open a new attempt.
    This only recognises an exact match; it never relaxes history writes.
    """
    for auth in reversed(run.get("recovery_authorizations", [])):
        if auth.get("request_source") != requested:
            continue
        if auth.get("status") not in ("active", "revoked", "consumed"):
            continue
        if auth.get("stage_index") != run["stage_index"]:
            continue
        return auth
    return None


def run_recover_attempt(args):
    """T7: retain a NEW attempt and issue per-role one-shot slots. Never prompts."""
    request_source = [args.dispatch_id, args.evidence]
    with locked():
        # Tier A: recovery deliberately runs when a Worker has already been
        # abandoned, so a ledger-level anchor cannot be required. Its own
        # `require_live_identity` check below is per required role and raises a
        # precise `identity_mismatch` for drift; a blunter check here would mask
        # that diagnosis.
        run = require_run(args.run_id, tier="A")
        # A terminal Run is refused before anything else, including an R1 replay:
        # no token may resurrect or act on a finished/cancelled ledger.
        if run.get("status") in ("finished", "cancelled"):
            raise GuardError("run_terminal")
        # R1 idempotency is settled BEFORE the remaining state gates: a
        # same-request retry under recovery_active must return the EXISTING
        # authorization, not be refused or allowed to open a second chain. Only
        # an exact (dispatch, evidence) match qualifies; history writes stay strict.
        replay = attempt_authorization_source(run, request_source)
        if replay is not None:
            return _recovery_result(run, replay, replay=True)
        check_transition(run, "recover-attempt")
        rec = next((x for x in reversed(run.get("dispatches", [])) if x["id"] == args.dispatch_id), None)
        if rec is None:
            raise Error("Unknown dispatch ID")
        if not is_current(run, rec):
            raise GuardError("history_unresolved",
                             "historical dispatches are decided records; diagnose and escalate")
        transient = current_group(run)
        if transient is None:
            raise GuardError("fault_group_open", "no fault group is open for this logical root")
        if not group_has_abandoned(run, transient):
            raise GuardError("recovery_authorization_invalid",
                             "T7 requires at least one safely abandoned member; a pure business group "
                             "must be closed by T21 and decided by an ordinary gate")
        assert_members_classified(run, transient)
        if run["status"] == "recovery_active":
            raise GuardError("run_state_forbidden", "recovery_active cannot open a second recovery chain")
        if run["status"] == "blocked_round_limit":
            # Only a trusted STRUCTURAL technical over-limit may enter recovery.
            # A ledger string is not proof: the trusted control plane must verify
            # the provenance AND confirm there is no business negative.
            technical = run.get("round_limit_provenance")
            if not isinstance(technical, dict) or technical.get("kind") != "technical":
                raise GuardError("run_state_forbidden",
                                 "blocked_round_limit provenance is business or missing; "
                                 "only a technically proven over-limit may recover")
            verify_round_limit_provenance(run, technical, transient)
        if not re.fullmatch(r"[A-Za-z0-9.\-]{1,80}", args.evidence):
            raise GuardError("artifact_invalid", "invalid evidence snapshot id")
        evidence_dir = STATE / "evidence" / run["id"] / rec["id"] / args.evidence
        if not (evidence_dir / "manifest.json").is_file():
            raise GuardError("artifact_invalid", f"unknown evidence snapshot {args.evidence!r}")
        required = required_roles(run, transient)
        if not required:
            raise GuardError("policy_forbidden", "no required role could be derived for this group")
        for name in required:
            # The ledger must be complete AND the live Agent must match it.
            require_live_identity(run, name)
        # Only now, after every precondition passed, persist the group. A
        # refused recovery must not leave a ledger side effect behind.
        group = record_group(run, transient)
        # Re-snapshot on every authorization: `authorized_members` must cover
        # the CURRENT full membership (including earlier recovery dispatches),
        # not the membership at the first T7.
        members_now = root_members(run, group["stage_index"], group["root_attempt"])
        group["authorized_members"] = [d["id"] for d in members_now]
        group["member_snapshot_sha256"] = group_digest(group["stage_index"],
                                                       group["root_attempt"], members_now)
        group.setdefault("events", []).append(
            {"at": now(), "type": "authorization_snapshot",
             "members": list(group["authorized_members"])})
        role_actions = {name: recovery_role_action(run, name) for name in required}
        for name in required:
            if role_budget(run, run["stage_index"], logical_root(run), name)["remaining"] <= 0:
                # Any single role out of budget refuses the WHOLE group: it must
                # not start an attempt it cannot complete.
                raise GuardError("recovery_budget_exhausted",
                                 f"role {name} has no remaining budget on this logical root; "
                                 "the group cannot open a new attempt")
        receipt, expected = request_trusted_receipt(run, group, required,
                                                    role_actions=role_actions)
        target_attempt = run["stage_attempt"] + 1
        auth = {"id": uuid.uuid4().hex[:12], "run_id": run["id"], "stage_index": run["stage_index"],
                "root_attempt": logical_root(run), "target_attempt": target_attempt,
                "group_id": group["id"], "group_sha256": group["member_snapshot_sha256"],
                "authorized_members": sorted(group.get("authorized_members", [])),
                "required_roles": required, "role_actions": role_actions,
                "binding": expected, "receipt": receipt,
                "role_budgets": expected["role_budgets"],
                "slots": {name: {"status": "unused", "dispatch_id": None, "consumed_at": None,
                                 "effect": role_actions[name]["effect"]} for name in required},
                "request_source": request_source,
                "round_limit_provenance": (run.get("round_limit_provenance")
                                           if run["status"] == "blocked_round_limit" else None),
                "issued_at": now(), "expires_at": receipt["expires_at"],
                "status": "active", "events": [{"at": now(), "type": "issued"}]}
        run.setdefault("recovery_authorizations", []).append(auth)
        previous_status = run["status"]
        if previous_status == "blocked_round_limit":
            run["status"] = "recovery_active"
        run["stage_attempt"] = target_attempt
        run["events"].append({"at": now(), "type": "recover_attempt", "group_id": group["id"],
                              "previous_status": previous_status, "stage_index": run["stage_index"],
                              "root_attempt": auth["root_attempt"], "target_attempt": target_attempt,
                              "required_roles": required, "authorization_id": auth["id"],
                              "receipt_id": receipt["receipt_id"], "round": run["round"]})
        save(run_file(args.run_id), run)
    return _recovery_result(run, auth)


def _recovery_result(run, auth, replay=False):
    return {"run_id": run["id"], "stage_index": auth["stage_index"],
            "stage_attempt": auth["target_attempt"], "status": run["status"],
            "group_id": auth["group_id"], "authorization_id": auth["id"],
            "required_roles": auth["required_roles"],
            "slots": {name: slot.get("status") for name, slot in auth["slots"].items()},
            "round": run["round"], "round_unchanged": True, "replay": replay,
            "next": ("The exact same recovery request already issued this authorization; "
                     "no new attempt or slot was created.") if replay else
                    ("dispatch each required role with the ordinary `run dispatch`; each dispatch consumes "
                     "its own slot exactly once. No prompt was sent by this command.")}



def run_dispatch(args):
    run, herdr_session, _own = _require_run_with_session(
        args.run_id, tier="C", target_role=args.role)
    check_transition(run, "dispatch")
    # A fault group freezes ordinary dispatch. Only a dispatch covered by the
    # current recovery authorization's per-role one-shot slot may proceed.
    group = current_group(run)
    auth = None
    if group is not None:
        auth = recovery_authorization_for(run)
        if auth is None:
            # Issue #2 P0: the retry budget alone would let a plain dispatch
            # bypass the evidence/isolation/authorization that abandon
            # required. Without a valid recovery authorization this always
            # refuses; a risk acceptance is never a recovery credential.
            raise GuardError("recovery_authorization_invalid",
                             "this stage attempt has an open fault group or an abandoned dispatch; "
                             "a verified, bound recovery authorization is required. Run "
                             "`run diagnose` and `run recover-attempt`, or escalate to the Owner")
        validate_recovery_authorization(run, auth, role=args.role)
        if args.remediation:
            raise GuardError("policy_forbidden",
                             "a recovery attempt dispatches each required role once; remediation "
                             "subtasks are not part of a recovery authorization")
        if not is_current(run, {"stage_index": auth["stage_index"], "stage_attempt": auth["target_attempt"]}):
            raise GuardError("stale_snapshot", "authorization does not match the current attempt")
    stage = stage_info(run)
    expected_index = run["stage_index"]
    expected_attempt = run["stage_attempt"]
    # A recovery attempt must redo EVERY required role of the group, so a
    # required role that is not the stage actor is still dispatched normally.
    # Any other non-actor role keeps requiring an explicit --remediation.
    recovery_role = auth is not None and args.role in auth["required_roles"]
    if stage["actor"] != args.role and not args.remediation and not recovery_role:
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
    if not verify_existing(role, session=herdr_session):
        raise Error(f"Agent {role['agent_name']} is not live; do not send to an unknown terminal")
    live = lookup_agent(role["agent_name"], session=herdr_session)
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
    slot_auth = None
    with locked():
        run = require_run(args.run_id, tier="C")
        if run["status"] not in ("active", "recovery_active") or run["stage_index"] != expected_index \
                or run["stage_attempt"] != expected_attempt:
            # The attempt must match too: a stale prompt prepared for attempt N
            # must never be recorded into attempt N+1.
            raise Error("Run changed before dispatch; inspect status")
        current_auth = None
        if current_group(run) is not None:
            current_auth = recovery_authorization_for(run)
            if current_auth is None:
                raise GuardError("recovery_authorization_invalid",
                                 "an open fault group appeared concurrently; recovery is not authorized")
            validate_recovery_authorization(run, current_auth, role=args.role)
            # The receipt must cover EVERY required role, and the acting role's
            # request must match the authorized logical action exactly: same
            # effect, same message, same stable key. A slot authorizes one
            # logical action, never "whatever the caller sends".
            bound = (current_auth.get("role_actions") or {}).get(args.role) or {}
            if args.effect != bound.get("effect"):
                raise GuardError("recovery_authorization_invalid",
                                 "a read slot cannot be spent on a write (or the reverse)")
            if args.message != bound.get("message"):
                raise GuardError("recovery_authorization_invalid",
                                 "the dispatch message differs from the authorized recovery action")
            if action_fingerprint("recovery-dispatch", run, args.role, args.effect, role,
                                  message=args.message) != bound.get("fingerprint"):
                raise GuardError("recovery_authorization_invalid",
                                 "the dispatch parameters differ from the authorized action")
            verify_receipt_still_valid(run, current_auth, "recovery-dispatch", role=args.role,
                                       effect=args.effect)
            # Re-observe the live Agent under the lock; the receipt echo is not proof.
            require_live_identity(run, args.role)
        if any(x["role"] == args.role and x["stage_index"] == run["stage_index"]
               and x["status"] in ("submission_uncertain", "submitted_unsettled", "uncertain") for x in run["dispatches"]):
            raise Error("Concurrent dispatch already pending for this Worker")
        if current_auth is None and sum(x["role"] == args.role and x["stage_index"] == run["stage_index"]
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
                "cwd": role.get("cwd"), "worktree": role.get("worktree"),
                "result_file": str(artifact),
                "root_attempt": logical_root(run),
                "action_key": ((current_auth.get("role_actions") or {}).get(args.role) or {}
                               ).get("action_key") if current_auth else action_key(
                                   run, args.role, "dispatch"),
                "recovery_authorization_id": current_auth["id"] if current_auth else None}
        run["dispatches"].append(item)
        slot_auth = current_auth
        if current_auth is not None:
            # Occupy this role's one-shot slot in the same atomic commit that
            # records the dispatch. A crash after this point may only resume
            # THIS dispatch; it must never re-send the prompt.
            consume_recovery_slot(run, current_auth, args.role, stamp, args.effect)
            # The recovery dispatch joins the SAME group, so a later fault on it
            # can never escape the group's classification or T7 requirements.
            group_for_dispatch = fault_group_for(run, current_auth["stage_index"],
                                                 current_auth["root_attempt"])
            if group_for_dispatch is not None:
                sync_group_members(run, group_for_dispatch)
            run["events"].append({"at": now(), "type": "recovery_slot_consumed",
                                  "authorization_id": current_auth["id"], "role": args.role,
                                  "dispatch_id": stamp, "stage_attempt": run["stage_attempt"]})
        if approvals:
            fresh = [x for x in run["approval_tokens"] if x["role"] == args.role and x["effect"] == args.effect
                     and x["stage_index"] == run["stage_index"]]
            if not fresh:
                raise Error("Approval token has already been used")
            token = fresh[0]
            run["approval_tokens"].remove(token)
            item["approval_reference"] = token["reason"]
        save(run_file(args.run_id), run)
    wait_timeout = getattr(args, "wait_timeout_seconds", None)
    budget = wait_timeout if wait_timeout is not None else run["settings"]["orchestration"]["task_timeout_seconds"]
    timeout_ms = max(1, int(budget)) * 1000
    cmd = ["agent", "prompt", role["agent_name"], text]
    if not args.no_wait:
        cmd += ["--wait", "--until", "idle", "--until", "done", "--until", "blocked", "--timeout", str(timeout_ms)]
    def finalize(update_of):
        """Async finalize: re-read under the lock and never touch a terminal Run.

        Applies the same guard the write entry points use (`run_terminal`),
        plus the expected attempt, the bound recovery authorization and the
        slot CAS. A terminal Run gets NO write at all — not even a refusal
        event — so an async return can never mutate a finished ledger.
        """
        try:
            with locked():
                current = require_run(args.run_id, tier="C")
                rec = next((x for x in current["dispatches"] if x["id"] == stamp), None)
                if rec is None:
                    return None
                if current["status"] in ("finished", "cancelled"):
                    return None
                if rec["status"] in ("settled", "abandoned") or (
                        rec["status"] == "blocked" and has_valid_report(rec)):
                    return None
                if current["stage_index"] != expected_index or \
                        current["stage_attempt"] != expected_attempt or not is_current(current, rec):
                    return None
                if slot_auth is not None:
                    live_auth = recovery_authorization_for(current)
                    if live_auth is None or live_auth.get("id") != slot_auth["id"]:
                        return None
                    slot = (live_auth.get("slots") or {}).get(args.role) or {}
                    if slot.get("dispatch_id") != stamp or slot.get("status") != "consumed":
                        return None
                rec.update(update_of(current, rec))
                save(run_file(args.run_id), current)
                return rec
        except Error:
            return None

    try:
        result = herdr(*cmd, timeout=timeout_ms/1000 + (15 if wait_timeout is None else 0), session=herdr_session)
        if args.no_wait:
            if finalize(lambda _run, _rec: {"status": "submitted_unsettled"}) is None:
                return {"dispatch_id": stamp, "status": "terminal",
                        "next": "the dispatch was decided while the submission was in flight; inspect it"}
            return {"dispatch_id": stamp, "status": "submitted_unsettled",
                    "next": "run collect --run-id ... --dispatch-id ... before retry or passing the stage"}
        # A successful wait is only a lifecycle observation. Report identity is
        # independently verified before settling.
        st = agent_status_of(result)
        rec = finalize(lambda run_, rec_: reconcile_result(run_, rec_, st))
        if rec is None:
            return {"dispatch_id": stamp, "status": "terminal",
                    "note": "a late wait return cannot overwrite a decided dispatch"}
        return {"dispatch_id": stamp, "status": rec["status"], "agent_state": st,
                "report": rec.get("report"), "error": rec.get("error"),
                "warning": "Worker JSON is not independent verification; Lead must check evidence and tests"}
    except Error as e:
        finalize(lambda _run, _rec, message=str(e): {"error": message})
        raise Error(f"Dispatch {stamp} uncertain; input might have been sent. Run collect before retry. {e}") from e


def herdr_read(name):
    # agent read deliberately prints plain text instead of JSON
    return process(["herdr", "agent", "read", name, "--source", "recent-unwrapped", "--lines", "160"],
                   timeout=30, raw=True)[-50000:]


def late_artifact_notice(run, rec):
    """Historical uncertain/abandoned + late report: retain evidence, change nothing.

    A report that arrives after the dispatch became history is evidence only.
    It never rewrites the record, never sets report/hash/status and never
    becomes gate evidence for any attempt.
    """
    snapshot = result_snapshot(run, rec)
    if not snapshot.get("valid"):
        raise GuardError("stale_snapshot",
                         f"dispatch {rec['id']} is historical ({rec['status']}); "
                         "decided records are not re-collected")
    presence = agent_presence(get_role(run, rec["role"]), session=run.get("session"))
    evidence = collect_evidence(run, rec, presence, snapshot, "late_artifact_rejected", git_evidence(rec))
    raise GuardError("history_unresolved",
                     f"late artifact for historical {rec['status']} dispatch retained as evidence "
                     f"{evidence['snapshot_id']}; it cannot change status/report/hash or gate evidence. "
                     "Escalate to the Owner")


def run_collect(args):
    run, herdr_session, _own = _require_run_with_session(args.run_id, tier="C")
    rec = next((x for x in reversed(run["dispatches"]) if x["id"] == args.dispatch_id), None)
    if rec is None: raise Error("Unknown dispatch ID")
    # The dispatch is what identifies the Worker here, so the target check has to
    # happen after resolving it rather than at ledger-load time.
    require_target_role(run, herdr_session, rec["role"], run_file(args.run_id))
    if not is_current(run, rec):
        # T18: an abandoned dispatch is terminal; a historical uncertain one may
        # only retain a late artifact. Neither is ever re-collected.
        if rec["status"] in ("abandoned", "uncertain"):
            with locked():
                run = require_run(args.run_id)
                rec = next(x for x in run["dispatches"] if x["id"] == args.dispatch_id)
                if is_current(run, rec):
                    raise GuardError("stale_snapshot", "dispatch became current again; re-run collect")
                late_artifact_notice(run, rec)
        if rec["status"] == "blocked" and not has_valid_report(rec):
            # Historical legacy blocked: T5 recheck only (adopt a valid report
            # as blocked(valid), otherwise diagnose). Not a current collect.
            snapshot = result_snapshot(run, rec)
            if not snapshot.get("valid"):
                out = diagnose_dispatch(args.run_id, args.dispatch_id)
                out["t5"] = "legacy_blocked_requires_recheck"
                return out
            with locked():
                run = require_run(args.run_id)
                rec = next(x for x in run["dispatches"] if x["id"] == args.dispatch_id)
                if is_current(run, rec) or rec["status"] != "blocked" or has_valid_report(rec):
                    raise GuardError("stale_snapshot", "dispatch changed while rechecking")
                report, digest = read_result(run, rec)
                if digest != snapshot["sha256"]:
                    raise GuardError("artifact_invalid",
                                     "report changed between the recheck and the lock")
                rec.update({"status": "blocked", "report": report, "artifact_sha256": digest,
                            "result_file": str(result_path(run["id"], rec["id"])), "error": None})
                run["events"].append({"at": now(), "type": "legacy_blocked_adopted",
                                      "dispatch_id": rec["id"], "artifact_sha256": digest})
                save(run_file(args.run_id), run)
            return {"dispatch_id": args.dispatch_id, "status": "blocked",
                    "artifact_sha256": digest, "t5": "legacy_blocked_report_adopted",
                    "note": "Historical legacy blocked adopted a valid bound report; it stays a "
                            "business blocked decision and never becomes settled"}
        raise GuardError("stale_snapshot",
                         f"dispatch {rec['id']} is historical ({rec['status']}); decided records are not re-collected")
    check_transition(run, "collect", rec)
    if rec["status"] in ("settled", "abandoned"):
        # Decided records are terminal: an abandoned dispatch is never reconciled,
        # and a late artifact must not flip it back to settled.
        retained = None
        if rec["status"] == "abandoned":
            # Keep an ACTUAL snapshot of a late report, not just a note.
            snapshot = result_snapshot(run, rec)
            if snapshot.get("valid"):
                presence = agent_presence(get_role(run, rec["role"]), session=run.get("session"))
                retained = collect_evidence(run, rec, presence, snapshot,
                                            "late_artifact_current_abandoned",
                                            git_evidence(rec))["snapshot_id"]
        return {"dispatch_id": args.dispatch_id, "status": rec["status"], "agent_state": None,
                "report": rec.get("report"), "error": rec.get("error"),
                "late_evidence_snapshot": retained,
                "note": "Terminal dispatch: no reconciliation performed; late artifacts are "
                        "evidence only and never become current-gate evidence"}
    if rec["status"] == "blocked" and rec.get("report") and rec.get("artifact_sha256"):
        raise Error("Terminal blocked dispatch has a report; cannot collect again")
    if rec["status"] == "blocked" and not has_valid_report(rec):
        # Legacy blocked(no valid report). For a CURRENT dispatch this is a
        # normal collect: an idle Worker with a valid complete report settles;
        # a missing/invalid report is uncertain. A HISTORICAL one only gets the
        # T5 recheck (adopt a valid report, otherwise diagnose).
        snapshot = result_snapshot(run, rec)
        if snapshot.get("valid"):
            pass  # fall through to the normal collect below
        else:
            presence = agent_presence(get_role(run, rec["role"]), session=run.get("session"))
            with locked():
                run = require_run(args.run_id)
                rec = next((x for x in run["dispatches"] if x["id"] == args.dispatch_id), None)
                if rec is None or not is_current(run, rec) or rec["status"] != "blocked":
                    raise GuardError("stale_snapshot", "dispatch changed while rechecking")
                if has_valid_report(rec):
                    return {"dispatch_id": args.dispatch_id, "status": rec["status"],
                            "report": rec.get("report"), "t5": "already_decided"}
                rec.update({"status": "uncertain", "collected_at": now(), "error": None,
                            "observed_status": presence.get("status"),
                            "diagnosis": "legacy_blocked_without_valid_report"})
                run["events"].append({"at": now(), "type": "legacy_blocked_uncertain",
                                      "dispatch_id": rec["id"]})
                save(run_file(args.run_id), run)
            return {"dispatch_id": args.dispatch_id, "status": "uncertain", "agent_state": None,
                    "t5": "legacy_blocked_uncertain",
                    "note": "Legacy blocked without a valid report is unresolved, not a business "
                            "result; it is now a technical fault requiring recovery"}
    # A current legacy blocked record re-enters the normal collect exactly once,
    # so a valid complete report settles it instead of staying a permanent lock.
    legacy_collect = rec["status"] == "blocked" and not has_valid_report(rec)
    role = get_role(run, rec["role"])
    if not verify_existing(role, session=herdr_session): raise Error("Registered Agent is no longer live")
    agent = lookup_agent(role["agent_name"], session=herdr_session)
    st = agent_status_of(agent)
    with locked():
        run = require_run(args.run_id, tier="C")
        rec = next(x for x in run["dispatches"] if x["id"] == args.dispatch_id)
        check_transition(run, "collect", rec)
        if rec["status"] in ("settled", "abandoned") or (
                rec["status"] == "blocked" and rec.get("report") and rec.get("artifact_sha256")):
            raise Error("Terminal dispatch cannot be collected again")
        if rec["status"] == "blocked" and not legacy_collect:
            raise Error("Legacy blocked dispatch requires diagnosis")
        rec.update(reconcile_result(run, rec, st))
        save(run_file(args.run_id), run)
    return {"dispatch_id": args.dispatch_id, "status": rec["status"], "agent_state": st,
            "report": rec.get("report"), "error": rec.get("error"),
            "warning": "Structured self-report must be independently reviewed; no automatic retry"}


def require_recovery_gate_authentication(run, decision, auth, group):
    """Recovery gates are human gates: no independent authentication = no gate.

    The record must bind decision, Run, stage, attempt, the collected report
    hashes of the current attempt, the fault group and the authorization slots.
    """
    if auth is None or group is None:
        raise GuardError("approval_required",
                         "a recovery gate needs a bound authorization and fault group")
    members = [d for d in run["dispatches"] if d.get("stage_index") == run["stage_index"]
               and d.get("stage_attempt", 1) == run["stage_attempt"]
               and d.get("artifact_sha256")]
    control = trusted_control()
    claim = {"action": "recovery-gate-approval", "decision": decision, "run_id": run["id"],
             "stage_index": run["stage_index"], "stage_attempt": run["stage_attempt"],
             "dispatch_hashes_sha256": hashlib.sha256(json.dumps(
                 sorted([d["id"], d.get("artifact_sha256")] for d in members),
                 sort_keys=True).encode("utf-8")).hexdigest(),
             "group_id": group.get("id"), "authorization_id": auth.get("id"),
             "slots": {r: (auth["slots"][r] or {}).get("dispatch_id")
                       for r in auth.get("required_roles", [])}}
    try:
        approval = control(dict(claim, type="verify"))
    except GuardError:
        raise
    except Exception as e:  # noqa: BLE001
        raise GuardError("approval_required", f"authentication unavailable: {e}") from e
    if not isinstance(approval, dict) or approval.get("authenticated") is not True:
        raise GuardError("approval_required",
                         "no independent authentication for this recovery gate decision")
    for key in ("decision", "run_id", "stage_index", "stage_attempt",
                "dispatch_hashes_sha256", "group_id", "authorization_id"):
        if approval.get(key) != claim[key]:
            raise GuardError("approval_required", f"authentication does not bind {key}")
    if not approval_ok({"approver": approval.get("approver"), "scope": "recovery-gate-approval",
                        "receipt": approval.get("receipt"), "issued_at": approval.get("issued_at"),
                        "expires_at": approval.get("expires_at")}):
        raise GuardError("approval_required", "authentication record is invalid or expired")
    return approval


def is_decided_member(rec):
    """A settled record, or a blocked record with a valid bound report.

    "Decided" means the record needs no further collection. It does NOT mean
    the stage may advance: a blocked record is a business negative even when
    its report is valid, so gate pass/revise and finish must still refuse it.
    Only a recorded revise turns it into a new attempt.
    """
    return rec.get("status") == "settled" or (rec.get("status") == "blocked" and has_valid_report(rec))


def verify_round_limit_provenance(run, technical, group):
    """Ask the trusted control plane to verify a technical over-limit.

    The ledger's `kind` is a claim, not evidence: the control plane must confirm
    the structured external event, this Run/stage/root, and that no business
    negative exists. Any failure fails closed.
    """
    control = trusted_control()
    claim = {"action": "round-limit-provenance", "run_id": run["id"],
             "stage_index": run["stage_index"], "root_attempt": logical_root(run),
             "blocked_round_limit_at": technical.get("at"),
             "external_event_id": technical.get("event_id")}
    try:
        verified = control(dict(claim, type="verify"))
    except GuardError:
        raise
    except Exception as e:  # noqa: BLE001
        raise GuardError("execution_not_fenced",
                         f"trusted control plane unavailable: {e}") from e
    if not isinstance(verified, dict) or verified.get("kind") != "technical" \
            or verified.get("verified_by") != "external" \
            or verified.get("run_id") != run["id"] \
            or verified.get("stage_index") != run["stage_index"] \
            or verified.get("root_attempt") != logical_root(run) \
            or verified.get("external_event_id") != claim["external_event_id"] \
            or verified.get("business_negative") not in (False, None):
        raise GuardError("run_state_forbidden",
                         "trusted control plane did not verify a structural technical over-limit")
    return verified


def recovery_business_negatives(run, auth):
    """Roles whose latest recovery report is a BUSINESS negative.

    A valid blocked report OR a settled fail/needs-work verdict both count.
    A technical redo with no negative at all does not.
    """
    negatives = []
    for role in (auth.get("required_roles") or []):
        latest = [d for d in run["dispatches"] if d.get("stage_index") == run["stage_index"]
                  and d.get("stage_attempt", 1) == run["stage_attempt"] and d.get("role") == role]
        if not latest:
            continue
        rec = latest[-1]
        report = rec.get("report") if isinstance(rec.get("report"), dict) else {}
        if rec.get("status") == "blocked" and has_valid_report(rec):
            negatives.append(role)
        elif rec.get("status") == "settled" and report.get("verdict") in ("fail", "needs-work"):
            negatives.append(role)
    return negatives


def gate_fault_group(run, decision):
    """Fault-group rules for a gate decision (T10/T11/T12/T13/T21).

    Returns the group that this decision closes, or None. A decision that would
    stop the whole group (blocked) never closes it.
    """
    group = current_group(run)
    if group is None:
        return None
    assert_members_classified(run, group)
    if decision == "blocked":
        raise GuardError("fault_group_open",
                         "a fault group is open; gate blocked would freeze it. Resolve every member "
                         "first, then decide the group as a whole")
    auth = recovery_authorization_for(run)
    if auth is not None:
        validate_recovery_authorization(run, auth)
        verify_receipt_still_valid(run, auth, "recovery-gate")
        for role in auth["required_roles"]:
            require_live_identity(run, role)
            slot = auth["slots"][role]
            if slot.get("status") != "consumed":
                raise GuardError("recovery_authorization_invalid",
                                 f"required role {role} has no dispatched recovery attempt yet")
            latest = [d for d in run["dispatches"] if d.get("stage_index") == run["stage_index"]
                      and d.get("stage_attempt", 1) == run["stage_attempt"] and d.get("role") == role]
            # A required role is done when it has a DECIDED report: a settled
            # pass, or a valid business-blocked negative for a T13 revise.
            if not latest or not (latest[-1].get("status") == "settled"
                                  or (latest[-1].get("status") == "blocked"
                                      and has_valid_report(latest[-1]))):
                raise GuardError("dispatch_unresolved",
                                 f"required role {role} has no decided report in the recovery attempt")
        return group
    if group_has_abandoned(run, group):
        raise GuardError("recovery_authorization_invalid",
                         "fault group contains an abandoned member; it must be recovered as a whole "
                         "with a bound authorization, never decided by an ordinary gate")
    # Pure reporting group: all technical faults are collected into valid
    # reports. T21 closes it first, then the ordinary T11/T12 decision applies.
    return group


def run_gate(args):
    with locked():
        run = require_run(args.run_id, tier="C")
        check_transition(run, "gate")
        stage = stage_info(run)
        actor = stage["actor"]
        # A recovery attempt is identified by its bound authorization, not by
        # the Run status: recovery entered from `active` keeps the Run active.
        recovery = recovery_authorization_for(run) is not None
        if recovery and args.decision == "revise" and run["status"] == "recovery_active":
            # T13 is only reachable from a recovery attempt that did NOT enter
            # recovery_active. recovery_active forbids revise entirely: the
            # restricted state may only pass, or escalate/cancel.
            raise GuardError("run_state_forbidden",
                             "recovery_active may not revise; escalate or cancel instead")
        closing_group = gate_fault_group(run, args.decision)
        if recovery and args.decision == "revise":
            auth = recovery_authorization_for(run)
            if not recovery_business_negatives(run, auth):
                # A revise without a business negative would be washing away the
                # technical faults and refunding budget. Refuse it.
                raise GuardError("fault_group_open",
                                 "a recovery revise requires at least one valid business negative "
                                 "(blocked report or fail/needs-work verdict); a technical redo "
                                 "cannot be converted into a business revise")
            if run["round"] + 1 > run["settings"]["orchestration"]["max_rounds"]:
                run["pending_round_limit"] = {"kind": "business", "source": "recovery-revise"}
        # Only the CURRENT attempt matters. Abandoned dispatches are decided
        # records and never positive evidence; a blocked one is a business
        # negative that `revise` (not `pass`) is meant to consume, so it stays
        # out of this "still open?" predicate. `pass` over a blocked member is
        # refused by the complete/pass check below, with a message that names
        # the actual reason instead of calling it unresolved.
        if args.decision in ("pass", "revise"):
            pending = [x for x in run["dispatches"] if x["stage_index"] == run["stage_index"] and
                       x.get("stage_attempt", 1) == run["stage_attempt"] and
                       x["status"] not in ("settled", "blocked", "abandoned")]
            if pending:
                raise Error(f"Cannot {args.decision} stage with unresolved/blocked dispatches: {[x['id'] for x in pending]}")
        good = None
        if args.decision == "pass" and actor != "lead":
            all_recs = [x for x in run["dispatches"] if x["stage_index"] == run["stage_index"] and
                        x.get("stage_attempt", 1) == run["stage_attempt"] and x["role"] == actor]
            if not all_recs:
                raise Error(f"Cannot pass stage {stage['stage']}: no bound positive artifact from {actor}")
            good = all_recs[-1]  # A later failing review overrides an earlier pass.
            if good["status"] != "settled" or good.get("report", {}).get("verdict") != "pass" or not good.get("artifact_sha256"):
                raise Error(f"Latest {actor} artifact is not an approved result")
        if args.decision == "pass":
            checked_roles = set(x["role"] for x in run["dispatches"] if x["stage_index"] == run["stage_index"] and
                                x.get("stage_attempt", 1) == run["stage_attempt"])
            if recovery:
                # T10: every required role must produce a NEW report in the
                # recovery attempt, with a positive verdict and a hash that
                # still matches the collected bytes. A lead actor is no
                # exception to checking the required roles.
                auth = recovery_authorization_for(run)
                checked_roles |= set(auth["required_roles"]) if auth else set()
            for role_name in sorted(checked_roles):
                latest_role = [x for x in run["dispatches"] if x["stage_index"] == run["stage_index"] and
                               x.get("stage_attempt", 1) == run["stage_attempt"] and x["role"] == role_name]
                if not latest_role:
                    raise Error(f"Latest remediation/worker artifact for {role_name} is missing")
                rec = latest_role[-1]
                report = rec.get("report") if isinstance(rec.get("report"), dict) else {}
                if recovery and auth is not None and role_name in auth.get("required_roles", []):
                    # T10: a recovery pass needs a COMPLETE report with a pass
                    # verdict. A business-blocked report is a negative, not a pass.
                    if rec.get("status") != "settled" or report.get("status") != "complete" \
                            or report.get("verdict") != "pass":
                        raise Error(f"Required role {role_name} has no complete/pass recovery report")
                elif rec.get("status") != "settled" or report.get("status") != "complete" \
                        or report.get("verdict") != "pass":
                    # A `blocked` record carrying a pass verdict is still a
                    # business negative: status, report status and verdict must
                    # ALL be affirmative before it can back a stage pass.
                    raise Error(f"Latest remediation/worker artifact for {role_name} is not a "
                                f"settled complete/pass result")
                _report, digest = read_result(run, rec)
                if digest != rec.get("artifact_sha256"):
                    raise Error(f"Worker artifact for {role_name} changed after collection")
            if good is None:
                own = [x for x in run["dispatches"] if x["stage_index"] == run["stage_index"]
                       and x.get("stage_attempt", 1) == run["stage_attempt"] and x["role"] == actor]
                good = own[-1] if own else None
            if good is not None and stage["stage"] in ("self_test", "independent_test"):
                tests = good["report"].get("tests", [])
                if not tests or any(t["exit_code"] != 0 for t in tests):
                    raise Error("Test stage requires explicit passing test records with exit_code=0")
        if args.decision == "pass" and actor == "lead":
            # Lead attestation remains human/agent reasoning, not an external proof.
            pass
        if recovery:
            # A recovery decision needs an independent human authentication bound
            # to the decision, Run, stage, attempt, current dispatch hashes, the
            # group and the slot. A Lead-typed evidence string is NOT that.
            require_recovery_gate_authentication(run, args.decision,
                                                 recovery_authorization_for(run),
                                                 closing_group)
        if len(args.evidence.strip()) < 12:
            raise Error("Gate requires specific evidence (at least 12 characters)")
        run["events"].append({"at": now(), "type": "gate", "stage": stage["stage"],
                              "stage_index": run["stage_index"], "stage_attempt": run["stage_attempt"],
                              "decision": args.decision, "evidence": args.evidence,
                              "artifact_sha256": good["artifact_sha256"] if args.decision == "pass" and good else None})
        if closing_group is not None:
            by_id = {d["id"]: d for d in run["dispatches"]}
            current_members = [by_id[i] for i in closing_group.get("members", []) if i in by_id]
            close_fault_group(run, closing_group, {
                "by": "gate", "decision": args.decision, "stage_index": run["stage_index"],
                "stage_attempt": run["stage_attempt"], "evidence": args.evidence, "at": now(),
                "group_sha256": closing_group.get("member_snapshot_sha256"),
                "member_hashes_sha256": group_digest(closing_group["stage_index"],
                                                     closing_group["root_attempt"],
                                                     current_members),
                "gate_event_index": len(run["events"]) - 1},
                auth=recovery_authorization_for(run))
            run["events"].append({"at": now(), "type": "fault_group_closed",
                                  "group_id": closing_group["id"], "decision": args.decision})
        pending_limit = run.pop("pending_round_limit", None)
        if args.decision == "pass":
            if recovery:
                # T10: recovery succeeded. Round is untouched; the Run returns
                # to ordinary operation and the closed group no longer limits it.
                run["status"] = "active"
            run["stage_index"] += 1
            run["stage_attempt"] = 1
        elif args.decision == "revise":
            run["round"] += 1
            run["stage_attempt"] += 1
            if recovery:
                run["status"] = "active"
            if run["round"] > run["settings"]["orchestration"]["max_rounds"]:
                run["status"] = "blocked_round_limit"
                run["round_limit_provenance"] = (pending_limit or
                                                 {"kind": "business", "source": "gate-revise"})
        else: run["status"] = "blocked"
        save(run_file(args.run_id), run)
    return {"decision": args.decision, "status": run["status"], **stage_info(run)}


def run_drive(args):
    """Choose existing primitives from the durable ledger; never infer delivery."""
    runid = args.run_id
    steps = []
    dispatches = []
    run = None
    started = False
    reason = "error"
    detail = None
    deadline = time.monotonic() + args.deadline_seconds

    def check_identity(role, herdr_session):
        try:
            if not verify_existing(role, session=herdr_session):
                raise Error("not live or identity cannot be verified")
            live = lookup_agent(role["agent_name"], session=herdr_session)
            pane = live.get("pane") if isinstance(live, dict) else None
            pane_id = (live.get("pane_id") or (pane.get("pane_id") if isinstance(pane, dict) else None)) if isinstance(live, dict) else None
            session = live.get("agent_session") if isinstance(live, dict) else None
            expected = role.get("agent_session") or {}
            same_session = isinstance(session, dict) and all(
                isinstance(expected.get(k), str) and expected.get(k).strip() and
                isinstance(session.get(k), str) and session.get(k) == expected.get(k)
                for k in ("kind", "source", "value"))
            if pane_id != role["pane_id"] or not same_session:
                raise Error("pane/session identity changed or cannot be verified")
        except Error as e:
            raise Error(f"Agent {role['agent_name']}: {e}") from e

    def result():
        info = stage_info(run) if run else {"stage": None}
        out = {"run_id": runid, "stopped_reason": reason, "stage": info["stage"],
               "stage_index": run.get("stage_index") if run else None,
               "stage_attempt": run.get("stage_attempt") if run else None,
               "round": run.get("round") if run else None, "steps": steps, "dispatches": dispatches,
               "deadline_exceeded": time.monotonic() >= deadline,
               "historical_blocked": [d["id"] for d in run["dispatches"] if d["status"] == "blocked" and
                   (d["stage_index"] < run["stage_index"] or (d["stage_index"] == run["stage_index"] and
                    d.get("stage_attempt", 1) < run["stage_attempt"]))] if run else []}
        if detail:
            out["detail"] = detail
        if run:
            progress_path = STATE / "runs" / (runid + ".driver.json")
            if progress_path.exists():
                try:
                    progress = load(progress_path)
                    if isinstance(progress, dict) and any(k in progress and progress[k] != run.get(k)
                            for k in ("stage_index", "stage_attempt", "round", "dispatches")):
                        out["ledger_overrides_progress"] = True
                except Error:
                    out["ledger_overrides_progress"] = True
        return out

    try:
        path = run_file(runid)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path.parent / (runid + ".driver.lock"), os.O_CREAT | os.O_WRONLY, 0o600)
    except (Error, OSError) as e:
        detail = str(e)
        return result()
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            reason, detail = "driver_running", "Driver already running for this Run"
            return result()
        try:
            with locked():
                run = require_run(runid, tier="C")
                events = [e for e in run["events"] if e.get("type") in ("driver_started", "driver_stopped")]
                if events and events[-1]["type"] == "driver_started" and not args.resume:
                    reason, detail = "resume_required", "Previous drive interrupted; use --resume"
                    return result()
                if run["status"] not in ("active", "recovery_active"):
                    reason = "round_limit" if run["status"] == "blocked_round_limit" else "run_not_active"
                    return result()
                for role in run["roles"]:
                    session = role.get("agent_session")
                    if (not isinstance(session, dict) or
                            not all(isinstance(session.get(k), str) and session[k].strip() for k in ("kind", "source", "value")) or
                            len(session["value"].strip()) < 8):
                        reason, detail = "identity_unbound", "旧 team 未绑定会话；请重建 team 或用 adopt 登记"
                        return result()
                run["events"].append({"type": "driver_started", "at": now()})
                save(path, run)
                started = True
            while True:
                # Ownership was proven once before entering the loop; re-proving it
                # on every iteration would repeat the same Herdr round-trips. The
                # per-iteration identity check below still guards each Worker.
                run, herdr_session, _own = _require_run_with_session(runid, tier="A")
                if run["status"] not in ("active", "recovery_active"):
                    reason = "round_limit" if run["status"] == "blocked_round_limit" else "run_not_active"
                    break
                if time.monotonic() >= deadline:
                    reason = "deadline"
                    break
                if len(steps) >= args.max_steps:
                    reason = "max_steps"
                    break
                pending = [d for d in run["dispatches"] if is_current(run, d) and d["status"] in UNRESOLVED]
                if pending:
                    rec = pending[0]
                    try:
                        check_identity(get_role(run, rec["role"]), herdr_session)
                    except Error as e:
                        reason, detail = "identity", str(e)
                        break
                    try:
                        collected = run_collect(argparse.Namespace(run_id=runid, dispatch_id=rec["id"]))
                    except Error as e:
                        reason, detail = "collect_error", str(e)
                        break
                    steps.append({"action": "collect", "dispatch_id": rec["id"], "status": collected["status"]})
                    if rec["id"] not in dispatches:
                        dispatches.append(rec["id"])
                    if collected["status"] != "settled":
                        reason = "worker_blocked" if collected["status"] == "blocked" else "stuck_unsettled"
                        break
                    continue
                if pending_dispatches(run, ("blocked",)):
                    reason = "worker_blocked"
                    break
                if pending_dispatches(run, ("abandoned",)) and recovery_authorization_for(run) is None:
                    # Drive never fabricates a recovery authorization.
                    reason, detail = "recovery_authorization_invalid", (
                        "an abandoned dispatch exists in the current attempt; no recovery "
                        "authorization is available. Run recover-attempt or escalate")
                    break
                stage = stage_info(run)
                if stage.get("actor") == "lead" or stage.get("stage") is None:
                    reason = "awaiting_lead"
                    break
                actor = stage["actor"]
                current = [d for d in run["dispatches"] if d["stage_index"] == run["stage_index"] and
                           d.get("stage_attempt", 1) == run["stage_attempt"] and d["status"] == "settled"]
                previous_revision = any(e.get("type") == "gate" and e.get("decision") == "revise" and
                                        e.get("stage_index") == run["stage_index"] and
                                        e.get("stage_attempt") == run["stage_attempt"] - 1 for e in run["events"])
                fixes = [d for d in current if d["role"] == "developer" and d.get("remediation")]
                if actor == "reviewer" and fixes and fixes[-1].get("report", {}).get("verdict") != "pass":
                    reason, detail = "worker_verdict", "Developer remediation did not pass"
                    break
                remediation = actor == "reviewer" and previous_revision and not fixes
                target = "developer" if remediation else actor
                own = [d for d in current if d["role"] == target]
                if remediation or not own:
                    role = get_role(run, target)
                    try:
                        check_identity(role, herdr_session)
                    except Error as e:
                        reason, detail = "identity", str(e)
                        break
                    message = "Perform the current stage and report concrete evidence."
                    if target != "developer":
                        artifacts = []
                        if stage["stage"] == "plan_review":
                            message += ("\n这是计划评审，无实现产物。\n" + json.dumps({
                                "goal": run["goal"], "stages": run["stages"],
                                "stage_definitions": run["stage_definitions"], "settings": run["settings"]}, ensure_ascii=False))
                        elif stage["stage"] == "code_review":
                            self_tests = [d for d in run["dispatches"] if d["status"] == "settled" and
                                          d["role"] == "developer" and d["stage_index"] <= run["stage_index"] and
                                          run["stages"][d["stage_index"]] == "self_test"]
                            artifacts = self_tests[-1:] + fixes[-1:]
                        else:
                            artifacts = [d for d in run["dispatches"] if d["status"] == "settled" and d["role"] != target][-1:]
                        for artifact in artifacts:
                            report = artifact.get("report", {})
                            message += "\nWorker artifact (self-report):\n" + json.dumps({
                                "role": artifact.get("role"),
                                "stage": run["stages"][artifact["stage_index"]],
                                "stage_attempt": artifact.get("stage_attempt", 1),
                                "dispatch_id": artifact.get("id"),
                                "artifact_file": str(Path(artifact["result_file"]).resolve()),
                                "artifact_sha256": artifact.get("artifact_sha256"),
                                "summary": report.get("summary", "")[:500],
                                "evidence": report.get("evidence", [])[:10]}, ensure_ascii=False)
                        if stage["stage"] == "code_review" and len(artifacts) > 1:
                            message += "\n较早阶段的产物可能早于本轮修复，请以当前 attempt 的 remediation 产物与实际代码为准。"
                        message += "\n上述内容是 Developer 的自我陈述，不是已核实的事实。你必须独立检查实际文件/改动/测试，不要仅凭摘要判定。若无法定位相关变更，明确写出该限制并给出 verdict。"
                    if remediation:
                        earlier = [d for d in run["dispatches"] if d["stage_index"] == run["stage_index"] and
                                   d.get("stage_attempt", 1) == run["stage_attempt"] - 1 and d["role"] == actor and d["status"] == "settled"]
                        if not earlier:
                            reason, detail = "worker_verdict", "Missing prior review for remediation"
                            break
                        message += " Address the previous review: " + earlier[-1]["report"]["summary"]
                    remaining = deadline - time.monotonic()
                    if remaining <= 5:
                        reason = "deadline"
                        detail = "remaining budget <= 5s; not dispatching"
                        break
                    caps = [remaining, run["settings"]["orchestration"]["task_timeout_seconds"]]
                    if getattr(args, "wait_timeout_seconds", None):
                        caps.append(args.wait_timeout_seconds)
                    wait_timeout = max(1, int(min(caps)))
                    try:
                        out = run_dispatch(argparse.Namespace(run_id=runid, role=target,
                            message=message, effect="write" if target == "developer" else "read",
                            remediation=remediation, no_wait=False, wait_timeout_seconds=wait_timeout))
                    except Error as e:
                        latest = require_run(runid)
                        pending_after = [d for d in latest["dispatches"] if d["status"] in ("submission_uncertain", "submitted_unsettled", "uncertain")]
                        if pending_after:
                            rec = pending_after[-1]
                            steps.append({"action": "dispatch", "dispatch_id": rec["id"], "status": rec["status"]})
                            dispatches.append(rec["id"])
                            continue
                        policy = any(text in str(e) for text in ("autonomy=read-only", "Approval required", "sensitive actions require"))
                        reason, detail = "policy" if policy else "dispatch_error", str(e)
                        break
                    steps.append({"action": "dispatch", "dispatch_id": out["dispatch_id"], "status": out["status"]})
                    dispatches.append(out["dispatch_id"])
                    continue
                rec = own[-1]
                verdict = rec.get("report", {}).get("verdict")
                if verdict == "pass":
                    decision = "pass"
                elif verdict in ("fail", "needs-work") and ((actor == "developer" and stage["stage"] in ("implement", "self_test")) or
                        (actor == "reviewer" and stage["stage"] in ("plan_review", "code_review") and any(r["role"] == "developer" for r in run["roles"]))):
                    decision = "revise"
                else:
                    reason, detail = "worker_verdict", f"No automatic remediation for {actor}: {verdict}"
                    break
                evidence = f"Dispatch {rec['id']} sha256 {rec.get('artifact_sha256', '')}: {rec['report']['summary'][:300]}"
                try:
                    out = run_gate(argparse.Namespace(run_id=runid, decision=decision, evidence=evidence))
                except Error as e:
                    reason, detail = "gate_error", str(e)
                    break
                steps.append({"action": "gate", "dispatch_id": rec["id"], "decision": decision, "status": out["status"]})
                if rec["id"] not in dispatches:
                    dispatches.append(rec["id"])
        except (Error, OSError, KeyError, TypeError, ValueError) as e:
            # Ledger/Worker identity failures are a distinct, actionable stop
            # reason; collapsing them into "error" hides why the driver refused.
            # `AgentUnobservable` is the precise signal; the markers only label a
            # ledger-level rejection. This is a diagnostic label for the stop
            # reason and never gates access -- access decisions are made by
            # _inspect_roles, which classifies by exception type.
            message = str(e)
            if isinstance(e, AgentUnobservable):
                reason = "identity_unavailable"
            elif isinstance(e, LedgerIdentityError):
                reason = "identity"
            else:
                reason = "error"
            detail = message
        finally:
            if started:
                try:
                    with locked():
                        run = require_run(runid, tier="B")
                        run["events"].append({"type": "driver_stopped", "at": now(), "reason": reason})
                        save(path, run)
                except (Error, OSError, KeyError, TypeError, ValueError) as e:
                    reason, detail = "error", str(e)
        return result()
    finally:
        os.close(fd)


def run_finish(args):
    with locked():
        run = require_run(args.run_id, tier="C")
        check_transition(run, "finish")
        open_groups = [g for g in run.get("fault_groups", []) if g.get("status") == "open"]
        if open_groups:
            raise GuardError("fault_group_open",
                             f"fault group {open_groups[0]['id']} is still open; close it before finishing")
        for closed in [g for g in run.get("fault_groups", []) if g.get("status") == "closed"]:
            problems = closure_problems(run, closed)
            if problems:
                raise GuardError("history_unresolved",
                                 f"fault group {closed['id']} has no verifiable closure: {problems[0]}")
        open_auths = [a for a in run.get("recovery_authorizations", []) if a.get("status") == "active"]
        if open_auths:
            raise GuardError("recovery_authorization_invalid",
                             f"recovery authorizations still active: {[a['id'] for a in open_auths]}")
        # Every abandoned dispatch must be covered by a closed group: an
        # abandonment nobody recovered is an unclosed historical fault, not a
        # decided record a finished Run may just walk past.
        covered = {m for g in run.get("fault_groups", []) if g.get("status") == "closed"
                   for m in g.get("members", [])}
        orphaned = [d["id"] for d in run["dispatches"]
                    if d.get("status") == "abandoned" and d["id"] not in covered]
        if orphaned:
            raise GuardError("history_unresolved",
                             f"abandoned dispatches with no closed fault group: {orphaned}")
        if run["stage_index"] != len(run["stages"]):
            raise Error(f"Cannot finish; next required stage is {stage_info(run)['stage']}")
        # Attempt-scoped: at the finish sentinel `stage_index` is len(stages),
        # so this predicate matches nothing and cannot be the last line of
        # defence. Gate transitions must have refused unresolved current work.
        # A historical `blocked` is NOT a decided record that finish may walk
        # past: it is an unresolved business negative, so it blocks finishing
        # just like an unresolved historical dispatch does below.
        if pending_dispatches(run, UNRESOLVED + ("blocked",)):
            raise GuardError("dispatch_unresolved", "current attempt still has unresolved work")
        # Design case 32: a blocked record is consumed once its stage goes on
        # to pass in a later attempt — then it is history and the Run finishes.
        # A blocked record whose stage never passed afterwards is the last word
        # on that stage: an unresolved business negative that finish must not
        # walk past.
        passed_stages = {e.get("stage_index") for e in run["events"]
                         if e.get("type") == "gate" and e.get("decision") == "pass"}
        unresolved_history = [d["id"] for d in run["dispatches"]
                              if d["status"] in UNRESOLVED or
                              (d["status"] == "blocked" and d.get("stage_index") not in passed_stages)]
        if unresolved_history:
            raise GuardError("history_unresolved",
                             f"unresolved historical dispatches: {unresolved_history}")
        passed = {e.get("stage_index") for e in run["events"] if e.get("type") == "gate" and e.get("decision") == "pass"}
        if passed != set(range(len(run["stages"]))):
            raise Error("Some mandatory gates have no recorded pass event")
        run["status"] = "finished"; run["finished_at"] = now()
        save(run_file(args.run_id), run)
    return {"finished": True, "run_id": args.run_id, "events": len(run["events"])}


def run_pause(args, paused):
    # Pausing is the fail-safe stop and stays available when identity cannot be
    # proven (tier B); resuming re-arms dispatch, so it requires proof (tier C).
    with locked():
        run, _session, ownership = _require_run_with_session(
            args.run_id, tier="B" if paused else "C")
        if paused:
            if run["status"] == "recovery_active":
                raise GuardError("run_state_forbidden",
                                 "recovery_active may not be paused; pass, escalate or cancel")
            if run["status"] != "active": raise Error("Only active run can be paused")
            run["status"] = "paused"
            # Record the identity confidence actually achieved, not just the
            # absence of a session tag: a `verified` ledger whose Workers all
            # contradict could otherwise pause silently and look audited.
            verified, _total, conflicts, _unavailable = _inspect_roles(run, _session)
            event = {"at": now(), "type": "pause",
                     "identity": "verified" if (ownership == "verified" and verified and not conflicts)
                     else "unverified"}
            run["events"].append(event)
        else:
            if run["status"] not in ("paused", "blocked", "blocked_round_limit"):
                raise Error("Only paused/blocked run can resume")
            if run["status"] == "blocked_round_limit" and not args.extend_rounds:
                raise Error("Reached round limit. Owner must explicitly supply --extend-rounds N")
            if args.extend_rounds:
                raise Error("Round-limit extension disabled until independent Owner authentication is available")
            run["status"] = "active"
            run["events"].append({"at": now(), "type": "resume"})
        save(run_file(args.run_id), run)
    return {"status": run["status"], "run_id": args.run_id, "ledger_session": ownership}


def run_list():
    """Discover orphaned/paused work without performing any new dispatch."""
    identity = lead_identity()
    ctx, current = identity["context"], identity["session"]
    result = []
    with locked():
        for p in sorted((STATE / "runs").glob("*.json")):
            if p.name.endswith(".driver.json"):
                continue
            run = load(p)
            if run.get("context") != ctx:
                continue
            pending = [d["id"] for d in run.get("dispatches", [])
                       if is_current(run, d) and d["status"] not in DECIDED]
            historical = [{"id": d["id"], "status": d["status"], "stage_index": d.get("stage_index"),
                           "stage_attempt": d.get("stage_attempt", 1)}
                          for d in run.get("dispatches", [])
                          if not is_current(run, d) and d["status"] not in ("settled",)]
            abandoned = [d["id"] for d in run.get("dispatches", []) if d.get("status") == "abandoned"]
            result.append({"run_id": run["id"], "status": run["status"],
                           "stage": stage_info(run)["stage"], "pending_dispatches": pending,
                           "ledger_session": ledger_ownership(run, current, p),
                           "historical_decided": historical, "abandoned_dispatches": abandoned,
                           "open_fault_groups": [{"id": g["id"], "stage_index": g["stage_index"],
                                                  "root_attempt": g["root_attempt"]}
                                                 for g in run.get("fault_groups", [])
                                                 if g.get("status") == "open"]})
    return {"runs": result, "unfinished": [x for x in result if x["status"] not in ("finished", "cancelled")]}


def run_recover(args):
    """Read-only reconciliation hint. Never resend uncertain prompts."""
    with locked():
        run = require_run(args.run_id, tier="A")
        pending = [d for d in run.get("dispatches", []) if is_current(run, d) and d["status"] not in DECIDED]
        historical = [d for d in run.get("dispatches", []) if not is_current(run, d) and d["status"] != "settled"]
        abandoned = [d for d in run.get("dispatches", []) if d.get("status") == "abandoned"]
        group = sync_group_members(run, fault_group_for(run)) if fault_group_for(run) else None
        if group is None:
            group = current_group(run)
        auth = recovery_authorization_for(run, attempt=run["stage_attempt"])
        return {"run_id": run["id"], "run_status": run["status"], "current_stage": stage_info(run),
                "fault_group": None if group is None else {
                    "id": group["id"], "members": group["members"],
                    "has_abandoned": group_has_abandoned(run, group),
                    "required_roles": required_roles(run, group),
                    "next": "run recover-attempt" if group_has_abandoned(run, group)
                            else "close the group with an ordinary gate after every member is decided"},
                "recovery_authorization": None if auth is None else {
                    "id": auth["id"], "required_roles": auth["required_roles"],
                    "slots": {r: s.get("status") for r, s in auth["slots"].items()},
                    "role_budgets": auth.get("role_budgets")},
                "pending_dispatches": [{"id": d["id"], "role": d["role"],
                                       "status": d["status"], "report_path": d.get("result_file"),
                                       "next": _next_hint(run, d)} for d in pending],
                "historical_decided": [{"id": d["id"], "role": d["role"], "status": d["status"],
                                        "stage_index": d.get("stage_index"),
                                        "stage_attempt": d.get("stage_attempt", 1),
                                        "note": "decided record of an earlier attempt; not pending"}
                                       for d in historical],
                "abandoned_dispatches": [{"id": d["id"], "role": d["role"],
                                          "evidence": d.get("abandon_evidence"),
                                          "risk_accepted": d.get("abandon_risk_accepted")} for d in abandoned],
                "next": "Collect every unresolved CURRENT dispatch; historical and abandoned records are "
                        "decided and must not be re-collected. NEVER blindly resend."}


def _next_hint(run, rec):
    if rec.get("status") == "blocked":
        return (f"run collect --run-id {run['id']} --dispatch-id {rec['id']} "
                f"(legacy blocked: re-check binding, business revise only with a valid report)")
    return f"run collect --run-id {run['id']} --dispatch-id {rec['id']}"


def run_cancel(args):
    """Explicitly mark a Run abandoned; never claim completion or delete workers."""
    with locked():
        # Tier is decided AFTER loading, because T17 class A (no execution
        # history) has no executor to own: a ledger that dispatched nothing
        # cannot belong to a foreign session's running agents, so requiring
        # Worker UUIDs there would reintroduce the very lock-up Issue #2 removes.
        # Class B always demands tier C. Load once at the weaker tier, then
        # escalate based on the record itself.
        run, herdr_session, _own = _require_run_with_session(args.run_id, tier="A")
        check_transition(run, "cancel")
        if args.confirm_run_id != args.run_id or len(args.reason.strip()) < 12:
            raise Error("Cancellation requires --confirm-run-id matching --run-id and specific --reason")
        pending = [d["id"] for d in run["dispatches"] if d["status"] not in ("settled", "blocked", "abandoned")]
        if pending:
            raise Error(f"Cannot cancel while dispatches are unresolved: {pending}. Inspect and reconcile safely")
        # T17 is graded by whether this Run ever dispatched a prompt, not by
        # its state or by whether a fault group exists.
        #
        # SAFETY PREMISE: run_dispatch saves the dispatch into the ledger
        # (scripts/htm.py:2513) BEFORE it actually sends the prompt at :2556
        # (:2517 only builds the `agent prompt` command). So an
        # empty `dispatches` list reliably proves no prompt was ever sent by
        # this Run, and therefore there is no executor to stop or isolate.
        # That is an absent obligation, not a lowered bar: no executor exists,
        # so no isolation proof can be required. Neither an idle/done
        # observation nor a caller's risk declaration is ever accepted as proof
        # for a Run that HAS dispatched.
        clean_run = (not run.get("dispatches")
                     and not run.get("fault_groups")
                     and not run.get("recovery_authorizations"))
        if clean_run:
            # Class A: nothing was ever dispatched. Re-checked under the lock,
            # so a concurrent dispatch that landed first is caught here.
            cancel_scope = "no_execution_history"
            run["status"] = "cancelled"
            run["events"].append({"at": now(), "type": "cancel", "reason": args.reason,
                                  "cancel_scope": cancel_scope})
            save(run_file(args.run_id), run)
            return {"run_id": args.run_id, "status": "cancelled", "finished": False,
                    "cancel_scope": cancel_scope}
        # Class B: execution history exists, so the ledger may describe another
        # session's real executors. Prove ownership through Worker UUIDs before
        # touching it.
        require_roles_owned(run, herdr_session, run_file(args.run_id))
        # Class B: this Run has execution history, so every actor it may have
        # started must be proven stopped/isolated with known side effects before
        # the reservation is released. A fault-group context is included as
        # evidence (a real abandon leaves a TRANSIENT group before
        # recover-attempt persists it).
        recovery_group = current_group(run)
        abort_groups = [g for g in run.get("fault_groups", []) if g.get("status") == "open"]
        fault_group_ids = ([recovery_group["id"]] if recovery_group is not None else [])
        for group in abort_groups:
            if group["id"] not in fault_group_ids:
                fault_group_ids.append(group["id"])
        control = trusted_control()
        try:
            verdict = control({"type": "verify", "action": "cancel", "run_id": run["id"],
                               "stage_index": run["stage_index"],
                               "stage_attempt": run["stage_attempt"],
                               "fault_group_ids": fault_group_ids,
                               "abandoned_dispatch_ids": [d["id"] for d in
                                                          abandoned_predecessors(run)],
                               "roles": [dict(role_identity(r), role=r["role"])
                                         for r in run["roles"]]})
        except GuardError:
            raise
        except Exception as e:  # noqa: BLE001
            raise GuardError("execution_not_fenced",
                             f"trusted control plane unavailable: {e}") from e
        if not isinstance(verdict, dict) or verdict.get("isolation") != "confirmed" \
                or verdict.get("side_effects") != "known" or verdict.get("revoked") not in (False, None):
            raise GuardError("execution_not_fenced",
                             "cancel needs verified isolation, identity and side-effect "
                             "safety for every actor; an observation is not proof")
        if sorted(verdict.get("verified_roles") or []) != sorted(r["role"] for r in run["roles"]):
            raise GuardError("identity_mismatch",
                             "cancel must verify every actor identity")
        run["status"] = "cancelled"
        for group in run.get("fault_groups", []):
            if group.get("status") == "open":
                # A cancelled Run is terminal: the group is closed by T17, not
                # by a recovery proof, so it keeps no closure evidence.
                group["status"] = "cancelled"
                group["closed_at"] = now()
                group.setdefault("events", []).append({"at": now(), "type": "closed_by_cancel",
                                                       "reason": args.reason})
        for auth in run.get("recovery_authorizations", []):
            if auth.get("status") == "active":
                revoke_slots(auth)
        run["events"].append({"at": now(), "type": "cancel", "reason": args.reason,
                              "cancel_scope": "execution_history"})
        save(run_file(args.run_id), run)
    return {"run_id": args.run_id, "status": "cancelled", "finished": False,
            "cancel_scope": "execution_history"}


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
    leadp = sp.add_parser("name-lead")
    leadp.add_argument("--current"); leadp.add_argument("--expect-session", help="required for --apply: agent_session.value from preview"); leadp.add_argument("--apply", action="store_true", help="rename using the previewed --expect-session")
    sp.add_parser("status")
    runp = sp.add_parser("run")
    rsp = runp.add_subparsers(dest="run_cmd", required=True)
    start = rsp.add_parser("start"); start.add_argument("--goal", required=True)
    rsp.add_parser("list")


    def _positive_timeout(value):
        parsed = int(value)
        if parsed < 1:
            raise argparse.ArgumentTypeError("--wait-timeout-seconds must be >= 1")
        return parsed

    for cmd in ("status", "stage", "finish", "pause", "resume", "approve", "dispatch", "collect", "gate",
                "recover", "cancel", "drive", "diagnose", "abandon", "recover-attempt"):
        sub = rsp.add_parser(cmd)
        sub.add_argument("--run-id", required=True)
        if cmd == "cancel":
            sub.add_argument("--confirm-run-id", required=True)
            sub.add_argument("--reason", required=True)
        if cmd == "resume": sub.add_argument("--extend-rounds", type=int, default=0)
        if cmd == "drive":
            sub.add_argument("--resume", action="store_true")
            sub.add_argument("--max-steps", type=int, default=20)
            sub.add_argument("--deadline-seconds", type=int, default=1800)
            sub.add_argument("--wait-timeout-seconds", type=_positive_timeout)
        if cmd == "approve":
            sub.add_argument("--role", required=True); sub.add_argument("--effect", required=True, choices=("read", "write", "sensitive")); sub.add_argument("--reason", required=True)
        if cmd == "dispatch":
            sub.add_argument("--role", required=True); sub.add_argument("--message", required=True)
            sub.add_argument("--effect", default="read", choices=("read", "write", "sensitive"))
            sub.add_argument("--remediation", action="store_true", help="delegate a corrective subtask to a different role while keeping the current gate")
            sub.add_argument("--no-wait", action="store_true", help="submit without waiting; explicit run collect required")
            sub.add_argument("--wait-timeout-seconds", type=_positive_timeout)
        if cmd == "collect": sub.add_argument("--dispatch-id", required=True)
        if cmd == "diagnose": sub.add_argument("--dispatch-id", required=True)
        if cmd == "recover-attempt":
            sub.add_argument("--dispatch-id", required=True)
            sub.add_argument("--evidence", required=True, help="snapshot id from `run diagnose`")
        if cmd == "abandon":
            sub.add_argument("--dispatch-id", required=True)
            sub.add_argument("--reason", required=True)
            sub.add_argument("--evidence", required=True, help="snapshot id from `run diagnose`")
            sub.add_argument("--confirm-untracked", action="store_true",
                             help="record explicit risk acceptance when absence is unproven")
            sub.add_argument("--quarantine-invalid", action="store_true",
                             help="retain the raw bytes of an invalid artifact before abandoning")
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
            emit(pending_plan(initial_plan(cfg, args.template, args))); return 0
        if args.cmd == "init": emit(init(cfg, args)); return 0
        if args.cmd == "adopt": emit(adopt(cfg, args)); return 0
        if args.cmd == "name-lead": emit(name_lead(args)); return 0
        if args.cmd == "status":
            team, herdr_session, ownership = _current_team_with_session(tier="A")
            live = []
            for r in team["roles"]:
                actual = lookup_agent(r["agent_name"], session=herdr_session)
                live.append({"role": r["role"], "agent_name": r["agent_name"],
                             "registered_pane": r["pane_id"], "live": bool(actual),
                             "observed_state": agent_status_of(actual) if isinstance(actual, dict) else "missing"})
            emit({"template": team["template"], "stored_status": team["status"], "agents": live,
                  "worktree": team.get("worktree"), "ledger_session": ownership}); return 0
        if args.cmd == "run":
            if args.run_cmd == "start": emit(run_start(cfg, args)); return 0
            if args.run_cmd == "list": emit(run_list()); return 0
            if args.run_cmd in ("status", "stage"):
                run = require_run(args.run_id, tier="A")
                emit(run if args.run_cmd == "status" else stage_info(run)); return 0
            action = {"approve": run_approve, "dispatch": run_dispatch, "collect": run_collect, "gate": run_gate,
                      "finish": run_finish, "pause": lambda a: run_pause(a,True),
                      "resume": lambda a: run_pause(a,False), "recover": run_recover,
                      "cancel": run_cancel, "drive": run_drive, "diagnose": _run_diagnose,
                      "abandon": run_abandon, "recover-attempt": run_recover_attempt}[args.run_cmd]
            emit(action(args)); return 0
    except (Error, TypeError, KeyError, ValueError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    return 0

if __name__ == "__main__":
    sys.exit(main())
