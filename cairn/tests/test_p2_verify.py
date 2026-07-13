from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from cairn.dispatcher.config import DispatchConfig
from cairn.dispatcher.contracts import validate_verify_payload
from cairn.dispatcher.harness import (
    AllowlistFence,
    harness_result_to_observations,
    select_verifies_target,
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


def _origin_json(**kwargs):
    data = {
        "codebase": {"path": "/tmp/code", "commit": "abc123"},
        "target": {"base_url": "http://127.0.0.1:18080", "credentials_ref": "secret:demo"},
        "allowlist": ["127.0.0.1:18080", "oob.example"],
    }
    data.update(kwargs)
    return json.dumps(data)


def _seed_chain(client: TestClient) -> str:
    resp = client.post(
        "/projects",
        json={
            "title": "p2",
            "origin": _origin_json(),
            "goal": "unauth RCE",
            "bootstrap_enabled": False,
        },
    )
    assert resp.status_code == 201
    pid = resp.json()["project"]["id"]

    # explore-style facts
    intent = client.post(
        f"/projects/{pid}/intents",
        json={"from": ["origin"], "description": "map sink", "creator": "tester"},
    ).json()
    client.post(
        f"/projects/{pid}/intents/{intent['id']}/heartbeat",
        json={"worker": "w1"},
    )
    concluded = client.post(
        f"/projects/{pid}/intents/{intent['id']}/conclude",
        json={
            "worker": "w1",
            "observations": [
                {
                    "type": "source",
                    "description": "multipart config upload",
                    "locations": ["app/import.py:10"],
                    "oracle_draft": "OOB callback token must hit oob.example",
                },
                {
                    "type": "dataflow",
                    "description": "config → yaml.load",
                    "locations": ["app/import.py:38", "app/config_loader.py:19"],
                },
                {
                    "type": "sink",
                    "description": "yaml.load RCE sink",
                    "locations": ["app/config_loader.py:19"],
                },
            ],
        },
    )
    assert concluded.status_code == 200
    facts = concluded.json()["facts"]
    by_type = {f["type"]: f["id"] for f in facts}
    assert "sink" in by_type
    return pid


def test_create_verify_intent_assembles_brief(client: TestClient):
    pid = _seed_chain(client)
    detail = client.get(f"/projects/{pid}").json()
    chain = [f["id"] for f in detail["facts"] if f["id"] not in ("origin", "goal") and f.get("type")]
    # order source, dataflow, sink if present
    typed = {f["type"]: f["id"] for f in detail["facts"] if f.get("type")}
    chain_ids = [typed[t] for t in ("source", "dataflow", "sink") if t in typed]

    resp = client.post(
        f"/projects/{pid}/intents",
        json={
            "from": chain_ids,
            "description": "VERIFY yaml.load RCE",
            "creator": "reasoner",
            "task_kind": "verify",
        },
    )
    assert resp.status_code == 201, resp.text
    intent = resp.json()
    assert intent["task_kind"] == "verify"
    assert intent["fire_status"] == "pending"
    brief = intent["poc_brief"]
    assert brief is not None
    assert brief["chain"] == chain_ids
    assert "entry" in brief and brief["entry"]["endpoint"]
    assert brief["success_signature"]["check"]


def test_conclude_verification_poc_confirmed(client: TestClient):
    pid = _seed_chain(client)
    detail = client.get(f"/projects/{pid}").json()
    sink = next(f for f in detail["facts"] if f.get("type") == "sink")
    typed = {f["type"]: f["id"] for f in detail["facts"] if f.get("type")}
    chain_ids = [typed[t] for t in ("source", "dataflow", "sink") if t in typed]

    intent = client.post(
        f"/projects/{pid}/intents",
        json={
            "from": chain_ids,
            "description": "VERIFY chain",
            "creator": "reasoner",
            "task_kind": "verify",
        },
    ).json()
    client.post(f"/projects/{pid}/intents/{intent['id']}/fire", json={"action": "approve", "actor": "human"})
    client.post(f"/projects/{pid}/intents/{intent['id']}/heartbeat", json={"worker": "verifier"})
    resp = client.post(
        f"/projects/{pid}/intents/{intent['id']}/conclude",
        json={
            "worker": "verifier",
            "observations": [
                {
                    "type": "verification",
                    "description": "poc-confirmed yaml RCE",
                    "confidence": "poc-confirmed",
                    "verifies": sink["id"],
                    "evidence": "oob hit",
                }
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["fact"]["type"] == "verification"
    assert body["fact"]["verifies"] == sink["id"]
    assert body["fact"]["confidence"] == "poc-confirmed"

    detail = client.get(f"/projects/{pid}").json()
    sink2 = next(f for f in detail["facts"] if f["id"] == sink["id"])
    assert sink2["effective_confidence"] == "poc-confirmed"


def test_conclude_refuted_with_constraint(client: TestClient):
    pid = _seed_chain(client)
    detail = client.get(f"/projects/{pid}").json()
    sink = next(f for f in detail["facts"] if f.get("type") == "sink")
    typed = {f["type"]: f["id"] for f in detail["facts"] if f.get("type")}
    chain_ids = [typed[t] for t in ("source", "dataflow", "sink") if t in typed]
    intent = client.post(
        f"/projects/{pid}/intents",
        json={"from": chain_ids, "description": "VERIFY", "creator": "r", "task_kind": "verify"},
    ).json()
    client.post(f"/projects/{pid}/intents/{intent['id']}/heartbeat", json={"worker": "v"})
    resp = client.post(
        f"/projects/{pid}/intents/{intent['id']}/conclude",
        json={
            "worker": "v",
            "observations": harness_result_to_observations(
                {
                    "triggered": False,
                    "request": "POST /api/import",
                    "response": "200",
                    "why_failed": {"reason": "sanitized", "detail": "safe_load"},
                },
                verifies_fact_id=sink["id"],
            ),
        },
    )
    assert resp.status_code == 200, resp.text
    facts = resp.json()["facts"]
    types = {f["type"] for f in facts}
    assert "verification" in types
    assert "constraint" in types
    v = next(f for f in facts if f["type"] == "verification")
    assert v["confidence"] == "refuted"


def test_allowlist_fence_blocks_unknown_host():
    fence = AllowlistFence(entries=["127.0.0.1:18080", "oob.example"])
    assert fence.allows("http://127.0.0.1:18080/api")
    assert fence.allows("oob.example")
    assert not fence.allows("http://evil.example/x")
    blocked = fence.check_or_block("http://evil.example/x")
    assert blocked is not None
    assert blocked.why_failed["reason"] == "allowlist_blocked"


def test_kill_switch_and_proxy_traffic(client: TestClient):
    pid = _seed_chain(client)
    kill = client.post(
        f"/projects/{pid}/verify/kill",
        json={"actor": "human", "reason": "stop"},
    )
    assert kill.status_code == 200
    assert kill.json()["kill_requested"] is True

    control = client.get(f"/projects/{pid}/verify/control")
    assert control.json()["kill_requested"] is True

    clear = client.post(f"/projects/{pid}/verify/kill/clear")
    assert clear.json()["kill_requested"] is False

    traffic = client.post(
        f"/projects/{pid}/verify/proxy_traffic",
        json={
            "intent_id": None,
            "request": "POST /api/import\n...",
            "response": "200 ok",
            "status": "recorded",
        },
    )
    assert traffic.status_code == 201
    listed = client.get(f"/projects/{pid}/verify/proxy_traffic")
    assert listed.status_code == 200
    assert len(listed.json()) == 1


def test_validate_verify_payload_harness_result():
    kind, data = validate_verify_payload(
        {
            "accepted": True,
            "data": {
                "harness_result": {
                    "triggered": True,
                    "evidence": "hit",
                    "request": "POST /x",
                    "response": "ok",
                    "why_failed": None,
                },
                "verifies": "f003",
            },
        }
    )
    assert kind == "harness_result"
    assert data["harness_result"]["triggered"] is True
    assert data["verifies"] == "f003"


def test_worker_capabilities_gate():
    cfg = DispatchConfig.model_validate(
        {
            "server": "http://127.0.0.1:8000",
            "runtime": {
                "interval": 3,
                "max_workers": 2,
                "max_running_projects": 1,
                "max_project_workers": 2,
                "healthcheck_timeout": 5,
                "prompt_group": "mock",
            },
            "tasks": {
                "bootstrap": {"timeout": 10, "conclude_timeout": 5},
                "reason": {"timeout": 10, "max_intents": 3},
                "explore": {"timeout": 10, "conclude_timeout": 5},
                "verify": {"timeout": 30, "conclude_timeout": 10, "require_fire_approval": False},
            },
            "container": {
                "image": "test-image",
                "network_mode": "host",
                "completed_action": "stop",
            },
            "workers": [
                {
                    "name": "static-only",
                    "type": "mock",
                    "task_types": ["verify"],
                    "capabilities": ["static_fs"],
                    "max_running": 1,
                    "priority": 0,
                },
                {
                    "name": "live",
                    "type": "mock",
                    "task_types": ["verify"],
                    "capabilities": ["static_fs", "live_http"],
                    "max_running": 1,
                    "priority": 1,
                },
            ],
        }
    )
    static = next(w for w in cfg.workers if w.name == "static-only")
    live = next(w for w in cfg.workers if w.name == "live")
    assert not static.has_capabilities(["live_http"])
    assert live.has_capabilities(["live_http"])


def test_select_verifies_target_prefers_sink():
    chain = ["f1", "f2", "f3"]
    facts = [
        {"id": "f1", "type": "source"},
        {"id": "f2", "type": "dataflow"},
        {"id": "f3", "type": "sink"},
    ]
    assert select_verifies_target(chain, facts) == "f3"


def test_container_dual_profile_resolve():
    cfg = DispatchConfig.model_validate(
        {
            "server": "http://x",
            "runtime": {
                "interval": 3,
                "max_workers": 1,
                "max_running_projects": 1,
                "max_project_workers": 1,
                "healthcheck_timeout": 5,
                "prompt_group": "mock",
            },
            "tasks": {
                "bootstrap": {"timeout": 1, "conclude_timeout": 1},
                "reason": {"timeout": 1},
                "explore": {"timeout": 1, "conclude_timeout": 1},
            },
            "container": {
                "image": "static-img",
                "network_mode": "none",
                "completed_action": "remove",
                "verify": {"image": "verify-img", "network_mode": "bridge"},
            },
            "workers": [
                {
                    "name": "m",
                    "type": "mock",
                    "task_types": ["reason"],
                    "max_running": 1,
                    "priority": 0,
                }
            ],
        }
    )
    static = cfg.container.resolve_profile("static")
    verify = cfg.container.resolve_profile("verify")
    assert static.image == "static-img"
    assert static.network_mode == "none"
    assert verify.image == "verify-img"
    assert verify.network_mode == "bridge"
