from __future__ import annotations

from fastapi.testclient import TestClient
import pytest

from cairn.server import db
from cairn.server.app import app


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "cairn.db")
    with TestClient(app) as test_client:
        yield test_client


def _create_project(client: TestClient) -> str:
    response = client.post(
        "/projects",
        json={
            "title": "test",
            "origin": "starting point",
            "goal": "finish",
            "hints": [{"content": "initial clue", "creator": "human"}],
        },
    )
    assert response.status_code == 201
    assert response.json()["project"]["bootstrap_enabled"] is True
    return response.json()["project"]["id"]


def test_project_workflow_create_conclude_complete_and_reopen(client: TestClient) -> None:
    project_id = _create_project(client)

    response = client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "investigate", "creator": "reasoner", "worker": None},
    )
    assert response.status_code == 201
    assert response.json()["id"] == "i001"

    response = client.post(
        f"/projects/{project_id}/intents/i001/heartbeat",
        json={"worker": "explorer"},
    )
    assert response.status_code == 200
    assert response.json()["worker"] == "explorer"

    response = client.post(
        f"/projects/{project_id}/intents/i001/conclude",
        json={"worker": "explorer", "description": "new fact"},
    )
    assert response.status_code == 200
    fact = response.json()["fact"]
    assert fact["id"] == "f001"
    assert fact["description"] == "new fact"

    response = client.post(
        f"/projects/{project_id}/complete",
        json={"from": ["f001"], "description": "solved", "worker": "reasoner"},
    )
    assert response.status_code == 200
    assert response.json()["to"] == "goal"

    response = client.post(
        f"/projects/{project_id}/reopen",
        json={"description": "human correction", "creator": "human"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["project"]["status"] == "active"
    assert payload["fact"]["id"] == "f002"
    assert payload["fact"]["description"] == "human correction"
    assert payload["intent"]["from"] == ["f001"]
    assert payload["intent"]["to"] == "f002"


def test_stopping_project_releases_claims_and_reason_but_keeps_hints_writable(client: TestClient) -> None:
    project_id = _create_project(client)
    client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "work", "creator": "worker-a", "worker": "worker-a"},
    )
    client.post(
        f"/projects/{project_id}/reason/claim",
        json={"worker": "worker-b", "trigger": "facts:2->3"},
    )

    response = client.put(f"/projects/{project_id}/status", json={"status": "stopped"})
    assert response.status_code == 200
    assert response.json()["reason"] is None

    detail = client.get(f"/projects/{project_id}").json()
    assert detail["intents"][0]["worker"] is None
    assert client.post(
        f"/projects/{project_id}/hints",
        json={"content": "manual note", "creator": "human"},
    ).status_code == 201
    assert client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "blocked", "creator": "reasoner", "worker": None},
    ).status_code == 403


def test_intent_creation_rejects_goal_source_and_mismatched_initial_worker(client: TestClient) -> None:
    project_id = _create_project(client)

    assert client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["goal"], "description": "invalid", "creator": "reasoner", "worker": None},
    ).status_code == 400
    assert client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "invalid", "creator": "reasoner", "worker": "explorer"},
    ).status_code == 400


def test_settings_and_export_are_backed_by_the_same_database(client: TestClient) -> None:
    project_id = _create_project(client)

    response = client.put("/settings", json={"intent_timeout": 30, "reason_timeout": 45})
    assert response.status_code == 200
    assert client.get("/settings").json() == {"intent_timeout": 30, "reason_timeout": 45}

    exported = client.get(f"/projects/{project_id}/export?format=yaml")
    assert exported.status_code == 200
    assert "origin: starting point" in exported.text
    assert "goal: finish" in exported.text
    assert client.get(f"/projects/{project_id}/export?format=invalid").status_code == 400


def test_expired_intent_and_reason_leases_can_be_reclaimed(client: TestClient) -> None:
    project_id = _create_project(client)
    client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "work", "creator": "worker-a", "worker": "worker-a"},
    )
    client.post(
        f"/projects/{project_id}/reason/claim",
        json={"worker": "worker-a", "trigger": "bootstrap"},
    )
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE intents SET last_heartbeat_at = '2000-01-01T00:00:00Z' WHERE project_id = ?",
            (project_id,),
        )
        conn.execute(
            "UPDATE projects SET reason_last_heartbeat_at = '2000-01-01T00:00:00Z' WHERE id = ?",
            (project_id,),
        )

    response = client.post(
        f"/projects/{project_id}/intents/i001/heartbeat",
        json={"worker": "worker-b"},
    )
    assert response.status_code == 200
    assert response.json()["worker"] == "worker-b"

    response = client.post(
        f"/projects/{project_id}/reason/claim",
        json={"worker": "worker-b", "trigger": "facts:2->3"},
    )
    assert response.status_code == 200
    assert response.json()["reason"]["worker"] == "worker-b"


def test_live_reason_lease_rejects_competing_worker(client: TestClient) -> None:
    project_id = _create_project(client)
    assert client.post(
        f"/projects/{project_id}/reason/claim",
        json={"worker": "worker-a", "trigger": "bootstrap"},
    ).status_code == 200

    response = client.post(
        f"/projects/{project_id}/reason/claim",
        json={"worker": "worker-b", "trigger": "facts:2->3"},
    )

    assert response.status_code == 409
    assert "worker-a" in response.json()["detail"]


def test_project_creation_persists_disabled_bootstrap_and_exports_it(client: TestClient) -> None:
    response = client.post(
        "/projects",
        json={
            "title": "no bootstrap",
            "origin": "start",
            "goal": "finish",
            "bootstrap_enabled": False,
        },
    )

    assert response.status_code == 201
    project_id = response.json()["project"]["id"]
    assert client.get(f"/projects/{project_id}").json()["project"]["bootstrap_enabled"] is False
    assert "bootstrap_enabled: false" in client.get(f"/projects/{project_id}/export?format=yaml").text


def test_project_creation_rejects_invalid_bootstrap_enabled(client: TestClient) -> None:
    response = client.post(
        "/projects",
        json={
            "title": "invalid bootstrap",
            "origin": "start",
            "goal": "finish",
            "bootstrap_enabled": "sometimes",
        },
    )

    assert response.status_code == 422


def test_conclude_with_rich_observations_produces_multiple_facts(client: TestClient) -> None:
    project_id = _create_project(client)

    client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "investigate", "creator": "reasoner", "worker": None},
    )
    client.post(
        f"/projects/{project_id}/intents/i001/heartbeat",
        json={"worker": "explorer"},
    )

    response = client.post(
        f"/projects/{project_id}/intents/i001/conclude",
        json={
            "worker": "explorer",
            "observations": [
                {"type": "source", "description": "input at api.py:10", "locations": ["api.py:10"]},
                {"type": "sink", "description": "exec at util.py:20", "locations": ["util.py:20"]},
            ],
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert len(payload["facts"]) == 2
    fact_ids = {f["id"] for f in payload["facts"]}
    assert fact_ids == {"f001", "f002"}

    main = payload["fact"]
    assert main["type"] == "sink"
    assert main["id"] in fact_ids
    assert payload["intent"]["to"] == main["id"]


def test_conclude_main_fact_priority_dataflow_over_sink(client: TestClient) -> None:
    project_id = _create_project(client)

    client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "investigate", "creator": "reasoner", "worker": None},
    )
    client.post(
        f"/projects/{project_id}/intents/i001/heartbeat",
        json={"worker": "explorer"},
    )

    response = client.post(
        f"/projects/{project_id}/intents/i001/conclude",
        json={
            "worker": "explorer",
            "observations": [
                {"type": "sink", "description": "sink", "locations": ["a.py:1"]},
                {"type": "source", "description": "source", "locations": ["b.py:1"]},
                {"type": "dataflow", "description": "dataflow", "locations": ["c.py:1"]},
            ],
        },
    )
    assert response.status_code == 200
    assert response.json()["fact"]["type"] == "dataflow"


def test_dedup_merges_locations_and_skips_duplicate_facts(client: TestClient) -> None:
    project_id = _create_project(client)

    client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "investigate", "creator": "reasoner", "worker": None},
    )
    client.post(
        f"/projects/{project_id}/intents/i001/heartbeat",
        json={"worker": "explorer"},
    )

    response = client.post(
        f"/projects/{project_id}/intents/i001/conclude",
        json={
            "worker": "explorer",
            "observations": [
                {"type": "sink", "description": "sink A", "locations": ["file.py:10"]},
            ],
        },
    )
    assert response.status_code == 200
    first_fid = response.json()["fact"]["id"]

    client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "more work", "creator": "reasoner", "worker": None},
    )
    client.post(
        f"/projects/{project_id}/intents/i002/heartbeat",
        json={"worker": "explorer"},
    )

    response = client.post(
        f"/projects/{project_id}/intents/i002/conclude",
        json={
            "worker": "explorer",
            "observations": [
                {"type": "sink", "description": "sink A again", "locations": ["file.py:10"]},
            ],
        },
    )
    assert response.status_code == 200
    assert response.json()["fact"]["id"] == first_fid
    assert response.json()["fact"]["locations"] == ["file.py:10"]

    exported = client.get(f"/projects/{project_id}/export?format=yaml")
    assert exported.status_code == 200
    fact_count = sum(1 for line in exported.text.splitlines() if line.startswith("- id: f"))
    assert fact_count == 1


def test_dedup_same_type_different_locations_creates_separate_fact(client: TestClient) -> None:
    project_id = _create_project(client)

    client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "work", "creator": "reasoner", "worker": None},
    )
    client.post(
        f"/projects/{project_id}/intents/i001/heartbeat",
        json={"worker": "explorer"},
    )
    client.post(
        f"/projects/{project_id}/intents/i001/conclude",
        json={
            "worker": "explorer",
            "observations": [
                {"type": "sink", "description": "sink at file.py:10", "locations": ["file.py:10"]},
            ],
        },
    )

    client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "more", "creator": "reasoner", "worker": None},
    )
    client.post(
        f"/projects/{project_id}/intents/i002/heartbeat",
        json={"worker": "explorer"},
    )
    response = client.post(
        f"/projects/{project_id}/intents/i002/conclude",
        json={
            "worker": "explorer",
            "observations": [
                {"type": "sink", "description": "sink at file.py:15", "locations": ["file.py:15"]},
            ],
        },
    )
    assert response.status_code == 200
    assert response.json()["fact"]["id"] == "f002"


def test_dedup_preserves_first_written_description(client: TestClient) -> None:
    project_id = _create_project(client)

    client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "work", "creator": "reasoner", "worker": None},
    )
    client.post(
        f"/projects/{project_id}/intents/i001/heartbeat",
        json={"worker": "explorer"},
    )

    client.post(
        f"/projects/{project_id}/intents/i001/conclude",
        json={
            "worker": "explorer",
            "observations": [
                {"type": "sink", "description": "first description", "locations": ["x.py:1"]},
            ],
        },
    )

    client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "more", "creator": "reasoner", "worker": None},
    )
    client.post(
        f"/projects/{project_id}/intents/i002/heartbeat",
        json={"worker": "explorer"},
    )

    response = client.post(
        f"/projects/{project_id}/intents/i002/conclude",
        json={
            "worker": "explorer",
            "observations": [
                    {"type": "sink", "description": "different description", "locations": ["x.py:1"]},
            ],
        },
    )
    assert response.status_code == 200
    assert response.json()["fact"]["description"] == "first description"


def test_origin_json_validation_accepts_valid_json(client: TestClient) -> None:
    origin = '{"codebase": {"path": "/repo", "commit": "abc123"}, "target": {"base_url": "https://example.com", "credentials_ref": "secret:key"}, "allowlist": ["host:443"]}'
    response = client.post(
        "/projects",
        json={
            "title": "valid origin json",
            "origin": origin,
            "goal": "unauth RCE",
        },
    )
    assert response.status_code == 201


def test_origin_json_validation_rejects_missing_codebase_path(client: TestClient) -> None:
    response = client.post(
        "/projects",
        json={
            "title": "bad origin",
            "origin": '{"codebase": {}}',
            "goal": "unauth RCE",
        },
    )
    assert response.status_code == 422


def test_origin_json_validation_rejects_bad_target(client: TestClient) -> None:
    response = client.post(
        "/projects",
        json={
            "title": "bad target",
            "origin": '{"codebase": {"path": "/repo"}, "target": "not_an_object"}',
            "goal": "unauth RCE",
        },
    )
    assert response.status_code == 422


def test_origin_json_validation_rejects_bad_allowlist(client: TestClient) -> None:
    response = client.post(
        "/projects",
        json={
            "title": "bad allowlist",
            "origin": '{"codebase": {"path": "/repo"}, "allowlist": "not_array"}',
            "goal": "unauth RCE",
        },
    )
    assert response.status_code == 422


def test_code_version_computed_and_present_in_export(client: TestClient) -> None:
    project_id = _create_project(client)

    client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "work", "creator": "reasoner", "worker": None},
    )
    client.post(
        f"/projects/{project_id}/intents/i001/heartbeat",
        json={"worker": "explorer"},
    )
    response = client.post(
        f"/projects/{project_id}/intents/i001/conclude",
        json={
            "worker": "explorer",
            "observations": [
                {"type": "sink", "description": "dangerous exec", "locations": ["app/util.py:42"]},
            ],
        },
    )
    assert response.status_code == 200

    exported = client.get(f"/projects/{project_id}/export?format=yaml")
    assert exported.status_code == 200
    assert "code_version:" in exported.text


def test_effective_confidence_in_export(client: TestClient) -> None:
    project_id = _create_project(client)

    client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "work", "creator": "reasoner", "worker": None},
    )
    client.post(
        f"/projects/{project_id}/intents/i001/heartbeat",
        json={"worker": "explorer"},
    )
    client.post(
        f"/projects/{project_id}/intents/i001/conclude",
        json={
            "worker": "explorer",
            "observations": [
                {"type": "sink", "description": "dangerous exec", "locations": ["app/util.py:42"]},
            ],
        },
    )

    exported = client.get(f"/projects/{project_id}/export?format=yaml")
    assert exported.status_code == 200
    assert "type: sink" in exported.text
    assert "confidence: static-confirmed" in exported.text
    assert "effective_confidence: static-confirmed" in exported.text


def test_legacy_single_description_conclude_still_works(client: TestClient) -> None:
    project_id = _create_project(client)

    client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "investigate", "creator": "reasoner", "worker": None},
    )
    client.post(
        f"/projects/{project_id}/intents/i001/heartbeat",
        json={"worker": "explorer"},
    )

    response = client.post(
        f"/projects/{project_id}/intents/i001/conclude",
        json={"worker": "explorer", "description": "plain text fact"},
    )
    assert response.status_code == 200
    assert response.json()["fact"]["description"] == "plain text fact"
    assert len(response.json()["facts"]) == 1


def test_backward_compatible_export_includes_fact_type_and_confidence(client: TestClient) -> None:
    project_id = _create_project(client)

    client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "work", "creator": "reasoner", "worker": None},
    )
    client.post(
        f"/projects/{project_id}/intents/i001/heartbeat",
        json={"worker": "explorer"},
    )
    client.post(
        f"/projects/{project_id}/intents/i001/conclude",
        json={
            "worker": "explorer",
            "observations": [
                {"type": "constraint", "description": "auth middleware blocks unauthenticated requests", "locations": ["app/middleware.py:10"]},
            ],
        },
    )

    detail = client.get(f"/projects/{project_id}").json()
    fact = detail["facts"][-1]
    assert fact["type"] == "constraint"
    assert fact["confidence"] == "static-confirmed"
    assert fact["locations"] == ["app/middleware.py:10"]
