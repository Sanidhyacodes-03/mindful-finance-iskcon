import math
import os
import uuid
import secrets
import requests
from functools import wraps
from datetime import datetime, timedelta
from dotenv import load_dotenv
from flask import (
    Flask, render_template, request, redirect, url_for, session, flash, g,
    Response, send_from_directory, abort
)
from werkzeug.utils import secure_filename
import config
from database.db import get_db, init_db, seed_db, log_activity, month_filter_sql, release_request_connections
from services.email_service import EmailService
from services.whatsapp_service import WhatsAppService
from services.report_service import ReportService
from services.donation_report_service import (
    generate_donations_csv, generate_donations_pdf, send_donations_report,
)
from services.security import hash_password, verify_password
from services.clerk_auth import (
    load_clerk_user, authenticate_clerk_request, clerk_org_required,
)

load_dotenv()  # reads variables from a .env file in this folder, if present

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "spendly-ai-expense-tracker-key-2026")
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024  # 5MB upload cap

# DATA_DIR points receipts (and, via database/db.py, the SQLite file) at a mounted
# persistent disk in production. Unset locally, so local behavior is unchanged.
_DATA_DIR = os.environ.get("DATA_DIR")
RECEIPT_DIR = (
    os.path.join(_DATA_DIR, "uploads", "receipts") if _DATA_DIR
    else os.path.join(app.root_path, "static", "uploads", "receipts")
)
ALLOWED_RECEIPT_EXTENSIONS = {"pdf", "png", "jpg", "jpeg"}

# Pages that show the rotating photo background instead of the usual subtle spiritual-bg.
CAROUSEL_ENDPOINTS = {'landing', 'login', 'forgot_password', 'reset_password'}


def _discover_background_images():
    """Walked once at import time — the backgrounds folder doesn't change at runtime."""
    backgrounds_dir = os.path.join(app.root_path, "static", "backgrounds")
    paths = []
    for root, _dirs, files in os.walk(backgrounds_dir):
        for fname in files:
            if fname.lower().endswith((".jpg", ".jpeg", ".png")):
                rel = os.path.relpath(os.path.join(root, fname), os.path.join(app.root_path, "static"))
                paths.append(rel.replace(os.sep, "/"))
    return paths


BACKGROUND_IMAGE_PATHS = _discover_background_images()

# Initialize and seed database on startup
with app.app_context():
    init_db()
    seed_db()

# ------------------------------------------------------------------ #
# Auth / RBAC
# ------------------------------------------------------------------ #

def login_required(f):
    clerk_guard = clerk_org_required(f)

    @wraps(f)
    def decorated_function(*args, **kwargs):
        if config.CLERK_ENABLED:
            return clerk_guard(*args, **kwargs)
        if 'user_id' not in session:
            flash("Please sign in to access this page.", "warning")
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function


def role_required(*roles):
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            if config.CLERK_ENABLED:
                gated = clerk_org_required(lambda *a, **k: None)
                auth_result = gated(*args, **kwargs)
                if auth_result is not None:
                    return auth_result
            elif 'user_id' not in session:
                flash("Please sign in to access this page.", "warning")
                return redirect(url_for('login'))
            if not g.user or g.user['role'] not in roles:
                flash("You do not have permission to perform this action.", "danger")
                return redirect(url_for('dashboard'))
            return f(*args, **kwargs)
        return wrapped
    return decorator


# Endpoints an Auditor is allowed to POST to (self-service, plus raising alerts — never other org data mutation).
AUDITOR_WRITE_WHITELIST = {'profile', 'logout', 'forgot_password', 'reset_password', 'add_alert'}

# A Manager may only reach these endpoints — everything org-wide (departments, users, reports,
# activity log) is off-limits. 'static' must be included or a Manager's CSS/JS silently breaks.
MANAGER_ALLOWED_ENDPOINTS = {
    'static', 'landing', 'terms', 'privacy',
    'dashboard', 'expenses', 'add_expense', 'edit_expense', 'delete_expense',
    'view_receipt', 'budgets', 'alerts', 'profile', 'logout',
    'reports', 'export_report_pdf', 'export_report_csv',
}


def _set_g_user_and_alert_count(db, cursor):
    """Shared by both auth paths once g.user has been resolved to a users row
    (or None) — computes the same manager-scoped/global unresolved-alerts
    badge count either way."""
    if g.user is None or g.user['role'] == 'pending':
        # Pending users must not learn even the count of org-wide alerts.
        g.unresolved_alerts_count = 0
    elif g.user['role'] == 'manager':
        # Scoped to their own department, matching what /alerts actually shows them —
        # otherwise the badge would leak the existence/count of alerts on departments
        # they have no visibility into.
        alert_filter, alert_params = _manager_alert_filter(_manager_department_ids(cursor, g.user), g.user['id'])
        cursor.execute(f"SELECT COUNT(*) FROM alerts a WHERE a.is_resolved = 0{alert_filter}", alert_params)
        res = cursor.fetchone()
        g.unresolved_alerts_count = res[0] if res else 0
    else:
        cursor.execute("SELECT COUNT(*) FROM alerts WHERE is_resolved = 0")
        res = cursor.fetchone()
        g.unresolved_alerts_count = res[0] if res else 0


@app.before_request
def load_logged_in_user():
    if request.endpoint == 'static':
        # CSS/JS/images are public and a page pulls in a dozen of them — resolving
        # the user for each would repeat the token check and database queries.
        g.user = None
        g.unresolved_alerts_count = 0
        return

    if config.CLERK_ENABLED:
        # Only resolves g.user here — the org-membership 403 is clerk_org_required's
        # job (services/clerk_auth.py), invoked by every guarded route via
        # login_required/role_required below. Every sensitive route already carries
        # one of those two decorators (verified: dashboard, departments, finances,
        # alerts, reports, users, profile, etc.) — landing/login/terms/privacy are
        # intentionally public. Checking org membership only once, inside the
        # decorator, avoids re-verifying the same Clerk session token twice per
        # request (authenticate_clerk_request caches its result on `g` either way).
        #
        # Clerk organization membership is not required: any authenticated person
        # is resolved to (or provisioned as) a local users row, and the local role
        # alone decides access. A brand-new sign-up becomes `pending`, which
        # enforce_pending_lockout below confines to the holding page until an
        # Admin approves them.
        claims = authenticate_clerk_request(request)
        if claims is None:
            g.user = None
            g.unresolved_alerts_count = 0
            return

        db = get_db()
        cursor = db.cursor()
        g.user = load_clerk_user(db, cursor, claims)
        _set_g_user_and_alert_count(db, cursor)
        db.close()
        return

    user_id = session.get('user_id')
    if user_id is None:
        g.user = None
        g.unresolved_alerts_count = 0
    else:
        db = get_db()
        cursor = db.cursor()
        cursor.execute("""
            SELECT u.*, r.name as role FROM users u
            JOIN roles r ON u.role_id = r.id
            WHERE u.id = ? AND u.is_active = 1
        """, (user_id,))
        g.user = cursor.fetchone()

        if g.user is None:
            # Account was deactivated/removed since the session was created.
            session.clear()
            g.unresolved_alerts_count = 0
        else:
            _set_g_user_and_alert_count(db, cursor)
        db.close()


# The only endpoints a pending user may reach. Everything else — finances, every
# POST route, receipts, reports — is behind this. It has to be a global gate rather
# than per-route checks: _manager_department_ids() returns None ("see everything")
# for any non-manager role, so a pending user slipping past even one route
# would get org-wide visibility, not none.
PENDING_ALLOWED_ENDPOINTS = {'static', 'pending_approval', 'login', 'logout', 'terms', 'privacy'}


@app.before_request
def enforce_pending_lockout():
    if g.user and g.user['role'] == 'pending':
        if request.endpoint not in PENDING_ALLOWED_ENDPOINTS:
            return redirect(url_for('pending_approval'))


@app.teardown_request
def release_db_connections(exc):
    # Runs after every request — including ones that raised — so no pooled
    # database connection outlives the request that checked it out.
    release_request_connections()


@app.before_request
def enforce_auditor_readonly():
    if g.user and g.user['role'] == 'tmc_member':
        if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
            if request.endpoint not in AUDITOR_WRITE_WHITELIST:
                flash("Your account has read-only access.", "danger")
                return redirect(url_for('dashboard'))


@app.before_request
def enforce_manager_scope():
    if g.user and g.user['role'] == 'manager':
        if request.endpoint and request.endpoint not in MANAGER_ALLOWED_ENDPOINTS:
            flash("Your account is scoped to your assigned department.", "danger")
            return redirect(url_for('dashboard'))



# What each internal role name is called on screen — the one place to change
# role wording (e.g. to all caps).
ROLE_LABELS = {
    'accountant': 'Accountant',
    'tmc_member': 'TMC Member',
    'manager': 'Manager',
    'pending': 'Pending',
}


@app.context_processor
def inject_globals():
    show_carousel = request.endpoint in CAROUSEL_ENDPOINTS
    return {
        'role_labels': ROLE_LABELS,
        'today_date': datetime.now().strftime('%Y-%m-%d'),
        'datetime': datetime,
        'show_bg_carousel': show_carousel,
        'background_images': [url_for('static', filename=p) for p in BACKGROUND_IMAGE_PATHS] if show_carousel else [],
        'clerk_enabled': config.CLERK_ENABLED,
        'clerk_publishable_key': config.CLERK_PUBLISHABLE_KEY,
        'clerk_frontend_api': config.CLERK_FRONTEND_API,
    }


@app.errorhandler(413)
def file_too_large(e):
    flash("That file is too large. Receipts must be under 5MB.", "danger")
    return redirect(request.referrer or url_for('dashboard'))


@app.errorhandler(403)
def forbidden(e):
    # Real 403s here — not a redirect — because the "not an ISKCON Shirpur org
    # member" case needs to be visibly rejected, not just bounced to login as
    # if the person simply wasn't signed in.
    return render_template("403.html"), 403


# ------------------------------------------------------------------ #
# Small shared helpers
# ------------------------------------------------------------------ #

def _visible_departments(cursor, user):
    """Departments a given user may see. Admin/auditor: all active. Manager: only their own."""
    if user['role'] == 'manager':
        cursor.execute("SELECT * FROM departments WHERE manager_id = ? AND is_active = 1 ORDER BY name ASC", (user['id'],))
    else:
        cursor.execute("SELECT * FROM departments WHERE is_active = 1 ORDER BY name ASC")
    return cursor.fetchall()


def _manager_department_ids(cursor, user):
    """None for non-managers (no scoping needed); a (possibly empty) id list for managers."""
    if user['role'] != 'manager':
        return None
    return [d['id'] for d in _visible_departments(cursor, user)]


def _manager_alert_filter(dept_ids, user_id):
    """SQL fragment (for `alerts a`) + params limiting a Manager to the alerts
    meant for them: anything addressed to them personally, plus unaddressed
    alerts on their own departments. An alert or query addressed to someone
    else never reaches a Manager just because it names their department."""
    if dept_ids:
        in_clause = ','.join('?' * len(dept_ids))
        return (f" AND (a.assigned_to = ? OR (a.assigned_to IS NULL AND a.department_id IN ({in_clause})))",
                [user_id] + list(dept_ids))
    return " AND a.assigned_to = ?", [user_id]


def _check_budget_cap(cursor, department_id, additional_amount, exclude_expense_id=None):
    """Manager-only enforcement: does adding/increasing an expense by additional_amount push
    this department's month-to-date spend past its allocated budget (budgets.monthly_limit)?
    Returns (ok, message) — message is None when ok or when the department has no budget set
    (no cap means no enforcement, matching how every other budget view already treats it)."""
    cursor.execute("SELECT name FROM departments WHERE id = ?", (department_id,))
    dept_row = cursor.fetchone()
    department_name = dept_row['name'] if dept_row else "this department"

    cursor.execute("SELECT monthly_limit FROM budgets WHERE department_id = ?", (department_id,))
    budget_row = cursor.fetchone()
    if not budget_row:
        return True, None
    monthly_limit = budget_row['monthly_limit']

    cur_month = datetime.now().strftime("%Y-%m")
    query = f"SELECT COALESCE(SUM(amount), 0) as total FROM expenses WHERE department_id = ? AND {month_filter_sql('date')} = ?"
    params = [department_id, cur_month]
    if exclude_expense_id is not None:
        query += " AND id != ?"
        params.append(exclude_expense_id)
    cursor.execute(query, params)
    month_spent = cursor.fetchone()['total']

    projected = month_spent + additional_amount
    if projected > monthly_limit:
        remaining = max(0, monthly_limit - month_spent)
        return False, (
            f"This expense would exceed {department_name}'s monthly budget of ₹{monthly_limit:,.2f}. "
            f"₹{remaining:,.2f} remaining this month."
        )
    return True, None


def _count_active_admins(cursor, exclude_user_id=None):
    query = """
        SELECT COUNT(*) FROM users u JOIN roles r ON u.role_id = r.id
        WHERE r.name = 'accountant' AND u.is_active = 1
    """
    params = []
    if exclude_user_id:
        query += " AND u.id != ?"
        params.append(exclude_user_id)
    cursor.execute(query, params)
    return cursor.fetchone()[0]


def _role_id(cursor, role_name):
    cursor.execute("SELECT id FROM roles WHERE name = ?", (role_name,))
    row = cursor.fetchone()
    return row['id'] if row else None


def _allowed_receipt(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_RECEIPT_EXTENSIONS


def _save_receipt(file_storage):
    """Validates and saves an uploaded receipt; returns the stored filename or None."""
    if not file_storage or not file_storage.filename:
        return None
    if not _allowed_receipt(file_storage.filename):
        raise ValueError("Receipts must be PDF, PNG, or JPG files.")
    stored_name = f"{uuid.uuid4().hex}_{secure_filename(file_storage.filename)}"
    os.makedirs(RECEIPT_DIR, exist_ok=True)
    file_storage.save(os.path.join(RECEIPT_DIR, stored_name))
    return stored_name


def _delete_receipt(filename):
    if not filename:
        return
    try:
        os.remove(os.path.join(RECEIPT_DIR, filename))
    except OSError:
        pass


# ------------------------------------------------------------------ #
# Public & Auth Routes
# ------------------------------------------------------------------ #

@app.route("/")
def landing():
    if g.user:
        return redirect(url_for('dashboard'))
    return render_template("landing.html")


def _safe_next(target):
    """A same-site path to return to after sign-in, or None. Rejects absolute
    and scheme-relative URLs so ?next= can't send anyone off-site."""
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return None
    return target


@app.route("/login", methods=["GET", "POST"])
def login():
    next_url = _safe_next(request.args.get("next"))
    if g.user:
        return redirect(next_url or url_for('expenses'))
    if config.CLERK_ENABLED:
        return render_template("login.html", next_url=next_url or url_for('expenses'))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        db = get_db()
        cursor = db.cursor()
        cursor.execute("SELECT * FROM users WHERE email = ? AND is_active = 1", (email,))
        user = cursor.fetchone()
        db.close()

        if user and verify_password(user['password_hash'], password):
            session['user_id'] = user['id']
            session['user_name'] = user['name']
            flash(f"Welcome back, {user['name']}!", "success")

            notify_mode = user['whatsapp_notify_mode'] or 'large_only'
            if user['phone_number'] and notify_mode != 'off':
                try:
                    WhatsAppService.notify_login(user['phone_number'], user['name'])
                except Exception as e:
                    app.logger.error(f"WhatsApp login notification failed for user {user['id']}: {e}")

            return redirect(url_for('dashboard'))

        flash("Invalid email or password. Please try again.", "danger")
        return render_template("login.html", error="Invalid email or password.")

    return render_template("login.html")


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()

        db = get_db()
        cursor = db.cursor()
        cursor.execute("SELECT * FROM users WHERE email = ? AND is_active = 1", (email,))
        user = cursor.fetchone()

        if user:
            reset_token = secrets.token_urlsafe(32)
            expiry = (datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
            cursor.execute("UPDATE users SET reset_token = ?, reset_token_expiry = ? WHERE id = ?", (reset_token, expiry, user['id']))
            db.commit()
            EmailService.send_password_reset_email(email, user['name'], reset_token, request.host_url.rstrip('/'))

        db.close()
        flash("If that email address exists in our system, a password reset link has been sent.", "info")
        return redirect(url_for('login'))

    return render_template("forgot_password.html")


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT * FROM users WHERE reset_token = ?", (token,))
    user = cursor.fetchone()

    def _token_valid(u):
        if not u or not u['reset_token_expiry']:
            return False
        try:
            expiry = datetime.strptime(u['reset_token_expiry'], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return False
        return datetime.now() <= expiry

    if not _token_valid(user):
        db.close()
        flash("Invalid or expired password reset link.", "danger")
        return redirect(url_for('login'))

    if request.method == "POST":
        new_password = request.form.get("password", "")
        if len(new_password) < 6:
            flash("Password must be at least 6 characters long.", "danger")
            return render_template("reset_password.html", token=token)

        hashed = hash_password(new_password)
        cursor.execute("UPDATE users SET password_hash = ?, reset_token = NULL, reset_token_expiry = NULL WHERE id = ?", (hashed, user['id']))
        db.commit()
        db.close()

        flash("Password updated successfully. Please sign in.", "success")
        return redirect(url_for('login'))

    db.close()
    return render_template("reset_password.html", token=token)


@app.route("/logout")
def logout():
    session.clear()
    if config.CLERK_ENABLED:
        # Clerk's own session cookie is only clearable client-side via its JS
        # SDK — this tiny page fires Clerk.signOut() then bounces to /.
        return render_template("clerk_logout.html")
    flash("You have been signed out safely.", "info")
    return redirect(url_for('landing'))


@app.route("/pending")
def pending_approval():
    # Not @login_required: under Clerk that decorator 403s anyone outside the
    # ISKCON org, which is exactly what a new pending self-signup still is.
    if g.user is None:
        return redirect(url_for('login'))
    if g.user['role'] != 'pending':
        return redirect(url_for('dashboard'))
    return render_template("pending.html")


@app.route("/terms")
def terms():
    return render_template("terms.html")


@app.route("/privacy")
def privacy():
    return render_template("privacy.html")


# ------------------------------------------------------------------ #
# Dashboard
# ------------------------------------------------------------------ #

@app.route("/dashboard")
@login_required
def dashboard():
    db = get_db()
    cursor = db.cursor()
    now = datetime.now()
    cur_month = now.strftime("%Y-%m")

    dept_ids = _manager_department_ids(cursor, g.user)
    scoped = dept_ids is not None
    if scoped and not dept_ids:
        # Manager with no department assigned yet — empty state, skip all queries.
        db.close()
        return render_template(
            "dashboard.html", month_spent=0.0, department_count=0, unresolved_count=0,
            department_tiles=[], recent_expenses=[], recent_alerts=[],
            chart_labels=[], chart_spent=[], chart_budget=[],
        )

    in_clause = f"({','.join('?' * len(dept_ids))})" if scoped else ""
    dept_params = list(dept_ids) if scoped else []
    e_dept_filter = f" AND e.department_id IN {in_clause}" if scoped else ""
    a_dept_filter, a_params = _manager_alert_filter(dept_ids, g.user['id']) if scoped else ("", [])
    d_dept_filter = f" AND d.id IN {in_clause}" if scoped else ""

    cursor.execute(
        f"SELECT SUM(amount) as total FROM expenses e WHERE {month_filter_sql('e.date')} = ?{e_dept_filter}",
        [cur_month] + dept_params
    )
    month_spent = cursor.fetchone()['total'] or 0.0

    cursor.execute(f"SELECT COUNT(*) as count FROM departments d WHERE d.is_active = 1{d_dept_filter}", dept_params)
    department_count = cursor.fetchone()['count'] or 0

    cursor.execute(f"SELECT COUNT(*) as count FROM alerts a WHERE a.is_resolved = 0{a_dept_filter}", a_params)
    unresolved_count = cursor.fetchone()['count'] or 0

    cursor.execute(f"""
        SELECT d.id, d.name, d.icon, d.color, d.is_active,
               COALESCE(MAX(b.monthly_limit), 0) as monthly_limit,
               COALESCE(SUM(CASE WHEN {month_filter_sql('e.date')} = ? THEN e.amount END), 0.0) as spent
        FROM departments d
        LEFT JOIN budgets b ON b.department_id = d.id
        LEFT JOIN expenses e ON e.department_id = d.id
        WHERE d.is_active = 1{d_dept_filter}
        GROUP BY d.id
        ORDER BY d.name ASC
    """, [cur_month] + dept_params)
    department_tiles = cursor.fetchall()

    cursor.execute(f"""
        SELECT e.*, d.name as department_name, d.icon as department_icon, d.color as department_color
        FROM expenses e
        JOIN departments d ON e.department_id = d.id
        WHERE 1=1{e_dept_filter}
        ORDER BY e.date DESC, e.id DESC
        LIMIT 8
    """, dept_params)
    recent_expenses = cursor.fetchall()

    cursor.execute(f"""
        SELECT a.*, d.name as department_name
        FROM alerts a
        LEFT JOIN departments d ON a.department_id = d.id
        WHERE a.is_resolved = 0{a_dept_filter}
        ORDER BY a.created_at DESC
        LIMIT 5
    """, a_params)
    recent_alerts = cursor.fetchall()

    chart_labels = [row['name'] for row in department_tiles]
    chart_spent = [round(row['spent'], 2) for row in department_tiles]
    chart_budget = [round(row['monthly_limit'], 2) for row in department_tiles]

    # Manager Overview: name their department(s) and budget instead of generic
    # wording. department_tiles is already scoped to the departments they manage.
    manager_summary = None
    if g.user['role'] == 'manager' and department_tiles:
        budget_total = sum(row['monthly_limit'] for row in department_tiles)
        manager_summary = {
            'department_names': ", ".join(row['name'] for row in department_tiles),
            'budget': budget_total,
            'remaining': budget_total - month_spent,
        }

    pending_users = []
    assignable_departments = []
    if g.user['role'] == 'accountant':
        cursor.execute("""
            SELECT u.id, u.name, u.email, u.created_at FROM users u
            JOIN roles r ON u.role_id = r.id
            WHERE r.name = 'pending' AND u.is_active = 1
            ORDER BY u.created_at ASC
        """)
        pending_users = cursor.fetchall()
        if pending_users:
            # Departments a newly approved Manager can take over: active, and
            # with no manager or only a deactivated one.
            cursor.execute("""
                SELECT d.id, d.name FROM departments d
                LEFT JOIN users m ON d.manager_id = m.id
                WHERE d.is_active = 1 AND (d.manager_id IS NULL OR m.is_active = 0)
                ORDER BY d.name ASC
            """)
            assignable_departments = cursor.fetchall()

    db.close()
    return render_template(
        "dashboard.html",
        month_spent=month_spent,
        department_count=department_count,
        unresolved_count=unresolved_count,
        department_tiles=department_tiles,
        recent_expenses=recent_expenses,
        recent_alerts=recent_alerts,
        chart_labels=chart_labels,
        chart_spent=chart_spent,
        chart_budget=chart_budget,
        pending_users=pending_users,
        assignable_departments=assignable_departments,
        manager_summary=manager_summary,
    )


# ------------------------------------------------------------------ #
# Departments
# ------------------------------------------------------------------ #

@app.route("/departments")
@login_required
def departments():
    db = get_db()
    cursor = db.cursor()
    now = datetime.now()
    cur_month = now.strftime("%Y-%m")

    cursor.execute(f"""
        SELECT d.*, COALESCE(MAX(b.monthly_limit), 0) as monthly_limit,
               COALESCE(SUM(CASE WHEN {month_filter_sql('e.date')} = ? THEN e.amount END), 0.0) as spent,
               COUNT(e.id) as expense_count,
               MAX(m.name) as manager_name, MAX(m.email) as manager_email, MAX(m.is_active) as manager_is_active
        FROM departments d
        LEFT JOIN budgets b ON b.department_id = d.id
        LEFT JOIN expenses e ON e.department_id = d.id
        LEFT JOIN users m ON d.manager_id = m.id
        GROUP BY d.id
        ORDER BY d.is_active DESC, d.name ASC
    """, (cur_month,))
    department_list = cursor.fetchall()

    # Not filtered by is_active so a department's currently-assigned manager still shows
    # correctly pre-selected in the edit form even if later deactivated.
    cursor.execute("""
        SELECT u.id, u.name, u.email, u.is_active FROM users u
        JOIN roles r ON u.role_id = r.id
        WHERE r.name = 'manager' ORDER BY u.name ASC
    """)
    managers_list = cursor.fetchall()

    db.close()

    return render_template("departments.html", departments=department_list, managers_list=managers_list)


def _resolve_manager_id(cursor, raw_manager_id):
    """Empty string -> None. Otherwise must reference an existing user with role='manager'."""
    if not raw_manager_id:
        return None, True
    cursor.execute("""
        SELECT u.id FROM users u JOIN roles r ON u.role_id = r.id
        WHERE u.id = ? AND r.name = 'manager'
    """, (raw_manager_id,))
    if not cursor.fetchone():
        return None, False
    return int(raw_manager_id), True


@app.route("/departments/<int:id>/edit", methods=["POST"])
@role_required('accountant')
def edit_department(id):
    name = request.form.get("name", "").strip()
    description = request.form.get("description", "").strip()
    icon = request.form.get("icon", "🏛️").strip() or "🏛️"
    color = request.form.get("color", "#6366f1").strip() or "#6366f1"
    raw_manager_id = request.form.get("manager_id", "")

    if not name:
        flash("Department name is required.", "danger")
        return redirect(url_for('departments'))

    db = get_db()
    cursor = db.cursor()

    manager_id, valid = _resolve_manager_id(cursor, raw_manager_id)
    if not valid:
        db.close()
        flash("Selected manager account is invalid.", "danger")
        return redirect(url_for('departments'))

    cursor.execute(
        "UPDATE departments SET name = ?, description = ?, icon = ?, color = ?, manager_id = ? WHERE id = ?",
        (name, description, icon, color, manager_id, id)
    )
    log_activity(cursor, g.user['id'], "update", "department", id, f"Updated department '{name}'")
    db.commit()
    db.close()

    flash("Department updated.", "success")
    return redirect(url_for('departments'))


@app.route("/departments/<int:id>/toggle", methods=["POST"])
@role_required('accountant')
def toggle_department(id):
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT name, is_active FROM departments WHERE id = ?", (id,))
    dept = cursor.fetchone()
    if dept:
        new_status = 0 if dept['is_active'] else 1
        cursor.execute("UPDATE departments SET is_active = ? WHERE id = ?", (new_status, id))
        action = "deactivate" if new_status == 0 else "update"
        log_activity(cursor, g.user['id'], action, "department", id, f"{'Deactivated' if new_status == 0 else 'Reactivated'} department '{dept['name']}'")
        db.commit()
        flash(f"Department {'deactivated' if new_status == 0 else 'reactivated'}.", "info")
    db.close()
    return redirect(url_for('departments'))


@app.route("/departments/<int:id>/delete", methods=["POST"])
@role_required('accountant')
def delete_department(id):
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT name FROM departments WHERE id = ?", (id,))
    dept = cursor.fetchone()
    if not dept:
        db.close()
        flash("Department not found.", "danger")
        return redirect(url_for('departments'))

    cursor.execute("SELECT COUNT(*) FROM expenses WHERE department_id = ?", (id,))
    has_expenses = cursor.fetchone()[0] > 0

    if has_expenses:
        cursor.execute("UPDATE departments SET is_active = 0 WHERE id = ?", (id,))
        log_activity(cursor, g.user['id'], "deactivate", "department", id, f"Soft-deleted department '{dept['name']}' (has expense history)")
        flash(f"'{dept['name']}' has expense history, so it was deactivated instead of deleted to preserve records.", "info")
    else:
        cursor.execute("DELETE FROM departments WHERE id = ?", (id,))
        log_activity(cursor, g.user['id'], "delete", "department", id, f"Deleted department '{dept['name']}'")
        flash(f"Department '{dept['name']}' deleted.", "success")

    db.commit()
    db.close()
    return redirect(url_for('departments'))


# ------------------------------------------------------------------ #
# Expense Management
# ------------------------------------------------------------------ #

@app.route("/expenses")
@app.route("/finances")
@login_required
def expenses():
    search_q = request.args.get("q", "").strip()
    department_id = request.args.get("department", "")
    date_from = request.args.get("from", "")
    date_to = request.args.get("to", "")

    db = get_db()
    cursor = db.cursor()

    dept_ids = _manager_department_ids(cursor, g.user)
    if dept_ids is not None and not dept_ids:
        db.close()
        flash("No department is assigned to your account yet. Contact an administrator.", "warning")
        return render_template("finances.html", expenses=[], departments=[], budgets=[], search_q=search_q,
                                selected_department=department_id, date_from=date_from, date_to=date_to)

    now = datetime.now()
    cur_month = now.strftime("%Y-%m")
    budget_dept_filter = f" AND d.id IN ({','.join('?' * len(dept_ids))})" if dept_ids is not None else ""
    budget_dept_params = list(dept_ids) if dept_ids is not None else []

    cursor.execute(f"""
        SELECT MAX(b.id) as budget_id, MAX(b.monthly_limit) as monthly_limit, d.id as department_id, d.name, d.icon, d.color,
               COALESCE(SUM(CASE WHEN {month_filter_sql('e.date')} = ? THEN e.amount END), 0.0) as spent
        FROM departments d
        LEFT JOIN budgets b ON b.department_id = d.id
        LEFT JOIN expenses e ON e.department_id = d.id
        WHERE d.is_active = 1{budget_dept_filter}
        GROUP BY d.id
        ORDER BY monthly_limit DESC, spent DESC
    """, [cur_month] + budget_dept_params)
    budget_items = cursor.fetchall()

    query = """
        SELECT e.*, d.name as department_name, d.icon as department_icon, d.color as department_color,
               u.name as added_by_name
        FROM expenses e
        JOIN departments d ON e.department_id = d.id
        LEFT JOIN users u ON e.added_by = u.id
        WHERE 1=1
    """
    params = []

    if dept_ids is not None:
        query += f" AND e.department_id IN ({','.join('?' * len(dept_ids))})"
        params.extend(dept_ids)
    if search_q:
        query += " AND (e.description LIKE ? OR d.name LIKE ?)"
        params.extend([f"%{search_q}%", f"%{search_q}%"])
    if department_id:
        query += " AND e.department_id = ?"
        params.append(department_id)
    if date_from:
        query += " AND e.date >= ?"
        params.append(date_from)
    if date_to:
        query += " AND e.date <= ?"
        params.append(date_to)

    query += " ORDER BY e.date DESC, e.id DESC"

    cursor.execute(query, params)
    expenses_list = cursor.fetchall()

    departments_list = _visible_departments(cursor, g.user)

    db.close()
    return render_template(
        "finances.html",
        expenses=expenses_list,
        departments=departments_list,
        budgets=budget_items,
        search_q=search_q,
        selected_department=department_id,
        date_from=date_from,
        date_to=date_to
    )


@app.route("/expenses/add", methods=["POST"])
@role_required('accountant', 'manager')
def add_expense():
    amount = float(request.form.get("amount", 0) or 0)
    description = request.form.get("description", "").strip()
    department_id = request.form.get("department_id")
    date_str = request.form.get("date", datetime.now().strftime("%Y-%m-%d"))
    payment_method = request.form.get("payment_method", "Cash")

    db = get_db()
    cursor = db.cursor()

    # Managers never choose a department in the UI — the backend assigns it, ignoring
    # whatever (if anything) the client sent, so this can't be spoofed either.
    dept_ids = _manager_department_ids(cursor, g.user)
    if dept_ids is not None:
        if not dept_ids:
            db.close()
            flash("No department is assigned to your account. Contact an administrator.", "danger")
            return redirect(url_for('dashboard'))
        department_id = dept_ids[0]

    if amount <= 0 or not description or not department_id:
        db.close()
        flash("Please fill in valid expense details.", "danger")
        return redirect(url_for('expenses'))

    if dept_ids is not None:
        if int(department_id) not in dept_ids:
            db.close()
            flash("You can only log expenses for your assigned department.", "danger")
            return redirect(url_for('expenses'))

        ok, message = _check_budget_cap(cursor, department_id, amount)
        if not ok:
            db.close()
            flash(message, "danger")
            return redirect(url_for('expenses'))

    try:
        receipt_path = _save_receipt(request.files.get("receipt"))
    except ValueError as e:
        db.close()
        flash(str(e), "danger")
        return redirect(url_for('expenses'))

    cursor.execute(
        "INSERT INTO expenses (department_id, amount, description, date, payment_method, receipt_path, added_by) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (department_id, amount, description, date_str, payment_method, receipt_path, g.user['id'])
    )
    expense_id = cursor.lastrowid

    cursor.execute("SELECT name FROM departments WHERE id = ?", (department_id,))
    dept_row = cursor.fetchone()
    department_name = dept_row['name'] if dept_row else "Unknown"

    log_activity(cursor, g.user['id'], "create", "expense", expense_id, f"Logged ₹{amount:,.2f} expense for '{department_name}': {description}")

    db.commit()
    db.close()

    flash("Expense logged successfully!", "success")
    return redirect(url_for('expenses'))


@app.route("/expenses/<int:id>/edit", methods=["POST"])
@role_required('accountant', 'manager')
def edit_expense(id):
    amount = float(request.form.get("amount", 0) or 0)
    description = request.form.get("description", "").strip()
    department_id = request.form.get("department_id")
    date_str = request.form.get("date")
    payment_method = request.form.get("payment_method", "Cash")

    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT department_id, receipt_path FROM expenses WHERE id = ?", (id,))
    existing = cursor.fetchone()
    if not existing:
        db.close()
        flash("Expense not found.", "danger")
        return redirect(url_for('expenses'))

    dept_ids = _manager_department_ids(cursor, g.user)
    if dept_ids is not None:
        if existing['department_id'] not in dept_ids or (department_id and int(department_id) not in dept_ids):
            db.close()
            flash("You can only edit expenses for your assigned department.", "danger")
            return redirect(url_for('expenses'))

        target_department_id = int(department_id) if department_id else existing['department_id']
        ok, message = _check_budget_cap(cursor, target_department_id, amount, exclude_expense_id=id)
        if not ok:
            db.close()
            flash(message, "danger")
            return redirect(url_for('expenses'))

    try:
        new_receipt = _save_receipt(request.files.get("receipt"))
    except ValueError as e:
        db.close()
        flash(str(e), "danger")
        return redirect(url_for('expenses'))

    if new_receipt:
        _delete_receipt(existing['receipt_path'])
        cursor.execute(
            "UPDATE expenses SET amount = ?, description = ?, department_id = ?, date = ?, payment_method = ?, receipt_path = ? WHERE id = ?",
            (amount, description, department_id, date_str, payment_method, new_receipt, id)
        )
    else:
        cursor.execute(
            "UPDATE expenses SET amount = ?, description = ?, department_id = ?, date = ?, payment_method = ? WHERE id = ?",
            (amount, description, department_id, date_str, payment_method, id)
        )

    log_activity(cursor, g.user['id'], "update", "expense", id, f"Updated expense: {description} (₹{amount:,.2f})")
    db.commit()
    db.close()

    flash("Expense record updated.", "success")
    return redirect(url_for('expenses'))


@app.route("/expenses/<int:id>/delete", methods=["POST"])
@role_required('accountant', 'manager')
def delete_expense(id):
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT department_id, description, receipt_path FROM expenses WHERE id = ?", (id,))
    existing = cursor.fetchone()

    dept_ids = _manager_department_ids(cursor, g.user)
    if existing and dept_ids is not None and existing['department_id'] not in dept_ids:
        db.close()
        flash("You can only delete expenses for your assigned department.", "danger")
        return redirect(url_for('expenses'))

    if existing:
        cursor.execute("DELETE FROM expenses WHERE id = ?", (id,))
        _delete_receipt(existing['receipt_path'])
        log_activity(cursor, g.user['id'], "delete", "expense", id, f"Deleted expense: {existing['description']}")
        db.commit()
        flash("Expense deleted.", "info")
    db.close()
    return redirect(url_for('expenses'))


@app.route("/receipts/<path:filename>")
@login_required
def view_receipt(filename):
    return send_from_directory(RECEIPT_DIR, filename)


# ------------------------------------------------------------------ #
# Budgets (department monthly limits)
# ------------------------------------------------------------------ #

@app.route("/budgets")
@login_required
def budgets():
    # Budgets are now shown as part of the merged Finances page — this route stays
    # in place only so old bookmarks/links to /budgets keep working.
    return redirect(url_for('expenses'))


@app.route("/budgets/add", methods=["POST"])
@role_required('accountant')
def add_budget():
    department_id = request.form.get("department_id")
    monthly_limit = float(request.form.get("monthly_limit", 0) or 0)

    if not department_id or monthly_limit <= 0:
        flash("Invalid budget amount or department.", "danger")
        return redirect(url_for('expenses'))

    db = get_db()
    cursor = db.cursor()
    cursor.execute("""
        INSERT INTO budgets (department_id, monthly_limit, updated_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(department_id) DO UPDATE SET monthly_limit = excluded.monthly_limit, updated_at = CURRENT_TIMESTAMP
    """, (department_id, monthly_limit))

    cursor.execute("SELECT name FROM departments WHERE id = ?", (department_id,))
    dept_row = cursor.fetchone()
    log_activity(cursor, g.user['id'], "update", "budget", department_id, f"Set monthly budget of ₹{monthly_limit:,.2f} for '{dept_row['name'] if dept_row else 'department'}'")

    db.commit()
    db.close()

    flash("Department budget limit updated.", "success")
    return redirect(url_for('expenses'))


@app.route("/budgets/<int:id>/delete", methods=["POST"])
@role_required('accountant')
def delete_budget(id):
    db = get_db()
    cursor = db.cursor()
    cursor.execute("DELETE FROM budgets WHERE id = ?", (id,))
    log_activity(cursor, g.user['id'], "delete", "budget", id, "Removed department budget limit")
    db.commit()
    db.close()

    flash("Budget limit removed.", "info")
    return redirect(url_for('expenses'))


# ------------------------------------------------------------------ #
# Alerts & Query Tagging
# ------------------------------------------------------------------ #

@app.route("/alerts")
@login_required
def alerts():
    department_id = request.args.get("department", "")
    alert_type = request.args.get("type", "")
    status = request.args.get("status", "")

    db = get_db()
    cursor = db.cursor()

    dept_ids = _manager_department_ids(cursor, g.user)

    query = """
        SELECT a.*, d.name as department_name, d.icon as department_icon,
               e.description as expense_description, e.amount as expense_amount,
               cu.name as created_by_name, ru.name as resolved_by_name,
               au.name as assigned_to_name
        FROM alerts a
        LEFT JOIN departments d ON a.department_id = d.id
        LEFT JOIN expenses e ON a.expense_id = e.id
        LEFT JOIN users cu ON a.created_by = cu.id
        LEFT JOIN users ru ON a.resolved_by = ru.id
        LEFT JOIN users au ON a.assigned_to = au.id
        WHERE 1=1
    """
    params = []
    if dept_ids is not None:
        # Managers see unaddressed alerts on their own department plus anything
        # addressed to them — org-wide alerts stay Accountant/TMC Member-only.
        alert_filter, alert_params = _manager_alert_filter(dept_ids, g.user['id'])
        query += alert_filter
        params.extend(alert_params)
    if department_id:
        query += " AND a.department_id = ?"
        params.append(department_id)
    if alert_type in ("alert", "query"):
        query += " AND a.type = ?"
        params.append(alert_type)
    if status == "resolved":
        query += " AND a.is_resolved = 1"
    elif status == "open":
        query += " AND a.is_resolved = 0"

    query += " ORDER BY a.is_resolved ASC, a.created_at DESC"
    cursor.execute(query, params)
    alerts_list = cursor.fetchall()

    departments_list = _visible_departments(cursor, g.user)

    # The New Alert / Query form is only shown to Accountants and TMC Members.
    recent_expenses, recipients = [], []
    if g.user['role'] in ('accountant', 'tmc_member'):
        cursor.execute("SELECT id, description, amount FROM expenses ORDER BY date DESC LIMIT 200")
        recent_expenses = cursor.fetchall()
        recipients = _alert_recipients(cursor, exclude_user_id=g.user['id'])

    db.close()
    return render_template(
        "alerts.html",
        alerts=alerts_list,
        departments=departments_list,
        recent_expenses=recent_expenses,
        recipients=recipients,
        selected_department=department_id,
        selected_type=alert_type,
        selected_status=status,
    )


def _alert_recipients(cursor, exclude_user_id=None):
    """Active Accountants, TMC Members and Managers a query can be addressed to."""
    cursor.execute("""
        SELECT u.id, u.name, r.name as role FROM users u
        JOIN roles r ON u.role_id = r.id
        WHERE u.is_active = 1 AND r.name IN ('accountant', 'tmc_member', 'manager') AND u.id != ?
        ORDER BY CASE r.name WHEN 'accountant' THEN 0 WHEN 'tmc_member' THEN 1 ELSE 2 END, u.name ASC
    """, (exclude_user_id or 0,))
    return cursor.fetchall()


@app.route("/alerts/add", methods=["POST"])
@role_required('accountant', 'tmc_member')
def add_alert():
    alert_type = request.form.get("type", "alert")
    if alert_type not in ("alert", "query"):
        alert_type = "alert"
    department_id = request.form.get("department_id") or None
    expense_id = request.form.get("expense_id") or None
    title = request.form.get("title", "").strip()
    message = request.form.get("message", "").strip()
    severity = request.form.get("severity", "info")
    if severity not in ("info", "warning", "critical"):
        severity = "info"

    try:
        assigned_to = int(request.form.get("assigned_to") or 0) or None
    except ValueError:
        assigned_to = None

    if not title or not message:
        flash("Please provide a title and message.", "danger")
        return redirect(url_for('alerts'))
    if alert_type == "query" and assigned_to is None:
        # A query always goes to a named person. Left unaddressed, it would fall
        # back to department routing and land with that department's Manager.
        flash("Choose who the query should go to.", "danger")
        return redirect(url_for('alerts'))

    db = get_db()
    cursor = db.cursor()
    recipient = None
    if assigned_to is not None:
        recipient = next((r for r in _alert_recipients(cursor) if r['id'] == assigned_to), None)
        if recipient is None:
            db.close()
            flash("That recipient isn't an active user.", "danger")
            return redirect(url_for('alerts'))

    cursor.execute(
        "INSERT INTO alerts (type, department_id, expense_id, title, message, severity, created_by, assigned_to) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (alert_type, department_id, expense_id, title, message, severity, g.user['id'], assigned_to)
    )
    alert_id = cursor.lastrowid
    label = "query" if alert_type == "query" else "alert"
    to_text = f" to {recipient['name']}" if recipient else ""
    log_activity(cursor, g.user['id'], "create", "alert", alert_id, f"Raised {label}{to_text}: {title}")
    db.commit()
    db.close()

    flash(f"{'Query' if alert_type == 'query' else 'Alert'} sent{to_text}.", "success")
    return redirect(url_for('alerts'))


@app.route("/alerts/<int:id>/resolve", methods=["POST"])
@role_required('accountant')
def resolve_alert(id):
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT title FROM alerts WHERE id = ?", (id,))
    alert_row = cursor.fetchone()
    if alert_row:
        cursor.execute(
            "UPDATE alerts SET is_resolved = 1, resolved_by = ?, resolved_at = CURRENT_TIMESTAMP WHERE id = ?",
            (g.user['id'], id)
        )
        log_activity(cursor, g.user['id'], "resolve", "alert", id, f"Resolved: {alert_row['title']}")
        db.commit()
        flash("Marked as resolved.", "success")
    db.close()
    return redirect(url_for('alerts'))


@app.route("/alerts/<int:id>/delete", methods=["POST"])
@role_required('accountant')
def delete_alert(id):
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT title FROM alerts WHERE id = ?", (id,))
    alert_row = cursor.fetchone()
    if alert_row:
        cursor.execute("DELETE FROM alerts WHERE id = ?", (id,))
        log_activity(cursor, g.user['id'], "delete", "alert", id, f"Deleted: {alert_row['title']}")
        db.commit()
        flash("Alert deleted.", "info")
    db.close()
    return redirect(url_for('alerts'))


# ------------------------------------------------------------------ #
# Activity Log
# ------------------------------------------------------------------ #

@app.route("/activity-log")
@login_required
def activity_log():
    # The activity log is now shown as part of the merged Reports & Audit page —
    # this route stays in place only so old bookmarks/links keep working. Managers
    # never reach this far: enforce_manager_scope blocks 'activity_log' earlier,
    # since it's intentionally left out of MANAGER_ALLOWED_ENDPOINTS.
    return redirect(url_for('reports'))


# ------------------------------------------------------------------ #
# Reports
# ------------------------------------------------------------------ #

@app.route("/reports")
@login_required
def reports():
    db = get_db()
    cursor = db.cursor()
    departments_list = _visible_departments(cursor, g.user)

    # Managers never had access to the org-wide activity log (blocked by
    # enforce_manager_scope on the old /activity-log route) — preserve that by
    # simply not fetching or rendering it for them on the merged page either.
    entries = []
    if g.user['role'] != 'manager':
        cursor.execute("""
            SELECT al.*, u.name as user_name, r.name as user_role
            FROM audit_logs al
            LEFT JOIN users u ON al.user_id = u.id
            LEFT JOIN roles r ON u.role_id = r.id
            ORDER BY al.created_at DESC
            LIMIT 300
        """)
        entries = cursor.fetchall()

    db.close()

    now = datetime.now()
    default_start = now.replace(day=1).strftime("%Y-%m-%d")
    default_end = now.strftime("%Y-%m-%d")

    return render_template(
        "reports_audit.html",
        departments=departments_list,
        default_start=default_start,
        default_end=default_end,
        entries=entries,
    )


@app.route("/reports/export/pdf")
@login_required
def export_report_pdf():
    now = datetime.now()
    start_date = request.args.get("start_date", now.replace(day=1).strftime("%Y-%m-%d"))
    end_date = request.args.get("end_date", now.strftime("%Y-%m-%d"))
    department_id = request.args.get("department") or None

    # Managers can never request another department's report — the backend assigns
    # their own department_id, ignoring/overriding whatever the query string says.
    db = get_db()
    cursor = db.cursor()
    dept_ids = _manager_department_ids(cursor, g.user)
    db.close()
    if dept_ids is not None:
        if not dept_ids:
            flash("No department is assigned to your account. Contact an administrator.", "danger")
            return redirect(url_for('dashboard'))
        department_id = dept_ids[0]

    pdf_bytes = ReportService.generate_department_pdf(department_id, start_date, end_date, g.user['name'])

    filename = f"iskcon_shirpur_report_{start_date}_to_{end_date}.pdf"
    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


@app.route("/reports/export/csv")
@login_required
def export_report_csv():
    now = datetime.now()
    start_date = request.args.get("start_date", now.replace(day=1).strftime("%Y-%m-%d"))
    end_date = request.args.get("end_date", now.strftime("%Y-%m-%d"))
    department_id = request.args.get("department") or None

    # Same override as the PDF export — a Manager's own department, always.
    db = get_db()
    cursor = db.cursor()
    dept_ids = _manager_department_ids(cursor, g.user)
    db.close()
    if dept_ids is not None:
        if not dept_ids:
            flash("No department is assigned to your account. Contact an administrator.", "danger")
            return redirect(url_for('dashboard'))
        department_id = dept_ids[0]

    csv_text = ReportService.generate_csv(department_id, start_date, end_date)

    filename = f"iskcon_shirpur_report_{start_date}_to_{end_date}.csv"
    return Response(
        csv_text,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


# ------------------------------------------------------------------ #
# Donations (Accountant records, TMC Member views)
# ------------------------------------------------------------------ #

DONATION_PAYMENT_METHODS = ("Cash", "UPI", "Bank")
DONATION_WEBHOOK_TIMEOUT_SECONDS = 3


def _send_donation_webhook(payload):
    """POST a donation to the external project's API. Never raises — their API
    being offline, slow or broken must not undo or block recording the donation.
    Returns the status stored on the donation: 'sent', 'failed' or 'not configured'."""
    if not config.DONATIONS_WEBHOOK_URL:
        return "not configured"
    try:
        response = requests.post(config.DONATIONS_WEBHOOK_URL, json=payload,
                                 timeout=DONATION_WEBHOOK_TIMEOUT_SECONDS)
        response.raise_for_status()
        return "sent"
    except Exception as e:
        app.logger.error(f"Donation webhook to {config.DONATIONS_WEBHOOK_URL} failed: {e}")
        return "failed"


@app.route("/admin/donations")
@role_required('accountant', 'tmc_member')
def admin_donations():
    db = get_db()
    cursor = db.cursor()
    cursor.execute("""
        SELECT dn.*, u.name AS recorded_by_name FROM donations dn
        LEFT JOIN users u ON dn.recorded_by = u.id
        ORDER BY dn.created_at DESC, dn.id DESC
    """)
    donations = cursor.fetchall()
    db.close()
    return render_template("admin_donations.html", donations=donations,
                           payment_methods=DONATION_PAYMENT_METHODS)


@app.route("/admin/add_donation", methods=["POST"])
@role_required('accountant')
def add_donation():
    donor_name = request.form.get("donor_name", "").strip()
    payment_method = request.form.get("payment_method", "")
    transaction_id = request.form.get("transaction_id", "").strip()
    notes = request.form.get("notes", "").strip()
    dob_raw = request.form.get("dob", "").strip()
    try:
        amount = float(request.form.get("amount", ""))
    except ValueError:
        amount = None
    dob = None
    if dob_raw:
        try:
            dob = datetime.strptime(dob_raw, "%Y-%m-%d").date()
        except ValueError:
            dob = "invalid"

    error = None
    if not donor_name or len(donor_name) > 200:
        error = "Enter the donor's name (up to 200 characters)."
    elif dob == "invalid" or (dob and (dob > datetime.now().date() or dob.year < 1900)):
        error = "Enter a valid date of birth (not in the future)."
    elif amount is None or not math.isfinite(amount) or amount <= 0:
        error = "Enter an amount greater than zero."
    elif payment_method not in DONATION_PAYMENT_METHODS:
        error = "Choose a payment method: Cash, UPI or Bank."
    elif len(transaction_id) > 100 or len(notes) > 1000:
        error = "Transaction ID or notes are too long."
    if error:
        flash(error, "danger")
        return redirect(url_for('admin_donations'))

    # 1. Save the donation. Committed straight after the INSERT, before anything
    #    external happens, so nothing later can lose it.
    db = get_db()
    cursor = db.cursor()
    try:
        cursor.execute(
            "INSERT INTO donations (donor_name, amount, payment_method, transaction_id, notes, recorded_by, dob) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (donor_name, amount, payment_method, transaction_id or None, notes or None, g.user['id'],
             dob.isoformat() if dob else None),
        )
        donation_id = cursor.lastrowid
        db.commit()
    except Exception as e:
        db.rollback()
        db.close()
        app.logger.exception(f"Saving donation from {donor_name} failed: {e}")
        flash("The donation could not be saved to the database. Nothing was recorded — please try again.", "danger")
        return redirect(url_for('admin_donations'))

    # The audit entry is secondary: if it fails, the donation stays saved.
    try:
        log_activity(cursor, g.user['id'], "create", "donation", donation_id,
                     f"Logged ₹{amount:,.2f} donation from {donor_name} ({payment_method})")
        db.commit()
    except Exception as e:
        db.rollback()
        app.logger.error(f"Audit log for donation {donation_id} failed: {e}")
    db.close()

    # 2. Tell the external system. Never raises; a failure is only logged.
    status = _send_donation_webhook({
        "donor_name": donor_name,
        "amount": amount,
        "payment_method": payment_method,
        "transaction_id": transaction_id,
        "notes": notes,
    })

    # 3. Record whether it reached them. Also non-fatal — the donation is saved.
    try:
        db = get_db()
        cursor = db.cursor()
        cursor.execute("UPDATE donations SET webhook_status = ? WHERE id = ?", (status, donation_id))
        db.commit()
        db.close()
    except Exception as e:
        app.logger.error(f"Recording webhook status for donation {donation_id} failed: {e}")

    if status == "failed":
        flash(f"Donation of ₹{amount:,.2f} from {donor_name} recorded, but the external system couldn't be "
              f"reached — it's marked as not sent.", "warning")
    elif status == "sent":
        flash(f"Donation of ₹{amount:,.2f} from {donor_name} recorded and sent to the external system.", "success")
    else:
        flash(f"Donation of ₹{amount:,.2f} from {donor_name} recorded.", "success")
    return redirect(url_for('admin_donations'))


@app.route("/admin/donations/export/csv")
@role_required('accountant', 'tmc_member')
def export_donations_csv():
    # BOM so Excel opens the file as UTF-8 (names in Devanagari, the ₹ sign).
    csv_text = "\ufeff" + generate_donations_csv()
    filename = f"iskcon_shirpur_donations_{datetime.now().strftime('%Y-%m-%d')}.csv"
    return Response(csv_text, mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})


@app.route("/admin/donations/export/pdf")
@role_required('accountant', 'tmc_member')
def export_donations_pdf():
    pdf_bytes = generate_donations_pdf(generated_by_name=g.user['name'])
    filename = f"iskcon_shirpur_donations_{datetime.now().strftime('%Y-%m-%d')}.pdf"
    return Response(pdf_bytes, mimetype="application/pdf",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})


@app.route("/admin/donations/email_report", methods=["POST"])
@role_required('accountant')
def email_donations_report():
    ok, message = send_donations_report(generated_by_name=g.user['name'])
    if ok:
        db = get_db()
        cursor = db.cursor()
        log_activity(cursor, g.user['id'], "export", "donation", None, message)
        db.commit()
        db.close()
    else:
        app.logger.error(f"Manual donations report failed: {message}")
    flash(message, "success" if ok else "danger")
    return redirect(url_for('admin_donations'))


# ------------------------------------------------------------------ #
# Manage Users (Admin only)
# ------------------------------------------------------------------ #

@app.route("/users")
@role_required('accountant')
def manage_users():
    db = get_db()
    cursor = db.cursor()
    cursor.execute("""
        SELECT u.*, r.name as role FROM users u
        JOIN roles r ON u.role_id = r.id
        ORDER BY u.is_active DESC, r.name ASC, u.name ASC
    """)
    users_list = cursor.fetchall()
    db.close()
    return render_template("users.html", users=users_list)


@app.route("/users/<int:id>/approve", methods=["POST"])
@role_required('accountant')
def approve_user(id):
    role = request.form.get("role", "")
    if role not in ("manager", "tmc_member"):
        flash("Choose Manager or TMC Member to approve this user.", "danger")
        return redirect(url_for('dashboard'))

    # A Manager is scoped entirely by the department they manage
    # (departments.manager_id) and capped by that department's budget
    # (budgets.monthly_limit) — so both are required at approval, and stored
    # there rather than on the users row, where nothing else would read them.
    # This is the only place departments can be created (the standalone
    # "New Department" form was removed): an unknown name creates one.
    department_name = request.form.get("department_name", "").strip()
    allocated_budget = None
    if role == "manager":
        if not department_name or len(department_name) > 100:
            flash("Enter a department name (up to 100 characters) to approve a Manager.", "danger")
            return redirect(url_for('dashboard'))
        try:
            allocated_budget = float(request.form.get("budget", ""))
        except ValueError:
            allocated_budget = None
        if allocated_budget is None or not math.isfinite(allocated_budget) or allocated_budget <= 0:
            flash("Enter a budget limit greater than zero to approve a Manager.", "danger")
            return redirect(url_for('dashboard'))

    db = get_db()
    cursor = db.cursor()
    cursor.execute("""
        SELECT u.id, u.name, u.email, u.clerk_id, r.name as role FROM users u
        JOIN roles r ON u.role_id = r.id
        WHERE u.id = ?
    """, (id,))
    target = cursor.fetchone()
    if not target or target['role'] != 'pending':
        db.close()
        flash("That user is not awaiting approval.", "warning")
        return redirect(url_for('dashboard'))

    dept_id = None
    dept_label = department_name
    if role == 'manager':
        # Case-insensitive match, so "prasadam" assigns the existing "Prasadam"
        # instead of creating a near-duplicate department.
        cursor.execute("SELECT id, name, manager_id, is_active FROM departments WHERE LOWER(name) = LOWER(?)",
                       (department_name,))
        dept = cursor.fetchone()
        if dept:
            dept_label = dept['name']
            if not dept['is_active']:
                db.close()
                flash(f"'{dept['name']}' is deactivated. Reactivate it on the Departments page first, "
                      f"or enter a different department name.", "danger")
                return redirect(url_for('dashboard'))
            if dept['manager_id'] and dept['manager_id'] != id:
                cursor.execute("SELECT name, is_active FROM users WHERE id = ?", (dept['manager_id'],))
                current = cursor.fetchone()
                if current and current['is_active']:
                    db.close()
                    flash(f"'{dept['name']}' is already managed by {current['name']}. Reassign it on the "
                          f"Departments page first, or enter a different department name.", "danger")
                    return redirect(url_for('dashboard'))
            dept_id = dept['id']
            cursor.execute("UPDATE departments SET manager_id = ? WHERE id = ?", (id, dept_id))
        else:
            cursor.execute("INSERT INTO departments (name, created_by, manager_id) VALUES (?, ?, ?)",
                           (department_name, g.user['id'], id))
            dept_id = cursor.lastrowid
            log_activity(cursor, g.user['id'], "create", "department", dept_id,
                         f"Created department '{department_name}' while approving {target['name']}")

        cursor.execute("""
            INSERT INTO budgets (department_id, monthly_limit, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(department_id) DO UPDATE SET monthly_limit = excluded.monthly_limit, updated_at = CURRENT_TIMESTAMP
        """, (dept_id, allocated_budget))
        log_activity(cursor, g.user['id'], "update", "budget", dept_id,
                     f"Set monthly budget of ₹{allocated_budget:,.2f} for '{dept_label}'")

    cursor.execute("UPDATE users SET role_id = ? WHERE id = ?", (_role_id(cursor, role), id))
    detail = f" for '{dept_label}' with a ₹{allocated_budget:,.2f} monthly budget" if role == 'manager' else ""
    log_activity(cursor, g.user['id'], "update", "user", id,
                 f"Approved {target['name']} ({target['email']}) as {role}{detail}")
    db.commit()
    db.close()

    if role == 'manager':
        flash(f"{target['name']} approved as Manager of {dept_label} "
              f"(monthly budget ₹{allocated_budget:,.2f}).", "success")
    else:
        flash(f"{target['name']} approved as TMC Member (read-only).", "success")
    return redirect(url_for('dashboard'))


@app.route("/users/<int:id>/edit", methods=["POST"])
@role_required('accountant')
def edit_user(id):
    name = request.form.get("name", "").strip()
    role = request.form.get("role", "tmc_member")
    phone = request.form.get("phone", "").strip()
    is_active = 1 if request.form.get("is_active") == "on" else 0

    if role not in ("accountant", "tmc_member", "manager"):
        role = "tmc_member"

    db = get_db()
    cursor = db.cursor()
    cursor.execute("""
        SELECT u.*, r.name as role FROM users u
        JOIN roles r ON u.role_id = r.id
        WHERE u.id = ?
    """, (id,))
    target = cursor.fetchone()
    if not target:
        db.close()
        flash("User not found.", "danger")
        return redirect(url_for('manage_users'))

    demoting_last_admin = target['role'] == 'accountant' and (role != 'accountant' or is_active == 0)
    if demoting_last_admin and _count_active_admins(cursor, exclude_user_id=id) == 0:
        db.close()
        flash("Cannot remove the last active Accountant account.", "danger")
        return redirect(url_for('manage_users'))

    cursor.execute(
        "UPDATE users SET name = ?, role_id = ?, phone_number = ?, is_active = ? WHERE id = ?",
        (name, _role_id(cursor, role), phone, is_active, id)
    )
    log_activity(cursor, g.user['id'], "update", "user", id, f"Updated user '{name}' (role={role}, active={bool(is_active)})")
    db.commit()
    db.close()

    flash("User account updated.", "success")
    return redirect(url_for('manage_users'))


@app.route("/users/<int:id>/reset-password", methods=["POST"])
@role_required('accountant')
def admin_reset_user_password(id):
    if config.CLERK_ENABLED:
        # Clerk owns credentials entirely when enabled — there's no local
        # password_hash to reset. The button that hit this route is hidden
        # in users.html when clerk_enabled; this is the server-side backstop.
        flash("Password resets aren't applicable — this account signs in via Clerk.", "info")
        return redirect(url_for('manage_users'))

    new_password = request.form.get("new_password", "")
    if len(new_password) < 6:
        flash("Password must be at least 6 characters long.", "danger")
        return redirect(url_for('manage_users'))

    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT name FROM users WHERE id = ?", (id,))
    target = cursor.fetchone()
    if target:
        hashed = hash_password(new_password)
        cursor.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hashed, id))
        log_activity(cursor, g.user['id'], "update", "user", id, f"Reset password for '{target['name']}'")
        db.commit()
        flash(f"Password reset for {target['name']}.", "success")
    db.close()
    return redirect(url_for('manage_users'))


def _load_user_with_role(cursor, user_id):
    cursor.execute("""
        SELECT u.*, r.name as role FROM users u
        JOIN roles r ON u.role_id = r.id
        WHERE u.id = ?
    """, (user_id,))
    return cursor.fetchone()


@app.route("/toggle_user_status/<int:user_id>", methods=["POST"])
@role_required('accountant')
def toggle_user_status(user_id):
    """Activate or deactivate an account. The form may send status=active or
    status=inactive (so a double-click can't flip it back); without it, flips."""
    db = get_db()
    cursor = db.cursor()
    target = _load_user_with_role(cursor, user_id)
    if not target:
        db.close()
        flash("User not found.", "danger")
        return redirect(request.referrer or url_for('manage_users'))

    wanted = request.form.get("status")
    new_active = {"active": 1, "inactive": 0}.get(wanted, 0 if target['is_active'] else 1)

    if new_active == 0:
        if user_id == g.user['id']:
            db.close()
            flash("You can't deactivate your own account.", "danger")
            return redirect(request.referrer or url_for('manage_users'))
        if target['role'] == 'accountant' and _count_active_admins(cursor, exclude_user_id=user_id) == 0:
            db.close()
            flash("Cannot deactivate the last active Accountant account.", "danger")
            return redirect(request.referrer or url_for('manage_users'))

    cursor.execute("UPDATE users SET is_active = ? WHERE id = ?", (new_active, user_id))
    action = "activate" if new_active else "deactivate"
    log_activity(cursor, g.user['id'], action, "user", user_id, f"{action.capitalize()}d user '{target['name']}'")
    db.commit()
    db.close()

    flash(f"{target['name']}'s account was {action}d.", "success" if new_active else "info")
    return redirect(request.referrer or url_for('manage_users'))


@app.route("/delete_user/<int:user_id>", methods=["POST"])
@role_required('accountant')
def delete_user(user_id):
    """Permanently remove an account. Their expenses, alerts and log entries
    are kept, with the person shown as removed (the foreign keys SET NULL)."""
    db = get_db()
    cursor = db.cursor()
    target = _load_user_with_role(cursor, user_id)
    if not target:
        db.close()
        flash("User not found.", "danger")
        return redirect(url_for('manage_users'))

    if user_id == g.user['id']:
        db.close()
        flash("You can't delete your own account.", "danger")
        return redirect(url_for('manage_users'))
    if target['role'] == 'accountant' and _count_active_admins(cursor, exclude_user_id=user_id) == 0:
        db.close()
        flash("Cannot delete the last active Accountant account.", "danger")
        return redirect(url_for('manage_users'))

    cursor.execute("DELETE FROM users WHERE id = ?", (user_id,))
    log_activity(cursor, g.user['id'], "delete", "user", user_id,
                 f"Deleted user '{target['name']}' ({target['email']}, {target['role']})")
    db.commit()
    db.close()

    flash(f"{target['name']}'s account was deleted.", "info")
    return redirect(url_for('manage_users'))


# ------------------------------------------------------------------ #
# Profile & Settings
# ------------------------------------------------------------------ #

@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    user_id = g.user['id']
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        phone = request.form.get("phone", "").strip()
        whatsapp_notify_mode = request.form.get("whatsapp_notify_mode", "large_only")
        if whatsapp_notify_mode not in ("all", "large_only", "off"):
            whatsapp_notify_mode = "large_only"

        current_password = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")

        db = get_db()
        cursor = db.cursor()

        if new_password or confirm_password or current_password:
            if not verify_password(g.user['password_hash'], current_password):
                db.close()
                flash("Current password is incorrect.", "danger")
                return redirect(url_for('profile'))
            if len(new_password) < 6:
                db.close()
                flash("New password must be at least 6 characters long.", "danger")
                return redirect(url_for('profile'))
            if new_password != confirm_password:
                db.close()
                flash("New password and confirmation do not match.", "danger")
                return redirect(url_for('profile'))
            cursor.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(new_password), user_id))

        cursor.execute(
            "UPDATE users SET name = ?, phone_number = ?, whatsapp_notify_mode = ? WHERE id = ?",
            (name, phone, whatsapp_notify_mode, user_id)
        )
        db.commit()
        db.close()

        session['user_name'] = name
        flash("Profile updated successfully.", "success")
        return redirect(url_for('profile'))

    db = get_db()
    cursor = db.cursor()
    cursor.execute("""
        SELECT u.*, r.name as role FROM users u
        JOIN roles r ON u.role_id = r.id
        WHERE u.id = ?
    """, (user_id,))
    user_data = cursor.fetchone()
    db.close()

    return render_template("profile.html", user=user_data)


# ------------------------------------------------------------------ #
# Background Scheduler
# ------------------------------------------------------------------ #
# Explicitly opt-in via START_SCHEDULER=true (unset in tests and in most local dev,
# so `pytest` importing this module never starts a stray background thread).
# Placed at module level — not inside `if __name__ == "__main__"` — so it actually
# runs under a production WSGI server too (gunicorn imports this module; it never
# executes the __main__ block). Run gunicorn with a single worker (see Procfile) so
# this only ever starts once, matching the sqlite-friendly single-writer design.
if os.environ.get("START_SCHEDULER", "").lower() == "true":
    if not app.debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        from services.scheduler_service import start_scheduler
        start_scheduler(app, receipt_dir=RECEIPT_DIR)


# ------------------------------------------------------------------ #
# App Runner (local dev only — production uses gunicorn, see Procfile)
# ------------------------------------------------------------------ #

if __name__ == "__main__":
    app.run(debug=True, port=5001)
