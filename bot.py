"""Точка входа: Flask-приложение (Mini App + API + webhook) и Telegram-бот Gold Star Store.

Запуск на Render:  gunicorn bot:app --workers 1 --threads 8
Локально:          python bot.py
"""
import hmac
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, abort, jsonify, send_from_directory, request
from telebot import types

import admin
import api
import config
import database as db
import payments
import shop
from utils import (
    STATUS_LABELS,
    bot,
    btn,
    fmt_dt,
    h,
    kb,
    notify,
    notify_admins,
    safe_answer,
    setup_logging,
    show,
    stars,
    uzs,
)

log = logging.getLogger("goldstar.bot")
executor = ThreadPoolExecutor(max_workers=config.WORKER_THREADS, thread_name_prefix="upd")
PAGE = 6


# =================================================================== экраны бота
def v_home(uid: int):
    profile = payments.get_wallet_profile(uid)
    text = (
        f"⭐ <b>{h(config.BRAND)}</b>\n\n"
        f"Telegram Stars, Telegram Premium и подарки.\n"
        f"Баланс: <b>{uzs(profile['balance_uzs'])}</b> | Бонусы: <b>{uzs(profile['bonus_uzs'])}</b>\n\n"
        f"Основная работа магазина проходит в Mini App. Здесь вы можете посмотреть каталог, проверить статус заявок или отправить чек на пополнение."
    )
    rows = []
    if config.WEBAPP_URL:
        rows.append([btn("✨ Открыть Mini App", web_app=config.WEBAPP_URL)])
    rows.append([btn("🛍 Каталог", "u:cats"), btn("💳 Пополнить", "u:topup_info")])
    rows.append([btn("🧾 Заказы", "u:orders:0"), btn("🛟 Поддержка", "u:support")])
    return text, kb(*rows)


def v_cats():
    cats = shop.list_categories()
    if not cats:
        return "Каталог пока пуст. Загляните позже!", kb([btn("‹ Меню", "u:home")])
    rows = [[btn(c["title"], f"u:cat:{c['id']}")] for c in cats]
    rows.append([btn("‹ Меню", "u:home")])
    return "🛍 <b>Каталог товаров</b>\nВыберите категорию (покупки доступны в Mini App):", kb(*rows)


def v_cat(cid: int):
    cat = shop.get_category(cid)
    if not cat or not cat["is_active"]:
        return v_cats()
    prods = shop.list_products(category_id=cid)
    rows = []
    for p in prods:
        rows.append([btn(f"{p['title']} — {uzs(p['price_uzs'])}", f"u:prod:{p['id']}")])
    if config.WEBAPP_URL:
        rows.append([btn("✨ Купить в Mini App", web_app=config.WEBAPP_URL)])
    rows.append([btn("‹ Категории", "u:cats")])
    text = (
        f"<b>{h(cat['title'])}</b>\n{h(cat['description'])}\n\n"
        + ("Выберите позицию для просмотра:" if prods else "В этой категории пока нет товаров.")
    )
    return text, kb(*rows)


def v_product(pid: int):
    p = shop.get_product(pid, active_only=True)
    if not p:
        return "Товар недоступен.", kb([btn("‹ Каталог", "u:cats")])
    mode_label = "Ручная выдача" if p["fulfillment_mode"] == "manual" else "Автовыдача"
    text = (
        f"<b>{h(p['title'])}</b>\n"
        f"SKU: <code>{h(p['sku'])}</code> · {h(mode_label)}\n\n"
        f"{h(p['description'])}\n\n"
        f"Цена: <b>{uzs(p['price_uzs'])}</b>"
    )
    rows = []
    if config.WEBAPP_URL:
        rows.append([btn("✨ Оформить в Mini App", web_app=config.WEBAPP_URL)])
    rows.append([btn("‹ Назад", f"u:cat:{p['category_id']}")])
    return text, kb(*rows)


def v_topup_info(uid: int):
    pending = payments.get_latest_pending_topup(uid)
    text = (
        f"💳 <b>Пополнение баланса</b>\n\n"
        f"1. Создайте заявку в Mini App на сумму от <b>{uzs(config.TOPUP_MIN_UZS)}</b> до <b>{uzs(config.TOPUP_MAX_UZS)}</b>.\n"
        f"2. Переведите средства по реквизитам:\n<code>{h(config.TOPUP_REQUISITES)}</code>\n"
        f"3. Отправьте фото или скриншот чека сюда в чат с ботом.\n"
        f"4. После проверки поступления в банке администратор подтвердит заявку."
    )
    if pending:
        text += (
            f"\n\n📌 Ваша активная заявка: <b>#{pending['id']}</b> на сумму <b>{uzs(pending['amount_uzs'])}</b>. "
            f"Просто отправьте фото/скриншот чека в этот чат."
        )
    rows = []
    if config.WEBAPP_URL:
        rows.append([btn("✨ Открыть Mini App", web_app=config.WEBAPP_URL)])
    rows.append([btn("‹ Меню", "u:home")])
    return text, kb(*rows)


def v_orders(uid: int, page: int):
    w_orders = shop.list_wallet_orders(uid, limit=PAGE, offset=page * PAGE)
    if w_orders:
        rows = [
            [
                btn(
                    f"#{o['id']} · {STATUS_LABELS.get(o['status'], o['status'])} · {uzs(o['total_uzs'])}",
                    f"u:worder:{o['id']}",
                )
            ]
            for o in w_orders
        ]
        nav = []
        if page > 0:
            nav.append(btn("‹ Назад", f"u:orders:{page - 1}"))
        if len(w_orders) == PAGE:
            nav.append(btn("Вперёд ›", f"u:orders:{page + 1}"))
        if nav:
            rows.append(nav)
        rows.append([btn("‹ Меню", "u:home")])
        return "🧾 <b>Мои заказы</b>", kb(*rows)

    total = shop.count_orders(uid)
    if not total:
        return "У вас пока нет заказов.", kb([btn("🛍 В каталог", "u:cats")], [btn("‹ Меню", "u:home")])
    rows = [
        [btn(f"#{o['id']} · {STATUS_LABELS.get(o['status'], o['status'])} · {stars(o['total'])}", f"u:order:{o['id']}")]
        for o in shop.list_orders(uid, PAGE, page * PAGE)
    ]
    rows.append([btn("‹ Меню", "u:home")])
    return "🧾 <b>История старых заказов</b>", kb(*rows)


def v_worder(uid: int, oid: int):
    o = shop.get_wallet_order(oid)
    if not o or int(o["user_id"]) != int(uid):
        return "Заказ не найден.", kb([btn("‹ Заказы", "u:orders:0")])
    text = (
        f"🧾 <b>Заказ #{o['id']}</b>\n"
        f"Статус: {STATUS_LABELS.get(o['status'], o['status'])}\n"
        f"Товар: <b>{h(o['product_title'])}</b> × {int(o['qty'])}\n"
        f"Получатель: <b>{h(o['recipient'])}</b>\n"
        f"Создан: {fmt_dt(o['created_at'])}\n"
        f"Списано: <b>{uzs(int(o['total_uzs']))}</b>"
    )
    return text, kb([btn("‹ Заказы", "u:orders:0")])


def v_order(uid: int, oid: int):
    o = shop.get_order(oid, uid)
    if not o:
        return "Заказ не найден.", kb([btn("‹ Заказы", "u:orders:0")])
    items = "\n".join(f"• {h(i['title'])} × {i['qty']} — {stars(i['price'] * i['qty'])}" for i in o["items"])
    text = (
        f"🧾 <b>Заказ #{o['id']}</b>\n{STATUS_LABELS.get(o['status'], o['status'])}\n"
        f"Получатель: <b>{h(o['recipient'])}</b>\nСоздан: {fmt_dt(o['created_at'])}\n\n{items}\n\n"
        f"Итого: <b>{stars(o['total'])}</b>"
    )
    return text, kb([btn("‹ Заказы", "u:orders:0")])


def v_support():
    uname = db.get_setting("support_username", "GoldStar_uz_bot")
    text = db.get_setting("support_text", "Напишите нам по вопросам заказов и пополнений баланса.")
    rows = []
    msg = f"🛟 <b>Поддержка</b>\n\n{h(text)}"
    if uname:
        rows.append([btn(f"✉️ Написать @{uname}", url=f"https://t.me/{uname}")])
    rows.append([btn("‹ Меню", "u:home")])
    return msg, kb(*rows)


# =================================================================== вспомогательное
def _send(chat_id: int, view) -> None:
    bot.send_message(chat_id, view[0], reply_markup=view[1], disable_web_page_preview=True)


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
        _touch(m.from_user)
        bot.send_message(
            m.chat.id,
            "/start — меню и Mini App\n/topup — инструкция по пополнению баланса\n/orders — мои заказы\n/support — поддержка\n/cancel — отмена действия",
        )

    @bot.message_handler(commands=["topup"])
    def cmd_topup(m):
        _touch(m.from_user)
        _send(m.chat.id, v_topup_info(m.from_user.id))

    @bot.message_handler(commands=["orders"])
    def cmd_orders(m):
        _touch(m.from_user)
        _send(m.chat.id, v_orders(m.from_user.id, 0))

    @bot.message_handler(commands=["support"])
    def cmd_support(m):
        _touch(m.from_user)
        _send(m.chat.id, v_support())

    @bot.message_handler(commands=["cancel"])
    def cmd_cancel(m):
        _touch(m.from_user)
        db.fsm_clear(m.from_user.id)
        bot.send_message(m.chat.id, "Действие отменено.", reply_markup=kb([btn("‹ Меню", "u:home")]))

    @bot.message_handler(content_types=["photo", "document"])
    def on_receipt_media(m):
        uid = int(m.from_user.id)
        _touch(m.from_user)

        pending = payments.get_latest_pending_topup(uid)
        if not pending:
            bot.send_message(
                m.chat.id,
                "У вас нет активной заявки на пополнение. Сначала создайте заявку в Mini App, затем отправьте чек.",
                reply_markup=kb([btn("‹ Меню", "u:home")]),
            )
            return

        file_id = None
        if m.content_type == "photo" and m.photo:
            file_id = m.photo[-1].file_id
        elif m.content_type == "document" and m.document:
            mime = (m.document.mime_type or "").lower()
            if mime.startswith("image/") or mime == "application/pdf":
                file_id = m.document.file_id

        if not file_id:
            bot.send_message(m.chat.id, "Пожалуйста, отправьте скриншот или фото чека (изображение или PDF).")
            return

        caption = (m.caption or "").strip()
        try:
            updated_topup = payments.attach_topup_receipt(int(pending["id"]), uid, file_id, caption)
        except shop.ShopError as exc:
            bot.send_message(m.chat.id, f"⚠️ {h(exc)}")
            return

        admin.send_topup_to_admins(updated_topup, m.from_user)
        bot.send_message(
            m.chat.id,
            f"🧾 <b>Чек по заявке #{updated_topup['id']} отправлен администратору.</b>\n"
            f"Сумма: <b>{uzs(int(updated_topup['amount_uzs']))}</b>.\n"
            f"Ожидайте проверки поступления средств в банке.",
            reply_markup=kb([btn("‹ Меню", "u:home")]),
        )

    @bot.pre_checkout_query_handler(func=lambda q: True)
    def on_pre_checkout(query):
        payments.handle_pre_checkout(query)

    @bot.message_handler(content_types=["successful_payment"])
    def on_successful_payment(m):
        sp = m.successful_payment
        uid = int(m.from_user.id)
        _touch(m.from_user)
        res = payments.record_payment(
            uid,
            sp.currency,
            sp.total_amount,
            sp.invoice_payload,
            sp.telegram_payment_charge_id,
            getattr(sp, "provider_payment_charge_id", "") or "",
        )
        if res["result"] == "duplicate":
            return
        if res["result"] == "paid" and res.get("order"):
            notify(
                uid,
                f"✅ <b>Оплата получена!</b> Заказ #{res['order']['id']} принят.",
                kb([btn("🧾 Мои заказы", "u:orders:0")]),
            )
            notify_admins(f"💰 Оплата счёта #{res['order']['id']} ({stars(sp.total_amount)})")
        elif res.get("problem"):
            notify(
                uid,
                f"⚠️ Платёж получен, но требует проверки администратором (<code>{h(res['problem'])}</code>).",
            )
            notify_admins(f"⚠️ Платёж на проверке: {stars(sp.total_amount)}, причина: <code>{h(res['problem'])}</code>")

    @bot.callback_query_handler(func=lambda c: (c.data or "").startswith("u:"))
    def on_user_callback(call):
        uid = int(call.from_user.id)
        _touch(call.from_user)
        p = call.data.split(":")
        a = p[1] if len(p) > 1 else ""

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
        if a == "cat" and len(p) > 2 and p[2].isdigit():
            return go(v_cat(int(p[2])))
        if a == "prod" and len(p) > 2 and p[2].isdigit():
            return go(v_product(int(p[2])))
        if a == "topup_info":
            return go(v_topup_info(uid))
        if a == "orders" and len(p) > 2 and p[2].isdigit():
            return go(v_orders(uid, int(p[2])))
        if a == "worder" and len(p) > 2 and p[2].isdigit():
            return go(v_worder(uid, int(p[2])))
        if a == "order" and len(p) > 2 and p[2].isdigit():
            return go(v_order(uid, int(p[2])))
        if a == "support":
            return go(v_support())
        safe_answer(call.id)

    @bot.message_handler(content_types=["text"], func=lambda m: bool(m.text) and not m.text.startswith("/"))
    def on_text(m):
        uid = int(m.from_user.id)
        _touch(m.from_user)
        fsm = db.fsm_get(uid)
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
            bot.set_webhook(
                url=f"{config.WEBHOOK_URL}/webhook",
                secret_token=config.WEBHOOK_SECRET,
                allowed_updates=config.ALLOWED_UPDATES,
            )
            log.info("webhook set: %s/webhook", config.WEBHOOK_URL)
            break
        except Exception:  # noqa: BLE001
            log.exception("set_webhook failed, retry in %ss", delay)
            time.sleep(delay)
            delay = min(delay * 2, 60)
    try:
        bot.set_my_commands(
            [
                types.BotCommand("start", "Главное меню и Mini App"),
                types.BotCommand("topup", "Пополнение баланса"),
                types.BotCommand("orders", "Мои заказы"),
                types.BotCommand("support", "Поддержка"),
            ]
        )
        if config.WEBAPP_URL:
            bot.set_chat_menu_button(
                menu_button=types.MenuButtonWebApp(text="Магазин", web_app=types.WebAppInfo(config.WEBAPP_URL))
            )
    except Exception:  # noqa: BLE001
        log.exception("bot profile setup failed (не критично)")


def _acquire_leader_lock() -> bool:
    try:
        import fcntl
    except ImportError:
        return True

    path = config.DATABASE_PATH + ".lock"
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    fd = open(path, "w")  # noqa: SIM115
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
    app = Flask(__name__, static_folder="static", template_folder="static")
    app.config.update(
        SECRET_KEY=config.FLASK_SECRET_KEY,
        MAX_CONTENT_LENGTH=10 * 1024 * 1024,
        JSON_AS_ASCII=False,
        SEND_FILE_MAX_AGE_DEFAULT=300,
    )
    app.register_blueprint(api.api)

    @app.get("/")
    def index():
        return send_from_directory(app.static_folder, "index.html")

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
            inserted = db.inbox_add(update_id, raw)
        except Exception:  # noqa: BLE001
            log.exception("inbox_add failed")
            return "", 500
        if inserted:
            executor.submit(process_update, update_id)
        return "", 200

    @app.after_request
    def security_headers(resp):
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' https://telegram.org 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; connect-src 'self' https:; "
            "frame-ancestors 'self' https://web.telegram.org https://webk.telegram.org https://webz.telegram.org"
        )
        if request.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.errorhandler(400)
    def bad_request(err):
        return jsonify({"error": getattr(err, "description", None) or "Некорректный запрос"}), 400

    @app.errorhandler(404)
    def not_found(_):
        return jsonify({"error": "Не найдено"}), 404

    @app.errorhandler(413)
    def too_large(_):
        return jsonify({"error": "Превышен размер запроса"}), 413

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
        for uid in db.inbox_recover(startup=True):
            executor.submit(process_update, uid)
        threading.Thread(target=_sweeper, name="sweeper", daemon=True).start()
        if config.AUTO_SET_WEBHOOK and not config.problems():
            threading.Thread(target=_setup_webhook, name="webhook-setup", daemon=True).start()
    else:
        log.warning("Другой процесс уже держит блокировку — фоновые задачи не запускаются")
    return app


app = boot()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")), debug=False, use_reloader=False)