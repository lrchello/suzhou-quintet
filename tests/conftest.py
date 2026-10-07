import pytest
from werkzeug.security import generate_password_hash

from app import create_app
from db import get_db


@pytest.fixture
def app(tmp_path):
    a = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-only-secret",
            "DATABASE": str(tmp_path / "test.sqlite3"),
            "UPLOAD_DIR": str(tmp_path / "uploads"),
        }
    )
    with a.app_context():
        db = get_db()
        db.execute(
            "INSERT INTO admins(username,password_hash) VALUES(?,?)",
            ("test-admin", generate_password_hash("long-test-password")),
        )
        for key, value in {
            "payment_image": "/uploads/test.png",
            "payee": "TEST ONLY",
            "contact": "test contact",
            "about": "test bio",
        }.items():
            db.execute("INSERT INTO settings VALUES(?,?)", (key, value))
        db.commit()
    return a
