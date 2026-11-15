"""Password-protected read-only view of MietRadar's Munich database."""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo

import streamlit as st
from dotenv import load_dotenv

from dashboard_auth import logout_button, require_authentication
from dashboard_data import load_dashboard


ROOT = Path(__file__).resolve().parent.parent
BERLIN = ZoneInfo("Europe/Berlin")
STATES = {
    "draft": "Entwurf", "held": "Zurückgehalten", "sent": "Gesendet", "done": "Erledigt",
    "unconfirmed": "Versand unklar", "already_contacted": "Bereits kontaktiert", "unavailable": "Kontakt nicht verfügbar",
}
SMALL_WORDS = {"am", "an", "auf", "für", "im", "in", "mit", "und", "von", "zu", "zur"}
INTRO = """
<style>
  [data-testid="stMainBlockContainer"] { max-width: 64rem; padding-top: 2.75rem; }
  .radar-heading { background: #173839; color: #fff; border-radius: 1.05rem;
    padding: clamp(1.25rem, 3.4vw, 2.25rem); margin-bottom: .75rem; }
  .radar-heading p { color: #d0e9df; margin: 0; }
  .radar-heading .place { color: #9fdbbf; font-size: .82rem; font-weight: 700; letter-spacing: .085em; }
  .radar-heading h1 { color: #fff; padding: 0; margin: .3rem 0 .45rem; }
</style>
<header class="radar-heading"><p class="place">MÜNCHEN</p><h1>MietRadar</h1>
<p>Neue Mietangebote und deine Nachrichten.</p></header>
"""


def _date(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, BERLIN).strftime("%d.%m.%Y · %H:%M")


def _headline(url: str | None) -> str:
    if not url:
        return "Mietangebot"
    parts = unquote(urlsplit(url).path).strip("/").split("/")
    if len(parts) == 3 and parts[0] == "s-anzeige":
        words = parts[1].split("-")
        return " ".join(word if index and word in SMALL_WORDS else word.capitalize()
                        for index, word in enumerate(words) if word) or "Mietangebot"
    if len(parts) == 1 and (details := _match_location(parts[0])):
        category, location = details
        return f"{category} · {location}"
    return "Mietangebot"


def _match_location(path: str) -> tuple[str, str] | None:
    match = re.fullmatch(r"(wg-zimmer|1-zimmer-wohnungen|wohnungen)-in-Muenchen(?:-([\w-]+))?\.\d+\.html", path, re.I)
    if not match:
        return None
    category = {"wg-zimmer": "WG-Zimmer", "1-zimmer-wohnungen": "1-Zimmer-Wohnung", "wohnungen": "Wohnung"}[match[1].lower()]
    return category, match[2].replace("-", " ") if match[2] else "München"


def _location_link(url: str | None, label: str = "Anzeige öffnen") -> None:
    if url:
        st.link_button(label, url, icon=":material/open_in_new:")


def _matches(rows, hours: int) -> None:
    st.subheader("Neue Mietangebote", anchor=False)
    platforms = sorted({row.platform for row in rows})
    if not rows:
        st.info(f"Keine passenden Angebote in den letzten {hours} Stunden.")
        return
    platform = st.selectbox("Plattform", ["Alle", *platforms]) if len(platforms) > 1 else "Alle"
    for row in rows:
        if platform != "Alle" and row.platform != platform:
            continue
        with st.container(border=True):
            st.subheader(_headline(row.url), anchor=False)
            st.caption(f"{row.platform} · {row.id} · {STATES.get(row.state, 'Entwurf')}")
            st.caption(f"{row.basis.capitalize()}: {_date(row.fresh_at)}")
            if row.review_reason and row.state == "held":
                st.text(row.review_reason)
            _location_link(row.url)
            if row.message:
                with st.expander("Bewerbung anzeigen"):
                    st.code(row.message, language=None, wrap_lines=True)


def _addresses(rows) -> None:
    st.subheader("Adresse prüfen", anchor=False)
    if not rows:
        st.info("Keine Kleinanzeigen-Angebote zur Ortsprüfung.")
        return
    for row in rows:
        with st.container(border=True):
            st.text(row.title or _headline(row.url))
            st.caption(f"Kleinanzeigen · {row.id} · {_date(row.fresh_at)}")
            st.text(row.reason)
            _location_link(row.url)


def _replies(rows) -> None:
    st.subheader("Antwortentwürfe", anchor=False)
    if not rows:
        st.info("Keine Antworten vorhanden.")
        return
    for row in rows:
        with st.container(border=True):
            st.caption(f"{row.listing_id or 'Kleinanzeigen'} · {STATES.get(row.state, 'Entwurf')} · {_date(row.received_at)}")
            if row.review_reason:
                st.text(row.review_reason)
            _location_link(row.url, "Nachrichtenverlauf öffnen")
            if row.message:
                with st.expander("Antwort anzeigen"):
                    st.code(row.message, language=None, wrap_lines=True)


def _activity(rows) -> None:
    st.subheader("Verlauf", anchor=False)
    if not rows:
        st.info("Noch keine Kleinanzeigen-Aktivität.")
        return
    for row in rows:
        with st.container(border=True):
            st.text(row.subject)
            st.caption(f"{row.listing_id or 'Kleinanzeigen'} · {_date(row.occurred_at)}")
            st.text(row.summary)
            _location_link(row.url, "Auf Kleinanzeigen öffnen")


def main() -> None:
    load_dotenv(ROOT / "config" / ".env")
    st.set_page_config(page_title="MietRadar · München", page_icon=":material/key:", layout="wide")
    st.set_option("client.toolbarMode", "minimal")
    st.set_option("client.showErrorDetails", "none")
    st.html(INTRO)
    require_authentication()

    with st.sidebar:
        st.header("MietRadar")
        st.button("Aktualisieren", icon=":material/refresh:")
        logout_button()

    directory = Path(os.getenv("MUNICH_DATA_DIR") or os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or ROOT / "data")
    try:
        hours = int(os.getenv("MUNICH_MAX_AGE_HOURS", "24"))
        if not 1 <= hours <= 72:
            raise ValueError
    except ValueError:
        st.error("MUNICH_MAX_AGE_HOURS muss zwischen 1 und 72 liegen.")
        return
    data = load_dashboard(directory / "munich_radar.sqlite3", max_age_hours=hours)
    if data.status == "missing":
        st.info("MietRadar hat hier noch keine Datenbank angelegt. Starte den Worker für den ersten Suchlauf.")
        return
    if data.status != "ready":
        st.warning("Die MietRadar-Daten konnten gerade nicht gelesen werden. Aktualisiere die Seite in Kürze.")
        return

    matches, drafts, pending = st.columns(3)
    matches.metric("Neue Angebote", len(data.matches), border=True)
    drafts.metric("Bewerbungsentwürfe", sum(bool(row.message) for row in data.matches), border=True)
    pending.metric("Adressen zur Prüfung", len(data.address_checks), border=True)
    offers, address, replies, activity = st.tabs(["Angebote", "Adresse prüfen", "Antwortentwürfe", "Verlauf"])
    with offers:
        _matches(data.matches, hours)
    with address:
        _addresses(data.address_checks)
    with replies:
        _replies(data.replies)
    with activity:
        _activity(data.activity)


if __name__ == "__main__":
    main()
