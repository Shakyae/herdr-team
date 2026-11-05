# Issue #2：uncertain 派单 abandon 设计

**设计修订稿。部分能力已实现并通过独立评审；标注为「拟议」的条目仍是设计意图，不代表已交付。** 本稿继承分支 `design/issue2-abandon`、HEAD `14123d9` 的设计，在独立分支 `feat/issue2-uncertain-recovery` 修补 Reviewer 报告 `628f14afb3` 的 B1/B2，并纳入 Issue #2 新增的 API 503 事故要求。原 Team `74cb47a7d586ea26`、原 Run `ca9e5107ac10` 及历史工件只读保留，不按本稿操作。原稿在仓库外留存字节一致的备份。旧报告的测试基线为 204 passed / 116 subtests，不冒充本次实测。以下源码行号以当前工作树为准；事实、推断与拟议行为分别标明。

> **实现状态（2026-11-05 更新）**
>
> **已完成并通过独立 Reviewer 审查**：`agent_presence` 三态、证据诊断、受控 abandon、故障组与 R1 每角色预算、一次性角色槽、原子 recover-attempt、封组与闭合证明、迟到工件只留证、B1 取消状态白名单、B2 working 禁止 abandon、B3 blocked 终态保护、T17 取消 A/B 分级。
>
> **仅在模拟环境验证**：上述全部能力均在 mock Herdr + 注入 fake `TRUSTED_CONTROL` 下测试（376 passed / 1 skipped / 159 subtests，无 PyYAML；377 passed 含 PyYAML）。这只证明守卫逻辑正确，**不证明**真实执行隔离、授权或副作用去重可用。
>
> **生产不可用**：`TRUSTED_CONTROL = None` 为固定值。B 类（有执行历史）Run 的 cancel、`recover-attempt`、恢复 gate 认证、`blocked_round_limit` 技术越限恢复，在真实环境全部 fail closed 并升级 Owner。A 类（从未派单）Run 的 cancel 可用。
>
> **未验收**：真实 API 503 四场景 E2E 一行未跑。前置能力已登记为 Issue #19；在 #19 完成前，不具备真实故障验收条件，M3 仅可在隔离 STATE 下做注入与模拟测试。
>
> 正文中的「拟议」标注保留，表示该条目是设计意图而非交付承诺；已实现条目以本状态块为准。

**新增需求来源**：Issue #2 正文及评论 https://github.com/Shakyae/herdr-team/issues/2#issuecomment-6614061492 （GitHub UTC 时间 2026-11-03T19:57:59Z，评论标题为 2026-11-04）。评论记录 Run `1d0debfb70e0` / Dispatch `661941c28f` 的 `503 auth_unavailable`、`model_not_found` 与报告缺失，不证明单一因果，也未核验部分文件修改。本稿只规定 #2 恢复闭环，不实现 #6 Happier 兼容、#7 Session Rotation、#9 Lead 长任务自治。

**本次修订回应的 Review 意见（拟议）**

| 阻塞意见 | 修订位置 |
|---|---|
| blocked 观测且无有效报告锁死 | §四状态表、§五迁移与 §九用例 24–25 |
| 隔离和授权缺可验证契约 | §二外部依赖与首版验收边界、§十 |
| 人工 gate 与恢复 attempt 的绑定 | §六证据契约、§九用例 26–27 |
| 真实 503 四场景验收缺失 | §九矩阵及用例 23、28–31 |
| 身份校验归属不准确 | §一、§三、§八 |

第四轮对照（拟议；「闭合」仅指本文提出可测设计，不代表实现或独立复审通过。前三轮条目改为指向当前章节与用例编号，不再引用第三轮版本的物理行号，避免第四轮插入权威状态机后行号漂移）：

| 项目 | 当前位置 | 对应源码现状 | 设计闭合及剩余不确定 |
|---|---|---|---|
| P0-1 终局死锁 | §四 T5、T10–T13、T16、T20；§九 32–33 | `scripts/htm.py:1233-1238,1274-1278,1532,1572,1582` | 设计闭合；历史数据迁移与并发实现待验证 |
| P0-2 协议绕过 | §四 T7–T9、故障组定义；§九 34–36 | `scripts/htm.py:1091,1101-1108,1136-1148,1473-1476` | 拟议闭合，模型已被 R1 配额取代：不再依赖单角色一次性 reservation；实现与外部隔离/授权待验证 |
| P1-3 人工 gate | §四 T10–T14；§六人工 gate 契约；§九 37–38 | `scripts/htm.py:1239-1270,1405-1408,1491-1505` | 设计闭合；人工认证服务尚不存在 |
| P1-4 lead 正向检查 | §四 T10 末句、T13；§九 39 | `scripts/htm.py:1239-1257,1262-1264` | 设计闭合；空工件 lead 阶段的人工认证仍依赖外部服务 |
| P1-5 recovery_active 消费者 | §四 T7–T17 与共享守卫消费者表；§五 5.3；§九 40 | `scripts/htm.py:969-992,1091,1138,1230,1357-1358,1372-1374,1529,1542-1555` | 设计闭合；状态迁移和暂停恢复的实现待验证 |
| P2-6 身份字段 | §一「已验证事实」；§三 present 契约；§九 41 | `scripts/htm.py:481-508,1295-1309` | 设计闭合；缺失字段是否可验证取决于 Herdr 数据 |
| P2-7 未定义编号 | §二「外部依赖与首版能力边界」 | 不涉及源码谓词；审批现状 `scripts/htm.py:1020-1023` | 设计闭合；外部依赖仍未交付 |
| R1 多角色恢复死锁 | §四 T7–T10、无死锁证明；§九 42 | `scripts/htm.py:1101-1108,1136-1148,1239-1257` | 拟议闭合：故障组统一授权、逻辑根配额及独立角色槽；余额不足时升级，非保证成功 |
| R2 混合故障洗白 | §四 T5–T10、T12–T13；§九 43–44 | `scripts/htm.py:1233-1238,1274-1278` | 拟议闭合：故障组整体封闭决策，业务 revise 不能绕过；尚待独立复审及实现验证 |

第五轮对照（拟议；闭合仅指设计修订，未实现且待独立复审）：

| 项目 | 当前位置 | 对应源码现状 | 设计闭合及剩余不确定 |
|---|---|---|---|
| F1 恢复后续阶段死锁 | §四 T10、有限路径；§九 46 | `scripts/htm.py:1091,1271-1273` | 拟议闭合：选 (a) 成功后转 active；原业务 round 不扩额，仍待实现验证 |
| F2 unknown 双重映射 | §四观察分类、T3–T4；§九 47 | `scripts/htm.py:1070-1085` | 拟议闭合：互斥分类、unknown 优先 uncertain；待实现验证 |
| F3 历史未决收集缺口 | §四 T17–T18、§五 5.1；§九 24、48 | `scripts/htm.py:1208-1221` | 拟议闭合：当前/历史 blocked 分流；历史 uncertain 的有效迟到报告仅留证并升级 |
| F4 纯业务组关闭次序 | §四故障组、T21、有限路径；§九 49 | `scripts/htm.py:1233-1238,1274-1278` | 拟议闭合：先 T21 再 T11/T12；待实现验证 |
| F5 恢复前态与技术越限来源 | §四 T7、T9、有限路径；§九 50 | `scripts/htm.py:1274-1278` | 拟议闭合：分 Run 状态；技术越限仅外部迁移初始态，可信迁移机制未提供 |

## 一、根因确认

**已验证事实（源码）**：`scripts/htm.py:1032-1067` 仅接受派单绑定、身份匹配的正规文件；`scripts/htm.py:1070-1085` 中 idle/done 且工件缺失或无效会变为 uncertain，blocked 对应 blocked；未知状态也为 uncertain。任务将 `:1051-1066` 标为 reconcile，实际该段是 `read_result` 验证，reconcile 在 `:1070-1085`。`run_collect` 先经 `verify_existing`（`:1208-1213`），非 settled 才 reconcile（`:1216-1221`）。`run_dispatch` 同阶段同角色的三类未决状态阻止投递、attempt 次数和全局并行数也分别受限（`:1101-1111`，锁内 `:1140-1148`）；gate 的 pass/revise 拒绝当前 attempt 的非 settled（`:1233-1238`）；drive 对 pending 先检查身份（`:1295-1311,1381-1398`）；cancel 拒绝未决派单；有执行历史的 Run 另需全部角色可信停止/隔离证明，无执行历史的 Run 不适用执行者维度（`:3378-3464`）。这些守卫共同使缺失工件且观测保持 idle/done 的派单无法通过正常路径解除阻塞；观测 blocked 且报告缺失或无效也会落入 blocked，随后 gate 同样拒绝。现状 `verify_existing` 检查 pane/kind、可选 name、已登记 session value、返回时的 cwd 和 Hermes profile（`scripts/htm.py:481-508`；cwd/profile 缺失时现状不拒绝）；drive 的 `check_identity` 另要求 session 的 kind/source/value 全部绑定且匹配（`scripts/htm.py:1295-1311`），不是 collect 已具备该完整检查。

**推断及边界**：这不是数学上的永久不可变；工件后来出现或状态变化可能改变结果。事故记录 `docs/bugs/htm-uncertain-dispatch-lockup.md:7-19,23-30` 支持上述条件性锁死，明确指出旧账本不能独立重现原始 collect 结果、内部唤醒原因未证实。该文 `:34,38,44-46` 提出可信后台任务停止前置；本设计按最终评审计划以第二、五节替代该前置，不将其当已验证能力。现有代码无受支持的 uncertain→abandoned 终态转换路径，基于源码路径检查；不是说有效工件出现后不能转 settled。

## 二、可信停止/隔离：明确不可证

**已验证事实**：`scripts/htm.py:423-435` 在状态缺失或空白时返回 `unknown`；事故记录 `docs/bugs/htm-uncertain-dispatch-lockup.md:19` 记录该次 `working` 来自 pane 标题的 `osc_title_working`，而非心跳。`scripts/htm.py:346-370` 的前台 shell 检查亦不能证明后台进程全部停止。

**推断及设计约束**：现有观察不能独立证明旧任务已停止或隔离。idle/done/blocked、pane 消失、超时、用户风险声明均不是停止证明。abandon 只终结该 Dispatch 的结果资格，不表示任务成功或进程终止；恢复投递是另一操作，必须另验旧执行者已被可信停止/隔离，以及副作用可安全重做。只有结果文件隔离而无进程/写入隔离时，不得重派写任务。对未知 presence、身份漂移、无法核实的授权或外部副作用一律 fail closed，返回可操作的升级原因；不再以「接受残余风险」替代 Issue 正文的安全前置。

**可信证据边界（拟议）**：可接受的执行隔离依据来自独立执行控制面，例如任务专属容器/OS 进程域的终止回执与写权限撤销，且必须绑定 Run、Dispatch、旧 session 和执行域。当前 Herdr 状态和同用户文本声明不能提供这种回执；首版没有可验证依据就报告 `execution_not_fenced`，而不是伪造证明。Lead 可自主收集证据、collect 可信有效报告及在既有权限内执行符合条件的 abandon；自动新投递仅限停止/隔离可核验、执行范围和全部副作用已核实且幂等的情形。不能将 `--effect read` 声明当作 OS 沙盒证明。对人工协调的非幂等任务，只列外部处置选项，不由本命令解除审批。

**外部依赖与首版能力边界（拟议）**：可信执行停止/写入隔离、独立 Owner 授权和人工身份认证均为尚未提供且尚未验证的外部依赖；本文不定义或声称存在生产验签接口。首版缺少这些依赖时，不接受本地字符串、同用户文件或测试假回执作为证明；涉及这些前置的 abandon、重派及人工 gate 一律 fail closed，只能收集证据并向人工/外部控制面升级，不能宣称原 Run 自动恢复。`scripts/htm.py:1020-1023` 的 `run_approve` 当前恒拒绝，因此任何需要该审批的现有 dispatch 都无法通过它获得 token（审批要求及使用见 `scripts/htm.py:1010-1017,1112-1115`）；本设计不把外部授权说成已接入，也不恢复同用户 TTY 审批。日后外部依赖若交付，须另行设计和评审其来源、可信根、目标绑定、有效期、撤销及失败关闭，再进行真实集成验收。

## 三、拟议 `agent_presence(role)` 三态

- `present`（拟议）：`herdr agent get` 成功且返回有效 Agent 数据，严格核验非空绑定名称、pane、kind、session 的 kind/source/value、解析后的 cwd 和 Hermes 的 profile（非 Hermes 不要求 profile）；绑定或观察字段缺少、不可核验或不匹配均不能视作安全的 present。现状 `verify_existing` 的名称、cwd、profile 返回缺失时不拒绝，只比较已登记的 session value（`scripts/htm.py:481-508`）；drive 的 `check_identity` 额外完整比较 session 三字段（`scripts/htm.py:1295-1309`），严格名称、cwd/profile 缺失拒绝与全链路检查均为拟议增强；无法提供这些字段时为 unavailable，而非放宽身份判断。
- `confirmed_absent`：`agent get` 的 JSON `error.code == agent_not_found`，且对账本中绑定 pane 的 `pane get` 以 JSON `error.code == pane_not_found` 失败，二者均必须满足。`scripts/htm.py:380-383` 是现有 pane 查询/校验位置，需扩展区分 pane_not_found，而非把任意错误视为缺失。
- `unavailable`：其他全部情况，包括超时、Herdr 不可用、未知 code、无效数据、身份不匹配、pane 仍存在或 pane 查询异常。拒绝支 B，不能降级为 absent。三态是观察结果而非终止证明。

**已实测事实（任务提供）**：不存在的 agent：returncode=1、stdout 空、错误 JSON 在 stderr，code 为 `agent_not_found`；不存在的 pane：returncode=1、code 为 `pane_not_found`。判定只依赖 JSON `error.code`，不得凭退出码推断缺失；`scripts/htm.py:319-329` 在失败时按 stderr→stdout 解析 code，`:463-468` 已有按 `agent_not_found` 判定名字可用的先例。成功数据也须检查结构，不能将解析失败当缺失。

## 四、唯一权威状态机与共享守卫（拟议）

本节取代原分散迁移规则及 blocked 表，是 Dispatch、Attempt 和 Run 的唯一权威；其他章节仅描述证据、接口和验证。`当前` 指 Run 当前 stage_index 与 stage_attempt 均匹配；其余为 `历史`。状态乘积以行中通配符覆盖，未列出的事件一律拒绝且不改变状态。`blocked(无有效报告)` 是旧账本可能出现的分类，不是允许新建的业务终态；`blocked(有效)` 必须有绑定报告及收集哈希。`settled` 包含 pass/fail/needs-work，不能一概视为成功。`*` 包含所有七种 Dispatch 分类或所有 attempt；`—` 表示尚无 Dispatch。Run 额外纳入现状 `paused`、`blocked`，避免遗漏。所有写操作锁内重新计算守卫并 CAS 持久化状态和事件；外部观察仅供快照，冲突返回重新诊断。终态后的证据采集不得修改账本结论。

**故障组 G（拟议）**：以 run、stage_index、逻辑 attempt 根为键，第一次出现 uncertain、投递返回异常或超时导致送达未知、需恢复处置的 submitted_unsettled 超时、或无有效报告 blocked 时打开；正常在途 submission_uncertain/submitted_unsettled 本身不打开组，成员是该逻辑根各关联 attempt 中所有已保留派单，包含 settled、有效 blocked、abandoned 和仍未决项，以及后来合法恢复派单；成员不可由调用者选择。组打开即冻结普通派单、gate pass/revise 和阶段推进，只允许表内处置。锁内完成所有在途提交及分类（未知送达仍是未决，不能靠超时封闭），每个技术故障均 collect 成有效报告或安全 abandon，确认成员快照、工件哈希、业务否定与副作用清单后封闭；封闭后追加仅允许由该组授权的恢复槽，新故障重新打开并扩充同一组。组内有任何 abandoned，即使另有有效 blocked，也不能单独业务 revise：先整体授权恢复并重做，得到新 attempt 的全部必要角色新报告；若新报告有业务否定，则在安全条件与普通 round 满足时整个组作业务 revise 决策，旧否定及 abandoned 被明确记录、逻辑根预算不返还。仅无 abandoned 且无技术未决的纯业务组须先按 T21 关闭并留证，再按 T11/T12 决策；不得在组开放时直接 revise。恢复 pass 或整体业务 revise 成功并写闭合证明后，组关闭；之后普通业务 attempt 或后续 stage 不再继承已关闭组的重派限制，审计与历史计数永久保留。若不能证明组成员完整或历史已闭合，升级而不跨 attempt。

**R1 配额模型（拟议）**：选择每逻辑 attempt 根的预算账本，按角色分别计 `1 + max_retries`，不是单角色一次性 reservation。一个原子故障组授权绑定 run/stage/目标 attempt、完整成员快照、逻辑根、必要角色集合（当前 stage actor，lead 不强制自身，另加旧 attempt 曾派出的所有角色及可信策略要求的角色）、每角色剩余预算、每角色动作/effect/身份、隔离、幂等和审批；发放每角色独立一次性槽。已 settled 的 developer 与 abandoned 的 reviewer 各有自己的槽；每槽在实际保留 dispatch 时原子消费、记入各自累计预算，所有槽共享组授权但不共享消费状态。恢复 attempt 不继承旧 pass，必要角色各自新出报告。授权生成不发送 prompt；并发及重入只能返回同一组授权和同一槽，已占槽崩溃只能续同一 dispatch，不得重复发送；送达未知须 collect/abandon，不能重发。任何角色预算不足则整个组不得开始新 attempt，升级或安全取消；这牺牲部分本可恢复任务的可用性，避免对成功角色无限免费重做。每个新槽仍按角色核查 Worker 身份、effect、并发、可信隔离与动作级幂等及独立授权，不能因统一授权绕过。恢复 attempt 中发现新的负面报告时保留组及预算，不能先 revise 洗掉原故障。

**观察分类及优先级（拟议）**：观察互斥分为 `idle/done`、`blocked`、`unknown/无法分类`（包括其他任何值）。身份不可核验先拒绝迁移；其余先判报告有效性及观察：报告缺失/无效或观察 unknown/无法分类均按 T4 进入 uncertain，即使报告完整或报告状态为 blocked；只有观察为 idle/done 或 blocked 且报告有效才按 T3 分类，观察 blocked 或报告 blocked 得有效 blocked，否则完整报告得 settled。

| 行 | 前一状态（Run / attempt / Dispatch） × 触发事件 | 守卫条件 → 后一状态 × 副作用 |
|---|---|---|
| T1 | active / 当前 / — × 普通 dispatch | 无开放故障组、资格/身份/审批/并发/角色预算满足 → submission_uncertain；原子占用预算并记提交意图。 |
| T2 | active 或 recovery_active / 当前 / submission_uncertain × no-wait 确认送达 | CAS 同 dispatch、attempt、非终态 → submitted_unsettled；未知送达保持原状态，不重发。 |
| T3 | active 或 recovery_active / 当前 / submission_uncertain、submitted_unsettled、uncertain × collect 或 wait | 身份可核验、报告有效且观察为 idle/done 或 blocked；观察 idle/done 且报告 complete → settled；观察 blocked 或报告 blocked → blocked(有效)；均保存报告和哈希。 |
| T4 | active 或 recovery_active / 当前 / submission_uncertain、submitted_unsettled、uncertain × collect 或 wait | 缺失/无效报告或 unknown/无法分类观察 → uncertain，记诊断；即使观察 blocked 也不生成有效业务 blocked。身份不可核验拒绝迁移。 |
| T5 | 非终态 Run / 当前或历史 / blocked(无有效报告) × 旧数据重检 | 锁内绑定工件和历史可验证且无有效报告 → uncertain，记迁移和故障组；有效报告 → blocked(有效)并固定哈希；来源矛盾、不可读或可能已接纳有效报告 → 不变并升级。 |
| T6 | active、recovery_active、blocked_round_limit、paused、blocked / 当前或历史 / submission_uncertain、submitted_unsettled、uncertain × abandon | 故障组成员、静默期、可信隔离、身份/授权及 §五证据校验均满足，锁内无有效报告 → abandoned；保存不可逆事件及证据，不退款不投递。否则不变升级。 |
| T7 | active 或符合结构化技术越限条件的 blocked_round_limit / 当前 / * × recover-attempt | 故障组封闭、至少一个 abandoned、所有必要角色预算/身份/隔离/幂等/授权均满足，且整个组业务否定已记录；blocked_round_limit 另须外部迁移初始态的可验证纯技术越限、组无业务否定；表内 T12/T13 越限均为业务来源，不产生纯技术越限 → 新当前 attempt / —；原 attempt 成历史，round 不变，组跨 attempt 继承并原子发放角色槽；越限时转 recovery_active，否则保持 active。并发重入返回同一授权。 |
| T8 | active 或 recovery_active / 当前恢复 attempt / — × dispatch | 开放恢复组的对应角色槽有效、匹配快照与预算，且 T1 的资格/身份/审批/并发/预算约束满足（T1 的无组条件不适用于本行）→ submission_uncertain；原子消费该角色槽并占预算；无槽拒绝。恢复中故障按 T2–T6 及 T9，禁止普通旁路。 |
| T9 | active 或 recovery_active / 当前恢复 attempt / * × 再次技术故障或 abandon | 原组重新打开并加入全部新派单；active 下 T6 后仅在各角色累计预算足够且重新满足 T7 全部条件时可再开 attempt；recovery_active 下禁止第二恢复链，不可调用 T7，安全处置后升级或 T17。组和预算从不重置。 |
| T10 | active 或 recovery_active / 当前恢复 attempt / * × gate pass | 组封闭、无未决/blocked/abandoned（此处的「无 abandoned」指无未闭合的降级项：组内原 abandoned 派单只要已被本 attempt 对应角色的新 settled/pass 工件覆盖，即为历史记录，不构成阻塞；未被覆盖的 abandoned 仍阻塞），每个必要角色本 attempt 最新 settled/pass 且哈希不变、全部 gate/人工/测试条件满足 → 原 attempt 成历史、stage 前进；组闭合、预算结清并保存完整证明，recovery_active 原子转 active（active 保持 active），round 不变。选择方案 (a)，使后续普通阶段仍经 T1 守卫派单；代价是恢复成功后不再保留 recovery_active 的限制，既有业务 round 限制及所有普通守卫仍生效。lead 空派单仍须人工认证；恢复角色集合不可为空时不得借空分支。 |
| T11 | active 或 recovery_active / 当前普通 attempt / * × gate pass | 无开放故障组、无未决/blocked/abandoned，必要角色最新 settled/pass、哈希和全部 gate/人工/测试条件满足 → 原 attempt 成历史、stage 前进。 |
| T12 | active / 当前 / * × gate revise | 所有派单已分类，无技术未决、无 abandoned、无开放故障组；存在有效业务否定（有效 blocked 或 settled fail/needs-work），证据及人工 gate 条件满足 → 新 attempt / —，round 加一，超上限转 blocked_round_limit；记业务决策及旧 attempt 闭合证明。 |
| T13 | active / 当前恢复 attempt / * × gate revise | 已完成整个故障组授权恢复，所有必要角色均有本 attempt 新的 settled 或有效 blocked 报告、无未决/abandoned（与 T10 同一派生谓词：组内原 abandoned 派单只要已被本 attempt 对应角色的新报告覆盖，即为历史记录，不构成阻塞；未被覆盖的 abandoned 仍阻塞）、哈希及业务否定有效，且人工 gate 条件满足 → 组整体业务决策并关闭，round 加一、新业务 attempt / —；超上限转 blocked_round_limit。禁止对旧 attempt 混合组单独 revise；recovery_active 不允许此行。 |
| T14 | active 或 recovery_active / 当前 / * × gate block | 人工 gate 条件及具体证据满足，不改变故障组成员/预算 → blocked；不得借 block 或 resume 洗掉故障组。 |
| T15 | active / * / * × pause；paused 或 blocked / * / * × resume | 保留组、attempt、预算；前者 → paused，后者 → active；recovery_active 禁止 pause/resume；blocked_round_limit 不得普通 resume 或扩额。 |
| T16 | active 或 recovery_active / 终点哨兵 / * × finish | 所有必需 gate pass 且其最终 attempt 证明闭合、所有历史组闭合、无未处置副作用或占槽 → finished；不能仅用当前 attempt 过滤历史。 |
| T17 | 任一非终态 Run / * / * × cancel | 确认与原因有效，当前未决已按 T3–T6 处置、历史未决已按 T5/T6 处置 → cancelled；留存未完成组及原因，不释放 Team。执行者维度按 Run 有无执行历史分级（见下）：A 类无执行历史时隔离义务不存在，只需锁内核对「从未派单」；B 类有执行历史时仍须所有执行者可信停止/隔离、副作用安全处置且身份可核验，缺证据 fail closed。 |
| T18 | 任意 Run / 历史 / settled、blocked(有效)、abandoned × collect/迟到结果 | 仅读与留证，不改变终态或供当前 gate 使用；历史技术未决不走 T3/T4 collect：仅可只读诊断及留证，按 T5 重检、满足 T6 时安全 abandon，否则升级；迟到有效报告只留证并升级（有效报告使 T6 拒绝），不改变该历史未决状态或当前 attempt 判定，不得成为当前 gate 证据。 |
| T19 | finished、cancelled / * / * × 任意写入 | 拒绝所有迁移及异步收尾；只读诊断/安全留证，不复活 Run。 |
| T20 | 任意 Run / * / * × list/recover/diagnose | 只读分类与升级提示，不迁移、不投递；pending 仅列当前技术未决和当前 blocked，历史异常单独展示并阻止 T16，abandoned 单列。 |
| T21 | active / 当前普通 attempt / * × 无 abandon 的组解决 | 前置：Run 为 active 且当前 attempt 为普通 attempt；组已封闭、所有技术故障均有有效报告且成员完整；组含 abandoned 一律禁止。满足时原 attempt 不变、组关闭并写证明，之后按 T11（全部必要角色最新 settled/pass）或 T12（存在有效业务否定）决策。恢复 attempt 的组必含 abandoned（T7 前置「至少一个 abandoned」），与「组含 abandoned 一律禁止」合取恒不满足，故本行不适用于任何恢复 attempt；恢复组由 T10/T13 自行闭合，不经 T21。`recovery_active` 下当前 attempt 必由 T7 建立且为恢复 attempt，因此整行不可达。blocked_round_limit、paused、blocked、finished、cancelled 等非 active 状态不满足前置，不可执行。 |

**T17 取消分级（拟议，本轮修订）**：cancel 的执行者义务取决于该 Run 是否曾经发出过任何 prompt，而不是取决于当前 Run 状态或故障组是否存在。

- **A 类，无执行历史 Run**：同时满足 `dispatches` 为空、无 `fault_groups`、无 `recovery_authorizations`。此类允许在**不具备执行隔离证明**的情况下 cancel。
- **B 类，有执行历史 Run**：其余一切情况（哪怕只有一个已 settled 的 dispatch，或仅存在组/授权记录）。保持原严格要求：所有执行者可信停止/隔离、副作用安全处置、身份可核验；缺证据 fail closed。

两类判定前，`check_transition` 还会先施加一个状态白名单：只有 `active`、`blocked`、`blocked_round_limit`、`paused`、`recovery_active` 才允许进入 cancel 判定；`finished`/`cancelled` 走 `run_terminal`，其余任何状态（含未知值、缺失、`None`、空串）一律 `run_state_forbidden` 失败关闭且不写账本。`recovery_active` 列入白名单是因为恢复中的 Run 必须始终保留一条安全退出的路径。

A 类不是「为可用性降低标准」，而是**该类别下隔离义务的对象不存在**：没有执行者，就没有需要停止或隔离的执行者，也没有需要核对身份的 Worker。这是逻辑推论，不是风险接受，也不把 idle/done 观察或调用者声明当作任何证明。

该推论的唯一事实前提是提交顺序：`scripts/htm.py:2513` 先把 dispatch 写入账本并 `save`，直到 `:2556` 才真正 `herdr(*cmd, ...)` 发送（`:2517` 仅构造 `agent prompt` 命令）。因此「`dispatches` 为空」可靠地证明本 Run 从未发出过任何 prompt；账本被外部篡改时该前提不成立，但那种情况下整个账本已不可信，不属于本设计可保证的范围。锁内必须重新读取并校验 A 类全部条件，任一项不满足即按 B 类处理。两类判定结果都写入 cancel 事件（`cancel_scope: no_execution_history | execution_history`）供审计。

**映射（拟议）**：§五证据与隔离实现 T4–T9；§六审计和人工认证供 T6–T17；§七迟到与幂等供 T2–T10、T18–T19；§八生命周期依 T15–T17；§九验证全部行；§十实施须以本表为准。`scripts/htm.py:1175-1198` 异步收尾现无这些 CAS；`:1208-1224,1070-1085` collect/reconcile、`:1233-1238,1274-1278` gate、`:1530-1535,1572,1582` finish/list/recover 均为现状而非本表已实现。

**有限路径及 fail-closed 证明（拟议）**：这是条件性可达性，不是无条件活性。submission_uncertain、submitted_unsettled、uncertain 各可 T3 settled 后（如已打开故障组则先 T21）按 Run 状态及 attempt 类型进入对应 gate 路径；或在 active 下 T6 abandoned → T7/T8/T3/T10，再经后续阶段及 T16；recovery_active 下再次故障不得 T7，须升级或安全取消；也可在安全处置后 T17。blocked(无有效报告) 先 T5 分流，再走对应路径。blocked(有效) 若属于打开的纯业务组，先 T21 关闭组再按 T11/T12 处理；未打开故障组时可直接 T12；混合组先 T6/T7/T8 重做全部角色，再 T10 或 T13；均可安全处置后 T17。abandoned 经 T7–T10 可成功，或安全 T17；settled pass 在无开放组或先 T21 关闭纯业务组后，普通 attempt 经 T11，恢复 attempt 经 T10；settled 否定在同样关组前提下，普通 attempt 经 T12，恢复 attempt 经 T13，或 T17。T21 仅适用于 active + 当前普通 attempt；恢复 attempt 的组必含 abandoned（T7 前置），由 T10/T13 自行闭合，T21 对其恒不适用；recovery_active 下当前 attempt 必为恢复 attempt，故整行不可达；非 active Run 状态同样不可执行。历史项仅在有可验证闭合证明时不阻断 T16；异常历史按 T5/T6 处理或升级，不能假设历史即闭合。

active 可经全部必需 gate 和 T16 finished，或 T17 cancelled；recovery_active 可经受限 T8/T10 成功后转 active，再由 T1 派后续普通阶段并按 T11/T16 完成，或 T17；负面业务报告不能 revise 时升级。blocked_round_limit 的纯技术越限是表内不可达的外部迁移初始态，只能由人工/外部结构化事件建立并可验证，机制尚未提供；仅此类满足 T7 其余条件时可进入 recovery_active，业务越限或来源不可验证仅安全 T17 或升级；paused/blocked 可 T15→active 或 T17，故障组不变；finished/cancelled 已终态。上述每条成功路径要求有限个外部动作均成功、余额足够及合法证据可用；若无法证明停止/身份/副作用、余额耗尽、认证不可用、历史冲突或不可安全取消，则是**设计上的 fail-closed 停止态**，不保证有限到达 finished/cancelled；保留 Worker 占用，输出原因、证据缺口及交独立 Owner/执行控制面处置，不允许删账本或伪造证明。故本设计排除 R1 的资格结构死锁，而不声称外部依赖故障下必定可终结。

**共享前置检查（拟议）**：`check_transition(snapshot, operation, target, decision, context) -> {allow, code, facts}`；snapshot 含 Run/全部 Dispatch/组/槽/预算及版本，context 含身份、报告哈希、隔离、幂等、审批与人工证据快照。输出首个拒绝码或 `ok`，只读返回分类；读入口允许返回诊断且不把缺依赖误报为写入许可。写入口在锁外预检、锁内重新取快照及调用同函数，原子提交并 CAS，异步回调也使用相同检查；外部证据需锁内验证其绑定/有效期/撤销和快照，无法原子验证则 fail closed。顺序及稳定码：① `run_terminal`（T19）；② `stale_snapshot`（版本/attempt/成员变化）；③ `run_state_forbidden`（T1–T17 状态不允许）；④ `history_unresolved`（T5/T16 历史闭合不可证）；⑤ `fault_group_open`（T10–T13 组未封闭或混合故障试图单独 revise）；⑥ `dispatch_unresolved`（T3–T6/T10–T17 未决或报告分类不明）；⑦ `recovery_authorization_invalid`（T7–T9 组授权或角色槽不匹配、过期、撤销、重放）；⑧ `recovery_budget_exhausted`（T1/T7–T9）；⑨ `identity_mismatch`；⑩ `execution_not_fenced`；⑪ `side_effects_unknown`（含幂等不保证）；⑫ `approval_required`（含人工 gate）；⑬ `artifact_invalid`（身份/哈希/正向报告/测试不满足）；⑭ `parallel_limit`；⑮ `policy_forbidden`（effect/autonomy/静默期/取消确认）；通过为 `ok`。只检查该事件适用的条件，先后顺序不构成安全豁免；不可用与不匹配均拒绝。诊断只显示所有缺项及首个适用码。

| 消费者（拟议） | operation / target / decision 及拒绝条件 |
|---|---|
| dispatch | dispatch / role、effect、槽 / 无；T1/T8，状态、组、槽、预算、身份、隔离、幂等、审批、并发、策略失败拒绝；锁外和锁内同函数。 |
| collect | collect / dispatch / 无；当前按 T3–T5，历史已决按 T18、历史未决按 T18 的收窄规则只读留证或升级；身份、快照或证据不可信拒绝迁移；终态只留证。 |
| abandon | abandon / 目标 dispatch、所属故障组 / 原因及确认；T6，以 §五文件、presence、隔离和授权证据作为 context，锁外预检与锁内复检；有效报告或缺证据拒绝。 |
| recover-attempt | recover-attempt / 目标故障组及其成员、必要角色 / 无；T7，context 包含预算、身份、隔离、幂等及授权；锁外预检与锁内复检并原子发槽，失败不投递。 |
| diagnose | diagnose / Run、目标 dispatch 或组 / 无；T20，只读分类及安全留证，不修改账本；终态仍可诊断，不把只读结果当写许可。 |
| 异步收尾 | async-finalize / 原 dispatch、attempt、组授权及角色槽 / no-wait、wait 或异常；分别依 T2、T3–T4 或 T19 检查，锁内重取快照并调用同函数后 CAS；异常仅在仍有资格时记录诊断，不得复活或覆盖终态。 |
| gate | gate / 当前组及全部角色 / pass、revise、block；T10–T14，组未闭合、未决、工件/哈希、人工认证或状态不满足拒绝。 |
| drive | drive / 计划动作及目标 / 对应决策；进入及每次循环检查，再由实际 dispatch/collect/gate 重检；无合法槽或人工 gate 停止，不创建授权。 |
| finish | finish / 全历史闭合证明 / 无；T16，非允许状态、历史未闭合、未决、gate/副作用不足拒绝。 |
| list | list / 全 Run / 无；T20，只读可展示终态及所有诊断，不因写守卫失败隐藏记录；坏账本返回诊断而非虚构状态。 |
| recover | recover / Run 和组 / 无；T20，只读提供分类和首个原因，不创建授权、不重派。recover-attempt 另以目标故障组调用 T7。 |
| cancel | cancel / 全部历史及 Worker / 确认和原因；T17，未决或确认不足拒绝，终态拒绝；B 类另有身份、隔离、副作用要求，A 类（从未派单）不适用执行者维度。 |

统一判断顺序和同一状态快照取代各入口自造 pending 谓词；具体动作仍只能执行表中行，不能把只读 `ok` 当写授权。start 现状 `scripts/htm.py:969-992` 仅跳过 finished/cancelled；拟议保持其他状态占用 Worker。pause/resume 另按 T15 调同函数。

## 五、abandon 前置校验与受控恢复（B1/B2）

以下命令均为待评审的拟议接口，当前 CLI 不支持，不可现在执行。三个步骤分开：`run diagnose --run-id ... --dispatch-id ...` 收集留证（不改 Dispatch 状态）；`run abandon --run-id ... --dispatch-id ... --reason ... --evidence <snapshot-id> [--quarantine-invalid] [--confirm-untracked]` 终结结果资格；`run recover-attempt --run-id ... --dispatch-id ... --evidence <snapshot-id>` 保留新 attempt，之后仍通过正规 `run dispatch` 投递。任何步骤不伪造 Worker 成功报告。

### 5.1 自动收集和诊断

诊断将原派单 prompt、身份 pin、stage/index/attempt、账本相关事件、结果路径及存在性/解析/字段校验结果、带时间的 Agent/pane 状态和可获得的执行日志、可信退出回执、Git HEAD/status/diff（含未跟踪文件清单及相关文件哈希）保存为不可覆盖的证据快照。Git 使用 `clean_git_env()` 与明确 worktree；不修改 Git index，不 reset/clean、不执行日志中的指令。日志可能含凭证，需本地最小权限保存和脱敏，不将敏感原文写入错误消息或上传。采集前后 HEAD、文件哈希或身份变化、日志截断/不可用必须显式标记，不将不一致快照用于自动恢复。

输出四类诊断：`completed_report_lost`（可信执行回执和独立验证证明完成，但没有合格报告，仍不得 gate pass）、`partial_execution`（可验证的部分修改或副作用）、`not_executed`（可信执行控制面证明未开始，而非仅 diff 为空）、`undetermined`（其余及证据矛盾）。每项附来源、时间、SHA-256、缺失项、可信程度、允许动作和升级原因。终端旧文本、API 503、Agent done、无 diff 均不能单独证明完成/未执行；没有可信回执时保持 undetermined，而不是根据错误字符串猜测。三类历史/当前 collect 边界（拟议）：(a) 当前 attempt 的 blocked(无有效报告) 经身份和报告校验后，可走正常 collect（T3/T4）；(b) 历史 attempt 的 blocked(无有效报告) 仅经 T5 锁内重检分流，有效报告转 blocked(有效) 并固定哈希，无有效报告转 uncertain，不走当前 collect；(c) 已转历史 uncertain 后出现的有效迟到报告只能留证并升级，不得 collect 成 settled 或 blocked(有效)，不得成为当前 gate 证据。可信有效报告仅在旧 Dispatch 尚未 abandoned、仍是当前 attempt 且身份可核验时可以正常 collect；缺失报告只能通过受控新验证/重做形成新报告，不能由 Lead 补写 pass。

### 5.2 工件存在但无效的受控路径（修补 B1）

前置明确覆盖 `submission_uncertain`、`submitted_unsettled`、`uncertain`，包括依第四节从无有效报告的历史 blocked 安全迁入 uncertain 的记录；并要求无有效已收集报告，blocked 的业务报告不能借此抹除。present 必须匹配所有已有身份 pin 且 idle/done/blocked；confirmed_absent 要两项 not-found 证据及显式确认；unavailable 拒绝。上述仅为可观测性检查，仍须第二节可信停止/隔离依据及已有权限检查。超时按带时区时间核对，created_at 无效/未来拒绝；若可取得实际提交时间，静默期从更晚的提交时间计算，不能把入账到投递之间的延迟当静默期。超时不是安全证明。

锁内对绑定路径使用不跟随符号链接的检查，分支处理：

- **当前 attempt 的有效、身份匹配的完整报告**：拒绝 abandon，建议正常 collect 并处理其 verdict；`fail/needs-work` verdict 或 `blocked` status 不能被伪装成传输故障。
- **历史 blocked(无有效报告) 的派单，工件路径出现有效报告**：按 T5 转 blocked(有效) 并固定哈希；这是正向分流，允许发生，但不得作为当前 attempt 的 gate 证据，也不改变当前 attempt 判定。该派单因此获得已决终态，不落入「既不能 collect 又不能 abandon」的死锁。
- **历史 uncertain（已由技术故障定性）的派单出现有效迟到报告**：只留证并升级，不得 collect 成 settled 或 blocked(有效)，不得写入 report/hash，不得作为当前 gate 证据；Dispatch 与历史状态不变（见 5.1 的 (c)）。T18 的前置状态列仅含 settled / blocked(有效) / abandoned，不覆盖历史 uncertain，故此处不引用 T18。
- **历史 abandoned 的派单出现任何迟到报告**：只留证，不得翻回 settled 或 blocked(有效)，不写 report/hash，不得作为当前 gate 证据（见 T18）。
- **路径当前缺失**：记录本次缺失，不再要求无法证明的「历史从未出现」。历史出现/删除记录仍保留并影响诊断；无法解释的变化阻止自动恢复，不靠删除文件获得资格。
- **可读、稳定的普通文件但 JSON 损坏、schema 或 Run/Dispatch/role/stage/attempt 不匹配**：只在显式 `--quarantine-invalid`、证据快照核对一致且执行已被隔离时，先留存原始字节，再 CAS abandon。快照须记录实际读到的身份（若能解析）和预期身份，不把文件移交给它自称的另一个 Run，也不改写其字段。
- **目录、设备、FIFO、符号链接（含悬空）、过大/不可读文件、读取过程中变化或路径逃逸**：不跟随、不递归、不覆盖，首版 fail closed，记录 lstat/错误并给外部安全隔离选项；不能以隔离证据失败来宣布恢复成功。

证据存放独立目录 `STATE/evidence/<run-id>/<dispatch-id>/<snapshot-id>/`，新目录独占创建，0700，原始文件与 manifest 0600。manifest 包含发现 UTC 时间、原路径、lstat 身份、字节数、SHA-256、错误分类、预期/实际绑定、采集范围及缺失项。普通文件以不跟随链接的 fd 读取并前后复核身份/大小/元数据，独占写证据文件、fsync、复读校验哈希；不 move/delete/覆盖原件，保留原路径作为证据。**隔离是取消原路径的结果资格 + 保存独立副本，不声称复制文件能隔离执行者。**新投递使用全新路径，原无效文件不会阻塞它。这样无效文件不再造成旧的「存在即永久拒绝」死锁，也不允许掩盖有效失败报告。

账本提交与复制不是跨文件原子事务：先落盘证据，再锁内复核目标、当前工件与快照一致，最后原子保存状态和审计引用。复制失败不改 Dispatch；CAS 失败只留下标明未提交的证据快照，不能追加成功事件。崩溃发生在复制后/提交前，重试复用已验证快照或另建快照；原文件仍在，不丢证据。提交后原路径又变化按迟到证据处理，不逆转终态。

### 5.3 轮数与恢复上下文（拟议）

现状 `scripts/htm.py:1274-1278` 普通 revise 同时增加 round 与 attempt，`:1551-1554` 禁止扩轮。技术恢复、混合组整体业务决策、角色槽、预算及 `recovery_active` 的唯一迁移规则见 §四 T7–T17。原 Run round=6/max_rounds=5 的越限来自业务 revise，不能推断为技术恢复，不操作旧账本。结构化纯技术越限的受限恢复不重写 round；仅 recovery_active 期间禁止普通 revise 和第二恢复链，必要 gate 与安全 cancel 仍须通过共享守卫。当前外部隔离与授权不可用时只能升级，不能声称自动恢复。恢复事件绑定整个故障组和各角色槽，非单一来源角色 reservation；`recover` 本身始终只读。

## 六、审计与不可否认性边界

锁内重新读取目标并 CAS 三类未决状态到 abandoned，状态更改和事件一起原子保存；并发两个请求仅一个成功，重复 abandon 返回既有结果或已处理错误，不追加成功事件。事件记录操作者（可观测本地身份/声明，注明来源，不冒充 Owner）、UTC 时间、原因、证据目录/manifest 哈希、presence 三态及底层 code/观测状态、预期/实际身份、原 stage/index/attempt、Dispatch ID；并记录可信隔离来源、诊断及不确定项、超时、工件分类、显式隔离确认和授权核验结果。恢复事件另记录 before/after round/attempt、逻辑根、累计投递数、剩余预算、reservation、原审批消费记录与新审批引用（如要求）。不恢复原审批 token，不把 recovery 当 approval。使用现有锁和原子保存机制（`scripts/htm.py:280-311`），不将成功日志单独提交造成状态与审计分裂；任何安全检查失败都返回稳定原因码和可选动作，不能静默无限重试。

同用户可写账本时，审计只能说明「操作被记录」，不能证明操作者获独立授权，也不能防同用户篡改；真正授权边界需 OS 用户隔离或外部审批，本设计不声称提供。`scripts/htm.py:1020-1023,1032-1037` 亦明确本地确认和自报不构成独立授权或签名。

**人工 gate 证据契约（拟议，外部依赖）**：现状 `scripts/htm.py:1262-1270` 的 Lead gate 仅检查 evidence 至少 12 字符，不认证人或授权；不能当作人工参与证明。拟议仅对下述明确分类为人工的 gate，其每次 `pass`、`revise`、`block` 均要求独立人工认证服务出具可验证、不可由同用户 Lead/drive 伪造的审批记录；Lead 或 drive 禁止自动填充、生成或代替人工审批。至少记录认证人及身份来源、签发时间与有效期、决策、批准范围、run_id、stage/index、stage_attempt，以及被审查 dispatch ID 集合和各已收集报告哈希（无 dispatch 时显式空集合与原因）。校验身份、授权角色、签发者、完整绑定、时效、撤销及一次性使用；不可核验、缺字段、过期、撤销、重放、范围不符均拒绝并不写 gate 事件。锁内复核当前 attempt、集合与哈希，恢复后的 attempt 必须重新获得绑定新 attempt 的人工证据，旧审批不可继承。

适用范围（拟议）：stage 为 `accept`、actor 为 lead、stage 显式 `requires_human`，或当前 attempt 涉及 sensitive effect / 恢复策略要求人工复核时，该 gate 的全部决策均为人工；其余非 lead、非 sensitive、未标记且不要求恢复人工复核的 gate 为非人工。标记和恢复策略须在可信配置/受控迁移中确定，调用者不能以参数降级。`run_gate` 以内部受限调用路径区分 drive 自动调用与人工调用，而非信任可伪造的 CLI 声明：自动路径仅允许非人工 gate，按 `:1491-1503` 从当前已收集绑定工件的 verdict 推导 pass/revise，并执行全部 pending、最新工件/哈希和测试守卫；无工件禁止自动 pass。人工路径无论由谁调用，只凭有效独立认证记录而非调用者自称判断；drive 遇人工 gate 停止并提示认证，不能代签或绕过。非人工 gate 缺人工认证仍可正常自动推进；无外部依赖不影响其既有协调。

逐类范围：① Lead 阶段的人工验收 gate，证据须明确批准当前阶段及其 dispatch/工件和决策；② 恢复后要求人工复核的 gate，须同时绑定恢复源 dispatch、故障组授权、对应角色槽与新 attempt，明确对哪些副作用和恢复结果作何决策；③ 涉及 sensitive、every-step、changes 或超权限的独立 Owner 授权，必须另有绑定目标 role/effect、动作及范围的授权记录，不能用 gate 记录替代 dispatch 授权，也不能用旧 token 替代新 attempt 的人工 gate。若同一操作跨类别，须分别满足全部类别。外部认证服务的生产可信根和实现尚不存在，首版上述人工 gate 不可执行，只能升级；假服务只用于测试守卫，不构成真实授权。

## 七、迟到产物结构性拒绝

abandoned 不进入 reconcile 集合，锁内复查状态，任何后到的旧 Dispatch 工件都不得令账本变 settled、覆盖审计或成为新 attempt 的 gate 证据。collect、diagnose 和恢复检查发现迟到普通文件时按 5.2 留存新快照（同内容可去重但不得覆盖历史），明确 `late_artifact_rejected`；复制失败报错并保持 abandoned，保留原件。非 abandoned 且仍为当前 attempt 的可信有效迟到报告可以正常 collect；collect 先赢 CAS 则 abandon 拒绝，abandon 先赢则报告只能留证。这两条不可混同为「迟到报告永不接收」。

旧 attempt 的已有 settled 报告及 gate 哈希保留为历史审计，不迁给新 attempt；新 attempt 只能使用新 Dispatch 的绑定报告。异步 dispatch 的 wait 成功、no-wait 返回、异常收尾（`scripts/htm.py:1175-1198`）均锁内确认 Dispatch 非终态、attempt/故障组授权及角色槽未变化，避免迟到返回覆写 abandoned、错误地继续新 stage 或占用第二槽。

下游幂等键绑定逻辑动作而非随机 Dispatch ID：恢复重做沿用同一动作键，存储目标回执供独立核对；不同参数却同键、无下游去重保证或未盘点副作用就禁止自主重派。Git diff 不能证明没有网络/数据库副作用；只隔离报告也不能防旧进程继续写仓库。对部分执行，先在可信停止/隔离后保存原 diff、列出已生效项，再派受限修复/验证任务，禁止自动 reset/clean 或从头重播不可幂等动作。测试须同时断言留证完整、原件仍在、状态不可逆及 gate 不被旧工件影响。

## 八、Team/Agent 生命周期分层

**已验证事实**：`verify_existing` 校验 pane、kind、已登记 session value，并仅在返回 name、cwd 或 Hermes profile 时分别校验它们（`scripts/htm.py:481-508`）；drive 额外校验 session kind/source/value（`scripts/htm.py:1295-1309`）。adopt 只接受当前 Lead 尚未登记的全新 Team（`:770-776`）；init 对现有 Team 比较配置/cwd/worktree 并核验角色后复用（`:669-685`）；cancel 只标记 Run cancelled 并写事件，不释放 Team（`:3378-3464`）。

**设计判断**：需要正式 team release 原语来解决生命周期退出及后续 Session Rotation 的登记边界，但不将其塞入第一阶段，也不把 cancel 等同 release。当前源码没有删除 Team 文件的路径，不能宣称已有 release。release 的授权、资源所有权、未决 Run、外部 Session、审计和崩溃恢复须另行设计评审；不得建议人工删除状态文件来绕过。

分层优于现在就建立统一大型生命周期框架：第一层提供只读 presence/identity 观察及证据类型，供 uncertain 恢复和未来 rotation 共用；第二层分别由 abandon、init/adopt、未来 release/rotation 实施各自状态迁移和安全约束。先复用观察结果而非统一迁移流程，避免 speculative 抽象掩盖不同授权及资源所有权；rotation 不能仅因 confirmed_absent 而视为安全停止或更换 Session。

## 九、用例级回归测试设计

以下为拟议用例，不是已执行结果。使用隔离临时 STATE、临时仓库、模拟 Herdr/时钟及可验证的假执行控制面回执，不接触真实 Team。失败断言 Dispatch/Run 不变、无成功审计、无 prompt；允许存在标明未提交的证据快照。

1. 参数化三种 uncertain 状态，可信隔离、身份/权限及静默期满足、文件缺失：abandon 成功且仅一条事件，不自动派单；无可信隔离则拒绝。
2. 当前 attempt 的有效完整报告（verdict 为 pass/fail/needs-work，status 为 complete/blocked）均拒绝 abandon，原件保留；只有当前 attempt 的正常 collect/verdict 流程可处理，历史有效迟到报告按 T18 留证升级。
3. 损坏 JSON、错误 run_id/dispatch_id/role/stage/stage_attempt、无效 schema 的稳定普通文件：无显式确认拒绝；确认且其他安全条件满足则原字节留存、哈希一致、原件仍在、CAS abandoned，不修改/借用身份。
4. 目录、FIFO、设备、符号链接/悬空链接、过大/不可读、文件替换、路径逃逸：不跟随不覆盖，明确失败原因；隔离失败、磁盘满、fsync 错误不得改 Dispatch。
5. 证据复制后/账本提交前崩溃，重启不丢原件/副本；CAS 冲突只留未提交快照，无重复成功事件；证据目录撞名不覆盖。
6. abandoned 后写入合法旧 pass 再 collect/drive，留存迟到快照而不翻转、不设 report/hash、不影响 gate；Agent 消失也能只读返回该终态；证据复制失败不逆转。
7. 同步屏障并发 abandon 两次、collect/abandon、wait/no-wait/异常收尾与 abandon：逐一控制两种顺序，仅一个终结者；无终态覆写、无双投递。
8. actor 或必要 remediation 最新记录 abandoned：pass 拒绝，不回退早先 pass；新 attempt 重做所有必要角色后才可通过，保留旧 gate 哈希为历史。
9. presence 参数化：绑定有效 present；两项 not-found 才 confirmed_absent；pane 仍在、超时、API 503、无效 JSON、缺 session/身份漂移均 unavailable；stderr/stdout JSON code 判定而非仅 exit code。
10. confirmed_absent 无显式确认或无可信停止回执均拒绝；cancel 无可信停止/隔离不释放预留；缺可用 Worker 的恢复提示人工处置而非 rotation。
11. 未到静默期、带时区无效/未来时间、提交时间晚于 created_at、present working/unknown：拒绝，账本和事件不变。
12. round==max_rounds、原投递 uncertain 后安全 abandon：recover-attempt 保持 round/max_rounds，递增 attempt，第二次投递受累计 retry 上限；恢复再次故障后预算耗尽，不允许第三次投递。
13. blocked_round_limit：仅结构化、纯技术越限且预算未尽可进入有限 recovery_active；旧 schema、业务 needs-work/fail、原因未知、预算耗尽均拒绝；保留原 round/max_rounds 和历史事件。
14. 两个 recover-attempt 并发及重入：最多一个组授权、每角色一个槽及一个 attempt 增量；dispatch 之前 attempt 改变必须 CAS 拒绝；保留槽后崩溃可续用，不重复送 prompt。
15. recovery_active 下 dispatch/collect/gate/finish/cancel 按同一恢复上下文正常受控运行；普通 revise、第二恢复链、额外派单、绕过 effect/autonomy/approval/parallel/retry 均拒绝。
16. 审批原 token 不返还；read-only 禁写、sensitive/every-step/changes 仍要求原有审批，伪造同用户 TTY、reason 或证据文件不获得 Owner 权限。
17. 旧 attempt/abandoned 的有效迟到报告不能通过新 gate；当前非 abandoned 报告正常迟到可 collect；已收集报告被修改仍触发哈希失败，自测缺 exit_code=0 仍拒绝。
18. list/recover 展示 pending、abandoned、诊断与剩余预算，recover 不修改状态/重派；finish 缺任何必需 gate 均拒绝，cancel 不删除 Team/资源。
19. API 503/auth_unavailable/model_not_found 注入四类场景：可信证明未执行、可验证部分修改、代码已完成但 JSON 丢失、证据不足；分别核对诊断分类、不假设单一故障原因，服务恢复不自动补 pass 或重投。
20. Git diff 为空/日志截断/证据矛盾仍 undetermined；部分执行保留 diff、未跟踪文件清单及哈希，不 reset/clean；网络副作用未知则升级。
21. 稳定动作幂等键由下游去重、恢复沿用同键；无去重或同键不同参数拒绝，累计投递有限；模拟副作用断言最多一次而非只数 prompt。
22. drive 只有证据、授权、可信隔离、身份、幂等与预算齐全才自主恢复；任何缺失输出稳定原因与动作，不死循环、不静默扩额、不无解释卡住。
23. 真实隔离环境执行下方四行验收矩阵，分别记录真实 503、外部可信证明与续跑证据；不得以注入或 Driver SIGKILL 代替。外部依赖未实现时四行真实自动恢复均未验收。
24. 顺序测试：先 collect 观测 blocked，分别对缺失、损坏、schema/身份不符报告核对 uncertain 与诊断；再在测试假隔离/授权成立时 abandon，原工件与证据保留。当前 attempt 的 blocked(无有效报告) 可正常 collect；历史 blocked(无有效报告) 经 T5 锁内重检，有效报告转 blocked(有效) 并固定哈希，无有效报告转 uncertain；有效报告存在时不得迁移/abandon，按当前/历史身份分别走当前 collect 或 T18 留证升级；哈希不符升级。
25. 并发测试：blocked collect、旧 blocked 迁移、abandon 与有效报告发布设置屏障覆盖先后顺序；逐次核对锁内重读/CAS、仅一条成功事件，先接纳有效报告则拒绝 abandon，先 abandon 则迟到报告仅留证；路径变化或证据冲突 fail closed。
26. 自动化冒充人工：Lead/drive 生成长 evidence 文本、伪造同用户审批文件或调用 gate 冒充人，均拒绝，不写 gate 事件；认证服务不可用也拒绝。
27. 人工 gate 绑定：对各类 gate 测缺认证人/时间/范围/dispatch、错误 run/stage/attempt/哈希、旧 attempt 证据、过期撤销重放和授权角色错误；均拒绝；隔离测试假服务签发正确记录才可通过守卫，不等于真实认证。
28. 真实 503 未执行场景按矩阵留证并核查幂等及续跑。
29. 真实 503 部分执行场景按矩阵留证并核查幂等及续跑。
30. 真实 503 已改代码但 JSON 丢失场景按矩阵留证并核查幂等及续跑。
31. 真实服务恢复后续跑场景按矩阵留证并核查幂等及续跑。
32. 有效 blocked 收集后 gate revise，后续 attempt 和各阶段全部 pass，最终 finish 成功；当前 blocked 未 revise 时 finish/阶段推进拒绝，历史记录不被改写。
33. revise 后 list/recover 不将历史 blocked 列为 pending 或建议 collect，仍可查看历史；当前 attempt blocked 仍列 pending；终点哨兵不得掩盖异常未决历史。
34. abandon 后同 attempt 无组授权角色槽的普通 dispatch 与 drive 派单均拒绝、不发 prompt，drive 不创建组授权或角色槽。
35. 隔离测试中合法故障组授权完整绑定故障组、逻辑根、角色集合与余额，各角色槽在锁内验证后各成功占用一次；崩溃续用同一槽不重复发送。
36. 伪造、过期、撤销、已消费、跨 Run/stage/attempt、故障集合或逻辑根不符、预算变化的组授权或角色槽 均拒绝，事件与 prompt 不增加。
37. drive 在非人工 worker gate 无人工认证时以当前绑定 pass 工件及哈希自动推进；无工件、旧/变更工件不能自动 pass。
38. accept、lead、requires_human、sensitive 及要求人工复核的恢复 gate，分别对 pass/revise/block 缺认证 fail closed；drive 停止升级，不写 gate，正确绑定假认证仅验证测试守卫。
39. lead 阶段先有 pass remediation 后最新 remediation abandoned，人工认证齐全也不能 pass；在恢复 attempt 对必要角色取得新 settled/pass 并匹配哈希后才可 pass；无派单 lead 仍需人工认证。
40. recovery_active 分别测试 dispatch 锁内外、gate、drive 进入/循环、finish 的允许与限制；start 不得重用占用 Worker，pause/resume 拒绝，cancel 保持安全守卫。
41. presence 对 cwd 改变、缺失/无法解析及 Hermes profile 改变或缺失均 unavailable；匹配绑定可 present，非 Hermes 不强制 profile。
42. R1：developer remediation 已 pass、reviewer abandoned；故障组授权分别产生 developer/reviewer 槽，两者各计自身逻辑根预算，分别新 dispatch/collect 后 gate pass；任一角色余额不足时组不创建新 attempt，升级且不发 prompt。并发重入及角色槽重放不能双发。
43. R2：同 attempt 有有效 blocked 和 abandoned；直接 revise、drive 自动 revise、跨 attempt 洗预算均拒绝，组成员及旧哈希不变；组整体恢复全部必要角色后，新报告业务否定才可 T13 整体 revise，并验证原隔离、幂等、审批和预算未被跳过。
44. 混合组故障新成员、提交未知、迟到有效报告、并发封组与 revise 分别设置屏障；CAS 冲突不推进，未知送达不得封组，关闭后后续普通 attempt 不被历史 abandoned 无限约束，历史闭合证明缺失则 finish 拒绝。
45. 所有消费者按共享守卫同一快照注入多重错误，验证首个原因码及锁内外一致；只读 list/recover/diagnose 不改变账本，abandon、recover-attempt 与异步收尾按消费者表映射调用同一守卫；终态异步回调拒绝，缺外部证据的取消与恢复均 fail closed。
46. 拟议 F1：外部技术越限初始态经 T7/T8/T3/T10，验证组闭合、预算结清、round 不变且转 active；后续非 lead 阶段经 T1 正规派单及 T11，最终 T16；缺任何 T10 证明时不转 active。
47. 拟议 F2：身份可核验时穷举 idle/done、blocked、unknown/其他观察与有效 complete/blocked 或缺失/无效报告，验证互斥 T3/T4；unknown 即使报告有效也只得 uncertain，身份不可核验不迁移。
48. 拟议 F3：当前 blocked(无有效报告) 走 T3/T4 正常 collect；历史 blocked(无有效报告) 经 T5 重检，有效报告转 blocked(有效) 并固定哈希，无有效报告转 uncertain、不走当前 collect。已转历史 uncertain 后的有效迟到报告调用 collect 只能留证并升级；断言 collect 不产生 settled 或 blocked(有效)、不写 report/hash、不进入当前 gate 证据，且 Dispatch 状态不变。有效报告存在时 T6 拒绝，无报告且证据齐全才可 T6，否则升级。
49. 拟议 F4：打开的无 abandoned 组全部收成有效报告后，未 T21 时 T11/T12 拒绝；T21 关闭并留证后，正向可 T11、有效业务否定可 T12，未开组的业务否定仍可直接 T12。
50. 拟议 F5：active 恢复再次故障后，T6 及 T7 全条件、预算满足才可再开 attempt；recovery_active 下 T7 拒绝并升级或安全 T17。T12/T13 业务越限不得冒充技术越限；仅人工/外部结构化迁移初始态且证据满足时允许 T7。

**503 验收矩阵（拟议）**：四行分别建隔离 Run/动作键；真实服务故障必须有服务端/网关时间戳及请求 ID 对应的真实 HTTP 503 证据，不能只靠客户端错误文本。独立执行控制面和授权服务的可信证据、下游真实去重能力均为先决外部依赖；任一不可用则相关真实自动恢复行标记「未验收」，只可人工升级，不得修改生产或伪造回执。证据含 Run/Dispatch/attempt、时间线、工件与哈希、前后副作用计数及验证者；保留脱敏日志。

| 场景 | 前置条件 | 可观测证据与判定标准 | 续跑行为 | 副作用幂等断言 |
|---|---|---|---|---|
| 未执行 | 真实 503 在任务开始前发生，独立控制面可证明未启动且已隔离 | 请求/503 与 dispatch 关联，可信未启动记录、原状态快照和缺失报告一致；仅无 diff 不算 | 授权、预算与隔离满足后同动作键恢复一次，产生新 attempt 正规报告 | 下游动作最终恰一次，恢复前为零；重试不重复 |
| 部分执行 | 真实 503 前仅部分动作已提交且旧执行者已隔离 | 控制面时间线、持久副作用回执、前后 diff/未跟踪文件和缺失报告共同证明部分执行 | 留存差异，授权后限定补做/验证，不自动 reset 或盲目重播 | 已完成部分不重复，剩余动作各恰一次；无去重则升级且不续投 |
| 代码已改但 JSON 丢失 | 真实 503 前代码修改已落盘而绑定报告未产生或丢失 | 修改前后哈希/diff、可信执行记录、报告路径状态及 503 时间线一致；不能假定已完成 | 保存原改动，独立验证并经授权派新验证任务形成新绑定报告，不补写旧 pass | 既有写入不重复；任何外部动作凭回执核对最多一次 |
| 服务恢复后续跑 | 上述故障之一已留证，服务真实恢复，隔离/审批/预算与去重均可核验 | 恢复前后健康及成功请求、账本事件、attempt/新报告和 gate 证据完整；不因服务恢复自动重发 | 受控 recover-attempt 后正规 dispatch/collect/gate；人工 gate 仍需独立审批 | 同逻辑动作键全链路最多一次，检查目标系统实际状态而非只计 prompt |

**两档结论（拟议）**：用例 19 的注入仅证明模拟下分类和守卫，测试假回执不得视为可运行的真实闭环；矩阵四行必须各有真实证据才可分别标记通过。任一缺真实 503、可信外部依赖或实际副作用证据即该行未验收；四行不全通过不得称真实 503 全部验收通过。本轮没有执行这些用例。

第二轮新增 24–31，第三轮新增 32–41；第四轮新增 42–45，改写 14、24、34–36 以匹配组授权及角色槽；第五轮拟议修改 §四 T3、T4、T7、T9、T10、T17、T18 及消费者映射，澄清 T21 次序，修改用例 45 并新增 46–50；本轮拟议补充 §五 5.1、改写用例 2、24、48 以统一 F3 当前/历史边界和迟到报告拒绝断言，并限定 T21 后续决策；未删除或合并用例，编号 1–50 连续，8、15、18、22 依 §四解释，23 仍为矩阵总验收，19 仍只属于注入档。

## 十、分阶段实施与独立验证

1. **设计修订先独立 Review**：新 dev-lite Team 的 Lead 是新 Claude session，Developer/Reviewer 各一，不复用旧 Team。模板没有 plan_review，因此在 plan_brief 阶段通过正规 `run dispatch --remediation` 先派 Developer 修订设计，再派独立 Reviewer 只读审查并写绑定 JSON；Lead 核对全部已结算、Reviewer 最新 pass、文件与报告哈希后才允许 plan_brief gate pass。该附加约束由 Lead 协议执行，模板原生 gate 不会替 Lead 推理；不能因模板较轻跳过审查。设计不通过只返工设计，不开始实现。
2. **核心实现与自测（拟议）**：依第四、五、七节实现三态、证据诊断、CAS abandon、有限恢复 attempt 和全部消费者；不建通用授权服务或 rotation/release 框架。Developer implement/self_test；隔离测试的假控制面只验证守卫，不代表真实可信隔离或授权。首版验收必须明确：真实自动恢复和依赖人工认证的 gate 不可用，只能人工升级；真实外部依赖及第九节四行未验证时记录未验收。全量 `uv run pytest -q` exit 0，旧 204 passed / 116 subtests 不减且新增回归通过；先实际复测基线，变化如实说明。
3. **最终独立代码 Review 和验收**：Reviewer 检查实际 diff、运行新增安全负例/全量回归并提供绑定 code_review JSON，重点检查跨 attempt 预算、审批、哈希 gate、异步竞态与证据故障。所有阻塞修复并复审 pass 后，Lead 独立核查日志、范围与未测边界，accept gate 后 `run finish` 返回 finished=true 才可称流程完成。真实 API 503 E2E 未跑就不得称全部验收通过。

预计无返工最低五次派单：设计修订、设计独立 Review、实现、自测、最终 Review；安全模块可拆为多个 implement 子任务，预计总计 5–9 次。业务 round 初始 1，预留最多 2 次普通 revise（仍受 Team 固定 max_rounds=5），不是预先扩额；发生 uncertain 时不得用多派单替代恢复。当前外部调用者没有合规 Herdr context，未创建新 Team/Run，因此上述仅执行方案，不能声称已获得独立评审。

## 十一、文档一致性

`SKILL.md:49` 声称 `run cancel` 在所有 Worker 已停止工作时「释放 Team」；实际 `scripts/htm.py:3378-3464` 只写入 Run cancelled 并记录 `cancel_scope`，并不删除或释放 Team；本设计现已要求有执行历史的 Run 取得可信停止/隔离证明（无执行历史的 Run 不存在该义务），但可观察状态本身仍不能证明后台任务已停止。现有文字不一致。本轮唯一交付为本设计文档，因此不修改 `SKILL.md`；第一阶段应另以文档改动更正其说法，明确 cancel 不释放 Team、观察状态不证明停止，且与实现一起验收。

## 十二、方案对比

- **abandon 与恢复分两步**（选定）：先留证并终结结果资格，再 recover-attempt、正规 dispatch；不将终结当作成功/审批/自动重投。比单步操作多一次事务，但崩溃与审计边界清楚。
- **有效/无效工件分支**：不再「存在即拒绝」；有效报告正常处理，无效稳定普通文件可显式留证后 abandon。复制保留原件优于移动后跨文件回滚；特殊文件和竞态保持失败关闭。
- **恢复不增加业务 round，但累计 retry 有限**：相比恢复必 revise，可在 round==max_rounds 继续技术重做；相比无上限豁免，沿逻辑根累计次数不退款。真实业务负面 Review 仍耗 round，不因故障抹除。
- **超时自动降级**（不选）：便利，但时间到不证明任务停止或无迟到产物，会放大重复副作用；超时仅是人工 abandon 的必要条件而非充分条件。
- **统一生命周期大抽象 vs 分层**：选第八节轻量共享观察层加独立迁移；前者可能复用更多代码，却提前耦合 release、rotation 和恢复的不同行为及授权边界，当前证据不足。
- **接受残余风险 vs 可信停止/隔离**：删除旧稿以前者替代安全条件的决策。没有独立可核实隔离、权限或副作用依据时明确无法自动恢复；不以同用户声明、Agent done 或新随机幂等键伪装绝对安全。
