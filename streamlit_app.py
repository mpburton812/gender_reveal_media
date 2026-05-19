from __future__ import annotations

import os
from pathlib import Path

import libsql_client
import pandas as pd
import streamlit as st
from libsql_client import LibsqlError

from gender_reveal_media.branding import inject_brand_styles, render_site_footer, render_site_header
from gender_reveal_media.config import load_settings
from gender_reveal_media.db import apply_schema


def _hydrate_env_from_streamlit_secrets() -> None:
    try:
        sec = st.secrets
        for key in (
            "GEMINI_API_KEY",
            "TURSO_DATABASE_URL",
            "TURSO_AUTH_TOKEN",
            "PATREON_URL",
            "MERCH_URL",
            "GEMINI_MODEL",
            "INGEST_MAX_EPISODES",
        ):
            if key in sec and str(sec[key]).strip():
                os.environ[key] = str(sec[key])
    except FileNotFoundError:
        return


@st.cache_resource
def _db_client(url: str, token: str | None):
    return libsql_client.create_client_sync(url, auth_token=token)


def _ensure_schema(client: libsql_client.ClientSync) -> None:
    schema_path = Path(__file__).resolve().parent / "schema.sql"
    apply_schema(client, schema_path=schema_path)


def _rows_to_df(rs: libsql_client.ResultSet, *, empty_columns: list[str]) -> pd.DataFrame:
    cols = list(rs.columns)
    rows = [dict(zip(cols, tuple(row))) for row in rs.rows]
    if not rows:
        return pd.DataFrame(columns=empty_columns)
    return pd.DataFrame(rows)


def _df_media_table(client: libsql_client.ClientSync) -> pd.DataFrame:
    rs = client.execute(
        """
        SELECT
            COALESCE(e.season, e.scraped_season) AS season,
            e.episode_number AS episode_num,
            COALESCE(e.episode_name, e.scraped_list_label) AS episode_name,
            e.episode_date AS episode_date,
            e.guest AS guest,
            m.media_type AS media_type,
            m.media_sub_category AS media_sub_category,
            m.media_name AS media_name,
            m.link_to_media AS link_to_media,
            m.context_description AS context_description,
            e.source_episode_key AS source_episode_key
        FROM media_references m
        JOIN episodes e ON e.id = m.episode_id
        ORDER BY e.id DESC, m.id ASC
        """
    )
    return _rows_to_df(
        rs,
        empty_columns=[
            "season",
            "episode_num",
            "episode_name",
            "episode_date",
            "guest",
            "media_type",
            "media_sub_category",
            "media_name",
            "link_to_media",
            "context_description",
            "source_episode_key",
        ],
    )


def _df_stage_counts(client: libsql_client.ClientSync) -> pd.DataFrame:
    rs = client.execute(
        """
        SELECT s.stage AS stage, COUNT(*) AS count
        FROM episode_processing_state s
        GROUP BY s.stage
        ORDER BY s.stage
        """
    )
    return _rows_to_df(rs, empty_columns=["stage", "count"])


def _df_import_runs(client: libsql_client.ClientSync, limit: int = 30) -> pd.DataFrame:
    rs = client.execute(
        """
        SELECT id, started_at, finished_at, trigger, status,
               episodes_discovered, transcripts_new, metadata_ok, media_ok, errors
        FROM import_runs
        ORDER BY id DESC
        LIMIT ?
        """,
        [limit],
    )
    return _rows_to_df(
        rs,
        empty_columns=[
            "id",
            "started_at",
            "finished_at",
            "trigger",
            "status",
            "episodes_discovered",
            "transcripts_new",
            "metadata_ok",
            "media_ok",
            "errors",
        ],
    )


def _df_logs(client: libsql_client.ClientSync, limit: int, severity: str | None) -> pd.DataFrame:
    if severity:
        rs = client.execute(
            """
            SELECT id, created_at, severity, component, episode_id, import_run_id, message, context_json
            FROM log_events
            WHERE severity = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            [severity, limit],
        )
    else:
        rs = client.execute(
            """
            SELECT id, created_at, severity, component, episode_id, import_run_id, message, context_json
            FROM log_events
            ORDER BY id DESC
            LIMIT ?
            """,
            [limit],
        )
    return _rows_to_df(
        rs,
        empty_columns=[
            "id",
            "created_at",
            "severity",
            "component",
            "episode_id",
            "import_run_id",
            "message",
            "context_json",
        ],
    )


def main() -> None:
    st.set_page_config(
        page_title="Gender Reveal — Media catalog",
        page_icon="https://www.genderpodcast.com/favicon.ico",
        layout="wide",
    )
    inject_brand_styles()
    _hydrate_env_from_streamlit_secrets()
    # Turso over wss often fails on Streamlit Cloud; HTTPS transport works there.
    # GitHub Actions does not set this and keeps libsql:// → wss (see config.normalize).
    os.environ.setdefault("TURSO_PREFER_HTTPS", "1")

    render_site_header(page_title="Media catalog")

    try:
        settings = load_settings(require_gemini=False)
    except Exception as exc:  # noqa: BLE001
        st.error(f"Configuration error: {exc}")
        st.stop()

    client = _db_client(settings.turso_database_url, settings.turso_auth_token)
    try:
        try:
            client.execute("PRAGMA foreign_keys = ON;")
        except LibsqlError:
            pass
        _ensure_schema(client)
    except Exception as exc:  # noqa: BLE001
        st.error(f"Database connection or schema error: {exc}")
        st.stop()

    search = st.sidebar.text_input(
        "Search all fields",
        value="",
        help="Case-insensitive filter across the table.",
        placeholder="Type to filter the catalog…",
    )

    tab_catalog, tab_progress, tab_logs = st.tabs(["Media catalog", "Import progress", "Logs"])

    with tab_catalog:
        df = _df_media_table(client)
        if search.strip():
            q = search.strip().casefold()

            def row_matches(row: pd.Series) -> bool:
                parts = [row.get(c) for c in row.index]
                blob = " ".join("" if p is None else str(p) for p in parts).casefold()
                return q in blob

            df = df[df.apply(row_matches, axis=1)]

        st.caption("Rows appear after the ingestion pipeline stores media references in Turso.")
        st.dataframe(df, width="stretch", hide_index=True)

    with tab_progress:
        c1, c2, c3, c4 = st.columns(4)
        stages = _df_stage_counts(client)

        def _sum_stage(df: pd.DataFrame, name: str) -> int:
            if df.empty or "stage" not in df.columns or "count" not in df.columns:
                return 0
            sub = df[df["stage"] == name]
            if sub.empty:
                return 0
            return int(sub["count"].sum())

        total_eps = (
            int(stages["count"].sum())
            if (not stages.empty and "count" in stages.columns)
            else 0
        )
        done = _sum_stage(stages, "media_extracted")
        failed = _sum_stage(stages, "failed")
        missing = _sum_stage(stages, "transcript_missing")
        c1.metric("Episodes tracked", total_eps)
        c2.metric("Media extraction complete", done)
        c3.metric("Failed", failed)
        c4.metric("Transcript missing (no file URL)", missing)

        if stages.empty:
            st.info("No processing state yet. Run the GitHub Action or CLI ingest once.")
        else:
            st.subheader("Episodes by pipeline stage")
            chart_df = stages.set_index("stage")[["count"]].rename(columns={"count": "Episodes"})
            st.bar_chart(chart_df, width="stretch")

        st.subheader("Recent import runs")
        runs = _df_import_runs(client, 30)
        if runs.empty:
            st.info("No import runs recorded yet.")
        else:
            st.dataframe(runs, width="stretch", hide_index=True)

    with tab_logs:
        sev = st.selectbox("Severity filter", options=["(all)", "ERROR", "WARNING", "INFO"])
        limit = st.slider("Row limit", min_value=50, max_value=2000, value=300, step=50)
        sev_arg = None if sev == "(all)" else sev
        logs = _df_logs(client, limit, sev_arg)
        if logs.empty:
            st.info("No log rows yet.")
        else:
            st.dataframe(logs, width="stretch", hide_index=True)

    render_site_footer()


if __name__ == "__main__":
    main()
