# -*- coding: utf-8 -*-
"""Gold Star Store — Telegram-магазин цифровых товаров.

Стек: pyTelegramBotAPI + Flask + SQLite + Webhook + Gunicorn.
Оплата: Telegram Stars (XTR) только для категории «Другие товары».
Telegram Stars — информационный раздел; Premium/Gifts — заявки администратору.
"""

import os
import re
import html
import hmac
import sqlite3
import logging
import threading
from datetime import datetime, timezone
from contextlib import closing

import telebot
from telebot import types
from flask import Flask, request, abort

# ------------------------- CONFIG -------------------------

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("goldstar")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
RENDER_URL = os.getenv("RENDER_URL", "").strip().rstrip("/")
DB_PATH = os.getenv("DB_PATH", "goldstar.db").strip()
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "").strip().lstrip("@")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)

if not BOT_TOKEN:
    raise RuntimeError("Не задана переменная BOT_TOKEN")
if not WEBHOOK_SECRET:
    raise RuntimeError("Не задана переменная WEBHOOK_SECRET")

bot = telebot.TeleBot(BOT_TOKEN, threaded=False, parse_mode="HTML")
app = Flask(__name__)

CATEGORIES = {
    "stars": "⭐ Telegram Stars",
    "premium": "💎 Telegram Premium",
    "gifts": "🎁 Telegram Gifts",
    "other": "📦 Другие товары",
}
STATUS = {
    "pending": "⏳ Ожидает оплаты",
    "paid": "💳 Оплачен, ожидает выдачи",
    "delivered": "✅ Выдан",
    "refunded": "↩️ Возврат",
}
REQUEST_STATUS = {
    "new": "🆕 Новая",
    "work": "🔧 В работе",
    "done": "✅ Выполнена",
    "rejected": "❌ Отклонена",
}
USERNAME_RE = re.compile(r"^@?([A-Za-z0-9_]{5,32})$")

SEED = [
    ("stars_50", "stars", "⭐ 50 Stars", "Пакет 50 Telegram Stars", 10490, "info", 10),
    ("stars_75", "stars", "⭐ 75 Stars", "Пакет 75 Telegram Stars", 15690, "info", 20),
    ("stars_100", "stars", "⭐ 100 Stars", "Пакет 100 Telegram Stars", 20990, "info", 30),
    ("stars_150", "stars", "⭐ 150 Stars", "Пакет 150 Telegram Stars", 31490, "info", 40),
    ("stars_200", "stars", "⭐ 200 Stars", "Пакет 200 Telegram Stars", 41990, "info", 50),
    ("stars_250", "stars", "⭐ 250 Stars", "Пакет 250 Telegram Stars", 52490, "info", 60),
    ("stars_350", "stars", "⭐ 350 Stars", "Пакет 350 Telegram Stars", 73490, "info", 70),
    ("stars_500", "stars", "⭐ 500 Stars", "Пакет 500 Telegram Stars", 104990, "info", 80),
    ("premium_3", "premium", "💎 Premium 3 мес.", "Telegram Premium на 3 месяца", 159990, "request", 10),
    ("premium_6", "premium", "💎 Premium 6 мес.", "Telegram Premium на 6 месяцев", 214990, "request", 20),
    ("premium_12", "premium", "💎 Premium 12 мес.", "Telegram Premium на 12 месяцев", 389990, "request", 30),
    ("gift_heart", "gifts", "❤️ Сердце", "Telegram Gift «Сердце»", 3790, "request", 10),
    ("gift_bear", "gifts", "🧸 Мишка", "Telegram Gift «Мишка»", 3790, "request", 20),
    ("gift_present", "gifts", "🎁 Подарок", "Telegram Gift «Подарок»", 5490, "request", 30),
    ("gift_rose", "gifts", "🌹 Роза", "Telegram Gift «Роза»", 5490, "request", 40),
    ("gift_cake", "gifts", "🎂 Торт", "Telegram Gift «Торт»", 10990, "request", 50),
    ("gift_bouquet", "gifts", "💐 Букет", "Telegram Gift «Букет»", 10990, "request", 60),
    ("gift_rocket", "gifts", "🚀 Ракета", "Telegram Gift «Ракета»", 10990, "request", 70),
    ("gift_champagne", "gifts", "🍾 Шампанское", "Telegram Gift «Шампанское»", 10990, "request", 80),
    ("gift_cup", "gifts", "🏆 Кубок", "Telegram Gift «Кубок»", 21990, "request", 90),
    ("gift_ring", "gifts", "💍 Кольцо", "Telegram Gift «Кольцо»", 21990, "request", 100),
    ("gift_diamond", "gifts", "💎 Бриллиант", "Telegram Gift «Бриллиант»", 21990, "request", 110),
]

# ------------------------- DATABASE -------------------------

def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db():
    with closing(db()) as c, c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS products(
            id INTEGER PRIMARY KEY AUTOINCREMENT, sku TEXT UNIQUE,
            category TEXT NOT NULL, title TEXT NOT NULL, description TEXT DEFAULT '',
            price_xtr INTEGER DEFAULT 0, price_uzs INTEGER DEFAULT 0,
            sale_mode TEXT NOT NULL DEFAULT 'xtr', active INTEGER NOT NULL DEFAULT 1,
            sort INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS orders(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            product_id INTEGER, product_title TEXT NOT NULL, price INTEGER NOT NULL,
            target TEXT DEFAULT '', status TEXT NOT NULL DEFAULT 'pending',
            charge_id TEXT UNIQUE, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS requests(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            product_id INTEGER, product_title TEXT NOT NULL, price_uzs INTEGER DEFAULT 0,
            target TEXT DEFAULT '', status TEXT NOT NULL DEFAULT 'new',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS states(
            user_id INTEGER PRIMARY KEY, state TEXT NOT NULL, data TEXT DEFAULT ''
        );
        """)
        # Совместимость с прежней базой проекта: добавляем недостающие поля,
        # не удаляя существующие заказы и пользователей.
        product_cols = {row["name"] for row in c.execute("PRAGMA table_info(products)")}
        for name, declaration in (
            ("sku", "TEXT"),
            ("price_xtr", "INTEGER DEFAULT 0"),
            ("price_uzs", "INTEGER DEFAULT 0"),
            ("sale_mode", "TEXT NOT NULL DEFAULT 'xtr'"),
        ):
            if name not in product_cols:
                c.execute(f"ALTER TABLE products ADD COLUMN {name} {declaration}")
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_products_sku ON products(sku)")
        product_cols = {row["name"] for row in c.execute("PRAGMA table_info(products)")}
        if "price" in product_cols:
            c.execute("UPDATE products SET price_xtr=price WHERE price_xtr=0 AND sale_mode='xtr'")
        c.execute("UPDATE products SET sale_mode='info' WHERE category='stars'")
        c.execute("UPDATE products SET sale_mode='request' WHERE category IN ('premium','gifts')")

        for sku, cat, title, desc, uzs, mode, sort in SEED:
            c.execute("""
                INSERT OR IGNORE INTO products
                (sku,category,title,description,price_uzs,sale_mode,sort,active)
                VALUES(?,?,?,?,?,?,?,1)
            """, (sku, cat, title, desc, uzs, mode, sort))

def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

def esc(v):
    return html.escape(str(v if v is not None else ""))

def q_one(sql, args=()):
    with closing(db()) as c:
        return c.execute(sql, args).fetchone()

def q_all(sql, args=()):
    with closing(db()) as c:
        return c.execute(sql, args).fetchall()

def q_exec(sql, args=()):
    with closing(db()) as c, c:
        cur = c.execute(sql, args)
        return cur.lastrowid, cur.rowcount

def is_admin(uid):
    return bool(ADMIN_ID and int(uid) == ADMIN_ID)

def save_user(user):
    q_exec("""
        INSERT INTO users(user_id,username,first_name,created_at)
        VALUES(?,?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,
        first_name=excluded.first_name
    """, (user.id, user.username or "", user.first_name or "", now()))

def set_state(uid, state, data=""):
    q_exec("INSERT INTO states(user_id,state,data) VALUES(?,?,?) "
           "ON CONFLICT(user_id) DO UPDATE SET state=excluded.state,data=excluded.data",
           (uid, state, str(data)))

def get_state(uid):
    return q_one("SELECT state,data FROM states WHERE user_id=?", (uid,))

def clear_state(uid):
    q_exec("DELETE FROM states WHERE user_id=?", (uid,))

def product(pid):
    return q_one("SELECT * FROM products WHERE id=? AND active=1", (pid,))

# ------------------------- KEYBOARDS / SCREENS -------------------------

def main_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("⭐ Telegram Stars", "💎 Telegram Premium")
    kb.row("🎁 Telegram Gifts", "📦 Другие товары")
    kb.row("📦 Мои покупки", "📞 Поддержка")
    kb.row("ℹ️ Информация")
    return kb

def inline_back(callback="home"):
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("⬅️ Назад", callback_data=callback))
    return kb

def show_home(chat_id, message_id=None):
    text = (
        "<b>⭐ GOLD STAR STORE</b>\n\n"
        "Добро пожаловать! Выберите нужный раздел в меню.\n\n"
        "💎 Premium и 🎁 Gifts оформляются заявкой.\n"
        "⭐ Telegram Stars — информационный каталог.\n"
        "📦 Другие товары — оплата Telegram Stars."
    )
    if message_id:
        try:
            bot.edit_message_text(text, chat_id, message_id, reply_markup=None)
            return
        except Exception:
            pass
    bot.send_message(chat_id, text, reply_markup=main_menu())

def show_category(chat_id, category, message=None):
    if category not in CATEGORIES:
        return
    rows = q_all("SELECT * FROM products WHERE category=? AND active=1 ORDER BY sort,id", (category,))
    kb = types.InlineKeyboardMarkup(row_width=1)
    for p in rows:
        price = (f"{p['price_xtr']} ⭐" if p["sale_mode"] == "xtr"
                 else f"{p['price_uzs']:,}".replace(",", " ") + " сум")
        kb.add(types.InlineKeyboardButton(
            f"{p['title']} · {price}", callback_data=f"product:{p['id']}"
        ))
    kb.add(types.InlineKeyboardButton("⬅️ Главное меню", callback_data="home"))
    note = {
        "stars": "\n\n<i>Только просмотр. Покупка Stars через бота пока недоступна.</i>",
        "premium": "\n\n<i>Выберите тариф, чтобы отправить заявку администратору.</i>",
        "gifts": "\n\n<i>Выберите подарок, чтобы отправить заявку администратору.</i>",
        "other": "\n\n<i>Оплата доступна через Telegram Stars.</i>",
    }.get(category, "")
    text = f"<b>{CATEGORIES[category]}</b>{note}"
    if message:
        try:
            bot.edit_message_text(text, chat_id, message.message_id, reply_markup=kb)
            return
        except Exception:
            pass
    bot.send_message(chat_id, text, reply_markup=kb)

def show_product(chat_id, pid, message=None):
    p = product(pid)
    if not p:
        bot.send_message(chat_id, "Товар не найден или уже недоступен.")
        return
    text = f"<b>{esc(p['title'])}</b>\n{esc(p['description'])}\n\n"
    if p["sale_mode"] == "info":
        text += "ℹ️ Этот раздел доступен только для просмотра."
        kb = inline_back(f"cat:{p['category']}")
    elif p["sale_mode"] == "request":
        text += f"Цена: <b>{p['price_uzs']:,} сум</b>\nНажмите кнопку, чтобы оставить заявку.".replace(",", " ")
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("📝 Оставить заявку", callback_data=f"request:{pid}"))
        kb.add(types.InlineKeyboardButton("⬅️ Назад", callback_data=f"cat:{p['category']}"))
    else:
        text += f"Цена: <b>{p['price_xtr']} ⭐</b>"
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("⭐ Купить", callback_data=f"buy:{pid}"))
        kb.add(types.InlineKeyboardButton("⬅️ Назад", callback_data=f"cat:{p['category']}"))
    if message:
        try:
            bot.edit_message_text(text, chat_id, message.message_id, reply_markup=kb)
            return
        except Exception:
            pass
    bot.send_message(chat_id, text, reply_markup=kb)

# ------------------------- ORDERS / REQUESTS -------------------------

def begin_target(uid, pid, state, chat_id, title):
    p = product(pid)
    if not p:
        bot.send_message(chat_id, "Товар больше недоступен.")
        return
    set_state(uid, state, pid)
    bot.send_message(
        chat_id,
        f"{title}\nОтправьте @username получателя (например, @username).\n/cancel — отмена."
    )

def create_request(user, chat_id, p, target):
    rid, _ = q_exec("""
        INSERT INTO requests(user_id,product_id,product_title,price_uzs,target,status,created_at,updated_at)
        VALUES(?,?,?,?,?,'new',?,?)
    """, (user.id, p["id"], p["title"], p["price_uzs"], target, now(), now()))
    uname = "@" + user.username if user.username else "—"
    if ADMIN_ID:
        kb = types.InlineKeyboardMarkup()
        kb.row(
            types.InlineKeyboardButton("🔧 В работу", callback_data=f"adm:reqwork:{rid}"),
            types.InlineKeyboardButton("✅ Выполнена", callback_data=f"adm:reqdone:{rid}")
        )
        kb.add(types.InlineKeyboardButton("❌ Отклонить", callback_data=f"adm:reqreject:{rid}"))
        bot.send_message(
            ADMIN_ID,
            f"🆕 <b>Новая заявка #{rid}</b>\n"
            f"Клиент: <code>{user.id}</code> {esc(uname)}\n"
            f"Товар: {esc(p['title'])}\nПолучатель: {esc(target)}\n"
            f"Цена: {p['price_uzs']:,} сум".replace(",", " "),
            reply_markup=kb
        )
    bot.send_message(chat_id, f"✅ Заявка #{rid} отправлена администратору. Он свяжется с вами.")

def send_invoice_for(chat_id, user_id, p, target):
    oid, _ = q_exec("""
        INSERT INTO orders(user_id,product_id,product_title,price,target,status,created_at)
        VALUES(?,?,?,?,?,'pending',?)
    """, (user_id, p["id"], p["title"], p["price_xtr"], target, now()))
    payload = f"goldstar:{oid}:{user_id}"
    prices = [types.LabeledPrice(label=p["title"], amount=int(p["price_xtr"]))]
    try:
        bot.send_invoice(
            chat_id=chat_id,
            title=p["title"][:32],
            description=(p["description"] or p["title"])[:255],
            invoice_payload=payload,
            provider_token="",
            currency="XTR",
            prices=prices,
        )
    except Exception:
        q_exec("DELETE FROM orders WHERE id=? AND status='pending'", (oid,))
        log.exception("Не удалось отправить счёт")
        bot.send_message(chat_id, "Не удалось создать счёт. Попробуйте позже.")

def parse_payload(payload):
    try:
        prefix, oid, uid = payload.split(":")
        if prefix != "goldstar":
            return None, None
        return int(oid), int(uid)
    except Exception:
        return None, None

# ------------------------- COMMANDS -------------------------

@bot.message_handler(commands=["start"])
def cmd_start(m):
    save_user(m.from_user)
    clear_state(m.from_user.id)
    show_home(m.chat.id)

@bot.message_handler(commands=["cancel"])
def cmd_cancel(m):
    clear_state(m.from_user.id)
    bot.send_message(m.chat.id, "Действие отменено.", reply_markup=main_menu())

@bot.message_handler(commands=["admin"])
def cmd_admin(m):
    if not is_admin(m.from_user.id):
        return
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("📊 Статистика", callback_data="adm:stats:0"))
    kb.add(types.InlineKeyboardButton("📦 Заказы", callback_data="adm:orders:0"))
    kb.add(types.InlineKeyboardButton("📝 Заявки", callback_data="adm:requests:0"))
    kb.add(types.InlineKeyboardButton("➕ Добавить товар", callback_data="adm:add:other"))
    bot.send_message(m.chat.id, "<b>Панель администратора</b>", reply_markup=kb)

@bot.message_handler(commands=["reply"])
def cmd_reply(m):
    if not is_admin(m.from_user.id):
        return
    parts = (m.text or "").split(maxsplit=2)
    if len(parts) != 3 or not parts[1].isdigit():
        bot.reply_to(m, "Формат: /reply USER_ID текст")
        return
    try:
        bot.send_message(int(parts[1]), "📨 <b>Ответ поддержки:</b>\n" + esc(parts[2]))
        bot.reply_to(m, "Сообщение отправлено.")
    except Exception as e:
        bot.reply_to(m, "Не удалось отправить: " + esc(e))

# ------------------------- TEXT HANDLERS -------------------------

@bot.message_handler(content_types=["text"], func=lambda m: bool(m.text) and not m.text.startswith("/"))
def on_text(m):
    save_user(m.from_user)
    uid, txt = m.from_user.id, (m.text or "").strip()
    st = get_state(uid)
    if st:
        state, data = st["state"], st["data"]
        if state == "admin_add" and is_admin(uid):
            parts = [x.strip() for x in txt.split("|", 2)]
            if len(parts) < 2 or not parts[0] or not parts[1].isdigit() or int(parts[1]) < 1:
                bot.send_message(m.chat.id, "Формат: Название | цена в XTR | описание")
                return
            category = data if data in CATEGORIES else "other"
            q_exec(
                "INSERT INTO products(category,title,description,price_xtr,price_uzs,sale_mode,sort,active) "
                "VALUES(?,?,?,?,0,'xtr',999,1)",
                (category, parts[0][:60], parts[2][:300] if len(parts) > 2 else "", int(parts[1]))
            )
            clear_state(uid)
            bot.send_message(m.chat.id, "✅ Товар добавлен.")
            return
        if state in ("target_order", "target_request"):
            match = USERNAME_RE.match(txt)
            if not match:
                bot.send_message(m.chat.id, "Введите корректный @username или /cancel.")
                return
            p = product(int(data))
            clear_state(uid)
            if not p:
                bot.send_message(m.chat.id, "Товар больше недоступен.")
                return
            target = "@" + match.group(1)
            if state == "target_request":
                create_request(m.from_user, m.chat.id, p, target)
            else:
                send_invoice_for(m.chat.id, uid, p, target)
            return
        if state == "support":
            clear_state(uid)
            if ADMIN_ID:
                bot.send_message(
                    ADMIN_ID,
                    f"📩 <b>Поддержка</b>\nПользователь: <code>{uid}</code>\n\n{esc(txt)}\n"
                    f"Ответ: /reply {uid} текст"
                )
            bot.send_message(m.chat.id, "✅ Сообщение отправлено в поддержку.")
            return

    mapping = {
        "⭐ Telegram Stars": "stars",
        "💎 Telegram Premium": "premium",
        "🎁 Telegram Gifts": "gifts",
        "📦 Другие товары": "other",
    }
    if txt in mapping:
        show_category(m.chat.id, mapping[txt])
    elif txt == "📦 Мои покупки":
        orders = q_all("SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 10", (uid,))
        reqs = q_all("SELECT * FROM requests WHERE user_id=? ORDER BY id DESC LIMIT 10", (uid,))
        lines = ["<b>📦 Ваши покупки и заявки</b>"]
        for o in orders:
            lines.append(f"Заказ #{o['id']} · {esc(o['product_title'])} · {STATUS.get(o['status'], o['status'])}")
        for r in reqs:
            lines.append(f"Заявка #{r['id']} · {esc(r['product_title'])} · {REQUEST_STATUS.get(r['status'], r['status'])}")
        bot.send_message(m.chat.id, "\n".join(lines) if len(lines) > 1 else "У вас пока нет покупок и заявок.", reply_markup=main_menu())
    elif txt == "📞 Поддержка":
        if SUPPORT_USERNAME:
            bot.send_message(m.chat.id, f"Поддержка: @{esc(SUPPORT_USERNAME)}")
        else:
            set_state(uid, "support")
            bot.send_message(m.chat.id, "Напишите сообщение для поддержки. /cancel — отмена.")
    elif txt == "ℹ️ Информация":
        bot.send_message(m.chat.id, "⭐ <b>Gold Star Store</b>\nPremium и Gifts оформляются заявкой. Оплата других товаров производится Telegram Stars.", reply_markup=main_menu())
    else:
        bot.send_message(m.chat.id, "Выберите раздел в меню.", reply_markup=main_menu())

# ------------------------- CALLBACKS -------------------------

@bot.callback_query_handler(func=lambda c: True)
def callbacks(c):
    try:
        data = c.data or ""
        uid, chat_id = c.from_user.id, c.message.chat.id
        if data == "home":
            show_home(chat_id, c.message.message_id)
        elif data.startswith("cat:"):
            show_category(chat_id, data.split(":", 1)[1], c.message)
        elif data.startswith("product:"):
            show_product(chat_id, int(data.split(":")[1]), c.message)
        elif data.startswith("request:"):
            pid = int(data.split(":")[1])
            begin_target(uid, pid, "target_request", chat_id, "Оформление заявки.")
        elif data.startswith("buy:"):
            pid = int(data.split(":")[1])
            p = product(pid)
            if not p or p["sale_mode"] != "xtr":
                bot.answer_callback_query(c.id, "Этот товар нельзя оплатить здесь.", show_alert=True)
            else:
                begin_target(uid, pid, "target_order", chat_id, "Покупка товара.")
        elif data.startswith("adm:"):
            if not is_admin(uid):
                bot.answer_callback_query(c.id, "Нет доступа", show_alert=True)
                return
            _, action, arg = (data.split(":") + ["0"])[:3]
            if action == "stats":
                users = q_one("SELECT COUNT(*) n FROM users")["n"]
                orders = q_one("SELECT COUNT(*) n FROM orders")["n"]
                reqs = q_one("SELECT COUNT(*) n FROM requests")["n"]
                bot.send_message(chat_id, f"📊 Пользователи: {users}\nЗаказы: {orders}\nЗаявки: {reqs}")
            elif action == "orders":
                rows = q_all("SELECT * FROM orders ORDER BY id DESC LIMIT 15")
                bot.send_message(chat_id, "\n".join(
                    f"#{o['id']} · {esc(o['product_title'])} · {STATUS.get(o['status'], o['status'])}"
                    for o in rows
                ) or "Заказов нет.")
            elif action == "requests":
                rows = q_all("SELECT * FROM requests ORDER BY id DESC LIMIT 15")
                bot.send_message(chat_id, "\n".join(
                    f"#{r['id']} · {esc(r['product_title'])} · {REQUEST_STATUS.get(r['status'], r['status'])}"
                    for r in rows
                ) or "Заявок нет.")
            elif action in ("reqwork", "reqdone", "reqreject"):
                status = {"reqwork": "work", "reqdone": "done", "reqreject": "rejected"}[action]
                row = q_one("SELECT * FROM requests WHERE id=?", (int(arg),))
                if row:
                    q_exec("UPDATE requests SET status=?,updated_at=? WHERE id=?", (status, now(), int(arg)))
                    messages = {
                        "work": "🔧 Ваша заявка принята в работу.",
                        "done": "✅ Ваша заявка выполнена.",
                        "rejected": "❌ Ваша заявка отклонена. По вопросам обратитесь в поддержку."
                    }
                    try:
                        bot.send_message(row["user_id"], messages[status])
                    except Exception:
                        log.exception("Не удалось уведомить пользователя")
                    bot.answer_callback_query(c.id, "Статус обновлён.")
                else:
                    bot.answer_callback_query(c.id, "Заявка не найдена.", show_alert=True)
                return
            elif action == "add":
                set_state(uid, "admin_add", arg)
                bot.send_message(chat_id, "Отправьте товар: Название | цена в XTR | описание\n/cancel — отмена.")
            bot.answer_callback_query(c.id)
            return
        bot.answer_callback_query(c.id)
    except Exception:
        log.exception("Ошибка callback")
        try:
            bot.answer_callback_query(c.id, "Произошла ошибка.", show_alert=True)
        except Exception:
            pass

# ------------------------- ADMIN ADD PRODUCT -------------------------

@bot.message_handler(func=lambda m: bool(m.text) and get_state(m.from_user.id) is not None)
def admin_state(m):
    st = get_state(m.from_user.id)
    if not st or st["state"] != "admin_add" or not is_admin(m.from_user.id):
        return
    parts = [x.strip() for x in m.text.split("|", 2)]
    if len(parts) < 2 or not parts[0] or not parts[1].isdigit() or int(parts[1]) < 1:
        bot.reply_to(m, "Формат: Название | цена в XTR | описание")
        return
    category = st["data"] if st["data"] in CATEGORIES else "other"
    q_exec("""
        INSERT INTO products(category,title,description,price_xtr,price_uzs,sale_mode,sort,active)
        VALUES(?,?,?,?,0,'xtr',999,1)
    """, (category, parts[0][:60], parts[2][:300] if len(parts) > 2 else "", int(parts[1])))
    clear_state(m.from_user.id)
    bot.reply_to(m, "✅ Товар добавлен.")

# ------------------------- TELEGRAM PAYMENTS -------------------------

@bot.pre_checkout_query_handler(func=lambda q: True)
def pre_checkout(q):
    oid, uid = parse_payload(q.invoice_payload)
    row = q_one("SELECT * FROM orders WHERE id=? AND user_id=? AND status='pending'", (oid, uid)) if oid else None
    valid = bool(row and uid == q.from_user.id and q.currency == "XTR" and q.total_amount == row["price"])
    bot.answer_pre_checkout_query(q.id, ok=valid, error_message=None if valid else "Счёт недействителен. Создайте новый заказ.")

@bot.message_handler(content_types=["successful_payment"])
def successful_payment(m):
    pay = m.successful_payment
    oid, uid = parse_payload(pay.invoice_payload)
    if not oid or uid != m.from_user.id or pay.currency != "XTR":
        log.warning("Некорректный payload успешного платежа")
        return
    row = q_one("SELECT * FROM orders WHERE id=? AND user_id=? AND status='pending'", (oid, uid))
    if not row or int(row["price"]) != int(pay.total_amount):
        log.error("Платёж не совпал с заказом #%s", oid)
        return
    _, changed = q_exec(
        "UPDATE orders SET status='paid',charge_id=? WHERE id=? AND status='pending'",
        (pay.telegram_payment_charge_id, oid)
    )
    if changed:
        bot.send_message(m.chat.id, f"✅ Оплата заказа #{oid} прошла успешно. Администратор скоро обработает заказ.")
        if ADMIN_ID:
            bot.send_message(
                ADMIN_ID,
                f"💳 <b>Новый оплаченный заказ #{oid}</b>\n"
                f"Пользователь: <code>{uid}</code>\nТовар: {esc(row['product_title'])}\n"
                f"Получатель: {esc(row['target'])}\nСумма: {pay.total_amount} ⭐"
            )

# ------------------------- FLASK WEBHOOK -------------------------

@app.get("/")
def index():
    return "Gold Star Store is running", 200

@app.get("/healthz")
def healthz():
    return "ok", 200

@app.post("/webhook")
def webhook():
    if not hmac.compare_digest(
        request.headers.get("X-Telegram-Bot-Api-Secret-Token", ""),
        WEBHOOK_SECRET
    ):
        abort(403)
    if not request.is_json:
        abort(403)
    try:
        update = telebot.types.Update.de_json(request.get_data().decode("utf-8"))
        bot.process_new_updates([update])
    except Exception:
        log.exception("Webhook update failed")
    return "", 200

def setup_webhook():
    if not RENDER_URL:
        log.warning("RENDER_URL не задан — webhook не настроен")
        return
    try:
        bot.set_webhook(
            url=RENDER_URL + "/webhook",
            secret_token=WEBHOOK_SECRET,
            allowed_updates=["message", "callback_query", "pre_checkout_query"]
        )
        log.info("Webhook установлен")
    except Exception:
        log.exception("Не удалось установить webhook")

init_db()
if RENDER_URL:
    threading.Thread(target=setup_webhook, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))