"""Persistent storage for profiles, attempts, and per-question answers.

Uses SQLite so the app works fully offline and on a single machine. The
database path can be overridden with the STREAMLIT_DB_PATH environment
variable (e.g. a mounted volume on Streamlit Cloud). To move to a hosted
PostgreSQL (Supabase/Neon) later, replace the connection helper and the
SQL below with the equivalent psycopg2/sqlalchemy calls — the function
signatures used by quiz_app.py can stay the same.
"""

import os
import sqlite3
import uuid
from datetime import datetime, timezone

DB_PATH = os.environ.get("STREAMLIT_DB_PATH", "quiz_progress.db")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    """Create tables if they do not exist. Safe to call on every startup."""
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS profiles (
                id          TEXT PRIMARY KEY,
                name        TEXT NOT NULL UNIQUE,
                created_at  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS attempts (
                id               TEXT PRIMARY KEY,
                profile_id       TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
                source_pdf       TEXT NOT NULL,
                start_page       INTEGER,
                end_page         INTEGER,
                mode             TEXT NOT NULL,
                attempt_type     TEXT NOT NULL DEFAULT 'original',
                parent_attempt_id TEXT,
                question_count   INTEGER NOT NULL DEFAULT 0,
                status           TEXT NOT NULL DEFAULT 'in_progress',
                started_at       TEXT NOT NULL,
                completed_at     TEXT
            );

            CREATE TABLE IF NOT EXISTS attempt_questions (
                attempt_id   TEXT NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
                position     INTEGER NOT NULL,
                question_id  TEXT NOT NULL,
                PRIMARY KEY (attempt_id, position)
            );

            CREATE TABLE IF NOT EXISTS answers (
                id               TEXT PRIMARY KEY,
                attempt_id       TEXT NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
                question_id      TEXT NOT NULL,
                selected_answer  TEXT,
                correct_answer   TEXT NOT NULL,
                status           TEXT NOT NULL,
                answered_at      TEXT NOT NULL
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
        rows = conn.execute(
            "SELECT id, name, created_at FROM profiles ORDER BY name"
        ).fetchall()
    return [dict(r) for r in rows]


def get_or_create_profile(name):
    """Return the profile dict for `name`, creating it if needed."""
    name = name.strip()
    if not name:
        raise ValueError("Profile name cannot be empty.")
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, name, created_at FROM profiles WHERE name = ?", (name,)
        ).fetchone()
        if row:
            return dict(row)
        pid = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO profiles (id, name, created_at) VALUES (?, ?, ?)",
            (pid, name, _now()),
        )
        return {"id": pid, "name": name, "created_at": _now()}


# ---------------------------------------------------------------------------
# Attempts
# ---------------------------------------------------------------------------
def create_attempt(profile_id, source_pdf, mode, question_ids,
                   start_page=None, end_page=None,
                   attempt_type="original", parent_attempt_id=None):
    """Create a new attempt and store its ordered question list.

    `question_ids` is the ordered list of stable question identifiers for this
    attempt (already shuffled/truncated by the caller).
    """
    attempt_id = uuid.uuid4().hex
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO attempts
                (id, profile_id, source_pdf, start_page, end_page, mode,
                 attempt_type, parent_attempt_id, question_count, status,
                 started_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'in_progress', ?)
            """,
            (
                attempt_id, profile_id, source_pdf, start_page, end_page, mode,
                attempt_type, parent_attempt_id, len(question_ids), _now(),
            ),
        )
        conn.executemany(
            "INSERT INTO attempt_questions (attempt_id, position, question_id) "
            "VALUES (?, ?, ?)",
            [(attempt_id, pos, qid) for pos, qid in enumerate(question_ids)],
        )
    return attempt_id


def save_answer(attempt_id, question_id, selected_answer, correct_answer, status):
    """Upsert a single answer for a question within an attempt."""
    with _connect() as conn:
        existing = conn.execute(
            "SELECT id FROM answers WHERE attempt_id = ? AND question_id = ?",
            (attempt_id, question_id),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE answers SET selected_answer = ?, correct_answer = ?, "
                "status = ?, answered_at = ? WHERE id = ?",
                (selected_answer, correct_answer, status, _now(), existing["id"]),
            )
        else:
            conn.execute(
                "INSERT INTO answers (id, attempt_id, question_id, selected_answer, "
                "correct_answer, status, answered_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    uuid.uuid4().hex, attempt_id, question_id, selected_answer,
                    correct_answer, status, _now(),
                ),
            )


def complete_attempt(attempt_id):
    with _connect() as conn:
        conn.execute(
            "UPDATE attempts SET status = 'completed', completed_at = ? WHERE id = ?",
            (_now(), attempt_id),
        )


def get_attempt(attempt_id):
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM attempts WHERE id = ?", (attempt_id,)
        ).fetchone()
    return dict(row) if row else None


def get_attempt_question_ids(attempt_id):
    with _connect() as conn:
        rows = conn.execute(
            "SELECT question_id FROM attempt_questions "
            "WHERE attempt_id = ? ORDER BY position",
            (attempt_id,),
        ).fetchall()
    return [r["question_id"] for r in rows]


def get_attempt_answers(attempt_id):
    """Return {question_id: answer_dict} for an attempt."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM answers WHERE attempt_id = ?", (attempt_id,)
        ).fetchall()
    return {r["question_id"]: dict(r) for r in rows}


def list_unfinished_attempts(profile_id):
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM attempts WHERE profile_id = ? AND status = 'in_progress' "
            "ORDER BY started_at DESC",
            (profile_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def list_completed_attempts(profile_id, limit=50):
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM attempts WHERE profile_id = ? AND status = 'completed' "
            "ORDER BY completed_at DESC LIMIT ?",
            (profile_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Retry helpers
# ---------------------------------------------------------------------------
def get_wrong_question_ids(attempt_id):
    with _connect() as conn:
        rows = conn.execute(
            "SELECT question_id FROM answers WHERE attempt_id = ? AND status = 'Wrong' "
            "ORDER BY answered_at",
            (attempt_id,),
        ).fetchall()
    return [r["question_id"] for r in rows]


def get_skipped_question_ids(attempt_id):
    """Questions in the attempt that have no answer record."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT aq.question_id FROM attempt_questions aq "
            "LEFT JOIN answers a ON a.attempt_id = aq.attempt_id "
            "AND a.question_id = aq.question_id "
            "WHERE aq.attempt_id = ? AND a.id IS NULL "
            "ORDER BY aq.position",
            (attempt_id,),
        ).fetchall()
    return [r["question_id"] for r in rows]


def get_unmastered_question_ids(attempt_id):
    """Wrong + skipped, preserving original order."""
    wrong = set(get_wrong_question_ids(attempt_id))
    skipped = set(get_skipped_question_ids(attempt_id))
    target = wrong | skipped
    ordered = get_attempt_question_ids(attempt_id)
    return [qid for qid in ordered if qid in target]


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------
def get_profile_stats(profile_id):
    """Aggregate stats across completed attempts for a profile."""
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT
                COUNT(*) AS total_attempts,
                SUM(CASE WHEN mode = 'Practice Mode' THEN 1 ELSE 0 END) AS practice_attempts,
                SUM(CASE WHEN mode = 'Test Mode' THEN 1 ELSE 0 END) AS test_attempts
            FROM attempts
            WHERE profile_id = ? AND status = 'completed'
            """,
            (profile_id,),
        ).fetchone()
        ans = conn.execute(
            """
            SELECT a.status, COUNT(*) AS n
            FROM answers a
            JOIN attempts t ON t.id = a.attempt_id
            WHERE t.profile_id = ? AND t.status = 'completed'
            GROUP BY a.status
            """,
            (profile_id,),
        ).fetchall()
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