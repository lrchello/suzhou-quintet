"""Exercise the real production entry point and its proxy header handling."""

import http.client
import os
import secrets
import socket
import subprocess
import sys
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlencode


class CsrfField(HTMLParser):
    token = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "input" and attrs.get("name") == "_csrf":
            self.token = attrs["value"]


def test_production_waitress_preserves_only_configured_proxy_trust(tmp_path):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    root = Path(__file__).resolve().parents[1]
    environment = {
        **os.environ,
        "SQ_ENV": "production",
        "HTTPS_ONLY": "1",
        "SECRET_KEY": secrets.token_hex(32),
        "PUBLIC_BASE_URL": "https://band.example",
        "TRUSTED_HOSTS": "band.example",
        "SQ_INSTANCE_PATH": str(tmp_path / "instance"),
        "PROXY_HOPS": "1",
        "FLASK_DEBUG": "0",
        "PYTHONUTF8": "1",
    }
    with (tmp_path / "server.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            [sys.executable, "serve.py", "--port", str(port)],
            cwd=root,
            env=environment,
            stdout=log,
            stderr=log,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        try:

            def fetch(
                path,
                method="GET",
                form=None,
                cookie=None,
                address="203.0.113.5",
                host="band.example",
            ):
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
                headers = {
                    "Host": host,
                    "X-Forwarded-For": address,
                    "X-Forwarded-Proto": "https",
                    "X-Forwarded-Host": "evil.example",
                }
                body = None
                if form is not None:
                    body = urlencode(form)
                    headers["Content-Type"] = "application/x-www-form-urlencoded"
                if cookie:
                    headers["Cookie"] = cookie
                try:
                    connection.request(method, path, body=body, headers=headers)
                    response = connection.getresponse()
                    return response.status, dict(response.getheaders()), response.read()
                finally:
                    connection.close()

            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                assert process.poll() is None, "Production server exited before becoming ready"
                try:
                    status, _, body = fetch("/healthz")
                    break
                except OSError:
                    time.sleep(0.1)
            else:
                raise AssertionError("Production server did not become ready")
            assert status == 200 and b'"ok"' in body
            assert fetch("/", host="evil.example")[0] == 400
            assert fetch("/events/" + "9" * 30)[0] == 404
            status, headers, body = fetch("/admin/login")
            assert status == 200 and "Secure" in headers["Set-Cookie"]
            assert "Strict-Transport-Security" in headers
            cookie = headers["Set-Cookie"].split(";", 1)[0]
            parser = CsrfField()
            parser.feed(body.decode("utf-8"))
            form = {"_csrf": parser.token, "username": "unknown", "password": "invalid"}
            for _ in range(8):
                assert fetch("/admin/login", "POST", form, cookie)[0] == 200
            assert fetch("/admin/login", "POST", form, cookie)[0] == 429
            # A second client must not inherit the first client's proxy IP quota.
            assert fetch("/admin/login", "POST", form, cookie, address="203.0.113.6")[0] == 200
        finally:
            process.terminate()
            process.wait(timeout=10)
