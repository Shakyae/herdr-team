# P1：跨 Session 的 Team / Run 账本键冲突

- 状态：**已缓解（身份边界层）** — 已实施最小修复，见 `docs/design/p1-ledger-identity-boundary.md` §9。**账本键本身仍不含 session 维度**，属已知残留限制
- 优先级：P1
- 提出日期：2026-11-05
- 来源：Issue #6 真实端到端验收
- 关联：`docs/design/happier-init-compat.md` §9、§10

---

## 1. 问题

HTM 的 Team 账本按 `state_file(ctx)` 定位，键为：

```python
sha256(f"{ctx['workspace']}|{ctx['lead_pane']}").hexdigest()[:16]
```

（`scripts/htm.py`，`state_file` 函数。）

**键里没有 session 维度。** 而 Herdr 的 Workspace / Tab / Pane ID 是**按 server（session）隔离**的——官方文档原文：

> IDs and live agent names are scoped to one server. Two saved SSH machines can both have `w1:p1`.

因此两个不同的 Herdr session 完全可能产生相同的 `(workspace, lead_pane)` 组合，映射到**同一个账本文件**。

## 2. 真实证据

2026-11-05 的 Issue #6 验收中直接撞上了这个情况：

| 项 | 值 |
|---|---|
| 新 session | `i6-accept`，新 server，workspace `w1`，Lead pane `w1:p1` |
| 计算出的 team key | `sha256("w1|w1:p1")[:16]` = `5940f14f4143682b` |
| 生产状态目录中同 key 文件 | `~/.local/state/herdr-team/teams/5940f14f4143682b.json` |
| 该文件内容 | 2026-11-03 的 `dev-review`，`status=partial`，context 同样是 `{workspace: w1, lead_pane: w1:p1}` |
| 本次实际结果 | 靠 `HTM_STATE_DIR` 指向独立目录才隔离成功，两个文件互不影响 |

**这不是巧合**：`default` session 当前持有的是 `wX` / `w0` / `w12`，而历史某个已关闭的 session 曾用过 `w1`。说明同一台机器上不同 session 复用 `w1` 是**常态**而非罕见情况。

## 3. 影响

若用户**不**设置 `HTM_STATE_DIR`（默认行为），两个 session 的 team 会命中同一文件，后果包括：

1. **覆盖**：后建的 team 直接 `save()` 覆盖前一个，前一个的 roles / resources 记录丢失。
2. **误复用**：`init` 读到 `status=ready` 的既有 team 会尝试复用其中登记的 Agent；若这些 Agent 属于另一个 session，`verify_existing` 的效果取决于 `--session` 路由是否正确——但账本里没有记录 session，无从判断。
3. **Run 串号**：Run 文件按 run-id 命名（12 位 hex），碰撞概率低，但 `require_run` 的校验是 `run["context"] != ctx`，两个 session 的 context 若相同则**校验形同虚设**。

第 3 点最危险：Run 归属校验依赖的正是那个不含 session 的 `context`。

## 4. 现有的部分防护（不足以解决问题）

`pane_of_current_process()` 在扫描 panes 时做了跨 session 检查：

```python
for seen, key, label in ((seen_panes, (workspace, pane_id), "Pane"),
                         (seen_tabs, (workspace, tab_id), "Tab")):
    if key in seen and seen[key] != name:
        raise Error(f"跨 session {label} ID 冲突: {key}")
```

**它挡的是"两个 session 同时运行且 ID 重复"**。一旦其中一个 session 停止（或 ID 空间不重叠），检查就失效，而账本键冲突依然存在。

Issue #6 验收现场正是这种情况：两个 session 同时运行但 ID 空间无交集（`w1` vs `wX`/`w0`/`w12`），跨 session 检查不触发，账本键却已经撞上了生产目录里的历史文件。

## 5. 候选取向（未设计，需先决策）

| 取向 | 做法 | 主要代价 |
|---|---|---|
| A. 键加 session 维度 | `sha256(f"{session}|{workspace}|{lead_pane}")` | 所有既有账本失效，需要迁移或兼容读取；与 §4.3 的"三键 context 契约"冲突 |
| B. 记录并校验 session | 账本内新增顶层 `session` 字段，读取时比对 | 需要处理无该字段的旧账本（视作"未知"并 fail closed，还是兼容放行） |
| C. 显式拒绝多 session | 检测到 >1 running session 时，若未设置 `HTM_STATE_DIR` 则拒绝写入 | 用户体验差，但实现最小、最安全；可作为 A/B 落地前的临时阻断 |
| D. 维持现状 + 文档声明 | 明确要求多 session 场景必须用 `HTM_STATE_DIR` | 依赖用户自律，不符合 fail-closed 原则 |

**推荐**：先落 C 作为阻断（成本最低、可直接消除数据损坏风险），再设计 A 或 B 作为正式方案。**不建议 D。**

## 6. 验收标准（草案）

- [ ] 两个 session 共用默认状态目录时，第二个 `init` 必须被明确拒绝（错误信息需指明 key 冲突与解决方式），而不是静默覆盖。
- [ ] 拒绝时不得产生任何半途写入（无 partial 账本、无 Herdr 写入）。
- [ ] 既有 v4.2 账本在单 session 场景下行为不变（零迁移）。
- [ ] 两个 session 使用不同 `HTM_STATE_DIR` 时互不影响（现状行为不得回退）。
- [ ] 有回归测试覆盖：同 key 不同 session、旧账本无 session 字段、并发 init。

## 7. 明确不在本 Issue 范围

- 不解决 Herdr 侧 ID 空间隔离问题（那是上游设计，非 HTM 可控）。
- 不涉及 `--machine` 远端场景的账本路径（需单独设计）。

## 8. 已实施的最小缓解（2026-11-05）

采用**身份边界校验**而非改账本键，理由见 `p1-ledger-identity-boundary.md`：改键会让 11 个既有 team、16 个既有 Run 全部失效，且与 `context` 三键契约冲突。

已落地：

1. 新账本写入 `session` 字段，读取时不匹配即拒绝（含只读）。
2. 旧账本按操作档位控制：只读放行并标注 `legacy`；会改变状态或派单的 C 档要求至少一个 Worker UUID 可在当前 session 验证。
3. 任何已发现的角色身份冲突 → 整体拒绝。
4. `pause` 保留为无身份时的安全停止通道，并记录 `identity: unverified`；`resume` 归 C 档。
5. `finish` 归 C 档（Owner 裁决：不允许错误 session 完成其他团队的 Run）。

**残留限制（未解决）**：账本键仍是 `sha256("workspace|lead_pane")[:16]`，不含 session。两个 session 若共用同一状态目录且 ID 相同，**仍会命中同一个文件**——第二个 session 的 `init` 会因身份校验被拒绝（fail closed），不会静默覆盖，但用户必须显式设置独立的 `HTM_STATE_DIR` 才能使用。这是可接受的安全降级，不是隔离通过。

**后续若要做正式方案**（键加 session 维度或引入迁移机制），仍需独立设计。
