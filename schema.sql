-- mem-service KG schema (ADR-2 storage, ADR-3 Fact reification)
-- No MemoryItem table — Fact reification is self-contained (per ADR-2 Decision).
-- Fact.value is the content carrier; object_id is nullable (unary/literal facts).

PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS entity (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    properties  TEXT NOT NULL DEFAULT '{}',   -- JSON object
    aliases        TEXT NOT NULL DEFAULT '[]',   -- JSON array (ADR-D7: 同实体异写别名)
    name_embedding TEXT,                          -- JSON array float (ADR-D7: 名称向量, 离线='[]'/NULL)
    created_at  TEXT NOT NULL,
    UNIQUE(name, entity_type)                     -- ADR-2 ①: DB 强制去重(resolver 是应用层闸非 DB 强制, 并发 re-ingest 竞态建孤儿)
);
CREATE INDEX IF NOT EXISTS idx_entity_name ON entity(name);
CREATE INDEX IF NOT EXISTS idx_entity_type ON entity(entity_type);

CREATE TABLE IF NOT EXISTS fact (
    id            TEXT PRIMARY KEY,
    subject_id    TEXT NOT NULL,
    predicate     TEXT NOT NULL,
    object_id     TEXT,                          -- nullable: literal facts carry value only
    value         TEXT,                          -- content carrier (ADR-3)
    valid_from    TEXT,
    valid_to      TEXT,
    fact_type     TEXT NOT NULL DEFAULT 'stable', -- ephemeral|stable|permanent
    LIF           REAL NOT NULL DEFAULT 0.5,      -- trust scalar (NOT NeuralField — ADR-4); composite of LIF five dims (ADR-8v2)
    original_lif  REAL NOT NULL DEFAULT 0.5,      -- ADR-8v2: source-dim initial-value snapshot (was decay base under ADR-8; decay now folded into lif_recency)
    confidence    REAL NOT NULL DEFAULT 0.5,
    source_refs   TEXT NOT NULL DEFAULT '[]',     -- JSON array: raw sessionId/leafUuid
    extractor     TEXT NOT NULL DEFAULT 'regex',
    status        TEXT NOT NULL DEFAULT 'active', -- active|deprecated|superseded
    supersedes_id TEXT,
    -- ADR-8v2 LIF five-dim composite (freq/recency/spread/coherence/source) + recall-reinforcement state
    lif_freq        REAL NOT NULL DEFAULT 0,        -- 1-exp(-access_count/5) — recall saturation
    lif_recency     REAL NOT NULL DEFAULT 0.5,      -- exp(-ln2·age_h/half_life_h), age_h=now-last_accessed_at
    lif_spread      REAL NOT NULL DEFAULT 0,        -- min(1, distinct_sessions/5) — cross-session
    lif_coherence   REAL NOT NULL DEFAULT 0,        -- 1-conflicts/max(1,neighbors) — subject-neighbor agreement
    lif_source      REAL NOT NULL DEFAULT 0.4,      -- SOURCE_WEIGHT[extractor] (regex=0.4/llm=0.7/human=0.9/vote=0.85)
    access_count    INTEGER NOT NULL DEFAULT 0,     -- recall hit count
    last_accessed_at TEXT,                          -- recall refresh timestamp (drives lif_recency)
    seen_sessions   TEXT NOT NULL DEFAULT '[]',     -- JSON array: sessions that recalled this fact (drives lif_spread)
    source_cwd    TEXT,                             -- ADR-14: 来源 cwd(b 方案, 跨 cwd 隔离; NULL=老数据/未知, recall --cwd 过滤含 NULL)
    topic        TEXT,                              -- ADR-C: LLM 生成的一句话可读事实(投影 filename slug + index title + description)
    supersede_reason TEXT,                          -- M1: contradiction|dedup|upgrade|confirm (update_fact_status reason 参写入; NULL=legacy 不回填)
    provenance       TEXT,                          -- M2: user_prose|tool_obs|agent_assert|human|system (P21 出处轴; M8 块归因接线)
    veracity         REAL,                          -- M3: P21 f(provenance) 权重标量 (DR-5 b / DR-6 REAL; NULL=legacy 不回填)
    raw_predicate    TEXT,                          -- v1.7 回补: 消除双源不同步先例 (batch 13: LLM 原文谓词; predicate 存聚类后 canonical)
    task_outcome     TEXT,                          -- v1.7 回补: 消除双源不同步先例 (prompt v5: 任务收尾分诊; NULL=非任务/legacy)
    extract_sessions TEXT NOT NULL DEFAULT '[]',    -- v1.7③: JSON array — 主径 llm 通道 UPDATE stamp 的 session 串 (语义由后续车道实现)
    recall_sessions  TEXT NOT NULL DEFAULT '[]',    -- v1.7④: JSON array — 注入吸收观测 session 串 (语义由后续车道实现)
    gate_score       REAL NOT NULL DEFAULT 0.0,     -- v1.7⑤: 累计 gate 分 (求和封顶), 缺省 0.0
    harness       TEXT,                             -- B3 (B3C-HYG): 来源 harness (cc|dsh|pi|omp|codex; NULL=legacy/未知 — 老库迁移不回填)
    created_at    TEXT NOT NULL,
    FOREIGN KEY (subject_id) REFERENCES entity(id),
    FOREIGN KEY (object_id)  REFERENCES entity(id),
    FOREIGN KEY (supersedes_id) REFERENCES fact(id)
);
CREATE INDEX IF NOT EXISTS idx_fact_subject ON fact(subject_id);
CREATE INDEX IF NOT EXISTS idx_fact_object  ON fact(object_id);
CREATE INDEX IF NOT EXISTS idx_fact_pred    ON fact(predicate);
CREATE INDEX IF NOT EXISTS idx_fact_status  ON fact(status);
CREATE INDEX IF NOT EXISTS idx_fact_source_cwd ON fact(source_cwd);  -- ADR-14 b 方案

-- M4 (spec v2 §1): wings 异步升级队列 — 占位(regex)产出待 LLM 升级。
-- status 流转: pending → in_flight → done | failed(→pending 重试) ; attempts≥3 → dead 冻结待人工。
-- M9: surprise(复合惊喜) + priority(=|surprise|^α, D8 唯一采纳采样公式) 入队时算。
CREATE TABLE IF NOT EXISTS upgrade_queue (
    id              TEXT PRIMARY KEY,
    material_ref    TEXT NOT NULL UNIQUE,      -- 升级素材定位: fact:<id> / segment:<path>#seg<n>
    transcript_path TEXT,
    byte_offset     INTEGER,
    surprise        REAL,                      -- M9 复合惊喜; NULL=embedding 离线不可考
    priority        REAL NOT NULL DEFAULT 0,   -- |surprise|^α, 出队按此降序
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending','in_flight','done','failed','dead')),
    attempts        INTEGER NOT NULL DEFAULT 0,
    material_text   TEXT,                          -- M11: 入队时转写的升级素材原文(源不变式: 提取输入=队列 material, 非 KG 读)
    material_prov   TEXT,                          -- M11: 素材段 provenance (wings 升级产出继承)
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_uq_status_priority ON upgrade_queue(status, priority DESC);

-- T4/P4 语义边 (laya 判 4 档 score, norm /3.0 >= 0.7 入边; spec v1.1 §四 Step2)。
-- 单向写(source=new fact, target=neighbor), 读侧联查 fact 两端时态 — fact 无
-- 硬删路径(软删 supersede/deprecated), 联查天然隐藏失效端, 无需级联 DELETE。
CREATE TABLE IF NOT EXISTS fact_relations (
    source_id  TEXT NOT NULL,
    target_id  TEXT NOT NULL,
    edge_type  TEXT NOT NULL DEFAULT 'semantic',
    weight     REAL NOT NULL,
    created_by TEXT NOT NULL DEFAULT 'laya',
    created_at TEXT NOT NULL,
    PRIMARY KEY (source_id, target_id, edge_type)
);
CREATE INDEX IF NOT EXISTS idx_fact_relations_target ON fact_relations(target_id);

-- 图改造 v2 (docs/specs/graph-reform-v2-ingest-tags.md §五 H5): 知识原子图 +
-- 分层 tag 索引 + ingest 去重台账。fact 表自本批起为 legacy 归档 (不删不改)。
CREATE TABLE IF NOT EXISTS atom (
    id          INTEGER PRIMARY KEY,
    text        TEXT NOT NULL,                          -- 知识原子结论句 (canonical)
    label       TEXT NOT NULL
                CHECK(label IN ('fact','judgment','experience','summary',
                                'preference','event')),
    p_dur       REAL DEFAULT 0.0,                       -- laya 审计 durable 概率 (组内 max)
    valid_from  TEXT,                                   -- D4 双时态: 成员 fact 最早 created_at
    valid_to    TEXT,                                   -- supersede/证伪清算写 (夜间)
    source_refs TEXT,                                   -- JSON array: 成员原 fact 的 source_refs 并集
    source_cwd TEXT,                                    -- 成员原 fact 溯源 cwd (多数决)
    gist        TEXT,                                   -- 结论句 (v4 段级 atom; 句级 atom 为 NULL)
    subjects    TEXT,                                   -- JSON 数组: 精确符号/实体名 (路径/服务/版本/命令)
    event_at    TEXT,                                   -- 事件发生时刻 (与 valid_from 记录时刻分离)
    last_seen_at TEXT,                                  -- 复现续期 (TTL 用; NULL=取 valid_from)
    needs_embed INTEGER DEFAULT 0,                      -- H2: embed 失败夜间补扫标记
    needs_audit INTEGER DEFAULT 0,                      -- H2: laya 审计欠账标记
    created_at  TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_atom_valid ON atom(valid_from, valid_to);

CREATE TABLE IF NOT EXISTS atom_edge (
    a_id INTEGER NOT NULL REFERENCES atom(id),
    b_id INTEGER NOT NULL REFERENCES atom(id),
    w    REAL NOT NULL,                                 -- laya relatedness 分
    kind TEXT NOT NULL DEFAULT 'related'
         CHECK(kind IN ('related','contradicts','supersedes')),
    PRIMARY KEY(a_id, b_id),
    CHECK(a_id < b_id)                                  -- 无向边规范序 (有向型另起表)
);
CREATE INDEX IF NOT EXISTS idx_atom_edge_b ON atom_edge(b_id);

-- tag 层 (裁决#1 递归层级, 硬帽应用层管): kind=factual (session:/repo: 铸币,
-- 裁决#2) | semantic (开放命名); level 1 为事实层底座。
CREATE TABLE IF NOT EXISTS tag (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK(kind IN ('factual','semantic')),
    level       INTEGER NOT NULL DEFAULT 1,
    description TEXT,
    parent_id   INTEGER REFERENCES tag(id),             -- parent_of (层级涌现)
    created_at  TEXT DEFAULT (datetime('now')),
    UNIQUE(name, level)
);
CREATE INDEX IF NOT EXISTS idx_tag_parent ON tag(parent_id);

CREATE TABLE IF NOT EXISTS tag_mount (
    tag_id  INTEGER NOT NULL REFERENCES tag(id),
    atom_id INTEGER NOT NULL REFERENCES atom(id),
    w       REAL DEFAULT 0.0,                           -- indexes 挂载分 (事实铸币=1.0 确定挂载)
    PRIMARY KEY(tag_id, atom_id)
);
CREATE INDEX IF NOT EXISTS idx_tag_mount_atom ON tag_mount(atom_id);

-- H2: 段内容 sha 去重台账 (跨文件重放免疫); status=ok/poison (毒段 DLQ, m3 入权威 DDL)
CREATE TABLE IF NOT EXISTS distill_seen (
    sha        TEXT PRIMARY KEY,
    created_at TEXT DEFAULT (datetime('now')),
    status     TEXT NOT NULL DEFAULT 'ok'
);
