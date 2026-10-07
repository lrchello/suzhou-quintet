"""Operational commands. Backups never overwrite an existing destination."""

import hashlib
import json
import sqlite3
import tempfile
import zipfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import click
from flask import current_app

from db import get_db, settings
from gallery_uploads import valid_filename


def backup_database(destination):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Reserve the filename first; a typo cannot overwrite the live database.
    with destination.open("xb"):
        pass
    try:
        with closing(sqlite3.connect(destination)) as target:
            get_db().backup(target)
        destination.chmod(0o600)
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def full_backup(destination):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=current_app.instance_path) as temporary:
        snapshot = Path(temporary, "band.sqlite3")
        db = get_db()
        db.execute("BEGIN IMMEDIATE")
        try:
            # A separate WAL reader can take a snapshot while this connection
            # prevents content updates from changing the corresponding images.
            source_path = Path(current_app.config["DATABASE"]).resolve()
            with closing(sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True)) as source:
                with closing(sqlite3.connect(snapshot)) as target:
                    source.backup(target)
            entries = {"band.sqlite3": snapshot}
            entries.update(
                {
                    "uploads/" + path.name: path
                    for path in Path(current_app.config["UPLOAD_DIR"]).iterdir()
                    if path.is_file() and not path.is_symlink() and valid_filename(path.name)
                }
            )
            secret_file = Path(current_app.instance_path, "secret.key")
            if secret_file.is_file():
                entries["secret.key"] = secret_file
            hashes = {}
            with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED) as archive:
                for name, path in entries.items():
                    data = path.read_bytes()
                    hashes[name] = hashlib.sha256(data).hexdigest()
                    archive.writestr(name, data)
                archive.writestr(
                    "manifest.json",
                    json.dumps(
                        {
                            "version": 1,
                            "created_at": datetime.now(timezone.utc).isoformat(),
                            "sha256": hashes,
                            "environment_secret_required": True,
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                )
            destination.chmod(0o600)
        except FileExistsError:
            raise
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        finally:
            db.rollback()


def verify_backup(path):
    with zipfile.ZipFile(path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        hashes = manifest["sha256"]
        if manifest["version"] != 1 or "band.sqlite3" not in hashes:
            raise ValueError("Unsupported backup manifest")
        if set(archive.namelist()) != set(hashes) | {"manifest.json"}:
            raise ValueError("Backup contains missing or unexpected files")
        for name, expected in hashes.items():
            if name not in {"band.sqlite3", "secret.key"} and not (
                name.startswith("uploads/") and valid_filename(name[len("uploads/") :])
            ):
                raise ValueError("Invalid backup member path")
            if hashlib.sha256(archive.read(name)).hexdigest() != expected:
                raise ValueError("Backup checksum mismatch")
        with tempfile.TemporaryDirectory(dir=current_app.instance_path) as temporary:
            snapshot = Path(temporary, "band.sqlite3")
            snapshot.write_bytes(archive.read("band.sqlite3"))
            with closing(sqlite3.connect(snapshot)) as db:
                if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise ValueError("Backup database integrity check failed")
                if db.execute("PRAGMA foreign_key_check").fetchone():
                    raise ValueError("Backup database relationships are invalid")
    return len(hashes)


def register_commands(app):
    @app.get("/healthz")
    def health():
        get_db().execute("SELECT 1 FROM settings LIMIT 1").fetchone()
        return {"status": "ok"}

    @app.cli.command("backup")
    @click.argument("destination", type=click.Path(path_type=Path))
    @click.option(
        "--full", is_flag=True, help="Include uploads, local secret file and checksums in a ZIP."
    )
    def backup(destination, full):
        try:
            (full_backup if full else backup_database)(destination)
        except (OSError, sqlite3.Error) as error:
            raise click.ClickException(
                "备份未完成；检查目标是否已存在、权限及磁盘空间。"
            ) from error
        click.echo(
            "完整备份完成。环境变量 SECRET_KEY 须单独安全保存。"
            if full
            else "数据库备份完成；请另行备份 uploads 和会话密钥。"
        )

    @app.cli.command("verify-backup")
    @click.argument("path", type=click.Path(exists=True, path_type=Path))
    def verify(path):
        try:
            count = verify_backup(path)
        except (OSError, ValueError, KeyError, zipfile.BadZipFile, sqlite3.Error) as error:
            raise click.ClickException("备份验证失败，不应使用此文件恢复。") from error
        click.echo(f"校验通过：{count} 个文件，SQLite 完整性及关联检查通过。")

    @app.cli.command("check-deployment")
    def check_deployment():
        """Check configuration and public content without printing private data."""
        db = get_db()
        checks = [
            (app.config["ENVIRONMENT"] == "production", "生产模式"),
            (app.config["SESSION_COOKIE_SECURE"], "HTTPS 安全 Cookie"),
            (bool(app.config["TRUSTED_HOSTS"]), "可信域名"),
            (bool(app.config["PUBLIC_BASE_URL"]), "公开地址"),
            (not app.debug, "调试模式关闭"),
            (db.execute("PRAGMA quick_check").fetchone()[0] == "ok", "数据库完整性"),
            (not db.execute("PRAGMA foreign_key_check").fetchone(), "数据库关联"),
            (bool(db.execute("SELECT 1 FROM admins").fetchone()), "管理员已设置"),
            (bool(settings().get("contact", "").strip()), "公开联系渠道已填写"),
            (bool(settings().get("about", "").strip()), "乐队介绍已填写"),
        ]
        missing = 0
        for passed, label in checks:
            click.echo(f"{'PASS' if passed else 'FAIL'} {label}")
            missing += not passed
        if missing:
            raise click.ClickException(
                f"还有 {missing} 项待处理；此检查不代替公网 HTTPS、备份恢复及手机验收。"
            )
        click.echo("配置检查通过；继续执行上线验收清单。")
