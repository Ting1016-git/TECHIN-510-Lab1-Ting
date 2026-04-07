from __future__ import annotations

import os
from pathlib import Path


APP_NAME = "Purchase Request Tracker"


def get_project_root() -> Path:
    """Repository / app root (parent of the `prt` package)."""
    return Path(__file__).resolve().parent.parent


def get_receipts_dir() -> Path:
    """Ensures `prt_data/receipts` exists and returns it."""
    d = get_project_root() / "prt_data" / "receipts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_db_path() -> str:
    """
    DB is stored locally inside the app folder so multiple projects don't collide.
    Override via PRTR_DB_PATH if needed.
    """

    env = os.getenv("PRTR_DB_PATH")
    if env:
        return env

    return str(get_project_root() / "prt_data" / "purchase_requests.sqlite3")

