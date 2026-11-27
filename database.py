# -*- coding: utf-8 -*-
"""
Gold Star Store — база данных.

SQLite, WAL, безопасные транзакции, миграции.
Не удаляет существующие данные.
Не обнуляет кошельки.
Не добавляет демонстрационные товары.
"""

import os
import sqlite3
import threading
import time
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

import config


_LOCAL = threading.local()


def _database_path() -> str:
    path = getattr(config, "DATABASE_PATH", None)
    if not path:
        path = os.getenv("DATABASE_PATH", "gold_star.db")
    return os.path.abspath(path)


def _connect() -> sqlite3.Connection:
    path = _database_path()
    parent = os.path.dirname(path)

    if parent:
        os.makedirs(parent, exist_ok=True)

    conn = sqlite3.connect(
        path,
        timeout=30,
        isolation_level=None,
        check_same_thread=False,
    )

    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")

    return conn


def get_connection() -> sqlite3.Connection:
    conn = getattr(_LOCAL, "connection", None)

    if conn is None:
        conn = _connect()
        _LOCAL.connection = conn
        _LOCAL.transaction_depth = 0

    return conn


def close_connection() -> None:
    conn = getattr(_LOCAL, "connection", None)

    if conn is not None:
        try:
            conn.close()
        finally:
            _LOCAL.connection = None
            _LOCAL.transaction_depth = 0


@contextmanager
def transaction(immediate: bool = True):
    """
    Транзакция с поддержкой вложенных вызовов.
    """

    conn = get_connection()
    depth = getattr(_LOCAL, "transaction_depth", 0)

    savepoint = f"sp_{depth}"

    try:
        if depth == 0:
            conn.execute(
                "BEGIN IMMEDIATE" if immediate else "BEGIN"
            )
        else:
            conn.execute(f"SAVEPOINT {savepoint}")

        _LOCAL.transaction_depth = depth + 1

        yield conn

        if depth == 0:
            conn.commit()
        else:
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")

    except Exception:
        if depth == 0:
            conn.rollback()
        else:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise

    finally:
        _LOCAL.transaction_depth = depth


def execute(sql: str, params: tuple = ()) -> sqlite3.Cursor:
    return get_connection().execute(sql, params)


def executemany(sql: str, params: list) -> sqlite3.Cursor:
    return get_connection().executemany(sql, params)


def fetchone(sql: str, params: tuple = ()):
    return execute(sql, params).fetchone()


def fetchall(sql: str, params: tuple = ()):
    return execute(sql, params).fetchall()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _columns(table: str) -> set[str]:
    rows = fetchall(f"PRAGMA table_info({table})")
    return {row["name"] for row in rows}


def _add_column(table: str, name: str, definition: str) -> None:
    if name not in _columns(table):
        execute(
            f"ALTER TABLE {table} ADD COLUMN {name} {definition}"
        )


def init_db() -> None:
    """
    Создаёт отсутствующие таблицы и выполняет
    безопасные добавочные миграции.
    """

    schema = """
    CREATE TABLE IF NOT EXISTS schema_state (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS users (
        telegram_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        last_name TEXT,
        language_code TEXT,
        is_premium INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS categories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        slug TEXT NOT NULL UNIQUE,
        title TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        sort_order INTEGER NOT NULL DEFAULT 0,
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        category_id INTEGER,
        sku TEXT UNIQUE,
        title TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        price_uzs INTEGER NOT NULL DEFAULT 0,
        price_xtr INTEGER NOT NULL DEFAULT 0,
        fulfillment_mode TEXT NOT NULL DEFAULT 'manual',
        active INTEGER NOT NULL DEFAULT 1,
        bonus_eligible INTEGER NOT NULL DEFAULT 1,
        stock INTEGER,
        image_url TEXT,
        sort_order INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(category_id) REFERENCES categories(id)
            ON DELETE SET NULL
    );

    CREATE TABLE IF NOT EXISTS cart_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        product_id INTEGER NOT NULL,
        qty INTEGER NOT NULL DEFAULT 1,
        recipient TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(user_id, product_id),
        FOREIGN KEY(user_id) REFERENCES users(telegram_id)
            ON DELETE CASCADE,
        FOREIGN KEY(product_id) REFERENCES products(id)
            ON DELETE RESTRICT
    );

    CREATE TABLE IF NOT EXISTS wallets (
        user_id INTEGER PRIMARY KEY,
        balance_uzs INTEGER NOT NULL DEFAULT 0,
        bonus_uzs INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(user_id) REFERENCES users(telegram_id)
            ON DELETE CASCADE,
        CHECK(balance_uzs >= 0),
        CHECK(bonus_uzs >= 0)
    );

    CREATE TABLE IF NOT EXISTS wallet_transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        transaction_type TEXT NOT NULL,
        amount_uzs INTEGER NOT NULL,
        balance_delta INTEGER NOT NULL DEFAULT 0,
        bonus_delta INTEGER NOT NULL DEFAULT 0,
        balance_after INTEGER NOT NULL DEFAULT 0,
        bonus_after INTEGER NOT NULL DEFAULT 0,
        reference_type TEXT,
        reference_id TEXT,
        description TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        FOREIGN KEY(user_id) REFERENCES users(telegram_id)
            ON DELETE RESTRICT
    );

    CREATE TABLE IF NOT EXISTS topups (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        amount_uzs INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        receipt_file_id TEXT,
        receipt_note TEXT NOT NULL DEFAULT '',
        admin_id INTEGER,
        admin_comment TEXT NOT NULL DEFAULT '',
        payment_reference TEXT,
        idempotency_key TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        confirmed_at TEXT,
        FOREIGN KEY(user_id) REFERENCES users(telegram_id)
            ON DELETE RESTRICT
    );

    CREATE TABLE IF NOT EXISTS wallet_orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        product_id INTEGER NOT NULL,
        product_title TEXT NOT NULL,
        qty INTEGER NOT NULL DEFAULT 1,
        recipient TEXT NOT NULL DEFAULT '',
        unit_price_uzs INTEGER NOT NULL,
        total_price_uzs INTEGER NOT NULL,
        bonus_used_uzs INTEGER NOT NULL DEFAULT 0,
        balance_used_uzs INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'pending',
        idempotency_key TEXT,
        admin_comment TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(user_id) REFERENCES users(telegram_id)
            ON DELETE RESTRICT,
        FOREIGN KEY(product_id) REFERENCES products(id)
            ON DELETE RESTRICT
    );

    CREATE TABLE IF NOT EXISTS legacy_orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        total_uzs INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(user_id) REFERENCES users(telegram_id)
            ON DELETE RESTRICT
    );

    CREATE TABLE IF NOT EXISTS legacy_order_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER NOT NULL,
        product_id INTEGER,
        product_title TEXT NOT NULL,
        qty INTEGER NOT NULL DEFAULT 1,
        unit_price_uzs INTEGER NOT NULL DEFAULT 0,
        FOREIGN KEY(order_id) REFERENCES legacy_orders(id)
            ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        order_id INTEGER,
        payment_type TEXT NOT NULL,
        provider TEXT NOT NULL,
        amount INTEGER NOT NULL,
        currency TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        provider_payment_id TEXT,
        telegram_charge_id TEXT UNIQUE,
        payload TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(user_id) REFERENCES users(telegram_id)
            ON DELETE RESTRICT
    );

    CREATE TABLE IF NOT EXISTS admin_audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        admin_id INTEGER NOT NULL,
        action TEXT NOT NULL,
        target_type TEXT,
        target_id TEXT,
        details TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS rate_limit_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_key TEXT NOT NULL,
        created_at REAL NOT NULL
    );

    CREATE TABLE IF NOT EXISTS used_init_nonces (
        nonce TEXT PRIMARY KEY,
        user_id INTEGER,
        created_at REAL NOT NULL
    );

    CREATE TABLE IF NOT EXISTS fsm_states (
        user_id INTEGER PRIMARY KEY,
        state TEXT NOT NULL,
        data TEXT NOT NULL DEFAULT '{}',
        updated_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS inbox_updates (
        update_id INTEGER PRIMARY KEY,
        status TEXT NOT NULL DEFAULT 'received',
        received_at TEXT NOT NULL,
        processed_at TEXT,
        error TEXT
    );
    """

    with transaction():
        conn = get_connection()
        conn.executescript(schema)

        # Добавочные миграции для ранее созданных таблиц.
        migrations = {
            "users": [
                ("username", "TEXT"),
                ("first_name", "TEXT"),
                ("last_name", "TEXT"),
                ("language_code", "TEXT"),
                ("is_premium", "INTEGER NOT NULL DEFAULT 0"),
                ("created_at", "TEXT"),
                ("updated_at", "TEXT"),
            ],
            "products": [
                ("category_id", "INTEGER"),
                ("sku", "TEXT"),
                ("description", "TEXT NOT NULL DEFAULT ''"),
                ("price_uzs", "INTEGER NOT NULL DEFAULT 0"),
                ("price_xtr", "INTEGER NOT NULL DEFAULT 0"),
                ("fulfillment_mode", "TEXT NOT NULL DEFAULT 'manual'"),
                ("active", "INTEGER NOT NULL DEFAULT 1"),
                ("bonus_eligible", "INTEGER NOT NULL DEFAULT 1"),
                ("stock", "INTEGER"),
                ("image_url", "TEXT"),
                ("sort_order", "INTEGER NOT NULL DEFAULT 0"),
                ("created_at", "TEXT"),
                ("updated_at", "TEXT"),
            ],
            "wallets": [
                ("balance_uzs", "INTEGER NOT NULL DEFAULT 0"),
                ("bonus_uzs", "INTEGER NOT NULL DEFAULT 0"),
                ("updated_at", "TEXT"),
            ],
            "topups": [
                ("receipt_file_id", "TEXT"),
                ("receipt_note", "TEXT NOT NULL DEFAULT ''"),
                ("admin_id", "INTEGER"),
                ("admin_comment", "TEXT NOT NULL DEFAULT ''"),
                ("payment_reference", "TEXT"),
                ("idempotency_key", "TEXT"),
                ("confirmed_at", "TEXT"),
                ("updated_at", "TEXT"),
            ],
            "wallet_orders": [
                ("qty", "INTEGER NOT NULL DEFAULT 1"),
                ("recipient", "TEXT NOT NULL DEFAULT ''"),
                ("unit_price_uzs", "INTEGER NOT NULL DEFAULT 0"),
                ("total_price_uzs", "INTEGER NOT NULL DEFAULT 0"),
                ("bonus_used_uzs", "INTEGER NOT NULL DEFAULT 0"),
                ("balance_used_uzs", "INTEGER NOT NULL DEFAULT 0"),
                ("idempotency_key", "TEXT"),
                ("admin_comment", "TEXT NOT NULL DEFAULT ''"),
                ("updated_at", "TEXT"),
            ],
        }

        for table, columns in migrations.items():
            for name, definition in columns:
                _add_column(table, name, definition)

        indexes = [
            """
            CREATE INDEX IF NOT EXISTS idx_products_category
            ON products(category_id, active, sort_order)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_orders_user
            ON wallet_orders(user_id, created_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_orders_status
            ON wallet_orders(status, created_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_topups_user
            ON topups(user_id, created_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_topups_status
            ON topups(status, created_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_transactions_user
            ON wallet_transactions(user_id, created_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_rate_limit_key
            ON rate_limit_events(event_key, created_at)
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_topups_idempotency
            ON topups(user_id, idempotency_key)
            WHERE idempotency_key IS NOT NULL
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_wallet_orders_idempotency
            ON wallet_orders(user_id, idempotency_key)
            WHERE idempotency_key IS NOT NULL
            """,
        ]

        for sql in indexes:
            conn.execute(sql)

        conn.execute(
            """
            INSERT INTO schema_state(key, value, updated_at)
            VALUES('database_version', '2', ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (now(),),
        )


def upsert_user(tg_user: dict[str, Any]) -> None:
    """
    Сохраняет пользователя Telegram WebApp.
    Принимает словарь user из проверенного initData.
    """

    if not isinstance(tg_user, dict):
        raise ValueError("Некорректные данные пользователя")

    user_id = int(tg_user.get("id") or 0)

    if user_id <= 0:
        raise ValueError("Некорректный Telegram ID")

    timestamp = now()

    with transaction():
        execute(
            """
            INSERT INTO users(
                telegram_id, username, first_name, last_name,
                language_code, is_premium, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name,
                last_name = excluded.last_name,
                language_code = excluded.language_code,
                is_premium = excluded.is_premium,
                updated_at = excluded.updated_at
            """,
            (
                user_id,
                tg_user.get("username"),
                tg_user.get("first_name"),
                tg_user.get("last_name"),
                tg_user.get("language_code"),
                int(bool(tg_user.get("is_premium"))),
                timestamp,
                timestamp,
            ),
        )

        execute(
            """
            INSERT OR IGNORE INTO wallets(
                user_id, balance_uzs, bonus_uzs, updated_at
            )
            VALUES (?, 0, 0, ?)
            """,
            (user_id, timestamp),
        )


def get_user(user_id: int):
    return fetchone(
        "SELECT * FROM users WHERE telegram_id = ?",
        (int(user_id),),
    )


def get_wallet(user_id: int):
    return fetchone(
        "SELECT * FROM wallets WHERE user_id = ?",
        (int(user_id),),
    )


def check_and_record_rate_limit(
    key: str,
    limit: int,
    window_sec: int,
) -> bool:
    """
    True — запрос разрешён.
    False — превышен лимит.
    """

    if not key or limit <= 0 or window_sec <= 0:
        return False

    current = time.time()
    cutoff = current - int(window_sec)

    with transaction():
        execute(
            "DELETE FROM rate_limit_events WHERE created_at < ?",
            (current - 86400,),
        )

        row = fetchone(
            """
            SELECT COUNT(*) AS total
            FROM rate_limit_events
            WHERE event_key = ? AND created_at >= ?
            """,
            (key, cutoff),
        )

        if int(row["total"]) >= int(limit):
            return False

        execute(
            """
            INSERT INTO rate_limit_events(event_key, created_at)
            VALUES (?, ?)
            """,
            (key, current),
        )

    return True


def mark_init_nonce_used(nonce: str, user_id: int | None = None) -> bool:
    """
    Защита от повторного использования nonce.
    True — nonce записан впервые.
    False — nonce уже использовался.
    """

    if not nonce:
        return False

    try:
        with transaction():
            execute(
                """
                INSERT INTO used_init_nonces(nonce, user_id, created_at)
                VALUES (?, ?, ?)
                """,
                (nonce, user_id, time.time()),
            )
        return True
    except sqlite3.IntegrityError:
        return False


def get_setting(key: str, default: str | None = None):
    row = fetchone(
        "SELECT value FROM settings WHERE key = ?",
        (key,),
    )
    return row["value"] if row else default


def set_setting(key: str, value: Any) -> None:
    with transaction():
        execute(
            """
            INSERT INTO settings(key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (key, str(value), now()),
        )


def record_admin_action(
    admin_id: int,
    action: str,
    target_type: str | None = None,
    target_id: str | None = None,
    details: Any = "",
) -> None:
    if not isinstance(details, str):
        details = json.dumps(
            details,
            ensure_ascii=False,
            default=str,
        )

    with transaction():
        execute(
            """
            INSERT INTO admin_audit_log(
                admin_id, action, target_type, target_id,
                details, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                int(admin_id),
                str(action),
                target_type,
                target_id,
                details,
                now(),
            ),
        )


# Совместимость с вариантами названий,
# используемыми в других модулях проекта.
db = get_connection
query_one = fetchone
query_all = fetchall
q_one = fetchone
q_all = fetchall
q_exec = execute