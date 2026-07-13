"""P3: codebase mount + payload_draft Brief + demo target → poc-confirmed."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cairn.dispatcher.config import DispatchConfig
from cairn.dispatcher.harness import execute_allowed_request, harness_result_to_observations
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.tasks import bootstrap, explore, reason, verify
from cairn.dispatcher.tasks.common import ensure_static_container, resolve_codebase_host_path
from cairn.dispatcher.tasks.verify import _initial_payload
from cairn.server import db
from cairn.server.app import app
from cairn.server.models import Fact, Intent, ProjectDetail, ProjectMeta
from cairn.server.services import assemble_poc_brief

from conftest import FakeClient, FakeContainerManager, FakeDriver, FakeLease, make_config, make_intent, make_project


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(db_path)
    with TestClient(app) as c:
        yield c


class _DemoHandler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8", errors="replace")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        if "CAIRN_POC_OK" in body or "!!python" in body:
            self.wfile.write(b"CAIRN_POC_OK token=demo")
        else:
            self.wfile.write(b"imported ok")

    def log_message(self, format, *args):  # noqa: A003
        return


@pytest.fixture()
def demo_server():
    server = HTTPServer(("127.0.0.1", 0), _DemoHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


def test_resolve_codebase_missing_path_errors(tmp_path):
    missing = tmp_path / "nope"
    origin = json.dumps({"codebase": {"path": str(missing)}, "target": {}, "allowlist": []})
    project = make_project()
    project.facts = [Fact(id="origin", description=origin), Fact(id="goal", description="rce")]
    path, err = resolve_codebase_host_path(project, require_readable=True)
    assert path is None
    assert err and "does not exist" in err


def test_ensure_static_container_passes_codebase_bind(tmp_path):
    code = tmp_path / "src"
    code.mkdir()
    origin = json.dumps(
        {
            "codebase": {"path": str(code), "commit": "abc"},
            "target": {"base_url": "http://127.0.0.1:9"},
            "allowlist": ["127.0.0.1"],
        }
    )
    project = make_project()
    project.facts = [Fact(id="origin", description=origin), Fact(id="goal", description="rce")]
    config = make_config()
    containers = FakeContainerManager()
    name, err = ensure_static_container(config, containers, project)
    assert err is None
    assert name == "container-proj_001"
    assert containers.ensure_calls
    call = containers.ensure_calls[0]
    assert call["profile"] == "static"
    assert call["codebase_host_path"] == str(code)


def test_explore_ensure_running_receives_codebase_bind(monkeypatch, tmp_path):
    code = tmp_path / "app"
    code.mkdir()
    origin = json.dumps({"codebase": {"path": str(code)}, "target": {}, "allowlist": []})
    intent = make_intent()
    project = make_project(intents=[intent])
    project.facts = [
        Fact(id="origin", description=origin),
        Fact(id="goal", description="finish"),
        Fact(id="f001", description="known"),
    ]
    config = make_config()
    client = FakeClient(project)
    containers = FakeContainerManager()
    driver = FakeDriver()
    lease = FakeLease()

    monkeypatch.setattr(explore, "get_driver", lambda _n: driver)
    monkeypatch.setattr(explore.HeartbeatLease, "for_intent", lambda *a, **k: lease)
    monkeypatch.setattr(
        explore,
        "run_worker_process",
        lambda *_a, **_k: __import__("cairn.dispatcher.runtime.process", fromlist=["ProcessResult"]).ProcessResult(
            0,
            json.dumps(
                {
                    "accepted": True,
                    "data": {
                        "observations": [
                            {
                                "type": "sink",
                                "description": "yaml.load",
                                "locations": ["vulnerable_app.py:18"],
                                "payload_draft": "config=!!python/object/apply:os.system ['echo CAIRN_POC_OK']",
                                "oracle_draft": "CAIRN_POC_OK",
                            }
                        ]
                    },
                }
            ),
            "",
        ),
    )
    monkeypatch.setattr(
        explore,
        "run_healthcheck",
        lambda *_a, **_k: __import__("cairn.dispatcher.tasks.common", fromlist=["HealthcheckRun"]).HealthcheckRun(
            __import__("cairn.dispatcher.runtime.process", fromlist=["ProcessResult"]).ProcessResult(0, "", ""),
            1,
        ),
    )

    # disable task healthcheck path complexity
    config.runtime.worker_healthcheck = "startup_only"

    outcome = explore.run_explore_task(
        config,
        client,
        containers,
        project,
        "facts: []\n",
        intent,
        config.workers[0],
        TaskCancellation(),
    )
    assert outcome == "success"
    assert containers.ensure_calls
    assert containers.ensure_calls[0].get("codebase_host_path") == str(code)


def test_bootstrap_fails_clearly_when_codebase_missing(monkeypatch, tmp_path):
    origin = json.dumps({"codebase": {"path": str(tmp_path / "missing")}, "target": {}, "allowlist": []})
    intent = make_intent("i_boot")
    project = make_project(intents=[intent])
    project.facts = [Fact(id="origin", description=origin), Fact(id="goal", description="g")]
    config = make_config()
    client = FakeClient(project)
    containers = FakeContainerManager()
    monkeypatch.setattr(bootstrap, "get_driver", lambda _n: FakeDriver())
    monkeypatch.setattr(bootstrap.HeartbeatLease, "for_intent", lambda *a, **k: FakeLease())

    outcome = bootstrap.run_bootstrap_task(
        config, client, containers, project, intent, config.workers[0], TaskCancellation()
    )
    assert outcome == "failed"
    assert containers.ensure_calls == []


def test_brief_prefers_payload_draft(client: TestClient, tmp_path):
    origin = {
        "codebase": {"path": str(tmp_path), "commit": "c1"},
        "target": {"base_url": "http://127.0.0.1:18080"},
        "allowlist": ["127.0.0.1:18080"],
    }
    proj = client.post(
        "/projects",
        json={
            "title": "p3-brief",
            "origin": json.dumps(origin),
            "goal": "unauth RCE",
            "bootstrap_enabled": False,
        },
    ).json()
    pid = proj["project"]["id"]
    client.put(
        f"/projects/{pid}/base_knowledge",
        json={
            "entries": [],
            "routing_map": [
                {"src": "vulnerable_app.py:18", "live": "POST /api/import", "via": "direct", "confidence": "assumed"}
            ],
            "actor": "t",
        },
    )
    intent = client.post(
        f"/projects/{pid}/intents",
        json={"from": ["origin"], "description": "map sink", "creator": "t"},
    ).json()
    client.post(f"/projects/{pid}/intents/{intent['id']}/heartbeat", json={"worker": "w"})
    draft = "config=!!python/object/apply:os.system ['echo CAIRN_POC_OK']"
    concluded = client.post(
        f"/projects/{pid}/intents/{intent['id']}/conclude",
        json={
            "worker": "w",
            "observations": [
                {
                    "type": "source",
                    "description": "unauth POST /api/import",
                    "locations": ["vulnerable_app.py:24"],
                },
                {
                    "type": "sink",
                    "description": "yaml.load unsafe Loader — long prose that must NOT become body",
                    "locations": ["vulnerable_app.py:18"],
                    "payload_draft": draft,
                    "oracle_draft": "CAIRN_POC_OK",
                },
            ],
        },
    )
    assert concluded.status_code == 200, concluded.text
    facts = {f["type"]: f for f in concluded.json()["facts"]}
    assert facts["sink"]["payload_draft"] == draft
    chain = [facts["source"]["id"], facts["sink"]["id"]]
    v = client.post(
        f"/projects/{pid}/intents",
        json={"from": chain, "description": "VERIFY long prose should not win", "creator": "r", "task_kind": "verify"},
    )
    assert v.status_code == 201, v.text
    shape = v.json()["poc_brief"]["payload_recipe"]["shape"]
    assert shape == draft
    assert "long prose" not in shape


def test_initial_payload_uses_draft_not_prose():
    brief = {
        "payload_recipe": {
            "gadget": "yaml.load uses unsafe Loader",
            "shape": "config=!!python/object/apply:os.system ['echo CAIRN_POC_OK']",
        },
        "success_signature": {"kind": "response_match", "check": "CAIRN_POC_OK"},
        "chain": [],
    }
    body = _initial_payload(brief)
    assert "!!python" in body
    assert "uses unsafe" not in body


def test_verify_task_fires_payload_draft_to_demo(client: TestClient, demo_server, tmp_path, monkeypatch):
    hostport = demo_server.replace("http://", "")
    code = tmp_path / "code"
    code.mkdir()
    origin = {
        "codebase": {"path": str(code)},
        "target": {"base_url": demo_server},
        "allowlist": [hostport, "127.0.0.1"],
    }
    proj = client.post(
        "/projects",
        json={
            "title": "p3-verify-e2e",
            "origin": json.dumps(origin),
            "goal": "unauth RCE",
            "bootstrap_enabled": False,
        },
    ).json()
    pid = proj["project"]["id"]
    client.put(
        f"/projects/{pid}/base_knowledge",
        json={
            "entries": [],
            "routing_map": [
                {"src": "vulnerable_app.py:18", "live": "POST /api/import", "via": "direct", "confidence": "assumed"}
            ],
            "actor": "t",
        },
    )
    intent = client.post(
        f"/projects/{pid}/intents",
        json={"from": ["origin"], "description": "find", "creator": "t"},
    ).json()
    client.post(f"/projects/{pid}/intents/{intent['id']}/heartbeat", json={"worker": "w"})
    draft = "config=!!python/object/apply:os.system ['echo CAIRN_POC_OK']"
    concluded = client.post(
        f"/projects/{pid}/intents/{intent['id']}/conclude",
        json={
            "worker": "w",
            "observations": [
                {"type": "source", "description": "entry", "locations": ["vulnerable_app.py:24"]},
                {
                    "type": "sink",
                    "description": "yaml.load RCE sink description prose",
                    "locations": ["vulnerable_app.py:18"],
                    "payload_draft": draft,
                    "oracle_draft": "CAIRN_POC_OK",
                },
            ],
        },
    )
    facts = {f["type"]: f for f in concluded.json()["facts"]}
    chain = [facts["source"]["id"], facts["sink"]["id"]]
    vintent = client.post(
        f"/projects/{pid}/intents",
        json={"from": chain, "description": "VERIFY", "creator": "r", "task_kind": "verify"},
    ).json()
    assert vintent["poc_brief"]["payload_recipe"]["shape"] == draft
    client.post(f"/projects/{pid}/intents/{vintent['id']}/fire", json={"action": "approve", "actor": "human"})
    client.post(f"/projects/{pid}/intents/{vintent['id']}/heartbeat", json={"worker": "test-worker"})

    # run real verify task against demo HTTP with fake containers
    detail = client.get(f"/projects/{pid}").json()
    project = ProjectDetail.model_validate(detail)
    # re-bind intent with poc_brief from API
    intent_obj = next(i for i in project.intents if i.id == vintent["id"])
    config = make_config()
    containers = FakeContainerManager()

    class _ApiClient:
        def __init__(self, tc: TestClient, project_id: str):
            self.tc = tc
            self.project_id = project_id
            self._project = project

        def get_project(self, _pid):
            return ProjectDetail.model_validate(self.tc.get(f"/projects/{self.project_id}").json())

        def get_verify_control(self, _pid):
            return self.tc.get(f"/projects/{self.project_id}/verify/control").json()

        def record_proxy_traffic(self, _pid, **kwargs):
            return self.tc.post(f"/projects/{self.project_id}/verify/proxy_traffic", json=kwargs)

        def conclude_observations(self, project_id, intent_id, worker, observations, base_knowledge_patches=None):
            body = {"worker": worker, "observations": observations}
            if base_knowledge_patches:
                body["base_knowledge_patches"] = base_knowledge_patches
            r = self.tc.post(f"/projects/{project_id}/intents/{intent_id}/conclude", json=body)
            from cairn.dispatcher.protocol.client import ApiResult

            data = r.json() if r.content else None
            return ApiResult(status_code=r.status_code, data=data, text=r.text)

        def release(self, *a, **k):
            from cairn.dispatcher.protocol.client import ApiResult

            return ApiResult(status_code=200, data={})

        def get_base_knowledge(self, _pid):
            return self.tc.get(f"/projects/{self.project_id}/base_knowledge").json()

        def put_base_knowledge(self, _pid, **kwargs):
            from cairn.dispatcher.protocol.client import ApiResult

            r = self.tc.put(
                f"/projects/{self.project_id}/base_knowledge",
                json={
                    "entries": kwargs.get("entries") or [],
                    "routing_map": kwargs.get("routing_map") or [],
                    "expected_version": kwargs.get("expected_version"),
                    "actor": kwargs.get("actor") or "verify",
                },
            )
            data = r.json() if r.content else None
            return ApiResult(status_code=r.status_code, data=data, text=r.text)

    class _Lease:
        failure = None

        def start(self):
            return None

        def stop(self):
            return None

        def attach_process(self, _p):
            return None

    monkeypatch.setattr(verify.HeartbeatLease, "for_intent", lambda *a, **k: _Lease())

    api = _ApiClient(client, pid)
    outcome = verify.run_verify_task(
        config,
        api,  # type: ignore[arg-type]
        containers,
        project,
        "",
        intent_obj,
        config.workers[0],
        TaskCancellation(),
    )
    assert outcome == "success"
    # verify container got codebase bind
    assert any(c.get("profile") == "verify" and c.get("codebase_host_path") == str(code) for c in containers.ensure_calls)

    detail2 = client.get(f"/projects/{pid}").json()
    sink = next(f for f in detail2["facts"] if f["id"] == facts["sink"]["id"])
    assert sink["effective_confidence"] == "poc-confirmed"
    traffic = client.get(f"/projects/{pid}/verify/proxy_traffic").json()
    assert traffic
    joined = "\n".join(t.get("request") or "" for t in traffic)
    assert "!!python" in joined or "CAIRN_POC_OK" in joined
    assert "long prose" not in joined and "yaml.load RCE sink description prose" not in joined


def test_demo_app_file_exists():
    root = Path(__file__).resolve().parents[2]
    assert (root / "examples" / "vuln_yaml_import" / "app.py").is_file()
    assert (root / "examples" / "vuln_yaml_import" / "vulnerable_app.py").is_file()
