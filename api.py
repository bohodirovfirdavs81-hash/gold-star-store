import hashlib
import hmac
import json
import time
from functools import wraps
from typing import Any
from urllib.parse import parse_qsl

from flask import Blueprint, g, jsonify, request

import admin
import config
import database as db
import payments
import shop

api = Blueprint("api", __name__, url_prefix="/api")


def verify_telegram_init_data(
    init_data: str,
    bot_token: str,
    max_age_sec: int,
) -> dict[str, Any] | None:
    if not init_data or not bot_token:
        return None

    try:
        pairs_list = parse_qsl(init_data, keep_blank_values=True)
        pairs = dict(pairs_list)

        # Duplicate fields are rejected.
        if len(pairs) != len(pairs_list):
            return None

        received_hash = pairs.pop("hash", "")
        if not received_hash:
            return None

        auth_date = int(pairs.get("auth_date", "0"))
        now_ts = int(time.time())

        if auth_date <= 0:
            return None
        if now_ts - auth_date > max_age_sec:
            return None
        if auth_date > now_ts + 300:
            return None

        data_check_string = "\n".join(
            f"{key}={pairs[key]}" for key in sorted(pairs)
        )

        secret_key = hmac.new(
            b"WebAppData",
            bot_token.encode("utf-8"),
            hashlib.sha256,
        ).digest()

        calculated_hash = hmac.new(
            secret_key,
            data_check_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        if not hmac.compare_digest(calculated_hash, received_hash):
            return None

        raw_user = pairs.get("user")
        if not raw_user:
            return None

        user_obj = json.loads(raw_user)
        if not isinstance(user_obj, dict):
            return None

        uid = int(user_obj.get("id", 0))
        if uid <= 0:
            return None

        nonce_hash = hashlib.sha256(
            f"{uid}:{auth_date}:{received_hash}".encode("utf-8")
        ).hexdigest()

        return {
            "id": uid,
            "username": user_obj.get("username"),
            "first_name": user_obj.get("first_name"),
            "last_name": user_obj.get("last_name"),
            "language_code": user_obj.get("language_code"),
            "nonce_hash": nonce_hash,
        }

    except (ValueError, TypeError, json.JSONDecodeError):
        return None


class WebAppUser:
    def __init__(self, data: dict[str, Any]):
        self.id = data["id"]
        self.username = data.get("username")
        self.first_name = data.get("first_name")
        self.last_name = data.get("last_name")
        self.language_code = data.get("language_code")


def json_error(message: str, status: int):
    return jsonify({"ok": False, "error": message}), status


def require_telegram_auth(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        # Do not trust arbitrary X-Forwarded-For values for security decisions.
        ip = request.remote_addr or "unknown"

        try:
            if not db.check_and_record_rate_limit(
                f"ip:{ip}", limit=120, window_sec=60
            ):
                return json_error("Слишком много запросов", 429)

            init_data = request.headers.get(
                "X-Telegram-Init-Data", ""
            ).strip()

            if not init_data and request.is_json:
                body = request.get_json(silent=True)
                if isinstance(body, dict):
                    init_data = str(body.get("initData") or "").strip()

            parsed = verify_telegram_init_data(
                init_data,
                config.BOT_TOKEN,
                config.INIT_DATA_MAX_AGE_SEC,
            )

            if not parsed:
                return json_error(
                    "Недействительная или просроченная подпись Telegram WebApp",
                    401,
                )

            if not db.check_and_record_rate_limit(
                f"uid:{parsed['id']}", limit=60, window_sec=60
            ):
                return json_error("Превышен лимит запросов", 429)

            tg_user = WebAppUser(parsed)
            db.upsert_user(tg_user)

            g.tg_user = tg_user
            g.user_id = parsed["id"]
            g.nonce_hash = parsed["nonce_hash"]

            return fn(*args, **kwargs)

        except Exception:
            # Avoid exposing internal errors to the client.
            return json_error("Внутренняя ошибка сервера", 500)

    return wrapper


@api.get("/me")
@require_telegram_auth
def get_me():
    profile = payments.get_wallet_profile(g.user_id)
    topups = payments.list_topups(g.user_id, limit=10)
    orders = shop.list_wallet_orders(g.user_id, limit=10)

    return jsonify({
        "ok": True,
        "brand": config.BRAND,
        "user": {
            "id": profile["user_id"],
            "username": profile["username"],
            "first_name": profile["first_name"],
            "last_name": profile["last_name"],
            "is_admin": admin.is_admin(profile["user_id"]),
        },
        "wallet": {
            "balance_uzs": profile["balance_uzs"],
            "bonus_uzs": profile["bonus_uzs"],
            "currency": "UZS",
        },
        "topup_config": {
            "min_uzs": profile["topup_min_uzs"],
            "max_uzs": profile["topup_max_uzs"],
            "requisites": profile["requisites"],
        },
        "stats": {
            "orders_count": len(orders),
            "topups_count": len(topups),
        },
    })


@api.get("/products")
@require_telegram_auth
def get_products():
    category_slug = (
        request.args.get("category") or ""
    ).strip().lower() or None

    return jsonify({
        "ok": True,
        "categories": shop.list_categories(),
        "products": shop.list_products(category_slug=category_slug),
    })


@api.get("/history")
@require_telegram_auth
def get_history():
    return jsonify({
        "ok": True,
        "transactions": payments.list_wallet_transactions(
            g.user_id, limit=50
        ),
        "topups": payments.list_topups(g.user_id, limit=30),
        "orders": shop.list_wallet_orders(g.user_id, limit=30),
    })


@api.post("/orders")
@require_telegram_auth
def post_order():
    data = request.get_json(silent=True)

    if not isinstance(data, dict):
        return json_error("Ожидался JSON-объект", 400)

    if not db.check_and_record_rate_limit(
        f"ord_post:{g.user_id}", limit=15, window_sec=60
    ):
        return json_error("Слишком частые запросы на оформление заказа", 429)

    try:
        product_id = int(data.get("product_id"))
        qty = int(data.get("qty", 1))
    except (TypeError, ValueError):
        return json_error("Некорректные параметры product_id или qty", 400)

    recipient = str(data.get("recipient") or "").strip()
    idempotency_key = str(data.get("idempotency_key") or "").strip()

    if not idempotency_key:
        idempotency_key = (
            f"auto_{g.nonce_hash}_{product_id}_{qty}_{recipient}"
        )

    try:
        order = shop.create_wallet_order(
            user_id=g.user_id,
            product_id=product_id,
            qty=qty,
            recipient=recipient,
            idempotency_key=idempotency_key,
        )
    except shop.ShopError as exc:
        return json_error(str(exc), 400)

    if order.get("fulfillment_mode") == "manual":
        admin.notify_admins_manual_order(order, g.tg_user)

    wallet = payments.get_wallet_profile(g.user_id)

    return jsonify({
        "ok": True,
        "order": order,
        "wallet": {
            "balance_uzs": wallet["balance_uzs"],
            "bonus_uzs": wallet["bonus_uzs"],
            "currency": "UZS",
        },
    }), 201


@api.post("/topups")
@require_telegram_auth
def post_topup():
    data = request.get_json(silent=True)

    if not isinstance(data, dict):
        return json_error("Ожидался JSON-объект", 400)

    if not db.check_and_record_rate_limit(
        f"topup_post:{g.user_id}", limit=10, window_sec=60
    ):
        return json_error("Слишком частые запросы на пополнение", 429)

    try:
        amount_uzs = int(data.get("amount_uzs"))
    except (TypeError, ValueError):
        return json_error("Укажите корректную сумму пополнения в UZS", 400)

    try:
        topup = payments.create_topup_request(
            user_id=g.user_id,
            amount_uzs=amount_uzs,
        )
    except shop.ShopError as exc:
        return json_error(str(exc), 400)

    return jsonify({
        "ok": True,
        "topup": topup,
        "requisites": config.TOPUP_REQUISITES,
        "next_step": (
            "Переведите указанную сумму по реквизитам и отправьте "
            "фото или скриншот чека в чат с ботом."
        ),
    }), 201