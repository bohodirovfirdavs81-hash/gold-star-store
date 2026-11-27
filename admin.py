import logging
from typing import Any

import config
import database as db
import payments
import shop
from utils import bot, h, uzs

logger = logging.getLogger(__name__)


def is_admin(user_id: int | None) -> bool:
    try:
        uid = int(user_id or 0)
    except (TypeError, ValueError):
        return False

    return uid > 0 and uid in config.ADMIN_IDS


def _keyboard(rows: list[list[Any]]) -> Any:
    from telebot import types

    markup = types.InlineKeyboardMarkup()
    for row in rows:
        markup.row(*row)
    return markup


def _send_admin_message(text: str, markup=None) -> None:
    for admin_id in config.ADMIN_IDS:
        try:
            bot.send_message(
                int(admin_id),
                text,
                reply_markup=markup,
                parse_mode="HTML",
            )
        except Exception:
            logger.exception("Не удалось отправить сообщение администратору")


def send_topup_to_admins(topup_id: int) -> None:
    topup = payments.get_topup(int(topup_id))
    if not topup:
        return

    if topup["status"] != "pending":
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
            logger.exception("Ошибка отправки заявки администратору")


def notify_admins_manual_order(order: dict[str, Any]) -> None:
    if not order:
        return

    order_id = int(order["id"])
    text = (
        "<b>Новый заказ требует обработки</b>\n\n"
        f"Заказ: <code>#{order_id}</code>\n"
        f"Пользователь: <code>{int(order['user_id'])}</code>\n"
        f"Товар: {h(order['product_title'])}\n"
        f"Количество: {int(order['qty'])}\n"
        f"Получатель: {h(order['recipient'])}\n"
        f"Сумма: <b>{h(uzs(int(order['total_uzs'])))}</b>\n"
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


def _answer(call, text: str, alert: bool = False) -> None:
    try:
        bot.answer_callback_query(
            call.id,
            text=text,
            show_alert=alert,
        )
    except Exception:
        logger.exception("Не удалось ответить на callback")


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
        logger.exception("Не удалось обновить сообщение администратора")


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
        if action_type == "topup" and action == "approve":
            result = payments.approve_topup_once(
                item_id,
                int(call.from_user.id),
            )

            if result.get("already_processed"):
                _answer(call, "Заявка уже обработана", True)
            else:
                _answer(call, "Пополнение подтверждено")
                _edit_message(
                    call,
                    f"Пополнение #{item_id} подтверждено.",
                )
            return True

        if action_type == "topup" and action == "reject":
            result = payments.reject_topup_once(
                item_id,
                int(call.from_user.id),
                "Отклонено администратором",
            )

            if result.get("already_processed"):
                _answer(call, "Заявка уже обработана", True)
            else:
                _answer(call, "Заявка отклонена")
                _edit_message(
                    call,
                    f"Пополнение #{item_id} отклонено.",
                )
            return True

        if action_type == "order" and action == "deliver":
            shop.mark_wallet_order_delivered(
                item_id,
                int(call.from_user.id),
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
                int(call.from_user.id),
                "Отменено администратором",
            )
            _answer(call, "Заказ отменён, возврат выполнен")
            _edit_message(
                call,
                f"Заказ #{item_id} отменён. Возврат выполнен.",
            )
            return True

    except Exception as exc:
        logger.exception("Ошибка обработки действия администратора")
        _answer(call, str(exc)[:180], True)
        return True

    return False


def handle_state(message, fsm=None) -> bool:
    """
    Заглушка для совместимости с обработчиком сообщений в bot.py.
    Ввод причины отклонения заявки пока не используется.
    """
    return False


def register() -> None:
    @bot.callback_query_handler(
        func=lambda call: str(call.data or "").startswith("adm:")
    )
    def admin_callback(call):
        handle_admin_callback(call)