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
    created_at  TEXT NOT NULL
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
