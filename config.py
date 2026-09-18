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
