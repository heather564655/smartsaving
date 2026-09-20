# maxbook.py
# MaxBook v5 - original social platform backend.
# Stack: FastAPI + SQLite + JWT + Argon2id.
# Full single-file server. Run: python maxbook.py
# Env: MAXBOOK_SECRET (required), MAXBOOK_CORS, MAXBOOK_DB,
#      MAXBOOK_HSTS, MAXBOOK_DEBUG_RESET.

import os
import re
import json
import uuid
import math
import time
import sqlite3
import secrets
import hashlib
import logging
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from typing import Optional, List, Any, Dict

from fastapi import FastAPI, HTTPException, Depends, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field, EmailStr
from pwdlib import PasswordHash
import jwt

# ----------------------------- CONFIG -----------------------------
DB_PATH = os.environ.get("MAXBOOK_DB", "maxbook.db")
JWT_SECRET = os.environ.get("MAXBOOK_SECRET")
if not JWT_SECRET or len(JWT_SECRET) < 32:
    raise RuntimeError("MAXBOOK_SECRET must be set to at least 32 chars")

JWT_ALG = "HS256"
ACCESS_TTL_MIN = 30
REFRESH_TTL_DAYS = 30
RANK_HALF_LIFE_HOURS = 18.0
CORS_ORIGINS = [o.strip() for o in os.environ.get("MAXBOOK_CORS", "").split(",") if o.strip()]

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("maxbook")

pw = PasswordHash.recommended()
app = FastAPI(title="MaxBook", version="5.0.0")
bearer = HTTPBearer(auto_error=False)

if CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS, allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
    )

@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["X-Frame-Options"] = "DENY"
    if os.environ.get("MAXBOOK_HSTS") == "1":
        resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return resp

# ----------------------------- RATE LIMIT -----------------------------
_buckets: Dict[str, list] = defaultdict(list)

def rate_limit(key: str, limit: int, window_sec: int):
    now = time.time()
    bucket = _buckets[key]
    cutoff = now - window_sec
    while bucket and bucket[0] < cutoff:
        bucket.pop(0)
    if len(bucket) >= limit:
        raise HTTPException(429, "too many requests")
    bucket.append(now)

# ----------------------------- CONSTANTS -----------------------------
POST_TYPES = {"thought", "moment", "question", "idea", "poll", "challenge", "event", "media"}
REACTION_KINDS = {"agree", "useful", "interesting", "funny", "support", "fire"}
SPACE_PRIVACY = {"public", "private", "secret"}
SPACE_ITEM_KINDS = {"discussion", "question", "poll", "announcement", "resource", "event"}
MOMENT_TTL_MAX_HOURS = 72
REPUTATION_KINDS = {"contributor", "helper", "creator", "builder"}
NOTIF_KINDS = {
    "reaction", "comment", "reply", "connection_request", "connection_accept",
    "message", "follow", "mention", "share", "space_invite", "space_join",
    "moment_reply", "system",
}

# ----------------------------- DB -----------------------------
def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    display_name TEXT NOT NULL,
    tagline TEXT DEFAULT '',
    bio TEXT DEFAULT '',
    avatar_url TEXT DEFAULT '',
    cover_url TEXT DEFAULT '',
    location TEXT DEFAULT '',
    birthday TEXT DEFAULT '',
    privacy TEXT DEFAULT 'public',
    role TEXT NOT NULL DEFAULT 'user',
    status TEXT NOT NULL DEFAULT 'active',
    verified INTEGER DEFAULT 0,
    reputation_score INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS user_interests (
    user_id TEXT NOT NULL,
    interest TEXT NOT NULL,
    weight INTEGER DEFAULT 1,
    created_at TEXT NOT NULL,
    PRIMARY KEY (user_id, interest),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS user_links (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    label TEXT NOT NULL,
    url TEXT NOT NULL,
    position INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    refresh_hash TEXT NOT NULL,
    device_name TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    rotated_from TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS password_resets (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    token_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS posts (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    post_type TEXT NOT NULL DEFAULT 'thought',
    body TEXT NOT NULL,
    media_url TEXT DEFAULT '',
    meta TEXT DEFAULT '{}',
    visibility TEXT DEFAULT 'public',
    space_id TEXT,
    page_id TEXT,
    parent_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS reactions (
    post_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (post_id, user_id),
    FOREIGN KEY (post_id) REFERENCES posts(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS comments (
    id TEXT PRIMARY KEY,
    post_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    parent_id TEXT,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT,
    FOREIGN KEY (post_id) REFERENCES posts(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS comment_reactions (
    comment_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (comment_id, user_id),
    FOREIGN KEY (comment_id) REFERENCES comments(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS connection_requests (
    id TEXT PRIMARY KEY,
    sender_id TEXT NOT NULL,
    receiver_id TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    responded_at TEXT,
    UNIQUE (sender_id, receiver_id),
    FOREIGN KEY (sender_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (receiver_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS friendships (
    user_id TEXT NOT NULL,
    friend_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (user_id, friend_id),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (friend_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS follows (
    follower_id TEXT NOT NULL,
    followee_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (follower_id, followee_id),
    FOREIGN KEY (follower_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (followee_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL DEFAULT 'direct',
    title TEXT DEFAULT '',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS conversation_members (
    conversation_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    role TEXT DEFAULT 'member',
    joined_at TEXT NOT NULL,
    last_read_at TEXT,
    PRIMARY KEY (conversation_id, user_id),
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT,
    thread_id TEXT,
    sender_id TEXT NOT NULL,
    recipient_id TEXT,
    body TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'text',
    media_url TEXT DEFAULT '',
    reply_to TEXT,
    created_at TEXT NOT NULL,
    read_at TEXT,
    pinned INTEGER DEFAULT 0,
    FOREIGN KEY (sender_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS notifications (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    read_at TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS moments (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'text',
    body TEXT DEFAULT '',
    media_url TEXT DEFAULT '',
    meta TEXT DEFAULT '{}',
    visibility TEXT DEFAULT 'public',
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS moment_views (
    moment_id TEXT NOT NULL,
    viewer_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (moment_id, viewer_id),
    FOREIGN KEY (moment_id) REFERENCES moments(id) ON DELETE CASCADE,
    FOREIGN KEY (viewer_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS moment_replies (
    id TEXT PRIMARY KEY,
    moment_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (moment_id) REFERENCES moments(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS spaces (
    id TEXT PRIMARY KEY,
    slug TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    topic TEXT DEFAULT '',
    description TEXT DEFAULT '',
    privacy TEXT DEFAULT 'public',
    owner_id TEXT NOT NULL,
    cover_url TEXT DEFAULT '',
    rules TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT,
    FOREIGN KEY (owner_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS space_members (
    space_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    role TEXT DEFAULT 'member',
    joined_at TEXT NOT NULL,
    PRIMARY KEY (space_id, user_id),
    FOREIGN KEY (space_id) REFERENCES spaces(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS space_items (
    id TEXT PRIMARY KEY,
    space_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    title TEXT DEFAULT '',
    body TEXT DEFAULT '',
    meta TEXT DEFAULT '{}',
    pinned INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT,
    FOREIGN KEY (space_id) REFERENCES spaces(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS pages (
    id TEXT PRIMARY KEY,
    slug TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    category TEXT DEFAULT '',
    description TEXT DEFAULT '',
    owner_id TEXT NOT NULL,
    avatar_url TEXT DEFAULT '',
    cover_url TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    FOREIGN KEY (owner_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS page_followers (
    page_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (page_id, user_id),
    FOREIGN KEY (page_id) REFERENCES pages(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    location TEXT DEFAULT '',
    starts_at TEXT NOT NULL,
    ends_at TEXT,
    host_id TEXT NOT NULL,
    cover_url TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    FOREIGN KEY (host_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS event_attendees (
    event_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (event_id, user_id),
    FOREIGN KEY (event_id) REFERENCES events(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS blocks (
    blocker_id TEXT NOT NULL,
    blocked_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (blocker_id, blocked_id),
    FOREIGN KEY (blocker_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (blocked_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS reports (
    id TEXT PRIMARY KEY,
    reporter_id TEXT NOT NULL,
    target_kind TEXT NOT NULL,
    target_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    description TEXT DEFAULT '',
    status TEXT DEFAULT 'open',
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    FOREIGN KEY (reporter_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS collections (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    visibility TEXT DEFAULT 'private',
    created_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS collection_items (
    collection_id TEXT NOT NULL,
    item_kind TEXT NOT NULL,
    item_id TEXT NOT NULL,
    added_at TEXT NOT NULL,
    PRIMARY KEY (collection_id, item_kind, item_id),
    FOREIGN KEY (collection_id) REFERENCES collections(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS reputation_events (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    points INTEGER NOT NULL,
    source_kind TEXT DEFAULT '',
    source_id TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS user_badges (
    user_id TEXT NOT NULL,
    badge TEXT NOT NULL,
    awarded_at TEXT NOT NULL,
    PRIMARY KEY (user_id, badge),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS content_stats (
    subject_kind TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    views INTEGER DEFAULT 0,
    reach INTEGER DEFAULT 0,
    reactions_count INTEGER DEFAULT 0,
    comments_count INTEGER DEFAULT 0,
    shares INTEGER DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (subject_kind, subject_id),
    FOREIGN KEY (owner_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS schema_migrations (
    id TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_posts_user ON posts(user_id, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_posts_space ON posts(space_id, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_posts_page ON posts(page_id, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_posts_type ON posts(post_type, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_msgs_convo ON messages(conversation_id, created_at ASC);
CREATE INDEX IF NOT EXISTS idx_notif_user ON notifications(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_moments_user ON moments(user_id, expires_at);
CREATE INDEX IF NOT EXISTS idx_spaces_privacy ON spaces(privacy, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_connreq_receiver ON connection_requests(receiver_id, status);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id, revoked_at);
CREATE INDEX IF NOT EXISTS idx_rep_user ON reputation_events(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_stats_owner ON content_stats(owner_id, updated_at DESC);
"""

MIGRATIONS = [
    ("0001_init", "initial schema"),
    ("0002_post_type", "ALTER TABLE posts ADD COLUMN post_type TEXT NOT NULL DEFAULT 'thought'"),
    ("0003_meta", "ALTER TABLE posts ADD COLUMN meta TEXT DEFAULT '{}'"),
    ("0004_tagline", "ALTER TABLE users ADD COLUMN tagline TEXT DEFAULT ''"),
    ("0005_reputation", "ALTER TABLE users ADD COLUMN reputation_score INTEGER DEFAULT 0"),
    ("0006_session_rotation", "ALTER TABLE sessions ADD COLUMN rotated_from TEXT"),
    ("0007_message_kind", "ALTER TABLE messages ADD COLUMN kind TEXT NOT NULL DEFAULT 'text'"),
    ("0008_message_media", "ALTER TABLE messages ADD COLUMN media_url TEXT DEFAULT ''"),
    ("0009_message_reply", "ALTER TABLE messages ADD COLUMN reply_to TEXT"),
    ("0010_message_pin", "ALTER TABLE messages ADD COLUMN pinned INTEGER DEFAULT 0"),
    ("0011_conversations", "CREATE TABLE IF NOT EXISTS conversations (id TEXT PRIMARY KEY, kind TEXT NOT NULL DEFAULT 'direct', title TEXT DEFAULT '', created_by TEXT NOT NULL, created_at TEXT NOT NULL)"),
    ("0012_convo_members", "CREATE TABLE IF NOT EXISTS conversation_members (conversation_id TEXT NOT NULL, user_id TEXT NOT NULL, role TEXT DEFAULT 'member', joined_at TEXT NOT NULL, last_read_at TEXT, PRIMARY KEY (conversation_id, user_id))"),
]

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def now_iso() -> str:
    return now_utc().isoformat()

def new_id() -> str:
    return uuid.uuid4().hex

def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()

def slugify(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s[:60] or new_id()[:8]

def _column_exists(c, table: str, column: str) -> bool:
    rows = c.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r["name"] == column for r in rows)

def _table_exists(c, table: str) -> bool:
    r = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    return r is not None

def run_migrations():
    with db() as c:
        c.executescript(SCHEMA)
        for mid, sql in MIGRATIONS:
            seen = c.execute("SELECT 1 FROM schema_migrations WHERE id=?", (mid,)).fetchone()
            if seen:
                continue
            try:
                if sql.startswith("ALTER TABLE"):
                    parts = sql.split()
                    table, column = parts[2], parts[4]
                    if _column_exists(c, table, column):
                        c.execute("INSERT OR IGNORE INTO schema_migrations (id,applied_at) VALUES (?,?)",
                                  (mid, now_iso()))
                        continue
                if sql.startswith("CREATE TABLE IF NOT EXISTS"):
                    name = sql.split("EXISTS", 1)[1].strip().split("(", 1)[0].strip()
                    if _table_exists(c, name):
                        c.execute("INSERT OR IGNORE INTO schema_migrations (id,applied_at) VALUES (?,?)",
                                  (mid, now_iso()))
                        continue
                c.execute(sql)
                c.execute("INSERT OR IGNORE INTO schema_migrations (id,applied_at) VALUES (?,?)",
                          (mid, now_iso()))
            except Exception as e:
                log.error("migration %s failed: %s", mid, e)

def init_db():
    run_migrations()

def make_access(user_id: str) -> str:
    return jwt.encode(
        {"sub": user_id, "typ": "access",
         "exp": now_utc() + timedelta(minutes=ACCESS_TTL_MIN), "iat": now_utc()},
        JWT_SECRET, algorithm=JWT_ALG)

def make_refresh(user_id: str) -> str:
    return jwt.encode(
        {"sub": user_id, "typ": "refresh", "jti": new_id(),
         "exp": now_utc() + timedelta(days=REFRESH_TTL_DAYS), "iat": now_utc()},
        JWT_SECRET, algorithm=JWT_ALG)

def decode_token(token: str, expected_typ: str = "access") -> dict:
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALG])
    except jwt.PyJWTError:
        raise HTTPException(401, "invalid token")
    if payload.get("typ") != expected_typ:
        raise HTTPException(401, "wrong token type")
    return payload

def row_to_dict(row) -> Optional[dict]:
    return {k: row[k] for k in row.keys()} if row else None

def json_or_empty(s: Optional[str]) -> dict:
    if not s:
        return {}
    try:
        return json.loads(s)
    except Exception:
        return {}

def notify(c, user_id: Optional[str], kind: str, payload: dict):
    if not user_id or kind not in NOTIF_KINDS:
        return
    c.execute(
        "INSERT INTO notifications (id,user_id,kind,payload,created_at) VALUES (?,?,?,?,?)",
        (new_id(), user_id, kind, json.dumps(payload), now_iso()))

def are_connected(c, a: str, b: str) -> bool:
    return c.execute(
        "SELECT 1 FROM friendships WHERE user_id=? AND friend_id=?",
        (a, b)).fetchone() is not None

def is_blocked(c, a: str, b: str) -> bool:
    return c.execute(
        """SELECT 1 FROM blocks
           WHERE (blocker_id=? AND blocked_id=?) OR (blocker_id=? AND blocked_id=?)""",
        (a, b, b, a)).fetchone() is not None

def decay(score: float, created_at: str, half_life_hours: float) -> float:
    try:
        t = datetime.fromisoformat(created_at)
    except Exception:
        return score
    age_h = max(0.0, (now_utc() - t).total_seconds() / 3600.0)
    return score * math.pow(0.5, age_h / half_life_hours)

def user_active(c, uid: str) -> bool:
    r = c.execute("SELECT status FROM users WHERE id=?", (uid,)).fetchone()
    return bool(r and r["status"] == "active")

def safe_user_view(row) -> dict:
    d = row_to_dict(row)
    if not d:
        return {}
    for k in ("email", "password_hash", "role"):
        d.pop(k, None)
    return d

def award(c, user_id: str, kind: str, points: int, source_kind: str = "", source_id: str = ""):
    if kind not in REPUTATION_KINDS:
        return
    c.execute(
        """INSERT INTO reputation_events (id,user_id,kind,points,source_kind,source_id,created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (new_id(), user_id, kind, points, source_kind, source_id, now_iso()))
    c.execute("UPDATE users SET reputation_score=COALESCE(reputation_score,0)+? WHERE id=?",
              (points, user_id))

def stat_bump(c, subject_kind: str, subject_id: str, owner_id: str,
              views: int = 0, reach: int = 0, reactions_count: int = 0,
              comments_count: int = 0, shares: int = 0):
    existing = c.execute(
        "SELECT 1 FROM content_stats WHERE subject_kind=? AND subject_id=?",
        (subject_kind, subject_id)).fetchone()
    if existing:
        c.execute(
            """UPDATE content_stats
               SET views=views+?, reach=reach+?, reactions_count=reactions_count+?,
                   comments_count=comments_count+?, shares=shares+?, updated_at=?
               WHERE subject_kind=? AND subject_id=?""",
            (views, reach, reactions_count, comments_count, shares, now_iso(),
             subject_kind, subject_id))
    else:
        c.execute(
            """INSERT INTO content_stats
               (subject_kind,subject_id,owner_id,views,reach,reactions_count,comments_count,shares,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (subject_kind, subject_id, owner_id, views, reach,
             reactions_count, comments_count, shares, now_iso()))

# ----------------------------- MODELS -----------------------------
USERNAME_RE = re.compile(r"^[a-zA-Z0-9_.]{3,30}$")

class RegisterIn(BaseModel):
    username: str = Field(..., min_length=3, max_length=30)
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=128)
    display_name: str = Field(..., min_length=1, max_length=64)

class LoginIn(BaseModel):
    username_or_email: str
    password: str
    device_name: Optional[str] = ""

class RefreshIn(BaseModel):
    refresh_token: str

class PasswordChangeIn(BaseModel):
    current_password: str
    new_password: str = Field(..., min_length=8, max_length=128)

class ForgotIn(BaseModel):
    email: EmailStr

class ResetIn(BaseModel):
    token: str
    new_password: str = Field(..., min_length=8, max_length=128)

class WorldUpdateIn(BaseModel):
    display_name: Optional[str] = None
    tagline: Optional[str] = None
    bio: Optional[str] = None
    avatar_url: Optional[str] = None
    cover_url: Optional[str] = None
    location: Optional[str] = None
    birthday: Optional[str] = None
    privacy: Optional[str] = None

class InterestIn(BaseModel):
    interest: str = Field(..., min_length=1, max_length=40)
    weight: int = Field(1, ge=1, le=5)

class LinkIn(BaseModel):
    label: str = Field(..., min_length=1, max_length=40)
    url: str = Field(..., min_length=1, max_length=500)
    position: int = 0

class PostIn(BaseModel):
    post_type: str = "thought"
    body: str = Field(..., min_length=1, max_length=10000)
    media_url: Optional[str] = ""
    meta: Optional[Dict[str, Any]] = None
    visibility: Optional[str] = "public"
    space_id: Optional[str] = None
    page_id: Optional[str] = None

class PostUpdateIn(BaseModel):
    body: Optional[str] = Field(None, min_length=1, max_length=10000)
    media_url: Optional[str] = None
    meta: Optional[Dict[str, Any]] = None
    visibility: Optional[str] = None
    post_type: Optional[str] = None

class ReactIn(BaseModel):
    kind: str

class CommentIn(BaseModel):
    body: str = Field(..., min_length=1, max_length=4000)
    parent_id: Optional[str] = None

class CommentUpdateIn(BaseModel):
    body: str = Field(..., min_length=1, max_length=4000)

class MessageIn(BaseModel):
    recipient_id: Optional[str] = None
    conversation_id: Optional[str] = None
    body: str = Field(..., min_length=1, max_length=4000)
    kind: str = "text"
    media_url: Optional[str] = ""
    reply_to: Optional[str] = None

class ConversationIn(BaseModel):
    title: Optional[str] = ""
    member_ids: List[str]

class MomentIn(BaseModel):
    kind: str = "text"
    body: Optional[str] = ""
    media_url: Optional[str] = ""
    meta: Optional[Dict[str, Any]] = None
    visibility: Optional[str] = "public"
    ttl_hours: int = Field(24, ge=1, le=MOMENT_TTL_MAX_HOURS)

class MomentReplyIn(BaseModel):
    body: str = Field(..., min_length=1, max_length=1000)

class SpaceIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    topic: Optional[str] = ""
    description: Optional[str] = ""
    privacy: Optional[str] = "public"
    cover_url: Optional[str] = ""
    rules: Optional[str] = ""

class SpaceUpdateIn(BaseModel):
    name: Optional[str] = None
    topic: Optional[str] = None
    description: Optional[str] = None
    privacy: Optional[str] = None
    cover_url: Optional[str] = None
    rules: Optional[str] = None

class SpaceItemIn(BaseModel):
    kind: str
    title: Optional[str] = ""
    body: Optional[str] = ""
    meta: Optional[Dict[str, Any]] = None
    pinned: Optional[bool] = False

class PageIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    category: Optional[str] = ""
    description: Optional[str] = ""
    avatar_url: Optional[str] = ""
    cover_url: Optional[str] = ""

class EventIn(BaseModel):
    title: str = Field(..., min_length=1, max_length=120)
    description: Optional[str] = ""
    location: Optional[str] = ""
    starts_at: str
    ends_at: Optional[str] = None
    cover_url: Optional[str] = ""

class ReportIn(BaseModel):
    target_kind: str = Field(..., pattern="^(post|comment|user|message|space|page|moment)$")
    target_id: str
    reason: str = Field(..., min_length=1, max_length=500)
    description: Optional[str] = ""

class CollectionIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    description: Optional[str] = ""
    visibility: Optional[str] = "private"

class CollectionItemIn(BaseModel):
    item_kind: str = Field(..., pattern="^(post|moment|space|page|event)$")
    item_id: str

# ----------------------------- AUTH DEPS -----------------------------
def current_user(creds: Optional[HTTPAuthorizationCredentials] = Depends(bearer)) -> str:
    if creds is None:
        raise HTTPException(401, "missing token", headers={"WWW-Authenticate": "Bearer"})
    payload = decode_token(creds.credentials, "access")
    uid = payload["sub"]
    with db() as c:
        row = c.execute("SELECT status FROM users WHERE id=?", (uid,)).fetchone()
        if not row:
            raise HTTPException(401, "user no longer exists",
                                headers={"WWW-Authenticate": "Bearer"})
        if row["status"] != "active":
            raise HTTPException(403, "account not active")
    return uid

def require_admin(uid: str = Depends(current_user)) -> str:
    with db() as c:
        r = c.execute("SELECT role FROM users WHERE id=?", (uid,)).fetchone()
        if not r or r["role"] not in ("admin", "moderator"):
            raise HTTPException(403, "insufficient role")
    return uid

init_db()

# ----------------------------- AUTH -----------------------------
@app.post("/auth/register")
def register(data: RegisterIn, request: Request):
    rate_limit(f"reg:{request.client.host}", 5, 3600)
    uname = data.username.lower()
    if not USERNAME_RE.match(uname):
        raise HTTPException(400, "username 3-30 chars: letters digits _ .")
    with db() as c:
        if c.execute("SELECT 1 FROM users WHERE username=? OR email=?",
                     (uname, data.email.lower())).fetchone():
            raise HTTPException(409, "username or email already taken")
        uid = new_id()
        c.execute("""INSERT INTO users
                     (id,username,email,password_hash,display_name,created_at)
                     VALUES (?,?,?,?,?,?)""",
                  (uid, uname, data.email.lower(), pw.hash(data.password),
                   data.display_name, now_iso()))
    return _issue_tokens(uid, "")

@app.post("/auth/login")
def login(data: LoginIn, request: Request):
    rate_limit(f"login:{request.client.host}", 10, 300)
    key = data.username_or_email.lower()
    with db() as c:
        row = c.execute("SELECT * FROM users WHERE username=? OR email=?",
                        (key, key)).fetchone()
        if not row or not pw.verify(data.password, row["password_hash"]):
            raise HTTPException(401, "invalid credentials")
        if row["status"] != "active":
            raise HTTPException(403, "account not active")
        uid = row["id"]
    return _issue_tokens(uid, data.device_name or "")

def _issue_tokens(uid: str, device_name: str) -> dict:
    access = make_access(uid)
    refresh = make_refresh(uid)
    sid = new_id()
    with db() as c:
        c.execute("""INSERT INTO sessions
                     (id,user_id,refresh_hash,device_name,created_at,expires_at)
                     VALUES (?,?,?,?,?,?)""",
                  (sid, uid, sha256_hex(refresh), device_name, now_iso(),
                   (now_utc() + timedelta(days=REFRESH_TTL_DAYS)).isoformat()))
    return {"user_id": uid, "access_token": access, "refresh_token": refresh,
            "token_type": "bearer", "expires_in": ACCESS_TTL_MIN * 60,
            "session_id": sid}

@app.post("/auth/refresh")
def refresh_token(data: RefreshIn):
    payload = decode_token(data.refresh_token, "refresh")
    uid = payload["sub"]
    h = sha256_hex(data.refresh_token)
    with db() as c:
        s = c.execute("SELECT * FROM sessions WHERE user_id=? AND refresh_hash=?",
                      (uid, h)).fetchone()
        if not s or s["revoked_at"]:
            raise HTTPException(401, "session revoked")
        if datetime.fromisoformat(s["expires_at"]) < now_utc():
            raise HTTPException(401, "session expired")
        if not user_active(c, uid):
            raise HTTPException(403, "account not active")
        c.execute("UPDATE sessions SET revoked_at=? WHERE id=?", (now_iso(), s["id"]))
        new_refresh = make_refresh(uid)
        new_sid = new_id()
        c.execute("""INSERT INTO sessions
                     (id,user_id,refresh_hash,device_name,created_at,expires_at,rotated_from)
                     VALUES (?,?,?,?,?,?,?)""",
                  (new_sid, uid, sha256_hex(new_refresh), s["device_name"],
                   now_iso(), (now_utc() + timedelta(days=REFRESH_TTL_DAYS)).isoformat(),
                   s["id"]))
    return {"access_token": make_access(uid), "refresh_token": new_refresh,
            "token_type": "bearer", "expires_in": ACCESS_TTL_MIN * 60,
            "session_id": new_sid}

@app.post("/auth/logout")
def logout(data: RefreshIn, uid: str = Depends(current_user)):
    h = sha256_hex(data.refresh_token)
    with db() as c:
        c.execute("UPDATE sessions SET revoked_at=? WHERE user_id=? AND refresh_hash=?",
                  (now_iso(), uid, h))
    return {"ok": True}

@app.post("/auth/logout-all")
def logout_all(uid: str = Depends(current_user)):
    with db() as c:
        c.execute("UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
                  (now_iso(), uid))
    return {"ok": True}

@app.get("/auth/sessions")
def list_sessions(uid: str = Depends(current_user)):
    with db() as c:
        rows = c.execute("""SELECT id,device_name,created_at,expires_at,revoked_at,
                                   rotated_from FROM sessions
                            WHERE user_id=? ORDER BY created_at DESC""", (uid,)).fetchall()
        return [row_to_dict(r) for r in rows]

@app.delete("/auth/sessions/{sid}")
def revoke_session(sid: str, uid: str = Depends(current_user)):
    with db() as c:
        c.execute("UPDATE sessions SET revoked_at=? WHERE id=? AND user_id=?",
                  (now_iso(), sid, uid))
    return {"ok": True}

@app.post("/auth/change-password")
def change_password(data: PasswordChangeIn, uid: str = Depends(current_user)):
    with db() as c:
        row = c.execute("SELECT password_hash FROM users WHERE id=?", (uid,)).fetchone()
        if not row or not pw.verify(data.current_password, row["password_hash"]):
            raise HTTPException(401, "current password incorrect")
        c.execute("UPDATE users SET password_hash=?, updated_at=? WHERE id=?",
                  (pw.hash(data.new_password), now_iso(), uid))
        c.execute("UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
                  (now_iso(), uid))
    return {"ok": True}

@app.post("/auth/forgot-password")
def forgot_password(data: ForgotIn, request: Request):
    rate_limit(f"forgot:{request.client.host}", 5, 3600)
    with db() as c:
        row = c.execute("SELECT id FROM users WHERE email=?",
                        (data.email.lower(),)).fetchone()
        if row:
            raw = secrets.token_urlsafe(32)
            c.execute("""INSERT INTO password_resets (id,user_id,token_hash,created_at,expires_at)
                         VALUES (?,?,?,?,?)""",
                      (new_id(), row["id"], sha256_hex(raw), now_iso(),
                       (now_utc() + timedelta(hours=1)).isoformat()))
            if os.environ.get("MAXBOOK_DEBUG_RESET") == "1":
                log.info("dev reset token for %s: %s", data.email, raw)
    return {"ok": True}

@app.post("/auth/reset-password")
def reset_password(data: ResetIn):
    h = sha256_hex(data.token)
    with db() as c:
        r = c.execute("""SELECT * FROM password_resets
                         WHERE token_hash=? AND used_at IS NULL""", (h,)).fetchone()
        if not r:
            raise HTTPException(400, "invalid token")
        if datetime.fromisoformat(r["expires_at"]) < now_utc():
            raise HTTPException(400, "token expired")
        c.execute("UPDATE users SET password_hash=?, updated_at=? WHERE id=?",
                  (pw.hash(data.new_password), now_iso(), r["user_id"]))
        c.execute("UPDATE password_resets SET used_at=? WHERE id=?", (now_iso(), r["id"]))
        c.execute("UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
                  (now_iso(), r["user_id"]))
    return {"ok": True}

@app.get("/auth/me")
def whoami(uid: str = Depends(current_user)):
    return {"user_id": uid}

@app.delete("/users/me")
def delete_me(uid: str = Depends(current_user)):
    with db() as c:
        c.execute("UPDATE users SET status='deleted', updated_at=? WHERE id=?",
                  (now_iso(), uid))
        c.execute("UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
                  (now_iso(), uid))
    return {"ok": True}

# ----------------------------- MY WORLD -----------------------------
@app.get("/my-world")
def my_world(uid: str = Depends(current_user)):
    with db() as c:
        user = c.execute("""SELECT id,username,display_name,tagline,bio,avatar_url,cover_url,
                                   location,verified,reputation_score,created_at
                            FROM users WHERE id=?""", (uid,)).fetchone()
        out = safe_user_view(user)
        out["interests"] = [row_to_dict(r) for r in c.execute(
            "SELECT interest,weight FROM user_interests WHERE user_id=? ORDER BY weight DESC",
            (uid,))]
        out["links"] = [row_to_dict(r) for r in c.execute(
            "SELECT id,label,url,position FROM user_links WHERE user_id=? ORDER BY position",
            (uid,))]
        out["people_count"] = c.execute(
            "SELECT COUNT(*) FROM friendships WHERE user_id=?", (uid,)).fetchone()[0]
        out["spaces"] = [row_to_dict(r) for r in c.execute(
            """SELECT s.id,s.slug,s.name,s.topic,s.privacy FROM space_members m
               JOIN spaces s ON s.id=m.space_id WHERE m.user_id=?""", (uid,))]
        out["moments_count"] = c.execute(
            "SELECT COUNT(*) FROM moments WHERE user_id=? AND expires_at > ?",
            (uid, now_iso())).fetchone()[0]
        out["posts_count"] = c.execute(
            "SELECT COUNT(*) FROM posts WHERE user_id=?", (uid,)).fetchone()[0]
        out["badges"] = [r["badge"] for r in c.execute(
            "SELECT badge FROM user_badges WHERE user_id=?", (uid,))]
        out["featured"] = [row_to_dict(r) for r in c.execute(
            """SELECT id,post_type,body,created_at FROM posts
               WHERE user_id=? AND post_type IN ('idea','challenge','question')
               ORDER BY created_at DESC LIMIT 5""", (uid,))]
        out["collections"] = [row_to_dict(r) for r in c.execute(
            "SELECT id,name,description,visibility,created_at FROM collections WHERE user_id=?",
            (uid,))]
        return out

@app.patch("/my-world")
def update_world(data: WorldUpdateIn, uid: str = Depends(current_user)):
    fields, values = [], []
    for k, v in data.model_dump(exclude_none=True).items():
        if k == "privacy" and v not in ("public", "connections", "private"):
            raise HTTPException(400, "privacy must be public/connections/private")
        fields.append(f"{k}=?"); values.append(v)
    if not fields:
        raise HTTPException(400, "nothing to update")
    fields.append("updated_at=?"); values.append(now_iso())
    values.append(uid)
    with db() as c:
        c.execute(f"UPDATE users SET {','.join(fields)} WHERE id=?", values)
    return {"ok": True}

@app.post("/my-world/interests")
def add_interest(data: InterestIn, uid: str = Depends(current_user)):
    with db() as c:
        c.execute("""INSERT INTO user_interests (user_id,interest,weight,created_at)
                     VALUES (?,?,?,?)
                     ON CONFLICT(user_id,interest) DO UPDATE SET weight=excluded.weight""",
                  (uid, data.interest.lower(), data.weight, now_iso()))
    return {"ok": True}

@app.delete("/my-world/interests/{interest}")
def remove_interest(interest: str, uid: str = Depends(current_user)):
    with db() as c:
        c.execute("DELETE FROM user_interests WHERE user_id=? AND interest=?",
                  (uid, interest.lower()))
    return {"ok": True}

@app.post("/my-world/links")
def add_link(data: LinkIn, uid: str = Depends(current_user)):
    lid = new_id()
    with db() as c:
        c.execute("""INSERT INTO user_links (id,user_id,label,url,position,created_at)
                     VALUES (?,?,?,?,?,?)""",
                  (lid, uid, data.label, data.url, data.position, now_iso()))
    return {"link_id": lid}

@app.delete("/my-world/links/{link_id}")
def remove_link(link_id: str, uid: str = Depends(current_user)):
    with db() as c:
        c.execute("DELETE FROM user_links WHERE id=? AND user_id=?", (link_id, uid))
    return {"ok": True}

@app.get("/world/user/{user_id}")
def view_world(user_id: str, uid: str = Depends(current_user)):
    with db() as c:
        if is_blocked(c, uid, user_id):
            raise HTTPException(403, "blocked")
        row = c.execute("""SELECT id,username,display_name,tagline,bio,avatar_url,cover_url,
                                  location,verified,reputation_score,created_at,privacy,status
                           FROM users WHERE id=?""", (user_id,)).fetchone()
        if not row or row["status"] == "deleted":
            raise HTTPException(404, "user not found")
        out = safe_user_view(row)
        out["interests"] = [r["interest"] for r in c.execute(
            "SELECT interest FROM user_interests WHERE user_id=? ORDER BY weight DESC",
            (user_id,))]
        out["people_count"] = c.execute(
            "SELECT COUNT(*) FROM friendships WHERE user_id=?", (user_id,)).fetchone()[0]
        out["spaces"] = [row_to_dict(r) for r in c.execute(
            """SELECT s.id,s.slug,s.name,s.topic FROM space_members m
               JOIN spaces s ON s.id=m.space_id
               WHERE m.user_id=? AND s.privacy='public'""", (user_id,))]
        out["is_connected"] = are_connected(c, uid, user_id)
        if out["privacy"] == "private" and uid != user_id:
            out.pop("bio", None)
        return out

# ----------------------------- POSTS -----------------------------
def _visible(c, viewer: str, post_row) -> bool:
    if post_row["user_id"] == viewer:
        return True
    owner_active = user_active(c, post_row["user_id"])
    if not owner_active:
        return False
    if post_row["visibility"] == "private":
        return False
    if post_row["visibility"] == "connections":
        return are_connected(c, post_row["user_id"], viewer)
    return True

def _post_view(c, viewer: str, pid: str) -> dict:
    r = c.execute("""SELECT p.*, u.username, u.display_name, u.avatar_url, u.verified
                     FROM posts p JOIN users u ON u.id=p.user_id WHERE p.id=?""",
                  (pid,)).fetchone()
    if not r:
        raise HTTPException(404, "post not found")
    if is_blocked(c, viewer, r["user_id"]):
        raise HTTPException(403, "blocked")
    if not _visible(c, viewer, r):
        raise HTTPException(403, "not visible")
    d = row_to_dict(r)
    d["meta"] = json_or_empty(d.get("meta"))
    d["reactions"] = dict(c.execute(
        "SELECT kind, COUNT(*) FROM reactions WHERE post_id=? GROUP BY kind", (pid,)).fetchall())
    d["reaction_count"] = sum(d["reactions"].values())
    d["comment_count"] = c.execute(
        "SELECT COUNT(*) FROM comments WHERE post_id=?", (pid,)).fetchone()[0]
    mine = c.execute("SELECT kind FROM reactions WHERE post_id=? AND user_id=?",
                     (pid, viewer)).fetchone()
    d["reacted_by_me"] = mine["kind"] if mine else None
    return d

@app.post("/posts")
def create_post(data: PostIn, uid: str = Depends(current_user)):
    if data.post_type not in POST_TYPES:
        raise HTTPException(400, "invalid post_type")
    if data.visibility not in ("public", "connections", "private"):
        raise HTTPException(400, "invalid visibility")
    if data.space_id:
        with db() as c:
            m = c.execute("""SELECT sm.role, s.privacy FROM space_members sm
                             JOIN spaces s ON s.id=sm.space_id
                             WHERE sm.space_id=? AND sm.user_id=?""",
                          (data.space_id, uid)).fetchone()
            if not m:
                raise HTTPException(403, "not a space member")
    pid = new_id()
    with db() as c:
        if data.page_id:
            p = c.execute("SELECT owner_id FROM pages WHERE id=?", (data.page_id,)).fetchone()
            if not p or p["owner_id"] != uid:
                raise HTTPException(403, "not your page")
        c.execute("""INSERT INTO posts
                     (id,user_id,post_type,body,media_url,meta,visibility,space_id,page_id,created_at)
                     VALUES (?,?,?,?,?,?,?,?,?,?)""",
                  (pid, uid, data.post_type, data.body, data.media_url or "",
                   json.dumps(data.meta or {}), data.visibility, data.space_id,
                   data.page_id, now_iso()))
        stat_bump(c, "post", pid, uid)
        award(c, uid, "contributor", 1, "post", pid)
    return {"post_id": pid}

@app.get("/posts/{post_id}")
def get_post(post_id: str, uid: str = Depends(current_user)):
    with db() as c:
        return _post_view(c, uid, post_id)

@app.patch("/posts/{post_id}")
def edit_post(post_id: str, data: PostUpdateIn, uid: str = Depends(current_user)):
    with db() as c:
        r = c.execute("SELECT user_id FROM posts WHERE id=?", (post_id,)).fetchone()
        if not r: raise HTTPException(404, "post not found")
        if r["user_id"] != uid: raise HTTPException(403, "not your post")
        fields, values = [], []
        for k, v in data.model_dump(exclude_none=True).items():
            if k == "visibility" and v not in ("public", "connections", "private"):
                raise HTTPException(400, "invalid visibility")
            if k == "post_type" and v not in POST_TYPES:
                raise HTTPException(400, "invalid post_type")
            if k == "meta":
                v = json.dumps(v)
            fields.append(f"{k}=?"); values.append(v)
        if not fields:
            raise HTTPException(400, "nothing to update")
        fields.append("updated_at=?"); values.append(now_iso())
        values.append(post_id)
        c.execute(f"UPDATE posts SET {','.join(fields)} WHERE id=?", values)
    return {"ok": True}

@app.delete("/posts/{post_id}")
def delete_post(post_id: str, uid: str = Depends(current_user)):
    with db() as c:
        r = c.execute("SELECT user_id, space_id FROM posts WHERE id=?", (post_id,)).fetchone()
        if not r: raise HTTPException(404, "post not found")
        allowed = r["user_id"] == uid
        if not allowed and r["space_id"]:
            m = c.execute("""SELECT role FROM space_members
                             WHERE space_id=? AND user_id=?""",
                          (r["space_id"], uid)).fetchone()
            if m and m["role"] in ("owner", "moderator"):
                allowed = True
        if not allowed:
            raise HTTPException(403, "not allowed")
        c.execute("DELETE FROM posts WHERE id=?", (post_id,))
    return {"ok": True}

@app.post("/posts/{post_id}/react")
def react_post(post_id: str, data: ReactIn, uid: str = Depends(current_user)):
    if data.kind not in REACTION_KINDS:
        raise HTTPException(400, "invalid reaction")
    with db() as c:
        r = c.execute("SELECT user_id FROM posts WHERE id=?", (post_id,)).fetchone()
        if not r: raise HTTPException(404, "post not found")
        if is_blocked(c, uid, r["user_id"]): raise HTTPException(403, "blocked")
        c.execute("""INSERT INTO reactions (post_id,user_id,kind,created_at)
                     VALUES (?,?,?,?)
                     ON CONFLICT(post_id,user_id) DO UPDATE SET kind=excluded.kind""",
                  (post_id, uid, data.kind, now_iso()))
        if r["user_id"] != uid:
            notify(c, r["user_id"], "reaction",
                   {"post_id": post_id, "by": uid, "kind": data.kind})
        stat_bump(c, "post", post_id, r["user_id"], reactions_count=1)
    return {"ok": True}

@app.delete("/posts/{post_id}/react")
def unreact_post(post_id: str, uid: str = Depends(current_user)):
    with db() as c:
        c.execute("DELETE FROM reactions WHERE post_id=? AND user_id=?", (post_id, uid))
    return {"ok": True}

@app.get("/posts/{post_id}/reactions")
def list_reactions(post_id: str, kind: Optional[str] = None):
    with db() as c:
        if kind:
            if kind not in REACTION_KINDS: raise HTTPException(400, "invalid reaction")
            rows = c.execute("""SELECT u.id,u.username,u.display_name,u.avatar_url
                                FROM reactions r JOIN users u ON u.id=r.user_id
                                WHERE r.post_id=? AND r.kind=?""", (post_id, kind)).fetchall()
        else:
            rows = c.execute("""SELECT u.id,u.username,u.display_name,u.avatar_url,r.kind
                                FROM reactions r JOIN users u ON u.id=r.user_id
                                WHERE r.post_id=?""", (post_id,)).fetchall()
        return [row_to_dict(r) for r in rows]

# ----------------------------- COMMENTS -----------------------------
@app.post("/posts/{post_id}/comments")
def add_comment(post_id: str, data: CommentIn, uid: str = Depends(current_user)):
    cid = new_id()
    with db() as c:
        r = c.execute("SELECT user_id FROM posts WHERE id=?", (post_id,)).fetchone()
        if not r: raise HTTPException(404, "post not found")
        if is_blocked(c, uid, r["user_id"]): raise HTTPException(403, "blocked")
        if data.parent_id:
            p = c.execute("SELECT post_id FROM comments WHERE id=?",
                          (data.parent_id,)).fetchone()
            if not p or p["post_id"] != post_id:
                raise HTTPException(400, "parent comment invalid")
        c.execute("""INSERT INTO comments (id,post_id,user_id,parent_id,body,created_at)
                     VALUES (?,?,?,?,?,?)""",
                  (cid, post_id, uid, data.parent_id, data.body, now_iso()))
        if r["user_id"] != uid:
            notify(c, r["user_id"], "comment",
                   {"post_id": post_id, "by": uid, "comment_id": cid})
        if data.parent_id:
            pr = c.execute("SELECT user_id FROM comments WHERE id=?",
                           (data.parent_id,)).fetchone()
            if pr and pr["user_id"] not in (uid, r["user_id"]):
                notify(c, pr["user_id"], "reply",
                       {"post_id": post_id, "comment_id": cid, "by": uid})
        stat_bump(c, "post", post_id, r["user_id"], comments_count=1)
        award(c, uid, "helper", 1, "comment", cid)
    return {"comment_id": cid}

@app.get("/posts/{post_id}/comments")
def list_comments(post_id: str, limit: int = 100):
    limit = max(1, min(limit, 500))
    with db() as c:
        rows = c.execute("""SELECT c.*, u.username, u.display_name, u.avatar_url, u.verified
                            FROM comments c JOIN users u ON u.id=c.user_id
                            WHERE c.post_id=? AND u.status='active'
                            ORDER BY c.created_at ASC LIMIT ?""",
                         (post_id, limit)).fetchall()
        out = []
        for r in rows:
            d = row_to_dict(r)
            d["reactions"] = dict(c.execute(
                "SELECT kind, COUNT(*) FROM comment_reactions WHERE comment_id=? GROUP BY kind",
                (r["id"],)).fetchall())
            out.append(d)
        return out

@app.patch("/comments/{comment_id}")
def edit_comment(comment_id: str, data: CommentUpdateIn, uid: str = Depends(current_user)):
    with db() as c:
        r = c.execute("SELECT user_id FROM comments WHERE id=?", (comment_id,)).fetchone()
        if not r: raise HTTPException(404, "comment not found")
        if r["user_id"] != uid: raise HTTPException(403, "not your comment")
        c.execute("UPDATE comments SET body=?, updated_at=? WHERE id=?",
                  (data.body, now_iso(), comment_id))
    return {"ok": True}

@app.post("/comments/{comment_id}/react")
def react_comment(comment_id: str, data: ReactIn, uid: str = Depends(current_user)):
    if data.kind not in REACTION_KINDS:
        raise HTTPException(400, "invalid reaction")
    with db() as c:
        if not c.execute("SELECT 1 FROM comments WHERE id=?", (comment_id,)).fetchone():
            raise HTTPException(404, "comment not found")
        c.execute("""INSERT INTO comment_reactions (comment_id,user_id,kind,created_at)
                     VALUES (?,?,?,?)
                     ON CONFLICT(comment_id,user_id) DO UPDATE SET kind=excluded.kind""",
                  (comment_id, uid, data.kind, now_iso()))
    return {"ok": True}

@app.delete("/comments/{comment_id}")
def delete_comment(comment_id: str, uid: str = Depends(current_user)):
    with db() as c:
        r = c.execute("SELECT user_id FROM comments WHERE id=?", (comment_id,)).fetchone()
        if not r: raise HTTPException(404, "comment not found")
        if r["user_id"] != uid: raise HTTPException(403, "not your comment")
        c.execute("DELETE FROM comments WHERE id=?", (comment_id,))
    return {"ok": True}

# ----------------------------- WORLD (feed) -----------------------------
def _cursor_clause(cursor: Optional[str], params: list, col: str = "created_at") -> str:
    if not cursor:
        return ""
    try:
        ts, pid = cursor.split("|", 1)
    except ValueError:
        return ""
    params.extend([ts, ts, pid])
    return f" AND (p.{col} < ? OR (p.{col} = ? AND p.id < ?))"

@app.get("/world")
def world_feed(limit: int = 20, cursor: Optional[str] = None,
               post_type: Optional[str] = None,
               uid: str = Depends(current_user)):
    limit = max(1, min(limit, 50))
    if post_type and post_type not in POST_TYPES:
        raise HTTPException(400, "invalid post_type")
    with db() as c:
        allowed = {uid}
        allowed |= {r["friend_id"] for r in c.execute(
            "SELECT friend_id FROM friendships WHERE user_id=?", (uid,))}
        allowed |= {r["followee_id"] for r in c.execute(
            "SELECT followee_id FROM follows WHERE follower_id=?", (uid,))}
        ph = ",".join("?" * len(allowed)) or "''"
        sql = f"""
            SELECT p.*, u.username, u.display_name, u.avatar_url, u.verified,
              (SELECT COUNT(*) FROM reactions WHERE post_id=p.id) AS reaction_count,
              (SELECT COUNT(*) FROM comments WHERE post_id=p.id) AS comment_count,
              (SELECT kind FROM reactions WHERE post_id=p.id AND user_id=?) AS reacted_by_me
            FROM posts p JOIN users u ON u.id=p.user_id
            WHERE p.user_id IN ({ph})
              AND p.space_id IS NULL AND p.page_id IS NULL
              AND u.status='active'
              AND (p.visibility='public'
                   OR p.visibility='connections' AND p.user_id IN (
                       SELECT friend_id FROM friendships WHERE user_id=?)
                   OR p.user_id=?)
              AND p.user_id NOT IN (SELECT blocked_id FROM blocks WHERE blocker_id=?)
              AND p.user_id NOT IN (SELECT blocker_id FROM blocks WHERE blocked_id=?)
        """
        params = [uid] + list(allowed) + [uid, uid, uid, uid]
        if post_type:
            sql += " AND p.post_type=?"
            params.append(post_type)
        sql += _cursor_clause(cursor, params)
        sql += " ORDER BY p.created_at DESC, p.id DESC LIMIT ?"
        params.append(limit)
        rows = c.execute(sql, params).fetchall()
        out = []
        for r in rows:
            d = row_to_dict(r)
            d["meta"] = json_or_empty(d.get("meta"))
            out.append(d)
        return out

@app.get("/world/discover")
def discover(limit: int = 20, cursor: Optional[str] = None,
             kind: str = "mixed", uid: str = Depends(current_user)):
    limit = max(1, min(limit, 40))
    with db() as c:
        friend_ids = {r["friend_id"] for r in c.execute(
            "SELECT friend_id FROM friendships WHERE user_id=?", (uid,))}
        exclude = set(friend_ids) | {uid}
        blocked = {r["blocked_id"] for r in c.execute(
            "SELECT blocked_id FROM blocks WHERE blocker_id=?", (uid,))}
        blocked |= {r["blocker_id"] for r in c.execute(
            "SELECT blocker_id FROM blocks WHERE blocked_id=?", (uid,))}
        exclude |= blocked
        ph = ",".join("?" * len(exclude)) or "''"
        params = [uid] + list(exclude)
        sql = f"""
            SELECT p.*, u.username, u.display_name, u.avatar_url, u.verified,
              (SELECT COUNT(*) FROM reactions WHERE post_id=p.id) AS reaction_count,
              (SELECT COUNT(*) FROM comments WHERE post_id=p.id) AS comment_count,
              (SELECT kind FROM reactions WHERE post_id=p.id AND user_id=?) AS reacted_by_me
            FROM posts p JOIN users u ON u.id=p.user_id
            WHERE p.visibility='public' AND p.space_id IS NULL AND p.page_id IS NULL
              AND u.status='active'
              AND p.user_id NOT IN ({ph})
        """
        sql += _cursor_clause(cursor, params)
        sql += " ORDER BY p.created_at DESC, p.id DESC LIMIT ?"
        params.append(limit)
        rows = c.execute(sql, params).fetchall()
        out = []
        for r in rows:
            d = row_to_dict(r)
            d["meta"] = json_or_empty(d.get("meta"))
            out.append(d)
        return out

@app.get("/world/trending")
def trending(limit: int = 20, uid: str = Depends(current_user)):
    limit = max(1, min(limit, 50))
    with db() as c:
        rows = c.execute("""
            SELECT p.*, u.username, u.display_name, u.avatar_url,
              (SELECT COUNT(*) FROM reactions WHERE post_id=p.id) AS reaction_count,
              (SELECT COUNT(*) FROM comments WHERE post_id=p.id) AS comment_count
            FROM posts p JOIN users u ON u.id=p.user_id
            WHERE p.visibility='public' AND p.space_id IS NULL AND p.page_id IS NULL
              AND u.status='active'
              AND p.created_at > ?
        """, ((now_utc() - timedelta(hours=72)).isoformat(),)).fetchall()
    scored = []
    for r in rows:
        s = 1.0 + 3.0 * r["reaction_count"] + 4.0 * r["comment_count"]
        s = decay(s, r["created_at"], RANK_HALF_LIFE_HOURS)
        d = row_to_dict(r); d["_score"] = round(s, 4)
        d["meta"] = json_or_empty(d.get("meta"))
        scored.append(d)
    scored.sort(key=lambda x: x["_score"], reverse=True)
    return scored[:limit]

@app.get("/world/space/{space_id}")
def space_world(space_id: str, limit: int = 20, uid: str = Depends(current_user)):
    limit = max(1, min(limit, 50))
    with db() as c:
        s = c.execute("SELECT privacy FROM spaces WHERE id=?", (space_id,)).fetchone()
        if not s: raise HTTPException(404, "space not found")
        if s["privacy"] == "secret":
            m = c.execute("SELECT 1 FROM space_members WHERE space_id=? AND user_id=?",
                          (space_id, uid)).fetchone()
            if not m: raise HTTPException(404, "space not found")
        rows = c.execute("""SELECT p.*, u.username, u.display_name, u.avatar_url
                            FROM posts p JOIN users u ON u.id=p.user_id
                            WHERE p.space_id=? AND u.status='active'
                            ORDER BY p.created_at DESC, p.id DESC LIMIT ?""",
                         (space_id, limit)).fetchall()
        return [row_to_dict(r) for r in rows]

# ----------------------------- PEOPLE (connections) -----------------------------
@app.post("/people/request/{target_id}")
def connection_request(target_id: str, uid: str = Depends(current_user)):
    if target_id == uid: raise HTTPException(400, "cannot connect self")
    with db() as c:
        if not user_active(c, target_id):
            raise HTTPException(404, "user not found")
        if is_blocked(c, uid, target_id):
            raise HTTPException(403, "blocked")
        if are_connected(c, uid, target_id):
            raise HTTPException(409, "already connected")
        existing = c.execute("""SELECT id,status FROM connection_requests
                                WHERE sender_id=? AND receiver_id=?""",
                             (uid, target_id)).fetchone()
        if existing and existing["status"] == "pending":
            raise HTTPException(409, "request already pending")
        rid = new_id()
        c.execute("""INSERT INTO connection_requests (id,sender_id,receiver_id,status,created_at)
                     VALUES (?,?,?,?,?)""",
                  (rid, uid, target_id, "pending", now_iso()))
        notify(c, target_id, "connection_request", {"from": uid, "request_id": rid})
    return {"request_id": rid}

@app.post("/people/accept/{request_id}")
def connection_accept(request_id: str, uid: str = Depends(current_user)):
    with db() as c:
        r = c.execute("""SELECT * FROM connection_requests
                         WHERE id=? AND receiver_id=? AND status='pending'""",
                      (request_id, uid)).fetchone()
        if not r: raise HTTPException(404, "no pending request")
        c.execute("UPDATE connection_requests SET status='accepted', responded_at=? WHERE id=?",
                  (now_iso(), request_id))
        ts = now_iso()
        c.execute("INSERT OR IGNORE INTO friendships (user_id,friend_id,created_at) VALUES (?,?,?)",
                  (r["sender_id"], uid, ts))
        c.execute("INSERT OR IGNORE INTO friendships (user_id,friend_id,created_at) VALUES (?,?,?)",
                  (uid, r["sender_id"], ts))
        notify(c, r["sender_id"], "connection_accept", {"by": uid})
    return {"ok": True}

@app.post("/people/decline/{request_id}")
def connection_decline(request_id: str, uid: str = Depends(current_user)):
    with db() as c:
        c.execute("""UPDATE connection_requests SET status='declined', responded_at=?
                     WHERE id=? AND receiver_id=? AND status='pending'""",
                  (now_iso(), request_id, uid))
    return {"ok": True}

@app.post("/people/cancel/{request_id}")
def connection_cancel(request_id: str, uid: str = Depends(current_user)):
    with db() as c:
        c.execute("""UPDATE connection_requests SET status='cancelled', responded_at=?
                     WHERE id=? AND sender_id=? AND status='pending'""",
                  (now_iso(), request_id, uid))
    return {"ok": True}

@app.delete("/people/{person_id}")
def disconnect(person_id: str, uid: str = Depends(current_user)):
    with db() as c:
        c.execute("""DELETE FROM friendships
                     WHERE (user_id=? AND friend_id=?) OR (user_id=? AND friend_id=?)""",
                  (uid, person_id, person_id, uid))
    return {"ok": True}

@app.get("/people")
def list_people(uid: str = Depends(current_user)):
    with db() as c:
        rows = c.execute("""SELECT u.id,u.username,u.display_name,u.avatar_url,u.verified
                            FROM friendships f JOIN users u ON u.id=f.friend_id
                            WHERE f.user_id=? AND u.status='active'""", (uid,)).fetchall()
        return [row_to_dict(r) for r in rows]

@app.get("/people/requests")
def incoming_connections(uid: str = Depends(current_user)):
    with db() as c:
        rows = c.execute("""SELECT cr.id AS request_id, u.id AS user_id, u.username,
                                   u.display_name, u.avatar_url, cr.created_at
                            FROM connection_requests cr JOIN users u ON u.id=cr.sender_id
                            WHERE cr.receiver_id=? AND cr.status='pending'""", (uid,)).fetchall()
        return [row_to_dict(r) for r in rows]

@app.post("/people/follow/{target_id}")
def follow_person(target_id: str, uid: str = Depends(current_user)):
    if target_id == uid: raise HTTPException(400, "cannot follow self")
    with db() as c:
        if not user_active(c, target_id): raise HTTPException(404, "user not found")
        if is_blocked(c, uid, target_id): raise HTTPException(403, "blocked")
        existing = c.execute("SELECT 1 FROM follows WHERE follower_id=? AND followee_id=?",
                             (uid, target_id)).fetchone()
        if existing: return {"ok": True, "already": True}
        c.execute("INSERT INTO follows (follower_id,followee_id,created_at) VALUES (?,?,?)",
                  (uid, target_id, now_iso()))
        notify(c, target_id, "follow", {"by": uid})
    return {"ok": True}

@app.delete("/people/follow/{target_id}")
def unfollow_person(target_id: str, uid: str = Depends(current_user)):
    with db() as c:
        c.execute("DELETE FROM follows WHERE follower_id=? AND followee_id=?",
                  (uid, target_id))
    return {"ok": True}

@app.get("/people/followers/{user_id}")
def followers(user_id: str):
    with db() as c:
        rows = c.execute("""SELECT u.id,u.username,u.display_name,u.avatar_url FROM follows f
                            JOIN users u ON u.id=f.follower_id
                            WHERE f.followee_id=? AND u.status='active'""", (user_id,)).fetchall()
        return [row_to_dict(r) for r in rows]

@app.get("/people/following/{user_id}")
def following(user_id: str):
    with db() as c:
        rows = c.execute("""SELECT u.id,u.username,u.display_name,u.avatar_url FROM follows f
                            JOIN users u ON u.id=f.followee_id
                            WHERE f.follower_id=? AND u.status='active'""", (user_id,)).fetchall()
        return [row_to_dict(r) for r in rows]

@app.get("/people/search")
def search_people(q: str = Query(..., min_length=1), uid: str = Depends(current_user)):
    with db() as c:
        rows = c.execute("""SELECT id,username,display_name,avatar_url,verified,
                                   CASE
                                     WHEN LOWER(username)=LOWER(?) THEN 0
                                     WHEN LOWER(username) LIKE LOWER(?) THEN 1
                                     WHEN LOWER(display_name) LIKE LOWER(?) THEN 2
                                     ELSE 3 END AS rank
                            FROM users
                            WHERE (username LIKE ? OR display_name LIKE ?)
                              AND status='active'
                              AND id NOT IN (SELECT blocked_id FROM blocks WHERE blocker_id=?)
                              AND id NOT IN (SELECT blocker_id FROM blocks WHERE blocked_id=?)
                            ORDER BY rank, username LIMIT 30""",
                         (q, f"{q}%", f"{q}%", f"%{q}%", f"%{q}%", uid, uid)).fetchall()
        return [row_to_dict(r) for r in rows]

# ----------------------------- SPACES -----------------------------
@app.post("/spaces")
def create_space(data: SpaceIn, uid: str = Depends(current_user)):
    if data.privacy not in SPACE_PRIVACY:
        raise HTTPException(400, "privacy must be public/private/secret")
    gid = new_id()
    slug = slugify(data.name)
    with db() as c:
        base = slug
        i = 1
        while c.execute("SELECT 1 FROM spaces WHERE slug=?", (slug,)).fetchone():
            slug = f"{base}-{i}"; i += 1
        c.execute("""INSERT INTO spaces
                     (id,slug,name,topic,description,privacy,owner_id,cover_url,rules,created_at)
                     VALUES (?,?,?,?,?,?,?,?,?,?)""",
                  (gid, slug, data.name, data.topic or "", data.description or "",
                   data.privacy, uid, data.cover_url or "", data.rules or "", now_iso()))
        c.execute("""INSERT INTO space_members (space_id,user_id,role,joined_at)
                     VALUES (?,?,?,?)""", (gid, uid, "owner", now_iso()))
        award(c, uid, "builder", 3, "space", gid)
    return {"space_id": gid, "slug": slug}

@app.get("/spaces/{space_id}")
def get_space(space_id: str, uid: str = Depends(current_user)):
    with db() as c:
        r = c.execute("SELECT * FROM spaces WHERE id=?", (space_id,)).fetchone()
        if not r: raise HTTPException(404, "space not found")
        if r["privacy"] == "secret":
            m = c.execute("SELECT 1 FROM space_members WHERE space_id=? AND user_id=?",
                          (space_id, uid)).fetchone()
            if not m: raise HTTPException(404, "space not found")
        d = row_to_dict(r)
        d["member_count"] = c.execute(
            "SELECT COUNT(*) FROM space_members WHERE space_id=?", (space_id,)).fetchone()[0]
        d["is_member"] = c.execute(
            "SELECT 1 FROM space_members WHERE space_id=? AND user_id=?",
            (space_id, uid)).fetchone() is not None
        return d

@app.patch("/spaces/{space_id}")
def update_space(space_id: str, data: SpaceUpdateIn, uid: str = Depends(current_user)):
    with db() as c:
        role = c.execute("SELECT role FROM space_members WHERE space_id=? AND user_id=?",
                         (space_id, uid)).fetchone()
        if not role or role["role"] not in ("owner", "moderator"):
            raise HTTPException(403, "insufficient role")
        fields, values = [], []
        for k, v in data.model_dump(exclude_none=True).items():
            if k == "privacy" and v not in SPACE_PRIVACY:
                raise HTTPException(400, "invalid privacy")
            fields.append(f"{k}=?"); values.append(v)
        if not fields:
            raise HTTPException(400, "nothing to update")
        fields.append("updated_at=?"); values.append(now_iso())
        values.append(space_id)
        c.execute(f"UPDATE spaces SET {','.join(fields)} WHERE id=?", values)
    return {"ok": True}

@app.post("/spaces/{space_id}/join")
def join_space(space_id: str, uid: str = Depends(current_user)):
    with db() as c:
        r = c.execute("SELECT privacy,owner_id FROM spaces WHERE id=?", (space_id,)).fetchone()
        if not r: raise HTTPException(404, "space not found")
        if r["privacy"] != "public":
            raise HTTPException(403, "space requires invitation")
        existing = c.execute("SELECT 1 FROM space_members WHERE space_id=? AND user_id=?",
                             (space_id, uid)).fetchone()
        if existing: return {"ok": True, "already": True}
        c.execute("""INSERT INTO space_members (space_id,user_id,role,joined_at)
                     VALUES (?,?,?,?)""", (space_id, uid, "member", now_iso()))
        notify(c, r["owner_id"], "space_join", {"space_id": space_id, "by": uid})
    return {"ok": True}

@app.post("/spaces/{space_id}/leave")
def leave_space(space_id: str, uid: str = Depends(current_user)):
    with db() as c:
        c.execute("""DELETE FROM space_members
                     WHERE space_id=? AND user_id=? AND role!='owner'""",
                  (space_id, uid))
    return {"ok": True}

@app.get("/spaces/{space_id}/members")
def space_members(space_id: str, uid: str = Depends(current_user)):
    with db() as c:
        r = c.execute("SELECT privacy FROM spaces WHERE id=?", (space_id,)).fetchone()
        if not r: raise HTTPException(404, "space not found")
        if r["privacy"] == "secret":
            m = c.execute("SELECT 1 FROM space_members WHERE space_id=? AND user_id=?",
                          (space_id, uid)).fetchone()
            if not m: raise HTTPException(404, "space not found")
        rows = c.execute("""SELECT u.id,u.username,u.display_name,u.avatar_url, sm.role
                            FROM space_members sm JOIN users u ON u.id=sm.user_id
                            WHERE sm.space_id=?""", (space_id,)).fetchall()
        return [row_to_dict(r) for r in rows]

@app.post("/spaces/{space_id}/members/{member_id}/promote")
def promote_member(space_id: str, member_id: str, uid: str = Depends(current_user)):
    with db() as c:
        o = c.execute("SELECT role FROM space_members WHERE space_id=? AND user_id=?",
                      (space_id, uid)).fetchone()
        if not o or o["role"] not in ("owner", "moderator"):
            raise HTTPException(403, "insufficient role")
        c.execute("UPDATE space_members SET role='moderator' WHERE space_id=? AND user_id=?",
                  (space_id, member_id))
    return {"ok": True}

@app.post("/spaces/{space_id}/items")
def create_space_item(space_id: str, data: SpaceItemIn, uid: str = Depends(current_user)):
    if data.kind not in SPACE_ITEM_KINDS:
        raise HTTPException(400, "invalid kind")
    with db() as c:
        m = c.execute("SELECT role FROM space_members WHERE space_id=? AND user_id=?",
                      (space_id, uid)).fetchone()
        if not m: raise HTTPException(403, "not a member")
        if data.kind == "announcement" and m["role"] not in ("owner", "moderator"):
            raise HTTPException(403, "only moderators can post announcements")
        iid = new_id()
        c.execute("""INSERT INTO space_items
                     (id,space_id,user_id,kind,title,body,meta,pinned,created_at)
                     VALUES (?,?,?,?,?,?,?,?,?)""",
                  (iid, space_id, uid, data.kind, data.title or "", data.body or "",
                   json.dumps(data.meta or {}), 1 if data.pinned else 0, now_iso()))
    return {"item_id": iid}

@app.get("/spaces/{space_id}/items")
def list_space_items(space_id: str, kind: Optional[str] = None,
                     limit: int = 50, uid: str = Depends(current_user)):
    limit = max(1, min(limit, 200))
    with db() as c:
        s = c.execute("SELECT privacy FROM spaces WHERE id=?", (space_id,)).fetchone()
        if not s: raise HTTPException(404, "space not found")
        if s["privacy"] in ("private", "secret"):
            m = c.execute("SELECT 1 FROM space_members WHERE space_id=? AND user_id=?",
                          (space_id, uid)).fetchone()
            if not m: raise HTTPException(403, "not a member")
        params = [space_id]
        sql = """SELECT i.*, u.username, u.display_name, u.avatar_url
                 FROM space_items i JOIN users u ON u.id=i.user_id
                 WHERE i.space_id=? AND u.status='active'"""
        if kind:
            if kind not in SPACE_ITEM_KINDS: raise HTTPException(400, "invalid kind")
            sql += " AND i.kind=?"; params.append(kind)
        sql += " ORDER BY i.pinned DESC, i.created_at DESC LIMIT ?"
        params.append(limit)
        rows = c.execute(sql, params).fetchall()
        out = []
        for r in rows:
            d = row_to_dict(r); d["meta"] = json_or_empty(d.get("meta"))
            out.append(d)
        return out

@app.get("/spaces")
def list_spaces(limit: int = 30, uid: str = Depends(current_user)):
    limit = max(1, min(limit, 100))
    with db() as c:
        rows = c.execute("""SELECT id,slug,name,topic,privacy,cover_url,created_at
                            FROM spaces WHERE privacy='public'
                            ORDER BY created_at DESC LIMIT ?""", (limit,)).fetchall()
        return [row_to_dict(r) for r in rows]

# ----------------------------- PAGES -----------------------------
@app.post("/pages")
def create_page(data: PageIn, uid: str = Depends(current_user)):
    pid = new_id()
    slug = slugify(data.name)
    with db() as c:
        base = slug; i = 1
        while c.execute("SELECT 1 FROM pages WHERE slug=?", (slug,)).fetchone():
            slug = f"{base}-{i}"; i += 1
        c.execute("""INSERT INTO pages
                     (id,slug,name,category,description,owner_id,avatar_url,cover_url,created_at)
                     VALUES (?,?,?,?,?,?,?,?,?)""",
                  (pid, slug, data.name, data.category or "", data.description or "",
                   uid, data.avatar_url or "", data.cover_url or "", now_iso()))
    return {"page_id": pid, "slug": slug}

@app.get("/pages/{page_id}")
def get_page(page_id: str):
    with db() as c:
        r = c.execute("SELECT * FROM pages WHERE id=?", (page_id,)).fetchone()
        if not r: raise HTTPException(404, "page not found")
        d = row_to_dict(r)
        d["follower_count"] = c.execute(
            "SELECT COUNT(*) FROM page_followers WHERE page_id=?", (page_id,)).fetchone()[0]
        return d

@app.post("/pages/{page_id}/follow")
def follow_page(page_id: str, uid: str = Depends(current_user)):
    with db() as c:
        if not c.execute("SELECT 1 FROM pages WHERE id=?", (page_id,)).fetchone():
            raise HTTPException(404, "page not found")
        c.execute("""INSERT OR IGNORE INTO page_followers (page_id,user_id,created_at)
                     VALUES (?,?,?)""", (page_id, uid, now_iso()))
    return {"ok": True}

@app.delete("/pages/{page_id}/follow")
def unfollow_page(page_id: str, uid: str = Depends(current_user)):
    with db() as c:
        c.execute("DELETE FROM page_followers WHERE page_id=? AND user_id=?",
                  (page_id, uid))
    return {"ok": True}

@app.get("/pages/{page_id}/feed")
def page_feed(page_id: str, limit: int = 20):
    limit = max(1, min(limit, 50))
    with db() as c:
        rows = c.execute("""SELECT p.*, u.username, u.display_name, u.avatar_url
                            FROM posts p JOIN users u ON u.id=p.user_id
                            WHERE p.page_id=? ORDER BY p.created_at DESC, p.id DESC LIMIT ?""",
                         (page_id, limit)).fetchall()
        return [row_to_dict(r) for r in rows]

# ----------------------------- MOMENTS -----------------------------
@app.post("/moments")
def create_moment(data: MomentIn, uid: str = Depends(current_user)):
    if data.kind not in ("text", "photo", "video", "music", "location"):
        raise HTTPException(400, "invalid kind")
    if data.visibility not in ("public", "connections", "private"):
        raise HTTPException(400, "invalid visibility")
    sid = new_id()
    exp = (now_utc() + timedelta(hours=data.ttl_hours)).isoformat()
    with db() as c:
        c.execute("""INSERT INTO moments
                     (id,user_id,kind,body,media_url,meta,visibility,expires_at,created_at)
                     VALUES (?,?,?,?,?,?,?,?,?)""",
                  (sid, uid, data.kind, data.body or "", data.media_url or "",
                   json.dumps(data.meta or {}), data.visibility, exp, now_iso()))
    return {"moment_id": sid, "expires_at": exp}

@app.get("/moments/world")
def moments_world(uid: str = Depends(current_user)):
    with db() as c:
        friend_ids = [r["friend_id"] for r in c.execute(
            "SELECT friend_id FROM friendships WHERE user_id=?", (uid,))]
        allowed = list({uid, *friend_ids})
        ph = ",".join("?" * len(allowed))
        rows = c.execute(f"""SELECT m.*, u.username, u.display_name, u.avatar_url
                             FROM moments m JOIN users u ON u.id=m.user_id
                             WHERE m.user_id IN ({ph})
                               AND m.expires_at > ?
                               AND u.status='active'
                               AND (m.visibility='public'
                                    OR m.user_id=?
                                    OR (m.visibility='connections' AND m.user_id IN (
                                        SELECT friend_id FROM friendships WHERE user_id=?)))
                             ORDER BY m.created_at DESC""",
                         (*allowed, now_iso(), uid, uid)).fetchall()
        out = []
        for r in rows:
            d = row_to_dict(r); d["meta"] = json_or_empty(d.get("meta"))
            out.append(d)
        return out

@app.post("/moments/{moment_id}/view")
def view_moment(moment_id: str, uid: str = Depends(current_user)):
    with db() as c:
        m = c.execute("""SELECT m.user_id,m.visibility,m.expires_at FROM moments m
                         WHERE m.id=?""", (moment_id,)).fetchone()
        if not m: raise HTTPException(404, "moment not found")
        if datetime.fromisoformat(m["expires_at"]) < now_utc():
            raise HTTPException(410, "moment expired")
        if m["user_id"] != uid:
            if is_blocked(c, uid, m["user_id"]): raise HTTPException(403, "blocked")
            if m["visibility"] == "private": raise HTTPException(403, "not visible")
            if m["visibility"] == "connections" and not are_connected(c, m["user_id"], uid):
                raise HTTPException(403, "not visible")
        c.execute("""INSERT OR IGNORE INTO moment_views (moment_id,viewer_id,created_at)
                     VALUES (?,?,?)""", (moment_id, uid, now_iso()))
        stat_bump(c, "moment", moment_id, m["user_id"], views=1)
    return {"ok": True}

@app.post("/moments/{moment_id}/reply")
def reply_moment(moment_id: str, data: MomentReplyIn, uid: str = Depends(current_user)):
    with db() as c:
        m = c.execute("SELECT user_id,expires_at FROM moments WHERE id=?",
                      (moment_id,)).fetchone()
        if not m: raise HTTPException(404, "moment not found")
        if datetime.fromisoformat(m["expires_at"]) < now_utc():
            raise HTTPException(410, "moment expired")
        if m["user_id"] != uid and is_blocked(c, uid, m["user_id"]):
            raise HTTPException(403, "blocked")
        rid = new_id()
        c.execute("""INSERT INTO moment_replies (id,moment_id,user_id,body,created_at)
                     VALUES (?,?,?,?,?)""",
                  (rid, moment_id, uid, data.body, now_iso()))
        if m["user_id"] != uid:
            notify(c, m["user_id"], "moment_reply",
                   {"moment_id": moment_id, "by": uid, "reply_id": rid})
    return {"reply_id": rid}

@app.get("/moments/{moment_id}/replies")
def moment_replies(moment_id: str, uid: str = Depends(current_user)):
    with db() as c:
        m = c.execute("SELECT user_id,expires_at FROM moments WHERE id=?",
                      (moment_id,)).fetchone()
        if not m: raise HTTPException(404, "moment not found")
        if m["user_id"] != uid and is_blocked(c, uid, m["user_id"]):
            raise HTTPException(403, "blocked")
        rows = c.execute("""SELECT r.*, u.username, u.display_name, u.avatar_url
                            FROM moment_replies r JOIN users u ON u.id=r.user_id
                            WHERE r.moment_id=? ORDER BY r.created_at ASC""",
                         (moment_id,)).fetchall()
        return [row_to_dict(r) for r in rows]

@app.delete("/moments/{moment_id}")
def delete_moment(moment_id: str, uid: str = Depends(current_user)):
    with db() as c:
        r = c.execute("SELECT user_id FROM moments WHERE id=?", (moment_id,)).fetchone()
        if not r: raise HTTPException(404, "moment not found")
        if r["user_id"] != uid: raise HTTPException(403, "not your moment")
        c.execute("DELETE FROM moments WHERE id=?", (moment_id,))
    return {"ok": True}

# ----------------------------- MESSAGING -----------------------------
@app.post("/conversations")
def create_conversation(data: ConversationIn, uid: str = Depends(current_user)):
    if not data.member_ids: raise HTTPException(400, "no members")
    members = set(data.member_ids) | {uid}
    kind = "group" if len(members) > 2 else "direct"
    cid = new_id()
    with db() as c:
        for m in members:
            if not user_active(c, m):
                raise HTTPException(404, f"user {m} not found")
            if m != uid and is_blocked(c, uid, m):
                raise HTTPException(403, "blocked user in members")
        c.execute("""INSERT INTO conversations (id,kind,title,created_by,created_at)
                     VALUES (?,?,?,?,?)""",
                  (cid, kind, data.title or "", uid, now_iso()))
        for m in members:
            c.execute("""INSERT INTO conversation_members
                         (conversation_id,user_id,role,joined_at)
                         VALUES (?,?,?,?)""",
                      (cid, m, "owner" if m == uid else "member", now_iso()))
    return {"conversation_id": cid, "kind": kind}

@app.get("/conversations")
def list_conversations(uid: str = Depends(current_user)):
    with db() as c:
        rows = c.execute("""SELECT c.id,c.kind,c.title,c.created_at,
                                   (SELECT MAX(created_at) FROM messages
                                    WHERE conversation_id=c.id) AS last_at,
                                   (SELECT COUNT(*) FROM messages
                                    WHERE conversation_id=c.id AND sender_id!=?
                                      AND read_at IS NULL) AS unread
                            FROM conversations c
                            JOIN conversation_members m ON m.conversation_id=c.id
                            WHERE m.user_id=?
                            ORDER BY last_at DESC""",
                         (uid, uid)).fetchall()
        return [row_to_dict(r) for r in rows]

@app.get("/conversations/{conversation_id}/messages")
def conversation_messages(conversation_id: str, limit: int = 50,
                          before: Optional[str] = None,
                          uid: str = Depends(current_user)):
    limit = max(1, min(limit, 200))
    with db() as c:
        m = c.execute("""SELECT 1 FROM conversation_members
                         WHERE conversation_id=? AND user_id=?""",
                      (conversation_id, uid)).fetchone()
        if not m: raise HTTPException(403, "not a member")
        params = [conversation_id]
        sql = "SELECT * FROM messages WHERE conversation_id=?"
        if before:
            sql += " AND created_at < ?"; params.append(before)
        sql += " ORDER BY created_at DESC LIMIT ?"; params.append(limit)
        rows = c.execute(sql, params).fetchall()
        out = [row_to_dict(r) for r in rows][::-1]
        c.execute("""UPDATE messages SET read_at=?
                     WHERE conversation_id=? AND sender_id!=? AND read_at IS NULL""",
                  (now_iso(), conversation_id, uid))
        c.execute("""UPDATE conversation_members SET last_read_at=?
                     WHERE conversation_id=? AND user_id=?""",
                  (now_iso(), conversation_id, uid))
        return out

@app.post("/messages")
def send_message(data: MessageIn, uid: str = Depends(current_user)):
    if data.kind not in ("text", "media", "voice", "poll", "shared_post", "file"):
        raise HTTPException(400, "invalid kind")
    with db() as c:
        if data.conversation_id:
            m = c.execute("""SELECT 1 FROM conversation_members
                             WHERE conversation_id=? AND user_id=?""",
                          (data.conversation_id, uid)).fetchone()
            if not m: raise HTTPException(403, "not a member")
            mid = new_id()
            c.execute("""INSERT INTO messages
                         (id,conversation_id,sender_id,body,kind,media_url,reply_to,created_at)
                         VALUES (?,?,?,?,?,?,?,?)""",
                      (mid, data.conversation_id, uid, data.body, data.kind,
                       data.media_url or "", data.reply_to, now_iso()))
            return {"message_id": mid, "conversation_id": data.conversation_id}
        if not data.recipient_id:
            raise HTTPException(400, "recipient_id or conversation_id required")
        if data.recipient_id == uid: raise HTTPException(400, "cannot message self")
        if not user_active(c, data.recipient_id):
            raise HTTPException(404, "recipient not found")
        if is_blocked(c, uid, data.recipient_id):
            raise HTTPException(403, "blocked")
        convo = c.execute("""SELECT c.id FROM conversations c
                             JOIN conversation_members m1 ON m1.conversation_id=c.id
                             JOIN conversation_members m2 ON m2.conversation_id=c.id
                             WHERE c.kind='direct'
                               AND m1.user_id=? AND m2.user_id=?""",
                          (uid, data.recipient_id)).fetchone()
        if convo:
            cid = convo["id"]
        else:
            cid = new_id()
            c.execute("""INSERT INTO conversations (id,kind,created_by,created_at)
                         VALUES (?,?,?,?)""",
                      (cid, "direct", uid, now_iso()))
            c.execute("""INSERT INTO conversation_members
                         (conversation_id,user_id,role,joined_at) VALUES (?,?,?,?)""",
                      (cid, uid, "owner", now_iso()))
            c.execute("""INSERT INTO conversation_members
                         (conversation_id,user_id,role,joined_at) VALUES (?,?,?,?)""",
                      (cid, data.recipient_id, "member", now_iso()))
        mid = new_id()
        tk = "::".join(sorted([uid, data.recipient_id]))
        c.execute("""INSERT INTO messages
                     (id,conversation_id,thread_id,sender_id,recipient_id,body,kind,media_url,reply_to,created_at)
                     VALUES (?,?,?,?,?,?,?,?,?,?)""",
                  (mid, cid, tk, uid, data.recipient_id, data.body, data.kind,
                   data.media_url or "", data.reply_to, now_iso()))
        notify(c, data.recipient_id, "message", {"from": uid, "message_id": mid})
    return {"message_id": mid, "conversation_id": cid}

@app.post("/conversations/{conversation_id}/pin/{message_id}")
def pin_message(conversation_id: str, message_id: str,
                uid: str = Depends(current_user)):
    with db() as c:
        m = c.execute("""SELECT role FROM conversation_members
                         WHERE conversation_id=? AND user_id=?""",
                      (conversation_id, uid)).fetchone()
        if not m: raise HTTPException(403, "not a member")
        c.execute("""UPDATE messages SET pinned=1
                     WHERE id=? AND conversation_id=?""",
                  (message_id, conversation_id))
    return {"ok": True}

@app.post("/messages/{message_id}/read")
def mark_message_read(message_id: str, uid: str = Depends(current_user)):
    with db() as c:
        c.execute("""UPDATE messages SET read_at=?
                     WHERE id=? AND recipient_id=? AND read_at IS NULL""",
                  (now_iso(), message_id, uid))
    return {"ok": True}

@app.get("/messages/unread-count")
def unread_messages_count(uid: str = Depends(current_user)):
    with db() as c:
        n = c.execute("""SELECT COUNT(*) FROM messages m
                         WHERE (
                           (m.recipient_id=? AND m.read_at IS NULL)
                           OR (m.conversation_id IN (
                                 SELECT conversation_id FROM conversation_members
                                 WHERE user_id=?) AND m.sender_id!=? AND m.read_at IS NULL)
                         )""", (uid, uid, uid)).fetchone()[0]
        return {"unread": n}

@app.get("/messages/thread/{other_id}")
def get_thread(other_id: str, limit: int = 50, before: Optional[str] = None,
               uid: str = Depends(current_user)):
    limit = max(1, min(limit, 200))
    tk = "::".join(sorted([uid, other_id]))
    params = [tk]
    sql = "SELECT * FROM messages WHERE thread_id=?"
    if before:
        sql += " AND created_at < ?"; params.append(before)
    sql += " ORDER BY created_at DESC LIMIT ?"; params.append(limit)
    with db() as c:
        if is_blocked(c, uid, other_id): raise HTTPException(403, "blocked")
        rows = c.execute(sql, params).fetchall()
        out = [row_to_dict(r) for r in rows][::-1]
        c.execute("""UPDATE messages SET read_at=?
                     WHERE thread_id=? AND recipient_id=? AND read_at IS NULL""",
                  (now_iso(), tk, uid))
        return out

# ----------------------------- NOTIFICATIONS -----------------------------
@app.get("/notifications")
def list_notifications(limit: int = 50, kind: Optional[str] = None,
                       uid: str = Depends(current_user)):
    limit = max(1, min(limit, 200))
    params = [uid]
    sql = "SELECT * FROM notifications WHERE user_id=?"
    if kind:
        if kind not in NOTIF_KINDS: raise HTTPException(400, "unknown kind")
        sql += " AND kind=?"; params.append(kind)
    sql += " ORDER BY created_at DESC LIMIT ?"; params.append(limit)
    with db() as c:
        rows = c.execute(sql, params).fetchall()
        out = []
        for r in rows:
            d = row_to_dict(r)
            d["payload"] = json_or_empty(d["payload"])
            out.append(d)
        return out

@app.get("/notifications/unread_count")
def unread_count(uid: str = Depends(current_user)):
    with db() as c:
        n = c.execute("SELECT COUNT(*) FROM notifications WHERE user_id=? AND read_at IS NULL",
                      (uid,)).fetchone()[0]
        return {"unread": n}

@app.post("/notifications/read")
def mark_all_read(uid: str = Depends(current_user)):
    with db() as c:
        c.execute("UPDATE notifications SET read_at=? WHERE user_id=? AND read_at IS NULL",
                  (now_iso(), uid))
    return {"ok": True}

# ----------------------------- REPUTATION -----------------------------
@app.get("/reputation/me")
def my_reputation(uid: str = Depends(current_user)):
    with db() as c:
        total = c.execute("SELECT COALESCE(reputation_score,0) FROM users WHERE id=?",
                          (uid,)).fetchone()[0]
        by_kind = dict(c.execute("""SELECT kind, SUM(points) FROM reputation_events
                                    WHERE user_id=? GROUP BY kind""", (uid,)).fetchall())
        recent = [row_to_dict(r) for r in c.execute("""SELECT kind,points,source_kind,
                                                             source_id,created_at
                                                      FROM reputation_events
                                                      WHERE user_id=?
                                                      ORDER BY created_at DESC LIMIT 50""",
                                                   (uid,)).fetchall()]
        badges = [r["badge"] for r in c.execute(
            "SELECT badge FROM user_badges WHERE user_id=?", (uid,))]
        return {"total": total, "by_kind": by_kind, "recent": recent, "badges": badges}

@app.get("/reputation/{user_id}")
def user_reputation(user_id: str, uid: str = Depends(current_user)):
    with db() as c:
        if is_blocked(c, uid, user_id): raise HTTPException(403, "blocked")
        total = c.execute("SELECT COALESCE(reputation_score,0) FROM users WHERE id=?",
                          (user_id,)).fetchone()[0]
        by_kind = dict(c.execute("""SELECT kind, SUM(points) FROM reputation_events
                                    WHERE user_id=? GROUP BY kind""", (user_id,)).fetchall())
        badges = [r["badge"] for r in c.execute(
            "SELECT badge FROM user_badges WHERE user_id=?", (user_id,))]
        return {"user_id": user_id, "total": total, "by_kind": by_kind, "badges": badges}

# ----------------------------- CREATOR TOOLS -----------------------------
@app.get("/creator/stats")
def creator_stats(uid: str = Depends(current_user)):
    with db() as c:
        rows = c.execute("""SELECT * FROM content_stats WHERE owner_id=?
                            ORDER BY updated_at DESC LIMIT 100""", (uid,)).fetchall()
        totals = c.execute("""SELECT COALESCE(SUM(views),0) AS views,
                                     COALESCE(SUM(reactions_count),0) AS reactions,
                                     COALESCE(SUM(comments_count),0) AS comments,
                                     COALESCE(SUM(shares),0) AS shares
                              FROM content_stats WHERE owner_id=?""", (uid,)).fetchone()
        followers = c.execute("SELECT COUNT(*) FROM follows WHERE followee_id=?",
                              (uid,)).fetchone()[0]
        return {"totals": row_to_dict(totals), "items": [row_to_dict(r) for r in rows],
                "followers": followers}

@app.get("/creator/top")
def creator_top(limit: int = 10, uid: str = Depends(current_user)):
    limit = max(1, min(limit, 50))
    with db() as c:
        rows = c.execute("""SELECT * FROM content_stats WHERE owner_id=?
                            ORDER BY (views + reactions_count*3 + comments_count*4 + shares*2) DESC
                            LIMIT ?""", (uid, limit)).fetchall()
        return [row_to_dict(r) for r in rows]

# ----------------------------- EVENTS -----------------------------
@app.post("/events")
def create_event(data: EventIn, uid: str = Depends(current_user)):
    try:
        starts = datetime.fromisoformat(data.starts_at)
    except Exception:
        raise HTTPException(400, "invalid starts_at")
    ends = None
    if data.ends_at:
        try:
            ends = datetime.fromisoformat(data.ends_at)
        except Exception:
            raise HTTPException(400, "invalid ends_at")
        if ends <= starts:
            raise HTTPException(400, "ends_at must be after starts_at")
    eid = new_id()
    with db() as c:
        c.execute("""INSERT INTO events
                     (id,title,description,location,starts_at,ends_at,host_id,cover_url,created_at)
                     VALUES (?,?,?,?,?,?,?,?,?)""",
                  (eid, data.title, data.description or "", data.location or "",
                   starts.isoformat(), ends.isoformat() if ends else None,
                   uid, data.cover_url or "", now_iso()))
    return {"event_id": eid}

@app.get("/events/{event_id}")
def get_event(event_id: str):
    with db() as c:
        r = c.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        if not r: raise HTTPException(404, "event not found")
        d = row_to_dict(r)
        d["attendees"] = dict(c.execute(
            "SELECT status, COUNT(*) FROM event_attendees WHERE event_id=? GROUP BY status",
            (event_id,)).fetchall())
        return d

@app.post("/events/{event_id}/attend")
def attend_event(event_id: str, status_: str = Query("going", alias="status"),
                 uid: str = Depends(current_user)):
    if status_ not in ("going", "interested", "declined"):
        raise HTTPException(400, "invalid status")
    with db() as c:
        if not c.execute("SELECT 1 FROM events WHERE id=?", (event_id,)).fetchone():
            raise HTTPException(404, "event not found")
        c.execute("""INSERT OR REPLACE INTO event_attendees (event_id,user_id,status,created_at)
                     VALUES (?,?,?,?)""", (event_id, uid, status_, now_iso()))
    return {"ok": True}

@app.get("/events")
def list_events(limit: int = 20):
    limit = max(1, min(limit, 50))
    with db() as c:
        rows = c.execute("SELECT * FROM events WHERE starts_at > ? ORDER BY starts_at ASC LIMIT ?",
                         (now_iso(), limit)).fetchall()
        return [row_to_dict(r) for r in rows]

# ----------------------------- BLOCKS -----------------------------
@app.post("/blocks/{target_id}")
def block_user(target_id: str, uid: str = Depends(current_user)):
    if target_id == uid: raise HTTPException(400, "cannot block self")
    with db() as c:
        c.execute("""INSERT OR IGNORE INTO blocks (blocker_id,blocked_id,created_at)
                     VALUES (?,?,?)""", (uid, target_id, now_iso()))
        c.execute("""DELETE FROM friendships
                     WHERE (user_id=? AND friend_id=?) OR (user_id=? AND friend_id=?)""",
                  (uid, target_id, target_id, uid))
        c.execute("""UPDATE connection_requests SET status='cancelled', responded_at=?
                     WHERE status='pending'
                       AND ((sender_id=? AND receiver_id=?) OR (sender_id=? AND receiver_id=?))""",
                  (now_iso(), uid, target_id, target_id, uid))
        c.execute("""DELETE FROM follows
                     WHERE (follower_id=? AND followee_id=?) OR (follower_id=? AND followee_id=?)""",
                  (uid, target_id, target_id, uid))
    return {"ok": True}

@app.delete("/blocks/{target_id}")
def unblock_user(target_id: str, uid: str = Depends(current_user)):
    with db() as c:
        c.execute("DELETE FROM blocks WHERE blocker_id=? AND blocked_id=?",
                  (uid, target_id))
    return {"ok": True}

@app.get("/blocks")
def list_blocks(uid: str = Depends(current_user)):
    with db() as c:
        rows = c.execute("""SELECT u.id,u.username,u.display_name,u.avatar_url
                            FROM blocks b JOIN users u ON u.id=b.blocked_id
                            WHERE b.blocker_id=?""", (uid,)).fetchall()
        return [row_to_dict(r) for r in rows]

# ----------------------------- REPORTS / ADMIN -----------------------------
@app.post("/reports")
def create_report(data: ReportIn, uid: str = Depends(current_user)):
    rid = new_id()
    with db() as c:
        c.execute("""INSERT INTO reports
                     (id,reporter_id,target_kind,target_id,reason,description,status,created_at)
                     VALUES (?,?,?,?,?,?,?,?)""",
                  (rid, uid, data.target_kind, data.target_id,
                   data.reason, data.description or "", "open", now_iso()))
    return {"report_id": rid}

@app.get("/admin/reports")
def admin_list_reports(status_: Optional[str] = Query(None, alias="status"),
                       limit: int = 100, uid: str = Depends(require_admin)):
    limit = max(1, min(limit, 500))
    with db() as c:
        if status_:
            rows = c.execute("SELECT * FROM reports WHERE status=? ORDER BY created_at DESC LIMIT ?",
                             (status_, limit)).fetchall()
        else:
            rows = c.execute("SELECT * FROM reports ORDER BY created_at DESC LIMIT ?",
                             (limit,)).fetchall()
        return [row_to_dict(r) for r in rows]

@app.patch("/admin/reports/{report_id}")
def admin_resolve(report_id: str, status_: str = Query(..., alias="status"),
                  uid: str = Depends(require_admin)):
    if status_ not in ("open", "reviewing", "resolved", "rejected"):
        raise HTTPException(400, "invalid status")
    with db() as c:
        c.execute("UPDATE reports SET status=?, resolved_at=? WHERE id=?",
                  (status_, now_iso() if status_ in ("resolved", "rejected") else None,
                   report_id))
    return {"ok": True}

@app.post("/admin/users/{user_id}/status")
def admin_set_status(user_id: str, status_: str = Query(..., alias="status"),
                     uid: str = Depends(require_admin)):
    if status_ not in ("active", "suspended", "banned", "deleted"):
        raise HTTPException(400, "invalid status")
    with db() as c:
        c.execute("UPDATE users SET status=?, updated_at=? WHERE id=?",
                  (status_, now_iso(), user_id))
        if status_ != "active":
            c.execute("UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
                      (now_iso(), user_id))
    return {"ok": True}

# ----------------------------- COLLECTIONS -----------------------------
@app.post("/collections")
def create_collection(data: CollectionIn, uid: str = Depends(current_user)):
    if data.visibility not in ("private", "connections", "public"):
        raise HTTPException(400, "invalid visibility")
    cid = new_id()
    with db() as c:
        c.execute("""INSERT INTO collections (id,user_id,name,description,visibility,created_at)
                     VALUES (?,?,?,?,?,?)""",
                  (cid, uid, data.name, data.description or "", data.visibility, now_iso()))
    return {"collection_id": cid}

@app.get("/collections/me")
def my_collections(uid: str = Depends(current_user)):
    with db() as c:
        rows = c.execute("SELECT * FROM collections WHERE user_id=?", (uid,)).fetchall()
        out = []
        for r in rows:
            d = row_to_dict(r)
            d["item_count"] = c.execute(
                "SELECT COUNT(*) FROM collection_items WHERE collection_id=?",
                (r["id"],)).fetchone()[0]
            out.append(d)
        return out

@app.post("/collections/{collection_id}/items")
def add_collection_item(collection_id: str, data: CollectionItemIn,
                        uid: str = Depends(current_user)):
    with db() as c:
        r = c.execute("SELECT user_id FROM collections WHERE id=?",
                      (collection_id,)).fetchone()
        if not r or r["user_id"] != uid:
            raise HTTPException(404, "collection not found")
        c.execute("""INSERT OR IGNORE INTO collection_items
                     (collection_id,item_kind,item_id,added_at)
                     VALUES (?,?,?,?)""",
                  (collection_id, data.item_kind, data.item_id, now_iso()))
    return {"ok": True}

@app.get("/collections/{collection_id}/items")
def list_collection_items(collection_id: str, uid: str = Depends(current_user)):
    with db() as c:
        r = c.execute("SELECT user_id,visibility FROM collections WHERE id=?",
                      (collection_id,)).fetchone()
        if not r: raise HTTPException(404, "collection not found")
        if r["user_id"] != uid:
            if r["visibility"] == "private": raise HTTPException(403, "private")
            if r["visibility"] == "connections" and not are_connected(c, r["user_id"], uid):
                raise HTTPException(403, "connections only")
        rows = c.execute("""SELECT item_kind,item_id,added_at FROM collection_items
                            WHERE collection_id=? ORDER BY added_at DESC""",
                         (collection_id,)).fetchall()
        return [row_to_dict(r) for r in rows]

@app.delete("/collections/{collection_id}")
def delete_collection(collection_id: str, uid: str = Depends(current_user)):
    with db() as c:
        c.execute("DELETE FROM collections WHERE id=? AND user_id=?",
                  (collection_id, uid))
    return {"ok": True}

# ----------------------------- DISCOVER AGGREGATE -----------------------------
@app.get("/discover")
def discover_all(uid: str = Depends(current_user)):
    with db() as c:
        people = c.execute("""SELECT id,username,display_name,avatar_url,verified
                              FROM users WHERE status='active' AND id!=?
                              ORDER BY reputation_score DESC LIMIT 10""",
                           (uid,)).fetchall()
        spaces = c.execute("""SELECT id,slug,name,topic,privacy
                              FROM spaces WHERE privacy='public'
                              ORDER BY created_at DESC LIMIT 10""").fetchall()
        posts = c.execute("""SELECT id,post_type,body,created_at FROM posts
                             WHERE visibility='public' AND space_id IS NULL AND page_id IS NULL
                             ORDER BY created_at DESC LIMIT 10""").fetchall()
        pages = c.execute("""SELECT id,slug,name,category FROM pages
                             ORDER BY created_at DESC LIMIT 10""").fetchall()
        events = c.execute("""SELECT id,title,starts_at FROM events
                              WHERE starts_at > ? ORDER BY starts_at ASC LIMIT 10""",
                           (now_iso(),)).fetchall()
        topics = [r["interest"] for r in c.execute("""SELECT interest, COUNT(*) AS n
                                                      FROM user_interests
                                                      GROUP BY interest
                                                      ORDER BY n DESC LIMIT 20""").fetchall()]
        return {
            "people": [row_to_dict(r) for r in people],
            "spaces": [row_to_dict(r) for r in spaces],
            "posts": [row_to_dict(r) for r in posts],
            "pages": [row_to_dict(r) for r in pages],
            "events": [row_to_dict(r) for r in events],
            "topics": topics,
        }

# ----------------------------- HEALTH -----------------------------
@app.get("/health")
def health():
    with db() as c:
        c.execute("SELECT 1").fetchone()
        pending = c.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
    return {"status": "ok", "time": now_iso(), "version": "5.0.0",
            "migrations_applied": pending}

# ----------------------------- ENTRY -----------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("maxbook:app", host="0.0.0.0",
                port=int(os.environ.get("PORT", 8000)), reload=False)
