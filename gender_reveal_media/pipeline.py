from __future__ import annotations

import logging
import os
from typing import Any

import libsql_client
import requests
from libsql_client import LibsqlError

from gender_reveal_media.config import Settings
from gender_reveal_media.db import apply_schema, insert_log, touch_episode_updated
from gender_reveal_media.discovery import DiscoveredEpisode, parse_listen_page
from gender_reveal_media import gemini_extract
from gender_reveal_media.itunes import populate_episodes_from_itunes
from gender_reveal_media.media_link_search import populate_media_links
from gender_reveal_media.transcript_extraction import download_transcript_text

logger = logging.getLogger(__name__)


def _safe_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        try:
            return int(float(str(value)))
        except (TypeError, ValueError):
            return None


def _fetch_episode_row(client: libsql_client.ClientSync, episode_id: int) -> dict[str, Any] | None:
    rs = client.execute(
        """
        SELECT e.id, e.source_episode_key, e.transcript_source_url, e.scraped_season, e.scraped_list_label,
               e.transcript_text, e.transcript_sha256, e.season, e.episode_number, e.episode_name,
               e.episode_date, e.guest,
               s.stage, s.last_error_code, s.last_error_message
        FROM episodes e
        JOIN episode_processing_state s ON s.episode_id = e.id
        WHERE e.id = ?
        """,
        [episode_id],
    )
    if len(rs.rows) == 0:
        return None
    r = rs.rows[0]
    cols = rs.columns
    return {cols[i]: r[i] for i in range(len(cols))}


def _set_stage(
    client: libsql_client.ClientSync,
    episode_id: int,
    stage: str,
    *,
    err_code: str | None = None,
    err_msg: str | None = None,
) -> None:
    client.execute(
        """
        UPDATE episode_processing_state
        SET stage = ?, last_error_code = ?, last_error_message = ?, updated_at = datetime('now')
        WHERE episode_id = ?
        """,
        [stage, err_code, err_msg, episode_id],
    )


def _normalize_stage(row: dict[str, Any]) -> str:
    stage = str(row["stage"])
    if stage != "failed":
        return stage
    code = row.get("last_error_code") or ""
    if code == "ERR_DOWNLOAD":
        return "discovered"
    if code == "ERR_MEDIA":
        return "metadata_extracted"
    return "transcript_downloaded"


def upsert_discovery(
    client: libsql_client.ClientSync,
    items: list[DiscoveredEpisode],
    import_run_id: int | None,
) -> int:
    discovered = 0
    for it in items:
        rs = client.execute(
            "SELECT id FROM episodes WHERE source_episode_key = ?",
            [it.source_episode_key],
        )
        if len(rs.rows) > 0:
            eid = int(rs.rows[0][0])
            client.execute(
                """
                UPDATE episodes
                SET scraped_season = ?, scraped_list_label = ?, transcript_source_url = ?,
                    updated_at = datetime('now')
                WHERE id = ?
                """,
                [it.scraped_season, it.scraped_list_label, it.transcript_source_url, eid],
            )
            continue
        stage0 = "transcript_missing" if not it.transcript_source_url else "discovered"
        client.execute(
            """
            INSERT INTO episodes (
                source_episode_key, listen_page_url, transcript_source_url,
                scraped_season, scraped_list_label
            ) VALUES (?, ?, ?, ?, ?)
            """,
            [
                it.source_episode_key,
                "https://www.genderpodcast.com/listen",
                it.transcript_source_url,
                it.scraped_season,
                it.scraped_list_label,
            ],
        )
        ins = client.execute("SELECT last_insert_rowid() AS id")
        new_id = int(ins.rows[0][0])
        client.execute(
            "INSERT INTO episode_processing_state (episode_id, stage) VALUES (?, ?)",
            [new_id, stage0],
        )
        discovered += 1
        insert_log(
            client,
            severity="INFO",
            component="discovery",
            message="New episode discovered",
            episode_id=new_id,
            import_run_id=import_run_id,
            context={"source_episode_key": it.source_episode_key, "stage": stage0},
        )
    return discovered


class ProcessResult:
    __slots__ = ("outcome", "downloaded", "metadata", "media_completed", "media_links_updated")

    def __init__(
        self,
        outcome: str,
        *,
        downloaded: bool = False,
        metadata: bool = False,
        media_completed: bool = False,
        media_links_updated: int = 0,
    ) -> None:
        self.outcome = outcome
        self.downloaded = downloaded
        self.metadata = metadata
        self.media_completed = media_completed
        self.media_links_updated = media_links_updated


def process_episode(
    client: libsql_client.ClientSync,
    episode_id: int,
    settings: Settings,
    import_run_id: int | None,
) -> ProcessResult:
    row = _fetch_episode_row(client, episode_id)
    if not row:
        return ProcessResult("skipped")
    stage = _normalize_stage(row)
    if stage == "transcript_missing":
        insert_log(
            client,
            severity="INFO",
            component="pipeline",
            message="Episode has no transcript file URL on listen page",
            episode_id=episode_id,
            import_run_id=import_run_id,
            context={"source_episode_key": row["source_episode_key"]},
        )
        return ProcessResult("skipped")

    if stage == "media_extracted":
        return ProcessResult("completed", media_completed=True)

    downloaded_flag = False
    metadata_flag = False

    try:
        url = row.get("transcript_source_url")
        transcript = str(row.get("transcript_text") or "")

        if stage == "discovered" and url:
            try:
                text, digest = download_transcript_text(
                    str(url),
                    user_agent=settings.user_agent,
                )
            except Exception as exc:  # noqa: BLE001
                _set_stage(client, episode_id, "failed", err_code="ERR_DOWNLOAD", err_msg=str(exc)[:2000])
                insert_log(
                    client,
                    severity="ERROR",
                    component="transcript",
                    message="Transcript download failed",
                    episode_id=episode_id,
                    import_run_id=import_run_id,
                    context={"error": str(exc)},
                )
                return ProcessResult("failed")

            client.execute(
                """
                UPDATE episodes
                SET transcript_text = ?, transcript_sha256 = ?, updated_at = datetime('now')
                WHERE id = ?
                """,
                [text, digest, episode_id],
            )
            _set_stage(client, episode_id, "transcript_downloaded", err_code=None, err_msg=None)
            transcript = text
            downloaded_flag = True
            insert_log(
                client,
                severity="INFO",
                component="transcript",
                message="Transcript downloaded and extracted",
                episode_id=episode_id,
                import_run_id=import_run_id,
                context={"sha256": digest},
            )
            stage = "transcript_downloaded"

        if stage == "discovered" and not url:
            _set_stage(client, episode_id, "transcript_missing")
            return ProcessResult("skipped")

        if stage in ("transcript_downloaded",):
            row2 = _fetch_episode_row(client, episode_id)
            transcript = str(row2.get("transcript_text") or "") if row2 else transcript
            if not transcript.strip():
                raise RuntimeError("MISSING_TRANSCRIPT_TEXT")
            try:
                meta = gemini_extract.extract_episode_metadata(
                    transcript,
                    settings,
                    list_label=str(row.get("scraped_list_label") or ""),
                )
            except Exception as exc:  # noqa: BLE001
                _set_stage(client, episode_id, "failed", err_code="ERR_METADATA", err_msg=str(exc)[:2000])
                insert_log(
                    client,
                    severity="ERROR",
                    component="gemini_metadata",
                    message="Metadata extraction failed",
                    episode_id=episode_id,
                    import_run_id=import_run_id,
                    context={"error": str(exc)},
                )
                return ProcessResult("failed")

            base = row2 if row2 else row
            season = _safe_int(meta.get("season")) or base.get("season") or base.get("scraped_season")
            epnum = _safe_int(meta.get("episode_number")) or base.get("episode_number")
            title = meta.get("episode_title")
            epdate = meta.get("episode_date")
            guest = meta.get("guest")
            episode_name = (
                str(title).strip()
                if title is not None and str(title).strip()
                else base.get("episode_name")
            )
            episode_date = (
                str(epdate).strip()
                if epdate is not None and str(epdate).strip()
                else base.get("episode_date")
            )
            guest_val = (
                str(guest).strip() if guest is not None and str(guest).strip() else base.get("guest")
            )
            client.execute(
                """
                UPDATE episodes
                SET season = ?, episode_number = ?, episode_name = ?, episode_date = ?, guest = ?,
                    updated_at = datetime('now')
                WHERE id = ?
                """,
                [
                    season,
                    epnum,
                    episode_name,
                    episode_date,
                    guest_val,
                    episode_id,
                ],
            )
            _set_stage(client, episode_id, "metadata_extracted", err_code=None, err_msg=None)
            metadata_flag = True
            insert_log(
                client,
                severity="INFO",
                component="gemini_metadata",
                message="Episode metadata extracted",
                episode_id=episode_id,
                import_run_id=import_run_id,
            )
            stage = "metadata_extracted"

        if stage == "metadata_extracted":
            row3 = _fetch_episode_row(client, episode_id)
            transcript = str(row3.get("transcript_text") or "") if row3 else transcript
            if not transcript.strip():
                raise RuntimeError("MISSING_TRANSCRIPT_TEXT")
            try:
                refs = gemini_extract.extract_media_references(transcript, settings)
            except Exception as exc:  # noqa: BLE001
                _set_stage(client, episode_id, "failed", err_code="ERR_MEDIA", err_msg=str(exc)[:2000])
                insert_log(
                    client,
                    severity="ERROR",
                    component="gemini_media",
                    message="Media extraction failed",
                    episode_id=episode_id,
                    import_run_id=import_run_id,
                    context={"error": str(exc)},
                )
                return ProcessResult("failed")

            client.execute("DELETE FROM media_references WHERE episode_id = ?", [episode_id])
            for ref in refs:
                client.execute(
                    """
                    INSERT INTO media_references (
                        episode_id, media_type, media_sub_category, media_name, link_to_media,
                        context_description, model_name, prompt_version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        episode_id,
                        ref["media_type"],
                        ref.get("media_sub_category"),
                        ref["media_name"],
                        ref.get("link_to_media"),
                        ref.get("context_description") or "",
                        settings.gemini_model,
                        settings.prompt_version,
                    ],
                )
            _set_stage(client, episode_id, "media_extracted", err_code=None, err_msg=None)
            touch_episode_updated(client, episode_id)
            insert_log(
                client,
                severity="INFO",
                component="gemini_media",
                message="Media references stored",
                episode_id=episode_id,
                import_run_id=import_run_id,
                context={"count": len(refs)},
            )
            link_counts = {"updated": 0}
            if settings.populate_media_links:
                link_counts = populate_media_links(
                    client,
                    settings,
                    episode_id=episode_id,
                    import_run_id=import_run_id,
                    limit=settings.media_link_search_limit,
                )
            return ProcessResult(
                "completed",
                downloaded=downloaded_flag,
                metadata=metadata_flag,
                media_completed=True,
                media_links_updated=int(link_counts.get("updated", 0)),
            )

        return ProcessResult("progressed", downloaded=downloaded_flag, metadata=metadata_flag)

    except Exception as exc:  # noqa: BLE001
        _set_stage(client, episode_id, "failed", err_code="ERR_METADATA", err_msg=str(exc)[:2000])
        insert_log(
            client,
            severity="ERROR",
            component="pipeline",
            message="Episode processing failed",
            episode_id=episode_id,
            import_run_id=import_run_id,
            context={"error": str(exc)},
        )
        logger.exception("Episode %s failed", episode_id)
        return ProcessResult("failed")


def _eligible_episode_ids(client: libsql_client.ClientSync, limit: int) -> list[int]:
    rs = client.execute(
        """
        SELECT e.id
        FROM episodes e
        JOIN episode_processing_state s ON s.episode_id = e.id
        WHERE s.stage IN ('discovered', 'transcript_downloaded', 'metadata_extracted', 'failed')
        ORDER BY e.id ASC
        LIMIT ?
        """,
        [limit],
    )
    return [int(r[0]) for r in rs.rows]


def run_populate_media_links(
    settings: Settings,
    *,
    refresh: bool = False,
    limit: int | None = None,
) -> dict[str, int]:
    client = libsql_client.create_client_sync(
        settings.turso_database_url,
        auth_token=settings.turso_auth_token,
    )
    try:
        try:
            client.execute("PRAGMA foreign_keys = ON;")
        except LibsqlError:
            pass
        apply_schema(client)
        effective_limit = limit if limit is not None else settings.media_link_search_limit
        return populate_media_links(
            client,
            settings,
            refresh=refresh,
            limit=effective_limit,
            import_run_id=None,
        )
    finally:
        client.close()


def run_ingest(settings: Settings, *, trigger: str = "cli") -> dict[str, int]:
    client = libsql_client.create_client_sync(
        settings.turso_database_url,
        auth_token=settings.turso_auth_token,
    )
    try:
        try:
            client.execute("PRAGMA foreign_keys = ON;")
        except LibsqlError:
            pass
        apply_schema(client)
        client.execute(
            "INSERT INTO import_runs (trigger, status) VALUES (?, 'running')",
            [trigger],
        )
        rid_rs = client.execute("SELECT last_insert_rowid() AS id")
        import_run_id = int(rid_rs.rows[0][0])
        insert_log(
            client,
            severity="INFO",
            component="pipeline",
            message="Ingest started",
            import_run_id=import_run_id,
            context={
                "gemini_model": settings.gemini_model,
                "gemini_model_env": os.environ.get("GEMINI_MODEL"),
                "github_actions": os.environ.get("GITHUB_ACTIONS"),
            },
        )

        headers = {"User-Agent": settings.user_agent}
        r = requests.get(settings.listen_url, headers=headers, timeout=60)
        r.raise_for_status()
        items = parse_listen_page(r.text, settings.listen_url)
        discovered = upsert_discovery(client, items, import_run_id)
        itunes_populated = populate_episodes_from_itunes(
            client, settings, import_run_id=import_run_id
        )

        counts = {
            "episodes_discovered": discovered,
            "itunes_populated": itunes_populated,
            "transcripts_new": 0,
            "metadata_ok": 0,
            "media_ok": 0,
            "media_links_updated": 0,
            "errors": 0,
        }
        for eid in _eligible_episode_ids(client, settings.ingest_max_episodes):
            res = process_episode(client, eid, settings, import_run_id)
            if res.outcome == "failed":
                counts["errors"] += 1
            if res.downloaded:
                counts["transcripts_new"] += 1
            if res.metadata:
                counts["metadata_ok"] += 1
            if res.media_completed:
                counts["media_ok"] += 1
            if res.media_links_updated:
                counts["media_links_updated"] += res.media_links_updated

        client.execute(
            """
            UPDATE import_runs
            SET finished_at = datetime('now'),
                status = ?,
                episodes_discovered = ?,
                transcripts_new = ?,
                metadata_ok = ?,
                media_ok = ?,
                errors = ?
            WHERE id = ?
            """,
            [
                "success" if counts["errors"] == 0 else "partial",
                discovered,
                counts["transcripts_new"],
                counts["metadata_ok"],
                counts["media_ok"],
                counts["errors"],
                import_run_id,
            ],
        )
        insert_log(
            client,
            severity="INFO",
            component="pipeline",
            message="Import run finished",
            import_run_id=import_run_id,
            context=counts,
        )
        return counts
    finally:
        client.close()
