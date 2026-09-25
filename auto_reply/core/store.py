"""本地镜像库：把解密后的微信库规范化进一个明文 sqlite，供统计/画像/建议查询。"""

import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    talker      TEXT NOT NULL,          -- 会话对方 wxid（群聊为群 id）
    is_sender   INTEGER NOT NULL,       -- 1=我发的 0=对方发的
    msg_type    INTEGER NOT NULL,       -- 1 文本 3 图 34 语音 47 表情包 49 卡片/文件 10000 系统
    content     TEXT DEFAULT '',
    create_time INTEGER NOT NULL,       -- unix 秒
    src_db      TEXT NOT NULL,
    src_table   TEXT NOT NULL DEFAULT '',
    src_rowid   INTEGER NOT NULL,
    UNIQUE(src_db, src_table, src_rowid)
);
CREATE INDEX IF NOT EXISTS idx_msg_talker_time ON messages(talker, create_time);
CREATE INDEX IF NOT EXISTS idx_msg_time ON messages(create_time);

CREATE TABLE IF NOT EXISTS contacts (
    wxid     TEXT PRIMARY KEY,
    nickname TEXT DEFAULT '',
    remark   TEXT DEFAULT '',
    alias    TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS sync_state (
    name    TEXT PRIMARY KEY,
    mtime   INTEGER,
    size    INTEGER,
    max_rowid INTEGER DEFAULT 0,
    synced_at INTEGER
);

CREATE TABLE IF NOT EXISTS personas (
    wxid           TEXT PRIMARY KEY,
    card_json      TEXT NOT NULL,
    built_at       INTEGER,
    built_msg_count INTEGER
);

CREATE TABLE IF NOT EXISTS event_cursor (
    name TEXT PRIMARY KEY,
    last_id INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS incoming_events (
    message_id INTEGER PRIMARY KEY REFERENCES messages(id),
    talker TEXT NOT NULL,
    seen_at INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_event_pending
    ON incoming_events(status, next_attempt_at, seen_at);

CREATE TABLE IF NOT EXISTS runtime_settings (
    name  TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reply_decisions (
    message_id      INTEGER PRIMARY KEY REFERENCES messages(id),
    provider        TEXT NOT NULL,
    selected_index  INTEGER NOT NULL,
    candidates_json TEXT NOT NULL,
    analysis        TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL,
    error           TEXT NOT NULL DEFAULT '',
    updated_at      INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS reply_parts (
    message_id INTEGER NOT NULL REFERENCES messages(id),
    ordinal INTEGER NOT NULL,
    kind TEXT NOT NULL,
    content TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    updated_at INTEGER NOT NULL,
    PRIMARY KEY(message_id, ordinal)
);

CREATE TABLE IF NOT EXISTS voice_profiles (
    scope       TEXT PRIMARY KEY,
    profile_json TEXT NOT NULL,
    built_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS voice_examples (
    reply_id    INTEGER PRIMARY KEY REFERENCES messages(id),
    talker      TEXT NOT NULL,
    incoming_id INTEGER NOT NULL REFERENCES messages(id),
    incoming    TEXT NOT NULL,
    reply       TEXT NOT NULL,
    created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_voice_example_talker_time
    ON voice_examples(talker, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_voice_example_time
    ON voice_examples(created_at DESC);

CREATE TABLE IF NOT EXISTS voice_index_state (
    name  TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS portable_voice_profiles (
    scope TEXT PRIMARY KEY,
    profile_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS portable_voice_examples (
    talker TEXT NOT NULL,
    incoming TEXT NOT NULL,
    reply TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_portable_voice_talker
    ON portable_voice_examples(talker, created_at DESC);

CREATE TABLE IF NOT EXISTS stickers (
    md5 TEXT PRIMARY KEY,
    caption TEXT NOT NULL DEFAULT '',
    tags_json TEXT NOT NULL DEFAULT '[]',
    source_url TEXT NOT NULL DEFAULT '',
    asset_path TEXT NOT NULL DEFAULT '',
    favorite_order INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sticker_calibration (
    id INTEGER PRIMARY KEY CHECK(id=1),
    dll_sha256 TEXT NOT NULL,
    favorites_json TEXT NOT NULL,
    visible_count INTEGER NOT NULL,
    calibrated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sticker_categories (
    name TEXT PRIMARY KEY,
    guidance TEXT NOT NULL DEFAULT '',
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sticker_assignments (
    md5 TEXT NOT NULL REFERENCES stickers(md5),
    category TEXT NOT NULL REFERENCES sticker_categories(name),
    description TEXT NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY(md5, category)
);
CREATE INDEX IF NOT EXISTS idx_sticker_assignments_category
    ON sticker_assignments(category);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    con.commit()
    return con


def upsert_sync_state(con, name, mtime, size, max_rowid):
    con.execute(
        "INSERT INTO sync_state(name, mtime, size, max_rowid, synced_at) "
        "VALUES(?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET mtime=?, size=?, "
        "max_rowid=?, synced_at=?",
        (name, mtime, size, max_rowid, int(time.time()),
         mtime, size, max_rowid, int(time.time())))
    con.commit()


def get_sync_state(con, name) -> sqlite3.Row | None:
    return con.execute("SELECT * FROM sync_state WHERE name=?", (name,)).fetchone()


def last_message(con) -> sqlite3.Row | None:
    return con.execute(
        "SELECT * FROM messages ORDER BY create_time DESC, id DESC LIMIT 1").fetchone()
