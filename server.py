"""
Spur — A self-hosted webhook event relay for AI agent teams.
Receives events, transforms them with templates, forwards to Telegram, Slack, Discord, or HTTP.
Route your signals, don't lose them.
"""

import sqlite3
import json
import uuid
import os
import urllib.request
import urllib.parse
import threading
from datetime import datetime, timezone
from pathlib import Path
from contextlib import contextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional, List
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

DB_PATH = Path(os.environ.get("SPUR_DB", str(Path(__file__).parent / "data" / "spur.db")))
STATIC_DIR = Path(__file__).parent / "static"
HOST = os.environ.get("SPUR_HOST", "127.0.0.1")
PORT = int(os.environ.get("SPUR_PORT", "8797"))
API_KEY = os.environ.get("SPUR_API_KEY", "")

app = FastAPI(title="Spur", version="1.1.0")


# --- Optional API Key Auth ---

READ_METHODS = {"GET", "HEAD", "OPTIONS"}

class ApiKeyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not API_KEY:
            return await call_next(request)
        path = request.url.path
        if path == "/" or path.startswith("/static") or path == "/health":
            return await call_next(request)
        if request.method in READ_METHODS:
            return await call_next(request)
        key = request.headers.get("x-api-key") or request.headers.get("authorization", "").removeprefix("Bearer ")
        if key != API_KEY:
            return JSONResponse(status_code=401, content={"detail": "Invalid or missing API key"})
        return await call_next(request)

app.add_middleware(ApiKeyMiddleware)


# --- Database ---

def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS routes (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                source_filter TEXT DEFAULT '',
                destination_type TEXT NOT NULL,
                destination_config TEXT NOT NULL DEFAULT '{}',
                template TEXT DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT DEFAULT '',
                source TEXT DEFAULT '',
                payload TEXT NOT NULL DEFAULT '{}',
                matched_routes TEXT DEFAULT '[]',
                status TEXT DEFAULT 'received',
                timestamp TEXT NOT NULL
            )
        """)
        db.execute("""
            CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events(timestamp DESC)
        """)


@contextmanager
def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


# --- Models ---

VALID_DEST_TYPES = ["telegram", "slack", "discord", "matrix", "http"]

class RouteCreate(BaseModel):
    name: str
    source_filter: str = ""
    destination_type: str
    destination_config: dict = {}
    template: str = ""
    enabled: bool = True

class RouteUpdate(BaseModel):
    name: Optional[str] = None
    source_filter: Optional[str] = None
    destination_type: Optional[str] = None
    destination_config: Optional[dict] = None
    template: Optional[str] = None
    enabled: Optional[bool] = None

class BulkReplayRequest(BaseModel):
    event_type: Optional[str] = None
    status: Optional[str] = None
    since: Optional[str] = None


# --- Template Engine ---

def render_template(template: str, data: dict) -> str:
    """Simple template rendering. Replaces {key} and {key.subkey} with values from data.
    Falls back to JSON dump if no template is provided."""
    if not template:
        event_type = data.get("event", data.get("event_type", "event"))
        source = data.get("source", "")
        # Build a sensible default message
        parts = [f"[{event_type}]"]
        if source:
            parts[0] = f"[{event_type} from {source}]"

        # Pull out common useful fields
        for key in ("title", "name", "message", "description", "status", "action"):
            if key in data:
                parts.append(str(data[key]))
                break

        # Add details if present
        details = data.get("details", {})
        if isinstance(details, dict):
            for k, v in list(details.items())[:3]:
                parts.append(f"{k}: {v}")

        return "\n".join(parts)

    result = template
    # Flatten nested dicts for template access: {details.title} etc.
    flat = _flatten_dict(data)
    for key, value in flat.items():
        result = result.replace("{" + key + "}", str(value))
    return result


def _flatten_dict(d: dict, prefix: str = "") -> dict:
    """Flatten a nested dict: {"a": {"b": 1}} -> {"a.b": "1", "a": {"b": 1}}"""
    items = {}
    for k, v in d.items():
        full_key = f"{prefix}{k}" if not prefix else f"{prefix}.{k}"
        items[full_key] = v
        if isinstance(v, dict):
            items.update(_flatten_dict(v, full_key))
    # Also add top-level keys without prefix
    if not prefix:
        for k, v in d.items():
            items[k] = v
    return items


# --- Delivery ---

def deliver(dest_type: str, config: dict, message: str, event_data: dict):
    """Deliver a message to a destination. Runs in background thread."""
    def _send():
        try:
            if dest_type == "telegram":
                _send_telegram(config, message)
            elif dest_type == "slack":
                _send_slack(config, message)
            elif dest_type == "discord":
                _send_discord(config, message)
            elif dest_type == "matrix":
                _send_matrix(config, message)
            elif dest_type == "http":
                _send_http(config, message, event_data)
        except Exception as e:
            import traceback
            print(f"[spur] Delivery failed ({dest_type}): {e}", flush=True)
            traceback.print_exc()
    threading.Thread(target=_send, daemon=True).start()


def _send_telegram(config: dict, message: str):
    """Send via Telegram Bot API."""
    token = config.get("bot_token", "")
    chat_id = config.get("chat_id", "")
    parse_mode = config.get("parse_mode", "Markdown")
    if not token or not chat_id:
        print("[spur] Telegram: missing bot_token or chat_id")
        return
    params = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": message[:4096],  # Telegram limit
        "parse_mode": parse_mode
    })
    url = f"https://api.telegram.org/bot{token}/sendMessage?{params}"
    urllib.request.urlopen(url, timeout=10)


def _send_slack(config: dict, message: str):
    """Send via Slack Incoming Webhook."""
    webhook_url = config.get("webhook_url", "")
    if not webhook_url:
        print("[spur] Slack: missing webhook_url")
        return
    payload = json.dumps({"text": message}).encode()
    req = urllib.request.Request(webhook_url, data=payload,
                                 headers={"Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(req, timeout=10)


def _send_discord(config: dict, message: str):
    """Send via Discord Webhook."""
    webhook_url = config.get("webhook_url", "")
    if not webhook_url:
        print("[spur] Discord: missing webhook_url")
        return
    payload = json.dumps({"content": message[:2000]}).encode()  # Discord limit
    req = urllib.request.Request(webhook_url, data=payload,
                                 headers={"Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(req, timeout=10)


def _send_matrix(config: dict, message: str):
    """Send via Matrix client-server API (PUT /_matrix/client/v3/rooms/{roomId}/send/{eventType}/{txnId})."""
    homeserver = config.get("homeserver", "").rstrip("/")
    room_id = config.get("room_id", "")
    access_token = config.get("access_token", "")
    if not homeserver or not room_id or not access_token:
        print("[spur] Matrix: missing homeserver, room_id, or access_token")
        return
    txn_id = uuid.uuid4().hex[:12]
    encoded_room = urllib.parse.quote(room_id, safe='')
    url = f"{homeserver}/_matrix/client/v3/rooms/{encoded_room}/send/m.room.message/{txn_id}"
    payload = json.dumps({
        "msgtype": "m.text",
        "body": message
    }).encode()
    req = urllib.request.Request(url, data=payload,
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Bearer {access_token}"},
                                 method="PUT")
    urllib.request.urlopen(req, timeout=10)


def _send_http(config: dict, message: str, event_data: dict):
    """Forward to an arbitrary HTTP endpoint."""
    url = config.get("url", "")
    method = config.get("method", "POST")
    headers = config.get("headers", {})
    if not url:
        print("[spur] HTTP: missing url")
        return
    payload = json.dumps({
        "message": message,
        "event": event_data,
        "timestamp": now_iso()
    }).encode()
    headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=payload, headers=headers, method=method)
    urllib.request.urlopen(req, timeout=10)


# --- Route Matching ---

def match_routes(event_type: str, source: str, payload: dict) -> list[dict]:
    """Find all enabled routes whose source_filter matches this event."""
    with get_db() as db:
        routes = db.execute("SELECT * FROM routes WHERE enabled = 1").fetchall()

    matched = []
    for route in routes:
        route_dict = dict(route)
        filter_str = route_dict.get("source_filter", "").strip()

        if not filter_str:
            # No filter = match everything
            matched.append(route_dict)
            continue

        # Filter format: "field:value" or "field:value,field2:value2" (all must match)
        # Also supports bare values which match against event_type
        conditions = [c.strip() for c in filter_str.split(",")]
        all_match = True

        for condition in conditions:
            if ":" in condition:
                field, value = condition.split(":", 1)
                field = field.strip()
                value = value.strip()

                # Check top-level event fields
                check_against = {
                    "event": event_type,
                    "event_type": event_type,
                    "source": source,
                }
                # Also check payload fields
                check_against.update(payload)

                actual = str(check_against.get(field, ""))
                if value.startswith("*") and value.endswith("*"):
                    if value[1:-1] not in actual:
                        all_match = False
                elif value.startswith("*"):
                    if not actual.endswith(value[1:]):
                        all_match = False
                elif value.endswith("*"):
                    if not actual.startswith(value[:-1]):
                        all_match = False
                elif actual != value:
                    all_match = False
            else:
                # Bare value matches against event_type
                if condition != event_type:
                    all_match = False

            if not all_match:
                break

        if all_match:
            matched.append(route_dict)

    return matched


# --- Core Event Processing ---

def process_event(event_type: str, source: str, payload: dict, event_type_prefix: str = "") -> dict:
    """Core event processing: match routes, log, deliver.

    Args:
        event_type: The event type string used for route matching.
        source: The source string used for route matching.
        payload: The full event payload dict.
        event_type_prefix: If set, prepended to event_type in the log entry (e.g. "replay:").

    Returns:
        dict with matched count, delivered_to list, and the logged event_type.
    """
    ts = now_iso()

    # Match routes using the original event_type (not the prefixed version)
    matched = match_routes(event_type, source, payload)
    matched_ids = [r["id"] for r in matched]

    # Log with optional prefix
    logged_type = f"{event_type_prefix}{event_type}" if event_type_prefix else event_type

    with get_db() as db:
        db.execute(
            "INSERT INTO events (event_type, source, payload, matched_routes, status, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
            (logged_type, source, json.dumps(payload), json.dumps(matched_ids),
             "delivered" if matched else "no_match", ts)
        )

    # Deliver to each matched route
    delivered_to = []
    for route in matched:
        config = json.loads(route["destination_config"]) if isinstance(route["destination_config"], str) else route["destination_config"]
        template = route.get("template", "")
        message = render_template(template, payload)
        deliver(route["destination_type"], config, message, payload)
        delivered_to.append({"route": route["name"], "destination": route["destination_type"]})

    return {
        "matched": len(matched),
        "delivered_to": delivered_to,
        "event_type": logged_type
    }


# --- API Routes ---

@app.post("/api/events", status_code=201)
async def receive_event(request: Request):
    """Receive an event. Matches against routes, delivers to destinations, logs everything."""
    data = await request.json()

    event_type = data.get("event", data.get("event_type", ""))
    source = data.get("source", "")

    result = process_event(event_type, source, data)

    return {
        "status": "ok",
        "matched": result["matched"],
        "delivered_to": result["delivered_to"]
    }


@app.post("/api/routes", status_code=201)
def create_route(route: RouteCreate):
    """Create a new route."""
    if route.destination_type not in VALID_DEST_TYPES:
        raise HTTPException(400, f"Invalid destination_type. Use one of: {VALID_DEST_TYPES}")

    route_id = f"rt-{uuid.uuid4().hex[:8]}"
    ts = now_iso()

    with get_db() as db:
        db.execute(
            """INSERT INTO routes (id, name, source_filter, destination_type, destination_config,
               template, enabled, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (route_id, route.name, route.source_filter, route.destination_type,
             json.dumps(route.destination_config), route.template,
             1 if route.enabled else 0, ts, ts)
        )

    return {"id": route_id, "status": "created"}


@app.get("/api/routes")
def list_routes():
    """List all routes."""
    with get_db() as db:
        rows = db.execute("SELECT * FROM routes ORDER BY created_at ASC").fetchall()
    routes = []
    for row in rows:
        route = dict(row)
        route["destination_config"] = json.loads(route["destination_config"])
        route["enabled"] = bool(route["enabled"])
        routes.append(route)
    return routes


@app.get("/api/routes/{route_id}")
def get_route(route_id: str):
    """Get a single route."""
    with get_db() as db:
        row = db.execute("SELECT * FROM routes WHERE id = ?", (route_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Route not found")
    route = dict(row)
    route["destination_config"] = json.loads(route["destination_config"])
    route["enabled"] = bool(route["enabled"])
    return route


@app.patch("/api/routes/{route_id}")
def update_route(route_id: str, update: RouteUpdate):
    """Update a route."""
    with get_db() as db:
        existing = db.execute("SELECT * FROM routes WHERE id = ?", (route_id,)).fetchone()
        if not existing:
            raise HTTPException(404, "Route not found")

        fields = {}
        if update.name is not None:
            fields["name"] = update.name
        if update.source_filter is not None:
            fields["source_filter"] = update.source_filter
        if update.destination_type is not None:
            if update.destination_type not in VALID_DEST_TYPES:
                raise HTTPException(400, f"Invalid destination_type. Use one of: {VALID_DEST_TYPES}")
            fields["destination_type"] = update.destination_type
        if update.destination_config is not None:
            fields["destination_config"] = json.dumps(update.destination_config)
        if update.template is not None:
            fields["template"] = update.template
        if update.enabled is not None:
            fields["enabled"] = 1 if update.enabled else 0

        if not fields:
            return {"status": "no changes"}

        fields["updated_at"] = now_iso()
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [route_id]
        db.execute(f"UPDATE routes SET {set_clause} WHERE id = ?", values)

    return {"status": "updated"}


@app.delete("/api/routes/{route_id}")
def delete_route(route_id: str):
    """Delete a route."""
    with get_db() as db:
        existing = db.execute("SELECT * FROM routes WHERE id = ?", (route_id,)).fetchone()
        if not existing:
            raise HTTPException(404, "Route not found")
        db.execute("DELETE FROM routes WHERE id = ?", (route_id,))
    return {"status": "deleted"}


@app.post("/api/routes/{route_id}/test")
def test_route(route_id: str):
    """Send a test event through a specific route."""
    with get_db() as db:
        row = db.execute("SELECT * FROM routes WHERE id = ?", (route_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Route not found")

    route = dict(row)
    config = json.loads(route["destination_config"])
    test_data = {
        "event": "test",
        "source": "spur",
        "message": "This is a test event from Spur.",
        "timestamp": now_iso()
    }
    message = render_template(route.get("template", ""), test_data)
    deliver(route["destination_type"], config, message, test_data)

    return {"status": "sent", "message": message}


@app.get("/api/events")
def list_events(limit: int = 50, event_type: Optional[str] = None, status: Optional[str] = None):
    """Get recent events from the log."""
    query = "SELECT * FROM events WHERE 1=1"
    params = []
    if event_type:
        query += " AND event_type = ?"
        params.append(event_type)
    if status:
        query += " AND status = ?"
        params.append(status)
    query += " ORDER BY timestamp DESC LIMIT ?"
    params.append(limit)

    with get_db() as db:
        rows = db.execute(query, params).fetchall()
    events = []
    for row in rows:
        event = dict(row)
        event["payload"] = json.loads(event["payload"])
        event["matched_routes"] = json.loads(event["matched_routes"])
        events.append(event)
    return events


@app.post("/api/events/{event_id}/replay")
def replay_event(event_id: int):
    """Replay a single event by ID. Re-processes it through route matching and delivery.
    Logs a new event entry with event_type prefixed as 'replay:' to distinguish from originals."""
    with get_db() as db:
        row = db.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Event not found")

    event = dict(row)
    payload = json.loads(event["payload"])

    # Use the original event_type for matching (strip any existing replay: prefix)
    original_type = event["event_type"]
    if original_type.startswith("replay:"):
        original_type = original_type[len("replay:"):]

    source = event["source"]

    result = process_event(original_type, source, payload, event_type_prefix="replay:")

    return {
        "status": "ok",
        "original_event_id": event_id,
        "replayed_event_type": result["event_type"],
        "matched": result["matched"],
        "delivered_to": result["delivered_to"]
    }


@app.post("/api/events/replay")
def replay_events_bulk(filters: BulkReplayRequest):
    """Replay multiple events matching the given filters.
    Accepts event_type, status, and since (ISO timestamp) as filters.
    Each replayed event is logged with 'replay:' prefix."""
    query = "SELECT * FROM events WHERE 1=1"
    params: list = []

    if filters.event_type:
        query += " AND event_type = ?"
        params.append(filters.event_type)
    if filters.status:
        query += " AND status = ?"
        params.append(filters.status)
    if filters.since:
        query += " AND timestamp >= ?"
        params.append(filters.since)

    query += " ORDER BY timestamp ASC"

    with get_db() as db:
        rows = db.execute(query, params).fetchall()

    results = []
    for row in rows:
        event = dict(row)
        payload = json.loads(event["payload"])

        original_type = event["event_type"]
        if original_type.startswith("replay:"):
            original_type = original_type[len("replay:"):]

        source = event["source"]

        result = process_event(original_type, source, payload, event_type_prefix="replay:")
        results.append({
            "original_event_id": event["id"],
            "replayed_event_type": result["event_type"],
            "matched": result["matched"],
            "delivered_to": result["delivered_to"]
        })

    return {
        "status": "ok",
        "total_replayed": len(results),
        "results": results
    }


@app.get("/api/events/stats")
def event_stats():
    """Get event statistics."""
    with get_db() as db:
        total = db.execute("SELECT COUNT(*) as c FROM events").fetchone()["c"]
        delivered = db.execute("SELECT COUNT(*) as c FROM events WHERE status = 'delivered'").fetchone()["c"]
        no_match = db.execute("SELECT COUNT(*) as c FROM events WHERE status = 'no_match'").fetchone()["c"]
        routes = db.execute("SELECT COUNT(*) as c FROM routes WHERE enabled = 1").fetchone()["c"]
    return {
        "total_events": total,
        "delivered": delivered,
        "no_match": no_match,
        "active_routes": routes
    }


@app.get("/health")
def health():
    return {"status": "ok", "service": "spur", "timestamp": now_iso()}


# --- Static files & SPA fallback ---

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

@app.get("/")
def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


# --- Startup ---

@app.on_event("startup")
def startup():
    init_db()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT)
