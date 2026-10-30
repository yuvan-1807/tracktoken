"""User accounts and roles: patient, staff, admin.

Passwords are stored as PBKDF2-SHA256 hashes with a random salt, never in plain text.
"""
import hashlib
import hmac
import secrets
import sqlite3
import time

ROLES = ("patient", "staff", "admin")
ITERATIONS = 200_000

def _hash(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), ITERATIONS).hex()


class AuthStore:
    def __init__(self, db: sqlite3.Connection):
        self.db = db
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                username   TEXT UNIQUE NOT NULL,
                full_name  TEXT NOT NULL,
                phone      TEXT,
                role       TEXT NOT NULL,
                salt       TEXT NOT NULL,
                pw_hash    TEXT NOT NULL,
                active     INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL
            );
            """
        )
        db.commit()

    def has_role(self, role):
        return self.db.execute("SELECT 1 FROM users WHERE role=? LIMIT 1", (role,)).fetchone() is not None

    def ensure_admin(self, username, password):
        """Create the first admin from secrets, once. Does nothing if an admin already exists."""
        if not username or not password or self.has_role("admin"):
            return False
        self.create(username, password, "Clinic admin", "admin")
        return True

    def create(self, username, password, full_name, role, phone=""):
        username = (username or "").strip().lower()
        full_name = (full_name or "").strip()
        if role not in ROLES:
            raise ValueError("Unknown role.")
        if not username or not full_name:
            raise ValueError("Name and username are required.")
        if len(password or "") < 6:
            raise ValueError("Password must be at least 6 characters.")
        salt = secrets.token_hex(16)
        try:
            self.db.execute(
                "INSERT INTO users(username, full_name, phone, role, salt, pw_hash, created_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?)",
                (username, full_name, (phone or "").strip(), role, salt, _hash(password, salt), time.time()),
            )
            self.db.commit()
        except sqlite3.IntegrityError:
            raise ValueError("That username is already taken.")

    def authenticate(self, username, password):
        row = self.db.execute(
            "SELECT * FROM users WHERE username=? AND active=1", ((username or "").strip().lower(),)
        ).fetchone()
        if not row:
            return None
        if not hmac.compare_digest(_hash(password or "", row["salt"]), row["pw_hash"]):
            return None
        return {k: row[k] for k in ("id", "username", "full_name", "phone", "role")}

    def list_users(self):
        rows = self.db.execute(
            "SELECT id, username, full_name, phone, role, active FROM users ORDER BY role, username"
        ).fetchall()
        return [dict(r) for r in rows]

    def set_active(self, user_id, active):
        self.db.execute("UPDATE users SET active=? WHERE id=?", (1 if active else 0, user_id))
        self.db.commit()
