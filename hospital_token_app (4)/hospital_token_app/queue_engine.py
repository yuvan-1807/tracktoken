"""Queue engine: queue rules and the SMS messages they trigger. State lives in SQLite."""
import sqlite3
import time

WAITING, CALLED, HELD, DONE = "waiting", "called", "held", "done"
DEFAULT_SERVICE_MIN = 6.0   # used until enough visits are recorded
EMERGENCY_DELAY_MIN = 15    # promised delay shown to patients during a pause


class QueueEngine:
    def __init__(self, db: sqlite3.Connection, sms=None):
        self.db = db
        self.sms = sms  # SmsSender or None
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS tokens (
                token      INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER,
                name       TEXT NOT NULL,
                phone      TEXT,
                status     TEXT NOT NULL,
                created_at REAL NOT NULL,
                called_at  REAL,
                done_at    REAL,
                holds      INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                msg TEXT NOT NULL
            );
            """
        )
        db.commit()
        if self._get("emergency") is None:
            self._set("emergency", "0")

    # ---------- helpers ----------
    def _get(self, key):
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def _set(self, key, value):
        self.db.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.db.commit()

    def log(self, msg):
        self.db.execute("INSERT INTO events(ts, msg) VALUES(?, ?)", (time.time(), msg))
        self.db.commit()

    def events(self, limit=30):
        rows = self.db.execute("SELECT ts, msg FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def _row(self, sql, params=()):
        row = self.db.execute(sql, params).fetchone()
        return dict(row) if row else None

    def _rows(self, sql, params=()):
        return [dict(r) for r in self.db.execute(sql, params).fetchall()]

    def _notify(self, phone, text):
        """Send an SMS if a number is on file and a sender is configured. Returns status or None."""
        if self.sms and phone:
            return self.sms.send(phone, text)
        return None

    # ---------- emergency pause ----------
    @property
    def emergency(self):
        return self._get("emergency") == "1"

    def set_emergency(self, on: bool):
        if on == self.emergency:
            return
        self._set("emergency", "1" if on else "0")
        if on:
            text = (f"Clinic update: the doctor is attending a critical emergency. Appointments are paused. "
                    f"We will update you within {EMERGENCY_DELAY_MIN} minutes.")
            action = "EMERGENCY PAUSE"
        else:
            text = "Clinic update: the queue has resumed. Thank you for your patience."
            action = "QUEUE RESUMED"
        count = 0
        for row in self._rows("SELECT * FROM tokens WHERE status=?", (WAITING,)):
            if self._notify(row["phone"], text):
                count += 1
        self.log(f"{action}: SMS sent to {count} waiting patient(s) with a number on file")

    # ---------- patient / intake ----------
    def my_token(self, user_id):
        """The patient's current open token, if any."""
        return self._row(
            "SELECT * FROM tokens WHERE user_id=? AND status IN (?, ?, ?) ORDER BY token DESC LIMIT 1",
            (user_id, WAITING, CALLED, HELD),
        )

    def take_token(self, name, phone="", user_id=None):
        name = (name or "").strip() or "Guest"
        if user_id is not None:
            existing = self.my_token(user_id)
            if existing:
                raise ValueError(f"You already have an open token, #{existing['token']}.")
        cur = self.db.execute(
            "INSERT INTO tokens(user_id, name, phone, status, created_at) VALUES(?, ?, ?, ?, ?)",
            (user_id, name, (phone or "").strip(), WAITING, time.time()),
        )
        self.db.commit()
        token = cur.lastrowid
        self.log(f"Token #{token} issued to {name}")
        return token

    # ---------- staff actions ----------
    def current(self):
        return self._row("SELECT * FROM tokens WHERE status=? ORDER BY token LIMIT 1", (CALLED,))

    def _finish(self, token):
        self.db.execute("UPDATE tokens SET status=?, done_at=? WHERE token=?", (DONE, time.time(), token))
        self.db.commit()

    def call_next(self):
        if self.emergency:
            return None, "Emergency pause is on. Resume the queue first."
        cur = self.current()
        if cur:
            self._finish(cur["token"])
        nxt = self._row("SELECT * FROM tokens WHERE status=? ORDER BY token LIMIT 1", (WAITING,))
        if not nxt:
            return None, "No patients waiting."
        self.db.execute(
            "UPDATE tokens SET status=?, called_at=? WHERE token=?", (CALLED, time.time(), nxt["token"])
        )
        self.db.commit()
        self.log(f"Called #{nxt['token']} {nxt['name']}")
        self._notify(
            nxt["phone"],
            f"Clinic: token #{nxt['token']}, it's your turn. Please go to the counter now.",
        )
        return nxt["token"], "ok"

    def mark_done(self):
        cur = self.current()
        if cur:
            self._finish(cur["token"])
            self.log(f"Visit completed for #{cur['token']}")

    def skip_current(self):
        cur = self.current()
        if not cur:
            return "Nobody is being served."
        self.db.execute(
            "UPDATE tokens SET status=?, holds=holds+1, called_at=NULL WHERE token=?", (HELD, cur["token"])
        )
        self.db.commit()
        self.log(f"#{cur['token']} absent, moved to hold list")
        self._notify(
            cur["phone"],
            f"Clinic: you were marked absent for token #{cur['token']}. Please see the front desk to rejoin.",
        )
        return "ok"

    def reinject(self, token):
        """Put a held patient back. Their original token number keeps their place."""
        row = self._row("SELECT * FROM tokens WHERE token=? AND status=?", (token, HELD))
        if not row:
            return
        self.db.execute("UPDATE tokens SET status=? WHERE token=?", (WAITING, token))
        self.db.commit()
        self.log(f"#{token} re-injected into the queue")
        self._notify(
            row["phone"], f"Clinic: token #{token} is back in the queue. Please wait for your number."
        )

    # ---------- queries ----------
    def held(self):
        return self._rows("SELECT * FROM tokens WHERE status=? ORDER BY token", (HELD,))

    def avg_service_min(self):
        rows = self._rows(
            "SELECT called_at, done_at FROM tokens WHERE status=? AND called_at IS NOT NULL "
            "ORDER BY done_at DESC LIMIT 20",
            (DONE,),
        )
        if len(rows) < 3:
            return DEFAULT_SERVICE_MIN
        return sum(r["done_at"] - r["called_at"] for r in rows) / len(rows) / 60

    def position(self, token):
        row = self._row("SELECT * FROM tokens WHERE token=?", (token,))
        if not row:
            return None
        ahead = self.db.execute(
            "SELECT COUNT(*) FROM tokens WHERE status=? AND token<?", (WAITING, token)
        ).fetchone()[0]
        serving = 1 if self.current() else 0
        eta = (ahead + serving) * self.avg_service_min()
        if self.emergency and row["status"] == WAITING:
            eta += EMERGENCY_DELAY_MIN
        return {
            "token": token,
            "name": row["name"],
            "status": row["status"],
            "ahead": ahead,
            "eta_min": round(eta) if row["status"] == WAITING else 0,
        }

    def board(self):
        """Everyone still in the building, in display order."""
        return self._rows(
            "SELECT * FROM tokens WHERE status IN (?, ?, ?) "
            "ORDER BY CASE status WHEN ? THEN 0 WHEN ? THEN 1 ELSE 2 END, token",
            (CALLED, WAITING, HELD, CALLED, WAITING),
        )

    def waiting_with_phone(self):
        return self._rows(
            "SELECT * FROM tokens WHERE status=? AND phone<>'' ORDER BY token", (WAITING,)
        )

    def wait_times(self):
        rows = self._rows(
            "SELECT token, created_at, called_at FROM tokens WHERE called_at IS NOT NULL ORDER BY token"
        )
        return [
            {"token": f"#{r['token']}", "wait_min": round((r["called_at"] - r["created_at"]) / 60, 1)}
            for r in rows
        ]

    def stats(self):
        def count(status):
            return self.db.execute("SELECT COUNT(*) FROM tokens WHERE status=?", (status,)).fetchone()[0]

        total = self.db.execute("SELECT COUNT(*) FROM tokens").fetchone()[0]
        served = count(DONE)
        holds = self.db.execute("SELECT COALESCE(SUM(holds), 0) FROM tokens").fetchone()[0]
        waits = self.wait_times()
        avg_wait = round(sum(w["wait_min"] for w in waits) / len(waits), 1) if waits else 0.0
        cur = self.current()
        return {
            "total": total,
            "served": served,
            "waiting": count(WAITING),
            "held": count(HELD),
            "serving": cur["token"] if cur else None,
            "avg_wait_min": avg_wait,
            "avg_service_min": round(self.avg_service_min(), 1),
            "no_show_pct": round(100 * holds / max(1, holds + served), 1),
        }
