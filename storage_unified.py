"""Unified storage module — auto-selects SQLite (local) or PostgreSQL (Supabase/Cloud).

Usage in quiz_app.py:
    import storage_unified as storage

Environment variables:
    DATABASE_URL  — if set, uses PostgreSQL (storage_pg.py)
                    postgresql://user:pass@host:port/dbname
    STREAMLIT_DB_PATH — optional, SQLite path (default: quiz_progress.db)
"""

import os

# Check if PostgreSQL is configured
if os.environ.get("DATABASE_URL"):
    # Use PostgreSQL backend (Supabase, Neon, etc.)
    from storage_pg import *  # noqa: F403,F401
    _BACKEND = "postgresql"
else:
    # Use SQLite backend (local development)
    from storage import *  # noqa: F403,F401
    _BACKEND = "sqlite"


def get_backend():
    """Return the active backend name: 'sqlite' or 'postgresql'."""
    return _BACKEND