from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path


def _read_int(
    name: str,
    default: int,
    minimum: int = 1,
    maximum: int | None = None,
) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if value < minimum:
        raise RuntimeError(f"{name} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise RuntimeError(f"{name} must be at most {maximum}")
    return value


def _resolve_token_hash() -> str:
    """Return the expected SHA-256 hex digest of the bearer token.

    ``SANDBOX_TOKEN`` holds the raw token in plaintext and takes precedence: it
    is hashed at startup so operators can supply a plaintext token when not
    using the Docker Compose SHA-256-only deployment.
    When it is unset we fall back to ``SANDBOX_TOKEN_SHA256``, a pre-computed
    digest that ships as the image default.
    """
    raw_token = os.getenv("SANDBOX_TOKEN", "").strip()
    if raw_token:
        return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    token_hash = os.getenv("SANDBOX_TOKEN_SHA256", "").strip().lower()
    if token_hash and (
        len(token_hash) != 64
        or any(character not in "0123456789abcdef" for character in token_hash)
    ):
        raise RuntimeError("SANDBOX_TOKEN_SHA256 must be a SHA-256 hex digest")
    return token_hash


@dataclass(frozen=True, slots=True)
class Settings:
    session_root: Path
    cache_root: Path
    token_sha256: str
    session_retention_minutes: int
    max_session_bytes: int
    max_timeout_seconds: int
    default_timeout_seconds: int
    max_output_bytes: int
    max_input_bytes: int
    max_file_output_bytes: int
    max_stream_file_output_bytes: int
    max_output_files: int
    max_concurrent_jobs: int

    @classmethod
    def from_env(cls) -> "Settings":
        max_timeout = _read_int("MAX_TIMEOUT_SECONDS", 300)
        default_timeout = min(
            _read_int("DEFAULT_TIMEOUT_SECONDS", 120), max_timeout
        )
        token_hash = _resolve_token_hash()
        return cls(
            session_root=Path(
                os.getenv("SANDBOX_SESSION_ROOT", "/tmp/sandbox-sessions")
            ),
            cache_root=Path(os.getenv("SANDBOX_CACHE_ROOT", "/tmp/sandbox-cache")),
            token_sha256=token_hash,
            session_retention_minutes=_read_int(
                "SESSION_RETENTION_MINUTES", 60, 1, 1440
            ),
            max_session_bytes=_read_int(
                "MAX_SESSION_BYTES", 268_435_456, 1_048_576
            ),
            max_timeout_seconds=max_timeout,
            default_timeout_seconds=default_timeout,
            max_output_bytes=_read_int("MAX_OUTPUT_BYTES", 2_000_000, 1024),
            max_input_bytes=_read_int("MAX_INPUT_BYTES", 20_000_000, 1024),
            max_file_output_bytes=_read_int(
                "MAX_FILE_OUTPUT_BYTES", 20_000_000, 1024
            ),
            max_stream_file_output_bytes=_read_int(
                "MAX_STREAM_FILE_OUTPUT_BYTES", 64_000_000, 1024
            ),
            max_output_files=_read_int("MAX_OUTPUT_FILES", 8),
            max_concurrent_jobs=_read_int("MAX_CONCURRENT_JOBS", 2),
        )
