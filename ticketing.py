"""Business rules. Money is stored in integer cents, never floating point."""

import secrets
import time
from contextlib import contextmanager

from werkzeug.security import generate_password_hash

from db import get_db, settings

STATUS = {
    "pending": "等待付款",
    "review": "待核对到账",
    "paid": "已出票",
    "expired": "已超时／需人工处理",
    "cancelled": "已取消",
    "refunded": "已退款",
}


class BusinessError(ValueError):
    pass


def now():
    return int(time.time())


@contextmanager
def transaction():
    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise


def expire(db, timestamp):
    db.execute(
        "UPDATE orders SET status='expired' WHERE (status='pending' AND expires_at<=?) OR (status='review' AND review_until<=?)",
        (timestamp, timestamp),
    )


def refresh_expired():
    with transaction() as db:
        expire(db, now())


def remaining(event, timestamp=None):
    t = now() if timestamp is None else timestamp
    held = (
        get_db()
        .execute(
            """SELECT COALESCE(SUM(quantity),0) FROM orders WHERE event_id=? AND
        (status='paid' OR (status='pending' AND expires_at>?) OR (status='review' AND review_until>?))""",
            (event["id"], t, t),
        )
        .fetchone()[0]
    )
    return max(0, event["capacity"] - held)


def sale_reason(event, admin=False):
    cfg = settings()
    if event["admission"] != "paid":
        return "本场不在本站售票"
    if not event["published"] and not admin:
        return "本场尚未发布"
    if event["is_test"] and not admin:
        return "测试演出仅供管理员验证"
    if now() >= min(event["sales_end"], event["starts_at"]):
        return "已停止售票"
    if not event["sales_open"]:
        return "尚未开售"
    if not event["is_test"] and not all(cfg.get(k) for k in ("payment_image", "payee", "contact")):
        return "收款信息尚未配置，暂未开售"
    if not event["refund_policy"].strip():
        return "购票须知尚未配置"
    if remaining(event) < 1:
        return "已售罄"
    return ""


def create_order(event_id, buyer, contact, quantity, request_key, admin=False):
    # The browser sends a per-form idempotency key: retrying cannot reserve twice.
    key = secrets.token_urlsafe(24)
    with transaction() as db:
        t = now()
        expire(db, t)
        existing = db.execute("SELECT * FROM orders WHERE request_key=?", (request_key,)).fetchone()
        if existing:
            if existing["event_id"] != event_id:
                raise BusinessError("请重新打开当前演出页下单")
            return dict(existing), None
        event = db.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        if not event:
            raise BusinessError("演出不存在")
        reason = sale_reason(event, admin)
        if reason:
            raise BusinessError(reason)
        if quantity < 1 or quantity > 6:
            raise BusinessError("每单限购 1–6 张")
        if quantity > remaining(event, t):
            raise BusinessError("余票不足，请减少数量")
        public_id = secrets.token_hex(6).upper()
        expires = min(t + 20 * 60, event["sales_end"], event["starts_at"])
        cfg = settings()
        cur = db.execute(
            """INSERT INTO orders(public_id,access_hash,request_key,event_id,buyer,contact,quantity,
            unit_cents,status,created_at,expires_at,policy_snapshot,payee_snapshot,payment_image_snapshot) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                public_id,
                generate_password_hash(key),
                request_key,
                event_id,
                buyer,
                contact,
                quantity,
                event["price_cents"],
                "pending",
                t,
                expires,
                event["refund_policy"],
                cfg.get("payee", ""),
                cfg.get("payment_image", ""),
            ),
        )
        order = dict(db.execute("SELECT * FROM orders WHERE id=?", (cur.lastrowid,)).fetchone())
    return order, key


def report_payment(order_id, note):
    with transaction() as db:
        t = now()
        expire(db, t)
        row = db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if row["status"] not in ("pending", "review", "expired"):
            raise BusinessError("当前订单状态不支持提交付款信息")
        # Expired orders never silently reclaim stock. Staff must reconcile them.
        if row["status"] == "expired":
            db.execute(
                "UPDATE orders SET reported_at=?,payment_note=? WHERE id=?", (t, note, order_id)
            )
        elif row["status"] == "pending":
            db.execute(
                "UPDATE orders SET status='review',reported_at=?,payment_note=?,review_until=? WHERE id=?",
                (t, note, t + 24 * 3600, order_id),
            )
        else:
            db.execute("UPDATE orders SET payment_note=? WHERE id=?", (note, order_id))


def audit(db, admin_id, action, object_id, note=""):
    db.execute(
        "INSERT INTO audit(created_at,admin_id,action,object_id,note) VALUES(?,?,?,?,?)",
        (now(), admin_id, action, object_id, note),
    )


def confirm_order(order_id, admin_id, receipt):
    """Idempotent approval; a UNIQUE receipt cannot be reused for another order."""
    with transaction() as db:
        t = now()
        expire(db, t)
        row = db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if row["status"] == "paid":
            return False
        if row["status"] not in ("review", "expired") or not row["reported_at"]:
            raise BusinessError("只能处理已提交付款信息的订单")
        if not receipt.strip():
            raise BusinessError("请填写实际到账交易单号")
        if db.execute(
            "SELECT 1 FROM orders WHERE receipt_ref=? AND id<>?", (receipt, order_id)
        ).fetchone():
            raise BusinessError("此到账交易单号已用于其他订单，请重新核对")
        event = db.execute("SELECT * FROM events WHERE id=?", (row["event_id"],)).fetchone()
        if t >= event["ends_at"]:
            raise BusinessError("演出已结束，请走退款处理")
        if row["status"] == "expired" and remaining(event, t) < row["quantity"]:
            raise BusinessError("名额已释放且余票不足，请退款，不可强制出票")
        db.execute(
            "UPDATE orders SET status='paid',confirmed_at=?,receipt_ref=? WHERE id=?",
            (t, receipt, order_id),
        )
        for _ in range(row["quantity"]):
            db.execute(
                "INSERT INTO tickets(order_id,code) VALUES(?,?)",
                (order_id, secrets.token_hex(12).upper()),
            )
        audit(db, admin_id, "confirm_payment", order_id, receipt)
    return True


def cancel_order(order_id, admin_id):
    with transaction() as db:
        row = db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if row["status"] not in ("pending", "review", "expired"):
            raise BusinessError("当前订单不能取消")
        db.execute("UPDATE orders SET status='cancelled' WHERE id=?", (order_id,))
        audit(db, admin_id, "cancel_after_reconciliation", order_id)


def refund_order(order_id, admin_id, reference):
    with transaction() as db:
        row = db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if row["status"] not in ("paid", "expired", "review"):
            raise BusinessError("当前订单不支持退款记录")
        if not reference.strip():
            raise BusinessError("请先实际退款，再填写退款凭据")
        if db.execute(
            "SELECT 1 FROM tickets WHERE order_id=? AND used_at IS NOT NULL", (order_id,)
        ).fetchone():
            raise BusinessError("存在已核销票券，请先线下处理争议；本版不支持部分退款")
        db.execute(
            "UPDATE orders SET status='refunded',refunded_at=?,refund_ref=? WHERE id=?",
            (now(), reference, order_id),
        )
        audit(db, admin_id, "record_refund", order_id, reference)


def redeem(code, admin_id):
    with transaction() as db:
        t = now()
        row = db.execute(
            """SELECT t.*,o.status,e.starts_at,e.ends_at FROM tickets t
            JOIN orders o ON o.id=t.order_id JOIN events e ON e.id=o.event_id WHERE t.code=?""",
            (code,),
        ).fetchone()
        if not row:
            raise BusinessError("票码不存在")
        if row["status"] != "paid":
            raise BusinessError("订单无效或已退款")
        if row["used_at"]:
            raise BusinessError("此票已经核销，请勿重复放行")
        if t < row["starts_at"] - 7200 or t > row["ends_at"]:
            raise BusinessError("尚未到验票时间或演出已结束（开演前两小时至结束可验票）")
        db.execute(
            "UPDATE tickets SET used_at=?,used_by=? WHERE id=? AND used_at IS NULL",
            (t, admin_id, row["id"]),
        )
        audit(db, admin_id, "redeem_ticket", row["id"])
