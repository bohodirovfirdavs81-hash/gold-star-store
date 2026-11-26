# -*- coding: utf-8 -*-
"""Gold Star ⭐ — Telegram-магазин цифровых товаров.

Стек: pyTelegramBotAPI (telebot) + Flask + SQLite + Webhook + Gunicorn.
Оплата: Telegram Stars (валюта XTR). Секреты — только через переменные окружения.

ЭТАП 1 (изменения относительно рабочей версии):
  * безопасная версионная миграция SQLite (резервная копия -> ALTER/CREATE IF NOT EXISTS);
  * каталог: Stars (только просмотр), Premium и Gifts (по заявке), цены в сумах;
  * заявки: создание клиентом, уведомление и управление в админ-панели;
  * /paysupport (обязательно по правилам Telegram), /backup (только админ);
  * журнал ошибок (таблица error_log);
  * Flask-маршруты, Webhook и оплата Stars для прочих товаров НЕ изменены.
"""
import functools
import hmac
import html
import logging
import os
import re
import sqlite3
import tempfile
import threading
import time
import traceback
from contextlib import closing
from datetime import datetime, timezone

import telebot
from flask import Flask, abort, request
from telebot import types
from telebot.apihelper import ApiTelegramException

# --------------------------------------------------------------------------
# Конфигурация
# --------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("goldstar")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "").strip()
RENDER_URL = os.environ.get("RENDER_URL", "").strip().rstrip("/")
DB_PATH = os.environ.get("DB_PATH", "goldstar.db").strip()
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "").strip().lstrip("@")
WEBAPP_URL = os.environ.get("WEBAPP_URL", "https://gold-star-store.onrender.com").strip()

try:
    ADMIN_ID = int(os.environ.get("ADMIN_ID", "0").strip() or 0)
except ValueError:
    ADMIN_ID = 0

if not BOT_TOKEN:
    raise RuntimeError("Не задана переменная окружения BOT_TOKEN")
if not WEBHOOK_SECRET:
    raise RuntimeError("Не задана переменная окружения WEBHOOK_SECRET")

bot = telebot.TeleBot(
    BOT_TOKEN,
    threaded=True,
    num_threads=4,
    parse_mode="HTML"
)
app = Flask(__name__)

# --------------------------------------------------------------------------
# Константы
# --------------------------------------------------------------------------
MENU_STARS = "⭐ Telegram Stars"
MENU_PREMIUM = "💎 Telegram Premium"
MENU_GIFTS = "🎁 Telegram Gifts"
MENU_MY = "📦 Мои покупки"
MENU_CATALOG = "🛒 Каталог товаров"
MENU_SUPPORT = "📞 Поддержка"
MENU_INFO = "ℹ️ Информация"
MENU_WEBAPP = "🌐 Открыть магазин"

CATS = {
    "stars": "⭐ Telegram Stars",
    "premium": "💎 Telegram Premium",
    "gifts": "🎁 Telegram Gifts",
    "other": "📦 Другие товары",
}
CAT_NOTES = {
    "stars": "ℹ️ Раздел только для просмотра: покупка Stars через бота недоступна.",
    "premium": "Выберите тариф и оформите заявку — администратор свяжется с вами.",
    "gifts": "Выберите подарок и оформите заявку — администратор свяжется с вами.",
}
NEEDS_TARGET = ("stars", "premium", "gifts")  # нужен @username получателя
# Категории, за которые бот НЕ принимает оплату (этап 1).
NON_PAYABLE_CATS = ("stars", "premium", "gifts")

STATUS = {
    "pending": "⏳ Ожидает оплаты",
    "paid": "💳 Оплачен, ждёт выдачи",
    "delivered": "✅ Выдан",
    "refunded": "↩️ Возврат",
}
EMO = {"pending": "⏳", "paid": "💳", "delivered": "✅", "refunded": "↩️"}

REQ_STATUS = {
    "new": "🆕 Новая",
    "work": "🔧 В работе",
    "done": "✅ Выполнена",
    "rejected": "❌ Отклонена",
}
REQ_EMO = {"new": "🆕", "work": "🔧", "done": "✅", "rejected": "❌"}
# в какой статус из каких статусов можно перевести заявку
REQ_TRANSITIONS = {
    "work": ("new",),
    "done": ("new", "work"),
    "rejected": ("new", "work"),
}
MODE_LABELS = {
    "xtr": "оплата Stars",
    "request": "заявка",
    "info": "только просмотр",
}
# режим продажи для товаров, добавляемых админом через бота
DEFAULT_MODE_BY_CAT = {"stars": "info", "premium": "request", "gifts": "request"}

USERNAME_RE = re.compile(r"^@?([A-Za-z0-9_]{5,32})$")

SEED_PRODUCTS = [
    # category, title, description, price (в Stars), sort
    ("stars", "⭐ 50 Stars", "Пакет 50 Telegram Stars", 50, 1),
    ("stars", "⭐ 100 Stars", "Пакет 100 Telegram Stars", 100, 2),
    ("stars", "⭐ 250 Stars", "Пакет 250 Telegram Stars", 250, 3),
    ("stars", "⭐ 500 Stars", "Пакет 500 Telegram Stars", 500, 4),
    ("stars", "⭐ 1000 Stars", "Пакет 1000 Telegram Stars", 1000, 5),
    ("premium", "💎 Premium 3 мес.", "Telegram Premium на 3 месяца", 1000, 1),
    ("premium", "💎 Premium 6 мес.", "Telegram Premium на 6 месяцев", 1800, 2),
    ("premium", "💎 Premium 12 мес.", "Telegram Premium на 12 месяцев", 3200, 3),
]

# --------------------------------------------------------------------------
# Каталог этапа 1 (миграция v1)
# sku, category, title, description, price_uzs, sale_mode, sort, legacy_title
# legacy_title — название строки, которая уже есть в базе (чтобы не плодить дубли)
# --------------------------------------------------------------------------
SCHEMA_VERSION = 1

CATALOG_V1 = [
    ("stars_50", "stars", "⭐ 50 Stars", "Пакет 50 Telegram Stars", 10490, "info", 10, "⭐ 50 Stars"),
    ("stars_75", "stars", "⭐ 75 Stars", "Пакет 75 Telegram Stars", 15690, "info", 20, None),
    ("stars_100", "stars", "⭐ 100 Stars", "Пакет 100 Telegram Stars", 20990, "info", 30, "⭐ 100 Stars"),
    ("stars_150", "stars", "⭐ 150 Stars", "Пакет 150 Telegram Stars", 31490, "info", 40, None),
    ("stars_200", "stars", "⭐ 200 Stars", "Пакет 200 Telegram Stars", 41990, "info", 50, None),
    ("stars_250", "stars", "⭐ 250 Stars", "Пакет 250 Telegram Stars", 52490, "info", 60, "⭐ 250 Stars"),
    ("stars_350", "stars", "⭐ 350 Stars", "Пакет 350 Telegram Stars", 73490, "info", 70, None),
    ("stars_500", "stars", "⭐ 500 Stars", "Пакет 500 Telegram Stars", 104990, "info", 80, "⭐ 500 Stars"),
    ("premium_3", "premium", "💎 Premium 3 мес.", "Telegram Premium на 3 месяца", 159990, "request", 10, "💎 Premium 3 мес."),
    ("premium_6", "premium", "💎 Premium 6 мес.", "Telegram Premium на 6 месяцев", 214990, "request", 20, "💎 Premium 6 мес."),
    ("premium_12", "premium", "💎 Premium 12 мес.", "Telegram Premium на 12 месяцев", 389990, "request", 30, "💎 Premium 12 мес."),
    ("gift_heart", "gifts", "❤️ Сердце", "Telegram Gift «Сердце»", 3790, "request", 10, None),
    ("gift_bear", "gifts", "🧸 Мишка", "Telegram Gift «Мишка»", 3790, "request", 20, None),
    ("gift_present", "gifts", "🎁 Подарок", "Telegram Gift «Подарок»", 5490, "request", 30, None),
    ("gift_rose", "gifts", "🌹 Роза", "Telegram Gift «Роза»", 5490, "request", 40, None),
    ("gift_cake", "gifts", "🎂 Торт", "Telegram Gift «Торт»", 10990, "request", 50, None),
    ("gift_bouquet", "gifts", "💐 Букет", "Telegram Gift «Букет»", 10990, "request", 60, None),
    ("gift_rocket", "gifts", "🚀 Ракета", "Telegram Gift «Ракета»", 10990, "request", 70, None),
    ("gift_champagne", "gifts", "🍾 Шампанское", "Telegram Gift «Шампанское»", 10990, "request", 80, None),
    ("gift_cup", "gifts", "🏆 Кубок", "Telegram Gift «Кубок»", 21990, "request", 90, None),
    ("gift_ring", "gifts", "💍 Кольцо", "Telegram Gift «Кольцо»", 21990, "request", 100, None),
    ("gift_diamond", "gifts", "💎 Бриллиант", "Telegram Gift «Бриллиант»", 21990, "request", 110, None),
]

NEW_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    product_id INTEGER,
    product_title TEXT NOT NULL,
    category TEXT,
    price_uzs INTEGER,
    target TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'new',
    admin_note TEXT DEFAULT '',
    created_at TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_requests_user ON requests(user_id);
CREATE INDEX IF NOT EXISTS idx_requests_status ON requests(status);
CREATE TABLE IF NOT EXISTS bonus_points (
    user_id INTEGER PRIMARY KEY,
    points INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS bonus_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    order_id INTEGER,
    points INTEGER NOT NULL,
    reason TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS error_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT,
    source TEXT,
    message TEXT
);
CREATE TABLE IF NOT EXISTS carts (
    user_id INTEGER PRIMARY KEY,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS cart_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    product_id INTEGER NOT NULL,
    qty INTEGER NOT NULL DEFAULT 1,
    UNIQUE(user_id, product_id)
);
"""


# --------------------------------------------------------------------------
# Утилиты
# --------------------------------------------------------------------------
def esc(value):
    return html.escape(str(value if value is not None else ""))


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def fmt_uzs(n):
    return "{:,}".format(int(n)).replace(",", " ") + " сум"


def is_admin(user_id):
    return bool(ADMIN_ID) and user_id == ADMIN_ID


def log_error(source, message):
    """Пишет ошибку в таблицу error_log (не должна ронять обработчик)."""
    try:
        q_exec(
            "INSERT INTO error_log (created_at, source, message) VALUES (?, ?, ?)",
            (now(), str(source)[:100], str(message)[:3000]),
        )
    except Exception:
        log.exception("Не удалось записать ошибку в error_log")


def safe(fn):
    """Ловит ошибки обработчика, пишет в лог и отвечает пользователю."""

    @functools.wraps(fn)
    def wrapper(obj, *args, **kwargs):
        try:
            return fn(obj, *args, **kwargs)
        except Exception:
            log.exception("Ошибка в обработчике %s", fn.__name__)
            log_error(fn.__name__, traceback.format_exc())
            try:
                if hasattr(obj, "data"):
                    bot.answer_callback_query(
                        obj.id, "⚠️ Ошибка, попробуйте ещё раз", show_alert=True
                    )
                else:
                    bot.send_message(
                        obj.chat.id,
                        "⚠️ Произошла ошибка. Попробуйте ещё раз чуть позже.",
                    )
            except Exception:
                log.exception("Не удалось сообщить об ошибке пользователю")

    return wrapper


def render(chat_id, text, markup=None, message=None):
    """Редактирует сообщение, если можно, иначе отправляет новое."""
    if message is not None:
        try:
            bot.edit_message_text(
                text, chat_id, message.message_id, reply_markup=markup
            )
            return
        except ApiTelegramException as exc:
            if "message is not modified" in str(exc):
                return
            log.info("Не удалось отредактировать сообщение: %s", exc)
    bot.send_message(chat_id, text, reply_markup=markup)


def notify_admin(text, markup=None):
    if not ADMIN_ID:
        log.warning("ADMIN_ID не задан, уведомление не отправлено: %s", text)
        return
    try:
        bot.send_message(ADMIN_ID, text, reply_markup=markup)
    except Exception:
        log.exception("Не удалось отправить уведомление администратору")


# --------------------------------------------------------------------------
# База данных (SQLite)
# --------------------------------------------------------------------------
def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.DatabaseError:
        pass
    return conn


def q_all(sql, params=()):
    with closing(get_conn()) as conn:
        return conn.execute(sql, params).fetchall()


def q_one(sql, params=()):
    with closing(get_conn()) as conn:
        return conn.execute(sql, params).fetchone()


def q_exec(sql, params=()):
    with closing(get_conn()) as conn:
        with conn:
            cur = conn.execute(sql, params)
            return cur.lastrowid, cur.rowcount


# ---- резервная копия и миграции -------------------------------------------
def backup_database():
    """Консистентная копия базы (SQLite backup API, учитывает WAL).

    Файл кладётся рядом с базой: <DB_PATH>.bak-ГГГГммдд-ЧЧММСС. Хранятся 5 последних.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    dst_path = "{}.bak-{}".format(DB_PATH, stamp)
    with closing(sqlite3.connect(DB_PATH)) as src, closing(sqlite3.connect(dst_path)) as dst:
        src.backup(dst)
    _prune_backups(keep=5)
    return dst_path


def _prune_backups(keep=5):
    try:
        dir_path = os.path.dirname(os.path.abspath(DB_PATH))
        prefix = os.path.basename(DB_PATH) + ".bak-"
        files = sorted(f for f in os.listdir(dir_path) if f.startswith(prefix))
        for name in files[:-keep]:
            try:
                os.remove(os.path.join(dir_path, name))
            except OSError:
                log.warning("Не удалось удалить старую копию %s", name)
    except Exception:
        log.exception("Ошибка очистки старых резервных копий")


def _schema_version_on_disk():
    """Версия схемы существующей базы (0 — если таблицы meta ещё нет)."""
    with closing(sqlite3.connect(DB_PATH)) as conn:
        try:
            row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            return int(row[0]) if row else 0
        except (sqlite3.OperationalError, ValueError):
            return 0


def _add_column_if_missing(conn, table, column, decl):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info({})".format(table))}
    if column in cols:
        return
    try:
        conn.execute("ALTER TABLE {} ADD COLUMN {} {}".format(table, column, decl))
    except sqlite3.OperationalError as exc:
        # другой воркер мог добавить колонку одновременно
        if "duplicate column" not in str(exc).lower():
            raise


def migrate_to_v1(conn):
    """Миграция v1. Идемпотентна: повторный запуск не создаёт дублей."""
    # 1) новые колонки products (старые данные не трогаем)
    _add_column_if_missing(conn, "products", "sku", "TEXT")
    _add_column_if_missing(conn, "products", "price_uzs", "INTEGER")
    _add_column_if_missing(conn, "products", "price_xtr", "INTEGER")
    _add_column_if_missing(conn, "products", "sale_mode", "TEXT NOT NULL DEFAULT 'xtr'")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_products_sku ON products(sku)")

    # 2) новые таблицы
    conn.executescript(NEW_TABLES_SQL)

    # 3) каталог — одной транзакцией
    with conn:
        for sku, cat, title, desc, uzs, mode, sort, legacy in CATALOG_V1:
            row = conn.execute("SELECT id FROM products WHERE sku=?", (sku,)).fetchone()
            if row is None and legacy:
                row = conn.execute(
                    "SELECT id FROM products WHERE category=? AND title=? AND sku IS NULL "
                    "ORDER BY id LIMIT 1",
                    (cat, legacy),
                ).fetchone()
            if row is not None:
                # существующую строку только размечаем; название/описание/price не меняем
                conn.execute(
                    "UPDATE products SET sku=?, price_uzs=?, sale_mode=?, active=1, sort=? "
                    "WHERE id=?",
                    (sku, uzs, mode, sort, row["id"]),
                )
            else:
                conn.execute(
                    "INSERT INTO products (sku, category, title, description, price, "
                    "price_uzs, sale_mode, active, sort) VALUES (?, ?, ?, ?, 0, ?, ?, 1, ?)",
                    (sku, cat, title, desc, uzs, mode, sort),
                )
        # пакет 1000 Stars: скрыть, не удалять
        conn.execute(
            "UPDATE products SET sku='stars_1000', sale_mode='info', active=0 "
            "WHERE category='stars' AND title='⭐ 1000 Stars' AND sku IS NULL"
        )
        # прочие товары этих разделов (добавленные админом) — без оплаты
        conn.execute("UPDATE products SET sale_mode='info' WHERE category='stars' AND sale_mode='xtr'")
        conn.execute("UPDATE products SET sale_mode='request' WHERE category='premium' AND sale_mode='xtr'")
        conn.execute("UPDATE products SET sale_mode='request' WHERE category='gifts' AND sale_mode='xtr'")
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('migrated_v1_at', ?)", (now(),)
        )


def init_db():
    existed = os.path.exists(DB_PATH) and os.path.getsize(DB_PATH) > 0

    # Резервная копия ДО любых изменений схемы (только для уже существующей базы).
    if existed and _schema_version_on_disk() < SCHEMA_VERSION:
        try:
            path = backup_database()
        except Exception as exc:
            # Миграция не выполняется без копии. Render оставит предыдущий деплой работающим.
            log.exception("Не удалось создать резервную копию базы")
            raise RuntimeError("Миграция остановлена: резервная копия не создана: {}".format(exc))
        log.info("Резервная копия базы создана: %s", path)

    with closing(get_conn()) as conn:
        with conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    created_at TEXT,
                    last_seen TEXT
                );
                CREATE TABLE IF NOT EXISTS products (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    category TEXT NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT DEFAULT '',
                    price INTEGER NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    sort INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    product_id INTEGER,
                    product_title TEXT NOT NULL,
                    category TEXT,
                    price INTEGER NOT NULL,
                    target TEXT DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    charge_id TEXT,
                    created_at TEXT,
                    paid_at TEXT,
                    delivered_at TEXT
                );
                CREATE TABLE IF NOT EXISTS payments (
                    charge_id TEXT PRIMARY KEY,
                    order_id INTEGER,
                    user_id INTEGER,
                    amount INTEGER,
                    created_at TEXT
                );
                CREATE TABLE IF NOT EXISTS states (
                    user_id INTEGER PRIMARY KEY,
                    state TEXT,
                    data TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id);
                CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
                """
            )
            count = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
            if count == 0:
                conn.executemany(
                    "INSERT INTO products (category, title, description, price, sort) "
                    "VALUES (?, ?, ?, ?, ?)",
                    SEED_PRODUCTS,
                )
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        version = int(row[0]) if row else 0
        if version < SCHEMA_VERSION:
            migrate_to_v1(conn)
            log.info("Миграция схемы v1 выполнена")
    log.info("База данных готова: %s", DB_PATH)


def upsert_user(u):
    ts = now()
    with closing(get_conn()) as conn:
        with conn:
            conn.execute(
                "INSERT INTO users (user_id, username, first_name, created_at, last_seen) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, "
                "first_name=excluded.first_name, last_seen=excluded.last_seen",
                (u.id, u.username, u.first_name, ts, ts),
            )


def get_user(user_id):
    return q_one("SELECT * FROM users WHERE user_id=?", (user_id,))


def set_state(user_id, state, data=""):
    q_exec(
        "INSERT INTO states (user_id, state, data) VALUES (?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET state=excluded.state, data=excluded.data",
        (user_id, state, str(data)),
    )


def get_state(user_id):
    row = q_one("SELECT state, data FROM states WHERE user_id=?", (user_id,))
    return (row["state"], row["data"]) if row else None


def clear_state(user_id):
    q_exec("DELETE FROM states WHERE user_id=?", (user_id,))


def get_product(pid):
    return q_one("SELECT * FROM products WHERE id=?", (pid,))


def list_products(category, active_only=True):
    sql = "SELECT * FROM products WHERE category=?"
    if active_only:
        sql += " AND active=1"
    sql += " ORDER BY sort, id"
    return q_all(sql, (category,))


def get_order(oid):
    return q_one("SELECT * FROM orders WHERE id=?", (oid,))


def get_request(rid):
    return q_one("SELECT * FROM requests WHERE id=?", (rid,))


# --------------------------------------------------------------------------
# Цены и правила продажи
# --------------------------------------------------------------------------
def price_label(p):
    """Цена для показа: Stars — для оплачиваемых, сумы — для остальных."""
    if p["sale_mode"] == "xtr":
        return "{} ⭐".format(p["price"])
    if p["price_uzs"]:
        return fmt_uzs(p["price_uzs"])
    return "по запросу"


def is_purchasable(p):
    """Можно ли купить товар через бота за Stars (этап 1: только «прочие» товары)."""
    return (
        bool(p)
        and bool(p["active"])
        and p["sale_mode"] == "xtr"
        and p["category"] not in NON_PAYABLE_CATS
        and p["price"] > 0
    )


def order_payable(o):
    """Можно ли ещё оплатить существующий неоплаченный заказ."""
    if o["category"] in NON_PAYABLE_CATS:
        return False
    if o["product_id"]:
        p = get_product(o["product_id"])
        if p and p["sale_mode"] != "xtr":
            return False
    return True


# --------------------------------------------------------------------------
# Клавиатуры и экраны пользователя
# --------------------------------------------------------------------------
def main_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    if WEBAPP_URL.startswith("https://"):
        kb.row(types.KeyboardButton(MENU_WEBAPP, web_app=types.WebAppInfo(url=WEBAPP_URL)))
    kb.add(
        types.KeyboardButton(MENU_STARS),
        types.KeyboardButton(MENU_PREMIUM),
        types.KeyboardButton(MENU_GIFTS),
        types.KeyboardButton(MENU_CATALOG),
        types.KeyboardButton(MENU_MY),
        types.KeyboardButton(MENU_SUPPORT),
        types.KeyboardButton(MENU_INFO),
    )
    return kb


def show_catalog_root(chat_id, message=None):
    kb = types.InlineKeyboardMarkup(row_width=1)
    for key, label in CATS.items():
        kb.add(types.InlineKeyboardButton(label, callback_data="cat:" + key))
    render(chat_id, "🛒 <b>Каталог товаров</b>\nВыберите раздел:", kb, message)


def show_category(chat_id, category, message=None):
    products = list_products(category)
    kb = types.InlineKeyboardMarkup(row_width=1)
    for p in products:
        kb.add(
            types.InlineKeyboardButton(
                "{} — {}".format(p["title"], price_label(p)),
                callback_data="prod:{}".format(p["id"]),
            )
        )
    kb.add(types.InlineKeyboardButton("⬅️ Каталог", callback_data="catalog"))
    head = "<b>{}</b>\n".format(esc(CATS.get(category, category)))
    note = CAT_NOTES.get(category)
    if products:
        text = head + (esc(note) + "\n\n" if note else "") + "Выберите товар:"
    else:
        text = head + "Пока нет доступных товаров."
    render(chat_id, text, kb, message)


def show_product(chat_id, p, message=None):
    kb = types.InlineKeyboardMarkup(row_width=1)
    mode = p["sale_mode"]
    if mode == "xtr":
        kb.add(
            types.InlineKeyboardButton(
                "🛍 Купить за {} ⭐".format(p["price"]),
                callback_data="buy:{}".format(p["id"]),
            )
        )
        note = ""
    elif mode == "request":
        kb.add(
            types.InlineKeyboardButton(
                "📝 Оформить заявку", callback_data="req:{}".format(p["id"])
            )
        )
        note = "\n\n📝 Оформите заявку — администратор свяжется с вами для уточнения деталей."
    else:
        note = "\n\nℹ️ Этот товар доступен только для просмотра."
    kb.add(types.InlineKeyboardButton("⬅️ Назад", callback_data="cat:" + p["category"]))
    text = "<b>{}</b>\n\n{}\n\n💰 Цена: <b>{}</b>{}".format(
        esc(p["title"]), esc(p["description"] or "—"), esc(price_label(p)), note
    )
    render(chat_id, text, kb, message)


def show_my_orders(chat_id, user_id, message=None):
    rows = q_all(
        "SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 10", (user_id,)
    )
    reqs = q_all(
        "SELECT * FROM requests WHERE user_id=? ORDER BY id DESC LIMIT 5", (user_id,)
    )
    if not rows and not reqs:
        render(chat_id, "📦 У вас пока нет покупок и заявок.", None, message)
        return
    lines = []
    kb = types.InlineKeyboardMarkup(row_width=1)
    pending_buttons = 0
    if rows:
        lines.append("📦 <b>Ваши последние заказы</b>\n")
    for o in rows:
        status_text = STATUS.get(o["status"], o["status"])
        payable = order_payable(o)
        if o["status"] == "pending" and not payable:
            status_text = "🚫 Оплата недоступна"
        lines.append(
            "<b>#{}</b> {} — {} ⭐\n{}\n{}".format(
                o["id"],
                esc(o["product_title"]),
                o["price"],
                status_text,
                esc(o["created_at"]),
            )
        )
        if o["status"] == "pending" and payable and pending_buttons < 3:
            pending_buttons += 1
            kb.add(
                types.InlineKeyboardButton(
                    "💳 Оплатить заказ #{}".format(o["id"]),
                    callback_data="pay:{}".format(o["id"]),
                )
            )
    text = "\n\n".join(lines)
    if reqs:
        req_lines = ["📨 <b>Ваши заявки</b>\n"]
        for r in reqs:
            req_lines.append(
                "<b>#{}</b> {}\n{}\n{}".format(
                    r["id"],
                    esc(r["product_title"]),
                    REQ_STATUS.get(r["status"], r["status"]),
                    esc(r["created_at"]),
                )
            )
        text = (text + "\n\n" if text else "") + "\n\n".join(req_lines)
    render(chat_id, text, kb, message)


def begin_purchase(user, chat_id, product):
    if not is_purchasable(product):
        bot.send_message(chat_id, "Покупка этого товара через бота сейчас недоступна.")
        return
    if product["category"] in NEEDS_TARGET:
        set_state(user.id, "await_target", product["id"])
        kb = types.InlineKeyboardMarkup(row_width=1)
        if user.username:
            kb.add(
                types.InlineKeyboardButton(
                    "👤 Для меня (@{})".format(user.username),
                    callback_data="self:{}".format(product["id"]),
                )
            )
        kb.add(types.InlineKeyboardButton("✖️ Отмена", callback_data="cancel"))
        bot.send_message(
            chat_id,
            "Отправьте <b>@username</b> получателя — аккаунта, на который нужно "
            "зачислить «{}».".format(esc(product["title"])),
            reply_markup=kb,
        )
    else:
        create_order_and_invoice(user, chat_id, product, "")


def create_order_and_invoice(user, chat_id, product, target):
    if not is_purchasable(product):
        bot.send_message(chat_id, "Этот товар сейчас недоступен для покупки.")
        return
    order_id, _ = q_exec(
        "INSERT INTO orders (user_id, product_id, product_title, category, price, "
        "target, status, created_at) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
        (
            user.id,
            product["id"],
            product["title"],
            product["category"],
            product["price"],
            target,
            now(),
        ),
    )
    send_invoice_for(chat_id, get_order(order_id))


def send_invoice_for(chat_id, order):
    desc = "Gold Star ⭐ — заказ #{}".format(order["id"])
    if order["target"]:
        desc += ". Получатель: {}".format(order["target"])
    bot.send_invoice(
        chat_id,
        title=order["product_title"][:32],
        description=desc[:255],
        invoice_payload="order:{}".format(order["id"]),
        provider_token="",
        currency="XTR",
        prices=[types.LabeledPrice(label=order["product_title"][:32], amount=order["price"])],
    )


def parse_payload(payload):
    if payload and payload.startswith("order:") and payload[6:].isdigit():
        return int(payload[6:])
    return None


# --------------------------------------------------------------------------
# Заявки (Premium / Gifts)
# --------------------------------------------------------------------------
def request_text(r):
    u = get_user(r["user_id"])
    uname = "@" + u["username"] if u and u["username"] else "—"
    price = fmt_uzs(r["price_uzs"]) if r["price_uzs"] else "по запросу"
    text = (
        "📨 <b>Заявка #{}</b>\nТовар: {}\nЦена: {}\nПолучатель: {}\n"
        "Клиент: <code>{}</code> {}\nСтатус: {}\nСоздана: {}\nОбновлена: {}"
    ).format(
        r["id"],
        esc(r["product_title"]),
        esc(price),
        esc(r["target"] or "—"),
        r["user_id"],
        esc(uname),
        REQ_STATUS.get(r["status"], r["status"]),
        esc(r["created_at"]),
        esc(r["updated_at"] or "—"),
    )
    if r["admin_note"]:
        text += "\nЗаметка: {}".format(esc(r["admin_note"]))
    return text


def begin_request(user, chat_id, product):
    set_state(user.id, "await_req_target", product["id"])
    kb = types.InlineKeyboardMarkup(row_width=1)
    if user.username:
        kb.add(
            types.InlineKeyboardButton(
                "👤 Для меня (@{})".format(user.username),
                callback_data="rself:{}".format(product["id"]),
            )
        )
    kb.add(types.InlineKeyboardButton("✖️ Отмена", callback_data="cancel"))
    bot.send_message(
        chat_id,
        "Отправьте <b>@username</b> получателя — аккаунта, для которого нужна "
        "«{}».".format(esc(product["title"])),
        reply_markup=kb,
    )


def create_request(user, chat_id, product, target):
    if not product or not product["active"] or product["sale_mode"] != "request":
        bot.send_message(chat_id, "Этот товар сейчас недоступен для заявки.")
        return
    dup = q_one(
        "SELECT id FROM requests WHERE user_id=? AND product_id=? AND target=? "
        "AND status IN ('new', 'work')",
        (user.id, product["id"], target),
    )
    if dup:
        bot.send_message(
            chat_id,
            "У вас уже есть открытая заявка <b>#{}</b> на этот товар. "
            "Администратор скоро свяжется с вами.".format(dup["id"]),
        )
        return
    ts = now()
    rid, _ = q_exec(
        "INSERT INTO requests (user_id, product_id, product_title, category, price_uzs, "
        "target, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'new', ?, ?)",
        (
            user.id,
            product["id"],
            product["title"],
            product["category"],
            product["price_uzs"],
            target,
            ts,
            ts,
        ),
    )
    r = get_request(rid)
    bot.send_message(
        chat_id,
        "📨 <b>Заявка #{} принята!</b>\n{}\nПолучатель: {}\n\n"
        "Администратор свяжется с вами. Статус заявки — в разделе «Мои покупки».".format(
            r["id"], esc(r["product_title"]), esc(r["target"] or "—")
        ),
        reply_markup=main_menu(),
    )
    notify_admin("📨 <b>Новая заявка</b>\n\n" + request_text(r), request_actions_kb(r["id"]))


def request_actions_kb(rid):
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("🔧 В работу", callback_data="adm:rwork:{}".format(rid)),
        types.InlineKeyboardButton("✅ Выполнена", callback_data="adm:rdone:{}".format(rid)),
        types.InlineKeyboardButton("❌ Отклонить", callback_data="adm:rrej:{}".format(rid)),
        types.InlineKeyboardButton("✉️ Написать", callback_data="adm:rmsg:{}".format(rid)),
    )
    return kb


def set_request_status(rid, new_status):
    allowed = REQ_TRANSITIONS[new_status]
    sql = "UPDATE requests SET status=?, updated_at=? WHERE id=? AND status IN ({})".format(
        ",".join("?" * len(allowed))
    )
    _, n = q_exec(sql, (new_status, now(), rid) + allowed)
    return n


# --------------------------------------------------------------------------
# Команды
# --------------------------------------------------------------------------
@bot.message_handler(commands=["start", "help"])
@safe
def cmd_start(m):
    upsert_user(m.from_user)
    clear_state(m.from_user.id)
    name = esc(m.from_user.first_name or "друг")
    bot.send_message(
        m.chat.id,
        "✨ <b>Добро пожаловать в Gold Star ⭐</b>, {}!\n\n"
        "Здесь можно посмотреть каталог цифровых товаров и оформить заявку на "
        "Telegram Premium и подарки. Оплата цифровых товаров — официально, "
        "через Telegram Stars.\n\n"
        "Выберите раздел в меню ниже 👇".format(name),
        reply_markup=main_menu(),
    )


@bot.message_handler(commands=["cancel"])
@safe
def cmd_cancel(m):
    clear_state(m.from_user.id)
    bot.send_message(m.chat.id, "Действие отменено.", reply_markup=main_menu())


@bot.message_handler(commands=["paysupport"])
@safe
def cmd_paysupport(m):
    upsert_user(m.from_user)
    clear_state(m.from_user.id)
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("✍️ Написать по платежу", callback_data="paysup"))
    extra = ""
    if SUPPORT_USERNAME:
        extra = "\nИли напрямую: @{}".format(esc(SUPPORT_USERNAME))
    bot.send_message(
        m.chat.id,
        "💳 <b>Поддержка по платежам</b>\n\n"
        "Если с оплатой Telegram Stars что-то пошло не так (списали Stars, но заказ не "
        "выдан, нужен возврат и т. п.) — нажмите кнопку ниже и опишите проблему. "
        "Укажите номер заказа. Мы проверим платёж и при необходимости вернём Stars.{}".format(extra),
        reply_markup=kb,
    )


@bot.message_handler(commands=["backup"])
@safe
def cmd_backup(m):
    if not is_admin(m.from_user.id):
        return
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    tmp_path = os.path.join(tempfile.gettempdir(), "goldstar-backup-{}.db".format(stamp))
    try:
        with closing(sqlite3.connect(DB_PATH)) as src, closing(sqlite3.connect(tmp_path)) as dst:
            src.backup(dst)
        with open(tmp_path, "rb") as f:
            bot.send_document(
                m.chat.id,
                f,
                caption="💾 Резервная копия базы Gold Star. Содержит данные пользователей — "
                "храните в безопасном месте.",
            )
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


@bot.message_handler(commands=["admin"])
@safe
def cmd_admin(m):
    if not is_admin(m.from_user.id):
        bot.send_message(m.chat.id, "⛔ Нет доступа.")
        return
    clear_state(m.from_user.id)
    bot.send_message(m.chat.id, "🛠 <b>Админ-панель</b>", reply_markup=admin_menu())


@bot.message_handler(commands=["reply"])
@safe
def cmd_reply(m):
    if not is_admin(m.from_user.id):
        return
    parts = (m.text or "").split(maxsplit=2)
    if len(parts) < 3 or not parts[1].isdigit():
        bot.send_message(m.chat.id, "Формат: /reply ID_пользователя текст")
        return
    try:
        bot.send_message(
            int(parts[1]), "📞 <b>Ответ поддержки:</b>\n\n{}".format(esc(parts[2]))
        )
        bot.send_message(m.chat.id, "✅ Отправлено.")
    except Exception as exc:
        bot.send_message(m.chat.id, "Не удалось отправить: {}".format(esc(exc)))


# --------------------------------------------------------------------------
# Кнопки главного меню
# --------------------------------------------------------------------------
@bot.message_handler(func=lambda m: m.text == MENU_STARS)
@safe
def menu_stars(m):
    upsert_user(m.from_user)
    clear_state(m.from_user.id)
    show_category(m.chat.id, "stars")


@bot.message_handler(func=lambda m: m.text == MENU_PREMIUM)
@safe
def menu_premium(m):
    upsert_user(m.from_user)
    clear_state(m.from_user.id)
    show_category(m.chat.id, "premium")


@bot.message_handler(func=lambda m: m.text == MENU_GIFTS)
@safe
def menu_gifts(m):
    upsert_user(m.from_user)
    clear_state(m.from_user.id)
    show_category(m.chat.id, "gifts")


@bot.message_handler(func=lambda m: m.text == MENU_CATALOG)
@safe
def menu_catalog(m):
    upsert_user(m.from_user)
    clear_state(m.from_user.id)
    show_catalog_root(m.chat.id)


@bot.message_handler(func=lambda m: m.text == MENU_MY)
@safe
def menu_my(m):
    upsert_user(m.from_user)
    clear_state(m.from_user.id)
    show_my_orders(m.chat.id, m.from_user.id)


@bot.message_handler(func=lambda m: m.text == MENU_SUPPORT)
@safe
def menu_support(m):
    upsert_user(m.from_user)
    clear_state(m.from_user.id)
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("✍️ Написать в поддержку", callback_data="support"))
    extra = ""
    if SUPPORT_USERNAME:
        extra = "\nИли напрямую: @{}".format(esc(SUPPORT_USERNAME))
    bot.send_message(
        m.chat.id,
        "📞 <b>Поддержка</b>\nОпишите проблему — мы ответим в этом чате.{}".format(extra),
        reply_markup=kb,
    )


@bot.message_handler(func=lambda m: m.text == MENU_INFO)
@safe
def menu_info(m):
    upsert_user(m.from_user)
    clear_state(m.from_user.id)
    bot.send_message(
        m.chat.id,
        "ℹ️ <b>Gold Star ⭐</b>\n\n"
        "Каталог цифровых товаров: Telegram Stars (только просмотр), Telegram Premium "
        "и Telegram Gifts (по заявке).\n"
        "• Оплата цифровых товаров — только через Telegram Stars.\n"
        "• По заявке администратор свяжется с вами и уточнит детали.\n"
        "• Статусы заказов и заявок — в разделе «Мои покупки».\n"
        "• Проблемы с платежом — команда /paysupport.",
    )


# --------------------------------------------------------------------------
# Callback-кнопки пользователя
# --------------------------------------------------------------------------
def dispatch_user_cb(c):
    """Возвращает текст alert-а или None."""
    user = c.from_user
    chat_id = c.message.chat.id
    msg = c.message
    parts = c.data.split(":")
    act = parts[0]

    if act == "catalog":
        show_catalog_root(chat_id, msg)
    elif act == "cat" and len(parts) > 1 and parts[1] in CATS:
        show_category(chat_id, parts[1], msg)
    elif act == "prod":
        p = get_product(int(parts[1]))
        if not p or not p["active"]:
            return "Товар недоступен"
        show_product(chat_id, p, msg)
    elif act == "buy":
        p = get_product(int(parts[1]))
        if not p or not p["active"]:
            return "Товар недоступен"
        if not is_purchasable(p):
            return "Покупка этого товара через бота недоступна"
        begin_purchase(user, chat_id, p)
    elif act == "self":
        p = get_product(int(parts[1]))
        if not p or not p["active"]:
            return "Товар недоступен"
        if not is_purchasable(p):
            return "Покупка этого товара через бота недоступна"
        if not user.username:
            return "У вас не задан username. Отправьте @username вручную."
        clear_state(user.id)
        create_order_and_invoice(user, chat_id, p, "@" + user.username)
    elif act == "req":
        p = get_product(int(parts[1]))
        if not p or not p["active"] or p["sale_mode"] != "request":
            return "Товар недоступен"
        begin_request(user, chat_id, p)
    elif act == "rself":
        p = get_product(int(parts[1]))
        if not p or not p["active"] or p["sale_mode"] != "request":
            return "Товар недоступен"
        if not user.username:
            return "У вас не задан username. Отправьте @username вручную."
        clear_state(user.id)
        create_request(user, chat_id, p, "@" + user.username)
    elif act == "pay":
        o = get_order(int(parts[1]))
        if not o or o["user_id"] != user.id or o["status"] != "pending":
            return "Этот заказ нельзя оплатить"
        if not order_payable(o):
            return "Оплата этого заказа через бота недоступна"
        send_invoice_for(chat_id, o)
    elif act == "my":
        show_my_orders(chat_id, user.id, msg)
    elif act == "support":
        if not ADMIN_ID:
            return "Поддержка временно недоступна"
        set_state(user.id, "await_support")
        bot.send_message(chat_id, "✍️ Напишите ваше сообщение одним текстом. /cancel — отмена.")
    elif act == "paysup":
        if not ADMIN_ID:
            return "Поддержка временно недоступна"
        set_state(user.id, "await_paysupport")
        bot.send_message(
            chat_id,
            "✍️ Опишите проблему с платежом одним сообщением (номер заказа, что произошло). "
            "/cancel — отмена.",
        )
    elif act == "cancel":
        clear_state(user.id)
        render(chat_id, "Отменено.", None, msg)
    return None


@bot.callback_query_handler(func=lambda c: bool(c.data) and not c.data.startswith("adm:"))
@safe
def on_user_cb(c):
    upsert_user(c.from_user)
    alert = dispatch_user_cb(c)
    bot.answer_callback_query(c.id, text=alert, show_alert=bool(alert))


# --------------------------------------------------------------------------
# Оплата Telegram Stars
# --------------------------------------------------------------------------
@bot.pre_checkout_query_handler(func=lambda q: True)
def on_pre_checkout(q):
    try:
        oid = parse_payload(q.invoice_payload)
        order = get_order(oid) if oid else None
        error = None
        if not order or order["user_id"] != q.from_user.id:
            error = "Заказ не найден."
        elif order["status"] != "pending":
            error = "Этот заказ уже оплачен или закрыт."
        elif not order_payable(order):
            error = "Оплата этого товара через бота сейчас недоступна."
        elif q.currency != "XTR" or q.total_amount != order["price"]:
            error = "Сумма платежа не совпадает с заказом."
        if error:
            bot.answer_pre_checkout_query(q.id, ok=False, error_message=error)
        else:
            bot.answer_pre_checkout_query(q.id, ok=True)
    except Exception:
        log.exception("Ошибка pre_checkout_query")
        log_error("pre_checkout", traceback.format_exc())
        try:
            bot.answer_pre_checkout_query(
                q.id, ok=False, error_message="Техническая ошибка. Попробуйте позже."
            )
        except Exception:
            log.exception("Не удалось ответить на pre_checkout_query")


@bot.message_handler(content_types=["successful_payment"])
@safe
def on_successful_payment(m):
    sp = m.successful_payment
    charge_id = sp.telegram_payment_charge_id
    order_id = parse_payload(sp.invoice_payload)
    user_id = m.from_user.id

    # Идемпотентность: charge_id — PRIMARY KEY. Повторная обработка того же
    # платежа вызовет IntegrityError, и товар не будет подтверждён второй раз.
    updated = 0
    try:
        with closing(get_conn()) as conn:
            with conn:
                conn.execute(
                    "INSERT INTO payments (charge_id, order_id, user_id, amount, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (charge_id, order_id, user_id, sp.total_amount, now()),
                )
                if order_id:
                    cur = conn.execute(
                        "UPDATE orders SET status='paid', charge_id=?, paid_at=? "
                        "WHERE id=? AND user_id=? AND status='pending'",
                        (charge_id, now(), order_id, user_id),
                    )
                    updated = cur.rowcount
    except sqlite3.IntegrityError:
        log.warning("Повторная обработка платежа %s пропущена", charge_id)
        return

    if updated == 0:
        # Платёж есть, но заказ уже оплачен/не найден — возвращаем звёзды.
        log.error("Платёж %s не привязан к ожидающему заказу %s", charge_id, order_id)
        refunded = False
        try:
            bot.refund_star_payment(user_id, charge_id)
            refunded = True
        except Exception:
            log.exception("Не удалось вернуть платёж %s", charge_id)
        notify_admin(
            "⚠️ Лишний платёж по заказу #{}\nПользователь: <code>{}</code>\n"
            "Charge ID: <code>{}</code>\nАвтовозврат: {}".format(
                order_id, user_id, esc(charge_id), "выполнен" if refunded else "НЕ выполнен"
            )
        )
        bot.send_message(
            m.chat.id,
            "Этот заказ уже был оплачен. Дублирующий платёж "
            + ("возвращён." if refunded else "будет возвращён администратором."),
        )
        return

    order = get_order(order_id)
    bot.send_message(
        m.chat.id,
        "✅ <b>Оплата получена!</b>\nЗаказ <b>#{}</b>: {}\n\n"
        "Администратор выдаст товар, мы пришлём уведомление.".format(
            order["id"], esc(order["product_title"])
        ),
        reply_markup=main_menu(),
    )
    warn = ""
    if sp.total_amount != order["price"] or sp.currency != "XTR":
        warn = "\n⚠️ Сумма/валюта платежа отличается от заказа!"
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("✅ Выдано", callback_data="adm:done:{}".format(order["id"])))
    notify_admin("💰 <b>Новая оплата</b>\n\n" + order_text(order) + warn, kb)


# --------------------------------------------------------------------------
# Админ-панель
# --------------------------------------------------------------------------
def admin_menu():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("🕐 Ждут выдачи", callback_data="adm:orders:wait"),
        types.InlineKeyboardButton("💳 Оплаченные", callback_data="adm:orders:paid"),
        types.InlineKeyboardButton("⏳ Неоплаченные", callback_data="adm:orders:pending"),
        types.InlineKeyboardButton("📋 Все заказы", callback_data="adm:orders:all"),
        types.InlineKeyboardButton("📨 Заявки", callback_data="adm:reqs:new"),
        types.InlineKeyboardButton("👥 Пользователи", callback_data="adm:users"),
        types.InlineKeyboardButton("📊 Статистика", callback_data="adm:stats"),
        types.InlineKeyboardButton("🗂 Каталог", callback_data="adm:cat"),
    )
    return kb


def back_to_admin():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("⬅️ Админ-меню", callback_data="adm:menu"))
    return kb


def order_text(o):
    u = get_user(o["user_id"])
    uname = "@" + u["username"] if u and u["username"] else "—"
    return (
        "🧾 <b>Заказ #{}</b>\nТовар: {}\nЦена: {} ⭐\nПолучатель: {}\n"
        "Покупатель: <code>{}</code> {}\nСтатус: {}\nСоздан: {}\nОплачен: {}"
    ).format(
        o["id"],
        esc(o["product_title"]),
        o["price"],
        esc(o["target"] or "—"),
        o["user_id"],
        esc(uname),
        STATUS.get(o["status"], o["status"]),
        esc(o["created_at"]),
        esc(o["paid_at"] or "—"),
    )


ORDER_FILTERS = {
    "all": "",
    "paid": "WHERE status IN ('paid','delivered')",
    "wait": "WHERE status='paid'",
    "pending": "WHERE status='pending'",
}
ORDER_TITLES = {
    "all": "Все заказы",
    "paid": "Оплаченные заказы",
    "wait": "Ждут выдачи",
    "pending": "Неоплаченные заказы",
}


def admin_orders(chat_id, flt, message=None):
    if flt not in ORDER_FILTERS:
        flt = "all"
    rows = q_all(
        "SELECT * FROM orders {} ORDER BY id DESC LIMIT 20".format(ORDER_FILTERS[flt])
    )
    kb = types.InlineKeyboardMarkup(row_width=1)
    for o in rows:
        kb.add(
            types.InlineKeyboardButton(
                "#{} {} {} · {}⭐".format(
                    o["id"], EMO.get(o["status"], ""), o["product_title"][:22], o["price"]
                ),
                callback_data="adm:order:{}".format(o["id"]),
            )
        )
    kb.add(types.InlineKeyboardButton("⬅️ Админ-меню", callback_data="adm:menu"))
    text = "<b>{}</b> (последние 20)".format(ORDER_TITLES[flt])
    if not rows:
        text += "\n\nПусто."
    render(chat_id, text, kb, message)


def admin_order_card(chat_id, oid, message=None):
    o = get_order(oid)
    if not o:
        render(chat_id, "Заказ не найден.", back_to_admin(), message)
        return
    kb = types.InlineKeyboardMarkup(row_width=1)
    if o["status"] == "paid":
        kb.add(
            types.InlineKeyboardButton("✅ Выдано", callback_data="adm:done:{}".format(oid)),
            types.InlineKeyboardButton(
                "↩️ Вернуть звёзды", callback_data="adm:refund:{}".format(oid)
            ),
        )
    kb.add(types.InlineKeyboardButton("⬅️ К заказам", callback_data="adm:orders:all"))
    render(chat_id, order_text(o), kb, message)


REQ_FILTERS = {
    "new": "WHERE status='new'",
    "work": "WHERE status='work'",
    "all": "",
}
REQ_TITLES = {
    "new": "Новые заявки",
    "work": "Заявки в работе",
    "all": "Все заявки",
}


def admin_requests(chat_id, flt, message=None):
    if flt not in REQ_FILTERS:
        flt = "all"
    rows = q_all(
        "SELECT * FROM requests {} ORDER BY id DESC LIMIT 20".format(REQ_FILTERS[flt])
    )
    kb = types.InlineKeyboardMarkup(row_width=1)
    for r in rows:
        kb.add(
            types.InlineKeyboardButton(
                "#{} {} {}".format(
                    r["id"], REQ_EMO.get(r["status"], ""), r["product_title"][:26]
                ),
                callback_data="adm:req:{}".format(r["id"]),
            )
        )
    kb.row(
        types.InlineKeyboardButton("🆕 Новые", callback_data="adm:reqs:new"),
        types.InlineKeyboardButton("🔧 В работе", callback_data="adm:reqs:work"),
        types.InlineKeyboardButton("📋 Все", callback_data="adm:reqs:all"),
    )
    kb.add(types.InlineKeyboardButton("⬅️ Админ-меню", callback_data="adm:menu"))
    text = "<b>{}</b> (последние 20)".format(REQ_TITLES[flt])
    if not rows:
        text += "\n\nПусто."
    render(chat_id, text, kb, message)


def admin_request_card(chat_id, rid, message=None):
    r = get_request(rid)
    if not r:
        render(chat_id, "Заявка не найдена.", back_to_admin(), message)
        return
    kb = types.InlineKeyboardMarkup(row_width=2)
    if r["status"] == "new":
        kb.add(types.InlineKeyboardButton("🔧 В работу", callback_data="adm:rwork:{}".format(rid)))
    if r["status"] in ("new", "work"):
        kb.add(
            types.InlineKeyboardButton("✅ Выполнена", callback_data="adm:rdone:{}".format(rid)),
            types.InlineKeyboardButton("❌ Отклонить", callback_data="adm:rrej:{}".format(rid)),
        )
    kb.add(types.InlineKeyboardButton("✉️ Написать клиенту", callback_data="adm:rmsg:{}".format(rid)))
    kb.add(types.InlineKeyboardButton("⬅️ К заявкам", callback_data="adm:reqs:all"))
    render(chat_id, request_text(r), kb, message)


def admin_users(chat_id, message=None):
    total = q_one("SELECT COUNT(*) AS n FROM users")["n"]
    rows = q_all(
        "SELECT u.user_id, u.username, u.first_name, "
        "(SELECT COUNT(*) FROM orders o WHERE o.user_id=u.user_id AND o.status IN ('paid','delivered')) AS paid_cnt "
        "FROM users u ORDER BY u.last_seen DESC LIMIT 25"
    )
    lines = ["👥 <b>Пользователи</b>: {}\n".format(total)]
    for u in rows:
        uname = "@" + u["username"] if u["username"] else "—"
        lines.append(
            "<code>{}</code> {} {} · покупок: {}".format(
                u["user_id"], esc(uname), esc(u["first_name"] or ""), u["paid_cnt"]
            )
        )
    render(chat_id, "\n".join(lines)[:4000], back_to_admin(), message)


def admin_stats(chat_id, message=None):
    users = q_one("SELECT COUNT(*) AS n FROM users")["n"]
    by_status = {r["status"]: r["n"] for r in q_all(
        "SELECT status, COUNT(*) AS n FROM orders GROUP BY status")}
    revenue = q_one(
        "SELECT COALESCE(SUM(price), 0) AS s FROM orders WHERE status IN ('paid','delivered')"
    )["s"]
    req_new = q_one("SELECT COUNT(*) AS n FROM requests WHERE status='new'")["n"]
    req_work = q_one("SELECT COUNT(*) AS n FROM requests WHERE status='work'")["n"]
    text = (
        "📊 <b>Статистика</b>\n\n👥 Пользователей: {}\n"
        "⏳ Неоплаченных: {}\n💳 Ждут выдачи: {}\n✅ Выдано: {}\n↩️ Возвратов: {}\n\n"
        "📨 Заявок новых: {} · в работе: {}\n\n"
        "💰 Выручка: <b>{} ⭐</b>"
    ).format(
        users,
        by_status.get("pending", 0),
        by_status.get("paid", 0),
        by_status.get("delivered", 0),
        by_status.get("refunded", 0),
        req_new,
        req_work,
        revenue,
    )
    render(chat_id, text, back_to_admin(), message)


def admin_cat_root(chat_id, message=None):
    kb = types.InlineKeyboardMarkup(row_width=1)
    for key, label in CATS.items():
        kb.add(types.InlineKeyboardButton(label, callback_data="adm:catlist:" + key))
    kb.add(types.InlineKeyboardButton("⬅️ Админ-меню", callback_data="adm:menu"))
    render(chat_id, "🗂 <b>Каталог</b>: выберите раздел", kb, message)


def admin_cat_list(chat_id, category, message=None):
    kb = types.InlineKeyboardMarkup(row_width=1)
    for p in list_products(category, active_only=False):
        mark = "🟢" if p["active"] else "⚫️"
        kb.add(
            types.InlineKeyboardButton(
                "{} {} — {}".format(mark, p["title"], price_label(p)),
                callback_data="adm:p:{}".format(p["id"]),
            )
        )
    kb.add(
        types.InlineKeyboardButton("➕ Добавить товар", callback_data="adm:add:" + category),
        types.InlineKeyboardButton("⬅️ Назад", callback_data="adm:cat"),
    )
    render(chat_id, "<b>{}</b>".format(esc(CATS.get(category, category))), kb, message)


def admin_product(chat_id, pid, message=None):
    p = get_product(pid)
    if not p:
        render(chat_id, "Товар не найден.", back_to_admin(), message)
        return
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("✏️ Цена", callback_data="adm:price:{}".format(pid)),
        types.InlineKeyboardButton("✏️ Название", callback_data="adm:title:{}".format(pid)),
        types.InlineKeyboardButton("📝 Описание", callback_data="adm:desc:{}".format(pid)),
        types.InlineKeyboardButton(
            "👁 Скрыть" if p["active"] else "👁 Показать",
            callback_data="adm:tog:{}".format(pid),
        ),
        types.InlineKeyboardButton("🗑 Удалить", callback_data="adm:del:{}".format(pid)),
        types.InlineKeyboardButton("⬅️ Назад", callback_data="adm:catlist:" + p["category"]),
    )
    text = (
        "<b>{}</b>\nID: {}\nРаздел: {}\nЦена: {}\nРежим: {}\nСтатус: {}\n\n{}"
    ).format(
        esc(p["title"]),
        p["id"],
        esc(CATS.get(p["category"], p["category"])),
        esc(price_label(p)),
        esc(MODE_LABELS.get(p["sale_mode"], p["sale_mode"])),
        "активен" if p["active"] else "скрыт",
        esc(p["description"] or "—"),
    )
    render(chat_id, text, kb, message)


def dispatch_admin_cb(c):
    user = c.from_user
    chat_id = c.message.chat.id
    msg = c.message
    parts = c.data.split(":")
    act = parts[1] if len(parts) > 1 else "menu"
    arg = parts[2] if len(parts) > 2 else ""

    if act == "menu":
        clear_state(user.id)
        render(chat_id, "🛠 <b>Админ-панель</b>", admin_menu(), msg)
    elif act == "orders":
        admin_orders(chat_id, arg, msg)
    elif act == "order":
        admin_order_card(chat_id, int(arg), msg)
    elif act == "done":
        oid = int(arg)
        _, n = q_exec(
            "UPDATE orders SET status='delivered', delivered_at=? "
            "WHERE id=? AND status='paid'",
            (now(), oid),
        )
        if n == 0:
            return "Заказ уже обработан или не оплачен"
        o = get_order(oid)
        try:
            bot.send_message(
                o["user_id"],
                "✅ Ваш заказ <b>#{}</b> ({}) выдан. Спасибо за покупку!".format(
                    o["id"], esc(o["product_title"])
                ),
            )
        except Exception:
            log.exception("Не удалось уведомить покупателя о выдаче")
        admin_order_card(chat_id, oid, msg)
        return "Отмечено как выданное"
    elif act == "refund":
        oid = int(arg)
        o = get_order(oid)
        if not o or o["status"] != "paid" or not o["charge_id"]:
            return "Возврат невозможен"
        try:
            bot.refund_star_payment(o["user_id"], o["charge_id"])
        except Exception as exc:
            log.exception("Ошибка возврата")
            log_error("refund", traceback.format_exc())
            return "Ошибка возврата: {}".format(str(exc)[:150])
        q_exec("UPDATE orders SET status='refunded' WHERE id=? AND status='paid'", (oid,))
        try:
            bot.send_message(
                o["user_id"],
                "↩️ Заказ <b>#{}</b>: звёзды возвращены.".format(o["id"]),
            )
        except Exception:
            log.exception("Не удалось уведомить покупателя о возврате")
        admin_order_card(chat_id, oid, msg)
        return "Звёзды возвращены"
    elif act == "reqs":
        admin_requests(chat_id, arg, msg)
    elif act == "req":
        admin_request_card(chat_id, int(arg), msg)
    elif act in ("rwork", "rdone", "rrej"):
        rid = int(arg)
        new_status = {"rwork": "work", "rdone": "done", "rrej": "rejected"}[act]
        if set_request_status(rid, new_status) == 0:
            return "Статус заявки уже изменён"
        r = get_request(rid)
        client_msgs = {
            "work": "🔧 Заявка <b>#{}</b> принята в работу.",
            "done": "✅ Заявка <b>#{}</b> выполнена. Спасибо!",
            "rejected": "❌ Заявка <b>#{}</b> отклонена. Если есть вопросы — напишите в поддержку.",
        }
        try:
            bot.send_message(r["user_id"], client_msgs[new_status].format(r["id"]))
        except Exception:
            log.exception("Не удалось уведомить клиента о статусе заявки")
        admin_request_card(chat_id, rid, msg)
        return "Статус обновлён"
    elif act == "rmsg":
        if not get_request(int(arg)):
            return "Заявка не найдена"
        set_state(user.id, "admin_rmsg", arg)
        bot.send_message(
            chat_id,
            "Отправьте текст сообщения клиенту по заявке #{}. /cancel — отмена.".format(esc(arg)),
        )
    elif act == "users":
        admin_users(chat_id, msg)
    elif act == "stats":
        admin_stats(chat_id, msg)
    elif act == "cat":
        admin_cat_root(chat_id, msg)
    elif act == "catlist" and arg in CATS:
        admin_cat_list(chat_id, arg, msg)
    elif act == "p":
        admin_product(chat_id, int(arg), msg)
    elif act in ("price", "title", "desc"):
        prompts = {
            "price": "Отправьте новую цену (целое число звёзд).",
            "title": "Отправьте новое название.",
            "desc": "Отправьте новое описание.",
        }
        text = prompts[act]
        if act == "price":
            p = get_product(int(arg))
            if p and p["sale_mode"] != "xtr":
                text = "Отправьте новую цену в сумах (целое число)."
        set_state(user.id, "admin_" + act, arg)
        bot.send_message(chat_id, text + " /cancel — отмена.")
    elif act == "tog":
        q_exec("UPDATE products SET active = 1 - active WHERE id=?", (int(arg),))
        admin_product(chat_id, int(arg), msg)
    elif act == "del":
        kb = types.InlineKeyboardMarkup(row_width=2)
        kb.add(
            types.InlineKeyboardButton("🗑 Да, удалить", callback_data="adm:delok:" + arg),
            types.InlineKeyboardButton("Отмена", callback_data="adm:p:" + arg),
        )
        render(chat_id, "Удалить товар #{}? История заказов сохранится.".format(esc(arg)), kb, msg)
    elif act == "delok":
        p = get_product(int(arg))
        q_exec("DELETE FROM products WHERE id=?", (int(arg),))
        admin_cat_list(chat_id, p["category"] if p else "stars", msg)
        return "Удалено"
    elif act == "add" and arg in CATS:
        set_state(user.id, "admin_add", arg)
        price_hint = "цена в звёздах" if arg == "other" else "цена в сумах"
        bot.send_message(
            chat_id,
            "Отправьте товар в формате:\n<code>Название | цена | описание</code>\n"
            "({}; описание можно не указывать). /cancel — отмена.".format(price_hint),
        )
    return None


@bot.callback_query_handler(func=lambda c: bool(c.data) and c.data.startswith("adm:"))
@safe
def on_admin_cb(c):
    if not is_admin(c.from_user.id):
        bot.answer_callback_query(c.id, "⛔ Нет доступа", show_alert=True)
        return
    alert = dispatch_admin_cb(c)
    bot.answer_callback_query(c.id, text=alert, show_alert=bool(alert))


def handle_admin_input(m, state, data):
    uid = m.from_user.id
    text = (m.text or "").strip()

    if state == "admin_price":
        if not text.isdigit() or int(text) < 1:
            bot.send_message(m.chat.id, "Нужно целое число больше 0. Попробуйте ещё раз.")
            return
        p = get_product(int(data))
        if p and p["sale_mode"] != "xtr":
            q_exec("UPDATE products SET price_uzs=? WHERE id=?", (int(text), int(data)))
        else:
            q_exec("UPDATE products SET price=? WHERE id=?", (int(text), int(data)))
        clear_state(uid)
        admin_product(m.chat.id, int(data))
    elif state == "admin_title":
        if not text:
            return
        q_exec("UPDATE products SET title=? WHERE id=?", (text[:60], int(data)))
        clear_state(uid)
        admin_product(m.chat.id, int(data))
    elif state == "admin_desc":
        q_exec("UPDATE products SET description=? WHERE id=?", (text[:500], int(data)))
        clear_state(uid)
        admin_product(m.chat.id, int(data))
    elif state == "admin_add":
        parts = [x.strip() for x in text.split("|")]
        if len(parts) < 2 or not parts[0] or not parts[1].isdigit() or int(parts[1]) < 1:
            bot.send_message(
                m.chat.id, "Формат: <code>Название | цена | описание</code>. Попробуйте ещё раз."
            )
            return
        desc = parts[2] if len(parts) > 2 else ""
        mode = DEFAULT_MODE_BY_CAT.get(data, "xtr")
        amount = int(parts[1])
        price_stars = amount if mode == "xtr" else 0
        price_uzs = None if mode == "xtr" else amount
        pid, _ = q_exec(
            "INSERT INTO products (category, title, description, price, price_uzs, sale_mode, sort) "
            "VALUES (?, ?, ?, ?, ?, ?, "
            "(SELECT COALESCE(MAX(sort), 0) + 10 FROM products WHERE category=?))",
            (data, parts[0][:60], desc[:500], price_stars, price_uzs, mode, data),
        )
        clear_state(uid)
        admin_product(m.chat.id, pid)
    elif state == "admin_rmsg":
        if not text:
            return
        r = get_request(int(data))
        if not r:
            clear_state(uid)
            bot.send_message(m.chat.id, "Заявка не найдена.")
            return
        try:
            bot.send_message(
                r["user_id"],
                "📨 <b>Сообщение по заявке #{}:</b>\n\n{}".format(r["id"], esc(text)),
            )
        except Exception as exc:
            bot.send_message(m.chat.id, "Не удалось отправить: {}".format(esc(exc)))
            return
        q_exec(
            "UPDATE requests SET admin_note=?, updated_at=? WHERE id=?",
            (text[:500], now(), r["id"]),
        )
        clear_state(uid)
        bot.send_message(m.chat.id, "✅ Отправлено клиенту.")
        admin_request_card(m.chat.id, r["id"])


# --------------------------------------------------------------------------
# Текстовый ввод по состояниям (регистрируется после кнопок меню)
# --------------------------------------------------------------------------
@bot.message_handler(
    content_types=["text"],
    func=lambda m: bool(m.text)
    and not m.text.startswith("/")
    and get_state(m.from_user.id) is not None,
)
@safe
def on_state_text(m):
    upsert_user(m.from_user)
    uid = m.from_user.id
    state, data = get_state(uid)

    if state == "await_target":
        match = USERNAME_RE.match(m.text.strip())
        if not match:
            bot.send_message(
                m.chat.id,
                "Нужен корректный @username (5–32 символа: латиница, цифры, _). "
                "Попробуйте ещё раз или /cancel.",
            )
            return
        product = get_product(int(data))
        clear_state(uid)
        if not product or not product["active"]:
            bot.send_message(m.chat.id, "Товар больше недоступен.")
            return
        create_order_and_invoice(m.from_user, m.chat.id, product, "@" + match.group(1))
    elif state == "await_req_target":
        match = USERNAME_RE.match(m.text.strip())
        if not match:
            bot.send_message(
                m.chat.id,
                "Нужен корректный @username (5–32 символа: латиница, цифры, _). "
                "Попробуйте ещё раз или /cancel.",
            )
            return
        product = get_product(int(data))
        clear_state(uid)
        if not product or not product["active"]:
            bot.send_message(m.chat.id, "Товар больше недоступен.")
            return
        create_request(m.from_user, m.chat.id, product, "@" + match.group(1))
    elif state == "await_support":
        clear_state(uid)
        uname = "@" + m.from_user.username if m.from_user.username else "—"
        notify_admin(
            "📩 <b>Обращение в поддержку</b>\nОт: <code>{}</code> {} {}\n\n{}\n\n"
            "Ответить: <code>/reply {} текст</code>".format(
                uid, esc(uname), esc(m.from_user.first_name or ""), esc(m.text), uid
            )
        )
        bot.send_message(m.chat.id, "✅ Сообщение отправлено. Мы ответим здесь же.")
    elif state == "await_paysupport":
        clear_state(uid)
        uname = "@" + m.from_user.username if m.from_user.username else "—"
        recent = q_all(
            "SELECT id, product_title, price, status, charge_id FROM orders "
            "WHERE user_id=? AND charge_id IS NOT NULL ORDER BY id DESC LIMIT 3",
            (uid,),
        )
        orders_info = "\n".join(
            "#{} {} · {} ⭐ · {} · <code>{}</code>".format(
                o["id"],
                esc(o["product_title"]),
                o["price"],
                STATUS.get(o["status"], o["status"]),
                esc(o["charge_id"]),
            )
            for o in recent
        ) or "— оплаченных заказов нет"
        notify_admin(
            "💳 <b>Обращение по платежу</b>\nОт: <code>{}</code> {} {}\n\n{}\n\n"
            "<b>Последние оплаты клиента:</b>\n{}\n\n"
            "Ответить: <code>/reply {} текст</code>".format(
                uid, esc(uname), esc(m.from_user.first_name or ""), esc(m.text), orders_info, uid
            )
        )
        bot.send_message(m.chat.id, "✅ Обращение по платежу отправлено. Мы ответим здесь же.")
    elif state.startswith("admin_") and is_admin(uid):
        handle_admin_input(m, state, data)
    else:
        clear_state(uid)


@bot.message_handler(content_types=["text"])
@safe
def fallback(m):
    upsert_user(m.from_user)
    bot.send_message(m.chat.id, "Выберите раздел в меню ниже 👇", reply_markup=main_menu())


# --------------------------------------------------------------------------
# Flask: Webhook  (НЕ ИЗМЕНЕНО)
# --------------------------------------------------------------------------
@app.route("/", methods=["GET"])
def index():
    return "Gold Star bot is running", 200


@app.route("/healthz", methods=["GET"])
def healthz():
    return "ok", 200


@app.route("/webhook", methods=["POST"])
def webhook():
    token = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not hmac.compare_digest(token, WEBHOOK_SECRET):
        abort(403)
    if not (request.headers.get("content-type", "")).startswith("application/json"):
        abort(403)
    try:
        update = telebot.types.Update.de_json(request.get_data().decode("utf-8"))
        bot.process_new_updates([update])
    except Exception:
        # Всегда отвечаем 200, чтобы Telegram не слал одно и то же обновление повторно.
        log.exception("Ошибка обработки обновления")
    return "", 200


def _setup_webhook():
    if not RENDER_URL:
        log.warning("RENDER_URL не задан — webhook не настроен")
        return
    for attempt in range(5):
        try:
            bot.set_webhook(
                url=RENDER_URL + "/webhook",
                secret_token=WEBHOOK_SECRET,
                allowed_updates=["message", "callback_query", "pre_checkout_query"],
            )
            log.info("Webhook установлен: %s/webhook", RENDER_URL)
            break
        except Exception:
            log.exception("Не удалось установить webhook (попытка %s)", attempt + 1)
            time.sleep(3 * (attempt + 1))
    else:
        return
    if WEBAPP_URL.startswith("https://"):
        try:
            bot.set_chat_menu_button(
                menu_button=types.MenuButtonWebApp(
                    text="🛍 Магазин", web_app=types.WebAppInfo(url=WEBAPP_URL)
                )
            )
            log.info("Кнопка Mini App установлена: %s", WEBAPP_URL)
        except Exception:
            log.exception("Не удалось установить кнопку меню Mini App")


init_db()
threading.Thread(target=_setup_webhook, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
