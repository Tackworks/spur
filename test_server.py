"""
Comprehensive test suite for the Spur webhook event relay.

Covers: health, route CRUD, validation, event processing, source filter matching,
template rendering, event replay, stats, route test endpoint, enable/disable,
Matrix delivery, and event log filtering.

Each test gets a fresh temporary database via the ``client`` fixture.
"""

import json
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def client(tmp_path):
    """Yield a TestClient backed by a throwaway SQLite database."""
    db_file = tmp_path / "spur_test.db"

    # Patch DB_PATH *before* importing anything that uses it at module scope.
    import server as srv
    original_db_path = srv.DB_PATH
    srv.DB_PATH = db_file

    # Initialise the schema in the temp DB.
    srv.init_db()

    with TestClient(srv.app) as tc:
        yield tc

    # Restore so other test modules (if any) aren't affected.
    srv.DB_PATH = original_db_path


def _make_route(client, **overrides):
    """Helper: create a route and return the JSON response."""
    payload = {
        "name": overrides.get("name", "test-route"),
        "destination_type": overrides.get("destination_type", "http"),
        "destination_config": overrides.get("destination_config", {"url": "http://localhost:9999/hook"}),
        "source_filter": overrides.get("source_filter", ""),
        "template": overrides.get("template", ""),
        "enabled": overrides.get("enabled", True),
    }
    resp = client.post("/api/routes", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# 1. Health endpoint
# ---------------------------------------------------------------------------

class TestHealth:
    def test_health_returns_ok(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["service"] == "spur"
        assert "timestamp" in body


# ---------------------------------------------------------------------------
# 2. Route CRUD
# ---------------------------------------------------------------------------

class TestRouteCRUD:
    def test_create_route(self, client):
        data = _make_route(client, name="my-route")
        assert data["status"] == "created"
        assert data["id"].startswith("rt-")

    def test_list_routes_empty(self, client):
        resp = client.get("/api/routes")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_routes_populated(self, client):
        _make_route(client, name="route-a")
        _make_route(client, name="route-b")
        routes = client.get("/api/routes").json()
        assert len(routes) == 2
        names = {r["name"] for r in routes}
        assert names == {"route-a", "route-b"}

    def test_get_route_by_id(self, client):
        created = _make_route(client, name="single")
        route_id = created["id"]
        resp = client.get(f"/api/routes/{route_id}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == route_id
        assert body["name"] == "single"
        assert body["enabled"] is True

    def test_get_route_not_found(self, client):
        resp = client.get("/api/routes/rt-nonexistent")
        assert resp.status_code == 404

    def test_update_route_name(self, client):
        route_id = _make_route(client)["id"]
        resp = client.patch(f"/api/routes/{route_id}", json={"name": "renamed"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "updated"
        updated = client.get(f"/api/routes/{route_id}").json()
        assert updated["name"] == "renamed"

    def test_update_route_no_changes(self, client):
        route_id = _make_route(client)["id"]
        resp = client.patch(f"/api/routes/{route_id}", json={})
        assert resp.status_code == 200
        assert resp.json()["status"] == "no changes"

    def test_update_route_not_found(self, client):
        resp = client.patch("/api/routes/rt-ghost", json={"name": "x"})
        assert resp.status_code == 404

    def test_delete_route(self, client):
        route_id = _make_route(client)["id"]
        resp = client.delete(f"/api/routes/{route_id}")
        assert resp.status_code == 200
        assert resp.json()["status"] == "deleted"
        # Confirm gone
        assert client.get(f"/api/routes/{route_id}").status_code == 404

    def test_delete_route_not_found(self, client):
        resp = client.delete("/api/routes/rt-nope")
        assert resp.status_code == 404

    def test_route_destination_config_roundtrip(self, client):
        cfg = {"bot_token": "tok123", "chat_id": "456"}
        route_id = _make_route(client, destination_type="telegram", destination_config=cfg)["id"]
        fetched = client.get(f"/api/routes/{route_id}").json()
        assert fetched["destination_config"] == cfg

    def test_created_at_and_updated_at(self, client):
        route_id = _make_route(client)["id"]
        route = client.get(f"/api/routes/{route_id}").json()
        assert "created_at" in route
        assert "updated_at" in route


# ---------------------------------------------------------------------------
# 3. Invalid destination type validation
# ---------------------------------------------------------------------------

class TestDestinationTypeValidation:
    def test_create_route_invalid_dest_type(self, client):
        resp = client.post("/api/routes", json={
            "name": "bad",
            "destination_type": "pigeon",
            "destination_config": {},
        })
        assert resp.status_code == 400
        assert "Invalid destination_type" in resp.json()["detail"]

    def test_update_route_invalid_dest_type(self, client):
        route_id = _make_route(client)["id"]
        resp = client.patch(f"/api/routes/{route_id}", json={"destination_type": "carrier_owl"})
        assert resp.status_code == 400
        assert "Invalid destination_type" in resp.json()["detail"]

    @pytest.mark.parametrize("dtype", ["telegram", "slack", "discord", "matrix", "http"])
    def test_all_valid_dest_types_accepted(self, client, dtype):
        resp = client.post("/api/routes", json={
            "name": f"route-{dtype}",
            "destination_type": dtype,
            "destination_config": {},
        })
        assert resp.status_code == 201


# ---------------------------------------------------------------------------
# 4. Event processing (receive, match, delivery)
# ---------------------------------------------------------------------------

class TestEventProcessing:
    def test_receive_event_no_routes(self, client):
        resp = client.post("/api/events", json={
            "event": "push",
            "source": "github",
        })
        assert resp.status_code == 201
        body = resp.json()
        assert body["status"] == "ok"
        assert body["matched"] == 0
        assert body["delivered_to"] == []

    @patch("server.deliver")
    def test_receive_event_matches_route(self, mock_deliver, client):
        _make_route(client, name="catch-all", source_filter="")
        resp = client.post("/api/events", json={
            "event": "deploy",
            "source": "ci",
        })
        assert resp.status_code == 201
        body = resp.json()
        assert body["matched"] == 1
        assert body["delivered_to"][0]["route"] == "catch-all"
        mock_deliver.assert_called_once()

    @patch("server.deliver")
    def test_event_logged_as_delivered(self, mock_deliver, client):
        _make_route(client, name="r1")
        client.post("/api/events", json={"event": "x", "source": "y"})
        events = client.get("/api/events").json()
        assert len(events) == 1
        assert events[0]["status"] == "delivered"
        assert len(events[0]["matched_routes"]) == 1
        assert events[0]["matched_routes"][0].startswith("rt-")

    def test_event_logged_as_no_match(self, client):
        client.post("/api/events", json={"event": "x", "source": "y"})
        events = client.get("/api/events").json()
        assert len(events) == 1
        assert events[0]["status"] == "no_match"

    @patch("server.deliver")
    def test_event_uses_event_type_alias(self, mock_deliver, client):
        """The API should accept 'event_type' as well as 'event'."""
        _make_route(client, source_filter="event_type:ping")
        resp = client.post("/api/events", json={"event_type": "ping", "source": "test"})
        assert resp.status_code == 201
        assert resp.json()["matched"] == 1

    @patch("server.deliver")
    def test_disabled_route_not_matched(self, mock_deliver, client):
        route_id = _make_route(client, name="disabled-route")["id"]
        client.patch(f"/api/routes/{route_id}", json={"enabled": False})
        resp = client.post("/api/events", json={"event": "push", "source": "gh"})
        assert resp.json()["matched"] == 0
        mock_deliver.assert_not_called()

    @patch("server.deliver")
    def test_multiple_routes_matched(self, mock_deliver, client):
        _make_route(client, name="r1", source_filter="event:push")
        _make_route(client, name="r2", source_filter="event:push")
        resp = client.post("/api/events", json={"event": "push", "source": "ci"})
        assert resp.json()["matched"] == 2
        assert mock_deliver.call_count == 2


# ---------------------------------------------------------------------------
# 5. Source filter matching (exact, wildcard, multi-condition)
# ---------------------------------------------------------------------------

class TestSourceFilterMatching:
    @patch("server.deliver")
    def test_exact_event_match(self, mock_deliver, client):
        _make_route(client, name="exact", source_filter="event:push")
        resp = client.post("/api/events", json={"event": "push", "source": "gh"})
        assert resp.json()["matched"] == 1

    @patch("server.deliver")
    def test_exact_event_no_match(self, mock_deliver, client):
        _make_route(client, name="exact", source_filter="event:push")
        resp = client.post("/api/events", json={"event": "deploy", "source": "ci"})
        assert resp.json()["matched"] == 0

    @patch("server.deliver")
    def test_source_field_match(self, mock_deliver, client):
        _make_route(client, name="src", source_filter="source:github")
        resp = client.post("/api/events", json={"event": "push", "source": "github"})
        assert resp.json()["matched"] == 1

    @patch("server.deliver")
    def test_source_field_no_match(self, mock_deliver, client):
        _make_route(client, name="src", source_filter="source:github")
        resp = client.post("/api/events", json={"event": "push", "source": "gitlab"})
        assert resp.json()["matched"] == 0

    @patch("server.deliver")
    def test_wildcard_suffix(self, mock_deliver, client):
        _make_route(client, name="wild-suffix", source_filter="event:push*")
        for evt in ("push", "push.main", "pushed"):
            resp = client.post("/api/events", json={"event": evt, "source": "x"})
            assert resp.json()["matched"] == 1, f"Expected match for event={evt}"

    @patch("server.deliver")
    def test_wildcard_prefix(self, mock_deliver, client):
        _make_route(client, name="wild-prefix", source_filter="source:*hub")
        resp = client.post("/api/events", json={"event": "push", "source": "github"})
        assert resp.json()["matched"] == 1
        mock_deliver.reset_mock()
        resp = client.post("/api/events", json={"event": "push", "source": "gitlab"})
        assert resp.json()["matched"] == 0

    @patch("server.deliver")
    def test_wildcard_contains(self, mock_deliver, client):
        _make_route(client, name="wild-contains", source_filter="source:*hub*")
        resp = client.post("/api/events", json={"event": "push", "source": "my-github-mirror"})
        assert resp.json()["matched"] == 1

    @patch("server.deliver")
    def test_multi_condition_all_must_match(self, mock_deliver, client):
        _make_route(client, name="multi", source_filter="event:push,source:github")
        # Both match
        resp = client.post("/api/events", json={"event": "push", "source": "github"})
        assert resp.json()["matched"] == 1
        mock_deliver.reset_mock()
        # Event wrong
        resp = client.post("/api/events", json={"event": "deploy", "source": "github"})
        assert resp.json()["matched"] == 0
        # Source wrong
        resp = client.post("/api/events", json={"event": "push", "source": "gitlab"})
        assert resp.json()["matched"] == 0

    @patch("server.deliver")
    def test_bare_value_matches_event_type(self, mock_deliver, client):
        _make_route(client, name="bare", source_filter="push")
        resp = client.post("/api/events", json={"event": "push", "source": "x"})
        assert resp.json()["matched"] == 1
        mock_deliver.reset_mock()
        resp = client.post("/api/events", json={"event": "deploy", "source": "x"})
        assert resp.json()["matched"] == 0

    @patch("server.deliver")
    def test_payload_field_filter(self, mock_deliver, client):
        _make_route(client, name="payload", source_filter="action:opened")
        resp = client.post("/api/events", json={
            "event": "issue",
            "source": "gh",
            "action": "opened",
        })
        assert resp.json()["matched"] == 1
        mock_deliver.reset_mock()
        resp = client.post("/api/events", json={
            "event": "issue",
            "source": "gh",
            "action": "closed",
        })
        assert resp.json()["matched"] == 0

    @patch("server.deliver")
    def test_empty_filter_matches_everything(self, mock_deliver, client):
        _make_route(client, name="catch-all", source_filter="")
        resp = client.post("/api/events", json={"event": "anything", "source": "anywhere"})
        assert resp.json()["matched"] == 1


# ---------------------------------------------------------------------------
# 6. Template rendering
# ---------------------------------------------------------------------------

class TestTemplateRendering:
    @patch("server.deliver")
    def test_template_with_placeholders(self, mock_deliver, client):
        _make_route(
            client,
            name="tpl",
            source_filter="",
            template="Event: {event}, Source: {source}, Action: {action}",
        )
        client.post("/api/events", json={
            "event": "push",
            "source": "github",
            "action": "completed",
        })
        # Check the message passed to deliver
        mock_deliver.assert_called_once()
        delivered_message = mock_deliver.call_args[0][2]  # 3rd positional arg: message
        assert "Event: push" in delivered_message
        assert "Source: github" in delivered_message
        assert "Action: completed" in delivered_message

    @patch("server.deliver")
    def test_template_nested_placeholders(self, mock_deliver, client):
        _make_route(
            client,
            name="nested",
            source_filter="",
            template="Title: {details.title}",
        )
        client.post("/api/events", json={
            "event": "alert",
            "source": "monitor",
            "details": {"title": "CPU High"},
        })
        mock_deliver.assert_called_once()
        delivered_message = mock_deliver.call_args[0][2]
        assert "Title: CPU High" in delivered_message

    @patch("server.deliver")
    def test_no_template_uses_default_format(self, mock_deliver, client):
        _make_route(client, name="no-tpl", source_filter="", template="")
        client.post("/api/events", json={
            "event": "deploy",
            "source": "ci",
            "message": "Deployment succeeded",
        })
        mock_deliver.assert_called_once()
        delivered_message = mock_deliver.call_args[0][2]
        assert "[deploy from ci]" in delivered_message
        assert "Deployment succeeded" in delivered_message

    @patch("server.deliver")
    def test_default_format_no_source(self, mock_deliver, client):
        _make_route(client, name="no-src", source_filter="", template="")
        client.post("/api/events", json={
            "event": "heartbeat",
            "title": "alive",
        })
        mock_deliver.assert_called_once()
        delivered_message = mock_deliver.call_args[0][2]
        assert "[heartbeat]" in delivered_message
        assert "alive" in delivered_message

    @patch("server.deliver")
    def test_default_format_with_details(self, mock_deliver, client):
        _make_route(client, name="details", source_filter="", template="")
        client.post("/api/events", json={
            "event": "metric",
            "source": "monitor",
            "details": {"cpu": "95%", "mem": "80%", "disk": "50%"},
        })
        mock_deliver.assert_called_once()
        delivered_message = mock_deliver.call_args[0][2]
        assert "cpu: 95%" in delivered_message
        assert "mem: 80%" in delivered_message
        assert "disk: 50%" in delivered_message


# ---------------------------------------------------------------------------
# 7. Event replay (single and bulk)
# ---------------------------------------------------------------------------

class TestEventReplay:
    @patch("server.deliver")
    def test_replay_single_event(self, mock_deliver, client):
        # Create a route and send an original event
        _make_route(client, name="replay-route", source_filter="event:push")
        client.post("/api/events", json={"event": "push", "source": "gh"})
        mock_deliver.reset_mock()

        events = client.get("/api/events").json()
        event_id = events[0]["id"]

        resp = client.post(f"/api/events/{event_id}/replay")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["original_event_id"] == event_id
        assert body["replayed_event_type"] == "replay:push"
        assert body["matched"] == 1
        mock_deliver.assert_called_once()

    def test_replay_event_not_found(self, client):
        resp = client.post("/api/events/99999/replay")
        assert resp.status_code == 404

    @patch("server.deliver")
    def test_replay_strips_existing_prefix(self, mock_deliver, client):
        """Replaying an already-replayed event should not double-prefix."""
        _make_route(client, name="rr", source_filter="event:push")
        client.post("/api/events", json={"event": "push", "source": "x"})
        mock_deliver.reset_mock()

        events = client.get("/api/events").json()
        eid = events[0]["id"]
        # First replay
        client.post(f"/api/events/{eid}/replay")
        # Get the replay event
        all_events = client.get("/api/events").json()
        replay_event = [e for e in all_events if e["event_type"] == "replay:push"][0]
        # Second replay of the replayed event
        resp = client.post(f"/api/events/{replay_event['id']}/replay")
        assert resp.json()["replayed_event_type"] == "replay:push"  # Not replay:replay:push

    @patch("server.deliver")
    def test_replay_bulk_by_event_type(self, mock_deliver, client):
        _make_route(client, name="bulk-r", source_filter="event:alert")
        client.post("/api/events", json={"event": "alert", "source": "a"})
        client.post("/api/events", json={"event": "alert", "source": "b"})
        client.post("/api/events", json={"event": "push", "source": "c"})
        mock_deliver.reset_mock()

        resp = client.post("/api/events/replay", json={"event_type": "alert"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_replayed"] == 2
        assert len(body["results"]) == 2

    @patch("server.deliver")
    def test_replay_bulk_by_status(self, mock_deliver, client):
        # No routes -> all events will be no_match
        client.post("/api/events", json={"event": "a", "source": "x"})
        client.post("/api/events", json={"event": "b", "source": "x"})

        resp = client.post("/api/events/replay", json={"status": "no_match"})
        body = resp.json()
        assert body["total_replayed"] == 2

    @patch("server.deliver")
    def test_replay_bulk_empty_filters(self, mock_deliver, client):
        client.post("/api/events", json={"event": "a", "source": "x"})
        resp = client.post("/api/events/replay", json={})
        body = resp.json()
        assert body["total_replayed"] == 1


# ---------------------------------------------------------------------------
# 8. Event stats
# ---------------------------------------------------------------------------

class TestEventStats:
    def test_stats_empty(self, client):
        resp = client.get("/api/events/stats")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_events"] == 0
        assert body["delivered"] == 0
        assert body["no_match"] == 0
        assert body["active_routes"] == 0

    @patch("server.deliver")
    def test_stats_after_events(self, mock_deliver, client):
        _make_route(client, name="stat-route", source_filter="event:push")
        # 2 matching events -> delivered
        client.post("/api/events", json={"event": "push", "source": "x"})
        client.post("/api/events", json={"event": "push", "source": "y"})
        # 1 non-matching event -> no_match
        client.post("/api/events", json={"event": "deploy", "source": "z"})

        stats = client.get("/api/events/stats").json()
        assert stats["total_events"] == 3
        assert stats["delivered"] == 2
        assert stats["no_match"] == 1
        assert stats["active_routes"] == 1

    @patch("server.deliver")
    def test_stats_active_routes_ignores_disabled(self, mock_deliver, client):
        r1 = _make_route(client, name="enabled")["id"]
        r2 = _make_route(client, name="disabled")["id"]
        client.patch(f"/api/routes/{r2}", json={"enabled": False})
        stats = client.get("/api/events/stats").json()
        assert stats["active_routes"] == 1


# ---------------------------------------------------------------------------
# 9. Route test endpoint
# ---------------------------------------------------------------------------

class TestRouteTestEndpoint:
    @patch("server.deliver")
    def test_route_test_sends_test_event(self, mock_deliver, client):
        route_id = _make_route(client, name="test-me", destination_type="http",
                               destination_config={"url": "http://example.com/hook"})["id"]
        resp = client.post(f"/api/routes/{route_id}/test")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "sent"
        assert "message" in body
        mock_deliver.assert_called_once()
        # Verify deliver was called with correct dest_type
        call_args = mock_deliver.call_args[0]
        assert call_args[0] == "http"  # dest_type

    def test_route_test_not_found(self, client):
        resp = client.post("/api/routes/rt-missing/test")
        assert resp.status_code == 404

    @patch("server.deliver")
    def test_route_test_uses_template(self, mock_deliver, client):
        route_id = _make_route(
            client,
            name="templated-test",
            template="TEST: {message}",
        )["id"]
        resp = client.post(f"/api/routes/{route_id}/test")
        body = resp.json()
        assert "TEST: This is a test event from Spur." in body["message"]


# ---------------------------------------------------------------------------
# 10. Enable / disable routes
# ---------------------------------------------------------------------------

class TestEnableDisableRoutes:
    def test_create_disabled_route(self, client):
        route_id = _make_route(client, name="off", enabled=False)["id"]
        route = client.get(f"/api/routes/{route_id}").json()
        assert route["enabled"] is False

    def test_disable_existing_route(self, client):
        route_id = _make_route(client, name="on-then-off")["id"]
        client.patch(f"/api/routes/{route_id}", json={"enabled": False})
        route = client.get(f"/api/routes/{route_id}").json()
        assert route["enabled"] is False

    def test_re_enable_route(self, client):
        route_id = _make_route(client, name="toggle", enabled=False)["id"]
        client.patch(f"/api/routes/{route_id}", json={"enabled": True})
        route = client.get(f"/api/routes/{route_id}").json()
        assert route["enabled"] is True

    @patch("server.deliver")
    def test_disabled_route_skipped_in_matching(self, mock_deliver, client):
        route_id = _make_route(client, name="disabled", source_filter="event:test", enabled=False)["id"]
        resp = client.post("/api/events", json={"event": "test", "source": "x"})
        assert resp.json()["matched"] == 0

    @patch("server.deliver")
    def test_enabled_route_matched_after_toggle(self, mock_deliver, client):
        route_id = _make_route(client, name="toggled", source_filter="event:test", enabled=False)["id"]
        # Disabled: no match
        resp = client.post("/api/events", json={"event": "test", "source": "x"})
        assert resp.json()["matched"] == 0
        # Enable it
        client.patch(f"/api/routes/{route_id}", json={"enabled": True})
        mock_deliver.reset_mock()
        # Now it should match
        resp = client.post("/api/events", json={"event": "test", "source": "x"})
        assert resp.json()["matched"] == 1


# ---------------------------------------------------------------------------
# 11. Matrix destination delivery (mock urllib)
# ---------------------------------------------------------------------------

class TestMatrixDelivery:
    @patch("server.urllib.request.urlopen")
    def test_matrix_delivery_constructs_correct_request(self, mock_urlopen, client):
        """Verify _send_matrix builds the right URL, headers, and payload."""
        import server as srv

        config = {
            "homeserver": "https://matrix.example.com",
            "room_id": "!room123:example.com",
            "access_token": "syt_secret_token",
        }
        message = "Hello from Spur"

        srv._send_matrix(config, message)

        mock_urlopen.assert_called_once()
        req = mock_urlopen.call_args[0][0]
        assert "/_matrix/client/v3/rooms/" in req.full_url
        assert req.method == "PUT"
        assert req.get_header("Authorization") == "Bearer syt_secret_token"
        assert req.get_header("Content-type") == "application/json"

        body = json.loads(req.data.decode())
        assert body["msgtype"] == "m.text"
        assert body["body"] == "Hello from Spur"

    @patch("server.urllib.request.urlopen")
    def test_matrix_missing_config_does_not_call(self, mock_urlopen, client):
        import server as srv
        # Missing room_id
        srv._send_matrix({"homeserver": "https://m.example.com", "access_token": "tok"}, "msg")
        mock_urlopen.assert_not_called()

    @patch("server.urllib.request.urlopen")
    def test_matrix_homeserver_trailing_slash_stripped(self, mock_urlopen, client):
        import server as srv
        config = {
            "homeserver": "https://matrix.example.com/",
            "room_id": "!room:example.com",
            "access_token": "tok",
        }
        srv._send_matrix(config, "msg")
        req = mock_urlopen.call_args[0][0]
        # Should not have double slash
        assert "///_matrix" not in req.full_url

    @patch("server.urllib.request.urlopen")
    def test_telegram_delivery(self, mock_urlopen, client):
        import server as srv
        config = {"bot_token": "123:ABC", "chat_id": "789"}
        srv._send_telegram(config, "hello")
        mock_urlopen.assert_called_once()
        url = mock_urlopen.call_args[0][0]
        assert "api.telegram.org/bot123:ABC/sendMessage" in url
        assert "chat_id=789" in url

    @patch("server.urllib.request.urlopen")
    def test_slack_delivery(self, mock_urlopen, client):
        import server as srv
        config = {"webhook_url": "https://hooks.slack.com/test"}
        srv._send_slack(config, "hello")
        mock_urlopen.assert_called_once()
        req = mock_urlopen.call_args[0][0]
        assert req.full_url == "https://hooks.slack.com/test"
        body = json.loads(req.data.decode())
        assert body["text"] == "hello"

    @patch("server.urllib.request.urlopen")
    def test_discord_delivery(self, mock_urlopen, client):
        import server as srv
        config = {"webhook_url": "https://discord.com/api/webhooks/test"}
        srv._send_discord(config, "hello")
        mock_urlopen.assert_called_once()
        req = mock_urlopen.call_args[0][0]
        body = json.loads(req.data.decode())
        assert body["content"] == "hello"

    @patch("server.urllib.request.urlopen")
    def test_http_delivery(self, mock_urlopen, client):
        import server as srv
        config = {"url": "https://example.com/hook", "method": "POST", "headers": {"X-Custom": "val"}}
        event_data = {"event": "test", "source": "spur"}
        srv._send_http(config, "hello", event_data)
        mock_urlopen.assert_called_once()
        req = mock_urlopen.call_args[0][0]
        assert req.full_url == "https://example.com/hook"
        assert req.method == "POST"
        assert req.get_header("X-custom") == "val"
        body = json.loads(req.data.decode())
        assert body["message"] == "hello"
        assert body["event"] == event_data

    @patch("server.urllib.request.urlopen")
    def test_delivery_missing_config_graceful(self, mock_urlopen, client):
        """Delivery functions with missing required config should not call urlopen."""
        import server as srv
        srv._send_telegram({}, "msg")
        srv._send_slack({}, "msg")
        srv._send_discord({}, "msg")
        srv._send_http({}, "msg", {})
        mock_urlopen.assert_not_called()


# ---------------------------------------------------------------------------
# 12. Event log filtering
# ---------------------------------------------------------------------------

class TestEventLogFiltering:
    def test_list_events_default(self, client):
        client.post("/api/events", json={"event": "a", "source": "x"})
        client.post("/api/events", json={"event": "b", "source": "y"})
        events = client.get("/api/events").json()
        assert len(events) == 2

    def test_list_events_limit(self, client):
        for i in range(5):
            client.post("/api/events", json={"event": f"e{i}", "source": "x"})
        events = client.get("/api/events?limit=3").json()
        assert len(events) == 3

    def test_list_events_filter_by_event_type(self, client):
        client.post("/api/events", json={"event": "push", "source": "a"})
        client.post("/api/events", json={"event": "deploy", "source": "b"})
        client.post("/api/events", json={"event": "push", "source": "c"})
        events = client.get("/api/events?event_type=push").json()
        assert len(events) == 2
        assert all(e["event_type"] == "push" for e in events)

    @patch("server.deliver")
    def test_list_events_filter_by_status(self, mock_deliver, client):
        _make_route(client, name="r", source_filter="event:push")
        client.post("/api/events", json={"event": "push", "source": "a"})
        client.post("/api/events", json={"event": "other", "source": "b"})

        delivered = client.get("/api/events?status=delivered").json()
        assert len(delivered) == 1
        assert delivered[0]["status"] == "delivered"

        no_match = client.get("/api/events?status=no_match").json()
        assert len(no_match) == 1
        assert no_match[0]["status"] == "no_match"

    @patch("server.deliver")
    def test_list_events_combined_filters(self, mock_deliver, client):
        _make_route(client, name="r", source_filter="event:push")
        client.post("/api/events", json={"event": "push", "source": "a"})
        client.post("/api/events", json={"event": "push", "source": "b"})
        client.post("/api/events", json={"event": "deploy", "source": "c"})

        events = client.get("/api/events?event_type=push&status=delivered").json()
        assert len(events) == 2

    def test_list_events_order_descending(self, client):
        client.post("/api/events", json={"event": "first", "source": "x"})
        client.post("/api/events", json={"event": "second", "source": "x"})
        events = client.get("/api/events").json()
        # Most recent first
        assert events[0]["event_type"] == "second"
        assert events[1]["event_type"] == "first"

    def test_list_events_payload_deserialized(self, client):
        client.post("/api/events", json={"event": "test", "source": "x", "data": {"key": "val"}})
        events = client.get("/api/events").json()
        assert isinstance(events[0]["payload"], dict)
        assert events[0]["payload"]["data"] == {"key": "val"}

    def test_list_events_matched_routes_deserialized(self, client):
        client.post("/api/events", json={"event": "test", "source": "x"})
        events = client.get("/api/events").json()
        assert isinstance(events[0]["matched_routes"], list)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    @patch("server.deliver")
    def test_route_update_multiple_fields(self, mock_deliver, client):
        route_id = _make_route(client, name="orig")["id"]
        resp = client.patch(f"/api/routes/{route_id}", json={
            "name": "new-name",
            "source_filter": "event:deploy",
            "template": "Deploy: {message}",
            "enabled": False,
        })
        assert resp.json()["status"] == "updated"
        route = client.get(f"/api/routes/{route_id}").json()
        assert route["name"] == "new-name"
        assert route["source_filter"] == "event:deploy"
        assert route["template"] == "Deploy: {message}"
        assert route["enabled"] is False

    def test_event_with_empty_body(self, client):
        resp = client.post("/api/events", json={})
        assert resp.status_code == 201
        body = resp.json()
        assert body["matched"] == 0

    @patch("server.deliver")
    def test_large_payload_survives_roundtrip(self, mock_deliver, client):
        big_data = {f"key_{i}": f"value_{i}" for i in range(100)}
        big_data["event"] = "big"
        big_data["source"] = "test"
        client.post("/api/events", json=big_data)
        events = client.get("/api/events").json()
        assert events[0]["payload"]["key_50"] == "value_50"

    @patch("server.deliver")
    def test_discord_message_truncation(self, mock_deliver, client):
        """Discord has a 2000-char limit. Verify truncation."""
        import server as srv
        with patch("server.urllib.request.urlopen") as mock_url:
            long_msg = "x" * 3000
            srv._send_discord({"webhook_url": "https://discord.com/api/webhooks/test"}, long_msg)
            req = mock_url.call_args[0][0]
            body = json.loads(req.data.decode())
            assert len(body["content"]) == 2000

    @patch("server.deliver")
    def test_telegram_message_truncation(self, mock_deliver, client):
        """Telegram has a 4096-char limit. Verify truncation."""
        import server as srv
        with patch("server.urllib.request.urlopen") as mock_url:
            long_msg = "x" * 5000
            srv._send_telegram({"bot_token": "tok", "chat_id": "123"}, long_msg)
            url = mock_url.call_args[0][0]
            # The text param in the URL should be truncated to 4096
            assert "text=" in url
