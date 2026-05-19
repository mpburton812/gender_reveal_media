from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    if v is None or v.strip() == "":
        return default
    return v


def _apply_secrets_toml(data: dict[str, Any], *, prefix: str = "") -> None:
    """Populate os.environ from secrets.toml for keys not already set in the shell."""
    for key, value in data.items():
        if not isinstance(key, str):
            continue
        env_name = f"{prefix}{key}".upper() if not prefix else f"{prefix}_{key}".upper()
        if isinstance(value, dict):
            _apply_secrets_toml(value, prefix=f"{prefix}{key}_" if prefix else f"{key}_")
            continue
        if isinstance(value, str) and value.strip():
            if not (_env(env_name) or "").strip():
                os.environ[env_name] = value.strip()


def _load_local_secrets_toml() -> None:
    """Load `.streamlit/secrets.toml` for CLI runs (Streamlit loads this automatically)."""
    path = Path(".streamlit") / "secrets.toml"
    if not path.is_file():
        return
    try:
        import tomllib
    except ModuleNotFoundError:
        return
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, ValueError):
        return
    if isinstance(data, dict):
        _apply_secrets_toml(data)


def resolve_gemini_model(value: str | None) -> str:
    """
    Default to a currently provisioned Flash model. Older configs often pin
    gemini-2.0-flash, which returns 404 for new API projects.
    """
    default = "gemini-2.5-flash"
    raw = (value or "").strip().strip('"').strip("'")
    raw = raw.replace("\ufeff", "")
    if not raw:
        return default
    name = raw.removeprefix("models/").strip().strip('"').strip("'")
    tail = name.split("/")[-1].lower()
    if tail == "gemini-2.0-flash" or tail.startswith("gemini-2.0-flash-"):
        return default
    if "gemini-2.0-flash" in name.lower() and "gemini-2.5" not in name.lower():
        return default
    return name


def _turso_coerce_https(url: str) -> str:
    """Use libsql-client HTTP transport (https://) for *.turso.io; avoids flaky wss handshakes."""
    u = url.strip()
    if "turso.io" not in u.lower():
        return u
    if u.startswith("libsql://"):
        return "https://" + u.removeprefix("libsql://")
    if u.startswith("wss://"):
        return "https://" + u.removeprefix("wss://")
    if u.startswith("https://"):
        return u
    return u


def normalize_turso_database_url(url: str) -> str:
    """
    Turso dashboard URLs are often libsql:// (client would otherwise use wss://).

    - GitHub Actions: coerce *.turso.io to https:// (wss often fails with 505 on runners).
    - Streamlit Cloud: same when TURSO_PREFER_HTTPS=1 (set by default in streamlit_app).

    Set TURSO_USE_WEBSOCKET=1 to keep libsql/wss as provided (no rewrite).
  By default *.turso.io URLs use https:// (reliable for CLI, Actions, Streamlit).
    """
    if (_env("TURSO_USE_WEBSOCKET") or "").strip().lower() in ("1", "true", "yes"):
        return url.strip()
    u = url.strip()
    if "turso.io" in u.lower():
        return _turso_coerce_https(u)
    if (_env("GITHUB_ACTIONS") or "").strip() == "true":
        return _turso_coerce_https(u)
    prefer = (_env("TURSO_PREFER_HTTPS") or "").strip().lower() in ("1", "true", "yes")
    if prefer:
        return _turso_coerce_https(u)
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
    itunes_podcast_id: int
    prompt_version: str
    google_cse_api_key: str | None
    google_cse_cx: str | None
    tmdb_api_key: str | None
    media_link_search_sleep_sec: float
    media_link_search_limit: int | None
    populate_media_links: bool


def load_settings(
    *,
    require_gemini: bool = True,
    require_google_cse: bool = False,
    populate_media_links: bool = False,
) -> Settings:
    _load_local_secrets_toml()
    url = _env("TURSO_DATABASE_URL")
    if not url:
        raise RuntimeError(
            "TURSO_DATABASE_URL is not set. Set it in the environment or in .streamlit/secrets.toml"
        )
    gemini = _env("GEMINI_API_KEY")
    if require_gemini and not gemini:
        raise RuntimeError("GEMINI_API_KEY is not set")
    if not gemini:
        gemini = ""
    cse_key = _env("GOOGLE_CSE_API_KEY") or _env("GOOGLE_API_KEY")
    cse_cx = _env("GOOGLE_CSE_CX") or _env("GOOGLE_CX")
    tmdb_key = _env("TMDB_API_KEY") or _env("THEMOVIEDB_API_KEY")
    if require_google_cse and (not cse_key or not cse_cx):
        raise RuntimeError("GOOGLE_CSE_API_KEY (or GOOGLE_API_KEY) and GOOGLE_CSE_CX (or GOOGLE_CX) must be set")
    sleep_raw = _env("MEDIA_LINK_SEARCH_SLEEP_SEC", "1.0") or "1.0"
    try:
        sleep_sec = max(0.0, float(sleep_raw))
    except ValueError:
        sleep_sec = 1.0
    limit_raw = _env("MEDIA_LINK_SEARCH_LIMIT")
    link_limit: int | None = None
    if limit_raw is not None and limit_raw.strip() != "":
        try:
            parsed = int(limit_raw)
            if parsed > 0:
                link_limit = parsed
        except ValueError:
            pass
    max_eps_raw = int(_env("INGEST_MAX_EPISODES", "5") or "5")
    cap = int(_env("INGEST_EPISODES_CAP", "5000") or "5000")
    cap = max(1, min(cap, 50_000))
    if max_eps_raw <= 0:
        max_eps = cap
    else:
        max_eps = max(1, min(max_eps_raw, cap))
    max_chars = int(_env("GEMINI_MAX_TRANSCRIPT_CHARS", "240000") or "240000")
    chunk = int(_env("GEMINI_CHUNK_CHARS", "90000") or "90000")
    overlap = int(_env("GEMINI_CHUNK_OVERLAP", "2000") or "2000")
    return Settings(
        turso_database_url=normalize_turso_database_url(url),
        turso_auth_token=_env("TURSO_AUTH_TOKEN"),
        gemini_api_key=gemini,
        gemini_model=resolve_gemini_model(_env("GEMINI_MODEL")),
        ingest_max_episodes=max_eps,
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
        itunes_podcast_id=int(_env("ITUNES_PODCAST_ID", "1330522019") or "1330522019"),
        prompt_version=_env("PROMPT_VERSION", "v1") or "v1",
        google_cse_api_key=cse_key,
        google_cse_cx=cse_cx,
        tmdb_api_key=tmdb_key,
        media_link_search_sleep_sec=sleep_sec,
        media_link_search_limit=link_limit,
        populate_media_links=populate_media_links,
    )
