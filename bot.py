# -*- coding: utf-8 -*-
"""Gold Star ⭐ — Telegram-магазин цифровых товаров.

Стек: pyTelegramBotAPI (telebot) + Flask + SQLite + Webhook + Gunicorn.
Оплата: Telegram Stars (валюта XTR). Секреты — только через переменные окружения.
"""
import functools
import hmac
import html
import logging
import os
import re
import sqlite3
import threading
import time
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

bot = telebot.TeleBot(BOT_TOKEN, threaded=False, parse_mode="HTML")
app = Flask(__name__)

# --------------------------------------------------------------------------
# Константы
# --------------------------------------------------------------------------
MENU_STARS = "⭐ Telegram Stars"
MENU_PREMIUM = "💎 Telegram Premium"
MENU_MY = "📦 Мои покупки"
MENU_CATALOG = "🛒 Каталог товаров"
MENU_SUPPORT = "📞 Поддержка"
MENU_INFO = "ℹ️ Информация"
MENU_WEBAPP = "🌐 Открыть магазин"

CATS = {
    "stars": "⭐ Telegram Stars",
    "premium": "💎 Telegram Premium",
    "other": "📦 Другие товары",
}
NEEDS_TARGET = ("stars", "premium")  # нужен @username получателя

STATUS = {
    "pending": "⏳ Ожидает оплаты",
    "paid": "💳 Оплачен, ждёт выдачи",
    "delivered": "✅ Выдан",
    "refunded": "↩️ Возврат",
}
EMO = {"pending": "⏳", "paid": "💳", "delivered": "✅", "refunded": "↩️"}

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
# Утилиты
# --------------------------------------------------------------------------
def esc(value):
    return html.escape(str(value if value is not None else ""))


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def is_admin(user_id):
    return bool(ADMIN_ID) and user_id == ADMIN_ID


def safe(fn):
    """Ловит ошибки обработчика, пишет в лог и отвечает пользователю."""

    @functools.wraps(fn)
    def wrapper(obj, *args, **kwargs):
        try:
            return fn(obj, *args, **kwargs)
        except Exception:
            log.exception("Ошибка в обработчике %s", fn.__name__)
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


def init_db():
    with closing(get_conn()) as conn:
        with conn:
            conn.executescript(
                """
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
                "{} — {} ⭐".format(p["title"], p["price"]),
                callback_data="prod:{}".format(p["id"]),
            )
        )
    kb.add(types.InlineKeyboardButton("⬅️ Каталог", callback_data="catalog"))
    head = "<b>{}</b>\n".format(esc(CATS.get(category, category)))
    if products:
        text = head + "Выберите товар:"
    else:
        text = head + "Пока нет доступных товаров."
    render(chat_id, text, kb, message)


def show_product(chat_id, p, message=None):
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton(
            "🛍 Купить за {} ⭐".format(p["price"]),
            callback_data="buy:{}".format(p["id"]),
        ),
        types.InlineKeyboardButton("⬅️ Назад", callback_data="cat:" + p["category"]),
    )
    text = "<b>{}</b>\n\n{}\n\n💰 Цена: <b>{} ⭐</b>".format(
        esc(p["title"]), esc(p["description"] or "—"), p["price"]
    )
    render(chat_id, text, kb, message)


def show_my_orders(chat_id, user_id, message=None):
    rows = q_all(
        "SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 10", (user_id,)
    )
    if not rows:
        render(chat_id, "📦 У вас пока нет покупок.", None, message)
        return
    lines = ["📦 <b>Ваши последние заказы</b>\n"]
    kb = types.InlineKeyboardMarkup(row_width=1)
    pending_buttons = 0
    for o in rows:
        lines.append(
            "<b>#{}</b> {} — {} ⭐\n{}\n{}".format(
                o["id"],
                esc(o["product_title"]),
                o["price"],
                STATUS.get(o["status"], o["status"]),
                esc(o["created_at"]),
            )
        )
        if o["status"] == "pending" and pending_buttons < 3:
            pending_buttons += 1
            kb.add(
                types.InlineKeyboardButton(
                    "💳 Оплатить заказ #{}".format(o["id"]),
                    callback_data="pay:{}".format(o["id"]),
                )
            )
    render(chat_id, "\n\n".join(lines), kb, message)


def begin_purchase(user, chat_id, product):
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
    if not product or not product["active"]:
        bot.send_message(chat_id, "Этот товар сейчас недоступен.")
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
        "Здесь можно купить Telegram Stars, Telegram Premium и другие цифровые "
        "товары. Оплата — официально, через Telegram Stars.\n\n"
        "Выберите раздел в меню ниже 👇".format(name),
        reply_markup=main_menu(),
    )


@bot.message_handler(commands=["cancel"])
@safe
def cmd_cancel(m):
    clear_state(m.from_user.id)
    bot.send_message(m.chat.id, "Действие отменено.", reply_markup=main_menu())


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
        "Магазин цифровых товаров: Telegram Stars, Telegram Premium и другое.\n"
        "• Оплата — только через Telegram Stars.\n"
        "• Заказ подтверждается после успешной оплаты.\n"
        "• Выдачу товара выполняет администратор — вы получите уведомление.\n"
        "• История заказов — в разделе «Мои покупки».",
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
        begin_purchase(user, chat_id, p)
    elif act == "self":
        p = get_product(int(parts[1]))
        if not p or not p["active"]:
            return "Товар недоступен"
        if not user.username:
            return "У вас не задан username. Отправьте @username вручную."
        clear_state(user.id)
        create_order_and_invoice(user, chat_id, p, "@" + user.username)
    elif act == "pay":
        o = get_order(int(parts[1]))
        if not o or o["user_id"] != user.id or o["status"] != "pending":
            return "Этот заказ нельзя оплатить"
        send_invoice_for(chat_id, o)
    elif act == "my":
        show_my_orders(chat_id, user.id, msg)
    elif act == "support":
        if not ADMIN_ID:
            return "Поддержка временно недоступна"
        set_state(user.id, "await_support")
        bot.send_message(chat_id, "✍️ Напишите ваше сообщение одним текстом. /cancel — отмена.")
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
        elif q.currency != "XTR" or q.total_amount != order["price"]:
            error = "Сумма платежа не совпадает с заказом."
        if error:
            bot.answer_pre_checkout_query(q.id, ok=False, error_message=error)
        else:
            bot.answer_pre_checkout_query(q.id, ok=True)
    except Exception:
        log.exception("Ошибка pre_checkout_query")
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
    text = (
        "📊 <b>Статистика</b>\n\n👥 Пользователей: {}\n"
        "⏳ Неоплаченных: {}\n💳 Ждут выдачи: {}\n✅ Выдано: {}\n↩️ Возвратов: {}\n\n"
        "💰 Выручка: <b>{} ⭐</b>"
    ).format(
        users,
        by_status.get("pending", 0),
        by_status.get("paid", 0),
        by_status.get("delivered", 0),
        by_status.get("refunded", 0),
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
                "{} {} — {}⭐".format(mark, p["title"], p["price"]),
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
    text = "<b>{}</b>\nID: {}\nРаздел: {}\nЦена: {} ⭐\nСтатус: {}\n\n{}".format(
        esc(p["title"]),
        p["id"],
        esc(CATS.get(p["category"], p["category"])),
        p["price"],
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
        set_state(user.id, "admin_" + act, arg)
        bot.send_message(chat_id, prompts[act] + " /cancel — отмена.")
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
        bot.send_message(
            chat_id,
            "Отправьте товар в формате:\n<code>Название | цена | описание</code>\n"
            "Описание можно не указывать. /cancel — отмена.",
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
        pid, _ = q_exec(
            "INSERT INTO products (category, title, description, price, sort) "
            "VALUES (?, ?, ?, ?, (SELECT COALESCE(MAX(sort), 0) + 1 FROM products WHERE category=?))",
            (data, parts[0][:60], desc[:500], int(parts[1]), data),
        )
        clear_state(uid)
        admin_product(m.chat.id, pid)


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
# Flask: Webhook
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
