from __future__ import annotations

import json
import logging
import re
from typing import Any

import google.generativeai as genai

from gender_reveal_media.config import Settings, resolve_gemini_model

logger = logging.getLogger(__name__)

ALLOWED_MEDIA_TYPES: frozenset[str] = frozenset(
    {
        "artists",
        "music",
        "publications",
        "movies",
        "books",
        "zines",
        "graphic novels",
        "games",
        "tv shows",
    }
)


def _configure(settings: Settings) -> None:
    genai.configure(api_key=settings.gemini_api_key)


def _chunk_transcript(text: str, chunk_size: int, overlap: int) -> list[str]:
    text = text.strip()
    if len(text) <= chunk_size:
        return [text]
    chunks: list[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(n, start + chunk_size)
        chunks.append(text[start:end])
        if end >= n:
            break
        start = max(0, end - overlap)
    return chunks


def _normalize_merge_key(media_type: str, media_name: str) -> tuple[str, str]:
    return (media_type.strip().lower(), re.sub(r"\s+", " ", media_name.strip()).lower())


def extract_episode_metadata(
    transcript: str,
    settings: Settings,
    *,
    list_label: str,
) -> dict[str, Any]:
    _configure(settings)
    model_name = resolve_gemini_model(settings.gemini_model)
    model = genai.GenerativeModel(model_name)
    cap = settings.gemini_max_transcript_chars
    body = transcript if len(transcript) <= cap else transcript[:cap]
    prompt = (
        "You extract structured episode metadata from a podcast transcript.\n"
        f"Page list label (may be partial): {list_label!r}\n"
        "Return ONLY JSON with keys: season (integer|null), episode_number (integer|null), "
        "episode_title (string), episode_date (string|null, ISO date YYYY-MM-DD if known), "
        "guest (string, empty if unknown).\n"
        "Use null only when unknown. episode_title should be the episode title without numbering prefix if clear.\n"
        "Transcript follows:\n\n"
        f"{body}"
    )
    resp = model.generate_content(
        prompt,
        generation_config=genai.GenerationConfig(
            temperature=0.1,
            response_mime_type="application/json",
        ),
    )
    raw = (resp.text or "").strip()
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("metadata JSON must be an object")
    return data


def extract_media_references(transcript: str, settings: Settings) -> list[dict[str, Any]]:
    _configure(settings)
    model_name = resolve_gemini_model(settings.gemini_model)
    model = genai.GenerativeModel(model_name)
    allowed = ", ".join(sorted(ALLOWED_MEDIA_TYPES))
    chunks = _chunk_transcript(
        transcript,
        settings.gemini_chunk_chars,
        settings.gemini_chunk_overlap,
    )
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for idx, chunk in enumerate(chunks):
        prompt = (
            "Identify media references mentioned in this podcast transcript chunk.\n"
            "Only include items whose media_type is EXACTLY one of these literals (case and spacing must match):\n"
            f"{allowed}\n"
            "Return ONLY JSON: an object with key 'references' whose value is an array of objects, each with:\n"
            "media_type (string, one of allowed literals),\n"
            "media_sub_category (string or empty string),\n"
            "media_name (string),\n"
            "link_to_media (string URL or empty string if unknown),\n"
            "context_description (one or two sentences describing how it was referenced).\n"
            "Omit any media types not in the allowed list. Omit duplicates within this chunk.\n"
            f"Chunk index: {idx + 1} / {len(chunks)}.\n\n"
            f"Transcript chunk:\n\n{chunk}"
        )
        resp = model.generate_content(
            prompt,
            generation_config=genai.GenerationConfig(
                temperature=0.1,
                response_mime_type="application/json",
            ),
        )
        raw = (resp.text or "").strip()
        payload = json.loads(raw)
        refs = payload.get("references") if isinstance(payload, dict) else None
        if not isinstance(refs, list):
            raise ValueError("media JSON must contain references array")
        for item in refs:
            if not isinstance(item, dict):
                continue
            mtype = str(item.get("media_type", "")).strip()
            if mtype not in ALLOWED_MEDIA_TYPES:
                logger.warning("Dropping reference with disallowed media_type=%r", mtype)
                continue
            name = str(item.get("media_name", "")).strip()
            if not name:
                continue
            sub = str(item.get("media_sub_category", "") or "").strip()
            link = str(item.get("link_to_media", "") or "").strip() or None
            ctx = str(item.get("context_description", "") or "").strip()
            key = _normalize_merge_key(mtype, name)
            row = {
                "media_type": mtype,
                "media_sub_category": sub or None,
                "media_name": name,
                "link_to_media": link,
                "context_description": ctx,
            }
            prev = merged.get(key)
            if prev is None or len(ctx) > len(prev.get("context_description", "")):
                merged[key] = row
    return list(merged.values())
