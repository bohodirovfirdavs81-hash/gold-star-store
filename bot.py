"""Точка входа: Flask-приложение (Mini App + API + webhook) и пользовательская часть бота.

Запуск на Render:  gunicorn bot:app --workers 1 --threads 8
Локально:          python bot.py   (нужен публичный https-адрес для webhook, например через туннель)
"""
import hmac
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, abort, jsonify, render_template, request
from telebot import types

import admin
import api
import config
import database as db
import payments
import shop
from utils import (STATUS_LABELS, bot, btn, fmt_dt, h, kb, normalize_recipient, notify, notify_admins,
                   safe_answer, setup_logging, show, stars)

log = logging.getLogger("goldstar.bot")
executor = ThreadPoolExecutor(max_workers=config.WORKER_THREADS, thread_name_prefix="upd")
PAGE = 6


# =================================================================== экраны пользователя
def v_home(uid: int):
    count = shop.get_cart(uid)["count"]
    text = ("⭐ <b>Добро пожаловать в Gold Star!</b>\n\n"
            "Telegram Premium, Stars и подарки — оплата Stars прямо в Telegram, без карт и комиссий банков.\n\n"
            "Выберите раздел:")
    rows = []
    if config.WEBAPP_URL:
        rows.append([btn("✨ Открыть магазин", web_app=config.WEBAPP_URL)])
    rows += [[btn("🛍 Каталог", "u:cats"), btn(f"🛒 Корзина{f' ({count})' if count else ''}", "u:cart")],
             [btn("🧾 Мои заказы", "u:orders:0"), btn("🛟 Поддержка", "u:support")]]
    return text, kb(*rows)


def v_cats():
    cats = shop.list_categories()
    if not cats:
        return "Каталог пока пуст. Загляните позже!", kb([btn("‹ Меню", "u:home")])
    rows = [[btn(c["title"], f"u:cat:{c['id']}")] for c in cats]
    rows.append([btn("‹ Меню", "u:home")])
    return "🛍 <b>Каталог</b>\nВыберите категорию:", kb(*rows)


def v_cat(cid: int):
    cat = shop.get_category(cid)
    if not cat or not cat["is_active"]:
        return v_cats()
    prods = shop.list_products(category_id=cid)
    rows = [[btn(f"{p['title']} — {stars(p['price'])}", f"u:prod:{p['id']}")] for p in prods]
    rows.append([btn("‹ Категории", "u:cats")])
    text = f"<b>{h(cat['title'])}</b>\n{h(cat['description'])}\n\n" + ("Выберите товар:" if prods else "В этой категории пока нет товаров.")
    return text, kb(*rows)


def v_product(pid: int, uid: int):
    p = shop.get_product(pid, active_only=True)
    if not p:
        return "Товар недоступен.", kb([btn("‹ Каталог", "u:cats")])
    in_cart = next((i["qty"] for i in shop.get_cart(uid)["items"] if i["product_id"] == pid), 0)
    text = f"<b>{h(p['title'])}</b>\n\n{h(p['description'])}\n\nЦена: <b>{stars(p['price'])}</b>"
    if in_cart:
        text += f"\nВ корзине: {in_cart} шт."
    return text, kb([btn("🛒 В корзину", f"u:add:{pid}"), btn("Корзина", "u:cart")],
                    [btn("‹ Назад", f"u:cat:{p['category_id']}")])


def v_cart(uid: int):
    cart = shop.get_cart(uid)
    if not cart["items"]:
        return "🛒 Корзина пуста.", kb([btn("🛍 В каталог", "u:cats")], [btn("‹ Меню", "u:home")])
    lines, rows = ["🛒 <b>Корзина</b>\n"], []
    for i in cart["items"]:
        mark = "" if i["available"] else " ⚠️ недоступно"
        lines.append(f"• {h(i['title'])}{mark}\n   {i['qty']} × {stars(i['price'])} = {stars(i['subtotal'])}")
        pid = i["product_id"]
        rows.append([btn("➖", f"u:dec:{pid}"), btn(f"{i['qty']} шт.", "u:noop"), btn("➕", f"u:inc:{pid}"),
                     btn("🗑", f"u:rm:{pid}")])
    lines.append(f"\nИтого: <b>{stars(cart['total'])}</b>")
    rows.append([btn("✅ Оформить заказ", "u:checkout")])
    rows.append([btn("🧹 Очистить", "u:clear"), btn("🛍 Каталог", "u:cats")])
    return "\n".join(lines), kb(*rows)


def v_orders(uid: int, page: int):
    total = shop.count_orders(uid)
    if not total:
        return "У вас пока нет заказов.", kb([btn("🛍 В каталог", "u:cats")], [btn("‹ Меню", "u:home")])
    rows = [[btn(f"#{o['id']} · {STATUS_LABELS.get(o['status'], o['status'])} · {stars(o['total'])}", f"u:order:{o['id']}")]
            for o in shop.list_orders(uid, PAGE, page * PAGE)]
    nav = []
    if page > 0:
        nav.append(btn("‹ Назад", f"u:orders:{page - 1}"))
    if (page + 1) * PAGE < total:
        nav.append(btn("Вперёд ›", f"u:orders:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([btn("‹ Меню", "u:home")])
    return "🧾 <b>Мои заказы</b>", kb(*rows)


def v_order(uid: int, oid: int):
    o = shop.get_order(oid, uid)
    if not o:
        return "Заказ не найден.", kb([btn("‹ Заказы", "u:orders:0")])
    items = "\n".join(f"• {h(i['title'])} × {i['qty']} — {stars(i['price'] * i['qty'])}" for i in o["items"])
    text = (f"🧾 <b>Заказ #{o['id']}</b>\n{STATUS_LABELS.get(o['status'], o['status'])}\n"
            f"Получатель: <b>{h(o['recipient'])}</b>\nСоздан: {fmt_dt(o['created_at'])}\n\n{items}\n\n"
            f"Итого: <b>{stars(o['total'])}</b>")
    rows = []
    if o["status"] == "pending":
        rows.append([btn("⭐ Оплатить", f"u:pay:{oid}"), btn("✖️ Отменить", f"u:cancel:{oid}")])
    rows.append([btn("‹ Заказы", "u:orders:0")])
    return text, kb(*rows)


def _support_contact():
    return db.get_setting("support_username"), db.get_setting("support_text")


def v_support():
    uname, text = _support_contact()
    rows = []
    msg = f"🛟 <b>Поддержка</b>\n\n{h(text)}"
    if uname:
        rows.append([btn(f"✉️ Написать @{uname}", url=f"https://t.me/{uname}")])
    else:
        msg += "\n\nКонтакт поддержки пока не настроен."
    rows.append([btn("‹ Меню", "u:home")])
    return msg, kb(*rows)


# =================================================================== вспомогательное
def _send(chat_id: int, view) -> None:
    bot.send_message(chat_id, view[0], reply_markup=view[1], disable_web_page_preview=True)


def _checkout_and_invoice(uid: int, chat_id: int, recipient_text: str) -> None:
    try:
        order = shop.create_order(uid, recipient_text)
    except shop.ShopError as exc:
        bot.send_message(chat_id, f"⚠️ {h(exc)}", reply_markup=kb([btn("🛒 Корзина", "u:cart")]))
        return
    try:
        payments.send_order_invoice(chat_id, order)
        bot.send_message(chat_id, f"Заказ <b>#{order['id']}</b> создан. Оплатите счёт выше — после оплаты мы сразу начнём выдачу.",
                         reply_markup=kb([btn("🧾 Мои заказы", "u:orders:0")]))
    except Exception:  # noqa: BLE001
        log.exception("send_invoice failed order=%s", order["id"])
        bot.send_message(chat_id, f"Заказ <b>#{order['id']}</b> сохранён, но счёт отправить не удалось. Оплатите его позже из раздела заказов.",
                         reply_markup=kb([btn("⭐ Оплатить", f"u:pay:{order['id']}")], [btn("🧾 Мои заказы", "u:orders:0")]))


def _touch(user) -> None:
    db.upsert_user(user)


# =================================================================== обработчики
def register_handlers() -> None:
    admin.register()

    @bot.message_handler(commands=["start", "menu"])
    def cmd_start(m):
        _touch(m.from_user)
        db.fsm_clear(m.from_user.id)
        _send(m.chat.id, v_home(m.from_user.id))

    @bot.message_handler(commands=["help"])
    def cmd_help(m):
        bot.send_message(m.chat.id, "/start — меню\n/cart — корзина\n/orders — заказы\n/support — поддержка\n/paysupport — вопросы по оплате\n/cancel — отмена действия")

    @bot.message_handler(commands=["cart"])
    def cmd_cart(m):
        _touch(m.from_user)
        _send(m.chat.id, v_cart(m.from_user.id))

    @bot.message_handler(commands=["orders"])
    def cmd_orders(m):
        _touch(m.from_user)
        _send(m.chat.id, v_orders(m.from_user.id, 0))

    @bot.message_handler(commands=["support"])
    def cmd_support(m):
        _send(m.chat.id, v_support())

    @bot.message_handler(commands=["paysupport"])
    def cmd_paysupport(m):
        uname, _ = _support_contact()
        contact = f"@{h(uname)}" if uname else "через раздел «Поддержка» в меню /start"
        bot.send_message(m.chat.id, "💳 <b>Вопросы по оплате и возвратам Stars</b>\n"
                         f"Напишите нам ({contact}) и укажите номер заказа. Возврат Stars оформляет администратор.")

    @bot.message_handler(commands=["cancel"])
    def cmd_cancel(m):
        db.fsm_clear(m.from_user.id)
        bot.send_message(m.chat.id, "Действие отменено.", reply_markup=kb([btn("‹ Меню", "u:home")]))

    # ---- оплата
    @bot.pre_checkout_query_handler(func=lambda q: True)
    def on_pre_checkout(query):
        payments.handle_pre_checkout(query)

    @bot.message_handler(content_types=["successful_payment"])
    def on_successful_payment(m):
        sp = m.successful_payment
        uid = m.from_user.id
        _touch(m.from_user)
        res = payments.record_payment(uid, sp.currency, sp.total_amount, sp.invoice_payload,
                                      sp.telegram_payment_charge_id,
                                      getattr(sp, "provider_payment_charge_id", "") or "")
        if res["result"] == "duplicate":
            return  # повторная доставка апдейта — ничего не делаем
        order, pay = res["order"], res["payment"]
        if res["result"] == "paid":
            notify(uid, f"✅ <b>Оплата получена!</b>\nЗаказ <b>#{order['id']}</b> принят. "
                        f"Получатель: {h(order['recipient'])}. Мы уведомим вас, когда заказ будет выдан.",
                   kb([btn("🧾 Мои заказы", "u:orders:0")]))
            notify_admins(f"💰 <b>Новая оплата</b>\nЗаказ #{order['id']} · {stars(order['total'])}\n"
                          f"Покупатель: id {uid}{(' @' + m.from_user.username) if m.from_user.username else ''}\n"
                          f"Получатель: <b>{h(order['recipient'])}</b>\n" +
                          "\n".join(f"• {h(i['title'])} × {i['qty']}" for i in order["items"]),
                          kb([btn("📦 Выдано", f"adm:deliver:{order['id']}"), btn("Открыть", f"adm:o:{order['id']}")]))
        else:
            notify(uid, "⚠️ Платёж получен, но требует проверки администратором. Мы свяжемся с вами или вернём Stars. "
                        "Подробности: /paysupport")
            notify_admins(f"⚠️ <b>Платёж требует проверки</b>\nПлатёж #{pay['id']} · {stars(pay['amount'])}\n"
                          f"Причина: <code>{h(res['problem'])}</code>\nТовар не выдавался.",
                          kb([btn("Открыть платёж", f"adm:pay:{pay['id']}")]))

    # ---- inline-кнопки пользователя
    @bot.callback_query_handler(func=lambda c: (c.data or "").startswith("u:"))
    def on_user_callback(call):
        uid = call.from_user.id
        _touch(call.from_user)
        p = call.data.split(":")
        a = p[1]
        chat = call.message.chat.id

        def go(view, toast=None):
            safe_answer(call.id, toast)
            show(call, view[0], view[1])

        if a == "noop":
            return safe_answer(call.id)
        if a == "home":
            db.fsm_clear(uid)
            return go(v_home(uid))
        if a == "cats":
            return go(v_cats())
        if a == "cat":
            return go(v_cat(int(p[2])))
        if a == "prod":
            return go(v_product(int(p[2]), uid))
        if a == "add":
            pid = int(p[2])
            try:
                qty = shop.cart_add(uid, pid, 1)
            except shop.ShopError as exc:
                return safe_answer(call.id, str(exc), True)
            return go(v_product(pid, uid), f"Добавлено. В корзине: {qty}")
        if a == "cart":
            return go(v_cart(uid))
        if a in ("inc", "dec", "rm"):
            pid = int(p[2])
            cur = next((i["qty"] for i in shop.get_cart(uid)["items"] if i["product_id"] == pid), 0)
            new = {"inc": cur + 1, "dec": cur - 1, "rm": 0}[a]
            try:
                shop.cart_set_qty(uid, pid, new)
            except shop.ShopError as exc:
                safe_answer(call.id, str(exc), True)
                return
            return go(v_cart(uid))
        if a == "clear":
            shop.cart_clear(uid)
            return go(v_cart(uid))
        if a == "checkout":
            cart = shop.get_cart(uid)
            if not cart["items"]:
                return go(v_cart(uid), "Корзина пуста")
            safe_answer(call.id)
            db.fsm_set(uid, "await_recipient", {})
            rows = []
            if call.from_user.username:
                rows.append([btn(f"Мне (@{call.from_user.username})", "u:recip:me")])
            rows.append([btn("‹ Отмена", "u:cart")])
            bot.send_message(chat, "Кому доставить заказ? Отправьте @username получателя в Telegram"
                                   " (для Premium и подарков он обязателен).", reply_markup=kb(*rows))
            return
        if a == "recip":
            safe_answer(call.id)
            fsm = db.fsm_get(uid)
            if not fsm or fsm["state"] != "await_recipient" or not call.from_user.username:
                return bot.send_message(chat, "Нажмите «Оформить заказ» в корзине ещё раз.")
            db.fsm_clear(uid)
            return _checkout_and_invoice(uid, chat, "@" + call.from_user.username)
        if a == "orders":
            return go(v_orders(uid, int(p[2])))
        if a == "order":
            return go(v_order(uid, int(p[2])))
        if a == "pay":
            safe_answer(call.id)
            order = shop.get_order(int(p[2]), uid)
            if not order:
                return bot.send_message(chat, "Заказ не найден.")
            try:
                payments.send_order_invoice(chat, order)
            except shop.ShopError as exc:
                bot.send_message(chat, f"⚠️ {h(exc)}")
            return
        if a == "cancel":
            oid = int(p[2])
            if shop.cancel_order(oid, uid):
                return go(v_order(uid, oid), "Заказ отменён")
            return go(v_order(uid, oid), "Отменить можно только неоплаченный заказ")
        if a == "support":
            return go(v_support())
        safe_answer(call.id)

    # ---- текстовый ввод (состояния) и всё остальное
    @bot.message_handler(content_types=["text"], func=lambda m: bool(m.text) and not m.text.startswith("/"))
    def on_text(m):
        uid = m.from_user.id
        _touch(m.from_user)
        fsm = db.fsm_get(uid)
        if fsm and fsm["state"] == "await_recipient":
            recipient = normalize_recipient(m.text)
            if not recipient:
                return bot.send_message(m.chat.id, "⚠️ Укажите username в формате @username (5–32 символа) или /cancel.")
            db.fsm_clear(uid)
            return _checkout_and_invoice(uid, m.chat.id, recipient)
        if fsm and admin.is_admin(uid):
            return admin.handle_state(m, fsm)
        if fsm:
            db.fsm_clear(uid)
        _send(m.chat.id, v_home(uid))


# =================================================================== обработка апдейтов
def process_update(update_id: int) -> None:
    row = db.inbox_claim(update_id)
    if not row:
        return
    try:
        update = types.Update.de_json(row["payload"])
        bot.process_new_updates([update])
        db.inbox_done(update_id)
    except Exception as exc:  # noqa: BLE001
        log.exception("update %s failed", update_id)
        db.inbox_fail(update_id, repr(exc))


def _sweeper() -> None:
    last_cleanup = 0.0
    while True:
        time.sleep(60)
        try:
            for uid in db.inbox_recover():
                executor.submit(process_update, uid)
            if time.time() - last_cleanup > 86400:
                db.inbox_cleanup()
                last_cleanup = time.time()
        except Exception:  # noqa: BLE001
            log.exception("sweeper error")


def _setup_webhook() -> None:
    delay = 2
    while True:
        try:
            bot.set_webhook(url=f"{config.WEBHOOK_URL}/webhook", secret_token=config.WEBHOOK_SECRET,
                            allowed_updates=config.ALLOWED_UPDATES)
            log.info("webhook set: %s/webhook", config.WEBHOOK_URL)
            break
        except Exception:  # noqa: BLE001
            log.exception("set_webhook failed, retry in %ss", delay)
            time.sleep(delay)
            delay = min(delay * 2, 60)
    try:
        bot.set_my_commands([types.BotCommand("start", "Главное меню"), types.BotCommand("cart", "Корзина"),
                             types.BotCommand("orders", "Мои заказы"), types.BotCommand("support", "Поддержка"),
                             types.BotCommand("paysupport", "Вопросы по оплате")])
        if config.WEBAPP_URL:
            bot.set_chat_menu_button(menu_button=types.MenuButtonWebApp(
                text="Магазин", web_app=types.WebAppInfo(config.WEBAPP_URL)))
    except Exception:  # noqa: BLE001
        log.exception("bot profile setup failed (не критично)")


def _acquire_leader_lock() -> bool:
    """Только один процесс на диске ведёт фоновые задачи и ставит webhook."""
    try:
        import fcntl
    except ImportError:  # Windows / локальная отладка
        return True
    import os
    path = config.DATABASE_PATH + ".lock"
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    fd = open(path, "w")  # noqa: SIM115 — дескриптор должен жить весь срок процесса
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fd.close()
        return False
    globals()["_lock_fd"] = fd
    return True


# =================================================================== Flask
def create_app() -> Flask:
    setup_logging()
    app = Flask(__name__, static_folder="static", template_folder="templates")
    app.config.update(SECRET_KEY=config.FLASK_SECRET_KEY, MAX_CONTENT_LENGTH=1024 * 1024,
                      JSON_AS_ASCII=False, SEND_FILE_MAX_AGE_DEFAULT=300)
    app.register_blueprint(api.api)

    @app.get("/")
    def index():
        return render_template("index.html", brand=config.BRAND)

    @app.get("/healthz")
    def healthz():
        try:
            db.q_one("SELECT 1 AS ok")
            return jsonify({"status": "ok"})
        except Exception:  # noqa: BLE001
            log.exception("healthz failed")
            return jsonify({"status": "db_error"}), 500

    @app.post("/webhook")
    def webhook():
        token = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not config.WEBHOOK_SECRET or not hmac.compare_digest(token, config.WEBHOOK_SECRET):
            abort(403)
        raw = request.get_data(cache=False, as_text=True)
        try:
            update_id = int(json.loads(raw)["update_id"])
        except (ValueError, KeyError, TypeError):
            abort(400)
        try:
            inserted = db.inbox_add(update_id, raw)  # сначала сохраняем — потом отвечаем 200
        except Exception:  # noqa: BLE001
            log.exception("inbox_add failed")
            return "", 500  # Telegram повторит доставку — апдейт не потеряется
        if inserted:
            executor.submit(process_update, update_id)
        return "", 200

    @app.after_request
    def security_headers(resp):
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' https://telegram.org; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; "
            "frame-ancestors 'self' https://web.telegram.org https://webk.telegram.org https://webz.telegram.org")
        if request.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.errorhandler(404)
    def not_found(_):
        return jsonify({"error": "Не найдено"}), 404

    @app.errorhandler(500)
    def server_error(_):
        return jsonify({"error": "Внутренняя ошибка сервера"}), 500

    return app


def boot() -> Flask:
    app = create_app()
    for problem in config.problems():
        log.error("Конфигурация: %s", problem)
    db.init_db()
    register_handlers()
    if _acquire_leader_lock():
        for uid in db.inbox_recover(startup=True):  # досылаем необработанные апдейты после рестарта
            executor.submit(process_update, uid)
        threading.Thread(target=_sweeper, name="sweeper", daemon=True).start()
        if config.AUTO_SET_WEBHOOK and not config.problems():
            threading.Thread(target=_setup_webhook, name="webhook-setup", daemon=True).start()
    else:
        log.warning("Другой процесс уже держит блокировку — фоновые задачи не запускаются")
    return app


app = boot()

if __name__ == "__main__":
    import os
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")), debug=False, use_reloader=False)