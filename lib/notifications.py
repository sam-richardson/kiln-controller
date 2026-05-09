"""Multi-backend notification dispatcher for kiln events.

Triggered events:
    - run_complete:  schedule finished, kiln cooling
    - safe_to_open:  kiln temperature has dropped below the safe-to-open
                     threshold
    - emergency:     over-temp, element failure, repeated TC errors

Backends are independent and configured in config.py. A failure in one
backend never blocks the others; failures are logged but never raised
into the oven control loop.
"""
import json
import logging
import smtplib
import ssl
import threading
from email.message import EmailMessage

try:
    import requests
except ImportError:
    requests = None

import config

log = logging.getLogger(__name__)


def _send_email(subject, body):
    if not getattr(config, "notify_email_enabled", False):
        return
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = config.notify_email_from
        msg["To"] = ", ".join(config.notify_email_to)
        msg.set_content(body)

        host = config.notify_email_smtp_host
        port = config.notify_email_smtp_port
        user = config.notify_email_smtp_user or config.notify_email_from
        pwd = config.notify_email_smtp_pass

        if config.notify_email_use_tls:
            with smtplib.SMTP(host, port, timeout=10) as s:
                s.ehlo()
                s.starttls(context=ssl.create_default_context())
                s.ehlo()
                if pwd:
                    s.login(user, pwd)
                s.send_message(msg)
        else:
            with smtplib.SMTP_SSL(host, port, timeout=10,
                                  context=ssl.create_default_context()) as s:
                if pwd:
                    s.login(user, pwd)
                s.send_message(msg)
        log.info("notification: email sent (%s)", subject)
    except Exception:
        log.exception("notification: email send failed")


def _send_pushover(subject, body):
    if not getattr(config, "notify_pushover_enabled", False):
        return
    if requests is None:
        log.error("notification: pushover requires the 'requests' package")
        return
    try:
        r = requests.post(
            "https://api.pushover.net/1/messages.json",
            data={
                "token": config.notify_pushover_app_token,
                "user": config.notify_pushover_user_key,
                "title": subject,
                "message": body,
            },
            timeout=5,
        )
        r.raise_for_status()
        log.info("notification: pushover sent (%s)", subject)
    except Exception:
        log.exception("notification: pushover send failed")


def _send_ntfy(subject, body):
    if not getattr(config, "notify_ntfy_enabled", False):
        return
    if requests is None:
        log.error("notification: ntfy requires the 'requests' package")
        return
    try:
        url = "%s/%s" % (
            config.notify_ntfy_server.rstrip("/"),
            config.notify_ntfy_topic,
        )
        r = requests.post(
            url,
            data=body.encode("utf-8"),
            headers={"Title": subject},
            timeout=5,
        )
        r.raise_for_status()
        log.info("notification: ntfy sent (%s)", subject)
    except Exception:
        log.exception("notification: ntfy send failed")


def _send_slack(subject, body):
    if not getattr(config, "notify_slack_enabled", False):
        return
    if not getattr(config, "notify_slack_webhook_url", ""):
        return
    if requests is None:
        log.error("notification: slack requires the 'requests' package")
        return
    try:
        text = "*%s*\n%s" % (subject, body)
        r = requests.post(
            config.notify_slack_webhook_url,
            json={"text": text},
            timeout=5,
        )
        r.raise_for_status()
        log.info("notification: slack sent (%s)", subject)
    except Exception:
        log.exception("notification: slack send failed")


def notify(event, subject, body, async_send=True):
    """Send a notification on every enabled backend.

    Backends are dispatched on a daemon thread so the control loop
    never blocks on network I/O.
    """
    log.info("notify(%s): %s", event, subject)

    def _do():
        _send_email(subject, body)
        _send_pushover(subject, body)
        _send_ntfy(subject, body)
        _send_slack(subject, body)

    if async_send:
        threading.Thread(target=_do, daemon=True).start()
    else:
        _do()


def format_state(state):
    """Format an oven state dict for inclusion in notifications."""
    keys = ("profile", "temperature", "target", "runtime", "totaltime",
            "state", "cost", "currency_type")
    parts = []
    for k in keys:
        if k in state:
            parts.append("%s: %s" % (k, state[k]))
    return "\n".join(parts)


def notify_run_complete(state):
    body = ("Your kiln has finished its firing schedule and is now"
            " cooling.\n\n%s" % format_state(state))
    notify("run_complete", "[Kiln] Firing complete", body)


def notify_safe_to_open(state):
    body = ("Kiln has cooled below the safe-to-open temperature.\n\n%s"
            % format_state(state))
    notify("safe_to_open", "[Kiln] Safe to open", body)


def notify_emergency(reason, state):
    body = "EMERGENCY: %s\n\n%s" % (reason, format_state(state))
    notify("emergency", "[Kiln] EMERGENCY: %s" % reason, body)
