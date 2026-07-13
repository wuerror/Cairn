from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import logging
import threading

from pydantic import TypeAdapter
import requests
from requests.adapters import HTTPAdapter

from cairn.server.models import Intent, ProjectDetail, ProjectSummary, Settings

LOG = logging.getLogger(__name__)


class ProtocolError(RuntimeError):
    def __init__(self, message: str, status_code: int, response_text: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.response_text = response_text


@dataclass(slots=True)
class ApiResult:
    status_code: int
    data: Any | None = None
    text: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300


class CairnClient:
    def __init__(self, base_url: str, timeout: float = 10.0):
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._summary_adapter = TypeAdapter(list[ProjectSummary])
        self._local = threading.local()
        self._sessions: dict[int, requests.Session] = {}
        self._sessions_lock = threading.Lock()

    def close(self) -> None:
        with self._sessions_lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            session.close()

    def list_projects(self) -> list[ProjectSummary]:
        response = self._session().get(self._url("/projects"), timeout=self._timeout)
        response.raise_for_status()
        return self._summary_adapter.validate_python(response.json())

    def get_project(self, project_id: str) -> ProjectDetail:
        response = self._session().get(self._url(f"/projects/{project_id}"), timeout=self._timeout)
        response.raise_for_status()
        return ProjectDetail.model_validate(response.json())

    def get_settings(self) -> Settings:
        response = self._session().get(self._url("/settings"), timeout=self._timeout)
        response.raise_for_status()
        return Settings.model_validate(response.json())

    def export_project(self, project_id: str) -> str:
        response = self._session().get(
            self._url(f"/projects/{project_id}/export"),
            params={"format": "yaml"},
            timeout=self._timeout,
        )
        response.raise_for_status()
        return response.text

    def export_relevant_subgraph(self, project_id: str, max_hops: int = 8) -> str:
        response = self._session().get(
            self._url(f"/projects/{project_id}/relevant_subgraph"),
            params={"format": "yaml", "max_hops": max_hops},
            timeout=self._timeout,
        )
        response.raise_for_status()
        return response.text

    def get_base_knowledge(self, project_id: str) -> dict:
        response = self._session().get(
            self._url(f"/projects/{project_id}/base_knowledge"),
            timeout=self._timeout,
        )
        response.raise_for_status()
        return response.json()

    def put_base_knowledge(
        self,
        project_id: str,
        *,
        entries: list[dict],
        routing_map: list[dict] | None = None,
        expected_version: int | None = None,
        actor: str = "worker",
    ) -> ApiResult:
        payload: dict[str, Any] = {
            "entries": entries,
            "routing_map": routing_map or [],
            "actor": actor,
        }
        if expected_version is not None:
            payload["expected_version"] = expected_version
        return self._request_json("PUT", f"/projects/{project_id}/base_knowledge", json=payload)

    def patch_base_knowledge_entry(
        self,
        project_id: str,
        entry_id: str,
        *,
        revised_by: str,
        actor: str = "worker",
        statement: str | None = None,
        evidence: list[str] | None = None,
        confidence: str | None = None,
        expected_version: int | None = None,
    ) -> ApiResult:
        payload: dict[str, Any] = {"revised_by": revised_by, "actor": actor}
        if statement is not None:
            payload["statement"] = statement
        if evidence is not None:
            payload["evidence"] = evidence
        if confidence is not None:
            payload["confidence"] = confidence
        if expected_version is not None:
            payload["expected_version"] = expected_version
        return self._request_json(
            "PATCH",
            f"/projects/{project_id}/base_knowledge/entries/{entry_id}",
            json=payload,
        )

    def heartbeat(self, project_id: str, intent_id: str, worker: str) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/intents/{intent_id}/heartbeat",
            json={"worker": worker},
        )

    def claim_reason(self, project_id: str, worker: str, trigger: str) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/reason/claim",
            json={"worker": worker, "trigger": trigger},
        )

    def reason_heartbeat(self, project_id: str, worker: str) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/reason/heartbeat",
            json={"worker": worker},
        )

    def release_reason(self, project_id: str, worker: str) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/reason/release",
            json={"worker": worker},
        )

    def release(self, project_id: str, intent_id: str, worker: str) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/intents/{intent_id}/release",
            json={"worker": worker},
        )

    def conclude(self, project_id: str, intent_id: str, worker: str, description: str) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/intents/{intent_id}/conclude",
            json={"worker": worker, "description": description},
        )

    def conclude_observations(
        self,
        project_id: str,
        intent_id: str,
        worker: str,
        observations: list[dict],
        base_knowledge_patches: list[dict] | None = None,
    ) -> ApiResult:
        payload: dict[str, Any] = {"worker": worker, "observations": observations}
        if base_knowledge_patches:
            payload["base_knowledge_patches"] = base_knowledge_patches
        return self._request_json(
            "POST",
            f"/projects/{project_id}/intents/{intent_id}/conclude",
            json=payload,
        )

    def complete(self, project_id: str, from_ids: list[str], description: str, worker: str) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/complete",
            json={"from": from_ids, "description": description, "worker": worker},
        )

    def create_intent(self, project_id: str, from_ids: list[str], description: str, creator: str) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/intents",
            json={"from": from_ids, "description": description, "creator": creator, "worker": None},
        )

    def _request_json(self, method: str, path: str, json: dict[str, Any]) -> ApiResult:
        try:
            response = self._session().request(
                method,
                self._url(path),
                json=json,
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            LOG.warning("request failed method=%s path=%s error=%s", method, path, exc)
            return ApiResult(status_code=0, text=str(exc))
        data: Any | None = None
        if response.headers.get("content-type", "").startswith("application/json"):
            data = response.json()
        return ApiResult(status_code=response.status_code, data=data, text=response.text)

    def _url(self, path: str) -> str:
        return f"{self._base_url}{path}"

    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is not None:
            return session

        session = requests.Session()
        adapter = HTTPAdapter(pool_connections=64, pool_maxsize=64, pool_block=False)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        self._local.session = session
        with self._sessions_lock:
            self._sessions[threading.get_ident()] = session
        return session
