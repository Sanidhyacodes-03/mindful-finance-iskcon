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
def client():
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
    assert b"Admin Overview" in rv.data


def test_login_auditor(client):
    rv = login(client, AUDITOR_EMAIL, AUDITOR_PASSWORD)
    assert rv.status_code == 200
    assert b"Auditor Overview" in rv.data


def test_login_manager(client):
    rv = login(client, MANAGER_EMAIL, MANAGER_PASSWORD)
    assert rv.status_code == 200
    assert b"Manager Overview" in rv.data


def test_admin_can_create_department(client):
    login(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    rv = client.post('/departments/add', data=dict(
        name="Test Department",
        description="Created by test",
        icon="🧪",
        color="#123456"
    ), follow_redirects=True)
    assert rv.status_code == 200
    assert b"Test Department" in rv.data

    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT id FROM departments WHERE name = ?", ("Test Department",))
    assert cursor.fetchone() is not None
    db.close()


def test_auditor_cannot_create_department(client):
    login(client, AUDITOR_EMAIL, AUDITOR_PASSWORD)
    rv = client.post('/departments/add', data=dict(
        name="Blocked Department",
        description="Should not be created",
        icon="🚫",
        color="#000000"
    ), follow_redirects=True)
    assert rv.status_code == 200
    assert b"read-only access" in rv.data

    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT id FROM departments WHERE name = ?", ("Blocked Department",))
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
    cursor.execute("SELECT id FROM departments WHERE id != ? LIMIT 1", (own_dept['id'],))
    other_dept_id = cursor.fetchone()['id']
    db.close()

    login(client, MANAGER_EMAIL, MANAGER_PASSWORD)

    # Blocked from org-wide pages entirely.
    for path in ('/departments', '/users', '/reports', '/activity-log'):
        rv = client.get(path, follow_redirects=True)
        assert rv.status_code == 200
        assert b"scoped to your assigned department" in rv.data

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
    client.post('/departments/add', data=dict(name="Logged Department", description="", icon="📝", color="#111111"))
    rv = client.get('/activity-log')
    assert rv.status_code == 200
    assert b"Logged Department" in rv.data


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
