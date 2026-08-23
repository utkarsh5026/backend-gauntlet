"""Shared configuration helpers — the Python sibling of `crates/common-config`.

Fully implemented on purpose: CLAUDE.md marks the `common-*` helpers as the one
exception to "the owner writes the interesting code". No project should have to
re-derive dotenv loading or settings plumbing — subclass `BaseConfig`, declare
typed fields, and let pydantic do the parsing and validation.

Why a settings *class* rather than the Rust helpers' `parse_or(key, default)`
free functions: in Python the type annotation is the parser. Declaring
`port: int = 8070` gets you the env lookup, the string->int coercion, the
default, and a startup error naming the bad variable, all in one line.
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["BaseConfig", "find_dotenv"]


def find_dotenv(start: Path | None = None) -> Path | None:
    """Nearest `.env` walking up from `start` (default: the caller's cwd).

    Returns `None` when there isn't one — a missing `.env` is not an error, since
    every project's config is expected to have working defaults.
    """
    current = (start or Path.cwd()).resolve()
    for directory in (current, *current.parents):
        candidate = directory / ".env"
        if candidate.is_file():
            return candidate
    return None


class BaseConfig(BaseSettings):
    """Base for a project's typed settings.

    Reads from the process environment first, then the nearest `.env`, so an
    explicit `PORT=... make run` always beats the file. Unknown variables are
    ignored rather than fatal: the same `.env` is shared with docker-compose,
    which sets keys this process doesn't care about.
    """

    model_config = SettingsConfigDict(
        env_file=find_dotenv(),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )
