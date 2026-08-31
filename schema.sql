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
