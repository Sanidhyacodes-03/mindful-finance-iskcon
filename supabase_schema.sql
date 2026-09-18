-- ISKCON Shirpur Finance — Supabase (PostgreSQL) schema
--
-- Run this once in the Supabase SQL Editor (Project > SQL Editor > New query > paste > Run)
-- before pointing the app at this project via DATABASE_URL. This mirrors the app's SQLite
-- schema exactly, translated to Postgres:
--   - AUTOINCREMENT -> GENERATED ALWAYS AS IDENTITY
--   - is_active / is_resolved kept as INTEGER (0/1), not native BOOLEAN — the app compares
--     them with `= 1` / `= 0` in ~30 places; a real BOOLEAN column rejects those comparisons.
--   - date columns kept as TEXT ('YYYY-MM-DD'), matching how the app already stores them.
--
-- Safe to re-run: every statement is idempotent (IF NOT EXISTS / ON CONFLICT DO NOTHING).

CREATE TABLE IF NOT EXISTS roles (
    id   INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name TEXT UNIQUE NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id                    INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name                  TEXT NOT NULL,
    email                 TEXT UNIQUE NOT NULL,
    password_hash         TEXT NOT NULL,
    role_id               INTEGER NOT NULL REFERENCES roles(id),
    is_active             INTEGER NOT NULL DEFAULT 1,
    phone_number          TEXT,
    whatsapp_notify_mode  TEXT DEFAULT 'large_only',
    reset_token           TEXT,
    reset_token_expiry    TEXT,
    created_at            TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS departments (
    id           INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name         TEXT UNIQUE NOT NULL,
    description  TEXT,
    icon         TEXT DEFAULT '🏛️',
    color        TEXT DEFAULT '#6366f1',
    is_active    INTEGER NOT NULL DEFAULT 1,
    created_by   INTEGER REFERENCES users(id) ON DELETE SET NULL,
    manager_id   INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS expenses (
    id              INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    department_id   INTEGER NOT NULL REFERENCES departments(id),
    amount          REAL NOT NULL,
    description     TEXT NOT NULL,
    date            TEXT NOT NULL,
    payment_method  TEXT DEFAULT 'Cash',
    receipt_path    TEXT,
    added_by        INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS budgets (
    id              INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    department_id   INTEGER NOT NULL UNIQUE REFERENCES departments(id) ON DELETE CASCADE,
    monthly_limit   REAL NOT NULL,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS alerts (
    id             INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    type           TEXT NOT NULL DEFAULT 'alert' CHECK (type IN ('alert', 'query')),
    department_id  INTEGER REFERENCES departments(id) ON DELETE SET NULL,
    expense_id     INTEGER REFERENCES expenses(id) ON DELETE SET NULL,
    title          TEXT NOT NULL,
    message        TEXT NOT NULL,
    severity       TEXT DEFAULT 'info' CHECK (severity IN ('info', 'warning', 'critical')),
    is_resolved    INTEGER NOT NULL DEFAULT 0,
    created_by     INTEGER REFERENCES users(id) ON DELETE SET NULL,
    resolved_by    INTEGER REFERENCES users(id) ON DELETE SET NULL,
    resolved_at    TIMESTAMP,
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id           INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id      INTEGER REFERENCES users(id) ON DELETE SET NULL,
    action       TEXT NOT NULL,
    entity_type  TEXT NOT NULL,
    entity_id    INTEGER,
    details      TEXT,
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Postgres doesn't auto-index foreign-key columns (unlike the primary key). These cover
-- every FK plus expenses.date, which every month-to-date budget query filters on.
CREATE INDEX IF NOT EXISTS idx_users_role_id           ON users(role_id);
CREATE INDEX IF NOT EXISTS idx_departments_created_by  ON departments(created_by);
CREATE INDEX IF NOT EXISTS idx_departments_manager_id  ON departments(manager_id);
CREATE INDEX IF NOT EXISTS idx_expenses_department_id  ON expenses(department_id);
CREATE INDEX IF NOT EXISTS idx_expenses_added_by       ON expenses(added_by);
CREATE INDEX IF NOT EXISTS idx_expenses_date           ON expenses(date);
CREATE INDEX IF NOT EXISTS idx_alerts_department_id    ON alerts(department_id);
CREATE INDEX IF NOT EXISTS idx_alerts_expense_id       ON alerts(expense_id);
CREATE INDEX IF NOT EXISTS idx_alerts_is_resolved      ON alerts(is_resolved);
CREATE INDEX IF NOT EXISTS idx_audit_logs_user_id      ON audit_logs(user_id);

-- The app also seeds these on startup (idempotent, ON CONFLICT DO NOTHING) — running
-- it here too just means they exist immediately rather than on first request.
INSERT INTO roles (name) VALUES ('admin'), ('auditor'), ('manager')
ON CONFLICT (name) DO NOTHING;

-- Note: demo accounts (admin@iskconshirpur.org etc.) and sample departments/expenses are
-- NOT created by this script — the app's seed_db() creates those automatically on first
-- run against this database, the same way it does for SQLite today.
