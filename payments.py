import re
import time
from typing import Any

from telebot import types

import config
import database as db
from shop import ShopError
from utils import bot, h, notify, uzs


MAX_HISTORY_LIMIT = 100
MAX_RECEIPT_CAPTION = 500
MAX_ADMIN_COMMENT = 300


def _limit(value: int, default: int = 50) -> int:
    try:
        return max(1, min(int(value), MAX_HISTORY_LIMIT))
    except (TypeError, ValueError):
        return default


def _parse_invoice_payload(
    payload: str,
) -> tuple[int, int] | None:
    if not isinstance(payload, str):
        return None

    match = re.fullmatch(
        r"order:([1-9][0-9]*):([1-9][0-9]*)",
        payload,
    )

    if not match:
        return None

    try:
        return int(match.group(1)), int(match.group(2))
    except ValueError:
        return None


def get_wallet_profile(user_id: int) -> dict[str, Any]:
    uid = int(user_id)

    if uid <= 0:
        raise ShopError("Некорректный идентификатор пользователя")

    with db.tx() as conn:
        user = conn.execute(
            "SELECT * FROM users WHERE telegram_id = ?",
            (uid,),
        ).fetchone()

        wallet = db.ensure_wallet(conn, uid)

    return {
        "user_id": uid,
        "username": user["username"] if user else None,
        "first_name": user["first_name"] if user else None,
        "last_name": user["last_name"] if user else None,
        "balance_uzs": int(wallet["balance_uzs"]),
        "bonus_uzs": int(wallet["bonus_uzs"]),
        "topup_min_uzs": config.TOPUP_MIN_UZS,
        "topup_max_uzs": config.TOPUP_MAX_UZS,
        "requisites": config.TOPUP_REQUISITES,
    }


def list_wallet_transactions(
    user_id: int,
    limit: int = 50,
) -> list[dict[str, Any]]:
    return db.q_all(
        """
        SELECT *
        FROM wallet_transactions
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (int(user_id), _limit(limit)),
    )


def create_topup_request(
    user_id: int,
    amount_uzs: int,
) -> dict[str, Any]:
    uid = int(user_id)

    if uid <= 0:
        raise ShopError("Некорректный пользователь")

    try:
        amount = int(amount_uzs)
    except (TypeError, ValueError) as exc:
        raise ShopError(
            "Введите корректную целую сумму в UZS"
        ) from exc

    if amount < config.TOPUP_MIN_UZS:
        raise ShopError(
            f"Минимальная сумма пополнения: "
            f"{uzs(config.TOPUP_MIN_UZS)}"
        )

    if amount > config.TOPUP_MAX_UZS:
        raise ShopError(
            f"Максимальная сумма пополнения: "
            f"{uzs(config.TOPUP_MAX_UZS)}"
        )

    requisites = (config.TOPUP_REQUISITES or "").strip()

    if not requisites:
        raise ShopError(
            "Пополнение временно недоступно. "
            "Администратор не настроил реквизиты."
        )

    now = int(time.time())

    with db.tx() as conn:
        db.ensure_wallet(conn, uid)

        pending_count = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM topups
            WHERE user_id = ?
              AND status = 'pending'
            """,
            (uid,),
        ).fetchone()["c"]

        if int(pending_count) >= 5:
            raise ShopError(
                "У вас уже есть активные заявки на пополнение. "
                "Дождитесь решения администратора."
            )

        cursor = conn.execute(
            """
            INSERT INTO topups (
                user_id,
                amount_uzs,
                requisites,
                status,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, 'pending', ?, ?)
            """,
            (
                uid,
                amount,
                requisites,
                now,
                now,
            ),
        )

        topup_id = int(cursor.lastrowid)

        row = conn.execute(
            "SELECT * FROM topups WHERE id = ?",
            (topup_id,),
        ).fetchone()

        topup = dict(row)

    notify(
        uid,
        f"💳 <b>Заявка на пополнение #{topup_id} создана</b>\n"
        f"Сумма: <b>{uzs(amount)}</b>\n\n"
        f"Реквизиты для перевода:\n"
        f"<code>{h(requisites)}</code>\n\n"
        f"После перевода отправьте фото или скриншот чека "
        f"в этот чат.\n\n"
        f"<b>Важно:</b> баланс будет пополнен только после "
        f"проверки поступления денег администратором.",
    )

    return topup


def list_topups(
    user_id: int,
    limit: int = 30,
) -> list[dict[str, Any]]:
    return db.q_all(
        """
        SELECT *
        FROM topups
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (int(user_id), _limit(limit, 30)),
    )


def get_topup(
    topup_id: int,
) -> dict[str, Any] | None:
    return db.q_one(
        "SELECT * FROM topups WHERE id = ?",
        (int(topup_id),),
    )


def get_latest_pending_topup(
    user_id: int,
) -> dict[str, Any] | None:
    return db.q_one(
        """
        SELECT *
        FROM topups
        WHERE user_id = ?
          AND status = 'pending'
        ORDER BY id DESC
        LIMIT 1
        """,
        (int(user_id),),
    )


def attach_topup_receipt(
    topup_id: int,
    user_id: int,
    file_id: str,
    caption: str = "",
) -> dict[str, Any]:
    receipt_id = (file_id or "").strip()

    if not receipt_id:
        raise ShopError("Файл чека не получен")

    if len(receipt_id) > 1024:
        raise ShopError("Некорректный идентификатор файла")

    now = int(time.time())

    with db.tx() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM topups
            WHERE id = ? AND user_id = ?
            """,
            (int(topup_id), int(user_id)),
        ).fetchone()

        if not row:
            raise ShopError("Заявка на пополнение не найдена")

        if row["status"] != "pending":
            raise ShopError(
                "По этой заявке уже принято решение"
            )

        updated = conn.execute(
            """
            UPDATE topups
            SET
                receipt_file_id = ?,
                receipt_caption = ?,
                updated_at = ?
            WHERE id = ?
              AND user_id = ?
              AND status = 'pending'
            """,
            (
                receipt_id,
                (caption or "")[:MAX_RECEIPT_CAPTION],
                now,
                int(topup_id),
                int(user_id),
            ),
        )

        if updated.rowcount != 1:
            raise ShopError(
                "Не удалось прикрепить чек. "
                "Обновите заявку."
            )

        result = conn.execute(
            "SELECT * FROM topups WHERE id = ?",
            (int(topup_id),),
        ).fetchone()

        return dict(result)


def approve_topup_once(
    topup_id: int,
    admin_id: int,
    comment: str = "",
) -> dict[str, Any]:
    """
    Подтверждает пополнение и зачисляет средства
    в рамках одной транзакции строго один раз.

    Вызывать только после фактической проверки
    поступления денег администратором.
    """

    now = int(time.time())
    admin_comment = (
        (comment or "").strip()
        or "Подтверждено администратором"
    )[:MAX_ADMIN_COMMENT]

    with db.tx() as conn:
        row = conn.execute(
            "SELECT * FROM topups WHERE id = ?",
            (int(topup_id),),
        ).fetchone()

        if not row:
            raise ShopError("Заявка не найдена")

        if row["status"] == "approved":
            return {
                "outcome": "already_approved",
                "topup": dict(row),
            }

        if row["status"] == "rejected":
            return {
                "outcome": "already_rejected",
                "topup": dict(row),
            }

        if row["status"] != "pending":
            raise ShopError(
                f"Некорректный статус заявки: {row['status']}"
            )

        uid = int(row["user_id"])
        amount = int(row["amount_uzs"])

        if amount <= 0:
            raise ShopError("Некорректная сумма пополнения")

        wallet = db.ensure_wallet(conn, uid)

        new_balance = int(wallet["balance_uzs"]) + amount
        new_bonus = int(wallet["bonus_uzs"])

        updated = conn.execute(
            """
            UPDATE topups
            SET
                status = 'approved',
                admin_id = ?,
                admin_comment = ?,
                updated_at = ?,
                decided_at = ?
            WHERE id = ?
              AND status = 'pending'
            """,
            (
                int(admin_id),
                admin_comment,
                now,
                now,
                int(topup_id),
            ),
        )

        if updated.rowcount != 1:
            raise ShopError(
                "Заявка уже обработана другим администратором"
            )

        wallet_update = conn.execute(
            """
            UPDATE wallets
            SET
                balance_uzs = balance_uzs + ?,
                updated_at = ?
            WHERE user_id = ?
            """,
            (
                amount,
                now,
                uid,
            ),
        )

        if wallet_update.rowcount != 1:
            raise ShopError("Не удалось обновить баланс")

        conn.execute(
            """
            INSERT INTO wallet_transactions (
                user_id,
                kind,
                amount_uzs,
                bonus_delta_uzs,
                balance_after_uzs,
                bonus_after_uzs,
                ref_type,
                ref_id,
                note,
                created_at
            )
            VALUES (
                ?, 'topup', ?, 0, ?, ?,
                'topup', ?, ?, ?
            )
            """,
            (
                uid,
                amount,
                new_balance,
                new_bonus,
                str(topup_id),
                f"Пополнение баланса по заявке #{topup_id}",
                now,
            ),
        )

        conn.execute(
            """
            INSERT INTO admin_audit_log (
                admin_id,
                action,
                target_type,
                target_id,
                details,
                created_at
            )
            VALUES (
                ?, 'approve_topup',
                'topup', ?, ?, ?
            )
            """,
            (
                int(admin_id),
                str(topup_id),
                f"Зачислено {amount} UZS. {admin_comment}",
                now,
            ),
        )

        updated_topup = conn.execute(
            "SELECT * FROM topups WHERE id = ?",
            (int(topup_id),),
        ).fetchone()

    notify(
        uid,
        f"✅ <b>Пополнение подтверждено!</b>\n"
        f"Заявка <b>#{topup_id}</b>\n"
        f"Зачислено: <b>{uzs(amount)}</b>\n"
        f"Текущий баланс: <b>{uzs(new_balance)}</b>.",
    )

    return {
        "outcome": "approved",
        "topup": dict(updated_topup),
        "balance_after_uzs": new_balance,
    }


def reject_topup_once(
    topup_id: int,
    admin_id: int,
    comment: str = "",
) -> dict[str, Any]:
    """Отклоняет заявку без изменения баланса."""

    now = int(time.time())

    admin_comment = (
        (comment or "").strip()
        or "Отклонено администратором"
    )[:MAX_ADMIN_COMMENT]

    with db.tx() as conn:
        row = conn.execute(
            "SELECT * FROM topups WHERE id = ?",
            (int(topup_id),),
        ).fetchone()

        if not row:
            raise ShopError("Заявка не найдена")

        if row["status"] == "approved":
            return {
                "outcome": "already_approved",
                "topup": dict(row),
            }

        if row["status"] == "rejected":
            return {
                "outcome": "already_rejected",
                "topup": dict(row),
            }

        if row["status"] != "pending":
            raise ShopError(
                f"Некорректный статус заявки: {row['status']}"
            )

        updated = conn.execute(
            """
            UPDATE topups
            SET
                status = 'rejected',
                admin_id = ?,
                admin_comment = ?,
                updated_at = ?,
                decided_at = ?
            WHERE id = ?
              AND status = 'pending'
            """,
            (
                int(admin_id),
                admin_comment,
                now,
                now,
                int(topup_id),
            ),
        )

        if updated.rowcount != 1:
            raise ShopError(
                "Заявка уже обработана другим администратором"
            )

        conn.execute(
            """
            INSERT INTO admin_audit_log (
                admin_id,
                action,
                target_type,
                target_id,
                details,
                created_at
            )
            VALUES (
                ?, 'reject_topup',
                'topup', ?, ?, ?
            )
            """,
            (
                int(admin_id),
                str(topup_id),
                admin_comment,
                now,
            ),
        )

        updated_topup = conn.execute(
            "SELECT * FROM topups WHERE id = ?",
            (int(topup_id),),
        ).fetchone()

    uid = int(row["user_id"])
    amount = int(row["amount_uzs"])

    notify(
        uid,
        f"❌ <b>Заявка #{topup_id} отклонена</b>\n"
        f"Сумма: <b>{uzs(amount)}</b>\n"
        f"Баланс не изменён.\n"
        f"Причина: {h(admin_comment)}",
    )

    return {
        "outcome": "rejected",
        "topup": dict(updated_topup),
    }


# --------------------------------------------------
# TELEGRAM STARS
# --------------------------------------------------


def send_order_invoice(
    chat_id: int,
    order: dict[str, Any],
) -> None:
    try:
        total = int(order.get("total", 0))
        order_id = int(order["id"])
        user_id = int(order["user_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ShopError("Некорректные данные заказа") from exc

    if total <= 0 or order_id <= 0 or user_id <= 0:
        raise ShopError("Некорректная сумма или идентификатор заказа")

    prices = [
        types.LabeledPrice(
            label=f"Заказ #{order_id}",
            amount=total,
        )
    ]

    bot.send_invoice(
        chat_id=chat_id,
        title=f"Заказ #{order_id} — {config.BRAND}",
        description=(
            f"Получатель: {order.get('recipient', '')}"
        )[:255],
        invoice_payload=f"order:{order_id}:{user_id}",
        provider_token="",
        currency="XTR",
        prices=prices,
    )


def handle_pre_checkout(
    query: types.PreCheckoutQuery,
) -> None:
    try:
        parsed = _parse_invoice_payload(
            query.invoice_payload or ""
        )

        if not parsed:
            bot.answer_pre_checkout_query(
                query.id,
                ok=False,
                error_message="Некорректный счёт",
            )
            return

        order_id, expected_uid = parsed

        order = db.q_one(
            """
            SELECT *
            FROM legacy_orders
            WHERE id = ?
            """,
            (order_id,),
        )

        if (
            not order
            or int(order["user_id"]) != int(query.from_user.id)
            or int(order["user_id"]) != expected_uid
            or order["currency"] != "XTR"
            or query.currency != "XTR"
            or int(order["total_amount"]) != int(query.total_amount)
            or order["status"] != "pending"
        ):
            bot.answer_pre_checkout_query(
                query.id,
                ok=False,
                error_message=(
                    "Заказ недействителен или уже обработан"
                ),
            )
            return

        bot.answer_pre_checkout_query(
            query.id,
            ok=True,
        )

    except Exception:
        logging_error = __import__("logging").getLogger(
            "goldstar.payments"
        )

        logging_error.exception(
            "Ошибка проверки Telegram Stars pre-checkout"
        )

        try:
            bot.answer_pre_checkout_query(
                query.id,
                ok=False,
                error_message="Ошибка проверки платежа",
            )
        except Exception:
            logging_error.exception(
                "Не удалось отправить ответ pre-checkout"
            )


def record_payment(
    user_id: int,
    currency: str,
    total_amount: int,
    invoice_payload: str,
    telegram_charge_id: str,
    provider_charge_id: str = "",
) -> dict[str, Any]:
    now = int(time.time())

    uid = int(user_id)
    charge_id = (telegram_charge_id or "").strip()
    provider_id = (provider_charge_id or "").strip()

    if not charge_id:
        return {
            "result": "review",
            "problem": "missing_charge_id",
            "order": None,
            "payment": None,
        }

    if len(charge_id) > 255:
        return {
            "result": "review",
            "problem": "invalid_charge_id",
            "order": None,
            "payment": None,
        }

    parsed = _parse_invoice_payload(invoice_payload or "")

    if not parsed:
        return {
            "result": "review",
            "problem": "invalid_invoice_payload",
            "order": None,
            "payment": None,
        }

    order_id, payload_uid = parsed

    if uid <= 0 or int(total_amount) <= 0:
        return {
            "result": "review",
            "problem": "invalid_payment_data",
            "order": None,
            "payment": None,
        }

    problem = None
    payment_id = None

    with db.tx() as conn:
        duplicate = conn.execute(
            """
            SELECT *
            FROM payments
            WHERE telegram_charge_id = ?
            """,
            (charge_id,),
        ).fetchone()

        if duplicate:
            if (
                int(duplicate["user_id"]) == uid
                and duplicate["payload"] == invoice_payload
            ):
                return {
                    "result": "duplicate",
                    "payment": dict(duplicate),
                }

            return {
                "result": "review",
                "problem": "charge_id_conflict",
                "order": None,
                "payment": dict(duplicate),
            }

        order = conn.execute(
            """
            SELECT *
            FROM legacy_orders
            WHERE id = ?
            """,
            (order_id,),
        ).fetchone()

        if not order:
            problem = "order_not_found"

        elif int(order["user_id"]) != uid:
            problem = "user_mismatch"

        elif payload_uid != uid:
            problem = "payload_user_mismatch"

        elif currency != "XTR":
            problem = f"currency_mismatch:{currency}"

        elif order["currency"] != "XTR":
            problem = "order_currency_mismatch"

        elif int(order["total_amount"]) != int(total_amount):
            problem = (
                f"amount_mismatch:{total_amount}"
                f"!={order['total_amount']}"
            )

        elif order["status"] != "pending":
            problem = f"invalid_order_status:{order['status']}"

        elif conn.execute(
            """
            SELECT 1
            FROM payments
            WHERE order_id = ?
              AND status = 'paid'
            LIMIT 1
            """,
            (order_id,),
        ).fetchone():
            problem = "order_already_paid"

        status = "paid" if problem is None else "review"

        cursor = conn.execute(
            """
            INSERT INTO payments (
                order_id,
                user_id,
                currency,
                amount,
                payload,
                telegram_charge_id,
                provider_charge_id,
                status,
                problem,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order_id,
                uid,
                currency,
                int(total_amount),
                invoice_payload,
                charge_id,
                provider_id,
                status,
                problem,
                now,
            ),
        )

        payment_id = int(cursor.lastrowid)

        if problem is None:
            updated = conn.execute(
                """
                UPDATE legacy_orders
                SET
                    status = 'paid',
                    updated_at = ?
                WHERE id = ?
                  AND user_id = ?
                  AND status = 'pending'
                """,
                (
                    now,
                    order_id,
                    uid,
                ),
            )

            if updated.rowcount != 1:
                raise ShopError(
                    "Не удалось подтвердить статус заказа"
                )

    order_full = db.q_one(
        """
        SELECT
            id,
            user_id,
            recipient,
            total_amount AS total,
            currency,
            status
        FROM legacy_orders
        WHERE id = ?
        """,
        (order_id,),
    )

    if order_full:
        order_full["items"] = db.q_all(
            """
            SELECT title, price, qty
            FROM legacy_order_items
            WHERE order_id = ?
            """,
            (order_id,),
        )

    payment_full = db.q_one(
        "SELECT * FROM payments WHERE id = ?",
        (payment_id,),
    )

    return {
        "result": "paid" if problem is None else "review",
        "problem": problem,
        "order": order_full,
        "payment": payment_full,
    }
