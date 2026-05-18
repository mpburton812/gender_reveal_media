from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlparse


def _env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    if v is None or v.strip() == "":
        return default
    return v


def _turso_https_to_libsql(url: str) -> str:
    """
    Turso often exposes an https:// database URL. libsql-client uses a different
    code path for https:// (HTTP /v1/execute) than for libsql:// (Hrana over wss).
    The HTTP responses can trigger KeyError: 'result' in older / mismatched clients.
    """
    u = url.strip()
    parsed = urlparse(u)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or "turso.io" not in host:
        return u
    netloc = parsed.netloc
    path = parsed.path or ""
    if path in ("", "/"):
        return f"libsql://{netloc}"
    return f"libsql://{netloc}{path}"


def normalize_turso_database_url(url: str) -> str:
    """
    Turso dashboard URLs often use libsql:// (Hrana over WebSocket / wss).

    - GitHub Actions and most servers: keep libsql:// so libsql-client uses wss.
      Forcing https:// can make Turso return JSON the HTTP client does not parse
      (libsql_client KeyError: 'result').

    - Streamlit Cloud: wss upgrades often fail; set TURSO_PREFER_HTTPS=1 (the
      Streamlit app does this by default) to rewrite libsql:// → https://.

    Set TURSO_USE_WEBSOCKET=1 to never rewrite (always use libsql/wss as given).
    """
    if (_env("TURSO_USE_WEBSOCKET") or "").strip().lower() in ("1", "true", "yes"):
        return url.strip()
    u = url.strip()
    if (_env("GITHUB_ACTIONS") or "").strip() == "true":
        return _turso_https_to_libsql(u)
    prefer = (_env("TURSO_PREFER_HTTPS") or "").strip().lower() in ("1", "true", "yes")
    if not prefer:
        return u
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
        gemini_model=_env("GEMINI_MODEL", "gemini-2.5-flash") or "gemini-2.5-flash",
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
