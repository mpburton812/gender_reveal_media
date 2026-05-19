from __future__ import annotations

from gender_reveal_media.media_api_resolvers import (
    _extract_year,
    _pick_best_candidate,
    _title_score,
    build_catalog_query,
    resolve_link_via_catalog_api,
)
from gender_reveal_media.config import Settings


def _settings() -> Settings:
    return Settings(
        turso_database_url="https://example.com",
        turso_auth_token=None,
        gemini_api_key="",
        gemini_model="gemini-2.5-flash",
        ingest_max_episodes=5,
        gemini_max_transcript_chars=240000,
        gemini_chunk_chars=90000,
        gemini_chunk_overlap=2000,
        listen_url="https://example.com/listen",
        user_agent="gender-reveal-media-test/0.1 (test@example.com)",
        patreon_url=None,
        merch_url=None,
        itunes_podcast_id=1330522019,
        prompt_version="v1",
        google_cse_api_key=None,
        google_cse_cx=None,
        tmdb_api_key=None,
        media_link_search_sleep_sec=1.0,
        media_link_search_limit=None,
        populate_media_links=False,
    )


def test_build_catalog_query_includes_subcategory() -> None:
    row = {
        "media_name": "Stone Butch Blues",
        "media_sub_category": "Leslie Feinberg",
    }
    assert build_catalog_query(row) == "Stone Butch Blues Leslie Feinberg"


def test_title_score_prefers_close_match() -> None:
    assert _title_score("The Matrix", "The Matrix") > _title_score("The Matrix", "Matrix Reloaded")


def test_pick_best_candidate_threshold() -> None:
    candidates = [
        ("https://openlibrary.org/works/OL1W", "Stone Butch Blues", "Leslie Feinberg"),
        ("https://openlibrary.org/works/OL2W", "Totally Different Book", None),
    ]
    url = _pick_best_candidate("Stone Butch Blues", candidates)
    assert url == "https://openlibrary.org/works/OL1W"


def test_extract_year() -> None:
    assert _extract_year("The Matrix 1999") == 1999
    assert _extract_year("No year here") is None


def test_movies_without_tmdb_key_returns_none_without_http() -> None:
    row = {"media_name": "The Matrix", "media_type": "movies", "media_sub_category": ""}
    assert resolve_link_via_catalog_api(row, _settings()) is None


def test_unsupported_type_returns_none_without_http() -> None:
    row = {"media_name": "Elden Ring", "media_type": "games", "media_sub_category": ""}
    assert resolve_link_via_catalog_api(row, _settings()) is None
