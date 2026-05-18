from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup


@dataclass(frozen=True)
class DiscoveredEpisode:
    source_episode_key: str
    transcript_source_url: str | None
    scraped_season: int | None
    scraped_list_label: str


def _slug_label(text: str) -> str:
    s = text.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s[:180] if s else "unknown"


def _basename_key(url: str) -> str:
    path = urlparse(url).path.rstrip("/")
    name = path.split("/")[-1]
    return name or path or url


def parse_listen_page(html: str, listen_url: str) -> list[DiscoveredEpisode]:
    """
    Parse the Squarespace listen page: h2 > em 'Season N' followed by ul of transcript links.
    """
    soup = BeautifulSoup(html, "lxml")
    out: list[DiscoveredEpisode] = []
    for h2 in soup.find_all("h2"):
        em = h2.find("em")
        if not em:
            continue
        label = em.get_text(strip=True)
        m = re.match(r"^\s*Season\s+(\d+)\s*$", label, re.I)
        if not m:
            continue
        season = int(m.group(1))
        ul = h2.find_next_sibling("ul")
        if not ul:
            continue
        for li in ul.find_all("li", recursive=False):
            link = li.find("a", href=True)
            if link:
                href = str(link.get("href", "")).strip()
                text = link.get_text(" ", strip=True)
                abs_url = urljoin(listen_url, href)
                key = _basename_key(abs_url)
                out.append(
                    DiscoveredEpisode(
                        source_episode_key=key,
                        transcript_source_url=abs_url,
                        scraped_season=season,
                        scraped_list_label=text,
                    )
                )
                continue
            p = li.find("p")
            raw = p.get_text(" ", strip=True) if p else li.get_text(" ", strip=True)
            if not raw:
                continue
            key = f"no-url::season-{season}::{_slug_label(raw)}"
            out.append(
                DiscoveredEpisode(
                    source_episode_key=key,
                    transcript_source_url=None,
                    scraped_season=season,
                    scraped_list_label=raw,
                )
            )
    return out
