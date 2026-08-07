# Spec: recall-trail-projection
Status: Locked(P5 收敛后,go P2)

## 1-3. Problem / Solution(In-Scope) / Out-of-Scope

**Problem**: ADR-15 投影仅 `build_index`(全局 LIF top-K),"本次 session 实际召回过的 fact"无投影通路(形式1缺失);`bootstrap` 不过滤投影 md 导致闭环污染(ADR-15 既有 bug);recall `top_k=None` 低相关尾巴灌入 CC 热层(grill 发现)。

**Solution(In-Scope)**(grill 收敛后修订):
- `recall()` session_id 默认 `os.environ.get("CLAUDE_CODE_SESSION_ID","unknown")`(ADR-16a,**env 取代原 --session 强制/hook/文件**)
- `build_index` UNION(轨迹 seen_sessions ∪ LIF top-K)+ `update_memory_md` **清空重写 [mem] 段**(修 ADR-15 累积 bug)
- `recall.py:249 score>=0.3` 噪音过滤 + cli `--top-k`(默认 None 兼容)
- `bootstrap` 过滤 `source: mem-service` md(闭环卫生)
- hook 透传 session_id(env 自动)

**Out-of-Scope**:
- UserPromptSubmit hook 兜底(grill 否决:env 取代;原脚本 LLM/validate 风险)
- 形式1 限定场景投影(首次召回/LIF<阈值,优化项 defer)
- 跨 compact 累积(本次仅当下 session)

## 4. User Stories
- **US1**: agent 调 `recall "rust"`(env 自动带 session_id)→ `/compact` → MEMORY.md `[mem]` 段含"用户 uses rust"(本次召回激活,score>=0.3)
- **US2**: `init-memory` 重跑 → 投影 md(mem-*.md)不被 re-ingest,KG 无重复无 seen_sessions 污染
- **US3**: env 注入时 seen_sessions 累积当前 session;无 env fallback "unknown" 不崩;`--session` 显式覆盖优先

## 5. Implementation Decisions
引用 [ADR-16](../adr/recall-trail-projection.md)(Revision: env / 清空重写 / score 阈值)。决策只在 ADR。

## 6. Testing Decisions(seams)
- general_test(确定性,qa_available=false),每 node 必带 skeptic
- 测试隔离:`db.init(tmp)` 切临时连接;env 测试注入 `CLAUDE_CODE_SESSION_ID`
- 闭环验证(US2)查 `source_refs`/`seen_sessions` 无 `mem-*.md` 假 session(区分过滤 vs 幂等 UPDATE)

## 7. Acceptance(关联编排图节点)
- US1 → Node C(T_C2 UNION + T_C4 score 阈值;build_index --session 后 [mem] 含轨迹 fact,score<0.3 不投影)
- US2 → Node A(T_A1 bootstrap 过滤)
- US3 → Node B(T_B1 recall env fallback;Node C 单 task 简化)
- Node C 4 task:T_C1 清空重写 / T_C2 UNION+env / T_C3 hook透传 / T_C4 score阈值
- green_definition: all nodes out(pass) AND p3 regression pass AND adr_compliance pass AND full_branch_review pass

## 8. Open Issues
(空——2 轮 grill 收敛,3 个盲点均有定论)

## 9. Defer 预判
- 形式1 限定场景投影(首次召回/LIF<0.65,优化边际)
- 跨 compact 轨迹累积
- bootstrap 过滤更通用形态(跳过任何非 CC 原生 source)
