# raw-mem.md — mem-service 记忆迁移

> **来源**:CC memory(`~/.claude/projects/-home-yy-projects-agent-os-v2/memory/`)里 memory-service 相关 + 本会话(2026-08-07)迭代。迁移到独立项目自包含,不再依赖 CC memory。
>
> **日期**:2026-08-07 · 独立项目 HEAD(从 agent-os-v2 cp)· 47 pytest 绿

---

## 1. mem-service 是什么

独立 Python CLI(**无 daemon/端口**),叠加在 Claude Code file-based memory(`MEMORY.md`)之上的 **KG fact 层**(Entity + Fact reified + LIF 五维 + 向量召回 + PreCompact autoDream)。**双轨不改 CC MEMORY.md**(ADR-15 单向投影)。

- **起源**:`agent-os-v2/services/memory-service/`(AO2 仓内)
- **独立**:`/home/yy/projects/memory-service/`(cp 到 AO2 同级,2026-08-07)
- **形态**:纯脚本 CLI(argparse 短命进程)+ SQLite 跨进程持久 + PreCompact hook(bash)触发

---

## 2. 当前能力(2026-08-07,非 v1)

| 层 | 能力 |
|----|------|
| **抽取** | 蝴蝶翼 LLM(Zhipu glm-5-turbo 直连 `open.bigmodel.cn`,N=3 **并行** fan-out + 投票 quorum ⌈n/2⌉),**无 regex 降级**(LLM 不可用即 block) |
| **投票** | `_vote` case-fold 归一(A2A/a2a 合并达 quorum,存原值) |
| **召回** | `α·match + β·centrality + γ·LIF + δ·vec_sim` 加权(ADR-4v2)+ 向量召回融合(`--vector`,解同义/字面盲区) |
| **增量** | PreCompact autoDream(session transcript → KG ADD/UPDATE/DELETE/NOOP 幂等);**多值谓词共存**(`uses` 等不当矛盾 supersede) |
| **衰减** | type-aware LIF decay(ADR-8v2,idempotent,half_life ephemeral=7d/stable=90d/permanent=∞) |
| **持久** | SQLite KG(`data/memory.db`,entity + fact 25 列)+ 向量 L2 cache(`data/embeddings.db`,two-tier L1 内存 + L2 SQLite) |

---

## 3. 12 ADR(v1→v6)

| ADR | 决策 |
|-----|------|
| ADR-1 | cli seam(Python + argv 同管道) |
| ADR-2 | Entity+Fact reified KG(无 MemoryItem, SQLite) |
| ADR-3 | Fact self-contained(value literal + object_id 可选) |
| ADR-4v2 | score = α·match+β·centrality+γ·LIF+δ·vec_sim(supersede ADR-4 match×lif) |
| ADR-5b | adapter 蝴蝶翼 LLM(N=3 并行 fan-out + 投票;**regex fallback 已移除 2026-08-07**) |
| ADR-8v2 | LIF 五维(freq/recency/spread/coherence/source)+ decay idempotent |
| ADR-9 | recall baseline(hit@k 量化盲区) |
| ADR-10 | PreCompact autoDream(session→KG 增量, exit 0 放行) |
| ADR-11 | 蝴蝶翼 LLM 接入 autodream(regex 降 fallback → 已移除) |
| ADR-12 | KG init bootstrap(CC memory→KG permanent 种子, autodream 复用) |
| ADR-13 | 向量层(embedding provider + recall --vector 融合 + SQLite L2 cache + **on-ingest 预计算 2026-08-07**) |
| ADR-14 | source_cwd b 方案(单体 KG + cwd 标签, recall --cwd 过滤 OR NULL 兼容) |
| ADR-15 | 分布式 index(KG→CC memory 投影, `[mem]` 标记, 真嵌入) |

---

## 4. 迭代历史

### v1→v6(详见 docs/mem-service-iteration-log.md,原仓)
- **v1**(cd4dcc9):KG 基础(schema/store/recall/ingest regex/consolidate)
- **v2**(d7f8058):decay + pagerank + baseline(盲区实证 synonym hit@5 0%)
- **v3**(00fdf10):adapter 蝴蝶翼 LLM + LIF 五维 + ScoreTune(调参无解 → 真解=向量/LLM)
- **v3b**(1a90939):PreCompact autoDream hook
- **v4**(c393cec):蝴蝶翼 LLM 接入 autodream(LLM 4 fact vs regex 2)
- **v5**(2350b0d):KG init bootstrap(22 CC memory → 72 fact)
- **v6**(07f1e5c/cea7557):向量层 embedding provider + recall --vector 融合(qwen3-4b blind 0%→57.1%)
- **修正**:qwen3-4b 默认(碾 nomic)/ ZhipuAnthropicProvider 直连 / source_cwd / 向量持久化 / 分布式 index / build-index 严格 cwd

### 本会话(2026-08-07,regex/ccr 移除 + 3 盲区修复 + 独立化)
- **commit f58449b** — 移除 regex 降级 + ccr 代理:
  - adapter 蝴蝶翼**并行** fan-out(ThreadPoolExecutor)+ 无 provider/全 error → RuntimeError(**block**)
  - 删 CCRProvider,ZhipuAnthropicProvider 直连(timeout 30→60)
  - cli 默认 `adapter.default_providers()`,去 `--regex` flag
  - bootstrap 大 md 分段(CHUNK=4000,覆盖全文非截断)+ 单段 RuntimeError → skip(不崩整体)
  - conftest RegexMockProvider + autouse(测试确定性走 LLM 路径)
- **commit 3fadf89** — 修 3 类召回盲区:
  - **Design 1 DELETE 多值**:`_FUNCTIONAL_PREDICATES`(is_a/belongs_to 单值真矛盾)vs multivalue(uses 等共存);`_find_active_fact` 扫 active+superseded(防重跑震荡);e2e 产品Z uses 3 value added=3 deleted=0
  - **Design 2 抽取覆盖**:`_EXTRACT_PROMPT` few-shot + 专有名词保留 + predicate 扩 part_of/relates_to;`_vote` case-fold;e2e a2a 字面 0→4 hits
  - **Design 3 向量通电**:`store.put_fact` 下沉 embed(value)预热 L2 + `cli embed-backfill` 回填 + conftest embedding mock;e2e embed-backfill 86 value,recall a2a --vector 20 hits
- **commit 62f795f** — INSTALL.md(6-task 接线 guide)+ SKILL.md 修过时
- **独立 cp** — `/home/yy/projects/memory-service/`(13 .py 2831 行,networkx 唯一三方,0 agent-os-v2 依赖,47 pytest 独立绿)

---

## 5. 接线 CC(6 task,详见 INSTALL.md)

1. **LLM provider**(必须):ZHIPU_API_KEY(env)或 CCR config(`~/.claude-code-router/config.json` Providers zhipu-anthropic.api_key)
2. **Embedding provider**(可选):LM Studio(16666, lms load qwen3-embedding-4b)+ Ollama(11434)
3. **skill 软链**:`~/.claude/skills/mem → 项目目录`
4. **PreCompact hook**:`~/.claude/settings.json` hooks.PreCompact → `hooks/pre-compact-mem.sh` 绝对路径
5. **初始化**:`cli init-memory`(CC memory → KG 种子)+ `cli embed-backfill`(向量回填)
6. **验证**:`cli recall <query>` / `recall <query> --vector`

---

## 6. 双轨(与 orchestrator memory)

| | mem-service(本项目) | orchestrator/src/memory/(AO2 仓) |
|---|---|---|
| 形态 | CLI 脚本(argparse 短命) | Python class,集成运行时 |
| 宿主 | Claude Code(shell 调用) | AO2 native agent(Python 进程) |
| 触发 | CC `/compact` hook / `/mem` | agent turn 内自动 |
| 文件 | 13 .py / 2831 行 | 57 文件(四层分层 + side agent) |
| 互引 | **零**(独立) | 零(不引用 mem-service) |

**解耦根因**:宿主进程不同(CC shell vs AO2 Python),mem-service 必须独立 CLI + SQLite 跨进程,不能复用 orchestrator Python class。

---

## 7. 关键教训 / 踩坑

- **运行时 db 并发写**(feedback-workflow-verify-blindspot 盲区 5):mem-service `data/memory.db` 被 hook/多 CC 实例并发写(WAL),单次 sqlite3 只读快照不可信 —— 声明"零产出/空"前看 `ls -la` mtime + `.bak` 备份 + `.db-wal`/`.db-shm` + 多次查询对比 + `pgrep -f` 看谁在写。本会话连续 3 次基于过期/不完整快照误判被打脸。
- **Zhipu key fallback**:`ZhipuAnthropicProvider._load_zhipu_key()` = env `ZHIPU_API_KEY` → CCR config 两段。env 无时 CCR config 兜底(`~/.claude-code-router/config.json` Providers zhipu-anthropic.api_key)。声明"LLM 不可用"前看 fallback,非只看 env。
- **大 md 分段非截断**:bootstrap init-memory 对长 .md(MEMORY.md 21KB 等)切 CHUNK=4000 段各喂 LLM(覆盖全文),非截断丢后部。截断是 ponytail 简化(防超时),分段是正解。
- **autodream DELETE 单值假设**:原 ADR-10 把同 (subject,predicate) 多 value 当矛盾 supersede,但多值可共存(项目用多工具)。修:functional(is_a/belongs_to)单值 vs multivalue(uses 等)共存。
- **adapter 蝴蝶翼并行**:fan-out 3 wing 用 ThreadPoolExecutor(非 serial),3x 加速 + 单 wing 超时不拖累其他。
- **store.put_fact 下沉 embed**:on-ingest 预计算 fact value embedding 入 L2 cache(单 chokepoint,所有 fact 写入经 put_fact)。conftest 必须配套 patch embedding(防 test 真发网络)。
- **state-fire-recall-route-bug**(orchestrator,非 mem-service):orchestrator `_state.fire → None` 丢返回值,致 `/v1/memories` recall 路由不可达。63e2049 已修(`→ Any`)。mem-service不经 _state(cli 直接 store/recall),不受影响。
- **CC `&&` 短路**:bash `cmd1 && cmd2` 若 cmd1 exit 非0,cmd2 不跑 —— 调查命令用 `;` 分隔 + echo 标识 + exit code,避免短路误判(本次 ZHIPU key 调查首次被 `&&` 短路误导)。

---

## 8. 仍 defer

- autoDream daemon(常驻进程,L1 累积/pagerank 持久)
- 冷层类聚 / 跨 scope 向量联邦(多 CC project)
- query 独立 cli(现 `recall --verbose` 替代)
- **存量 superseded backfill**(Design 1 只防新增,旧 KG 43 superseded 需 backfill 脚本复活多值谓词的)
- CC→KG 反向 re-ingest(用户编辑 memory/*.md → KG)
- SessionStart build-index hook(新触发投影)

---

## 9. 文件索引

- **代码**:13 .py(cli/adapter/autodream/bootstrap/db/embedding/extractor/llm_provider/projection/recall/scoring/store/consolidate)
- **schema**:`schema.sql`(entity 5 列 + fact 25 列含 source_cwd)
- **接线**:`INSTALL.md`(6 task)+ `SKILL.md`(CC /mem 用法)
- **hook**:`hooks/pre-compact-mem.sh`
- **测试**:`tests/`(9 文件,47 pytest;conftest RegexMockProvider + _NoVecProvider autouse)
- **原仓文档**:`agent-os-v2/docs/mem-service-iteration-log.md`(v1→v6 完整 12 ADR)+ `docs/mem-service-architecture.html`
