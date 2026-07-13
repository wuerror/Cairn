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


def _conclude_observations(client: TestClient, project_id: str, intent_id: str, observations: list[dict], worker: str = "explorer") -> dict:
    client.post(
        f"/projects/{project_id}/intents/{intent_id}/heartbeat",
        json={"worker": worker},
    )
    response = client.post(
        f"/projects/{project_id}/intents/{intent_id}/conclude",
        json={"worker": worker, "observations": observations},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _seed_audit_chain(client: TestClient, project_id: str) -> dict[str, str]:
    """Build origin→source→dataflow→sink chain + an unrelated sqli sink."""
    # i001: source + constraint batch
    client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "map import entry", "creator": "reasoner", "worker": None},
    )
    r1 = _conclude_observations(
        client,
        project_id,
        "i001",
        [
            {"type": "source", "description": "unauth multipart upload config field", "locations": ["app/api/import_bp.py:31"]},
            {"type": "constraint", "description": "import_bp not decorated with login_required", "locations": ["app/api/import_bp.py:31", "app/__init__.py:44"]},
        ],
    )
    source_id = next(f["id"] for f in r1["facts"] if f["type"] == "source")
    constraint_id = next(f["id"] for f in r1["facts"] if f["type"] == "constraint")
    main1 = r1["fact"]["id"]

    # i002: dataflow from source
    client.post(
        f"/projects/{project_id}/intents",
        json={"from": [source_id], "description": "trace config to yaml.load", "creator": "reasoner", "worker": None},
    )
    r2 = _conclude_observations(
        client,
        project_id,
        "i002",
        [
            {
                "type": "dataflow",
                "description": "config field flows request.files to yaml.load without sanitize",
                "locations": ["app/api/import_bp.py:38", "app/config_loader.py:19"],
            },
        ],
    )
    dataflow_id = r2["fact"]["id"]

    # i003: RCE sink from dataflow
    client.post(
        f"/projects/{project_id}/intents",
        json={"from": [dataflow_id], "description": "confirm yaml deserialize sink", "creator": "reasoner", "worker": None},
    )
    r3 = _conclude_observations(
        client,
        project_id,
        "i003",
        [
            {
                "type": "sink",
                "description": "yaml.load deserialize allows !!python/object/apply RCE",
                "locations": ["app/config_loader.py:19"],
            },
        ],
    )
    rce_sink_id = r3["fact"]["id"]

    # i004: unrelated SQLi sink from origin (should drop out of RCE subgraph)
    client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "map sql query sink", "creator": "reasoner", "worker": None},
    )
    r4 = _conclude_observations(
        client,
        project_id,
        "i004",
        [
            {
                "type": "sink",
                "description": "raw SQL query string concatenation sqli",
                "locations": ["app/db.py:88"],
            },
        ],
    )
    sqli_sink_id = r4["fact"]["id"]

    return {
        "source": source_id,
        "constraint": constraint_id,
        "dataflow": dataflow_id,
        "rce_sink": rce_sink_id,
        "sqli_sink": sqli_sink_id,
        "main1": main1,
    }


def test_relevant_subgraph_reverse_bfs_and_batch_attach(client: TestClient) -> None:
    project = client.post(
        "/projects",
        json={
            "title": "subgraph",
            "origin": '{"codebase": {"path": "/repo", "commit": "aaa111"}}',
            "goal": "unauth RCE",
            "bootstrap_enabled": False,
        },
    )
    assert project.status_code == 201
    project_id = project.json()["project"]["id"]
    ids = _seed_audit_chain(client, project_id)

    response = client.get(f"/projects/{project_id}/relevant_subgraph?format=json")
    assert response.status_code == 200
    payload = response.json()
    fact_ids = set(payload["fact_ids"])

    assert "origin" in fact_ids
    assert "goal" in fact_ids
    assert ids["rce_sink"] in fact_ids
    assert ids["dataflow"] in fact_ids
    assert ids["source"] in fact_ids
    # batch satellite constraint attached via batch_id
    assert ids["constraint"] in fact_ids
    # unrelated sqli sink not on RCE reverse path
    assert ids["sqli_sink"] not in fact_ids

    yaml_resp = client.get(f"/projects/{project_id}/relevant_subgraph?format=yaml")
    assert yaml_resp.status_code == 200
    assert "yaml.load" in yaml_resp.text
    assert "sqli" not in yaml_resp.text.lower() or ids["sqli_sink"] not in yaml_resp.text


def test_relevant_subgraph_excludes_refuted_sink_paths(client: TestClient) -> None:
    project = client.post(
        "/projects",
        json={
            "title": "refuted path",
            "origin": '{"codebase": {"path": "/repo2", "commit": "bbb222"}}',
            "goal": "unauth RCE",
            "bootstrap_enabled": False,
        },
    )
    project_id = project.json()["project"]["id"]
    ids = _seed_audit_chain(client, project_id)

    # Append a verification fact refuting the RCE sink (direct DB insert via second conclude path is hard;
    # use raw SQL through services by concluding a verification-like typed fact isn't allowed from explore.
    # Insert via db connection used by the app fixture.
    from cairn.server.db import get_conn

    with get_conn() as conn:
        conn.execute(
            """INSERT INTO facts (id, project_id, description, type, confidence, locations, code_version, verifies, batch_id)
               VALUES (?, ?, ?, 'verification', 'refuted', ?, ?, ?, ?)""",
            (
                "f_refute",
                project_id,
                "runtime refute",
                "[]",
                "bbb222",
                ids["rce_sink"],
                "b_verify",
            ),
        )

    response = client.get(f"/projects/{project_id}/relevant_subgraph?format=json")
    assert response.status_code == 200
    fact_ids = set(response.json()["fact_ids"])
    # RCE path dropped because terminal sink is refuted; sqli may still appear if matched? goal is RCE so only RCE keywords
    assert ids["rce_sink"] not in fact_ids
    assert ids["dataflow"] not in fact_ids


def test_code_version_mismatch_without_verification_is_not_stale(client: TestClient) -> None:
    """Unverified static facts keep own confidence; code_version mismatch alone is not stale."""
    project = client.post(
        "/projects",
        json={
            "title": "stale cv",
            "origin": '{"codebase": {"path": "/repo3", "commit": "oldcommit"}}',
            "goal": "RCE",
            "bootstrap_enabled": False,
        },
    )
    project_id = project.json()["project"]["id"]
    client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "find sink", "creator": "reasoner", "worker": None},
    )
    _conclude_observations(
        client,
        project_id,
        "i001",
        [{"type": "sink", "description": "exec sink", "locations": ["a.py:1"]}],
    )

    from cairn.server.db import get_conn

    with get_conn() as conn:
        conn.execute(
            "UPDATE facts SET description = ? WHERE project_id = ? AND id = 'origin'",
            ('{"codebase": {"path": "/repo3", "commit": "newcommit"}}', project_id),
        )

    detail = client.get(f"/projects/{project_id}").json()
    sink = next(f for f in detail["facts"] if f.get("type") == "sink")
    assert sink["code_version"] == "oldcommit"
    assert sink.get("stale") is not True
    assert sink.get("effective_confidence") == "static-confirmed"


def test_expired_verification_marks_stale(client: TestClient) -> None:
    project = client.post(
        "/projects",
        json={
            "title": "verification stale",
            "origin": '{"codebase": {"path": "/repo3b", "commit": "oldcommit"}}',
            "goal": "RCE",
            "bootstrap_enabled": False,
        },
    )
    project_id = project.json()["project"]["id"]
    client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "find sink", "creator": "reasoner", "worker": None},
    )
    concluded = _conclude_observations(
        client,
        project_id,
        "i001",
        [{"type": "sink", "description": "exec sink", "locations": ["a.py:1"]}],
    )
    sink_id = concluded["fact"]["id"]

    from cairn.server.db import get_conn

    with get_conn() as conn:
        conn.execute(
            """INSERT INTO facts (id, project_id, description, type, confidence, locations, code_version, verifies, batch_id)
               VALUES (?, ?, ?, 'verification', 'poc-confirmed', ?, ?, ?, ?)""",
            ("f_v1", project_id, "poc ok", "[]", "oldcommit", sink_id, "b_v"),
        )
        conn.execute(
            "UPDATE facts SET description = ? WHERE project_id = ? AND id = 'origin'",
            ('{"codebase": {"path": "/repo3b", "commit": "newcommit"}}', project_id),
        )

    detail = client.get(f"/projects/{project_id}").json()
    sink = next(f for f in detail["facts"] if f["id"] == sink_id)
    assert sink["stale"] is True
    assert sink.get("effective_confidence") == "static-confirmed"


def test_cross_run_fact_reuse_on_same_codebase(client: TestClient) -> None:
    origin = '{"codebase": {"path": "/shared/repo", "commit": "c0ffee"}}'
    p1 = client.post(
        "/projects",
        json={"title": "goal1 RCE", "origin": origin, "goal": "unauth RCE", "bootstrap_enabled": False},
    ).json()["project"]["id"]
    ids = _seed_audit_chain(client, p1)

    p2 = client.post(
        "/projects",
        json={"title": "goal2 IDOR", "origin": origin, "goal": "horizontal IDOR", "bootstrap_enabled": False},
    )
    assert p2.status_code == 201
    detail = p2.json()
    typed = [f for f in detail["facts"] if f.get("type")]
    assert len(typed) >= 4
    # Imported facts keep locations (canonical key identity)
    locations = {tuple(f.get("locations") or []) for f in typed}
    assert ("app/config_loader.py:19",) in locations or any(
        "app/config_loader.py:19" in (f.get("locations") or []) for f in typed
    )
    # goal is the new project's goal, not imported
    goal = next(f for f in detail["facts"] if f["id"] == "goal")
    assert goal["description"] == "horizontal IDOR"
    # No duplicate rebuild of same sink under same key when concluding again
    created = client.post(
        f"/projects/{detail['project']['id']}/intents",
        json={"from": ["origin"], "description": "recheck sink", "creator": "reasoner", "worker": None},
    )
    assert created.status_code == 201
    new_intent_id = created.json()["id"]
    before = len([f for f in client.get(f"/projects/{detail['project']['id']}").json()["facts"] if f.get("type") == "sink"])
    _conclude_observations(
        client,
        detail["project"]["id"],
        new_intent_id,
        [
            {
                "type": "sink",
                "description": "yaml.load deserialize allows !!python/object/apply RCE",
                "locations": ["app/config_loader.py:19"],
            }
        ],
    )
    after = len([f for f in client.get(f"/projects/{detail['project']['id']}").json()["facts"] if f.get("type") == "sink"])
    assert after == before
    assert ids["rce_sink"]  # ensure seed produced rce sink


def test_base_knowledge_put_and_patch_audit_chain(client: TestClient) -> None:
    project_id = _create_project(client)

    put = client.put(
        f"/projects/{project_id}/base_knowledge",
        json={
            "actor": "bootstrap-worker",
            "expected_version": 0,
            "entries": [
                {
                    "id": "bk001",
                    "kind": "auth",
                    "statement": "Auth via @login_required per route",
                    "evidence": ["app/auth.py:22"],
                    "confidence": "assumed",
                }
            ],
            "routing_map": [
                {
                    "src": "app/api/import_bp.py:31",
                    "live": "POST /api/import",
                    "via": "direct",
                    "confidence": "assumed",
                }
            ],
        },
    )
    assert put.status_code == 200, put.text
    body = put.json()
    assert body["version"] == 1
    assert body["entries"][0]["kind"] == "auth"
    assert body["routing_map"][0]["live"] == "POST /api/import"
    assert any(a["action"] == "replace" for a in body["audit"])

    # Need a fact to reference as revised_by
    client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "find bypass", "creator": "reasoner", "worker": None},
    )
    concluded = _conclude_observations(
        client,
        project_id,
        "i001",
        [
            {
                "type": "constraint",
                "description": "import route skips login_required",
                "locations": ["app/api/import_bp.py:31"],
            }
        ],
    )
    fact_id = concluded["fact"]["id"]

    patch = client.patch(
        f"/projects/{project_id}/base_knowledge/entries/bk001",
        json={
            "statement": "Most routes use @login_required; import_bp is an exception",
            "confidence": "code-confirmed",
            "revised_by": fact_id,
            "actor": "explorer",
            "expected_version": 1,
        },
    )
    assert patch.status_code == 200, patch.text
    patched = patch.json()
    assert patched["version"] == 2
    assert patched["entries"][0]["revised_by"] == fact_id
    assert patched["entries"][0]["confidence"] == "code-confirmed"
    assert any(a["action"] == "patch" and a["revised_by"] == fact_id for a in patched["audit"])

    # Version conflict
    conflict = client.put(
        f"/projects/{project_id}/base_knowledge",
        json={"actor": "x", "expected_version": 1, "entries": [], "routing_map": []},
    )
    assert conflict.status_code == 409

    detail = client.get(f"/projects/{project_id}").json()
    assert detail["base_knowledge"]["version"] == 2
    assert detail["base_knowledge"]["entries"][0]["id"] == "bk001"


def test_relevant_subgraph_keyword_miss_does_not_seed_all_sinks(client: TestClient) -> None:
    project = client.post(
        "/projects",
        json={
            "title": "keyword miss",
            "origin": '{"codebase": {"path": "/repo-miss", "commit": "abc"}}',
            "goal": "business logic race condition",
            "bootstrap_enabled": False,
        },
    )
    project_id = project.json()["project"]["id"]
    ids = _seed_audit_chain(client, project_id)

    response = client.get(f"/projects/{project_id}/relevant_subgraph?format=json")
    assert response.status_code == 200
    fact_ids = set(response.json()["fact_ids"])
    assert "origin" in fact_ids
    assert "goal" in fact_ids
    # RCE/SQLi sinks must not be seeded when goal keywords do not match them
    assert ids["rce_sink"] not in fact_ids
    assert ids["sqli_sink"] not in fact_ids
    assert ids["dataflow"] not in fact_ids


def test_conclude_applies_base_knowledge_patches(client: TestClient) -> None:
    project_id = _create_project(client)
    client.put(
        f"/projects/{project_id}/base_knowledge",
        json={
            "actor": "bootstrap-worker",
            "expected_version": 0,
            "entries": [
                {
                    "id": "bk001",
                    "kind": "auth",
                    "statement": "Auth via @login_required per route",
                    "evidence": ["app/auth.py:22"],
                    "confidence": "assumed",
                }
            ],
            "routing_map": [],
        },
    )
    client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "find bypass", "creator": "reasoner", "worker": None},
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
                {
                    "type": "constraint",
                    "description": "import route skips login_required",
                    "locations": ["app/api/import_bp.py:31"],
                }
            ],
            "base_knowledge_patches": [
                {
                    "entry_id": "bk001",
                    "statement": "Most routes use @login_required; import_bp is an exception",
                    "confidence": "code-confirmed",
                    "evidence": ["app/api/import_bp.py:31"],
                }
            ],
        },
    )
    assert response.status_code == 200, response.text
    fact_id = response.json()["fact"]["id"]
    bk = client.get(f"/projects/{project_id}/base_knowledge").json()
    assert bk["version"] == 2
    assert bk["entries"][0]["statement"].startswith("Most routes")
    assert bk["entries"][0]["revised_by"] == fact_id
    assert any(a["action"] == "patch" and a["revised_by"] == fact_id for a in bk["audit"])


def test_conclude_skips_invalid_base_knowledge_patch_entry(client: TestClient) -> None:
    project_id = _create_project(client)
    client.put(
        f"/projects/{project_id}/base_knowledge",
        json={
            "actor": "w",
            "entries": [
                {
                    "id": "bk001",
                    "kind": "architecture",
                    "statement": "Flask app factory",
                    "evidence": ["app/__init__.py:1"],
                    "confidence": "code-confirmed",
                }
            ],
            "routing_map": [],
        },
    )
    client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "find thing", "creator": "reasoner", "worker": None},
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
                {
                    "type": "sink",
                    "description": "exec sink",
                    "locations": ["a.py:1"],
                }
            ],
            "base_knowledge_patches": [
                {"entry_id": "bk_missing", "statement": "should be skipped"},
            ],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["fact"]["type"] == "sink"
    bk = client.get(f"/projects/{project_id}/base_knowledge").json()
    assert bk["version"] == 1
    assert bk["entries"][0]["id"] == "bk001"
    assert bk["entries"][0].get("revised_by") is None


def test_base_knowledge_patch_requires_revised_by_fact(client: TestClient) -> None:
    project_id = _create_project(client)
    client.put(
        f"/projects/{project_id}/base_knowledge",
        json={
            "actor": "w",
            "entries": [
                {
                    "id": "bk001",
                    "kind": "architecture",
                    "statement": "Flask app factory",
                    "evidence": ["app/__init__.py:1"],
                    "confidence": "code-confirmed",
                }
            ],
            "routing_map": [],
        },
    )
    response = client.patch(
        f"/projects/{project_id}/base_knowledge/entries/bk001",
        json={"statement": "updated", "revised_by": "f999", "actor": "w"},
    )
    assert response.status_code == 404
