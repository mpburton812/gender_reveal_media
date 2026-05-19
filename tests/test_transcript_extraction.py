from __future__ import annotations

from gender_reveal_media.transcript_extraction import (
    _filename_from_content_disposition,
    infer_transcript_extension,
)


def test_filename_from_content_disposition_star() -> None:
    cd = "attachment; filename=\"Episode.docx\"; filename*=UTF-8''Episode%202.docx"
    assert _filename_from_content_disposition(cd) == "Episode 2.docx"


def test_infer_extension_from_content_type() -> None:
    ext = infer_transcript_extension(
        "https://www.genderpodcast.com/s/abc123",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        None,
    )
    assert ext == ".docx"


def test_infer_extension_squarespace_short_link() -> None:
    ext = infer_transcript_extension(
        "https://static1.squarespace.com/static/foo/transcript",
        "application/msword",
        'attachment; filename="GR35.doc"',
    )
    assert ext == ".doc"


def test_infer_extension_pdf() -> None:
    ext = infer_transcript_extension(
        "https://www.genderpodcast.com/s/55_Morgen-Bromell.pdf",
        "application/pdf",
        None,
    )
    assert ext == ".pdf"


def test_infer_extension_html() -> None:
    ext = infer_transcript_extension(
        "https://translash.org/transcript-example",
        "text/html; charset=UTF-8",
        None,
    )
    assert ext == ".html"
