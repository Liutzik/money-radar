"""Local SQLite store. Bets are kept without player names: only money, game and time."""
import hashlib
import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    slug TEXT PRIMARY KEY,      -- stake casino slug, e.g. donutgaming-ganja-snail
    engine_slug TEXT,           -- verifier slug from the demo page, e.g. ganja-snail
    catalog_slug TEXT,          -- engine catalog slug, e.g. donut-gaming-ganja-snail
    engine_id INTEGER,          -- engine catalog id; higher = listed later
    not_engine INTEGER,         -- 1 for a looked-up slug from another provider (Hacksaw, Pragmatic ...)
    name TEXT,
    studio TEXT,
    released_at INTEGER,        -- unix seconds, Stake release time
    game_id INTEGER,
    modes TEXT,                 -- JSON [{name, cost}] or NULL when unknown
    info_at INTEGER,            -- when release info was last looked up
    modes_at INTEGER
);
CREATE TABLE IF NOT EXISTS bets (
    bet_key TEXT PRIMARY KEY,
    site TEXT,
    slug TEXT,
    amount_usd REAL,
    payout_usd REAL,
    currency TEXT,
    created_at INTEGER,
    hidden INTEGER,             -- 1 when the player had hidden mode on
    kind TEXT,                  -- spin | buy | unclear
    mode TEXT,
    cost REAL,
    tier TEXT,                  -- high | extreme
    age_days REAL,              -- game age when the bet landed
    image TEXT
);
CREATE INDEX IF NOT EXISTS bets_time ON bets(created_at);
CREATE INDEX IF NOT EXISTS bets_slug ON bets(slug, created_at);
CREATE TABLE IF NOT EXISTS alerts (key TEXT PRIMARY KEY, sent_at INTEGER);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    have = {r["name"] for r in db.execute("PRAGMA table_info(games)")}
    for col in ("engine_id", "not_engine"):
        if col not in have:
            db.execute(f"ALTER TABLE games ADD COLUMN {col} INTEGER")
    return db


def meta_get(db, k, default=None):
    row = db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return json.loads(row["v"]) if row else default


def meta_set(db, k, v):
    db.execute("INSERT OR REPLACE INTO meta(k, v) VALUES(?, ?)", (k, json.dumps(v)))
    db.commit()


def account_hash(site, username):
    """Affiliate accounts are remembered only as a one-way hash, never by name."""
    return hashlib.sha256(f"{site}:{(username or '').lower()}".encode()).hexdigest()[:20]


def game(db, slug):
    row = db.execute("SELECT * FROM games WHERE slug=?", (slug,)).fetchone()
    if not row:
        return None
    g = dict(row)
    g["modes"] = json.loads(g["modes"]) if g["modes"] else None
    return g


def upsert_game(db, slug, **fields):
    if "modes" in fields and fields["modes"] is not None:
        fields["modes"] = json.dumps(fields["modes"])
    db.execute("INSERT OR IGNORE INTO games(slug) VALUES(?)", (slug,))
    if fields:
        cols = ", ".join(f"{k}=?" for k in fields)
        db.execute(f"UPDATE games SET {cols} WHERE slug=?", (*fields.values(), slug))


def has_bet(db, bet_key):
    return db.execute("SELECT 1 FROM bets WHERE bet_key=?", (bet_key,)).fetchone() is not None


def add_bet(db, row):
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    cur = db.execute(f"INSERT OR IGNORE INTO bets({cols}) VALUES({marks})", tuple(row.values()))
    return cur.rowcount == 1


def alert_due(db, key, now, cooldown):
    row = db.execute("SELECT sent_at FROM alerts WHERE key=?", (key,)).fetchone()
    return not row or now - row["sent_at"] >= cooldown


def alert_sent(db, key, now):
    db.execute("INSERT OR REPLACE INTO alerts(key, sent_at) VALUES(?, ?)", (key, now))
    db.commit()
