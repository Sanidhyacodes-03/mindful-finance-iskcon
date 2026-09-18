import os
import re
import sqlite3
import weakref
from datetime import datetime, timedelta

import psycopg2
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool

from config import DATABASE_URL, IS_POSTGRES, PG_POOL_MINCONN, PG_POOL_MAXCONN
from services.security import hash_password

# DATA_DIR points this at a mounted persistent disk in production (e.g. Render).
# Unset locally, so DB_PATH resolves to the project root exactly as before.
# Only used on the SQLite path — irrelevant once DATABASE_URL is set.
_DATA_DIR = os.environ.get("DATA_DIR") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(_DATA_DIR, "spendly.db")


# ------------------------------------------------------------------ #
# Postgres compatibility layer
# ------------------------------------------------------------------ #
# Everything below exists so app.py / report_service.py / scheduler_service.py
# don't need to change at all: they keep writing "?" placeholders, keep reading
# cursor.lastrowid after an INSERT, and keep accessing rows as row['col'] /
# row[0] exactly as they do against sqlite3.Row today.

_INSERT_RE = re.compile(r'^\s*INSERT\s+INTO', re.IGNORECASE)
_RETURNING_RE = re.compile(r'\bRETURNING\b', re.IGNORECASE)
_pg_pool = None


class _PGCursorWrapper:
    """Makes a psycopg2 DictCursor behave like sqlite3's cursor for this app's
    usage: '?' placeholders and a working .lastrowid after INSERTs."""

    def __init__(self, real_cursor):
        self._cursor = real_cursor
        self.lastrowid = None

    def execute(self, query, params=None):
        translated = query.replace('?', '%s')  # safe: no query text contains a literal '?'
        if _INSERT_RE.match(translated) and not _RETURNING_RE.search(translated):
            translated = translated.rstrip().rstrip(';') + ' RETURNING id'
            self._cursor.execute(translated, params or ())
            row = self._cursor.fetchone()
            # Idempotent seeding (ON CONFLICT DO NOTHING) legitimately returns 0 rows
            # on every run after the first — this is steady-state, not an edge case.
            self.lastrowid = row['id'] if row else None
        else:
            self._cursor.execute(translated, params or ())
        return self._cursor

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class _PGConnectionWrapper:
    """Drop-in replacement for the sqlite3 connection object this app already
    uses: .cursor() / .commit() / .rollback() / .close()."""

    def __init__(self, pool):
        self._pool = pool
        self._conn = pool.getconn()
        self._closed = False
        # Returns the connection to the pool even if a route raises before reaching
        # its db.close() call — none of app.py's ~30 get_db() call sites use
        # try/finally. weakref.finalize fires as soon as this wrapper is garbage
        # collected (effectively immediately under CPython refcounting once the
        # caller's local `db` variable goes out of scope), so this isn't relying
        # on an unpredictable GC cycle.
        self._finalizer = weakref.finalize(self, pool.putconn, self._conn)

    def cursor(self):
        real_cursor = self._conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        return _PGCursorWrapper(real_cursor)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        if not self._closed:
            self._closed = True
            self._finalizer()


def _get_pg_pool():
    global _pg_pool
    if _pg_pool is None:
        _pg_pool = ThreadedConnectionPool(minconn=PG_POOL_MINCONN, maxconn=PG_POOL_MAXCONN, dsn=DATABASE_URL)
    return _pg_pool


def month_filter_sql(column_expr):
    """Dialect-aware month filter — both compare against '=  ?' with a 'YYYY-MM'
    value, against the existing 'YYYY-MM-DD' TEXT date columns."""
    if IS_POSTGRES:
        return f"LEFT({column_expr}, 7)"
    return f"strftime('%Y-%m', {column_expr})"


def get_db():
    if IS_POSTGRES:
        return _PGConnectionWrapper(_get_pg_pool())
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db():
    conn = get_db()
    cursor = conn.cursor()

    if IS_POSTGRES:
        # Schema is created once via supabase_schema.sql in the Supabase SQL Editor,
        # not auto-created here — see that file for the full CREATE TABLE statements.
        print("Postgres detected (DATABASE_URL set). Skipping schema auto-creation — "
              "run supabase_schema.sql in the Supabase SQL Editor once, if you haven't already.")
        for role_name in ("admin", "auditor", "manager"):
            cursor.execute("INSERT INTO roles (name) VALUES (?) ON CONFLICT (name) DO NOTHING", (role_name,))
        conn.commit()
        conn.close()
        return

    # Roles Table — a real normalized table (not just an enum column) so role
    # metadata/permissions can grow without a schema migration later.
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS roles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL
    );
    """)

    # Users Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        email TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        role_id INTEGER NOT NULL,
        is_active INTEGER NOT NULL DEFAULT 1,
        phone_number TEXT,
        whatsapp_notify_mode TEXT DEFAULT 'large_only',
        reset_token TEXT,
        reset_token_expiry TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(role_id) REFERENCES roles(id)
    );
    """)

    # Departments Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS departments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        description TEXT,
        icon TEXT DEFAULT '🏛️',
        color TEXT DEFAULT '#6366f1',
        is_active INTEGER NOT NULL DEFAULT 1,
        created_by INTEGER,
        manager_id INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(created_by) REFERENCES users(id) ON DELETE SET NULL,
        FOREIGN KEY(manager_id) REFERENCES users(id) ON DELETE SET NULL
    );
    """)

    # Expenses Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS expenses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        department_id INTEGER NOT NULL,
        amount REAL NOT NULL,
        description TEXT NOT NULL,
        date TEXT NOT NULL,
        payment_method TEXT DEFAULT 'Cash',
        receipt_path TEXT,
        added_by INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(department_id) REFERENCES departments(id),
        FOREIGN KEY(added_by) REFERENCES users(id) ON DELETE SET NULL
    );
    """)

    # Budgets Table (one monthly limit per department)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS budgets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        department_id INTEGER NOT NULL UNIQUE,
        monthly_limit REAL NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(department_id) REFERENCES departments(id) ON DELETE CASCADE
    );
    """)

    # Alerts / Queries Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        type TEXT NOT NULL DEFAULT 'alert' CHECK(type IN ('alert', 'query')),
        department_id INTEGER,
        expense_id INTEGER,
        title TEXT NOT NULL,
        message TEXT NOT NULL,
        severity TEXT DEFAULT 'info' CHECK(severity IN ('info', 'warning', 'critical')),
        is_resolved INTEGER NOT NULL DEFAULT 0,
        created_by INTEGER,
        resolved_by INTEGER,
        resolved_at TIMESTAMP,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(department_id) REFERENCES departments(id) ON DELETE SET NULL,
        FOREIGN KEY(expense_id) REFERENCES expenses(id) ON DELETE SET NULL,
        FOREIGN KEY(created_by) REFERENCES users(id) ON DELETE SET NULL,
        FOREIGN KEY(resolved_by) REFERENCES users(id) ON DELETE SET NULL
    );
    """)

    # Audit Logs Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS audit_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        action TEXT NOT NULL,
        entity_type TEXT NOT NULL,
        entity_id INTEGER,
        details TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE SET NULL
    );
    """)

    # Fixed set of roles — idempotent, safe to run on every startup.
    for role_name in ("admin", "auditor", "manager"):
        cursor.execute("INSERT OR IGNORE INTO roles (name) VALUES (?)", (role_name,))

    conn.commit()
    conn.close()


def log_activity(cursor, user_id, action, entity_type, entity_id=None, details=None):
    """Record an admin mutation for the Auditor's Audit Log view."""
    cursor.execute(
        "INSERT INTO audit_logs (user_id, action, entity_type, entity_id, details) VALUES (?, ?, ?, ?, ?)",
        (user_id, action, entity_type, entity_id, details)
    )


def seed_db():
    conn = get_db()
    cursor = conn.cursor()

    # Only seed once — if the admin account already exists, assume the DB is populated.
    cursor.execute("SELECT id FROM users WHERE email = ?", ("admin@iskconshirpur.org",))
    if cursor.fetchone():
        conn.close()
        return

    admin_pass = hash_password("Admin@123")
    auditor_pass = hash_password("Auditor@123")
    manager_pass = hash_password("Manager@123")

    cursor.execute(
        "INSERT INTO users (name, email, password_hash, role_id, phone_number) VALUES (?, ?, ?, (SELECT id FROM roles WHERE name = 'admin'), ?)",
        ("Radha Das", "admin@iskconshirpur.org", admin_pass, "+919876500001")
    )
    admin_id = cursor.lastrowid

    cursor.execute(
        "INSERT INTO users (name, email, password_hash, role_id, phone_number) VALUES (?, ?, ?, (SELECT id FROM roles WHERE name = 'auditor'), ?)",
        ("Govind Sharma", "auditor@iskconshirpur.org", auditor_pass, "+919876500002")
    )
    auditor_id = cursor.lastrowid

    cursor.execute(
        "INSERT INTO users (name, email, password_hash, role_id, phone_number) VALUES (?, ?, ?, (SELECT id FROM roles WHERE name = 'manager'), ?)",
        ("Suresh Iyer", "manager@iskconshirpur.org", manager_pass, "+919876500003")
    )
    manager_id = cursor.lastrowid

    departments = [
        ("Temple Construction", "Building maintenance, renovation, and new construction works.", "🏗️", "#f97316", 120000.0),
        ("Prasadam & Food Distribution", "Kitchen supplies, groceries, and daily prasadam distribution.", "🍲", "#10b981", 60000.0),
        ("Festival & Events", "Festival decorations, celebrations, and special event costs.", "🎉", "#ec4899", 45000.0),
        ("Deity Seva", "Deity ornaments, flowers, and daily seva items.", "🪔", "#eab308", 25000.0),
        ("General Administration", "Office supplies, utility bills, and administrative overhead.", "🗂️", "#6366f1", 20000.0),
    ]

    dept_ids = {}
    for name, description, icon, color, monthly_limit in departments:
        assigned_manager_id = manager_id if name == "Prasadam & Food Distribution" else None
        cursor.execute(
            "INSERT INTO departments (name, description, icon, color, created_by, manager_id) VALUES (?, ?, ?, ?, ?, ?)",
            (name, description, icon, color, admin_id, assigned_manager_id)
        )
        dept_id = cursor.lastrowid
        dept_ids[name] = dept_id
        cursor.execute(
            "INSERT INTO budgets (department_id, monthly_limit) VALUES (?, ?)",
            (dept_id, monthly_limit)
        )

    today = datetime.now()
    this_month = today.strftime("%Y-%m")
    last_month_date = (today.replace(day=1) - timedelta(days=1))
    last_month = last_month_date.strftime("%Y-%m")

    def d(day, month=this_month):
        return f"{month}-{day:02d}"

    sample_expenses = [
        ("Temple Construction", 45000.0, "Cement and construction materials", d(3), "Bank Transfer"),
        ("Temple Construction", 38000.0, "Mason and labor wages", d(10), "Cash"),
        ("Temple Construction", 22000.0, "Roofing repair works", d(18), "Bank Transfer"),
        ("Prasadam & Food Distribution", 18500.0, "Weekly grocery and vegetable supplies", d(2), "Cash"),
        ("Prasadam & Food Distribution", 9200.0, "Sunday feast prasadam ingredients", d(9), "Cash"),
        ("Prasadam & Food Distribution", 12300.0, "Kitchen LPG and cooking supplies", d(16), "UPI"),
        ("Festival & Events", 15600.0, "Janmashtami decorations and flowers", d(5), "UPI"),
        ("Festival & Events", 9800.0, "Sound and lighting rental for festival", d(14), "Bank Transfer"),
        ("Deity Seva", 6400.0, "Deity ornaments and jewellery polishing", d(6), "Cash"),
        ("Deity Seva", 4200.0, "Daily fresh flowers for deity seva", d(20), "Cash"),
        ("General Administration", 3100.0, "Office stationery and printing", d(4), "UPI"),
        ("General Administration", 8600.0, "Monthly electricity bill", d(11), "Bank Transfer"),
        ("Temple Construction", 15000.0, "Paint and finishing materials", d(20, last_month), "Cash"),
        ("Prasadam & Food Distribution", 11000.0, "Rice and grains bulk purchase", d(22, last_month), "Bank Transfer"),
        ("General Administration", 2400.0, "Internet and phone bill", d(25, last_month), "UPI"),
    ]

    expense_ids = {}
    for dept_name, amount, description, date_str, payment_method in sample_expenses:
        cursor.execute(
            "INSERT INTO expenses (department_id, amount, description, date, payment_method, added_by) VALUES (?, ?, ?, ?, ?, ?)",
            (dept_ids[dept_name], amount, description, date_str, payment_method, admin_id)
        )
        expense_ids.setdefault(dept_name, []).append(cursor.lastrowid)

    # Sample alerts / queries
    cursor.execute("""
        INSERT INTO alerts (type, department_id, title, message, severity, created_by)
        VALUES ('alert', ?, 'Construction spend nearing monthly budget', 'Temple Construction has crossed 85% of its monthly budget. Please review upcoming expenses before approving more.', 'warning', ?)
    """, (dept_ids["Temple Construction"], admin_id))

    query_expense_id = expense_ids["Temple Construction"][1]
    cursor.execute("""
        INSERT INTO alerts (type, department_id, expense_id, title, message, severity, created_by)
        VALUES ('query', ?, ?, 'Clarify labor wage breakdown', 'Can we get an itemized breakdown of the mason and labor wages entry? Amount seems higher than the previous cycle.', 'info', ?)
    """, (dept_ids["Temple Construction"], query_expense_id, admin_id))

    cursor.execute("""
        INSERT INTO alerts (type, department_id, title, message, severity, is_resolved, created_by, resolved_by, resolved_at)
        VALUES ('alert', ?, 'Festival budget confirmed sufficient', 'Reviewed Festival & Events spending against the monthly allocation — currently within safe limits.', 'info', 1, ?, ?, CURRENT_TIMESTAMP)
    """, (dept_ids["Festival & Events"], admin_id, admin_id))

    # Activity log entries so the Auditor's log isn't empty on first login
    log_activity(cursor, admin_id, "create", "department", dept_ids["Temple Construction"], "Created department 'Temple Construction'")
    log_activity(cursor, admin_id, "create", "department", dept_ids["Prasadam & Food Distribution"], "Created department 'Prasadam & Food Distribution'")
    log_activity(cursor, admin_id, "create", "expense", expense_ids["Temple Construction"][0], "Logged expense 'Cement and construction materials' (₹45,000.00)")
    log_activity(cursor, admin_id, "create", "budget", dept_ids["Temple Construction"], "Set monthly budget of ₹120,000.00 for Temple Construction")
    log_activity(cursor, admin_id, "create", "alert", None, "Raised alert: Construction spend nearing monthly budget")
    log_activity(cursor, admin_id, "create", "user", auditor_id, "Created auditor account for Govind Sharma")
    log_activity(cursor, admin_id, "create", "user", manager_id, "Created manager account for Suresh Iyer, assigned to Prasadam & Food Distribution")

    conn.commit()
    conn.close()
