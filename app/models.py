from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class InputFile(BaseModel):
    path: str = Field(min_length=1, max_length=500)
    content_base64: str = Field(min_length=1)

    @field_validator("path")
    @classmethod
    def path_must_be_relative(cls, value: str) -> str:
        if "\x00" in value or value.startswith(("/", "~")):
            raise ValueError("input file path must be relative to the session")
        return value


class InputUrl(BaseModel):
    path: str = Field(min_length=1, max_length=500)
    url: str = Field(min_length=1, max_length=4000)

    @field_validator("path")
    @classmethod
    def path_must_be_relative(cls, value: str) -> str:
        if "\x00" in value or value.startswith(("/", "~")):
            raise ValueError("input URL path must be relative to the session")
        return value

    @field_validator("url")
    @classmethod
    def url_must_be_http(cls, value: str) -> str:
        if not value.startswith(("https://", "http://")):
            raise ValueError("input URL must use http or https")
        return value


class OutputFile(BaseModel):
    path: str
    mime_type: str
    size: int
    content_base64: str


class ExecRequest(BaseModel):
    command: str | list[str]
    new_session: bool = Field(
        default=False,
        description="Create a new session, optionally replacing replace_session_id.",
    )
    session_id: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$",
        description="Reuse the same ID to retain files and installed packages.",
    )
    replace_session_id: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$",
        description="Current session to delete when new_session is true.",
    )
    owner_key: str | None = Field(
        default=None,
        pattern=r"^[a-f0-9]{64}$",
        description="Opaque SHA-256 owner scope supplied by a trusted client.",
    )
    cwd: str | None = Field(
        default=None,
        description="Relative paths start inside the session; absolute paths are allowed.",
    )
    shell: bool = True
    timeout_seconds: int | None = Field(default=None, ge=1)
    stdin: str | None = Field(default=None, max_length=65_536)
    env: dict[str, str] = Field(default_factory=dict)
    python_packages: list[str] = Field(default_factory=list, max_length=64)
    node_packages: list[str] = Field(default_factory=list, max_length=64)
    input_files: list[InputFile] = Field(default_factory=list, max_length=16)
    input_urls: list[InputUrl] = Field(default_factory=list, max_length=16)
    reset_paths: list[str] = Field(
        default_factory=list,
        max_length=16,
        description="Relative session paths to remove before writing input files.",
    )
    output_files: list[str] = Field(
        default_factory=list,
        max_length=32,
        description="Glob patterns relative to the session, such as outputs/*.png.",
    )

    @field_validator("command")
    @classmethod
    def command_must_not_be_empty(cls, value: str | list[str]):
        if isinstance(value, str):
            if not value.strip():
                raise ValueError("command cannot be empty")
        elif not value or any(not item for item in value):
            raise ValueError("command cannot be empty")
        return value

    @model_validator(mode="after")
    def session_arguments_must_be_consistent(self) -> "ExecRequest":
        if self.new_session and self.session_id:
            raise ValueError("new_session=true cannot be combined with session_id")
        if self.replace_session_id and not self.new_session:
            raise ValueError("replace_session_id requires new_session=true")
        if self.replace_session_id and not self.owner_key:
            raise ValueError("replace_session_id requires owner_key")
        return self

    @field_validator("python_packages", "node_packages")
    @classmethod
    def packages_must_be_reasonable(cls, values: list[str]) -> list[str]:
        for value in values:
            if not value.strip() or len(value) > 500 or "\x00" in value:
                raise ValueError("invalid package specification")
        return values

    @field_validator("env")
    @classmethod
    def environment_must_be_reasonable(cls, values: dict[str, str]) -> dict[str, str]:
        if len(values) > 128:
            raise ValueError("too many environment variables")
        for key, value in values.items():
            if not key or "=" in key or "\x00" in key or "\x00" in value:
                raise ValueError("invalid environment variable")
        return values

    @field_validator("output_files", "reset_paths")
    @classmethod
    def output_patterns_must_be_relative(cls, values: list[str]) -> list[str]:
        for value in values:
            if (
                not value.strip()
                or len(value) > 500
                or "\x00" in value
                or value.startswith(("/", "~"))
            ):
                raise ValueError("file paths and patterns must be relative to the session")
        return values


class StepResult(BaseModel):
    name: str
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    output_truncated: bool
    duration_seconds: float


class ExecResponse(BaseModel):
    job_id: str
    session_id: str
    cwd: str
    status: Literal[
        "completed",
        "setup_failed",
        "timed_out",
        "output_limit_exceeded",
        "storage_limit",
    ]
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    output_truncated: bool
    duration_seconds: float
    setup_steps: list[StepResult] = Field(default_factory=list)
    files: list[OutputFile] = Field(default_factory=list)
    files_truncated: bool = False
    session_created: bool = False
    replaced_session_id: str | None = None
    replaced_session_removed: bool = False
    expires_at: float | None = None


class HealthResponse(BaseModel):
    status: str
    authentication: str
    auth_configured: bool
    active_jobs: int
    max_concurrent_jobs: int
    default_timeout_seconds: int
    max_timeout_seconds: int
    max_output_bytes: int
    max_input_bytes: int
    max_file_output_bytes: int
    max_stream_file_output_bytes: int
    max_output_files: int
    session_retention_minutes: int
    max_session_bytes: int
    indexed_sessions: int
    next_expiry: float | None


class CleanupResponse(BaseModel):
    session_id: str
    removed: bool
