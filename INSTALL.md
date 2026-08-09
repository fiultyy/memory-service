# mem-service 安装与接线 Guide

mem-service = 独立 Python CLI(无 daemon/端口),叠加在 CC MEMORY.md 之上的 KG fact 层
(Entity + Fact reified + LIF 五维 + 向量召回 + PreCompact autoDream)。**双轨不改 CC MEMORY.md**。

本 guide:部署到新环境 / 新 CC 的 step-by-step。能力详见 `docs/mem-service-iteration-log.md`(12 ADR, v1→v6)。

## 能力速览(当前,非 v1)
- **抽取**:蝴蝶翼 LLM(Zhipu glm-5-turbo 直连,N=3 并行 fan-out + 投票),**无 regex 降级**(LLM 不可用即 block)
- **召回**:`α·match + β·centrality + γ·LIF + δ·vec_sim` 加权(ADR-4v2)+ 向量召回融合(`--vector`,解同义/字面盲区)
- **增量**:PreCompact autoDream(session transcript → KG ADD/UPDATE/DELETE/NOOP 幂等);多值谓词共存(`uses` 等不当矛盾 supersede)
- **持久**:SQLite KG(`data/memory.db`)+ 向量 L2 cache(`data/embeddings.db`)

## 前置
- Python 3.10+
- repo:`/home/yy/projects/memory-service/`(本仓,2026-08-07 从 AO2 `services/memory-service/` cp 独立)
- 三方依赖:`networkx`(唯一,`pip install networkx`)

## 接线 6 task

### 1. LLM provider(必须 — 抽取 fact)
`ZhipuAnthropicProvider` 直连智谱 glm-5-turbo(`open.bigmodel.cn/api/anthropic`)。key 两源(任一):
- env:`export ZHIPU_API_KEY=<key>`
- CCR config:`~/.claude-code-router/config.json` → `Providers[zhipu-anthropic].api_key`

验证:
```bash
cd /home/yy/projects/memory-service && python3 cli.py ingest "项目使用 rust 做后端"
# → {"entities": N, "facts": [...]} 非空 = LLM 通
```
无 key → `RuntimeError` block(**不降级 regex**)。

### 2. Embedding provider(可选 — 向量召回解同义盲区)
LM Studio + Ollama(local OpenAI-compat),`default_providers() = [LM_STUDIO 16666, OLLAMA 11434]`:
- LM Studio:`lms load qwen3-embedding-4b`(port 16666)
- Ollama:`ollama pull qwen3-embedding:4b`(port 11434)

两者都不在 → 向量召回禁用(passive,字面召回仍工作)。

验证:`curl localhost:16666/v1/models`

### 3. skill 软链(CC `/mem` 入口)
```bash
ln -s /home/yy/projects/memory-service ~/.claude/skills/mem
```
验证:`ls -l ~/.claude/skills/mem` → 指向本仓

### 4. PreCompact hook 注册(`/compact` 前抢救 session → KG)
`~/.claude/settings.json`:
```json
{
  "hooks": {
    "PreCompact": [{
      "matcher": "*",
      "hooks": [{
        "type": "command",
        "command": "/home/yy/projects/memory-service/hooks/pre-compact-mem.sh",
        "timeout": 60
      }]
    }]
  }
}
```
验证:CC `/compact` → hook stdout `PreCompact [...] completed`;KG 新增 stable fact(autodream 增量)。

### 5. 初始化(新装 / 空 KG)
```bash
cd /home/yy/projects/memory-service
# KG 种子:CC memory .md → permanent fact
python3 cli.py init-memory --memory-dir ~/.claude/projects/<encoded-cwd>/memory --cwd <repo>
# 向量回填:active fact value → L2 cache
python3 cli.py embed-backfill
```
注:大 memory 目录 init 慢(LLM 蝴蝶翼,~10s/文件);**分段自动**(CHUNK=4000,覆盖全文);单文件超时 → skip(不崩整体)。

### 6. 验证
```bash
python3 cli.py recall "<本 repo 主题词>"          # 字面命中
python3 cli.py recall "<同义词>" --vector          # 向量召回(解盲区)
sqlite3 data/memory.db      "SELECT COUNT(*) FROM fact WHERE status='active'"
sqlite3 data/embeddings.db  "SELECT COUNT(*) FROM embed_cache"
```

## 故障排查
| 症状 | 根因 | 修 |
|------|------|-----|
| `RuntimeError: no reachable LLM provider` | ZHIPU_API_KEY 未设 + CCR config 无 zhipu-anthropic | 设 env 或 CCR config |
| `recall --vector` 0 / embeddings.db 空 | LM Studio/Ollama 未跑 / `embed-backfill` 未跑 | 启 provider + 跑 embed-backfill |
| hook 不触发 | settings.json command 路径错(非绝对) | 用绝对路径 |
| superseded 占比高(>20%) | 旧 KG(Design 1 多值共存前灌的存量) | 重新 init(多值生效)或 backfill 复活脚本(follow-up) |
| init 慢 / 超时 skip 多 | 大 .md + LLM 慢 | 已自动分段 + 容错 skip;或减 CHUNK |

## 子命令(cli.py)
| 命令 | 作用 |
|------|------|
| `ingest "<text>"` | 蝴蝶翼 LLM 抽 fact 入 KG |
| `recall "<q>" [--vector] [--bfs] [--as-of <ts>] [--cwd <cwd>] [--top-k <n>]` | 加权召回(字面 / 向量 / BFS / 点时) |
| `consolidate` | LIF decay + 精确重复 dedup |
| `autodream --session <id> --transcript <jsonl> [--cwd]` | session transcript → KG 增量(ADR-10/11) |
| `init-memory --memory-dir <dir> [--cwd]` | CC memory .md → KG permanent 种子(ADR-12) |
| `re-ingest <file>` | 单 md → KG 增量(ADR-17) |
| `synthesis-index [--scope <cwd>] [--memory-dir <dir>] [--session <id>]` | 散 mem-*.md 对账 → MEMORY 投影(ADR-15 P2, 唯一写入口) |
| `prune [--scope <cwd>] [--memory-dir <dir>]` | 删除 KG 中无对应 memory .md 的孤儿 fact |
| `embed-backfill` | active fact value → L2 向量 cache |
| `stats` | 只读 churn 快照(entity/fact 计数 + status 分布) |
| `dream-daemon [--cwd] [--interval <s>] [--once]` | 常驻 autoDream loop(operational #1) |

详见 `SKILL.md`(CC `/mem` 用法)+ `docs/mem-service-iteration-log.md`(12 ADR + 完整迭代)。
