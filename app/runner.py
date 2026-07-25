from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import heapq
import json
import mimetypes
import os
import re
import shlex
import shutil
import signal
import stat as stat_module
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import httpx

from .config import Settings
from .models import ExecRequest, ExecResponse, OutputFile, StepResult


_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_STORAGE_KEY_PATTERN = re.compile(r"^[A-Fa-f0-9]{64}$")
_SESSION_METADATA = ".session.json"
_LAST_USED_MARKER = ".last-used"
_EXCHANGE_DIRECTORIES = ("inputs", "outputs", ".tmp")
_MAINTENANCE_INTERVAL_SECONDS = 60.0


class SessionNotFoundError(RuntimeError):
    """Raised when a scoped session is missing, expired, or belongs to another owner."""


class SessionBusyError(RuntimeError):
    """Raised when replacement cannot acquire the old session before the deadline."""


@dataclass(slots=True)
class _ProcessResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    output_truncated: bool
    storage_limit_exceeded: bool
    duration_seconds: float


@dataclass(slots=True)
class _SessionInfo:
    storage_key: str
    session_id: str
    owner_key: str | None
    directory: Path
    last_used: float
    generation: int = 0
    active_count: int = 0
    waiting_count: int = 0
    deleting: bool = False


class _OutputState:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.total = 0
        self.truncated = False

    def take(self, chunk: bytes) -> bytes:
        available = max(self.limit - self.total, 0)
        kept = chunk[:available]
        self.total += len(kept)
        if len(kept) != len(chunk):
            self.truncated = True
        return kept


class SandboxRunner:
    def __init__(
        self,
        settings: Settings,
        http_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self._http_transport = http_transport
        self._semaphore = asyncio.Semaphore(settings.max_concurrent_jobs)
        self._active_jobs = 0
        self._sessions: dict[str, _SessionInfo] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._expiry_heap: list[tuple[float, str, int]] = []
        self._index_lock = asyncio.Lock()
        self._indexed = False
        self._maintenance_stop = asyncio.Event()
        self._maintenance_task: asyncio.Task[None] | None = None
        self.settings.session_root.mkdir(parents=True, exist_ok=True)
        self.settings.cache_root.mkdir(parents=True, exist_ok=True)

    @property
    def active_jobs(self) -> int:
        return self._active_jobs

    @property
    def indexed_sessions(self) -> int:
        return len(self._sessions)

    @property
    def next_expiry(self) -> float | None:
        self._discard_stale_expiry_entries()
        return self._expiry_heap[0][0] if self._expiry_heap else None

    async def start(self) -> None:
        await self.ensure_indexed()
        await self.cleanup_expired_sessions()
        if self._maintenance_task is None or self._maintenance_task.done():
            self._maintenance_stop.clear()
            self._maintenance_task = asyncio.create_task(
                self._maintenance_loop(),
                name="sandbox-session-maintenance",
            )

    async def stop(self) -> None:
        self._maintenance_stop.set()
        if self._maintenance_task is not None:
            await self._maintenance_task
            self._maintenance_task = None

    async def _maintenance_loop(self) -> None:
        while not self._maintenance_stop.is_set():
            try:
                await asyncio.wait_for(
                    self._maintenance_stop.wait(),
                    timeout=_MAINTENANCE_INTERVAL_SECONDS,
                )
            except asyncio.TimeoutError:
                try:
                    await self.cleanup_expired_sessions()
                except Exception:
                    # Maintenance must never take the API process down. Runtime
                    # logging is left to the ASGI server's exception handler.
                    pass

    def _storage_key(self, owner_key: str | None, session_id: str) -> str:
        if owner_key:
            value = f"{owner_key}:{session_id}".encode("utf-8")
            return hashlib.sha256(value).hexdigest()
        return session_id

    def session_path(self, session_id: str, owner_key: str | None = None) -> Path:
        return self.settings.session_root / self._storage_key(owner_key, session_id)

    def _session_lock(self, storage_key: str) -> asyncio.Lock:
        lock = self._locks.get(storage_key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[storage_key] = lock
        return lock

    def _schedule_expiry(
        self,
        info: _SessionInfo,
        *,
        when: float | None = None,
    ) -> None:
        expires = (
            info.last_used + self.settings.session_retention_minutes * 60
            if when is None
            else when
        )
        heapq.heappush(
            self._expiry_heap,
            (expires, info.storage_key, info.generation),
        )

    def _discard_stale_expiry_entries(self) -> None:
        while self._expiry_heap:
            _, storage_key, generation = self._expiry_heap[0]
            info = self._sessions.get(storage_key)
            if info is not None and not info.deleting and info.generation == generation:
                return
            heapq.heappop(self._expiry_heap)

    def _read_last_used(self, directory: Path) -> float:
        marker = directory / _LAST_USED_MARKER
        try:
            return float(marker.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return directory.stat().st_mtime

    async def ensure_indexed(self) -> None:
        if self._indexed:
            return
        async with self._index_lock:
            if self._indexed:
                return
            for entry in self.settings.session_root.iterdir():
                if not entry.is_dir() or entry.is_symlink():
                    continue
                owner_key: str | None = None
                session_id = entry.name
                metadata_path = entry / _SESSION_METADATA
                try:
                    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                    if metadata.get("version") != 1:
                        continue
                    session_id = str(metadata.get("session_id", ""))
                    owner_value = metadata.get("owner_key")
                    owner_key = str(owner_value) if owner_value else None
                    expected_key = self._storage_key(owner_key, session_id)
                    if expected_key != entry.name:
                        continue
                except FileNotFoundError:
                    if not _SESSION_ID_PATTERN.fullmatch(entry.name):
                        continue
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    continue

                if not _SESSION_ID_PATTERN.fullmatch(session_id):
                    continue
                if owner_key is not None and not _STORAGE_KEY_PATTERN.fullmatch(owner_key):
                    continue
                info = _SessionInfo(
                    storage_key=entry.name,
                    session_id=session_id,
                    owner_key=owner_key,
                    directory=entry,
                    last_used=self._read_last_used(entry),
                )
                self._sessions[info.storage_key] = info
                self._schedule_expiry(info)
            self._indexed = True

    def _write_session_metadata(self, info: _SessionInfo) -> None:
        temporary = info.directory / f"{_SESSION_METADATA}.tmp"
        temporary.write_text(
            json.dumps(
                {
                    "version": 1,
                    "session_id": info.session_id,
                    "owner_key": info.owner_key,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        os.replace(temporary, info.directory / _SESSION_METADATA)

    def _write_last_used(self, info: _SessionInfo) -> None:
        marker = info.directory / _LAST_USED_MARKER
        temporary = info.directory / f"{_LAST_USED_MARKER}.tmp"
        temporary.write_text(str(info.last_used), encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, marker)

    def _touch_session(self, info: _SessionInfo) -> None:
        if info.deleting or not info.directory.exists():
            return
        info.last_used = time.time()
        info.generation += 1
        self._write_last_used(info)
        self._schedule_expiry(info)

    def _create_session(
        self,
        owner_key: str | None,
        *,
        requested_session_id: str | None = None,
    ) -> _SessionInfo:
        for _ in range(5):
            session_id = requested_session_id or str(uuid.uuid4())
            storage_key = self._storage_key(owner_key, session_id)
            directory = self.settings.session_root / storage_key
            if storage_key not in self._sessions and not directory.exists():
                break
            if requested_session_id:
                existing = self._sessions.get(storage_key)
                if existing is not None:
                    return existing
                raise RuntimeError(f"Session directory already exists: {session_id}")
        else:
            raise RuntimeError("Unable to allocate a unique session ID")

        directory.mkdir(parents=True, mode=0o700)
        for name in (*_EXCHANGE_DIRECTORIES, ".home"):
            (directory / name).mkdir(parents=True, mode=0o700)
        info = _SessionInfo(
            storage_key=storage_key,
            session_id=session_id,
            owner_key=owner_key,
            directory=directory,
            last_used=time.time(),
        )
        self._write_session_metadata(info)
        self._write_last_used(info)
        self._sessions[storage_key] = info
        self._schedule_expiry(info)
        return info

    def _delete_session_info(self, info: _SessionInfo) -> bool:
        if info.deleting:
            return False
        info.deleting = True
        try:
            shutil.rmtree(info.directory, ignore_errors=False)
        except FileNotFoundError:
            pass
        except OSError:
            info.deleting = False
            raise
        self._sessions.pop(info.storage_key, None)
        return True

    def _find_session(
        self,
        owner_key: str | None,
        session_id: str,
        *,
        expire_if_idle: bool = True,
    ) -> _SessionInfo | None:
        storage_key = self._storage_key(owner_key, session_id)
        info = self._sessions.get(storage_key)
        if info is None or info.deleting:
            return None
        if info.owner_key != owner_key or info.session_id != session_id:
            return None
        expires_at = info.last_used + self.settings.session_retention_minutes * 60
        if (
            expire_if_idle
            and expires_at <= time.time()
            and info.active_count == 0
            and info.waiting_count == 0
        ):
            self._delete_session_info(info)
            return None
        return info

    async def _acquire_session(self, info: _SessionInfo, timeout: float) -> None:
        lock = self._session_lock(info.storage_key)
        info.waiting_count += 1
        try:
            await asyncio.wait_for(lock.acquire(), timeout=max(timeout, 0.001))
        except asyncio.TimeoutError as exc:
            raise SessionBusyError(
                f"Session {info.session_id} is busy"
            ) from exc
        finally:
            info.waiting_count -= 1
        if info.deleting:
            lock.release()
            raise SessionNotFoundError(
                f"Session {info.session_id} no longer exists"
            )
        info.active_count += 1

    def _release_session(self, info: _SessionInfo, *, touch: bool) -> None:
        info.active_count = max(info.active_count - 1, 0)
        if touch:
            self._touch_session(info)
        lock = self._session_lock(info.storage_key)
        if lock.locked():
            lock.release()
        if info.deleting:
            self._locks.pop(info.storage_key, None)

    async def execute(
        self,
        request: ExecRequest,
        *,
        max_file_output_bytes: int | None = None,
    ) -> ExecResponse:
        async with self._semaphore:
            self._active_jobs += 1
            try:
                return await self._execute_serialized(
                    request,
                    max_file_output_bytes=max_file_output_bytes,
                )
            finally:
                self._active_jobs -= 1

    async def _execute_serialized(
        self,
        request: ExecRequest,
        *,
        max_file_output_bytes: int | None,
    ) -> ExecResponse:
        await self.ensure_indexed()
        started = time.monotonic()
        timeout = min(
            request.timeout_seconds or self.settings.default_timeout_seconds,
            self.settings.max_timeout_seconds,
        )
        deadline = started + timeout
        session_created = False
        replaced_session_id: str | None = None
        replaced_session_removed = False

        if request.new_session:
            if request.replace_session_id:
                previous = self._find_session(
                    request.owner_key,
                    request.replace_session_id,
                )
                if previous is not None:
                    remaining = max(deadline - time.monotonic(), 0.001)
                    await self._acquire_session(previous, remaining)
                    try:
                        replaced_session_id = previous.session_id
                        replaced_session_removed = self._delete_session_info(previous)
                    finally:
                        self._release_session(previous, touch=False)
            info = self._create_session(request.owner_key)
            session_created = True
        elif request.session_id:
            info = self._find_session(request.owner_key, request.session_id)
            if info is None:
                if request.owner_key:
                    raise SessionNotFoundError(
                        f"Session {request.session_id} does not exist or has expired"
                    )
                info = self._create_session(
                    None,
                    requested_session_id=request.session_id,
                )
                session_created = True
        else:
            info = self._create_session(request.owner_key)
            session_created = True

        remaining = max(deadline - time.monotonic(), 0.001)
        await self._acquire_session(info, remaining)
        result: ExecResponse | None = None
        job_id = uuid.uuid4().hex
        runtime_tmp_dir = self._runtime_tmp_directory(job_id)
        try:
            runtime_tmp_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
            result = await self._execute_in_session(
                request,
                info,
                job_id=job_id,
                runtime_tmp_dir=runtime_tmp_dir,
                started=started,
                deadline=deadline,
                max_file_output_bytes=max_file_output_bytes,
            )
        finally:
            shutil.rmtree(runtime_tmp_dir, ignore_errors=True)
            self._release_session(info, touch=True)

        result.session_created = session_created
        result.replaced_session_id = replaced_session_id
        result.replaced_session_removed = replaced_session_removed
        result.expires_at = (
            info.last_used + self.settings.session_retention_minutes * 60
        )
        return result

    async def _execute_in_session(
        self,
        request: ExecRequest,
        info: _SessionInfo,
        *,
        job_id: str,
        runtime_tmp_dir: Path,
        started: float,
        deadline: float,
        max_file_output_bytes: int | None,
    ) -> ExecResponse:
        session_dir = info.directory
        home_dir = session_dir / ".home"
        home_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

        self._reset_exchange_directories(session_dir)
        reset_error = self._reset_paths(session_dir, request)
        if reset_error:
            return self._setup_error_response(
                job_id, info.session_id, session_dir, started, reset_error
            )

        input_error = self._write_input_files(session_dir, request)
        if input_error:
            return self._setup_error_response(
                job_id, info.session_id, session_dir, started, input_error
            )

        download_error = await self._download_input_urls(
            session_dir, request, deadline
        )
        if download_error:
            return self._setup_error_response(
                job_id, info.session_id, session_dir, started, download_error
            )

        try:
            cwd = self._resolve_cwd(
                session_dir,
                request.cwd,
                restrict_to_session=bool(request.owner_key),
            )
        except ValueError as exc:
            return self._setup_error_response(
                job_id, info.session_id, session_dir, started, str(exc)
            )
        cwd.mkdir(parents=True, exist_ok=True)
        environment = self._build_environment(
            session_dir,
            home_dir,
            runtime_tmp_dir,
            request.env,
            info.session_id,
        )

        setup_steps: list[StepResult] = []

        if request.python_packages:
            python_steps = []
            venv_python = session_dir / ".venv" / "bin" / "python"
            if not venv_python.exists():
                python_steps.append(
                    (
                        "create_python_environment",
                        [
                            "uv",
                            "venv",
                            "--system-site-packages",
                            str(session_dir / ".venv"),
                        ],
                    )
                )
            python_steps.append(
                (
                    "install_python_packages",
                    [
                        "uv",
                        "pip",
                        "install",
                        "--python",
                        str(venv_python),
                        "--",
                        *request.python_packages,
                    ],
                )
            )
            failure = await self._run_setup_steps(
                python_steps,
                cwd,
                environment,
                deadline,
                setup_steps,
                session_dir,
            )
            if failure is not None:
                return self._setup_failure_response(
                    job_id,
                    info.session_id,
                    cwd,
                    started,
                    setup_steps,
                    failure,
                )

        if request.node_packages:
            node_modules = session_dir / "node_modules"
            failure = await self._run_setup_steps(
                [
                    (
                        "install_node_packages",
                        [
                            "npm",
                            "install",
                            "--prefix",
                            str(session_dir),
                            "--no-audit",
                            "--no-fund",
                            "--",
                            *request.node_packages,
                        ],
                    )
                ],
                cwd,
                environment,
                deadline,
                setup_steps,
                session_dir,
            )
            if failure is not None:
                return self._setup_failure_response(
                    job_id,
                    info.session_id,
                    cwd,
                    started,
                    setup_steps,
                    failure,
                )
            environment["NODE_PATH"] = str(node_modules)

        if self._directory_size_exceeds(
            session_dir, self.settings.max_session_bytes
        ):
            return ExecResponse(
                job_id=job_id,
                session_id=info.session_id,
                cwd=str(cwd),
                status="storage_limit",
                exit_code=137,
                stdout="",
                stderr="Session directory exceeds MAX_SESSION_BYTES",
                timed_out=False,
                output_truncated=False,
                duration_seconds=round(time.monotonic() - started, 4),
                setup_steps=setup_steps,
            )

        remaining = max(deadline - time.monotonic(), 0.001)
        result = await self._run_process(
            self._command_argv(request),
            cwd=cwd,
            environment=environment,
            timeout=remaining,
            stdin=request.stdin,
            session_dir=session_dir,
        )
        status = "completed"
        if result.timed_out:
            status = "timed_out"
        elif result.storage_limit_exceeded:
            status = "storage_limit"
        elif result.output_truncated:
            status = "output_limit_exceeded"

        files, files_truncated = self._collect_output_files(
            session_dir,
            request,
            max_file_output_bytes=max_file_output_bytes,
        )

        return ExecResponse(
            job_id=job_id,
            session_id=info.session_id,
            cwd=str(cwd),
            status=status,
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            timed_out=result.timed_out,
            output_truncated=result.output_truncated,
            duration_seconds=round(time.monotonic() - started, 4),
            setup_steps=setup_steps,
            files=files,
            files_truncated=files_truncated,
        )

    def _setup_error_response(
        self,
        job_id: str,
        session_id: str,
        cwd: Path,
        started: float,
        error: str,
    ) -> ExecResponse:
        return ExecResponse(
            job_id=job_id,
            session_id=session_id,
            cwd=str(cwd),
            status="setup_failed",
            exit_code=2,
            stdout="",
            stderr=error,
            timed_out=False,
            output_truncated=False,
            duration_seconds=round(time.monotonic() - started, 4),
        )

    async def _run_setup_steps(
        self,
        steps: list[tuple[str, list[str]]],
        cwd: Path,
        environment: dict[str, str],
        deadline: float,
        results: list[StepResult],
        session_dir: Path,
    ) -> _ProcessResult | None:
        for name, command in steps:
            remaining = max(deadline - time.monotonic(), 0.001)
            result = await self._run_process(
                command,
                cwd=cwd,
                environment=environment,
                timeout=remaining,
                stdin=None,
                session_dir=session_dir,
            )
            results.append(
                StepResult(
                    name=name,
                    exit_code=result.exit_code,
                    stdout=result.stdout,
                    stderr=result.stderr,
                    timed_out=result.timed_out,
                    output_truncated=result.output_truncated,
                    duration_seconds=round(result.duration_seconds, 4),
                )
            )
            if (
                result.exit_code != 0
                or result.timed_out
                or result.output_truncated
                or result.storage_limit_exceeded
            ):
                return result
        return None

    def _setup_failure_response(
        self,
        job_id: str,
        session_id: str,
        cwd: Path,
        started: float,
        setup_steps: list[StepResult],
        failure: _ProcessResult,
    ) -> ExecResponse:
        stderr = failure.stderr
        if failure.storage_limit_exceeded:
            stderr = f"{stderr}\nSession directory exceeds MAX_SESSION_BYTES".strip()
        return ExecResponse(
            job_id=job_id,
            session_id=session_id,
            cwd=str(cwd),
            status="storage_limit" if failure.storage_limit_exceeded else "setup_failed",
            exit_code=failure.exit_code,
            stdout=failure.stdout,
            stderr=stderr,
            timed_out=failure.timed_out,
            output_truncated=failure.output_truncated,
            duration_seconds=round(time.monotonic() - started, 4),
            setup_steps=setup_steps,
        )

    def _reset_exchange_directories(self, session_dir: Path) -> None:
        for name in _EXCHANGE_DIRECTORIES:
            target = session_dir / name
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            elif target.exists() or target.is_symlink():
                target.unlink()
            target.mkdir(parents=True, mode=0o700)

    def _resolve_cwd(
        self,
        session_dir: Path,
        requested: str | None,
        *,
        restrict_to_session: bool,
    ) -> Path:
        if not requested:
            return session_dir
        path = Path(requested).expanduser()
        if not path.is_absolute():
            path = session_dir / path
        resolved = path.resolve()
        if restrict_to_session and not resolved.is_relative_to(session_dir.resolve()):
            raise ValueError("cwd must stay inside the session directory")
        return resolved

    def _write_input_files(self, session_dir: Path, request: ExecRequest) -> str | None:
        total_bytes = 0
        session_root = session_dir.resolve()
        for uploaded in request.input_files:
            try:
                content = base64.b64decode(uploaded.content_base64, validate=True)
            except (ValueError, binascii.Error):
                return f"Invalid base64 content for input file: {uploaded.path}"
            total_bytes += len(content)
            if total_bytes > self.settings.max_input_bytes:
                return "Input files exceed MAX_INPUT_BYTES"

            destination = (session_dir / uploaded.path).resolve()
            if not destination.is_relative_to(session_root):
                return f"Input file escapes the session directory: {uploaded.path}"
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
        return None

    def _reset_paths(self, session_dir: Path, request: ExecRequest) -> str | None:
        session_root = session_dir.resolve()
        exchange = set(_EXCHANGE_DIRECTORIES)
        for relative in request.reset_paths:
            if relative.rstrip("/") in exchange:
                continue
            target = (session_dir / relative).resolve()
            if target == session_root or not target.is_relative_to(session_root):
                return f"Reset path escapes the session directory: {relative}"
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            elif target.exists() or target.is_symlink():
                target.unlink()
        return None

    async def _download_input_urls(
        self, session_dir: Path, request: ExecRequest, deadline: float
    ) -> str | None:
        if not request.input_urls:
            return None

        session_root = session_dir.resolve()
        total_bytes = 0
        timeout = httpx.Timeout(30.0, connect=15.0)
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout,
            headers={"User-Agent": "PrivateRobotSandbox/0.3"},
            transport=self._http_transport,
        ) as client:
            for remote in request.input_urls:
                destination = (session_dir / remote.path).resolve()
                if not destination.is_relative_to(session_root):
                    return f"Input URL path escapes the session directory: {remote.path}"
                destination.parent.mkdir(parents=True, exist_ok=True)

                try:
                    remaining = max(deadline - time.monotonic(), 0.001)
                    async with asyncio.timeout(remaining):
                        async with client.stream("GET", remote.url) as response:
                            response.raise_for_status()
                            with destination.open("wb") as output:
                                async for chunk in response.aiter_bytes(65_536):
                                    total_bytes += len(chunk)
                                    if total_bytes > self.settings.max_input_bytes:
                                        output.close()
                                        destination.unlink(missing_ok=True)
                                        return "Downloaded input files exceed MAX_INPUT_BYTES"
                                    output.write(chunk)
                except TimeoutError:
                    destination.unlink(missing_ok=True)
                    return f"Timed out downloading {remote.url}"
                except httpx.HTTPError as exc:
                    destination.unlink(missing_ok=True)
                    return f"Failed to download {remote.url}: {exc}"
        return None

    def _collect_output_files(
        self,
        session_dir: Path,
        request: ExecRequest,
        *,
        max_file_output_bytes: int | None = None,
    ) -> tuple[list[OutputFile], bool]:
        if not request.output_files:
            return [], False

        session_root = session_dir.resolve()
        candidates: dict[str, Path] = {}
        for pattern in request.output_files:
            for path in session_dir.glob(pattern):
                resolved = path.resolve()
                if (
                    resolved.is_relative_to(session_root)
                    and resolved.is_file()
                    and not path.is_symlink()
                    and not resolved.is_symlink()
                ):
                    relative = resolved.relative_to(session_root).as_posix()
                    candidates[relative] = resolved

        results: list[OutputFile] = []
        total_bytes = 0
        truncated = False
        output_limit = (
            self.settings.max_file_output_bytes
            if max_file_output_bytes is None
            else max_file_output_bytes
        )
        for relative, path in sorted(candidates.items()):
            if len(results) >= self.settings.max_output_files:
                truncated = True
                break
            size = path.stat().st_size
            if total_bytes + size > output_limit:
                truncated = True
                continue
            content = path.read_bytes()
            total_bytes += len(content)
            results.append(
                OutputFile(
                    path=relative,
                    mime_type=self._guess_mime_type(path, content),
                    size=len(content),
                    content_base64=base64.b64encode(content).decode("ascii"),
                )
            )
        return results, truncated

    def _guess_mime_type(self, path: Path, content: bytes) -> str:
        if content.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if content.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if content.startswith((b"GIF87a", b"GIF89a")):
            return "image/gif"
        if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
            return "image/webp"
        guessed, _ = mimetypes.guess_type(path.name)
        return guessed or "application/octet-stream"

    def _build_environment(
        self,
        session_dir: Path,
        home_dir: Path,
        runtime_tmp_dir: Path,
        requested: dict[str, str],
        session_id: str,
    ) -> dict[str, str]:
        venv_bin = session_dir / ".venv" / "bin"
        base_path = os.getenv(
            "PATH",
            "/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin",
        )
        environment = {
            "PATH": f"{venv_bin}:{base_path}",
            "HOME": str(home_dir),
            "LANG": os.getenv("LANG", "C.UTF-8"),
            "LC_ALL": os.getenv("LC_ALL", "C.UTF-8"),
            "TERM": os.getenv("TERM", "xterm-256color"),
            "PYTHONUNBUFFERED": "1",
            "UV_CACHE_DIR": str(self.settings.cache_root / "uv"),
            "PIP_CACHE_DIR": str(self.settings.cache_root / "pip"),
            "npm_config_cache": str(self.settings.cache_root / "npm"),
        }
        environment.update(requested)
        chromium = (
            shutil.which("chromium")
            or shutil.which("chromium-browser")
            or shutil.which("google-chrome")
            or ""
        )
        environment.update(
            {
                "SANDBOX_SESSION_ID": session_id,
                "SANDBOX_SESSION_DIR": str(session_dir),
                "SANDBOX_INPUT_DIR": str(session_dir / "inputs"),
                "SANDBOX_OUTPUT_DIR": str(session_dir / "outputs"),
                "SANDBOX_CHROMIUM": chromium,
                "TMPDIR": str(runtime_tmp_dir),
            }
        )
        return environment

    def _runtime_tmp_directory(self, job_id: str) -> Path:
        # Chromium creates a Unix domain socket below TMPDIR. Linux limits these
        # socket paths to roughly 108 bytes, while persistent session paths contain
        # a 64-character owner/session hash. Keep the per-job path deliberately
        # short and remove it as soon as the call finishes.
        root = Path("/tmp") if os.name == "posix" else self.settings.cache_root.parent
        return root / f"sb-{job_id[:12]}"

    def _command_argv(self, request: ExecRequest) -> list[str]:
        if isinstance(request.command, list):
            return request.command
        if request.shell:
            return ["/bin/bash", "-c", request.command]
        return shlex.split(request.command)

    def _directory_size_exceeds(self, directory: Path, limit: int) -> bool:
        total = 0
        pending = [directory]
        while pending:
            current = pending.pop()
            try:
                with os.scandir(current) as entries:
                    for entry in entries:
                        try:
                            file_stat = entry.stat(follow_symlinks=False)
                        except FileNotFoundError:
                            continue
                        if stat_module.S_ISLNK(file_stat.st_mode):
                            continue
                        if stat_module.S_ISDIR(file_stat.st_mode):
                            pending.append(Path(entry.path))
                        elif stat_module.S_ISREG(file_stat.st_mode):
                            total += file_stat.st_size
                            if total > limit:
                                return True
            except FileNotFoundError:
                continue
        return False

    async def _run_process(
        self,
        command: list[str],
        *,
        cwd: Path,
        environment: dict[str, str],
        timeout: float,
        stdin: str | None,
        session_dir: Path,
    ) -> _ProcessResult:
        started = time.monotonic()
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(cwd),
                env=environment,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            exit_code = 127 if isinstance(exc, FileNotFoundError) else 126
            return _ProcessResult(
                exit_code=exit_code,
                stdout="",
                stderr=str(exc),
                timed_out=False,
                output_truncated=False,
                storage_limit_exceeded=False,
                duration_seconds=time.monotonic() - started,
            )
        assert process.stdout is not None
        assert process.stderr is not None
        assert process.stdin is not None

        if stdin:
            process.stdin.write(stdin.encode("utf-8"))
        process.stdin.close()

        state = _OutputState(self.settings.max_output_bytes)
        limit_event = asyncio.Event()
        stdout_buffer = bytearray()
        stderr_buffer = bytearray()

        async def read_stream(
            stream: asyncio.StreamReader, buffer: bytearray
        ) -> None:
            while True:
                chunk = await stream.read(65_536)
                if not chunk:
                    return
                kept = state.take(chunk)
                buffer.extend(kept)
                if state.truncated:
                    limit_event.set()

        async def monitor_storage() -> bool:
            while True:
                if self._directory_size_exceeds(
                    session_dir, self.settings.max_session_bytes
                ):
                    return True
                await asyncio.sleep(1)

        stdout_task = asyncio.create_task(read_stream(process.stdout, stdout_buffer))
        stderr_task = asyncio.create_task(read_stream(process.stderr, stderr_buffer))
        wait_task = asyncio.create_task(process.wait())
        limit_task = asyncio.create_task(limit_event.wait())
        storage_task = asyncio.create_task(monitor_storage())

        timed_out = False
        storage_limit_exceeded = False
        try:
            done, _ = await asyncio.wait(
                {wait_task, limit_task, storage_task},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if wait_task not in done:
                timed_out = not done
                storage_limit_exceeded = storage_task in done
                await self._terminate_process_group(process)
            await wait_task
        finally:
            limit_task.cancel()
            storage_task.cancel()
            await asyncio.gather(
                limit_task,
                storage_task,
                return_exceptions=True,
            )
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)

        if (
            not storage_limit_exceeded
            and self._directory_size_exceeds(
                session_dir, self.settings.max_session_bytes
            )
        ):
            storage_limit_exceeded = True

        return _ProcessResult(
            exit_code=process.returncode if process.returncode is not None else -1,
            stdout=stdout_buffer.decode("utf-8", errors="replace"),
            stderr=stderr_buffer.decode("utf-8", errors="replace"),
            timed_out=timed_out,
            output_truncated=state.truncated,
            storage_limit_exceeded=storage_limit_exceeded,
            duration_seconds=time.monotonic() - started,
        )

    async def _terminate_process_group(
        self, process: asyncio.subprocess.Process
    ) -> None:
        if os.name == "nt":
            try:
                process.terminate()
            except ProcessLookupError:
                return
            try:
                await asyncio.wait_for(process.wait(), timeout=1.5)
                return
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=1.5)
            return
        except asyncio.TimeoutError:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        await process.wait()

    async def cleanup_expired_sessions(self) -> dict[str, int | float | None]:
        await self.ensure_indexed()
        now = time.time()
        self._discard_stale_expiry_entries()
        if not self._expiry_heap or self._expiry_heap[0][0] > now:
            return {
                "checked": 0,
                "removed": 0,
                "next_expiry": self.next_expiry,
            }

        checked = 0
        removed = 0
        while self._expiry_heap and self._expiry_heap[0][0] <= now:
            _, storage_key, generation = heapq.heappop(self._expiry_heap)
            info = self._sessions.get(storage_key)
            if info is None or info.deleting or info.generation != generation:
                continue
            checked += 1
            if info.active_count > 0 or info.waiting_count > 0:
                self._schedule_expiry(
                    info,
                    when=now + _MAINTENANCE_INTERVAL_SECONDS,
                )
                continue
            expires_at = (
                info.last_used + self.settings.session_retention_minutes * 60
            )
            if expires_at > now:
                self._schedule_expiry(info, when=expires_at)
                continue
            self._delete_session_info(info)
            removed += 1

        return {
            "checked": checked,
            "removed": removed,
            "next_expiry": self.next_expiry,
        }

    async def cleanup_session(
        self,
        session_id: str,
        owner_key: str | None = None,
    ) -> bool:
        await self.ensure_indexed()
        info = self._find_session(
            owner_key,
            session_id,
            expire_if_idle=False,
        )
        if info is None or info.active_count > 0 or info.waiting_count > 0:
            return False
        await self._acquire_session(info, self.settings.default_timeout_seconds)
        try:
            return self._delete_session_info(info)
        finally:
            self._release_session(info, touch=False)
