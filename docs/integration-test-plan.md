# herdr-team 阶段化联调方案（Herdr 0.9.5 · 派单通信链路）

- 适用版本：herdr-team v4.2.0-beta.1（`scripts/htm.py`）、Herdr 0.9.5。本次执行对应的提交由证据目录 `00-head.txt` 现场记录（不在此硬编码，避免失真）。
- 文档状态：**方案，未执行**。本文档不被文档修订轮执行；创建 workspace / tab / pane、启动 agent、`htm init`、`run start/dispatch/collect/gate/cancel`、关闭资源等操作，均需用户单独、明确授权。
- 读者：能在 Herdr 内操作 CLI 的工程师或 Agent。命令可复制，但必须先完成 §3 的初始化与 `abort()` 定义。
- 阶段：A（单 Worker 通信验证）→ B（HTM 调度/结果/异常验证）。**A 不 Go，不得进入 B**。

---

## 1. Context 与修订说明

上一版方案（911 行）在可执行性上有 10 类缺陷：失败分支只打印不退出；重复派单序列没有落到账本状态；报告归属从 dispatch 响应读取不存在的 `run_id`；超时用例从失败命令的 stdout 取 dispatch id；在真实 state 里与活跃 worker 抢写同一工件；断言用“忽略退出码”的写法（强制成功、丢弃 stderr）掩盖失败；blocked 场景主动请求危险操作；清理前不校验归属；单阶段覆盖过多、缺少独立门槛；agent 与 prompt 数量失控。

- v3 定向整改（2026-10-31）：修正 jq key 顺序断言、export 退出码、nonce 验证、GO 落盘、跨 workspace 变量恢复、超时非确定性、Mock 文案、外部清理、fail-closed jq、资源数量。

### 1.1 十项修正映射

| # | 修正 | 落地位置 |
|---|---|---|
| 1 | 所有 ABORT/STOP 统一 `abort()` 并真正 `exit 1`；workspace/tab/pane ID 校验非空、非 `null`、格式、前缀一致 | §3.1、§3.3 |
| 2 | 重复派单精确序列：`--no-wait` → 账本断言 `submitted_unsettled` → 第二次派单**断言被拒**（exit=2 + stderr 正则）→ `herdr agent wait` → `collect --dispatch-id <账本里的真实 id>` | §5.3–§5.5 |
| 3 | 报告归属**绝不**从 dispatch 响应读 `run_id`；run_id 只来自 `htm run list` 与 `$HTM_STATE_DIR/runs/*.json` | §5.7 |
| 4 | 超时用例用 run 文件集合差集定位新 run，再从 `dispatches[-1].id` 取 dispatch id 并断言唯一 | §5.9 |
| 5 | 明确「同一 pane 只允许一个 non-terminal run」，给出只读断言 | §3.6、§5.8 |
| 6 | 错误报告注入改为独立 Mock：`cp -R` 一份 state 到 `$MOCK/state`，在副本中伪造账本与工件后直接 `collect` | §5.10 |
| 7 | 16 条机器断言重写，无 `\|\| true`、无 `2>/dev/null`；每条含编号/对象/命令/通过条件/失败诊断；身份不符按 JSON 判定 | §6 |
| 8 | Blocked 只做被动观察，不构造危险操作；未发生标「未验证」；禁止 send-keys 代答 | §5.12 |
| 9 | 清理前依次断言资源前缀、worker 消失、workspace 已关闭且不在列表、临时目录安全；失败即 abort 并保留现场 | §8.2 |
| 10 | 拆阶段 A / B，各有独立 Go/No-Go；A 不 Go 不进 B | §4.6、§5.14 |

### 1.2 agent 与 prompt 预算（硬性）

- 全程只注册 **1 个 team**（`dev-lite`）；`init` 创建 `developer` + `reviewer` 两个 worker，**只向 developer 发 prompt**，reviewer 从不被驱动。测试 agent 总数 = 2，被驱动的 = 1。
- 阶段 A **不额外创建探针**：直接以主 team 的 developer 作为探针（2 次 prompt：正常 + 超时）。阶段 A 与阶段 B 复用同一个 agent。
- 阶段 B 的 htm prompt 共 3 次：D1（`--no-wait`）、D2、超时派单。
- 超时用例在同一 team 内完成（`init` 时 `--set orchestration.task_timeout_seconds=60`，用 worker 侧 `sleep 180` 触发真实超时），不注册第二个 team、不建第二个 Lead。
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

- [ ] 用户已明确授权：1 个测试 Workspace；2 个 Tab（根 Tab + Worker Tab）；1 个 team、2 个 Worker agent（developer + reviewer，仅 developer 被派单）；共 5 次 prompt（A1、A2、D1、D2、B8）
- [ ] 执行环境在 Herdr 内，`HERDR_ENV=1` 且 `HERDR_WORKSPACE_ID` / `HERDR_TAB_ID` / `HERDR_PANE_ID` 均非空；缺失则**停止**并修启动器，禁止手工伪造
- [ ] 已确认 `config/team.yaml` 默认 `max_rounds=5, max_retries=1, max_parallel=3, task_timeout_seconds=300`，governance `autonomy=workspace-write, approval=boundary`；本方案在 `init` 时用 `--set orchestration.task_timeout_seconds=60` 覆盖（范围 15..300）
- [ ] 已确认不改 `scripts/htm.py`、`tests/`、`pyproject.toml`、`uv.lock`、`SKILL.md`、`config/`
- [ ] 已确认不碰 `~/pagi`、`w1`、`w2`（见 §3.2 受保护清单）
- [ ] 已阅读 §3.1 的 `abort()` / ID 校验器，并在每个 shell 中定义
- [ ] **目录信任已由人确认**：目标 cwd 里已人工运行过一次 `claude` 并选 Yes，`/exit` 后根 pane 无残留 agent（见 §3.4）

### 2.2 阶段 A（单 Worker 通信）

- [ ] 探针就是主 team 的 developer（`ht…`），不额外创建 agent
- [ ] 正常 prompt（A1）exit=0，返回体结构已记录，`agent_status_of(doc["result"])` 落在 `{idle, done}`（不得喂未解包的 `doc`，见 §3.8），且本轮 nonce 在 **`⏺` 模型回复行**命中（排除 `❯` 输入回显，见 §4.3）
- [ ] 超时 prompt（A2）记录 timeout 或未触发；不据此断言输入未送达
- [ ] 证据写入 `$EVIDENCE/phaseA/`
- [ ] 阶段 A Go/No-Go 已判定为 **Go**

### 2.3 阶段 B（HTM 调度）

- [ ] 仅在 A=Go 后开始
- [ ] 只有一个 team，`init` 时加 `--set orchestration.max_retries=5 --set orchestration.task_timeout_seconds=60`
- [ ] 主 run 是该 pane 上唯一的 non-terminal run（见 §3.6）
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
abort() {
  printf 'ABORT: %s\n' "$*" >&2
  if [ -n "${GO_FILE:-}" ]; then
    if [ -e "$GO_FILE" ] || [ -L "$GO_FILE" ]; then
      printf '门槛文件已存在，拒绝覆盖: %s\n' "$GO_FILE" >&2
      exit 1
    fi
    (set -C; printf 'NO-GO: %s\n' "$*" > "$GO_FILE") || {
      printf '无法写入 NO-GO 文件或文件冲突: %s\n' "$GO_FILE" >&2
      exit 1
    }
  fi
  exit 1
}
need() {
  [ -n "${1:-}" ] && [ "${1:-}" != "null" ] || abort "${2:-missing value}"
}
ID_ALPHA='123456789ABCDEFGHJKMNPQRSTVWXYZ0'
need_ws() {
  case "${1:-}" in *$'\n'*) abort "非法 workspace id: ${1:-<empty>}";; esac
  printf '%s' "${1:-}" | grep -Eq '^w['"$ID_ALPHA"']+$' || abort "非法 workspace id: ${1:-<empty>}"
}
need_tab() {
  case "${1:-}" in *$'\n'*) abort "非法 tab id: ${1:-<empty>}";; esac
  printf '%s' "${1:-}" | grep -Eq '^w['"$ID_ALPHA"']+:t['"$ID_ALPHA"']+$' || abort "非法 tab id: ${1:-<empty>}"
}
need_pane() {
  case "${1:-}" in *$'\n'*) abort "非法 pane id: ${1:-<empty>}";; esac
  printf '%s' "${1:-}" | grep -Eq '^w['"$ID_ALPHA"']+:p['"$ID_ALPHA"']+$' || abort "非法 pane id: ${1:-<empty>}"
}
same_ws() {
  [ "${2%%:*}" = "$1" ] || abort "$3: 前缀不一致 ($1 vs $2)"
}
assert_no_protected() {
  case "$1" in w1:*|w2:*) abort "$2 落在受保护 workspace: $1";; esac
}
assert_single_active_run() {
  local ws="$1" pane="$2" dir="$HTM_STATE_DIR/runs" n=0 f out
  if [ -d "$dir" ]; then
    for f in "$dir"/*.json; do
      [ -e "$f" ] || continue
      if ! out=$(jq -r 'select(.status=="active") | [(.context.workspace // ""), (.context.lead_pane // ""), .id] | @tsv' "$f"); then
        abort "账本解析失败，拒绝继续: $f"
      fi
      [ -n "$out" ] || continue
      case "$out" in
        "$ws"$'\t'"$pane"$'\t'*) n=$((n+1));;
      esac
    done
  fi
  [ "$n" -le 1 ] || abort "同一 pane 上有 $n 个 active run ($ws/$pane)"
}
```

一次性初始化变量：

```bash
export REPO=/Users/shakya/dev/herdr-team
_tmp=$(python3 -c 'import secrets; print(secrets.token_hex(4))') || abort "生成 RUNTOKEN 失败"
export RUNTOKEN="$_tmp"
printf '%s' "$RUNTOKEN" | grep -Eq '^[a-z0-9]{6,16}$' || abort "RUNTOKEN 格式非法"
_tmp=$(mktemp -d "${TMPDIR:-/tmp}/htm-itest-$RUNTOKEN.XXXXXX") || abort "mktemp 失败"
export ITEST_ROOT="$_tmp"
case "$ITEST_ROOT" in
  "${TMPDIR:-/tmp}"/htm-itest-*) ;;
  *) abort "临时目录前缀异常: $ITEST_ROOT" ;;
esac
export HTM_STATE_DIR="$ITEST_ROOT/state"
export EVIDENCE="$ITEST_ROOT/evidence"
mkdir -p "$EVIDENCE/phaseA" "$EVIDENCE/phaseB" || abort "无法创建证据目录"
htm() { python3 "$REPO/scripts/htm.py" "$@"; }
# 引导文件供新 pane 和外部控制终端恢复；使用 shell 引号转义真实路径
{
  printf 'export RUNTOKEN=%q ITEST_ROOT=%q HTM_STATE_DIR=%q EVIDENCE=%q REPO=%q ID_ALPHA=%q\n' \
    "$RUNTOKEN" "$ITEST_ROOT" "$HTM_STATE_DIR" "$EVIDENCE" "$REPO" "$ID_ALPHA"
  typeset -f abort need need_ws need_tab need_pane same_ws assert_no_protected assert_single_active_run htm
} > "$ITEST_ROOT/env.sh" || abort "写入引导文件失败"
printf '引导文件：%s/env.sh\n' "$ITEST_ROOT"
```

要点：

- `RUNTOKEN` 每次随机生成，禁止写死；进入新 pane 时变量和函数不会继承，复制上一步打印的引导文件路径并执行 `set -u; . "<打印的引导文件路径>" || exit 1`（路径替换为实际值，不硬编码）；若已设置 `ITEST_ROOT`，执行 `set -u; . "$ITEST_ROOT/env.sh" || exit 1`。source 失败即停止。
- `HTM_STATE_DIR` 在模块导入时读取（`scripts/htm.py:34`），必须每条命令都带着它；未设置时默认 `~/.local/state/herdr-team`。
- 所有 ABORT 必须 `exit 1`；不允许 `echo "ABORT: ..."` 后继续执行。
- `env.sh` 只恢复变量与函数，**不恢复 shell 选项**：每个新 shell 都要先执行 §3.1 的 `set -u`，再 `source`。本方案不依赖 `pipefail`，因此每条管道都按 §3.1 的约定单独检查上游退出码，而不是指望 shell 选项兜底。

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

### 3.3 建立不可变基线，然后创建测试 workspace（阶段 A 前）

**采集时机（硬性）**：下列基线必须在**任何测试资源（workspace / tab / pane / agent / run / state 文件）创建之前**采集，并且此后不得重写。`00-agents-before.tsv` 是 §5.13 B12 唯一允许使用的比对基准；**不得用阶段 B 中段或末段的快照替代**，否则“全程未变”的结论超出证据。创建 workspace 前采集是流程约束，不是可独立审计的 workspace 创建时序证明；只读权限也不是防篡改证明。采集后立即置为只读，时间锚点单独落盘为 `00-baseline-epoch.txt`（供默认 state 扫描使用，不得用会变的 `env.sh` 充当锚点），采集时间与校验和写入 `00-baseline-meta.txt`。

```bash
BASE="$EVIDENCE/phaseB"
# 1) 采集基线列表与仓库状态（均为写操作之前）
# 先落盘原始 JSON 再转 TSV：管道会把 herdr 的退出码丢掉，只看最后一段就等于 fail-open
herdr agent list > "$BASE/00-agents-before.json" || abort "基线 agent list 失败"
jq -r '.result.agents[] | [.workspace_id, .pane_id, (.name // "-"), (.agent_status // "-"), (.revision|tostring), (.state_change_seq|tostring)] | @tsv' \
  "$BASE/00-agents-before.json" > "$BASE/00-agents-before.raw" || abort "基线 agent jq 失败"
[ -s "$BASE/00-agents-before.raw" ] || abort "基线 agent 列表为空"
sort "$BASE/00-agents-before.raw" > "$BASE/00-agents-before.tsv" || abort "基线 agent sort 失败"
herdr workspace list > "$BASE/00-workspaces-before.json" || abort "基线 workspace list 失败"
jq -r '.result.workspaces[] | [.workspace_id, (.label // "-")] | @tsv' \
  "$BASE/00-workspaces-before.json" > "$BASE/00-workspaces-before.raw" || abort "基线 workspace jq 失败"
[ -s "$BASE/00-workspaces-before.raw" ] || abort "基线 workspace 列表为空"
sort "$BASE/00-workspaces-before.raw" > "$BASE/00-workspaces-before.tsv" || abort "基线 workspace sort 失败"
git -C "$REPO" rev-parse HEAD > "$BASE/00-head.txt" || abort "记录 HEAD 失败"
git -C "$REPO" status --porcelain > "$BASE/00-git-status.txt" || abort "记录 git status 失败"
for p in w1:p1 w1:p5 w1:p6 w2:p1 w2:p2 w2:p3; do
  count=$(awk -v p="$p" '$2==p {n++} END {print n+0}' "$BASE/00-agents-before.tsv") || abort "基线检查失败"
  [ "$count" -gt 0 ] || abort "基线缺少受保护 pane: $p"
done
# 2) 记录默认 state 目录在基线时刻是否存在（§5.13 fail-closed 判定用）
DEFAULT_STATE="$HOME/.local/state/herdr-team"
default_state_presence() {
  local dir="$HOME" component
  [ -d "$dir" ] && [ -x "$dir" ] || abort "HOME 不可搜索"
  for component in .local state herdr-team; do
    dir="$dir/$component"
    if [ -L "$dir" ]; then
      abort "默认 state 路径含符号链接，需人工核查: $dir"
    elif [ -e "$dir" ]; then
      [ -d "$dir" ] && [ -x "$dir" ] && [ -r "$dir" ] || abort "默认 state 路径非目录或不可访问: $dir"
    else
      printf 'absent\n' || abort "输出存在性失败"
      return
    fi
  done
  printf 'present\n' || abort "输出存在性失败"
}
typeset -f default_state_presence >> "$ITEST_ROOT/env.sh" || abort "保存默认 state 检查函数失败"
DEFAULT_PRESENCE=$(default_state_presence) || abort "检查默认 state 失败"
if [ "$DEFAULT_PRESENCE" = "present" ]; then
  printf 'present\n' > "$BASE/00-default-state-exists.txt" || abort "记录默认 state 存在性失败"
else
  printf 'absent\n' > "$BASE/00-default-state-exists.txt" || abort "记录默认 state 存在性失败"
fi

# 3) 基线全部落盘后再取时间锚点：保证基线 mtime <= epoch，且 epoch 仍早于任何测试资源创建
BASELINE_EPOCH=$(date +%s) || abort "生成基线时间戳失败"
printf '%s\n' "$BASELINE_EPOCH" > "$BASE/00-baseline-epoch.txt" || abort "写入基线时间锚点失败"
chmod 0444 "$BASE/00-baseline-epoch.txt" || abort "基线时间锚点置为只读失败"

# 4) 元信息 + 校验和，然后冻结基线文件
CAPTURED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ) || abort "读取采集时间失败"
SHA_OUTPUT=$(shasum -a 256 "$BASE/00-agents-before.tsv") || abort "计算基线校验和失败"
SHA=${SHA_OUTPUT%% *}
printf '%s\n' "$SHA" | grep -Eq '^[a-f0-9]{64}$' || abort "基线校验和格式非法"
AGENTS_MTIME=$(stat -f '%m' "$BASE/00-agents-before.tsv") || abort "读取基线 mtime 失败"
{
  printf 'captured_at=%s\n' "$CAPTURED_AT"
  printf 'epoch=%s\n' "$BASELINE_EPOCH"
  printf 'agents_sha256=%s\n' "$SHA"
  printf 'agents_mtime=%s\n' "$AGENTS_MTIME"
  printf '%s\n' 'rule=阶段 B 的 B12 只能与本文件及 00-agents-before.tsv 比对；不得重写或用末段快照替代'
} > "$BASE/00-baseline-meta.txt" || abort "写入基线元信息失败"
META_SHA=$(awk -F= '$1=="agents_sha256"{print $2}' "$BASE/00-baseline-meta.txt") || abort "读取元信息校验和失败"
printf '%s\n' "$META_SHA" | grep -Eq '^[a-f0-9]{64}$' || abort "元信息校验和格式非法"
[ "$META_SHA" = "$SHA" ] || abort "元信息校验和与基线不符"
chmod 0444 "$BASE/00-agents-before.tsv" "$BASE/00-baseline-meta.txt" \
  || abort "基线文件置为只读失败"
```

```bash
WS_JSON=$(herdr workspace create --label "htm-itest-$RUNTOKEN" --cwd "$ITEST_ROOT" --focus) || abort "workspace create 失败"
_tmp=$(jq -er '.result.workspace.workspace_id' <<< "$WS_JSON") || abort "取不到 workspace id"
export ITEST_WS="$_tmp"
_tmp=$(jq -er '.result.tab.tab_id' <<< "$WS_JSON") || abort "取不到 tab id"
export ITEST_TAB="$_tmp"
_tmp=$(jq -er '.result.root_pane.pane_id' <<< "$WS_JSON") || abort "取不到 root pane id"
export ITEST_ROOT_PANE="$_tmp"
need_ws "$ITEST_WS"; need_tab "$ITEST_TAB"; need_pane "$ITEST_ROOT_PANE"
same_ws "$ITEST_WS" "$ITEST_TAB" "workspace/tab"
same_ws "$ITEST_WS" "$ITEST_ROOT_PANE" "workspace/pane"
case "$ITEST_WS" in w1|w2) abort "测试 workspace 不独立: $ITEST_WS";; esac
printf 'export ITEST_WS=%q ITEST_TAB=%q ITEST_ROOT_PANE=%q\n' \
  "$ITEST_WS" "$ITEST_TAB" "$ITEST_ROOT_PANE" >> "$ITEST_ROOT/env.sh" || abort "保存 workspace 身份失败"
printf '在新 pane 粘贴执行（source 失败即停止）：set -u; . %q || exit 1\n' "$ITEST_ROOT/env.sh" || abort "打印引导命令失败"
```

创建测试资源之后，立即确认基线仍未被改写（B12 依赖它）：

```bash
SHA_OUTPUT=$(shasum -a 256 "$BASE/00-agents-before.tsv") || abort "计算基线校验和失败"
SHA=${SHA_OUTPUT%% *}
META_SHA=$(awk -F= '$1=="agents_sha256"{print $2}' "$BASE/00-baseline-meta.txt") || abort "读取元信息校验和失败"
printf '%s\n' "$SHA" | grep -Eq '^[a-f0-9]{64}$' || abort "基线校验和格式非法"
printf '%s\n' "$META_SHA" | grep -Eq '^[a-f0-9]{64}$' || abort "元信息校验和格式非法"
[ "$SHA" = "$META_SHA" ] || abort "基线 00-agents-before.tsv 已被改写，B12 失去可比基准"
```

### 3.4 目录信任预检（必须人工，`htm init` 之前）

**为什么需要**：Claude Code 对从未信任过的 cwd 会弹出 `Is this a project you created or one you trust?` 对话框。`agent start ... --kind claude` 无法越过该对话框，agent 会停在 `blocked`，`htm init` 因此返回 `partial`（`agent_not_ready ... blocked during startup`）。而且 `init` 对该 partial team **拒绝自动重试**：`scripts/htm.py:497-498` 明确抛出 `Previous init is partial ... no automatic duplicate creation.`，所以一旦踩中就只能人工收拾现场。

先在根 pane 恢复变量与函数：粘贴执行 §3.3 打印的 `set -u; . <path>/env.sh || exit 1`（新 pane 尚未设置 `$ITEST_ROOT`；source 失败即停止），然后断言身份；任一断言失败即 abort：

```bash
[ "${HERDR_ENV:-}" = "1" ] || abort "HERDR_ENV 缺失；禁止伪造"
[ "${HERDR_WORKSPACE_ID:-}" = "$ITEST_WS" ] || abort "不在测试 workspace（期望 $ITEST_WS）"
[ "${HERDR_PANE_ID:-}" = "$ITEST_ROOT_PANE" ] || abort "不在测试根 pane（期望 $ITEST_ROOT_PANE）"
CURRENT_DIR=$(pwd) || abort "读取 cwd 失败"
[ "$CURRENT_DIR" = "$ITEST_ROOT" ] || abort "cwd 不是临时目录: $CURRENT_DIR"
```

**做法**（在目标 cwd 的根 pane 里，即 §3.3 创建的 `$ITEST_ROOT_PANE`，cwd 为 `$ITEST_ROOT`）：

1. 直接在该 pane 运行 `claude`。
2. 若出现 trust 对话框：**停止自动流程，交给人在终端里确认**（选 Yes）。
3. 确认后输入 `/exit` 退出 claude。
4. 执行 `herdr agent list`，确认该 pane 没有残留 agent（`pane_id == $ITEST_ROOT_PANE` 的行数为 0）。
5. 再按 §3.5 执行 `htm init`。

```bash
# 在第 4 步之后、htm init 之前执行
herdr agent list > "$EVIDENCE/phaseB/00b-agents-before-init.json" || abort "agent list 失败"
jq -e --arg p "$ITEST_ROOT_PANE" '[.result.agents[]|select(.pane_id==$p)]|length==0' \
  "$EVIDENCE/phaseB/00b-agents-before-init.json" >/dev/null \
  || abort "根 pane 仍有残留 agent，不得 init: $ITEST_ROOT_PANE"
```

**明确禁止**：不得用 `send-keys`（或其他自动输入手段）代替人应答 trust 对话框，除非用户当轮明确授权。

**partial 发生后的处理**：**不要**删除 team 文件硬闯，也不要手工把 `status` 改回正常。记录现场（team 文件路径、`htm status` 输出、`herdr agent list`），如实向用户报告，由人决定是否新建 workspace、换新 `RUNTOKEN` 重来。

### 3.5 注册唯一的 team（阶段 A 前）

阶段 A 的探针就是这里生成的 developer，因此 team 必须在阶段 A 之前注册。**必须在测试 workspace 的根 pane（`$ITEST_ROOT_PANE`）里执行**，先在新 pane 执行 §3.3 打印的 `set -u; . <path>/env.sh || exit 1` 恢复变量与函数（source 失败即停止；若前一节已成功执行可跳过），然后断言身份：

```bash
[ "${HERDR_ENV:-}" = "1" ] || abort "HERDR_ENV 缺失；禁止伪造"
[ "${HERDR_WORKSPACE_ID:-}" = "$ITEST_WS" ] || abort "不在测试 workspace（期望 $ITEST_WS）"
[ "${HERDR_PANE_ID:-}" = "$ITEST_ROOT_PANE" ] || abort "不在测试根 pane（期望 $ITEST_ROOT_PANE）"
CURRENT_DIR=$(pwd) || abort "读取 cwd 失败"
[ "$CURRENT_DIR" = "$ITEST_ROOT" ] || abort "cwd 不是临时目录: $CURRENT_DIR"

htm validate > "$EVIDENCE/phaseB/00-validate.json" || abort "validate 失败"
jq -e '.valid == true' "$EVIDENCE/phaseB/00-validate.json" >/dev/null || abort "validate 未返回 valid"
htm init dev-lite --layout tab --worktree off --cwd "$ITEST_ROOT" \
  --set orchestration.max_retries=5 \
  --set orchestration.task_timeout_seconds=60 > "$EVIDENCE/phaseB/01-init.json" || abort "init 失败"
jq -e '.reused == false' "$EVIDENCE/phaseB/01-init.json" >/dev/null \
  || abort "init 返回 reused=true；先人工核对，不得重跑"
_tmp=$(jq -er '.team.roles[]|select(.role=="developer")|.agent_name' "$EVIDENCE/phaseB/01-init.json") || abort "取不到 developer"
export DEV="$_tmp"
_tmp=$(jq -er '.team.roles[]|select(.role=="developer")|.pane_id' "$EVIDENCE/phaseB/01-init.json") || abort "取不到 developer pane"
export DEV_PANE="$_tmp"
_tmp=$(jq -er '.team.roles[]|select(.role=="reviewer")|.agent_name' "$EVIDENCE/phaseB/01-init.json") || abort "取不到 reviewer"
export REV="$_tmp"
need_pane "$DEV_PANE"
same_ws "$ITEST_WS" "$DEV_PANE" "workspace/developer"
assert_no_protected "$DEV_PANE" "developer"
case "$DEV" in ht*) ;; *) abort "worker 名不符合 ht<digest>-N-<role>: $DEV";; esac
case "$REV" in ht*) ;; *) abort "worker 名不符合 ht<digest>-N-<role>: $REV";; esac
printf 'export DEV=%q DEV_PANE=%q REV=%q\n' "$DEV" "$DEV_PANE" "$REV" >> "$ITEST_ROOT/env.sh" || abort "保存 worker 身份失败"
htm status > "$EVIDENCE/phaseB/02-status.json" || abort "htm status 失败"
jq -e '.agents[] | select(.role=="developer") | .live==true and (.observed_state=="idle" or .observed_state=="done")' \
  "$EVIDENCE/phaseB/02-status.json" >/dev/null || abort "developer 未处于 idle/done"
jq -e '.agents[] | select(.role=="reviewer") | .live==true' "$EVIDENCE/phaseB/02-status.json" >/dev/null \
  || abort "reviewer 未就绪（它不会被驱动，但必须存在）"
```

要点：

- `--layout tab` 让 worker 独占 `init` 自建的 `htm-dev-lite` tab，与 Lead 分屏；`--worktree off` 避免触碰任何 Git 仓库。
- `--set orchestration.max_retries=5` 是必须的：同一 stage_attempt 内同一 role 的派单上限是 `1+max_retries`，默认 1 只允许 2 次，而本方案在同一次 run 内需要 D1 + D2 + 超时共 3 次。
- `--set orchestration.task_timeout_seconds=60` 把 htm 侧单次派单等待固定为 60 秒（合法范围 15..300）；**阶段 B 的 htm 派单**统一使用该值，阶段 A 的 A2 是直接 CLI 调用，使用 `--timeout 15000`（见 §4.4）。该值事后不可改。
- `agent start` 没有 `--cwd`；worker 的 cwd 来自 `init` 的 `--cwd`。`agent_not_ready` 表示启动期被弹窗阻塞，由**人**处理，不自动应答（见 §3.4）。

### 3.6 单 pane 单 Run 约束（只读断言）

`run_start` 会拒绝与其 `(workspace, pane_id)` 重叠的未完成 Run（`scripts/htm.py:591-598`）。本方案额外要求执行者主动断言——使用 §3.1 已定义的 `assert_single_active_run <ws> <pane>`，字段依据 `scripts/htm.py:234-242,599-601`（`workspace` / `lead_pane`）；它只计数 `active`，账本解析失败即 abort，在 `runs/` 目录不存在时按 0 处理，不使用 `2>/dev/null`，逐文件读取以避免 `jq -s` 对空 glob 的行为差异：

```bash
# 建 run 前与建 run 后各执行一次
assert_single_active_run "$ITEST_WS" "$ITEST_ROOT_PANE"
```

### 3.7 已核实 CLI / 实现事实

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
| Herdr public ID 使用 32 字符 Crockford 风格字母表 `123456789ABCDEFGHJKMNPQRSTVWXYZ0`，排除 I/L/O/U；例如 `wA`、`wA:t1`、`wA:p1`，不得假设只含十进制数字 | Herdr `src/workspace.rs` 的 `PUBLIC_ID_ALPHABET` |
| 最低支持 Herdr 0.9.5（protocol 22） | Herdr 0.9.5 |
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
| `process()` 已解包 Herdr 响应：`return out.get("result", out)` | `:309` |
| `agent_status_of()` 读 `agent_status`（回退 `status`）；缺省返回 `unknown`，调用方按 uncertain 处理 | `:339-352` |

`--set orchestration.max_retries=5` 的必要性：同一 stage_attempt 内对同一 role 的派单上限是 `1+max_retries`。默认 1 只允许 2 次；本方案在 `implement` 上需要 D1、D2、超时共 3 次，并预留 collect 未结算后的重派余量，因此固定到上限 5（允许 6 次）。`task_timeout_seconds` 在 `init` 时固化（本方案取 60），事后不能改，所以超时用例靠 worker 侧 `sleep 180` 触发，而不是改配置或开第二个 team。

命名说明：`htm` 的 worker 由 `init` 生成 `ht…`，`verify_existing()` 按 `team["agent_name"]` 查活体，因此**不能**把派单目标改成其他探针名。阶段 A 的原始探针调用不写入 htm 账本；同一 agent 在阶段 B 由 `init` 生成的 `ht…` 名称进入账本。

### 3.8 `.result` 状态解析（原始 CLI vs htm 内部）

真实 `herdr agent prompt --wait` 的原始返回是 `{id, result}`，其中 `result = {agent, type}`；Agent 对象在 `result.agent` 里，含 `agent_status` / `agent` / `pane_id` 等字段。

- **htm 内部**：`process()` 已经 `return out.get("result", out)`（`scripts/htm.py:309`），所以 `agent_status_of()` 实际收到的是**已解包的 result 层**对象，能正确解析为 `done`/`idle`。
- 因此“顶层没有 status”这一说法是针对**原始 CLI 输出**的，不是针对 htm 内部。文档中凡是要在 python 里直接调用 `agent_status_of()` 校验原始输出，必须先解包：

```python
doc = json.load(open(path))
h.agent_status_of(doc["result"])   # 正确：htm 内部同样是先解包 result 再解析
h.agent_status_of(doc)             # 错误：得到 unknown，属于调用方式错误，不是代码缺陷
```

- 若某处示例直接喂未解包的 `doc` 并观察到 `unknown`，那是**调用方式错误**，不得据此判定 htm 映射缺陷或 No-Go。全文相关示例（§4.3 解析验证、§4.6 A1 状态解析）均已按此修正。

---

## 4. 阶段 A：单 Worker 通信验证

### 4.1 目的

在最小成本下确认：真实 Herdr 0.9.5 的 `agent prompt --wait` 返回形状能被 `agent_status_of` 解释为 `idle`/`done`，超时返回 `Error` 时不能让调用方误判为已送达/已成功。阶段 A 直接使用 §3.5 生成的 `$DEV`（同一个 agent 后续也会被 htm 派单），**不额外创建探针**。

### 4.2 前置

进入阶段 A 前设置 `GO_FILE="$EVIDENCE/phaseA/GO-NOGO.txt"`；每个执行阶段断言的 shell 都须设置。断言失败由 `abort` 写入 NO-GO 并退出；写入失败也须报告 No-Go、保留现场，等待负责人判定。

§3.3、§3.4、§3.5 完成；`herdr agent get "$DEV"` 的 `.result.agent.agent_status` 为 `idle`/`done`。若为其他值则 abort，不得发送。

```bash
GO_FILE="$EVIDENCE/phaseA/GO-NOGO.txt"
[ ! -e "$GO_FILE" ] && [ ! -L "$GO_FILE" ] || abort "阶段 A 门槛文件已存在，必须使用新证据目录"
herdr agent get "$DEV" > "$EVIDENCE/phaseA/00-probe-raw.json" || abort "读取 developer 失败"
jq -r '.result.agent | {name, pane_id, agent, agent_status, cwd}' \
  "$EVIDENCE/phaseA/00-probe-raw.json" > "$EVIDENCE/phaseA/00-probe-agent.json" || abort "解析 developer 失败"
jq -e --arg p "$DEV_PANE" '.pane_id==$p and (.agent_status|IN("idle","done"))' \
  "$EVIDENCE/phaseA/00-probe-agent.json" >/dev/null || abort "developer 不在预期 pane 或未 idle/done"
```

### 4.3 A1 正常 prompt（第 1 次）

```bash
A1_OUT="$EVIDENCE/phaseA/10-prompt-ok.json"
A1_ERR="$EVIDENCE/phaseA/10-prompt-ok.err"
rc=0
herdr agent prompt "$DEV" "只回复 ITEST_PONG_${RUNTOKEN}_A1，然后结束本轮。不要读写任何文件。" \
  --wait --timeout 120000 >"$A1_OUT" 2>"$A1_ERR" || rc=$?
[ "$rc" -eq 0 ] || abort "A1 prompt 非零退出: $rc（见 $A1_ERR）"
jq -e . "$A1_OUT" >/dev/null || abort "A1 返回不是合法 JSON"
jq -r 'keys[]' "$A1_OUT" > "$EVIDENCE/phaseA/11-return-keys.txt" || abort "A1 key 记录失败"
jq -r '.result | keys[]' "$A1_OUT" > "$EVIDENCE/phaseA/12-result-keys.txt" || abort "A1 result key 记录失败"
jq -r '.result | to_entries[] | "\(.key):\(.value|type)"' "$A1_OUT" \
  > "$EVIDENCE/phaseA/13-result-shape.txt" || abort "A1 结构记录失败"
```

解析验证（只记录 key 与短值，不记录长文本）。注意：原始 CLI 输出是 `{id, result}`，Agent 对象在 `result.agent` 里，**必须**先解包 `.result` 再交给 `agent_status_of`（详见 §3.8）：

```bash
python3 - "$A1_OUT" "$REPO/scripts/htm.py" <<'PY' > "$EVIDENCE/phaseA/14-status-parse.txt" || abort "A1 状态解析失败"
import importlib.util, json, sys
doc_path, module = sys.argv[1], sys.argv[2]
spec = importlib.util.spec_from_file_location("htm", module)
h = importlib.util.module_from_spec(spec); spec.loader.exec_module(h)
doc = json.load(open(doc_path, encoding="utf-8"))
res = doc["result"]                      # 原始 CLI 返回 {id, result}
print("result_status_of =", h.agent_status_of(res))
print("result_kind_of =", h.agent_kind_of(res))
PY
cat "$EVIDENCE/phaseA/14-status-parse.txt"
grep -Eq 'result_status_of = (idle|done)$' "$EVIDENCE/phaseA/14-status-parse.txt" \
  || abort "A1 状态无法解析为 idle/done（记录为需补映射，No-Go）"
herdr agent read "$DEV" --source recent-unwrapped --lines 40 > "$EVIDENCE/phaseA/15-a1-read.txt" || abort "A1 终端读取失败"
grep -Eq "^⏺ .*ITEST_PONG_${RUNTOKEN}_A1" "$EVIDENCE/phaseA/15-a1-read.txt" \
  || abort "A1 模型回复行未命中本次 nonce，通信未被证实"
```

状态 `idle/done` 只证明生命周期；只匹配以 `⏺` 开头且含本次 nonce 的模型回复行，可排除以 `❯` 开头的输入回显。模型回复命中证明观察到本轮回复，但本身不证明其他动作已执行。终端输出证据可能含对话内容，留存或分享前须脱敏。

### 4.4 A2 超时 prompt（第 2 次）

用调用方 `--timeout 15000`（15 秒）对 `sleep 45` 的任务发 prompt。模型可能不执行 `sleep 45`，因此 15 秒超时不一定发生；A2 带 nonce，但超时可能尚未完成，不要求命中。

CLI timeout 时的**标准形态**：stderr 为 `{"error":{"code":"timeout","message":"timed out waiting for agent status"}}`，exit=1。

```bash
A2_OUT="$EVIDENCE/phaseA/20-prompt-timeout.json"
A2_ERR="$EVIDENCE/phaseA/20-prompt-timeout.err"
rc=0
herdr agent prompt "$DEV" "先执行 sleep 45，再回复 ITEST_PONG_${RUNTOKEN}_A2。不要读写文件。" \
  --wait --timeout 15000 >"$A2_OUT" 2>"$A2_ERR" || rc=$?
if [ "$rc" -eq 0 ]; then
  printf '%s\n' 'NOT-OBSERVED: timeout 未触发（模型未执行 sleep），本次超时不作为验证项' > "$EVIDENCE/phaseA/16-a2-result.txt" || abort "写入证据失败"
elif [ "$rc" -eq 1 ] && grep -Eq '"code"[[:space:]]*:[[:space:]]*"timeout"' "$A2_ERR"; then
  printf '%s\n' 'VERIFIED: CLI timeout（stderr code=timeout, exit=1）；timeout ≠ 未送达，须用 agent read 查 nonce' \
    > "$EVIDENCE/phaseA/16-a2-result.txt" || abort "写入证据失败"
else
  printf 'NOT-OBSERVED: 非 timeout 错误 rc=%s，超时未验证；失败不代表输入未送达\n' "$rc" > "$EVIDENCE/phaseA/16-a2-result.txt" || abort "写入证据失败"
fi
```

随后尝试等待 developer 稳定，成功后再用 `agent read` 查模型回复行（只读，不发 prompt）。**timeout ≠ 未送达**：超时只说明调用方不再等，任务可能仍在执行；若 `agent wait` 失败，后续读取与 nonce 判定均标为未验证，不得宣称已稳定或已处理：

```bash
if herdr agent wait "$DEV" --timeout 180000; then
  herdr agent read "$DEV" --source recent-unwrapped --lines 60 > "$EVIDENCE/phaseA/21-a2-read.txt" || abort "A2 终端读取失败"
  NONCE_COUNT=$(awk -v nonce="ITEST_PONG_${RUNTOKEN}_A2" '/^⏺ / && index($0, nonce) {n++} END {print n+0}' "$EVIDENCE/phaseA/21-a2-read.txt") || abort "读取 nonce 证据失败"
  if [ "$NONCE_COUNT" -gt 0 ]; then
    printf '%s\n' 'NONCE-A2: HIT（模型回复行命中；timeout 不代表未送达）' >> "$EVIDENCE/phaseA/16-a2-result.txt" || abort "写入证据失败"
  else
    printf '%s\n' 'NONCE-A2: MISS（不能据此断言未送达，只记录）' >> "$EVIDENCE/phaseA/16-a2-result.txt" || abort "写入证据失败"
  fi
else
  printf 'NOT-OBSERVED: A2 后 agent wait 失败；后续读取与 nonce 判定未验证\n' >> "$EVIDENCE/phaseA/16-a2-result.txt" || abort "写入证据失败"
fi
herdr agent get "$DEV" > "$EVIDENCE/phaseA/22-after-timeout.json" || abort "A2 后状态读取失败"
jq -r '.result.agent | [.name, .agent_status, .revision, .state_change_seq] | @tsv' "$EVIDENCE/phaseA/22-after-timeout.json" \
  > "$EVIDENCE/phaseA/22-after-timeout.tsv" || abort "A2 后状态解析失败"
```

观察值为 `blocked` 时只做记录（见 §5.12 的被动观察规则），不判失败，也不代答。

### 4.5 阶段 A 证据

`$EVIDENCE/phaseA/` 至少包含：`00-probe-agent.json`、`10/11/12/13/14-*`、`15-a1-read.txt`、`16-a2-result.txt`、`20-prompt-timeout.{json,err}`；wait 成功时还包括 `21-a2-read.txt`，状态读取成功时包括 `22-after-timeout.tsv`。

### 4.6 阶段 A Go/No-Go

| 项 | Go 条件 |
|---|---|
| A1 nonce | prompt 成功返回，且本轮 nonce 在以 `⏺` 开头的模型回复行命中 |
| A1 状态解析 | `agent_status_of(doc["result"])` 返回 `idle` 或 `done`（不得直接喂未解包的 `doc`，见 §3.8） |
| A1 返回体结构 | 合法 JSON，key 和类型已记录 |

仅上述三项决定 Go/No-Go；A2 未触发 timeout 不判 No-Go。阶段 A 所有必要断言通过后才写 GO；失败写 `NO-GO: <原因>` 并保留现场、不清理，等待负责人判定。文件缺失或写入失败也按 No-Go 处理；重新执行前必须使用新的 `RUNTOKEN` 和证据目录。

```bash
GO_FILE="$EVIDENCE/phaseA/GO-NOGO.txt"
# 只有在前面所有必要 abort 断言都通过后才会执行到这里
[ ! -e "$GO_FILE" ] && [ ! -L "$GO_FILE" ] || abort "门槛文件已存在，拒绝覆盖"
(set -C; printf 'GO\n' > "$GO_FILE") || abort "写入 GO 文件失败或文件冲突"
grep -qx GO "$GO_FILE" || abort "阶段 A 未 GO"
```

### 4.7 进入阶段 B 的唯一条件

`grep -qx 'GO' "$EVIDENCE/phaseA/GO-NOGO.txt" || abort "阶段 A 未 GO，禁止进入阶段 B"`。这是硬性前置：A 的 `GO-NOGO.txt` 必须已在进入 B 之前生成；缺失即禁止进入 B，且不得在 B 中补写 A 的门槛文件。

---

## 5. 阶段 B：HTM 调度、结果、异常验证

阶段 B 只对 `developer` 派单；`reviewer` 由 `init` 创建但**不发 prompt**。team 已在 §3.5 注册（同一 team，`max_retries=5`，`task_timeout_seconds=60`）。

超时参数分工（全文统一）：

| 参数 | 位置 | 取值 | 作用 |
|---|---|---|---|
| A2 直接 CLI | `herdr agent prompt ... --timeout 15000`（§4.4） | `15000` ms | 调用方等待 15s |
| B8 htm | `init --set orchestration.task_timeout_seconds=60`（§3.5），htm 生成 `--timeout 60000` | `60000` ms | Herdr 等待 60s；htm 的 `herdr()` 子进程上限为 `60000/1000+15 = 75` 秒（`scripts/htm.py:781-786`） |

CLI timeout 时 stderr 为 `{"error":{"code":"timeout","message":"timed out waiting for agent status"}}`，exit=1。**timeout ≠ 未送达**：等待成功后可用 `herdr agent read` 检查模型回复行中的本轮 nonce；未命中不能证明未送达。

### 5.1 B0 前置复核（team 已由 §3.5 注册）

进入阶段 B 前确认 A 的 GO；每个执行 B 断言的 shell 均须设置 B 的 `GO_FILE`，失败时 `abort` 写入 NO-GO、保留现场并等待负责人判定。

```bash
GO_FILE="$EVIDENCE/phaseB/GO-NOGO.txt"
[ ! -e "$GO_FILE" ] && [ ! -L "$GO_FILE" ] || abort "阶段 B 门槛文件已存在，必须使用新证据目录"
grep -qx GO "$EVIDENCE/phaseA/GO-NOGO.txt" || abort "阶段 A 未 GO，禁止进入阶段 B"
[ -n "${DEV:-}" ] && [ -n "${DEV_PANE:-}" ] && [ -n "${REV:-}" ] || abort "§3.5 未完成"
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
# run start 返回后立即固化 run 文件 birth time；后续不得重新采集或覆盖
RUN_JSON=$(htm run start --goal '阶段 B：验证 dispatch/collect、账本状态机与异常降级，不改任何仓库') || abort "run start 失败"
_tmp=$(jq -er '.run_id' <<< "$RUN_JSON") || abort "取不到 run_id"
export RUN="$_tmp"
printf '%s' "$RUN" | grep -Eq '^[a-f0-9]{12}$' || abort "run_id 形状非法"
export RUN_FILE="$HTM_STATE_DIR/runs/$RUN.json"
[ -f "$RUN_FILE" ] || abort "run 文件不存在: $RUN_FILE"
STAT_ERR="$EVIDENCE/phaseB/00-run-birth.err"
: > "$STAT_ERR" || abort "无法创建 stat 诊断文件"
RUN_CREATED_BIRTH=$(stat -f '%B' "$RUN_FILE" 2>> "$STAT_ERR") || abort "读取 run birth time 失败（见 $STAT_ERR）"
[ -f "$STAT_ERR" ] && [ -r "$STAT_ERR" ] || abort "stat 诊断证据不可读: $STAT_ERR"
printf '%s' "$RUN_CREATED_BIRTH" | grep -Eq '^[0-9]+$' || abort "run birth time 非法"
[ "$RUN_CREATED_BIRTH" -gt 0 ] || abort "文件系统不支持 birth time（返回 0），不得用 mtime 冒充"
[ ! -e "$EVIDENCE/phaseB/00-run-created-birth.txt" ] || abort "run birth time 已存在，不得覆盖"
printf '%s\n' "$RUN_CREATED_BIRTH" > "$EVIDENCE/phaseB/00-run-created-birth.txt" || abort "记录 run birth time 失败"
chmod 0444 "$EVIDENCE/phaseB/00-run-created-birth.txt" || abort "run 时间证据置为只读失败"
assert_single_active_run "$ITEST_WS" "$ITEST_ROOT_PANE"

htm run stage --run-id "$RUN" > "$EVIDENCE/phaseB/03-stage-before.json" || abort "读取首阶段失败"
jq -e '.stage=="plan_brief"' "$EVIDENCE/phaseB/03-stage-before.json" >/dev/null || abort "首阶段不是 plan_brief"
htm run gate --run-id "$RUN" --decision pass \
  --evidence 'plan_brief：本轮仅验证派单与回收链路，验收标准见 docs/integration-test-plan.md' \
  > "$EVIDENCE/phaseB/03-gate-plan.json" || abort "plan_brief gate 失败"
htm run stage --run-id "$RUN" > "$EVIDENCE/phaseB/03-stage-after.json" || abort "读取推进后阶段失败"
jq -e '.stage=="implement" and .actor=="developer"' "$EVIDENCE/phaseB/03-stage-after.json" >/dev/null \
  || abort "未推进到 implement/developer"
```

`stat` 成功时 stderr 允许为空；非零退出即 abort 并保留诊断文件，成功后仍须核验诊断证据可读。

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
jq -r 'keys[]' "$D1_OUT" > "$EVIDENCE/phaseB/11-dispatch-keys.txt" || abort "记录 key 失败"
jq -e 'keys|sort == ["dispatch_id","next","status"]' "$D1_OUT" >/dev/null || abort "派单响应 key 集合不符"
jq -e '(.dispatch_id | type=="string" and test("^[a-f0-9]{10}$")) and .status=="submitted_unsettled" and (.next | type=="string" and length>0)' "$D1_OUT" >/dev/null || abort "派单响应字段值不符"
```

从**账本**取真实 id（不从响应之外的任何推断），并断言与响应一致：

```bash
_tmp=$(jq -er '.dispatches[-1].id' "$RUN_FILE") || abort "账本无 dispatch"
export D1="$_tmp"
printf '%s' "$D1" | grep -Eq '^[a-f0-9]{10}$' || abort "D1 形状非法: $D1"
D1_RESPONSE=$(jq -er '.dispatch_id' "$D1_OUT") || abort "读取响应 dispatch_id 失败"
[ "$D1_RESPONSE" = "$D1" ] || abort "响应 dispatch_id 与账本不一致"
D1_STATUS=$(jq -er '.status' "$D1_OUT") || abort "读取响应状态失败"
[ "$D1_STATUS" = "submitted_unsettled" ] || abort "no-wait 响应状态不是 submitted_unsettled"
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

说明：`collect` 不等待；若 worker 仍 `working`，会得到 `uncertain` + `Unexpected agent status: working`。这属于设计的 fail-closed；遇到时立即 abort 并保留现场，不得继续。

### 5.6 B5 结算后再派单必须产生新 id（第 2 次 htm prompt）

```bash
# dispatch 要求 worker 处于 idle/done；上一轮刚结束先等稳定
herdr agent wait "$DEV" --timeout 120000 || abort "D2 前等待 developer 失败"
herdr agent get "$DEV" > "$EVIDENCE/phaseB/19-agent.json" || abort "D2 前读取 developer 失败"
jq -e '.result.agent.agent_status|IN("idle","done")' "$EVIDENCE/phaseB/19-agent.json" >/dev/null \
  || abort "D2 前 developer 不是 idle/done"

D2_OUT="$EVIDENCE/phaseB/20-dispatch2.json"
rc=0
htm run dispatch --run-id "$RUN" --role developer --effect read \
  --message '只回复 ITEST_D2_OK；写一份新的 JSON 报告。' \
  > "$D2_OUT" || rc=$?
[ "$rc" -eq 0 ] || abort "第二次派单失败 rc=$rc"
_tmp=$(jq -er '.dispatch_id' "$D2_OUT") || abort "取不到 D2"
export D2="$_tmp"
printf '%s' "$D2" | grep -Eq '^[a-f0-9]{10}$' || abort "D2 形状非法"
[ "$D2" != "$D1" ] || abort "D2 与 D1 相同，历史被覆盖"
jq -e --arg d1 "$D1" --arg d2 "$D2" \
  '([.dispatches[]|select(.id==$d1)]|length)==1 and ([.dispatches[]|select(.id==$d2)]|length)==1' \
  "$RUN_FILE" >/dev/null || abort "账本未同时保留 D1/D2"
```

### 5.7 B6 报告归属（正例；run_id 只从 run list / run 文件取）

```bash
htm run list > "$EVIDENCE/phaseB/30-runlist.json" || abort "run list 失败"
_tmp=$(jq -er --arg r "$RUN" '.runs[]|select(.run_id==$r)|.run_id' "$EVIDENCE/phaseB/30-runlist.json") \
  || abort "run list 中找不到本 run"
export RUN_FROM_LIST="$_tmp"
[ "$RUN_FROM_LIST" = "$RUN" ] || abort "run list 的 run_id 与本地不一致"
jq -r --arg d "$D2" '.dispatches[]|select(.id==$d)
  | {id, role, stage_index, stage_attempt, status, artifact_sha256, result_file,
     trace_run: .report.run_id, trace_dispatch: .report.dispatch_id}' \
  "$RUN_FILE" > "$EVIDENCE/phaseB/31-report-ownership.json" || abort "读取报告归属失败"
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
  printf 'workspace=%s lead_pane=%s\n' "$ITEST_WS" "$ITEST_ROOT_PANE" || abort "写入 run 身份失败"
  for f in "$HTM_STATE_DIR"/runs/*.json; do
    [ -e "$f" ] || continue
    jq -r --arg ws "$ITEST_WS" --arg pane "$ITEST_ROOT_PANE" '
      select(.context.workspace==$ws and .context.lead_pane==$pane)
      | "RUN=\(.id) status=\(.status)"' "$f" || abort "读取账本失败: $f"
  done
} > "$EVIDENCE/phaseB/32-active-runs.txt" || abort "写入 run 证据失败"
count=$(awk '/status=active/ {n++} END {print n+0}' "$EVIDENCE/phaseB/32-active-runs.txt") || abort "统计 active run 失败"
[ "$count" -eq 1 ] || abort "该 pane 的 active run 数不等于 1"
```

在同一 team 上对同一 developer 再开 run 会被 `run_start` 拒绝（`Worker(s) reserved by unfinished Run ...`）。该负例可选，不构造成本。

### 5.9 B8 超时用例（同一 team / `task_timeout_seconds=60`，第 3 次 htm prompt）

`task_timeout_seconds` 在 `init` 时固化为 60（§3.5），事后不能改；为了不注册第二个 team，本用例让 worker `sleep 180`，尝试触发真实的 `timeout`。模型不一定执行 sleep，且其他错误也会走 uncertain 路径，必须核验账本中的 timeout 证据。

**不从失败命令的 stdout 取 dispatch id**（超时失败时 stdout 可能为空）。做法：派单前后快照 `runs/*.json` 集合与目标 run 的派单数，差集必须为空；dispatch id 从目标 run 文件的 `dispatches[-1].id` 读取，并断言唯一。

```bash
export TRUN="$RUN"                       # 同一个 run
export TRUN_FILE="$RUN_FILE"
assert_single_active_run "$ITEST_WS" "$ITEST_ROOT_PANE"

# dispatch 要求 worker idle/done；先等 D2 稳定
herdr agent wait "$DEV" --timeout 180000 || abort "B8 前等待 developer 失败"
herdr agent get "$DEV" > "$EVIDENCE/phaseB/40-agent.json" || abort "B8 前读取 developer 失败"
jq -e '.result.agent.agent_status|IN("idle","done")' "$EVIDENCE/phaseB/40-agent.json" >/dev/null \
  || abort "B8 前 developer 不是 idle/done"

# 派单前快照
ls "$HTM_STATE_DIR"/runs/*.json > "$EVIDENCE/phaseB/41-runs-before.raw" || abort "读取 run 集合失败"
[ -s "$EVIDENCE/phaseB/41-runs-before.raw" ] || abort "run 集合为空"
sort "$EVIDENCE/phaseB/41-runs-before.raw" > "$EVIDENCE/phaseB/41-runs-before.txt" || abort "排序 run 集合失败"
BEFORE_COUNT=$(jq -er '.dispatches|length' "$TRUN_FILE") || abort "读派单数失败"
printf 'run=%s dispatch-count-before=%s\n' "$TRUN" "$BEFORE_COUNT" > "$EVIDENCE/phaseB/41-dispatch-count.txt" || abort "记录派单数失败"

rc=0
htm run dispatch --run-id "$TRUN" --role developer --effect read \
  --message "先执行 sleep 180，完成后回复 ITEST_PONG_${RUNTOKEN}_B8，再按提示路径写入 JSON 报告；不要提前写。" \
  > "$EVIDENCE/phaseB/42-dispatch-timeout.txt" 2>&1 || rc=$?
[ "$rc" -eq 2 ] || abort "超时派单应失败 exit=2，实际 $rc"
grep -Eq 'Dispatch [a-f0-9]{10} uncertain; input might have been sent\. Run collect before retry\.' \
  "$EVIDENCE/phaseB/42-dispatch-timeout.txt" || abort "超时错误文案不符"

# 集合差集：必须为空（失败路径不得创建新 run）
ls "$HTM_STATE_DIR"/runs/*.json > "$EVIDENCE/phaseB/44-runs-after.raw" || abort "读取 run 集合失败"
[ -s "$EVIDENCE/phaseB/44-runs-after.raw" ] || abort "run 集合为空"
sort "$EVIDENCE/phaseB/44-runs-after.raw" > "$EVIDENCE/phaseB/44-runs-after.txt" || abort "排序 run 集合失败"
comm -13 "$EVIDENCE/phaseB/41-runs-before.txt" "$EVIDENCE/phaseB/44-runs-after.txt" \
  > "$EVIDENCE/phaseB/45-runs-new.txt" || abort "计算 run 差集失败"
[ ! -s "$EVIDENCE/phaseB/45-runs-new.txt" ] || abort "派单失败路径意外创建了新 run"
comm -23 "$EVIDENCE/phaseB/41-runs-before.txt" "$EVIDENCE/phaseB/44-runs-after.txt" > "$EVIDENCE/phaseB/45-runs-removed.txt" || abort "计算消失 run 差集失败"
[ ! -s "$EVIDENCE/phaseB/45-runs-removed.txt" ] || abort "派单期间 run 文件消失"

# 从目标 run 文件读取新派单 id，并断言唯一、账本恰好 +1
AFTER_COUNT=$(jq -er '.dispatches|length' "$TRUN_FILE") || abort "读派单数失败"
[ "$AFTER_COUNT" -eq "$((BEFORE_COUNT + 1))" ] || abort "超时派单没有新增恰好 1 条账本记录"
_tmp=$(jq -er '.dispatches[-1].id' "$TRUN_FILE") || abort "timeout run 无 dispatch"
export TD1="$_tmp"
printf '%s' "$TD1" | grep -Eq '^[a-f0-9]{10}$' || abort "TD1 形状非法"
jq -e --arg d "$TD1" '([.dispatches[]|select(.id==$d)]|length)==1' "$TRUN_FILE" >/dev/null \
  || abort "TD1 在账本中不唯一"
jq -e --arg d "$TD1" '.dispatches[]|select(.id==$d)|.status=="submission_uncertain" and (.error|length>0)' \
  "$TRUN_FILE" >/dev/null || abort "派单后账本不是 submission_uncertain 或 error 为空"
jq -er --arg d "$TD1" '.dispatches[]|select(.id==$d)|.error' "$TRUN_FILE" \
  > "$EVIDENCE/phaseB/42-ledger-error.txt" || abort "读取账本 error 失败"
# Command unavailable 是 OSError，可能是可执行文件缺失而非超时
if ! grep -Fq 'Command unavailable' "$EVIDENCE/phaseB/42-ledger-error.txt" && \
   { grep -Eq '"code"[[:space:]]*:[[:space:]]*"timeout"' "$EVIDENCE/phaseB/42-ledger-error.txt" || \
     grep -Fq 'timed out waiting for agent status' "$EVIDENCE/phaseB/42-ledger-error.txt"; }; then
  printf '%s\n' 'VERIFIED: 账本 error 含 timeout 证据' > "$EVIDENCE/phaseB/42-timeout-verification.txt" || abort "写入证据失败"
else
  printf '%s\n' 'NOT-VERIFIED: timeout 未确认（账本仅有其他错误）' > "$EVIDENCE/phaseB/42-timeout-verification.txt" || abort "写入证据失败"
  abort "NOT-VERIFIED: timeout 未确认，不得算 B8 通过"
fi
# fail-closed：确认落盘内容为 VERIFIED，否则后续步骤一律不得执行
grep -qx 'VERIFIED: 账本 error 含 timeout 证据' "$EVIDENCE/phaseB/42-timeout-verification.txt" \
  || abort "timeout 证据未确认为 VERIFIED，禁止继续 B8"
grep -q "$TD1" "$EVIDENCE/phaseB/42-dispatch-timeout.txt" \
  || abort "错误信息里的 dispatch id 与账本不一致"
```

NOT-VERIFIED 分支**必须 `abort`**：不得只 printf 后继续执行重发、收尾或 collect，否则会把未确证的超时当成通过，或在后续写入中覆盖已有证据。

账本 error 可能是 Herdr 自身返回的 `{"code":"timeout",...}` 或 `timed out waiting for agent status`；htm 子进程超时会以 `Command unavailable/timed out` 出现，本判据**保守地**归为 NOT-VERIFIED（fail-closed），不作为 timeout 证据。含 `Command unavailable` 的 OSError 可能并非超时，必须排除。`stalled`、命令错误等其他 `Error` 同样会写入 `submission_uncertain`，但不能证明 timeout，须记为 NOT-VERIFIED 并保留现场。这是有意的保守取舍：宁可将可超时但未确证的情形记为 NOT-VERIFIED，也不把非 timeout 错误误判为通过。

不自动重发：

```bash
rc=0
htm run dispatch --run-id "$TRUN" --role developer --effect read --message '重发探测' \
  > "$EVIDENCE/phaseB/46-retry-timeout.txt" 2>&1 || rc=$?
[ "$rc" -eq 2 ] || abort "超时后重发未被拒绝 exit=$rc"
grep -Eq 'Pending/uncertain dispatch [a-f0-9]{10}; use run collect, never blindly resend' "$EVIDENCE/phaseB/46-retry-timeout.txt" \
  || abort "超时后重发拒绝原因不符"
```

收尾（collect 会把 `submission_uncertain` 改写为 settled/blocked/uncertain，属正常推进）。**`agent wait` 失败时必须 fail-closed**：不得继续读取、不得判定 nonce、不得用新的 collect 结果覆盖已有证据；一律记为未验证并 abort。仅匹配以 `⏺` 开头的模型回复行，排除以 `❯` 开头的输入回显：

```bash
if ! herdr agent wait "$DEV" --timeout 600000; then
  if [ ! -e "$EVIDENCE/phaseB/43-b8-nonce.txt" ]; then
    printf '%s\n' 'B8-NONCE: NOT-VERIFIED（agent wait 失败，未读取终端，未做 nonce 判定）' \
      > "$EVIDENCE/phaseB/43-b8-nonce.txt" || exit 1
  fi
  abort "developer 等待失败：后续读取、nonce 判定与 collect 一律未验证，不得继续"
fi
# timeout ≠ 未送达：只记录模型回复行中的 nonce，不凭 MISS 断言未送达
herdr agent read "$DEV" --source recent-unwrapped --lines 80 > "$EVIDENCE/phaseB/43-b8-read.txt" \
  || abort "B8 终端读取失败"
NONCE_COUNT=$(awk -v nonce="ITEST_PONG_${RUNTOKEN}_B8" '/^⏺ / && index($0, nonce) {n++} END {print n+0}' "$EVIDENCE/phaseB/43-b8-read.txt") || abort "读取 nonce 证据失败"
if [ "$NONCE_COUNT" -gt 0 ]; then
  printf '%s\n' 'B8-NONCE: HIT（模型回复行命中；timeout 不代表未送达）' > "$EVIDENCE/phaseB/43-b8-nonce.txt" || abort "写入证据失败"
else
  printf '%s\n' 'B8-NONCE: MISS（仅记录，不据此断言未送达）' > "$EVIDENCE/phaseB/43-b8-nonce.txt" || abort "写入证据失败"
fi
# 已存在的 collect 结果不得被后续写入覆盖；存在即视为现场异常
[ ! -e "$EVIDENCE/phaseB/47-collect-timeout.json" ] \
  || abort "47-collect-timeout.json 已存在，拒绝覆盖既有证据"
htm run collect --run-id "$TRUN" --dispatch-id "$TD1" > "$EVIDENCE/phaseB/47-collect-timeout.json" || abort "timeout collect 失败"
jq -e --arg d "$TD1" '.dispatch_id==$d and (.status|IN("settled","blocked","uncertain"))' \
  "$EVIDENCE/phaseB/47-collect-timeout.json" >/dev/null || abort "timeout collect 返回异常"
```

若 `agent wait` 失败，`43-b8-read.txt` 不生成、`47-collect-timeout.json` 不生成，B8 只能记未验证/NO-GO，不得补写或改用旧证据宣称通过。

注意：若 worker 执行了 `sleep 180`，在 `--wait` 超时（`task_timeout_seconds=60`）后可能仍在工作；须等待其结束。若它最终写出合法报告，collect 会得到 `settled`，这**不构成**超时降级用例失败——关键断言是账本中的 timeout 证据、`submission_uncertain` 与“不自动重发”。

**timeout ≠ 未送达**：`htm run dispatch` 的 exit=2 也可能来自其他错误，本身不能证明 timeout 或输入没送进 pane；wait 成功后可用 `herdr agent read` 查该轮模型回复行 nonce，命中也不单独证明 sleep 已执行。同理，`herdr agent prompt --timeout` 返回 `{"error":{"code":"timeout",...}}` 时不据此断言未送达。

### 5.10 B9 Mock：错误报告注入（独立 state 副本，**Mock**）

不碰真实 worker 正在使用的文件。复制一份 state，在副本里伪造账本 + 伪造工件，`HTM_STATE_DIR` 指向副本。本地 Mock 原始证据保留文件名与原始内容，统一由同目录 `50-mock-collect.meta.txt` 首行 `MOCK` 标记（见 §7）；清单列出原始名，交付副本时各文件名加 `mock-` 前缀；JSON 不插入标记行。

```bash
export MOCK="$ITEST_ROOT/mock"
rm -rf "$MOCK" || abort "无法清理旧 mock 目录"
mkdir -p "$MOCK" || abort "无法创建 mock 目录"
cp -R "$HTM_STATE_DIR" "$MOCK/state" || abort "复制 state 失败"
export MOCK_STATE="$MOCK/state"
MOCK_DIR=$(cd "$MOCK_STATE" && pwd) || abort "读取 Mock 路径失败"
[ "$MOCK_DIR" != "$HTM_STATE_DIR" ] || abort "Mock state 与真实 state 相同"
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
MOCK_ID=$(jq -er '.run_id' "$MOCK_STATE/artifacts/$MOCK_RUN/$MOCK_DID.json") || abort "读取 mock run_id 失败"
[ "$MOCK_ID" = "000000000000" ] \
  || abort "mock 工件 run_id 未写错"
```

在**主 Lead pane 的 shell**（与真实 run 同一 `context`）里执行 collect，只把 `HTM_STATE_DIR` 换成副本。先确保 developer 处于 `idle`/`done`，否则 `reconcile_result` 会先以 `Unexpected agent status` 拒绝，测不到身份校验：

```bash
herdr agent wait "$DEV" --timeout 240000 || abort "Mock 前等待 developer 失败"
herdr agent get "$DEV" > "$EVIDENCE/phaseB/49-agent.json" || abort "Mock 前读取 developer 失败"
jq -e '.result.agent.agent_status|IN("idle","done")' "$EVIDENCE/phaseB/49-agent.json" >/dev/null \
  || abort "Mock 前 developer 不是 idle/done"

SHA_OUTPUT=$(shasum -a 256 "$RUN_FILE") || abort "真实账本哈希失败"
REAL_RUN_SHA_BEFORE=${SHA_OUTPUT%% *}
printf '%s\n' "$REAL_RUN_SHA_BEFORE" | grep -Eq '^[a-f0-9]{64}$' || abort "真实账本哈希格式非法"
printf 'MOCK\n50-mock-collect.json\n50-mock-collect.err\n51-mock-verdict.txt\n61-mock-role.txt\n' \
  > "$EVIDENCE/phaseB/50-mock-collect.meta.txt" || abort "写入 Mock 标记失败"
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
SHA_OUTPUT=$(shasum -a 256 "$RUN_FILE") || abort "真实账本哈希失败"
REAL_RUN_SHA_AFTER=${SHA_OUTPUT%% *}
printf '%s\n' "$REAL_RUN_SHA_AFTER" | grep -Eq '^[a-f0-9]{64}$' || abort "真实账本哈希格式非法"
[ "$REAL_RUN_SHA_BEFORE" = "$REAL_RUN_SHA_AFTER" ] || abort "真实账本被 Mock 测试改动"
[ ! -e "$HTM_STATE_DIR/artifacts/$MOCK_RUN/$MOCK_DID.json" ] \
  || abort "Mock 工件泄漏到真实 state"
echo "MOCK PASS: 身份不符 -> status=uncertain, error=Result identity mismatch: run_id" \
  > "$EVIDENCE/phaseB/51-mock-verdict.txt" || abort "写入 Mock 结论失败"
echo "REAL UNTOUCHED: ledger sha $REAL_RUN_SHA_BEFORE" >> "$EVIDENCE/phaseB/51-mock-verdict.txt" || abort "写入 Mock 隔离证据失败"
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

role→pane 篡改**只在 Mock 副本**里做（复制 `$HTM_STATE_DIR` 后修改 `.roles[].pane_id` 为 `w1:p5`），报错中的 pane 是 agent **实际观察到的 pane**，不是篡改后的期望值 `w1:p5`；断言前缀 `is live in unexpected pane` 与非空 pane id，不断言具体 id。`w1:p5` 仅读取元数据，不发送任何输入。本地 `61-mock-role.txt` 的 Mock 标记由 §5.10 的同目录元信息提供。

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
grep -Eq 'is live in unexpected pane [^[:space:];]+; refusing reuse' "$EVIDENCE/phaseB/61-mock-role.txt" \
  || abort "错误 pane 映射报错文案不符"
SHA_OUTPUT=$(shasum -a 256 "$RUN_FILE") || abort "真实账本哈希失败"
SHA=${SHA_OUTPUT%% *}
printf '%s\n' "$SHA" | grep -Eq '^[a-f0-9]{64}$' || abort "真实账本哈希格式非法"
printf '%s\n' "$REAL_RUN_SHA_AFTER" | grep -Eq '^[a-f0-9]{64}$' || abort "原真实账本哈希格式非法"
[ "$SHA" = "$REAL_RUN_SHA_AFTER" ] || abort "真实账本被 role 篡改测试改动"
```

### 5.12 B11 Blocked：只做被动观察

**不主动请求任何需要审批、敏感或危险的操作来构造 blocked。** 每次 collect / dispatch 前后只读观察：

```bash
BLOCK_OBS="$EVIDENCE/phaseB/70-blocked-observation.txt"
herdr agent get "$DEV" > "$EVIDENCE/phaseB/70-developer.json" || abort "读取 developer 失败"
herdr agent get "$REV" > "$EVIDENCE/phaseB/70-reviewer.json" || abort "读取 reviewer 失败"
DEV_STATUS=$(jq -er '.result.agent.agent_status | select(type=="string" and IN("idle","done","working","blocked"))' "$EVIDENCE/phaseB/70-developer.json") || abort "developer 状态非法"
REV_STATUS=$(jq -er '.result.agent.agent_status | select(type=="string" and IN("idle","done","working","blocked"))' "$EVIDENCE/phaseB/70-reviewer.json") || abort "reviewer 状态非法"
{
  printf 'developer %s %s\n' "$DEV" "$DEV_STATUS" || abort "写入状态失败"
  printf 'reviewer %s %s\n' "$REV" "$REV_STATUS" || abort "写入状态失败"
} > "$BLOCK_OBS" || abort "读取 agent 状态失败"
BLOCK_COUNT=$(awk '$3=="blocked" {n++} END {print n+0}' "$BLOCK_OBS") || abort "读取 blocked 观察失败"
if [ "$BLOCK_COUNT" -gt 0 ]; then
  echo "OBSERVED: blocked（不代答；由人在 Herdr 中处理）" >> "$BLOCK_OBS" || abort "写入 blocked 观察失败"
else
  echo "NOT-OBSERVED: 未验证" >> "$BLOCK_OBS" || abort "写入 blocked 观察失败"
fi
```

规则：

- 观察到 blocked 才记录；未发生必须标「未验证」，不得伪造。
- 禁止 `send-keys`、禁止再发 prompt 代答弹窗；htm 不会自动应答。
- 若 Run 因 blocked 需要挂起，用 `htm run gate --run-id "$RUN" --decision blocked --evidence 'worker 停在审批弹窗，需人工确认，本轮不代答'`（evidence ≥12 字符），由人执行。
- 统计“本测试对 blocked agent 的 send-keys/prompt 次数”恒为 0。

### 5.13 B12 隔离复核

B12 的比对基准**只有一个**：§3.3 在任何测试资源创建之前采集的 `00-agents-before.tsv`。先校验基线只读、校验和未变、mtime ≤ 采集时刻，且采集时刻 ≤ run 文件的 birth time，再比对末态；不得改用末段快照，也不得重采“before”。`stat -f %B` 返回 0 必须 abort；秒级时间只是时序旁证，同秒无法证明先后，也不能独立证明基线早于 workspace 创建。

```bash
BASE="$EVIDENCE/phaseB"
# 1) 基线文件本身必须存在、只读、未被改写
[ -f "$BASE/00-agents-before.tsv" ] || abort "缺少 §3.3 早期基线 00-agents-before.tsv"
[ ! -w "$BASE/00-agents-before.tsv" ] || abort "基线 00-agents-before.tsv 不是只读，可能被改写"
[ -f "$BASE/00-baseline-meta.txt" ] && [ ! -w "$BASE/00-baseline-meta.txt" ] || abort "基线元信息缺失或非只读"
SHA_OUTPUT=$(shasum -a 256 "$BASE/00-agents-before.tsv") || abort "计算基线校验和失败"
SHA=${SHA_OUTPUT%% *}
META_SHA=$(awk -F= '$1=="agents_sha256"{print $2}' "$BASE/00-baseline-meta.txt") || abort "读取元信息校验和失败"
printf '%s\n' "$SHA" | grep -Eq '^[a-f0-9]{64}$' || abort "基线校验和格式非法"
printf '%s\n' "$META_SHA" | grep -Eq '^[a-f0-9]{64}$' || abort "元信息校验和格式非法"
[ "$SHA" = "$META_SHA" ] || abort "基线校验和与元信息不符"
BASE_EPOCH=$(awk -F= '$1=="epoch"{print $2}' "$BASE/00-baseline-meta.txt") || abort "读取基线时间戳失败"
printf '%s' "$BASE_EPOCH" | grep -Eq '^[0-9]+$' || abort "基线时间戳非法: ${BASE_EPOCH:-<empty>}"

# 2) 校验固定时间锚点、基线 mtime 和 run birth time
[ -f "$BASE/00-baseline-epoch.txt" ] && [ ! -w "$BASE/00-baseline-epoch.txt" ] || abort "时间锚点缺失或非只读"
EPOCH_FILE=$(cat "$BASE/00-baseline-epoch.txt") || abort "读取时间锚点失败"
[ "$EPOCH_FILE" = "$BASE_EPOCH" ] || abort "时间锚点($EPOCH_FILE) 与元信息($BASE_EPOCH) 不一致"
BASE_MTIME_REC=$(awk -F= '$1=="agents_mtime"{print $2}' "$BASE/00-baseline-meta.txt") \
  || abort "读取基线记录的 mtime 失败"
printf '%s' "$BASE_MTIME_REC" | grep -Eq '^[0-9]+$' || abort "基线记录的 mtime 非法"
BASE_MTIME=$(stat -f '%m' "$BASE/00-agents-before.tsv") || abort "读取基线 mtime 失败"
[ "$BASE_MTIME" -eq "$BASE_MTIME_REC" ] \
  || abort "基线 mtime($BASE_MTIME) 与采集时记录($BASE_MTIME_REC) 不符，基线被改写"
[ "$BASE_MTIME" -le "$BASE_EPOCH" ] \
  || abort "基线 mtime($BASE_MTIME) 晚于采集时刻($BASE_EPOCH)，不是不可变早期基线"
[ -f "$BASE/00-run-created-birth.txt" ] && [ ! -w "$BASE/00-run-created-birth.txt" ] || abort "run 创建后时间证据缺失或非只读"
RUN_CREATED_BIRTH=$(cat "$BASE/00-run-created-birth.txt") || abort "读取 run birth time 失败"
printf '%s\n' "$RUN_CREATED_BIRTH" | grep -Eq '^[0-9]+$' || abort "run birth time 非法"
[ "$RUN_CREATED_BIRTH" -gt 0 ] || abort "文件系统不支持 birth time（返回 0）"
[ "$BASE_EPOCH" -le "$RUN_CREATED_BIRTH" ] \
  || abort "基线 epoch($BASE_EPOCH) 晚于 run birth time($RUN_CREATED_BIRTH)"
printf 'baseline_mtime=%s baseline_epoch=%s run_created_birth=%s\n' \
  "$BASE_MTIME" "$BASE_EPOCH" "$RUN_CREATED_BIRTH" > "$BASE/00-baseline-timing.txt" \
  || abort "记录基线时序失败"

# 3) 采集末态并与早期基线比对
herdr agent list > "$EVIDENCE/phaseB/80-agents-after.json" || abort "agent list 失败"
jq -r '.result.agents[] | [.workspace_id, .pane_id, (.name // "-"), (.agent_status // "-"), (.revision|tostring), (.state_change_seq|tostring)] | @tsv' \
  "$EVIDENCE/phaseB/80-agents-after.json" > "$BASE/80-agents-after.raw" || abort "末态 agent jq 失败"
[ -s "$BASE/80-agents-after.raw" ] || abort "末态 agent 列表为空"
sort "$BASE/80-agents-after.raw" > "$BASE/80-agents-after.tsv" || abort "末态 agent sort 失败"

PROTECTED='w1:p5 w1:p6 w2:p2 w2:p3'
pick() { awk -v list="$PROTECTED" '
  BEGIN { n=split(list,a," "); for(i=1;i<=n;i++) keep[a[i]]=1 }
  keep[$2] { print $2 "\t" $5 "\t" $6 }' "$1"; }
check_pick() {
  count=$(wc -l < "$1") || abort "统计受保护 pane 失败"
  [ "$count" -eq 4 ] || abort "受保护 pane 行数不是 4: $1 ($count)"
  for p in $PROTECTED; do
    count=$(awk -v p="$p" '$1==p {n++} END {print n+0}' "$1") || abort "统计 $p 失败"
    [ "$count" -eq 1 ] || abort "受保护 pane $p 出现 $count 次: $1"
  done
}
pick "$BASE/00-agents-before.tsv" > "$BASE/81-protected-before.raw" || abort "基线提取失败"
pick "$BASE/80-agents-after.tsv" > "$BASE/82-protected-after.raw" || abort "复核提取失败"
check_pick "$BASE/81-protected-before.raw"
check_pick "$BASE/82-protected-after.raw"
sort "$BASE/81-protected-before.raw" > "$BASE/81-protected-before.tsv" || abort "基线排序失败"
sort "$BASE/82-protected-after.raw" > "$BASE/82-protected-after.tsv" || abort "复核排序失败"
diff -u "$EVIDENCE/phaseB/81-protected-before.tsv" "$EVIDENCE/phaseB/82-protected-after.tsv" \
  > "$EVIDENCE/phaseB/83-protected-diff.txt" || abort "受保护 worker 的 revision/seq 发生变化（见 83-protected-diff.txt）"
awk '
  $2=="w1:p1" || $2=="w2:p1" {
    key=$2; if (FNR==NR) { before[key]=$0; nb[key]++ } else { after[key]=$0; na[key]++ }
  }
  END {
    split("w1:p1 w2:p1", leads, " ")
    for (i=1; i<=2; i++) {
      p=leads[i]; if (nb[p]!=1 || na[p]!=1) { print "Lead 行缺失或重复: " p > "/dev/stderr"; exit 1 }
      print "before: " before[p]; print "after:  " after[p]
      print (before[p]==after[p] ? "unchanged" : "drift")
    }
  }' "$BASE/00-agents-before.tsv" "$BASE/80-agents-after.tsv" \
  > "$BASE/85-lead-drift.txt" || abort "提取 Lead 基线与末态失败"
```

受保护 worker（`w1:p5`、`w1:p6`、`w2:p2`、`w2:p3`）的 `revision` 与 `state_change_seq` 必须与 §3.3 的早期基线逐字相同；`w1:p1`、`w2:p1` 为 Lead，允许自身工作造成漂移。执行者须人工核查 `85-lead-drift.txt` 和相关只读证据，记录各 Lead 的漂移原因（无漂移也须说明），核查是否有本测试输入痕迹。发现痕迹或无法确认时必须 abort；不得凭元数据差异自行推断无痕迹。**基线有效性断言未通过时，B12 只能记未验证/NO-GO，不得用末段快照补位后宣称“全程不变”。**

先生成核查模板，由执行者人工填写三个必填字段：`drift_cause`、`test-input-trace`（只能填 `yes` 或 `no`）、`reviewer`。填写并保存后执行下方断言；未填写、含糊或无法确认均不得继续。

```bash
LEAD_REVIEW="$BASE/85-lead-drift-review.txt"
[ ! -e "$LEAD_REVIEW" ] && [ ! -L "$LEAD_REVIEW" ] || abort "Lead 核查文件已存在，拒绝覆盖"
printf 'drift_cause=\ntest-input-trace=yes|no\nreviewer=\n' > "$LEAD_REVIEW" || abort "创建 Lead 核查模板失败"
```

人工完成核查并填写保存后，执行：

```bash
LEAD_REVIEW="$EVIDENCE/phaseB/85-lead-drift-review.txt"
[ -f "$LEAD_REVIEW" ] && [ -r "$LEAD_REVIEW" ] || abort "Lead 核查证据缺失或不可读"
awk -F= '
  $1=="drift_cause" { cause++; if ($0 !~ /^drift_cause=[[:space:]]*$/) cause_ok=1 }
  $1=="test-input-trace" { trace++; if ($0=="test-input-trace=no") trace_ok=1 }
  $1=="reviewer" { reviewer++; if ($0 !~ /^reviewer=[[:space:]]*$/) reviewer_ok=1 }
  END { exit !(cause==1 && cause_ok && trace==1 && trace_ok && reviewer==1 && reviewer_ok) }
' "$LEAD_REVIEW" || abort "Lead 核查未完成或发现测试输入痕迹（见 $LEAD_REVIEW）"
```

默认状态目录隔离（A-16）：`HTM_STATE_DIR` 全程指向临时目录，默认目录不应出现新文件。本检查必须 fail-closed：`find` 前逐级确认目录存在性与访问权限，锚点用 §3.3 固定的 `00-baseline-epoch.txt`（不用会变化的 `env.sh`），`find` 退出码必须检查。

```bash
DEFAULT_STATE="$HOME/.local/state/herdr-team"
BASE_EXPECT=$(cat "$EVIDENCE/phaseB/00-default-state-exists.txt") || abort "读取默认 state 基线失败"
if [ "$BASE_EXPECT" = "present" ]; then
  DEFAULT_PRESENCE=$(default_state_presence) || abort "检查默认 state 失败"
  [ "$DEFAULT_PRESENCE" = present ] || abort "基线记录默认 state 存在，但现在不存在，需人工核查"
  BASE_EPOCH=$(awk -F= '$1=="epoch"{print $2}' "$EVIDENCE/phaseB/00-baseline-meta.txt") \
    || abort "读取基线时间戳失败"
  NEWER_REF="$EVIDENCE/phaseB/00-baseline-epoch.txt"
  test -f "$NEWER_REF" || abort "缺少固定时间锚点: $NEWER_REF"
  # 探测性调用，有意丢弃诊断，不用于断言；实际扫描另行检查退出码
  if find "$DEFAULT_STATE" -maxdepth 0 -newermt '@1700000000' >/dev/null 2>&1; then
    find "$DEFAULT_STATE" -type f -newermt "@$BASE_EPOCH" \
      > "$EVIDENCE/phaseB/86-default-state-new.txt" || abort "默认 state 目录扫描失败"
  else
    find "$DEFAULT_STATE" -type f -exec sh -c 'for f do stat -f "%m %N" "$f" || exit 1; done' sh {} + \
      > "$EVIDENCE/phaseB/86-default-state-stat.txt" || abort "默认 state 扫描或 stat 失败"
    awk -v e="$BASE_EPOCH" '$1 > e' "$EVIDENCE/phaseB/86-default-state-stat.txt" \
      > "$EVIDENCE/phaseB/86-default-state-new.txt" || abort "解析默认 state mtime 失败"
  fi
  [ ! -s "$EVIDENCE/phaseB/86-default-state-new.txt" ] \
    || abort "默认状态目录出现新文件（见 86-default-state-new.txt），需人工核查"
elif [ "$BASE_EXPECT" = "absent" ]; then
  DEFAULT_PRESENCE=$(default_state_presence) || abort "检查默认 state 失败"
  [ "$DEFAULT_PRESENCE" = absent ] || abort "基线记录默认 state 不存在，但现在出现了路径：$DEFAULT_STATE"
  : > "$EVIDENCE/phaseB/86-default-state-new.txt" || abort "写入默认 state 扫描结果失败"
else
  abort "默认 state 基线存在性非法: $BASE_EXPECT"
fi
```

说明：先探测 `find -newermt` 能力，再执行互斥的主路径或逐文件 `stat` fallback；两者均检查退出码，失败即 abort。锚点固定为 §3.3 的整数时间戳。

扫描仅检查结束时仍存在的常规文件，无法发现中途创建后删除、回填旧 mtime 或其他类型的写入；fallback 的整数秒 `>` 也会漏掉同秒文件。因此它只是隔离的必要条件，不是充分证明；不得据此宣称从未发生写入。上述检查不依赖 `set -e` / `pipefail`，基线与校验和的各步均单独检查退出码。

### 5.14 阶段 B Go/No-Go

| 项 | Go 条件 |
|---|---|
| B2（§5.3） | 派单成功，账本 `submitted_unsettled`，响应无 `run_id` |
| B3（§5.4） | 在途重复派单 exit=2，旧记录不变 |
| B4（§5.5） | collect 后 `settled` |
| B5（§5.6） | 新 id ≠ 旧 id，账本两条记录都在 |
| B6（§5.7） | 工件身份/形状全通过，`artifact_sha256` 非空 |
| B7（§5.8） | 同一 pane active run 恰好 1 |
| B8（§5.9） | exit=2、账本 `submission_uncertain` 且 error 含 timeout 错误码或 `timed out waiting for agent status` 证据、重发被拒、run 差集为空、账本恰好 +1；其他错误为 `NOT-VERIFIED: timeout 未确认`，不得通过 |
| B9（§5.10） | Mock 身份不符 → exit=0 且 JSON `uncertain` + `Result identity mismatch: run_id`；真实 state 哈希不变 |
| B10（§5.11） | 错误 run-id exit=2；Mock role 篡改报 `unexpected pane` |
| B11（§5.12） | blocked 未主动构造；观察结果如实标记 |
| B12（§5.13） | 受保护 worker 的 revision/seq 与 §3.3 早期基线 `00-agents-before.tsv` 逐字相同；基线只读且校验和未变、基线 epoch ≤ 固定的 run birth time；`85-lead-drift-review.txt` 存在、原因及核查人已填写且 `test-input-trace=no`；默认 state 扫描以固定时间戳锚点 fail-closed，未发现较新文件（仅必要条件） |

本阶段所有断言全通过后才写 `GO`；任一失败由 `abort` 写 `NO-GO: <原因>` 并退出，保留现场、不清理，等待负责人判定。文件缺失或写入失败也按 No-Go 处理。

**阶段门槛（硬性）**：阶段 A 与阶段 B **各自必须产出** `GO-NOGO.txt`。A 的 `$EVIDENCE/phaseA/GO-NOGO.txt` 必须在进入阶段 B 之前生成并通过 §4.7 的 `grep -qx GO` 检查；B 的 `$EVIDENCE/phaseB/GO-NOGO.txt` 必须在 B12 全部断言之后生成。任一文件缺失即该阶段 No-Go，不得以“测试大体通过”代替门槛文件。

```bash
GO_FILE="$EVIDENCE/phaseB/GO-NOGO.txt"
[ -s "$EVIDENCE/phaseB/42-timeout-verification.txt" ] && [ -s "$EVIDENCE/phaseB/43-b8-nonce.txt" ] || abort "B8 必需证据缺失"
LEAD_REVIEW="$EVIDENCE/phaseB/85-lead-drift-review.txt"
[ -f "$LEAD_REVIEW" ] && [ -r "$LEAD_REVIEW" ] || abort "Lead 核查证据缺失或不可读"
awk -F= '
  $1=="drift_cause" { cause++; if ($0 !~ /^drift_cause=[[:space:]]*$/) cause_ok=1 }
  $1=="test-input-trace" { trace++; if ($0=="test-input-trace=no") trace_ok=1 }
  $1=="reviewer" { reviewer++; if ($0 !~ /^reviewer=[[:space:]]*$/) reviewer_ok=1 }
  END { exit !(cause==1 && cause_ok && trace==1 && trace_ok && reviewer==1 && reviewer_ok) }
' "$LEAD_REVIEW" || abort "Lead 核查未完成或发现测试输入痕迹（见 $LEAD_REVIEW）"
# 只有在前面所有必要 abort 断言都通过后才会执行到这里
[ ! -e "$GO_FILE" ] && [ ! -L "$GO_FILE" ] || abort "门槛文件已存在，拒绝覆盖"
(set -C; printf 'GO\n' > "$GO_FILE") || abort "写入 GO 文件失败或文件冲突"
grep -qx GO "$GO_FILE" || abort "阶段 B 未 GO"
```

---

## 6. 断言规范（16 条）

写法约束：禁止“忽略退出码”的兜底写法（强制成功、丢弃断言命令的 stderr、diff 判失败仍继续）；每条失败必须 abort。§5.13 的能力探测是有意丢弃诊断的例外，不用于断言。身份不符类断言以 **JSON** 判定，退出码可能为 0。

| 编号 | 对象 | 命令（要点） | 通过条件 | 失败诊断 |
|---|---|---|---|---|
| A-01 | 配置 | `htm validate` | exit=0 且 `.valid==true` | 配置或模板解析失败 |
| A-02 | 资源 ID | `need_ws/need_tab/need_pane` + `same_ws` | 非空、非 null、格式匹配、前缀一致 | 创建返回缺字段或跨 workspace |
| A-03 | dispatch id 形状 | `printf %s "$D1" \| grep -Eq '^[a-f0-9]{10}$'` | exit=0 | id 生成异常 |
| A-04 | run 可见性 | `jq -e --arg r "$RUN" '[.runs[]\|select(.run_id==$r)]\|length==1'` | exit=0 | run list 遗漏/重复 |
| A-05 | `--no-wait` 状态 | 账本 `select(.id==$D1).status` | 精确 `submitted_unsettled` | 状态机未落账 |
| A-06 | 在途重复派单 | 第二次 dispatch 捕获 rc 与 combined output | rc=2 且正则 `Pending/uncertain dispatch [a-f0-9]{10}; use run collect, never blindly resend` | 重复投递风险 |
| A-07 | 派单响应无 run_id | `jq -e 'keys\|sort == ["dispatch_id","next","status"]'`，并逐字段校验 | no-wait key 集合相等、id 格式正确、status 正确且 next 为非空字符串；不含 `run_id` | 从响应取 run_id 的写法作废 |
| A-08 | 报告归属 | 账本 `report.run_id==$RUN` 且 `report.dispatch_id==$D2` | exit=0，且 `status==settled` | 工件错配 |
| A-09 | 产物契约 | `jq -e` 校验 run_id/dispatch_id/role/stage/attempt/status/verdict/summary/evidence/tests | exit=0 | 工件形状不合规 |
| A-10 | 错位工件（Mock） | `HTM_STATE_DIR=$MOCK_STATE htm run collect` 后读 stdout JSON | exit=0 **且** `.status=="uncertain"` 且 `.error` 以 `Result identity mismatch: run_id` 开头 | 归属校验失效 |
| A-11 | 超时账本 | 校验 `submission_uncertain` 与非空 error，另按 §5.9 匹配 timeout 证据 | 两项均通过 | 状态错误或 timeout 未确认 |
| A-12 | 超时不重发 | 超时后立即 dispatch | rc=2 且含 `never blindly resend` | 重复投递风险 |
| A-13 | 二次派单唯一性 | `test "$D2" != "$D1"` + 账本两条各 1 | exit=0 | 历史被覆盖 |
| A-14 | 单 pane 单 Run | `assert_single_active_run "$ITEST_WS" "$ITEST_ROOT_PANE"` | active run ≤1；B7 另要求 active 恰好 1 | 冲突 Run 并存 |
| A-15 | Mock 隔离 | `$MOCK_STATE` ≠ `$HTM_STATE_DIR`；Mock 后真实账本 sha 不变；真实 state 无 Mock 工件 | 三项均成立 | Mock 污染真实 state |
| A-16 | 状态目录隔离 + 受保护 pane | 默认 state 的 `find`/`stat` 扫描（能力探测分支）退出码为 0 且无较新文件；受保护 worker 行 diff 为空 | 扫描成功且输出为空、四行 protected 各恰好一行 | 越界写或测试泄漏。注意：扫描为空只是**必要条件**，不能证明中途创建后删除或回填 mtime 从未发生（见 §5.13） |

pytest 等价写法（供后续把断言固化为测试时使用；其中 `test_timeout_ledger` 仅验证账本不确定态与 error 中的 timeout 证据，不验证超时成因，不能单独作为 B8 的等价验证）：

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
    # Command unavailable 是 OSError，可能是可执行文件缺失而非超时
    error = rec["error"]
    assert "Command unavailable" not in error, error
    # 优先确认 Herdr 等待超时；兜底收紧为完整文案，排除 stalled 等其他含 timed out 的错误
    assert re.search(r'"code"\s*:\s*"timeout"', error) or "timed out waiting for agent status" in error, error

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
                    21-a2-read.txt（仅 `agent wait` 成功时产生；缺失不等于失败）、22-after-timeout.tsv（**内容非空**表示 `herdr agent get` 成功；空文件/缺失均不等于失败）
$EVIDENCE/phaseB/   00-03 初始化（00-agents-before.tsv 为资源创建前只读基线，00-baseline-*.txt 为其元信息/时间锚，00-run-created-birth.txt 为 run birth time，00-run-birth.err 为 stat 诊断；00b 为 init 前残留 agent 检查）、10-13 D1、20 D2、30-32 归属与互斥、
                    40-47 超时、50-51 Mock（含 50-mock-collect.meta.txt 标记）、60 负例（61-mock-role.txt 为 Mock）、70 blocked、80-86 隔离（含 85-lead-drift.txt 和 85-lead-drift-review.txt）、GO-NOGO.txt
                    43-b8-read.txt（仅 `agent wait` 成功时产生；缺失不等于失败）
```

规则：

- **本地原始证据**保留流程实际落盘的原文件名和内容，可能含终端标题、长文本、对话内容或完整响应；须限制访问，不得直接对外分享。Mock 文件名内含 `mock`（如 `50-mock-collect.json`、`61-mock-role.txt`），同目录 `50-mock-collect.meta.txt` 首行为 `MOCK` 并列出对应文件；不在原始 JSON 等内容前插入标记。
- **对外交付或长期留存的脱敏副本**须先复制到用户另行授权的交付目录：真实响应只保留 **key 名、值的类型、短值**（如 `idle`、`claude`、10 位 id），去掉终端标题、长文本和对话内容；`15-a1-read.txt`、`21-a2-read.txt`、`43-b8-read.txt` 也须脱敏，仅保留验证必需的短值。
- 对外脱敏的账本快照用 `jq '{id,status,stage_index,stage_attempt,dispatches:[.dispatches[]|{id,role,status,error,artifact_sha256}]}'`，不保留完整 prompt；其中 `error` 也须去掉长文本和敏感内容。
- 复制到交付目录时，Mock 证据副本改名加 `mock-` 前缀，与真实测试区分；保留独立元信息的 `MOCK` 首行，不给 JSON 插入非 JSON 首行，并同样去掉长文本和敏感内容。
- 原始证据留在 `$ITEST_ROOT` 内；需长期保留时，在清理前按上述规则复制并脱敏。

---

## 8. 清理与回滚

清理与验收命令必须在一个不属于待关闭测试 workspace 的 pane / 终端里执行（例如原来的 w2:p1 或另一个已存在的 shell）。在控制终端用打印或粘贴的实际路径执行 `set -u; . <path>/env.sh || exit 1` 恢复变量与函数；source 失败即停止，然后确认身份：

```bash
printf '%s\n' "${HERDR_WORKSPACE_ID:-<unset>}"
[ "${HERDR_WORKSPACE_ID:-}" != "$ITEST_WS" ] || abort "清理不能在待关闭的 workspace 内执行"
```

若环境变量缺失（如经 happier 启动的会话），用 `herdr workspace list` 并结合当前焦点及自身 pane 确认不属于 `$ITEST_WS`；无法确认则停止，不得硬编码身份。

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

# 2) 关闭前：受保护 worker 仍与 §3.3 早期基线一致（清理不得在隔离已破坏时继续）
PROTECTED='w1:p5 w1:p6 w2:p2 w2:p3'
pick() { awk -v list="$PROTECTED" '
  BEGIN { n=split(list,a," "); for(i=1;i<=n;i++) keep[a[i]]=1 }
  keep[$2] { print $2 "\t" $5 "\t" $6 }' "$1"; }
check_pick() {
  count=$(wc -l < "$1") || abort "统计受保护 pane 失败"
  [ "$count" -eq 4 ] || abort "受保护 pane 行数不是 4: $1 ($count)"
  for p in $PROTECTED; do
    count=$(awk -v p="$p" '$1==p {n++} END {print n+0}' "$1") || abort "统计 $p 失败"
    [ "$count" -eq 1 ] || abort "受保护 pane $p 出现 $count 次: $1"
  done
}
herdr agent list > "$EVIDENCE/cleanup-agents-current.json" || abort "agent list 失败"
# pick 只接受 TSV（与 00-agents-before.tsv 同形）
jq -r '.result.agents[] | [.workspace_id, .pane_id, (.name // "-"), (.agent_status // "-"), (.revision|tostring), (.state_change_seq|tostring)] | @tsv' \
  "$EVIDENCE/cleanup-agents-current.json" > "$EVIDENCE/cleanup-agents-current.raw" || abort "当前 agent jq 失败"
[ -s "$EVIDENCE/cleanup-agents-current.raw" ] || abort "当前 agent 列表为空"
sort "$EVIDENCE/cleanup-agents-current.raw" > "$EVIDENCE/cleanup-agents-current.tsv" || abort "当前 agent sort 失败"
pick "$EVIDENCE/phaseB/00-agents-before.tsv" > "$EVIDENCE/cleanup-protected-before.raw" || abort "基线提取失败"
pick "$EVIDENCE/cleanup-agents-current.tsv" > "$EVIDENCE/cleanup-protected-current.raw" || abort "当前状态提取失败"
check_pick "$EVIDENCE/cleanup-protected-before.raw"
check_pick "$EVIDENCE/cleanup-protected-current.raw"
sort "$EVIDENCE/cleanup-protected-before.raw" > "$EVIDENCE/cleanup-protected-before.tsv" || abort "基线排序失败"
sort "$EVIDENCE/cleanup-protected-current.raw" > "$EVIDENCE/cleanup-protected-current.tsv" || abort "当前状态排序失败"
diff -u "$EVIDENCE/cleanup-protected-before.tsv" "$EVIDENCE/cleanup-protected-current.tsv" \
  > "$EVIDENCE/cleanup-protected-diff.txt" \
  || abort "受保护 worker 已偏离早期基线，保留现场并人工核查（见 cleanup-protected-diff.txt）"

# 3) 关闭前：测试 workspace 内确有 agent，且都属于本测试
herdr agent list > "$EVIDENCE/cleanup-agents-before.json" || abort "agent list 失败"
jq -e --arg ws "$ITEST_WS" '
  [.result.agents[] | select(.workspace_id==$ws)] as $mine
  | ($mine|length) > 0 and all($mine[]; ((.name // "") | startswith("ht")))
' "$EVIDENCE/cleanup-agents-before.json" >/dev/null || abort "测试 workspace 内存在非本测试 agent"

# 4) 关闭 workspace（其内 pane/agent 一并回收）
herdr workspace close "$ITEST_WS" || abort "workspace close 失败，保留现场"

# 5) workspace 已从列表消失
herdr workspace list > "$EVIDENCE/cleanup-workspaces.json" || abort "workspace list 失败"
jq -e --arg w "$ITEST_WS" '[.result.workspaces[]|select(.workspace_id==$w)]|length==0' \
  "$EVIDENCE/cleanup-workspaces.json" >/dev/null || abort "workspace 关闭后仍出现在列表: $ITEST_WS"

# 6) 测试 worker 已从 agent list 消失
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
| 真实测试 | 阶段 A 的正常返回及超时（若观察到）；阶段 B 的 dispatch、账本状态、collect、报告归属、真实超时降级 | 真实 Herdr + 真实 agent + 真实 htm 调用 |
| Mock 测试 | 身份不符工件注入（`$MOCK/state`）、错误 pane 映射（`$MOCK2/state`） | 隔离 state，不发送 prompt，不写真实工件 |
| 未验证项 | blocked 未自然发生时的行为、`agent_prompt_stalled`、长任务/审批/多文件行为、默认 state 目录下的并发 | 只在出现时记录，否则标「未验证」 |

**口径红线**：本方案只验证「调度通信链路」（dispatch → prompt → collect → 账本与工件校验）。它**不等于「自主监督闭环」验证通过**——不证明 Lead 能自主判断交付质量、不证明审批边界、不证明多团队并发与长期运行稳定性。任何“闭环已验证”的表述都超出证据范围。

**摘要写法**：摘要只能陈述本次实际验证到的内容，不得使用超出证据的绝对表述。禁止写“未发现任何 htm.py 缺陷”“所有断言均已严格实现”“一次重试是常态”这类结论；应写成“本方案未验证 `xxx`”“本次单样本观察到一次重试，不足以推广”“本次未覆盖 `xxx` 路径”。已知缺陷必须如实列入 §10。

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
| `pane_shell_ready` 的 argv0 白名单 | nu/xonsh/elvish/pwsh 不在白名单，可能 fail-closed 并报 `stayed busy after N readiness checks` | 使用支持的 shell，或另行评估白名单 |
| ID 字母表漂移 | 校验依赖 Herdr 的 `PUBLIC_ID_ALPHABET` | Herdr 升级改变字母表时同步更新校验 |
| HTM_STATE_DIR 隔离 | 与默认 state 完全隔离 | 不得据此宣称真实多团队并存已验证 |
| run birth time | `stat -f %B` 返回 0 表示不支持 | 必须 abort，不得以 mtime 冒充 |
| 临时工件清理 | 清理后不可审计 | 需保留时先复制到授权目录 |
| Lead pane 漂移 | 并行任务导致 revision 变化 | 只对 worker 严格断言 |
| 超时阈值 | `task_timeout_seconds` init 时固化（本方案设为 60s） | 用 worker 侧 `sleep 180` 触发真实超时；不改 team 配置；timeout 不等于未送达 |
| Mock 与真实差异 | 副本中的账本/工件是伪造的 | 结论必须标 Mock |
| reviewer 未被验证 | 全程 0 次 prompt | 报告中明确“reviewer 仅验证存在性，未验证其工作链路” |
| `reconcile_result()` 残留旧 error | 已定位但**本轮未修复**：`scripts/htm.py:684-700` 的 `reconcile_result()` 成功时只对新建的 `data` 执行 `data.pop("error", None)`，旧 `rec["error"]` 仍被 `rec.update(...)` 保留，导致已 settled 的 collect 仍返回过期 error（如首次 missing artifact / timeout 后重新 collect 成功仍带原 error） | 仅记录，不改源码；待用户授权后另行修复，并补“先 missing/timeout、后 settled”回归测试。报告中不得据此宣称“未发现任何 htm.py 缺陷” |

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
