# -*- coding: utf-8 -*-
"""
Gold Star Store — платежи и пополнения.

Поддерживает:
- ручные пополнения кошелька с подтверждением администратором;
- однократное зачисление пополнений;
- Telegram Stars (XTR) только для категории other;
- проверку суммы, валюты, пользователя и payload;
- защиту от повторной обработки Telegram-платежей;
- сохранение платежа, если товар закончился после оплаты;
- совместимость с существующей схемой database.py.

Существующие балансы, заказы и история платежей не удаляются.
"""

import logging
import secrets
import sqlite3
import uuid
from typing import Any

import config
import database as db
import shop


logger = logging.getLogger(__name__)

MAX_TOPUP_AMOUNT = int(getattr(config, "TOPUP_MAX_UZS", 10_000_000))
MIN_TOPUP_AMOUNT = int(getattr(config, "TOPUP_MIN_UZS", 1_000))

MAX_XTR_QUANTITY = 50


class PaymentError(Exception):
    """Ошибка платёжной операции."""


def _dict(row: Any) -> dict | None:
    if row is None:
        return None

    if isinstance(row, dict):
        return dict(row)

    try:
        return dict(row)
    except (TypeError, ValueError):
        return None


def _columns(table: str) -> set[str]:
    rows = db.fetchall(f"PRAGMA table_info({table})")
    return {str(row["name"]) for row in rows}


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _timestamp() -> str:
    return db.now()


def _update_payment(payment_id: int, values: dict[str, Any]) -> None:
    available = _columns("payments")
    values = {
        key: value
        for key, value in values.items()
        if key in available
    }

    if not values:
        return

    assignments = ", ".join(
        f"{column} = ?" for column in values
    )

    db.execute(
        f"UPDATE payments SET {assignments} WHERE id = ?",
        (*values.values(), int(payment_id)),
    )


def _insert_payment(values: dict[str, Any]) -> int:
    available = _columns("payments")
    values = {
        key: value
        for key, value in values.items()
        if key in available
    }

    if not values:
        raise PaymentError("Таблица payments не содержит нужных полей")

    columns = ", ".join(values)
    placeholders = ", ".join("?" for _ in values)

    cursor = db.execute(
        f"INSERT INTO payments ({columns}) VALUES ({placeholders})",
        tuple(values.values()),
    )

    return int(cursor.lastrowid)


def _ensure_xtr_orders_table() -> None:
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS xtr_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            payment_id INTEGER NOT NULL UNIQUE,
            user_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            product_title TEXT NOT NULL,
            qty INTEGER NOT NULL DEFAULT 1,
            recipient TEXT NOT NULL DEFAULT '',
            amount INTEGER NOT NULL,
            currency TEXT NOT NULL DEFAULT 'XTR',
            payload TEXT NOT NULL UNIQUE,
            telegram_charge_id TEXT UNIQUE,
            provider_charge_id TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            problem TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )

    db.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_xtr_orders_charge
        ON xtr_orders(telegram_charge_id)
        WHERE telegram_charge_id IS NOT NULL
          AND telegram_charge_id != ''
        """
    )

    db.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_xtr_orders_user
        ON xtr_orders(user_id, created_at)
        """
    )


def _user_exists(user_id: int) -> bool:
    return db.fetchone(
        "SELECT telegram_id FROM users WHERE telegram_id = ?",
        (int(user_id),),
    ) is not None


def get_wallet_profile(user_id: int) -> dict:
    wallet = _dict(db.get_wallet(int(user_id)))

    if wallet is None:
        raise PaymentError("Кошелёк пользователя не найден")

    return wallet


# ============================================================
# РУЧНЫЕ ПОПОЛНЕНИЯ
# ============================================================

def create_topup(
    user_id: int,
    amount_uzs: int,
    idempotency_key: str | None = None,
) -> dict:
    user_id = _safe_int(user_id)
    amount_uzs = _safe_int(amount_uzs)

    if user_id <= 0 or not _user_exists(user_id):
        raise PaymentError("Пользователь не найден")

    if amount_uzs < MIN_TOPUP_AMOUNT:
        raise PaymentError(
            f"Минимальная сумма пополнения: {MIN_TOPUP_AMOUNT}"
        )

    if amount_uzs > MAX_TOPUP_AMOUNT:
        raise PaymentError(
            f"Максимальная сумма пополнения: {MAX_TOPUP_AMOUNT}"
        )

    key = str(idempotency_key or "").strip()

    if not key:
        key = secrets.token_urlsafe(24)

    if len(key) > 200:
        raise PaymentError("Некорректный ключ операции")

    with db.transaction():
        existing = db.fetchone(
            """
            SELECT *
            FROM topups
            WHERE user_id = ?
              AND idempotency_key = ?
            LIMIT 1
            """,
            (user_id, key),
        )

        if existing:
            result = _dict(existing)

            if _safe_int(result.get("amount_uzs")) != amount_uzs:
                raise PaymentError(
                    "Ключ операции уже использован для другой суммы"
                )

            return result

        pending = db.fetchone(
            """
            SELECT *
            FROM topups
            WHERE user_id = ?
              AND status = 'pending'
            ORDER BY id DESC
            LIMIT 1
            """,
            (user_id,),
        )

        if pending:
            raise PaymentError(
                "У тебя уже есть незавершённая заявка на пополнение"
            )

        timestamp = _timestamp()

        cursor = db.execute(
            """
            INSERT INTO topups (
                user_id,
                amount_uzs,
                status,
                idempotency_key,
                created_at,
                updated_at
            )
            VALUES (?, ?, 'pending', ?, ?, ?)
            """,
            (
                user_id,
                amount_uzs,
                key,
                timestamp,
                timestamp,
            ),
        )

        row = db.fetchone(
            "SELECT * FROM topups WHERE id = ?",
            (int(cursor.lastrowid),),
        )

        return _dict(row)


def get_topup(topup_id: int) -> dict | None:
    return _dict(
        db.fetchone(
            "SELECT * FROM topups WHERE id = ?",
            (_safe_int(topup_id),),
        )
    )


def get_user_topups(
    user_id: int,
    limit: int = 20,
) -> list[dict]:
    limit = max(1, min(_safe_int(limit, 20), 100))

    rows = db.fetchall(
        """
        SELECT *
        FROM topups
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (_safe_int(user_id), limit),
    )

    return [_dict(row) for row in rows]


def get_pending_topups(limit: int = 50) -> list[dict]:
    limit = max(1, min(_safe_int(limit, 50), 100))

    rows = db.fetchall(
        """
        SELECT *
        FROM topups
        WHERE status = 'pending'
        ORDER BY id ASC
        LIMIT ?
        """,
        (limit,),
    )

    return [_dict(row) for row in rows]


def get_latest_pending_topup(user_id: int) -> dict | None:
    return _dict(
        db.fetchone(
            """
            SELECT *
            FROM topups
            WHERE user_id = ?
              AND status = 'pending'
            ORDER BY id DESC
            LIMIT 1
            """,
            (_safe_int(user_id),),
        )
    )


def attach_topup_receipt(
    topup_id: int,
    user_id: int,
    file_id: str,
    note: str = "",
) -> dict:
    topup_id = _safe_int(topup_id)
    user_id = _safe_int(user_id)
    file_id = str(file_id or "").strip()
    note = str(note or "").strip()[:1000]

    if topup_id <= 0 or user_id <= 0 or not file_id:
        raise PaymentError("Некорректные данные чека")

    with db.transaction():
        row = db.fetchone(
            """
            SELECT *
            FROM topups
            WHERE id = ?
              AND user_id = ?
            """,
            (topup_id, user_id),
        )

        if row is None:
            raise PaymentError("Заявка на пополнение не найдена")

        if row["status"] != "pending":
            raise PaymentError("Эта заявка уже обработана")

        db.execute(
            """
            UPDATE topups
            SET receipt_file_id = ?,
                receipt_note = ?,
                updated_at = ?
            WHERE id = ?
              AND user_id = ?
              AND status = 'pending'
            """,
            (
                file_id,
                note,
                _timestamp(),
                topup_id,
                user_id,
            ),
        )

        updated = db.fetchone(
            "SELECT * FROM topups WHERE id = ?",
            (topup_id,),
        )

        return _dict(updated)


def approve_topup_once(
    topup_id: int,
    admin_id: int,
    comment: str = "",
) -> dict:
    topup_id = _safe_int(topup_id)
    admin_id = _safe_int(admin_id)
    comment = str(comment or "").strip()[:1000]

    if topup_id <= 0 or admin_id <= 0:
        raise PaymentError("Некорректные данные подтверждения")

    with db.transaction():
        topup = db.fetchone(
            "SELECT * FROM topups WHERE id = ?",
            (topup_id,),
        )

        if topup is None:
            raise PaymentError("Заявка на пополнение не найдена")

        if topup["status"] != "pending":
            raise PaymentError("Заявка уже обработана")

        user_id = _safe_int(topup["user_id"])
        amount = _safe_int(topup["amount_uzs"])

        if amount <= 0:
            raise PaymentError("Некорректная сумма пополнения")

        wallet = db.fetchone(
            "SELECT * FROM wallets WHERE user_id = ?",
            (user_id,),
        )

        if wallet is None:
            raise PaymentError("Кошелёк пользователя не найден")

        balance_before = _safe_int(wallet["balance_uzs"])
        bonus_before = _safe_int(wallet["bonus_uzs"])
        balance_after = balance_before + amount
        timestamp = _timestamp()

        cursor = db.execute(
            """
            UPDATE topups
            SET status = 'approved',
                admin_id = ?,
                admin_comment = ?,
                confirmed_at = ?,
                updated_at = ?
            WHERE id = ?
              AND status = 'pending'
            """,
            (
                admin_id,
                comment,
                timestamp,
                timestamp,
                topup_id,
            ),
        )

        if cursor.rowcount != 1:
            raise PaymentError("Заявка уже была обработана")

        db.execute(
            """
            UPDATE wallets
            SET balance_uzs = ?,
                updated_at = ?
            WHERE user_id = ?
            """,
            (balance_after, timestamp, user_id),
        )

        db.execute(
            """
            INSERT INTO wallet_transactions (
                user_id,
                transaction_type,
                amount_uzs,
                balance_delta,
                bonus_delta,
                balance_after,
                bonus_after,
                reference_type,
                reference_id,
                description,
                created_at
            )
            VALUES (?, 'topup', ?, ?, 0, ?, ?, 'topup', ?, ?, ?)
            """,
            (
                user_id,
                amount,
                amount,
                balance_after,
                bonus_before,
                str(topup_id),
                f"Подтверждено администратором #{admin_id}",
                timestamp,
            ),
        )

        db.record_admin_action(
            admin_id,
            "approve_topup",
            "topup",
            str(topup_id),
            {
                "user_id": user_id,
                "amount_uzs": amount,
                "comment": comment,
            },
        )

        result = _dict(
            db.fetchone(
                "SELECT * FROM topups WHERE id = ?",
                (topup_id,),
            )
        )

        result["user_id"] = user_id
        result["amount_uzs"] = amount
        return result


def reject_topup_once(
    topup_id: int,
    admin_id: int,
    comment: str = "",
) -> dict:
    topup_id = _safe_int(topup_id)
    admin_id = _safe_int(admin_id)
    comment = str(comment or "").strip()[:1000]

    if topup_id <= 0 or admin_id <= 0:
        raise PaymentError("Некорректные данные отклонения")

    with db.transaction():
        topup = db.fetchone(
            "SELECT * FROM topups WHERE id = ?",
            (topup_id,),
        )

        if topup is None:
            raise PaymentError("Заявка на пополнение не найдена")

        if topup["status"] != "pending":
            raise PaymentError("Заявка уже обработана")

        user_id = _safe_int(topup["user_id"])
        amount = _safe_int(topup["amount_uzs"])
        timestamp = _timestamp()

        cursor = db.execute(
            """
            UPDATE topups
            SET status = 'rejected',
                admin_id = ?,
                admin_comment = ?,
                updated_at = ?
            WHERE id = ?
              AND status = 'pending'
            """,
            (
                admin_id,
                comment,
                timestamp,
                topup_id,
            ),
        )

        if cursor.rowcount != 1:
            raise PaymentError("Заявка уже была обработана")

        db.record_admin_action(
            admin_id,
            "reject_topup",
            "topup",
            str(topup_id),
            {
                "user_id": user_id,
                "amount_uzs": amount,
                "comment": comment,
            },
        )

        result = _dict(
            db.fetchone(
                "SELECT * FROM topups WHERE id = ?",
                (topup_id,),
            )
        )

        result["user_id"] = user_id
        result["amount_uzs"] = amount
        return result


# Совместимость с admin.py.
approve_topup = approve_topup_once
reject_topup = reject_topup_once


# ============================================================
# TELEGRAM STARS
# ============================================================

def _get_xtr_product(product_id: int) -> dict:
    product_id = _safe_int(product_id)

    if product_id <= 0:
        raise PaymentError("Некорректный товар")

    product = _dict(shop.get_product(product_id))

    if not product:
        raise PaymentError("Товар не найден")

    if not _safe_int(product.get("active")):
        raise PaymentError("Товар временно недоступен")

    if str(product.get("fulfillment_mode") or "").lower() != "xtr":
        raise PaymentError("Для этого товара оплата Stars не разрешена")

    category_id = product.get("category_id")

    if category_id is None:
        raise PaymentError("Товар не относится к категории «Другие товары»")

    category = db.fetchone(
        """
        SELECT slug, active
        FROM categories
        WHERE id = ?
        """,
        (_safe_int(category_id),),
    )

    if (
        category is None
        or str(category["slug"]).lower() != "other"
        or not _safe_int(category["active"])
    ):
        raise PaymentError(
            "Telegram Stars доступны только для категории «Другие товары»"
        )

    price = _safe_int(product.get("price_xtr"))

    if price <= 0:
        raise PaymentError("Для товара не задана цена в Stars")

    return product


def create_xtr_invoice(
    user_id: int,
    product_id: int,
    quantity: int = 1,
    recipient: str = "",
) -> dict:
    """
    Создаёт запись ожидающего платежа и данные для bot.send_invoice().

    Возвращает:
    title, description, invoice_payload, provider_token,
    currency, prices.
    """

    user_id = _safe_int(user_id)
    product_id = _safe_int(product_id)
    quantity = _safe_int(quantity, 1)
    recipient = str(recipient or "").strip()[:200]

    if user_id <= 0 or not _user_exists(user_id):
        raise PaymentError("Пользователь не найден")

    if quantity < 1 or quantity > MAX_XTR_QUANTITY:
        raise PaymentError("Некорректное количество товара")

    product = _get_xtr_product(product_id)

    stock = product.get("stock")

    if stock is not None and _safe_int(stock) < quantity:
        raise PaymentError("Недостаточно товара на складе")

    price = _safe_int(product.get("price_xtr"))
    total = price * quantity

    if total <= 0:
        raise PaymentError("Некорректная сумма платежа")

    payload = f"gss_xtr_{user_id}_{uuid.uuid4().hex}"

    _ensure_xtr_orders_table()

    with db.transaction():
        payment_id = _insert_payment(
            {
                "user_id": user_id,
                "order_id": None,
                "payment_type": "product",
                "provider": "telegram_stars",
                "amount": total,
                "currency": "XTR",
                "status": "pending",
                "provider_payment_id": None,
                "telegram_charge_id": None,
                "payload": payload,
                "created_at": _timestamp(),
                "updated_at": _timestamp(),
            }
        )

    try:
        from telebot.types import LabeledPrice

        prices = [
            LabeledPrice(
                label=str(product.get("title") or "Товар"),
                amount=total,
            )
        ]
    except ImportError as exc:
        raise PaymentError(
            "Не удалось загрузить типы Telegram Bot API"
        ) from exc

    return {
        "title": str(product.get("title") or "Покупка")[:32],
        "description": str(
            product.get("description") or "Покупка цифрового товара"
        )[:255],
        "invoice_payload": payload,
        "provider_token": "",
        "currency": "XTR",
        "prices": prices,
        "payment_id": payment_id,
    }


def validate_xtr_pre_checkout(query: Any) -> tuple[bool, str]:
    """
    Проверяет Telegram PreCheckoutQuery.
    Ничего не списывает и не отмечает как оплаченное.
    """

    try:
        payload = str(query.invoice_payload or "")
        user_id = _safe_int(query.from_user.id)
        currency = str(query.currency or "")
        amount = _safe_int(query.total_amount)

        if not payload or user_id <= 0:
            return False, "Некорректные данные платежа"

        if currency != "XTR":
            return False, "Поддерживается только оплата Telegram Stars"

        payment = db.fetchone(
            """
            SELECT *
            FROM payments
            WHERE payload = ?
              AND user_id = ?
              AND provider = 'telegram_stars'
              AND payment_type = 'product'
              AND currency = 'XTR'
              AND status = 'pending'
            LIMIT 1
            """,
            (payload, user_id),
        )

        if payment is None:
            return False, "Платёж не найден или уже обработан"

        if _safe_int(payment["amount"]) != amount:
            return False, "Сумма платежа не совпадает"

        xtr_order = db.fetchone(
            """
            SELECT id
            FROM xtr_orders
            WHERE payment_id = ?
            """,
            (_safe_int(payment["id"]),),
        )

        if xtr_order is not None:
            return False, "Этот платёж уже использован"

        return True, ""

    except Exception:
        logger.exception("Ошибка проверки Telegram PreCheckout")
        return False, "Не удалось проверить платёж"


def _record_xtr_success(
    user_id: int,
    currency: str,
    total_amount: int,
    invoice_payload: str,
    telegram_charge_id: str,
    provider_charge_id: str = "",
) -> dict:
    user_id = _safe_int(user_id)
    total_amount = _safe_int(total_amount)
    currency = str(currency or "")
    invoice_payload = str(invoice_payload or "")
    telegram_charge_id = str(telegram_charge_id or "")
    provider_charge_id = str(provider_charge_id or "")

    if (
        user_id <= 0
        or currency != "XTR"
        or total_amount <= 0
        or not invoice_payload
        or not telegram_charge_id
    ):
        raise PaymentError("Некорректные данные успешного платежа")

    _ensure_xtr_orders_table()

    with db.transaction():
        duplicate = db.fetchone(
            """
            SELECT id
            FROM payments
            WHERE telegram_charge_id = ?
            LIMIT 1
            """,
            (telegram_charge_id,),
        )

        if duplicate:
            existing_order = db.fetchone(
                """
                SELECT *
                FROM xtr_orders
                WHERE telegram_charge_id = ?
                LIMIT 1
                """,
                (telegram_charge_id,),
            )

            return {
                "ok": True,
                "duplicate": True,
                "order": _dict(existing_order),
            }

        payment = db.fetchone(
            """
            SELECT *
            FROM payments
            WHERE payload = ?
              AND user_id = ?
              AND provider = 'telegram_stars'
              AND payment_type = 'product'
              AND currency = 'XTR'
            LIMIT 1
            """,
            (invoice_payload, user_id),
        )

        if payment is None:
            raise PaymentError("Запись платежа не найдена")

        if _safe_int(payment["amount"]) != total_amount:
            raise PaymentError("Сумма успешного платежа не совпадает")

        if payment["status"] == "paid":
            existing_order = db.fetchone(
                """
                SELECT *
                FROM xtr_orders
                WHERE payment_id = ?
                """,
                (_safe_int(payment["id"]),),
            )

            return {
                "ok": True,
                "duplicate": True,
                "order": _dict(existing_order),
            }

        if payment["status"] != "pending":
            raise PaymentError("Платёж находится в неподходящем статусе")

        # Товар может закончиться после PreCheckout.
        # Подтверждённый Telegram платёж всё равно записываем.
        # В таком случае заказ получает статус paid_unfulfilled.
        order_status = "paid_unfulfilled"
        problem = "Требуется проверка наличия товара после оплаты"

        product = _dict(shop.get_product(
            _safe_int(
                db.fetchone(
                    "SELECT id FROM products WHERE id = (SELECT id FROM products WHERE id = ?)",
                    (_safe_int(payment["order_id"]),),
                )
            )
        )) if False else None

        # Payload связан с данными товара через запись ожидания.
        # Получаем товар по данным, сохранённым в invoice payload.
        # ID товара передаётся отдельно в таблицу ожидания ниже.
        pending = db.fetchone(
            """
            SELECT *
            FROM xtr_orders
            WHERE payment_id = ?
            """,
            (_safe_int(payment["id"]),),
        )

        if pending is None:
            # Поддержка записей, созданных этой версией модуля:
            # товар и количество хранятся в xtr_invoice_items.
            pending_item = db.fetchone(
                """
                SELECT *
                FROM xtr_invoice_items
                WHERE payment_id = ?
                """,
                (_safe_int(payment["id"]),),
            )
        else:
            pending_item = pending

        if pending_item is None:
            raise PaymentError(
                "Не найдены данные товара для оплаченного счёта"
            )

        product_id = _safe_int(pending_item["product_id"])
        quantity = _safe_int(pending_item["qty"], 1)
        recipient = str(pending_item["recipient"] or "")

        product_row = db.fetchone(
            "SELECT * FROM products WHERE id = ?",
            (product_id,),
        )

        if product_row is not None:
            product_data = _dict(product_row)
            active = _safe_int(product_data.get("active"))
            current_stock = product_data.get("stock")

            if active and (
                current_stock is None
                or _safe_int(current_stock) >= quantity
            ):
                if current_stock is not None:
                    cursor = db.execute(
                        """
                        UPDATE products
                        SET stock = stock - ?,
                            updated_at = ?
                        WHERE id = ?
                          AND active = 1
                          AND stock >= ?
                        """,
                        (
                            quantity,
                            _timestamp(),
                            product_id,
                            quantity,
                        ),
                    )

                    if cursor.rowcount == 1:
                        order_status = "paid"
                        problem = ""
                else:
                    order_status = "paid"
                    problem = ""
            else:
                problem = "Товар недоступен или закончился после оплаты"
        else:
            problem = "Товар не найден после успешной оплаты"

        timestamp = _timestamp()

        _update_payment(
            _safe_int(payment["id"]),
            {
                "status": "paid",
                "provider_payment_id": provider_charge_id or None,
                "telegram_charge_id": telegram_charge_id,
                "updated_at": timestamp,
            },
        )

        cursor = db.execute(
            """
            INSERT INTO xtr_orders (
                payment_id,
                user_id,
                product_id,
                product_title,
                qty,
                recipient,
                amount,
                currency,
                payload,
                telegram_charge_id,
                provider_charge_id,
                status,
                problem,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 'XTR', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _safe_int(payment["id"]),
                user_id,
                product_id,
                str(pending_item["product_title"]),
                quantity,
                recipient,
                total_amount,
                invoice_payload,
                telegram_charge_id,
                provider_charge_id,
                order_status,
                problem,
                timestamp,
                timestamp,
            ),
        )

        order = _dict(
            db.fetchone(
                "SELECT * FROM xtr_orders WHERE id = ?",
                (int(cursor.lastrowid),),
            )
        )

        if order_status == "paid_unfulfilled":
            logger.error(
                "Telegram Stars payment requires manual review: "
                "payment_id=%s user_id=%s product_id=%s charge=%s",
                payment["id"],
                user_id,
                product_id,
                telegram_charge_id,
            )

        return {
            "ok": True,
            "duplicate": False,
            "fulfilled": order_status == "paid",
            "order": order,
        }


def process_xtr_success(
    user_id: int,
    currency: str,
    total_amount: int,
    invoice_payload: str,
    telegram_charge_id: str,
    provider_charge_id: str = "",
) -> dict:
    return _record_xtr_success(
        user_id,
        currency,
        total_amount,
        invoice_payload,
        telegram_charge_id,
        provider_charge_id,
    )


# Старый интерфейс bot.py.
def handle_pre_checkout(query: Any) -> bool:
    from utils import bot

    allowed, message = validate_xtr_pre_checkout(query)

    try:
        bot.answer_pre_checkout_query(
            query.id,
            ok=allowed,
            error_message=None if allowed else message,
        )
    except Exception:
        logger.exception("Не удалось ответить на PreCheckoutQuery")
        return False

    return allowed


def record_payment(
    user_id: int,
    currency: str,
    total_amount: int,
    invoice_payload: str,
    telegram_charge_id: str,
    provider_charge_id: str = "",
) -> dict:
    return _record_xtr_success(
        user_id,
        currency,
        total_amount,
        invoice_payload,
        telegram_charge_id,
        provider_charge_id,
    )


def get_xtr_order(order_id: int) -> dict | None:
    _ensure_xtr_orders_table()

    return _dict(
        db.fetchone(
            "SELECT * FROM xtr_orders WHERE id = ?",
            (_safe_int(order_id),),
        )
    )


def get_user_xtr_orders(
    user_id: int,
    limit: int = 20,
) -> list[dict]:
    _ensure_xtr_orders_table()
    limit = max(1, min(_safe_int(limit, 20), 100))

    rows = db.fetchall(
        """
        SELECT *
        FROM xtr_orders
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (_safe_int(user_id), limit),
    )

    return [_dict(row) for row in rows]


# ============================================================
# СОВМЕСТИМОСТЬ И ИНИЦИАЛИЗАЦИЯ
# ============================================================

def init_payments() -> None:
    """
    Вызывать после database.init_db().
    Создаёт только вспомогательные таблицы Telegram Stars.
    """
    _ensure_xtr_orders_table()

    db.execute(
        """
        CREATE TABLE IF NOT EXISTS xtr_invoice_items (
            payment_id INTEGER PRIMARY KEY,
            product_id INTEGER NOT NULL,
            product_title TEXT NOT NULL,
            qty INTEGER NOT NULL DEFAULT 1,
            recipient TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )
        """
    )


# Совместимость с предыдущими именами.
create_payment_invoice = create_xtr_invoice
validate_pre_checkout = validate_xtr_pre_checkout
process_successful_payment = process_xtr_success