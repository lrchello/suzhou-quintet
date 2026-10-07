"""Environment configuration, with stricter defaults for public deployments."""

import os
import secrets
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.routing import IntegerConverter


class DatabaseIdConverter(IntegerConverter):
    """Reject values which cannot fit in a SQLite integer before routing."""

    regex = r"[0-9]{1,19}"

    def __init__(self, url_map):
        super().__init__(url_map, min=1, max=2**63 - 1)


def configure(app, overrides=None):
    app.url_map.converters["int"] = DatabaseIdConverter
    app.config.update(
        ENVIRONMENT=os.environ.get("SQ_ENV", "development"),
        SECRET_KEY=os.environ.get("SECRET_KEY"),
        DATABASE=str(Path(app.instance_path, "band.sqlite3")),
        UPLOAD_DIR=str(Path(app.instance_path, "uploads")),
        MAX_CONTENT_LENGTH=20 * 1024 * 1024,
        MAX_FORM_MEMORY_SIZE=128 * 1024,
        MAX_FORM_PARTS=150,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("HTTPS_ONLY") == "1",
        SESSION_REFRESH_EACH_REQUEST=False,
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
        ADMIN_IDLE_SECONDS=30 * 60,
        ADMIN_SESSION_SECONDS=8 * 3600,
        PUBLIC_BASE_URL=os.environ.get("PUBLIC_BASE_URL", "").rstrip("/"),
        TRUSTED_HOSTS=[
            host.strip() for host in os.environ.get("TRUSTED_HOSTS", "").split(",") if host.strip()
        ]
        or None,
        PROXY_HOPS=int(os.environ.get("PROXY_HOPS", "0")),
    )
    if overrides:
        app.config.update(overrides)

    if app.config["ENVIRONMENT"] not in {"development", "production"}:
        raise RuntimeError("SQ_ENV must be development or production")
    if not 0 <= app.config["PROXY_HOPS"] <= 3:
        raise RuntimeError("PROXY_HOPS must be between 0 and 3")
    if app.config["ENVIRONMENT"] == "production":
        _validate_production(app.config)

    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    Path(app.config["DATABASE"]).parent.mkdir(parents=True, exist_ok=True)
    Path(app.config["UPLOAD_DIR"]).mkdir(parents=True, exist_ok=True)
    if not app.config["SECRET_KEY"]:
        secret_file = Path(app.instance_path, "secret.key")
        try:
            # Exclusive creation prevents two workers from choosing different keys.
            with secret_file.open("x", encoding="utf-8") as stream:
                stream.write(secrets.token_hex(32))
            secret_file.chmod(0o600)
        except FileExistsError:
            pass
        app.config["SECRET_KEY"] = secret_file.read_text(encoding="utf-8").strip()
    if len(app.config["SECRET_KEY"]) < 32 and not app.testing:
        raise RuntimeError("SECRET_KEY must contain at least 32 random characters")

    if app.config["PROXY_HOPS"]:
        # Enable only with a loopback listener behind the configured trusted proxy.
        hops = app.config["PROXY_HOPS"]
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=hops, x_proto=hops, x_host=0)


def _validate_production(config):
    if not config["SECRET_KEY"] or len(config["SECRET_KEY"]) < 32:
        raise RuntimeError("Production requires an explicit random SECRET_KEY (32+ characters)")
    if not config["SESSION_COOKIE_SECURE"]:
        raise RuntimeError("Production requires HTTPS_ONLY=1")
    url = urlsplit(config["PUBLIC_BASE_URL"])
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username
        or url.password
        or url.path
        or url.query
        or url.fragment
    ):
        raise RuntimeError("PUBLIC_BASE_URL must be a bare HTTPS origin")
    hosts = config["TRUSTED_HOSTS"]
    if not hosts or any(
        any(char in host for char in "/:*\\ \t\r\n") or host.startswith(".") for host in hosts
    ):
        raise RuntimeError("Production requires exact hostnames in TRUSTED_HOSTS")
    if url.hostname not in hosts:
        raise RuntimeError("PUBLIC_BASE_URL hostname must be included in TRUSTED_HOSTS")
    if config.get("DEBUG") or os.environ.get("FLASK_DEBUG") == "1":
        raise RuntimeError("Debug mode must be disabled in production")
