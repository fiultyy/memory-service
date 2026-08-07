# Grill 终稿:噪音注入 CC 热层 + 形式1 边际价值

## 【查证】
1. **Recall 返回集 score 分布**:`recall.py:249` 仅过滤 score=0;`recall.py:251-252` top_k 截断在过滤后,但 **top_k 默认 None**(`cli.py:134`)→ recall 返回所有 score>0 的 fact,尾巴低相关全量返回。
2. **轨迹集 vs LIF top-K 重叠**:形式1 查 seen_sessions LIKE;形式2 ORDER BY LIF DESC LIMIT top_k。关键:`scoring.refresh_lif_on_recall`(`scoring.py:325-414`)recall 时提升 LIF(access_count++/last_accessed/seen_sessions 扩充)→ 高 LIF fact 本身就是反复被召回的 → **天然高度重叠**,形式1 注入的大概率已被形式2 覆盖。
3. **Description 入 context 污染**:`projection.py:38` 投影 md frontmatter description;CC 开局所有投影 md(含低相关尾巴)description 入 context → 尾巴占 token 无召回价值。

## 【结论】
1. 噪音注入:**成立**。recall 无默认 top_k 截断,低相关尾巴被 seen_sessions 标记 → 轨迹投影 → description 入 CC context,污染面 = 尾巴数 × description token。
2. 形式1 边际价值:**高度怀疑但非多余**。LIF top-K 天然覆盖反复召回的高 LIF fact;形式1 唯一边际 = 低 LIF 但本次召回的(新事实/长期未访问/cwd 专属)。

## 【修订建议】
1. ADR-16c 修订:加 score 阈值过滤(建议 0.3,与 ADR-4v2 α=0.5 对齐,match≥0.6 达标)。
2. ADR-16 Consequences 新增:明确形式1 边际价值场景(新事实/长期未访问/cwd 专属)。
3. Node C verify acceptance 加:`"低相关尾巴 fact(score < 0.3)不投影(噪音过滤)"`。

**形式1 非多余,但必须加噪音过滤**,否则低相关尾巴灌入 CC 热层是设计缺陷。阈值一行代码解决。
