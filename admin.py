import config
import database as db
import payments
import shop
from utils import bot, btn, fmt_dt, h, kb, notify, safe_answer, show, uzs


def is_admin(user_id: int | None) -> bool:
    if not user_id:
        return False
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return False
    return uid > 0 and uid in config.ADMIN_IDS


def send_topup_to_admins(topup: dict, user_obj) -> None:
    tid = int(topup["id"])
    uid = int(topup["user_id"])
    amt = int(topup["amount_uzs"])
    uname = f"@{user_obj.username}" if getattr(user_obj, "username", None) else "без username"
    caption = (
        f"💳 <b>Заявка на пополнение #{tid}</b>\n"
        f"Пользователь: <code>{uid}</code> ({h(uname)})\n"
        f"Сумма: <b>{uzs(amt)}</b>\n"
        f"Статус: ожидает проверки поступления средств в банке"
    )
    if topup.get("receipt_caption"):
        caption += f"\nКомментарий пользователя: {h(topup['receipt_caption'])}"

    markup = kb(
        [
            btn("✅ Подтвердить", f"adm:topup_ok:{tid}"),
            btn("❌ Отклонить", f"adm:topup_no:{tid}"),
        ]
    )

    file_id = topup.get("receipt_file_id")
    for admin_id in config.ADMIN_IDS:
        try:
            if file_id:
                bot.send_photo(admin_id, file_id, caption=caption, reply_markup=markup)
            else:
                bot.send_message(admin_id, caption, reply_markup=markup, disable_web_page_preview=True)
        except Exception:  # noqa: BLE001
            pass


def notify_admins_manual_order(order: dict, buyer_obj=None) -> None:
    oid = int(order["id"])
    uid = int(order["user_id"])
    buyer_label = f"id {uid}"
    if buyer_obj and getattr(buyer_obj, "username", None):
        buyer_label += f" (@{buyer_obj.username})"
    text = (
        f"🛠 <b>Заказ на ручную выдачу #{oid}</b>\n"
        f"Покупатель: {h(buyer_label)}\n"
        f"Получатель: <b>{h(order['recipient'])}</b>\n"
        f"Товар: <b>{h(order['product_title'])}</b> × {int(order['qty'])}\n"
        f"Сумма: <b>{uzs(int(order['total_uzs']))}</b>\n"
        f"Списано с баланса: {uzs(int(order['paid_from_balance_uzs']))} | Бонусами: {uzs(int(order['paid_from_bonus_uzs']))}"
    )
    markup = kb(
        [
            btn("📦 Выдано", f"adm:deliver:{oid}"),
            btn("❌ Отменить и вернуть", f"adm:cancel_ord:{oid}"),
        ]
    )
    for admin_id in config.ADMIN_IDS:
        try:
            bot.send_message(admin_id, text, reply_markup=markup, disable_web_page_preview=True)
        except Exception:  # noqa: BLE001
            pass


def handle_state(message, fsm: dict) -> None:
    uid = int(message.from_user.id)
    if not is_admin(uid):
        db.fsm_clear(uid)
        return
    state = fsm.get("state")
    data = fsm.get("data") or {}
    text = (message.text or "").strip()

    if state == "adm_reject_reason":
        tid = int(data.get("topup_id", 0))
        db.fsm_clear(uid)
        try:
            res = payments.reject_topup_once(tid, uid, text)
            bot.send_message(message.chat.id, f"Заявка #{tid}: {res['outcome']}")
        except shop.ShopError as exc:
            bot.send_message(message.chat.id, f"⚠️ {h(exc)}")
        return

    db.fsm_clear(uid)


def register() -> None:
    @bot.callback_query_handler(func=lambda c: (c.data or "").startswith("adm:"))
    def on_admin_callback(call):
        uid = int(call.from_user.id)
        if not is_admin(uid):
            return safe_answer(call.id, "Доступ запрещён", True)

        parts = call.data.split(":")
        action = parts[1] if len(parts) > 1 else ""

        if action == "topup_ok" and len(parts) > 2 and parts[2].isdigit():
            tid = int(parts[2])
            try:
                res = payments.approve_topup_once(tid, uid)
            except shop.ShopError as exc:
                return safe_answer(call.id, str(exc), True)

            outcome = res["outcome"]
            if outcome == "approved":
                safe_answer(call.id, "Пополнение подтверждено")
                base_txt = call.message.caption or call.message.text or f"Заявка #{tid}"
                show(
                    call,
                    f"{base_txt}\n\n✅ <b>Подтверждено администратором</b> ({fmt_dt(res['topup']['decided_at'])})",
                )
            elif outcome == "already_approved":
                safe_answer(call.id, "Заявка уже была подтверждена ранее", True)
            elif outcome == "already_rejected":
                safe_answer(call.id, "Заявка уже была отклонена ранее", True)
            else:
                safe_answer(call.id, "Заявка уже обработана", True)
            return

        if action == "topup_no" and len(parts) > 2 and parts[2].isdigit():
            tid = int(parts[2])
            try:
                res = payments.reject_topup_once(tid, uid, "Отклонено администратором")
            except shop.ShopError as exc:
                return safe_answer(call.id, str(exc), True)

            outcome = res["outcome"]
            if outcome == "rejected":
                safe_answer(call.id, "Заявка отклонена")
                base_txt = call.message.caption or call.message.text or f"Заявка #{tid}"
                show(
                    call,
                    f"{base_txt}\n\n❌ <b>Отклонено администратором</b> ({fmt_dt(res['topup']['decided_at'])})",
                )
            elif outcome == "already_approved":
                safe_answer(call.id, "Заявка уже была подтверждена ранее", True)
            else:
                safe_answer(call.id, "Заявка уже была отклонена ранее", True)
            return

        if action == "deliver" and len(parts) > 2 and parts[2].isdigit():
            oid = int(parts[2])
            try:
                order = shop.mark_wallet_order_delivered(oid, uid)
            except shop.ShopError as exc:
                return safe_answer(call.id, str(exc), True)
            safe_answer(call.id, "Заказ выполнен")
            notify(
                int(order["user_id"]),
                f"📦 <b>Заказ #{oid} выполнен!</b>\n"
                f"Товар: <b>{h(order['product_title'])}</b> × {int(order['qty'])} для <b>{h(order['recipient'])}</b>.",
            )
            base_txt = call.message.text or f"Заказ #{oid}"
            show(call, f"{base_txt}\n\n📦 <b>Выдано администратором</b>")
            return

        if action == "cancel_ord" and len(parts) > 2 and parts[2].isdigit():
            oid = int(parts[2])
            try:
                res = shop.refund_wallet_order_once(oid, uid, "Отменено администратором")
            except shop.ShopError as exc:
                return safe_answer(call.id, str(exc), True)
            order = res["order"]
            if res["already_refunded"]:
                return safe_answer(call.id, "Средства по заказу уже были возвращены", True)
            safe_answer(call.id, "Заказ отменён, средства возвращены")
            notify(
                int(order["user_id"]),
                f"↩️ <b>Заказ #{oid} отменён</b>\n"
                f"Средства возвращены на ваш баланс.",
            )
            base_txt = call.message.text or f"Заказ #{oid}"
            show(call, f"{base_txt}\n\n❌ <b>Заказ отменён и возвращён</b>")
            return

        safe_answer(call.id)