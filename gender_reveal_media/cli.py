from __future__ import annotations

import argparse
import json
import sys

from gender_reveal_media.config import load_settings
from gender_reveal_media.logging_utils import configure_logging
from gender_reveal_media.db import apply_schema, connect
from gender_reveal_media.itunes import populate_episodes_from_itunes
from gender_reveal_media.pipeline import run_ingest, run_populate_media_links


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    parser = argparse.ArgumentParser(prog="gender-reveal-media")
    sub = parser.add_subparsers(dest="cmd", required=True)

    ingest_p = sub.add_parser("ingest", help="Discover episodes, fetch transcripts, run Gemini extraction")
    ingest_p.add_argument(
        "--trigger",
        default="cli",
        help="Value stored on import_runs.trigger (e.g. github_actions)",
    )
    ingest_p.add_argument(
        "--populate-links",
        action="store_true",
        help="After media extraction, fill link_to_media via free catalog APIs (and Google CSE if configured)",
    )

    links_p = sub.add_parser(
        "populate-links",
        help="Fill missing link_to_media via free catalog APIs (optional Google CSE fallback)",
    )
    links_p.add_argument(
        "--refresh",
        action="store_true",
        help="Replace existing link_to_media values (default: only rows with empty links)",
    )
    links_p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max media rows to process (overrides MEDIA_LINK_SEARCH_LIMIT)",
    )

    sub.add_parser(
        "populate-itunes",
        help="Fill episode metadata from Apple Podcasts (iTunes lookup + RSS)",
    )

    args = parser.parse_args(argv)
    if args.cmd == "ingest":
        settings = load_settings(
            require_gemini=True,
            require_google_cse=False,
            populate_media_links=args.populate_links,
        )
        counts = run_ingest(settings, trigger=args.trigger)
        json.dump(counts, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0
    if args.cmd == "populate-links":
        settings = load_settings(require_gemini=False, require_google_cse=False)
        counts = run_populate_media_links(settings, refresh=args.refresh, limit=args.limit)
        json.dump(counts, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0
    if args.cmd == "populate-itunes":
        settings = load_settings(require_gemini=False)
        client = connect(settings)
        try:
            apply_schema(client)
            updated = populate_episodes_from_itunes(client, settings)
        finally:
            client.close()
        json.dump({"itunes_populated": updated}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
