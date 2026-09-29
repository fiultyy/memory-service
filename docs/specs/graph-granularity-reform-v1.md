# 图记忆粒度改造 spec v1 (定稿, 2026-09-29)

- v0→v1: 四裁决落定; 依用户三项方向裁决 + 交互四裁决
- 病根与动机见 v0 §一(50 对标注实测: 裸关系边 P(high) 0.18-0.30, gate 全负基线 78% 不可战胜)

## 一、模型

**原子 = chunk(完整语义段)**; 每 chunk 携带: 结论句(人话 topic 句, 必填)、原文引用
(source_refs 溯源)、时间/session/cwd、结论句 embedding。

**边 = 开放边集(用户裁决)**: LLM 抽取时自由给出边类型字段值(描述性: 类型/话题/归属/
总结/补充/矛盾…不设封闭词表), **laya 判定该边成立后才入图**。参考边型(非封闭):
topic_of / type_of / belongs_to / summarizes / supplements / contradicts / supersedes。

**切分(用户裁决)**: regex 以标点符号为原子单元 → 打包填充 ≤8k(对齐 laya TOKEN_BUDGET)
→ **批量给 laya 做语义 chunk 聚合**(laya 输出: 哪些句聚成一个 chunk + 该 chunk 结论句 +
挂载边提案)。LLM(蝴蝶翼)不再直接产三元组, 职责上移为 laya 聚合结果的校验/补全。

## 二、schema 映射(增量, 不推翻表)

- `fact.value` = 结论句(非裸实体); predicate = laya 判定后的边类型(开放)
- entity 层**随 G2 瘦身**(用户裁决): 无 chunk 挂载的孤例实体硬删, 别名随迁, 消歧机制保留
- `EdgeOut.topic` 升格必填; fact_relations(语义边表)沿用承载 chunk↔chunk 横向边

## 三、实施票

- **G1 抽取管道**: 标点单元切分器 + ≤8k 打包器 + laya 聚合批(输出 chunk 结论+边提案)
  + 入库(结论句 embedding); regex/gazetteer 老通道降级为仅产归属信息
- **G2 存量全量重组**(裁决 #2): 现有 23384 fact 全量重织成 chunk 关系;
  **无法聚合重织的直接物理 DELETE**(仓内首破无删除惯例; 迁移前 db 文件快照, 单写者窗口);
  entity 同步瘦身
- **G3 laya 全量关系重建 + 抽样交互**(裁决 #3): 重组后 laya 对全量 chunk 批量重建关系边;
  从重建结果抽样走交互式 Y/N 由用户参考判断(人审样本锚 laya 边质量)
- **G4 召回/gate 重校准**: cand_texts 换结论句; 新分布重跑标注; 阈值实测

## 四、裁决记录(全部已定)

1. ✅(#1) 原子换 chunk, 边为含义抽象描述关系
2. ✅(#2) 存量全量重组, 无法聚合者硬删(快照兜底)
3. ✅(#3) 重组后 laya 全量重建 + 抽样交互 Y/N
4. ✅ 边集完全开放: LLM 产字段值, laya 判后入图
5. ✅ entity 层随 G2 瘦身
6. ✅ 切分: regex 标点单元 ≤8k 打包, laya 语义聚合
7. ✅ 新分支 iterate/graph-reform 立即开 DAG: G1→G2→G3→G4(G2 起 G3 可并行准备)
