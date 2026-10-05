-- 应急演练离线终端记录汇聚服务 - SQLite 初始化脚本（幂等）

PRAGMA foreign_keys = ON;

-- 演练
CREATE TABLE IF NOT EXISTS drills (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- 演练终端（先登记，后上报）
CREATE TABLE IF NOT EXISTS terminals (
    id         TEXT PRIMARY KEY,
    drill_id   TEXT NOT NULL REFERENCES drills(id),
    code       TEXT NOT NULL,
    name       TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (drill_id, code)
);

-- 终端上报记录（每个终端本地序号从 1 递增，同一序号仅一条“当前有效”内容）
CREATE TABLE IF NOT EXISTS records (
    id          TEXT PRIMARY KEY,
    drill_id    TEXT NOT NULL REFERENCES drills(id),
    terminal_id TEXT NOT NULL REFERENCES terminals(id),
    seq         INTEGER NOT NULL CHECK (seq >= 1),
    occurred_at TEXT NOT NULL,
    record_type TEXT NOT NULL,
    content     TEXT NOT NULL,
    revision_no INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    UNIQUE (terminal_id, seq)
);

-- 修订留痕：revision_no = 0 为原始上报，每次更正追加一条
CREATE TABLE IF NOT EXISTS record_revisions (
    id          TEXT PRIMARY KEY,
    record_id   TEXT NOT NULL REFERENCES records(id),
    revision_no INTEGER NOT NULL,
    occurred_at TEXT NOT NULL,
    record_type TEXT NOT NULL,
    content     TEXT NOT NULL,
    reason      TEXT,
    created_at  TEXT NOT NULL,
    UNIQUE (record_id, revision_no)
);

-- 冲突证据：同终端同序号重复上报但内容不一致
CREATE TABLE IF NOT EXISTS conflicts (
    id          TEXT PRIMARY KEY,
    drill_id    TEXT NOT NULL REFERENCES drills(id),
    terminal_id TEXT NOT NULL REFERENCES terminals(id),
    record_id   TEXT NOT NULL REFERENCES records(id),
    seq         INTEGER NOT NULL,
    existing    TEXT NOT NULL,   -- 已存内容 JSON
    incoming    TEXT NOT NULL,   -- 冲突上报 JSON
    created_at  TEXT NOT NULL
);

-- 逐号确认丢失的序号（缺号闭合的另一种方式）
CREATE TABLE IF NOT EXISTS lost_sequences (
    id          TEXT PRIMARY KEY,
    drill_id    TEXT NOT NULL REFERENCES drills(id),
    terminal_id TEXT NOT NULL REFERENCES terminals(id),
    seq         INTEGER NOT NULL CHECK (seq >= 1),
    reason      TEXT,
    created_at  TEXT NOT NULL,
    UNIQUE (terminal_id, seq)
);

-- 封存版本（一经写入不可变；迟到上报/更正只影响“当前时间线”）
CREATE TABLE IF NOT EXISTS seals (
    id             TEXT PRIMARY KEY,
    drill_id       TEXT NOT NULL REFERENCES drills(id),
    version        INTEGER NOT NULL,
    snapshot       TEXT NOT NULL,  -- 封存时刻完整时间线 JSON
    terminal_count INTEGER NOT NULL,
    record_count   INTEGER NOT NULL,
    open_gap_count INTEGER NOT NULL,
    created_at     TEXT NOT NULL,
    UNIQUE (drill_id, version)
);

CREATE INDEX IF NOT EXISTS idx_terminals_drill      ON terminals(drill_id);
CREATE INDEX IF NOT EXISTS idx_records_drill        ON records(drill_id);
CREATE INDEX IF NOT EXISTS idx_records_terminal_seq ON records(terminal_id, seq);
CREATE INDEX IF NOT EXISTS idx_revisions_record     ON record_revisions(record_id);
CREATE INDEX IF NOT EXISTS idx_conflicts_drill      ON conflicts(drill_id);
CREATE INDEX IF NOT EXISTS idx_lost_drill           ON lost_sequences(drill_id);
CREATE INDEX IF NOT EXISTS idx_seals_drill          ON seals(drill_id);
