import io
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree

from PIL import Image
from test_app import event, post, signed_client

from db import get_db


def image_file(color="blue"):
    data = io.BytesIO()
    Image.new("RGB", (60, 80), color).save(data, format="JPEG")
    data.seek(0)
    return data, "photo.jpg"


def event_form(client, eid):
    html = client.get(f"/admin/events/{eid}/edit").get_data(as_text=True)

    class Fields(HTMLParser):
        def __init__(self):
            super().__init__()
            self.values = {}

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "input" and "name" in attrs and "value" in attrs:
                self.values[attrs["name"]] = attrs["value"]

    parser = Fields()
    parser.feed(html)
    values = parser.values
    return {
        "title": "Gallery test",
        "venue": "Test venue",
        "address": "Test address",
        "admission": "free",
        "price": "0",
        "capacity": "0",
        "starts_at": values["starts_at"],
        "ends_at": values["ends_at"],
        "sales_end": values["sales_end"],
        "published": "on",
        "gallery_editor": "1",
    }


def test_multiple_gallery_uploads_and_removal(app):
    eid = event(app, admission="free")
    client = signed_client(app, True)
    form = event_form(client, eid)
    response = post(
        client,
        f"/admin/events/{eid}/edit",
        {**form, "gallery_files": [image_file(), image_file("red")]},
        content_type="multipart/form-data",
    )
    assert response.status_code == 302
    with app.app_context():
        originals = (
            get_db()
            .execute("SELECT gallery FROM events WHERE id=?", (eid,))
            .fetchone()[0]
            .splitlines()
        )
    assert len(originals) == 2 and originals[0] != originals[1]
    assert all(app.test_client().get(url).status_code == 200 for url in originals)
    response = post(client, f"/admin/events/{eid}/edit", {**form, "keep_gallery": originals[1]})
    assert response.status_code == 302
    with app.app_context():
        assert (
            get_db().execute("SELECT gallery FROM events WHERE id=?", (eid,)).fetchone()[0]
            == originals[1]
        )
    assert app.test_client().get(originals[0]).status_code == 404


def test_gallery_failure_rolls_back_files_and_existing_content(app):
    eid = event(app, admission="free")
    client = signed_client(app, True)
    form = event_form(client, eid)
    with app.app_context():
        db = get_db()
        db.execute(
            "UPDATE events SET gallery=? WHERE id=?", ("https://example.invalid/original.jpg", eid)
        )
        db.commit()
    response = post(
        client,
        f"/admin/events/{eid}/edit",
        {
            **form,
            "keep_gallery": "https://example.invalid/original.jpg",
            "gallery_files": [image_file(), (io.BytesIO(b"not an image"), "bad.jpg")],
            "cover_file": image_file("green"),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 200
    assert not list(Path(app.config["UPLOAD_DIR"]).iterdir())
    with app.app_context():
        assert (
            get_db().execute("SELECT gallery FROM events WHERE id=?", (eid,)).fetchone()[0]
            == "https://example.invalid/original.jpg"
        )


def test_gallery_rejects_forged_existing_selection(app):
    eid = event(app, admission="free")
    client = signed_client(app, True)
    response = post(
        client,
        f"/admin/events/{eid}/edit",
        {**event_form(client, eid), "keep_gallery": "https://example.invalid/forged.jpg"},
    )
    assert response.status_code == 200 and "请刷新" in response.get_data(as_text=True)


def test_sitemap_excludes_hidden_and_test_events(app):
    public = event(app, admission="free")
    private = event(app, published=0)
    internal = event(app, is_test=1)
    app.config["PUBLIC_BASE_URL"] = "https://band.example"
    response = app.test_client().get("/sitemap.xml")
    assert response.status_code == 200
    root = ElementTree.fromstring(response.data)
    urls = [item.text for item in root.findall(".//{*}loc")]
    assert f"https://band.example/events/{public}" in urls
    assert f"https://band.example/events/{private}" not in urls
    assert f"https://band.example/events/{internal}" not in urls
    assert all("admin" not in url and "orders" not in url for url in urls)
    assert "Sitemap: https://band.example/sitemap.xml" in app.test_client().get(
        "/robots.txt"
    ).get_data(as_text=True)


def test_private_preview_has_no_canonical_url(app):
    internal = event(app, is_test=1)
    app.config["PUBLIC_BASE_URL"] = "https://band.example"
    client = signed_client(app, True)
    response = client.get(f"/events/{internal}")
    assert 'rel="canonical"' not in response.get_data(as_text=True)
    assert response.headers["X-Robots-Tag"] == "noindex, nofollow"
    assert app.test_client().get("/privacy").status_code == 200


def test_order_pagination_and_filter_preservation(app):
    eid = event(app, capacity=100)
    other = event(app)
    with app.app_context():
        db = get_db()
        for n in range(55):
            db.execute(
                """INSERT INTO orders(public_id,access_hash,request_key,event_id,buyer,
                contact,quantity,unit_cents,status,created_at,expires_at,policy_snapshot)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    f"ORDER{n:04}",
                    "test-only",
                    f"key{n}",
                    other if n == 0 else eid,
                    "Test buyer",
                    "Private",
                    1,
                    100,
                    "paid",
                    n,
                    0,
                    "Test policy",
                ),
            )
        db.commit()
    client = signed_client(app, True)
    response = client.get(f"/admin/orders?event_id={eid}&status=paid")
    html = response.get_data(as_text=True)
    assert html.count("/orders/ORDER") == 50
    assert "ORDER0000" not in html
    assert "page=2" in html and "status=paid" in html
    html = client.get(f"/admin/orders?event_id={eid}&status=paid&page=2").get_data(as_text=True)
    assert html.count("/orders/ORDER") == 4
    assert client.get("/admin/orders?page=bad&event_id=" + "9" * 5000).status_code == 200
