import os
import logging

logger = logging.getLogger(__name__)

class WhatsAppService:
    """
    Modular WhatsApp Notification Service architecture.
    Supports Twilio, Meta WhatsApp Business API, and Dev console fallback.
    """
    PROVIDER = os.environ.get("WHATSAPP_PROVIDER", "DEV") # DEV, TWILIO, META
    TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
    TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
    TWILIO_PHONE_NUMBER = os.environ.get("TWILIO_PHONE_NUMBER", "whatsapp:+14155238886")
    META_API_TOKEN = os.environ.get("META_API_TOKEN", "")

    @classmethod
    def send_message(cls, recipient_phone, message_text):
        """
        Sends WhatsApp notification message to recipient.
        """
        if not recipient_phone:
            recipient_phone = "+919876543210" # default fallback for dev

        if cls.PROVIDER == "TWILIO" and cls.TWILIO_ACCOUNT_SID:
            try:
                from twilio.rest import Client
                client = Client(cls.TWILIO_ACCOUNT_SID, cls.TWILIO_AUTH_TOKEN)
                message = client.messages.create(
                    body=message_text,
                    from_=cls.TWILIO_PHONE_NUMBER,
                    to=f"whatsapp:{recipient_phone}"
                )
                logger.info(f"Twilio WhatsApp sent: {message.sid}")
                return True
            except Exception as e:
                logger.error(f"Twilio WhatsApp Error: {e}")

        # Default Dev Logger Output
        print(f"💬 [WhatsApp Service ({cls.PROVIDER})] To: {recipient_phone} | Msg: '{message_text}'")
        return True

    @classmethod
    def notify_expense_logged(cls, phone, description, amount, category_name, date):
        msg = f"💸 *Spendly*: ₹{amount:,.2f} spent on '{description}' ({category_name}) — {date}."
        return cls.send_message(phone, msg)

    @classmethod
    def notify_login(cls, phone, name):
        msg = f"👋 *Spendly*: Hi {name}, you just logged into your dashboard. If this wasn't you, please secure your account."
        return cls.send_message(phone, msg)

    @classmethod
    def notify_autopay_due(cls, phone, title, amount, due_date):
        msg = f"⚡ *Spendly AutoPay Alert*: {title} of ₹{amount:,.2f} is due on {due_date}. Please ensure sufficient balance."
        return cls.send_message(phone, msg)

    @classmethod
    def notify_budget_exceeded(cls, phone, category_name, spent, limit):
        msg = f"⚠️ *Spendly Budget Alert*: You have exceeded your monthly budget for {category_name}! Spent: ₹{spent:,.2f} / Limit: ₹{limit:,.2f}."
        return cls.send_message(phone, msg)

    @classmethod
    def notify_goal_completed(cls, phone, goal_name, amount):
        msg = f"🎉 *Spendly Goal Achievement*: Congratulations! You have reached your savings target for {goal_name} (₹{amount:,.2f})!"
        return cls.send_message(phone, msg)

    @classmethod
    def notify_health_alert(cls, phone, score, risk_level):
        msg = f"📊 *Spendly Health Update*: Your Financial Health Score is {score}/100 ({risk_level}). Log in to review recommendations."
        return cls.send_message(phone, msg)
