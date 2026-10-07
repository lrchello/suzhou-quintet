import io
import re
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from PIL import Image

from db import get_db
from ticketing import (
    BusinessError,
    confirm_order,
    create_order,
    redeem,
    refresh_expired,
    refund_order,
    remaining,
    report_payment,
)


def event(app, capacity=3, admission="paid", published=1, is_test=0):
    t = int(time.time())
    with app.app_context():
        db = get_db()
        cur = db.execute(
            """INSERT INTO events(title,starts_at,ends_at,venue,address,description,admission,
            price_cents,capacity,sales_end,sales_open,refund_policy,published,is_test)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "Test Event",
                t + 1800,
                t + 7200,
                "Test Venue",
                "Test Address",
                "No real payment",
                admission,
                6800 if admission == "paid" else 0,
                capacity,
                t + 1800,
                1,
                "Test refund policy",
                published,
                is_test,
            ),
        )
        db.commit()
        return cur.lastrowid


def signed_client(app, admin=False):
    client = app.test_client()
    with client.session_transaction() as session:
        session["csrf"] = "test-csrf"
    if admin:
        response = client.post(
            "/admin/login",
            data={"_csrf": "test-csrf", "username": "test-admin", "password": "long-test-password"},
        )
        assert response.status_code == 302
        with client.session_transaction() as session:
            session["csrf"] = "test-csrf"
    return client


def post(client, path, data=None, **kw):
    return client.post(path, data={"_csrf": "test-csrf", **(data or {})}, **kw)


def purchase(app, eid, quantity=1, key=None):
    with app.app_context():
        return create_order(
            eid, "Buyer", "Private Contact", quantity, key or secrets.token_hex(12), True
        )[0]


def pay(app, o, reference="receipt-test"):
    with app.app_context():
        report_payment(o["id"], "test-only payment")
        confirm_order(o["id"], 1, reference)


def test_public_pages_and_free_event(app):
    eid = event(app, admission="free")
    private = event(app, published=0, is_test=1)
    c = app.test_client()
    for path in [
        "/",
        "/events",
        "/archive",
        "/members",
        "/lookup",
        f"/events/{eid}",
        "/admin/login",
    ]:
        r = c.get(path)
        assert r.status_code == 200, path
    html = c.get(f"/events/{eid}").get_data(as_text=True)
    assert "免票入场" in html and 'name="quantity"' not in html
    assert c.get(f"/events/{private}").status_code == 404
    with app.app_context(), pytest.raises(BusinessError):
        create_order(eid, "buyer", "contact", 1, "free-no-order")


def test_real_browser_style_order_flow_and_recovery(app):
    eid = event(app)
    c = signed_client(app)
    c.get(f"/events/{eid}")
    with c.session_transaction() as session:
        key = session["checkout_keys"][str(eid)]
    form = dict(
        buyer="Tester",
        contact="private@example.invalid",
        quantity="2",
        request_key=key,
        agree="on",
        price="0.01",
    )
    r = post(c, f"/events/{eid}/orders", form)
    assert r.status_code == 200
    codes = re.findall(r"<code>([^<]+)</code>", r.get_data(as_text=True))
    oid, recovery = codes
    assert post(c, f"/events/{eid}/orders", form).status_code == 302
    with app.app_context():
        db = get_db()
        o = db.execute("SELECT * FROM orders WHERE public_id=?", (oid,)).fetchone()
        assert o["unit_cents"] == 6800 and o["quantity"] == 2
        assert db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1
        assert o["access_hash"] != recovery
    r = post(
        c,
        f"/orders/{oid}/report",
        {"paid": "on", "payment_note": "transfer test"},
        follow_redirects=True,
    )
    assert "待核对到账" in r.get_data(as_text=True)
    stranger = signed_client(app)
    assert stranger.get(f"/orders/{oid}").status_code == 302
    assert (
        post(stranger, f"/orders/{oid}/report", {"paid": "on", "payment_note": "bad"}).status_code
        == 403
    )
    lookup = post(stranger, "/lookup", {"public_id": oid, "access_key": recovery})
    assert lookup.status_code == 302
    assert stranger.get(f"/orders/{oid}").status_code == 200
    staff = signed_client(app, True)
    for _ in range(2):
        r = post(
            staff,
            f"/admin/orders/{o['id']}/confirm",
            {"verified": "on", "reference": "unique-test-receipt"},
            follow_redirects=True,
        )
        assert r.status_code == 200 and "电子票" in r.get_data(as_text=True)
    with app.app_context():
        tickets = get_db().execute("SELECT * FROM tickets").fetchall()
        assert len(tickets) == 2 and tickets[0]["code"] != tickets[1]["code"]
    assert c.get(f"/orders/{oid}/tickets/{tickets[0]['id']}.png").mimetype == "image/png"
    assert app.test_client().get(f"/orders/{oid}/tickets/{tickets[0]['id']}.png").status_code == 403


def test_last_seat_concurrent_requests(app):
    eid = event(app, capacity=1)

    def buy(n):
        try:
            purchase(app, eid, key=f"concurrent-{n}")
            return True
        except BusinessError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(buy, range(2)))
    assert sum(results) == 1
    with app.app_context():
        e = get_db().execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone()
        assert remaining(e) == 0


def test_late_payment_cannot_oversell(app):
    eid = event(app, capacity=1)
    first = purchase(app, eid)
    with app.app_context():
        db = get_db()
        db.execute("UPDATE orders SET expires_at=0 WHERE id=?", (first["id"],))
        db.commit()
        refresh_expired()
        report_payment(first["id"], "late transfer")
    second = purchase(app, eid)
    pay(app, second)
    with app.app_context():
        with pytest.raises(BusinessError, match="余票不足"):
            confirm_order(first["id"], 1, "late-receipt")
        assert get_db().execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 1


def test_review_window_not_extended_and_receipt_not_reusable(app):
    eid = event(app, capacity=3)
    one = purchase(app, eid)
    two = purchase(app, eid)
    with app.app_context():
        report_payment(one["id"], "test")
        db = get_db()
        first = db.execute("SELECT review_until FROM orders WHERE id=?", (one["id"],)).fetchone()[0]
        report_payment(one["id"], "updated note")
        assert (
            db.execute("SELECT review_until FROM orders WHERE id=?", (one["id"],)).fetchone()[0]
            == first
        )
        confirm_order(one["id"], 1, "same-receipt")
        report_payment(two["id"], "test")
        with pytest.raises(BusinessError, match="已用于"):
            confirm_order(two["id"], 1, "same-receipt")
        db.execute("UPDATE orders SET review_until=0 WHERE id=?", (two["id"],))
        db.commit()
        refresh_expired()
        assert (
            db.execute("SELECT status FROM orders WHERE id=?", (two["id"],)).fetchone()[0]
            == "expired"
        )


def test_double_redeem_refund_and_wrong_time(app):
    eid = event(app)
    one = purchase(app, eid)
    two = purchase(app, eid)
    pay(app, one, "r-one")
    pay(app, two, "r-two")
    with app.app_context():
        db = get_db()
        code = db.execute("SELECT code FROM tickets WHERE order_id=?", (one["id"],)).fetchone()[0]
        redeem(code, 1)
        with pytest.raises(BusinessError, match="已经核销"):
            redeem(code, 1)
        with pytest.raises(BusinessError, match="已核销"):
            refund_order(one["id"], 1, "refund-one")
        refund_order(two["id"], 1, "refund-two")
        other = db.execute("SELECT code FROM tickets WHERE order_id=?", (two["id"],)).fetchone()[0]
        with pytest.raises(BusinessError, match="已退款"):
            redeem(other, 1)
    third = purchase(app, eid)
    pay(app, third, "r-three")
    with app.app_context():
        db = get_db()
        db.execute("UPDATE events SET starts_at=? WHERE id=?", (int(time.time()) + 86400, eid))
        db.commit()
        code = db.execute("SELECT code FROM tickets WHERE order_id=?", (third["id"],)).fetchone()[0]
        with pytest.raises(BusinessError, match="验票时间"):
            redeem(code, 1)


def test_csrf_admin_protection_and_hidden_test(app):
    eid = event(app, published=0, is_test=1)
    c = app.test_client()
    assert c.get("/admin").status_code == 302
    assert (
        c.post(
            "/admin/login", data={"username": "test-admin", "password": "long-test-password"}
        ).status_code
        == 400
    )
    assert c.get(f"/events/{eid}").status_code == 404
    staff = signed_client(app, True)
    for path in [
        "/admin",
        "/admin/orders",
        "/admin/settings",
        "/admin/checkin",
        "/admin/events/new",
        f"/admin/events/{eid}/edit",
        "/admin/members/new",
        f"/events/{eid}",
    ]:
        r = staff.get(path)
        assert r.status_code == 200, path
    assert post(signed_client(app), "/admin/settings", {"about": "attack"}).status_code == 302


def test_configuration_required_and_no_contact_leak(app):
    eid = event(app)
    with app.app_context():
        db = get_db()
        db.execute("DELETE FROM settings WHERE key='payee'")
        db.commit()
        with pytest.raises(BusinessError, match="尚未配置"):
            create_order(eid, "buyer", "contact", 1, "config")
    html = app.test_client().get("/").get_data(as_text=True)
    assert "Private Contact" not in html


def test_calendar_encoding(app):
    eid = event(app, admission="free")
    c = app.test_client()
    with app.app_context():
        db = get_db()
        db.execute(
            "UPDATE events SET title=?,description=? WHERE id=?",
            ("爵士,音乐;现场", "长段落" * 100 + "\n第二行", eid),
        )
        db.commit()
    r = c.get(f"/events/{eid}/calendar.ics")
    assert r.status_code == 200 and b"BEGIN:VCALENDAR" in r.data
    assert all(len(line) <= 75 for line in r.data.split(b"\r\n"))
    assert "爵士\\,音乐\\;现场" in r.get_data(as_text=True)


def test_admin_content_edit_upload_and_untrusted_links(app):
    c = signed_client(app, True)
    r = post(
        c,
        "/admin/members/new",
        {
            "name": "<script>alert(1)</script>",
            "instrument": "Guitar",
            "sort_order": "1",
            "published": "on",
        },
    )
    assert r.status_code == 302
    html = c.get("/members").get_data(as_text=True)
    assert "&lt;script&gt;" in html and "<script>alert(1)</script>" not in html
    bad = post(
        c,
        "/admin/members/new",
        {"name": "Test", "instrument": "Drums", "sort_order": "1", "image": "javascript:alert(1)"},
    )
    assert bad.status_code == 200 and "https://" in bad.get_data(as_text=True)
    payload = io.BytesIO()
    Image.new("RGB", (100, 100), "blue").save(payload, format="PNG")
    payload.seek(0)
    r = post(
        c,
        "/admin/media",
        {"image_file": (payload, "image.png")},
        content_type="multipart/form-data",
    )
    assert r.status_code == 200 and "/uploads/" in r.get_data(as_text=True)
    assert c.get("/admin").status_code == 200


def test_valid_login_and_rate_limit(app):
    c = signed_client(app)
    r = post(c, "/admin/login", {"username": "test-admin", "password": "long-test-password"})
    assert r.status_code == 302 and c.get("/admin").status_code == 200
    other = signed_client(app)
    for _ in range(8):
        post(other, "/admin/login", {"username": "bad", "password": "bad"})
    assert post(other, "/admin/login", {"username": "bad", "password": "bad"}).status_code == 429


def test_admin_event_editor_and_inventory_lower_bound(app):
    c = signed_client(app, True)
    dt = datetime.now(timezone(timedelta(hours=8)))
    form = {
        "title": "Editable",
        "starts_at": (dt + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M"),
        "ends_at": (dt + timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M"),
        "sales_end": (dt + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"),
        "venue": "Test",
        "address": "Test Address",
        "admission": "paid",
        "price": "68",
        "capacity": "3",
        "refund_policy": "Test policy",
        "sales_open": "on",
        "published": "on",
    }
    r = post(c, "/admin/events/new", form)
    assert r.status_code == 302
    with app.app_context():
        eid = get_db().execute("SELECT id FROM events").fetchone()[0]
    o = purchase(app, eid, 2)
    r = post(c, f"/admin/events/{eid}/edit", {**form, "capacity": "1"})
    assert r.status_code == 200 and "容量不能低于" in r.get_data(as_text=True)
    r = post(c, f"/admin/events/{eid}/edit", {**form, "price": "88", "capacity": "3"})
    assert r.status_code == 302
    with app.app_context():
        db = get_db()
        assert (
            db.execute("SELECT unit_cents FROM orders WHERE id=?", (o["id"],)).fetchone()[0] == 6800
        )
        assert db.execute("SELECT price_cents FROM events WHERE id=?", (eid,)).fetchone()[0] == 8800
    r = post(c, f"/admin/events/{eid}/edit", {**form, "admission": "free"})
    assert r.status_code == 200 and "已有订单" in r.get_data(as_text=True)


def test_separate_event_checkout_keys(app):
    a = event(app)
    b = event(app)
    c = signed_client(app)
    c.get(f"/events/{a}")
    c.get(f"/events/{b}")
    with c.session_transaction() as session:
        keys = session["checkout_keys"]
        assert keys[str(a)] != keys[str(b)]
    for eid in (a, b):
        r = post(
            c,
            f"/events/{eid}/orders",
            {
                "buyer": "Buyer",
                "contact": "private",
                "quantity": "1",
                "agree": "on",
                "request_key": keys[str(eid)],
            },
        )
        assert r.status_code == 200 and "订单已创建" in r.get_data(as_text=True)
    with app.app_context():
        assert get_db().execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 2


def test_archive_media_and_external_tickets(app):
    past = event(app, admission="free")
    external = event(app, admission="external")
    with app.app_context():
        db = get_db()
        db.execute(
            "UPDATE events SET starts_at=1,ends_at=2,recap=?,setlist=?,gallery=?,video_url=? WHERE id=?",
            (
                "Test recap",
                "Test tune",
                "https://example.invalid/photo.png",
                "https://example.invalid/video",
                past,
            ),
        )
        db.execute(
            "UPDATE events SET external_url=? WHERE id=?",
            ("https://example.invalid/tickets", external),
        )
        db.commit()
    c = app.test_client()
    archive = c.get("/archive").get_data(as_text=True)
    assert f"/events/{past}" in archive
    detail = c.get(f"/events/{past}").get_data(as_text=True)
    assert all(
        x in detail
        for x in [
            "Test recap",
            "Test tune",
            "https://example.invalid/photo.png",
            "https://example.invalid/video",
        ]
    )
    ext = c.get(f"/events/{external}").get_data(as_text=True)
    assert "https://example.invalid/tickets" in ext and 'name="quantity"' not in ext
