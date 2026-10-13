import os
from dotenv import load_dotenv

# Must happen here, at import time — database.db imports from this module before
# app.py gets a chance to call load_dotenv() itself (app.py's own load_dotenv() call
# currently runs after its database.db import), so a DATABASE_URL set only in a local
# .env would otherwise be silently missed and the app would fall back to SQLite
# without any error.
load_dotenv()

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
IS_POSTGRES = bool(DATABASE_URL)

# Only needed if supabase-py (Storage/Auth/Realtime) is added later — not required
# for the raw-SQL/psycopg2 connection this app actually uses.
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

PG_POOL_MINCONN = int(os.environ.get("PG_POOL_MINCONN", "1"))
PG_POOL_MAXCONN = int(os.environ.get("PG_POOL_MAXCONN", "10"))

# --- Clerk (B2B Organizations) ---
# Leave these blank to keep today's local email/password login — this is the
# default and needs no setup. Set all three to switch to Clerk (passwordless
# OTP + Google OAuth, invite-only ISKCON Shirpur organization membership).
CLERK_SECRET_KEY = os.environ.get("CLERK_SECRET_KEY", "").strip()
CLERK_PUBLISHABLE_KEY = os.environ.get("CLERK_PUBLISHABLE_KEY", "").strip()
# From the Clerk dashboard: API Keys > Frontend API URL (e.g. "clean-example-1.clerk.accounts.dev").
# Optional to set explicitly — a Clerk publishable key is just base64("<frontend-api-domain>$")
# after its "pk_test_"/"pk_live_" prefix, so if CLERK_FRONTEND_API is left blank it's derived
# from CLERK_PUBLISHABLE_KEY instead (confirmed against a real key: decodes cleanly to
# "<slug>.clerk.accounts.dev$", trailing "$" stripped below).
CLERK_FRONTEND_API = os.environ.get("CLERK_FRONTEND_API", "").strip()
if not CLERK_FRONTEND_API and CLERK_PUBLISHABLE_KEY:
    try:
        import base64
        _encoded = CLERK_PUBLISHABLE_KEY.split("_", 2)[2]
        _padded = _encoded + "=" * (-len(_encoded) % 4)
        CLERK_FRONTEND_API = base64.b64decode(_padded).decode().rstrip("$")
    except Exception:
        CLERK_FRONTEND_API = ""

# The "ISKCON Shirpur" Organization's ID (starts with "org_"), from Clerk dashboard > Organizations.
# Default is the live ISKCON Shirpur org so membership checks work without editing .env.
CLERK_ORG_ID = os.environ.get("CLERK_ORG_ID", "").strip() or "org_3Jfcz8rUSNwckQ3LHQAVE4p8pxP"

# Optional: Clerk dashboard > API Keys > "JWT public key" (the PEM block). With it set,
# session tokens are verified with zero calls to Clerk. Without it, Clerk's public
# keys are fetched once per server process and then reused from memory.
CLERK_JWT_KEY = os.environ.get("CLERK_JWT_KEY", "").strip().replace("\\n", "\n")

# Clerk is on only when a secret is present — missing CLERK_SECRET_KEY keeps local email/password auth.
CLERK_ENABLED = bool(CLERK_SECRET_KEY and CLERK_PUBLISHABLE_KEY and CLERK_FRONTEND_API and CLERK_ORG_ID)

# --- Google Drive backup (uploaded bills/vouchers) ---
# Leave these unset to keep backups off — this is the default and needs no setup.
# GOOGLE_SERVICE_ACCOUNT_FILE is a path to the service account's downloaded JSON key,
# never the key content itself — see services/backup_service.py for the setup steps.
GOOGLE_SERVICE_ACCOUNT_FILE = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "credentials.json").strip()
GOOGLE_DRIVE_BACKUP_FOLDER_ID = os.environ.get("GOOGLE_DRIVE_BACKUP_FOLDER_ID", "").strip()

BACKUP_ENABLED = bool(GOOGLE_DRIVE_BACKUP_FOLDER_ID and os.path.exists(GOOGLE_SERVICE_ACCOUNT_FILE))

# --- Supabase keep-alive + weekly health-check email ---
# Free-tier Supabase projects pause after 7 days with no database activity.
# Defaults to the real ISKCON Shirpur admin address (same as CLERK_ORG_ID's
# default) so this works without editing .env; override via env var if needed.
ADMIN_ALERT_EMAIL = os.environ.get("ADMIN_ALERT_EMAIL", "").strip() or "shirpuriskconacc@gmail.com"

# --- Donations webhook (external project) ---
# Each donation an Admin logs is POSTed here as JSON. Leave blank to skip sending —
# donations are still saved, and marked "not configured" on the Donations page.
DONATIONS_WEBHOOK_URL = os.environ.get("DONATIONS_WEBHOOK_URL", "").strip()
# Who receives the donations CSV + PDF ("Send Report to Temple" button and the every-7-days email).
DONATIONS_REPORT_EMAIL = os.environ.get("DONATIONS_REPORT_EMAIL", "").strip() or "shirpuriskcon@gmail.com"
