# Round 2 终稿:形式1 边际价值量化

## 【查证】LIF 提升速率推演(scoring.py:44-50 LIF_WEIGHTS + 公式)
- 冷启动 fact(从未 recall):LIF≈0.555(source=0.7, freq=0, recency≈1.0)
- 首次 recall 后:LIF≈0.600(freq=0.181)
- 3 次 recall 后:LIF≈0.668(freq=0.451)
- 老高 LIF fact(10 次 recall):LIF≈0.771(freq=0.865)
- **首次 recall 的 fact LIF~0.60,低于老 fact 0.77+(gap 17%+)**,top-K 竞争激烈时不进 → 形式2 漏。

## 形式1 唯一覆盖窗口
1. 新事实首次 recall(LIF~0.60,freq/spread 拖累)
2. 长期未访问重新 recall(recency=1.0 但 freq 低,需多次才进 top-K)
3. cwd 专属低 LIF fact(全局 top-K 竞争激烈)

## 【结论】形式1 边际价值高,保留但限场景
窗口期真实存在(LIF gap 17%+),砍则丢"本次 session 实际召回"语义。但有噪音风险(recall 无 top_k 截断,低相关尾巴被 seen_sessions 标记 → 投影污染)。

## 【修订方案】保留形式1 + 两道过滤
1. score 阈值 0.3(修正噪音,见 noise-threshold-basis)。
2. 可选限定场景(优化边际):只投影首次召回(access_count=1)或 LIF<0.65 的轨迹 fact,避开已被形式2 覆盖的高 LIF 老 fact。**可 defer**(优化项,非阻塞)。

ADR-16 Consequences 新增:形式1 边际场景 + score 阈值过滤。
