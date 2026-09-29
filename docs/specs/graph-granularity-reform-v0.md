# 图记忆粒度改造 spec v0 (2026-09-29, 依用户裁决起草待审)

## 一、病根(50 对标注实测背书)

现图原子 = 实体-谓词-实体 三元组(`X connected_to Y`), 抽取端"抽到什么记什么":
- 2793 distinct 谓词 / 1266 单次出现 → 无质量控制
- 裸关系边零信息量: 对人无用, 对 gate 无判据(P(high) 0.18-0.30)
- gate 在此分布上数学死亡: 全负基线 78% 不可战胜 → **先改图, 再谈 gate**

## 二、新 schema: chunk 语义图(用户裁决 #1)

**原子 = chunk(完整语义段)**, 边 = chunk 含义的**抽象描述关系**, 不是简单谓词。

### 节点
- **Chunk 节点**: 一段完整语义(一个结论/决策/事实/教训), 携带: 结论句(topic 句, 人话),
  原文引用(source_refs→chunk 文本), 时间, 来源 session/cwd, embedding(结论句向量)
- **Topic 节点**: 话题/类型实体(已有的 entity 层降级复用——不再是图主角, 是 chunk 的挂载点)

### 边(封闭 5 种描述关系, 取代 2793 野谓词)
| 边 | 含义 | 例 |
|---|---|---|
| `topic_of` | chunk 归属哪个话题 | chunk→"mem-service 召回链路" |
| `type_of` | chunk 是什么类型(决策/教训/结论/偏好/事实) | chunk→decision |
| `summarizes` | chunk 是谁的总结构(父 chunk→子 chunk 层级) | 里程碑结论→过程 chunk |
| `supplements` | chunk 补充谁(横向关联, 语义级) | 新踩坑→旧教训 |
| `belongs_to` | chunk 属于哪个项目/领域(归属轴) | chunk→memory-service |

### 与现 schema 的映射(增量迁移, 不推翻表结构)
- `fact.subject_id` → 挂载 topic/belongs 目标(entity 层保留)
- `fact.predicate` ∈ 封闭 5 边
- `fact.value/object_key` → **结论句**(不再是裸实体名); 原文经 source_refs 溯源 chunk
- `EdgeOut.topic` 字段(已有, regex 通道一直没产)升格为必填——它就是新 value

### 召回链路变化
- 向量路: 结论句 embedding 语义命中(主力, 替代实体名 LIKE)
- BFS: chunk→topic→同 topic chunk(话题聚簇), chunk→supplements→关联 chunk
- gate 判据复活: query vs 结论句的相关性是人可判的(不再是裸实体名沾边)

## 三、实施切票(建议)

- **G1 抽取改造**: chunk 切分(语义完整, 非固定窗口) + LLM 产"结论句+5 边类型+挂载点";
  regex/gazetteer 通道降级为仅产 topic_of 归属(不再产裸关系边)
- **G2 存量全量重组**(用户裁决 #2): 对现有图做**全量** chunk 片段关系重组——
  裸关系 fact 按 (subject/topic) 聚簇, 经 laya 批量判型重织成 chunk 结论 + 5 边;
  **无法聚合重织成 node 的直接物理 DELETE**(硬删, 不留软删包袱; 仓内首破"无物理
  DELETE"惯例——一次性迁移窗口内单写者执行, 删前 mysqldump 式快照备份 db 文件)
- **G3 召回/gate 重校准**: 新分布上重跑 50 对标注(gate 判据改为 query vs 结论句), 阈值实测
- **G4 laya T1 语义重接**: gate 的 cand_texts 从裸三元组换结论句——预期 P(high) 区分度回升

## 四、裁决记录
1. ✅(#2) 存量 23384 条**全量重组**成 chunk 关系; 无法聚合的直接 **DELETE**(硬删+迁移前快照)
2. 待裁决: 5 边封闭集够不够(要不要加 `contradicts`/`supersedes` 承接矛盾语义?)
3. 待裁决: entity 层(别名/消歧/语义边)保留现状还是随 G2 一并瘦身?
