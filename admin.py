# -*- coding: utf-8 -*-
"""Gold Star Store — административные обработчики."""

import logging

import config
import database as db
import payments
import shop

from utils import (
    bot,
    btn,
    fmt_dt,
    h,
    kb,
    notify,
    safe_answer,
    show,
    uzs,
)

logger = logging.getLogger(__name__)

MAX_REJECT_REASON_LENGTH = 500


def is_admin(user_id: int | None) -> bool:
    """Проверяет, является ли пользователь администратором."""
    if user_id is None:
        return False

    try:
        uid = int(user_id)
    except (TypeError, ValueError, OverflowError):
        return False

    return uid > 0 and uid in config.ADMIN_IDS


def _safe_int(value, default: int = 0) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return default

    return result if result > 0 else default


def _log_admin_notification_error(admin_id: int, exc: Exception) -> None:
    logger.warning(
        "Не удалось отправить административное уведомление admin_id=%s: %s",
        admin_id,
        exc,
        exc_info=True,
    )


def _remove_keyboard(call) -> None:
    """Удаляет старые кнопки после обработки действия."""
    try:
        if call.message:
            bot.edit_message_reply_markup(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=None,
            )
    except Exception:
        logger.debug(
            "Не удалось удалить клавиатуру сообщения",
            exc_info=True,
        )


def _finish_admin_message(call, text: str) -> None:
    """Обновляет административное сообщение и удаляет кнопки."""
    _remove_keyboard(call)

    try:
        show(call, text)
    except Exception:
        logger.exception("Не удалось обновить сообщение администратора")


def send_topup_to_admins(topup: dict, user_obj) -> None:
    """Отправляет администраторам заявку на пополнение."""
    try:
        tid = _safe_int(topup.get("id"))
        uid = _safe_int(topup.get("user_id"))
        amount = _safe_int(topup.get("amount_uzs"))

        if not tid or not uid or not amount:
            logger.error("Некорректные данные заявки на пополнение: %r", topup)
            return

        username = getattr(user_obj, "username", None) if user_obj else None
        user_label = f"@{username}" if username else "без username"

        caption = (
            f"💳 <b>Заявка на пополнение #{tid}</b>\n"
            f"Пользователь: <code>{uid}</code> ({h(user_label)})\n"
            f"Сумма: <b>{uzs(amount)}</b>\n"
            "Статус: ожидает проверки поступления средств в банке"
        )

        receipt_caption = topup.get("receipt_caption")
        if receipt_caption:
            caption += (
                "\nКомментарий пользователя: "
                f"{h(str(receipt_caption)[:500])}"
            )

        markup = kb([
            [
                btn("✅ Подтвердить", f"adm:topup_ok:{tid}"),
                btn("❌ Отклонить", f"adm:topup_no:{tid}"),
            ]
        ])

        file_id = topup.get("receipt_file_id")

        for admin_id in config.ADMIN_IDS:
            try:
                if file_id:
                    bot.send_photo(
                        admin_id,
                        file_id,
                        caption=caption,
                        reply_markup=markup,
                    )
                else:
                    bot.send_message(
                        admin_id,
                        caption,
                        reply_markup=markup,
                        disable_web_page_preview=True,
                    )
            except Exception as exc:
                _log_admin_notification_error(admin_id, exc)

    except Exception:
        logger.exception("Ошибка формирования уведомления о пополнении")


def notify_admins_manual_order(order: dict, buyer_obj=None) -> None:
    """Отправляет администраторам заказ, ожидающий ручной выдачи."""
    try:
        oid = _safe_int(order.get("id"))
        uid = _safe_int(order.get("user_id"))

        if not oid or not uid:
            logger.error("Некорректные данные заказа: %r", order)
            return

        username = (
            getattr(buyer_obj, "username", None)
            if buyer_obj
            else None
        )

        buyer_label = f"id {uid}"
        if username:
            buyer_label += f" (@{username})"

        recipient = order.get("recipient") or "—"
        product_title = order.get("product_title") or "—"

        qty = _safe_int(order.get("qty"))
        total = _safe_int(order.get("total_uzs"))
        paid_balance = _safe_int(
            order.get("paid_from_balance_uzs")
        )
        paid_bonus = _safe_int(
            order.get("paid_from_bonus_uzs")
        )

        message_text = (
            f"🛠 <b>Заказ на ручную выдачу #{oid}</b>\n"
            f"Покупатель: {h(buyer_label)}\n"
            f"Получатель: <b>{h(recipient)}</b>\n"
            f"Товар: <b>{h(product_title)}</b> × {qty}\n"
            f"Сумма: <b>{uzs(total)}</b>\n"
            f"Списано с баланса: {uzs(paid_balance)} "
            f"| Бонусами: {uzs(paid_bonus)}"
        )

        markup = kb([
            [
                btn("📦 Выдано", f"adm:deliver:{oid}"),
                btn("❌ Отменить и вернуть", f"adm:cancel_ord:{oid}"),
            ]
        ])

        for admin_id in config.ADMIN_IDS:
            try:
                bot.send_message(
                    admin_id,
                    message_text,
                    reply_markup=markup,
                    disable_web_page_preview=True,
                )
            except Exception as exc:
                _log_admin_notification_error(admin_id, exc)

    except Exception:
        logger.exception("Ошибка формирования уведомления о заказе")


def handle_state(message, fsm: dict) -> None:
    """Обрабатывает административные состояния."""
    try:
        uid = int(message.from_user.id)
    except (AttributeError, TypeError, ValueError, OverflowError):
        return

    if not is_admin(uid):
        db.fsm_clear(uid)
        return

    state = fsm.get("state")
    data = fsm.get("data") or {}

    if not isinstance(data, dict):
        db.fsm_clear(uid)
        return

    if state == "adm_reject_reason":
        tid = _safe_int(data.get("topup_id"))

        if not tid:
            db.fsm_clear(uid)
            bot.send_message(
                message.chat.id,
                "⚠️ Не удалось определить заявку. Начните действие заново.",
            )
            return

        reason = (message.text or "").strip()

        if not reason:
            bot.send_message(
                message.chat.id,
                "Укажите причину отклонения текстовым сообщением.",
            )
            return

        if len(reason) > MAX_REJECT_REASON_LENGTH:
            bot.send_message(
                message.chat.id,
                f"Причина слишком длинная. Максимум "
                f"{MAX_REJECT_REASON_LENGTH} символов.",
            )
            return

        try:
            result = payments.reject_topup_once(
                tid,
                uid,
                reason,
            )

            db.fsm_clear(uid)

            outcome = result.get("outcome", "unknown")

            bot.send_message(
                message.chat.id,
                f"Заявка #{tid}: {h(outcome)}",
            )

        except shop.ShopError as exc:
            db.fsm_clear(uid)
            bot.send_message(
                message.chat.id,
                f"⚠️ {h(exc)}",
            )

        except Exception:
            db.fsm_clear(uid)
            logger.exception(
                "Ошибка отклонения заявки topup_id=%s admin_id=%s",
                tid,
                uid,
            )
            bot.send_message(
                message.chat.id,
                "⚠️ Не удалось обработать заявку. Проверьте журнал ошибок.",
            )

        return

    db.fsm_clear(uid)


def register() -> None:
    """Регистрирует административные callback-обработчики."""

    @bot.callback_query_handler(
        func=lambda call: (call.data or "").startswith("adm:")
    )
    def on_admin_callback(call):
        try:
            uid = int(call.from_user.id)
        except (AttributeError, TypeError, ValueError, OverflowError):
            return safe_answer(
                call.id,
                "Не удалось определить пользователя",
                True,
            )

        if not is_admin(uid):
            return safe_answer(
                call.id,
                "Доступ запрещён",
                True,
            )

        parts = (call.data or "").split(":")

        if len(parts) != 3:
            return safe_answer(
                call.id,
                "Некорректная команда",
                True,
            )

        action = parts[1]
        object_id = parts[2]

        if not object_id.isdigit():
            return safe_answer(
                call.id,
                "Некорректный идентификатор",
                True,
            )

        object_id = _safe_int(object_id)

        if not object_id:
            return safe_answer(
                call.id,
                "Некорректный идентификатор",
                True,
            )

        # ----------------------------------------------------
        # ПОДТВЕРЖДЕНИЕ ПОПОЛНЕНИЯ
        # ----------------------------------------------------

        if action == "topup_ok":
            try:
                result = payments.approve_topup_once(
                    object_id,
                    uid,
                )

            except shop.ShopError as exc:
                return safe_answer(
                    call.id,
                    str(exc),
                    True,
                )

            except Exception:
                logger.exception(
                    "Ошибка подтверждения пополнения "
                    "topup_id=%s admin_id=%s",
                    object_id,
                    uid,
                )
                return safe_answer(
                    call.id,
                    "Внутренняя ошибка обработки",
                    True,
                )

            outcome = result.get("outcome")

            if outcome == "approved":
                safe_answer(
                    call.id,
                    "Пополнение подтверждено",
                )

                base_text = (
                    call.message.caption
                    or call.message.text
                    or f"Заявка #{object_id}"
                )

                decided_at = (
                    result.get("topup", {}).get("decided_at")
                )

                suffix = "✅ <b>Подтверждено администратором</b>"

                if decided_at:
                    suffix += f" ({fmt_dt(decided_at)})"

                _finish_admin_message(
                    call,
                    f"{base_text}\n\n{suffix}",
                )

            elif outcome == "already_approved":
                safe_answer(
                    call.id,
                    "Заявка уже подтверждена ранее",
                    True,
                )

            elif outcome == "already_rejected":
                safe_answer(
                    call.id,
                    "Заявка уже отклонена ранее",
                    True,
                )

            else:
                safe_answer(
                    call.id,
                    "Заявка уже обработана",
                    True,
                )

            return

        # ----------------------------------------------------
        # ОТКЛОНЕНИЕ ПОПОЛНЕНИЯ
        # ----------------------------------------------------

        if action == "topup_no":
            try:
                result = payments.reject_topup_once(
                    object_id,
                    uid,
                    "Отклонено администратором",
                )

            except shop.ShopError as exc:
                return safe_answer(
                    call.id,
                    str(exc),
                    True,
                )

            except Exception:
                logger.exception(
                    "Ошибка отклонения пополнения "
                    "topup_id=%s admin_id=%s",
                    object_id,
                    uid,
                )
                return safe_answer(
                    call.id,
                    "Внутренняя ошибка обработки",
                    True,
                )

            outcome = result.get("outcome")

            if outcome == "rejected":
                safe_answer(
                    call.id,
                    "Заявка отклонена",
                )

                base_text = (
                    call.message.caption
                    or call.message.text
                    or f"Заявка #{object_id}"
                )

                decided_at = (
                    result.get("topup", {}).get("decided_at")
                )

                suffix = "❌ <b>Отклонено администратором</b>"

                if decided_at:
                    suffix += f" ({fmt_dt(decided_at)})"

                _finish_admin_message(
                    call,
                    f"{base_text}\n\n{suffix}",
                )

            elif outcome == "already_approved":
                safe_answer(
                    call.id,
                    "Заявка уже подтверждена",
                    True,
                )

            else:
                safe_answer(
                    call.id,
                    "Заявка уже отклонена или обработана",
                    True,
                )

            return

        # ----------------------------------------------------
        # ПОДТВЕРЖДЕНИЕ ВЫДАЧИ ЗАКАЗА
        # ----------------------------------------------------

        if action == "deliver":
            try:
                order = shop.mark_wallet_order_delivered(
                    object_id,
                    uid,
                )

            except shop.ShopError as exc:
                return safe_answer(
                    call.id,
                    str(exc),
                    True,
                )

            except Exception:
                logger.exception(
                    "Ошибка выдачи заказа "
                    "order_id=%s admin_id=%s",
                    object_id,
                    uid,
                )
                return safe_answer(
                    call.id,
                    "Внутренняя ошибка обработки",
                    True,
                )

            safe_answer(
                call.id,
                "Заказ выполнен",
            )

            try:
                notify(
                    int(order["user_id"]),
                    f"📦 <b>Заказ #{object_id} выполнен!</b>\n"
                    f"Товар: <b>{h(order['product_title'])}</b> × "
                    f"{int(order['qty'])} "
                    f"для <b>{h(order['recipient'])}</b>.",
                )
            except Exception:
                logger.exception(
                    "Не удалось уведомить пользователя "
                    "о выполнении заказа order_id=%s",
                    object_id,
                )

            base_text = (
                call.message.text
                or call.message.caption
                or f"Заказ #{object_id}"
            )

            _finish_admin_message(
                call,
                f"{base_text}\n\n📦 <b>Выдано администратором</b>",
            )
            return

        # ----------------------------------------------------
        # ОТМЕНА ЗАКАЗА И ВОЗВРАТ СРЕДСТВ
        # ----------------------------------------------------

        if action == "cancel_ord":
            try:
                result = shop.refund_wallet_order_once(
                    object_id,
                    uid,
                    "Отменено администратором",
                )

            except shop.ShopError as exc:
                return safe_answer(
                    call.id,
                    str(exc),
                    True,
                )

            except Exception:
                logger.exception(
                    "Ошибка возврата средств "
                    "order_id=%s admin_id=%s",
                    object_id,
                    uid,
                )
                return safe_answer(
                    call.id,
                    "Внутренняя ошибка обработки",
                    True,
                )

            if result.get("already_refunded"):
                return safe_answer(
                    call.id,
                    "Средства по заказу уже были возвращены",
                    True,
                )

            order = result["order"]

            safe_answer(
                call.id,
                "Заказ отменён, средства возвращены",
            )

            try:
                notify(
                    int(order["user_id"]),
                    f"↩️ <b>Заказ #{object_id} отменён</b>\n"
                    "Средства возвращены на ваш баланс.",
                )
            except Exception:
                logger.exception(
                    "Не удалось уведомить пользователя "
                    "о возврате order_id=%s",
                    object_id,
                )

            base_text = (
                call.message.text
                or call.message.caption
                or f"Заказ #{object_id}"
            )

            _finish_admin_message(
                call,
                f"{base_text}\n\n"
                "❌ <b>Заказ отменён и возвращён</b>",
            )
            return

        safe_answer(
            call.id,
            "Неизвестное действие",
            True,
        )