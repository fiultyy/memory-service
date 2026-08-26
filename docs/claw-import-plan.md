# Claw 记忆全量导入方案（claw-import-plan）

> 目标：把 OpenClaw claw-02 workspace 的全部高价值记忆导入 mem-service KG
> 生成：2026-08-26 | 基于 mem-service 现有 bootstrap/autodream 幂等管道（ADR-12/17）
> 原则：不改 mem-service 代码，只用现有接口 + 一个分批 driver 脚本

## 1. 数据源盘点与分层决策

| 批次 | 源 | 文件量 | fact_type | 决策 |
|---|---|---|---|---|
| **P1 核心** | `memory/topics-*.md` + `memory-schema.md` | ~62 | **permanent** | 高价值人肉 promote 产物，第一批 |
| **P2 项目档案** | `memory/projects/*.md` | ~10 | permanent | graphiti/cognee 等框架档案 |
| **P3 问题清单** | `memory/questions-*.md` | ~3 | stable | 开放问题会过时 |
| **P4 时间线（可选）** | `memory/YYYY-MM-DD.md` | ~25 | **stable** | 价值密度低，默认跳过；导入则 stable 衰减 |
| 排除 | `MEMORY.md` | 1 | — | ADR-16f 自动跳过（防自指）✅ |
| 排除 | `memory/dreaming/` | 大量 | — | 候选噪音（confidence 0.00 层） |
| 后续 | `memory/kb-sydney/`、wiki syntheses | — | — | 独立库，验证通过后补 |

注意：`init-memory` 只扫 `memory_dir` 顶层 `*.md`（非递归），且无白名单机制 → P1 若直接跑会连带 daily（permanent 不当）。**用 driver 脚本按白名单调 `bootstrap.re_ingest_file()`**。

## 2. 现有能力映射（零代码改动）

| 需求 | mem-service 现成能力 |
|---|---|
| md → KG 增量 | `bootstrap.re_ingest_file(file_path, source_cwd)`（ADR-17b，幂等 ADD/UPDATE/DELETE/NOOP） |
| LLM 抽取 | 蝴蝶翼 N-fan-out + 投票（ADR-5b）；`providers=None` → 直连 Zhipu（.env 已配 ZHIPU_API_KEY ✅） |
| 实体对齐 | resolver：精确/别名闸 → 向量 top-k + LLM 去重（ADR-D3）；LM Studio qwen3-embedding 可接（MEM_LMSTUDIO_URL 已配） |
| 隔离 | `source_cwd` 打 claw 标签：`/home/yy/.openclaw/workspace-claw-02`（ADR-14，recall --cwd 过滤） |
| 矛盾 | "640×350 vs 576×288" 类冲突 → fact supersede/矛盾判定链（ADR 已有，contradiction-judge 测试覆盖） |

## 2.5 锚点修正（2026-08-26 用户指出：wings 不能用于初始提取）

**问题**：蝴蝶翼投票在冷启动失效——①投票对齐无基准（中文开放抽取实体/谓词措辞各异，case-fold 归一不够）；②空库上 ADD/UPDATE/NOOP 退化为重复检测；③resolver 空闸照单全收碎片；④统计层：三翼=同模型采样，错误相关，quorum 独立性假设不成立（运行时可靠性来自 KG 锚点+recall 环境纠偏，非投票本身）。

**修正：锚点先行三段式**：
- **0a（零 LLM）**：种子实体表 ← 文件标题+MEMORY.md pointer+frontmatter 确定性解析（Topic/Category/种子实体带别名）；受控谓词表起步（uses/supplies/part_of/has_spec/contradicts…）
- **0b（有锚提取）**：closed-world 提示（已知实体表优先链接，链接不上标 [NEW] 进 staged）；wings 降级为可选（有锚才有对齐基准）
- **runtime**：wings 归位增量 transcript 工况

**对 §3 driver 的影响**：不再直接调 `bootstrap.re_ingest_file`（其内部 autodream 蝴蝶翼无锚），改为：0a 脚本建种子（直接写 store/entity）→ 0b 锚点约束提取脚本 → 入库时先过 resolver（此时闸非空）

```python
# 放 ~/projects/memory-service/ 下，~15 行
from pathlib import Path
import bootstrap

WS = Path("/home/yy/.openclaw/workspace-claw-02/memory")
CWD = "/home/yy/.openclaw/workspace-claw-02"

def batch(pattern, ftype):
    for f in sorted(WS.glob(pattern)):
        r = bootstrap.re_ingest_file(f, source_cwd=CWD)  # ftype 经 autodream 参数传递
        print(f.name, r)

# 分批执行（fact_type 传参以 cli/autodream 实际签名为准，见 §6 验证步）
```

## 4. 执行步骤

```
Step 0  冒烟（3 文件白名单：topics-雷鸟iO / topics-智能眼镜自研项目战略 / topics-全澳房产数据）
        → 检查 added/updated 分布 + recall 验证
Step 1  P1 全量 topics（~62 文件）
Step 2  P2 projects + P3 questions
Step 3  幂等重跑 Step 1 → 期望 added≈0（noop/update 为主）
Step 4  （决策点）P4 daily 是否导入
```

## 5. 成本估算

蝴蝶翼 N=3 × CHUNK 4000 字符：topics 均 3-4KB ≈ 1 chunk/文件
P1+P2+P3 ≈ 75 文件 × 3 wing ≈ **225 次 LLM 调用**，GLM-4-flash 级 **<¥5**；embedding 走本地 LM Studio 零成本

## 6. 验收标准

1. **幂等**：重跑 P1，`added=0`，noop/update 占比 >90%
2. **召回冒烟**（recall hit@k）：
   - "雷鸟 iO 光引擎体积" → 0.085cc 萤火 Nano
   - "Even G2 主控芯片" → Apollo510b 三芯片
   - "智能眼镜项目芯片路线" → S3 先行/P4 升级
   - "乐奇" → 别名命中 Rokid（resolver 验证）
3. **对账**：KG entity 数 vs MEMORY.md pointer 覆盖域；`--cwd` 过滤生效（claw 标签下无 CC 项目记忆混入）
4. **统计留档**：各批 files/added/updated/noop 写入 `data/claw-import-stats.json`

## 7. 风险与注意

| 风险 | 缓解 |
|---|---|
| 表格密集内容抽取质量 | 蝴蝶翼投票天然抑制单翼幻觉；Step 0 冒烟先验证 3 个表格重的文件 |
| frontmatter 混入正文 | 小噪音可接受；若严重再改 re_ingest 前置剥离（1 行 regex） |
| re_ingest_file 不透传 fact_type | 验证 cli init-memory 的 fact_type 参数路径；不行则 P1 走 init-memory + daily 预移出 |
| .env provider 失效 | Step 0 即暴露（LLM 不可用 raise block，fail-fast） |

## 8. 后续接线（不在本方案范围）

- claw 侧接入：OpenClaw skill/hook 调 `cli.py recall`（SKILL.md 语法），飞书直接问 KG
- runtime 增量：topics 更新时 PostToolUse → re_ingest（ADR-17 hook 位已有）
- wiki claims ↔ KG fact 对齐（双向：synthesis 投影 ↔ fact source_refs）
