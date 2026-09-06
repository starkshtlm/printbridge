"""Identity: one key per person, the same principal by path or header, 404 for everything else."""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from printbridge.app import KeyRoutingMiddleware
from printbridge.auth import SCOPE_CLIENT_IP, SCOPE_PRINCIPAL, KeyAuth, Principal
from printbridge.keys import KeyStore


class Recorder:
    """Stands in for the MCP app and remembers what the middleware handed it."""

    def __init__(self):
        self.scopes: list[dict] = []

    async def __call__(self, scope, receive, send):
        self.scopes.append(dict(scope))
        await send({"type": "http.response.start", "status": 200, "headers": [(b"x-ok", b"1")]})
        await send({"type": "http.response.body", "body": b"ok"})


@pytest.fixture
def guarded(runtime):
    recorder = Recorder()
    return recorder, TestClient(KeyRoutingMiddleware(recorder, runtime))


def test_path_key_and_bearer_give_the_same_principal(runtime, guarded):
    recorder, client = guarded
    key, _ = runtime.keys.add("daniel", role="admin")

    assert client.get(f"/mcp/{key}").status_code == 200
    assert client.post("/mcp", headers={"Authorization": f"Bearer {key}"}).status_code == 200

    by_path, by_header = (s[SCOPE_PRINCIPAL] for s in recorder.scopes)
    assert by_path.user == by_header.user == "daniel"
    assert by_path.role == by_header.role == "admin"
    assert (by_path.method, by_header.method) == ("key-path", "key-bearer")


def test_the_key_never_reaches_the_mcp_layer(runtime, guarded):
    recorder, client = guarded
    key, _ = runtime.keys.add("daniel")
    client.get(f"/mcp/{key}")
    assert recorder.scopes[0]["path"] == "/mcp"
    assert key not in recorder.scopes[0]["path"]


@pytest.mark.parametrize("path", ["/mcp/pb_wrongkey", "/mcp/", "/mcp/almost/right"])
def test_unknown_key_is_404_not_401(runtime, guarded, path):
    _, client = guarded
    runtime.keys.add("daniel")
    assert client.get(path).status_code == 404


def test_bearer_with_an_unknown_key_is_404(runtime, guarded):
    _, client = guarded
    runtime.keys.add("daniel")
    assert client.post("/mcp", headers={"Authorization": "Bearer pb_nope"}).status_code == 404


def test_revoked_key_stops_working_immediately(runtime, guarded):
    _, client = guarded
    key, _ = runtime.keys.add("fanny")
    assert client.get(f"/mcp/{key}").status_code == 200
    runtime.keys.revoke("fanny")
    assert client.get(f"/mcp/{key}").status_code == 404


def test_rotation_keeps_the_old_link_alive_for_the_grace_period(runtime, guarded):
    _, client = guarded
    old, _ = runtime.keys.add("fanny")
    new, _ = runtime.keys.rotate("fanny", grace_minutes=60)
    assert client.get(f"/mcp/{old}").status_code == 200
    assert client.get(f"/mcp/{new}").status_code == 200


def test_rotation_with_no_grace_kills_the_old_link(runtime, guarded):
    _, client = guarded
    old, _ = runtime.keys.add("fanny")
    runtime.keys.rotate("fanny", grace_minutes=0)
    assert client.get(f"/mcp/{old}").status_code == 404


def test_revoking_one_person_leaves_the_others_alone(runtime, guarded):
    _, client = guarded
    daniel, _ = runtime.keys.add("daniel")
    fanny, _ = runtime.keys.add("fanny")
    runtime.keys.revoke("fanny")
    assert client.get(f"/mcp/{daniel}").status_code == 200
    assert client.get(f"/mcp/{fanny}").status_code == 404


def test_key_guessing_raises_one_alert_not_twenty(runtime, guarded):
    _, client = guarded
    runtime.keys.add("daniel")
    for _ in range(25):
        client.get("/mcp/pb_guess")
    events = [e for e, _ in runtime.notifier.sent if e == "probe_404"]
    assert len(events) == 1


def test_forwarded_ip_is_preferred_over_the_socket(runtime, guarded):
    recorder, client = guarded
    key, _ = runtime.keys.add("daniel")
    client.get(f"/mcp/{key}", headers={"X-Forwarded-For": "198.51.100.7, 10.0.0.1"})
    assert recorder.scopes[0][SCOPE_CLIENT_IP] == "198.51.100.7"


def test_a_second_key_for_the_same_person_is_still_that_person(runtime):
    store: KeyStore = runtime.keys
    personal, _ = store.add("daniel", role="admin")
    agent, agent_record = store.add("daniel", label="agent")
    auth = KeyAuth(store=store, grace_minutes=60)
    first = auth.resolve(path_key=personal, bearer=None)
    second = auth.resolve(path_key=None, bearer=agent)
    assert first.user == second.user == "daniel"
    assert second.key_label == "agent"
    assert agent_record.name == "daniel/agent"


def test_per_key_limits_travel_with_the_principal(runtime):
    store: KeyStore = runtime.keys
    key, _ = store.add("fanny", printers=["home"], max_pages=20, rate_per_hour=10)
    principal = KeyAuth(store=store).resolve(path_key=key, bearer=None)
    assert principal.printers == ("home",)
    assert principal.page_ceiling(100) == 20
    assert principal.rate_ceiling(30) == 10
    assert principal.may_print_to("home") and not principal.may_print_to("office")


def test_an_admin_key_is_not_created_by_accident(runtime):
    key, record = runtime.keys.add("fanny")
    assert record.role == "user"
    assert not Principal.from_key(record, "key-path").is_admin
