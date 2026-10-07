"""SUZHOU QUINTET — Flask routes and form validation.

Run setup.py once, then: python -m flask --app app run
All mutations use POST + CSRF. User content is escaped by Jinja.
"""

import io
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode, urlsplit

import click
import qrcode
from flask import (
    Flask,
    Response,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    send_file,
    send_from_directory,
    session,
    url_for,
)
from werkzeug.exceptions import SecurityError, ServiceUnavailable
from werkzeug.security import check_password_hash, generate_password_hash

from config import configure
from db import close_db, get_db, init_db, settings
from gallery_uploads import read_gallery_selection, save_gallery_uploads
from maintenance import register_commands
from public_pages import register_public_pages
from security import (
    DUMMY_PASSWORD_HASH,
    admin_required,
    csrf_token,
    end_admin_session,
    limit,
    protect_request,
    start_admin_session,
)
from ticketing import (
    STATUS,
    BusinessError,
    audit,
    cancel_order,
    confirm_order,
    create_order,
    now,
    redeem,
    refresh_expired,
    refund_order,
    remaining,
    report_payment,
    sale_reason,
    transaction,
)

CHINA = timezone(timedelta(hours=8))


def create_app(test_config=None):
    app = Flask(
        __name__,
        instance_relative_config=True,
        instance_path=os.environ.get("SQ_INSTANCE_PATH") or None,
    )
    configure(app, test_config)
    app.teardown_appcontext(close_db)
    with app.app_context():
        init_db()
    app.before_request(protect_request)

    @app.teardown_request
    def discard_failed_uploads(error=None):
        for path in g.get("pending_uploads", []):
            path.unlink(missing_ok=True)

    @app.after_request
    def headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' https: data: blob:; style-src 'self'; script-src 'self'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'; object-src 'none'"
        )
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if response.status_code >= 400 or not request.path.startswith(("/static/", "/uploads/")):
            response.headers["Cache-Control"] = "no-store"
        if request.path.startswith(("/admin", "/orders", "/lookup", "/healthz")) or g.get("admin"):
            response.headers["X-Robots-Tag"] = "noindex, nofollow"
        if app.config["SESSION_COOKIE_SECURE"]:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    @app.context_processor
    def shared():
        try:
            cfg = settings()
        except sqlite3.OperationalError:
            cfg = {}
        return dict(
            cfg=cfg,
            csrf_token=csrf_token,
            clock=now(),
            status_labels=STATUS,
            admin=g.get("admin"),
            remaining=remaining,
            sale_reason=sale_reason,
            map_link=map_link,
        )

    @app.template_filter("when")
    def when(value, fmt="%Y.%m.%d %H:%M"):
        return datetime.fromtimestamp(value, CHINA).strftime(fmt) if value else "—"

    @app.template_filter("money")
    def money(value):
        return f"{value / 100:.2f}"

    @app.template_filter("local_input")
    def local_input(value):
        return when(value, "%Y-%m-%dT%H:%M") if value else ""

    def text(name, maximum=3000, required=False):
        value = request.form.get(name, "").strip()
        if required and not value:
            raise BusinessError("请填写所有必填项")
        if len(value) > maximum:
            raise BusinessError(f"{name} 内容过长，最多 {maximum} 字")
        return value

    def integer(name, minimum=0, maximum=100000):
        try:
            value = int(request.form.get(name, ""))
        except ValueError:
            raise BusinessError("请输入有效整数")
        if not minimum <= value <= maximum:
            raise BusinessError("数值超出允许范围")
        return value

    def date(name):
        try:
            return int(
                datetime.strptime(request.form.get(name, ""), "%Y-%m-%dT%H:%M")
                .replace(tzinfo=CHINA)
                .timestamp()
            )
        except ValueError:
            raise BusinessError("请填写有效日期和时间（北京时间）")

    def checked(name):
        return int(request.form.get(name) == "on")

    def link(value, local=False):
        if not value:
            return ""
        if any(char.isspace() or ord(char) < 32 or char == "\\" for char in value):
            raise BusinessError("链接不能包含空白、控制字符或反斜线")
        if local and value.startswith("/uploads/"):
            from gallery_uploads import valid_filename

            if valid_filename(value[len("/uploads/") :]):
                return value
            raise BusinessError("本地图片路径无效")
        try:
            url = urlsplit(value)
        except ValueError:
            raise BusinessError("链接格式无效，请填写完整 https:// 地址")
        if url.scheme != "https" or not url.hostname or url.username or url.password:
            raise BusinessError("外部链接必须使用完整 https:// 地址")
        return value

    def image_upload(name, existing=""):
        file = request.files.get(name)
        if not file or not file.filename:
            return existing
        urls, paths = save_gallery_uploads([file], app.config["UPLOAD_DIR"])
        g.pending_uploads.extend(paths)
        return urls[0]

    def event_or_404(event_id):
        event = get_db().execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        if not event or ((not event["published"] or event["is_test"]) and not g.admin):
            abort(404)
        return event

    def owned_order(public_id):
        if not g.admin and public_id not in session.get("order_access", []):
            return None
        order = (
            get_db()
            .execute(
                "SELECT o.*,e.title,e.starts_at,e.ends_at,e.venue,e.address,e.is_test FROM orders o JOIN events e ON e.id=o.event_id WHERE o.public_id=?",
                (public_id,),
            )
            .fetchone()
        )
        if not order:
            abort(404)
        return order

    def grant(public_id):
        session["order_access"] = (session.get("order_access", []) + [public_id])[-15:]
        session.permanent = True

    @app.route("/")
    def home():
        db = get_db()
        upcoming = db.execute(
            "SELECT * FROM events WHERE published=1 AND is_test=0 AND ends_at>? ORDER BY starts_at LIMIT 1",
            (now(),),
        ).fetchone()
        past = db.execute(
            "SELECT * FROM events WHERE published=1 AND is_test=0 AND ends_at<=? ORDER BY starts_at DESC LIMIT 3",
            (now(),),
        ).fetchall()
        members = db.execute(
            "SELECT * FROM members WHERE published=1 ORDER BY sort_order,id LIMIT 5"
        ).fetchall()
        return render_template("home.html", upcoming=upcoming, past=past, members=members)

    @app.route("/events")
    def events():
        rows = (
            get_db()
            .execute(
                "SELECT * FROM events WHERE published=1 AND is_test=0 AND ends_at>? ORDER BY starts_at",
                (now(),),
            )
            .fetchall()
        )
        return render_template("events.html", events=rows, past=False)

    @app.route("/archive")
    def archive():
        rows = (
            get_db()
            .execute(
                "SELECT * FROM events WHERE published=1 AND is_test=0 AND ends_at<=? ORDER BY starts_at DESC",
                (now(),),
            )
            .fetchall()
        )
        return render_template("events.html", events=rows, past=True)

    @app.route("/members")
    def members():
        return render_template(
            "members.html",
            members=get_db()
            .execute("SELECT * FROM members WHERE published=1 ORDER BY sort_order,id")
            .fetchall(),
        )

    @app.route("/events/<int:event_id>")
    def event_detail(event_id):
        e = event_or_404(event_id)
        g.public_event = bool(e["published"] and not e["is_test"])
        lineup = (
            get_db()
            .execute(
                "SELECT m.* FROM members m JOIN event_members em ON em.member_id=m.id WHERE em.event_id=? AND m.published=1 ORDER BY m.sort_order,m.id",
                (event_id,),
            )
            .fetchall()
        )
        keys = session.get("checkout_keys", {})
        keys.setdefault(str(event_id), secrets.token_urlsafe(24))
        session["checkout_keys"] = dict(list(keys.items())[-15:])
        return render_template("event.html", e=e, lineup=lineup, checkout_key=keys[str(event_id)])

    @app.route("/events/<int:event_id>/calendar.ics")
    def calendar(event_id):
        e = event_or_404(event_id)

        def escape(v):
            return (
                v.replace("\\", "\\\\")
                .replace("\r", "")
                .replace("\n", "\\n")
                .replace(";", "\\;")
                .replace(",", "\\,")
            )

        # Only the confirmed start time is exported. End time is an operational cutoff.
        lines = [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//Suzhou Quintet//Events//ZH",
            "BEGIN:VEVENT",
            f"UID:event-{e['id']}@suzhou-quintet",
            "DTSTAMP:" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
            "DTSTART:"
            + datetime.fromtimestamp(e["starts_at"], timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
            "SUMMARY:" + escape(e["title"]),
            "LOCATION:" + escape(e["venue"] + " " + e["address"]),
            "DESCRIPTION:" + escape(e["description"]),
            "END:VEVENT",
            "END:VCALENDAR",
        ]
        # RFC 5545 line folding at <=75 UTF-8 octets without splitting a code point.
        folded = []
        for line in lines:
            chunk = ""
            for char in line:
                if len((chunk + char).encode("utf-8")) > 74:
                    folded.append(chunk)
                    chunk = " "
                chunk += char
            folded.append(chunk)
        return Response(
            "\r\n".join(folded) + "\r\n",
            mimetype="text/calendar",
            headers={"Content-Disposition": f'attachment; filename="event-{event_id}.ics"'},
        )

    @app.route("/events/<int:event_id>/orders", methods=["POST"])
    def checkout(event_id):
        event_or_404(event_id)
        limit("checkout", 12)
        try:
            key = text("request_key", 100, True)
            if key != session.get("checkout_keys", {}).get(str(event_id)):
                raise BusinessError("请重新打开演出页再下单")
            if not checked("agree"):
                raise BusinessError("请先阅读并同意购票须知")
            buyer = text("buyer", 60, True)
            contact = text("contact", 120, True)
            quantity = integer("quantity", 1, 6)
            order, recovery = create_order(event_id, buyer, contact, quantity, key, bool(g.admin))
            grant(order["public_id"])
            if recovery:
                return render_template("created.html", order=order, recovery=recovery)
            return redirect(url_for("order_detail", public_id=order["public_id"]))
        except BusinessError as exc:
            flash(str(exc), "error")
            return redirect(url_for("event_detail", event_id=event_id))

    @app.route("/new-checkout", methods=["POST"])
    def new_checkout():
        session.pop("checkout_keys", None)
        return redirect(url_for("events"))

    @app.route("/lookup", methods=["GET", "POST"])
    def lookup():
        if request.method == "POST":
            limit("lookup", 12)
            row = (
                get_db()
                .execute(
                    "SELECT * FROM orders WHERE public_id=?",
                    (request.form.get("public_id", "").strip().upper()[:30],),
                )
                .fetchone()
            )
            valid = check_password_hash(
                row["access_hash"] if row else DUMMY_PASSWORD_HASH,
                request.form.get("access_key", "")[:200],
            )
            if row and valid:
                grant(row["public_id"])
                return redirect(url_for("order_detail", public_id=row["public_id"]))
            flash("订单号或查询密钥不正确。", "error")
        return render_template("lookup.html")

    @app.route("/orders/<public_id>")
    def order_detail(public_id):
        refresh_expired()
        row = owned_order(public_id)
        if row is None:
            return redirect(url_for("lookup"))
        tickets = (
            get_db()
            .execute("SELECT * FROM tickets WHERE order_id=? ORDER BY id", (row["id"],))
            .fetchall()
        )
        return render_template("order.html", o=row, tickets=tickets)

    @app.route("/orders/<public_id>/report", methods=["POST"])
    def payment_report(public_id):
        row = owned_order(public_id)
        if row is None:
            abort(403)
        limit("report", 20)
        try:
            if not checked("paid"):
                raise BusinessError("实际付款后才可提交")
            report_payment(row["id"], text("payment_note", 300, True))
            flash("付款信息已提交。请等待人工核对，不要重复付款。", "success")
        except BusinessError as exc:
            flash(str(exc), "error")
        return redirect(url_for("order_detail", public_id=public_id))

    @app.route("/orders/<public_id>/tickets/<int:ticket_id>.png")
    def ticket_qr(public_id, ticket_id):
        row = owned_order(public_id)
        if row is None:
            abort(403)
        ticket = (
            get_db()
            .execute("SELECT * FROM tickets WHERE id=? AND order_id=?", (ticket_id, row["id"]))
            .fetchone()
        )
        if not ticket or row["status"] != "paid":
            abort(404)
        # A plain random code works without a public domain and reveals no contact data.
        image = qrcode.make(ticket["code"])
        data = io.BytesIO()
        image.save(data, format="PNG")
        data.seek(0)
        return send_file(data, mimetype="image/png", max_age=0)

    @app.route("/uploads/<filename>")
    def upload_file(filename):
        from gallery_uploads import valid_filename

        if not valid_filename(filename):
            abort(404)
        path = "/uploads/" + filename
        db = get_db()
        public = db.execute(
            "SELECT 1 FROM members WHERE published=1 AND image=?", (path,)
        ).fetchone()
        if not public:
            public = db.execute(
                """SELECT 1 FROM events WHERE published=1 AND is_test=0
                AND (cover=? OR instr(char(10)||gallery||char(10),char(10)||?||char(10))>0)""",
                (path, path),
            ).fetchone()
        permitted = bool(public or g.admin)
        if not permitted:
            ids = session.get("order_access", [])
            if ids:
                permitted = bool(
                    db.execute(
                        "SELECT 1 FROM orders WHERE payment_image_snapshot=? AND public_id IN ("
                        + ",".join("?" for _ in ids)
                        + ")",
                        (path, *ids),
                    ).fetchone()
                )
        if not permitted:
            abort(404)
        response = send_from_directory(app.config["UPLOAD_DIR"], filename, max_age=0)
        if not public:
            response.headers["Cache-Control"] = "private, no-store"
            response.headers["X-Robots-Tag"] = "noindex, nofollow"
        return response

    @app.route("/admin/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            limit("login", 8, 900)
            username = request.form.get("username", "").strip()[:80]
            limit("login-account", 20, 3600, identity=username.casefold())
            row = get_db().execute("SELECT * FROM admins WHERE username=?", (username,)).fetchone()
            valid = check_password_hash(
                row["password_hash"] if row else DUMMY_PASSWORD_HASH,
                request.form.get("password", "")[:300],
            )
            if row and valid:
                start_admin_session(row["id"])
                next_url = request.args.get("next", "")
                if (
                    not next_url.startswith("/admin")
                    or next_url.startswith("//")
                    or "\\" in next_url
                ):
                    next_url = url_for("admin_home")
                return redirect(next_url)
            flash("用户名或密码错误。", "error")
        return render_template("login.html")

    @app.route("/admin/logout", methods=["POST"])
    def logout():
        end_admin_session()
        return redirect(url_for("home"))

    @app.route("/admin")
    @admin_required
    def admin_home():
        refresh_expired()
        db = get_db()
        rows = db.execute("""SELECT e.*,COALESCE((SELECT SUM(quantity) FROM orders WHERE event_id=e.id AND status='paid'),0) AS sold,
          (SELECT COUNT(*) FROM orders WHERE event_id=e.id AND status='review') AS reviewing,
          (SELECT COUNT(*) FROM tickets t JOIN orders o ON o.id=t.order_id WHERE o.event_id=e.id AND t.used_at IS NOT NULL) AS used
          FROM events e ORDER BY starts_at DESC""").fetchall()
        members = db.execute("SELECT * FROM members ORDER BY sort_order,id").fetchall()
        logs = db.execute(
            "SELECT a.*,u.username FROM audit a LEFT JOIN admins u ON u.id=a.admin_id ORDER BY a.id DESC LIMIT 20"
        ).fetchall()
        return render_template("admin.html", events=rows, members=members, logs=logs)

    @app.route("/admin/events/new", methods=["GET", "POST"])
    @app.route("/admin/events/<int:event_id>/edit", methods=["GET", "POST"])
    @admin_required
    def edit_event(event_id=None):
        db = get_db()
        e = (
            db.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
            if event_id
            else None
        )
        if event_id and not e:
            abort(404)
        if request.method == "POST":
            new_gallery_paths = []
            gallery_saved = False
            try:
                admission = text("admission", 15, True)
                if admission not in ("free", "paid", "external"):
                    raise BusinessError("入场方式无效")
                price = Decimal(text("price", 12) or "0")
                if (
                    not price.is_finite()
                    or price < 0
                    or price > 100000
                    or price * 100 != (price * 100).to_integral_value()
                ):
                    raise BusinessError("票价须为最多两位小数的非负金额")
                starts = date("starts_at")
                ends = date("ends_at")
                cutoff = date("sales_end")
                if ends <= starts:
                    raise BusinessError("归档／验票截止时间必须晚于开演")
                if cutoff > starts:
                    raise BusinessError("停止售票时间不可晚于开演")
                cap = integer("capacity", 0, 100000)
                if admission == "paid" and (price <= 0 or cap < 1):
                    raise BusinessError("收费演出必须设置正数票价和容量")
                external = link(text("external_url", 1000))
                if admission == "external" and not external:
                    raise BusinessError("请填写外部售票链接")
                policy = text("refund_policy", 3000)
                if admission == "paid" and not policy:
                    raise BusinessError("收费演出必须填写退票和购票须知")
                selected = [int(x) for x in request.form.getlist("member_ids")]
                gallery = read_gallery_selection(e["gallery"] if e else "", request.form, link)
                values = dict(
                    title=text("title", 150, True),
                    starts_at=starts,
                    ends_at=ends,
                    venue=text("venue", 120, True),
                    address=text("address", 300, True),
                    description=text("description", 6000),
                    admission=admission,
                    price_cents=int(price * 100) if admission == "paid" else 0,
                    capacity=cap,
                    sales_end=cutoff,
                    sales_open=checked("sales_open"),
                    external_url=external,
                    refund_policy=policy,
                    cover=image_upload("cover_file", link(text("cover", 1000), True)),
                    recap=text("recap", 10000),
                    setlist=text("setlist", 3000),
                    gallery=gallery,
                    video_url=link(text("video_url", 1000)),
                    published=checked("published"),
                    is_test=checked("is_test"),
                )
                added_gallery, new_gallery_paths = save_gallery_uploads(
                    request.files.getlist("gallery_files"), app.config["UPLOAD_DIR"]
                )
                values["gallery"] = "\n".join(part for part in [gallery, *added_gallery] if part)
                with transaction() as tx:
                    if e:
                        e = tx.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
                        count = tx.execute(
                            "SELECT COUNT(*) FROM orders WHERE event_id=?", (event_id,)
                        ).fetchone()[0]
                        if count and (
                            e["admission"] != admission or e["is_test"] != values["is_test"]
                        ):
                            raise BusinessError("已有订单的演出不能改变入场方式或测试属性")
                        if admission == "paid" and cap < e["capacity"] - remaining(e):
                            raise BusinessError("容量不能低于已售和保留票数")
                        tx.execute(
                            "UPDATE events SET "
                            + ",".join(k + "=?" for k in values)
                            + " WHERE id=?",
                            tuple(values.values()) + (event_id,),
                        )
                    else:
                        cur = tx.execute(
                            "INSERT INTO events("
                            + ",".join(values)
                            + ") VALUES("
                            + ",".join("?" for _ in values)
                            + ")",
                            tuple(values.values()),
                        )
                        event_id = cur.lastrowid
                    tx.execute("DELETE FROM event_members WHERE event_id=?", (event_id,))
                    for mid in set(selected):
                        if not tx.execute("SELECT 1 FROM members WHERE id=?", (mid,)).fetchone():
                            raise BusinessError("成员不存在")
                        tx.execute("INSERT INTO event_members VALUES(?,?)", (event_id, mid))
                    audit(tx, g.admin["id"], "save_event", event_id)
                g.pending_uploads.clear()
                gallery_saved = True
                flash("演出已保存。", "success")
                return redirect(url_for("admin_home"))
            except (BusinessError, InvalidOperation, ValueError) as exc:
                flash(str(exc) or "输入无效", "error")
                if any(file.filename for file in request.files.getlist("gallery_files")):
                    flash("演出未保存，原有照片保持不变。修正错误后请重新选择本次新照片。", "error")
            finally:
                if not gallery_saved:
                    for path in new_gallery_paths:
                        path.unlink(missing_ok=True)
        members = db.execute("SELECT * FROM members ORDER BY sort_order,id").fetchall()
        selected = [
            r[0]
            for r in db.execute("SELECT member_id FROM event_members WHERE event_id=?", (event_id,))
        ]
        return render_template("event_form.html", e=e, members=members, selected=selected)

    @app.route("/admin/members/new", methods=["GET", "POST"])
    @app.route("/admin/members/<int:member_id>/edit", methods=["GET", "POST"])
    @admin_required
    def edit_member(member_id=None):
        db = get_db()
        m = (
            db.execute("SELECT * FROM members WHERE id=?", (member_id,)).fetchone()
            if member_id
            else None
        )
        if member_id and not m:
            abort(404)
        if request.method == "POST":
            try:
                values = (
                    text("name", 60, True),
                    text("instrument", 100, True),
                    text("bio", 3000),
                    image_upload("image_file", link(text("image", 1000), True)),
                    integer("sort_order", 0, 1000),
                    checked("published"),
                )
                with transaction() as tx:
                    if m:
                        tx.execute(
                            "UPDATE members SET name=?,instrument=?,bio=?,image=?,sort_order=?,published=? WHERE id=?",
                            values + (member_id,),
                        )
                    else:
                        member_id = tx.execute(
                            "INSERT INTO members(name,instrument,bio,image,sort_order,published) VALUES(?,?,?,?,?,?)",
                            values,
                        ).lastrowid
                    audit(tx, g.admin["id"], "save_member", member_id)
                g.pending_uploads.clear()
                flash("成员已保存。", "success")
                return redirect(url_for("admin_home"))
            except BusinessError as exc:
                flash(str(exc), "error")
        return render_template("member_form.html", m=m)

    @app.route("/admin/settings", methods=["GET", "POST"])
    @admin_required
    def edit_settings():
        if request.method == "POST":
            try:
                cfg = settings()
                values = {
                    "about": text("about", 3000),
                    "contact": text("contact", 300),
                    "payee": text("payee", 100),
                    "payment_image": image_upload("payment_file", cfg.get("payment_image", "")),
                }
                with transaction() as db:
                    for k, v in values.items():
                        db.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (k, v))
                    audit(db, g.admin["id"], "save_settings", None)
                g.pending_uploads.clear()
                flash("设置已保存。", "success")
                return redirect(url_for("edit_settings"))
            except BusinessError as exc:
                flash(str(exc), "error")
        return render_template("settings.html")

    @app.route("/admin/media", methods=["POST"])
    @admin_required
    def media():
        try:
            path = image_upload("image_file")
            if not path:
                raise BusinessError("请选择一张图片")
            g.pending_uploads.clear()
            return render_template("media.html", path=path)
        except BusinessError as exc:
            flash(str(exc), "error")
            return redirect(url_for("admin_home"))

    @app.route("/admin/orders")
    @admin_required
    def admin_orders():
        refresh_expired()
        clauses = []
        params = []
        if request.args.get("status") in STATUS:
            clauses.append("o.status=?")
            params.append(request.args["status"])
        if request.args.get("event_id", "").isdigit() and len(request.args["event_id"]) <= 10:
            clauses.append("o.event_id=?")
            params.append(int(request.args["event_id"]))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        try:
            page = max(1, min(1000000, int(request.args.get("page", "1")[:10])))
        except ValueError:
            page = 1
        total = get_db().execute("SELECT COUNT(*) FROM orders o" + where, params).fetchone()[0]
        pages = max(1, (total + 49) // 50)
        page = min(page, pages)
        rows = (
            get_db()
            .execute(
                "SELECT o.*,e.title,e.is_test FROM orders o JOIN events e ON e.id=o.event_id"
                + where
                + " ORDER BY o.created_at DESC,o.id DESC LIMIT 50 OFFSET ?",
                [*params, (page - 1) * 50],
            )
            .fetchall()
        )
        filters = {
            key: request.args[key] for key in ("status", "event_id") if request.args.get(key)
        }
        return render_template(
            "admin_orders.html",
            orders=rows,
            page=page,
            pages=pages,
            total=total,
            previous=url_for("admin_orders", page=page - 1, **filters) if page > 1 else None,
            following=url_for("admin_orders", page=page + 1, **filters) if page < pages else None,
        )

    @app.route("/admin/orders/<int:order_id>/<action>", methods=["POST"])
    @admin_required
    def order_action(order_id, action):
        row = get_db().execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if not row:
            abort(404)
        try:
            if not checked("verified"):
                raise BusinessError("请完成实际到账／退款核对后再确认")
            if action == "confirm":
                confirm_order(order_id, g.admin["id"], text("reference", 120, True))
                flash("到账已确认，票券已生成。", "success")
            elif action == "refund":
                refund_order(order_id, g.admin["id"], text("reference", 120, True))
                flash("已记录实际退款，票券已作废。", "success")
            elif action == "cancel":
                cancel_order(order_id, g.admin["id"])
                flash("已取消并释放名额。", "success")
            else:
                abort(404)
        except BusinessError as exc:
            flash(str(exc), "error")
        return redirect(url_for("order_detail", public_id=row["public_id"]))

    @app.route("/admin/checkin", methods=["GET", "POST"])
    @admin_required
    def checkin():
        code = (
            (
                request.form.get("code", "")
                if request.method == "POST"
                else request.args.get("code", "")
            )
            .strip()
            .upper()[:100]
        )
        if request.method == "POST":
            try:
                redeem(code, g.admin["id"])
                flash("核销成功，可以入场。", "success")
            except BusinessError as exc:
                flash(str(exc), "error")
            return redirect(url_for("checkin", code=code))
        ticket = (
            get_db()
            .execute(
                """SELECT t.*,o.public_id,o.status,o.buyer,e.title,e.starts_at,e.is_test
            FROM tickets t JOIN orders o ON o.id=t.order_id JOIN events e ON e.id=o.event_id WHERE t.code=?""",
                (code,),
            )
            .fetchone()
            if code
            else None
        )
        return render_template("checkin.html", ticket=ticket, code=code)

    @app.errorhandler(400)
    @app.errorhandler(403)
    @app.errorhandler(404)
    @app.errorhandler(405)
    @app.errorhandler(413)
    @app.errorhandler(429)
    @app.errorhandler(500)
    @app.errorhandler(503)
    def errors(error):
        if isinstance(error, SecurityError):
            return Response("请求的域名无效。", status=400, mimetype="text/plain")
        messages = {
            400: "请求无效，请刷新后重试。",
            403: "你没有权限访问此内容。",
            404: "没有找到这个页面。",
            405: "此页面不支持这个操作。",
            413: "提交内容太大，请将图片与表单总大小控制在 20 MB 以内。",
            429: "操作频繁，请稍后再试。",
            500: "服务暂时出现问题，请稍后重试或联系工作人员。",
            503: "服务暂时繁忙，请稍后重试。",
        }
        response = app.make_response(
            (
                render_template("error.html", code=error.code, message=messages[error.code]),
                error.code,
            )
        )
        if error.code == 429:
            response.headers["Retry-After"] = str(error.retry_after or 60)
        if error.code == 503:
            response.headers["Retry-After"] = "30"
        return response

    @app.errorhandler(sqlite3.OperationalError)
    def database_error(error):
        app.logger.error("Database operation failed (%s)", type(error).__name__)
        return errors(ServiceUnavailable())

    @app.cli.command("expire-orders")
    def expire_orders_command():
        refresh_expired()
        click.echo("Expired reservations released.")

    @app.cli.command("set-admin")
    @click.option("--username", prompt=True)
    @click.password_option(confirmation_prompt=True)
    def set_admin(username, password):
        if not 12 <= len(password) <= 300:
            raise click.ClickException("密码需为 12–300 位")
        if not 1 <= len(username) <= 80:
            raise click.ClickException("用户名需为 1–80 字")
        with transaction() as db:
            db.execute(
                "INSERT INTO admins(username,password_hash) VALUES(?,?) ON CONFLICT(username) DO UPDATE SET password_hash=excluded.password_hash",
                (username, generate_password_hash(password)),
            )
            row = db.execute("SELECT id FROM admins WHERE username=?", (username,)).fetchone()
            db.execute("DELETE FROM admin_sessions WHERE admin_id=?", (row["id"],))
            audit(db, row["id"], "reset_password", row["id"])
        click.echo("管理员已设置。")

    @app.cli.command("seed-demo")
    def seed_demo():
        """Private test event; never receives real money or enters public lists."""
        with transaction() as db:
            cfg = {
                "about": "来自苏州的爵士五重奏，以 Bebop 与 Hard Bop 为主要演奏方向。小号、吉他、键盘、低音提琴与鼓，在旋律与即兴之间展开对话。",
                "contact": "",
                "payee": "",
                "payment_image": "",
            }
            for k, v in cfg.items():
                db.execute("INSERT OR IGNORE INTO settings VALUES(?,?)", (k, v))
            if not db.execute("SELECT 1 FROM members").fetchone():
                db.execute(
                    "INSERT INTO members(name,instrument,bio,published) VALUES('刘睿聪','Trumpet','',1)"
                )
            if not db.execute("SELECT 1 FROM events").fetchone():
                t = int(datetime(2026, 9, 26, 18, tzinfo=CHINA).timestamp())
                eid = db.execute(
                    """INSERT INTO events(title,starts_at,ends_at,venue,address,description,admission,sales_end,published)
                    VALUES(?,?,?,?,?,?,?,?,1)""",
                    (
                        "SUZHOU QUINTET · dv",
                        t,
                        t + 6 * 3600,
                        "dv",
                        "苏州市姑苏区平桥直街 120 号 2–3 楼",
                        "小号、吉他、键盘、低音提琴与鼓。这个夜晚，我们演奏 Bebop 与 Hard Bop。",
                        "free",
                        t,
                    ),
                ).lastrowid
                mid = db.execute("SELECT id FROM members ORDER BY id LIMIT 1").fetchone()[0]
                db.execute("INSERT INTO event_members VALUES(?,?)", (eid, mid))
            if not db.execute("SELECT 1 FROM events WHERE is_test=1").fetchone():
                t = now() + 1800
                db.execute(
                    """INSERT INTO events(title,starts_at,ends_at,venue,address,description,admission,price_cents,capacity,
                    sales_end,sales_open,refund_policy,is_test,published) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1,0)""",
                    (
                        "内部测试 · 请勿付款",
                        t,
                        t + 10800,
                        "测试场地",
                        "仅供管理员测试",
                        "模拟下单、确认、发票券和核销。严禁真实付款。",
                        "paid",
                        100,
                        6,
                        t,
                        1,
                        "仅用于功能测试，不收取费用。",
                    ),
                )
        click.echo("已初始化免费演出和管理员专用测试场次。测试场次不公开。")

    register_commands(app)
    register_public_pages(app)
    return app


def map_link(event):
    return "https://uri.amap.com/search?" + urlencode(
        {"keyword": event["address"], "city": "苏州", "src": "SuzhouQuintet", "callnative": "0"}
    )


app = create_app()
