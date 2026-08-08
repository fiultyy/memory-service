# NEXT_SESSION — mem-service 启动 prompt

> 新 session 开头粘贴此 prompt(或复制下方代码块)进入状态。2026-08-08 版。

## 启动 prompt(复制此段)

```
继续 mem-service 项目(~/projects/memory-service)。

【上次 session 2026-08-08 交付】KG 核心能力五迭代齐(全在 p0-entities-edges-schema 分支, 未 merge main):
P0 schema 边(8fa5c9a)→ projection 原生格式(780d1dd/3f56163)→ projection 加固(0603427)→ entity-dedupe D7+D3(50402ef)→ bfs D5+D6(4f1c9f7)→ bi-temporal D4(60092a0)。
HEAD=60092a0。详见 memory/session-handoff-2026-08-08。

【进入状态】
1. 读 ~/.claude/projects/-home-yy-projects-memory-service/memory/MEMORY.md 索引 + session-handoff-2026-08-08
2. git -C ~/projects/memory-service log --oneline -8        # 五迭代 commits
3. git -C ~/projects/memory-service branch --show-current   # p0-entities-edges-schema
4. cat ~/projects/memory-service/docs/mem-service-iteration-log.md  # 迭代记录 + defer 总览(open defer 查这章)
5. cd ~/projects/memory-service && for t in test_*.py; do python3 "$t" >/dev/null 2>&1 || echo FAIL_$t; done  # 应全过
6. sqlite3 ~/projects/memory-service/data/memory.db "SELECT count(*) FROM fact WHERE status='active'"  # 应 0(开发库空)

【当前 defer(详 log "defer 总览" 章)】
- 研究机制: Graphiti 式 LLM 矛盾检测 / bi-temporal churn 监控
- minor: BFS auto-suggest hint / as_of+BFS 组合深测 / valid_from 从 source 推导 / embedding 模型升级无迁移 / aliases 无 GC / 并发无 UNIQUE 竞态 / ISO 时区 / 同事务时间戳竞态
- 全局 operational 10 项(daemon / 中文 embedding / on-ingest 预计算 / 跨 scope 联邦 / mtime / 新 provider / 冷层 / CC→KG 反向 re-ingest / SessionStart hook)
- 已清零: D3-D7(projection 那轮 defer 全做完了)

【注意】
- guard: orchestrator-state.json 存在时主 agent 写项目代码被拦 → delegate agent/workflow;只读(grep/sqlite SELECT/jq/python test)放行
- 测试假绿(头号雷区): 单测 resolver/recall 不覆盖 cli.ingest→put_fact 接线, 要集成测试断言 fact id 真值
- OMP 终端: 实时列终端确认(tab+title+cwd), 别靠排除法猜; OMP 按目标路径绝对编辑
- ultracode review 并发别太猛(6+ 高 effort 打爆 glm API); 大 fan-out 用低并发
- commit 具体文件非 -A; 测试 db.init(tmp) 禁污染 data/memory.db; memory 目录 gitignored

【继续选项】merge main(5 迭代压着)/ eval_recall baseline 重跑(量化召回提升)/ Graphiti LLM 矛盾检测 / 收 minor 尾巴
```
