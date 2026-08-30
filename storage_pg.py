"""PostgreSQL storage backend for Supabase/Neon/any PostgreSQL.

Drop-in replacement for storage.py with the same function signatures.
Uses psycopg2 (synchronous) — works on Streamlit Cloud.

Environment variables required:
    DATABASE_URL  — postgresql://user:pass@host:port/dbname
                    (get this from Supabase Dashboard → Settings → Database)
"""

import os
import uuid
from datetime import datetime, timezone
from contextlib import contextmanager

import psycopg2
from psycopg2.extras import RealDictCursor

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL environment variable not set. "
        "On Streamlit Cloud, add it in Settings → Secrets."
    )


@contextmanager
def _connect():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _now():
    return datetime.now(timezone.utc).isoformat()


def init_db():
    """Create tables if they do not exist. Safe to call on every startup."""
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS profiles (
                    id          TEXT PRIMARY KEY,
                    name        TEXT NOT NULL UNIQUE,
                    created_at  TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS attempts (
                    id                   TEXT PRIMARY KEY,
                    profile_id           TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
                    source_pdf           TEXT NOT NULL,
                    start_page           INTEGER,
                    end_page             INTEGER,
                    mode                 TEXT NOT NULL,
                    attempt_type         TEXT NOT NULL DEFAULT 'original',
                    parent_attempt_id    TEXT,
                    question_count       INTEGER NOT NULL DEFAULT 0,
                    status               TEXT NOT NULL DEFAULT 'in_progress',
                    started_at           TEXT NOT NULL,
                    completed_at         TEXT
                );

                CREATE TABLE IF NOT EXISTS attempt_questions (
                    attempt_id   TEXT NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
                    position     INTEGER NOT NULL,
                    question_id  TEXT NOT NULL,
                    PRIMARY KEY (attempt_id, position)
                );

                CREATE TABLE IF NOT EXISTS answers (
                    id                TEXT PRIMARY KEY,
                    attempt_id        TEXT NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
                    question_id       TEXT NOT NULL,
                    selected_answer   TEXT,
                    correct_answer    TEXT NOT NULL,
                    status            TEXT NOT NULL,
                    answered_at       TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_attempts_profile
                    ON attempts(profile_id, started_at);
                CREATE INDEX IF NOT EXISTS idx_answers_attempt
                    ON answers(attempt_id);
                """
            )


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------
def list_profiles():
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, name, created_at FROM profiles ORDER BY name"
            )
            return [dict(r) for r in cur.fetchall()]


def get_or_create_profile(name):
    name = name.strip()
    if not name:
        raise ValueError("Profile name cannot be empty.")
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, name, created_at FROM profiles WHERE name = %s",
                (name,),
            )
            row = cur.fetchone()
            if row:
                return dict(row)
            pid = uuid.uuid4().hex
            cur.execute(
                "INSERT INTO profiles (id, name, created_at) VALUES (%s, %s, %s)",
                (pid, name, _now()),
            )
            return {"id": pid, "name": name, "created_at": _now()}


# ---------------------------------------------------------------------------
# Attempts
# ---------------------------------------------------------------------------
def create_attempt(profile_id, source_pdf, mode, question_ids,
                   start_page=None, end_page=None,
                   attempt_type="original", parent_attempt_id=None):
    attempt_id = uuid.uuid4().hex
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO attempts
                    (id, profile_id, source_pdf, start_page, end_page, mode,
                     attempt_type, parent_attempt_id, question_count, status,
                     started_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'in_progress', %s)
                """,
                (
                    attempt_id, profile_id, source_pdf, start_page, end_page, mode,
                    attempt_type, parent_attempt_id, len(question_ids), _now(),
                ),
            )
            cur.executemany(
                "INSERT INTO attempt_questions (attempt_id, position, question_id) "
                "VALUES (%s, %s, %s)",
                [(attempt_id, pos, qid) for pos, qid in enumerate(question_ids)],
            )
    return attempt_id


def save_answer(attempt_id, question_id, selected_answer, correct_answer, status):
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM answers WHERE attempt_id = %s AND question_id = %s",
                (attempt_id, question_id),
            )
            existing = cur.fetchone()
            if existing:
                cur.execute(
                    "UPDATE answers SET selected_answer = %s, correct_answer = %s, "
                    "status = %s, answered_at = %s WHERE id = %s",
                    (selected_answer, correct_answer, status, _now(), existing["id"]),
                )
            else:
                cur.execute(
                    "INSERT INTO answers (id, attempt_id, question_id, selected_answer, "
                    "correct_answer, status, answered_at) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (
                        uuid.uuid4().hex, attempt_id, question_id, selected_answer,
                        correct_answer, status, _now(),
                    ),
                )


def complete_attempt(attempt_id):
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE attempts SET status = 'completed', completed_at = %s WHERE id = %s",
                (_now(), attempt_id),
            )


def get_attempt(attempt_id):
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM attempts WHERE id = %s", (attempt_id,))
            row = cur.fetchone()
    return dict(row) if row else None


def get_attempt_question_ids(attempt_id):
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT question_id FROM attempt_questions "
                "WHERE attempt_id = %s ORDER BY position",
                (attempt_id,),
            )
            return [r["question_id"] for r in cur.fetchall()]


def get_attempt_answers(attempt_id):
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM answers WHERE attempt_id = %s", (attempt_id,))
            return {r["question_id"]: dict(r) for r in cur.fetchall()}


def list_unfinished_attempts(profile_id):
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM attempts WHERE profile_id = %s AND status = 'in_progress' "
                "ORDER BY started_at DESC",
                (profile_id,),
            )
            return [dict(r) for r in cur.fetchall()]


def list_completed_attempts(profile_id, limit=50):
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM attempts WHERE profile_id = %s AND status = 'completed' "
                "ORDER BY completed_at DESC LIMIT %s",
                (profile_id, limit),
            )
            return [dict(r) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Retry helpers
# ---------------------------------------------------------------------------
def get_wrong_question_ids(attempt_id):
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT question_id FROM answers WHERE attempt_id = %s AND status = 'Wrong' "
                "ORDER BY answered_at",
                (attempt_id,),
            )
            return [r["question_id"] for r in cur.fetchall()]


def get_skipped_question_ids(attempt_id):
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT aq.question_id FROM attempt_questions aq "
                "LEFT JOIN answers a ON a.attempt_id = aq.attempt_id "
                "AND a.question_id = aq.question_id "
                "WHERE aq.attempt_id = %s AND a.id IS NULL "
                "ORDER BY aq.position",
                (attempt_id,),
            )
            return [r["question_id"] for r in cur.fetchall()]


def get_unmastered_question_ids(attempt_id):
    wrong = set(get_wrong_question_ids(attempt_id))
    skipped = set(get_skipped_question_ids(attempt_id))
    target = wrong | skipped
    ordered = get_attempt_question_ids(attempt_id)
    return [qid for qid in ordered if qid in target]


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------
def get_profile_stats(profile_id):
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    COUNT(*) AS total_attempts,
                    SUM(CASE WHEN mode = 'Practice Mode' THEN 1 ELSE 0 END) AS practice_attempts,
                    SUM(CASE WHEN mode = 'Test Mode' THEN 1 ELSE 0 END) AS test_attempts
                FROM attempts
                WHERE profile_id = %s AND status = 'completed'
                """,
                (profile_id,),
            )
            row = cur.fetchone()
            cur.execute(
                """
                SELECT a.status, COUNT(*) AS n
                FROM answers a
                JOIN attempts t ON t.id = a.attempt_id
                WHERE t.profile_id = %s AND t.status = 'completed'
                GROUP BY a.status
                """,
                (profile_id,),
            )
            ans = cur.fetchall()
    stats = dict(row)
    stats["correct"] = 0
    stats["wrong"] = 0
    for r in ans:
        if r["status"] == "Correct":
            stats["correct"] = r["n"]
        elif r["status"] == "Wrong":
            stats["wrong"] = r["n"]
    attempted = stats["correct"] + stats["wrong"]
    stats["attempted"] = attempted
    stats["accuracy"] = (stats["correct"] / attempted * 100) if attempted else 0.0
    return stats