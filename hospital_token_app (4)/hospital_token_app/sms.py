"""SMS sender.

Provider is chosen by environment variables:
  SMS_PROVIDER=twilio          send real SMS through Twilio
  TWILIO_ACCOUNT_SID=...       from the Twilio console
  TWILIO_AUTH_TOKEN=...        from the Twilio console
  TWILIO_FROM_NUMBER=+1...     your Twilio number, SMS-enabled

Without these, messages are recorded as "simulated" in the SMS log and nothing is sent.
Every attempt, sent or not, is written to the sms_log table.
"""
import base64
import json
import os
import sqlite3
import time
import urllib.parse
import urllib.request

from config import get_setting

DEFAULT_COUNTRY_CODE = "91"  # India; 10-digit numbers get this prefix


def normalize_phone(raw):
    digits = "".join(ch for ch in (raw or "") if ch.isdigit())
    if not digits:
        return None
    if len(digits) == 10:
        digits = DEFAULT_COUNTRY_CODE + digits
    return "+" + digits


class SmsSender:
    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self.provider = get_setting("SMS_PROVIDER", "console").lower()
        self.sid = get_setting("TWILIO_ACCOUNT_SID")
        self.token = get_setting("TWILIO_AUTH_TOKEN")
        self.from_number = get_setting("TWILIO_FROM_NUMBER")
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS sms_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                to_number TEXT,
                body TEXT,
                status TEXT NOT NULL,
                detail TEXT
            );
            """
        )
        db.commit()

    def configured(self):
        return self.provider == "twilio" and all([self.sid, self.token, self.from_number])

    def mode_label(self):
        return "Twilio (live SMS)" if self.configured() else "Simulated (logged, not sent)"

    def send(self, phone, body):
        """Send one SMS. Returns 'sent', 'simulated', 'failed' or 'skipped'."""
        to = normalize_phone(phone)
        if not to:
            status, detail = "skipped", "no valid phone number"
        elif self.configured():
            try:
                detail = self._twilio(to, body)
                status = "sent"
            except Exception as exc:  # network or credential errors must not break the queue
                status, detail = "failed", str(exc)[:200]
        else:
            status, detail = "simulated", "no SMS provider configured"
        self.db.execute(
            "INSERT INTO sms_log(ts, to_number, body, status, detail) VALUES(?, ?, ?, ?, ?)",
            (time.time(), to or (phone or ""), body, status, detail),
        )
        self.db.commit()
        return status

    def _twilio(self, to, body):
        url = f"https://api.twilio.com/2010-04-01/Accounts/{self.sid}/Messages.json"
        data = urllib.parse.urlencode({"To": to, "From": self.from_number, "Body": body}).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        creds = base64.b64encode(f"{self.sid}:{self.token}".encode()).decode()
        req.add_header("Authorization", "Basic " + creds)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())["sid"]

    def recent(self, limit=50):
        rows = self.db.execute("SELECT * FROM sms_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def counts(self):
        rows = self.db.execute("SELECT status, COUNT(*) AS n FROM sms_log GROUP BY status").fetchall()
        return {r["status"]: r["n"] for r in rows}
