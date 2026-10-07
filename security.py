"""CSRF protection, revocable staff sessions and shared SQLite rate limits."""

import hashlib
import hmac
import secrets
from functools import wraps

from flask import abort, current_app, g, redirect, request, session, url_for
from werkzeug.exceptions import TooManyRequests
from werkzeug.security import generate_password_hash

from db import get_db
from ticketing import audit, now, transaction

# Checking a missing account should cost the same as checking an existing one.
DUMMY_PASSWORD_HASH = generate_password_hash(secrets.token_urlsafe(32))


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


def admin_required(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        if not g.admin:
            target = request.full_path if request.method == "GET" else url_for("admin_home")
            return redirect(url_for("login", next=target))
        return function(*args, **kwargs)

    return wrapped


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def start_admin_session(admin_id):
    token = secrets.token_urlsafe(32)
    timestamp = now()
    with transaction() as db:
        db.execute("DELETE FROM admin_sessions WHERE expires_at<=?", (timestamp,))
        db.execute(
            "INSERT INTO admin_sessions(token_hash,admin_id,expires_at,last_seen) VALUES(?,?,?,?)",
            (
                token_hash(token),
                admin_id,
                timestamp + current_app.config["ADMIN_SESSION_SECONDS"],
                timestamp,
            ),
        )
        audit(db, admin_id, "login", admin_id)
    owned = session.get("order_access", [])
    session.clear()
    session.update(admin_id=admin_id, admin_token=token, order_access=owned)
    session.permanent = True


def end_admin_session():
    token = session.get("admin_token")
    if token:
        with transaction() as db:
            db.execute("DELETE FROM admin_sessions WHERE token_hash=?", (token_hash(token),))
            if g.admin:
                audit(db, g.admin["id"], "logout", g.admin["id"])
    session.clear()


def protect_request():
    g.admin = None
    g.pending_uploads = []
    if current_app.config["ENVIRONMENT"] == "production" and current_app.debug:
        abort(503)
    timestamp = now()
    token = session.get("admin_token", "")
    if token:
        row = (
            get_db()
            .execute(
                """SELECT a.id,a.username,s.expires_at,s.last_seen FROM admin_sessions s
            JOIN admins a ON a.id=s.admin_id WHERE s.token_hash=? AND s.admin_id=?""",
                (token_hash(token), session.get("admin_id", -1)),
            )
            .fetchone()
        )
        if (
            row
            and row["expires_at"] > timestamp
            and row["last_seen"] > timestamp - current_app.config["ADMIN_IDLE_SECONDS"]
        ):
            g.admin = row
            if timestamp - row["last_seen"] >= 60:
                with transaction() as db:
                    db.execute(
                        "UPDATE admin_sessions SET last_seen=? WHERE token_hash=?",
                        (timestamp, token_hash(token)),
                    )
        else:
            with transaction() as db:
                db.execute("DELETE FROM admin_sessions WHERE token_hash=?", (token_hash(token),))
            session.pop("admin_id", None)
            session.pop("admin_token", None)
            session.pop("csrf", None)
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        supplied = request.form.get("_csrf", "")
        expected = session.get("csrf", "")
        if (
            not supplied
            or not expected
            or not hmac.compare_digest(supplied.encode(), expected.encode())
        ):
            abort(400, description="表单已过期，请刷新页面后重试。")


def limit(action, maximum, period=600, identity=None):
    identity = identity if identity is not None else request.remote_addr or "local"
    digest = hmac.new(
        current_app.secret_key.encode(), identity.encode(), hashlib.sha256
    ).hexdigest()
    key = action + ":" + digest
    timestamp = now()
    with transaction() as db:
        row = db.execute("SELECT * FROM rate_limits WHERE key=?", (key,)).fetchone()
        if not row or row["started_at"] <= timestamp - period:
            db.execute("INSERT OR REPLACE INTO rate_limits VALUES(?,?,1)", (key, timestamp))
        else:
            if row["attempts"] >= maximum:
                raise TooManyRequests(retry_after=max(1, row["started_at"] + period - timestamp))
            db.execute("UPDATE rate_limits SET attempts=attempts+1 WHERE key=?", (key,))
        db.execute("DELETE FROM rate_limits WHERE started_at<?", (timestamp - 86400,))
