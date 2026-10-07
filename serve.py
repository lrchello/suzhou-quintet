"""Production entry point, or an explicit loopback-only local preview."""

import argparse
import os

from waitress import serve


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preview", action="store_true", help="Use local HTTP for demonstration.")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    if args.preview:
        if os.environ.get("SQ_ENV") == "production":
            parser.error("Use a separate development environment for preview")
        os.environ["SQ_ENV"] = "development"
    else:
        os.environ["SQ_ENV"] = "production"
    from app import app

    print(f"SUZHOU QUINTET: http://127.0.0.1:{args.port}", flush=True)
    serve(
        app,
        host="127.0.0.1",
        port=args.port,
        threads=4,
        max_request_body_size=app.config["MAX_CONTENT_LENGTH"],
        clear_untrusted_proxy_headers=app.config["PROXY_HOPS"] == 0,
        channel_timeout=30,
        expose_tracebacks=False,
        ident="SUZHOU QUINTET",
    )


if __name__ == "__main__":
    main()
