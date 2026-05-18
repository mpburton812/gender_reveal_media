from __future__ import annotations

import argparse
import json
import sys

from gender_reveal_media.config import load_settings
from gender_reveal_media.logging_utils import configure_logging
from gender_reveal_media.pipeline import run_ingest


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

    args = parser.parse_args(argv)
    if args.cmd == "ingest":
        settings = load_settings(require_gemini=True)
        counts = run_ingest(settings, trigger=args.trigger)
        json.dump(counts, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
