# Issue #6：真实非默认 Herdr Session 隔离验收计划

## 范围、前置条件与风险边界

本计划尚未执行。目标是在真实 Happier Lead 与两个同时运行的 Herdr session 中验证 Lead 侧身份及路由；不把模拟单测当作真实验收。使用本分支及独立、无敏感数据的测试 Git 仓库、独立测试 workspace 和 Pane，记录版本、命令、退出码、session 名及资源 ID。使用独立任务名称，不复用现有资源。

**需要同时隔离三类落盘路径**：

| 隔离项 | 默认值 | 违约后果 |
|---|---|---|
| `HTM_STATE_DIR` | `~/.local/state/herdr-team`（`scripts/htm.py:35`） | 会读写既有 team 账本 |
| `HTM_WORKTREE_DIR` | `~/.local/share/herdr-team/worktrees`（`scripts/htm.py:755`） | `--task` 会写入共享 worktree 根；`config/team.yaml:7` 的 development 模板 `execution.worktree: "auto"` 会自动触发 |
| Git 仓库 | 本项目 | 污染被测代码 |

执行前确认两个目录均为空且不指向默认路径。**验收统一设置 `HTM_STATE_DIR=<临时目录>` 与 `HTM_WORKTREE_DIR=<临时目录>`**；若某个用例改用 `--task`/`--worktree`，必须显式指定已隔离的 worktree 根，否则改用不带 `--task` 的初始化路径并记录该取舍。

严禁读写或改变 Run `1d0debfb70e0` 的任何状态或 artifact，严禁操作 `wH`、`wS` 与既有 team 账本。默认 session 仅允许只读查询和操作明确新建的测试 workspace，不停止、不删除 default session。任何目标不明确、存在非测试资源或出现意外 Run 时立即停止并报告 Owner，不自行修复、伪造身份或强制清理。

[源码] `state_file` 不含 session。**双 session ID 冲突是大概率/近乎必然的结果，不是边缘情况**：Herdr 官方文档明确 "IDs and live agent names are scoped to one server. Two saved SSH machines can both have w1:p1"，即不同 server 的 ID 空间独立，两边都可能出现 `w1`/`p1` 这类相同 ID。而 `scripts/htm.py:425-429` 对**所有 running session** 做 `(workspace_id, pane_id)` / `(workspace_id, tab_id)` 全局唯一性检查，冲突即 `raise Error`——因此第二个 session 一旦启动并出现同键，**两边所有需要 pane 身份的命令都会 fail closed**（含 default 侧，不只是测试侧）。

因此执行第一步必须是**只读核对**：对每个 running session 执行 `herdr --session <名> pane list --json`，取全部 `(workspace_id, pane_id)` 与 `(workspace_id, tab_id)` 求交集。

- 交集为空 → 按本计划继续。
- 交集非空 → **本批用例无法执行**。不要尝试掩盖或绕过，必须停下并按候选路径表向 Owner 提出决策，例如：换用 ID 空间不同的机器（`--machine` 走远端）、由 Owner 决定是否临时停止 default 中相关 workspace，或重新规划测试 workspace 布局。**不得伪造通过。**

## 第二个 session 的候选路径（均需 Owner 确认）

已知此前本机仅 default running，socket 为 `~/.config/herdr/herdr.sock`；在嵌套 Herdr 内执行 `herdr session attach <name>` 曾被 `nested herdr is disabled by default` 拒绝。执行前重新只读核对，不能假定现状未变。

| 路径 | 可行性与隔离程度 | 风险 |
|---|---|---|
| 独立的非 Herdr 管理终端执行 `herdr session attach <测试名>` | 候选首选；预计可启动或连接同机第二个 server，须实测 `session list` 的 running 与 socket。server 独立但 OS 用户、全局 session 表及默认状态目录仍共享。 | 测试名可能已存在；先确认名称不存在，防止接管旧 session。**ID 冲突近乎必然**（见范围节）；必须同时隔离 `HTM_STATE_DIR` 与 `HTM_WORKTREE_DIR`。 |
| 调整 Herdr 配置启用 nested | 可行性和确切配置项尚未确认；仍需验证独立 server/socket。 | 修改全局配置可能影响现有 session；须 Owner 单独批准具体变更、记录原值并约定恢复；不为测试贸然改配置。 |
| 另一台机器使用 `--machine` | 具体版本的机器配置、session 列表与路由语义尚待确认；可获得机器级资源隔离，但本地父链不能证明远端 Pane，命令须实际在目标机器的 Pane 内执行。 | 远程连接、权限、账本路径和清理范围不同；不能把远程 client 发出的命令当作 Pane 内执行，须 Owner 批准机器及网络范围。 |

## 验收用例矩阵

所有用例先确认实际调用进程、测试目录与预期 session；`lead_identity()` 可在对应 Pane 内通过项目的 Python 环境导入 `scripts.htm` 只读调用，不打印敏感环境。保留输出与时间戳供复核。

**argv 观测方式（C 与 G 共用，需 Owner 单独批准）**

选定方案：**PATH 前置 herdr 包装脚本**。理由与要点：

- `scripts/htm.py:392` 的 `herdr()` 通过 `process([...])` 以裸名 `herdr` 调用，**没有使用 `HERDR_BIN_PATH`**（该变量只在 Herdr 自己的注入列表里出现）。因此把包装脚本放在 PATH 最前即可拦截全部 HTM 发起的 herdr 调用，无需改动任何生产代码或测试。
- 脚本内容要点：写成可执行 shell 脚本，第一行做**只追加的只读记录** `printf '%s\n' "$*" >> <临时目录>/herdr-argv.log`，然后 `exec <真实 herdr 绝对路径> "$@"`。**不得修改参数、不得吞掉退出码、不得回写任何环境变量**。真实 herdr 路径执行前用 `command -v herdr` 记录一次，避免包装脚本递归调用自己。
- 记录粒度足以判定断言：`$*` 保留了完整的参数顺序，可以直接检查 `--session <名>` 是否出现在子命令（`pane`/`agent`/`workspace`/`session`）**之前**，以及 session 名是否等于探测到的非默认名。这是本方案相对 `ps -o args=` 采样的优势：`ps` 是瞬时采样，秒级内的短命子进程容易漏采；包装脚本是同步拦截，不漏。
- 隔离与清理：日志写到本计划的临时目录，`chmod 600`；验收结束后删除包装脚本并从 PATH 移除，日志作为证据保留给 Owner 审核。
- 局限（必须如实记录）：包装脚本只能观测 **HTM 进程树内**的调用。若某命令在包装脚本未生效的子 shell 里运行，该次记录缺失——**缺失即视为未验证，不得推断为"没有 --session"**。

`ps -o args=` 采样与 DTrace 作为备选：前者实现最轻但可能漏采；后者需要 sudo 且规则复杂。**三者都需要 Owner 批准后执行**，不得在未批准时自行启用。

| 用例 | 前置 | 操作 | 期望结果 | 失败判定 |
|---|---|---|---|---|
| A 双 session 归属 | default 与新 session 同时 running，均有独立测试 Pane，且扫描无跨 session ID 冲突 | 各 Pane 内分别调用 `lead_identity()`，比较 `session` 和三项 context 与各自 `herdr --session <名> pane list` | 各自返回实际 session、workspace、tab、pane；两次探测一致 | 名称或 ID 错误、报错、命中其他 Pane；若 ID 冲突则记录阻塞而非通过 |
| B 派生路径 | 非默认 session 的 Happier Lead 位于测试 Pane，A 的冲突前置成立 | 在 Lead 的子进程环境移除所有 `HERDR_*`，调用 `lead_identity()` 和只读 `status`；核对其归属及真实输出 | 唯一定位到该 Pane、session 非默认，`status` 指向该测试账本；无账本时仅允许符合实际的未初始化结果 | 定位失败、归属错误、依赖注入或伪造 `HERDR_*`；尚无账本时不得误判 status 为成功 |
| C 显式路由 | 两个 session 的名字和 socket 已由 `herdr session list --json` 核对；PATH 前置的 herdr 包装脚本已由 Owner 批准启用 | 在两边各执行会触发 Herdr 管理查询的 HTM 命令（如 `status`、`pane list` 类只读调用）；从 `herdr-argv.log` 逐条核对实际子进程参数，与 session list 和返回结果比对；不得修改生产代码或伪造身份 | 日志中 `--session <对应名>` 出现在子命令之前，且名字等于该 Pane 所属 session；结果来自对应 server | 无 argv 证据、名字错误或访问另一 server；日志缺失该次调用则标未验证（不得推断为无 `--session`） |
| D 跨 session ID 冲突 | Owner 批准仅在两个全新测试 session 构造或自然出现相同 `(workspace_id, pane_id)` 或 `(workspace_id, tab_id)`；不改现有资源或伪造 API 数据 | 先用各 session 的 pane list 确认重复键，再在测试 Pane 调用 `lead_identity()` | fail closed，错误含 `跨 session Pane ID 冲突` 或 `跨 session Tab ID 冲突`；不产生账本或 Herdr 写入 | 成功返回身份或产生副作用；无法安全构造时标未验证，不用 mock 冒充真实验收 |
| E 非 Pane 环境 | 独立非 Herdr 管理终端，移除所有 `HERDR_*`，其他 session 正常运行 | 在该终端调用 `lead_identity()` 或 `status` | fail closed，提示无法定位 Pane；不写账本 | 返回任一 Pane 身份或产生写入 |
| F 账本键限制（不可验证项） | A 通过；两测试 Pane 的 context 与空的 `HTM_STATE_DIR` 已确认；已记录 `state_file` 键 = `sha256`（以 `workspace` 和 `lead_pane` 拼接为输入）取前 16 位（`scripts/htm.py:311-313`） | **单一口径**：先声明"不同键 → 文件分散"是哈希定义的同义反复、**不构成隔离证据**。然后只尝试同键场景：两测试 Pane 的 `(workspace, lead_pane)` 若同键，`init`（`scripts/htm.py:778-784`）会在 `lead_identity()` 阶段就撞上 425-429 的跨 session 检查并 fail closed，**永远走不到 `state_file()` 落盘**，因此同键账本在本分支下不可能被观察；若不同键，则如前所述不证明隔离。两条路都归结为同一结论：**本项不可验证** | **预期在 `lead_identity`/`state_file` 之前 fail closed**（错误含 `跨 session Pane ID 冲突` 或 `跨 session Tab ID 冲突`），因此无法产生同键账本；把结论写成"本项不可验证并附上拒绝对应的错误文案"，**不写成通过** | 把"不同键落到不同文件"当作隔离证据；把"init 被拒绝"当作"账本隔离已验证"；试图绕过冲突检查或删改既有账本去构造同键；无证据即宣称隔离通过 |
| G Run 生命周期与路由 | B 通过；非默认 session 的 Happier Lead 位于测试 Pane；`HTM_STATE_DIR`/`HTM_WORKTREE_DIR` 均已隔离；已有可用 team——按下方「team 来源」建立 | 在该 Pane 内执行 `run start --goal "<验收用目标>"`（**`run start` 只接受 `--goal`，没有 `--run-id` 参数**，`scripts/htm.py:1780`），从 stdout 的 `run_id` 字段取 ID（由 `uuid4().hex[:12]` 自动生成，`:1112`）；记录账本实际路径与摘要；`run list` 确认可见；再按下方「G 的完整命令序列」逐阶段推进并核对 `run dispatch` 的 argv 中的 `--session` | Run ID 生成且账本落在隔离 `HTM_STATE_DIR` 下；`run list` 能看到该 Run；`run dispatch` 的 herdr 调用 argv 含正确 `--session`；`run drive` 的结果与「先读 team 再定预期」的判断一致（有绑定则不得是 `identity_unbound`，无绑定才预期 `identity_unbound`）；**执行后该 Run 必须已 settle 至 `finished`** | Run 账本落在默认 `~/.local/state/herdr-team`；`run list` 读不到或在 default session 看到该 Run；argv 无 `--session` 或指向 default server；把 `identity_unbound` 当作必然结果、或把绑定成功当作通过；**执行后仍有未结算派单**（必须停止、保留证据、上报 Owner 并中止清理）；把 `run cancel` 的结果当作验收通过 |

**G 的 team 来源（F 不可验证的连锁处理）**：F 最终只能给出"本项不可验证"，因此 G 不能依赖 F 产出 team。可行路径是：在范围节的跨 session ID 只读交集核对**通过**（无交集）的前提下，用隔离的 `HTM_STATE_DIR` / `HTM_WORKTREE_DIR` 执行一次不带 `--task` 的 `init dev-lite --worktree off` 建立 team，再用它跑 G。若 `init` 本身也被跨 session ID 冲突阻断，则 G 前置不满足，**G 标注"前置不满足、未执行"并如实上报**，不得伪造、不得跳过冲突检查。

**G 的完整命令序列（不做单次 collect→finish 的假设）**

为什么不能"一次派单就收尾"：[源码] `config/team.yaml:14` 的 `plan_brief` 的 actor 是 **lead**，而 `dev-lite` 的 roles 只有 `developer` 与 `reviewer`；`scripts/htm.py:1221` 规定 `stage["actor"] != args.role and not args.remediation` 就报错（`:1217-1219` 是加载 run、校验 `status == "active"`、取 `stage_info`；`:1220` 取 `expected_index`；`:1221` 才是 actor 判断）。

关于模板选择：**dev-* 四个模板（`dev-lite`/`dev-review`/`dev-test`/`full`）的首阶段 actor 都是 lead**（`dev-lite`/`dev-test` 为 `plan_brief`，`dev-review`/`full` 为 `plan`）。但并非所有模板都如此——`research` 首阶段是 `investigate`（actor=researcher）、`audit` 首阶段是 `security_audit`（actor=security）、`hermes-guide` 首阶段是 `diagnose`（actor=operator），这三个的首阶段 actor 就是 worker。本计划仍选 `dev-lite` 并完整走 5 阶段，理由是：G 要验证的正是 dev 场景下的 Run 生命周期与 session 路由，而那三个模板分别需要 researcher/security/operator 角色与不同的治理配置（其中 `research`/`audit`/`hermes-guide` 都是 `autonomy: read-only`），换模板会把验收对象从"dev 场景"变成别的场景，且角色种类与 dev 不同。因此这里的选择是**口径对齐**，不是"没有别的模板可选"。

另外 `run_finish`（`:1663-1669`）要求 `stage_index == len(stages)` 且每个阶段都有 `gate pass` 事件，`dev-lite` 有 5 个阶段，所以不存在"派一次单就能 finish"的路径。

以下所有命令都在非默认 session 的测试 Pane 内执行，`ID` 为 `run start` 返回的 `run_id`。Lead 阶段（`plan_brief`、`accept`）由测试 Lead 自己 gate；worker 阶段各派一次。

```bash
# 0) 建 Run
python3 scripts/htm.py run start --goal "验证非默认 session 的 Run 生命周期与 session 路由"
#    从 stdout 的 run_id 取 ID

# 1) plan_brief（actor=lead，无需派单）
python3 scripts/htm.py run gate --run-id ID --decision pass \
  --evidence "plan_brief: 验收目标为验证非默认 session 下的 Run 账本与 --session 路由"

# 2) implement（actor=developer）
#    派单前确认 PATH 最前是 Owner 批准的 herdr 包装脚本，日志文件已就位
python3 scripts/htm.py run dispatch --run-id ID --role developer --effect write \
  --message "在隔离目录写一份最小实现说明，产出 JSON 报告，verdict=pass"
#    派单后立即核对 herdr-argv.log 中本次 agent prompt 调用：
#    期望形如 "--session <非默认名> agent prompt <name> ..."，--session 在 agent 之前
grep -n -- "--session" <临时目录>/herdr-argv.log
python3 scripts/htm.py run gate --run-id ID --decision pass \
  --evidence "implement: developer 报告已由 --wait 自动结算（:1312-1317），artifact_sha256 已绑定，verdict=pass"

# 3) self_test（actor=developer，gate 额外要求 tests 非空且 exit_code 全为 0）
python3 scripts/htm.py run dispatch --run-id ID --role developer --effect write \
  --message "对第 2 步产物做自测，报告 tests 字段须含 exit_code=0 的记录，verdict=pass"
python3 scripts/htm.py run gate --run-id ID --decision pass \
  --evidence "self_test: 报告含 exit_code=0 的测试记录，artifact 哈希与 --wait 自动结算时一致（:1312-1317）"

# 4) code_review（actor=reviewer）
python3 scripts/htm.py run dispatch --run-id ID --role reviewer --effect read \
  --message "独立评审第 2、3 步产物，verdict=pass 或 needs-work"
python3 scripts/htm.py run gate --run-id ID --decision pass \
  --evidence "code_review: reviewer 报告 verdict=pass，artifact_sha256 与 --wait 自动结算时一致（:1312-1317）"

# 5) accept（actor=lead）
python3 scripts/htm.py run gate --run-id ID --decision pass \
  --evidence "accept: Lead 核对目标已达成，无未结算派单，证据链完整"

# 6) 可选：run drive（**必须在 finish 之前执行**；Run 一旦 finished 就非 active，
#    只会返回 run_not_active（:1490-1492），无绑定分支的 identity_unbound 不会出现）
#    先读 team 文件确认 Worker 是否已有 agent_session，再决定预期值：
#    - 有 agent_session（原生 claude Worker 走 init 的主路径，:869-875 会写入）：
#      预期 drive 正常推进或按正常业务理由停止，**不得断言 identity_unbound**
#    - 无 agent_session：预期返回 identity_unbound（:1493-1499）
#    注意：此时各阶段的 Worker 派单与 gate 已全部完成，drive 不会重复派单；
#    若最新阶段已推进到 accept（actor=lead），drive 会立即以 awaiting_lead 停止（:1539-1541），
#    这是正常业务结果，记录即可。要观察 drive 的派单行为，应在含待推进 worker 阶段时执行，
#    即把它前移到对应阶段之前。
python3 scripts/htm.py run drive --run-id ID

# 7) 收尾
python3 scripts/htm.py run finish --run-id ID
```

必填参数复核 [源码]：`run dispatch` 的 `--role` 与 `--message` 为 required（`:1805`），`--effect` 默认 `read`；`run gate` 的 `--decision` 与 `--evidence` 为 required（`:1811-1812`，`:1810` 是 `collect` 的 `--dispatch-id`），且 `evidence` 去空白后长度须 **≥12**（`:1398` 判断、`:1399` 抛出）。`self_test` 阶段还要求报告里 `tests` 非空且每条 `exit_code == 0`（`:1392-1394`）。`governance.approval` 默认为 `boundary`，`--effect write` 不需要额外 approve token（`needs_approval` 定义在 `:1137`、调用在 `:1226`，approve 缺失的报错在 `:1241-1242`；`approval=boundary` 时对 `write` 返回 false）。若某阶段 Worker 的依赖无法满足（例如 Happier 下 Worker 未绑定身份），该阶段 gate 会因"latest artifact is not an approved result"失败，此时按下方 uncertain/失败处置处理并记录，不要伪造 gate。

**G 的 uncertain 处置（不要用 cancel 掩盖）**

[源码] `run cancel`（`:1732` 的 pending 过滤、`:1734` 抛错）只放行 `settled` 与 `blocked`，`uncertain`/`submission_uncertain`/`submitted_unsettled` **不在白名单**，调用会被拒绝。因此"collect 返回 uncertain 时改用 cancel"是错误路径，本计划**不把 cancel 当作任何正常收尾路径**。

uncertain 的正确处置是**停止执行、保留证据、升级人工处置**。依据 `docs/bugs/htm-uncertain-dispatch-lockup.md` 的「人工救援政策」一节（该节原文，已逐字核对）：直接 `herdr agent prompt` 绕过账本重投是事故应急行为，不是受支持的常规恢复命令，也不是安全性证明；在有可信停止/隔离和授权机制前，正式 runbook 应明确禁止该绕过；应保存证据、暂停并升级人工处置，不要盲目重投或编辑账本；不得把本次成功作为未来重投的许可。

**因此本计划不提供任何绕过账本的救援步骤，也不把历史事故中的手工重投当作许可**，更不引入任何直接操作 pane 内进程的具体命令。

一旦出现 `uncertain`/`submission_uncertain`/`submitted_unsettled`：立即停止本次验收，保留账本、artifact 目录与全部命令输出作为证据，向 Owner 升级并**中止整个清理流程**（不关闭 workspace、不停止 session）。**该情形记为"未能验收"，不得计入本验收的能力结论**，也不得用 cancel、改账本或重投来制造一个"通过"的结果。

**G 对 `run drive` 的断言口径（二选一，先读 team 再定预期）**：`run_drive`（`:1417` 起）函数体内**没有任何 `herdr()` 调用**，它是通过 `check_identity` → `verify_existing` → `lookup_agent` 间接访问 Herdr 的，因此"`run drive` 的 argv 含 `--session`"不是可直接观测的断言。本用例只对 **`run dispatch`** 断言 argv 中的 `--session`。

`run drive` 的 `identity_unbound` **不是必然结果**：`scripts/htm.py:869-875` 在 Worker 有合法 `agent_session` 时会把 `agent_session` 写进 team，原生 claude Worker 走 `init` 的主路径时必然被绑定，此时 `run drive` 的预检（`:1493-1499`）会通过，**不应出现 `identity_unbound`**。因此执行顺序固定为：**先只读 team 文件，确认每个 role 是否带 `agent_session`（三项 `kind`/`source`/`value` 齐全且 `value` ≥8 字符），再据此确定预期值**——

- 有绑定：预期 drive 正常推进或按正常业务理由停止（如 `stuck_unsettled`、`run_not_active`、`deadline_exceeded`），**不得把 `identity_unbound` 当作预期通过**；若真出现 `identity_unbound`，说明预检与 team 内容不一致，应作为异常记录。
- 无绑定：预期返回 `identity_unbound`，属**预期拒绝**，记录即为达成。

**不得把其中任何一种结果预先当作必然**，也不得只记录一种。若本次决定不执行 `run drive` 步骤，则 `identity_unbound` 相关断言全部移出通过条件，只在"未验证"中标注。

`run list` 也**不做 session 路由断言**：`run_list()`（`:1694`）只调 `context()` 与读账本、比较 `run["context"]`，全程无 `herdr()` 调用。

## 执行顺序与清理

1. 经 Owner 批准具体机器、测试名称、workspace、临时账本目录、观测方式及允许写入范围；只读核对 `herdr session list --json`、每个 session 的 pane list 和测试名不存在。建立独立空目录及仓库，记录路径；不得扫描或读取被禁止的 Run 文件。
2. 在独立非 Herdr 终端前台执行 `herdr session attach <测试名>`；若无法创建，停止并请 Owner 在候选路径中决策。核对新 session 的 running、socket 与 default 不同，并记录启动前后列表。**随后立即执行范围节的 ID 冲突只读核对**：两边 `pane list --json` 求 `(workspace_id, pane_id)`/`(workspace_id, tab_id)` 交集；有交集即停止本批用例并请求 Owner 决策。确认无冲突后，明确区分两个测试 workspace，在各自 Pane 启动 Lead，非默认侧使用 Happier。
3. 先做 A、E；完成测试账本 init 前，B 仅验证身份，init 后再验 `status`；按 C、F、G 收集证据。**F 只产出"不可验证"结论**；G 的 team 按 G 用例下的「team 来源」用 `init dev-lite --worktree off` 单独建立，不依赖 F。G 必须以 `finished` 结束，因此 G 必须先于清理步骤。D 只在可安全构造真实冲突且已获批准时执行；冲突会阻断其他用例，宜最后执行。
4. 在隔离的 `HTM_STATE_DIR` 内只读列出**本次创建的** Run（G 用例产生的），核对 **G 的 Run 状态为 `finished`**。若出现 `uncertain`/`submission_uncertain`/`submitted_unsettled` 或其他未结算派单：**不是走 `run cancel`**，而是停止执行、保留账本与 artifact 作为证据、上报 Owner 人工处置，**本次清理流程就此中止**（不关闭 workspace、不停止 session、不删除任何资源）。不得读取既有默认账本来做该确认。**若本次未执行 G（含前置不满足）或未产生任何 Run，跳过该条并注明"本次无 Run，无需核对未结算派单"**——不得因为跳过而暗示已验证。
5. 核对隔离 `HTM_WORKTREE_DIR` 下本次创建的 worktree 目录：只读列出其内容与分支名，确认没有写入默认的 `~/.local/share/herdr-team/worktrees`；如使用了 `--task`，记录实际 worktree 路径及其存放位置。不主动删除 worktree，由 Owner 另行批准清理。
6. 仅对记录在案、本次创建且确认安全的测试 workspace，使用 `herdr --session <对应名> workspace close <测试 workspace ID>`；关闭前再次核对 session 和 ID。随后在独立终端使用 `herdr session stop <测试名> --json`，确认停止后如 Owner 明确批准删除该新建 session，再用 `herdr session delete <测试名> --json`。绝不对 default 执行 stop/delete。保留隔离账本与证据供 Owner 审核；临时资源是否删除另行批准。
7. 最后只读核对 default 仍 running、socket 未改变，新测试 session 按约定停止或删除，测试 workspace 与隔离目录的清理结果、以及未验证项如实报告。不得因清理失败操作其他资源。

**本计划需 Owner 批准后才执行**
