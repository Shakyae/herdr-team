# herdr-team 阶段化联调方案（Herdr 0.9.5 · 派单通信链路）

- 适用版本：herdr-team v4.2.0-beta.1（`scripts/htm.py`，HEAD `88deef9`）、Herdr 0.9.5
- 文档状态：**方案，未执行**。本文档不被文档修订轮执行；创建 workspace / tab / pane、启动 agent、`htm init`、`run start/dispatch/collect/gate/cancel`、关闭资源等操作，均需用户单独、明确授权。
- 读者：能在 Herdr 内操作 CLI 的工程师或 Agent。命令可复制，但必须先完成 §3 的初始化与 `abort()` 定义。
- 阶段：A（单 Worker 通信验证）→ B（HTM 调度/结果/异常验证）。**A 不 Go，不得进入 B**。

---

## 1. Context 与修订说明

上一版方案（911 行）在可执行性上有 10 类缺陷：失败分支只打印不退出；重复派单序列没有落到账本状态；报告归属从 dispatch 响应读取不存在的 `run_id`；超时用例从失败命令的 stdout 取 dispatch id；在真实 state 里与活跃 worker 抢写同一工件；断言用“忽略退出码”的写法（强制成功、丢弃 stderr）掩盖失败；blocked 场景主动请求危险操作；清理前不校验归属；单阶段覆盖过多、缺少独立门槛；agent 与 prompt 数量失控。

### 1.1 十项修正映射

| # | 修正 | 落地位置 |
|---|---|---|
| 1 | 所有 ABORT/STOP 统一 `abort()` 并真正 `exit 1`；workspace/tab/pane ID 校验非空、非 `null`、格式、前缀一致 | §3.1、§3.3 |
| 2 | 重复派单精确序列：`--no-wait` → 账本断言 `submitted_unsettled` → 第二次派单**断言被拒**（exit=2 + stderr 正则）→ `herdr agent wait` → `collect --dispatch-id <账本里的真实 id>` | §5.3–§5.5 |
| 3 | 报告归属**绝不**从 dispatch 响应读 `run_id`；run_id 只来自 `htm run list` 与 `$HTM_STATE_DIR/runs/*.json` | §5.7 |
| 4 | 超时用例用 run 文件集合差集定位新 run，再从 `dispatches[-1].id` 取 dispatch id 并断言唯一 | §5.9 |
| 5 | 明确「同一 pane 只允许一个 non-terminal run」，给出只读断言 | §3.5、§5.8 |
| 6 | 错误报告注入改为独立 Mock：`cp -R` 一份 state 到 `$MOCK/state`，在副本中伪造账本与工件后直接 `collect` | §5.10 |
| 7 | 16 条机器断言重写，无 `\|\| true`、无 `2>/dev/null`；每条含编号/对象/命令/通过条件/失败诊断；身份不符按 JSON 判定 | §6 |
| 8 | Blocked 只做被动观察，不构造危险操作；未发生标「未验证」；禁止 send-keys 代答 | §5.12 |
| 9 | 清理前依次断言资源前缀、worker 消失、workspace 已关闭且不在列表、临时目录安全；失败即 abort 并保留现场 | §8.2 |
| 10 | 拆阶段 A / B，各有独立 Go/No-Go；A 不 Go 不进 B | §4.6、§5.14 |

### 1.2 agent 与 prompt 预算（硬性）

- 全程只注册 **1 个 team**（`dev-lite`）；`init` 创建 `developer` + `reviewer` 两个 worker，**只向 developer 发 prompt**，reviewer 从不被驱动。测试 agent 总数 = 2，被驱动的 = 1。
- 阶段 A **不额外创建探针**：直接以主 team 的 developer 作为探针（2 次 prompt：正常 + 超时）。阶段 A 与阶段 B 复用同一个 agent。
- 阶段 B 的 htm prompt 共 3 次：D1（`--no-wait`）、D2、超时派单。
- 超时用例在同一 team 内完成（默认 `task_timeout_seconds=300`，用 `sleep 420` 触发真实超时），不注册第二个 team、不建第二个 Lead。
- 全程不构造危险审批、不改任何仓库代码。

### 1.3 范围

验证两条链路：

```text
htm run dispatch ...  -> herdr agent prompt <agent> TEXT --wait --until idle --until done --until blocked --timeout <ms>
htm run collect --dispatch-id DID -> herdr agent get <agent> -> agent_status_of() -> read_result()
```

验证：dispatch id 生成与账本状态机、真实 `prompt --wait` 返回的形状与状态识别、报告归属 fail-closed、超时/未结算不自动重发。

不验证：业务交付质量、模型能力、`run approve` 正常通过路径（v4.2 恒拒绝）、多机、worktree（统一 `--worktree off`）。

---

## 2. 执行检查表（Pre-flight，逐项勾选）

### 2.1 授权与前置

- [ ] 用户已明确授权：1 个测试 workspace、1 个 tab（`init` 自建）、1 个 team（2 个 worker）、共 5 次 prompt
- [ ] 执行环境在 Herdr 内，`HERDR_ENV=1` 且 `HERDR_WORKSPACE_ID` / `HERDR_TAB_ID` / `HERDR_PANE_ID` 均非空；缺失则**停止**并修启动器，禁止手工伪造
- [ ] 已确认 `config/team.yaml` 默认 `max_rounds=5, max_retries=1, max_parallel=3, task_timeout_seconds=300`，governance `autonomy=workspace-write, approval=boundary`
- [ ] 已确认不改 `scripts/htm.py`、`tests/`、`pyproject.toml`、`uv.lock`、`SKILL.md`、`config/`
- [ ] 已确认不碰 `~/pagi`、`w1`、`w2`（见 §3.2 受保护清单）
- [ ] 已阅读 §3.1 的 `abort()` / ID 校验器，并在每个 shell 中定义

### 2.2 阶段 A（单 Worker 通信）

- [ ] 探针就是主 team 的 developer（`ht…`），不额外创建 agent
- [ ] 正常 prompt（A1）exit=0，`agent_status_of` 落在 `{idle, done}` 且非 `unknown`
- [ ] 超时 prompt（A2）得到 timeout 类错误，且**不**据此断言输入未送达
- [ ] 证据写入 `$EVIDENCE/phaseA/`
- [ ] 阶段 A Go/No-Go 已判定为 **Go**

### 2.3 阶段 B（HTM 调度）

- [ ] 仅在 A=Go 后开始
- [ ] 只有一个 team，`init` 时加 `--set orchestration.max_retries=5`
- [ ] 主 run 是该 pane 上唯一的 non-terminal run（见 §3.5）
- [ ] 已记录 dispatch 响应的 key 集合，确认其中**没有** `run_id`
- [ ] B1–B12 中每条断言都走了 §6 编号断言，失败即 abort
- [ ] 证据写入 `$EVIDENCE/phaseB/`
- [ ] 阶段 B Go/No-Go 已判定
- [ ] reviewer 全程未被驱动（0 次 prompt）

### 2.4 收尾

- [ ] Mock 测试明确标记为 Mock，未污染真实 state
- [ ] 清理前归属断言全部通过（§8.2）
- [ ] 结论按「真实 / Mock / 未验证」三列表写出（§9）
- [ ] 未把「调度通信链路验证」表述成「自主监督闭环验证」

---

## 3. 安全护栏与环境

### 3.1 统一定义（每个 shell 必须先执行）

```bash
set -u
abort() { printf 'ABORT: %s\n' "$*" >&2; exit 1; }
need() {
  [ -n "${1:-}" ] && [ "${1:-}" != "null" ] || abort "${2:-missing value}"
}
need_ws() {
  printf '%s' "${1:-}" | grep -Eq '^w[0-9]+$' || abort "非法 workspace id: ${1:-<empty>}"
}
need_tab() {
  printf '%s' "${1:-}" | grep -Eq '^w[0-9]+:t[0-9]+$' || abort "非法 tab id: ${1:-<empty>}"
}
need_pane() {
  printf '%s' "${1:-}" | grep -Eq '^w[0-9]+:p[0-9]+$' || abort "非法 pane id: ${1:-<empty>}"
}
same_ws() {
  [ "${2%%:*}" = "$1" ] || abort "$3: 前缀不一致 ($1 vs $2)"
}
assert_no_protected() {
  case "$1" in w1:*|w2:*) abort "$2 落在受保护 workspace: $1";; esac
}
assert_single_active_run() {   # $1=workspace $2=lead_pane
  local ws="$1" pane="$2" dir="$HTM_STATE_DIR/runs" n=0 f
  if [ -d "$dir" ]; then
    for f in "$dir"/*.json; do
      [ -e "$f" ] || continue
      n=$((n + $(jq -r --arg ws "$ws" --arg pane "$pane" '
        if .context.workspace==$ws and .context.lead_pane==$pane
           and (.status!="finished" and .status!="cancelled") then 1 else 0 end' "$f")))
    done
  fi
  [ "$n" -le 1 ] || abort "pane $pane 上有 $n 个 non-terminal run"
}
```

一次性初始化变量：

```bash
export REPO=/Users/shakya/dev/herdr-team
export RUNTOKEN=$(python3 -c 'import secrets; print(secrets.token_hex(4))')
export ITEST_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/htm-itest-$RUNTOKEN.XXXXXX") || abort "mktemp 失败"
case "$ITEST_ROOT" in
  "${TMPDIR:-/tmp}"/htm-itest-*) ;;
  *) abort "临时目录前缀异常: $ITEST_ROOT" ;;
esac
export HTM_STATE_DIR="$ITEST_ROOT/state"
export EVIDENCE="$ITEST_ROOT/evidence"
mkdir -p "$EVIDENCE/phaseA" "$EVIDENCE/phaseB" || abort "无法创建证据目录"
htm() { python3 "$REPO/scripts/htm.py" "$@"; }
echo "RUNTOKEN=$RUNTOKEN ITEST_ROOT=$ITEST_ROOT"
```

要点：

- `HTM_STATE_DIR` 在模块导入时读取（`scripts/htm.py:34`），必须每条命令都带着它；未设置时默认 `~/.local/state/herdr-team`。
- 所有 ABORT 必须 `exit 1`；不允许 `echo "ABORT: ..."` 后继续执行。

### 3.2 隔离与受保护资源

| 资源 | 约定 |
|---|---|
| workspace label | `htm-itest-$RUNTOKEN`（不得复用 `w1` pagi、`w2` herdr-team） |
| tab | `init --layout tab` 自建 `htm-dev-lite`；本方案不另建 tab |
| 探针 / 派单 agent | 同一个 `ht<digest>-1-developer`（`init` 生成），阶段 A 与阶段 B 复用 |
| reviewer | `init` 生成但全程 0 次 prompt；失败时如实记录，不得为了“跑通”而驱动它 |
| cwd | `$ITEST_ROOT`，禁止 `~/pagi`、`~/dev/herdr-team` |
| state | `$HTM_STATE_DIR=$ITEST_ROOT/state` |

受保护 pane（测试期间只读观察，绝不写入）：

```text
w1:p1  pagi Lead
w1:p5  developer
w1:p6  reviewer
w2:p1  herdr-team Lead（本会话）
w2:p2  developer-w2
w2:p3  reviewer-w2
```

### 3.3 创建测试 workspace（阶段 A 前）

先采集基线（任何写操作之前），放入 `$EVIDENCE/phaseB/`，供 §5.13 复核：

```bash
herdr agent list \
  | jq -r '.result.agents[] | [.workspace_id, .pane_id, (.name // "-"), (.agent_status // "-"), (.revision|tostring), (.state_change_seq|tostring)] | @tsv' \
  | sort > "$EVIDENCE/phaseB/00-agents-before.tsv" || abort "基线 agent list 失败"
herdr workspace list \
  | jq -r '.result.workspaces[] | [.workspace_id, (.label // "-")] | @tsv' \
  | sort > "$EVIDENCE/phaseB/00-workspaces-before.tsv" || abort "基线 workspace list 失败"
git -C "$REPO" rev-parse HEAD > "$EVIDENCE/phaseB/00-head.txt" || abort "记录 HEAD 失败"
git -C "$REPO" status --porcelain > "$EVIDENCE/phaseB/00-git-status.txt" || abort "记录 git status 失败"
for p in w1:p1 w1:p5 w1:p6 w2:p1 w2:p2 w2:p3; do
  awk -v p="$p" '$2==p' "$EVIDENCE/phaseB/00-agents-before.tsv" | grep -q . \
    || abort "基线缺少受保护 pane: $p"
done
```

```bash
WS_JSON=$(herdr workspace create --label "htm-itest-$RUNTOKEN" --cwd "$ITEST_ROOT" --focus) || abort "workspace create 失败"
export ITEST_WS=$(printf '%s' "$WS_JSON" | jq -er '.result.workspace.workspace_id') || abort "取不到 workspace id"
export ITEST_TAB=$(printf '%s' "$WS_JSON" | jq -er '.result.tab.tab_id') || abort "取不到 tab id"
export ITEST_ROOT_PANE=$(printf '%s' "$WS_JSON" | jq -er '.result.root_pane.pane_id') || abort "取不到 root pane id"
need_ws "$ITEST_WS"; need_tab "$ITEST_TAB"; need_pane "$ITEST_ROOT_PANE"
same_ws "$ITEST_WS" "$ITEST_TAB" "workspace/tab"
same_ws "$ITEST_WS" "$ITEST_ROOT_PANE" "workspace/pane"
case "$ITEST_WS" in w1|w2) abort "测试 workspace 不独立: $ITEST_WS";; esac
```

### 3.4 注册唯一的 team（阶段 A 前）

阶段 A 的探针就是这里生成的 developer，因此 team 必须在阶段 A 之前注册。**必须在测试 workspace 的根 pane（`$ITEST_ROOT_PANE`）里执行**，先断言身份：

```bash
[ "${HERDR_ENV:-}" = "1" ] || abort "HERDR_ENV 缺失；禁止伪造"
[ "${HERDR_WORKSPACE_ID:-}" = "$ITEST_WS" ] || abort "不在测试 workspace（期望 $ITEST_WS）"
[ "${HERDR_PANE_ID:-}" = "$ITEST_ROOT_PANE" ] || abort "不在测试根 pane（期望 $ITEST_ROOT_PANE）"
[ "$(pwd)" = "$ITEST_ROOT" ] || abort "cwd 不是临时目录: $(pwd)"

htm validate > "$EVIDENCE/phaseB/00-validate.json" || abort "validate 失败"
jq -e '.valid == true' "$EVIDENCE/phaseB/00-validate.json" >/dev/null || abort "validate 未返回 valid"
htm init dev-lite --layout tab --worktree off --cwd "$ITEST_ROOT" \
  --set orchestration.max_retries=5 > "$EVIDENCE/phaseB/01-init.json" || abort "init 失败"
jq -e '.reused == false' "$EVIDENCE/phaseB/01-init.json" >/dev/null \
  || abort "init 返回 reused=true；先人工核对，不得重跑"
export DEV=$(jq -er '.team.roles[]|select(.role=="developer")|.agent_name' "$EVIDENCE/phaseB/01-init.json") || abort "取不到 developer"
export DEV_PANE=$(jq -er '.team.roles[]|select(.role=="developer")|.pane_id' "$EVIDENCE/phaseB/01-init.json") || abort "取不到 developer pane"
export REV=$(jq -er '.team.roles[]|select(.role=="reviewer")|.agent_name' "$EVIDENCE/phaseB/01-init.json") || abort "取不到 reviewer"
need_pane "$DEV_PANE"
same_ws "$ITEST_WS" "$DEV_PANE" "workspace/developer"
assert_no_protected "$DEV_PANE" "developer"
case "$DEV" in ht*) ;; *) abort "worker 名不符合 ht<digest>-N-<role>: $DEV";; esac
case "$REV" in ht*) ;; *) abort "worker 名不符合 ht<digest>-N-<role>: $REV";; esac
htm status > "$EVIDENCE/phaseB/02-status.json" || abort "htm status 失败"
jq -e '.agents[] | select(.role=="developer") | .live==true and (.observed_state=="idle" or .observed_state=="done")' \
  "$EVIDENCE/phaseB/02-status.json" >/dev/null || abort "developer 未处于 idle/done"
jq -e '.agents[] | select(.role=="reviewer") | .live==true' "$EVIDENCE/phaseB/02-status.json" >/dev/null \
  || abort "reviewer 未就绪（它不会被驱动，但必须存在）"
```

要点：

- `--layout tab` 让 worker 独占 `init` 自建的 `htm-dev-lite` tab，与 Lead 分屏；`--worktree off` 避免触碰任何 Git 仓库。
- `--set orchestration.max_retries=5` 是必须的：同一 stage_attempt 内同一 role 的派单上限是 `1+max_retries`，默认 1 只允许 2 次，而本方案在同一次 run 内需要 D1 + D2 + 超时共 3 次。
- `agent start` 没有 `--cwd`；worker 的 cwd 来自 `init` 的 `--cwd`。`agent_not_ready` 表示启动期被弹窗阻塞，由**人**处理，不自动应答。

### 3.5 单 pane 单 Run 约束（只读断言）

`run_start` 会拒绝与其 `(workspace, pane_id)` 重叠的未完成 Run（`scripts/htm.py:591-598`）。本方案额外要求执行者主动断言——使用 §3.1 已定义的 `assert_single_active_run <ws> <pane>`，它在 `runs/` 目录不存在时按 0 处理，不使用 `2>/dev/null`，逐文件读取以避免 `jq -s` 对空 glob 的行为差异：

```bash
# 建 run 前与建 run 后各执行一次
assert_single_active_run "$ITEST_WS" "$ITEST_ROOT_PANE"
```

### 3.6 已核实 CLI / 实现事实

**Herdr**

- `agent start <name> --kind <kind> --pane <pane> [--timeout ms]`，**无 `--cwd`**。
- `agent prompt <target> <text> [--wait] [--until STATUS]... [--timeout MS]`。
- `pane split [--cwd] [--env] [--no-focus]`；`workspace/tab create [--cwd] [--label] [--no-focus]`；`workspace close <id> [--group]`、`tab close <id>`、`pane close <id>`。
- `--wait` 从非 `working` 状态提交后 5 秒内必须观察到 `working`/`blocked`，否则 `agent_prompt_stalled`；调用方 `--timeout` 先到则 `timeout`。两者**都不证明输入未送达**。
- 返回结构：`workspace create` → `.result.workspace` / `.result.tab` / `.result.root_pane`；`tab create` → `.result.tab` / `.result.root_pane`；`pane split` → `.result.pane`；`agent get` → `.result.agent`；`agent list` → `.result.agents`；`pane list` → `.result.panes`。
- Agent 字段：`agent`（kind 字符串）、`agent_status`、`pane_id`、`revision`、`state_change_seq`、`cwd`、`name`。

**htm**

| 事实 | 位置 |
|---|---|
| 默认 `max_rounds=5, max_retries=1, max_parallel=3, task_timeout_seconds=300` | `config/team.yaml` |
| 校验范围：max_rounds 1..30、max_retries 0..5、max_parallel 1..8、task_timeout 15..300 | `scripts/htm.py:46-47` |
| 派单顺序：active → actor 匹配（否则需 `--remediation`）→ 在途检查 → retries 上限 `1+max_retries` → max_parallel → approval → `verify_existing` → 状态必须 `idle\|done` | `:700-810` |
| 在途报错：`Pending/uncertain dispatch <id>; use run collect, never blindly resend` | `:714-718` |
| 先写账本 `submission_uncertain`，再调 `agent prompt ... --wait ...` | `:769`、`:798-802` |
| 派单响应字段只有 `dispatch_id/status/agent_state/report/error/warning`，**没有 `run_id`** | `:784-808` |
| `--no-wait` 成功改 `submitted_unsettled`，返回 `next` | `:787-795` |
| 任何 `Error`（含超时）只补 `error`，status 保持 `submission_uncertain`，抛 `Dispatch <id> uncertain; input might have been sent. Run collect before retry.` | `:806-811` |
| `collect` 只吃 `--dispatch-id` | `:820-836` |
| `reconcile_result`：非 `idle\|done\|blocked` → `uncertain` + `Unexpected agent status`；读报告失败 → `uncertain`（observed 为 blocked 则 `blocked`）；成功 → `settled`/`blocked` | `:683-700` |
| `read_result` 只读 `artifacts/<run>/<dispatch>.json`，校验身份/形状，非安全边界；不符抛 `Result identity mismatch: <key>` | `:646-682` |
| `run_gate`：pass/revise 前要求无未 settled；非 lead 要求最新报告 verdict=pass 且字节未变；测试阶段要求 tests 全 0；evidence ≥12；超轮 → `blocked_round_limit` | `:838-903` |
| `run_approve` 恒抛错 | `:634-637` |
| run_id 12 位 hex、dispatch_id 10 位 hex | `:640-644` |
| worker 名 `ht<digest>-N-<role>`；team 文件 `teams/<sha256(workspace\|lead_pane)[:16]>.json`；run 文件 `runs/<12位>.json` | `:548-549`、`:249-251` |
| `--config` 是全局参数，必须在子命令前 | `:992-996` |

`--set orchestration.max_retries=5` 的必要性：同一 stage_attempt 内对同一 role 的派单上限是 `1+max_retries`。默认 1 只允许 2 次；本方案在 `implement` 上需要 D1、D2、超时共 3 次，并预留 collect 未结算后的重派余量，因此固定到上限 5（允许 6 次）。`task_timeout_seconds` 在 `init` 时固化（默认 300），事后不能改，所以超时用例靠 worker 侧 `sleep 420` 触发，而不是改配置或开第二个 team。

命名说明：`htm` 的 worker 由 `init` 生成 `ht…`，`verify_existing()` 按 `team["agent_name"]` 查活体，因此**不能**把派单目标改成探针名。探针只用于阶段 A 的原始返回实测，不进入 htm 账本。

---

## 4. 阶段 A：单 Worker 通信验证

### 4.1 目的

在最小成本下确认：真实 Herdr 0.9.5 的 `agent prompt --wait` 返回形状能被 `agent_status_of` 解释为 `idle`/`done`，超时返回 `Error` 时不能让调用方误判为已送达/已成功。阶段 A 直接使用 §3.4 生成的 `$DEV`（同一个 agent 后续也会被 htm 派单），**不额外创建探针**。

### 4.2 前置

§3.3、§3.4 完成；`herdr agent get "$DEV"` 的 `.result.agent.agent_status` 为 `idle`/`done`。若为其他值，先 `herdr agent wait "$DEV" --timeout 120000`；仍非 `idle/done` 则 abort，不得发送。

```bash
herdr agent get "$DEV" \
  | jq -r '.result.agent | {name, pane_id, agent, agent_status, cwd}' \
  > "$EVIDENCE/phaseA/00-probe-agent.json" || abort "读取 developer 失败"
jq -e --arg p "$DEV_PANE" '.pane_id==$p and (.agent_status|IN("idle","done"))' \
  "$EVIDENCE/phaseA/00-probe-agent.json" >/dev/null || abort "developer 不在预期 pane 或未 idle/done"
```

### 4.3 A1 正常 prompt（第 1 次）

```bash
A1_OUT="$EVIDENCE/phaseA/10-prompt-ok.json"
A1_ERR="$EVIDENCE/phaseA/10-prompt-ok.err"
rc=0
herdr agent prompt "$DEV" '只回复 ITEST_PONG，然后结束本轮。不要读写任何文件。' \
  --wait --timeout 120000 >"$A1_OUT" 2>"$A1_ERR" || rc=$?
[ "$rc" -eq 0 ] || abort "A1 prompt 非零退出: $rc（见 $A1_ERR）"
jq -e . "$A1_OUT" >/dev/null || abort "A1 返回不是合法 JSON"
jq -r 'keys[]' "$A1_OUT" > "$EVIDENCE/phaseA/11-return-keys.txt"
jq -r '.result | keys[]' "$A1_OUT" > "$EVIDENCE/phaseA/12-result-keys.txt"
jq -r '.result | to_entries[] | "\(.key):\(.value|type)"' "$A1_OUT" \
  > "$EVIDENCE/phaseA/13-result-shape.txt"
```

解析验证（只记录 key 与短值，不记录长文本）：

```bash
python3 - "$A1_OUT" "$REPO/scripts/htm.py" <<'PY' > "$EVIDENCE/phaseA/14-status-parse.txt"
import importlib.util, json, sys
doc_path, module = sys.argv[1], sys.argv[2]
spec = importlib.util.spec_from_file_location("htm", module)
h = importlib.util.module_from_spec(spec); spec.loader.exec_module(h)
doc = json.load(open(doc_path, encoding="utf-8"))
res = doc.get("result") if isinstance(doc, dict) else None
print("doc_status_of =", h.agent_status_of(doc))
print("result_status_of =", h.agent_status_of(res))
print("doc_kind_of =", h.agent_kind_of(doc))
PY
cat "$EVIDENCE/phaseA/14-status-parse.txt"
grep -Eq 'doc_status_of = (idle|done)$|result_status_of = (idle|done)$' "$EVIDENCE/phaseA/14-status-parse.txt" \
  || abort "A1 状态无法解析为 idle/done（记录为需补映射，No-Go）"
```

### 4.4 A2 超时 prompt（第 2 次）

用 15 秒调用方超时对 `sleep 45` 的任务发 prompt，确认超时是错误而非成功：

```bash
A2_OUT="$EVIDENCE/phaseA/20-prompt-timeout.json"
A2_ERR="$EVIDENCE/phaseA/20-prompt-timeout.err"
rc=0
herdr agent prompt "$DEV" '先执行 sleep 45，再回复 ITEST_SLOW_DONE。不要读写文件。' \
  --wait --timeout 15000 >"$A2_OUT" 2>"$A2_ERR" || rc=$?
[ "$rc" -ne 0 ] || abort "A2 未超时（期望超时错误）"
grep -Eqi 'timeout|timed out|stalled' "$A2_ERR" "$A2_OUT" \
  || abort "A2 失败但不是 timeout/stalled 类错误"
echo "A2 rc=$rc（失败不代表输入未送达）" > "$EVIDENCE/phaseA/21-timeout-summary.txt"
```

随后等待 developer 稳定并记录观察值（只读，不发 prompt）：

```bash
herdr agent wait "$DEV" --timeout 180000 || abort "A2 后 agent wait 失败"
herdr agent get "$DEV" | jq -r '.result.agent | [.name, .agent_status, .revision, .state_change_seq] | @tsv' \
  > "$EVIDENCE/phaseA/22-after-timeout.tsv" || abort "A2 后 agent get 失败"
```

观察值为 `blocked` 时只做记录（见 §5.12 的被动观察规则），不判失败，也不代答。

### 4.5 阶段 A 证据

`$EVIDENCE/phaseA/` 至少包含：`00-probe-agent.json`、`10/11/12/13/14-*`、`20-prompt-timeout.{json,err}`、`21-timeout-summary.txt`、`22-after-timeout.tsv`。

### 4.6 阶段 A Go/No-Go

| 项 | Go 条件 |
|---|---|
| A1 exit | 0 |
| A1 状态解析 | `agent_status_of` 对顶层或 `.result` 至少一处返回 `idle` 或 `done`；`unknown` 不算通过 |
| A1 kind 解析 | `agent_kind_of` 返回 `claude`（形状确认） |
| A2 | 非零退出且错误含 timeout/stalled；已记录“失败不等于未送达” |
| 结论记录 | 写入 `$EVIDENCE/phaseA/GO-NOGO.txt`，首行 `GO` 或 `NO-GO` |

任一项不满足 → **NO-GO，停止**，不创建任何 Run，不进入阶段 B。若 A1 解析为 `unknown`，在报告里写“需补映射”，不得静默通过。

### 4.7 进入阶段 B 的唯一条件

`grep -qx 'GO' "$EVIDENCE/phaseA/GO-NOGO.txt" || abort "阶段 A 未 GO，禁止进入阶段 B"`。

---

## 5. 阶段 B：HTM 调度、结果、异常验证

阶段 B 只对 `developer` 派单；`reviewer` 由 `init` 创建但**不发 prompt**。team 已在 §3.4 注册（同一 team，`max_retries=5`，默认 `task_timeout_seconds=300`），超时用 `sleep 420` 在同一 team 内触发真实超时。

### 5.1 B0 前置复核（team 已由 §3.4 注册）

```bash
[ -n "${DEV:-}" ] && [ -n "${DEV_PANE:-}" ] && [ -n "${REV:-}" ] || abort "§3.4 未完成"
[ -n "${ITEST_WS:-}" ] && [ -n "${ITEST_ROOT_PANE:-}" ] || abort "§3.3 未完成"
htm status > "$EVIDENCE/phaseB/02b-status.json" || abort "htm status 失败"
jq -e '.agents[] | select(.role=="developer") | .live==true and (.observed_state=="idle" or .observed_state=="done")' \
  "$EVIDENCE/phaseB/02b-status.json" >/dev/null || abort "developer 未处于 idle/done"
jq -e '.agents[] | select(.role=="reviewer") | .live==true' "$EVIDENCE/phaseB/02b-status.json" >/dev/null \
  || abort "reviewer 未就绪"
assert_single_active_run "$ITEST_WS" "$ITEST_ROOT_PANE"
```

### 5.2 B1 建 run 并推进到 implement

```bash
RUN_JSON=$(htm run start --goal '阶段 B：验证 dispatch/collect、账本状态机与异常降级，不改任何仓库') || abort "run start 失败"
export RUN=$(printf '%s' "$RUN_JSON" | jq -er '.run_id') || abort "取不到 run_id"
printf '%s' "$RUN" | grep -Eq '^[a-f0-9]{12}$' || abort "run_id 形状非法"
export RUN_FILE="$HTM_STATE_DIR/runs/$RUN.json"
[ -f "$RUN_FILE" ] || abort "run 文件不存在: $RUN_FILE"
assert_single_active_run "$ITEST_WS" "$ITEST_ROOT_PANE"

htm run stage --run-id "$RUN" | jq -e '.stage=="plan_brief"' >/dev/null || abort "首阶段不是 plan_brief"
htm run gate --run-id "$RUN" --decision pass \
  --evidence 'plan_brief：本轮仅验证派单与回收链路，验收标准见 docs/integration-test-plan.md' \
  > "$EVIDENCE/phaseB/03-gate-plan.json" || abort "plan_brief gate 失败"
htm run stage --run-id "$RUN" | jq -e '.stage=="implement" and .actor=="developer"' >/dev/null \
  || abort "未推进到 implement/developer"
```

### 5.3 B2 首次派单（`--no-wait`，第 1 次 htm prompt）

派单前记录 key 形状，确认响应没有 `run_id`：

```bash
D1_OUT="$EVIDENCE/phaseB/10-dispatch1.json"
rc=0
htm run dispatch --run-id "$RUN" --role developer --effect read --no-wait \
  --message '只回复 ITEST_D1_OK；按提示路径原子写入 JSON 报告。不要修改任何仓库。' \
  > "$D1_OUT" || rc=$?
[ "$rc" -eq 0 ] || abort "首次派单失败 rc=$rc"
jq -e . "$D1_OUT" >/dev/null || abort "派单响应不是 JSON"
jq -r 'keys | join(",")' "$D1_OUT" > "$EVIDENCE/phaseB/11-dispatch-keys.txt"
if grep -q 'run_id' "$EVIDENCE/phaseB/11-dispatch-keys.txt"; then
  abort "派单响应出现 run_id（与源码事实不符）"
fi
grep -qx 'dispatch_id,status,next' "$EVIDENCE/phaseB/11-dispatch-keys.txt" \
  || abort "派单响应 key 与预期不符: $(cat "$EVIDENCE/phaseB/11-dispatch-keys.txt")"
```

从**账本**取真实 id（不从响应之外的任何推断），并断言与响应一致：

```bash
export D1=$(jq -er '.dispatches[-1].id' "$RUN_FILE") || abort "账本无 dispatch"
printf '%s' "$D1" | grep -Eq '^[a-f0-9]{10}$' || abort "D1 形状非法: $D1"
[ "$(jq -r '.dispatch_id' "$D1_OUT")" = "$D1" ] || abort "响应 dispatch_id 与账本不一致"
[ "$(jq -r '.status' "$D1_OUT")" = "submitted_unsettled" ] || abort "no-wait 响应状态不是 submitted_unsettled"
jq -e --arg d "$D1" '.dispatches[] | select(.id==$d) | .status=="submitted_unsettled"' "$RUN_FILE" >/dev/null \
  || abort "账本未记录 submitted_unsettled"
```

### 5.4 B3 在途重复派单必须被拒（不发 prompt）

```bash
DUP_ERR="$EVIDENCE/phaseB/12-dup-dispatch.txt"
rc=0
htm run dispatch --run-id "$RUN" --role developer --effect read --message '重复派单探测' \
  > "$DUP_ERR" 2>&1 || rc=$?
[ "$rc" -eq 2 ] || abort "在途重复派单未被拒绝：exit=$rc"
grep -Eq 'Pending/uncertain dispatch [a-f0-9]{10}; use run collect, never blindly resend' "$DUP_ERR" \
  || abort "拒绝原因不符: $(cat "$DUP_ERR")"
jq -e --arg d "$D1" '.dispatches[] | select(.id==$d) | .status=="submitted_unsettled"' "$RUN_FILE" >/dev/null \
  || abort "重复派单改写了旧记录状态"
```

### 5.5 B4 等待并 collect 该 id

```bash
herdr agent wait "$DEV" --timeout 240000 || abort "developer 等待超时"
htm run collect --run-id "$RUN" --dispatch-id "$D1" > "$EVIDENCE/phaseB/13-collect-d1.json" \
  || abort "collect D1 失败"
jq -e --arg d "$D1" '.dispatch_id==$d and .status=="settled"' "$EVIDENCE/phaseB/13-collect-d1.json" >/dev/null \
  || abort "D1 未 settled: $(cat "$EVIDENCE/phaseB/13-collect-d1.json")"
```

说明：`collect` 不等待；若 worker 仍 `working`，会得到 `uncertain` + `Unexpected agent status: working`。这属于设计的 fail-closed；遇到时记录后 `agent wait` 再 collect 一次，但仍须最终 `settled` 才继续。

### 5.6 B5 结算后再派单必须产生新 id（第 2 次 htm prompt）

```bash
# dispatch 要求 worker 处于 idle/done；上一轮刚结束先等稳定
herdr agent wait "$DEV" --timeout 120000 || abort "D2 前等待 developer 失败"
herdr agent get "$DEV" | jq -e '.result.agent.agent_status|IN("idle","done")' >/dev/null \
  || abort "D2 前 developer 不是 idle/done"

D2_OUT="$EVIDENCE/phaseB/20-dispatch2.json"
rc=0
htm run dispatch --run-id "$RUN" --role developer --effect read \
  --message '只回复 ITEST_D2_OK；写一份新的 JSON 报告。' \
  > "$D2_OUT" || rc=$?
[ "$rc" -eq 0 ] || abort "第二次派单失败 rc=$rc"
export D2=$(jq -er '.dispatch_id' "$D2_OUT") || abort "取不到 D2"
printf '%s' "$D2" | grep -Eq '^[a-f0-9]{10}$' || abort "D2 形状非法"
[ "$D2" != "$D1" ] || abort "D2 与 D1 相同，历史被覆盖"
jq -e --arg d1 "$D1" --arg d2 "$D2" \
  '([.dispatches[]|select(.id==$d1)]|length)==1 and ([.dispatches[]|select(.id==$d2)]|length)==1' \
  "$RUN_FILE" >/dev/null || abort "账本未同时保留 D1/D2"
```

### 5.7 B6 报告归属（正例；run_id 只从 run list / run 文件取）

```bash
htm run list > "$EVIDENCE/phaseB/30-runlist.json" || abort "run list 失败"
export RUN_FROM_LIST=$(jq -er --arg r "$RUN" '.runs[]|select(.run_id==$r)|.run_id' "$EVIDENCE/phaseB/30-runlist.json") \
  || abort "run list 中找不到本 run"
[ "$RUN_FROM_LIST" = "$RUN" ] || abort "run list 的 run_id 与本地不一致"
jq -r --arg d "$D2" '.dispatches[]|select(.id==$d)
  | {id, role, stage_index, stage_attempt, status, artifact_sha256, result_file,
     trace_run: .report.run_id, trace_dispatch: .report.dispatch_id}' \
  "$RUN_FILE" > "$EVIDENCE/phaseB/31-report-ownership.json"
jq -e --arg r "$RUN" --arg d "$D2" '
  .trace_run==$r and .trace_dispatch==$d and .status=="settled"
  and (.artifact_sha256|type=="string") and (.artifact_sha256|length>0)
' "$EVIDENCE/phaseB/31-report-ownership.json" >/dev/null || abort "报告归属不符或未 settled"

P="$HTM_STATE_DIR/artifacts/$RUN/$D2.json"
[ -f "$P" ] || abort "工件不存在: $P"
[ ! -L "$P" ] || abort "工件是符号链接"
jq -e --arg r "$RUN" --arg d "$D2" '
  .run_id==$r and .dispatch_id==$d and .role=="developer"
  and .stage=="implement" and (.stage_attempt|type=="number")
  and (.status|IN("complete","blocked")) and (.verdict|IN("pass","fail","needs-work"))
  and (.summary|length>=20)
  and (.evidence|length>0) and (all(.evidence[]; length>=8))
  and ((.tests // []) | (type=="array") and all(.[]; (.command|type=="string") and (.exit_code|type=="number")))
' "$P" >/dev/null || abort "工件契约不满足"
```

关键：`dispatch` 响应**没有** `run_id`；任何“用 dispatch 响应里的 run_id 做归属断言”的写法都是错的。

### 5.8 B7 同一 pane 的 run 互斥

```bash
assert_single_active_run "$ITEST_WS" "$ITEST_ROOT_PANE"
{
  printf 'workspace=%s lead_pane=%s\n' "$ITEST_WS" "$ITEST_ROOT_PANE"
  for f in "$HTM_STATE_DIR"/runs/*.json; do
    [ -e "$f" ] || continue
    jq -r --arg ws "$ITEST_WS" --arg pane "$ITEST_ROOT_PANE" '
      select(.context.workspace==$ws and .context.lead_pane==$pane)
      | "RUN=\(.id) status=\(.status)"' "$f"
  done
} > "$EVIDENCE/phaseB/32-active-runs.txt"
grep -c 'status=active' "$EVIDENCE/phaseB/32-active-runs.txt" \
  | grep -qx '1' || abort "该 pane 的 active run 数不等于 1"
```

在同一 team 上对同一 developer 再开 run 会被 `run_start` 拒绝（`Worker(s) reserved by unfinished Run ...`）。该负例可选，不构造成本。

### 5.9 B8 超时用例（同一 team / 默认 300s，第 3 次 htm prompt）

`task_timeout_seconds` 在 `init` 时固化（`config/team.yaml` 默认 300），事后不能改；为了不注册第二个 team，本用例让 worker `sleep 420`，从而在默认 300 秒超时下触发真实的 `timeout`。

**不从失败命令的 stdout 取 dispatch id**（超时失败时 stdout 可能为空）。做法：派单前后快照 `runs/*.json` 集合与目标 run 的派单数，差集必须为空；dispatch id 从目标 run 文件的 `dispatches[-1].id` 读取，并断言唯一。

```bash
export TRUN="$RUN"                       # 同一个 run
export TRUN_FILE="$RUN_FILE"
assert_single_active_run "$ITEST_WS" "$ITEST_ROOT_PANE"

# dispatch 要求 worker idle/done；先等 D2 稳定
herdr agent wait "$DEV" --timeout 180000 || abort "B8 前等待 developer 失败"
herdr agent get "$DEV" | jq -e '.result.agent.agent_status|IN("idle","done")' >/dev/null \
  || abort "B8 前 developer 不是 idle/done"

# 派单前快照
ls "$HTM_STATE_DIR"/runs/*.json | sort > "$EVIDENCE/phaseB/41-runs-before.txt" || abort "快照 run 集合失败"
BEFORE_COUNT=$(jq -er '.dispatches|length' "$TRUN_FILE") || abort "读派单数失败"
printf 'run=%s dispatch-count-before=%s\n' "$TRUN" "$BEFORE_COUNT" > "$EVIDENCE/phaseB/41-dispatch-count.txt"

rc=0
htm run dispatch --run-id "$TRUN" --role developer --effect read \
  --message '先执行 sleep 420，再按提示路径写入 JSON 报告；不要提前写。' \
  > "$EVIDENCE/phaseB/42-dispatch-timeout.txt" 2>&1 || rc=$?
[ "$rc" -eq 2 ] || abort "超时派单应失败 exit=2，实际 $rc"
grep -Eq 'Dispatch [a-f0-9]{10} uncertain; input might have been sent\. Run collect before retry\.' \
  "$EVIDENCE/phaseB/42-dispatch-timeout.txt" || abort "超时错误文案不符"

# 集合差集：必须为空（失败路径不得创建新 run）
ls "$HTM_STATE_DIR"/runs/*.json | sort > "$EVIDENCE/phaseB/44-runs-after.txt" || abort "快照 run 集合失败"
comm -13 "$EVIDENCE/phaseB/41-runs-before.txt" "$EVIDENCE/phaseB/44-runs-after.txt" \
  > "$EVIDENCE/phaseB/45-runs-new.txt" || abort "计算 run 差集失败"
[ ! -s "$EVIDENCE/phaseB/45-runs-new.txt" ] || abort "派单失败路径意外创建了新 run"

# 从目标 run 文件读取新派单 id，并断言唯一、账本恰好 +1
AFTER_COUNT=$(jq -er '.dispatches|length' "$TRUN_FILE") || abort "读派单数失败"
[ "$AFTER_COUNT" -eq "$((BEFORE_COUNT + 1))" ] || abort "超时派单没有新增恰好 1 条账本记录"
export TD1=$(jq -er '.dispatches[-1].id' "$TRUN_FILE") || abort "timeout run 无 dispatch"
printf '%s' "$TD1" | grep -Eq '^[a-f0-9]{10}$' || abort "TD1 形状非法"
jq -e --arg d "$TD1" '([.dispatches[]|select(.id==$d)]|length)==1' "$TRUN_FILE" >/dev/null \
  || abort "TD1 在账本中不唯一"
jq -e --arg d "$TD1" '.dispatches[]|select(.id==$d)|.status=="submission_uncertain" and (.error|length>0)' \
  "$TRUN_FILE" >/dev/null || abort "超时后账本不是 submission_uncertain 或 error 为空"
grep -q "$TD1" "$EVIDENCE/phaseB/42-dispatch-timeout.txt" \
  || abort "错误信息里的 dispatch id 与账本不一致"
```

不自动重发：

```bash
rc=0
htm run dispatch --run-id "$TRUN" --role developer --effect read --message '重发探测' \
  > "$EVIDENCE/phaseB/46-retry-timeout.txt" 2>&1 || rc=$?
[ "$rc" -eq 2 ] || abort "超时后重发未被拒绝 exit=$rc"
grep -Eq 'Pending/uncertain dispatch [a-f0-9]{10}; use run collect, never blindly resend' "$EVIDENCE/phaseB/46-retry-timeout.txt" \
  || abort "超时后重发拒绝原因不符"
```

收尾（collect 会把 `submission_uncertain` 改写为 settled/blocked/uncertain，属正常推进）：

```bash
herdr agent wait "$DEV" --timeout 600000 || abort "developer 等待失败"
htm run collect --run-id "$TRUN" --dispatch-id "$TD1" > "$EVIDENCE/phaseB/47-collect-timeout.json" || abort "timeout collect 失败"
jq -e --arg d "$TD1" '.dispatch_id==$d and (.status|IN("settled","blocked","uncertain"))' \
  "$EVIDENCE/phaseB/47-collect-timeout.json" >/dev/null || abort "timeout collect 返回异常"
```

注意：`sleep 420` 的 worker 在 `--wait` 超时（300s）后仍在工作；`herdr agent wait` 要等它真正结束。若它最终写出合法报告，collect 会得到 `settled`，这**不构成**超时降级用例失败——关键断言是上一步的账本 `submission_uncertain` 与“不自动重发”。

### 5.10 B9 Mock：错误报告注入（独立 state 副本，**Mock**）

不碰真实 worker 正在使用的文件。复制一份 state，在副本里伪造账本 + 伪造工件，`HTM_STATE_DIR` 指向副本：

```bash
export MOCK="$ITEST_ROOT/mock"
rm -rf "$MOCK" || abort "无法清理旧 mock 目录"
mkdir -p "$MOCK" || abort "无法创建 mock 目录"
cp -R "$HTM_STATE_DIR" "$MOCK/state" || abort "复制 state 失败"
export MOCK_STATE="$MOCK/state"
[ "$(cd "$MOCK_STATE" && pwd)" != "$HTM_STATE_DIR" ] || abort "Mock state 与真实 state 相同"
mock_htm() { HTM_STATE_DIR="$MOCK_STATE" python3 "$REPO/scripts/htm.py" "$@"; }
```

不要在 Mock 里新开 run：副本里仍有未完成的真实 `$RUN`，`run_start` 会因 worker 占用而拒绝（`Worker(s) reserved by unfinished Run ...`）。改为复制出的**同一 run** 上追加一条伪造派单：

```bash
MOCK_RUN="$RUN"
export MOCK_RUN_FILE="$MOCK_STATE/runs/$MOCK_RUN.json"
[ -f "$MOCK_RUN_FILE" ] || abort "mock 中无 run 文件"
MOCK_DID=$(python3 -c 'import secrets; print(secrets.token_hex(5))') || abort "生成 mock id 失败"
printf '%s' "$MOCK_DID" | grep -Eq '^[a-f0-9]{10}$' || abort "mock id 形状非法"
jq --arg d "$MOCK_DID" --arg role developer --arg art "$MOCK_STATE/artifacts/$MOCK_RUN/$MOCK_DID.json" '
  .dispatches += [{id:$d, stage_index:.stage_index, stage_attempt:.stage_attempt,
                   remediation:false, role:$role, effect:"read",
                   status:"submitted_unsettled", created_at:"mock", prompt:"mock",
                   result_file:$art}]
' "$MOCK_RUN_FILE" > "$MOCK_RUN_FILE.tmp" || abort "构造 mock 账本失败"
mv "$MOCK_RUN_FILE.tmp" "$MOCK_RUN_FILE" || abort "替换 mock 账本失败"
mkdir -p "$MOCK_STATE/artifacts/$MOCK_RUN" || abort "创建 mock 工件目录失败"
```

写入身份错误的工件（**只**把 `run_id` 写错，其余按账本真实值对齐，确保命中的是 run_id 这一条）：

```bash
python3 - "$MOCK_RUN_FILE" "$MOCK_DID" \
         "$MOCK_STATE/artifacts/$MOCK_RUN/$MOCK_DID.json" <<'PY' || abort "重写 mock 工件失败"
import json, sys
run_file, did, out = sys.argv[1], sys.argv[2], sys.argv[3]
run = json.load(open(run_file, encoding="utf-8"))
rec = next(d for d in run["dispatches"] if d["id"] == did)
doc = {"run_id": "000000000000", "dispatch_id": did, "role": rec["role"],
       "stage": run["stages"][rec["stage_index"]], "stage_attempt": rec["stage_attempt"],
       "status": "complete", "verdict": "pass",
       "summary": "这是一份故意错位的 Mock 工件，用于验证 run_id 身份校验的 fail-closed。",
       "evidence": ["injected-by-mock-test"]}
open(out, "w", encoding="utf-8").write(json.dumps(doc, ensure_ascii=False))
PY
[ "$(jq -r '.run_id' "$MOCK_STATE/artifacts/$MOCK_RUN/$MOCK_DID.json")" = "000000000000" ] \
  || abort "mock 工件 run_id 未写错"
```

在**主 Lead pane 的 shell**（与真实 run 同一 `context`）里执行 collect，只把 `HTM_STATE_DIR` 换成副本。先确保 developer 处于 `idle`/`done`，否则 `reconcile_result` 会先以 `Unexpected agent status` 拒绝，测不到身份校验：

```bash
herdr agent wait "$DEV" --timeout 240000 || abort "Mock 前等待 developer 失败"
herdr agent get "$DEV" | jq -e '.result.agent.agent_status|IN("idle","done")' >/dev/null \
  || abort "Mock 前 developer 不是 idle/done"

REAL_RUN_SHA_BEFORE=$(shasum -a 256 "$RUN_FILE" | awk '{print $1}') || abort "真实账本哈希失败"
rc=0
mock_htm run collect --run-id "$MOCK_RUN" --dispatch-id "$MOCK_DID" \
  > "$EVIDENCE/phaseB/50-mock-collect.json" 2>"$EVIDENCE/phaseB/50-mock-collect.err" || rc=$?
[ "$rc" -eq 0 ] || abort "Mock collect 非法失败 rc=$rc: $(cat "$EVIDENCE/phaseB/50-mock-collect.err")"
jq -e '.status=="uncertain" and (.error|startswith("Result identity mismatch: run_id"))' \
  "$EVIDENCE/phaseB/50-mock-collect.json" >/dev/null \
  || abort "Mock 身份不符未 fail-closed: $(cat "$EVIDENCE/phaseB/50-mock-collect.json")"
jq -e --arg d "$MOCK_DID" '.dispatches[]|select(.id==$d)|.status!="settled"' "$MOCK_RUN_FILE" >/dev/null \
  || abort "Mock 账本被错误写成 settled"

# 隔离断言：真实账本与真实工件目录都未被改动
REAL_RUN_SHA_AFTER=$(shasum -a 256 "$RUN_FILE" | awk '{print $1}') || abort "真实账本哈希失败"
[ "$REAL_RUN_SHA_BEFORE" = "$REAL_RUN_SHA_AFTER" ] || abort "真实账本被 Mock 测试改动"
[ ! -e "$HTM_STATE_DIR/artifacts/$MOCK_RUN/$MOCK_DID.json" ] \
  || abort "Mock 工件泄漏到真实 state"
echo "MOCK PASS: 身份不符 -> status=uncertain, error=Result identity mismatch: run_id" \
  > "$EVIDENCE/phaseB/51-mock-verdict.txt"
echo "REAL UNTOUCHED: ledger sha $REAL_RUN_SHA_BEFORE" >> "$EVIDENCE/phaseB/51-mock-verdict.txt"
export REAL_RUN_SHA_AFTER="$REAL_RUN_SHA_AFTER"
```

语义边界（必须写入报告）：`read_result` 是身份/形状校验，**不是**签名或 OS 安全边界（`scripts/htm.py:646-682` 注释 `identity/shape check`）。Mock 文件与真实 state 完全隔离，且不与任何真实 worker 抢写同一路径。

### 5.11 B10 错误 run-id / 错误 role 映射负例（真实 state，只读 + 副本）

`collect` 只吃 `--dispatch-id`。错误 run-id 的负例**不新开 run**（否则会与主 run 争用同一批 worker）：直接用主 run 之外的一个 12 位 hex id 构造，期望 `Unknown dispatch ID`：

```bash
FAKE_RUN=$(python3 -c 'import secrets; print("f"*11 + secrets.token_hex(1)[0])') || abort "生成假 run id 失败"
printf '%s' "$FAKE_RUN" | grep -Eq '^[a-f0-9]{12}$' || abort "假 run id 形状非法"
[ ! -f "$HTM_STATE_DIR/runs/$FAKE_RUN.json" ] || abort "假 run id 与现有 run 撞车"
rc=0
htm run collect --run-id "$FAKE_RUN" --dispatch-id "$D1" > "$EVIDENCE/phaseB/60-wrong-run.txt" 2>&1 || rc=$?
[ "$rc" -eq 2 ] || abort "错误 run-id 未拒绝 exit=$rc"
grep -Eq 'Invalid Run ID|Cannot read state|Unknown dispatch ID' "$EVIDENCE/phaseB/60-wrong-run.txt" \
  || abort "错误 run-id 报错文案不符"
```

role→pane 篡改**只在 Mock 副本**里做（复制 `$HTM_STATE_DIR` 后修改 `.roles[].pane_id` 为 `w1:p5`），期望 `is live in unexpected pane w1:p5; refusing reuse`；`w1:p5` 仅读取元数据，不发送任何输入。

```bash
export MOCK2="$ITEST_ROOT/mock-role"
mkdir -p "$MOCK2" || abort "创建 mock2 目录失败"
cp -R "$HTM_STATE_DIR" "$MOCK2/state" || abort "复制 state 失败"
export MOCK2_STATE="$MOCK2/state"
mock2_htm() { HTM_STATE_DIR="$MOCK2_STATE" python3 "$REPO/scripts/htm.py" "$@"; }

MOCK2_RUN_FILE="$MOCK2_STATE/runs/$RUN.json"
python3 - "$MOCK2_RUN_FILE" <<'PY' || abort "篡改 mock 角色映射失败"
import json, sys
path = sys.argv[1]
run = json.load(open(path, encoding="utf-8"))
for r in run["roles"]:
    if r["role"] == "developer":
        r["pane_id"] = "w1:p5"
open(path, "w", encoding="utf-8").write(json.dumps(run, ensure_ascii=False))
PY
rc=0
mock2_htm run collect --run-id "$RUN" --dispatch-id "$D1" \
  > "$EVIDENCE/phaseB/61-mock-role.txt" 2>&1 || rc=$?
[ "$rc" -eq 2 ] || abort "错误 pane 映射未拒绝 exit=$rc"
grep -Eq 'is live in unexpected pane w1:p5; refusing reuse' "$EVIDENCE/phaseB/61-mock-role.txt" \
  || abort "错误 pane 映射报错文案不符"
[ "$(shasum -a 256 "$RUN_FILE" | awk '{print $1}')" = "$REAL_RUN_SHA_AFTER" ] \
  || abort "真实账本被 role 篡改测试改动"
```

### 5.12 B11 Blocked：只做被动观察

**不主动请求任何需要审批、敏感或危险的操作来构造 blocked。** 每次 collect / dispatch 前后只读观察：

```bash
BLOCK_OBS="$EVIDENCE/phaseB/70-blocked-observation.txt"
{
  printf 'developer %s %s\n' "$DEV" \
    "$(herdr agent get "$DEV" | jq -r '.result.agent.agent_status')"
  printf 'reviewer %s %s\n' "$REV" \
    "$(herdr agent get "$REV" | jq -r '.result.agent.agent_status')"
} > "$BLOCK_OBS" || abort "读取 agent 状态失败"
if grep -q ' blocked$' "$BLOCK_OBS"; then
  echo "OBSERVED: blocked（不代答；由人在 Herdr 中处理）" >> "$BLOCK_OBS"
else
  echo "NOT-OBSERVED: 未验证" >> "$BLOCK_OBS"
fi
```

规则：

- 观察到 blocked 才记录；未发生必须标「未验证」，不得伪造。
- 禁止 `send-keys`、禁止再发 prompt 代答弹窗；htm 不会自动应答。
- 若 Run 因 blocked 需要挂起，用 `htm run gate --run-id "$RUN" --decision blocked --evidence 'worker 停在审批弹窗，需人工确认，本轮不代答'`（evidence ≥12 字符），由人执行。
- 统计“本测试对 blocked agent 的 send-keys/prompt 次数”恒为 0。

### 5.13 B12 隔离复核

```bash
herdr agent list \
  | jq -r '.result.agents[] | [.workspace_id, .pane_id, (.name // "-"), (.agent_status // "-"), (.revision|tostring), (.state_change_seq|tostring)] | @tsv' \
  | sort > "$EVIDENCE/phaseB/80-agents-after.tsv" || abort "agent list 失败"

PROTECTED='w1:p5 w1:p6 w2:p2 w2:p3'
pick() { awk -v list="$PROTECTED" '
  BEGIN { n=split(list,a," "); for(i=1;i<=n;i++) keep[a[i]]=1 }
  keep[$2] { print $2 "\t" $5 "\t" $6 }' "$1" | sort; }

pick "$EVIDENCE/phaseB/00-agents-before.tsv" > "$EVIDENCE/phaseB/81-protected-before.tsv" || abort "基线提取失败"
pick "$EVIDENCE/phaseB/80-agents-after.tsv"  > "$EVIDENCE/phaseB/82-protected-after.tsv"  || abort "复核提取失败"
diff -u "$EVIDENCE/phaseB/81-protected-before.tsv" "$EVIDENCE/phaseB/82-protected-after.tsv" \
  > "$EVIDENCE/phaseB/83-protected-diff.txt" || abort "受保护 worker 的 revision/seq 发生变化（见 83-protected-diff.txt）"
```

受保护 worker（`w1:p5`、`w1:p6`、`w2:p2`、`w2:p3`）的 `revision` 与 `state_change_seq` 必须逐字不变；`w1:p1`、`w2:p1` 为 Lead，允许自身工作造成漂移，把它们的差异写进 `85-lead-drift.txt` 并说明原因，不得出现本测试输入的痕迹。

默认状态目录隔离（A-16）：`HTM_STATE_DIR` 全程指向临时目录，默认目录不应出现新文件。

```bash
DEFAULT_STATE="$HOME/.local/state/herdr-team"
if [ -d "$DEFAULT_STATE" ]; then
  find "$DEFAULT_STATE" -newer "$EVIDENCE/phaseB/00-head.txt" -type f \
    > "$EVIDENCE/phaseB/86-default-state-new.txt" || abort "默认 state 目录扫描失败"
  [ ! -s "$EVIDENCE/phaseB/86-default-state-new.txt" ] \
    || abort "默认状态目录出现新文件（见 86-default-state-new.txt），需人工核查"
else
  echo "默认 state 目录不存在，视为未写入" > "$EVIDENCE/phaseB/86-default-state-new.txt"
fi
```

### 5.14 阶段 B Go/No-Go

| 项 | Go 条件 |
|---|---|
| B2（§5.3） | 派单成功，账本 `submitted_unsettled`，响应无 `run_id` |
| B3（§5.4） | 在途重复派单 exit=2，旧记录不变 |
| B4（§5.5） | collect 后 `settled` |
| B5（§5.6） | 新 id ≠ 旧 id，账本两条记录都在 |
| B6（§5.7） | 工件身份/形状全通过，`artifact_sha256` 非空 |
| B7（§5.8） | 同一 pane active run 恰好 1 |
| B8（§5.9） | 超时 exit=2、账本 `submission_uncertain`、error 非空、重发被拒、run 差集为空、账本恰好 +1 |
| B9（§5.10） | Mock 身份不符 → exit=0 且 JSON `uncertain` + `Result identity mismatch: run_id`；真实 state 哈希不变 |
| B10（§5.11） | 错误 run-id exit=2；Mock role 篡改报 `unexpected pane` |
| B11（§5.12） | blocked 未主动构造；观察结果如实标记 |
| B12（§5.13） | 受保护 worker 的 revision/seq 不变；默认 state 无新文件 |

全通过 → `GO`；任一失败 → `NO-GO`，保留现场。结论写入 `$EVIDENCE/phaseB/GO-NOGO.txt`。

---

## 6. 断言规范（16 条）

写法约束：禁止“忽略退出码”的兜底写法（强制成功、丢弃 stderr、diff 判失败仍继续）；每条失败必须 abort。身份不符类断言以 **JSON** 判定，退出码可能为 0。

| 编号 | 对象 | 命令（要点） | 通过条件 | 失败诊断 |
|---|---|---|---|---|
| A-01 | 配置 | `htm validate` | exit=0 且 `.valid==true` | 配置或模板解析失败 |
| A-02 | 资源 ID | `need_ws/need_tab/need_pane` + `same_ws` | 非空、非 null、格式匹配、前缀一致 | 创建返回缺字段或跨 workspace |
| A-03 | dispatch id 形状 | `printf %s "$D1" \| grep -Eq '^[a-f0-9]{10}$'` | exit=0 | id 生成异常 |
| A-04 | run 可见性 | `jq -e --arg r "$RUN" '[.runs[]\|select(.run_id==$r)]\|length==1'` | exit=0 | run list 遗漏/重复 |
| A-05 | `--no-wait` 状态 | 账本 `select(.id==$D1).status` | 精确 `submitted_unsettled` | 状态机未落账 |
| A-06 | 在途重复派单 | 第二次 dispatch 捕获 rc 与 combined output | rc=2 且正则 `Pending/uncertain dispatch [a-f0-9]{10}; use run collect, never blindly resend` | 重复投递风险 |
| A-07 | 派单响应无 run_id | `jq -r 'keys\|join(",")'` | 精确 `dispatch_id,status,next`（no-wait）或含 `dispatch_id,status,agent_state,report,error,warning`（wait）；不含 `run_id` | 从响应取 run_id 的写法作废 |
| A-08 | 报告归属 | 账本 `report.run_id==$RUN` 且 `report.dispatch_id==$D2` | exit=0，且 `status==settled` | 工件错配 |
| A-09 | 产物契约 | `jq -e` 校验 run_id/dispatch_id/role/stage/attempt/status/verdict/summary/evidence/tests | exit=0 | 工件形状不合规 |
| A-10 | 错位工件（Mock） | `HTM_STATE_DIR=$MOCK_STATE htm run collect` 后读 stdout JSON | exit=0 **且** `.status=="uncertain"` 且 `.error` 以 `Result identity mismatch: run_id` 开头 | 归属校验失效 |
| A-11 | 超时账本 | `jq -e --arg d "$TD1" '.dispatches[]\|select(.id==$d)\|.status=="submission_uncertain" and (.error\|length>0)'` | exit=0 | 超时被当成成功/无错误 |
| A-12 | 超时不重发 | 超时后立即 dispatch | rc=2 且含 `never blindly resend` | 重复投递风险 |
| A-13 | 二次派单唯一性 | `test "$D2" != "$D1"` + 账本两条各 1 | exit=0 | 历史被覆盖 |
| A-14 | 单 pane 单 Run | `assert_single_active_run "$ITEST_WS" "$ITEST_ROOT_PANE"` | non-terminal run ≤1；B7 另要求 active 恰好 1 | 冲突 Run 并存 |
| A-15 | Mock 隔离 | `$MOCK_STATE` ≠ `$HTM_STATE_DIR`；Mock 后真实账本 sha 不变；真实 state 无 Mock 工件 | 三项均成立 | Mock 污染真实 state |
| A-16 | 状态目录隔离 + 受保护 pane | `find` 默认 state 目录新文件为空；受保护 worker 行 diff 为空 | 输出为空 | 越界写或测试泄漏 |

pytest 等价写法（供后续把断言固化为测试时使用）：

```python
import json, re, pathlib, subprocess

HTM = "/Users/shakya/dev/herdr-team/scripts/htm.py"

def htm(env, *args):
    return subprocess.run(["python3", HTM, *args], text=True, capture_output=True, env=env)

def test_dispatch_shape_and_ledger(dispatch_out, ledger, dispatch_id):
    assert re.fullmatch(r"[a-f0-9]{10}", dispatch_id)
    assert dispatch_out["dispatch_id"] == dispatch_id
    rec = next(d for d in ledger["dispatches"] if d["id"] == dispatch_id)
    assert rec["status"] in ("submitted_unsettled", "settled", "submission_uncertain")

def test_dispatch_response_has_no_run_id(dispatch_out):
    assert "run_id" not in dispatch_out
    assert set(dispatch_out) == {"dispatch_id", "status", "next"}

def test_identity_mismatch_is_json_fail_closed(env, run_id, dispatch_id):
    out = htm(env, "run", "collect", "--run-id", run_id, "--dispatch-id", dispatch_id)
    assert out.returncode == 0          # 身份不符不是命令失败，必须看 JSON
    doc = json.loads(out.stdout)
    assert doc["status"] == "uncertain"
    assert doc["error"].startswith("Result identity mismatch: run_id")

def test_timeout_ledger(env, run_id, dispatch_id):
    out = htm(env, "run", "status", "--run-id", run_id)
    rec = next(d for d in json.loads(out.stdout)["dispatches"] if d["id"] == dispatch_id)
    assert rec["status"] == "submission_uncertain"
    assert rec["error"]

def test_artifact_contract(artifact, run_id, dispatch_id):
    doc = json.loads(pathlib.Path(artifact).read_text(encoding="utf-8"))
    assert doc["run_id"] == run_id and doc["dispatch_id"] == dispatch_id
    assert doc["status"] in ("complete", "blocked")
    assert doc["verdict"] in ("pass", "fail", "needs-work")
    assert len(doc["summary"].strip()) >= 20
    assert doc["evidence"] and all(len(s.strip()) >= 8 for s in doc["evidence"])
```

---

## 7. 证据留存与脱敏

目录：

```text
$EVIDENCE/phaseA/   00-探针身份、10-14 正常返回、20-22 超时、GO-NOGO.txt
$EVIDENCE/phaseB/   00-03 初始化、10-13 D1、20 D2、30-32 归属与互斥、
                    40-47 超时、50-51 Mock、60 负例、70 blocked、80 隔离、GO-NOGO.txt
```

规则：

- 真实响应只保留 **key 名、值的类型、短值**（如 `idle`、`claude`、10 位 id）；不保存终端标题、长文本、对话内容。
- 账本快照用 `jq '{id,status,stage_index,stage_attempt,dispatches:[.dispatches[]|{id,role,status,error,artifact_sha256}]}'`，不保留完整 prompt。
- Mock 证据文件名带 `mock` 前缀，内容首行标注 `MOCK`，与真实测试分开。
- 证据留在 `$ITEST_ROOT` 内；如需长期保留，清理前复制到用户另行授权的目录。

---

## 8. 清理与回滚

### 8.1 顺序

只关闭用已捕获 id 创建的资源，不做通配、不按 label 匹配。

```bash
need_ws "$ITEST_WS"; need_pane "$ITEST_ROOT_PANE"
case "$ITEST_WS" in w1|w2) abort "拒绝关闭受保护 workspace: $ITEST_WS";; esac
case "$ITEST_ROOT" in
  "${TMPDIR:-/tmp}"/htm-itest-*) ;;
  *) abort "临时目录前缀异常，保留现场: $ITEST_ROOT" ;;
esac
[ "$ITEST_ROOT" != "/" ] && [ "$ITEST_ROOT" != "$HOME" ] || abort "危险路径: $ITEST_ROOT"
```

### 8.2 关闭前归属断言（全部通过才继续）

```bash
# 1) 资源名前缀：htm worker 必须 ht 开头
for a in "$DEV" "$REV"; do
  case "$a" in
    "ht"*) ;;
    *) abort "agent 名不属于本测试: $a" ;;
  esac
done

# 2) 关闭前：测试 workspace 内确有 agent，且都属于本测试
herdr agent list > "$EVIDENCE/cleanup-agents-before.json" || abort "agent list 失败"
jq -e --arg ws "$ITEST_WS" '
  [.result.agents[] | select(.workspace_id==$ws)] as $mine
  | ($mine|length) > 0 and all($mine[]; ((.name // "") | startswith("ht")))
' "$EVIDENCE/cleanup-agents-before.json" >/dev/null || abort "测试 workspace 内存在非本测试 agent"

# 3) 关闭 workspace（其内 pane/agent 一并回收）
herdr workspace close "$ITEST_WS" || abort "workspace close 失败，保留现场"

# 4) workspace 已从列表消失
herdr workspace list > "$EVIDENCE/cleanup-workspaces.json" || abort "workspace list 失败"
jq -e --arg w "$ITEST_WS" '[.result.workspaces[]|select(.workspace_id==$w)]|length==0' \
  "$EVIDENCE/cleanup-workspaces.json" >/dev/null || abort "workspace 关闭后仍出现在列表: $ITEST_WS"

# 5) 测试 worker 已从 agent list 消失
herdr agent list > "$EVIDENCE/cleanup-agents-after.json" || abort "agent list 失败"
jq -e --arg ws "$ITEST_WS" '[.result.agents[]|select(.workspace_id==$ws)]|length==0' \
  "$EVIDENCE/cleanup-agents-after.json" >/dev/null || abort "workspace 关闭后仍有 agent 残留: $ITEST_WS"
for a in "$DEV" "$REV"; do
  jq -e --arg n "$a" '[.result.agents[]|select(.name==$n)]|length==0' \
    "$EVIDENCE/cleanup-agents-after.json" >/dev/null || abort "agent 仍存活: $a"
done
```

若 `workspace close` 因前台 agent 失败：先用 `herdr pane close <该 workspace 内的 pane>` 关闭其内 pane，再重试一次；仍失败则 abort 并保留现场，**不得**升级为关闭其他 workspace。

### 8.3 临时目录

```bash
[ -d "$HTM_STATE_DIR" ] || abort "state 目录不存在"
case "$HTM_STATE_DIR" in
  "$ITEST_ROOT"/state) ;;
  *) abort "HTM_STATE_DIR 不在临时目录内: $HTM_STATE_DIR" ;;
esac
rm -rf "$ITEST_ROOT" || abort "删除临时目录失败: $ITEST_ROOT"
[ ! -e "$ITEST_ROOT" ] || abort "临时目录仍存在: $ITEST_ROOT"
unset HTM_STATE_DIR RUN RUN_FILE D1 D2 TD1 TRUN TRUN_FILE OTHER FAKE_RUN DEV DEV_PANE REV \
      ITEST_WS ITEST_TAB ITEST_ROOT_PANE RUNTOKEN EVIDENCE \
      MOCK MOCK_STATE MOCK_RUN MOCK_RUN_FILE MOCK_DID MOCK2 MOCK2_STATE \
      REAL_RUN_SHA_BEFORE REAL_RUN_SHA_AFTER BEFORE_COUNT AFTER_COUNT
unset -f htm mock_htm mock2_htm abort need need_ws need_tab need_pane same_ws \
         assert_no_protected assert_single_active_run
```

### 8.4 明确禁止

- 不关闭/重命名 `w1`、`w2` 及其 tab/pane/agent；不执行 `herdr server stop`。
- 不删除 `~/pagi`、`~/dev/herdr-team` 下任何文件。
- 不 `rclone sync`、不 `rm -rf` 任何项目目录。
- 若误写默认 state 目录：不删除，如实报告受影响文件，由用户决定。

---

## 9. 结论口径

必须按三列表区分，不允许合并叙述：

| 类别 | 内容 | 判定依据 |
|---|---|---|
| 真实测试 | 阶段 A 的正常/超时返回；阶段 B 的 dispatch、账本状态、collect、报告归属、真实超时降级 | 真实 Herdr + 真实 agent + 真实 htm 调用 |
| Mock 测试 | 身份不符工件注入、错误 pane 映射（在 `$MOCK/state` 副本中） | 隔离 state，不发送 prompt，不写真实工件 |
| 未验证项 | blocked 未自然发生时的行为、`agent_prompt_stalled`、长任务/审批/多文件行为、默认 state 目录下的并发 | 只在出现时记录，否则标「未验证」 |

**口径红线**：本方案只验证「调度通信链路」（dispatch → prompt → collect → 账本与工件校验）。它**不等于「自主监督闭环」验证通过**——不证明 Lead 能自主判断交付质量、不证明审批边界、不证明多团队并发与长期运行稳定性。任何“闭环已验证”的表述都超出证据范围。

---

## 10. 风险与未验证项

| 项 | 现状 | 处置 |
|---|---|---|
| `prompt --wait` 返回结构 | 阶段 A 实测；若 `unknown` 则 No-Go | 记录 key 形状，不静默改源码 |
| `agent_start` 返回结构 | 未单独验证 | 失败时用 `htm status` 的 `live` 复核 |
| `stalled` | 无法确定性构造 | 出现即记录，不追求复现 |
| blocked 自然发生 | 不可控 | 只观察，未发生标未验证；禁止构造危险审批 |
| `run approve` 恒拒绝 | v4.2 有意禁用（`:634-637`） | 仅用 `--effect read` 与默认 autonomy |
| Herdr 字段漂移 | 0.9.5 使用 `agent`/`agent_status` | 升级后必须重跑阶段 A |
| HTM_STATE_DIR 隔离 | 与默认 state 完全隔离 | 不得据此宣称真实多团队并存已验证 |
| 临时工件清理 | 清理后不可审计 | 需保留时先复制到授权目录 |
| Lead pane 漂移 | 并行任务导致 revision 变化 | 只对 worker 严格断言 |
| 超时阈值 | `task_timeout_seconds` init 时固化（默认 300s） | 用 `sleep 420` 触发真实超时；不改 team 配置 |
| Mock 与真实差异 | 副本中的账本/工件是伪造的 | 结论必须标 Mock |
| reviewer 未被验证 | 全程 0 次 prompt | 报告中明确“reviewer 仅验证存在性，未验证其工作链路” |

---

## 11. 附录

### 11.1 变量速查

| 变量 | 含义 |
|---|---|
| `REPO` | `/Users/shakya/dev/herdr-team` |
| `RUNTOKEN` | 8 位随机 token，用于全部资源命名 |
| `ITEST_ROOT` | `mktemp -d` 临时根目录 |
| `HTM_STATE_DIR` | `$ITEST_ROOT/state` |
| `EVIDENCE` | `$ITEST_ROOT/evidence` |
| `ITEST_WS` / `ITEST_TAB` / `ITEST_ROOT_PANE` | 测试 workspace / 根 tab / 主 Lead pane |
| `DEV` / `DEV_PANE` / `REV` | 唯一 team 的 worker（`DEV` 同时是阶段 A 探针） |
| `RUN` / `RUN_FILE` | 主 run 与其账本文件 |
| `D1` / `D2` | 主 run 的两次正常派单 |
| `TRUN` / `TRUN_FILE` / `TD1` | 超时用例（同一 run）与其派单 |
| `MOCK_STATE` / `MOCK_RUN_FILE` / `MOCK_DID` | Mock 副本、账本、伪造派单 |
| `MOCK2_STATE` | role→pane 篡改的第二个 Mock 副本 |
| `FAKE_RUN` | 错误 run-id 负例使用的 12 位 hex |
| `REAL_RUN_SHA_BEFORE` / `REAL_RUN_SHA_AFTER` | Mock 前后真实账本哈希 |

### 11.2 报告工件契约（worker 必须写出）

路径由 `htm dispatch` 在提示词中给出：`$HTM_STATE_DIR/artifacts/<run_id>/<dispatch_id>.json`。

```json
{
  "run_id": "<12 位十六进制>",
  "dispatch_id": "<10 位十六进制>",
  "role": "developer",
  "stage": "implement",
  "stage_attempt": 1,
  "status": "complete",
  "verdict": "pass",
  "summary": "至少 20 个字符的具体结论，不能是占位文本",
  "evidence": ["每条至少 8 个字符的具体证据", "例如命令与输出摘要"],
  "tests": [{"command": "pytest -q tests/x.py", "exit_code": 0}]
}
```

硬性要求：

- 先写同目录临时文件，再原子 rename；直接覆盖或写到别处不被接受。
- 必须是非符号链接的普通文件，小于 1 MiB。
- `tests` 可为空数组；出现时每项必须有 `command` 字符串与整数 `exit_code`。
- 终端输出、聊天消息、`agent read` 滚动内容都**不是**报告。
- 报告是 worker 的自述，不是独立证明；gate 只在 `verdict=pass` 且字节未变时通过。
