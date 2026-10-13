"""Donations exports (CSV / PDF) and the donations email report.

Used by the Donations page's download buttons, its "Send Report to Temple"
button, and the every-7-days scheduler job (services/scheduler_service.py).
Everything is built in memory — no files are written to disk.
"""
import csv
import io
import logging
import smtplib
from datetime import datetime
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    HRFlowable, Image as RLImage, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
)

import config
from database.db import get_db
from services.email_service import EmailService
from services.report_service import FONT_BOLD, FONT_REGULAR, _ensure_fonts, _find_logo_path

logger = logging.getLogger(__name__)

CSV_HEADER = ["Date", "Donor Name", "Date of Birth", "Amount", "Payment Method",
              "Transaction ID", "Notes", "Recorded By", "External Sync"]


def fetch_donations():
    """All donations, newest first, with the recording user's name."""
    db = get_db()
    cursor = db.cursor()
    cursor.execute("""
        SELECT dn.*, u.name AS recorded_by_name FROM donations dn
        LEFT JOIN users u ON dn.recorded_by = u.id
        ORDER BY dn.created_at DESC, dn.id DESC
    """)
    rows = cursor.fetchall()
    db.close()
    return rows


def _text(value):
    """Postgres returns date/datetime objects, SQLite returns strings — show both the same way."""
    if value is None or value == "":
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    return str(value)[:16] if len(str(value)) > 10 else str(value)


def _sync_label(status):
    return {"sent": "Sent", "failed": "Not sent", "not configured": "Not configured"}.get(status, "Pending")


def generate_donations_csv(rows=None):
    """CSV text of every donation."""
    rows = fetch_donations() if rows is None else rows
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(CSV_HEADER)
    for d in rows:
        writer.writerow([
            _text(d['created_at']), d['donor_name'], _text(d['dob']), f"{d['amount']:.2f}",
            d['payment_method'], d['transaction_id'] or "", d['notes'] or "",
            d['recorded_by_name'] or "", _sync_label(d['webhook_status']),
        ])
    return buf.getvalue()


def generate_donations_pdf(rows=None, generated_by_name=None):
    """PDF bytes: a formatted table of every donation plus the total."""
    rows = fetch_donations() if rows is None else rows
    _ensure_fonts()

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=landscape(A4),
        topMargin=14 * mm, bottomMargin=14 * mm, leftMargin=12 * mm, rightMargin=12 * mm,
        title="ISKCON Shirpur Donations Report",
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("DonTitle", parent=styles["Title"], fontName=FONT_BOLD, fontSize=20, spaceAfter=2, alignment=0)
    sub_style = ParagraphStyle("DonSub", parent=styles["Normal"], fontName=FONT_REGULAR, fontSize=9,
                               textColor=colors.HexColor("#6b7280"))
    normal_style = ParagraphStyle("DonNormal", parent=styles["Normal"], fontName=FONT_REGULAR, fontSize=9.5)
    cell = ParagraphStyle("DonCell", parent=styles["Normal"], fontName=FONT_REGULAR, fontSize=8.5)
    cell_bold = ParagraphStyle("DonCellBold", parent=styles["Normal"], fontName=FONT_BOLD, fontSize=8.5)

    story = []
    heading = [Paragraph("ISKCON Shirpur", title_style), Paragraph("Donations Report", sub_style)]
    logo_path = _find_logo_path()
    if logo_path:
        try:
            logo = RLImage(logo_path, width=24 * mm, height=24 * mm, kind="proportional")
            header = Table([[logo, heading]], colWidths=[30 * mm, None])
            header.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
            story.append(header)
        except Exception:
            story.extend(heading)
    else:
        story.extend(heading)

    total = sum(d['amount'] for d in rows)
    story.append(Spacer(1, 6))
    story.append(Paragraph(
        f"Generated on {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        + (f" by {generated_by_name}" if generated_by_name else "")
        + f" · {len(rows)} donation{'s' if len(rows) != 1 else ''} · Total ₹{total:,.2f}",
        sub_style))
    story.append(Spacer(1, 8))
    story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor("#e5e7eb")))
    story.append(Spacer(1, 8))

    if rows:
        headers = ["Date", "Donor Name", "Date of Birth", "Amount (₹)", "Method", "Transaction ID", "Notes", "Recorded By"]
        data = [[Paragraph(f"<b>{h}</b>", cell_bold) for h in headers]]
        for d in rows:
            data.append([
                Paragraph(_text(d['created_at']), cell),
                Paragraph(d['donor_name'], cell),
                Paragraph(_text(d['dob']) or "-", cell),
                Paragraph(f"{d['amount']:,.2f}", cell),
                Paragraph(d['payment_method'], cell),
                Paragraph(d['transaction_id'] or "-", cell),
                Paragraph(d['notes'] or "-", cell),
                Paragraph(d['recorded_by_name'] or "-", cell),
            ])
        data.append([Paragraph("<b>Total</b>", cell_bold), "", "", Paragraph(f"<b>{total:,.2f}</b>", cell_bold),
                     "", "", "", ""])
        table = Table(data, colWidths=[78, 120, 66, 70, 58, 92, 200, 86], repeatRows=1)
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#4f46e5")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("ROWBACKGROUNDS", (0, 1), (-1, -2), [colors.white, colors.HexColor("#f9fafb")]),
            ("ALIGN", (3, 0), (3, -1), "RIGHT"),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e5e7eb")),
            ("LINEABOVE", (0, -1), (-1, -1), 1, colors.HexColor("#4f46e5")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        story.append(table)
    else:
        story.append(Paragraph("No donations recorded yet.", normal_style))

    story.append(Spacer(1, 14))
    story.append(Paragraph(
        "This is a system-generated report from the ISKCON Shirpur Finance system for record-keeping purposes.",
        sub_style))
    doc.build(story)
    return buf.getvalue()


def send_donations_report(recipient=None, generated_by_name=None):
    """Email the donations CSV and PDF as attachments via smtplib, using the
    same SMTP settings (.env SMTP_*) as the rest of the app's email.
    Returns (ok, message) and never raises, so a mail problem can't crash the
    request or the scheduler thread that called it."""
    recipient = recipient or config.DONATIONS_REPORT_EMAIL
    smtp = EmailService
    if not smtp.SMTP_SERVER or not smtp.SMTP_USERNAME:
        return False, "Email isn't configured on the server (SMTP settings are missing)."

    try:
        rows = fetch_donations()
        csv_bytes = generate_donations_csv(rows).encode("utf-8-sig")  # BOM so Excel shows ₹ correctly
        pdf_bytes = generate_donations_pdf(rows, generated_by_name)
    except Exception as e:
        logger.exception("Building the donations report failed")
        return False, f"The report could not be generated: {e}"

    stamp = datetime.now().strftime("%Y-%m-%d")
    total = sum(d['amount'] for d in rows)
    msg = MIMEMultipart()
    msg["Subject"] = f"ISKCON Shirpur Donations Report — {stamp}"
    msg["From"] = smtp.SENDER_EMAIL
    msg["To"] = recipient
    msg.attach(MIMEText(
        f"""<div style="font-family: sans-serif; max-width: 600px; margin: auto; padding: 20px; border: 1px solid #e2e8f0; border-radius: 12px;">
            <h2 style="color: #4f46e5;">ISKCON Shirpur — Donations Report</h2>
            <p>Attached are all recorded donations as of {datetime.now().strftime('%Y-%m-%d %H:%M')}
            ({len(rows)} donation{'s' if len(rows) != 1 else ''}, total <strong>₹{total:,.2f}</strong>),
            as a CSV spreadsheet and a PDF.</p>
            <p style="font-size: 0.85em; color: #64748b;">Sent automatically by the ISKCON Shirpur Finance system.</p>
        </div>""", "html", "utf-8"))
    for filename, payload, subtype in (
        (f"iskcon_shirpur_donations_{stamp}.csv", csv_bytes, "csv"),
        (f"iskcon_shirpur_donations_{stamp}.pdf", pdf_bytes, "pdf"),
    ):
        part = MIMEApplication(payload, _subtype=subtype)
        part.add_header("Content-Disposition", "attachment", filename=filename)
        msg.attach(part)

    try:
        with smtplib.SMTP(smtp.SMTP_SERVER, smtp.SMTP_PORT, timeout=30) as server:
            server.starttls()
            server.login(smtp.SMTP_USERNAME, smtp.SMTP_PASSWORD)
            server.sendmail(smtp.SENDER_EMAIL, [recipient], msg.as_string())
    except Exception as e:
        logger.error(f"Donations report email to {recipient} failed: {e}")
        return False, f"The email could not be sent: {e}"

    return True, f"Donations report sent to {recipient}."
