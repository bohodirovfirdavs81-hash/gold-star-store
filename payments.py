import time
from typing import Any

from telebot import types

import config
import database as db
from shop import ShopError
from utils import bot, h, notify, uzs


def get_wallet_profile(user_id: int) -> dict[str, Any]:
    uid = int(user_id)
    with db.tx() as conn:
        user = conn.execute("SELECT * FROM users WHERE telegram_id = ?", (uid,)).fetchone()
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


def list_wallet_transactions(user_id: int, limit: int = 50) -> list[dict[str, Any]]:
    return db.q_all(
        """
        SELECT * FROM wallet_transactions
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (int(user_id), int(limit)),
    )


def create_topup_request(user_id: int, amount_uzs: int) -> dict[str, Any]:
    uid = int(user_id)
    try:
        amt = int(amount_uzs)
    except (TypeError, ValueError) as exc:
        raise ShopError("Введите корректную целую сумму в UZS") from exc

    if amt < config.TOPUP_MIN_UZS:
        raise ShopError(f"Минимальная сумма пополнения: {uzs(config.TOPUP_MIN_UZS)}")
    if amt > config.TOPUP_MAX_UZS:
        raise ShopError(f"Максимальная сумма пополнения: {uzs(config.TOPUP_MAX_UZS)}")

    now = int(time.time())
    with db.tx() as conn:
        db.ensure_wallet(conn, uid)
        pending_count = conn.execute(
            "SELECT COUNT(*) AS c FROM topups WHERE user_id = ? AND status = 'pending'",
            (uid,),
        ).fetchone()["c"]
        if int(pending_count) >= 5:
            raise ShopError("У вас уже есть активные необработанные заявки на пополнение. Дождитесь решения администратора.")

        cur = conn.execute(
            """
            INSERT INTO topups (user_id, amount_uzs, requisites, status, created_at, updated_at)
            VALUES (?, ?, ?, 'pending', ?, ?)
            """,
            (uid, amt, config.TOPUP_REQUISITES, now, now),
        )
        tid = int(cur.lastrowid)
        row = conn.execute("SELECT * FROM topups WHERE id = ?", (tid,)).fetchone()
        topup = dict(row)

    # Бот также отправляет реквизиты пользователю в личный чат (если чат открыт)
    notify(
        uid,
        f"💳 <b>Заявка на пополнение #{tid} создана</b>\n"
        f"Сумма: <b>{uzs(amt)}</b>\n\n"
        f"Реквизиты для перевода:\n<code>{h(config.TOPUP_REQUISITES)}</code>\n\n"
        f"После перевода отправьте фото или скриншот чека в этот чат.",
    )
    return topup


def list_topups(user_id: int, limit: int = 30) -> list[dict[str, Any]]:
    return db.q_all(
        """
        SELECT * FROM topups
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (int(user_id), int(limit)),
    )


def get_topup(topup_id: int) -> dict[str, Any] | None:
    return db.q_one("SELECT * FROM topups WHERE id = ?", (int(topup_id),))


def get_latest_pending_topup(user_id: int) -> dict[str, Any] | None:
    return db.q_one(
        """
        SELECT * FROM topups
        WHERE user_id = ? AND status = 'pending'
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
    now = int(time.time())
    with db.tx() as conn:
        row = conn.execute(
            "SELECT * FROM topups WHERE id = ? AND user_id = ?",
            (int(topup_id), int(user_id)),
        ).fetchone()
        if not row:
            raise ShopError("Заявка на пополнение не найдена")
        if row["status"] != "pending":
            raise ShopError("По этой заявке уже принято решение")
        conn.execute(
            """
            UPDATE topups
            SET receipt_file_id = ?, receipt_caption = ?, updated_at = ?
            WHERE id = ? AND status = 'pending'
            """,
            (file_id, (caption or "")[:500], now, int(topup_id)),
        )
        updated = conn.execute("SELECT * FROM topups WHERE id = ?", (int(topup_id),)).fetchone()
        return dict(updated)


def approve_topup_once(topup_id: int, admin_id: int, comment: str = "") -> dict[str, Any]:
    """Подтверждает заявку и зачисляет средства ровно один раз."""
    now = int(time.time())
    with db.tx() as conn:
        row = conn.execute("SELECT * FROM topups WHERE id = ?", (int(topup_id),)).fetchone()
        if not row:
            raise ShopError("Заявка не найдена")
        status = row["status"]
        if status == "approved":
            return {"outcome": "already_approved", "topup": dict(row)}
        if status == "rejected":
            return {"outcome": "already_rejected", "topup": dict(row)}
        if status != "pending":
            raise ShopError(f"Некорректный статус заявки: {status}")

        uid = int(row["user_id"])
        amt = int(row["amount_uzs"])
        wallet = db.ensure_wallet(conn, uid)
        new_bal = int(wallet["balance_uzs"]) + amt
        new_bonus = int(wallet["bonus_uzs"])

        upd = conn.execute(
            """
            UPDATE topups
            SET status = 'approved', admin_id = ?, admin_comment = ?, updated_at = ?, decided_at = ?
            WHERE id = ? AND status = 'pending'
            """,
            (int(admin_id), (comment or "Подтверждено администратором")[:300], now, now, int(topup_id)),
        )
        if upd.rowcount != 1:
            latest = conn.execute("SELECT * FROM topups WHERE id = ?", (int(topup_id),)).fetchone()
            return {"outcome": "already_processed", "topup": dict(latest)}

        conn.execute(
            """
            UPDATE wallets
            SET balance_uzs = balance_uzs + ?, updated_at = ?
            WHERE user_id = ?
            """,
            (amt, now, uid),
        )
        conn.execute(
            """
            INSERT INTO wallet_transactions (
                user_id, kind, amount_uzs, bonus_delta_uzs,
                balance_after_uzs, bonus_after_uzs, ref_type, ref_id, note, created_at
            ) VALUES (?, 'topup', ?, 0, ?, ?, 'topup', ?, ?, ?)
            """,
            (uid, amt, new_bal, new_bonus, str(topup_id), f"Пополнение баланса по заявке #{topup_id}", now),
        )
        conn.execute(
            """
            INSERT INTO admin_audit_log (admin_id, action, target_type, target_id, details, created_at)
            VALUES (?, 'approve_topup', 'topup', ?, ?, ?)
            """,
            (int(admin_id), str(topup_id), f"Зачислено {amt} UZS. {comment}".strip(), now),
        )
        updated = conn.execute("SELECT * FROM topups WHERE id = ?", (int(topup_id),)).fetchone()

    notify(
        uid,
        f"✅ <b>Пополнение подтверждено!</b>\n"
        f"Заявка <b>#{topup_id}</b> на сумму <b>{uzs(amt)}</b> зачислена на ваш баланс.\n"
        f"Текущий баланс: <b>{uzs(new_bal)}</b>.",
    )
    return {"outcome": "approved", "topup": dict(updated), "balance_after_uzs": new_bal}


def reject_topup_once(topup_id: int, admin_id: int, comment: str = "") -> dict[str, Any]:
    """Отклоняет заявку без изменения баланса."""
    now = int(time.time())
    with db.tx() as conn:
        row = conn.execute("SELECT * FROM topups WHERE id = ?", (int(topup_id),)).fetchone()
        if not row:
            raise ShopError("Заявка не найдена")
        status = row["status"]
        if status == "approved":
            return {"outcome": "already_approved", "topup": dict(row)}
        if status == "rejected":
            return {"outcome": "already_rejected", "topup": dict(row)}

        upd = conn.execute(
            """
            UPDATE topups
            SET status = 'rejected', admin_id = ?, admin_comment = ?, updated_at = ?, decided_at = ?
            WHERE id = ? AND status = 'pending'
            """,
            (int(admin_id), (comment or "Отклонено администратором")[:300], now, now, int(topup_id)),
        )
        if upd.rowcount != 1:
            latest = conn.execute("SELECT * FROM topups WHERE id = ?", (int(topup_id),)).fetchone()
            return {"outcome": "already_processed", "topup": dict(latest)}

        conn.execute(
            """
            INSERT INTO admin_audit_log (admin_id, action, target_type, target_id, details, created_at)
            VALUES (?, 'reject_topup', 'topup', ?, ?, ?)
            """,
            (int(admin_id), str(topup_id), comment or "Отклонено администратором", now),
        )
        updated = conn.execute("SELECT * FROM topups WHERE id = ?", (int(topup_id),)).fetchone()

    uid = int(row["user_id"])
    amt = int(row["amount_uzs"])
    reason_txt = f"\nПричина: {h(comment)}" if comment else ""
    notify(
        uid,
        f"❌ <b>Заявка #{topup_id} отклонена</b>\n"
        f"Сумма: <b>{uzs(amt)}</b>. Баланс не изменён.{reason_txt}",
    )
    return {"outcome": "rejected", "topup": dict(updated)}


# --- Проверка и запись Telegram Stars платежей ---
def send_order_invoice(chat_id: int, order: dict[str, Any]) -> None:
    total = int(order.get("total", 0))
    if total <= 0:
        raise ShopError("Некорректная сумма заказа")
    prices = [types.LabeledPrice(label=f"Заказ #{order['id']}", amount=total)]
    bot.send_invoice(
        chat_id=chat_id,
        title=f"Заказ #{order['id']} — {config.BRAND}",
        description=f"Получатель: {order.get('recipient', '')}",
        invoice_payload=f"order:{order['id']}:{order['user_id']}",
        provider_token="",
        currency="XTR",
        prices=prices,
    )


def handle_pre_checkout(query: types.PreCheckoutQuery) -> None:
    try:
        parts = (query.invoice_payload or "").split(":")
        order_id = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 0
        expected_uid = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else 0
        order = db.q_one("SELECT * FROM legacy_orders WHERE id = ?", (order_id,))
        if (
            not order
            or int(order["user_id"]) != int(query.from_user.id)
            or (expected_uid and int(order["user_id"]) != expected_uid)
            or order["currency"] != "XTR"
            or query.currency != "XTR"
            or int(order["total_amount"]) != int(query.total_amount)
            or order["status"] != "pending"
        ):
            bot.answer_pre_checkout_query(query.id, ok=False, error_message="Заказ недействителен или уже оплачен")
            return
        bot.answer_pre_checkout_query(query.id, ok=True)
    except Exception:  # noqa: BLE001
        try:
            bot.answer_pre_checkout_query(query.id, ok=False, error_message="Ошибка проверки платежа")
        except Exception:  # noqa: BLE001
            pass


def record_payment(
    user_id: int,
    currency: str,
    total_amount: int,
    invoice_payload: str,
    telegram_charge_id: str,
    provider_charge_id: str = "",
) -> dict[str, Any]:
    now = int(time.time())
    charge_id = (telegram_charge_id or "").strip()
    if not charge_id:
        return {"result": "review", "problem": "missing_charge_id", "order": None, "payment": None}

    order_id = 0
    payload_uid = 0
    parts = (invoice_payload or "").split(":")
    if len(parts) >= 2 and parts[1].isdigit():
        order_id = int(parts[1])
    if len(parts) >= 3 and parts[2].isdigit():
        payload_uid = int(parts[2])

    with db.tx() as conn:
        dup = conn.execute(
            "SELECT id FROM payments WHERE telegram_charge_id = ?",
            (charge_id,),
        ).fetchone()
        if dup:
            return {"result": "duplicate"}

        order_row = conn.execute("SELECT * FROM legacy_orders WHERE id = ?", (order_id,)).fetchone()
        problem = None
        if not order_row:
            problem = "order_not_found"
        elif int(order_row["user_id"]) != int(user_id):
            problem = "user_mismatch"
        elif payload_uid and int(user_id) != payload_uid:
            problem = "payload_user_mismatch"
        elif currency != "XTR":
            problem = f"currency_mismatch:{currency}"
        elif int(order_row["total_amount"]) != int(total_amount):
            problem = f"amount_mismatch:{total_amount}!={order_row['total_amount']}"
        elif order_row["status"] != "pending":
            problem = f"invalid_order_status:{order_row['status']}"

        status = "paid" if not problem else "review"
        cur = conn.execute(
            """
            INSERT INTO payments (
                order_id, user_id, currency, amount, payload,
                telegram_charge_id, provider_charge_id, status, problem, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order_id,
                int(user_id),
                currency,
                int(total_amount),
                invoice_payload,
                charge_id,
                provider_charge_id,
                status,
                problem,
                now,
            ),
        )
        pay_id = int(cur.lastrowid)
        if not problem and order_row:
            conn.execute(
                "UPDATE legacy_orders SET status = 'paid', updated_at = ? WHERE id = ? AND status = 'pending'",
                (now, order_id),
            )

    order_full = db.q_one(
        "SELECT id, user_id, recipient, total_amount AS total, currency, status FROM legacy_orders WHERE id = ?",
        (order_id,),
    )
    if order_full:
        order_full["items"] = db.q_all(
            "SELECT title, price, qty FROM legacy_order_items WHERE order_id = ?",
            (order_id,),
        )
    pay_full = db.q_one("SELECT * FROM payments WHERE id = ?", (pay_id,))
    return {
        "result": "paid" if not problem else "review",
        "problem": problem,
        "order": order_full,
        "payment": pay_full,
    }