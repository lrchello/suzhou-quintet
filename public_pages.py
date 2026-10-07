"""Public privacy information and search metadata for published content."""

from xml.etree import ElementTree

from flask import Response, g, render_template, request

from db import get_db


def register_public_pages(app):
    @app.get("/privacy")
    def privacy():
        return render_template("privacy.html")

    @app.context_processor
    def metadata():
        public = request.endpoint in {"home", "events", "archive", "members", "privacy"}
        public = public or (request.endpoint == "event_detail" and g.get("public_event"))
        origin = app.config["PUBLIC_BASE_URL"]
        return {"canonical_url": origin + request.path if origin and public else None}

    @app.get("/robots.txt")
    def robots():
        lines = ["User-agent: *", "Disallow: /admin", "Disallow: /orders", "Disallow: /lookup"]
        if app.config["PUBLIC_BASE_URL"]:
            lines.append("Sitemap: " + app.config["PUBLIC_BASE_URL"] + "/sitemap.xml")
        return Response("\n".join(lines) + "\n", mimetype="text/plain")

    @app.get("/sitemap.xml")
    def sitemap():
        origin = app.config["PUBLIC_BASE_URL"]
        if not origin:
            return Response("Public URL is not configured.", status=404, mimetype="text/plain")
        namespace = "http://www.sitemaps.org/schemas/sitemap/0.9"
        ElementTree.register_namespace("", namespace)
        root = ElementTree.Element("{" + namespace + "}urlset")
        paths = ["/", "/events", "/members", "/archive", "/privacy"]
        paths += [
            f"/events/{row[0]}"
            for row in get_db().execute(
                "SELECT id FROM events WHERE published=1 AND is_test=0 ORDER BY id"
            )
        ]
        for path in paths:
            item = ElementTree.SubElement(root, "{" + namespace + "}url")
            ElementTree.SubElement(item, "{" + namespace + "}loc").text = origin + path
        return Response(
            ElementTree.tostring(root, encoding="utf-8", xml_declaration=True),
            mimetype="application/xml",
        )
