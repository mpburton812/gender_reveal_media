from __future__ import annotations

import logging
from typing import Any

import libsql_client
import requests
from libsql_client import LibsqlError

from gender_reveal_media.config import Settings
from gender_reveal_media.db import apply_schema, insert_log, touch_episode_updated
from gender_reveal_media.discovery import DiscoveredEpisode, parse_listen_page
from gender_reveal_media import gemini_extract
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
    __slots__ = ("outcome", "downloaded", "metadata", "media_completed")

    def __init__(
        self,
        outcome: str,
        *,
        downloaded: bool = False,
        metadata: bool = False,
        media_completed: bool = False,
    ) -> None:
        self.outcome = outcome
        self.downloaded = downloaded
        self.metadata = metadata
        self.media_completed = media_completed


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

            season = meta.get("season")
            epnum = meta.get("episode_number")
            title = meta.get("episode_title")
            epdate = meta.get("episode_date")
            guest = meta.get("guest")
            client.execute(
                """
                UPDATE episodes
                SET season = ?, episode_number = ?, episode_name = ?, episode_date = ?, guest = ?,
                    updated_at = datetime('now')
                WHERE id = ?
                """,
                [
                    _safe_int(season),
                    _safe_int(epnum),
                    str(title) if title is not None else None,
                    str(epdate) if epdate is not None else None,
                    str(guest) if guest is not None else None,
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
            return ProcessResult(
                "completed",
                downloaded=downloaded_flag,
                metadata=metadata_flag,
                media_completed=True,
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

        headers = {"User-Agent": settings.user_agent}
        r = requests.get(settings.listen_url, headers=headers, timeout=60)
        r.raise_for_status()
        items = parse_listen_page(r.text, settings.listen_url)
        discovered = upsert_discovery(client, items, import_run_id)

        counts = {
            "episodes_discovered": discovered,
            "transcripts_new": 0,
            "metadata_ok": 0,
            "media_ok": 0,
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
