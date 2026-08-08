# Fix Tickets: projection-native-format 硬化

- **Date**: 2026-08-08
- **审查依据**: 对抗式 5 维度 review（10 确认缺陷 / 5 反驳），主 agent 实测复核
- **范围**: ADR-A/B/C 迭代的投影写入防御 + 投影判定单一源 + 投票 topic 完整性
- **根因总结**: topic 从 LLM 一路流到"写文件/删文件"是通的，但写文件这一步没有把 topic 当**不可信外部输入**对待——没有 YAML/markdown 转义、没有长度截断、删除标记和识别标记用了两套口径。

---

## 执行批次与依赖

```
Batch 1 (投影写入防御, projection.py)   Batch 2 (判定单一源)   Batch 3 (投票)
  F4 YAML 转义                            F1 prune 判定            F5 _vote topic
  F6 slug 截断+字符防护                   F2 删除标记收紧
  F7 markdown link 转义
        \___________ ___________/              |                   |
                    V                            V                   V
              Batch 4 (回归测试, 依赖 1+2+3)
                T1..T6
```

- **Batch 1/2/3 互相独立，可并行**（不同文件/函数，无共享状态）。
- **Batch 4 必须在 1+2+3 之后**（测试断言修复后的行为）。
- 推荐执行序: 1 → 2 → 3 → 4（1 收益最高、风险最低，先落）。

---

## Ticket F4 — YAML frontmatter description 转义

| 项 | 值 |
|---|---|
| **严重度** | MAJOR（实测复现，破坏迭代核心目的） |
| **文件** | `projection.py` |
| **根因** | `project_fact_md` line 100 写 `description: {topic}` 原文，topic 含冒号/引号破坏 YAML 解析。`_fact_topic`（line 62-68）只 strip `\r\n`，不处理冒号。 |
| **实测** | topic=`config ratio: 3:1 per "zone"` → `yaml.safe_load` 报 `mapping values are not allowed here`。CC 靠 description 代码层召回 → 该 fact 对 CC 不可见。 |

### 改动
1. `projection.py` 新增 helper `_yaml_scalar(s: str) -> str`（紧邻 `_fact_topic`）：
   ```python
   def _yaml_scalar(s: str) -> str:
       """YAML scalar 安全输出: 含特殊字符则双引号包裹并转义。

       description 是 CC 召回命中的字段, 必须保证 yaml.safe_load 能解析。
       """
       s = s.replace("\r", " ").replace("\n", " ").strip()
       if not s:
           return '""'
       # 需引号的触发: 冒号/井号/引号/方括号/花括号/反引号/首尾空格/以特殊符开头
       if re.search(r'[:#"\'\[\]{}`]', s) or s[0] in "!&*?|-+>%@":
           return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
       return s
   ```
2. `project_fact_md` line 100：`description: {topic}` → `description: {_yaml_scalar(topic)}`。
3. line 105 标题 `# {topic}`：保持（markdown ATX 标题对冒号免疫；换行已 strip）。仅当 topic 含前导 `#` 时退化——极低概率，不单独处理（若需，可 `topic.lstrip('#')`，但改语义，不做）。

### 验收
- topic=`a: b`、`含"引号"`、`key:val:end` 生成 md 后，`yaml.safe_load(frontmatter)` 返回正确 description 字符串，无异常。
- 干净 topic（`用户使用 rust`）输出不变（无特殊字符走原文分支）→ 现有 `test_recall_p1`/`test_synthesis_index` 仍 pass。
- 断言：CC description 召回链路（明文子串匹配）仍能命中——quote 后的 `"` 是可见字符，不影响子串召回（topic 原文作为子串仍存在于 quote 内）。

### 风险
- 双引号包裹后 description 多了 `"` 边界字符。CC 召回是子串匹配（topic 原文仍是子串），不破坏。✓

---

## Ticket F6 — slug 截断 + URL/markdown 不安全字符防护

| 项 | 值 |
|---|---|
| **严重度** | MINOR（verifier 校正；但实测 492 字节文件名，热路径 OSError 未捕获） |
| **文件** | `projection.py` |
| **根因** | `_sanitize_slug`（line 44-53）只替 `[\s/]+`，不截断 → 长 topic 产 >255 字节文件名 → `os.replace`/`write` 抛 `OSError`（非 `_mem_filename` 文档承诺的 `ValueError`），`project_fact_md`/`recall` 未捕获 → 单条坏 topic 致整个 recall/synthesis 崩。 |

### 改动
1. `_sanitize_slug` 增强（line 44-53）：
   ```python
   _SLUG_MAX = 60  # 字符上限: 全中文 60 字 = 180 字节 + "mem-xxxx-.md" ≈ 193 < 255

   def _sanitize_slug(s: str | None) -> str:
       """topic → 路径安全 slug(ADR-B)。只做路径/URL 安全, 不改语义——
       topic 原文完整保留在 description/title(召回靠那里), slug 仅文件名标识。"""
       if not s:
           return "fact"
       s = re.sub(r"[\s/]+", "_", s).strip("_")
       s = re.sub(r"[()\[\]<>\"'`|\\^]", "", s)   # URL/markdown/YAML 不安全字符删除
       s = s[:_SLUG_MAX].strip("_")                # 截断防超 NAME_MAX
       return s or "fact"
   ```
2. **不改** `_mem_filename`（其 `MEM_FILE_RE` 断言保留，作第二道闸）；截断后 slug 仍匹配 `.+`。✓
3. **无需**在 `project_fact_md` 加 OSError 兜底——源头截断已杜绝超长。

### 验收
- topic = `'极长主题' * 40`（200 字符）→ 文件名字节数 < 200。
- topic = `'ratio(3:1)[x]'` → slug 不含 `()`/`[]`/`:` → 文件名匹配 `MEM_FILE_RE`。
- `test_prune` 用例 5（native `mem-service-*.md` 不被误判）仍 pass（serv 非 4-hex 不变）。

### 风险/权衡
- 删 `()`/`[]` 等"改 slug 语义"——但 ADR-B 的 sanitize 约束是"路径安全不改**召回语义**"，召回靠 description（topic 原文），slug 仅文件名标识，简化不违精神。在代码注释标明。

---

## Ticket F7 — markdown 链接文本/URL 转义

| 项 | 值 |
|---|---|
| **严重度** | MINOR |
| **文件** | `projection.py` |
| **根因** | `_format_mem_line`（line 279）`- [{topic}]({fname}) — {topic}`，topic 含 `]` 断 link text、fname 的 slug 含 `)` 断 link URL。CommonMark 实测确认。 |

### 改动
1. 新增 helper `_md_link_text(s: str) -> str`（紧邻 `_yaml_scalar`）：
   ```python
   def _md_link_text(s: str) -> str:
       """markdown link text 安全: 转义 ] [ \\ (不破坏 CC 明文召回, 子串仍命中)。"""
       return s.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
   ```
2. `_format_mem_line` line 279：
   ```python
   return f"- [{_md_link_text(topic)}]({fname}) — {_md_link_text(topic)}"
   ```
   - `fname` 的 URL 部分：F6 已让 slug 删 `()`/`[]`，故 fname 不含断 URL 字符，无需额外 `<...>` 包裹。

### 验收
- topic = `'a]b[c'` → 索引行 link 语法合法（markdown-it parse 无误）。
- topic = `'用户使用 rust'` → 输出与现状一致（无 `[]` 不转义）→ 现有测试 pass。

---

## Ticket F1 — prune 投影判定与 ingestion 判定统一

| 项 | 值 |
|---|---|
| **严重度** | MAJOR（静默 soft-delete native fact，可逆但无告警） |
| **文件** | `bootstrap.py` |
| **根因** | `prune_deleted`（line 187-188）用**文件名** `MEM_FILE_RE` 排除投影文件；而 ingestion 跳过投影靠**frontmatter `source: mem-service`**（`_is_mem_service_projection`）。native 文件命名 `mem-dead-notes.md`（dead=合法 hex，撞正则）→ ingestion 不跳过（无 frontmatter）抽出 fact → prune 把它从 existing 排除 → fact 判孤儿 → 静默删。 |

### 改动
`prune_deleted` line 186-190，对撞正则的文件加 frontmatter 二次确认：
```python
if mem_dir.is_dir():
    existing = set()
    for p in mem_dir.glob("*.md"):
        if p.name == "MEMORY.md":
            continue
        if MEM_FILE_RE.match(p.name):
            # 撞正则: frontmatter 确认是否真投影(source:mem-service)
            # 真投影 → 排除(产物非源); 无 frontmatter → native 撞名, 留 existing
            try:
                txt = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                txt = ""
            if _is_mem_service_projection(txt):
                continue
        existing.add(p.name)
else:
    existing = set()
```
- `_is_mem_service_projection` 已在同模块（line 25-40），直接调用。仅对 `MEM_FILE_RE` 命中的少数文件读 frontmatter，开销可控。

### 验收
- native `mem-dead-notes.md`（无 source frontmatter）+ 其 fact 存在 → prune **不删**该 fact（留在 existing）。
- 真投影 `mem-abcd-xxx.md`（有 `source: mem-service`）→ 仍被排除（行为不变）。
- `test_prune` 用例 5（native `mem-service-native.md` 不被误判）仍 pass。

### 风险
- 读文件 frontmatter 增加 IO，但仅对撞正则文件（正常场景近 0 个）。可接受。

---

## Ticket F2 — MEMORY 行删除标记收紧到 4-hex 口径

| 项 | 值 |
|---|---|
| **严重度** | MINOR（违 ADR-B"全仓唯一单一源"宣称） |
| **文件** | `projection.py` |
| **根因** | 识别投影文件用严格 `MEM_FILE_RE`（4-hex），但**删除** MEMORY 行用松散子串 `](mem-`（line 145，无 4-hex）。CC 原生行 `- [笔记](mem-notes.md)` 被误删。`_rewrite_mem_lines` 每次 synthesis 全删匹配行。 |

### 改动
`projection.py` line 145-151：
```python
# 删除侧收紧到 MEM_FILE_RE 口径(4-hex), 与识别侧单一源一致。
_MEM_LINE_NEW_RE = re.compile(r"\]\(mem-[0-9a-f]{4}-.+?\.md\)")
_MEM_LINE_OLD_MARKER = "(memory/mem-"   # 迁移期旧格式残留, 保留清理


def _is_mem_index_line(ln: str) -> bool:
    """MEMORY 索引行是否为 mem-service 投影行。

    新格式按 MEM_FILE_RE 口径(4-hex)匹配 ``](mem-{4hex}-{slug}.md)``;
    旧格式 ``(memory/mem-`` 迁移期一并清。两者都在 MEMORY 重写时永远删。
    """
    return bool(_MEM_LINE_NEW_RE.search(ln)) or _MEM_LINE_OLD_MARKER in ln
```

### 验收
- CC 原生行 `- [笔记](mem-notes.md) — x`（notes 非 4-hex）→ `_is_mem_index_line` 返回 False → **不删**。
- 投影行 `- [topic](mem-abcd-slug.md) — topic` → 匹配 → 删（行为不变）。
- `test_synthesis_index` T4（CC 原生行保留）仍 pass，且更严格。

---

## Ticket F5 — `_vote` surviving edge 取非空 topic

| 项 | 值 |
|---|---|
| **严重度** | MINOR |
| **文件** | `adapter.py` |
| **根因** | `_vote`（line 144）surviving edge = `wing_edges[0][1]`（首个 wing 整条 EdgeOut）。topic 不参与投票键也不做"取非空"。wing1 topic 空、wing2 有好 topic → 好 topic 丢弃。 |

### 改动
`adapter.py` line 143-148，topic 单独选非空（surface form 仍 first wing，保 case）：
```python
if len(wing_edges) >= quorum:
    edge = wing_edges[0][1]  # first surface form (case preserved)
    if _is_env_entity(edge.subject) or _is_env_entity(edge.object):
        continue
    # topic: 取首个非空 wing 的 topic(避免首 wing 空 topic 遮蔽)。
    # surface form(subject/object case)仍 first wing, 仅 topic 补全。
    if not (edge.topic or "").strip():
        best_topic = next(
            (e.topic for _, e in wing_edges if (e.topic or "").strip()),
            "",
        )
        if best_topic:
            from dataclasses import replace
            edge = replace(edge, topic=best_topic)
    surviving.append(edge)
    for wi, _ in wing_edges:
        contributing_confidences.append(extractions[wi].confidence)
```
- `from dataclasses import replace` 提到模块顶部 import（避免函数内 import）。

### 验收
- 3 wing，wing0 topic=""、wing1 topic="好"、wing2 topic="好" → surviving topic="好"。
- 3 wing 全空 topic → surviving topic=""（行为不变，projection fallback 三元组）。
- 现有 `test_re_ingest`（单 wing fake，topic 非空）仍 pass。

---

## Ticket T（Batch 4）— 回归测试补全

依赖 F4/F5/F6/F7/F1/F2 落地。6 个新断言 + 修正 1 个现有弱测试。

| ID | 文件 | 缺口 | 测试 |
|---|---|---|---|
| T1 | `test_re_ingest.py` | topic 持久化未断言 | ingest 后 `SELECT topic FROM fact WHERE id=?` 断言 = 投入值 |
| T2 | `test_synthesis_index.py` T8 | 没测真换行 | 改 `_mk_fact(topic='line1\nline2')`，断言生成的文件名/description 无换行 |
| T3 | 新增或 `test_re_ingest.py` | `_vote` 多 edge topic 合并 | 构造 2 wing（wing0 topic 空 + wing1 非空）→ 断言 surviving topic 非空 |
| T4 | `test_synthesis_index.py` 新用例 | 长 topic 文件名 | topic 200 字符 → 断言文件名字节 < 255 且 `MEM_FILE_RE` 匹配 |
| T5 | `test_synthesis_index.py` 新用例 | YAML 冒号破坏 | topic=`a: b` → `yaml.safe_load(frontmatter)['description']` == `'a: b'` |
| T6 | `test_synthesis_index.py` 新用例 | markdown `]` 断链接 | topic=`a]b` → 索引行 `markdown-it` parse 无 broken link |
| T7 | `test_prune.py` 新用例 | F1 native 撞名不误删 | `mem-dead-notes.md`（无 frontmatter）+ 其 fact → prune 不删 |

### 全量回归门
```bash
cd /home/yy/projects/memory-service
python3 adapter.py && \
for t in test_bootstrap_skip test_re_ingest test_mem_score test_recall_p1 \
         test_synthesis_index test_prune test_init_memory_defaults; do
  python3 $t.py || echo FAIL_$t
done
# + F4/F6 自检: 含冒号/长 topic 的 throwaway
# 期望: 全 exit 0, data/memory.db 零污染(db.init(tmp) 隔离)
```

---

## 验收矩阵（缺陷 × 修复 × 测试）

| 缺陷 | Ticket | 修复文件 | 回归测试 | 状态 |
|---|---|---|---|---|
| YAML 冒号破坏召回 | F4 | projection.py | T5 | 待修 |
| 长 topic OSError | F6 | projection.py | T4 | 待修 |
| markdown `]` 断链接 | F7 | projection.py | T6 | 待修 |
| prune 误删 native | F1 | bootstrap.py | T7 | 待修 |
| 删除标记口径不一 | F2 | projection.py | (T4 间接) | 待修 |
| _vote topic 丢失 | F5 | adapter.py | T3 | 待修 |
| topic 持久化无测 | — | — | T1 | 待补 |
| T8 假换行测试 | — | test_synthesis | T2 | 待修 |

---

## 不修复（已反驳，记录留痕）

- topic 端到端主路径贯通、P0 `{entities,edges}` 未破、`put_fact` 26 占位符列值对齐、topic 迁移对老库生效、`MEM_FILE_RE` 三处共用常量——均经对抗验证确认无误。
- 生产 `data/memory.db` 当前 0 行：状态问题（无生产调用），非代码缺陷，不在本批。
