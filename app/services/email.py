from __future__ import annotations

import smtplib
from email.message import EmailMessage

from app.core.config import settings


class EmailDeliveryError(RuntimeError):
    pass


def send_email(*, to_email: str, subject: str, body: str) -> None:
    if not settings.smtp_username or not settings.smtp_password or not settings.smtp_from_email:
        raise EmailDeliveryError("The code was not sent. Please try again.")

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = f"{settings.smtp_from_name} <{settings.smtp_from_email}>"
    message["To"] = to_email
    message.set_content(body)

    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as server:
            server.starttls()
            server.login(settings.smtp_username, settings.smtp_password)
            server.send_message(message)
    except OSError as exc:
        raise EmailDeliveryError("The code was not sent. Please try again.") from exc
    except smtplib.SMTPException as exc:
        raise EmailDeliveryError("The code was not sent. Please try again.") from exc


def send_verification_code(*, to_email: str, code: str) -> None:
    send_email(
        to_email=to_email,
        subject="Verify your Mafeteng Youth League account",
        body=(
            "Mafeteng Youth League email verification\n\n"
            f"Your verification code is: {code}\n\n"
            "This code expires soon. If you did not create this account, ignore this email."
        ),
    )


def send_login_code(*, to_email: str, code: str) -> None:
    send_email(
        to_email=to_email,
        subject="Your Mafeteng Youth League login code",
        body=(
            "Mafeteng Youth League two-factor authentication\n\n"
            f"Your one-time login code is: {code}\n\n"
            "This code expires soon. If you did not try to log in, ignore this email."
        ),
    )


def send_notification_email(*, to_email: str, title: str, message: str, link: str | None = None) -> None:
    body = (
        "Mafeteng Youth League notification\n\n"
        f"{title}\n\n"
        f"{message}\n"
    )
    if link:
        body += f"\nOpen: {link}\n"
    send_email(
        to_email=to_email,
        subject=f"Mafeteng Youth League: {title}",
        body=body,
    )
