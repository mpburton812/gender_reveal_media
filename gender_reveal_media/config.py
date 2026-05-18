from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    if v is None or v.strip() == "":
        return default
    return v


def normalize_turso_database_url(url: str) -> str:
    """
    Turso dashboard URLs often use libsql://, which the client maps to wss://.
    Some hosts (e.g. Streamlit Cloud) fail WebSocket upgrades; HTTPS works reliably.
    Set TURSO_USE_WEBSOCKET=1 to keep libsql/wss as provided.
    """
    flag = (_env("TURSO_USE_WEBSOCKET") or "").strip().lower()
    if flag in ("1", "true", "yes"):
        return url.strip()
    u = url.strip()
    if u.startswith("libsql://"):
        return "https://" + u.removeprefix("libsql://")
    if u.startswith("wss://"):
        return "https://" + u.removeprefix("wss://")
    return u


@dataclass(frozen=True)
class Settings:
    turso_database_url: str
    turso_auth_token: str | None
    gemini_api_key: str
    gemini_model: str
    ingest_max_episodes: int
    gemini_max_transcript_chars: int
    gemini_chunk_chars: int
    gemini_chunk_overlap: int
    listen_url: str
    user_agent: str
    patreon_url: str | None
    merch_url: str | None
    prompt_version: str


def load_settings(*, require_gemini: bool = True) -> Settings:
    url = _env("TURSO_DATABASE_URL")
    if not url:
        raise RuntimeError("TURSO_DATABASE_URL is not set")
    gemini = _env("GEMINI_API_KEY")
    if require_gemini and not gemini:
        raise RuntimeError("GEMINI_API_KEY is not set")
    if not gemini:
        gemini = ""
    max_eps = int(_env("INGEST_MAX_EPISODES", "5") or "5")
    max_chars = int(_env("GEMINI_MAX_TRANSCRIPT_CHARS", "240000") or "240000")
    chunk = int(_env("GEMINI_CHUNK_CHARS", "90000") or "90000")
    overlap = int(_env("GEMINI_CHUNK_OVERLAP", "2000") or "2000")
    return Settings(
        turso_database_url=normalize_turso_database_url(url),
        turso_auth_token=_env("TURSO_AUTH_TOKEN"),
        gemini_api_key=gemini,
        gemini_model=_env("GEMINI_MODEL", "gemini-2.0-flash") or "gemini-2.0-flash",
        ingest_max_episodes=max(1, min(max_eps, 50)),
        gemini_max_transcript_chars=max(10_000, max_chars),
        gemini_chunk_chars=max(5000, chunk),
        gemini_chunk_overlap=max(0, min(overlap, chunk // 2)),
        listen_url=_env("LISTEN_PAGE_URL", "https://www.genderpodcast.com/listen")
        or "https://www.genderpodcast.com/listen",
        user_agent=_env(
            "HTTP_USER_AGENT",
            "gender-reveal-media-ingest/0.1 (+https://github.com/mpburton812/gender_reveal_media)",
        )
        or "gender-reveal-media-ingest/0.1",
        patreon_url=_env("PATREON_URL"),
        merch_url=_env("MERCH_URL"),
        prompt_version=_env("PROMPT_VERSION", "v1") or "v1",
    )
