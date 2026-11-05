# Issue #6 后续事项（不在本轮开发范围内）

- 记录日期：2026-11-05
- 来源：Issue #6 真实端到端验收
- 状态：仅记录，未排期、未设计

本文件只记录验收中确认存在、但**明确不在本轮顺带开发**的事项，供后续排期时取用。

---

## 1. Lead 的 `agent_session` 缺失（Happier 侧）

**现象**：Happier 启动的 claude 不向 Herdr 上报 `agent_session`。

**实测**：`i6-accept` 的 Lead pane `w1:p1`，`herdr pane get` 返回 `agent_session: None`；0.2.17 时期的 `wH:p5` 同样为 `null`。

**影响**：
- `name-lead` 无法验证 Lead 身份 → 拒绝执行
- `adopt` 无法绑定被接管的 Worker → 拒绝执行
- Lead 侧没有任何稳定的跨命令身份标识

**为什么本轮不处理**：这是 Happier 的上报缺失，HTM 侧无法补齐。伪造或转发 `agent_session` 属于自报身份，已在 §5.1 明确禁止。

**后续可能需要**：
- 推动 Happier 调用 `pane.report_agent_session`（设计见 `happier-init-compat.md` §8.1，其中 vendor session id 来源仍是 Open Question）
- 或等待 Herdr 提供服务端对端身份（§8.2，长期正解）

---

## 2. `name-lead` / `adopt` 在 Happier 下不可用

**现象**：两者都会因 `agent_session` 不可核验而拒绝执行。

**重要区分**：**能力本身已在代码中实现**（`adopt` 起于 `scripts/htm.py:885`，`name_lead` 起于 `:1018`），不是功能缺失。在原生 Herdr 环境下二者正常工作。

**影响范围**：仅 Happier 包装的 Lead / Worker 场景。

**后续可能需要**：
- 依赖第 1 项解决
- 若 Happier 长期不上报，则考虑在文档中永久标注为"Happier 环境不支持"，而不是留一个会拒绝的命令入口

---

## 3. Worker 侧自发启动后台子代理

**现象**：Claude Code Worker（无论原生还是 Happier 包装）会在收到任务后**自动启动后台 subagent / Task 子代理**，导致主线程停在 `✻ Waiting for 1 background agent to finish`。

**本轮实测**：Happier Lead 在三次派单中有两次自动起了 `general-purpose` 子代理，分别 14s / 32s 后返回，**未卡死**。

**历史事故**：同一模式已导致两次 uncertain 派单锁死（Run `ca060cd383e6`、Run `a9768b65cab1`），见 `docs/bugs/htm-uncertain-dispatch-lockup.md`。

**为什么本轮不处理**：HTM 是调度器，无法从外部阻止 Worker 内部启动子代理。现有缓解只有两条，均不构成保证：
- 派单 prompt 里显式禁止（本轮采用，**有效但非强制**）
- uncertain 后停止并升级人工（`htm-uncertain-dispatch-lockup.md` 的人工救援政策）

**后续可能需要**：
- `run abandon --dispatch-id --reason`（该 bug 文档中的推荐方案），提供受控的放弃与重派原语
- 或在 Worker 启动参数层面禁用 subagent（需确认 Claude Code 是否支持）

**风险提示**：这是一个**概率性**问题。本轮两次都恰好返回了，不能据此认为已缓解。

### 3.1 重要更正：`S+` + 低 CPU **不能**判定 Worker 卡死

2026-11-05 的执行过程中，我三次依据「进程 `S+` 睡眠、CPU <3%、唯一子进程是 `caffeinate`、`state_change_seq` 长时间不变」判断 Worker 卡死。**其中至少两次是误判。**

实际情况：Claude 在 `thinking with high effort` 时就是上述进程特征。其中一个 Worker 最终在 20+ 分钟后正常产出了 4900 字节的审查报告，另一个跑了 22 条 shell 命令后因 **`API Error: Server error mid-response`** 断流才没写报告。

**正确的判定方法**：
- 看 `herdr agent read` 的**实际输出内容**是否有进展（如工具调用计数、token 增量、耗时计时器在动）
- 看 `herdr agent explain` 的 evidence 字段
- **不要**仅凭进程状态和 CPU 判断

**`osc_title_working` 的局限仍然成立**（它是标题信号不是心跳），但它导致的不是「卡死检测失败」，而是「无法区分卡死与深度思考」。

### 3.2 新增真实复现：abandon 后 Run 完全锁死（2026-11-05）

**Run `73d77630456f`**（生产 default 状态目录，Team `w0`）：

1. 派单 `ea7897bcdf` → `submission_uncertain`（Worker 卡在等待输入）
2. `run collect` → `uncertain`（`Result artifact not found`）
3. `run diagnose` → `presence=done`、`artifact.exists=false`、`diagnosis=undetermined`
4. `run recover-attempt` → `T7 requires at least one safely abandoned member; a pure business group must be closed by T21`
5. `run abandon --confirm-untracked` → **成功**，返回 `abandoned`、`recovery_authorization: "unavailable"`
6. `run gate --decision pass` → `fault group contains an abandoned member; it must be recovered as a whole with a bound authorization, never decided by an ordinary gate`
7. `run cancel` → `execution_not_fenced: no trusted execution control plane`

**结论**：`abandon` 是**单行道**。一旦放弃某个派单，该 fault group 就要求「整体受控恢复」，而本版本 `recovery_authorization` **未实现**，于是 Run 既不能 gate、也不能 cancel——比不 abandon 更死。

**这修正了 `htm-uncertain-dispatch-lockup.md` 的推荐方案**：该文档把 `run abandon` 列为首选，但它落地后引入了一个新的、更硬的锁死点。**在 `recovery_authorization` 实现之前，不应把 `abandon` 当作恢复手段推荐给用户。**

**现场保留**：`73d77630456f` 保持锁死状态作为证据，未做任何绕过尝试。

---

## 4. 跨 Session 账本键冲突

已独立成篇：`docs/issues/p1-cross-session-team-ledger-key.md`（P1，正式支持多 Session 前必须解决）。

---

## 5. Happier 版本行为漂移

**现象**：Happier 0.2.17 时 claude 子进程**整体没有** `HERDR_*`；0.2.20 时**只缺 `HERDR_ENV`**，其余五个都在。

**影响**：设计文档 §2 的旧描述已按 0.2.20 修订并保留历史记录，但这类漂移说明**上游剥离策略不是稳定契约**。

**后续可能需要**：
- 在每次 Happier 升级后重跑最小验证：`ps eww` 检查 claude 子进程的 `HERDR_*` 集合
- 不依赖"某个版本一定剥离/保留哪些变量"的假设
