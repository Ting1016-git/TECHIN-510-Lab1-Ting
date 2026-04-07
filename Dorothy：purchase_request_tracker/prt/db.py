from __future__ import annotations

import hashlib
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import get_db_path
from .statuses import (
    STATUS_APPROVED,
    STATUS_LOST,
    STATUS_PENDING,
    STATUS_PROCESSING,
    STATUS_REJECTED,
    STATUS_WITHDRAWN,
    compute_combined_status,
)

# Orders in these states commit team budget (excludes REJECTED and WITHDRAWN).
_BUDGET_COMMIT_STATUSES: tuple[str, ...] = (
    STATUS_APPROVED,
    STATUS_PENDING,
    STATUS_PROCESSING,
)


DB_MIGRATION_NOTE = "Schema created/updated automatically on startup."


ROLE_STUDENT_CFO = "Student (CFO)"
ROLE_INSTRUCTOR = "Instructor"
ROLE_ADMIN = "Admin"

VALID_USER_ROLES = frozenset({ROLE_STUDENT_CFO, ROLE_INSTRUCTOR, ROLE_ADMIN})

_DEFAULT_ADMIN_EMAIL = "admin@uw.edu"
_DEFAULT_ADMIN_PASSWORD = "admin123"


def hash_password(plain: str) -> str:
    return hashlib.sha256((plain or "").encode("utf-8")).hexdigest()


def _ensure_users_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);")


def _seed_default_admin_if_empty(conn: sqlite3.Connection) -> None:
    row = conn.execute("SELECT COUNT(*) AS c FROM users;").fetchone()
    if row and int(row["c"]) > 0:
        return
    conn.execute(
        """
        INSERT INTO users (full_name, email, password_hash, role, created_at)
        VALUES (?, ?, ?, ?, ?);
        """,
        (
            "Default Admin",
            _DEFAULT_ADMIN_EMAIL.lower(),
            hash_password(_DEFAULT_ADMIN_PASSWORD),
            ROLE_ADMIN,
            _utc_now_iso(),
        ),
    )


def create_user(full_name: str, email: str, password: str, role: str) -> int:
    fn = (full_name or "").strip()
    em = (email or "").strip().lower()
    pw = password or ""
    rl = (role or "").strip()
    if not fn:
        raise ValueError("Full name is required.")
    if not em:
        raise ValueError("Email is required.")
    if not pw:
        raise ValueError("Password is required.")
    if rl not in VALID_USER_ROLES:
        raise ValueError("Invalid role.")
    if rl == ROLE_ADMIN:
        raise ValueError("Admin accounts cannot be created through self-registration.")
    with db_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO users (full_name, email, password_hash, role, created_at)
            VALUES (?, ?, ?, ?, ?);
            """,
            (fn, em, hash_password(pw), rl, _utc_now_iso()),
        )
        return int(cur.lastrowid)


def get_user_by_email(email: str) -> dict[str, Any] | None:
    em = (email or "").strip().lower()
    if not em:
        return None
    with db_conn() as conn:
        row = conn.execute(
            """
            SELECT id, full_name, email, password_hash, role, created_at
            FROM users WHERE email = ?;
            """,
            (em,),
        ).fetchone()
        if not row:
            return None
        return {
            "id": int(row["id"]),
            "full_name": str(row["full_name"]),
            "email": str(row["email"]),
            "password_hash": str(row["password_hash"]),
            "role": str(row["role"]),
            "created_at": str(row["created_at"]),
        }


def authenticate_user(email: str, password: str) -> dict[str, Any] | None:
    row = get_user_by_email(email)
    if not row:
        return None
    if row["password_hash"] != hash_password(password or ""):
        return None
    return {
        "id": row["id"],
        "full_name": row["full_name"],
        "email": row["email"],
        "role": row["role"],
        "created_at": row["created_at"],
    }


def _apply_derived_fields_from_approvals(
    conn: sqlite3.Connection,
    order_id: int,
    *,
    touch_timestamps: bool = True,
) -> None:
    row = conn.execute(
        """
        SELECT instructor_status, admin_status, received_at, approved_at, rejected_at
        FROM orders WHERE id = ?;
        """,
        (order_id,),
    ).fetchone()
    if not row:
        return
    ri = row["instructor_status"]
    inst_for_combined = "" if ri is None else str(ri).strip()
    combined = compute_combined_status(inst_for_combined, str(row["admin_status"]))
    now = _utc_now_iso()
    received_at = row["received_at"]
    archived = 1 if combined == STATUS_REJECTED else (1 if received_at else 0)

    if touch_timestamps:
        conn.execute(
            """
            UPDATE orders
            SET status = ?,
                updated_at = ?,
                approved_at = CASE
                    WHEN ? = ? AND approved_at IS NULL THEN ?
                    ELSE approved_at
                END,
                rejected_at = CASE
                    WHEN ? = ? AND rejected_at IS NULL THEN ?
                    ELSE rejected_at
                END,
                archived = ?
            WHERE id = ?;
            """,
            (
                combined,
                now,
                combined,
                STATUS_APPROVED,
                now,
                combined,
                STATUS_REJECTED,
                now,
                archived,
                order_id,
            ),
        )
    else:
        conn.execute(
            """
            UPDATE orders
            SET status = ?,
                updated_at = ?,
                archived = ?
            WHERE id = ?;
            """,
            (combined, now, archived, order_id),
        )


PROJECT_TYPE_GROUP = "group"
PROJECT_TYPE_INDIVIDUAL = "individual"


def _migrate_class_budget_columns(conn: sqlite3.Connection) -> None:
    """Backfill budget_per_group and project_type for existing databases."""
    conn.execute(
        f"""
        UPDATE classes
        SET project_type = '{PROJECT_TYPE_GROUP}'
        WHERE project_type IS NULL OR TRIM(project_type) = '';
        """
    )
    rows = conn.execute("SELECT id, total_budget FROM classes;").fetchall()
    for r in rows:
        cid = int(r["id"])
        has_bpg = conn.execute(
            "SELECT budget_per_group FROM classes WHERE id = ?;",
            (cid,),
        ).fetchone()
        bpg = has_bpg["budget_per_group"] if has_bpg else None
        if bpg is not None and float(bpg) > 0:
            continue
        avg_row = conn.execute(
            """
            SELECT AVG(budget_total) AS a
            FROM teams
            WHERE class_id = ?;
            """,
            (cid,),
        ).fetchone()
        avg_b = avg_row["a"] if avg_row else None
        if avg_b is not None and float(avg_b) > 0:
            conn.execute(
                "UPDATE classes SET budget_per_group = ? WHERE id = ?;",
                (float(avg_b), cid),
            )
        elif r["total_budget"] is not None and float(r["total_budget"]) > 0:
            conn.execute(
                "UPDATE classes SET budget_per_group = ? WHERE id = ?;",
                (float(r["total_budget"]), cid),
            )
        else:
            conn.execute(
                "UPDATE classes SET budget_per_group = ? WHERE id = ? AND (budget_per_group IS NULL OR budget_per_group <= 0);",
                (500.0, cid),
            )


def _migrate_split_approval_columns(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS _schema_migrations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE
        );
        """
    )
    row = conn.execute(
        "SELECT 1 AS ok FROM _schema_migrations WHERE name = ?;",
        ("split_approval_v1",),
    ).fetchone()
    if row:
        return
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(orders);").fetchall()}
    if "instructor_status" not in cols or "admin_status" not in cols:
        return
    conn.execute(
        """
        UPDATE orders SET
            instructor_status = CASE
                WHEN status = ? THEN ?
                WHEN status = ? THEN ?
                ELSE ?
            END,
            admin_status = CASE
                WHEN status = ? THEN ?
                WHEN status = ? THEN ?
                ELSE ?
            END
        WHERE 1 = 1;
        """,
        (
            STATUS_PENDING,
            STATUS_PENDING,
            STATUS_REJECTED,
            STATUS_PENDING,
            STATUS_APPROVED,
            STATUS_APPROVED,
            STATUS_APPROVED,
            STATUS_REJECTED,
            STATUS_REJECTED,
            STATUS_PENDING,
        ),
    )
    rows = conn.execute("SELECT id FROM orders;").fetchall()
    for r in rows:
        _apply_derived_fields_from_approvals(conn, int(r["id"]), touch_timestamps=False)
    conn.execute(
        "INSERT INTO _schema_migrations (name) VALUES (?);",
        ("split_approval_v1",),
    )


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _local_now() -> datetime:
    return datetime.now().astimezone()


def next_monday_1300_local(after: datetime | None = None) -> datetime:
    """Next Monday 13:00 in the same local timezone as `after` (default: now)."""
    after = after or _local_now()
    tz = after.tzinfo
    monday = after.date() - timedelta(days=after.weekday())
    cand = datetime.combine(monday, time(13, 0), tzinfo=tz)
    if after <= cand:
        return cand
    return datetime.combine(monday + timedelta(days=7), time(13, 0), tzinfo=tz)


def _week_of_label(monday: date) -> str:
    return f"Week of {monday.strftime('%b')} {monday.day}"


def _dt_to_sqlite(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_local_now().tzinfo)
    return dt.astimezone().isoformat(timespec="seconds")


def _parse_dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _deadline_date_str_from_window_dt(deadline_dt_iso: str) -> str:
    return _parse_dt(deadline_dt_iso).date().isoformat()


def _seed_submission_windows_for_class(conn: sqlite3.Connection, class_id: int, count: int = 8) -> None:
    start = next_monday_1300_local()
    monday_date = start.date()
    for i in range(count):
        d = monday_date + timedelta(days=7 * i)
        dt = datetime.combine(d, time(13, 0), tzinfo=start.tzinfo)
        label = _week_of_label(d)
        dt_sql = _dt_to_sqlite(dt)
        conn.execute(
            """
            INSERT OR IGNORE INTO submission_windows (class_id, deadline_datetime, label, is_active)
            VALUES (?, ?, ?, 1);
            """,
            (class_id, dt_sql, label),
        )


def _migrate_submission_windows(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS _schema_migrations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE
        );
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS submission_windows (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            class_id INTEGER NOT NULL,
            deadline_datetime TEXT NOT NULL,
            label TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1,
            UNIQUE (class_id, deadline_datetime),
            FOREIGN KEY (class_id) REFERENCES classes(id) ON DELETE CASCADE
        );
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_submission_windows_class_deadline ON submission_windows(class_id, deadline_datetime);"
    )
    try:
        conn.execute("ALTER TABLE orders ADD COLUMN window_id INTEGER REFERENCES submission_windows(id) ON DELETE SET NULL;")
    except sqlite3.OperationalError:
        pass
    conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_window ON orders(window_id);")

    row = conn.execute(
        "SELECT 1 AS ok FROM _schema_migrations WHERE name = ?;",
        ("submission_windows_v1",),
    ).fetchone()
    if row:
        return
    classes = conn.execute("SELECT id FROM classes;").fetchall()
    for r in classes:
        cid = int(r["id"])
        n = int(
            conn.execute(
                "SELECT COUNT(*) AS c FROM submission_windows WHERE class_id = ?;",
                (cid,),
            ).fetchone()["c"]
        )
        if n == 0:
            _seed_submission_windows_for_class(conn, cid, 8)
    conn.execute(
        "INSERT INTO _schema_migrations (name) VALUES (?);",
        ("submission_windows_v1",),
    )


def _migrate_workday_lost_replacement_columns(conn: sqlite3.Connection) -> None:
    for col_sql in (
        "ALTER TABLE orders ADD COLUMN workday_verified INTEGER NOT NULL DEFAULT 0;",
        "ALTER TABLE orders ADD COLUMN workday_verified_at TEXT;",
        "ALTER TABLE orders ADD COLUMN workday_verified_by TEXT;",
        "ALTER TABLE orders ADD COLUMN lost_at TEXT;",
        "ALTER TABLE orders ADD COLUMN replacement_for_order_id INTEGER REFERENCES orders(id) ON DELETE SET NULL;",
    ):
        try:
            conn.execute(col_sql)
        except sqlite3.OperationalError:
            pass


def connect() -> sqlite3.Connection:
    db_path = Path(get_db_path())
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


@contextmanager
def db_conn():
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with db_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS classes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS teams (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                class_id INTEGER NOT NULL,
                team_number TEXT NOT NULL,
                cfo_name TEXT NOT NULL,
                budget_total REAL NOT NULL,
                UNIQUE (class_id, team_number),
                FOREIGN KEY (class_id) REFERENCES classes(id) ON DELETE CASCADE
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS providers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                class_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                UNIQUE (class_id, name),
                FOREIGN KEY (class_id) REFERENCES classes(id) ON DELETE CASCADE
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                class_id INTEGER NOT NULL,
                team_id INTEGER NOT NULL,
                team_number_snapshot TEXT NOT NULL,
                cfo_name_snapshot TEXT NOT NULL,
                provider_name_snapshot TEXT NOT NULL,
                item_name TEXT NOT NULL,
                quantity REAL NOT NULL,
                unit_price REAL NOT NULL,
                total_price REAL NOT NULL,
                purchase_link TEXT NOT NULL,
                notes TEXT NOT NULL,
                deadline TEXT NOT NULL, -- YYYY-MM-DD
                status TEXT NOT NULL, -- PENDING/PROCESSING/APPROVED/REJECTED
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                approved_at TEXT,
                rejected_at TEXT,
                received_at TEXT,
                return_flag INTEGER NOT NULL DEFAULT 0,
                return_reason TEXT,
                archived INTEGER NOT NULL DEFAULT 0, -- Never delete; archived=1 => historical
                FOREIGN KEY (class_id) REFERENCES classes(id) ON DELETE CASCADE,
                FOREIGN KEY (team_id) REFERENCES teams(id) ON DELETE CASCADE
            );
            """
        )

        # Helpful index for admin filtering/sorting.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_class_deadline ON orders(class_id, deadline);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_class_status ON orders(class_id, status);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_class_team ON orders(class_id, team_id);")

        # Optional column for richer submit-page tracker (older DB files).
        try:
            conn.execute("ALTER TABLE orders ADD COLUMN rejection_reason TEXT;")
        except sqlite3.OperationalError:
            pass

        try:
            conn.execute("ALTER TABLE classes ADD COLUMN total_budget REAL;")
        except sqlite3.OperationalError:
            pass

        try:
            conn.execute("ALTER TABLE classes ADD COLUMN budget_per_group REAL;")
        except sqlite3.OperationalError:
            pass

        try:
            conn.execute("ALTER TABLE classes ADD COLUMN project_type TEXT;")
        except sqlite3.OperationalError:
            pass

        _migrate_class_budget_columns(conn)

        for col_sql in (
            "ALTER TABLE orders ADD COLUMN instructor_status TEXT NOT NULL DEFAULT 'PENDING';",
            "ALTER TABLE orders ADD COLUMN instructor_rejection_reason TEXT;",
            "ALTER TABLE orders ADD COLUMN admin_status TEXT NOT NULL DEFAULT 'PENDING';",
        ):
            try:
                conn.execute(col_sql)
            except sqlite3.OperationalError:
                pass

        try:
            conn.execute("ALTER TABLE orders ADD COLUMN withdrawn_at TEXT;")
        except sqlite3.OperationalError:
            pass

        try:
            conn.execute("ALTER TABLE orders ADD COLUMN receipt_path TEXT;")
        except sqlite3.OperationalError:
            pass

        _migrate_split_approval_columns(conn)
        _migrate_submission_windows(conn)
        _migrate_workday_lost_replacement_columns(conn)

        _ensure_users_table(conn)
        _seed_default_admin_if_empty(conn)
        _migrate_email_settings(conn)

        # Seed demo data if empty.
        cur = conn.execute("SELECT COUNT(*) AS c FROM classes;")
        count = int(cur.fetchone()["c"])
        if count == 0:
            _seed_demo_data(conn)


def _seed_demo_data(conn: sqlite3.Connection) -> None:
    # Classes
    conn.execute(
        """
        INSERT INTO classes (name, budget_per_group, project_type)
        VALUES (?, ?, ?);
        """,
        ("Class 1", 1200.0, PROJECT_TYPE_GROUP),
    )
    conn.execute(
        """
        INSERT INTO classes (name, budget_per_group, project_type)
        VALUES (?, ?, ?);
        """,
        ("Class 2", 1500.0, PROJECT_TYPE_GROUP),
    )

    class1_id = int(conn.execute("SELECT id FROM classes WHERE name=?;", ("Class 1",)).fetchone()["id"])
    class2_id = int(conn.execute("SELECT id FROM classes WHERE name=?;", ("Class 2",)).fetchone()["id"])

    for cid in (class1_id, class2_id):
        n = int(
            conn.execute(
                "SELECT COUNT(*) AS c FROM submission_windows WHERE class_id = ?;",
                (cid,),
            ).fetchone()["c"]
        )
        if n == 0:
            _seed_submission_windows_for_class(conn, cid, 8)

    # Teams + budgets + CFO names
    teams = [
        (class1_id, "1", "CFO A", 1200.00),
        (class1_id, "2", "CFO B", 800.00),
        (class2_id, "1", "CFO C", 1500.00),
        (class2_id, "3", "CFO D", 900.00),
    ]
    for class_id, team_number, cfo_name, budget_total in teams:
        conn.execute(
            """
            INSERT INTO teams (class_id, team_number, cfo_name, budget_total)
            VALUES (?, ?, ?, ?);
            """,
            (class_id, team_number, cfo_name, float(budget_total)),
        )

    # Providers
    providers = [
        (class1_id, "Supplier X"),
        (class1_id, "Supplier Y"),
        (class2_id, "Supplier Z"),
        (class2_id, "Supplier W"),
    ]
    for class_id, name in providers:
        conn.execute(
            "INSERT INTO providers (class_id, name) VALUES (?, ?);",
            (class_id, name),
        )


def get_classes() -> list[dict[str, Any]]:
    with db_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, name, total_budget, budget_per_group, project_type
            FROM classes ORDER BY id ASC;
            """
        ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            tb = r["total_budget"]
            bpg = r["budget_per_group"]
            pt = r["project_type"]
            out.append(
                {
                    "id": int(r["id"]),
                    "name": r["name"],
                    "total_budget": float(tb) if tb is not None else None,
                    "budget_per_group": float(bpg) if bpg is not None else None,
                    "project_type": (str(pt).strip() if pt else None) or PROJECT_TYPE_GROUP,
                }
            )
        return out


def get_class_by_id(class_id: int) -> dict[str, Any] | None:
    with db_conn() as conn:
        row = conn.execute(
            """
            SELECT id, name, total_budget, budget_per_group, project_type
            FROM classes WHERE id = ?;
            """,
            (class_id,),
        ).fetchone()
        if not row:
            return None
        tb = row["total_budget"]
        bpg = row["budget_per_group"]
        pt = row["project_type"]
        return {
            "id": int(row["id"]),
            "name": row["name"],
            "total_budget": float(tb) if tb is not None else None,
            "budget_per_group": float(bpg) if bpg is not None else None,
            "project_type": (str(pt).strip() if pt else None) or PROJECT_TYPE_GROUP,
        }


def get_class_project_type(class_id: int) -> str:
    """Return normalized project_type for the class: group or individual."""
    meta = get_class_by_id(int(class_id))
    if not meta:
        return PROJECT_TYPE_GROUP
    pt = (meta.get("project_type") or "").strip().lower()
    if pt == PROJECT_TYPE_INDIVIDUAL:
        return PROJECT_TYPE_INDIVIDUAL
    return PROJECT_TYPE_GROUP


def list_submission_windows(class_id: int) -> list[dict[str, Any]]:
    with db_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, class_id, deadline_datetime, label, is_active
            FROM submission_windows
            WHERE class_id = ?
            ORDER BY deadline_datetime ASC;
            """,
            (int(class_id),),
        ).fetchall()
        return [
            {
                "id": int(r["id"]),
                "class_id": int(r["class_id"]),
                "deadline_datetime": str(r["deadline_datetime"]),
                "label": str(r["label"]),
                "is_active": bool(int(r["is_active"])),
            }
            for r in rows
        ]


def add_custom_submission_window(
    class_id: int,
    deadline_datetime: datetime,
    label: str | None = None,
) -> int:
    cid = int(class_id)
    if deadline_datetime.tzinfo is None:
        deadline_datetime = deadline_datetime.replace(tzinfo=_local_now().tzinfo)
    dt_sql = _dt_to_sqlite(deadline_datetime)
    d = deadline_datetime.date()
    lab = (label or "").strip() or _week_of_label(d - timedelta(days=d.weekday()))
    with db_conn() as conn:
        row = conn.execute("SELECT id FROM classes WHERE id = ?;", (cid,)).fetchone()
        if not row:
            raise ValueError("Class not found.")
        try:
            cur = conn.execute(
                """
                INSERT INTO submission_windows (class_id, deadline_datetime, label, is_active)
                VALUES (?, ?, ?, 1);
                """,
                (cid, dt_sql, lab),
            )
            return int(cur.lastrowid)
        except sqlite3.IntegrityError as e:
            raise ValueError("A submission window with this date and time already exists for this class.") from e


def set_submission_window_active(window_id: int, is_active: bool) -> None:
    wid = int(window_id)
    with db_conn() as conn:
        conn.execute(
            "UPDATE submission_windows SET is_active = ? WHERE id = ?;",
            (1 if is_active else 0, wid),
        )
        if conn.total_changes == 0:
            raise ValueError("Submission window not found.")


def get_student_submission_window_ui_state(class_id: int) -> dict[str, Any]:
    """
    For the student submit page: current open window (earliest active future deadline),
    countdown target, and whether the open window has passed (closed banner).
    """
    now = _local_now()
    now_iso = _dt_to_sqlite(now)
    with db_conn() as conn:
        row = conn.execute(
            """
            SELECT id, deadline_datetime, label, is_active
            FROM submission_windows
            WHERE class_id = ?
              AND is_active = 1
              AND deadline_datetime > ?
            ORDER BY deadline_datetime ASC
            LIMIT 1;
            """,
            (int(class_id), now_iso),
        ).fetchone()
    if row:
        ddl = str(row["deadline_datetime"])
        end = _parse_dt(ddl)
        return {
            "has_open_window": True,
            "window_id": int(row["id"]),
            "label": str(row["label"]),
            "deadline_datetime_iso": ddl,
            "deadline_end_local": end,
            "show_closed_banner": False,
        }
    next_m = next_monday_1300_local(now)
    return {
        "has_open_window": False,
        "window_id": None,
        "label": None,
        "deadline_datetime_iso": _dt_to_sqlite(next_m),
        "deadline_end_local": next_m,
        "show_closed_banner": True,
    }


def get_or_create_window_at_deadline(class_id: int, deadline_dt: datetime) -> tuple[int, str]:
    """Returns (window_id, deadline_date_str for orders.deadline). Inserts window row if missing."""
    cid = int(class_id)
    if deadline_dt.tzinfo is None:
        deadline_dt = deadline_dt.replace(tzinfo=_local_now().tzinfo)
    dt_sql = _dt_to_sqlite(deadline_dt)
    monday = deadline_dt.date() - timedelta(days=deadline_dt.weekday())
    label = _week_of_label(monday)
    date_str = deadline_dt.date().isoformat()
    with db_conn() as conn:
        ex = conn.execute(
            "SELECT id FROM submission_windows WHERE class_id = ? AND deadline_datetime = ?;",
            (cid, dt_sql),
        ).fetchone()
        if ex:
            return int(ex["id"]), date_str
        cur = conn.execute(
            """
            INSERT INTO submission_windows (class_id, deadline_datetime, label, is_active)
            VALUES (?, ?, ?, 1);
            """,
            (cid, dt_sql, label),
        )
        return int(cur.lastrowid), date_str


def resolve_window_for_order_submission(class_id: int) -> dict[str, Any]:
    """
    Chooses the submission window for a new order: current active open window, or next Monday 1PM.
    Returns window_id, orders.deadline date string, closed_message for student (if any).
    """
    ui = get_student_submission_window_ui_state(class_id)
    if ui["has_open_window"] and ui["window_id"] is not None:
        wid = int(ui["window_id"])
        ddl = ui["deadline_datetime_iso"]
        date_str = _deadline_date_str_from_window_dt(ddl)
        return {
            "window_id": wid,
            "deadline": date_str,
            "closed_message": None,
        }
    next_m = next_monday_1300_local()
    wid, date_str = get_or_create_window_at_deadline(class_id, next_m)
    msg = (
        "The current window is closed. Your request has been added to next week's purchasing list "
        f"(Monday {next_m.strftime('%b')} {next_m.day}, 1:00 PM)."
    )
    return {
        "window_id": wid,
        "deadline": date_str,
        "closed_message": msg,
    }


def delete_class_by_id(class_id: int) -> None:
    """Permanently removes a course and all related teams, providers, and orders (FK CASCADE)."""
    cid = int(class_id)
    with db_conn() as conn:
        row = conn.execute("SELECT id FROM classes WHERE id = ?;", (cid,)).fetchone()
        if not row:
            raise ValueError("Course not found.")
        conn.execute("DELETE FROM classes WHERE id = ?;", (cid,))


def create_class(name: str, budget_per_group: float, project_type: str) -> int:
    n = (name or "").strip()
    if not n:
        raise ValueError("Course name is required.")
    if budget_per_group is None or float(budget_per_group) <= 0:
        raise ValueError("Budget per group/student must be a number greater than 0.")
    pt = (project_type or "").strip().lower()
    if pt not in {PROJECT_TYPE_GROUP, PROJECT_TYPE_INDIVIDUAL}:
        raise ValueError("Project type must be group or individual.")
    with db_conn() as conn:
        try:
            cur = conn.execute(
                """
                INSERT INTO classes (name, budget_per_group, project_type)
                VALUES (?, ?, ?);
                """,
                (n, float(budget_per_group), pt),
            )
            new_id = int(cur.lastrowid)
            _seed_submission_windows_for_class(conn, new_id, 8)
            return new_id
        except sqlite3.IntegrityError as e:
            raise ValueError("A course with this name already exists.") from e


def get_cfo_names(class_id: int) -> list[str]:
    with db_conn() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT cfo_name
            FROM teams
            WHERE class_id = ?
            ORDER BY cfo_name ASC;
            """,
            (class_id,),
        ).fetchall()
        return [r["cfo_name"] for r in rows]


def get_team_numbers(class_id: int) -> list[str]:
    with db_conn() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT team_number
            FROM teams
            WHERE class_id = ?
            ORDER BY team_number ASC;
            """,
            (class_id,),
        ).fetchall()
        return [r["team_number"] for r in rows]


def get_team_numbers_from_orders(class_id: int) -> list[str]:
    """Distinct team numbers appearing on non-archived submitted orders for this class."""
    with db_conn() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT team_number_snapshot
            FROM orders
            WHERE class_id = ? AND archived = 0
            ORDER BY team_number_snapshot ASC;
            """,
            (class_id,),
        ).fetchall()
        return [r["team_number_snapshot"] for r in rows]


def get_providers(class_id: int) -> list[str]:
    with db_conn() as conn:
        rows = conn.execute(
            """
            SELECT name
            FROM providers
            WHERE class_id = ?
            ORDER BY name ASC;
            """,
            (class_id,),
        ).fetchall()
        return [r["name"] for r in rows]


def _ensure_provider(conn: sqlite3.Connection, class_id: int, name: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO providers (class_id, name) VALUES (?, ?);",
        (class_id, name.strip()),
    )


def _get_team_row(conn: sqlite3.Connection, class_id: int, team_number: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT id, class_id, team_number, cfo_name, budget_total
        FROM teams
        WHERE class_id = ? AND team_number = ?;
        """,
        (class_id, team_number),
    ).fetchone()


def _ensure_team_row_for_submission(
    conn: sqlite3.Connection,
    class_id: int,
    budget_key: str,
    cfo_name: str,
) -> sqlite3.Row:
    tn = (budget_key or "").strip()
    if not tn:
        raise ValueError("Team number or name is required for budget tracking.")
    team = _get_team_row(conn, class_id, tn)
    if team:
        return team
    cls = conn.execute(
        "SELECT budget_per_group FROM classes WHERE id = ?;",
        (class_id,),
    ).fetchone()
    if not cls:
        raise ValueError("Class not found.")
    bpg = cls["budget_per_group"]
    if bpg is None or float(bpg) <= 0:
        raise ValueError("This class has no per-group budget configured.")
    conn.execute(
        """
        INSERT INTO teams (class_id, team_number, cfo_name, budget_total)
        VALUES (?, ?, ?, ?);
        """,
        (class_id, tn, (cfo_name or "").strip(), float(bpg)),
    )
    team = _get_team_row(conn, class_id, tn)
    if not team:
        raise ValueError("Could not create budget entry for this team or student.")
    return team


def get_submission_budget_preview(class_id: int, budget_key: str) -> tuple[float, float, float] | None:
    """
    Returns (total_budget, used_budget, remaining) for the budget key (team number or student name).
    Used budget counts approved, pending, and processing orders (not withdrawn/rejected).
    If no team row exists yet, uses the class's budget_per_group with zero spend.
    Returns None if the key is empty or the class has no valid per-group budget.
    """
    key = (budget_key or "").strip()
    if not key:
        return None
    with db_conn() as conn:
        team = _get_team_row(conn, class_id, key)
        if team:
            total_budget = float(team["budget_total"])
            _ph = ",".join("?" * len(_BUDGET_COMMIT_STATUSES))
            used = conn.execute(
                f"""
                SELECT COALESCE(SUM(o.total_price), 0) AS used_amount
                FROM orders o
                WHERE o.class_id = ?
                  AND o.team_id = ?
                  AND o.status IN ({_ph});
                """,
                (class_id, int(team["id"]), *_BUDGET_COMMIT_STATUSES),
            ).fetchone()["used_amount"]
            used_amount = float(used or 0.0)
            remaining = total_budget - used_amount
            return total_budget, used_amount, remaining

        cls = conn.execute(
            "SELECT budget_per_group FROM classes WHERE id = ?;",
            (class_id,),
        ).fetchone()
        if not cls:
            return None
        bpg = cls["budget_per_group"]
        if bpg is None or float(bpg) <= 0:
            return None
        total_budget = float(bpg)
        return total_budget, 0.0, total_budget


def team_exists_for_class(class_id: int, team_number: str) -> bool:
    tn = (team_number or "").strip()
    if not tn:
        return False
    with db_conn() as conn:
        return _get_team_row(conn, class_id, tn) is not None


def list_teams_for_class(class_id: int) -> list[dict[str, Any]]:
    with db_conn() as conn:
        rows = conn.execute(
            """
            SELECT team_number, budget_total, cfo_name
            FROM teams
            WHERE class_id = ?
            ORDER BY team_number ASC;
            """,
            (class_id,),
        ).fetchall()
        return [
            {
                "team_number": r["team_number"],
                "budget_total": float(r["budget_total"]),
                "cfo_name": (r["cfo_name"] or "").strip(),
            }
            for r in rows
        ]


def upsert_team_budget(class_id: int, team_number: str, budget_total: float) -> None:
    tn = (team_number or "").strip()
    if not tn:
        raise ValueError("Team number is required.")
    if budget_total is None or float(budget_total) <= 0:
        raise ValueError("Budget must be a number greater than 0.")

    with db_conn() as conn:
        conn.execute(
            """
            INSERT INTO teams (class_id, team_number, cfo_name, budget_total)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(class_id, team_number) DO UPDATE SET
                budget_total = excluded.budget_total;
            """,
            (class_id, tn, "", float(budget_total)),
        )


def get_team_budget(class_id: int, team_number: str) -> tuple[float, float, float]:
    """
    Returns: (total_budget, used_budget, remaining_budget).
    Used budget sums approved, pending, and processing orders (excludes withdrawn and rejected).
    """

    with db_conn() as conn:
        team = _get_team_row(conn, class_id, (team_number or "").strip())
        if not team:
            raise ValueError("Unknown team for this class.")

        total_budget = float(team["budget_total"])
        _ph = ",".join("?" * len(_BUDGET_COMMIT_STATUSES))
        used = conn.execute(
            f"""
            SELECT
                COALESCE(SUM(o.total_price), 0) AS used_amount
            FROM orders o
            WHERE o.class_id = ?
              AND o.team_id = ?
              AND o.status IN ({_ph});
            """,
            (class_id, int(team["id"]), *_BUDGET_COMMIT_STATUSES),
        ).fetchone()["used_amount"]
        used_amount = float(used or 0.0)
        remaining = total_budget - used_amount
        return total_budget, used_amount, remaining


def get_budget_summary_by_team(class_id: int) -> list[dict[str, Any]]:
    _ph = ",".join("?" * len(_BUDGET_COMMIT_STATUSES))
    with db_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT
                t.team_number,
                t.budget_total,
                COALESCE(SUM(CASE WHEN o.status IN ({_ph}) THEN o.total_price ELSE 0 END), 0) AS used_amount,
                (t.budget_total - COALESCE(SUM(CASE WHEN o.status IN ({_ph}) THEN o.total_price ELSE 0 END), 0)) AS remaining_amount,
                COUNT(o.id) AS total_orders,
                SUM(CASE WHEN o.status = ? THEN 1 ELSE 0 END) AS pending_count
            FROM teams t
            LEFT JOIN orders o
              ON o.team_id = t.id
             AND o.class_id = t.class_id
            WHERE t.class_id = ?
            GROUP BY t.id
            ORDER BY t.team_number ASC;
            """,
            (*_BUDGET_COMMIT_STATUSES, *_BUDGET_COMMIT_STATUSES, STATUS_PENDING, class_id),
        ).fetchall()

        out: list[dict[str, Any]] = []
        for r in rows:
            out.append(
                {
                    "team_number": r["team_number"],
                    "budget_total": float(r["budget_total"]),
                    "used_amount": float(r["used_amount"]),
                    "remaining_amount": float(r["remaining_amount"]),
                    "total_orders": int(r["total_orders"] or 0),
                    "pending_count": int(r["pending_count"] or 0),
                }
            )
        return out


def list_orders(
    class_id: int,
    team_number: str | None = None,
    status: str | None = None,
    provider_name: str | None = None,
    deadline_start: str | None = None,
    deadline_end: str | None = None,
    window_label: str | None = None,
    exclude_withdrawn: bool = True,
) -> list[sqlite3.Row]:
    with db_conn() as conn:
        sql = """
            SELECT
                o.id,
                o.team_number_snapshot,
                o.cfo_name_snapshot,
                o.provider_name_snapshot,
                o.item_name,
                o.quantity,
                o.unit_price,
                o.total_price,
                o.purchase_link,
                o.notes,
                o.deadline,
                o.window_id,
                sw.label AS window_label,
                sw.deadline_datetime AS window_deadline_datetime,
                o.status,
                o.instructor_status,
                o.instructor_rejection_reason,
                o.admin_status,
                o.rejection_reason,
                o.created_at,
                o.approved_at,
                o.rejected_at,
                o.received_at,
                o.return_flag,
                o.return_reason,
                o.archived,
                o.withdrawn_at,
                o.receipt_path,
                o.workday_verified,
                o.workday_verified_at,
                o.workday_verified_by,
                o.lost_at,
                o.replacement_for_order_id
            FROM orders o
            LEFT JOIN submission_windows sw ON sw.id = o.window_id
            WHERE o.class_id = :class_id
        """
        params: dict[str, Any] = {"class_id": class_id}

        if exclude_withdrawn:
            sql += " AND o.status != :ex_wd"
            params["ex_wd"] = STATUS_WITHDRAWN

        if team_number and team_number != "ALL":
            sql += " AND o.team_number_snapshot = :team_number"
            params["team_number"] = team_number
        if status and status != "ALL":
            sql += " AND o.status = :status"
            params["status"] = status
        if provider_name and provider_name != "ALL":
            sql += " AND o.provider_name_snapshot = :provider_name"
            params["provider_name"] = provider_name
        if deadline_start:
            sql += " AND o.deadline >= :deadline_start"
            params["deadline_start"] = deadline_start
        if deadline_end:
            sql += " AND o.deadline <= :deadline_end"
            params["deadline_end"] = deadline_end
        if window_label and window_label != "ALL":
            sql += " AND sw.label = :window_label"
            params["window_label"] = window_label

        sql += " ORDER BY o.deadline ASC, o.created_at DESC;"
        return conn.execute(sql, params).fetchall()


def list_archived_orders(
    class_id: int | None = None,
    archived: bool = True,
) -> list[sqlite3.Row]:
    """
    Returns historical records (archived=1).
    """

    with db_conn() as conn:
        params: dict[str, Any] = {"archived": 1 if archived else 0}
        sql = """
            SELECT
                o.id,
                c.name AS class_name,
                o.team_number_snapshot,
                o.cfo_name_snapshot,
                o.provider_name_snapshot,
                o.item_name,
                o.quantity,
                o.unit_price,
                o.total_price,
                o.purchase_link,
                o.deadline,
                o.status,
                o.created_at,
                o.approved_at,
                o.rejected_at,
                o.received_at,
                o.return_flag,
                o.return_reason,
                o.archived
            FROM orders o
            JOIN classes c ON c.id = o.class_id
            WHERE o.archived = :archived
        """
        if class_id is not None:
            sql += " AND o.class_id = :class_id"
            params["class_id"] = class_id
        sql += " ORDER BY c.name ASC, o.deadline ASC, o.created_at DESC;"
        return conn.execute(sql, params).fetchall()


def get_order(order_id: int) -> sqlite3.Row | None:
    with db_conn() as conn:
        row = conn.execute("SELECT * FROM orders WHERE id = ?;", (order_id,)).fetchone()
        return row


def create_order(
    class_id: int,
    team_number: str,
    cfo_name: str,
    provider_name: str,
    item_name: str,
    quantity: float,
    unit_price: float,
    purchase_link: str,
    notes: str,
    deadline: str,
    window_id: int | None = None,
) -> int:
    """
    Creates an order in PENDING state.
    """

    with db_conn() as conn:
        cls = conn.execute(
            "SELECT project_type FROM classes WHERE id = ?;",
            (class_id,),
        ).fetchone()
        if not cls:
            raise ValueError("Class not found.")
        pt = (cls["project_type"] or PROJECT_TYPE_GROUP).strip().lower()
        if pt not in (PROJECT_TYPE_INDIVIDUAL, PROJECT_TYPE_GROUP):
            pt = PROJECT_TYPE_GROUP
        if pt == PROJECT_TYPE_INDIVIDUAL:
            budget_key = (cfo_name or "").strip()
        else:
            budget_key = (team_number or "").strip()
        team = _ensure_team_row_for_submission(conn, class_id, budget_key, cfo_name)

        _ensure_provider(conn, class_id, provider_name)

        total_price = float(quantity) * float(unit_price)

        _ph = ",".join("?" * len(_BUDGET_COMMIT_STATUSES))
        used_amount_row = conn.execute(
            f"""
            SELECT COALESCE(SUM(o.total_price), 0) AS used_amount
            FROM orders o
            WHERE o.class_id = ?
              AND o.team_id = ?
              AND o.status IN ({_ph});
            """,
            (class_id, int(team["id"]), *_BUDGET_COMMIT_STATUSES),
        ).fetchone()
        used_amount = float(used_amount_row["used_amount"] or 0.0)

        remaining = float(team["budget_total"]) - used_amount
        if total_price > remaining + 1e-9:
            raise ValueError(
                f"Insufficient remaining budget. Remaining is {remaining:.2f}, requested is {total_price:.2f}."
            )

        now = _utc_now_iso()
        cur = conn.execute(
            """
            INSERT INTO orders (
                class_id,
                team_id,
                team_number_snapshot,
                cfo_name_snapshot,
                provider_name_snapshot,
                item_name,
                quantity,
                unit_price,
                total_price,
                purchase_link,
                notes,
                deadline,
                window_id,
                status,
                created_at,
                updated_at,
                approved_at,
                rejected_at,
                received_at,
                return_flag,
                return_reason,
                archived,
                instructor_status,
                instructor_rejection_reason,
                admin_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                class_id,
                int(team["id"]),
                budget_key,
                cfo_name,
                provider_name,
                item_name,
                float(quantity),
                float(unit_price),
                total_price,
                purchase_link,
                notes,
                deadline,
                window_id,
                STATUS_PENDING,
                now,
                now,
                None,
                None,
                None,
                0,
                None,
                0,
                STATUS_PENDING,
                None,
                STATUS_PENDING,
            ),
        )
        return int(cur.lastrowid)


def create_orders_batch(
    class_id: int,
    team_number: str,
    cfo_name: str,
    items: list[dict[str, Any]],
    deadline: str,
    window_id: int | None = None,
    individual_budget_key: str | None = None,
) -> list[int]:
    """
    Creates one order row per item. Each item dict must include `provider_name` (resolved label).
    Validates that the sum of line totals fits the team's remaining budget (committed spend),
    then inserts all rows in one transaction.
    """

    if not items:
        raise ValueError("Add at least one item.")

    with db_conn() as conn:
        cls = conn.execute(
            "SELECT project_type FROM classes WHERE id = ?;",
            (class_id,),
        ).fetchone()
        if not cls:
            raise ValueError("Class not found.")
        pt = (cls["project_type"] or PROJECT_TYPE_GROUP).strip().lower()
        if pt not in (PROJECT_TYPE_INDIVIDUAL, PROJECT_TYPE_GROUP):
            pt = PROJECT_TYPE_GROUP
        if pt == PROJECT_TYPE_INDIVIDUAL:
            if individual_budget_key is not None:
                budget_key = (individual_budget_key or "").strip()
            else:
                budget_key = (cfo_name or "").strip()
        else:
            budget_key = (team_number or "").strip()
        team = _ensure_team_row_for_submission(conn, class_id, budget_key, cfo_name)

        line_totals: list[float] = []
        for it in items:
            q = float(it["quantity"])
            p = float(it["unit_price"])
            line_totals.append(q * p)
        batch_total = sum(line_totals)

        _ph = ",".join("?" * len(_BUDGET_COMMIT_STATUSES))
        used_amount_row = conn.execute(
            f"""
            SELECT COALESCE(SUM(o.total_price), 0) AS used_amount
            FROM orders o
            WHERE o.class_id = ?
              AND o.team_id = ?
              AND o.status IN ({_ph});
            """,
            (class_id, int(team["id"]), *_BUDGET_COMMIT_STATUSES),
        ).fetchone()
        used_amount = float(used_amount_row["used_amount"] or 0.0)
        remaining = float(team["budget_total"]) - used_amount
        if batch_total > remaining + 1e-9:
            raise ValueError(
                f"Insufficient remaining budget. Remaining is {remaining:.2f}, requested total is {batch_total:.2f}."
            )

        order_ids: list[int] = []
        now = _utc_now_iso()
        team_id = int(team["id"])
        for it, total_price in zip(items, line_totals):
            provider_name = str(it.get("provider_name") or "").strip()
            if not provider_name:
                raise ValueError("Each item must include a provider / supplier.")
            _ensure_provider(conn, class_id, provider_name)
            cur = conn.execute(
                """
                INSERT INTO orders (
                    class_id,
                    team_id,
                    team_number_snapshot,
                    cfo_name_snapshot,
                    provider_name_snapshot,
                    item_name,
                    quantity,
                    unit_price,
                    total_price,
                    purchase_link,
                    notes,
                    deadline,
                    window_id,
                    status,
                    created_at,
                    updated_at,
                    approved_at,
                    rejected_at,
                    received_at,
                    return_flag,
                    return_reason,
                    archived,
                    instructor_status,
                    instructor_rejection_reason,
                    admin_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    class_id,
                    team_id,
                    budget_key,
                    cfo_name.strip(),
                    provider_name,
                    str(it["item_name"]).strip(),
                    float(it["quantity"]),
                    float(it["unit_price"]),
                    float(total_price),
                    str(it["purchase_link"]).strip(),
                    str(it["notes"]).strip(),
                    deadline,
                    window_id,
                    STATUS_PENDING,
                    now,
                    now,
                    None,
                    None,
                    None,
                    0,
                    None,
                    0,
                    STATUS_PENDING,
                    None,
                    STATUS_PENDING,
                ),
            )
            order_ids.append(int(cur.lastrowid))
        return order_ids


def _order_eligible_for_student_withdraw_or_edit(row: sqlite3.Row) -> bool:
    """Pending pipeline before instructor/admin review; legacy orders (no instructor column) allowed."""
    if int(row["archived"] or 0):
        return False
    if str(row["status"] or "").strip() != STATUS_PENDING:
        return False
    adm = str(row["admin_status"] or "").strip()
    if adm != STATUS_PENDING:
        return False
    inst_raw = row["instructor_status"]
    legacy = inst_raw is None or (isinstance(inst_raw, str) and not str(inst_raw).strip())
    if legacy:
        return True
    return str(inst_raw).strip() == STATUS_PENDING


def order_eligible_for_student_withdraw_or_edit(row: sqlite3.Row) -> bool:
    """Same eligibility rules as set_order_withdrawn / update_order_details (for student UI)."""
    return _order_eligible_for_student_withdraw_or_edit(row)


def set_order_withdrawn(order_id: int) -> None:
    """Sets status to WITHDRAWN and records withdrawn_at. Only for pre-review pending orders."""
    with db_conn() as conn:
        row = conn.execute(
            """
            SELECT status, instructor_status, admin_status, archived
            FROM orders WHERE id = ?;
            """,
            (order_id,),
        ).fetchone()
        if not row:
            raise ValueError("Order not found.")
        if not _order_eligible_for_student_withdraw_or_edit(row):
            raise ValueError("This request can no longer be withdrawn.")
        now = _utc_now_iso()
        conn.execute(
            """
            UPDATE orders
            SET status = ?,
                withdrawn_at = ?,
                updated_at = ?
            WHERE id = ?;
            """,
            (STATUS_WITHDRAWN, now, now, order_id),
        )


def update_order_details(
    order_id: int,
    item_name: str,
    quantity: float,
    unit_price: float,
    purchase_link: str,
    notes: str,
    provider_name: str,
) -> None:
    """Updates line item fields for a pending order before instructor review; enforces team budget."""
    n_item = (item_name or "").strip()
    prov = (provider_name or "").strip()
    if not n_item:
        raise ValueError("Item name is required.")
    if not prov:
        raise ValueError("Provider / supplier is required.")

    q = float(quantity)
    up = float(unit_price)
    if q <= 0:
        raise ValueError("Quantity must be greater than 0.")
    if up < 0:
        raise ValueError("Unit price cannot be negative.")
    total_price = q * up
    pl = (purchase_link or "").strip()
    nt = (notes or "").strip()

    with db_conn() as conn:
        row = conn.execute(
            """
            SELECT class_id, team_id, status, instructor_status, admin_status, archived
            FROM orders WHERE id = ?;
            """,
            (order_id,),
        ).fetchone()
        if not row:
            raise ValueError("Order not found.")
        if not _order_eligible_for_student_withdraw_or_edit(row):
            raise ValueError("This request can no longer be edited.")

        class_id = int(row["class_id"])
        team_id = int(row["team_id"])

        _ph = ",".join("?" * len(_BUDGET_COMMIT_STATUSES))
        used_others = conn.execute(
            f"""
            SELECT COALESCE(SUM(o.total_price), 0) AS u
            FROM orders o
            WHERE o.class_id = ?
              AND o.team_id = ?
              AND o.id != ?
              AND o.status IN ({_ph});
            """,
            (class_id, team_id, order_id, *_BUDGET_COMMIT_STATUSES),
        ).fetchone()["u"]
        used_others_f = float(used_others or 0.0)

        team = conn.execute(
            "SELECT budget_total FROM teams WHERE id = ? AND class_id = ?;",
            (team_id, class_id),
        ).fetchone()
        if not team:
            raise ValueError("Team not found for this order.")
        budget_total = float(team["budget_total"])
        remaining = budget_total - used_others_f
        if total_price > remaining + 1e-9:
            raise ValueError(
                f"Insufficient remaining budget. Remaining is {remaining:.2f}, "
                f"edited line total is {total_price:.2f}."
            )

        _ensure_provider(conn, class_id, prov)
        now = _utc_now_iso()
        conn.execute(
            """
            UPDATE orders
            SET item_name = ?,
                quantity = ?,
                unit_price = ?,
                total_price = ?,
                purchase_link = ?,
                notes = ?,
                provider_name_snapshot = ?,
                updated_at = ?
            WHERE id = ?;
            """,
            (n_item, q, up, total_price, pl, nt, prov, now, order_id),
        )


def admin_update_order_details(
    order_id: int,
    item_name: str,
    quantity: float,
    unit_price: float,
    purchase_link: str,
    notes: str,
    provider_name: str,
) -> None:
    """
    Admin (Dorothy) may edit line items for any order that has not been marked received yet.
    Recalculates total_price and enforces team budget (same as student edit).
    """
    n_item = (item_name or "").strip()
    prov = (provider_name or "").strip()
    if not n_item:
        raise ValueError("Item name is required.")
    if not prov:
        raise ValueError("Provider / supplier is required.")

    q = float(quantity)
    up = float(unit_price)
    if q <= 0:
        raise ValueError("Quantity must be greater than 0.")
    if up < 0:
        raise ValueError("Unit price cannot be negative.")
    total_price = q * up
    pl = (purchase_link or "").strip()
    nt = (notes or "").strip()

    with db_conn() as conn:
        row = conn.execute(
            """
            SELECT class_id, team_id, status, archived, received_at
            FROM orders WHERE id = ?;
            """,
            (order_id,),
        ).fetchone()
        if not row:
            raise ValueError("Order not found.")
        if row["received_at"] is not None:
            raise ValueError("Cannot edit an order that has already been received.")
        if str(row["status"] or "").strip() == STATUS_WITHDRAWN:
            raise ValueError("Cannot edit a withdrawn order.")
        if str(row["status"] or "").strip() == STATUS_LOST:
            raise ValueError("Cannot edit a lost order.")

        class_id = int(row["class_id"])
        team_id = int(row["team_id"])

        _ph = ",".join("?" * len(_BUDGET_COMMIT_STATUSES))
        used_others = conn.execute(
            f"""
            SELECT COALESCE(SUM(o.total_price), 0) AS u
            FROM orders o
            WHERE o.class_id = ?
              AND o.team_id = ?
              AND o.id != ?
              AND o.status IN ({_ph});
            """,
            (class_id, team_id, order_id, *_BUDGET_COMMIT_STATUSES),
        ).fetchone()["u"]
        used_others_f = float(used_others or 0.0)

        team = conn.execute(
            "SELECT budget_total FROM teams WHERE id = ? AND class_id = ?;",
            (team_id, class_id),
        ).fetchone()
        if not team:
            raise ValueError("Team not found for this order.")
        budget_total = float(team["budget_total"])
        remaining = budget_total - used_others_f
        if total_price > remaining + 1e-9:
            raise ValueError(
                f"Insufficient remaining budget. Remaining is {remaining:.2f}, "
                f"edited line total is {total_price:.2f}."
            )

        _ensure_provider(conn, class_id, prov)
        now = _utc_now_iso()
        conn.execute(
            """
            UPDATE orders
            SET item_name = ?,
                quantity = ?,
                unit_price = ?,
                total_price = ?,
                purchase_link = ?,
                notes = ?,
                provider_name_snapshot = ?,
                updated_at = ?
            WHERE id = ?;
            """,
            (n_item, q, up, total_price, pl, nt, prov, now, order_id),
        )


def save_receipt_path(order_id: int, file_path: str) -> None:
    """Stores relative or absolute path string to the receipt file on disk."""
    fp = (file_path or "").strip()
    if not fp:
        raise ValueError("Receipt path is required.")
    with db_conn() as conn:
        n = conn.execute(
            "UPDATE orders SET receipt_path = ?, updated_at = ? WHERE id = ?;",
            (fp, _utc_now_iso(), order_id),
        ).rowcount
        if not n:
            raise ValueError("Order not found.")


def get_receipt_path(order_id: int) -> str | None:
    with db_conn() as conn:
        row = conn.execute(
            "SELECT receipt_path FROM orders WHERE id = ?;",
            (order_id,),
        ).fetchone()
        if not row:
            return None
        rp = row["receipt_path"]
        if rp is None:
            return None
        s = str(rp).strip()
        return s if s else None


def set_order_status(order_id: int, new_status: str, rejection_reason: str | None = None) -> None:
    """Admin approval step: updates `admin_status` and syncs derived `status`."""
    if new_status not in {STATUS_PENDING, STATUS_PROCESSING, STATUS_APPROVED, STATUS_REJECTED}:
        raise ValueError("Invalid status.")

    if new_status == STATUS_REJECTED:
        rr = (rejection_reason or "").strip()
        if not rr:
            raise ValueError("Rejection reason is required when rejecting.")

    with db_conn() as conn:
        row = conn.execute(
            "SELECT received_at, instructor_status, admin_status, status FROM orders WHERE id = ?;",
            (order_id,),
        ).fetchone()
        if not row:
            raise ValueError("Order not found.")
        if str(row["status"] or "").strip() == STATUS_WITHDRAWN:
            raise ValueError("This order was withdrawn.")
        if str(row["status"] or "").strip() == STATUS_LOST:
            raise ValueError("This order was marked as lost.")

        inst_raw = row["instructor_status"]
        instructor_is_legacy = inst_raw is None or (
            isinstance(inst_raw, str) and not str(inst_raw).strip()
        )
        if (
            new_status == STATUS_APPROVED
            and not instructor_is_legacy
            and str(inst_raw).strip() != STATUS_APPROVED
        ):
            raise ValueError(
                "This item is still waiting for instructor approval. "
                "Please ask the instructor to review it first."
            )

        if new_status == STATUS_APPROVED:
            admin_status_new = STATUS_APPROVED
        elif new_status == STATUS_REJECTED:
            admin_status_new = STATUS_REJECTED
        else:
            admin_status_new = STATUS_PENDING

        rr_val = (rejection_reason or "").strip() if new_status == STATUS_REJECTED else None
        now = _utc_now_iso()

        conn.execute(
            """
            UPDATE orders
            SET admin_status = ?,
                updated_at = ?,
                rejection_reason = CASE
                    WHEN ? = ? THEN ?
                    WHEN ? = ? THEN NULL
                    ELSE rejection_reason
                END
            WHERE id = ?;
            """,
            (
                admin_status_new,
                now,
                new_status,
                STATUS_REJECTED,
                rr_val,
                new_status,
                STATUS_APPROVED,
                order_id,
            ),
        )
        _apply_derived_fields_from_approvals(conn, order_id)


def set_instructor_order_status(
    order_id: int,
    new_status: str,
    rejection_reason: str | None = None,
) -> None:
    """Instructor approval step: updates `instructor_status` and syncs derived `status`."""
    if new_status not in {STATUS_APPROVED, STATUS_REJECTED}:
        raise ValueError("Instructor decision must be APPROVED or REJECTED.")

    if new_status == STATUS_REJECTED:
        rr = (rejection_reason or "").strip()
        if not rr:
            raise ValueError("Rejection reason is required when rejecting.")

    with db_conn() as conn:
        row = conn.execute(
            "SELECT instructor_status, status FROM orders WHERE id = ?;",
            (order_id,),
        ).fetchone()
        if not row:
            raise ValueError("Order not found.")
        if str(row["status"] or "").strip() == STATUS_WITHDRAWN:
            raise ValueError("This order was withdrawn.")
        if str(row["status"] or "").strip() == STATUS_LOST:
            raise ValueError("This order was marked as lost.")
        if str(row["instructor_status"]) != STATUS_PENDING:
            raise ValueError("Instructor has already acted on this order.")

        inst = STATUS_APPROVED if new_status == STATUS_APPROVED else STATUS_REJECTED
        ir = (rejection_reason or "").strip() if new_status == STATUS_REJECTED else None
        now = _utc_now_iso()
        conn.execute(
            """
            UPDATE orders
            SET instructor_status = ?,
                instructor_rejection_reason = ?,
                updated_at = ?
            WHERE id = ?;
            """,
            (inst, ir, now, order_id),
        )
        _apply_derived_fields_from_approvals(conn, order_id)


def mark_received(order_id: int, return_flag: bool, return_reason: str | None = None) -> None:
    with db_conn() as conn:
        now = _utc_now_iso()
        archived = 1
        conn.execute(
            """
            UPDATE orders
            SET received_at = ?,
                return_flag = ?,
                return_reason = ?,
                updated_at = ?,
                archived = ?
            WHERE id = ?;
            """,
            (now, 1 if return_flag else 0, return_reason, now, archived, order_id),
        )


def set_workday_verified(order_id: int, verified_by_name: str) -> None:
    name = (verified_by_name or "").strip()
    if not name:
        raise ValueError("Verifier name is required.")
    with db_conn() as conn:
        row = conn.execute(
            "SELECT received_at, workday_verified FROM orders WHERE id = ?;",
            (order_id,),
        ).fetchone()
        if not row:
            raise ValueError("Order not found.")
        if row["received_at"] is None:
            raise ValueError("Order is not marked as received yet.")
        if int(row["workday_verified"] or 0):
            raise ValueError("This order is already Workday verified.")
        now = _utc_now_iso()
        conn.execute(
            """
            UPDATE orders
            SET workday_verified = 1,
                workday_verified_at = ?,
                workday_verified_by = ?,
                updated_at = ?
            WHERE id = ?;
            """,
            (now, name, now, order_id),
        )


def _mark_order_lost_conn(conn: sqlite3.Connection, order_id: int) -> None:
    row = conn.execute(
        "SELECT status, received_at FROM orders WHERE id = ?;",
        (order_id,),
    ).fetchone()
    if not row:
        raise ValueError("Order not found.")
    st = str(row["status"] or "").strip()
    if st == STATUS_WITHDRAWN:
        raise ValueError("Cannot mark a withdrawn order as lost.")
    if st == STATUS_LOST:
        raise ValueError("Order is already marked as lost.")
    if st != STATUS_APPROVED:
        raise ValueError("Only approved orders that are not yet received can be marked as lost.")
    if row["received_at"] is not None:
        raise ValueError("Cannot mark a received order as lost.")
    now = _utc_now_iso()
    conn.execute(
        """
        UPDATE orders
        SET status = ?,
            lost_at = ?,
            updated_at = ?,
            archived = 0
        WHERE id = ?;
        """,
        (STATUS_LOST, now, now, order_id),
    )


def _create_replacement_order_conn(conn: sqlite3.Connection, lost_order_id: int) -> int:
    row = conn.execute("SELECT * FROM orders WHERE id = ?;", (lost_order_id,)).fetchone()
    if not row:
        raise ValueError("Order not found.")
    if str(row["status"] or "").strip() != STATUS_LOST:
        raise ValueError("Replacement can only be created for an order marked as lost.")

    class_id = int(row["class_id"])
    team_id = int(row["team_id"])
    total_price = float(row["total_price"])

    _ph = ",".join("?" * len(_BUDGET_COMMIT_STATUSES))
    used_amount_row = conn.execute(
        f"""
        SELECT COALESCE(SUM(o.total_price), 0) AS used_amount
        FROM orders o
        WHERE o.class_id = ?
          AND o.team_id = ?
          AND o.status IN ({_ph});
        """,
        (class_id, team_id, *_BUDGET_COMMIT_STATUSES),
    ).fetchone()
    used_amount = float(used_amount_row["used_amount"] or 0.0)

    team = conn.execute(
        "SELECT budget_total FROM teams WHERE id = ? AND class_id = ?;",
        (team_id, class_id),
    ).fetchone()
    if not team:
        raise ValueError("Team not found for this order.")
    remaining = float(team["budget_total"]) - used_amount
    if total_price > remaining + 1e-9:
        raise ValueError(
            f"Insufficient remaining budget. Remaining is {remaining:.2f}, replacement total is {total_price:.2f}."
        )

    orig_notes = str(row["notes"] or "").strip()
    rep_line = f"Replacement for lost order #{lost_order_id}"
    new_notes = f"{orig_notes}\n\n{rep_line}" if orig_notes else rep_line

    now = _utc_now_iso()
    cur = conn.execute(
        """
        INSERT INTO orders (
            class_id,
            team_id,
            team_number_snapshot,
            cfo_name_snapshot,
            provider_name_snapshot,
            item_name,
            quantity,
            unit_price,
            total_price,
            purchase_link,
            notes,
            deadline,
            window_id,
            status,
            created_at,
            updated_at,
            approved_at,
            rejected_at,
            received_at,
            return_flag,
            return_reason,
            archived,
            instructor_status,
            instructor_rejection_reason,
            admin_status,
            replacement_for_order_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """,
        (
            class_id,
            team_id,
            row["team_number_snapshot"],
            row["cfo_name_snapshot"],
            row["provider_name_snapshot"],
            row["item_name"],
            float(row["quantity"]),
            float(row["unit_price"]),
            total_price,
            row["purchase_link"],
            new_notes,
            row["deadline"],
            row["window_id"],
            STATUS_PENDING,
            now,
            now,
            None,
            None,
            None,
            0,
            None,
            0,
            STATUS_PENDING,
            None,
            STATUS_PENDING,
            lost_order_id,
        ),
    )
    return int(cur.lastrowid)


def mark_order_lost(order_id: int) -> None:
    with db_conn() as conn:
        _mark_order_lost_conn(conn, order_id)


def create_replacement_order(original_order_id: int) -> int:
    with db_conn() as conn:
        return _create_replacement_order_conn(conn, original_order_id)


def mark_lost_and_create_replacement(order_id: int) -> int:
    """Marks an approved, unreceived order as lost and creates a replacement line in one transaction."""
    with db_conn() as conn:
        _mark_order_lost_conn(conn, order_id)
        return _create_replacement_order_conn(conn, order_id)


def count_pending_workday_verification(class_id: int) -> int:
    with db_conn() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM orders
            WHERE class_id = ?
              AND received_at IS NOT NULL
              AND (workday_verified IS NULL OR workday_verified = 0);
            """,
            (class_id,),
        ).fetchone()
        return int(row["c"] or 0)


def export_orders_csv_rows(
    include_all_classes: bool,
    include_archived: bool,
    class_id: int | None,
    *,
    exclude_withdrawn: bool = True,
) -> list[dict[str, Any]]:
    with db_conn() as conn:
        where = []
        params: dict[str, Any] = {}
        if not include_all_classes:
            if class_id is None:
                raise ValueError("class_id required when include_all_classes is False.")
            where.append("o.class_id = :class_id")
            params["class_id"] = class_id
        if not include_archived:
            where.append("o.archived = 0")
        if exclude_withdrawn:
            where.append("o.status != :ex_wd")
            params["ex_wd"] = STATUS_WITHDRAWN

        sql_where = ""
        if where:
            sql_where = "WHERE " + " AND ".join(where)

        rows = conn.execute(
            f"""
            SELECT
                o.id AS order_id,
                c.name AS class_name,
                o.team_number_snapshot AS team_number,
                o.cfo_name_snapshot AS cfo_name,
                o.provider_name_snapshot AS provider_name,
                o.item_name,
                o.quantity,
                o.unit_price,
                o.total_price,
                o.purchase_link,
                o.notes,
                o.deadline,
                o.status,
                o.created_at,
                o.approved_at,
                o.rejected_at,
                o.received_at,
                o.return_flag,
                o.return_reason,
                o.archived
            FROM orders o
            JOIN classes c ON c.id = o.class_id
            {sql_where}
            ORDER BY c.name ASC, o.deadline ASC, o.created_at DESC;
            """,
            params,
        ).fetchall()
        return [dict(r) for r in rows]


def delete_sql_injection_safe_note() -> str:
    return DB_MIGRATION_NOTE


def _migrate_email_settings(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS email_settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            smtp_host TEXT NOT NULL DEFAULT 'smtp.gmail.com',
            smtp_port INTEGER NOT NULL DEFAULT 587,
            smtp_user TEXT NOT NULL DEFAULT '',
            smtp_password TEXT NOT NULL DEFAULT '',
            sender_name TEXT NOT NULL DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO email_settings (id, smtp_host, smtp_port)
        VALUES (1, 'smtp.gmail.com', 587);
        """
    )


def get_email_settings() -> dict[str, Any]:
    with db_conn() as conn:
        row = conn.execute("SELECT * FROM email_settings WHERE id = 1;").fetchone()
        if not row:
            return {
                "id": 1,
                "smtp_host": "smtp.gmail.com",
                "smtp_port": 587,
                "smtp_user": "",
                "smtp_password": "",
                "sender_name": "",
                "enabled": False,
            }
        return {
            "id": int(row["id"]),
            "smtp_host": str(row["smtp_host"] or "smtp.gmail.com"),
            "smtp_port": int(row["smtp_port"] or 587),
            "smtp_user": str(row["smtp_user"] or ""),
            "smtp_password": str(row["smtp_password"] or ""),
            "sender_name": str(row["sender_name"] or ""),
            "enabled": bool(int(row["enabled"] or 0)),
        }


def save_email_settings(
    smtp_user: str,
    sender_name: str,
    enabled: bool,
    *,
    smtp_password: str | None = None,
) -> None:
    su = (smtp_user or "").strip()
    sn = (sender_name or "").strip()
    with db_conn() as conn:
        if smtp_password is not None and str(smtp_password).strip() != "":
            conn.execute(
                """
                UPDATE email_settings
                SET smtp_user = ?, sender_name = ?, enabled = ?, smtp_password = ?
                WHERE id = 1;
                """,
                (su, sn, 1 if enabled else 0, str(smtp_password)),
            )
        else:
            conn.execute(
                """
                UPDATE email_settings
                SET smtp_user = ?, sender_name = ?, enabled = ?
                WHERE id = 1;
                """,
                (su, sn, 1 if enabled else 0),
            )


def get_first_admin_email() -> str | None:
    """Email of the lowest-id Admin user (e.g. Dorothy), for inbound notifications."""
    with db_conn() as conn:
        row = conn.execute(
            """
            SELECT email FROM users
            WHERE role = ?
            ORDER BY id ASC
            LIMIT 1;
            """,
            (ROLE_ADMIN,),
        ).fetchone()
        if not row:
            return None
        em = str(row["email"] or "").strip()
        return em or None


def get_user_email_by_full_name(full_name: str) -> str | None:
    """Match registered student/instructor by full name (trimmed, case-insensitive)."""
    fn = (full_name or "").strip()
    if not fn:
        return None
    with db_conn() as conn:
        row = conn.execute(
            """
            SELECT email FROM users
            WHERE LOWER(TRIM(full_name)) = LOWER(?)
            LIMIT 1;
            """,
            (fn,),
        ).fetchone()
        if not row:
            return None
        em = str(row["email"] or "").strip()
        return em or None


def get_submission_window_label(window_id: int | None) -> str | None:
    if window_id is None:
        return None
    with db_conn() as conn:
        row = conn.execute(
            "SELECT label, deadline_datetime FROM submission_windows WHERE id = ?;",
            (int(window_id),),
        ).fetchone()
        if not row:
            return None
        lab = str(row["label"] or "").strip()
        ddl = str(row["deadline_datetime"] or "").strip()
        if lab and ddl:
            return f"{lab} (deadline {ddl})"
        return lab or ddl or None

