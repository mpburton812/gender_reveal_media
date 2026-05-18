-- Episodes and transcript storage (Turso / SQLite)
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_episode_key TEXT NOT NULL UNIQUE,
    listen_page_url TEXT NOT NULL DEFAULT 'https://www.genderpodcast.com/listen',
    transcript_source_url TEXT,
    scraped_season INTEGER,
    scraped_list_label TEXT NOT NULL,
    transcript_text TEXT,
    transcript_sha256 TEXT,
    season INTEGER,
    episode_number INTEGER,
    episode_name TEXT,
    episode_date TEXT,
    guest TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS episode_processing_state (
    episode_id INTEGER PRIMARY KEY,
    stage TEXT NOT NULL CHECK (
        stage IN (
            'discovered',
            'transcript_missing',
            'transcript_downloaded',
            'metadata_extracted',
            'media_extracted',
            'failed'
        )
    ),
    last_error_code TEXT,
    last_error_message TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (episode_id) REFERENCES episodes(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS media_references (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id INTEGER NOT NULL,
    media_type TEXT NOT NULL CHECK (
        media_type IN (
            'artists',
            'music',
            'publications',
            'movies',
            'books',
            'zines',
            'graphic novels',
            'games',
            'tv shows'
        )
    ),
    media_sub_category TEXT,
    media_name TEXT NOT NULL,
    link_to_media TEXT,
    context_description TEXT NOT NULL DEFAULT '',
    model_name TEXT,
    prompt_version TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (episode_id) REFERENCES episodes(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS import_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT,
    trigger TEXT NOT NULL DEFAULT 'unknown',
    status TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'success', 'partial', 'failed')),
    episodes_discovered INTEGER NOT NULL DEFAULT 0,
    transcripts_new INTEGER NOT NULL DEFAULT 0,
    metadata_ok INTEGER NOT NULL DEFAULT 0,
    media_ok INTEGER NOT NULL DEFAULT 0,
    errors INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS log_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    severity TEXT NOT NULL CHECK (severity IN ('ERROR', 'WARNING', 'INFO')),
    component TEXT NOT NULL,
    episode_id INTEGER,
    import_run_id INTEGER,
    message TEXT NOT NULL,
    context_json TEXT,
    FOREIGN KEY (episode_id) REFERENCES episodes(id) ON DELETE SET NULL,
    FOREIGN KEY (import_run_id) REFERENCES import_runs(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_episodes_sha ON episodes(transcript_sha256);
CREATE INDEX IF NOT EXISTS idx_media_episode ON media_references(episode_id);
CREATE INDEX IF NOT EXISTS idx_log_created ON log_events(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_log_run ON log_events(import_run_id);
CREATE INDEX IF NOT EXISTS idx_state_stage ON episode_processing_state(stage);
