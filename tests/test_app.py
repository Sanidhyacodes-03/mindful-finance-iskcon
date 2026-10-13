import pytest
from datetime import datetime
from app import app
from database.db import init_db, seed_db, get_db
from services.email_service import EmailService
from services.whatsapp_service import WhatsAppService
from services.security import hash_password
from services.scheduler_service import generate_month_end_alerts

ADMIN_EMAIL = "admin@iskconshirpur.org"
ADMIN_PASSWORD = "Admin@123"
AUDITOR_EMAIL = "auditor@iskconshirpur.org"
AUDITOR_PASSWORD = "Auditor@123"
MANAGER_EMAIL = "manager@iskconshirpur.org"
MANAGER_PASSWORD = "Manager@123"


@pytest.fixture
def client(monkeypatch):
    # This suite exercises the local email/password auth path (login() below
    # POSTs email/password directly) — deliberately force that path regardless
    # of whatever's in a developer's real .env. Without this, a real
    # CLERK_SECRET_KEY/CLERK_ORG_ID sitting in .env would make load_logged_in_user()
    # always take the Clerk branch, silently ignoring the session cookie this
    # suite sets via local login and making every "logged in" assertion fail —
    # not a bug in the app, just tests that were never meant to depend on
    # whichever real third-party credentials happen to be configured locally.
    import config
    monkeypatch.setattr(config, "CLERK_ENABLED", False)
    monkeypatch.setattr(config, "DONATIONS_WEBHOOK_URL", "")

    # Same reasoning, for the database: db.py imports IS_POSTGRES via
    # "from config import IS_POSTGRES" (a one-time name binding), so patching
    # config.IS_POSTGRES here wouldn't reach it — and _postgres_available()'s
    # result is cached process-wide in _pg_available once any test touches the
    # DB, so merely patching IS_POSTGRES wouldn't un-cache an already-true
    # result either. Forcing the cache itself guarantees every test hits local
    # SQLite, never a real configured Supabase database — found the hard way:
    # this suite was silently running against and writing test data into a
    # live Postgres database once DATABASE_URL became reachable again.
    import database.db as dbmod
    monkeypatch.setattr(dbmod, "_pg_available", False)

    app.config['TESTING'] = True
    app.config['SECRET_KEY'] = 'test-secret'
    app.config['WTF_CSRF_ENABLED'] = False
    with app.app_context():
        init_db()
        seed_db()
    with app.test_client() as client:
        yield client


def login(client, email, password):
    return client.post('/login', data=dict(email=email, password=password), follow_redirects=True)


def test_landing_page(client):
    rv = client.get('/')
    assert rv.status_code == 200
    assert b"ISKCON Shirpur" in rv.data


def test_login_admin(client):
    rv = login(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    assert rv.status_code == 200
    assert b"Accountant Overview" in rv.data


def test_login_auditor(client):
    rv = login(client, AUDITOR_EMAIL, AUDITOR_PASSWORD)
    assert rv.status_code == 200
    assert b"TMC Member Overview" in rv.data


def test_login_manager(client):
    rv = login(client, MANAGER_EMAIL, MANAGER_PASSWORD)
    assert rv.status_code == 200
    assert b"Manager Overview" in rv.data


def test_nobody_can_create_a_department(client):
    for email, password in [(ADMIN_EMAIL, ADMIN_PASSWORD), (AUDITOR_EMAIL, AUDITOR_PASSWORD)]:
        login(client, email, password)
        client.post('/departments/add', data=dict(name="Forced Department", icon="X", color="#000000"))
        client.get('/logout')

    login(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    assert client.post('/departments/add', data=dict(name="Forced Department")).status_code == 404
    rv = client.get('/departments')
    assert b"New Department" not in rv.data

    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT id FROM departments WHERE name = ?", ("Forced Department",))
    assert cursor.fetchone() is None
    db.close()


def test_auditor_cannot_add_expense_but_can_raise_alert(client):
    login(client, AUDITOR_EMAIL, AUDITOR_PASSWORD)

    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT id FROM departments LIMIT 1")
    dept_id = cursor.fetchone()['id']
    db.close()

    rv = client.post('/expenses/add', data=dict(
        amount="100",
        description="Should be blocked",
        department_id=dept_id,
        date="2026-09-01",
        payment_method="Cash"
    ), follow_redirects=True)
    assert rv.status_code == 200
    assert b"read-only access" in rv.data

    # Auditors may now raise alerts (this is new — previously admin-only).
    rv2 = client.post('/alerts/add', data=dict(
        type="alert",
        title="Auditor Raised Alert",
        message="Auditors can now create alerts",
        severity="info"
    ), follow_redirects=True)
    assert rv2.status_code == 200
    assert b"Auditor Raised Alert" in rv2.data

    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT id FROM alerts WHERE title = ?", ("Auditor Raised Alert",))
    assert cursor.fetchone() is not None
    db.close()


def test_manager_scoped_to_own_department(client):
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT id, name FROM departments WHERE manager_id = (SELECT id FROM users WHERE email = ?)", (MANAGER_EMAIL,))
    own_dept = cursor.fetchone()
    cursor.execute("SELECT id, name FROM departments WHERE id != ? LIMIT 1", (own_dept['id'],))
    other_dept_row = cursor.fetchone()
    other_dept_id = other_dept_row['id']
    other_dept_id_name = other_dept_row['name']
    db.close()

    login(client, MANAGER_EMAIL, MANAGER_PASSWORD)

    # Blocked from org-wide pages entirely.
    for path in ('/departments', '/users', '/activity-log'):
        rv = client.get(path, follow_redirects=True)
        assert rv.status_code == 200
        assert b"scoped to your assigned department" in rv.data

    # Reports IS accessible to managers, but must always be forced to their own
    # department — a hand-edited ?department=<other id> must be silently overridden.
    rv_page = client.get('/reports')
    assert rv_page.status_code == 200
    rv_pdf = client.get(f'/reports/export/pdf?start_date=2026-01-01&end_date=2026-12-31&department={other_dept_id}')
    assert rv_pdf.status_code == 200
    assert rv_pdf.mimetype == "application/pdf"
    rv_csv = client.get(f'/reports/export/csv?start_date=2026-01-01&end_date=2026-12-31&department={other_dept_id}')
    assert rv_csv.status_code == 200
    assert other_dept_id_name.encode() not in rv_csv.data
    assert own_dept['name'].encode() in rv_csv.data

    # Can add an expense to their own department.
    rv = client.post('/expenses/add', data=dict(
        amount="500",
        description="Manager own department expense",
        department_id=own_dept['id'],
        date="2026-09-01",
        payment_method="Cash"
    ), follow_redirects=True)
    assert rv.status_code == 200
    assert b"Manager own department expense" in rv.data

    # A foreign department_id is silently overridden, never trusted — the backend
    # always assigns the manager's own department regardless of what's submitted.
    rv2 = client.post('/expenses/add', data=dict(
        amount="500",
        description="Spoofed department attempt",
        department_id=other_dept_id,
        date="2026-09-01",
        payment_method="Cash"
    ), follow_redirects=True)
    assert rv2.status_code == 200

    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT department_id FROM expenses WHERE description = ?", ("Spoofed department attempt",))
    row = cursor.fetchone()
    assert row is not None
    assert row['department_id'] == own_dept['id']
    db.close()


def test_both_roles_can_download_reports(client):
    for email, password in [(ADMIN_EMAIL, ADMIN_PASSWORD), (AUDITOR_EMAIL, AUDITOR_PASSWORD)]:
        login(client, email, password)
        rv_pdf = client.get('/reports/export/pdf?start_date=2026-01-01&end_date=2026-12-31')
        assert rv_pdf.status_code == 200
        assert rv_pdf.mimetype == "application/pdf"

        rv_csv = client.get('/reports/export/csv?start_date=2026-01-01&end_date=2026-12-31')
        assert rv_csv.status_code == 200
        assert rv_csv.mimetype == "text/csv"
        client.get('/logout')


def test_activity_log_records_admin_actions(client):
    login(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    client.post('/admin/add_donation', data=dict(donor_name="Logged Donor", amount="101", payment_method="Cash"))
    # The activity log now lives on the merged Reports & Audit page; /activity-log
    # is kept only as a redirect there for old bookmarks/links.
    rv = client.get('/activity-log', follow_redirects=True)
    assert rv.status_code == 200
    assert b"Logged Donor" in rv.data


def test_only_admin_can_reach_manage_users(client):
    login(client, AUDITOR_EMAIL, AUDITOR_PASSWORD)
    rv = client.get('/users', follow_redirects=True)
    assert rv.status_code == 200
    assert b"permission" in rv.data


def test_departments_view_shows_manager(client):
    login(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    rv = client.get('/departments')
    assert rv.status_code == 200
    assert b"Suresh Iyer" in rv.data
    assert b"manager@iskconshirpur.org" in rv.data


def test_forgot_and_reset_password(client):
    rv = client.post('/forgot-password', data=dict(
        email=ADMIN_EMAIL
    ), follow_redirects=True)
    assert rv.status_code == 200

    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT reset_token FROM users WHERE email = ?", (ADMIN_EMAIL,))
    token = cursor.fetchone()['reset_token']
    db.close()

    assert token is not None

    rv2 = client.post(f'/reset-password/{token}', data=dict(
        password='newpassword123'
    ), follow_redirects=True)
    assert rv2.status_code == 200
    assert b"Password updated successfully" in rv2.data

    # restore original password so other tests / fixtures relying on it still work
    db = get_db()
    cursor = db.cursor()
    cursor.execute("UPDATE users SET password_hash = ? WHERE email = ?", (hash_password(ADMIN_PASSWORD), ADMIN_EMAIL))
    db.commit()
    db.close()


def test_generate_month_end_alerts_is_idempotent(client):
    # ref_date in the middle of a month so "last month" is fully in the past and has seeded expenses.
    ref_date = datetime.now().replace(day=15)

    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT COUNT(*) FROM departments WHERE is_active = 1")
    active_dept_count = cursor.fetchone()[0]
    db.close()

    created_first = generate_month_end_alerts(ref_date=ref_date)
    assert len(created_first) == active_dept_count

    created_second = generate_month_end_alerts(ref_date=ref_date)
    assert created_second == []


def test_email_service_dispatcher():
    res = EmailService.send_welcome_email("test@example.com", "Test User")
    assert res is True


def test_whatsapp_service_dispatcher():
    res = WhatsAppService.notify_login("+919876543210", "Test User")
    assert res is True


# ------------------------------------------------------------------ #
# Pending-approval workflow
# ------------------------------------------------------------------ #

def _make_pending_user(email, password="Pending@123", name="Pending Person"):
    db = get_db()
    cursor = db.cursor()
    cursor.execute(
        "INSERT INTO users (name, email, password_hash, role_id) "
        "VALUES (?, ?, ?, (SELECT id FROM roles WHERE name = 'pending'))",
        (name, email, hash_password(password)),
    )
    user_id = cursor.lastrowid
    db.commit()
    db.close()
    return user_id


def _role_of(user_id):
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT r.name FROM users u JOIN roles r ON u.role_id = r.id WHERE u.id = ?", (user_id,))
    role = cursor.fetchone()[0]
    db.close()
    return role


def test_pending_user_is_locked_out_of_everything(client):
    _make_pending_user("locked.out@example.com")
    login(client, "locked.out@example.com", "Pending@123")

    # Every app route — pages and POST endpoints alike — bounces to the holding page.
    for path in ('/dashboard', '/finances', '/expenses', '/departments', '/reports', '/alerts', '/users'):
        rv = client.get(path)
        assert rv.status_code == 302, path
        assert rv.headers['Location'].endswith('/pending'), path

    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT id FROM departments LIMIT 1")
    dept_id = cursor.fetchone()['id']
    db.close()
    rv = client.post('/expenses/add', data=dict(
        amount="999", description="Pending user write attempt", department_id=dept_id,
        date="2026-09-01", payment_method="Cash"))
    assert rv.headers['Location'].endswith('/pending')

    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT id FROM expenses WHERE description = ?", ("Pending user write attempt",))
    assert cursor.fetchone() is None
    db.close()

    rv = client.get('/pending')
    assert rv.status_code == 200
    assert b"pending Accountant approval" in rv.data
    # Navbar must not advertise the app's pages to a pending user.
    assert b'title="Finances"' not in rv.data


def test_admin_approves_pending_user(client):
    pending_id = _make_pending_user("approve.me@example.com", name="Approve Me")
    login(client, ADMIN_EMAIL, ADMIN_PASSWORD)

    rv = client.get('/dashboard')
    assert b"Pending Access Requests" in rv.data
    assert b"approve.me@example.com" in rv.data

    # Approval only grants Manager or Auditor — never Admin.
    client.post(f'/users/{pending_id}/approve', data=dict(role="accountant"))
    assert _role_of(pending_id) == 'pending'

    rv = client.post(f'/users/{pending_id}/approve', data=dict(role="tmc_member"), follow_redirects=True)
    assert rv.status_code == 200
    assert _role_of(pending_id) == 'tmc_member'

    # Already approved — a second approval is refused rather than silently changing the role.
    client.post(f'/users/{pending_id}/approve', data=dict(role="manager"))
    assert _role_of(pending_id) == 'tmc_member'


def test_non_admin_cannot_approve(client):
    pending_id = _make_pending_user("not.by.auditor@example.com")
    login(client, AUDITOR_EMAIL, AUDITOR_PASSWORD)
    client.post(f'/users/{pending_id}/approve', data=dict(role="manager"))
    assert _role_of(pending_id) == 'pending'


def test_clerk_first_sign_in_provisions_pending_user(client, monkeypatch):
    import services.clerk_auth as clerk_auth
    from services.clerk_auth import load_clerk_user

    identities = {
        "user_new": ("brand.new@example.com", "Brand New"),
        "user_invited": (ADMIN_EMAIL, "Ignored Name"),
    }
    monkeypatch.setattr(clerk_auth, "_fetch_clerk_identity", lambda uid: identities[uid])

    db = get_db()
    cursor = db.cursor()

    # Unknown person -> new pending row, linked to their Clerk id.
    user = load_clerk_user(db, cursor, {"sub": "user_new"})
    assert user['role'] == 'pending'
    assert user['email'] == "brand.new@example.com"
    assert user['clerk_id'] == "user_new"

    # Second request resolves the same row instead of inserting a duplicate.
    again = load_clerk_user(db, cursor, {"sub": "user_new"})
    assert again['id'] == user['id']
    cursor.execute("SELECT COUNT(*) FROM users WHERE email = ?", ("brand.new@example.com",))
    assert cursor.fetchone()[0] == 1

    # An Admin-created row with no clerk_id is linked and keeps its role — not made pending.
    linked = load_clerk_user(db, cursor, {"sub": "user_invited"})
    assert linked['role'] == 'accountant'

    # A rejected (deactivated) user is never re-provisioned.
    cursor.execute("UPDATE users SET is_active = 0 WHERE id = ?", (user['id'],))
    db.commit()
    assert load_clerk_user(db, cursor, {"sub": "user_new"}) is None
    db.close()


def test_clerk_access_no_longer_requires_org_membership(monkeypatch):
    import werkzeug.exceptions
    from flask import g
    import config
    import services.clerk_auth as clerk_auth

    monkeypatch.setattr(config, "CLERK_SECRET_KEY", "sk_test_dummy")
    # A real signed-in Clerk session that has NO organization in its claims.
    monkeypatch.setattr(clerk_auth, "authenticate_clerk_request", lambda req: {"sub": "user_x"})

    guarded = clerk_auth.clerk_org_required(lambda: "allowed")

    with app.test_request_context('/finances'):
        g.user = {"id": 1, "role": "tmc_member"}
        assert guarded() == "allowed"

    # Rejected/deactivated or unknown identity (no local row) is still refused.
    with app.test_request_context('/finances'):
        g.user = None
        with pytest.raises(werkzeug.exceptions.Forbidden):
            guarded()


def _dept_row(name):
    db = get_db()
    cursor = db.cursor()
    cursor.execute("""
        SELECT d.id, d.name, d.manager_id, b.monthly_limit FROM departments d
        LEFT JOIN budgets b ON b.department_id = d.id
        WHERE d.name = ?
    """, (name,))
    row = cursor.fetchone()
    db.close()
    return row


def _make_department(name, manager_email=None):
    db = get_db()
    cursor = db.cursor()
    manager_id = None
    if manager_email:
        cursor.execute("SELECT id FROM users WHERE email = ?", (manager_email,))
        manager_id = cursor.fetchone()['id']
    cursor.execute("INSERT INTO departments (name, manager_id) VALUES (?, ?)", (name, manager_id))
    dept_id = cursor.lastrowid
    db.commit()
    db.close()
    return dept_id


def test_manager_approval_requires_department_and_budget(client):
    pending_id = _make_pending_user("needs.dept@example.com")
    login(client, ADMIN_EMAIL, ADMIN_PASSWORD)

    client.post(f'/users/{pending_id}/approve', data=dict(role="manager", budget="5000"))
    assert _role_of(pending_id) == 'pending'
    client.post(f'/users/{pending_id}/approve', data=dict(role="manager", department_name="Gaushala"))
    assert _role_of(pending_id) == 'pending'
    client.post(f'/users/{pending_id}/approve',
                data=dict(role="manager", department_name="Gaushala", budget="-10"))
    assert _role_of(pending_id) == 'pending'
    assert _dept_row("Gaushala") is None


def test_manager_approval_creates_department_when_name_is_new(client):
    pending_id = _make_pending_user("new.dept.mgr@example.com", password="Pending@123", name="Gopal Das")
    login(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    client.post(f'/users/{pending_id}/approve',
                data=dict(role="manager", department_name="Book Distribution", budget="15000"))

    assert _role_of(pending_id) == 'manager'
    dept = _dept_row("Book Distribution")
    assert dept['manager_id'] == pending_id
    assert dept['monthly_limit'] == 15000

    # The new Manager sees their own department and budget, not generic wording.
    client.get('/logout')
    login(client, "new.dept.mgr@example.com", "Pending@123")
    rv = client.get('/dashboard')
    assert b"Book Distribution" in rv.data
    assert "₹15,000.00".encode() in rv.data
    assert b"Across all departments" not in rv.data


def test_manager_approval_assigns_existing_department_case_insensitively(client):
    _make_department("Kitchen Seva")
    pending_id = _make_pending_user("existing.dept.mgr@example.com")
    login(client, ADMIN_EMAIL, ADMIN_PASSWORD)

    # Unassigned departments are suggested in the text box.
    assert b'<option value="Kitchen Seva">' in client.get('/dashboard').data

    client.post(f'/users/{pending_id}/approve',
                data=dict(role="manager", department_name="kitchen seva", budget="8000"))
    assert _role_of(pending_id) == 'manager'
    dept = _dept_row("Kitchen Seva")
    assert dept['manager_id'] == pending_id
    assert dept['monthly_limit'] == 8000
    assert _dept_row("kitchen seva") is None  # no near-duplicate created


def test_manager_approval_refuses_department_with_active_manager(client):
    _make_department("Taken Seva", manager_email=MANAGER_EMAIL)
    pending_id = _make_pending_user("wants.taken.dept@example.com")
    login(client, ADMIN_EMAIL, ADMIN_PASSWORD)

    # Not suggested...
    assert b'<option value="Taken Seva">' not in client.get('/dashboard').data
    # ...and refused if typed anyway.
    client.post(f'/users/{pending_id}/approve',
                data=dict(role="manager", department_name="Taken Seva", budget="1000"))
    assert _role_of(pending_id) == 'pending'
    assert _dept_row("Taken Seva")['manager_id'] != pending_id


# ------------------------------------------------------------------ #
# Donations
# ------------------------------------------------------------------ #

def _donation(donor_name):
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT * FROM donations WHERE donor_name = ?", (donor_name,))
    row = cursor.fetchone()
    db.close()
    return row


def test_admin_records_donation_and_sends_it_to_webhook(client, monkeypatch):
    import config
    import app as app_module
    monkeypatch.setattr(config, "DONATIONS_WEBHOOK_URL", "https://partner.example/webhook")
    sent = {}

    class OkResponse:
        def raise_for_status(self):
            pass

    def fake_post(url, json, timeout):
        sent.update(url=url, json=json, timeout=timeout)
        return OkResponse()

    monkeypatch.setattr(app_module.requests, "post", fake_post)
    login(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    rv = client.post('/admin/add_donation', data=dict(
        donor_name="Hari Prasad", amount="2501", payment_method="UPI",
        transaction_id="UPI123", notes="Janmashtami"), follow_redirects=True)

    assert rv.status_code == 200
    assert sent["url"] == "https://partner.example/webhook"
    assert sent["json"] == {"donor_name": "Hari Prasad", "amount": 2501.0, "payment_method": "UPI",
                            "transaction_id": "UPI123", "notes": "Janmashtami"}
    assert sent["timeout"] > 0
    assert _donation("Hari Prasad")['webhook_status'] == 'sent'
    assert b"Hari Prasad" in rv.data and "₹2,501.00".encode() in rv.data


def test_donation_is_kept_when_webhook_is_offline(client, monkeypatch):
    import config
    import requests
    import app as app_module
    monkeypatch.setattr(config, "DONATIONS_WEBHOOK_URL", "https://partner.example/webhook")

    def offline(*args, **kwargs):
        raise requests.ConnectionError("partner API down")

    monkeypatch.setattr(app_module.requests, "post", offline)
    login(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    rv = client.post('/admin/add_donation', data=dict(
        donor_name="Offline Donor", amount="500", payment_method="Cash"), follow_redirects=True)

    assert rv.status_code == 200
    assert b"be reached" in rv.data
    assert _donation("Offline Donor")['webhook_status'] == 'failed'


def test_donation_validation_and_admin_only(client):
    login(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    for bad in [dict(donor_name="", amount="100", payment_method="Cash"),
                dict(donor_name="Bad Amount", amount="-5", payment_method="Cash"),
                dict(donor_name="Bad Method", amount="100", payment_method="Cheque")]:
        client.post('/admin/add_donation', data=bad)
    assert _donation("Bad Amount") is None and _donation("Bad Method") is None
    client.get('/logout')

    for email, password in [(AUDITOR_EMAIL, AUDITOR_PASSWORD), (MANAGER_EMAIL, MANAGER_PASSWORD)]:
        login(client, email, password)
        assert b"Record Donation" not in client.get('/admin/donations', follow_redirects=True).data
        client.post('/admin/add_donation', data=dict(donor_name="Sneaky", amount="1", payment_method="Cash"))
        client.get('/logout')
    assert _donation("Sneaky") is None


def _user_id(email):
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT id FROM users WHERE email = ?", (email,))
    row = cursor.fetchone()
    db.close()
    return row['id'] if row else None


def _sign_clerk_token(private_key, **overrides):
    import time
    import jwt
    now = int(time.time())
    claims = {"sub": "user_abc", "sid": "sess_1", "azp": "http://localhost", "iat": now, "nbf": now, "exp": now + 60}
    claims.update(overrides)
    return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "ins_test"})


def test_clerk_session_token_is_verified_locally_without_network(monkeypatch):
    import time
    import requests
    from flask import g
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    import config
    import services.clerk_auth as clerk_auth

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_pem = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setattr(config, "CLERK_SECRET_KEY", "sk_test_dummy")
    monkeypatch.setattr(config, "CLERK_JWT_KEY", public_pem)

    def no_network(*args, **kwargs):
        raise AssertionError("verification must not call Clerk")
    monkeypatch.setattr(requests, "get", no_network)
    monkeypatch.setattr(requests, "post", no_network)

    def check(token):
        with app.test_request_context('/finances', headers={"Cookie": f"__session={token}"}):
            claims = clerk_auth.authenticate_clerk_request(clerk_auth.request)
            return claims, g.clerk_token_expired

    claims, expired = check(_sign_clerk_token(key))
    assert claims["sub"] == "user_abc" and not expired

    now = int(time.time())
    claims, expired = check(_sign_clerk_token(key, iat=now - 120, nbf=now - 120, exp=now - 60))
    assert claims is None and expired

    assert check(_sign_clerk_token(other_key)) == (None, False)                    # forged signature
    assert check(_sign_clerk_token(key, azp="https://evil.example")) == (None, False)  # other site
    assert check("not-a-jwt") == (None, False)


def test_clerk_signing_keys_are_fetched_once_then_cached(monkeypatch):
    import requests
    from cryptography.hazmat.primitives.asymmetric import rsa
    from jwt.algorithms import RSAAlgorithm
    import json
    import config
    import services.clerk_auth as clerk_auth

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid="ins_test", alg="RS256", use="sig")
    calls = []

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"keys": [jwk]}

    def fake_get(url, headers, timeout):
        calls.append(url)
        return Response()

    monkeypatch.setattr(config, "CLERK_SECRET_KEY", "sk_test_dummy")
    monkeypatch.setattr(config, "CLERK_JWT_KEY", "")
    monkeypatch.setattr(clerk_auth, "_jwks_keys", {})
    monkeypatch.setattr(clerk_auth, "_jwks_fetched_at", 0.0)
    monkeypatch.setattr(clerk_auth.requests, "get", fake_get)

    for _ in range(5):
        with app.test_request_context('/finances', headers={"Cookie": f"__session={_sign_clerk_token(key)}"}):
            assert clerk_auth.authenticate_clerk_request(clerk_auth.request)["sub"] == "user_abc"
    assert len(calls) == 1


def test_expired_clerk_token_returns_to_the_same_page(monkeypatch):
    from flask import g
    import config
    import services.clerk_auth as clerk_auth

    monkeypatch.setattr(config, "CLERK_SECRET_KEY", "sk_test_dummy")
    guarded = clerk_auth.clerk_org_required(lambda: "allowed")
    with app.test_request_context('/reports?department=2'):
        g._clerk_auth_claims = None
        g.clerk_token_expired = True
        g.user = None
        rv = guarded()
        assert rv.status_code == 302
        assert rv.headers['Location'] == '/login?next=/reports?department%3D2'

    # ?next= only ever leads back into this site.
    from app import _safe_next
    assert _safe_next("/alerts") == "/alerts"
    for bad in ("https://evil.example", "//evil.example", "/\\evil.example", None, ""):
        assert _safe_next(bad) is None


def test_static_files_skip_user_lookup(client):
    login(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    import app as app_module
    calls = []
    original = app_module._set_g_user_and_alert_count

    def spy(db, cursor):
        calls.append(1)
        return original(db, cursor)

    app_module._set_g_user_and_alert_count = spy
    try:
        assert client.get('/static/css/style.css').status_code == 200
        assert calls == []
        client.get('/dashboard')
        assert calls == [1]
    finally:
        app_module._set_g_user_and_alert_count = original


def test_donation_webhook_waits_at_most_three_seconds(client, monkeypatch):
    import config
    import app as app_module
    monkeypatch.setattr(config, "DONATIONS_WEBHOOK_URL", "https://partner.example/webhook")
    seen = {}

    def boom(url, json, timeout):
        seen["timeout"] = timeout
        raise ValueError("unexpected failure inside the webhook client")

    monkeypatch.setattr(app_module.requests, "post", boom)
    login(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    rv = client.post('/admin/add_donation', data=dict(
        donor_name="Timeout Donor", amount="75", payment_method="UPI"), follow_redirects=True)

    assert seen["timeout"] == 3
    assert rv.status_code == 200
    assert _donation("Timeout Donor")['webhook_status'] == 'failed'


def test_tmc_member_sees_donations_read_only(client):
    login(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    client.post('/admin/add_donation', data=dict(donor_name="Visible Donor", amount="11", payment_method="Cash"))
    client.get('/logout')

    login(client, AUDITOR_EMAIL, AUDITOR_PASSWORD)
    rv = client.get('/admin/donations')
    assert rv.status_code == 200
    assert b"Visible Donor" in rv.data
    assert b"Record Donation" not in rv.data and b"add_donation" not in rv.data
    assert b'title="Donations"' in client.get('/dashboard').data


def _alert(title):
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT * FROM alerts WHERE title = ?", (title,))
    row = cursor.fetchone()
    db.close()
    return row


def test_query_goes_to_the_chosen_person_not_the_department_manager(client):
    manager_id = _user_id(MANAGER_EMAIL)
    auditor_id = _user_id(AUDITOR_EMAIL)
    db = get_db()
    cursor = db.cursor()
    # fetchall, not fetchone: earlier tests may give this manager a second
    # department, and an unfinished SQLite read keeps the database locked.
    cursor.execute("SELECT id FROM departments WHERE manager_id = ? ORDER BY id", (manager_id,))
    manager_dept = cursor.fetchall()[0]['id']
    db.close()

    login(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    # A query must name its recipient.
    client.post('/alerts/add', data=dict(type="query", department_id=manager_dept,
                                         title="Unaddressed Query", message="?", severity="info"))
    assert _alert("Unaddressed Query") is None

    client.post('/alerts/add', data=dict(type="query", department_id=manager_dept, assigned_to=auditor_id,
                                         title="Query For TMC", message="Please check", severity="info"))
    client.post('/alerts/add', data=dict(type="query", assigned_to=manager_id,
                                         title="Query For Manager", message="Please explain", severity="info"))
    client.post('/alerts/add', data=dict(type="alert", department_id=manager_dept,
                                         title="Plain Department Alert", message="FYI", severity="info"))
    assert _alert("Query For TMC")['assigned_to'] == auditor_id
    client.get('/logout')

    login(client, MANAGER_EMAIL, MANAGER_PASSWORD)
    page = client.get('/alerts').data
    assert b"Query For TMC" not in page            # names their department, but isn't for them
    assert b"Query For Manager" in page            # addressed to them, no department needed
    assert b"Plain Department Alert" in page       # unaddressed department alerts still reach them
    client.get('/logout')

    login(client, AUDITOR_EMAIL, AUDITOR_PASSWORD)
    page = client.get('/alerts').data
    assert b"Query For TMC" in page and b"To: " in page


def test_manage_users_activate_deactivate_and_delete(client):
    target = _make_pending_user("to.manage@example.com", name="To Manage")
    admin_id = _user_id(ADMIN_EMAIL)
    login(client, ADMIN_EMAIL, ADMIN_PASSWORD)

    page = client.get('/users').data
    assert b"New User" not in page and b"addUserModal" not in page
    for label in (b">Edit", b">Deactivate</button>", b">Activate</button>", b">Delete</button>"):
        assert label in page

    def is_active(uid):
        db = get_db()
        cursor = db.cursor()
        cursor.execute("SELECT is_active FROM users WHERE id = ?", (uid,))
        row = cursor.fetchone()
        db.close()
        return None if row is None else row['is_active']

    client.post(f'/toggle_user_status/{target}', data=dict(status="inactive"))
    assert is_active(target) == 0
    client.post(f'/toggle_user_status/{target}', data=dict(status="inactive"))  # repeat is a no-op
    assert is_active(target) == 0
    client.post(f'/toggle_user_status/{target}')                                # no status: flips
    assert is_active(target) == 1

    client.post(f'/toggle_user_status/{admin_id}', data=dict(status="inactive"))
    client.post(f'/delete_user/{admin_id}')
    assert is_active(admin_id) == 1                 # never yourself

    client.post(f'/delete_user/{target}')
    assert is_active(target) is None
    client.get('/logout')

    other = _make_pending_user("not.yours@example.com")
    login(client, AUDITOR_EMAIL, AUDITOR_PASSWORD)
    client.post(f'/delete_user/{other}')
    client.post(f'/toggle_user_status/{other}', data=dict(status="inactive"))
    assert is_active(other) == 1                    # Accountant only


# ------------------------------------------------------------------ #
# Donations: date of birth, exports, email report
# ------------------------------------------------------------------ #

def test_donation_saves_date_of_birth(client):
    login(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    rv = client.post('/admin/add_donation', data=dict(
        donor_name="Dob Donor", amount="108", payment_method="Cash", dob="1990-05-17"), follow_redirects=True)
    assert str(_donation("Dob Donor")['dob']) == "1990-05-17"
    assert b"1990-05-17" in rv.data

    client.post('/admin/add_donation', data=dict(donor_name="No Dob Donor", amount="5", payment_method="UPI"))
    assert _donation("No Dob Donor")['dob'] is None

    for bad in ("2999-01-01", "17/05/1990"):
        client.post('/admin/add_donation', data=dict(donor_name=f"Bad Dob {bad}", amount="5",
                                                      payment_method="Cash", dob=bad))
        assert _donation(f"Bad Dob {bad}") is None


def test_donations_download_csv_and_pdf(client):
    import csv as csv_module
    import io
    login(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    client.post('/admin/add_donation', data=dict(donor_name="Export Donor", amount="1001.5",
                                                  payment_method="Bank", dob="1985-01-02", notes="Seva"))
    page = client.get('/admin/donations').data
    assert b"Download CSV" in page and b"Download PDF" in page and b"Send Report to Temple" in page

    rv = client.get('/admin/donations/export/csv')
    assert rv.status_code == 200 and rv.mimetype == "text/csv"
    assert "attachment" in rv.headers["Content-Disposition"]
    rows = list(csv_module.reader(io.StringIO(rv.data.decode("utf-8-sig"))))
    assert rows[0][:4] == ["Date", "Donor Name", "Date of Birth", "Amount"]
    export_row = next(r for r in rows if r[1] == "Export Donor")
    assert export_row[2:5] == ["1985-01-02", "1001.50", "Bank"]

    rv = client.get('/admin/donations/export/pdf')
    assert rv.status_code == 200 and rv.mimetype == "application/pdf"
    assert rv.data.startswith(b"%PDF")
    client.get('/logout')

    # TMC Members may download, but not email the report.
    login(client, AUDITOR_EMAIL, AUDITOR_PASSWORD)
    page = client.get('/admin/donations').data
    assert b"Download CSV" in page and b"Send Report to Temple" not in page
    assert client.get('/admin/donations/export/csv').status_code == 200
    assert client.get('/admin/donations/export/pdf').status_code == 200
    client.get('/logout')

    login(client, MANAGER_EMAIL, MANAGER_PASSWORD)
    assert client.get('/admin/donations/export/csv').status_code == 302


class _FakeSMTP:
    sent = []

    def __init__(self, server, port, timeout=None):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        pass

    def login(self, user, password):
        pass

    def sendmail(self, sender, recipients, message):
        _FakeSMTP.sent.append((recipients, message))


def _fake_mail(monkeypatch):
    import services.donation_report_service as drs
    monkeypatch.setattr(drs.EmailService, "SMTP_SERVER", "smtp.test")
    monkeypatch.setattr(drs.EmailService, "SMTP_USERNAME", "sender@test")
    monkeypatch.setattr(drs.EmailService, "SMTP_PASSWORD", "x")
    monkeypatch.setattr(drs.smtplib, "SMTP", _FakeSMTP)
    _FakeSMTP.sent = []
    monkeypatch.setattr(drs.config, "DONATIONS_REPORT_EMAIL", "shirpuriskcon@gmail.com")


def _attachment_names(raw_message):
    import email
    msg = email.message_from_string(raw_message)
    return sorted(part.get_filename() for part in msg.walk() if part.get_filename())


def test_send_donations_report_emails_csv_and_pdf(client, monkeypatch):
    _fake_mail(monkeypatch)
    login(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    client.post('/admin/add_donation', data=dict(donor_name="Mailed Donor", amount="51", payment_method="Cash"))

    rv = client.post('/admin/donations/email_report', follow_redirects=True)
    assert b"Donations report sent to shirpuriskcon@gmail.com" in rv.data
    assert len(_FakeSMTP.sent) == 1
    recipients, raw = _FakeSMTP.sent[0]
    assert recipients == ["shirpuriskcon@gmail.com"]
    names = _attachment_names(raw)
    assert len(names) == 2 and names[0].endswith(".csv") and names[1].endswith(".pdf")
    client.get('/logout')

    # TMC Members can't trigger it.
    login(client, AUDITOR_EMAIL, AUDITOR_PASSWORD)
    client.post('/admin/donations/email_report')
    assert len(_FakeSMTP.sent) == 1


def test_send_donations_report_failure_is_reported_not_raised(client, monkeypatch):
    _fake_mail(monkeypatch)

    def refuse(*args, **kwargs):
        raise OSError("connection refused")

    import services.donation_report_service as drs
    monkeypatch.setattr(drs.smtplib, "SMTP", refuse)
    login(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    rv = client.post('/admin/donations/email_report', follow_redirects=True)
    assert rv.status_code == 200
    assert b"could not be sent" in rv.data


def test_weekly_donations_report_job_is_scheduled_every_7_days(client, monkeypatch):
    from datetime import timedelta
    import services.scheduler_service as scheduler_service

    scheduler = scheduler_service.start_scheduler(app)
    try:
        job = scheduler.get_job('donations_weekly_report')
        assert job is not None
        assert job.trigger.interval == timedelta(days=7)
    finally:
        scheduler.shutdown(wait=False)

    _fake_mail(monkeypatch)
    scheduler_service._run_weekly_donations_report(app)
    assert len(_FakeSMTP.sent) == 1
    assert _FakeSMTP.sent[0][0] == ["shirpuriskcon@gmail.com"]


def test_placeholder_clerk_jwt_key_falls_back_to_published_keys(monkeypatch):
    import json
    from cryptography.hazmat.primitives.asymmetric import rsa
    from jwt.algorithms import RSAAlgorithm
    import config
    import services.clerk_auth as clerk_auth

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid="ins_test", alg="RS256", use="sig")

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"keys": [jwk]}

    monkeypatch.setattr(config, "CLERK_SECRET_KEY", "sk_test_dummy")
    # Exactly what was left in .env: the example placeholder, not a real key.
    monkeypatch.setattr(config, "CLERK_JWT_KEY", "-----BEGIN PUBLIC KEY-----\nMIIB...\n-----END PUBLIC KEY-----")
    monkeypatch.setattr(clerk_auth, "_jwks_keys", {})
    monkeypatch.setattr(clerk_auth, "_jwks_fetched_at", 0.0)
    monkeypatch.setattr(clerk_auth.requests, "get", lambda url, headers, timeout: Response())

    with app.test_request_context('/finances', headers={"Cookie": f"__session={_sign_clerk_token(key)}"}):
        assert clerk_auth.authenticate_clerk_request(clerk_auth.request)["sub"] == "user_abc"
