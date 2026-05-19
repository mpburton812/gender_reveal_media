from __future__ import annotations

import logging
import re
from difflib import SequenceMatcher
from typing import Any
import requests

from gender_reveal_media.config import Settings

logger = logging.getLogger(__name__)

# Media types handled by keyless catalog APIs (see resolve_link_via_catalog_api).
_FREE_API_TYPES: frozenset[str] = frozenset(
    {
        "books",
        "music",
        "artists",
        "publications",
        "zines",
    }
)

_TMDB_TYPES: frozenset[str] = frozenset({"movies", "tv shows"})

_TMDB_BASE = "https://api.themoviedb.org/3"

_MIN_TITLE_SCORE = 0.58
_REQUEST_TIMEOUT = 25


def _extract_year(text: str) -> int | None:
    match = re.search(r"\b(19|20)\d{2}\b", text)
    if not match:
        return None
    year = int(match.group(0))
    if 1900 <= year <= 2099:
        return year
    return None


def build_catalog_query(row: dict[str, Any]) -> str:
    """Short query for catalog APIs (no podcast-specific terms)."""
    parts: list[str] = [str(row["media_name"]).strip()]
    sub = str(row.get("media_sub_category") or "").strip()
    if sub and sub.lower() not in parts[0].lower():
        parts.append(sub)
    return " ".join(p for p in parts if p)


def _normalize_title(value: str) -> str:
    text = value.lower()
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _title_score(query: str, candidate: str) -> float:
    if not query or not candidate:
        return 0.0
    return SequenceMatcher(None, _normalize_title(query), _normalize_title(candidate)).ratio()


def _pick_best_candidate(
    query: str,
    candidates: list[tuple[str, str, str | None]],
    *,
    min_score: float = _MIN_TITLE_SCORE,
) -> str | None:
    """
    candidates: (url, title_for_scoring, optional_extra_title)
    """
    best_url: str | None = None
    best_score = min_score
    for url, title, extra in candidates:
        score = _title_score(query, title)
        if extra:
            score = max(score, _title_score(query, extra))
        if score > best_score:
            best_score = score
            best_url = url
    return best_url


def _get_json(url: str, settings: Settings, *, params: dict[str, Any] | None = None) -> Any:
    headers = {"User-Agent": settings.user_agent, "Accept": "application/json"}
    r = requests.get(url, params=params, headers=headers, timeout=_REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.json()


def resolve_open_library(query: str, settings: Settings) -> str | None:
    data = _get_json(
        "https://openlibrary.org/search.json",
        settings,
        params={"q": query, "limit": 8, "fields": "key,title,author_name"},
    )
    docs = data.get("docs") if isinstance(data, dict) else None
    if not isinstance(docs, list):
        return None
    candidates: list[tuple[str, str, str | None]] = []
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        key = str(doc.get("key") or "").strip()
        title = str(doc.get("title") or "").strip()
        if not key or not title:
            continue
        if not key.startswith("/"):
            key = "/" + key
        url = f"https://openlibrary.org{key}"
        authors = doc.get("author_name")
        author_hint = authors[0] if isinstance(authors, list) and authors else None
        candidates.append((url, title, author_hint))
    return _pick_best_candidate(query, candidates)


def _musicbrainz_search(
    entity: str,
    query: str,
    settings: Settings,
) -> list[dict[str, Any]]:
    data = _get_json(
        f"https://musicbrainz.org/ws/2/{entity}/",
        settings,
        params={"query": query, "fmt": "json", "limit": 8},
    )
    artists = data.get(f"{entity}s") if isinstance(data, dict) else None
    if not isinstance(artists, list):
        return []
    return [a for a in artists if isinstance(a, dict)]


def resolve_musicbrainz_artist(query: str, settings: Settings) -> str | None:
    items = _musicbrainz_search("artist", query, settings)
    candidates: list[tuple[str, str, str | None]] = []
    for item in items:
        mbid = str(item.get("id") or "").strip()
        name = str(item.get("name") or "").strip()
        if mbid and name:
            candidates.append((f"https://musicbrainz.org/artist/{mbid}", name, None))
    return _pick_best_candidate(query, candidates)


def resolve_musicbrainz_music(query: str, settings: Settings) -> str | None:
    for entity in ("release", "recording"):
        items = _musicbrainz_search(entity, query, settings)
        candidates: list[tuple[str, str, str | None]] = []
        for item in items:
            mbid = str(item.get("id") or "").strip()
            title = str(item.get("title") or "").strip()
            if mbid and title:
                candidates.append((f"https://musicbrainz.org/{entity}/{mbid}", title, None))
        url = _pick_best_candidate(query, candidates)
        if url:
            return url
    return None


def resolve_openalex(query: str, settings: Settings) -> str | None:
    data = _get_json(
        "https://api.openalex.org/works",
        settings,
        params={"search": query, "per_page": 8},
    )
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list):
        return None
    candidates: list[tuple[str, str, str | None]] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        title = str(item.get("display_name") or item.get("title") or "").strip()
        if not title:
            continue
        doi = str(item.get("doi") or "").strip()
        if doi.startswith("https://"):
            url = doi
        elif doi:
            url = f"https://doi.org/{doi.removeprefix('https://doi.org/')}"
        else:
            openalex_id = str(item.get("id") or "").strip()
            if not openalex_id:
                continue
            url = openalex_id if openalex_id.startswith("http") else f"https://openalex.org/{openalex_id.rsplit('/', 1)[-1]}"
        candidates.append((url, title, None))
    return _pick_best_candidate(query, candidates)


def resolve_tmdb(query: str, settings: Settings, *, media_type: str) -> str | None:
    if not settings.tmdb_api_key:
        return None
    mtype = media_type.strip().lower()
    if mtype == "movies":
        path = "movie"
        url_template = "https://www.themoviedb.org/movie/{id}"
        title_key = "title"
        alt_key = "original_title"
    elif mtype == "tv shows":
        path = "tv"
        url_template = "https://www.themoviedb.org/tv/{id}"
        title_key = "name"
        alt_key = "original_name"
    else:
        return None

    params: dict[str, Any] = {"api_key": settings.tmdb_api_key, "query": query}
    year = _extract_year(query)
    if year is not None:
        if path == "movie":
            params["year"] = year
        else:
            params["first_air_date_year"] = year

    data = _get_json(f"{_TMDB_BASE}/search/{path}", settings, params=params)
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list):
        return None

    candidates: list[tuple[str, str, str | None]] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        tmdb_id = item.get("id")
        title = str(item.get(title_key) or "").strip()
        alt = str(item.get(alt_key) or "").strip() or None
        if tmdb_id is None or not title:
            continue
        candidates.append((url_template.format(id=int(tmdb_id)), title, alt))
    return _pick_best_candidate(query, candidates)


def resolve_internet_archive(query: str, settings: Settings) -> str | None:
    # Prefer text/publications for zines; still search broadly if none match.
    q = f"title:({query})"
    data = _get_json(
        "https://archive.org/advancedsearch.php",
        settings,
        params={
            "q": q,
            "fl[]": ["identifier", "title"],
            "rows": 8,
            "output": "json",
        },
    )
    response = data.get("response") if isinstance(data, dict) else None
    docs = response.get("docs") if isinstance(response, dict) else None
    if not isinstance(docs, list):
        return None
    candidates: list[tuple[str, str, str | None]] = []
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        ident = str(doc.get("identifier") or "").strip()
        title = str(doc.get("title") or "").strip()
        if ident and title:
            candidates.append((f"https://archive.org/details/{ident}", title, None))
    return _pick_best_candidate(query, candidates)


def resolve_link_via_catalog_api(row: dict[str, Any], settings: Settings) -> str | None:
    media_type = str(row["media_type"]).strip().lower()
    if media_type not in _FREE_API_TYPES and media_type not in _TMDB_TYPES:
        return None

    query = build_catalog_query(row)
    if not query:
        return None

    try:
        if media_type in _TMDB_TYPES:
            return resolve_tmdb(query, settings, media_type=media_type)
        if media_type == "books":
            return resolve_open_library(query, settings)
        if media_type == "artists":
            return resolve_musicbrainz_artist(query, settings)
        if media_type == "music":
            return resolve_musicbrainz_music(query, settings)
        if media_type == "publications":
            return resolve_openalex(query, settings)
        if media_type == "zines":
            return resolve_internet_archive(query, settings)
    except requests.HTTPError as exc:
        logger.warning("Catalog API HTTP error for %s %r: %s", media_type, query, exc)
        raise
    except requests.RequestException as exc:
        logger.warning("Catalog API request failed for %s %r: %s", media_type, query, exc)
        raise

    return None


def resolve_link_via_free_api(row: dict[str, Any], settings: Settings) -> str | None:
    """Backward-compatible alias for resolve_link_via_catalog_api."""
    return resolve_link_via_catalog_api(row, settings)
