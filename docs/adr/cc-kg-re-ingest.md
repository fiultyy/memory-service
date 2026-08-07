# ADR: cc-kg-re-ingest(CC→KG 反向 re-ingest)
Date: 2026-08-07
Status: Active

## ADR-17: CC→KG 反向 re-ingest(memory md → KG 增量)
Status: Accepted
Date: 2026-08-07

Context: KG→CC 单向(build_index,ADR-15/16)。CC→KG 仅 `init-memory`(全量种子,一次性)。用户编辑 `memory/*.md` 后 KG 不更新——反向增量缺。grill #4 警告:反向 re-ingest 必须过滤 `source:mem-service` 投影 md(否则吃回污染,同 bootstrap 既有 bug ADR-16f)。

Decision:
- (a) **触发(组合)**:`cli re-ingest <file>`(手动)+ PostToolUse hook(Write/Edit 后自动)。用户选组合(grill gate)。
- (b) **增量逻辑 = 复用 autodream**(单 md → synthetic transcript → `autodream.autodream(session_id="memory:<file>", fact_type="permanent", source_cwd=cwd)`,幂等 ADD/UPDATE/DELETE)。复用 bootstrap.init_memory 单文件逻辑,不新造。
- (c) **过滤 `source:mem-service`**:re-ingest 跳过 frontmatter `source:mem-service` 的 md(投影产物,复用 bootstrap ADR-16f 过滤)。
- (d) **DELETE 语义 (2026-08-07 续, 已实现)**:手动 `cli prune [--dry-run]`。扫 `source_cwd` 的 active fact, 按 `source_refs` 里 `memory:<file>#` 反查源 md; 源 md 全不在 `memory_dir` → `status='deleted'` (可逆)。投影 `mem-<32hex>.md` + `MEMORY.md` 不算源(产物; native `mem-service-*.md` 用 32-hex 形态区分, 非前缀 — 否则误删)。**不自动**: PostToolUse 不捕 `rm` (无 tool 触发); SessionStart 自动 prune 仍 defer (删除低频, 手动够用)。实现 `bootstrap.prune_deleted` + `cli prune`, 见 `test_prune.py`。
- (e) **PostToolUse hook 后台执行**:re-ingest 走 LLM(慢),hook 用 `nohup ... &` 后台 + `exit 0` 立即返回,不阻塞 tool。timeout 兜底。
- (f) **PostToolUse 并存**:settings.json PostToolUse 已注册 orchestrator-state-callback;mem re-ingest hook 并存(数组加项或 if-else 链),不替换。

Alternatives:
- SessionStart mtime 对比:被否(不实时,编辑后到下次开局才同步)。
- 仅手动:被否(不自动,靠用户记着)。
- 实时前台 PostToolUse:被否(re-ingest LLM 慢,阻塞 tool)。

Consequences:
- 反向闭环:用户编辑 memory md → KG 增量(实时后台)→ 下次 build-index 投影回 CC(形式1/2)→ 双向通。
- 过滤保证:投影 md(source:mem-service)不被 re-ingest 吃回(同 ADR-16f)。
- 性能:PostToolUse 后台 nohup,tool 不阻塞;LLM 失败静默(像 pre-compact-mem.sh)。
- 测试隔离:db.init(tmp);re-ingest 单文件 mock provider 验证 ADD/UPDATE + 过滤。

Constrains: [T_A1, T_B1, T_B2]
