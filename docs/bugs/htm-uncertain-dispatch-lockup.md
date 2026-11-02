# Bug：结果工件缺失导致 uncertain 派单锁死 Run

状态：待设计、未修复。本文仅记录事故和候选方案，不改变调度器。代码行号对应提交 `eb204d5` 的 `scripts/htm.py`。

## 事故、证据与复现条件

2026-11-02，Run `ca060cd383e6` 的 `plan_review` 第 3 次 stage attempt（本次也是第 3 轮）中，role `reviewer` 的派单 `7dc61b5abe` 于 **01:37:16.309597 UTC** 创建。事故记录显示，reviewer pane 停在 `✻ Waiting for 1 background agent to finish`，21 分钟无变化；当时工件目录仅有前两轮 `e73847ec4f.json` 和 `90af4fc982.json`，原派单的 `7dc61b5abe.json` 未生成。人工中断后 collect 的关键返回如下（摘录）：

```json
{"dispatch_id":"7dc61b5abe","status":"uncertain","agent_state":"done","report":null,"error":"Result artifact not found for dispatch 7dc61b5abe"}
```

这里的“未生成”指故障及人工救援之前，并非整个事故之后：手工重投成功后，同一 dispatch 的工件最终生成并被 collect，现存账本已显示 `settled`。当前账本覆盖了派单原来的 collect 状态，无法仅凭最终账本独立还原上述原始返回；返回内容及 21 分钟观察来自事故记录。

最小触发条件：pane 内的主线程启动后台 agent 并等待它；后台 agent 在结果工件写出前终止，主线程未恢复，原派单没有结果工件。后台 agent 的退出为什么没有唤醒主线程尚无内部日志证明，不能将此机制视为已证实的 Claude Code 缺陷。

## 根因及恢复缺口

**观察与推断分开。** pane 显示等待后台 agent，而 Herdr 对 agent 的观测并不等于后台任务的真实生命周期。事故中的 `herdr agent explain` 显示 `working` 由 `osc_title_working`（pane 标题 `◐`）得出；这只是标题信号，不是真实心跳，不能证明子任务仍在运行，也不能证明所有子任务已停止。主线程未恢复是观察到的表现；后台生命周期与 pane 空闲检测解耦是合理的系统边界解释，不是对内部唤醒原因的证明。

**代码中的具体守卫并非完全相同，作用范围如下。**

- `scripts/htm.py:1013-1027`：结果必须读取派单绑定的工件；缺失时抛 `Result artifact not found`。`1051-1066`：观测为 `idle` 或 `done` 且工件无效/缺失时转 `uncertain`；若观测为 `blocked` 则转 `blocked`。有效工件才能结算（或报告 blocked）。
- `scripts/htm.py:1189-1205`：`run collect` 对非 settled 派单重复 reconcile；工件持续缺失且状态仍为 idle/done 时仍 uncertain。它不会主动重发，也没有 abandon 终态。
- `scripts/htm.py:1081-1092,1117-1129`：`run dispatch` 拒绝同阶段同 role 的三类未决状态 `submission_uncertain`、`submitted_unsettled`、`uncertain`；所有未决派单还计入 `max_parallel`。这不是对任意阶段任意 role 的无条件全局禁止。
- `scripts/htm.py:1208-1219`：`run gate` 的 pass/revise 对当前 stage attempt 中任何非 settled 派单拒绝；blocked 也会拦截。其他 gate 决策的行为不能概括为同一守卫。
- `scripts/htm.py:1362-1380`：`run drive` 优先 collect 任意上述三类未决派单，未结算则以 `stuck_unsettled` 或 `worker_blocked` 停止；`--resume` 不绕过它。
- `scripts/htm.py:1572-1589`：`run cancel` 拒绝任何非 settled/blocked 派单，随后还要求 Worker 可核验且处于安全状态。`scripts/htm.py:1523-1539` 的 resume 只改变 Run 状态，不处理派单；`1507-1517` 的 finish 也拒绝未决或 blocked 派单。

因此在本事故的 idle/done + 永久缺失工件条件持续成立时，collect、重派、revise、cancel 和 drive 无法形成恢复闭环。它不是数学意义上永远不可变：工件后来出现，或者观测状态变为 blocked，行为会改变；但现有公开原语没有受控地将该 uncertain 派单标记为已放弃的能力。

## 方案对比（设计，不实现）

1. **推荐：显式 `run abandon --dispatch-id --reason`，关闭旧 attempt，再在新 attempt 受控重派。** 在同一账本锁内核验派单身份和未决状态并重新读取工件；至少要求最近一次 collect 明确证实工件不存在、绑定 Agent 处于 idle/done/blocked、可信来源确认无进行中的后台任务，并要求理由附具体证据。只要文件存在（包括无效文件、符号链接等）或状态/后台任务无法可靠核实，就拒绝，不能把无效工件当作不存在。记录操作者、时间、原派单/attempt、检查结果及证据，原子写入 `abandoned` 和审计事件；不可重复 abandon。明确修改 gate/finish/驱动状态机：abandoned 不等于成功，不能作为正向工件，先关闭旧 attempt 并以记录的恢复事件启动新 attempt，再允许重派；原 attempt 不可通过。保留重试上限、权限、身份与并发守卫，不能因 abandon 自动消耗或绕开审批。
2. **显式 `run dispatch --reissue --confirm-no-artifact`。** 一步完成放弃与重派，操作简便，但把风险确认与发起副作用耦合，难以审阅、恢复中断或区分放弃和重派；仍需同样的状态校验、原子审计及防并发约束。
3. **工件缺失超时自动降级。** 操作便捷，但超时不证明原工作已停止或不会晚到，可能重复副作用；不推荐。

**安全边界与剩余风险。** 人工显式确认、工件缺失检查和审计能减少盲目重派并提供追责，但不能单独证明原 Worker 已停止，也不能在同用户可写工件目录时保证无迟到写入或重复副作用。必须先有可验证的任务终止/隔离机制（例如结束并隔离原 session，再拒绝旧 attempt 的迟到产物），在同一锁内重查身份、状态和工件并通过 compare-and-set 防并发；无法证明无后台任务时 fail closed。即使如此，对不可幂等的外部副作用仍需要独立幂等键、下游去重或人工协调，不能承诺绝对不会重复。权限必须由独立可信的授权边界实施，而不是只依赖任意文本理由；记录完整事件，拒绝重复请求，告警并限制高风险 effect 的恢复。

**人工救援政策。** 本次直接 `herdr agent prompt` 绕过账本重投原 prompt 是事故应急行为，不是受支持的常规恢复命令，也不是安全性证明。在有可信停止/隔离和授权机制前，正式 runbook 应明确禁止该绕过；应保存证据、暂停并升级人工处置，不要盲目重投或编辑账本。不得把本次成功作为未来重投的许可。

## 自动化回归测试设计

- 构造 uncertain 且工件不存在：分别验证 collect 未确认、Agent 非 idle/done/blocked、身份变化、后台任务仍运行或不可观察、理由/授权不足时 fail closed；全部前置条件满足时成功。
- 工件存在时无论有效、无效、损坏或是符号链接，均拒绝 abandon；模拟检查与提交间工件出现、并发 collect/abandon、身份变更，验证锁内复查与原子性。
- abandon 后旧 attempt 不可 gate pass；通过受控恢复创建新 attempt，Run 可继续，后续可完成。原 Worker 晚到工件不能被接受为新 attempt 工件。
- 同一派单不能重复 abandon；审计事件包含身份、时间、原因、证据、检查结果、原 stage/attempt 和派单 ID，失败操作不得产生伪成功事件。
- 验证不会重复派单：并发恢复请求最多一个生效，旧任务未终止/无法隔离时不发新派单；新派单只能属于新 attempt，断言同一 stage attempt 不出现两条已投递派单（如需保留原有重试能力，应明确限定为恢复路径的断言）。针对外部写操作用模拟下游幂等键验证至多一次副作用。
- 防错误验收：abandoned 不产生 pass；新 attempt 的 gate pass 必须绑定最新 actor 工件、verdict=pass、身份与 attempt 匹配，且重新读取后的 SHA-256 等于 collect 时哈希。缺失、旧工件、fail、篡改均拒绝；测试阶段仍须有 exit_code=0 的测试记录。

### 验收标准

- [ ] 未确认工件缺失、任务停止和权限时无状态变化、无重派。
- [ ] 任意已有工件均不能被 abandon 掩盖，竞态和迟到工件有明确处理。
- [ ] 放弃与恢复全过程可审计、可幂等，不绕过身份、并发、重试和审批约束。
- [ ] 可安全关闭旧 attempt 并恢复 Run；不得把 abandon 当通过。
- [ ] 测试证明恢复路径不并发重复投递，并覆盖外部副作用去重与 gate 哈希校验。
- [ ] 无可信后台任务状态或隔离能力时明确 fail closed，不承诺绝对不重复。

## 附录：时间线与处置

时间均为 2026-11-02 UTC；标注账本的时间可由最终账本核对，未记录精确时间的操作按事故记录描述，不能据此推断秒数或严格先后。

| 时间 | 事件 | 来源/限制 |
|---|---|---|
| 01:32:14.870090 → 01:33:54.390679 | 第一轮 `e73847ec4f` 创建并 settled | 账本 |
| 01:34:36.444288 | plan_review 第 1 attempt gate revise | 账本 |
| 01:34:53.458144 → 01:36:24.254201 | 第二轮 `90af4fc982` 创建并 settled | 账本 |
| 01:36:46.398033 | 第 2 attempt gate revise | 账本 |
| 01:37:16.309597 | 第 3 attempt 派单 `7dc61b5abe` 创建 | 账本 |
| 其后，精确时间未保存 | pane 等待后台 agent、21 分钟无变化，故障时缺少该派单工件；人工中断 | 事故记录 |
| 其后，精确时间未保存 | collect 返回上述 uncertain；gate revise 与 cancel 均被拒 | 事故记录；最终账本不保留被覆盖的 collect 返回或拒绝操作 |
| 02:05:45.492620 → 02:05:45.511215 | drive 启动后以 `stuck_unsettled` 停止 | 账本 |
| 其后，精确时间未保存 | 人工 `herdr agent prompt` 绕过账本重投原派单，最终生成工件 | 事故记录；不代表受支持操作 |
| 02:12:51.734691 | 原派单 collect 为 settled | 最终账本 |
| 02:13:00.459114 | plan_review 第 3 attempt gate pass | 账本 |
| 02:48:10.635961 以后 | accept gate pass，Run 最终 finished | 账本；finished 的准确时间以账本 `finished_at` 为准 |
