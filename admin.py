import logging
from typing import Any

import config
import database as db
import payments
import shop
from utils import bot, h, uzs

logger = logging.getLogger(__name__)


# -------------------- Общие функции --------------------

def is_admin(user_id: int | None) -> bool:
    try:
        uid = int(user_id or 0)
    except (TypeError, ValueError):
        return False

    return uid > 0 and uid in config.ADMIN_IDS


def _uid(user_id: int) -> int:
    uid = int(user_id)
    if uid <= 0 or not is_admin(uid):
        raise PermissionError("Доступ запрещён")
    return uid


def _keyboard(rows: list[list[Any]]) -> Any:
    from telebot import types

    markup = types.InlineKeyboardMarkup()
    for row in rows:
        markup.row(*row)
    return markup


def _row_dict(row):
    if row is None:
        return None

    try:
        return dict(row)
    except (TypeError, ValueError):
        return row


def _send_admin_message(text: str, markup=None) -> None:
    for admin_id in config.ADMIN_IDS:
        try:
            bot.send_message(
                int(admin_id),
                text,
                reply_markup=markup,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception:
            logger.exception(
                "Не удалось отправить сообщение администратору"
            )


def _audit(
    admin_id: int,
    action: str,
    target_type: str,
    target_id: int,
    details: str,
) -> None:
    try:
        db.record_admin_action(
            int(admin_id),
            str(action),
            str(target_type),
            str(target_id),
            str(details),
        )
    except Exception:
        logger.exception("Не удалось записать действие администратора")


# -------------------- Статистика --------------------

def get_stats(admin_id: int) -> dict[str, int]:
    _uid(admin_id)

    users = db.fetchone(
        "SELECT COUNT(*) AS total FROM users"
    )

    products = db.fetchone(
        "SELECT COUNT(*) AS total FROM products WHERE active = 1"
    )

    pending_topups = db.fetchone(
        "SELECT COUNT(*) AS total FROM topups WHERE status = 'pending'"
    )

    pending_orders = db.fetchone(
        """
        SELECT COUNT(*) AS total
        FROM wallet_orders
        WHERE status IN ('pending', 'processing')
        """
    )

    paid_orders = db.fetchone(
        """
        SELECT COUNT(*) AS total
        FROM wallet_orders
        WHERE status = 'delivered'
        """
    )

    wallet = db.fetchone(
        """
        SELECT
            COALESCE(SUM(balance_uzs), 0) AS balance,
            COALESCE(SUM(bonus_uzs), 0) AS bonus
        FROM wallets
        """
    )

    approved_topups = db.fetchone(
        """
        SELECT COALESCE(SUM(amount_uzs), 0) AS total
        FROM topups
        WHERE status = 'approved'
        """
    )

    return {
        "users": int(users["total"] if users else 0),
        "products": int(products["total"] if products else 0),
        "pending_topups": int(
            pending_topups["total"] if pending_topups else 0
        ),
        "pending_orders": int(
            pending_orders["total"] if pending_orders else 0
        ),
        "paid_orders": int(
            paid_orders["total"] if paid_orders else 0
        ),
        "wallet_balance_uzs": int(
            wallet["balance"] if wallet else 0
        ),
        "wallet_bonus_uzs": int(
            wallet["bonus"] if wallet else 0
        ),
        "approved_topups_uzs": int(
            approved_topups["total"] if approved_topups else 0
        ),
    }


# -------------------- Пополнения --------------------

def get_topups(
    admin_id: int,
    status: str | None = None,
    limit: int = 20,
    offset: int = 0,
):
    _uid(admin_id)

    limit = max(1, min(int(limit), 100))
    offset = max(0, int(offset))

    if status is not None:
        status = str(status).strip().lower()

        if status not in {
            "pending",
            "approved",
            "rejected",
        }:
            raise ValueError("Некорректный статус пополнения")

        return db.fetchall(
            """
            SELECT *
            FROM topups
            WHERE status = ?
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """,
            (status, limit, offset),
        )

    return db.fetchall(
        """
        SELECT *
        FROM topups
        ORDER BY id DESC
        LIMIT ? OFFSET ?
        """,
        (limit, offset),
    )


def approve_topup(
    admin_id: int,
    topup_id: int,
    comment: str = "Подтверждено администратором",
):
    admin_id = _uid(admin_id)
    topup_id = int(topup_id)

    if topup_id <= 0:
        raise ValueError("Некорректный номер заявки")

    topup = payments.get_topup(topup_id)

    if not topup:
        raise ValueError("Заявка не найдена")

    if topup["status"] != "pending":
        raise ValueError("Заявка уже обработана")

    result = payments.approve_topup_once(
        topup_id,
        admin_id,
        comment,
    )

    if isinstance(result, dict) and result.get("already_processed"):
        raise ValueError("Заявка уже обработана")

    _audit(
        admin_id,
        "approve_topup",
        "topup",
        topup_id,
        comment,
    )

    return {
        "id": topup_id,
        "user_id": int(topup["user_id"]),
        "amount_uzs": int(topup["amount_uzs"]),
        "status": "approved",
    }


def reject_topup(
    admin_id: int,
    topup_id: int,
    comment: str = "Отклонено администратором",
):
    admin_id = _uid(admin_id)
    topup_id = int(topup_id)

    if topup_id <= 0:
        raise ValueError("Некорректный номер заявки")

    topup = payments.get_topup(topup_id)

    if not topup:
        raise ValueError("Заявка не найдена")

    if topup["status"] != "pending":
        raise ValueError("Заявка уже обработана")

    result = payments.reject_topup_once(
        topup_id,
        admin_id,
        comment,
    )

    if isinstance(result, dict) and result.get("already_processed"):
        raise ValueError("Заявка уже обработана")

    _audit(
        admin_id,
        "reject_topup",
        "topup",
        topup_id,
        comment,
    )

    return {
        "id": topup_id,
        "user_id": int(topup["user_id"]),
        "amount_uzs": int(topup["amount_uzs"]),
        "status": "rejected",
    }


def send_topup_to_admins(topup_id: int) -> None:
    topup = payments.get_topup(int(topup_id))

    if not topup or topup["status"] != "pending":
        return

    user_id = int(topup["user_id"])
    amount = int(topup["amount_uzs"])
    receipt = topup["receipt_file_id"]

    text = (
        "<b>Новое пополнение баланса</b>\n\n"
        f"Номер заявки: <code>#{topup_id}</code>\n"
        f"Пользователь: <code>{user_id}</code>\n"
        f"Сумма: <b>{h(uzs(amount))}</b>\n"
        "Статус: ожидает проверки"
    )

    from telebot import types

    markup = _keyboard([
        [
            types.InlineKeyboardButton(
                "Подтвердить",
                callback_data=f"adm:topup:approve:{topup_id}",
            ),
            types.InlineKeyboardButton(
                "Отклонить",
                callback_data=f"adm:topup:reject:{topup_id}",
            ),
        ]
    ])

    for admin_id in config.ADMIN_IDS:
        try:
            if receipt:
                bot.send_photo(
                    int(admin_id),
                    receipt,
                    caption=text,
                    reply_markup=markup,
                    parse_mode="HTML",
                )
            else:
                bot.send_message(
                    int(admin_id),
                    text,
                    reply_markup=markup,
                    parse_mode="HTML",
                )
        except Exception:
            logger.exception(
                "Ошибка отправки заявки администратору"
            )


# -------------------- Заказы --------------------

def get_orders(
    admin_id: int,
    limit: int = 15,
    offset: int = 0,
    status: str | None = None,
):
    _uid(admin_id)

    limit = max(1, min(int(limit), 100))
    offset = max(0, int(offset))

    if status is not None:
        status = str(status).strip().lower()

        if status not in {
            "pending",
            "processing",
            "delivered",
            "cancelled",
            "rejected",
        }:
            raise ValueError("Некорректный статус заказа")

        return db.fetchall(
            """
            SELECT *
            FROM wallet_orders
            WHERE status = ?
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """,
            (status, limit, offset),
        )

    return db.fetchall(
        """
        SELECT *
        FROM wallet_orders
        ORDER BY id DESC
        LIMIT ? OFFSET ?
        """,
        (limit, offset),
    )


def get_order_details(admin_id: int, order_id: int):
    _uid(admin_id)

    order_id = int(order_id)

    if order_id <= 0:
        raise ValueError("Некорректный номер заказа")

    return db.fetchone(
        """
        SELECT *
        FROM wallet_orders
        WHERE id = ?
        LIMIT 1
        """,
        (order_id,),
    )


def change_order_status(
    admin_id: int,
    order_id: int,
    status: str,
    comment: str = "",
):
    admin_id = _uid(admin_id)
    order_id = int(order_id)
    status = str(status).strip().lower()

    if order_id <= 0:
        raise ValueError("Некорректный номер заказа")

    if status not in {"processing", "delivered", "rejected"}:
        raise ValueError("Недопустимый статус заказа")

    order = get_order_details(admin_id, order_id)

    if not order:
        raise ValueError("Заказ не найден")

    current_status = str(order["status"])

    if current_status in {"delivered", "cancelled", "rejected"}:
        raise ValueError(
            "Нельзя изменить уже завершённый заказ"
        )

    if status == "delivered":
        shop.mark_wallet_order_delivered(
            order_id,
            admin_id,
        )

    elif status == "rejected":
        shop.refund_wallet_order_once(
            order_id,
            admin_id,
            comment or "Отклонено администратором",
        )

    else:
        with db.transaction():
            cursor = db.execute(
                """
                UPDATE wallet_orders
                SET status = 'processing',
                    updated_at = ?
                WHERE id = ?
                  AND status IN ('pending', 'processing')
                """,
                (db.now(), order_id),
            )

            if cursor.rowcount != 1:
                raise ValueError(
                    "Не удалось изменить статус заказа"
                )

    _audit(
        admin_id,
        "change_order_status",
        "wallet_order",
        order_id,
        f"{current_status} -> {status}; {comment}",
    )

    return get_order_details(admin_id, order_id)


def notify_admins_manual_order(order: dict[str, Any]) -> None:
    if not order:
        return

    order_id = int(order["id"])

    total = order.get(
        "total_price_uzs",
        order.get("total_uzs", 0),
    )

    text = (
        "<b>Новый заказ требует обработки</b>\n\n"
        f"Заказ: <code>#{order_id}</code>\n"
        f"Пользователь: <code>{int(order['user_id'])}</code>\n"
        f"Товар: {h(order['product_title'])}\n"
        f"Количество: {int(order['qty'])}\n"
        f"Получатель: {h(order.get('recipient') or '—')}\n"
        f"Сумма: <b>{h(uzs(int(total)))}</b>\n"
        "Статус: ожидает выполнения"
    )

    from telebot import types

    markup = _keyboard([
        [
            types.InlineKeyboardButton(
                "Выдать заказ",
                callback_data=f"adm:order:deliver:{order_id}",
            ),
            types.InlineKeyboardButton(
                "Отменить и вернуть",
                callback_data=f"adm:order:cancel:{order_id}",
            ),
        ]
    ])

    _send_admin_message(text, markup)


# -------------------- Пользователи --------------------

def get_users(
    admin_id: int,
    limit: int = 15,
    offset: int = 0,
):
    _uid(admin_id)

    limit = max(1, min(int(limit), 100))
    offset = max(0, int(offset))

    return db.fetchall(
        """
        SELECT
            u.telegram_id,
            u.username,
            u.first_name,
            u.last_name,
            u.created_at,
            COALESCE(w.balance_uzs, 0) AS balance_uzs,
            COALESCE(w.bonus_uzs, 0) AS bonus_uzs
        FROM users AS u
        LEFT JOIN wallets AS w
            ON w.user_id = u.telegram_id
        ORDER BY u.created_at DESC, u.telegram_id DESC
        LIMIT ? OFFSET ?
        """,
        (limit, offset),
    )


# -------------------- Callback администратора --------------------

def _answer(call, text: str = "", alert: bool = False) -> None:
    try:
        bot.answer_callback_query(
            call.id,
            text=text,
            show_alert=alert,
        )
    except Exception:
        logger.exception(
            "Не удалось ответить на callback"
        )


def _edit_message(call, text: str) -> None:
    try:
        bot.edit_message_text(
            text,
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            parse_mode="HTML",
            reply_markup=None,
        )
    except Exception:
        logger.exception(
            "Не удалось обновить сообщение администратора"
        )


def handle_admin_callback(call) -> bool:
    data = str(call.data or "").split(":")

    if len(data) != 4 or data[0] != "adm":
        return False

    if not is_admin(call.from_user.id):
        _answer(call, "Нет доступа", True)
        return True

    action_type = data[1]
    action = data[2]

    try:
        item_id = int(data[3])

        if item_id <= 0:
            raise ValueError

    except (TypeError, ValueError):
        _answer(call, "Некорректный номер", True)
        return True

    try:
        admin_id = int(call.from_user.id)

        if action_type == "topup" and action == "approve":
            result = approve_topup(
                admin_id,
                item_id,
                "Подтверждено администратором",
            )

            _answer(call, "Пополнение подтверждено")
            _edit_message(
                call,
                f"Пополнение #{item_id} подтверждено.",
            )
            return True

        if action_type == "topup" and action == "reject":
            reject_topup(
                admin_id,
                item_id,
                "Отклонено администратором",
            )

            _answer(call, "Заявка отклонена")
            _edit_message(
                call,
                f"Пополнение #{item_id} отклонено.",
            )
            return True

        if action_type == "order" and action == "deliver":
            change_order_status(
                admin_id,
                item_id,
                "delivered",
                "Выдано администратором",
            )

            _answer(call, "Заказ отмечен как выданный")
            _edit_message(
                call,
                f"Заказ #{item_id} отмечен как выданный.",
            )
            return True

        if action_type == "order" and action == "cancel":
            shop.refund_wallet_order_once(
                item_id,
                admin_id,
                "Отменено администратором",
            )

            _answer(call, "Заказ отменён, возврат выполнен")
            _edit_message(
                call,
                f"Заказ #{item_id} отменён. Возврат выполнен.",
            )
            return True

    except Exception as exc:
        logger.exception(
            "Ошибка обработки действия администратора"
        )
        _answer(call, str(exc)[:180], True)
        return True

    return False


def handle_state(message, fsm=None) -> bool:
    return False


def register() -> None:
    @bot.callback_query_handler(
        func=lambda call: str(call.data or "").startswith("adm:")
    )
    def admin_callback(call):
        handle_admin_callback(call)