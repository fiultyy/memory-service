# Round 2 终稿:0.3 阈值依据 + top_k 兼容

## 【查证】
- `scoring.py:115-118` ADR-4v2 权重:α=0.5(match) β=0.3(centrality) γ=0.2(LIF) δ=0.3(vec)。
- score 范围:无向量 [0,1.0],有向量 [0,1.3]。0.3 = 30% 无向量范围,对应最低 match=0.6(其他维度=0 时)。**0.3 非随意**:与 α=0.5 对齐,match≥0.6 达标(半个以上 token 命中)。
- `recall.py:134` top_k 默认 None;`cli.py:134-143` 仅 cli.recall 调用(grep 无 eval_recall/tests 调用方)。**改默认不破坏兼容**。
- `recall.py:249` 已有 score>0 过滤 → 加阈值仅改一处。

## 【结论】
1. 0.3 阈值合理(α=0.5 对齐,match≥0.6 达标)。
2. top_k 默认 None 仅 cli.recall 用,改不破坏兼容。
3. 应用点:recall.py:249。

## 【修订方案】方案 A(推荐):recall 层过滤
`recall.py:249` 改 `s["score"] > 0.0` → `s["score"] >= 0.3`。一行,投影层自动受益,低相关尾巴不灌 CC。不改 top_k 默认(保留全量语义)+ cli 加 --top-k(默认 None,用户可覆盖)。

方案 B(备选):投影层 LIF 过滤,不治源头(cli 直接打印仍含尾巴),否决。
