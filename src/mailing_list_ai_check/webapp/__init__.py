"""Flask app for the dashboard: a JSON API plus (in production) the built Vue SPA.

The API lives in :mod:`mailing_list_ai_check.webapp.api`. This module wires it into
an application via :func:`create_app` and exposes the ``mail-ai-web`` dev-server
entry point (:func:`main`).

Frontend serving
----------------
If ``frontend/dist`` exists (``npm run build`` has run) the app serves it as a
single-page app: static files are returned directly and every other non-``/api``
path falls back to ``index.html`` so client-side routing works. If it does not
exist (the common state during development), the app is in **dev mode**: ``/``
returns a small JSON notice and CORS headers are emitted for the Vite dev server
origin (``http://localhost:5173``) so the separately served frontend can call the
API.

Connection handling
--------------------
One ``sqlite3``-backed :class:`~mailing_list_ai_check.store.Store` is opened per
request (lazily, in :func:`~mailing_list_ai_check.webapp.api.get_store`) and closed
on app-context teardown. Per-request connections keep each connection on a single
thread, so the stdlib default ``check_same_thread=True`` holds under the threaded
dev server without sharing a connection across threads.
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from flask import Flask, g, jsonify, send_from_directory

from ..config import Config
from .api import ApiError, api_bp

log = logging.getLogger("mailing_list_ai_check.web")

#: The Vite dev server origin allowed via CORS when no built frontend is present.
DEV_CORS_ORIGIN = "http://localhost:5173"

#: Sentinel: auto-detect ``frontend/dist`` from the repo root.
_AUTODETECT = object()


def _default_frontend_dist() -> Path:
    """Locate ``frontend/dist`` relative to the repo root (…/src/pkg/webapp)."""
    return Path(__file__).resolve().parents[3] / "frontend" / "dist"


def create_app(
    config: Config | None = None,
    *,
    frontend_dist: Any = _AUTODETECT,
) -> Flask:
    """Build the Flask application.

    ``config`` defaults to :meth:`Config.load`. ``frontend_dist`` defaults to
    auto-detecting ``frontend/dist``; pass an explicit path (or ``None``) to force
    production or dev behaviour — used by tests.
    """
    if config is None:
        config = Config.load()

    if frontend_dist is _AUTODETECT:
        dist: Path | None = _default_frontend_dist()
    elif frontend_dist is None:
        dist = None
    else:
        dist = Path(frontend_dist)

    dev_mode = dist is None or not dist.exists()

    app = Flask(__name__, static_folder=None)
    app.config["STORE_PATH"] = config.database_path
    # The full Config is needed by the /api/pull endpoint (IMAP credentials and
    # the optional Pangram key). Stashed here so tests can inject a config with,
    # e.g., an empty pangram_api_key without touching the environment.
    app.config["APP_CONFIG"] = config
    app.config["FRONTEND_DIST"] = str(dist) if dist is not None else None
    app.config["DEV_MODE"] = dev_mode

    _register_teardown(app)
    if dev_mode:
        _register_cors(app)
    app.register_blueprint(api_bp)
    _register_frontend(app, None if dev_mode else dist)
    _register_errors(app)
    return app


def _register_teardown(app: Flask) -> None:
    @app.teardown_appcontext
    def _close_store(_exc: BaseException | None) -> None:
        store = g.pop("store", None)
        if store is not None:
            store.close()


def _register_cors(app: Flask) -> None:
    @app.after_request
    def _add_cors_headers(response: Any) -> Any:
        response.headers["Access-Control-Allow-Origin"] = DEV_CORS_ORIGIN
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        return response


def _register_frontend(app: Flask, dist: Path | None) -> None:
    if dist is not None:
        index = "index.html"

        @app.route("/", defaults={"path": ""})
        @app.route("/<path:path>")
        def spa(path: str) -> Any:
            # Never let the SPA fallback shadow the API; unknown API routes must
            # still 404 as JSON.
            if path.startswith("api/"):
                raise ApiError("not found", 404)
            candidate = dist / path
            if path and candidate.is_file():
                return send_from_directory(dist, path)
            return send_from_directory(dist, index)
    else:

        @app.route("/")
        def notice() -> Any:
            return jsonify(
                {
                    "app": "mailing-list-ai-check",
                    "message": (
                        "API is running. The Vue frontend has not been built "
                        "(frontend/dist is missing). Run the Vite dev server, or "
                        "'npm run build' to have this app serve the bundle."
                    ),
                    "api_base": "/api",
                }
            )


def _register_errors(app: Flask) -> None:
    @app.errorhandler(ApiError)
    def _handle_api_error(exc: ApiError) -> Any:
        return jsonify({"error": exc.message}), exc.status

    @app.errorhandler(400)
    def _bad_request(_exc: Any) -> Any:
        return jsonify({"error": "bad request"}), 400

    @app.errorhandler(404)
    def _not_found(_exc: Any) -> Any:
        return jsonify({"error": "not found"}), 404

    @app.errorhandler(405)
    def _method_not_allowed(_exc: Any) -> Any:
        return jsonify({"error": "method not allowed"}), 405


# --- dev-server entry point ---------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mail-ai-web",
        description="Run the dashboard JSON API (and built frontend, if present).",
    )
    parser.add_argument("--host", help="bind host (default from FLASK_HOST)")
    parser.add_argument("--port", type=int, help="bind port (default from FLASK_PORT)")
    parser.add_argument("--db", metavar="PATH", help="override the database path")
    parser.add_argument("--debug", action="store_true", help="run Flask in debug mode")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = Config.load()
    if args.db:
        config = replace(config, database_path=args.db)

    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    host = args.host or config.flask_host
    port = args.port or config.flask_port
    app = create_app(config)
    mode = "dev (no built frontend)" if app.config["DEV_MODE"] else "serving frontend/dist"
    log.info("starting mail-ai-web on http://%s:%d [%s]", host, port, mode)
    # threaded=True so a long-running /api/pull (network + paid scoring) does not
    # block the SPA's other API requests on the single-process dev server.
    app.run(host=host, port=port, debug=args.debug, threaded=True)
    return 0


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.exit(main())
