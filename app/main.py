from __future__ import annotations

import json
from contextlib import asynccontextmanager
from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Path, Query
from fastapi.responses import StreamingResponse

from .auth import make_token_dependency
from .config import Settings
from .models import CleanupResponse, ExecRequest, ExecResponse, HealthResponse
from .runner import SandboxRunner, SessionBusyError, SessionNotFoundError


settings = Settings.from_env()
runner = SandboxRunner(settings)
require_token = make_token_dependency(settings)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await runner.start()
    try:
        yield
    finally:
        await runner.stop()


app = FastAPI(
    title="Private Robot Sandbox",
    version="0.3.0",
    description="An intentionally permissive, authenticated command runner for a private bot.",
    lifespan=lifespan,
)


@app.get("/", include_in_schema=False)
async def root() -> dict[str, str]:
    return {"service": "private-robot-sandbox", "docs": "/docs"}


@app.get("/healthz", response_model=HealthResponse)
async def healthz() -> HealthResponse:
    return HealthResponse(
        status="ok",
        authentication="application-bearer-token",
        auth_configured=bool(settings.token_sha256),
        active_jobs=runner.active_jobs,
        max_concurrent_jobs=settings.max_concurrent_jobs,
        default_timeout_seconds=settings.default_timeout_seconds,
        max_timeout_seconds=settings.max_timeout_seconds,
        max_output_bytes=settings.max_output_bytes,
        max_input_bytes=settings.max_input_bytes,
        max_file_output_bytes=settings.max_file_output_bytes,
        max_stream_file_output_bytes=settings.max_stream_file_output_bytes,
        max_output_files=settings.max_output_files,
        session_retention_minutes=settings.session_retention_minutes,
        max_session_bytes=settings.max_session_bytes,
        indexed_sessions=runner.indexed_sessions,
        next_expiry=runner.next_expiry,
    )


async def run_request(
    request: ExecRequest,
    *,
    max_file_output_bytes: int | None = None,
) -> ExecResponse:
    try:
        return await runner.execute(
            request,
            max_file_output_bytes=max_file_output_bytes,
        )
    except SessionNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "session_not_found", "message": str(exc)},
        ) from exc
    except SessionBusyError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "session_busy", "message": str(exc)},
        ) from exc


@app.post(
    "/v1/exec",
    response_model=ExecResponse,
    dependencies=[Depends(require_token)],
)
async def execute(request: ExecRequest) -> ExecResponse:
    return await run_request(request)


_STREAM_CHUNK_CHARACTERS = 262_144


def iter_exec_stream(response: ExecResponse) -> Iterator[bytes]:
    result = response.model_dump(
        exclude={"files": {"__all__": {"content_base64"}}}
    )
    yield (
        json.dumps(
            {"type": "result", "data": result},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")

    for index, file in enumerate(response.files):
        content = file.content_base64
        for offset in range(0, len(content), _STREAM_CHUNK_CHARACTERS):
            yield (
                json.dumps(
                    {
                        "type": "file_chunk",
                        "index": index,
                        "content_base64": content[
                            offset : offset + _STREAM_CHUNK_CHARACTERS
                        ],
                    },
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")
    yield b'{"type":"end"}\n'


@app.post(
    "/v1/exec-stream",
    dependencies=[Depends(require_token)],
)
async def execute_stream(request: ExecRequest) -> StreamingResponse:
    result = await run_request(
        request,
        max_file_output_bytes=settings.max_stream_file_output_bytes,
    )
    return StreamingResponse(
        iter_exec_stream(result),
        media_type="application/x-ndjson",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
        },
    )


@app.delete(
    "/v1/sessions/{session_id}",
    response_model=CleanupResponse,
    dependencies=[Depends(require_token)],
)
async def cleanup_session(
    session_id: Annotated[
        str, Path(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    ],
    owner_key: Annotated[
        str | None,
        Query(pattern=r"^[a-f0-9]{64}$"),
    ] = None,
) -> CleanupResponse:
    return CleanupResponse(
        session_id=session_id,
        removed=await runner.cleanup_session(session_id, owner_key),
    )
