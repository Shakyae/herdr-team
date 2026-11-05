# Issue #6 收尾：Herdr 与账本资源清理记录

- 清理日期：2026-11-05
- 范围：Issue #6 开发与验收期间产生的 Herdr Workspace / Pane / 测试 Session、测试
  状态目录、临时目录与 Git Worktree。
- 原则：先保全证据，再清理；无法确认的保留现场。

本文记录**保留了什么、为什么保留**，以及清理后如何恢复现场。原始账本文件不做摘要替换，
保留现场是恢复能力的唯一来源。

---

## 1. 保留：锁死 Run 与事故 Run

| Run | 状态 | 位置 | 保留原因 |
|---|---|---|---|
| `1d0debfb70e0` | `active` | 生产账本 `~/.local/state/herdr-team/` | 用户明确要求不触碰的旧事故 Run。含 uncertain 派单 `661941c28f`（developer），workspace `wW`，审查未完成。 |
| `73d77630456f` | `active` | 生产账本 `~/.local/state/herdr-team/` | `run abandon` 后 Run 锁死的缺陷复现证据。派单 `ea7897bcdf` 为 `abandoned`，`fault_groups` 为空，导致 gate 与 cancel 均拒绝。 |
| `34a8d36b6947` | `active` | `~/.local/state/herdr-team-i6-native/` | `w14` 上 uncertain 派单 `d847ed6e17`（reviewer），是第一轮独立审查因 API 断流的现场。 |
| `ee42ec1a96ce` | `active` | `~/.local/state/herdr-team-i6-review/` | `w16` 上的独立审查 Run，第 4 轮 attempt 无最终报告。其派单工件 `ea2aa4b6fb` 是「API mid-response 断流导致独立复审缺失」的唯一原始证据。 |

这四个 Run 都**不是**成功记录，也不因此清理。它们的账本目录整体保留，不做删减。

## 2. `run abandon` 锁死缺陷（待跟进）

`73d77630456f` 证明了当前实现的缺陷：`run abandon` 把派单置为 `abandoned` 但未建立
fault group，而 `run_gate` / `run_cancel` / `run_finish` 都要求 abandoned 成员必须归属于
已闭合的 fault group，于是该 Run 在缺少 `TRUSTED_CONTROL` 的生产环境下无法走向任何终态。

复现要点：

1. 正常 Run 处于 `active`，某角色派单工件缺失。
2. 执行 `run abandon`，派单进入 `abandoned`。
3. 此后 `run gate`、`run cancel` 均因 `fault_group` 缺失或 `execution_not_fenced` 拒绝。

这与 `docs/issues/issue6-followups.md` 中记录的事项不重复：那个文件记录的是 Happier 侧
身份限制，本缺陷是 Run 生命周期自身的缺陷，需要单独立项。

## 3. 保留：Herdr Workspace 与 Pane

### 保留 `w0`（不做破坏性清理）

`w0` 是本项目的长期开发工作区，共 9 个 Pane。其中 `w0:p4`~`w0:p9` 是 Issue #6 期间
多次 `init` 留下的 Worker Pane，但：

- `w0:p1` 是当前的 Lead Pane，且承载 `73d77630456f` 的现场（该 Run 的 `context`
  指向 `w0:p1`）。关闭 `w0:p1` 会让锁死 Run 的现场不可复现。
- 其余 Worker Pane 均 `idle` 且无活跃派单，但删除它们不会改善任何状态，只会削弱
  `73d77630456f` 的现场完整性。

结论：**`w0` 整体保留**。待 `run abandon` 缺陷修复并能安全结算该 Run 后再清理。

### 保留 `w19`（`herdr-team` 主项目团队）

`w19` 是主项目 `~/dev/herdr-team` 的 dev-lite 团队（Lead `w19:p1`，Developer
`w19:p2`，Reviewer `w19:p3`），与 Issue #6 无关，全部保留。

### 关闭：`w14` / `w16` / `w18` 与 `i6-accept` 会话

见下方「实际执行」一节。

## 4. 保留：Git Worktree

| 目录 | 分支 | 保留原因 |
|---|---|---|
| `~/dev/herdr-team` | `design/issue2-abandon` | 主项目。注意其未跟踪文件 `docs/design/uncertain-dispatch-abandon.md` 是与 `origin/main` 已合并版本**不同**的早期草稿，不删除、不覆盖。 |
| `~/dev/herdr-team-happier` | `fix/happier-herdr-init` | 含**未提交**的 `scripts/htm.py` + `tests/test_regressions_v42.py` 改动（herdr `--session` 显式路由 + 原生 `pane get` 身份校验），以及 `.incident-evidence/issue6-1d0debfb70e0/`（旧事故 Run 的完整现场）。两者都不能丢。 |
| `~/dev/herdr-team-issue2-recovery` | `feat/issue2-uncertain-recovery` | 与 `origin/main` 同步（已由 PR #20 合并），工作区干净，但其存在是 Issue #2 恢复链路的参照。 |

`herdr-team-archive`、`herdr-team-itest`、`htm-issue2-evidence.w2uitn` 都不是 Git
仓库，内容与 Issue #2 / 早期联调相关，本轮不清理。

## 5. 已知未完成

- 独立复审（Reviewer 第二轮）因 API 断流始终没有完整报告。最终验证依据是 Lead 自查
  与原生 Herdr 五阶段回归，**不等于**一次合格的独立评审闭环。
- `run abandon` 锁死缺陷未修复，`73d77630456f` 与 `1d0debfb70e0` 仍无法安全结算。
- `SKILL.md` 的 Happier 支持范围说明未随本次合并发布，待在正式发布时与生产 Skill
  一同更新。

---

## 6. 实际执行（2026-11-05）

### 已关闭

| 资源 | 类型 | 依据 |
|---|---|---|
| `w0:p4` ~ `w0:p9` | Pane（6 个） | 无任何账本引用（team/run 的 roles 均指向 p1–p3），全部 `idle`。关闭后 `w0` 从 9 个 Pane 收敛到 3 个。 |
| `w16` | Workspace | Run `ee42ec1a96ce` 无未决派单；其审查工件已随账本固化到 `docs/evidence/i6-20261105/`。 |
| `w18` | Workspace | Run `ab79b30029ab` 为 `finished`，五阶段 `pass`。 |
| `i6-accept` | Herdr Session | `session stop`（非 delete，保留目录可恢复）。三个 Pane 均为 `done`，唯一使用方是该验收 Run，已 `finished`。 |

### 保留

| 资源 | 原因 |
|---|---|
| `w0`（p1/p2/p3） | `w0:p1` 是锁死 Run `73d77630456f` 的 Lead Pane，`p2`/`p3` 是其角色 Pane。关闭会破坏缺陷现场的可复现性。 |
| `w14` | Run `34a8d36b6947` 仍为 `active`，含 uncertain 派单 `d847ed6e17`（reviewer，API 断流现场）。 |
| `w19` | 主项目 `~/dev/herdr-team` 的 dev-lite 团队，与 Issue #6 无关。 |
| `w12` / `w13` | 其他项目（shipyard-ai-research、pagi），与本次无关。 |
| `w1A` | 本次收尾新建的 dev-lite 团队，正在执行合并前独立审查 Run `4d36bf75eca2`。 |

### 证据固化

四个已完成的验收/回归 Run 账本已复制到 `docs/evidence/i6-20261105/`（字节一致，含五阶段
`pass` 记录）。`~/.local/state/herdr-team-i6-{accept,final,native,review}/` 的原始目录
**全部保留**，因为其中仍含未结束 Run（`34a8d36b6947`、`ee42ec1a96ce`）。

---

## 7. 最终状态（PR #21 合并后）

Issue #6 已通过 PR #21 合并（merge commit `3701732`）并关闭。在干净 main 快照上
验证 `442 passed, 1 skipped, 173 subtests passed`。

### 已清理

- 临时目录：`/tmp/i6-accept-work`、`/tmp/i6-native-work`、`/tmp/htm-review-probe`、
  `/tmp/htm-skill-verify`、`/tmp/htm-upgrade`、`/tmp/i6-detach-result.txt`、
  `htm_{d5,g,head,s,t17}.py`、`probe*.py` 等全部测试脚本与产物。
- Herdr：`w0:p4`~`w0:p9`（6 个无引用 Pane）、`w16`、`w18`、`i6-accept` 会话。

### 保留（附原因）

| 资源 | 原因 |
|---|---|
| `w0`（p1/p2/p3） | 承载锁死 Run `73d77630456f` 的现场。 |
| `w14` | Run `34a8d36b6947` 仍 `active`，含 uncertain 派单 `d847ed6e17`（API 断流现场）。 |
| `w19` | 主项目 `~/dev/herdr-team` 的团队，与 Issue #6 无关。 |
| `w12` / `w13` | 其他项目（shipyard-ai-research、pagi）。 |
| `w1A` | 收尾用的 dev-lite 团队；本次会话正在此 Pane 中运行。 |
| `~/dev/herdr-team-happier-i6-isolated-...` | Git Worktree。**本会话自身运行在该目录内**，无法自删。合并后工作区干净、提交全部在远程 main，需在退出会话后手动移除。 |
| `~/dev/herdr-team-happier` | 含 WIP 提交 `8cf13c2`（另一条未采用的原生分支方案）与 `.incident-evidence/issue6-1d0debfb70e0/` 事故现场。 |
| `~/dev/herdr-team-issue2-recovery` | 与 main 同步（PR #20 已合并），工作区干净，作为 Issue #2 恢复链路参照。 |
| `herdr-team-archive`、`herdr-team-itest`、`htm-issue2-evidence.w2uitn` | 非 Git 仓库，内容是 Issue #2 与早期联调证据，本轮不动。 |

### 仍需人工处理

```
# 退出本会话后执行（worktree 内不能自删）
cd ~/dev/herdr-team
git worktree remove ~/dev/herdr-team-happier-i6-isolated-20261104044949
```

### 新增的未结束 Run

`4d36bf75eca2`（本次合并前审查）在 `w1A` 上，因 uncertain 派单 `41b12db5aa` 被
Issue #2 的生产门禁锁死（`TRUSTED_CONTROL=None` 无法 recover-attempt）。其审查
结论已通过直接读取工件取得并全部处理完毕，账本保留作为该缺陷的又一实例。

### 补充：w1A 的复核 Pane

收尾期间为绕开 `4d36bf75eca2` 的锁死，在 `w1A` 临时拆出 `p6` 启动独立复核
Agent。审查结论取回后已关闭，`w1A` 恢复为 p1/p2/p3（dev-lite 团队本体）。
