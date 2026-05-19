from __future__ import annotations

from pathlib import Path

import streamlit as st

_SITE_URL = "https://www.genderpodcast.com/"
_HERO_IMAGE = (
    "https://images.squarespace-cdn.com/content/v1/5b8edb475cfd79695f73e271/"
    "1606256644877-WFYKIU0O7GC6RAIEYZN2/Full_Size_Gender_Reveal_new_color_cover_photo.jpg"
)

_NAV_LINKS: tuple[tuple[str, str], ...] = (
    ("Home", f"{_SITE_URL}"),
    ("Episodes", f"{_SITE_URL}listen"),
    ("Live Shows", f"{_SITE_URL}live-shows"),
    ("Starter Packs", f"{_SITE_URL}starterpacks"),
    ("FAQ", f"{_SITE_URL}faq"),
    ("Grant", f"{_SITE_URL}grant"),
    ("Donate", f"{_SITE_URL}donate"),
    ("Contact", f"{_SITE_URL}contact"),
)


def inject_brand_styles() -> None:
    css_path = Path(__file__).resolve().parent.parent / ".streamlit" / "gender_reveal.css"
    if css_path.is_file():
        st.markdown(f"<style>{css_path.read_text(encoding='utf-8')}</style>", unsafe_allow_html=True)


def render_site_header(*, page_title: str = "Media catalog") -> None:
    nav = " ".join(
        f'<a href="{href}" target="_blank" rel="noopener noreferrer">{label}</a>'
        for label, href in _NAV_LINKS
    )
    st.markdown(
        f"""
        <div class="gr-site-header">
          <nav class="gr-nav" aria-label="Gender Reveal site">{nav}</nav>
          <a class="gr-logo" href="{_SITE_URL}" target="_blank" rel="noopener noreferrer">
            <span>Gender</span><span>Reveal</span>
          </a>
          <p class="gr-tagline">
            Media referenced on the Gender Reveal podcast — search, browse, and follow links
            to books, film, music, and more.
          </p>
        </div>
        <div class="gr-hero" style="background-image: url('{_HERO_IMAGE}');">
          <div class="gr-hero-inner">
            <h1>{page_title}</h1>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_site_footer() -> None:
    st.markdown(
        f"""
        <div class="gr-footer">
          An extension of
          <a href="{_SITE_URL}" target="_blank" rel="noopener noreferrer">genderpodcast.com</a>
          · Media data populated by the ingestion pipeline
        </div>
        """,
        unsafe_allow_html=True,
    )
