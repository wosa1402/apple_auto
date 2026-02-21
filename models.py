import os
import sqlite3
import threading
from datetime import datetime


class Database:
    def __init__(self, db_path):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._local = threading.local()
        self._init_db()

    def _get_conn(self):
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return self._local.conn

    def _init_db(self):
        conn = self._get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                password TEXT NOT NULL DEFAULT '',
                remark TEXT DEFAULT '',
                dob TEXT NOT NULL,
                question1 TEXT NOT NULL DEFAULT '',
                answer1 TEXT NOT NULL DEFAULT '',
                question2 TEXT NOT NULL DEFAULT '',
                answer2 TEXT NOT NULL DEFAULT '',
                question3 TEXT NOT NULL DEFAULT '',
                answer3 TEXT NOT NULL DEFAULT '',
                check_interval INTEGER NOT NULL DEFAULT 30,
                message TEXT NOT NULL DEFAULT '',
                last_check TEXT NOT NULL DEFAULT '2000-01-01 00:00:00',
                enable_check_password_correct INTEGER NOT NULL DEFAULT 0,
                enable_delete_devices INTEGER NOT NULL DEFAULT 0,
                enable_auto_update_password INTEGER NOT NULL DEFAULT 0,
                fail_retry INTEGER NOT NULL DEFAULT 1,
                proxy_id INTEGER DEFAULT NULL,
                enabled INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS proxies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                protocol TEXT NOT NULL DEFAULT 'http',
                content TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL,
                status INTEGER NOT NULL DEFAULT 0,
                message TEXT DEFAULT '',
                ip TEXT DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS proxy_blacklist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ip TEXT NOT NULL UNIQUE,
                reason TEXT DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            );
        """)
        conn.commit()

    # ── Account CRUD ──

    def list_accounts(self):
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT a.*, p.protocol AS proxy_protocol, p.content AS proxy_content "
            "FROM accounts a LEFT JOIN proxies p ON a.proxy_id = p.id "
            "ORDER BY a.id"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_account(self, account_id):
        conn = self._get_conn()
        row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        return dict(row) if row else None

    def find_account_by_username(self, username):
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM accounts WHERE username = ?", (username,)
        ).fetchone()
        return dict(row) if row else None

    def export_accounts_raw(self):
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT id, username, password, remark, dob,
                      question1, answer1, question2, answer2, question3, answer3,
                      check_interval, enable_check_password_correct, enable_delete_devices,
                      enable_auto_update_password, fail_retry, proxy_id, enabled
               FROM accounts ORDER BY id"""
        ).fetchall()
        return [dict(r) for r in rows]

    def create_account(self, data):
        conn = self._get_conn()
        conn.execute(
            """INSERT INTO accounts
               (username, password, remark, dob, question1, answer1,
                question2, answer2, question3, answer3, check_interval,
                enable_check_password_correct, enable_delete_devices,
                enable_auto_update_password, fail_retry, proxy_id, enabled)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                data["username"], data["password"], data.get("remark", ""),
                data["dob"], data["question1"], data["answer1"],
                data["question2"], data["answer2"],
                data["question3"], data["answer3"],
                int(data.get("check_interval", 30)),
                int(data.get("enable_check_password_correct", 0)),
                int(data.get("enable_delete_devices", 0)),
                int(data.get("enable_auto_update_password", 0)),
                int(data.get("fail_retry", 1)),
                int(data["proxy_id"]) if data.get("proxy_id") else None,
                int(data.get("enabled", 1)),
            ),
        )
        conn.commit()

    def update_account(self, account_id, data):
        conn = self._get_conn()
        conn.execute(
            """UPDATE accounts SET
               username=?, password=?, remark=?, dob=?,
               question1=?, answer1=?, question2=?, answer2=?,
               question3=?, answer3=?, check_interval=?,
               enable_check_password_correct=?, enable_delete_devices=?,
               enable_auto_update_password=?, fail_retry=?,
               proxy_id=?, enabled=?
               WHERE id=?""",
            (
                data["username"], data["password"], data.get("remark", ""),
                data["dob"], data["question1"], data["answer1"],
                data["question2"], data["answer2"],
                data["question3"], data["answer3"],
                int(data.get("check_interval", 30)),
                int(data.get("enable_check_password_correct", 0)),
                int(data.get("enable_delete_devices", 0)),
                int(data.get("enable_auto_update_password", 0)),
                int(data.get("fail_retry", 1)),
                int(data["proxy_id"]) if data.get("proxy_id") else None,
                int(data.get("enabled", 1)),
                account_id,
            ),
        )
        conn.commit()

    def delete_account(self, account_id):
        conn = self._get_conn()
        conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
        conn.commit()

    def toggle_account(self, account_id):
        conn = self._get_conn()
        conn.execute(
            "UPDATE accounts SET enabled = CASE WHEN enabled = 1 THEN 0 ELSE 1 END WHERE id = ?",
            (account_id,),
        )
        conn.commit()

    def disable_account(self, username):
        conn = self._get_conn()
        conn.execute("UPDATE accounts SET enabled = 0 WHERE username = ?", (username,))
        conn.commit()

    def get_due_accounts(self):
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT * FROM accounts
               WHERE enabled = 1
               AND datetime('now','localtime') >= datetime(last_check, '+' || check_interval || ' minutes')
               ORDER BY last_check ASC"""
        ).fetchall()
        return [dict(r) for r in rows]

    def update_after_check(self, account_id, message, password=None):
        conn = self._get_conn()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if password:
            conn.execute(
                "UPDATE accounts SET last_check=?, message=?, password=? WHERE id=?",
                (now, message, password, account_id),
            )
        else:
            conn.execute(
                "UPDATE accounts SET last_check=?, message=? WHERE id=?",
                (now, message, account_id),
            )
        conn.commit()

    def update_account_message(self, username, message):
        conn = self._get_conn()
        conn.execute("UPDATE accounts SET message=? WHERE username=?", (message, username))
        conn.commit()

    def update_account_password(self, username, password):
        conn = self._get_conn()
        conn.execute("UPDATE accounts SET password=? WHERE username=?", (password, username))
        conn.commit()

    # ── Proxy CRUD ──

    def list_proxies(self):
        conn = self._get_conn()
        rows = conn.execute("SELECT * FROM proxies ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def get_proxy(self, proxy_id):
        conn = self._get_conn()
        row = conn.execute("SELECT * FROM proxies WHERE id = ?", (proxy_id,)).fetchone()
        return dict(row) if row else None

    def create_proxy(self, data):
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO proxies (protocol, content, enabled) VALUES (?,?,?)",
            (data["protocol"], data["content"], int(data.get("enabled", 1))),
        )
        conn.commit()

    def update_proxy(self, proxy_id, data):
        conn = self._get_conn()
        conn.execute(
            "UPDATE proxies SET protocol=?, content=?, enabled=? WHERE id=?",
            (data["protocol"], data["content"], int(data.get("enabled", 1)), proxy_id),
        )
        conn.commit()

    def delete_proxy(self, proxy_id):
        conn = self._get_conn()
        conn.execute("UPDATE accounts SET proxy_id = NULL WHERE proxy_id = ?", (proxy_id,))
        conn.execute("DELETE FROM proxies WHERE id = ?", (proxy_id,))
        conn.commit()

    def find_proxy_by_content(self, protocol, content):
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM proxies WHERE protocol = ? AND content = ?",
            (protocol, content),
        ).fetchone()
        return dict(row) if row else None

    def import_proxy(self, data):
        conn = self._get_conn()
        cursor = conn.execute(
            "INSERT INTO proxies (protocol, content, enabled) VALUES (?,?,?)",
            (data["protocol"], data["content"], int(data.get("enabled", 1))),
        )
        conn.commit()
        return cursor.lastrowid

    def disable_proxy(self, proxy_id):
        conn = self._get_conn()
        conn.execute("UPDATE proxies SET enabled = 0 WHERE id = ?", (proxy_id,))
        conn.commit()

    # ── Records ──

    def add_record(self, account_id, status, message, ip=""):
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO records (account_id, status, message, ip) VALUES (?,?,?,?)",
            (account_id, int(status), message, ip),
        )
        conn.commit()

    def list_records(self, page=1, per_page=50):
        conn = self._get_conn()
        offset = (page - 1) * per_page
        rows = conn.execute(
            """SELECT r.*, a.username
               FROM records r LEFT JOIN accounts a ON r.account_id = a.id
               ORDER BY r.id DESC LIMIT ? OFFSET ?""",
            (per_page, offset),
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
        return {
            "items": [dict(r) for r in rows],
            "total": total,
            "page": page,
            "per_page": per_page,
            "total_pages": max(1, (total + per_page - 1) // per_page),
        }

    # ── Settings ──

    def get_setting(self, key, default=""):
        conn = self._get_conn()
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key, value):
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )
        conn.commit()

    def get_all_settings(self):
        conn = self._get_conn()
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return {r["key"]: r["value"] for r in rows}

    # ── Proxy Blacklist ──

    def add_blacklist(self, ip, reason=""):
        conn = self._get_conn()
        conn.execute(
            "INSERT OR IGNORE INTO proxy_blacklist (ip, reason) VALUES (?, ?)",
            (ip, reason),
        )
        conn.commit()

    def is_blacklisted(self, ip):
        conn = self._get_conn()
        row = conn.execute(
            "SELECT 1 FROM proxy_blacklist WHERE ip = ?", (ip,)
        ).fetchone()
        return row is not None

    def list_blacklist(self):
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM proxy_blacklist ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def clear_blacklist(self):
        conn = self._get_conn()
        conn.execute("DELETE FROM proxy_blacklist")
        conn.commit()
