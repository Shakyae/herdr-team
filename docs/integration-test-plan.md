# herdr-team 真实联调方案（Herdr 0.9.5 · `agent prompt --wait` 派单链路）

- 适用版本：herdr-team v4.2.0-beta.1（`scripts/htm.py`）、Herdr 0.9.5
- 文档状态：**方案，未执行**。本文档不被本轮（2026-10-30 文档提交）执行；任何创建 workspace / tab / pane、`htm init`、`run start`、`dispatch`、`collect`、`close` 操作都需要用户单独、明确地授权。
- 读者：可在 Herdr 中操作 CLI 的工程师或 Agent。命令可直接复制，但必须先完成 §3 的变量初始化。

---

## 1. 目标与范围

### 1.1 目标

在真实 Herdr 0.9.5 上验证 htm 的派单链路，具体是 `scripts/htm.py` 中的：

```text
htm run dispatch  --role R --message TEXT [--no-wait]
        └─ herdr agent prompt <agent_name> TEXT --wait --until idle --until done --until blocked --timeout <ms>
htm run collect   --dispatch-id DID
        └─ herdr agent get <agent_name> → agent_status_of() → 读取 dispatch 绑定的 JSON 工件
```

验证四项：

1. **Dispatch ID 生成与复用**：首次派单产生唯一 10 位十六进制 dispatch id；`run list` 可见；重复派单不静默覆盖历史；`--no-wait` 后必须显式 `collect`。
2. **agent 状态识别**：`agent_status_of()` 能否在真实 `prompt --wait` 返回结构上取到 `idle` / `done` / `blocked` / `working` / `unknown`，并记录真实返回的 key 形状。
3. **JSON 报告归属**：worker 写出的报告文件是否被正确归属到 run / dispatch / role / stage / stage_attempt；内容不符时是否 fail-closed。
4. **异常与降级**：`--wait` 超时、`agent_prompt_stalled`、`blocked` 三种情况下，账本是否保持 `submission_uncertain` / 不自动重发 / 不自动应答审批。

### 1.2 不在范围内

- 不验证真实业务交付质量，不验证模型能力。
- 不验证 `hermes-guide` 模板与 Hermes Profile。
- 不验证 `run approve` 的“正常通过”路径（v4.2 中该命令**恒拒绝**，见 §4.5）。
- 不验证多机（`--machine`）场景。
- 不验证 worktree 创建（统一使用 `--worktree off`）。

---

## 2. 硬约束 / 前置条件

### 2.1 隔离要求（强制）

| 资源 | 约定 | 说明 |
|---|---|---|
| workspace | 新建，label `htm-itest` | 不得复用 `w1`（pagi）、`w2`（herdr-team） |
| tab / pane | 全部在 `htm-itest` 内 | 不向 `w1` / `w2` 做任何写操作 |
| 直连探针 agent | `itest-worker`（可选 `itest-reviewer`） | 只用于 §4.2 的 `prompt --wait` 返回结构实测 |
| htm 派单 worker | 由 `htm init` 生成 `ht<digest>-N-<role>` | 见 §2.4 命名说明 |
| cwd | `mktemp -d` 生成的临时目录 | **不得**是 `~/pagi` 或 `~/dev/herdr-team` |
| 状态目录 | `HTM_STATE_DIR=<临时目录>/state` | 不得写入 `~/.local/state/herdr-team` |

**禁止占用**以下既有 agent / pane（它们是别的团队的资产，不是本测试的）：

```text
w1:p1  pagi Lead
w1:p5  developer
w1:p6  reviewer
w2:p1  herdr-team Lead（本会话）
w2:p2  developer-w2
w2:p3  reviewer-w2
```

### 2.2 环境变量纪律

- `HTM_STATE_DIR` 在 `scripts/htm.py` **导入时**读取（`scripts/htm.py:34`），必须在每条命令的进程环境中已导出。
- `HERDR_ENV=1`、`HERDR_WORKSPACE_ID`、`HERDR_TAB_ID`、`HERDR_PANE_ID` 由 Herdr 提供（`scripts/htm.py:234-243`）。**如果执行环境中这些变量缺失，立即停止**，去修启动器的环境透传；**不得**手工 `export HERDR_ENV=1` 伪装身份。
  - 已观察到的现实情况：某些由 happier / SDK 启动的会话中 `HERDR_ENV` 与其余 `HERDR_*` 全部为空（`herdr ... --current` 会直接报错）。这是本方案要求“在 Herdr 内、测试 workspace 的 shell 里执行”的原因。

### 2.3 不修改的边界

- 不改 Happier / Herdr 安装包，不改 `~/.claude/skills/herdr-team` 与 `~/.agents/skills/herdr-team` 的已安装副本。
- 不改 `scripts/htm.py`、`tests/`、`pyproject.toml`、`uv.lock`、`SKILL.md`、`config/`。
- 不碰 `~/pagi` 与 w1 的任何 pane / agent / 文件。

### 2.4 命名说明（必须知悉的实现事实）

`htm init` 为 worker 生成的 agent 名是：

```python
name = f"ht{digest}-{idx+1}-{role[:9]}"   # scripts/htm.py:548-549
```

`htm` 的 Run 必须绑定一个由 `init` 注册的 team，而 `verify_existing()` 按 `team["agent_name"]` 查活体 agent，因此**无法把 htm 的派单目标改名成 `itest-worker`**。所以：

- `itest-worker` / `itest-reviewer` 是 **§4.2 直连探针 agent**，用于真实返回结构实测，独立于 htm 账本；
- htm 派单链路使用 `init` 生成的 `ht…` worker，它们位于测试 workspace 内，物理上不可能与 `w1:p5` / `w2:p2` 冲突（pane id 带 workspace 前缀，且 `verify_existing` 会二次校验 pane 与 kind）。

这是方案的刻意取舍，不是遗漏；如需统一命名，属于对 `scripts/htm.py` 的功能变更，不在本轮范围。

### 2.5 已核实的 CLI / 实现事实（写在这里，避免执行者再猜）

**Herdr 侧**

- `herdr agent prompt <TARGET> <TEXT> --wait --until idle|working|blocked|done|unknown --timeout <MS>`。
- `--wait` 从**非 working** 状态提交时，Herdr 在 5 秒内必须观察到 `working` 或 `blocked`，否则返回 `agent_prompt_stalled`；调用方 `--timeout` 先到则返回 `timeout`。`timeout` / `stalled` **都不证明输入没送达**。
- agent 已停在审批/提问弹窗时，`prompt` 在写入任何输入前就以 `agent_blocked` 拒绝。
- `agent start` **没有** `--cwd`；cwd 只能先由 `pane split --cwd`、`workspace/tab create --cwd` 或 `pane run <id> "cd PATH"` 设定。
- 返回结构：`workspace create` → `.result.workspace` / `.result.tab` / `.result.root_pane`；`tab create` → `.result.tab` / `.result.root_pane`；`pane split` → `.result.pane`；`agent get` → `.result.agent`；`agent list` → `.result.agents`；`pane list` → `.result.panes`（以 `herdr --skill` 文档与本次只读实测为准）。
- Agent 对象字段：`agent`（kind 字符串）、`agent_status`、`pane_id`、`revision`、`state_change_seq`、`cwd`、`name`（仅命名 agent 才有）。

**htm 侧（源码事实，可逐条复核）**

| 事实 | 位置 | 含义 |
|---|---|---|
| 派单报错时账本状态是字面 `submission_uncertain` | `scripts/htm.py:769`（写入）、`:806-811`（`except Error` 只补 `error`，不改 status） | 不是 `uncertain`；`uncertain` 是 `reconcile_result` 在 collect 阶段写出的另一值 |
| `collect` 只接受 `--dispatch-id` | `scripts/htm.py:1025`、`:820-836` | 不接受 pane / role / workspace 参数；身份校验走内部 role→agent_name→pane_id 映射 |
| 状态缺失按 `unknown` | `scripts/htm.py:339-354` | `agent_status` 缺失时回退 `status`，都为空则 `unknown`，并被视为不可结算 |
| kind 缺失 fail-closed | `scripts/htm.py:325-336`、`:373` | 报 `Agent <name> has no verifiable kind` |
| 报告只认文件，不认终端文字 | `scripts/htm.py:646-682`、`:820-836` | collect 不读 `agent read`；终端里贴 JSON 不算报告 |
| 未结算派单禁止重发 | `scripts/htm.py:714-718` | 报 `Pending/uncertain dispatch <id>; use run collect, never blindly resend` |
| `--no-wait` 记 `submitted_unsettled` | `scripts/htm.py:787-795` | 必须再显式 `collect` |
| `run approve` 恒拒绝 | `scripts/htm.py:634-637` | 报文 `Owner approval is disabled...` |
| 模板 `dev-lite` 首阶段 actor 是 `lead` | `config/team.yaml` | dispatch 给 developer 前必须先 gate 掉 `plan_brief` |
| `--config` 是全局参数，必须写在子命令**之前** | `scripts/htm.py:992-996` | `htm validate --config X` 会 `unrecognized arguments`（已实测 exit=2） |

---

## 3. 环境准备（逐步命令）

> 全部命令在 **Herdr 内、测试 workspace 的 shell** 里执行。步骤 3.1–3.3 可在任意能访问 Herdr CLI 的 shell 里做；3.4 起必须在测试 workspace 内，且 `HERDR_WORKSPACE_ID` 等于测试 workspace id。

### 3.1 记录基线（在任何写操作之前）

```bash
export REPO=/Users/shakya/dev/herdr-team
export ITEST_ROOT=$(mktemp -d /tmp/htm-itest.XXXXXX)
export HTM_STATE_DIR="$ITEST_ROOT/state"
htm() { python3 "$REPO/scripts/htm.py" "$@"; }
echo "ITEST_ROOT=$ITEST_ROOT"

herdr workspace list \
  | jq -r '.result.workspaces[] | [.workspace_id, (.label // "-"), (.focused | tostring)] | @tsv' \
  | tee "$ITEST_ROOT/00-workspaces.tsv"

herdr agent list \
  | jq -r '.result.agents[] | [.workspace_id, .pane_id, (.name // "-"), (.agent_status // "-"), (.revision|tostring), (.state_change_seq|tostring)] | @tsv' \
  | sort | tee "$ITEST_ROOT/00-agents.tsv"

herdr pane list \
  | jq -r '.result.panes[] | [.workspace_id, .pane_id, (.revision|tostring)] | @tsv' \
  | sort | tee "$ITEST_ROOT/00-panes.tsv"

git -C "$REPO" status --porcelain | tee "$ITEST_ROOT/00-git-status.txt"
git -C "$REPO" rev-parse HEAD | tee "$ITEST_ROOT/00-head.txt"
```

基线必须包含 §2.1 列出的 6 个受保护 pane。若 `00-agents.tsv` 里缺少其中某个，说明当前环境看到的 Herdr 与服务端不一致，停止。

说明：`config/team.yaml` 是 JSON-compatible YAML，`scripts/htm.py` 在未安装 PyYAML 时回退到 `json.loads`，因此无需 `uv add` 任何依赖；如需与 CI 一致，可把 `htm()` 改成 `uv run --project "$REPO" --extra yaml python "$REPO/scripts/htm.py" "$@"`，两者行为相同。

### 3.2 创建独立 workspace

```bash
WS_JSON=$(herdr workspace create --label htm-itest --cwd "$ITEST_ROOT" --focus)
export ITEST_WS=$(printf '%s' "$WS_JSON" | jq -r '.result.workspace.workspace_id')
export ITEST_TAB=$(printf '%s' "$WS_JSON" | jq -r '.result.tab.tab_id')
export ITEST_ROOT_PANE=$(printf '%s' "$WS_JSON" | jq -r '.result.root_pane.pane_id')
echo "ITEST_WS=$ITEST_WS  ITEST_TAB=$ITEST_TAB  ITEST_ROOT_PANE=$ITEST_ROOT_PANE"
```

断言：

```bash
test "$ITEST_WS" != "w1" && test "$ITEST_WS" != "w2" || { echo "ABORT: 测试 workspace 不独立"; }
test -n "$ITEST_WS" && test -n "$ITEST_ROOT_PANE"
```

### 3.3 确认测试 Lead pane 的 cwd 与身份

切到 `htm-itest` workspace（它已被 `--focus` 激活），在该 workspace 的根 pane（`$ITEST_ROOT_PANE`）里开一个 shell。若根 pane 的 cwd 不是 `$ITEST_ROOT`，补一条：

```bash
herdr pane run "$ITEST_ROOT_PANE" "cd '$ITEST_ROOT'"
```

在**该 pane 的 shell**里重新导出变量（新 shell 没有上一步的变量）：

```bash
export REPO=/Users/shakya/dev/herdr-team
export ITEST_ROOT=...            # 填上一步打印的实际路径
export HTM_STATE_DIR="$ITEST_ROOT/state"
htm() { python3 "$REPO/scripts/htm.py" "$@"; }

echo "HERDR_ENV=${HERDR_ENV:-<unset>} WS=${HERDR_WORKSPACE_ID:-<unset>} TAB=${HERDR_TAB_ID:-<unset>} PANE=${HERDR_PANE_ID:-<unset>}"
test "${HERDR_ENV:-}" = "1"             || echo "STOP: HERDR_ENV 缺失，不要伪造"
test "${HERDR_TAB_ID:-}" = "$ITEST_TAB" || echo "STOP: 不在测试 tab"
test "$(pwd)" = "$ITEST_ROOT"           || echo "STOP: cwd 不是临时目录"
ls -l "$HTM_STATE_DIR" 2>/dev/null || echo "state dir 尚未创建（init 时会创建）"
```

### 3.4 启动直连探针 agent `itest-worker`

```bash
PROBE_JSON=$(herdr tab create --workspace "$ITEST_WS" --label itest-probe --cwd "$ITEST_ROOT" --no-focus)
export PROBE_TAB=$(printf '%s' "$PROBE_JSON" | jq -r '.result.tab.tab_id')
export PROBE_PANE=$(printf '%s' "$PROBE_JSON" | jq -r '.result.root_pane.pane_id')

herdr agent start itest-worker --kind claude --pane "$PROBE_PANE" --timeout 120000
```

说明：

- `agent start` 没有 `--cwd`，cwd 由上一步 `tab create --cwd "$ITEST_ROOT"` 决定；**不要**在 `~/pagi` 或 `~/dev/herdr-team` 下创建探针。
- 成功只表示“检测到 agent 且可交互”，不表示工作完成。
- 若返回 `agent_not_ready`（启动期被弹窗阻塞）：用 `herdr agent read itest-worker` 查看，**由人处理**，不要自动应答。

断言：

```bash
herdr agent get itest-worker \
  | jq -r '.result.agent | [.name, .pane_id, .agent, (.agent_status // "-"), .cwd] | @tsv'
# 期望：name=itest-worker, pane_id=$PROBE_PANE, agent=claude, cwd=$ITEST_ROOT
```

可选再建 `itest-reviewer`（同法，另建 tab 或 split 一个 pane），本方案除 §4.6 复核外不强制使用。

### 3.5 注册 htm 团队（worker 由 init 生成）

```bash
htm validate | tee "$ITEST_ROOT/10-validate.json"

htm init dev-lite --layout tab --worktree off --cwd "$ITEST_ROOT" \
  --set orchestration.max_retries=5 \
  | tee "$ITEST_ROOT/11-init.json"

export DEV=$(jq -r '.team.roles[] | select(.role=="developer") | .agent_name' "$ITEST_ROOT/11-init.json")
export REV=$(jq -r '.team.roles[] | select(.role=="reviewer")  | .agent_name' "$ITEST_ROOT/11-init.json")
export DEV_PANE=$(jq -r '.team.roles[] | select(.role=="developer") | .pane_id' "$ITEST_ROOT/11-init.json")
export REV_PANE=$(jq -r '.team.roles[] | select(.role=="reviewer")  | .pane_id' "$ITEST_ROOT/11-init.json")
echo "DEV=$DEV($DEV_PANE)  REV=$REV($REV_PANE)"

htm status | tee "$ITEST_ROOT/12-status.json"
```

要点：

- `--layout tab` 让 worker 独占一个新 tab（label `htm-dev-lite`），与测试 Lead 分屏；`--worktree off` 避免触碰任何 Git 仓库。
- **`--set orchestration.max_retries=5` 是必须的**：htm 对「同一 stage_index + stage_attempt + role」的派单上限是 `1 + max_retries`（`scripts/htm.py:719-722`）。默认 `max_retries=1` 只允许 2 次，而 §4.1–§4.3 需要在同一 stage_attempt 上派 4 次，否则会报 `max_retries limit reached`。设到上限 5 后允许 6 次。
- `dev-lite` 的角色是 `developer` + `reviewer`，两级 agent 都是 `claude`。
- 断言 worker 不在受保护 pane 上，且 cwd 正确：

```bash
for p in "$DEV_PANE" "$REV_PANE"; do
  case "$p" in w1:*|w2:*) echo "ABORT: worker 落在受保护 workspace: $p";; esac
done
herdr pane list --workspace "$ITEST_WS" \
  | jq -r '.result.panes[] | [.pane_id, (.cwd // "-")] | @tsv'
```

- 若 `init` 返回 `reused: true`，说明该 Lead pane 已注册过 team：先人工核对 `11-init.json` 里的 cwd 是否为 `$ITEST_ROOT`，不符则按报错信息处理，**不要**直接重跑。

### 3.6 需要超时用例时的第二个团队

`task_timeout_seconds` 在 `init` 时固化进 team settings，事后不能改。超时用例（§4.4）需要 15 秒的短超时，因此单独开一个测试 tab 作为第二 Lead：

```bash
TAB2_JSON=$(herdr tab create --workspace "$ITEST_WS" --label itest-timeout --cwd "$ITEST_ROOT" --no-focus)
export TAB2=$(printf '%s' "$TAB2_JSON" | jq -r '.result.tab.tab_id')
export TAB2_PANE=$(printf '%s' "$TAB2_JSON" | jq -r '.result.root_pane.pane_id')
```

在该 tab 的根 pane 里（`HTM_STATE_DIR`、`REPO`、`htm()` 重新导出一次）执行：

```bash
htm init dev-lite --layout tab --worktree off --cwd "$ITEST_ROOT" \
    --set orchestration.task_timeout_seconds=15 | tee "$ITEST_ROOT/13-init-timeout.json"
export DEV_T=$(jq -r '.team.roles[] | select(.role=="developer") | .agent_name' "$ITEST_ROOT/13-init-timeout.json")
```

两个 team 的 `state_file` 由 `(workspace, lead_pane)` 的哈希区分（`scripts/htm.py:249-251`），互不覆盖；Run 的 Worker 保留检查按 `(workspace, pane_id)` 判重（`:591-598`），两套 worker pane 不同，因此可以并存。

---

## 4. 测试用例

每个用例统一格式：**前置 / 操作 / 预期 / 失败含义**。所有命令都在 §3.3 的那个 shell 里执行（变量已导出）。

### 4.1 用例 1：Dispatch ID 生成、可见、不静默覆盖

**前置**：§3.5 已完成，`htm status` 的 `.agents[]` 里 worker 均 `live: true` 且 `observed_state` 为 `idle`/`done`（`scripts/htm.py:1039-1047`）。

**操作 A — 建 run 并推进到 developer 可派单的阶段**

```bash
RUN_JSON=$(htm run start --goal '真实联调：验证 dispatch/collect 链路与异常降级，不修改任何仓库代码')
export RUN=$(printf '%s' "$RUN_JSON" | jq -r '.run_id')
echo "RUN=$RUN"

htm run stage --run-id "$RUN"
# dev-lite 首阶段 plan_brief 的 actor 是 lead，必须先由 Lead 亲自 gate 掉
htm run gate --run-id "$RUN" --decision pass \
  --evidence 'plan_brief：本轮仅验证派单与回收链路，验收标准见 docs/integration-test-plan.md'
htm run stage --run-id "$RUN"     # 期望 stage=implement, actor=developer
```

**操作 B — 首次派单**

```bash
htm run dispatch --run-id "$RUN" --role developer --effect read \
  --message '请在一个 30 秒内可完成的最小任务上验证报告链路：创建 /tmp 下一个临时文件并立即删除，然后按提示中的路径写入 JSON 报告。不要修改任何仓库。' \
  | tee "$ITEST_ROOT/20-dispatch1.json"

export D1=$(jq -r '.dispatch_id' "$ITEST_ROOT/20-dispatch1.json")
echo "D1=$D1"
printf '%s' "$D1" | grep -Eq '^[a-f0-9]{10}$' && echo "dispatch_id 形状 OK"
```

**操作 C — 可见性与不覆盖**

```bash
htm run list | tee "$ITEST_ROOT/21-runlist.json"
jq -r --arg r "$RUN" '.runs[] | select(.run_id==$r)' "$ITEST_ROOT/21-runlist.json"
```

- 派单仍在途（`submission_uncertain` / `submitted_unsettled`）时立刻重复派单，期望被拒：

```bash
htm run dispatch --run-id "$RUN" --role developer --effect read --message '重复派单探测'
echo "exit=$?"        # 期望 2，stderr 含 "Pending/uncertain dispatch" 与 "never blindly resend"
```

- 结算后再次派单，期望**新** dispatch id，而不是覆盖旧记录：

```bash
export D2=$(htm run dispatch --run-id "$RUN" --role developer --effect read \
  --message '第二轮：只回复 DUP_PROBE_OK，并写一份新的 JSON 报告。' | jq -r '.dispatch_id')
test "$D2" != "$D1" && echo "新 id OK"
htm run status --run-id "$RUN" \
  | jq -r '.dispatches[] | [.id, .role, .status, (.stage_attempt|tostring)] | @tsv'
# 期望：D1、D2 两条记录都存在
```

**操作 D — `--no-wait` 必须显式 collect**

```bash
export D3=$(htm run dispatch --run-id "$RUN" --role developer --effect read --no-wait \
  --message '第三轮：只回复 NOWAIT_OK，并写一份新的 JSON 报告。' | jq -r '.dispatch_id')
htm run status --run-id "$RUN" | jq -r --arg d "$D3" '.dispatches[] | select(.id==$d) | .status'
# 期望：submitted_unsettled
htm run collect --run-id "$RUN" --dispatch-id "$D3"
# 注意：collect 不等待；若 worker 仍在 working，会得到 status=uncertain + Unexpected agent status
```

等待 worker 稳定后再 collect 的正确做法：

```bash
herdr agent wait "$DEV" --timeout 180000
htm run collect --run-id "$RUN" --dispatch-id "$D3" | tee "$ITEST_ROOT/22-collect-d3.json"
```

**预期**

- `D1`、`D2`、`D3` 均为 10 位十六进制且互不相同；
- `run list` 与 `run status` 都能看到每个 dispatch；
- 在途时重复派单 exit=2，且旧记录保持原状态；
- `--no-wait` 的返回含 `"status": "submitted_unsettled"` 与 `next` 提示，只有 `collect` 才推进状态。

**失败含义**

- id 不唯一 / 重复派单成功覆盖 → 账本隔离失效，**不得继续**做后续用例，先冻结算例；
- 在途重复派单成功 → 同一 worker 会被投递两次，属于重复输入风险，必须停止。

另外两个必须知道的前置门（`scripts/htm.py:703-741`）：

- 派单前要求 worker 的 `agent_status` 为 `idle` 或 `done`；否则报 `Agent <name> is <status>; inspect before sending a new task`。上一轮刚结束但还没稳定时，先用 `herdr agent wait "$DEV" --timeout 120000` 等它。
- `run collect` **不等待**：它在 worker 仍 `working` 时会把账本写成 `uncertain` + `Unexpected agent status: working`。这是设计上的 fail-closed，不是缺陷；正确姿势是先 `agent wait` 再 collect。

### 4.2 用例 2：真实 `prompt --wait` 返回上的状态识别

**前置**：`itest-worker` 已启动且 `agent_status` 为 `idle`/`done`（`herdr agent get itest-worker`）。

**操作 A — 抓取真实返回（只记录 key 形状，不记录长文本）**

```bash
herdr agent prompt itest-worker '只回复 ITEST_PONG，然后结束本轮。不要读写文件。' \
  --wait --timeout 120000 > "$ITEST_ROOT/30-prompt-return.json" 2>"$ITEST_ROOT/30-prompt-return.err"
echo "exit=$?"
jq -r 'keys[]' "$ITEST_ROOT/30-prompt-return.json" | sed 's/^/top: /'
jq -r '.result | keys[]' "$ITEST_ROOT/30-prompt-return.json" 2>/dev/null | sed 's/^/result: /'
jq -r '.result | to_entries[] | "\(.key):\(.value|type)"' "$ITEST_ROOT/30-prompt-return.json" 2>/dev/null
```

记录规则：**只列 key 名与值的类型/短值**；不把终端标题、长文本写进报告。

**操作 B — 用真实返回喂给 htm 的解析函数**

```bash
python3 - "$ITEST_ROOT/30-prompt-return.json" "$REPO/scripts/htm.py" <<'PY'
import importlib.util, json, sys
doc_path, module = sys.argv[1], sys.argv[2]
spec = importlib.util.spec_from_file_location("htm", module)
h = importlib.util.module_from_spec(spec); spec.loader.exec_module(h)
doc = json.load(open(doc_path, encoding="utf-8"))
cands = [("doc", doc),
         ("result", doc.get("result")),
         ("result.agent", (doc.get("result") or {}).get("agent") if isinstance(doc.get("result"), dict) else None),
         ("doc.agent", doc.get("agent"))]
for label, obj in cands:
    if isinstance(obj, dict) or obj is None:
        print(f"{label:14s} -> status={h.agent_status_of(obj)!r} kind={h.agent_kind_of(obj)!r}")
PY
```

**操作 C — 覆盖全部目标状态**

| 目标状态 | 观察方式 | 说明 |
|---|---|---|
| `working` | 提交后立刻 `herdr agent get itest-worker` | 只读观测，不依赖返回体 |
| `done` | `prompt --wait` 正常返回后的 `agent get` | Herdr 在回合完成的 settled 状态 |
| `idle` | agent 空转等待输入时 | 与 `done` 的区分按 Herdr 语义记录，不额外翻译 |
| `blocked` | §4.4 的 blocked 用例 | 只在真实阻塞时记录 |
| `unknown` | 摘除 `agent_status` 后的合成对象 | 合成仅用于验证解析契约，必须注明“合成” |

`unknown` 的合成分两步，不触碰真实 agent：

```bash
python3 - "$ITEST_ROOT/30-prompt-return.json" "$REPO/scripts/htm.py" <<'PY'
import importlib.util, json, sys
doc_path, module = sys.argv[1], sys.argv[2]
spec = importlib.util.spec_from_file_location("htm", module)
h = importlib.util.module_from_spec(spec); spec.loader.exec_module(h)
doc = json.load(open(doc_path, encoding="utf-8"))
def strip(o):
    if isinstance(o, dict):
        return {k: strip(v) for k, v in o.items() if k not in ("agent_status", "status")}
    return o
print("stripped ->", h.agent_status_of(strip(doc.get("result"))))
PY
```

**预期**

- `prompt --wait` 正常返回时 exit=0；
- 解析结果落在 `{idle, done, blocked, working, unknown}` 之一，核心要求是 **`unknown` 不能被视为成功**；
- 记录到的真实返回 key 形状写入测试报告（一般应为顶层 `id`、`result`，`result` 内含 `agent_status` 或 `agent` 映射）。

**失败含义**

- 真实返回里没有 `agent_status`/`status`，解析出 `unknown` → 说明 Herdr 返回形状与 `agent_status_of` 的假设不符，**记录为“需补映射”**，不得静默通过，也不得在本轮直接改 `scripts/htm.py`；
- 解析到 `working` 却被当成可结算 → 风险最高，必须阻断后续用例。

### 4.3 用例 3：JSON 报告归属

**前置**：用例 1 已产生至少一个 `settled` 的 dispatch（示例用 `D1`）。

**操作 A — 正例：报告归属正确**

```bash
RUN=$(jq -r '.run_id' "$ITEST_ROOT/20-dispatch1.json")   # 若换了 shell，重新赋值
D1=$(jq -r '.dispatch_id' "$ITEST_ROOT/20-dispatch1.json")

htm run status --run-id "$RUN" \
  | jq -r --arg d "$D1" '.dispatches[] | select(.id==$d)
      | {id, role, stage_index, stage_attempt, status, artifact_sha256, result_file,
         verdict: .report.verdict, trace_id: .report.run_id, trace_dispatch: .report.dispatch_id}'
```

断言（全部成立才算正例）：

```bash
P="$HTM_STATE_DIR/artifacts/$RUN/$D1.json"
test -f "$P"
jq -e --arg r "$RUN" --arg d "$D1" '
  .run_id==$r and .dispatch_id==$d and (.role|type=="string")
  and (.stage_attempt|type=="number")
  and (.status|IN("complete","blocked"))
  and (.verdict|IN("pass","fail","needs-work"))
  and (.summary|length>=20)
  and (.evidence|length>0)
' "$P" >/dev/null && echo "报告契约 OK"
```

**操作 B — 负例：身份不符必须 fail-closed**

在隔离 state 里模拟一份“错位报告”，不触碰任何真实用户的报告：

```bash
# 为 D4 分配一个真实的 dispatch id，但在它绑定的路径上写入身份错误的 JSON
export D4=$(htm run dispatch --run-id "$RUN" --role developer --effect read --no-wait \
  --message '第四轮：请勿写报告，本轮由测试脚本注入错误工件。' | jq -r '.dispatch_id')

P4="$HTM_STATE_DIR/artifacts/$RUN/$D4.json"
mkdir -p "$(dirname "$P4")"
cat > "${P4%.json}.tmp" <<'JSON'
{
  "run_id": "000000000000",
  "dispatch_id": "0000000000",
  "role": "developer",
  "stage": "implement",
  "stage_attempt": 1,
  "status": "complete",
  "verdict": "pass",
  "summary": "这是一份故意错位的测试工件，用于验证 htm 的身份校验。",
  "evidence": ["injected-by-test-script"]
}
JSON
mv "${P4%.json}.tmp" "$P4"

herdr agent wait "$DEV" --timeout 180000
htm run collect --run-id "$RUN" --dispatch-id "$D4"
echo "exit=$?"
# 期望：exit=0，但返回体 status == "uncertain"，error == "Result identity mismatch: run_id"
# 注意：身份不符不是“命令失败”，而是“账本拒绝结算”——必须看 JSON，不能只看退出码
```

注意：注入的工件要**先于** worker 可能写出的报告存在。若 worker 违反指令、真的写了自洽报告并 rename 覆盖了注入文件，本负例作废——记录实际情况，换一个 dispatch id 重做，不要把它算成通过。

**需要记录的语义边界（必须如实写入报告）**

- `read_result` 只校验 `run_id/dispatch_id/role/stage/stage_attempt` 与最小契约，**它是身份/形状校验，不是签名或 OS 安全边界**（`scripts/htm.py:646-682`，`identity/shape check` 注释在 `:649`）。
- 报告字段全部匹配且 `verdict=pass` → 状态 `settled`，`run gate --decision pass` 才可能通过；
- 报告匹配但 `verdict=fail` → 状态仍 `settled`，但 gate pass 会拒绝（`scripts/htm.py:851-862`）；
- 报告缺失/身份不符/契约不满足 → `uncertain`（若观察状态为 `blocked` 则记 `blocked`），**不能**通过 gate。
- 终端里打印的 JSON **不算报告**：collect 只读 `$HTM_STATE_DIR/artifacts/<run>/<dispatch>.json`，不读 `agent read` 的滚动输出。

**失败含义**

- 错位报告被结算成 pass → 归属校验失效，最高优先级缺陷；
- 终端文字被当报告 → 与“只认文件”的设计冲突，记录并阻断。

### 4.4 用例 4：超时、stalled 与 blocked 降级

**前置**：§3.6 的超时团队已注册，`DEV_T` 已导出；本用例建议在第二 Lead 的 shell 里执行。

**操作 A — `--wait` 超时**

```bash
TRUN=$(htm run start --goal '超时降级验证：15 秒超时、不自动重发、账本保持 submission_uncertain' | jq -r '.run_id')
htm run gate --run-id "$TRUN" --decision pass --evidence 'plan_brief：仅验证超时降级路径与账本状态'
export TD1=$(htm run dispatch --run-id "$TRUN" --role developer --effect read \
  --message '先执行 sleep 45，再按提示路径写入 JSON 报告。不要提前写。' | jq -r '.dispatch_id')
# 期望：约 15 秒后命令失败（exit=2），stderr 含 "Dispatch <id> uncertain; input might have been sent. Run collect before retry."
```

断言超时后的账本：

```bash
htm run status --run-id "$TRUN" \
  | jq -r --arg d "$TD1" '.dispatches[] | select(.id==$d) | {id, status, error}'
# 期望：status == "submission_uncertain"（字面值），error 含 timeout 信息
```

断言“不自动重发”：

```bash
htm run dispatch --run-id "$TRUN" --role developer --effect read --message '重发探测'
echo "exit=$?"   # 期望 2；stderr 含 Pending/uncertain dispatch 与 never blindly resend
```

收尾（worker 45 秒后写完报告）：

```bash
herdr agent wait "$(jq -r '.team.roles[]|select(.role=="developer")|.agent_name' "$ITEST_ROOT/13-init-timeout.json")" --timeout 180000
htm run collect --run-id "$TRUN" --dispatch-id "$TD1" | tee "$ITEST_ROOT/40-collect-timeout.json"
```

注意：collect 会把 `submission_uncertain` **改写**为 `settled` / `blocked` / `uncertain` 之一（`reconcile_result`），这是正常推进，不是“静默覆盖”——必须同时核对它是否真的读到了**同一 dispatch id** 绑定的工件。

**操作 B — `agent_prompt_stalled`**

无法确定性构造：它要求提交后 5 秒内观察不到 `working`/`blocked`。尝试方式（只在确实发生时才记录，不追求复现）：

```bash
herdr agent prompt itest-worker '请只回复 STALL_PROBE，不要执行任何工具。' --wait --timeout 30000
echo "exit=$?"     # 若返回 agent_prompt_stalled，记录原始 stderr 摘要
```

处理规则（无论是否复现都必须遵守）：

- 出现的 stalled 与 timeout 一样，**不能证明输入未送达**；
- htm 侧表现为 `run dispatch` 报错、账本 `submission_uncertain`、禁止重发；
- 记录“未复现”比伪造一次成功更有价值。

**操作 C — `blocked` 不自动应答**

构造：让 worker 进入需要人工确认的状态（例如要求它执行明显需要审批的操作），或用 `herdr agent prompt` 指向一个已经在弹窗的 agent（此时 Herdr 在写入前以 `agent_blocked` 拒绝）。

```bash
# 本段在超时团队的 shell 中执行，Run 变量是 TRUN
herdr agent get "$DEV_T" | jq -r '.result.agent.agent_status'
# 若为 blocked：
#   1) 绝对不要用 send-keys / prompt 去“代答”弹窗
#   2) 记录 herdr agent read "$DEV_T" --source recent-unwrapped --lines 60 的摘要
#   3) 用 gate 显式阻断，而不是绕过
htm run gate --run-id "$TRUN" --decision blocked --evidence 'worker 停在审批弹窗，需人工确认，本轮不代答'
echo "exit=$?"      # 期望 0，run status 变为 blocked
```

**预期**

- 超时：exit=2，账本 `submission_uncertain`，`.error` 含 timeout，重复派单被拒；
- stalled：如果出现，记录原始响应，不重试；
- blocked：htm 不改写 agent 的弹窗，不做任何自动应答；`run gate --decision blocked` 把 Run 显式挂起。

**失败含义**

- 超时后账本变成 `settled` 或空 → 降级路径失效，会给 Lead 造成“任务已完成”的假象，必须阻断；
- 自动重发成功 → 可能重复投递，严重缺陷；
- htm 自动应答审批弹窗 → 越过人类授权边界，立即停止全部测试。

### 4.5 用例 5：身份不符必须拒绝（fail-closed）

**前置**：用例 1 的 `RUN` 存在；`HTM_STATE_DIR` 是隔离目录。`collect` 只吃 `--dispatch-id`（`scripts/htm.py:1025`），因此负例只能通过“错误的 run-id”或“篡改内部映射”构造，不存在“指定错误 role/pane 参数”的路径。

**操作 A — 用错误的 run-id 收集**

```bash
OTHER=$(htm run start --goal '负例容器 run：仅用于承载错误 run-id 的 collect 探测' | jq -r '.run_id')
htm run collect --run-id "$OTHER" --dispatch-id "$D1"
echo "exit=$?"    # 期望 2，stderr: "Unknown dispatch ID"
```

**操作 B — 篡改 role→pane 映射（只改隔离 state，改前备份）**

```bash
RK="$HTM_STATE_DIR/runs/$RUN.json"
cp "$RK" "$RK.bak"
BEFORE=$(shasum -a 256 "$RK" | awk '{print $1}')

jq --arg p "w1:p5" '
  .roles |= map(if .role=="developer" then .pane_id=$p else . end)
' "$RK.bak" > "$RK"

htm run collect --run-id "$RUN" --dispatch-id "$D1"
echo "exit=$?"    # 期望 2，stderr 含 "is live in unexpected pane w1:p5; refusing reuse"

# 立刻还原（这是唯一允许修改隔离 state 的场景）
mv "$RK.bak" "$RK"
AFTER=$(shasum -a 256 "$RK" | awk '{print $1}')
test "$BEFORE" = "$AFTER" && echo "state 已还原"
```

说明：该负例**只读取** w1:p5 的元数据用于比对，不向 w1 发送任何输入、不修改 w1 的任何资源。

**操作 C — 跨 Lead pane 的 run 归属**

在另一个 Lead pane（例如 `itest-probe` tab 的根 shell，或第二 Lead）里执行同一条 `collect`：

```bash
# 在另一个 pane 的 shell 中；把 $ITEST_ROOT 换成 3.1 打印的实际路径
export REPO=/Users/shakya/dev/herdr-team
export HTM_STATE_DIR="/tmp/htm-itest.REPLACE_ME/state"   # 必须指向同一隔离 state
python3 "$REPO/scripts/htm.py" run collect --run-id "$RUN" --dispatch-id "$D1"
echo "exit=$?"    # 期望 2，stderr: "Run belongs to a different Lead pane/session"
```

**操作 D — kind 校验负例（可选）**

`verify_existing` 会在两种情况下拒绝：取不到 kind 时报 `Agent <name> has no verifiable kind`（`scripts/htm.py:373`），kind 与注册值不符时报 `has unexpected kind`。**真实环境只能构造后者**：把 run state 里 developer 的 `kind` 改成 `__none__` 后 `collect`，期望 exit=2 且报 `has unexpected kind claude`；随后按操作 B 的方式还原。

`no verifiable kind` 这条路径需要活体 agent 缺 `agent` 字段，真实 Herdr 下无法构造，已由单测覆盖（`tests/test_regressions_v42.py:320-326`），本方案不重复构造。

```bash
cp "$RK" "$RK.bak2"
jq '.roles |= map(if .role=="developer" then .kind="__none__" else . end)' "$RK" > "$RK.tmp" && mv "$RK.tmp" "$RK"
htm run collect --run-id "$RUN" --dispatch-id "$D1"
echo "exit=$?"    # 期望 2，stderr 含 "has unexpected kind"
mv "$RK.bak2" "$RK"
```

**预期**

- 三种负例全部 exit=2 且 stderr 给出明确原因；
- 被拒绝时账本**不得**产生 `settled`，不得读取任何工件；
- 原状态在还原后与备份逐字节一致。

**失败含义**

- 任一种错位被接受 → 归属/身份校验失效；
- 拒绝时仍写入了 `artifact_sha256` → 校验顺序错误。

### 4.6 用例 6：隔离性复核（测试末尾必须执行）

**前置**：§3.1 的基线文件仍在。

```bash
herdr agent list \
  | jq -r '.result.agents[] | [.workspace_id, .pane_id, (.name // "-"), (.agent_status // "-"), (.revision|tostring), (.state_change_seq|tostring)] | @tsv' \
  | sort > "$ITEST_ROOT/90-agents.tsv"

herdr pane list \
  | jq -r '.result.panes[] | [.workspace_id, .pane_id, (.revision|tostring)] | @tsv' \
  | sort > "$ITEST_ROOT/90-panes.tsv"

# 只比对受保护 pane 的 (pane_id, revision, state_change_seq)；第二列是 pane_id
PROTECTED='w1:p1 w1:p5 w1:p6 w2:p1 w2:p2 w2:p3'

pick() {
  awk -v list="$PROTECTED" '
    BEGIN { n = split(list, a, " "); for (i=1;i<=n;i++) keep[a[i]]=1 }
    keep[$2] { print $2 "\t" $5 "\t" $6 }
  ' "$1" | sort
}

pick "$ITEST_ROOT/00-agents.tsv" > "$ITEST_ROOT/91-protected-before.tsv"
pick "$ITEST_ROOT/90-agents.tsv" > "$ITEST_ROOT/91-protected-after.tsv"
diff -u "$ITEST_ROOT/91-protected-before.tsv" "$ITEST_ROOT/91-protected-after.tsv" || true
```

判定规则（重要）：

- **worker 类受保护 agent**（`w1:p5 developer`、`w1:p6 reviewer`、`w2:p2 developer-w2`、`w2:p3 reviewer-w2`）：`revision` 与 `state_change_seq` 必须**逐字不变**；任何变化都视为隔离失败。
- **Lead 类 pane**（`w1:p1`、`w2:p1`）：它们正在跑别的任务，`agent_status` / `revision` / `state_change_seq` 可能因自身工作而漂移。把差异**如实记录**，不判失败，但必须确认没有任何 diff 指向“本测试的输入”。

**预期**：受保护 worker 的 diff 为空；Lead 的漂移有合理解释。

**失败含义**：受保护 worker 的 seq 变了 → 测试越界，立即停止并向用户报告，不得继续清理之外的操作。

---

## 5. 通过标准（机器可判定）

| # | 断言对象 | 判定命令 / 表达式 | 通过条件 |
|---|---|---|---|
| 1 | 配置 | `htm validate` | exit=0 且 `.valid == true` |
| 2 | dispatch id 形状 | `printf '%s' "$D1" \| grep -Eq '^[a-f0-9]{10}$'` | exit=0 |
| 3 | id 唯一性 | `test "$D1" != "$D2" && test "$D2" != "$D3"` | exit=0 |
| 4 | run list 可见 | `jq -e --arg r "$RUN" '[.runs[] \| select(.run_id==$r)] \| length == 1'` | exit=0 |
| 5 | 在途重复派单被拒 | `htm run dispatch ...; test $? -eq 2` | exit=2，stderr 匹配 `Pending/uncertain dispatch` |
| 6 | `--no-wait` 状态 | `jq -r ...select(.id==$D3).status` | 精确等于 `submitted_unsettled` |
| 7 | 状态识别 | `agent_status_of(...) in {"idle","done","blocked","working","unknown"}` | 恒成立，且 `unknown` 不参与结算 |
| 8 | 报告契约 | `jq -e` 的 §4.3 断言块 | exit=0 |
| 9 | 归属正确 | `jq -e --arg r --arg d '.run_id==$r and .dispatch_id==$d'` | exit=0 且账本 `status=="settled"` 且 `artifact_sha256` 非空 |
| 10 | 归属错误 fail-closed | 注入错位工件后 `htm run collect` | exit=0，但返回 JSON `status=="uncertain"` 且 `error` 以 `Result identity mismatch` 开头；账本非 `settled` |
| 11 | 超时降级 | `jq -r ...select(.id==$TD1).status` | 精确等于 `submission_uncertain` |
| 12 | 超时不自动重发 | 超时后立即 `htm run dispatch` | exit=2，stderr 含 `never blindly resend` |
| 13 | blocked 不代答 | 测试期间对 blocked agent 的 `send-keys`/`prompt` 次数 | 恒为 0 |
| 14 | 身份负例 | `htm run collect` 三种错位 | 均 exit=2，且 `Result identity mismatch` / `is live in unexpected pane` / `different Lead pane` 之一 |
| 15 | 隔离 | §4.6 的 `diff` | 四个受保护 worker 行完全一致 |
| 16 | 状态目录隔离 | `find ~/.local/state/herdr-team -newer "$ITEST_ROOT/00-head.txt" -print -quit` | 输出为空（默认状态目录无新增/修改文件） |

pytest 可断言形式（把上面的 shell 判断改成测试函数时的等价写法）：

```python
import re, json, subprocess, pathlib

def run(*args, env):
    return subprocess.run(["python3", "/Users/shakya/dev/herdr-team/scripts/htm.py", *args],
                          text=True, capture_output=True, env=env)

def test_dispatch_id_shape(d1):
    assert re.fullmatch(r"[a-f0-9]{10}", d1)

def test_timeout_ledger(env, run_id, dispatch_id):
    out = json.loads(run("run", "status", "--run-id", run_id, env=env).stdout)
    rec = next(d for d in out["dispatches"] if d["id"] == dispatch_id)
    assert rec["status"] == "submission_uncertain"
    assert "timeout" in rec.get("error", "")

def test_report_identity(artifact, run_id, dispatch_id):
    doc = json.loads(pathlib.Path(artifact).read_text(encoding="utf-8"))
    assert doc["run_id"] == run_id and doc["dispatch_id"] == dispatch_id
    assert doc["status"] in ("complete", "blocked")
    assert doc["verdict"] in ("pass", "fail", "needs-work")
    assert len(doc["summary"].strip()) >= 20
    assert doc["evidence"] and all(len(s.strip()) >= 8 for s in doc["evidence"])

def test_unknown_never_settles(status):
    assert status == "unknown"
    # 契约：unknown 只能导致 uncertain，绝不能 settled
```

---

## 6. 清理与回滚

### 6.1 关闭顺序

只允许关闭本方案创建的 tab / workspace，且必须用捕获到的 id，不使用通配或按 label 匹配。

```bash
# 0) 先做安全闸：id 必须存在且不是 w1/w2
test -n "$ITEST_WS" || { echo "ABORT: ITEST_WS 为空，不做任何 close"; }
case "$ITEST_WS" in
  w1|w2) echo "ABORT: 拒绝关闭受保护 workspace $ITEST_WS"; exit 1;;
esac

# 1) 关闭测试内额外的 tab（探针 tab、超时 tab）
for t in "${PROBE_TAB:-}" "${TAB2:-}"; do
  [ -n "$t" ] && herdr tab close "$t" && echo "closed tab $t"
done

# 2) 关闭测试 workspace（其内剩余 pane 一并回收）
herdr workspace close "$ITEST_WS"
echo "exit=$?"
```

- 若 `workspace close` 因仍有 agent 在前台而失败：先用 `herdr pane close <id>` 关闭该 workspace 内的 pane，再重试一次；仍失败则**如实报告**并保留现场，不升级为对其他 workspace 的操作。
- 该 workspace 内还有别的 tab 属正常情况（`init` 创建的 `htm-dev-lite` tab），随 workspace 一起回收。

### 6.2 临时目录与状态目录

```bash
# 确认隔离状态目录没被复制回真实目录
test "$(cd "$HTM_STATE_DIR" && pwd)" = "$ITEST_ROOT/state" && echo "state 仍在临时目录内"

# 删除临时目录（只删本测试 mktemp 出来的目录）
case "$ITEST_ROOT" in
  /tmp/htm-itest.*|/var/folders/*/T/htm-itest.*) rm -rf "$ITEST_ROOT";;
  *) echo "ABORT: ITEST_ROOT 不在预期临时前缀，不删除: $ITEST_ROOT";;
esac

# 恢复 shell 变量
unset HTM_STATE_DIR RUN D1 D2 D3 D4 TD1 DEV REV DEV_PANE REV_PANE ITEST_WS ITEST_TAB \
      ITEST_ROOT_PANE PROBE_TAB PROBE_PANE TAB2 TAB2_PANE DEV_T
unset -f htm
```

- `HTM_STATE_DIR` 不设置后，后续 shell 恢复默认 `~/.local/state/herdr-team`；本方案全程未向默认目录写入。
- 若某个 shell 曾漏设 `HTM_STATE_DIR` 而误写了默认目录：**不要**删除默认目录，改为如实报告受影响的文件，由用户决定。

### 6.3 明确禁止

- **不要删除或关闭 `w1`（pagi）与 `w2`（herdr-team）**，包括其中的 tab、pane、agent。
- 不要执行 `herdr server stop`、`herdr workspace close w1|w2`、`herdr agent rename` 作用于受保护 agent。
- 不要删除 `~/pagi`、`~/dev/herdr-team` 下的任何文件。
- 不要用 `rclone sync` / `rm -rf` 等破坏性命令处理任何项目目录。

---

## 7. 风险与未验证项

| 项 | 现状 | 影响 / 处置 |
|---|---|---|
| `agent prompt --wait` 返回结构 | **尚未实测**。`agent_status_of` 依赖 `agent_status`（回退 `status`），并会解开 `{"agent": {...}}` 包装 | §4.2 就是为它设计的；若形状不符，记录“需补映射”，不得静默通过 |
| `agent start` 返回结构 | 未在本轮验证（只验证了 `agent get` 的 `.result.agent`） | `init` 只用它判断成败；失败时靠 `htm status` 的 `live` 复核 |
| `workspace create` / `tab create` / `pane split` 返回结构 | 依据 `herdr --skill` 文档与本次只读实测归纳，本轮未实际创建 | §3.2 的 `jq` 断言若取到 `null`，先 `herdr ... \| jq .` 看真实形状再调整，不要用默认值继续 |
| `agent_prompt_stalled` | 无法确定性构造 | 只做“出现即记录”，不做复现要求 |
| Herdr 升级字段漂移 | 0.9.5 已把字段从 `kind`/`status` 改为 `agent`/`agent_status` | 升级后必须重跑 §4.2；`agent_kind_of` / `agent_status_of` 已做双字段兼容，但未来字段改名仍会打破 |
| 测试 agent 与真实 worker 差异 | `itest-worker` 与 `ht…` worker 都是交互式 Claude Code，但任务极短、无仓库依赖 | 典型“长任务、多文件、需审批”的行为未被覆盖 |
| `HTM_STATE_DIR` 隔离 | 会将本测试与真实 `~/.local/state/herdr-team` 完全分开 | 可能掩盖真实状态目录里的跨 run 互斥、锁竞争、路径长度、磁盘配额问题；不得据此宣称“真实多团队并存已验证” |
| 报告工件在临时目录内 | 清理后工件消失，无法事后审计 | 需要保留证据时，在 §6.2 删除前把 `artifacts/` 与 `*.json` 复制到用户指定的保留目录（需另行授权） |
| `run approve` 恒拒绝 | v4.2 有意禁用同用户 TTY 审批（`scripts/htm.py:634-637`） | 任何需要审批的 `--effect sensitive` 派单都会被拒；本方案只用 `--effect read` 与默认 `autonomy` |
| Lead pane 自身状态漂移 | w2:p1 等 Lead 在并行跑任务 | §4.6 只对 worker 做严格断言，Lead 差异如实记录 |
| Herdr 环境变量透传 | 已观察到某些会话 `HERDR_*` 全空 | 必须修启动器；严禁手工伪造 `HERDR_ENV=1` |
| worker 名称 | `init` 生成 `ht<digest>-N-<role>`，不能是 `itest-worker` | 见 §2.4；如需统一命名，属于源码变更，另行评审 |

---

## 附录 A：一次完整执行的最小命令序列（速查）

```bash
# 0. 变量
export REPO=/Users/shakya/dev/herdr-team
export ITEST_ROOT=$(mktemp -d /tmp/htm-itest.XXXXXX)
export HTM_STATE_DIR="$ITEST_ROOT/state"
htm() { python3 "$REPO/scripts/htm.py" "$@"; }

# 1. 基线
herdr agent list | jq -r '.result.agents[] | [.workspace_id,.pane_id,(.name//"-"),(.revision|tostring),(.state_change_seq|tostring)] | @tsv' | sort > "$ITEST_ROOT/00-agents.tsv"
git -C "$REPO" status --porcelain > "$ITEST_ROOT/00-git-status.txt"

# 2. 隔离 workspace
WS_JSON=$(herdr workspace create --label htm-itest --cwd "$ITEST_ROOT" --focus)
export ITEST_WS=$(printf '%s' "$WS_JSON" | jq -r '.result.workspace.workspace_id')
export ITEST_TAB=$(printf '%s' "$WS_JSON" | jq -r '.result.tab.tab_id')

# 3. 进入该 workspace 的根 pane 后（确认 HERDR_ENV=1 与 HERDR_WORKSPACE_ID=$ITEST_WS）

# 4. 探针 agent
PROBE_JSON=$(herdr tab create --workspace "$ITEST_WS" --label itest-probe --cwd "$ITEST_ROOT" --no-focus)
export PROBE_PANE=$(printf '%s' "$PROBE_JSON" | jq -r '.result.root_pane.pane_id')
herdr agent start itest-worker --kind claude --pane "$PROBE_PANE" --timeout 120000

# 5. htm 团队
htm validate
htm init dev-lite --layout tab --worktree off --cwd "$ITEST_ROOT" | tee "$ITEST_ROOT/11-init.json"
export DEV=$(jq -r '.team.roles[]|select(.role=="developer")|.agent_name' "$ITEST_ROOT/11-init.json")

# 6. run + gate plan_brief + dispatch
RUN=$(htm run start --goal '真实联调：验证 dispatch/collect 链路与异常降级' | jq -r .run_id)
htm run gate --run-id "$RUN" --decision pass --evidence 'plan_brief：仅验证链路，验收标准见方案文档'
D1=$(htm run dispatch --run-id "$RUN" --role developer --effect read --message '最小验证任务，按提示写 JSON 报告' | jq -r .dispatch_id)
htm run status --run-id "$RUN" | jq -r --arg d "$D1" '.dispatches[]|select(.id==$d)|{id,status,error}'

# 7. 负例 / 超时 / 隔离复核 → 见 §4.1–4.6

# 8. 清理 → 见 §6
```

## 附录 B：报告工件契约（worker 必须写出）

路径由 `htm dispatch` 在提示词里给出，形如：

```text
$HTM_STATE_DIR/artifacts/<run_id>/<dispatch_id>.json
```

内容（字段名必须完全一致）：

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

- 必须**先写同目录临时文件再原子 rename**；直接覆盖或写到别处都不被接受。
- 必须是非符号链接的普通文件，且小于 1 MiB。
- `tests` 可为空数组，但只要出现，每项都必须有 `command` 字符串与整数 `exit_code`。
- 终端输出、聊天消息、`agent read` 的滚动内容都**不是**报告。
