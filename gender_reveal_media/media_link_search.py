from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import urlparse

import libsql_client
import requests

from gender_reveal_media.config import Settings
from gender_reveal_media.db import insert_log, touch_episode_updated
from gender_reveal_media.media_api_resolvers import (
    build_catalog_query,
    resolve_link_via_catalog_api,
)

logger = logging.getLogger(__name__)

_CSE_URL = "https://customsearch.googleapis.com/customsearch/v1"

_BLOCKED_HOST_SUFFIXES = (
    "pinterest.com",
    "facebook.com",
    "fb.com",
    "twitter.com",
    "x.com",
    "tiktok.com",
    "reddit.com",
    "quora.com",
    "answers.yahoo.com",
)

_PREFERRED_HOSTS: dict[str, tuple[str, ...]] = {
    "movies": ("imdb.com", "themoviedb.org", "rottentomatoes.com", "letterboxd.com"),
    "tv shows": ("imdb.com", "themoviedb.org", "rottentomatoes.com"),
    "books": ("goodreads.com", "openlibrary.org", "worldcat.org", "books.google.com"),
    "graphic novels": ("goodreads.com", "comicvine.gamespot.com", "openlibrary.org"),
    "music": ("open.spotify.com", "music.apple.com", "bandcamp.com", "discogs.com"),
    "artists": ("open.spotify.com", "music.apple.com", "bandcamp.com", "instagram.com"),
    "games": ("store.steampowered.com", "igdb.com", "metacritic.com", "store.playstation.com"),
    "publications": ("wikipedia.org",),
    "zines": ("wikipedia.org",),
}


def build_search_query(
    *,
    media_name: str,
    media_type: str,
    media_sub_category: str | None,
    episode_name: str | None,
    guest: str | None,
) -> str:
    parts: list[str] = [media_name.strip()]
    sub = (media_sub_category or "").strip()
    if sub and sub.lower() not in media_name.lower():
        parts.append(sub)
    mtype = media_type.strip()
    if mtype and mtype.lower() not in media_name.lower():
        parts.append(mtype)
    parts.append("Gender Reveal podcast")
    ep = (episode_name or "").strip()
    if ep and ep.lower() not in media_name.lower():
        parts.append(ep)
    g = (guest or "").strip()
    if g and g.lower() not in media_name.lower():
        parts.append(g)
    return " ".join(p for p in parts if p)


def _host(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
    except Exception:  # noqa: BLE001
        return ""
    if host.startswith("www."):
        host = host[4:]
    return host


def _host_matches(host: str, suffix: str) -> bool:
    return host == suffix or host.endswith("." + suffix)


def _score_result(url: str, media_type: str, rank: int) -> int:
    host = _host(url)
    if not host:
        return -1000
    for blocked in _BLOCKED_HOST_SUFFIXES:
        if _host_matches(host, blocked):
            return -1000
    score = max(0, 10 - rank)
    preferred = _PREFERRED_HOSTS.get(media_type.strip().lower(), ())
    for i, suffix in enumerate(preferred):
        if _host_matches(host, suffix):
            score += 30 - i
            break
    if host.endswith(".gov") or host.endswith(".edu"):
        score += 5
    if url.startswith("https://"):
        score += 1
    return score


def pick_best_url(items: list[dict[str, Any]], media_type: str) -> str | None:
    best_url: str | None = None
    best_score = -1
    for rank, item in enumerate(items):
        link = str(item.get("link") or "").strip()
        if not link or not link.startswith(("http://", "https://")):
            continue
        score = _score_result(link, media_type, rank)
        if score > best_score:
            best_score = score
            best_url = link
    if best_score < 0:
        return None
    return best_url


def google_custom_search(
    query: str,
    settings: Settings,
    *,
    num: int = 5,
) -> list[dict[str, Any]]:
    if not settings.google_cse_api_key or not settings.google_cse_cx:
        raise RuntimeError("GOOGLE_CSE_API_KEY and GOOGLE_CSE_CX must be set")
    params = {
        "key": settings.google_cse_api_key,
        "cx": settings.google_cse_cx,
        "q": query,
        "num": max(1, min(num, 10)),
    }
    r = requests.get(_CSE_URL, params=params, timeout=30)
    r.raise_for_status()
    data = r.json()
    items = data.get("items")
    if not isinstance(items, list):
        return []
    return [it for it in items if isinstance(it, dict)]


def resolve_link_for_media(row: dict[str, Any], settings: Settings) -> str | None:
    url = resolve_link_via_catalog_api(row, settings)
    if url:
        return url
    if settings.google_cse_api_key and settings.google_cse_cx:
        query = build_search_query(
            media_name=str(row["media_name"]),
            media_type=str(row["media_type"]),
            media_sub_category=row.get("media_sub_category"),
            episode_name=row.get("episode_name"),
            guest=row.get("guest"),
        )
        items = google_custom_search(query, settings)
        return pick_best_url(items, str(row["media_type"]))
    return None


def _fetch_rows(
    client: libsql_client.ClientSync,
    *,
    refresh: bool,
    limit: int | None,
    episode_id: int | None = None,
) -> list[dict[str, Any]]:
    clauses = ["1=1"]
    args: list[Any] = []
    if episode_id is not None:
        clauses.append("mr.episode_id = ?")
        args.append(episode_id)
    if not refresh:
        clauses.append("(mr.link_to_media IS NULL OR TRIM(mr.link_to_media) = '')")
    sql = f"""
        SELECT
            mr.id AS media_id,
            mr.episode_id AS episode_id,
            mr.media_type AS media_type,
            mr.media_sub_category AS media_sub_category,
            mr.media_name AS media_name,
            mr.link_to_media AS link_to_media,
            COALESCE(e.episode_name, e.scraped_list_label) AS episode_name,
            e.guest AS guest
        FROM media_references mr
        JOIN episodes e ON e.id = mr.episode_id
        WHERE {" AND ".join(clauses)}
        ORDER BY mr.id ASC
    """
    if limit is not None and limit > 0:
        sql += " LIMIT ?"
        args.append(limit)
    rs = client.execute(sql, args)
    cols = rs.columns
    return [{cols[i]: r[i] for i in range(len(cols))} for r in rs.rows]


def populate_media_links(
    client: libsql_client.ClientSync,
    settings: Settings,
    *,
    refresh: bool = False,
    limit: int | None = None,
    episode_id: int | None = None,
    import_run_id: int | None = None,
) -> dict[str, int]:
    rows = _fetch_rows(client, refresh=refresh, limit=limit, episode_id=episode_id)
    counts = {"candidates": len(rows), "updated": 0, "skipped": 0, "errors": 0}
    if not rows:
        return counts

    for row in rows:
        media_id = int(row["media_id"])
        eid = int(row["episode_id"])
        existing = str(row.get("link_to_media") or "").strip()
        if existing and not refresh:
            counts["skipped"] += 1
            continue
        try:
            url = resolve_link_for_media(row, settings)
        except requests.HTTPError as exc:
            counts["errors"] += 1
            insert_log(
                client,
                severity="ERROR",
                component="media_link_search",
                message="Media catalog / search HTTP error",
                episode_id=eid,
                import_run_id=import_run_id,
                context={
                    "media_id": media_id,
                    "error": str(exc)[:500],
                    "catalog_query": build_catalog_query(row),
                },
            )
            logger.warning("Link resolve failed for media_id=%s: %s", media_id, exc)
            time.sleep(settings.media_link_search_sleep_sec)
            continue
        except Exception as exc:  # noqa: BLE001
            counts["errors"] += 1
            insert_log(
                client,
                severity="ERROR",
                component="media_link_search",
                message="Media link search failed",
                episode_id=eid,
                import_run_id=import_run_id,
                context={"media_id": media_id, "error": str(exc)[:500]},
            )
            logger.warning("Link search failed for media_id=%s: %s", media_id, exc)
            time.sleep(settings.media_link_search_sleep_sec)
            continue

        if not url:
            counts["skipped"] += 1
            insert_log(
                client,
                severity="INFO",
                component="media_link_search",
                message="No suitable link found",
                episode_id=eid,
                import_run_id=import_run_id,
                context={
                    "media_id": media_id,
                    "media_name": row["media_name"],
                    "media_type": row["media_type"],
                    "catalog_query": build_catalog_query(row),
                },
            )
        else:
            client.execute(
                "UPDATE media_references SET link_to_media = ? WHERE id = ?",
                [url, media_id],
            )
            touch_episode_updated(client, eid)
            counts["updated"] += 1
            insert_log(
                client,
                severity="INFO",
                component="media_link_search",
                message="Media link populated",
                episode_id=eid,
                import_run_id=import_run_id,
                context={"media_id": media_id, "media_name": row["media_name"], "link_to_media": url},
            )

        time.sleep(settings.media_link_search_sleep_sec)

    return counts
