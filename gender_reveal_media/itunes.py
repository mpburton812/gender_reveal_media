from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any

import libsql_client
import requests

from gender_reveal_media.config import Settings
from gender_reveal_media.db import insert_log

logger = logging.getLogger(__name__)

DEFAULT_PODCAST_ID = 1330522019
_ITUNES_LOOKUP = "https://itunes.apple.com/lookup"


@dataclass(frozen=True)
class EpisodeCatalogEntry:
    list_label: str
    display_name: str
    episode_number: int | None
    release_date_iso: str | None
    is_bonus: bool


@dataclass(frozen=True)
class EpisodeCatalog:
    by_label: dict[str, EpisodeCatalogEntry]
    by_display: dict[str, EpisodeCatalogEntry]


def _norm_label(text: str) -> str:
    s = text.strip()
    s = s.replace("\u2019", "'").replace("\u2018", "'")
    s = s.replace("\ufffd", "'")
    s = re.sub(r"[!?.]+$", "", s)
    return re.sub(r"\s+", " ", s.casefold())


def parse_list_label(label: str) -> tuple[int | None, str, bool]:
    """Return (episode_number, display_name, is_bonus) from a listen-page or RSS title."""
    text = label.strip()
    m = re.match(r"^Episode\s+(\d+):\s*(.+)$", text, re.I)
    if m:
        return int(m.group(1)), m.group(2).strip(), False
    m = re.match(r"^Bonus:\s*(.+)$", text, re.I)
    if m:
        return None, m.group(1).strip(), True
    return None, text, False


def _pub_date_to_iso(pub_date: str | None) -> str | None:
    if not pub_date or not pub_date.strip():
        return None
    try:
        dt = parsedate_to_datetime(pub_date.strip())
        return dt.date().isoformat()
    except (TypeError, ValueError, OverflowError):
        return None


def lookup_podcast_feed_url(
    podcast_id: int,
    *,
    user_agent: str,
    session: requests.Session | None = None,
) -> str:
    sess = session or requests.Session()
    r = sess.get(
        _ITUNES_LOOKUP,
        params={"id": podcast_id},
        headers={"User-Agent": user_agent},
        timeout=60,
    )
    r.raise_for_status()
    payload = r.json()
    for item in payload.get("results", []):
        if item.get("wrapperType") == "track" and item.get("feedUrl"):
            return str(item["feedUrl"])
    raise RuntimeError(f"No feedUrl in iTunes lookup for podcast id {podcast_id}")


def fetch_episode_catalog_from_rss(
    feed_url: str,
    *,
    user_agent: str,
    session: requests.Session | None = None,
) -> dict[str, EpisodeCatalogEntry]:
    sess = session or requests.Session()
    r = sess.get(feed_url, headers={"User-Agent": user_agent}, timeout=120)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    channel = root.find("channel")
    if channel is None:
        return {}
    out: dict[str, EpisodeCatalogEntry] = {}
    for item in channel.findall("item"):
        title_el = item.find("title")
        if title_el is None or not (title_el.text or "").strip():
            continue
        title = title_el.text.strip()
        pub_el = item.find("pubDate")
        pub = pub_el.text if pub_el is not None else None
        epnum, display, is_bonus = parse_list_label(title)
        entry = EpisodeCatalogEntry(
            list_label=title,
            display_name=display,
            episode_number=epnum,
            release_date_iso=_pub_date_to_iso(pub),
            is_bonus=is_bonus,
        )
        out[_norm_label(title)] = entry
    return out


def build_episode_catalog(settings: Settings) -> EpisodeCatalog:
    podcast_id = int(settings.itunes_podcast_id)
    session = requests.Session()
    feed_url = lookup_podcast_feed_url(
        podcast_id,
        user_agent=settings.user_agent,
        session=session,
    )
    logger.info("Loaded podcast RSS from iTunes feedUrl for id=%s", podcast_id)
    by_label = fetch_episode_catalog_from_rss(
        feed_url,
        user_agent=settings.user_agent,
        session=session,
    )
    by_display: dict[str, EpisodeCatalogEntry] = {}
    for entry in by_label.values():
        key = _norm_label(entry.display_name)
        if key and key not in by_display:
            by_display[key] = entry
    return EpisodeCatalog(by_label=by_label, by_display=by_display)


def _lookup_catalog_entry(catalog: EpisodeCatalog, list_label: str) -> EpisodeCatalogEntry | None:
    entry = catalog.by_label.get(_norm_label(list_label))
    if entry is not None:
        return entry
    _epnum, display, _bonus = parse_list_label(list_label)
    return catalog.by_display.get(_norm_label(display))


def populate_episodes_from_itunes(
    client: libsql_client.ClientSync,
    settings: Settings,
    *,
    import_run_id: int | None = None,
) -> int:
    """
    Match Turso episodes to Apple Podcasts metadata (iTunes lookup + show RSS)
    and fill episode_name, episode_date, episode_number, and season when missing.
    """
    catalog_index = build_episode_catalog(settings)
    rs = client.execute(
        """
        SELECT id, scraped_list_label, scraped_season,
               episode_name, episode_date, episode_number, season
        FROM episodes
        """
    )
    cols = rs.columns
    updated = 0
    for row in rs.rows:
        rec: dict[str, Any] = {cols[i]: row[i] for i in range(len(cols))}
        label = str(rec.get("scraped_list_label") or "").strip()
        if not label:
            continue
        entry = _lookup_catalog_entry(catalog_index, label)
        if entry is None:
            continue

        new_name = entry.display_name
        new_date = entry.release_date_iso
        new_epnum = entry.episode_number
        scraped_season = rec.get("scraped_season")
        new_season = rec.get("season")
        if new_season is None and scraped_season is not None:
            new_season = scraped_season

        cur_name = rec.get("episode_name")
        cur_date = rec.get("episode_date")
        cur_epnum = rec.get("episode_number")
        cur_season = rec.get("season")

        final_name = cur_name if cur_name else new_name
        final_date = cur_date if cur_date else new_date
        final_epnum = cur_epnum if cur_epnum is not None else new_epnum
        final_season = cur_season if cur_season is not None else new_season

        if (
            final_name == cur_name
            and final_date == cur_date
            and final_epnum == cur_epnum
            and final_season == cur_season
        ):
            continue

        client.execute(
            """
            UPDATE episodes
            SET episode_name = ?, episode_date = ?, episode_number = ?, season = ?,
                updated_at = datetime('now')
            WHERE id = ?
            """,
            [final_name, final_date, final_epnum, final_season, rec["id"]],
        )
        updated += 1
        insert_log(
            client,
            severity="INFO",
            component="itunes",
            message="Episode metadata populated from Apple Podcasts",
            episode_id=int(rec["id"]),
            import_run_id=import_run_id,
            context={
                "list_label": label,
                "episode_name": final_name,
                "episode_date": final_date,
                "episode_number": final_epnum,
                "season": final_season,
            },
        )
    return updated
