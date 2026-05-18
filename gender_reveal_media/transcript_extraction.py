from __future__ import annotations

import hashlib
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import requests
from docx import Document

logger = logging.getLogger(__name__)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _pandoc_to_plain(path: Path) -> str | None:
    pandoc = shutil.which("pandoc")
    if not pandoc:
        return None
    try:
        proc = subprocess.run(
            [pandoc, "-f", "auto", "-t", "plain", str(path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=300,
        )
        if proc.returncode != 0:
            logger.warning("pandoc failed rc=%s stderr=%s", proc.returncode, proc.stderr[:500])
            return None
        return proc.stdout.strip()
    except subprocess.TimeoutExpired:
        logger.warning("pandoc timed out for %s", path)
        return None


def _extract_docx_python(path: Path) -> str:
    doc = Document(str(path))
    parts: list[str] = []
    for p in doc.paragraphs:
        t = (p.text or "").strip()
        if t:
            parts.append(t)
    return "\n".join(parts).strip()


def _extract_odt_python(path: Path) -> str:
    from odf import teletype
    from odf.opendocument import load
    from odf import text as odf_text

    doc = load(str(path))
    chunks: list[str] = []
    for el in doc.getElementsByType(odf_text.P):
        t = teletype.extractText(el).strip()
        if t:
            chunks.append(t)
    return "\n".join(chunks).strip()


def extract_local_file(path: Path) -> str:
    suffix = path.suffix.lower()
    plain = _pandoc_to_plain(path)
    if plain:
        return plain
    if suffix == ".docx":
        return _extract_docx_python(path)
    if suffix == ".odt":
        return _extract_odt_python(path)
    if suffix == ".doc":
        raise RuntimeError(
            "Legacy .doc requires pandoc or LibreOffice; install pandoc in CI or convert manually."
        )
    raise RuntimeError(f"Unsupported transcript file type: {suffix}")


def download_transcript_text(
    url: str,
    *,
    user_agent: str,
    timeout: int = 120,
) -> tuple[str, str]:
    """
    Download transcript file and return (plain_text, sha256_hex).
    Raises on HTTP errors or unsupported hosts for non-document URLs.
    """
    lower = url.lower()
    parsed = urlparse(url)
    allowed_ext = (".docx", ".odt", ".doc")
    if not any(lower.split("?", 1)[0].endswith(ext) for ext in allowed_ext):
        raise RuntimeError("UNSUPPORTED_TRANSCRIPT_URL: only .docx, .odt, .doc are supported for extraction.")

    headers = {"User-Agent": user_agent}
    r = requests.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    data = r.content
    tmpdir = Path(tempfile.mkdtemp(prefix="grm-transcript-"))
    try:
        ext = Path(parsed.path.split("?", 1)[0]).suffix.lower() or ".docx"
        local = tmpdir / f"transcript{ext}"
        local.write_bytes(data)
        text = extract_local_file(local)
        if not text.strip():
            raise RuntimeError("EMPTY_TRANSCRIPT_AFTER_EXTRACTION")
        digest = hashlib.sha256(data).hexdigest()
        return text, digest
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
