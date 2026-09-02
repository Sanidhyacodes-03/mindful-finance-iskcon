from datetime import datetime, timedelta
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


def start_scheduler(app):
    """Starts a daemon background scheduler that generates end-of-month alerts daily at 2 AM."""
    from apscheduler.schedulers.background import BackgroundScheduler

    def _run_with_context():
        with app.app_context():
            generate_month_end_alerts()

    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(_run_with_context, 'cron', hour=2, minute=0, id='month_end_alerts')
    scheduler.start()
    return scheduler
