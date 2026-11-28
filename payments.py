# -*- coding: utf-8 -*-
"""
Gold Star Store — платежи и пополнения.

Поддерживает:
- ручные пополнения кошелька;
- подтверждение пополнений администратором;
- однократное зачисление средств;
- Telegram Stars (XTR) только для категории other;
- проверку пользователя, суммы, валюты и payload;
- защиту от повторной обработки платежей;
- фиксацию оплаченных, но необработанных заказов.

Не удаляет существующие балансы, платежи и историю.
"""

import logging
import secrets
import uuid
from typing import Any

import config
import database as db
import shop


logger = logging.getLogger(__name__)

MIN_TOPUP_AMOUNT = int(getattr(config, "TOPUP_MIN_UZS", 1000))
MAX_TOPUP_AMOUNT = int(getattr(config, "TOPUP_MAX_UZS", 10_000_000))
MAX_XTR_QUANTITY = 50


class PaymentError(Exception):
    pass


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _dict(row: Any) -> dict | None:
    if row is None:
        return None
    if isinstance(row, dict):
        return dict(row)
    try:
        return dict(row)
    except (TypeError, ValueError):
        return None


def _now() -> str:
    return db.now()


def _columns(table: str) -> set[str]:
    return {
        str(row["name"])
        for row in db.fetchall(f"PRAGMA table_info({table})")
    }


def _user_exists(user_id: int) -> bool:
    return db.fetchone(
        "SELECT telegram_id FROM users WHERE telegram_id = ?",
        (user_id,),
    ) is not None


def _update_payment(payment_id: int, values: dict) -> None:
    available = _columns("payments")
    values = {
        key: value
        for key, value in values.items()
        if key in available
    }

    if not values:
        return

    assignments = ", ".join(
        f"{key} = ?" for key in values
    )

    db.execute(
        f"""
        UPDATE payments
        SET {assignments}
        WHERE id = ?
        """,
        (*values.values(), payment_id),
    )


def _insert_payment(values: dict) -> int:
    available = _columns("payments")
    values = {
        key: value
        for key, value in values.items()
        if key in available
    }

    required = {
        "user_id",
        "payment_type",
        "provider",
        "amount",
        "currency",
        "status",
        "payload",
    }

    if not required.issubset(values):
        raise PaymentError(
            "Структура таблицы payments не соответствует требованиям"
        )

    columns = ", ".join(values)
    placeholders = ", ".join("?" for _ in values)

    cursor = db.execute(
        f"""
        INSERT INTO payments ({columns})
        VALUES ({placeholders})
        """,
        tuple(values.values()),
    )

    return int(cursor.lastrowid)


# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ ТАБЛИЦЫ STARS
# ============================================================

def init_payments() -> None:
    """
    Вызывать после db.init_db().
    Создаёт вспомогательные таблицы, не затрагивая старые данные.
    """

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
        CREATE INDEX IF NOT EXISTS idx_xtr_orders_user
        ON xtr_orders(user_id, created_at)
        """
    )


def _ensure_xtr_tables() -> None:
    init_payments()


# ============================================================
# РУЧНЫЕ ПОПОЛНЕНИЯ
# ============================================================

def create_topup(
    user_id: int,
    amount_uzs: int,
    idempotency_key: str | None = None,
) -> dict:
    user_id = _int(user_id)
    amount_uzs = _int(amount_uzs)

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

            if _int(result.get("amount_uzs")) != amount_uzs:
                raise PaymentError(
                    "Ключ операции уже использован для другой суммы"
                )

            return result

        pending = db.fetchone(
            """
            SELECT id
            FROM topups
            WHERE user_id = ?
              AND status = 'pending'
            LIMIT 1
            """,
            (user_id,),
        )

        if pending:
            raise PaymentError(
                "У тебя уже есть незавершённая заявка на пополнение"
            )

        timestamp = _now()

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

        return _dict(
            db.fetchone(
                "SELECT * FROM topups WHERE id = ?",
                (int(cursor.lastrowid),),
            )
        )


def get_topup(topup_id: int) -> dict | None:
    return _dict(
        db.fetchone(
            "SELECT * FROM topups WHERE id = ?",
            (_int(topup_id),),
        )
    )


def get_user_topups(
    user_id: int,
    limit: int = 20,
) -> list[dict]:
    limit = max(1, min(_int(limit, 20), 100))

    rows = db.fetchall(
        """
        SELECT *
        FROM topups
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (_int(user_id), limit),
    )

    return [_dict(row) for row in rows]


def get_pending_topups(limit: int = 50) -> list[dict]:
    limit = max(1, min(_int(limit, 50), 100))

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
            (_int(user_id),),
        )
    )


def attach_topup_receipt(
    topup_id: int,
    user_id: int,
    file_id: str,
    note: str = "",
) -> dict:
    topup_id = _int(topup_id)
    user_id = _int(user_id)
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
            raise PaymentError("Заявка уже обработана")

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
                _now(),
                topup_id,
                user_id,
            ),
        )

        return _dict(
            db.fetchone(
                "SELECT * FROM topups WHERE id = ?",
                (topup_id,),
            )
        )


def approve_topup_once(
    topup_id: int,
    admin_id: int,
    comment: str = "",
) -> dict:
    topup_id = _int(topup_id)
    admin_id = _int(admin_id)
    comment = str(comment or "").strip()[:1000]

    if topup_id <= 0 or admin_id <= 0:
        raise PaymentError("Некорректные данные подтверждения")

    with db.transaction():
        topup = db.fetchone(
            "SELECT * FROM topups WHERE id = ?",
            (topup_id,),
        )

        if topup is None:
            raise PaymentError("Заявка не найдена")

        if topup["status"] != "pending":
            raise PaymentError("Заявка уже обработана")

        user_id = _int(topup["user_id"])
        amount = _int(topup["amount_uzs"])

        if amount <= 0:
            raise PaymentError("Некорректная сумма")

        wallet = db.fetchone(
            "SELECT * FROM wallets WHERE user_id = ?",
            (user_id,),
        )

        if wallet is None:
            raise PaymentError("Кошелёк пользователя не найден")

        balance_before = _int(wallet["balance_uzs"])
        bonus_before = _int(wallet["bonus_uzs"])
        balance_after = balance_before + amount
        timestamp = _now()

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
            raise PaymentError("Заявка уже обработана")

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
            VALUES (
                ?, 'topup', ?, ?, 0, ?, ?,
                'topup', ?, ?, ?
            )
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
    topup_id = _int(topup_id)
    admin_id = _int(admin_id)
    comment = str(comment or "").strip()[:1000]

    if topup_id <= 0 or admin_id <= 0:
        raise PaymentError("Некорректные данные отклонения")

    with db.transaction():
        topup = db.fetchone(
            "SELECT * FROM topups WHERE id = ?",
            (topup_id,),
        )

        if topup is None:
            raise PaymentError("Заявка не найдена")

        if topup["status"] != "pending":
            raise PaymentError("Заявка уже обработана")

        user_id = _int(topup["user_id"])
        amount = _int(topup["amount_uzs"])
        timestamp = _now()

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
            raise PaymentError("Заявка уже обработана")

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
    product_id = _int(product_id)

    if product_id <= 0:
        raise PaymentError("Некорректный товар")

    product = _dict(shop.get_product(product_id))

    if not product:
        raise PaymentError("Товар не найден")

    if not _int(product.get("active")):
        raise PaymentError("Товар неактивен")

    if str(product.get("fulfillment_mode") or "").lower() != "xtr":
        raise PaymentError("Оплата Stars для этого товара недоступна")

    category_id = product.get("category_id")

    if category_id is None:
        raise PaymentError("Товар не относится к категории other")

    category = db.fetchone(
        """
        SELECT slug, active
        FROM categories
        WHERE id = ?
        """,
        (_int(category_id),),
    )

    if (
        category is None
        or str(category["slug"]).lower() != "other"
        or not _int(category["active"])
    ):
        raise PaymentError(
            "Stars разрешены только для активной категории other"
        )

    if _int(product.get("price_xtr")) <= 0:
        raise PaymentError("Не задана цена товара в Stars")

    return product


def create_xtr_invoice(
    user_id: int,
    product_id: int,
    quantity: int = 1,
    recipient: str = "",
) -> dict:
    user_id = _int(user_id)
    product_id = _int(product_id)
    quantity = _int(quantity, 1)
    recipient = str(recipient or "").strip()[:200]

    if user_id <= 0 or not _user_exists(user_id):
        raise PaymentError("Пользователь не найден")

    if quantity < 1 or quantity > MAX_XTR_QUANTITY:
        raise PaymentError("Некорректное количество товара")

    product = _get_xtr_product(product_id)

    stock = product.get("stock")
    if stock is not None and _int(stock) < quantity:
        raise PaymentError("Недостаточно товара на складе")

    total = _int(product.get("price_xtr")) * quantity

    if total <= 0:
        raise PaymentError("Некорректная сумма")

    _ensure_xtr_tables()

    payload = f"gss_xtr_{user_id}_{uuid.uuid4().hex}"
    timestamp = _now()

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
                "created_at": timestamp,
                "updated_at": timestamp,
            }
        )

        db.execute(
            """
            INSERT INTO xtr_invoice_items (
                payment_id,
                product_id,
                product_title,
                qty,
                recipient,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                payment_id,
                product_id,
                str(product.get("title") or "Товар"),
                quantity,
                recipient,
                timestamp,
            ),
        )

    from telebot.types import LabeledPrice

    return {
        "title": str(product.get("title") or "Покупка")[:32],
        "description": str(
            product.get("description") or "Покупка цифрового товара"
        )[:255],
        "invoice_payload": payload,
        "provider_token": "",
        "currency": "XTR",
        "prices": [
            LabeledPrice(
                label=str(product.get("title") or "Товар"),
                amount=total,
            )
        ],
        "payment_id": payment_id,
    }


def validate_xtr_pre_checkout(query: Any) -> tuple[bool, str]:
    try:
        _ensure_xtr_tables()

        payload = str(query.invoice_payload or "")
        user_id = _int(query.from_user.id)
        currency = str(query.currency or "")
        amount = _int(query.total_amount)

        if not payload or user_id <= 0:
            return False, "Некорректные данные платежа"

        if currency != "XTR":
            return False, "Поддерживается только Telegram Stars"

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

        if _int(payment["amount"]) != amount:
            return False, "Сумма платежа не совпадает"

        item = db.fetchone(
            """
            SELECT *
            FROM xtr_invoice_items
            WHERE payment_id = ?
            """,
            (_int(payment["id"]),),
        )

        if item is None:
            return False, "Данные товара не найдены"

        try:
            product = _get_xtr_product(_int(item["product_id"]))
        except PaymentError:
            return False, "Товар больше недоступен"

        stock = product.get("stock")
        if stock is not None and _int(stock) < _int(item["qty"]):
            return False, "Недостаточно товара на складе"

        return True, ""

    except Exception:
        logger.exception("Ошибка проверки PreCheckout")
        return False, "Не удалось проверить платёж"


def process_xtr_success(
    user_id: int,
    currency: str,
    total_amount: int,
    invoice_payload: str,
    telegram_charge_id: str,
    provider_charge_id: str = "",
) -> dict:
    """
    Обрабатывает успешный платёж Telegram.

    Повторный update не создаёт повторный заказ.
    Если товар закончился после оплаты, платёж сохраняется,
    а заказ получает статус paid_unfulfilled.
    """

    user_id = _int(user_id)
    total_amount = _int(total_amount)
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

    _ensure_xtr_tables()

    with db.transaction():
        duplicate = db.fetchone(
            """
            SELECT *
            FROM xtr_orders
            WHERE telegram_charge_id = ?
            """,
            (telegram_charge_id,),
        )

        if duplicate:
            return {
                "ok": True,
                "duplicate": True,
                "fulfilled": duplicate["status"] == "paid",
                "order": _dict(duplicate),
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

        if _int(payment["amount"]) != total_amount:
            raise PaymentError("Сумма успешного платежа не совпадает")

        if payment["status"] != "pending":
            raise PaymentError("Платёж уже обработан")

        item = db.fetchone(
            """
            SELECT *
            FROM xtr_invoice_items
            WHERE payment_id = ?
            """,
            (_int(payment["id"]),),
        )

        if item is None:
            raise PaymentError("Не найдены данные товара")

        product_id = _int(item["product_id"])
        quantity = _int(item["qty"], 1)
        product_title = str(item["product_title"])
        recipient = str(item["recipient"] or "")

        timestamp = _now()
        order_status = "paid_unfulfilled"
        problem = "Требуется ручная проверка наличия товара"

        product_row = db.fetchone(
            """
            SELECT p.*, c.slug AS category_slug,
                   c.active AS category_active
            FROM products p
            LEFT JOIN categories c ON c.id = p.category_id
            WHERE p.id = ?
            """,
            (product_id,),
        )

        if product_row is not None:
            product = _dict(product_row)

            product_is_valid = (
                _int(product.get("active")) == 1
                and str(product.get("fulfillment_mode") or "").lower() == "xtr"
                and str(product.get("category_slug") or "").lower() == "other"
                and _int(product.get("category_active")) == 1
            )

            stock = product.get("stock")

            if product_is_valid:
                if stock is None:
                    order_status = "paid"
                    problem = ""
                else:
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
                            timestamp,
                            product_id,
                            quantity,
                        ),
                    )

                    if cursor.rowcount == 1:
                        order_status = "paid"
                        problem = ""
                    else:
                        problem = "Недостаточно товара после подтверждения оплаты"
            else:
                problem = "Товар или категория больше неактивны"
        else:
            problem = "Товар не найден после успешной оплаты"

        # Сначала фиксируем факт успешной оплаты в той же транзакции,
        # что и запись заказа. При ошибке всё откатывается.
        _update_payment(
            _int(payment["id"]),
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
                _int(payment["id"]),
                user_id,
                product_id,
                product_title,
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
                "Paid Telegram Stars order needs manual review: "
                "payment_id=%s user_id=%s product_id=%s charge_id=%s",
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


# ============================================================
# ПОЛУЧЕНИЕ STARS-ЗАКАЗОВ
# ============================================================

def get_xtr_order(order_id: int) -> dict | None:
    _ensure_xtr_tables()

    return _dict(
        db.fetchone(
            "SELECT * FROM xtr_orders WHERE id = ?",
            (_int(order_id),),
        )
    )


def get_user_xtr_orders(
    user_id: int,
    limit: int = 20,
) -> list[dict]:
    _ensure_xtr_tables()
    limit = max(1, min(_int(limit, 20), 100))

    rows = db.fetchall(
        """
        SELECT *
        FROM xtr_orders
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (_int(user_id), limit),
    )

    return [_dict(row) for row in rows]


# ============================================================
# СОВМЕСТИМОСТЬ
# ============================================================

approve_topup = approve_topup_once
reject_topup = reject_topup_once

create_payment_invoice = create_xtr_invoice
validate_pre_checkout = validate_xtr_pre_checkout
process_successful_payment = process_xtr_success