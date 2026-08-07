# Grill Report: ADR-16 Round-trip 污染面审查

**角度**: 闭环未封污染面(round-trip 完整性)
**日期**: 2026-08-07
**审查者**: grill teammate (group-orche)

---

## 【查证】逐路径核实

### 路径1: `init-memory` (bootstrap.py)
**现状**: `bootstrap.py:47` `glob("*.md")` **无过滤**
**污染**: ✅ **真污染** — 投影 md (`source: mem-service` frontmatter) 被 re-ingest:
- 产生假 session `memory:mem-<id>.md#0` (`bootstrap.py:65`)
- fact_type 冲突 (投影 md 来源 KG fact 可能是 `stable`，但 bootstrap 强制 `permanent`)
- 措辞偏移时重复 ADD (投影 md 是 KG→CC 转述，re-ingest 后 LLM 重抽可能措辞微变)

**修复**: **已规划 (Node A, T_A1)** — ADR-16f 要求 bootstrap 跳过 `source: mem-service` 的 md
**当前状态**: 未实施 (代码仍 `glob("*.md")` 无过滤)

---

### 路径2: `autodream _read_transcript` 吃投影 md 内容
**现状**: `autodream.py:37-73` 只读 transcript 的 user/assistant `message.content`(跳过 tool_result)
**污染**: ❌ **非污染** — 理由:
1. `_read_transcript` 不读文件系统 md，只读 transcript JSONL
2. transcript 里 agent 引用投影 md 内容(如"用户 uses rust")是对话语义的一部分
3. autodream 增量决策走 **UPDATE 路径** (`autodream.py:184-203`) — 相同三元组仅更新 `seen_sessions`/`source_refs`，不产生重复 fact
4. compact-session 加入 `seen_sessions` 是正确的强化信号(该 session 确实包含该 fact 的语义表述)

**结论**: 幂等/无害，无需封。

---

### 路径3: SessionStart build-index hook (defer 项)
**现状**: **defer** (`mem-service-wiring-deferred.md:24` 明记)
**若实现**: 会调用 `build_index --scope <cwd>` 投影 KG 高 LIF fact → CC memory
**污染**: ❌ **非污染** — build-index 是 KG→CC 投影方向，不是 CC→KG re-ingest 方向:
- 投影 md 已有 `source: mem-service` 标记
- 若 bootstrap 已实施过滤 (路径1 已封)，则 build-index 产出的投影 md 不会被 re-ingest
- build-index 本身不读 memory/*.md，只写

**结论**: defer 项，需等路径1 (bootstrap 过滤) 先封。

---

### 路径4: CC→KG 反向 re-ingest (defer 项)
**现状**: **defer** (`mem-service-wiring-deferred.md:23`)
**描述**: 用户编辑 memory/*.md → KG 同步
**若实现**: 会读 memory/*.md 内容抽取 fact
**污染**: ✅ **潜在真污染** — 若不过滤 `source: mem-service`:
- 投影 md 被当成用户手写 md re-ingest
- 产生重复 fact 或 `seen_sessions` 污染

**修复**: **必须加过滤** — 该 re-ingest 路径需复用 bootstrap 同样过滤逻辑(跳过 `source: mem-service` md)

---

### 路径5: MEMORY.md `[mem]` 索引行本身
**现状**: `[mem]` 行是纯文本索引 (`projection.py:59-66`)
**污染**: ❌ **非污染** — 理由:
1. `[mem]` 行不含三元组语义，只是索引格式: `- [mem] subject predicate value(memory/mem-<id>.md) — LIF X · kg://fact/<id>`
2. 若 LLM 从 `[mem]` 行抽取，会抽到索引元数据(文件路径/LIF 值/kg:// URL)，而非业务三元组
3. 即使抽到类似 `(subject, predicate, value)` 的子串，也是重复的 exact match，走 autodream UPDATE 路径(不产生重复 fact)

**结论**: 索引行结构特殊，抽取难产生有效三元组，幂等无害。

---

## 【结论】未封污染面清单

| 编号 | 路径 | 是否真污染 | 修法 | 优先级 |
|------|------|-----------|------|--------|
| P1 | `init-memory` (bootstrap.py:47) | ✅ 是 | Node A T_A1 已规划: 加 frontmatter 解析，跳过 `source: mem-service` | **P0** (ADR-16f 核心修复) |
| P2 | CC→KG 反向 re-ingest (defer 项) | ✅ 潜在 | 实现时必须加过滤(复用 bootstrap 逻辑) | P1 (defer 实现时加) |
| P3 | autodream _read_transcript | ❌ 否 | 无需修复 | — |
| P4 | SessionStart build-index hook | ❌ 否 (方向相反) | 无需修复 | — |
| P5 | MEMORY.md `[mem]` 索引行 | ❌ 否 | 无需修复 | — |

---

## 【修订建议】

### 1. P1 (bootstrap 过滤) — 立即实施
- **加 Task**: Node A T_A1 已在 `orchestrator-graph.json`，需尽快实施
- **验证**: general_test 断言 `source_refs`/`seen_sessions` 不含 `mem-*.md` 假 session
- **范围**: 仅跳过 `source: mem-service` 的 md (不过滤其他 `source` 或无 source 的 CC 原生 md)

### 2. P2 (CC→KG re-ingest) — defer 实现时加过滤
- **标 defer**: 在 CC→KG re-ingest 设计文档中显式标注"需过滤 `source: mem-service` md"
- **复用逻辑**: 抽取 bootstrap 过滤函数为公共 utility (`_should_skip_memory_md(path)`)

### 3. ADR-16f 范围评估
**当前 ADR-16f 只封了 `init-memory` 路径**。
- ✅ 已够: 封了当前唯一的 CC→KG re-ingest 路径 (bootstrap 是唯一的 memory md →KG 入口)
- ⚠️ 需补充: 若未来实现 CC→KG 反向 re-ingest，必须复用同样过滤(可在 defer 文档或 ADR 附录标注)

---

## 【尖锐问题】(追问 team-lead)

1. **Node A T_A1 未实施** — `bootstrap.py:47` 仍是 `glob("*.md")` 无过滤，ADR-16f 承诺的"闭环卫生"未封。这是 P0 阻塞。
   - **Q**: 为什么 Node A 还在 pending？是否需立即实施？

2. **ADR-16b (recall-trail 投影) 依赖 --session 参数** — 但 `cli.py:239-245` recall 子命令**没有 `--session` 参数**(Node B T_B1 未实施)。
   - **Q**: 若 Node B 不实施，recall-trail 投影无法工作(因 `seen_sessions` 不会被 recall 写入)。Node B 是否与 Node A 同优先级？

3. **build-index --session 缺失** — `cli.py:270-275` build-index 子命令**没有 `--session` 参数**(Node C T_C2 未实施)。
   - **Q**: 即使 recall 有 --session，若 build-index 不接 --session，recall-trail 投影仍无法消费。Node C 是否是 ADR-16 核心功能？

---

## 【终判】

**ADR-16f 闭环未完全封堵** — 当前只有**规划**(orchestrator-graph.json)，**代码未实施**。

**真污染面**: P1 (bootstrap) + P2 (defer 项，未来实施时需加)。
**看似污染实则无害**: P3 (autodream UPDATE 幂等) + P4 (build-index 方向相反) + P5 (索引行难抽有效三元组)。

**修法**:
1. **立即**: 实施 Node A (bootstrap 过滤) + Node B (recall --session) + Node C (build-index --session)。
2. **未来**: CC→KG re-ingest 实现时加过滤。

**ADR-16f 范围**: 当前够用(只封现有路径)，但需在 defer 文档标注未来路径同样过滤。
