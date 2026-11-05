# Happier 内 `/herdr-team init` 的最小可实施修复路径

- 状态：设计文档 + 已落地实现 + **真实端到端验收通过**（分支 `fix/issue6-session-isolation-20261104044949`，未合并 main）
- 证据标签：**[实测]** 本机真实执行并核对；**[源码]** 从已安装产物读到；**[推断]** 基于前两者的推理，未端到端验证
- 版本：Herdr 0.9.5（protocol 22）、Happier **0.2.20**（端到端验收版本；早期根因分析基于 0.2.17）、HTM v4.2

---

## 1. 结论

**采用方案 B：HTM 用内核父链 + `pane.process-info` 自证所在 Pane。** 改动只落在 `scripts/htm.py`，不依赖上游发版。

方案 A（Happier 代注册 Session）**不能单独修复本问题**：`agent_session` 挂在 Pane 上，HTM 必须先知道自己属于哪个 Pane 才能读它，属循环依赖。A 只能作为后续阶段解决 `name-lead`/`adopt`，且被一个未解问题阻塞（§7）。

---

## 2. 根因（三层）

| 层 | 事实 | 证据 |
|---|---|---|
| 环境（**0.2.20 实测，当前结论**） | Happier 启动的 claude 子进程中，**只有 `HERDR_ENV` 缺失**；`HERDR_PANE_ID`/`HERDR_WORKSPACE_ID`/`HERDR_TAB_ID`/`HERDR_SOCKET_PATH`/`HERDR_SESSION` **全部存在且值正确**（例如 `HERDR_SESSION=i6-accept`、`HERDR_SOCKET_PATH=~/.config/herdr/sessions/i6-accept/herdr.sock`）。`CLAUDE_CODE_SESSION_ID` 仍然缺失 | **[实测]** 2026-11-05 `ps eww` 逐键核对 claude 子进程 pid 31563 |
| 环境（0.2.17 早期记录，**已过时**） | 早期分析记录的是 claude 子进程**整体没有** `HERDR_*` 五个变量。该描述对应 Happier 0.2.17，**不适用于 0.2.20** | **[实测]** 2026-11-04 旧记录 |
| 代码 | Happier 白名单丢弃 `HERDR_*`：`stripNestedSessionDetectionEnv` 显式 `delete HERDR_ENV`；`buildClaudeSubprocessEnv` 的 exact/prefix 白名单不含 `HERDR_` 前缀；统一终端路径另用 `buildTerminalLauncherProcessEnv` | **[源码]** `api-BJP7Fl6P.mjs:95121`、`:95566`；`command-1D3NR-aN.mjs:21362`（0.2.17 产物） |
| HTM（修复前） | `context()` 读 `HERDR_ENV` 与三个 ID，缺失即报错；`--dry-run` 不调 `context()` 所以能过。当前实现已改为进程归属探测（§4） | **[实测]** 修复前移除 `HERDR_*` 后 `status` rc=2；**[源码]** 当前 `scripts/htm.py:262-304` |

**关键事实**：无论哪个版本，`HERDR_ENV` 在 claude 子进程层都不可用。Happier 自己的 runner 环境中 `HERDR_ENV=1` 与全部 ID 都在。**[实测 0.2.20]** 因此 Pane 归属在 Happier 侧是确定的，只是缺少 `HERDR_ENV` 这个开关；其余 ID 虽被继承，但按 §5.1 不能单独作为身份证据，仍必须由父链探测交叉确认。

**剥离范围收窄的后果**：0.2.20 下派生路径会在「其余 `HERDR_*` 存在」的情况下被触发，这是设计时未显式列举的组合。实测两条证据链结论一致（探测 session `i6-accept` 与 `HERDR_SESSION` 相同、socket 解析一致），未出现冲突；但**这不改变 §4.2 的分支判定规则**——仍以 `HERDR_ENV` 是否存在决定走哪条路径。

---

## 3. 三条候选路径

| 路径 | 能解决 | 不能解决 | 判定 |
|---|---|---|---|
| A. Happier 调 `pane.report_agent_session` | Session 身份（`agent_session`） | Pane/Workspace 定位；`context()` 仍失败 | **不能单独用** |
| B. HTM 用父链 + `process-info` 自证 | Pane/Workspace/Tab 定位；`init`/`status`/`run drive` 前置 | Session 身份（Happier 的 claude 本就不上报） | **本次采用** |
| C. Herdr 服务端提供可信调用者身份 | 两者都能 | 需核心改动 + 协议变更 + 发版 | 长期正解，本次不动 |

Herdr 0.9.5 **没有**可信调用者身份接口：**[实测]** `herdr api schema`（protocol 22）无 caller/peer/client_id 等字段；`pane.current` 的 `caller_pane_id` 由调用方自报，服务端不校验；`pane.current` 在无 `HERDR_*` 时返回**焦点** Pane（当时 `wH:p7`，而调用者在 `wS:p2`），与调用者无关。

---

## 4. 方案 B 的实现

全部改动在 `scripts/htm.py`。

### 4.1 新增 `pane_of_current_process()`

**[源码]** 位置：`socket_session()` 在 247 行，`lead_identity()` 在 262 行，`context()` 在 303 行，`pane_of_current_process()` 在 395 行。`socket_session()` 按 socket 查询唯一 session；`lead_identity()` 内部对原生和派生路径均做两次探测，原生路径随后按需调用 `socket_session()`；`context()` 仅返回其 context。探测函数不读写 `HERDR_*`，返回 `{"session", "workspace", "tab", "pane"}`。

1. `herdr session list --json` → 取 `running: true` 的 session。**[实测]** 当前返回 `[{name:"default", running:true, socket_path:"..."}]`。
2. 对每个 session 执行 `herdr --session <name> pane list`。**[实测]** 无 `HERDR_*` 环境下 rc=0。
3. 祖先集合：从 `os.getpid()` 起循环 `/bin/ps -o ppid= -p <pid>` 直到 `ppid <= 1` 或 64 层上限。用绝对路径是**防御性写法**：**[实测]** Happier claude 的 PATH 实际含 `/usr/bin`、`/bin`、`/usr/sbin`、`/sbin`，裸 `ps` 也可用，但不应依赖调用方 PATH 的解析顺序。
4. 对每个 session 的每个 Pane 执行 `pane process-info`，取 `shell_pid` 与 `foreground_processes[].pid` 的并集，与祖先集合求交。
5. **唯一命中**才返回，并带上命中所属的 session 名；0 命中或多命中一律 `raise Error`，错误分两种文案（"无法定位" / "身份不唯一，拒绝猜测"）。多命中即使同属一个 session 也拒绝。
6. 返回前复用现有 ID 正则与"tab/pane 前缀必须等于 workspace"校验（与 `context()` 同构）。

可靠性依据：`ppid` 由内核在 fork 时写入，进程无法自报；`process-info` 的 pid 来自 Herdr 服务端自己的 PTY 记录。两侧同时命中才成立。

**[实测，2026-11-04 历史样本]** 本进程唯一命中 `wS:p2`；Happier 的 claude（29187）与 MCP 桥（29222）为种子均唯一命中 `wH:p5`；原生 claude（93044）唯一命中 `wS:p3`。11 个 Pane 全量扫描耗时 0.065s。

**[实测，2026-11-05 双 session 复核]** 在 `default`（`wX`/`w0`/`w12`，7 个 Pane）与 `i6-accept`（`w1`，1 个 Pane）同时 running 的条件下，`default` 内的 `pane_of_current_process()` 仍唯一定位到 `w0:p1`，未因多 server 扫描而误判或报"身份不唯一"。上述历史样本的 PID 与 Pane ID 属当时现场，现已不可复现，仅作方法学证据保留。

### 4.2 `lead_identity(require=True)` 与 `context()`（262-304）

**分支顺序与现有代码一致；原生分支保留原有校验，并新增一次真实进程归属交叉检查；无 `HERDR_ENV` 时才走派生兜底：**

| 情形 | 行为 | 二次探测 | 理由 |
|---|---|---|---|
| `HERDR_ENV == "1"` 且三个 ID 齐全 | 保留 ID presence/format/cross-workspace 校验，**再**用 `pane_of_current_process()` 做真实进程归属交叉检查：`workspace`/`tab`/`pane` 必逐项相等，session 以探测结果为准并被 `HERDR_SESSION`/`HERDR_SOCKET_PATH` 交叉确认 | **做**（两次探测须一致） | 业务兼容不退化；但继承来的 `HERDR_*` 可被伪造或已过期，不能单独作为身份证据 |
| `HERDR_ENV == "1"` 但 ID 缺失/非法 | **保持现有报错**（`Missing Herdr pane/workspace/tab environment` 等），**不退回派生** | 不适用（在探测之前就失败） | 在"已声明在 Herdr 内但环境不完整"时改用另一套身份来源，会掩盖真实的环境错误 |
| 无 `HERDR_ENV` | 调 `pane_of_current_process()` | **做** | 这是 Happier 的情形 |

**原生路径不是"零变化"**：ID 的格式与互属校验、`context()` 的三键契约、落盘与比较都保持不变，但**新增了一次真实进程归属交叉检查**。这是本轮对 §4.2 旧结论的修正——原设计以"原生可靠性不降级"为由跳过探测，但它信任了可继承、可伪造的 `HERDR_*`，而 Herdr 0.9.5 没有服务端对端身份（§3），因此必须用内核父链 + `process-info` 的独立证据交叉验证。

**核对规则（原生路径）**：
1. 探测结果的 `workspace`/`tab`/`pane` 必须与环境三 ID **逐项相等**，否则 `raise Error`（文案含 `进程归属与 HERDR_* 身份不一致`）。
2. 环境 `HERDR_SESSION` 非空时，必须等于探测到的 session 名。
3. 环境 `HERDR_SOCKET_PATH` 非空时，必须用 `herdr session list --json` 中**`socket_path` 与它逐字节相等的唯一 session 名**证明该名字等于探测 session；0 个或多个匹配、名字为空、或匹配到的 session 与探测不符一律 `raise Error`。**不把缺失值默认成 `default`**。（socket 路径由 Herdr 自己生成，属比较即可；若出现规范化差异会因 0 匹配而 fail closed。）
4. 探测结果自身必须通过既有 ID 正则与 workspace 前缀校验。

**行为后果（有意）**：原生路径现在要求调用进程确实位于该 Pane 的进程树内。若用**真正脱离 Pane 进程树**的方式运行 HTM（例如双 `fork` + `setsid`），将 fail closed 而不再"看似成功"。这是本方案要的语义：身份必须有内核证据，不能只靠环境变量。

**[实测 2026-11-05] 脱离方式的边界**：
- 双 `fork` + `setsid`，且在干净环境（无 `HERDR_*`）下 → `FAIL_CLOSED: 无法定位 Herdr pane: no matching pane` ✅ 符合预期
- `nohup` **不会**改变 ppid，进程仍在 Pane 的进程树内 → 定位**仍然成功**。`nohup` 只是忽略 SIGHUP、重定向 stdio，不改变父链归属
- 即使显式 `env -u` 清空全部 `HERDR_*`，只要进程仍在 Pane 进程树内，派生探测就会成功——这是设计语义（证明"我在这里"），不是漏洞

因此**不要把 `nohup` 当作"脱离 Pane"的例子**；判断依据是内核 ppid 链，不是 shell 的重定向或信号处理。

**`require=False` 契约保持不变**：返回 `{"workspace","lead_tab","lead_pane"}` 三键，提前返回、不解析 session、不探测、不调 herdr。

**CLI 选择语义 [实测]**：`herdr --session <name>` 会**覆盖**继承的 `HERDR_SESSION` 与 `HERDR_SOCKET_PATH`（本机只读验证：`HERDR_SOCKET_PATH=/tmp/missing.sock herdr --session default pane list` 成功连到 default server；不带 `--session` 时同一变量导致 `server_not_running`）。`herdr session list --json` 则忽略两者、始终返回全局 session 表，因此适合用作"哪个 socket 属于哪个 session"的权威查询。**结论：一旦确认了 session，管理调用必须显式带 `--session`，否则 CLI 可能连到别的 server**——这正是本方案要防的误连。无法证明探测 session 与 CLI 目标 server 一致时，直接失败，不静默继续。

**不要**把解析结果写回 `os.environ`：那会制造一个可被后续进程继承的伪造源。

### 4.3 session 维度

`context()` **永远不包含 session**。原因：它的返回值用于落盘和精确相等比较（本节旧行号为设计时的历史定位，不代表当前源码行号）。任何新增顶层键都会让全部既有账本失效——**[实测，2026-11-04 历史样本]** 当时现有 3 个 team 与 6 个 run 的 `context` 均为恰好 3 键，其中 wS 的 `95fd9bc7acd24c75` 正被 Run `bdd4938067b5` 使用。**[实测，2026-11-05]** 隔离 session 新建的 team 与 Run 同样为恰好 3 键，契约未变。

改为平行返回：

```python
def lead_identity(require=True) -> dict:
    """{"context": {"workspace","lead_tab","lead_pane"}, "session": str}"""
```

`context()` 成为 `lead_identity()["context"]` 的薄封装，因此**所有既有落盘与 `!=` 比较零改动，三个既有 team 与六个 run 都不需要迁移**。`state_file` 的 key 仍是 `sha256("workspace|lead_pane")[:16]`（当前 311-313 行），不分裂。

**session 取值规则（按分支）**

| 分支 | 来源 | 取值 | 额外调用 |
|---|---|---|---|
| 原生 `HERDR_ENV=1`（`HERDR_*` 齐全） | `pane_of_current_process()` 的真实归属，被 `HERDR_SESSION`/`HERDR_SOCKET_PATH` 交叉确认 | 已确认的实际 session 名，**必然非空**（默认 server 即 `default`） | 两次探测；有 `HERDR_SOCKET_PATH` 时额外一次 `session list` |
| 派生路径（无 `HERDR_ENV`） | `pane_of_current_process()` | 命中所属 session 名，必然非空 | 两次探测（每轮内部含 `session list`） |

**路由方式**：确认后的 session 显式传给既有 `herdr(..., session=...)` 调用链，`lead_identity()["session"]` 是唯一来源。默认 session 用 `'default'` 显式锁定即可；所有默认 `herdr()` legacy 调用（未传 session）保持不改。

**旧结论作废**：原 §4.3 写"原生分支 `session` 恒为 `""`、不反查"。该写法**已废弃**，与 §4.2 的交叉检查冲突。现规则是：原生分支只要探测成功就返回已确认的 session（`default` 或其它），并由调用链显式传给 `herdr()`；这样即使 `HERDR_SOCKET_PATH` 指向别的 server，CLI 也不会连错。

**fail closed 规则（两条路径共用）**：
- `herdr session list --json` **命令本身失败**（非零退出、超时、输出不是合法 JSON）→ 直接 `raise Error`。`scripts/htm.py:356-381` 的 `process()` 已对这三类情况抛 `Error`/`HerdrError`，**不得捕获后降级为"找不到 session 所以跳过"**——那会把环境故障伪装成"无身份"，从而放行。
- `session list` 成功但返回 0 个 running session → `raise Error`。
- 扫描到的多个 running session 之间出现相同 `(workspace_id, pane_id)` 或 `(workspace_id, tab_id)` → `raise Error`（冲突存在时不做猜测）。该检查位于 `pane_of_current_process()`（395 起）内部的 panes 扫描循环（425-429），**没有分支判断，原生与派生两条路径都生效**，且即使冲突键不属于命中 Pane 也拒绝；同一 session 内共享 Tab 不触发。

**为什么不用 `HERDR_SESSION_NAME`**：**[实测]** Herdr 注入的 `HERDR_*` 只有 6 个（`HERDR_BIN_PATH`/`ENV`/`PANE_ID`/`SOCKET_PATH`/`TAB_ID`/`WORKSPACE_ID`），无此变量。

**`herdr()` 新增可选参数**：

```python
def herdr(*args, timeout=40, session=None):
    # 空串/纯空白视同 None；非法字符集直接报错
    argv = ["herdr"] + (["--session", session] if session else []) + list(args)
```

必须用**非空真值**判断：**[实测]** `herdr --session "" pane list` → `session name cannot be empty`；`herdr --session " " pane list` → `session name may only contain ASCII letters, numbers, '.', '_' and '-'`。默认 `session=None` 时拼出的 argv 与现在逐字节相同，15 处既有调用全部不受影响。session 名以 `herdr session list --json` 返回值为准，正则 `[A-Za-z0-9._-]+` 只是把 CLI 自身规则前置成 `Error`；两条路径都只传入探测确认过的非空名字。

**需要透传 session 的位置**（下表行号为修复前设计时的历史定位，当前实现行号已变化；6 个既有 `context()` 调用点之外还有间接链）：

| 位置 | 用途 | 需 session |
|---|---|---|
| 665 `init` | 722/734 `tab create`/`pane split`、348/380/386 探活与启动、438/464 `lookup_agent`/`agent get` | 是 |
| 771 `adopt` | 846 `pane get`、438 `lookup_agent` | 是 |
| 901 `name_lead` | 908 `pane get`、912/935 `agent get`、947 `agent rename`、454 `require_name_available` | 是 |
| 974 `run_start`（间接） | 经 `current_team()` 拿到 ctx，974 `verify_existing` → 438 | 是 |
| 1117/1120 `run_dispatch`（间接） | `verify_existing` 与直接 `lookup_agent` | 是 |
| 1174 `run_dispatch` 发 prompt | `herdr("agent","prompt",...)` | 是 |
| 1295-1309 `run_drive.check_identity` | 内部经 `verify_existing`/`lookup_agent` 读 Agent，不自己拼命令 | 是（作为 `check_identity(role, session)` 透传） |
| 890 `current_team`、963 `require_run`、1563 `run_list` | 只读账本 | 否 |

`run_start`/`run_dispatch` 拿不到 `context()` 的返回值，因此 `current_team()`/`require_run()` 需另出返回 session 的内部变体。**不得用模块级全局变量传 session**（会跨调用泄漏）。

上表"需 session=是"表示该调用链必须把 `identity["session"]` 透传下去；两条路径都返回已确认的非空 session，因此 `herdr()` 都会显式拼 `--session`，CLI 因此不会因继承的 `HERDR_SOCKET_PATH` 连错 server。

### 4.4 阶段序列与二次校验

**二次校验适用于两条路径**（原生与派生，见 §4.2）。序列为：

| 阶段 | 动作 | 失败时 |
|---|---|---|
| S1 | `context()`/`lead_identity()` 执行**第 1 轮**探测 | 报错，不返回部分结果 |
| S2 | **第 2 轮完整探测**，要求与 S1 逐项相同（`session`/`workspace`/`tab`/`pane`） | **报错，此时尚未产生任何副作用** |
| S3 | `init` 现有流程（创建 partial 账本、`git worktree add`、首个 Herdr 写操作） | 现有逻辑 |

**关键：第 2 轮必须在 `save(path, team)`（首次落盘 partial 账本）之前完成。** 下列行号为修复前设计时的历史定位；原因是可以避免一个无法恢复的状态：

- `init` 在 `:707-708` 先写 `status: "partial"` 账本，随后 `:718-719` 可能创建 worktree，再在 `:720+` 写 Herdr；
- 而 `:680-681` 对已存在的 partial 账本**直接拒绝重试**（`Previous init is partial...`），没有自动恢复路径；
- 因此若二次校验放在落盘之后失败，会留下"partial 账本 + 可能已建的 worktree + 零个 Pane"，用户只能人工清理。这是本轮要避免的。

放在落盘之前则失败时无任何残留：不写账本、不建 worktree、不动 Herdr，用户直接重试即可。

**代价**：第 2 轮与首个 Herdr 写操作之间隔了 `git worktree add`（可能秒级），窗口比"紧贴写操作"更大。这是有意取舍——**用更大的窗口换"失败零残留"**。若 Owner 更看重窗口大小，替代方案是保留原位但**必须额外定义 partial 的可恢复语义**（例如记录 `phase` 字段并在下次 init 时按 phase 清理），属未设计的扩展，本轮不采纳。

至此 S3 内部的首个写操作**依配置而变**：`layout.mode == "tab"` 时是 `tab create`（当前 `scripts/htm.py:836-838`），否则是 `pane split`（当前 `:841-842`）。

### 4.5 竞争窗口（如实表述）

两轮完整探测**只能降低误判概率，不能证明零窗口**：

1. **轮内变化**：两轮不一致时 fail closed，这部分被覆盖。
2. **轮后窗口**：第 2 轮探测结束到首个 Herdr 写操作之间仍有一段窗口。按 §4.4 的顺序，这段窗口**包含** `git worktree add`（可能秒级），因此比"紧贴写操作"的方案更长——这是为换取"失败零残留"而有意接受的代价。若该窗口内 Pane 前台进程被替换——包括 ABA 型（换走又换回、或换成同样落在祖先链上的兄弟进程）——第 2 轮的结论就过期了，而 HTM 无法再观察。**这段窗口不可关闭**：不存在能同时锁住 Pane 前台进程与写 Herdr 结构的原子操作。

落地只有两个选择：

| 选择 | 做法 | 局限 |
|---|---|---|
| 接受 | 明写风险，不做额外防护 | ABA 场景下 `init` 会**成功写入错误对象**而无法察觉；`partial`/`resources` 只在写入失败时有帮助 |
| 写入前终态复核 | 每个 `operate(...)` 前都做一次完整探测 | 复核与实际 `herdr` 子进程执行之间仍有微秒级窗口，不可关死；`init` 有 3-4 次 split/start，耗时显著增加 |

---

## 5. 安全性

### 5.1 不可接受的冒充手段

| 手段 | 为什么不行 |
|---|---|
| 手设 `HERDR_ENV=1` / `HERDR_PANE_ID=...` | ID 可任意自报，身份校验退化为字符串比较 |
| 转发 Happier runner（29054）的 `HERDR_*` | 那是 Happier 的身份，不是调用者的；任何子进程都能继承 |
| 用 `pane.current` 推断 | **[实测]** 返回焦点 Pane，与调用者无关 |
| 自报 `caller_pane_id` | 服务端不校验对端 |
| 以 `source="herdr:claude"` 调 `report_agent_session` | 冒用官方集成事源 |
| 用 `HAPPIER_SESSION_ID`/`TERM_PROGRAM=herdr` 当身份 | 前者与 Pane 非一一对应，后者是常量 |
| 仅凭 pgid 判定归属 | pgid 可被 `setpgid` 改写，且 Pane 内多进程共享 |

### 5.2 本方案的边界

- 证明的是"**我在这个 Pane 里**"，不是"我是这个 Pane 的 Lead Agent"。同 Pane 内的兄弟进程同样命中，这是正确语义而非漏洞。
- 不回答"我是否有权操作这里"。`verify_existing`/`check_identity` 的语义保持不变，不得因 B 而放宽。
- 结果不落盘、不写回环境、不继承。

---

## 6. 验证（隔离 Workspace）

**V0** 本分支 `uv run pytest -q` → `239 passed, 1 skipped, 127 subtests passed`（**[实测]**）。

**V1** 修复前复现（历史记录，非当前预期）：`env -u HERDR_ENV -u HERDR_PANE_ID -u HERDR_WORKSPACE_ID -u HERDR_TAB_ID -u HERDR_SOCKET_PATH python3 scripts/htm.py status` → rc=2，stderr 为 `Must run inside Herdr: HERDR_ENV=1`（**[实测]**）。

**V2** `herdr workspace create --label htm-once-<slug>`，记录返回的 workspace id；在其中用 `happier claude` 启动 Lead。`init dev-lite --dry-run` 应返回 `requires_herdr: true`、`no_mutations: true`。

**V3**（核心）在该终端执行真实 `init dev-lite --task <slug>`，判定全部成立：
- rc=0 且 `reused: false`；
- 新 Pane 的 `workspace_id` 等于 V2 的 workspace；
- 同一终端内 `env -u HERDR_ENV ... python3 scripts/htm.py status` 返回 rc=0（直接证明 B 生效且不依赖环境变量）；
- 新建 team 文件的 `context.workspace` 等于 V2 workspace。

**V4**（负例，必须与 V3 同时成立）：
- 在非 Herdr 终端执行同一命令 → rc=2，且文案说明"进程树未唯一定位到 Herdr Pane"；
- 单测（派生分支）：0 命中抛错、多命中抛错、两轮不一致抛错；
- 单测（原生分支）：`HERDR_ENV=1` 且 ID 合法时**必须**执行两次真实归属探测并与三 ID 交叉核对；`HERDR_SESSION` 冲突、`HERDR_SOCKET_PATH` 指向别的 session、探测与 `HERDR_*` 不一致时均 fail closed；确认后的 session 必须以 `--session <name>` 出现在管理调用的 argv 中；`require=False` 不探测。
- 单测（socket 解析）：0 个匹配、多个匹配、名字为空、socket 不匹配均 fail closed；`session list` 命令失败不得降级；
- 单测：第 2 轮失败时**未产生任何副作用**（无 team 账本、无 worktree、无 Herdr 写入）。

**V5** 清理：确认无未结算 Run 后 `herdr workspace close <V2 id>`。不要删除本地账本，不要触碰 `wH`/`wS`。

判定标准一句话：**移除全部 `HERDR_*` 后 `init` 与 `status` 仍成功，同时非 Pane 环境下严格失败。两条必须同时成立。**

### 6.1 真实端到端验收结果（2026-11-05，**[实测]**）

**环境**：Herdr 0.9.5、Happier 0.2.20、claude 2.1.311、分支 `fix/issue6-session-isolation-20261104044949` @ `b03f537`。独立命名 session `i6-accept`（`~/.config/herdr/sessions/i6-accept/`，独立 socket 与 session.json），与 `default` 同时运行。

| 项 | 值 |
|---|---|
| Session | `i6-accept`（非默认） |
| Workspace / Tab / Pane | `w1` / `w1:t1` / `w1:p1` |
| Team 文件 | `<隔离状态目录>/teams/5940f14f4143682b.json`，`status=ready` |
| Run | `e1f66ce5dfab`，`status=finished`，`stage_index=5` |
| 隔离状态目录 | `~/.local/state/herdr-team-i6-accept/` |

**Team 与身份绑定**：

| 角色 | Agent | Pane | `agent_session.value` |
|---|---|---|---|
| developer | `developer-w1p2` | `w1:p2` | `b5cd4a03-75b4-451d-a7a3-57209e9cdbe2` |
| reviewer | `reviewer-w1p3` | `w1:p3` | `17d02554-084b-4bff-b68e-828021a91c56` |
| Lead | （Happier claude） | `w1:p1` | **`None`** — Happier 不上报 |

**Run 五阶段**：`plan_brief` → `implement` → `self_test` → `code_review` → `accept`，全部 `pass`。

| Dispatch | 角色 | 阶段 | 状态 | `artifact_sha256` 前缀 |
|---|---|---|---|---|
| `89aaacccb6` | developer | implement | settled | `bd7b9fd36c59` |
| `672b3b1311` | developer | self_test | settled | `86961494cc4e` |
| `05345cf8f9` | reviewer | code_review | settled | `d5b116b0a16c` |

**关键结论**：
1. **Happier 无需 `HERDR_*` 即可 `init` 成功**（口径修正：0.2.20 实际只缺 `HERDR_ENV`，见 §2）。`lead_identity()` 返回 `session=i6-accept, workspace=w1, pane=w1:p1`，两次探测一致。
2. **Developer / Reviewer 创建正确且 session 有效**，两者 `agent_session` 均为真实 UUID。
3. **`plan_brief` 的 Lead 保护实测生效**：对 developer 派单返回 `ERROR: Current stage 'plan_brief' expects lead` + `exit=2`，未被误派。
4. **真实产物**：`/private/tmp/i6-accept-work/i6-accept-out.txt`，内容 `I6 ACCEPTANCE OK`，17 字节，sha256 `24fbb4a7…`。Reviewer 独立执行 `cat`/`wc -c`/`wc -l`/`od -c`/`md5`/`file` 六条命令核验，未采信转述。
5. **零错误路由**：`--session` 实测覆盖继承 socket；隔离账本 6 个文件全部写在隔离目录；生产目录 `~/.local/state/herdr-team/` 在验收窗口内零写入；隔离 Run 在 default 视角不可见。
6. **`artifact_sha256` 是报告 JSON 的哈希，不是产物文件哈希**——两者本就不同对象，已在验收中核对澄清，避免误判。

**原生路径未退化回归（同窗口实测）**：
- `default` 的 `w0:p1` 内 `HERDR_ENV=1`，`lead_identity()` → `{workspace: w0, lead_pane: w0:p1, session: default}`，两次探测一致 ✅
- `HERDR_SESSION=fake-session` → `FAIL CLOSED: Herdr 进程归属 session 与环境不一致: 'fake-session' != 'default'` ✅
- 双 fork 脱离进程树 + 清空 `HERDR_*` → `FAIL CLOSED: 无法定位 Herdr pane: no matching pane` ✅
- 清空全部 `HERDR_*` 但仍在 Pane 内 → 派生路径成功定位 ✅
- `uv run pytest -q` → `239 passed, 1 skipped, 127 subtests passed`；`-k native` 20 passed、`-k derived` 16 passed + 11 subtests ✅

**仍未覆盖**：原生 Herdr 路径的**完整 Run**（五阶段）未在本轮重跑；本轮原生侧只做了身份与负例回归，完整 Run 的最近证据仍是 CHANGELOG 中 v4.2.0-beta.2 的 `wF` 联调。因此**不能把本轮 Happier 验收直接等同于"所有启动方式全通过"**。

---

## 7. 剩余障碍

**已实现并通过真实端到端验收（B）**：`init`、`status`、`run start`/`run list` 的 Lead 侧上下文可通过进程归属定位，不再依赖 `HERDR_ENV`。**[实测 2026-11-05]** 在 `i6-accept` 非默认 session 中完成 dev-lite 五阶段完整 Run（§6.1）。

**受环境限制不可用（Happier 侧）**：`name-lead` 与 `adopt` 的能力本身已在代码中实现（`adopt` 起于 885，`name_lead` 起于 1018），但在 Happier 环境下不可用——Happier 的 claude 不上报 `agent_session`（**[实测 2026-11-05]** `w1:p1` 的 Lead 与 0.2.17 时的 `wH:p5` 均为 `null`），两个命令都会因身份不可核验而拒绝执行。这是环境限制，不是功能缺失。

**`run drive` 自动模式在 Happier 下的行为**：`run drive` 预检要求全部 Worker 绑定 `agent_session`。本轮 team 的 Worker 是 Herdr 原生启动的 claude，**有**合法 `agent_session`（§6.1），因此该预检会通过；只有 Worker 也无绑定时才返回 `identity_unbound`。**不要预设它一定返回 `identity_unbound`**，应先读 team 文件确认。

**Open Question（阻塞 A 落地，未实测）**：Happier 上报后同一 Pane 的 `agent_session.source` 最终是哪一个？

按真实代码分三处看（行号为当前源码位置）：

| 代码位置 | 比较内容 | 作用范围 | `source` 不同时 |
|---|---|---|---|
| `verify_existing`（610-615） | **只比 `value`** | team 角色登记 | 仍可通过 |
| `name_lead`（1041-1044） | kind + **`value`**，**不含 source** | Lead 自身 | 仍可通过 |
| `run_drive.check_identity`（1436-1442） | 逐字段比 `kind`/`source`/`value` | 仅 Run 的**工作角色** | 条件性拒绝 |

**方案 A 的影响是条件性的，不是全面的。** 具体：

- `check_identity` 在整个 `run_drive` 中只被用于 Run 的工作角色——`:1493-1499` 是 drive 启动前对 `run["roles"]` 的 `identity_unbound` 预检，`:1518` 核对待结算派单的 `rec["role"]`，`:1558` 核对返工目标角色。**Lead 不在这些检查的范围内**，因此 Lead 的 `source` 发生变化不必触发 `run drive` 失败。
- 只有当**某个工作角色**在登记时保存的 `source` 与实时读到的 `source` 不同时，该角色的 `check_identity` 才会拒绝——即 Happier 写入改变了**该 Worker 的** `agent_session.source` 且与登记值不一致。
- 若团队在 Happier 环境下从零建立（Worker 登记时就已是 `"happier"`），保存值与实时值一致，`run drive` 可正常推进。

因此准确结论是：**方案 A 落地后 `name-lead` 可能可用；`run drive` 是否受影响取决于 Run 中工作角色的 `source` 是否发生漂移，而非取决于 Lead。** 跨环境接管既有 team（原生建、Happier 跑）才是最可能触发拒绝的场景，届时需要 source 归一化设计。归一化方案未设计，本轮不得假定其可行。

判别方法（需写入型操作，本轮未执行）：隔离 Pane 上先让 Herdr 集成写入，再手工 `herdr pane report-agent-session --source happier --agent claude <pane> --agent-session-id <同一 value>`，随后两次 `herdr pane get` 观察 `source`。

**仍需 Owner 批准**：
- B 扩张 `context()` 的拒绝条件边界；
- 竞争窗口二选一（接受 / 终态复核）；
- 跨机场景：iPhone 控制端 + Mac 执行端时，若命令仍在执行端 Mac 的 Pane 内运行，父链本机成立、B 可用；若命令运行在 Pane 之外（手机侧直连 daemon 下发），B 必然 0 命中并 fail closed —— **这是预期行为**，若要支持远端发起只能走方案 C；
- 真实环境验收计划及其资源、清理范围需 Owner 批准后执行（见 §10）。

---

## 8. 上游最小修改（若只接受上游修复）

### 8.1 Happier 补 `pane.report_agent_session`

文件：`~/.happier/cli/versions/0.2.17/package-dist/applyRunnerMcpSessionContext-DPRBYk-y.mjs`

| 行 | 符号 | 内容 |
|---|---|---|
| 3853 | `bindHerdrAgentIfNeeded` | 解析 terminal，末尾调 `bindManagedHerdrSession` |
| 3880 | 调用处 | 传入 session/client/terminalId/agent/sessionId |
| 3889 | `bindManagedHerdrSession` 定义 | — |
| 3918 | `report()` | 只发 `pane.report_agent`（`source:"happier"`），从不发 `report_agent_session` |
| `api-BJP7Fl6P.mjs:54914` | `findPane(terminalId)` | 用 `pane.list` 的 `terminal_id` 反查，**paneId 由服务端给出** |

写入点已确定（`report()` 内有权威 paneId），**缺的是权威 vendor session id**：该文件内 `grep claudeSessionId` 命中数为 0；`command-1D3NR-aN.mjs:5800` 的 `readClaudeSessionIdFromSession` 未被导出且用 `session.client.getMetadataSnapshot()`，而本文件 3839 行用 `session.getMetadataSnapshot?.()`，两种访问器并存、未确认 bind 层可用哪一种。**这是本节的 Open Question，不写实现细节。**

参数形态（`PaneReportAgentSessionParams`，protocol 22）：必填 `pane_id`/`source`/`agent`；可选 `agent_session_id`、`agent_session_path`、`seq`、`session_start_source`、`resume_argv`。`source` 应沿用 `"happier"`。

风险：同一 Pane 已有 Herdr 集成的写入者，双写来源的 `source` 必然不同，见 §7 的 Open Question。

### 8.2 Herdr 提供服务端对端身份（长期正解）

accept 后取对端 pid（macOS `LOCAL_PEERPID` / Linux `SO_PEERCRED`），映射到自己的 Pane 注册表，作为请求上下文。完成后 §4 的父链推断可整体删除。属核心 + 协议变更，周期不可控。

---

## 9. 已知限制与未验证项

**已知限制 [源码]**：`state_file` 只以 `workspace|lead_pane` 为键（311-313），不含 session。两个独立 server 出现相同 workspace+pane ID 时会命中同一账本路径。当前 panes 扫描对所有 running session 检查 `(workspace_id, pane_id)` 和 `(workspace_id, tab_id)`：同一键出现在不同 session 时，即使不是命中 Pane 也 fail closed；同一 session 内共享 Tab 不触发此跨 session 检查。它不能修复账本键本身：例如两个 session 不同时运行时仍可能复用同一路径。引入 session 维度需要处理 §4.3 的旧账本兼容问题。

**[实测 2026-11-05] 该限制在有真实数据佐证**：隔离 session `i6-accept` 的 team key 为 `5940f14f4143682b`（`sha256("w1|w1:p1")[:16]`），而生产状态目录 `~/.local/state/herdr-team/teams/` 中**恰好存在同名 key 的旧文件**（2026-11-03 的 `dev-review` partial，context 同样是 `w1|w1:p1`）。本次靠 `HTM_STATE_DIR` 隔离而未互相影响，但**键本身不含 session 维度**——若两个 session 共用同一状态目录，会直接命中同一文件。已作为独立 P1 Issue 记录，见 `docs/issues/p1-cross-session-team-ledger-key.md`。

**未验证项**（不得当作已完成）：
- B 已落地且有单测覆盖：原生分支双探测一致性、`HERDR_SESSION` / `HERDR_SOCKET_PATH` 冲突、socket 零或多个匹配、session list 失败 fail closed、第二轮探测失败无副作用、`require=False` 不探测、session 以 `--session` 注入 argv。**[实测 2026-11-05]** Happier + 非默认 session 的完整 Run 已通过（§6.1）；
- **原生 Herdr 路径的完整 Run（五阶段）本轮未重跑**：只做了身份正确性与负例 fail closed 回归。最近一次原生完整 Run 证据仍是 v4.2.0-beta.2 的 `wF` 联调。**不得把本轮 Happier 验收等同于"所有启动方式全通过"**；
- 跨 session 同名 Pane/Tab ID 的 fail closed 行为**只有单测覆盖**，本轮两个 session 的 ID 空间恰好无交集（`default`: `wX`/`w0`/`w12`；`i6-accept`: `w1`），**未在真实冲突条件下验证**；
- `source` 仲裁未实测（§7）；
- `happier session create --env` 能否让 `HERDR_*` 穿透统一终端白名单未实测，且即便可行也不应采纳（等同用户自报身份）；
- `pane.report_agent` 与 `pane.report_agent_session` 是否互相覆盖状态未验证；
- **Happier Lead 仍会自发启动后台子代理**：本轮三次派单中有两次 Happier Lead 自动起了 `general-purpose` 子代理（14s / 32s 后返回，未卡死）。历史事故中的同款模式仍然存在，HTM 无法从 Worker 侧阻止，只能靠派单 prompt 约束。

## 10. 实现与验收现状

| 状态 | 范围 |
|---|---|
| **已真实端到端验收（Happier + 非默认 session）** | `i6-accept` 中执行 `init dev-lite --worktree off`、创建 developer/reviewer、完整走完 dev-lite 五阶段并 `run finish`（Run `e1f66ce5dfab`）。含真实产物、Reviewer 独立核验、零错误路由、账本全程写入隔离目录。证据见 §6.1。 |
| 已实现并单测覆盖，真实冲突条件未验证 | 父链与 `process-info` 定位、原生身份交叉验证、两轮探测、`--session` 显式路由、`HERDR_SESSION`/`HERDR_SOCKET_PATH` 冲突 fail closed、跨 session Pane/Tab 冲突拒绝。其中**跨 session ID 冲突的 fail closed 只有单测**——本轮两个 session 的 ID 空间恰好无交集。 |
| 原生路径：身份与负例已验证，完整 Run 未在本轮重跑 | `default` 内 `lead_identity()` 正确、`HERDR_SESSION` 冲突 fail closed、双 fork 脱离进程树 fail closed、清空 `HERDR_*` 后派生路径仍可定位。但原生侧**完整五阶段 Run** 的最近证据仍是 v4.2.0-beta.2 的 `wF` 联调。 |
| 受环境限制不可用 | Happier 环境下 Lead/Worker 缺失 `agent_session` 时的 `name-lead`、`adopt`。**能力已在代码中实现**，仅因无 `agent_session` 可核验而按身份要求拒绝执行；属环境限制，不是功能缺失。 |
| 已知设计限制（需独立 P1 Issue 解决） | 跨 session Team/Run 账本键冲突：`state_file` 键为 `sha256("workspace|lead_pane")[:16]`，不含 session 维度。见 `docs/issues/p1-cross-session-team-ledger-key.md`。 |

**验收计划状态**：`docs/design/issue6-nondefault-session-acceptance-plan.md` 的 A–G 用例中，A（双 session 归属）、B（派生路径）、C（显式路由，以 `--session` 行为与返回值取证）、E（非 Pane 环境 fail closed）、G（Run 生命周期）已在 2026-11-05 实测覆盖；D（跨 session ID 冲突）因本轮 ID 空间无交集未触发，仍为单测覆盖；F（账本键限制）结论为「本项不可验证」，已被真实数据佐证为 P1 问题。
