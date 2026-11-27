import contextlib
import json
import os
import sqlite3
import threading
import time
from typing import Any

import config

_LOCAL = threading.local()


def _connect() -> sqlite3.Connection:
    db_path = config.DATABASE_PATH
    parent = os.path.dirname(os.path.abspath(db_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30.0, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.OperationalError:
        pass
    return conn


def get_conn() -> sqlite3.Connection:
    conn = getattr(_LOCAL, "conn", None)
    if conn is None:
        conn = _connect()
        _LOCAL.conn = conn
    return conn


@contextlib.contextmanager
def tx():
    conn = get_conn()
    started_here = False
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
        started_here = True
    try:
        yield conn
        if started_here and conn.in_transaction:
            conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
        raise


def q_one(sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    row = get_conn().execute(sql, params).fetchone()
    return dict(row) if row else None


def q_all(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    rows = get_conn().execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name = ?",
        (table,),
    ).fetchone()
    return bool(row)


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(conn, table):
        return set()
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r["name"] for r in rows}


def _seed_catalog_if_empty(conn: sqlite3.Connection) -> None:
    cat_count = conn.execute("SELECT COUNT(*) AS c FROM categories").fetchone()["c"]
    if int(cat_count) == 0:
        conn.executemany(
            "INSERT INTO categories (slug, title, description, sort_order, is_active) VALUES (?, ?, ?, ?, 1)",
            [
                ("stars", "Telegram Stars", "Звёзды Telegram на указанный аккаунт", 1),
                ("premium", "Telegram Premium", "Подписка Telegram Premium", 2),
                ("gifts", "Telegram Gifts", "Подарки Telegram", 3),
            ],
        )

    prod_count = conn.execute("SELECT COUNT(*) AS c FROM products").fetchone()["c"]
    if int(prod_count) == 0:
        cats = {
            r["slug"]: r["id"]
            for r in conn.execute("SELECT id, slug FROM categories").fetchall()
        }
        seed_items = [
            (cats.get("stars"), "STAR-100", "100 Telegram Stars", "100 звёзд на @username", 25000, "auto", 1),
            (cats.get("stars"), "STAR-500", "500 Telegram Stars", "500 звёзд на @username", 120000, "auto", 2),
            (cats.get("premium"), "PREM-3M", "Telegram Premium 3 мес.", "Подписка на 3 месяца", 185000, "manual", 3),
            (cats.get("premium"), "PREM-12M", "Telegram Premium 12 мес.", "Подписка на 12 месяцев", 640000, "manual", 4),
            (cats.get("gifts"), "GIFT-ROSE", "Подарок «Золотая роза»", "Подарок в Telegram", 45000, "manual", 5),
            (cats.get("gifts"), "GIFT-STARBOX", "Подарок «Звёздный бокс»", "Подарок в Telegram", 95000, "manual", 6),
        ]
        valid_items = [(c, sku, title, desc, price, mode) for c, sku, title, desc, price, mode, _ in seed_items if c is not None]
        conn.executemany(
            """
            INSERT INTO products (category_id, sku, title, description, price_uzs, fulfillment_mode, is_active, sort_order)
            VALUES (?, ?, ?, ?, ?, ?, 1, ?)
            """,
            [(c, sku, title, desc, price, mode, idx) for idx, (c, sku, title, desc, price, mode) in enumerate(valid_items, 1)],
        )


def _migrate_legacy_tables(conn: sqlite3.Connection) -> None:
    if _table_exists(conn, "orders"):
        cols = _table_columns(conn, "orders")
        if "total_amount" in cols and "recipient" in cols:
            conn.execute(
                """
                INSERT OR IGNORE INTO legacy_orders (id, user_id, recipient, total_amount, currency, status, created_at, updated_at)
                SELECT id, user_id, recipient, total_amount, COALESCE(currency, 'XTR'), status, created_at, updated_at
                FROM orders
                """
            )
    if _table_exists(conn, "order_items"):
        cols = _table_columns(conn, "order_items")
        if {"order_id", "title", "price", "qty"}.issubset(cols):
            conn.execute(
                """
                INSERT OR IGNORE INTO legacy_order_items (id, order_id, product_id, title, price, qty)
                SELECT id, order_id, product_id, title, price, qty
                FROM order_items
                """
            )

    prod_cols = _table_columns(conn, "products")
    if prod_cols and "price_uzs" not in prod_cols and "price" in prod_cols:
        conn.execute("ALTER TABLE products ADD COLUMN price_uzs INTEGER NOT NULL DEFAULT 0")
        conn.execute("UPDATE products SET price_uzs = COALESCE(price, 0)")
    if prod_cols and "fulfillment_mode" not in prod_cols:
        conn.execute("ALTER TABLE products ADD COLUMN fulfillment_mode TEXT NOT NULL DEFAULT 'auto'")
    if prod_cols and "sku" not in prod_cols:
        conn.execute("ALTER TABLE products ADD COLUMN sku TEXT")
        conn.execute("UPDATE products SET sku = 'SKU-' || id WHERE sku IS NULL OR sku = ''")


def _run_one_time_wallet_reset(conn: sqlite3.Connection) -> None:
    """Обнуляет балансы и бонусные балансы существующих пользователей строго один раз."""
    flag = conn.execute(
        "SELECT value FROM schema_state WHERE key = 'wallet_balances_reset_v1'"
    ).fetchone()
    if flag:
        return

    now = int(time.time())
    existing_users = conn.execute("SELECT telegram_id FROM users").fetchall()
    for u in existing_users:
        uid = int(u["telegram_id"])
        conn.execute(
            """
            INSERT INTO wallets (user_id, balance_uzs, bonus_uzs, updated_at)
            VALUES (?, 0, 0, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                balance_uzs = 0,
                bonus_uzs = 0,
                updated_at = excluded.updated_at
            """,
            (uid, now),
        )
        conn.execute(
            """
            INSERT INTO wallet_transactions
                (user_id, kind, amount_uzs, bonus_delta_uzs, balance_after_uzs, bonus_after_uzs, ref_type, ref_id, note, created_at)
            VALUES (?, 'reset', 0, 0, 0, 0, 'system', 'wallet_init_v1', 'Однократный сброс баланса при запуске кошельков', ?)
            """,
            (uid, now),
        )

    conn.execute(
        """
        INSERT INTO schema_state (key, value, updated_at)
        VALUES ('wallet_balances_reset_v1', 'done', ?)
        ON CONFLICT(key) DO UPDATE SET value = 'done', updated_at = excluded.updated_at
        """,
        (now,),
    )


def init_db() -> None:
    conn = get_conn()
    if conn.in_transaction:
        conn.execute("COMMIT")

    # DDL-скрипт выполняется отдельно, так как SQLite неявно завершает транзакцию при DDL
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            language_code TEXT,
            is_blocked INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT UNIQUE NOT NULL,
            title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            sort_order INTEGER NOT NULL DEFAULT 0,
            is_active INTEGER NOT NULL DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category_id INTEGER NOT NULL REFERENCES categories(id),
            sku TEXT UNIQUE NOT NULL,
            title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            price_uzs INTEGER NOT NULL CHECK(price_uzs >= 0),
            fulfillment_mode TEXT NOT NULL DEFAULT 'auto',
            is_active INTEGER NOT NULL DEFAULT 1,
            sort_order INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS cart_items (
            user_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL REFERENCES products(id),
            qty INTEGER NOT NULL CHECK(qty > 0),
            PRIMARY KEY (user_id, product_id)
        );

        CREATE TABLE IF NOT EXISTS legacy_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            recipient TEXT NOT NULL,
            total_amount INTEGER NOT NULL,
            currency TEXT NOT NULL DEFAULT 'XTR',
            status TEXT NOT NULL DEFAULT 'pending',
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS legacy_order_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL REFERENCES legacy_orders(id),
            product_id INTEGER,
            title TEXT NOT NULL,
            price INTEGER NOT NULL,
            qty INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            currency TEXT NOT NULL,
            amount INTEGER NOT NULL,
            payload TEXT,
            telegram_charge_id TEXT UNIQUE,
            provider_charge_id TEXT,
            status TEXT NOT NULL DEFAULT 'paid',
            problem TEXT,
            created_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS wallets (
            user_id INTEGER PRIMARY KEY,
            balance_uzs INTEGER NOT NULL DEFAULT 0 CHECK(balance_uzs >= 0),
            bonus_uzs INTEGER NOT NULL DEFAULT 0 CHECK(bonus_uzs >= 0),
            updated_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS wallet_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            amount_uzs INTEGER NOT NULL DEFAULT 0,
            bonus_delta_uzs INTEGER NOT NULL DEFAULT 0,
            balance_after_uzs INTEGER NOT NULL DEFAULT 0,
            bonus_after_uzs INTEGER NOT NULL DEFAULT 0,
            ref_type TEXT,
            ref_id TEXT,
            note TEXT,
            created_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS topups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount_uzs INTEGER NOT NULL CHECK(amount_uzs > 0),
            requisites TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            receipt_file_id TEXT,
            receipt_caption TEXT,
            admin_id INTEGER,
            admin_comment TEXT,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            decided_at INTEGER
        );

        CREATE TABLE IF NOT EXISTS wallet_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_uid TEXT UNIQUE NOT NULL,
            user_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL REFERENCES products(id),
            sku TEXT NOT NULL,
            product_title TEXT NOT NULL,
            category_slug TEXT NOT NULL,
            fulfillment_mode TEXT NOT NULL,
            recipient TEXT NOT NULL,
            qty INTEGER NOT NULL CHECK(qty > 0),
            unit_price_uzs INTEGER NOT NULL CHECK(unit_price_uzs >= 0),
            total_uzs INTEGER NOT NULL CHECK(total_uzs >= 0),
            paid_from_balance_uzs INTEGER NOT NULL DEFAULT 0,
            paid_from_bonus_uzs INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'paid',
            refunded INTEGER NOT NULL DEFAULT 0,
            admin_id INTEGER,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS admin_audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            target_type TEXT NOT NULL,
            target_id TEXT NOT NULL,
            details TEXT,
            created_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS rate_limit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bucket_key TEXT NOT NULL,
            created_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_rl_bucket_time ON rate_limit_events(bucket_key, created_at);

        CREATE TABLE IF NOT EXISTS used_init_nonces (
            nonce_hash TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            created_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS fsm_states (
            user_id INTEGER PRIMARY KEY,
            state TEXT NOT NULL,
            data_json TEXT NOT NULL DEFAULT '{}',
            updated_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS inbox_updates (
            update_id INTEGER PRIMARY KEY,
            payload TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            error TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            updated_at INTEGER NOT NULL
        );
        """
    )

    with tx() as c:
        _migrate_legacy_tables(c)
        c.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('support_username', 'GoldStar_uz_bot')"
        )
        c.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('support_text', 'Напишите нам по вопросам заказов и пополнений баланса.')"
        )
        _seed_catalog_if_empty(c)
        _run_one_time_wallet_reset(c)


def ensure_wallet(conn: sqlite3.Connection, user_id: int) -> dict[str, int]:
    uid = int(user_id)
    now = int(time.time())
    conn.execute(
        """
        INSERT OR IGNORE INTO wallets (user_id, balance_uzs, bonus_uzs, updated_at)
        VALUES (?, 0, 0, ?)
        """,
        (uid, now),
    )
    row = conn.execute(
        "SELECT balance_uzs, bonus_uzs FROM wallets WHERE user_id = ?",
        (uid,),
    ).fetchone()
    return {"balance_uzs": int(row["balance_uzs"]), "bonus_uzs": int(row["bonus_uzs"])}


def upsert_user(tg_user: Any) -> None:
    if not tg_user:
        return
    uid = int(tg_user.id)
    now = int(time.time())
    with tx() as conn:
        conn.execute(
            """
            INSERT INTO users (telegram_id, username, first_name, last_name, language_code, is_blocked, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 0, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name,
                last_name = excluded.last_name,
                language_code = COALESCE(excluded.language_code, users.language_code),
                updated_at = excluded.updated_at
            """,
            (
                uid,
                getattr(tg_user, "username", None),
                getattr(tg_user, "first_name", None),
                getattr(tg_user, "last_name", None),
                getattr(tg_user, "language_code", None),
                now,
                now,
            ),
        )
        ensure_wallet(conn, uid)


def check_and_record_rate_limit(bucket_key: str, limit: int, window_sec: int) -> bool:
    now = int(time.time())
    cutoff = now - int(window_sec)
    with tx() as conn:
        conn.execute(
            "DELETE FROM rate_limit_events WHERE bucket_key = ? AND created_at < ?",
            (bucket_key, cutoff),
        )
        cnt = conn.execute(
            "SELECT COUNT(*) AS c FROM rate_limit_events WHERE bucket_key = ? AND created_at >= ?",
            (bucket_key, cutoff),
        ).fetchone()["c"]
        if int(cnt) >= int(limit):
            return False
        conn.execute(
            "INSERT INTO rate_limit_events (bucket_key, created_at) VALUES (?, ?)",
            (bucket_key, now),
        )
        return True


def consume_init_nonce_once(nonce_hash: str, user_id: int, ttl_sec: int = 120) -> bool:
    now = int(time.time())
    cutoff = now - int(ttl_sec)
    with tx() as conn:
        conn.execute("DELETE FROM used_init_nonces WHERE created_at < ?", (cutoff,))
        cur = conn.execute(
            "INSERT OR IGNORE INTO used_init_nonces (nonce_hash, user_id, created_at) VALUES (?, ?, ?)",
            (nonce_hash, int(user_id), now),
        )
        return cur.rowcount > 0


def get_setting(key: str, default: str = "") -> str:
    row = q_one("SELECT value FROM settings WHERE key = ?", (key,))
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with tx() as conn:
        conn.execute(
            """
            INSERT INTO settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )


def fsm_set(user_id: int, state: str, data: dict[str, Any] | None = None) -> None:
    now = int(time.time())
    payload = json.dumps(data or {}, ensure_ascii=False)
    with tx() as conn:
        ensure_wallet(conn, int(user_id))
        conn.execute(
            """
            INSERT INTO fsm_states (user_id, state, data_json, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                state = excluded.state,
                data_json = excluded.data_json,
                updated_at = excluded.updated_at
            """,
            (int(user_id), state, payload, now),
        )


def fsm_get(user_id: int) -> dict[str, Any] | None:
    row = q_one("SELECT state, data_json, updated_at FROM fsm_states WHERE user_id = ?", (int(user_id),))
    if not row:
        return None
    try:
        data = json.loads(row["data_json"] or "{}")
    except json.JSONDecodeError:
        data = {}
    return {"state": row["state"], "data": data, "updated_at": int(row["updated_at"])}


def fsm_clear(user_id: int) -> None:
    with tx() as conn:
        conn.execute("DELETE FROM fsm_states WHERE user_id = ?", (int(user_id),))


def inbox_add(update_id: int, raw_payload: str) -> bool:
    now = int(time.time())
    with tx() as conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO inbox_updates (update_id, payload, status, attempts, updated_at)
            VALUES (?, ?, 'pending', 0, ?)
            """,
            (int(update_id), raw_payload, now),
        )
        return cur.rowcount > 0


def inbox_claim(update_id: int) -> dict[str, Any] | None:
    now = int(time.time())
    with tx() as conn:
        row = conn.execute(
            "SELECT update_id, payload, status, attempts FROM inbox_updates WHERE update_id = ?",
            (int(update_id),),
        ).fetchone()
        if not row or row["status"] == "done":
            return None
        conn.execute(
            """
            UPDATE inbox_updates
            SET status = 'processing', attempts = attempts + 1, updated_at = ?
            WHERE update_id = ?
            """,
            (now, int(update_id)),
        )
        return {"update_id": int(row["update_id"]), "payload": row["payload"]}


def inbox_done(update_id: int) -> None:
    now = int(time.time())
    with tx() as conn:
        conn.execute(
            "UPDATE inbox_updates SET status = 'done', error = NULL, updated_at = ? WHERE update_id = ?",
            (now, int(update_id)),
        )


def inbox_fail(update_id: int, err: str) -> None:
    now = int(time.time())
    with tx() as conn:
        conn.execute(
            "UPDATE inbox_updates SET status = 'failed', error = ?, updated_at = ? WHERE update_id = ?",
            (err[:1000], now, int(update_id)),
        )


def inbox_recover(startup: bool = False) -> list[int]:
    cutoff = int(time.time()) - (5 if startup else 120)
    rows = q_all(
        """
        SELECT update_id FROM inbox_updates
        WHERE status IN ('pending', 'failed') OR (status = 'processing' AND updated_at <= ?)
        ORDER BY update_id ASC
        LIMIT 200
        """,
        (cutoff,),
    )
    return [int(r["update_id"]) for r in rows]


def inbox_cleanup() -> None:
    cutoff = int(time.time()) - 7 * 86400
    with tx() as conn:
        conn.execute("DELETE FROM inbox_updates WHERE status = 'done' AND updated_at < ?", (cutoff,))