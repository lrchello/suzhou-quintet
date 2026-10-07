"""Regression tests for adversarial requests and operational recovery."""

import io
import sqlite3
import zipfile
from pathlib import Path

import pytest
from PIL import Image
from test_app import event, post, purchase, signed_client

from app import create_app
from db import get_db
from maintenance import full_backup, verify_backup


def upload(client, image_format="PNG", **fields):
    data = io.BytesIO()
    Image.new("RGB", (40, 60), "blue").save(data, format=image_format)
    data.seek(0)
    return post(
        client,
        "/admin/members/new",
        {
            "name": "Photo member",
            "instrument": "Drums",
            "sort_order": "1",
            "image_file": (data, "photo." + image_format.lower()),
            **fields,
        },
        content_type="multipart/form-data",
    )


def test_logout_revokes_replayed_cookie(app):
    client = signed_client(app, True)
    cookie = client.get_cookie(app.config["SESSION_COOKIE_NAME"]).value
    assert post(client, "/admin/logout").status_code == 302
    attacker = app.test_client()
    attacker.set_cookie(app.config["SESSION_COOKIE_NAME"], cookie)
    assert attacker.get("/admin").status_code == 302


def test_password_reset_revokes_existing_sessions(app):
    client = signed_client(app, True)
    result = app.test_cli_runner().invoke(
        args=["set-admin", "--username", "test-admin", "--password", "new-long-password"]
    )
    assert result.exit_code == 0, result.output
    assert client.get("/admin").status_code == 302
    with app.app_context():
        assert get_db().execute("SELECT COUNT(*) FROM admin_sessions").fetchone()[0] == 0


@pytest.mark.parametrize("column", ["last_seen", "expires_at"])
def test_staff_session_timeout(app, column):
    client = signed_client(app, True)
    with app.app_context():
        db = get_db()
        db.execute(f"UPDATE admin_sessions SET {column}=0")
        db.commit()
    assert client.get("/admin").status_code == 302


def test_admin_id_without_server_session_has_no_authority(app):
    client = signed_client(app)
    with client.session_transaction() as session:
        session["admin_id"] = 1
    assert client.get("/admin").status_code == 302


def test_csrf_unicode_and_repeated_tokens(app):
    client = signed_client(app, True)
    assert client.post("/admin/settings", data={"_csrf": "过期凭证"}).status_code == 400
    assert client.post("/admin/settings", data={"_csrf": "wrong"}).status_code == 400


def test_rate_limit_retry_header_and_client_isolation(app):
    client = signed_client(app)
    for _ in range(12):
        post(client, "/lookup", {"public_id": "missing", "access_key": "wrong"})
    response = post(client, "/lookup")
    assert response.status_code == 429
    assert 0 < int(response.headers["Retry-After"]) <= 600
    assert (
        post(
            signed_client(app), "/lookup", environ_overrides={"REMOTE_ADDR": "192.0.2.2"}
        ).status_code
        == 200
    )


def test_unknown_order_does_not_reveal_existence(app):
    eid = event(app)
    order = purchase(app, eid)
    client = app.test_client()
    known = client.get("/orders/" + order["public_id"])
    unknown = client.get("/orders/DOESNOTEXIST")
    assert known.status_code == unknown.status_code == 302
    assert known.location == unknown.location


def test_private_images_and_payment_codes_require_access(app):
    client = signed_client(app, True)
    assert upload(client).status_code == 302
    with app.app_context():
        db = get_db()
        row = db.execute("SELECT id,image FROM members").fetchone()
        image = row["image"]
    assert client.get(image).status_code == 200
    assert app.test_client().get(image).status_code == 404
    with app.app_context():
        db = get_db()
        db.execute("UPDATE members SET published=1 WHERE id=?", (row["id"],))
        db.commit()
    assert app.test_client().get(image).status_code == 200
    with app.app_context():
        db = get_db()
        db.execute("UPDATE members SET published=0")
        db.execute("UPDATE settings SET value=? WHERE key='payment_image'", (image,))
        db.commit()
    order = purchase(app, event(app))
    assert app.test_client().get(image).status_code == 404
    owner = signed_client(app)
    with owner.session_transaction() as session:
        session["order_access"] = [order["public_id"]]
    response = owner.get(image)
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "private, no-store"


@pytest.mark.parametrize(
    "filename", ["secret.key", "band.sqlite3", "NUL:", "..%2fsecret.key", "bad.svg"]
)
def test_upload_path_allowlist(app, filename):
    assert signed_client(app, True).get("/uploads/" + filename).status_code == 404


def test_image_format_validation_and_rollback(app):
    client = signed_client(app, True)
    assert upload(client, "GIF").status_code == 200
    assert not list(Path(app.config["UPLOAD_DIR"]).iterdir())
    # Decode succeeds, then form validation fails: the temporary image is removed.
    assert upload(client, "PNG", sort_order="invalid").status_code == 200
    assert not list(Path(app.config["UPLOAD_DIR"]).iterdir())


def test_full_backup_round_trip_and_tampering(app, tmp_path):
    client = signed_client(app, True)
    upload(client, published="on")
    destination = tmp_path / "full.zip"
    with app.app_context():
        full_backup(destination)
        assert verify_backup(destination) >= 2
        with pytest.raises(FileExistsError):
            full_backup(destination)
        assert verify_backup(destination) >= 2
    with zipfile.ZipFile(destination) as archive:
        names = archive.namelist()
        restored = tmp_path / "restored.sqlite3"
        restored.write_bytes(archive.read("band.sqlite3"))
        assert any(name.startswith("uploads/") for name in names)
        with sqlite3.connect(restored) as db:
            assert db.execute("SELECT name FROM members").fetchone()[0] == "Photo member"
    corrupt = tmp_path / "corrupt.zip"
    with zipfile.ZipFile(destination) as original, zipfile.ZipFile(corrupt, "w") as archive:
        for name in names:
            archive.writestr(name, b"changed" if name == "band.sqlite3" else original.read(name))
    with app.app_context(), pytest.raises(ValueError, match="checksum"):
        verify_backup(corrupt)


def test_backup_cannot_overwrite_live_database(app):
    result = app.test_cli_runner().invoke(args=["backup", app.config["DATABASE"]])
    assert result.exit_code != 0
    with app.app_context():
        assert get_db().execute("PRAGMA quick_check").fetchone()[0] == "ok"


def production_config(tmp_path):
    return {
        "TESTING": True,
        "ENVIRONMENT": "production",
        "SECRET_KEY": "a" * 64,
        "SESSION_COOKIE_SECURE": True,
        "TRUSTED_HOSTS": ["band.example"],
        "PUBLIC_BASE_URL": "https://band.example",
        "PROXY_HOPS": 0,
        "DATABASE": str(tmp_path / "production.sqlite3"),
        "UPLOAD_DIR": str(tmp_path / "uploads"),
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"SECRET_KEY": None},
        {"SECRET_KEY": "short"},
        {"SESSION_COOKIE_SECURE": False},
        {"TRUSTED_HOSTS": None},
        {"TRUSTED_HOSTS": [".example"]},
        {"PUBLIC_BASE_URL": "http://band.example"},
        {"PUBLIC_BASE_URL": "https://evil.example"},
        {"DEBUG": True},
        {"PROXY_HOPS": -1},
    ],
)
def test_unsafe_production_configuration_rejected(tmp_path, changes):
    with pytest.raises(RuntimeError):
        create_app({**production_config(tmp_path), **changes})


def test_host_validation_and_secure_cookie(tmp_path):
    configured = create_app(production_config(tmp_path))
    client = configured.test_client()
    assert client.get("/", base_url="https://evil.example").status_code == 400
    response = client.get("/lookup", base_url="https://band.example")
    assert response.status_code == 200
    assert "Secure; HttpOnly" in response.headers["Set-Cookie"]
    assert response.headers["Strict-Transport-Security"] == "max-age=31536000"


@pytest.mark.parametrize("hops", [0, 1])
def test_proxy_trust_is_explicit(tmp_path, hops):
    configured = create_app({**production_config(tmp_path), "PROXY_HOPS": hops})

    @configured.get("/test-client")
    def address():
        from flask import request

        return {"address": request.remote_addr, "scheme": request.scheme}

    response = configured.test_client().get(
        "/test-client",
        base_url="https://band.example",
        headers={"X-Forwarded-For": "203.0.113.1, 192.0.2.5", "X-Forwarded-Proto": "https"},
    )
    assert response.json["address"] == ("192.0.2.5" if hops else "127.0.0.1")


def test_health_and_safe_database_failure(app, monkeypatch):
    client = app.test_client()
    response = client.get("/healthz")
    assert response.status_code == 200 and response.json == {"status": "ok"}
    assert response.headers["Cache-Control"] == "no-store"

    def broken_database():
        raise sqlite3.OperationalError("secret path should never be shown")

    monkeypatch.setattr("app.get_db", broken_database)
    response = client.get("/")
    assert response.status_code == 503
    assert b"secret path" not in response.data


def test_security_headers_cover_errors_and_sensitive_pages(app):
    for path in ["/", "/lookup", "/missing", "/uploads/missing.png"]:
        response = app.test_client().get(path)
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["X-Frame-Options"] == "DENY"
        assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
        assert response.headers["Cache-Control"] == "no-store"
