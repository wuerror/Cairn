from __future__ import annotations

import io
import logging
from pathlib import PurePosixPath
import tarfile
import threading
import uuid

import docker
from docker.errors import APIError, DockerException, NotFound
from docker.models.containers import Container

from cairn.dispatcher.config import ContainerConfig, ContainerProfileName, ResolvedContainerProfile
from cairn.dispatcher.runtime.process import ManagedProcess

LOG = logging.getLogger(__name__)


class ContainerManager:
    _PREFIX = "cairn-dispatch-"
    _VERIFY_PREFIX = "cairn-verify-"
    _STARTUP_PREFIX = "cairn-startup-healthcheck-"

    def __init__(self, config: ContainerConfig):
        self._config = config
        self._client = docker.from_env()
        self._ensure_running_locks: dict[str, threading.Lock] = {}
        self._ensure_running_locks_guard = threading.Lock()
        self._verify_containers: dict[str, set[str]] = {}
        self._verify_containers_lock = threading.Lock()

    def close(self) -> None:
        self._client.close()

    def container_name(self, project_id: str, profile: ContainerProfileName = "static") -> str:
        sanitized = project_id.replace("/", "-")
        if profile == "verify":
            return f"{self._VERIFY_PREFIX}{sanitized}-{uuid.uuid4().hex[:8]}"
        return f"{self._PREFIX}{sanitized}"

    def static_container_name(self, project_id: str) -> str:
        return self.container_name(project_id, "static")

    def ensure_running(
        self,
        project_id: str,
        profile: ContainerProfileName = "static",
        *,
        codebase_host_path: str | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> str:
        resolved = self._config.resolve_profile(
            profile,
            codebase_host_path=codebase_host_path,
            extra_env=extra_env,
        )
        if profile == "verify":
            # always create a fresh short-lived verify container
            name = self.container_name(project_id, "verify")
            created = self._create_container(project_id, name, resolved)
            with self._verify_containers_lock:
                self._verify_containers.setdefault(project_id, set()).add(created)
            return created
        name = self.container_name(project_id, "static")
        with self._ensure_running_lock(name):
            return self._ensure_running_locked(project_id, name, resolved)

    def destroy_verify_containers(self, project_id: str) -> int:
        """Kill-switch: immediately remove all tracked verify containers for project."""
        with self._verify_containers_lock:
            names = list(self._verify_containers.get(project_id, set()))
            self._verify_containers[project_id] = set()
        # also sweep by name prefix in case tracking missed
        prefix = f"{self._VERIFY_PREFIX}{project_id.replace('/', '-')}"
        for name in self.managed_container_names():
            if name.startswith(prefix) and name not in names:
                names.append(name)
        removed = 0
        for name in names:
            try:
                self.remove_container(name, force=True)
                removed += 1
            except Exception as exc:
                LOG.warning("destroy verify container failed name=%s error=%s", name, exc)
        return removed

    def _ensure_running_locked(
        self, project_id: str, name: str, profile: ResolvedContainerProfile | None = None
    ) -> str:
        resolved = profile or self._config.resolve_profile("static")
        state = self.inspect_state(name)
        if state == "running":
            LOG.debug("container already running project=%s container=%s", project_id, name)
            return name
        if state is not None:
            LOG.info("starting existing container project=%s container=%s state=%s", project_id, name, state)
            self._start_existing(name)
            return name
        return self._create_container(project_id, name, resolved)

    def _create_container(self, project_id: str, name: str, profile: ResolvedContainerProfile) -> str:
        LOG.info(
            "creating container project=%s container=%s image=%s profile=%s binds=%s env_keys=%s",
            project_id,
            name,
            profile.image,
            profile.name,
            profile.binds,
            sorted(profile.environment.keys()),
        )
        run_kwargs: dict = {
            "detach": True,
            "name": name,
            "network_mode": profile.network_mode,
            "cap_add": profile.cap_add or None,
        }
        if profile.environment:
            run_kwargs["environment"] = profile.environment
        if profile.binds:
            run_kwargs["volumes"] = profile.binds
        try:
            self._client.containers.run(
                profile.image,
                ["sleep", "infinity"],
                **run_kwargs,
            )
            LOG.info("created container project=%s container=%s", project_id, name)
            return name
        except APIError as exc:
            if not self._is_name_conflict(exc):
                raise RuntimeError(f"failed to create container {name}: {exc}") from exc
        LOG.info("container name conflict, reusing existing container project=%s container=%s", project_id, name)
        state = self.inspect_state(name)
        if state == "running":
            return name
        if state is not None:
            LOG.info("starting conflicted existing container project=%s container=%s state=%s", project_id, name, state)
            self._start_existing(name)
            return name
        raise RuntimeError(f"failed to create container {name}")

    def _ensure_running_lock(self, name: str) -> threading.Lock:
        with self._ensure_running_locks_guard:
            lock = self._ensure_running_locks.get(name)
            if lock is None:
                lock = threading.Lock()
                self._ensure_running_locks[name] = lock
            return lock

    def create_startup_container(self) -> str:
        name = f"{self._STARTUP_PREFIX}{uuid.uuid4().hex[:12]}"
        profile = self._config.resolve_profile("static")
        LOG.debug("creating startup healthcheck container container=%s image=%s", name, profile.image)
        try:
            self._client.containers.run(
                profile.image,
                ["sleep", "infinity"],
                detach=True,
                name=name,
                network_mode=profile.network_mode,
                cap_add=profile.cap_add or None,
            )
        except DockerException as exc:
            raise RuntimeError(f"failed to create startup container {name}: {exc}") from exc
        return name

    def inspect_state(self, name: str) -> str | None:
        container = self._get_container(name)
        if container is None:
            return None
        try:
            container.reload()
        except DockerException as exc:
            raise RuntimeError(f"failed to inspect container {name}: {exc}") from exc
        state = container.attrs.get("State", {}).get("Status")
        return str(state) if state else None

    def cleanup_completed(self, project_id: str) -> bool:
        name = self.static_container_name(project_id)
        state = self.inspect_state(name)
        if state is None:
            return True
        container = self._require_container(name)
        if self._config.completed_action == "remove":
            LOG.info("removing completed project container project=%s container=%s", project_id, name)
            try:
                container.remove(force=True)
            except NotFound:
                return True
            except DockerException as exc:
                LOG.warning("failed to remove container=%s error=%s", name, exc)
                return False
            return self.inspect_state(name) is None
        elif state == "running":
            LOG.info("stopping completed project container project=%s container=%s", project_id, name)
            try:
                container.stop(timeout=1)
            except NotFound:
                return True
            except DockerException as exc:
                LOG.warning("failed to stop container=%s error=%s", name, exc)
                return False
            return self.inspect_state(name) != "running"
        return True

    def cleanup_stopped(self, project_id: str) -> bool:
        name = self.static_container_name(project_id)
        state = self.inspect_state(name)
        if state != "running":
            return True
        LOG.info("stopping stopped project container project=%s container=%s", project_id, name)
        container = self._require_container(name)
        try:
            container.stop(timeout=1)
        except NotFound:
            return True
        except DockerException as exc:
            LOG.warning("failed to stop stopped project container=%s error=%s", name, exc)
            return False
        return self.inspect_state(name) != "running"

    def cleanup_orphan(self, name: str) -> bool:
        state = self.inspect_state(name)
        if state is None:
            return True
        LOG.info("removing orphan project container container=%s state=%s", name, state)
        container = self._require_container(name)
        try:
            container.remove(force=True)
        except NotFound:
            return True
        except DockerException as exc:
            LOG.warning("failed to remove orphan container=%s error=%s", name, exc)
            return False
        return self.inspect_state(name) is None

    def managed_container_names(self) -> list[str]:
        try:
            containers = self._client.containers.list(all=True)
        except DockerException as exc:
            LOG.warning("failed to list managed containers error=%s", exc)
            return []
        return sorted(
            container.name
            for container in containers
            if container.name.startswith(self._PREFIX) or container.name.startswith(self._VERIFY_PREFIX)
        )

    def needs_completed_cleanup(self, project_id: str) -> bool:
        name = self.static_container_name(project_id)
        state = self.inspect_state(name)
        if state is None:
            return False
        if self._config.completed_action == "remove":
            return True
        return state == "running"

    def needs_orphan_cleanup(self, name: str) -> bool:
        return self.inspect_state(name) is not None

    def needs_stopped_cleanup(self, project_id: str) -> bool:
        return self.inspect_state(self.static_container_name(project_id)) == "running"

    def build_exec_process(
        self,
        container_name: str,
        env: dict[str, str],
        command: list[str],
        timeout_seconds: int | None = None,
        kill_after_seconds: int = 5,
    ) -> ManagedProcess:
        container = self._require_container(container_name)
        argv: list[str] = []
        if timeout_seconds is not None:
            argv.extend(
                [
                    "timeout",
                    "-k",
                    f"{kill_after_seconds}s",
                    f"{timeout_seconds}s",
                ]
            )
        argv.extend(command)
        return ManagedProcess(container, argv, env)

    def write_text_file(self, container_name: str, path: str, content: str) -> None:
        archive_path, archive = self._text_file_archive(path, content)
        container = self._require_container(container_name)
        try:
            ok = container.put_archive(archive_path, archive)
        except DockerException as exc:
            raise RuntimeError(f"failed to write container file {path}: {exc}") from exc
        if not ok:
            raise RuntimeError(f"failed to write container file {path}")

    def remove_container(self, name: str, *, force: bool = True) -> None:
        container = self._get_container(name)
        if container is None:
            return
        try:
            container.remove(force=force)
        except NotFound:
            return
        except DockerException as exc:
            LOG.warning("failed to remove container=%s error=%s", name, exc)
        finally:
            with self._verify_containers_lock:
                for project_id, names in list(self._verify_containers.items()):
                    if name in names:
                        names.discard(name)

    def _start_existing(self, name: str) -> None:
        LOG.debug("starting container=%s", name)
        container = self._require_container(name)
        try:
            container.start()
            return
        except DockerException as exc:
            if self.inspect_state(name) == "running":
                return
            raise RuntimeError(f"failed to start container {name}: {exc}") from exc

    def _get_container(self, name: str) -> Container | None:
        try:
            return self._client.containers.get(name)
        except NotFound:
            return None
        except DockerException as exc:
            raise RuntimeError(f"failed to get container {name}: {exc}") from exc

    def _require_container(self, name: str) -> Container:
        container = self._get_container(name)
        if container is None:
            raise RuntimeError(f"container not found: {name}")
        return container

    @staticmethod
    def _is_name_conflict(exc: APIError) -> bool:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        explanation = str(getattr(exc, "explanation", "") or exc)
        return status_code == 409 or "is already in use" in explanation

    @staticmethod
    def _text_file_archive(path: str, content: str) -> tuple[str, bytes]:
        target = PurePosixPath(path)
        if not target.is_absolute() or target.name in ("", ".", ".."):
            raise ValueError(f"container file path must be absolute: {path}")
        parts = target.parts[1:]
        if not parts or any(part in ("", ".", "..") for part in parts):
            raise ValueError(f"invalid container file path: {path}")
        if len(parts) == 1:
            archive_path = "/"
            archive_parts = parts
        else:
            archive_path = f"/{parts[0]}"
            archive_parts = parts[1:]

        payload = content.encode("utf-8")
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as archive:
            parent = ""
            for part in archive_parts[:-1]:
                parent = f"{parent}/{part}" if parent else part
                info = tarfile.TarInfo(parent)
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                archive.addfile(info)

            file_name = "/".join(archive_parts)
            info = tarfile.TarInfo(file_name)
            info.size = len(payload)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(payload))
        return archive_path, stream.getvalue()
