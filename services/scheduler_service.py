from datetime import datetime, timedelta
import config
from database.db import get_db, log_activity


def month_bounds(ref_date):
    """Given a reference datetime, return (start_date, end_date, iso_month, month_label)
    of the most recently fully-completed calendar month as of ref_date."""
    first_of_this_month = ref_date.replace(day=1)
    last_month_end = (first_of_this_month - timedelta(days=1)).date()
    last_month_start = last_month_end.replace(day=1)
    return last_month_start, last_month_end, last_month_start.strftime("%Y-%m"), last_month_start.strftime("%B %Y")


def generate_month_end_alerts(ref_date=None):
    """Pure(ish), testable core — pass a fixed ref_date to test without waiting on wall-clock
    time. Creates one 'End of Month Report' alert per active department summarizing the most
    recently completed calendar month's spend vs budget. Idempotent per (department, month):
    a second call with the same ref_date is a no-op. Returns the list of created alert ids."""
    ref_date = ref_date or datetime.now()
    start_date, end_date, iso_month, month_label = month_bounds(ref_date)

    db = get_db()
    cursor = db.cursor()
    created = []

    cursor.execute("SELECT * FROM departments WHERE is_active = 1")
    for dept in cursor.fetchall():
        title = f"End of Month Report — {dept['name']} ({iso_month})"
        cursor.execute("SELECT id FROM alerts WHERE title = ? AND department_id = ?", (title, dept['id']))
        if cursor.fetchone():
            continue  # already generated for this department + month

        cursor.execute(
            "SELECT COALESCE(SUM(amount), 0) as total FROM expenses WHERE department_id = ? AND date >= ? AND date <= ?",
            (dept['id'], start_date.isoformat(), end_date.isoformat())
        )
        total_spent = cursor.fetchone()['total']

        cursor.execute("SELECT monthly_limit FROM budgets WHERE department_id = ?", (dept['id'],))
        budget_row = cursor.fetchone()
        monthly_limit = budget_row['monthly_limit'] if budget_row else None

        if monthly_limit:
            pct = (total_spent / monthly_limit * 100)
            message = f"{dept['name']} spent ₹{total_spent:,.2f} of its ₹{monthly_limit:,.2f} monthly budget ({pct:.1f}%) in {month_label}."
            severity = "critical" if pct >= 100 else ("warning" if pct >= 80 else "info")
        else:
            message = f"{dept['name']} spent ₹{total_spent:,.2f} in {month_label}. No monthly budget is set for this department."
            severity = "info"

        cursor.execute(
            "INSERT INTO alerts (type, department_id, title, message, severity, created_by) VALUES ('alert', ?, ?, ?, ?, NULL)",
            (dept['id'], title, message, severity)
        )
        alert_id = cursor.lastrowid
        log_activity(cursor, None, "create", "alert", alert_id, f"System-generated: {title}")
        created.append(alert_id)

    db.commit()
    db.close()
    return created


def _ping_database():
    """Runs SELECT 1 against the configured database. Returns (ok, error_message) —
    never raises, since the keep-alive and health-check jobs must keep running
    (and alerting) precisely when the database is the thing that's failing."""
    try:
        db = get_db()
        cursor = db.cursor()
        cursor.execute("SELECT 1")
        cursor.fetchone()
        db.close()
        return True, None
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _run_keepalive_ping(app):
    """Daily SELECT 1 — keeps Supabase's free tier from pausing the project
    (it pauses after 7 days with no database activity), and immediately emails
    an URGENT alert to config.ADMIN_ALERT_EMAIL if the database can't be reached."""
    from services.email_service import EmailService

    with app.app_context():
        ok, error = _ping_database()
        if ok:
            app.logger.info("Supabase keep-alive ping OK.")
            return

        app.logger.error(f"Supabase keep-alive ping FAILED: {error}")
        try:
            sent = EmailService.send_db_offline_alert(config.ADMIN_ALERT_EMAIL, error, datetime.now())
            if not sent:
                app.logger.error("URGENT database-offline alert email could not be delivered (check SMTP settings).")
        except Exception as e:
            app.logger.error(f"URGENT database-offline alert email failed to send: {e}")


def _run_weekly_health_check_email(app):
    """Does its own fresh ping (not just reporting the last keep-alive's cached
    result) so the email reflects the database's state at send time, and sends
    to config.ADMIN_ALERT_EMAIL regardless of whether the ping succeeded —
    a failure notification is the whole point of a health check."""
    from services.email_service import EmailService

    with app.app_context():
        ok, _error = _ping_database()
        try:
            EmailService.send_db_health_check_email(config.ADMIN_ALERT_EMAIL, ok, datetime.now())
        except Exception as e:
            app.logger.error(f"Weekly DB health-check email failed to send: {e}")


def _run_drive_backup(app, receipt_dir):
    """Wrapped so one failed run (bad credentials, network, Drive API error) logs
    and gets retried at the next 15-day cycle instead of crashing the scheduler
    thread — APScheduler doesn't reschedule a job whose function raised."""
    from services.backup_service import backup_uploads_to_drive

    with app.app_context():
        try:
            file_id = backup_uploads_to_drive(receipt_dir)
            if file_id:
                app.logger.info(f"Google Drive backup uploaded (file id={file_id}).")
            else:
                app.logger.info("Google Drive backup skipped — no receipt files to back up.")
        except Exception as e:
            app.logger.error(f"Google Drive backup failed: {e}")


def _run_weekly_donations_report(app):
    """Every 7 days: email the donations CSV + PDF to config.DONATIONS_REPORT_EMAIL.
    send_donations_report never raises, so a failed run is logged and the job
    simply tries again next week."""
    from services.donation_report_service import send_donations_report

    with app.app_context():
        ok, message = send_donations_report(generated_by_name="Automatic weekly report")
        if ok:
            app.logger.info(message)
        else:
            app.logger.error(f"Weekly donations report failed: {message}")


def start_scheduler(app, receipt_dir=None):
    """Starts a daemon background scheduler — one shared instance for every
    background job in this app, not a separate scheduler per feature:
      - end-of-month alerts, daily at 2 AM
      - Google Drive receipts backup, every 15 days (only if config.BACKUP_ENABLED —
        see services/backup_service.py for setup)
      - Supabase keep-alive ping, daily at 3 AM, with an immediate URGENT email to
        config.ADMIN_ALERT_EMAIL if it fails (only if config.IS_POSTGRES — pinging
        a local SQLite database wouldn't do anything)
      - weekly DB health-check email to config.ADMIN_ALERT_EMAIL (same gate)
      - donations CSV + PDF report emailed to config.DONATIONS_REPORT_EMAIL every 7 days
    receipt_dir is app.py's RECEIPT_DIR, passed in explicitly rather than imported
    back from app.py to avoid a circular import (app.py is what imports and calls
    this function)."""
    from apscheduler.schedulers.background import BackgroundScheduler

    def _run_with_context():
        with app.app_context():
            generate_month_end_alerts()

    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(_run_with_context, 'cron', hour=2, minute=0, id='month_end_alerts')

    if config.BACKUP_ENABLED and receipt_dir:
        scheduler.add_job(
            lambda: _run_drive_backup(app, receipt_dir), 'interval', days=15, id='drive_backup'
        )

    if config.IS_POSTGRES:
        scheduler.add_job(lambda: _run_keepalive_ping(app), 'cron', hour=3, minute=0, id='db_keepalive')
        scheduler.add_job(
            lambda: _run_weekly_health_check_email(app), 'interval', days=7, id='db_health_check_email'
        )

    scheduler.add_job(
        lambda: _run_weekly_donations_report(app), 'interval', days=7, id='donations_weekly_report'
    )

    scheduler.start()
    return scheduler
