"""Interactive first run. Not a packaging/install script."""

import getpass

from werkzeug.security import generate_password_hash

from app import app
from db import get_db

if __name__ == "__main__":
    print("SUZHOU QUINTET — 首次初始化（不会删除已有内容）")
    with app.app_context():
        db = get_db()
        if not db.execute("SELECT 1 FROM admins").fetchone():
            username = input("管理员用户名 [admin]：").strip() or "admin"
            if len(username) > 80:
                raise SystemExit("用户名最多 80 字，请重新运行初始化。")
            while True:
                password = getpass.getpass("设置管理员密码（至少12位，输入不会显示）：")
                confirmation = getpass.getpass("再次输入：")
                if 12 <= len(password) <= 300 and password == confirmation:
                    break
                print("密码不足12位或两次不一致，请重试。")
            db.execute(
                "INSERT INTO admins(username,password_hash) VALUES(?,?)",
                (username, generate_password_hash(password)),
            )
            db.commit()
        else:
            print("已有管理员账号，保持不变。")
    result = app.test_cli_runner().invoke(args=["seed-demo"])
    print(result.output)
    if result.exit_code:
        raise SystemExit(result.exit_code)
    print("完成。运行：python -m flask --app app run")
