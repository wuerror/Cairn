from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient

from cairn.dispatcher.harness import (
    AllowlistFence,
    execute_allowed_request,
    resolve_credentials_ref,
)
from cairn.server import db
from cairn.server.app import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(db_path)
    with TestClient(app) as c:
        yield c


class _RceHandler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8", errors="replace")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        if "yaml.load" in body or "RCE" in body or "!!python" in body:
            self.wfile.write(b"CAIRN_POC_OK token=abc")
        else:
            self.wfile.write(b"imported ok")

    def log_message(self, format, *args):  # noqa: A003
        return


@pytest.fixture()
def rce_server():
    server = HTTPServer(("127.0.0.1", 0), _RceHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


def test_execute_allowed_request_hits_real_target(rce_server):
    hostport = rce_server.replace("http://", "")
    result = execute_allowed_request(
        base_url=rce_server,
        endpoint="POST /api/import",
        allowlist=[hostport, "127.0.0.1"],
        payload_body="config=!!python/object/apply:os.system RCE",
        success_signature={"kind": "response_match", "check": "CAIRN_POC_OK"},
    )
    assert result.triggered is True
    assert result.evidence and "matched" in result.evidence


def test_execute_allowed_request_blocks_offlist_without_socket():
    # evil host must never be contacted — allowlist fails closed
    result = execute_allowed_request(
        base_url="http://203.0.113.9:9",
        endpoint="/x",
        allowlist=["127.0.0.1:18080"],
        payload_body="x",
        success_signature={"kind": "response_match", "check": "ok"},
        timeout_seconds=1.0,
    )
    assert result.triggered is False
    assert result.why_failed["reason"] == "allowlist_blocked"
    assert "BLOCKED" in str(result.request)


def test_empty_allowlist_fail_closed():
    fence = AllowlistFence(entries=[])
    assert not fence.allows("127.0.0.1")
    result = execute_allowed_request(
        base_url="http://127.0.0.1:1",
        endpoint="/",
        allowlist=[],
        payload_body="x",
    )
    assert result.why_failed["reason"] == "allowlist_blocked"


def test_e2e_flaskish_target_to_poc_confirmed(client: TestClient, rce_server, tmp_path):
    hostport = rce_server.replace("http://", "")
    origin = {
        "codebase": {"path": str(tmp_path), "commit": "deadbeef"},
        "target": {"base_url": rce_server, "credentials_ref": "secret:demo"},
        "allowlist": [hostport, "127.0.0.1"],
    }
    proj = client.post(
        "/projects",
        json={
            "title": "e2e-rce",
            "origin": json.dumps(origin),
            "goal": "unauth RCE",
            "bootstrap_enabled": False,
        },
    ).json()
    pid = proj["project"]["id"]

    # put routing map so Brief endpoint is live path
    client.put(
        f"/projects/{pid}/base_knowledge",
        json={
            "entries": [],
            "routing_map": [
                {
                    "src": "app/import.py:10",
                    "live": "POST /api/import",
                    "via": "direct",
                    "confidence": "assumed",
                }
            ],
            "actor": "test",
        },
    )

    intent = client.post(
        f"/projects/{pid}/intents",
        json={"from": ["origin"], "description": "find sink", "creator": "t"},
    ).json()
    client.post(f"/projects/{pid}/intents/{intent['id']}/heartbeat", json={"worker": "w"})
    concluded = client.post(
        f"/projects/{pid}/intents/{intent['id']}/conclude",
        json={
            "worker": "w",
            "observations": [
                {
                    "type": "source",
                    "description": "upload entry",
                    "locations": ["app/import.py:10"],
                    "oracle_draft": "CAIRN_POC_OK",
                },
                {
                    "type": "dataflow",
                    "description": "to yaml.load",
                    "locations": ["app/import.py:38"],
                },
                {
                    "type": "sink",
                    "description": "yaml.load RCE",
                    "locations": ["app/config_loader.py:19"],
                },
            ],
        },
    )
    assert concluded.status_code == 200
    facts = {f["type"]: f for f in concluded.json()["facts"]}
    chain = [facts["source"]["id"], facts["dataflow"]["id"], facts["sink"]["id"]]

    vintent = client.post(
        f"/projects/{pid}/intents",
        json={
            "from": chain,
            "description": "VERIFY yaml RCE",
            "creator": "reason",
            "task_kind": "verify",
        },
    )
    assert vintent.status_code == 201, vintent.text
    brief = vintent.json()["poc_brief"]
    assert brief["entry"]["endpoint"]
    assert "UNKNOWN" not in brief["entry"]["endpoint"]

    # forced harness fire (dispatcher path)
    hr = execute_allowed_request(
        base_url=rce_server,
        endpoint=brief["entry"]["endpoint"],
        allowlist=origin["allowlist"],
        payload_body=brief["payload_recipe"]["shape"] + " !!python RCE",
        success_signature=brief["success_signature"],
    )
    assert hr.triggered is True

    client.post(f"/projects/{pid}/intents/{vintent.json()['id']}/fire", json={"action": "approve", "actor": "human"})
    client.post(
        f"/projects/{pid}/intents/{vintent.json()['id']}/heartbeat",
        json={"worker": "verifier"},
    )
    from cairn.dispatcher.harness import harness_result_to_observations

    obs = harness_result_to_observations(hr, verifies_fact_id=facts["sink"]["id"])
    # audit traffic
    client.post(
        f"/projects/{pid}/verify/proxy_traffic",
        json={"intent_id": vintent.json()["id"], "request": str(hr.request), "response": hr.response, "status": "recorded"},
    )
    done = client.post(
        f"/projects/{pid}/intents/{vintent.json()['id']}/conclude",
        json={"worker": "verifier", "observations": obs},
    )
    assert done.status_code == 200, done.text
    assert done.json()["fact"]["type"] == "verification"
    assert done.json()["fact"]["confidence"] == "poc-confirmed"

    detail = client.get(f"/projects/{pid}").json()
    sink = next(f for f in detail["facts"] if f["id"] == facts["sink"]["id"])
    assert sink["effective_confidence"] == "poc-confirmed"
    traffic = client.get(f"/projects/{pid}/verify/proxy_traffic").json()
    assert len(traffic) >= 1


def test_credentials_ref_resolution(monkeypatch):
    monkeypatch.setenv("CAIRN_SECRET_demo", "s3cret")
    env = resolve_credentials_ref("secret:demo")
    assert env["CAIRN_TARGET_CREDENTIAL"] == "s3cret"
    monkeypatch.setenv("MY_TOKEN", "abc")
    env2 = resolve_credentials_ref("env:MY_TOKEN")
    assert env2["MY_TOKEN"] == "abc"
