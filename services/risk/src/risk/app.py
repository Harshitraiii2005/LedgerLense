"""Risk service HTTP API (Flask). The Kafka consumer runs in a background thread of the same process.

Run with one gunicorn worker per pod (threads for HTTP concurrency); scale out with more
replicas, which Kafka spreads across partitions via the `risk` consumer group.
"""

import atexit
import logging
import os
import sys
from typing import Any

from flask import Flask, Response, g, jsonify, request
from pydantic import ValidationError
from pymongo import ASCENDING, DESCENDING, MongoClient
from werkzeug.exceptions import HTTPException

from ledgerlens_common.logging import REQUEST_ID_HEADER, configure_logging, new_request_id, request_id_var
from ledgerlens_common.models import Transaction
from risk.config import Settings
from risk.consumer import RiskConsumer
from risk.store import ProfileStore

log = logging.getLogger("risk")


def load_settings() -> Settings:
    """Parse config or exit loudly. A bad ConfigMap should crash-loop with an obvious log line."""
    try:
        return Settings()
    except ValidationError as e:
        errors = [
            {"field": ".".join(map(str, err["loc"])) or "settings", "msg": err["msg"]} for err in e.errors()
        ]
        log.critical("invalid risk configuration, refusing to start", extra={"fields": {"errors": errors}})
        sys.exit(78)  # EX_CONFIG


def error(status: int, code: str, message: str) -> tuple[Response, int]:
    return jsonify(code=code, message=message, request_id=request_id_var.get()), status


def create_app(settings: Settings | None = None, store: Any = None, start_consumer: bool = True) -> Flask:
    configure_logging("risk", os.environ.get("LOG_LEVEL", "INFO"))
    cfg = settings or load_settings()
    store = store or ProfileStore(cfg)
    app = Flask(__name__)
    consumer: RiskConsumer | None = None
    mongo: MongoClient[dict[str, Any]] | None = None

    if start_consumer:
        store.ensure_index()
        mongo = MongoClient(cfg.mongo_uri, serverSelectionTimeoutMS=5000)
        transactions = mongo[cfg.mongo_db]["transactions"]
        # Serves "flagged transactions, newest first" (gateway /v2 flagged view).
        transactions.create_index([("risk.flagged", ASCENDING), ("timestamp", DESCENDING)], name="flagged_ts")
        consumer = RiskConsumer(cfg, store, transactions)
        consumer.start()
        atexit.register(consumer.stop)

    @app.before_request
    def set_request_id() -> None:
        g.request_id = request.headers.get(REQUEST_ID_HEADER) or new_request_id()
        request_id_var.set(g.request_id)

    @app.after_request
    def echo_request_id(resp: Response) -> Response:
        resp.headers[REQUEST_ID_HEADER] = g.get("request_id", "")
        return resp

    @app.errorhandler(HTTPException)
    def http_error(e: HTTPException) -> tuple[Response, int]:
        return error(e.code or 500, (e.name or "error").lower().replace(" ", "_"), e.description or "")

    @app.errorhandler(Exception)
    def unhandled(e: Exception) -> tuple[Response, int]:
        log.exception("unhandled error")
        return error(500, "internal_error", "internal server error")

    @app.get("/health/live")
    def live() -> Response:
        return jsonify(status="ok")

    @app.get("/health/ready")
    def ready() -> tuple[Response, int]:
        checks: dict[str, str] = {}
        try:
            store.ping()
            checks["couchbase"] = "ok"
        except Exception as e:
            checks["couchbase"] = f"error: {type(e).__name__}"
        if mongo is not None:
            try:
                mongo.admin.command("ping")
                checks["mongo"] = "ok"
            except Exception as e:
                checks["mongo"] = f"error: {type(e).__name__}"
        if consumer is not None:
            checks["consumer"] = "ok" if consumer.alive() else "stopped"
        healthy = all(v == "ok" for v in checks.values())
        stats = consumer.stats if consumer else {}
        return jsonify(ready=healthy, checks=checks, stats=stats), 200 if healthy else 503

    @app.get("/customers/<customer_id>/risk")
    def customer_risk(customer_id: str) -> Response | tuple[Response, int]:
        profile, _ = store.get(customer_id)
        if profile is None:
            return error(404, "not_found", f"no risk profile for {customer_id}")
        return jsonify(profile)

    @app.get("/reports/top-risky")
    def top_risky() -> Response | tuple[Response, int]:
        # Not args.get(type=int): that silently falls back to the default on garbage input.
        raw = request.args.get("limit", "10")
        limit = int(raw) if raw.isdigit() else 0
        if not 1 <= limit <= 100:
            return error(400, "bad_request", "limit must be an integer between 1 and 100")
        return jsonify(customers=store.top_risky(limit))

    @app.post("/score")
    def score() -> Response | tuple[Response, int]:
        """Dry run: how would this transaction score right now? Reads the profile, writes nothing."""
        try:
            txn = Transaction.model_validate_json(request.get_data())
        except ValidationError as e:
            return error(
                422,
                "validation_error",
                "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()),
            )
        return Response(store.dry_run(txn).model_dump_json(), mimetype="application/json")

    return app


def run() -> None:
    """Console-script entrypoint: hand the process over to gunicorn (the container's PID 1)."""
    # Validate config *before* gunicorn starts: a worker that dies on bad config is just
    # respawned by the gunicorn master forever, whereas exiting here makes the pod crash-loop
    # with the reason in its log.
    configure_logging("risk", os.environ.get("LOG_LEVEL", "INFO"))
    load_settings()
    port = os.environ.get("PORT", "8001")
    args = [
        "gunicorn",
        "--workers",
        "1",
        "--threads",
        "4",
        "--bind",
        f"0.0.0.0:{port}",
        "--access-logfile",
        "-",
        "--graceful-timeout",
        "20",
        "--no-control-socket",  # needs a writable $HOME; the non-root image has none
        "risk.app:create_app()",
    ]
    os.execvp("gunicorn", args)  # noqa: S606, S607 - gunicorn is on PATH in the venv
