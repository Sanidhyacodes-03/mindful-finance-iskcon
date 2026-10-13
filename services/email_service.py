import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import logging

logger = logging.getLogger(__name__)

class EmailService:
    SMTP_SERVER = os.environ.get("SMTP_SERVER", "")
    SMTP_PORT = int(os.environ.get("SMTP_PORT", 587))
    SMTP_USERNAME = os.environ.get("SMTP_USERNAME", "")
    SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
    SENDER_EMAIL = os.environ.get("SENDER_EMAIL", "noreply@spendly.io")

    @classmethod
    def _send(cls, recipient_email, subject, html_body):
        """
        Sends an email using configured SMTP parameters.
        Falls back gracefully to logging if SMTP parameters are missing or fail.
        """
        if not cls.SMTP_SERVER or not cls.SMTP_USERNAME:
            logger.info(f"[EMAIL DEV MODE] Subject: '{subject}' -> Sent to: {recipient_email}")
            print(f"📧 [Email Service] Simulated email to '{recipient_email}': Subject: '{subject}'")
            return True

        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = cls.SENDER_EMAIL
            msg["To"] = recipient_email

            part = MIMEText(html_body, "html")
            msg.attach(part)

            with smtplib.SMTP(cls.SMTP_SERVER, cls.SMTP_PORT) as server:
                server.starttls()
                server.login(cls.SMTP_USERNAME, cls.SMTP_PASSWORD)
                server.sendmail(cls.SENDER_EMAIL, recipient_email, msg.as_string())
            return True
        except Exception as e:
            logger.error(f"Failed to send email to {recipient_email}: {e}")
            print(f"⚠️ [Email Service Error] {e}")
            return False

    @classmethod
    def send_verification_email(cls, email, name, token, host_url="http://127.0.0.1:5001"):
        link = f"{host_url}/verify-email/{token}"
        html = f"""
        <div style="font-family: sans-serif; max-width: 600px; margin: auto; padding: 20px; border: 1px solid #e2e8f0; border-radius: 12px;">
            <h2 style="color: #4f46e5;">Welcome to Spendly, {name}!</h2>
            <p>Please verify your email address to complete your account setup.</p>
            <p><a href="{link}" style="display: inline-block; background: #4f46e5; color: white; padding: 10px 20px; text-decoration: none; border-radius: 6px; font-weight: bold;">Verify Email Address</a></p>
            <p style="font-size: 0.8em; color: #64748b;">Or copy this link: {link}</p>
        </div>
        """
        return cls._send(email, "Verify Your Spendly Account", html)

    @classmethod
    def send_welcome_email(cls, email, name):
        html = f"""
        <div style="font-family: sans-serif; max-width: 600px; margin: auto; padding: 20px; border: 1px solid #e2e8f0; border-radius: 12px;">
            <h2 style="color: #4f46e5;">Welcome aboard, {name}!</h2>
            <p>Your Spendly AI Financial Assistant is now ready to track expenses, forecast monthly cash flows, and help you reach your goals.</p>
        </div>
        """
        return cls._send(email, "Welcome to Spendly AI!", html)

    @classmethod
    def send_password_reset_email(cls, email, name, token, host_url="http://127.0.0.1:5001"):
        link = f"{host_url}/reset-password/{token}"
        html = f"""
        <div style="font-family: sans-serif; max-width: 600px; margin: auto; padding: 20px; border: 1px solid #e2e8f0; border-radius: 12px;">
            <h2 style="color: #4f46e5;">Reset Your Spendly Password</h2>
            <p>Hello {name}, we received a request to reset your password.</p>
            <p><a href="{link}" style="display: inline-block; background: #4f46e5; color: white; padding: 10px 20px; text-decoration: none; border-radius: 6px; font-weight: bold;">Reset Password</a></p>
            <p style="font-size: 0.8em; color: #64748b;">Link valid for 1 hour. If you did not request this, please ignore.</p>
        </div>
        """
        return cls._send(email, "Reset Your Spendly Password", html)

    @classmethod
    def send_db_health_check_email(cls, email, db_ok, checked_at):
        status_color = "#16a34a" if db_ok else "#dc2626"
        status_text = "Active and responding" if db_ok else "Ping failed — check server logs"
        html = f"""
        <div style="font-family: sans-serif; max-width: 600px; margin: auto; padding: 20px; border: 1px solid #e2e8f0; border-radius: 12px;">
            <h2 style="color: #4f46e5;">ISKCON Shirpur Finance — Weekly Database Health Check</h2>
            <p>Status as of {checked_at.strftime('%Y-%m-%d %H:%M')}:</p>
            <p style="font-size: 1.1em; font-weight: bold; color: {status_color};">{status_text}</p>
            <p style="font-size: 0.85em; color: #64748b;">
                This is an automated weekly check confirming the Supabase database is reachable
                and the every-3-days keep-alive ping (which prevents the free-tier project from
                pausing after 7 days of inactivity) is running.
            </p>
        </div>
        """
        subject = "✅ DB Health Check: OK" if db_ok else "⚠️ DB Health Check: FAILED"
        return cls._send(email, subject, html)

    @classmethod
    def send_db_offline_alert(cls, email, error_message, checked_at):
        html = f"""
        <div style="font-family: sans-serif; max-width: 600px; margin: auto; padding: 20px; border: 2px solid #dc2626; border-radius: 12px;">
            <h2 style="color: #dc2626;">🚨 URGENT: Database Offline</h2>
            <p>The ISKCON Shirpur Finance daily keep-alive check could not reach the Supabase database
            at {checked_at.strftime('%Y-%m-%d %H:%M')}.</p>
            <p style="background: #fef2f2; padding: 10px; border-radius: 6px; font-family: monospace; font-size: 0.85em; color: #991b1b;">{error_message}</p>
            <p><strong>What to check:</strong></p>
            <ul>
                <li>Supabase dashboard — if the project shows as <em>Paused</em>, click <em>Restore</em>.</li>
                <li>Network/firewall on the server running the app (outbound port 5432).</li>
                <li>That <code>DATABASE_URL</code> in the server's <code>.env</code> is still correct.</li>
            </ul>
        </div>
        """
        return cls._send(email, "🚨 URGENT: Database Offline — ISKCON Shirpur Finance", html)

    @classmethod
    def send_bill_reminder_email(cls, email, name, bill_title, amount, due_date):
        html = f"""
        <div style="font-family: sans-serif; max-width: 600px; margin: auto; padding: 20px; border: 1px solid #e2e8f0; border-radius: 12px;">
            <h3 style="color: #f59e0b;">⚡ Upcoming AutoPay Reminder</h3>
            <p>Hello {name}, your recurring bill <strong>{bill_title}</strong> of <strong>₹{amount:,.2f}</strong> is due on <strong>{due_date}</strong>.</p>
        </div>
        """
        return cls._send(email, f"AutoPay Reminder: {bill_title}", html)
