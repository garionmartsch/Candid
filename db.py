"""
Tiny sqlite3 data layer. No ORM dependency required (just the stdlib),
so the app runs with nothing but Flask + requests installed.

Anonymity design: the `feedback` table has NO column that references who
submitted anything, and never has, at any stage. A draft under review
lives only in the submitter's browser session (see app.py) and is never
written here. The only INSERT this module exposes for feedback
(`create_feedback_anonymous`) takes recipient/category/text/timing —
there is no parameter for a submitter, so it is structurally impossible
for a caller to accidentally persist that link. A full dump of this
database reveals who received what and when, and nothing about who sent
it.
"""

import os
import random
import sqlite3
from datetime import datetime, time as dt_time, timedelta, timezone

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DB_PATH = os.path.join(BASE_DIR, "instance", "feedback.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS user (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recipient_id INTEGER NOT NULL REFERENCES user(id),
    category TEXT NOT NULL DEFAULT 'suggestion',
    reworded_text TEXT NOT NULL,
    edited_by_submitter INTEGER NOT NULL DEFAULT 0,
    reword_source TEXT NOT NULL DEFAULT 'heuristic',
    queued_at TEXT NOT NULL,
    release_at TEXT NOT NULL,
    read_at TEXT
);
"""

CATEGORY_COMPLAINT = "complaint"
CATEGORY_SUGGESTION = "suggestion"

# --- delivery-timing config (see README: "Delivery timing protection") ---
# Approved feedback isn't delivered the moment it's approved. It's released
# at the next fixed daily window that is at least MIN_DELAY_HOURS away, plus
# a little jitter, so arrival time doesn't closely track submission time.
RELEASE_WINDOWS = [
    w.strip() for w in os.environ.get("FEEDBACK_RELEASE_WINDOWS", "09:00,21:00").split(",") if w.strip()
]
MIN_DELAY_HOURS = float(os.environ.get("FEEDBACK_MIN_DELAY_HOURS", "4"))
RELEASE_JITTER_MINUTES = float(os.environ.get("FEEDBACK_RELEASE_JITTER_MINUTES", "20"))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def now_dt() -> datetime:
    return datetime.now(timezone.utc)


def parse_dt(value):
    if not value:
        return None
    return datetime.fromisoformat(value)


def compute_release_at(now: datetime | None = None) -> str:
    """Next batch window at least MIN_DELAY_HOURS from now, plus jitter."""
    now = now or now_dt()
    threshold = now + timedelta(hours=MIN_DELAY_HOURS)
    windows = sorted(RELEASE_WINDOWS) or ["09:00", "21:00"]

    day = threshold.date()
    for _ in range(0, 400):  # generous cap; loops at most a few hundred days
        for w in windows:
            hour, minute = (int(p) for p in w.split(":"))
            candidate = datetime.combine(day, dt_time(hour, minute), tzinfo=timezone.utc)
            if candidate >= threshold:
                jitter = timedelta(minutes=random.uniform(0, RELEASE_JITTER_MINUTES))
                return (candidate + jitter).isoformat()
        day = day + timedelta(days=1)
    # Fallback (should be unreachable): just use the threshold itself.
    return threshold.isoformat()


def get_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_db()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


# --- users -----------------------------------------------------------

def create_user(name: str, email: str, password_hash: str):
    conn = get_db()
    try:
        cur = conn.execute(
            "INSERT INTO user (name, email, password_hash, created_at) VALUES (?, ?, ?, ?)",
            (name, email, password_hash, now_iso()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_user_by_email(email: str):
    conn = get_db()
    try:
        return conn.execute("SELECT * FROM user WHERE email = ?", (email,)).fetchone()
    finally:
        conn.close()


def get_user(user_id: int):
    conn = get_db()
    try:
        return conn.execute("SELECT * FROM user WHERE id = ?", (user_id,)).fetchone()
    finally:
        conn.close()


def list_other_users(exclude_user_id: int):
    conn = get_db()
    try:
        return conn.execute(
            "SELECT * FROM user WHERE id != ? ORDER BY name COLLATE NOCASE", (exclude_user_id,)
        ).fetchall()
    finally:
        conn.close()


# --- feedback ----------------------------------------------------------
# No function in this section accepts a submitter identity of any kind.

def create_feedback_anonymous(recipient_id, category, reworded_text, edited_by_submitter, reword_source):
    """The only way a feedback row is created. Note what's absent: no
    submitter_id, no submitter session id, no IP, nothing that traces back
    to whoever wrote it — that information simply never reaches this
    function."""
    release_at = compute_release_at()
    conn = get_db()
    try:
        cur = conn.execute(
            """INSERT INTO feedback
               (recipient_id, category, reworded_text, edited_by_submitter,
                reword_source, queued_at, release_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                recipient_id, category, reworded_text, int(edited_by_submitter),
                reword_source, now_iso(), release_at,
            ),
        )
        conn.commit()
        return cur.lastrowid, release_at
    finally:
        conn.close()


def get_feedback(feedback_id):
    conn = get_db()
    try:
        return conn.execute("SELECT * FROM feedback WHERE id = ?", (feedback_id,)).fetchone()
    finally:
        conn.close()


def mark_feedback_read(feedback_id):
    conn = get_db()
    try:
        conn.execute("UPDATE feedback SET read_at = ? WHERE id = ?", (now_iso(), feedback_id))
        conn.commit()
    finally:
        conn.close()


def list_received_feedback(recipient_id):
    """Recipient inbox: only items whose release_at has passed, and
    (as always) no column here could identify who sent any of them."""
    conn = get_db()
    try:
        return conn.execute(
            """SELECT id, category, reworded_text, release_at, read_at
               FROM feedback
               WHERE recipient_id = ? AND release_at <= ?
               ORDER BY release_at DESC""",
            (recipient_id, now_iso()),
        ).fetchall()
    finally:
        conn.close()
