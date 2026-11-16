"""Password-protected dashboard for MietRadar's Munich listings and drafts."""

from __future__ import annotations

import os
import re
import sqlite3
from collections.abc import Sequence
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from html import escape
from math import isfinite, log2
from pathlib import Path
from typing import TypeVar
from urllib.parse import unquote, urlsplit

import streamlit as st
from dotenv import load_dotenv

from dashboard_auth import logout_button, require_authentication
from dashboard_data import (
    BERLIN, RESULT_LIMIT, WG_REPLY_INBOX, AddressCheck, DateBasis, DatedListing, HiddenListing, ListingProfile,
    MapListing, Match, ProviderFilter, Reply, SecondaryListing, SentApplication, confirm_application_delivery,
    filter_listing_providers, load_dashboard, map_listings, set_listing_hidden, weekend_dates,
)
from listing_profiles import valid_listing_coordinates
from munich_transit import HAUPTBAHNHOF, OSTBAHNHOF
from reviewed_applications import (
    MAX_APPLICATION_ATTACHMENT_BYTES, ApplicationAttachmentInfo, ApplicationReviewError,
    cancel_application_review, load_application_attachment, remove_application_attachment,
    review_application, save_application_attachment,
)


ROOT = Path(__file__).resolve().parent.parent
STATES = {
    "draft": "Entwurf", "held": "Zurückgehalten", "sent": "Gesendet", "done": "Erledigt",
    "unconfirmed": "Versand unklar", "already_contacted": "Bereits kontaktiert", "unavailable": "Kontakt nicht verfügbar",
    "edited": "Entwurf bearbeitet", "queued": "Freigegeben · wartet auf Versand", "failed": "Versand nicht erfolgt",
}
_REVIEW_NOTICE = "mietradar_application_review_notice"
_VISIBILITY_NOTICE = "mietradar_listing_visibility_notice"
OFFERS_PER_PAGE = 4
OfferRow = TypeVar("OfferRow", Match, DatedListing, SecondaryListing)
SMALL_WORDS = {"am", "an", "auf", "für", "im", "in", "mit", "und", "von", "zu", "zur"}
WEEKDAYS = ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag")
RENT_LABELS = {"warm": "Warmmiete", "cold": "Kaltmiete", "total": "Gesamtmiete"}
FIT_LABELS = {"strong": "Passt gut", "possible": "Könnte passen", "unclear": "Noch unklar"}
RECIPIENT_LABELS = {"manager": "Verwaltung/Vermietung", "roommate": "Mitbewohner"}
COMMERCIAL_LABEL = "Gewerblicher Anbieter"
PROVIDER_FILTER_LABELS: dict[ProviderFilter, str] = {
    "all": "Alle Anbieter",
    "commercial": "Gewerbliche Anbieter",
    "other": "Privat oder nicht gekennzeichnet",
}
MAP_TIERS = {
    "a*": ("A* · bis 20 Min.", "#087660", "ab Hbf oder Ostbahnhof"),
    "a": ("A · bis 30 Min.", "#2557a5", "zum Hbf oder Ostbahnhof"),
    "b": ("B · bis 45 Min.", "#a44d08", "ab Ostbahnhof"),
    "c": ("C · bis 45 Min.", "#884498", "ab Hauptbahnhof"),
}
OSM_COPYRIGHT = '[© OpenStreetMap-Mitwirkende](https://www.openstreetmap.org/copyright)'
OSM_TILES = {
    "style": "white-bg",
    "layers": [{"sourcetype": "raster", "source": ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
                "below": "traces", "sourceattribution": "© OpenStreetMap contributors"}],
}
MAP_CONFIG = {"displaylogo": False, "modeBarButtonsToRemove": ["toImage"]}
INTRO = """
<style>
  [data-testid="stMainBlockContainer"] { max-width: 64rem; padding-top: 2.75rem; }
  .radar-heading { background: #173839; color: #fff; border-radius: 1.05rem;
    padding: clamp(1.25rem, 3.4vw, 2.25rem); margin-bottom: .75rem; }
  .radar-heading p { color: #d0e9df; margin: 0; }
  .radar-heading .place { color: #9fdbbf; font-size: .82rem; font-weight: 700; letter-spacing: .085em; }
  .radar-heading h1 { color: #fff; padding: 0; margin: .3rem 0 .45rem; }
  .radar-listing { display: grid; gap: .9rem; padding-block: .2rem; color: inherit; }
  .radar-listing h3 { margin: .32rem 0 0; padding: 0; font-size: clamp(1.15rem, 2.5vw, 1.5rem);
    line-height: 1.25; overflow-wrap: anywhere; }
  .radar-listing p { margin: 0; overflow-wrap: anywhere; }
  .radar-listing__meta { display: flex; flex-wrap: wrap; align-items: center; gap: .45rem .65rem; font-size: .82rem; }
  .radar-listing__source { font-weight: 650; }
  .radar-listing__badge { padding: .15rem .55rem; border-radius: .45rem; font-size: .76rem; font-weight: 650;
    background: #e1efed; color: #224a44; }
  .radar-listing__commercial { background: #fbe3a9; color: #523600; }
  .radar-listing__photos { display: grid; grid-template-columns: repeat(auto-fill, minmax(min(11rem, 45%), 1fr));
    gap: .55rem; }
  .radar-listing__photo { position: relative; display: block; overflow: hidden; border-radius: .7rem;
    background: rgba(78, 133, 123, .12); }
  .radar-listing__photo::before { content: 'Inseratsfoto'; position: absolute; inset: 0; display: grid;
    place-items: center; color: var(--text-color); opacity: .75; font-size: .85rem; }
  .radar-listing__photo img { position: relative; display: block; width: 100%; aspect-ratio: 4 / 3;
    object-fit: cover; font-size: 0; }
  .radar-listing__facts { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(9rem, 100%), 1fr)); gap: .6rem; }
  .radar-listing__fact { background: rgba(78, 133, 123, .12); border-radius: .6rem; padding: .55rem .75rem; }
  .radar-listing__label { display: block; font-size: .78rem; opacity: .84; }
  .radar-listing__value { display: block; font-size: 1.11rem; font-weight: 680; line-height: 1.4; }
  .radar-listing__missing { font-size: .88rem; font-weight: 500; }
  .radar-listing__section .radar-listing__label { font-weight: 650; opacity: 1; margin-bottom: .15rem; }
  .radar-listing__section p { font-size: .92rem; line-height: 1.46; }
  .radar-listing__section .radar-listing__badge { display: inline-block; margin: .05rem .4rem .15rem 0; }
  .radar-listing__section .radar-fit-possible { background: #fcf1d7; color: #664713; }
  .radar-listing__section .radar-fit-unclear { background: #efeaed; color: #51434e; }
  .radar-listing .radar-listing__foot { font-size: .8rem; opacity: .86; }
</style>
<header class="radar-heading"><p class="place">MÜNCHEN</p><h1>MietRadar</h1>
<p>Neue Mietangebote und deine Nachrichten.</p></header>
"""


def _date(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, BERLIN).strftime("%d.%m.%Y · %H:%M")


def _posting_day(day: date) -> str:
    return f"Veröffentlicht am {WEEKDAYS[day.weekday()]}, {day:%d.%m.%Y}"


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


def _change_listing_visibility(db_path: Path, listing_id: str, hidden: bool, title: str, url: str | None) -> None:
    try:
        set_listing_hidden(db_path, listing_id, hidden, title=title, url=url)
    except (OSError, sqlite3.Error):
        st.session_state[_VISIBILITY_NOTICE] = (False, "Die Änderung konnte gerade nicht gespeichert werden.")
    else:
        message = "Angebot ausgeblendet. Du findest es unter „Ausgeblendet“." if hidden else "Angebot wieder eingeblendet."
        st.session_state[_VISIBILITY_NOTICE] = (True, message)


def _offer_actions(
    db_path: Path, listing_id: str, title: str, url: str | None, *, view: str, hidden: bool = False,
) -> None:
    with st.container(horizontal=True):
        _location_link(url)
        st.button(
            "Wieder einblenden" if hidden else "Ausblenden",
            key=f"{view}_visibility_{listing_id}",
            icon=":material/visibility:" if hidden else ":material/visibility_off:",
            help=None if hidden else "Blendet das Angebot im Dashboard aus. Eine Versandfreigabe bleibt gültig.",
            on_click=_change_listing_visibility,
            args=(db_path, listing_id, not hidden, title, url),
        )


def _format_value(value: float | None, unit: str) -> str | None:
    if not isinstance(value, (int, float)) or not isfinite(value) or value <= 0:
        return None
    precision = 0 if int(value) == value else 2
    formatted = f"{value:,.{precision}f}".translate(str.maketrans(",.", ".,"))
    if unit == "m²" and precision:
        formatted = formatted.rstrip("0")
    return f"{formatted} {unit}"


def _fact(label: str, value: str | None) -> str:
    missing = " radar-listing__missing" if value is None else ""
    return (
        f'<div class="radar-listing__fact"><span class="radar-listing__label">{escape(label)}</span>'
        f'<strong class="radar-listing__value{missing}">{escape(value or "Keine Angabe")}</strong></div>'
    )


def _primary_route(profile: ListingProfile | None) -> tuple[str, tuple[str, str]]:
    if profile and profile.route_tier == "a*":
        category, direction, limit = "A*", "Ab Hbf oder Ostbahnhof", 20
    else:
        category, direction, limit = "A", "Zum Hbf oder Ostbahnhof", 30
    minutes = profile.route_minutes if profile else None
    duration = f"{minutes} Min." if isinstance(minutes, int) and 0 <= minutes <= limit else f"Bis zu {limit} Min."
    return category, (direction, duration)


def _map_facts(row: MapListing) -> str:
    tier, _, direction = MAP_TIERS[row.tier]
    route = f"{row.tier.upper()} · {row.minutes} Min." if row.minutes is not None else tier
    if row.station:
        direction = f"ab {row.station}"
    rent = _format_value(row.rent_eur, "€")
    if rent and (rent_kind := RENT_LABELS.get(row.rent_kind)):
        rent = f"{rent} {rent_kind}"
    area = _format_value(row.area_sqm, "m²")
    provider = COMMERCIAL_LABEL if row.is_commercial else None
    recipient = RECIPIENT_LABELS.get(row.recipient_role)
    return " · ".join(filter(None, (row.platform, provider, recipient, f"{route} {direction}", rent, area)))


def _listing_map(rows: tuple[MapListing, ...], db_path: Path) -> None:
    import plotly.graph_objects as go

    st.subheader("Angebote auf der Karte", anchor=False)
    pins = {row.id: row for row in rows if row.latitude is not None and row.longitude is not None}
    if not pins:
        st.info("Für die Angebote im gewählten Zeitraum liegen noch keine geprüften Koordinaten vor." if rows
                else "Für den gewählten Zeitraum gibt es keine passenden Angebote.")
    elif missing := len(rows) - len(pins):
        st.caption(f"{len(pins)} von {len(rows)} Angeboten auf der Karte. Bei {missing} fehlt ein geprüfter Standort.")

    stations = [tuple(map(float, value.split(","))) for value in (HAUPTBAHNHOF, OSTBAHNHOF)]
    latitudes, longitudes = map(list, zip(*stations))
    latitudes.extend(row.latitude for row in pins.values())
    longitudes.extend(row.longitude for row in pins.values())
    extent = max((max(latitudes) - min(latitudes)) / 0.67, max(longitudes) - min(longitudes), 0.11)
    fig = go.Figure(go.Scattermap(
        lat=latitudes[:2], lon=longitudes[:2], mode="markers", name="Bahnhöfe", legendrank=20,
        marker={"size": 13, "color": "#243849"}, text=["München Hauptbahnhof", "München Ostbahnhof"],
        unselected={"marker": {"opacity": 1}},
        hovertemplate="<b>%{text}</b><extra></extra>",
    ))
    for rank, (tier, (label, color, _)) in enumerate(MAP_TIERS.items()):
        tier_pins = [row for row in pins.values() if row.tier == tier]
        if not tier_pins:
            continue
        fig.add_trace(go.Scattermap(
            lat=[row.latitude for row in tier_pins], lon=[row.longitude for row in tier_pins],
            mode="markers", name=label, legendrank=rank, marker={"size": 17, "color": color, "opacity": 0.9},
            selected={"marker": {"size": 21}}, unselected={"marker": {"opacity": 0.9}},
            text=[escape(row.title) for row in tier_pins],
            customdata=[(row.id, escape(_map_facts(row))) for row in tier_pins],
            hovertemplate="<b>%{text}</b><br>%{customdata[1]}<extra></extra>",
        ))
    fig.update_layout(
        height=510, margin={"l": 0, "r": 0, "t": 52, "b": 0}, uirevision="munich-map",
        clickmode="event+select", legend={"orientation": "h", "x": 0, "y": 1.02},
        map={
            **OSM_TILES,
            "center": {"lat": (min(latitudes) + max(latitudes)) / 2, "lon": (min(longitudes) + max(longitudes)) / 2},
            "zoom": min(11.4, log2(360 / extent) - 0.7),
        },
    )
    event = st.plotly_chart(
        fig, key="mietradar_offers_map", on_select="rerun", selection_mode="points",
        config=MAP_CONFIG,
    )
    st.caption(OSM_COPYRIGHT)
    if not pins:
        return
    selected = event.selection.points[-1].get("customdata") if event.selection.points else None
    selected_id = selected[0] if selected and selected[0] in pins else None
    choices = list(pins)
    chosen = st.selectbox(
        "Angebot auswählen", choices, index=choices.index(selected_id) if selected_id else None,
        format_func=lambda item: f"{pins[item].tier.upper()} · {pins[item].title}",
        placeholder="Wähle einen Punkt auf der Karte oder ein Angebot aus der Liste",
        key=f"map_listing_{selected_id or 'list'}",
    )
    if chosen:
        row = pins[chosen]
        st.caption(_map_facts(row))
        _offer_actions(db_path, row.id, row.title, row.url, view="map")


def _offer_location_map(profile: ListingProfile | None, title: str, *, key: str) -> None:
    if profile and not profile.map_eligible:
        return
    point = valid_listing_coordinates(profile.latitude, profile.longitude) if profile else None
    if point is None:
        st.caption("Standort noch nicht bestätigt.")
        return

    import plotly.graph_objects as go

    latitude, longitude = point
    figure = go.Figure(go.Scattermap(
        lat=[latitude], lon=[longitude], mode="markers", marker={"size": 20, "color": "#087660"},
        text=[escape(title)], hovertemplate="<b>%{text}</b><extra></extra>",
    ))
    figure.update_layout(
        height=320, margin={"l": 0, "r": 0, "t": 0, "b": 0},
        map={**OSM_TILES, "center": {"lat": latitude, "lon": longitude}, "zoom": 15},
    )
    st.markdown("**Lage auf der Karte**")
    st.plotly_chart(figure, key=key, config=MAP_CONFIG)
    st.caption(OSM_COPYRIGHT)


def _listing_photos(photo_urls: tuple[str, ...]) -> str:
    if not photo_urls:
        return ""

    links = []
    for position, url in enumerate(photo_urls, start=1):
        source = escape(url, quote=True)
        links.append(
            f'<a class="radar-listing__photo" href="{source}" target="_blank" '
            f'rel="noopener noreferrer" aria-label="Inseratsfoto {position} in voller Größe öffnen">'
            f'<img src="{source}" alt="Inseratsfoto {position}" loading="lazy" decoding="async" '
            'referrerpolicy="no-referrer"></a>'
        )
    return f'<div class="radar-listing__photos" role="group" aria-label="Inseratsfotos">{"".join(links)}</div>'


def _listing_card(
    profile: ListingProfile | None, *, title: str, platform: str, status: str, date_label: str,
    route: tuple[str, str] | None = None, note: str | None = None, map_key: str | None = None,
) -> None:
    if profile and profile.title:
        title = profile.title
    rent_label = RENT_LABELS.get(profile.rent_kind, "Miete") if profile else "Miete"
    facts = _fact(rent_label, _format_value(profile.rent_eur, "€") if profile else None)
    facts += _fact("Fläche", _format_value(profile.area_sqm, "m²") if profile else None)
    if route:
        facts += _fact(*route)
    sections = []
    if profile and profile.summary:
        sections.append('<section class="radar-listing__section"><span class="radar-listing__label">Inseratszusammenfassung</span>'
                        f'<p>{escape(profile.summary)}</p></section>')
    if profile and (profile.fit or profile.fit_reason):
        fit = profile.fit if profile.fit in FIT_LABELS else "unclear"
        reason = f"<p>{escape(profile.fit_reason)}</p>" if profile.fit_reason else ""
        sections.append('<section class="radar-listing__section"><span class="radar-listing__label">Einschätzung</span>'
                        f'<span class="radar-listing__badge radar-fit-{fit}">{FIT_LABELS[fit]}</span>{reason}</section>')
    if note:
        sections.append(f'<p class="radar-listing__foot">{escape(note)}</p>')
    badge = f'<span class="radar-listing__badge">{escape(status)}</span>' if status else ""
    provider = (
        f'<span class="radar-listing__badge radar-listing__commercial">{COMMERCIAL_LABEL}</span>'
        if profile and profile.is_commercial else ""
    )
    recipient = RECIPIENT_LABELS.get(profile.recipient_role) if profile else None
    recipient_badge = f'<span class="radar-listing__badge">{recipient}</span>' if recipient else ""
    photos = _listing_photos(profile.photo_urls) if profile else ""
    st.html(
        '<article class="radar-listing"><header>'
        f'<div class="radar-listing__meta"><span class="radar-listing__source">{escape(platform)}</span>'
        f'{provider}{recipient_badge}{badge}</div>'
        f'<h3>{escape(title)}</h3></header>{photos}<div class="radar-listing__facts">{facts}</div>'
        f'{"".join(sections)}<p class="radar-listing__foot">{escape(date_label)}</p></article>'
    )
    if map_key is not None:
        _offer_location_map(profile, title, key=map_key)


def _credit_proof(db_path: Path, attachment: ApplicationAttachmentInfo | None) -> None:
    with st.expander("Bonitätsnachweis"):
        if attachment:
            st.text(f"Gespeichert · {attachment.name}")
            st.caption("Die Datei wird bei jeder neuen WG-Gesucht-Freigabe angezeigt und mit der Bewerbung verschickt.")
        version = attachment.digest if attachment else "no-attachment"
        with st.form(f"application-attachment-{version}", enter_to_submit=False, clear_on_submit=True):
            upload = st.file_uploader(
                "Bonitätsnachweis als PDF", type="pdf",
                max_upload_size=MAX_APPLICATION_ATTACHMENT_BYTES // (1024 * 1024),
                help="Wähle einen Bonitätsnachweis, der zur Weitergabe an Vermieter gedacht ist.",
            )
            save = st.form_submit_button("PDF ersetzen" if attachment else "PDF speichern")
        remove = bool(attachment and st.button("PDF entfernen", key=f"remove-application-attachment-{version}"))
        if not (save or remove):
            return
        try:
            if remove:
                remove_application_attachment(db_path)
                notice = "Der Bonitätsnachweis wurde entfernt."
            else:
                if upload is None:
                    st.error("Wähle zuerst eine PDF-Datei aus.")
                    return
                save_application_attachment(db_path, filename=upload.name, content=upload.getvalue())
                notice = "Der Bonitätsnachweis wurde gespeichert."
        except ApplicationReviewError as error:
            st.error(str(error))
            return
        st.session_state[_REVIEW_NOTICE] = notice
        st.rerun()


def _application(
    row: Match, db_path: Path, hours: int, *, review_available: bool, reviewed_send: bool,
    attachment: ApplicationAttachmentInfo | None,
) -> None:
    if row.state == "unconfirmed" and row.platform == "WG-Gesucht":
        st.warning("Der Versand ließ sich nicht bestätigen. Schau im WG-Gesucht-Postfach nach.")
        with st.container(horizontal=True):
            _location_link(WG_REPLY_INBOX, "Postfach öffnen")
            confirm = st.button("Versand im Postfach bestätigt", key=f"confirm_delivery_{row.id}",
                                help="Übernimmt die im Postfach geprüfte Bewerbung in deine Bewerbungsliste.")
        if confirm:
            try:
                saved = confirm_application_delivery(db_path, row.id, now=datetime.now(timezone.utc))
            except (OSError, sqlite3.Error):
                st.error("Die Versandbestätigung konnte gerade nicht gespeichert werden.")
                return
            if not saved:
                st.warning("Der Versandstatus hat sich geändert. Aktualisiere die Ansicht.")
                return
            st.session_state[_REVIEW_NOTICE] = "Die Bewerbung steht jetzt unter „Bewerbungen“."
            st.rerun()
    elif row.review_reason and row.review_reason != row.judge_reason:
        st.text(row.review_reason)
    if row.message is None:
        return
    if row.judge_approved is False:
        st.warning(row.judge_reason or "Die Textprüfung hat diese Fassung zurückgehalten.")
    elif row.judge_approved is True:
        st.caption(row.judge_reason or "Diese Fassung hat die Textprüfung bestanden.")

    editable = (review_available and row.platform == "WG-Gesucht" and row.url is not None
                and row.state not in {"sent", "unconfirmed"})
    with st.expander("Bewerbung prüfen" if editable else "Bewerbung anzeigen"):
        if not editable:
            st.code(row.message, language=None, wrap_lines=True)
            return

        version = sha256(row.message.encode("utf-8")).hexdigest()
        try:
            if row.state == "queued":
                st.code(row.message, language=None, wrap_lines=True)
                if row.attachment_digest:
                    if attachment and attachment.digest == row.attachment_digest:
                        st.text(f"Freigegebener Anhang · {attachment.name}")
                    else:
                        st.warning("Der freigegebene Anhang ist gerade nicht verfügbar.")
                if not st.button("Freigabe zurückziehen", key=f"cancel-application-{row.id}-{version}"):
                    return
                cancel_application_review(db_path, row.id, row.message, now=datetime.now(timezone.utc))
                notice = "Die Versandfreigabe wurde zurückgezogen. Dein Entwurf bleibt gespeichert."
            else:
                cutoff = datetime.now(timezone.utc) - timedelta(hours=min(hours, 24))
                may_approve = reviewed_send and row.fresh_at >= cutoff.timestamp()
                attachment_version = attachment.digest if attachment else "no-attachment"
                with st.form(f"application-{row.id}-{version}-{attachment_version}", enter_to_submit=False):
                    message = st.text_area("Deine Nachricht", value=row.message, height=220, max_chars=6000)
                    save = st.form_submit_button("Entwurf speichern")
                    approve = False
                    if may_approve:
                        if attachment:
                            st.text(f"Anhang · {attachment.name}")
                            approve = st.form_submit_button("Text und PDF freigeben und senden", type="primary")
                            st.caption("MietRadar verschickt genau diesen Text zusammen mit der angezeigten Datei.")
                        else:
                            approve = st.form_submit_button("Freigeben und senden", type="primary")
                            st.caption("MietRadar verschickt genau diesen Text.")
                    elif reviewed_send:
                        st.caption("Das Zeitfenster für den Versand ist abgelaufen.")
                    else:
                        st.caption("Der Versand nach Freigabe ist noch nicht eingerichtet.")
                if not (save or approve):
                    return
                result = review_application(
                    db_path, row.id, row.message, message, approve=approve,
                    now=datetime.now(timezone.utc), max_age=timedelta(hours=hours),
                    attachment_digest=attachment.digest if approve and attachment else None,
                )
                if result == "queued":
                    notice = ("Deine Bewerbung mit Anhang wurde zum Versand freigegeben." if attachment
                              else "Deine Nachricht wurde zum Versand freigegeben.")
                else:
                    notice = "Entwurf gespeichert."
        except ApplicationReviewError as error:
            st.error(str(error))
            return
        except (OSError, sqlite3.Error):
            st.error("Die Änderung konnte gerade nicht gespeichert werden. Versuch es nach dem Aktualisieren erneut.")
            return
        st.session_state[_REVIEW_NOTICE] = notice
        st.rerun()


def _offer_page(rows: Sequence[OfferRow], view: str) -> Sequence[OfferRow]:
    # Keep concurrent OpenStreetMap charts within the browser's WebGL context budget.
    if len(rows) <= OFFERS_PER_PAGE:
        return rows
    page_count = (len(rows) + OFFERS_PER_PAGE - 1) // OFFERS_PER_PAGE
    options = range(1, page_count + 1)
    key = f"{view}_offer_page"
    if st.session_state.get(key, 1) not in options:
        del st.session_state[key]
    page = st.selectbox("Angebotsseite", options, format_func=lambda page: f"{page} von {page_count}", key=key)
    start = (page - 1) * OFFERS_PER_PAGE
    return rows[start:start + OFFERS_PER_PAGE]


def _matches(
    rows: tuple[Match, ...], hours: int, wg_scan: tuple[float, int] | None, profiles: dict[str, ListingProfile],
    db_path: Path, *, review_available: bool, reviewed_send: bool, attachment: ApplicationAttachmentInfo | None,
    show_maps: bool,
) -> None:
    st.subheader("Neue Mietangebote", anchor=False)
    _scan_status(wg_scan)
    platforms = sorted({row.platform for row in rows})
    if not rows:
        st.info(f"Keine passenden Angebote in den letzten {hours} Stunden.")
        return
    platform = st.selectbox("Plattform", ["Alle", *platforms]) if len(platforms) > 1 else "Alle"
    category = st.selectbox("ÖPNV-Kategorie", ["A* und A", "A*", "A"])
    filtered = [row for row in rows if (platform == "Alle" or row.platform == platform)
                and (category == "A* und A" or _primary_route(profiles.get(row.id))[0] == category)]
    if not filtered:
        st.info("Keine Angebote mit diesen Filtern.")
        return
    for row in _offer_page(filtered, "current"):
        profile = profiles.get(row.id)
        tier, route = _primary_route(profile)
        with st.container(border=True):
            date_label = f"{row.basis.capitalize()} · {_date(row.fresh_at)}"
            if row.basis == "zuerst gesehen":
                posting = (f"{_posting_day(row.published_day)}, Uhrzeit unbekannt"
                           if row.published_day else "Veröffentlichungsdatum unbekannt")
                date_label = f"{date_label} · {posting}"
                if row.message and row.state != "queued":
                    if review_available and reviewed_send and row.platform == "WG-Gesucht":
                        date_label += " · Versand nur nach Freigabe"
                    else:
                        date_label += " · nur als Entwurf"
            status = STATES.get(row.state, "Entwurf")
            if row.judge_approved is False:
                status = "Freigegeben · Text zurückgehalten" if row.state == "queued" else "Text zurückgehalten"
            elif row.state == "queued" and row.review_reason:
                status = "Freigegeben · Versand verzögert"
            _listing_card(
                profile, title=_headline(row.url), platform=row.platform,
                status=f"{tier} · {status}", date_label=f"{date_label} · {row.id}", route=route,
                map_key=f"current_map_{row.id}" if show_maps and row.state != "unavailable" else None,
            )
            _offer_actions(db_path, row.id, profile.title if profile and profile.title else _headline(row.url),
                           row.url, view="current")
            _application(row, db_path, hours, review_available=review_available, reviewed_send=reviewed_send,
                         attachment=attachment)


def _scan_status(wg_scan: tuple[float, int] | None) -> None:
    if wg_scan is not None:
        timestamp, count = wg_scan
        st.caption(f"Letzte WG-Gesucht-Suche: {_date(timestamp)} · Sichtbare Anzeigen erfasst: {count}")


def _date_filter(today: date) -> tuple[DateBasis | None, tuple[date, date] | None]:
    current = "Aktuelle Treffer"
    weekend = "Dieses Wochenende" if today.weekday() >= 5 else "Letztes Wochenende"
    custom = "Eigener Zeitraum"
    selected = st.selectbox("Zeitraum für Angebote", [current, weekend, custom])
    if selected == current:
        return None, None
    date_label = st.radio("Datum filtern nach", ["Veröffentlicht", "Zuerst gesehen"])
    basis: DateBasis = "first_seen" if date_label == "Zuerst gesehen" else "published"
    start, end = weekend_dates(today)
    if selected == weekend:
        return basis, (start, min(end, today))
    days = st.date_input("Von – bis", (start, min(end, today)), max_value=today, format="DD.MM.YYYY")
    return basis, days if isinstance(days, tuple) and len(days) == 2 else None


def _dated_offers(
    rows: tuple[DatedListing, ...], days: tuple[date, date] | None, basis: DateBasis,
    wg_scan: tuple[float, int] | None, profiles: dict[str, ListingProfile], db_path: Path,
    *, provider_filtered: bool = False, show_maps: bool,
) -> None:
    sightings = basis == "first_seen"
    st.subheader("Zuerst erfasste Mietangebote" if sightings else "Veröffentlichte Mietangebote", anchor=False)
    _scan_status(wg_scan)
    if days is None:
        st.info("Wähle ein Start- und Enddatum.")
        return
    start, end = days
    st.caption(f"{start:%d.%m.%Y} bis {end:%d.%m.%Y} · Münchner Zeit")
    if sightings:
        st.caption("Die erste Suche erfasst auch Anzeigen, die schon online sind.")
    if not rows:
        if provider_filtered:
            st.info("Für diesen Zeitraum gibt es keine Angebote dieses Anbietertyps.")
        else:
            st.info("Für diesen Zeitraum gibt es keine Erstsichtungen ohne Veröffentlichungsdatum." if sightings else
                    "Für diesen Zeitraum wurde kein Veröffentlichungsdatum bestätigt. Unter „Zuerst gesehen“ stehen die Erstsichtungen ohne Datum.")
        return
    if len(rows) == RESULT_LIMIT:
        st.caption(f"Anzeige auf {RESULT_LIMIT} Ergebnisse begrenzt.")
    for row in _offer_page(rows, "dated"):
        with st.container(border=True):
            if sightings:
                date_label = (f"Zuerst gesehen am {WEEKDAYS[row.day.weekday()]}, {row.day:%d.%m.%Y}"
                              " · Veröffentlichungsdatum unbekannt")
            else:
                date_label = _posting_day(row.day)
            profile = profiles.get(row.id)
            status = "Filter erfüllt" if row.matched else "Erfasst"
            route = None
            if row.matched and profile and profile.route_tier in {"a*", "a"}:
                tier, route = _primary_route(profile)
                status = f"{tier} · {status}"
            _listing_card(profile, title=_headline(row.url), platform=row.platform,
                          status=status, date_label=date_label, route=route,
                          map_key=f"dated_map_{row.id}" if show_maps else None)
            _offer_actions(db_path, row.id, profile.title if profile and profile.title else _headline(row.url),
                           row.url, view="dated")


def _addresses(rows: tuple[AddressCheck, ...], profiles: dict[str, ListingProfile], db_path: Path) -> None:
    st.subheader("Adresse prüfen", anchor=False)
    if not rows:
        st.info("Keine Kleinanzeigen-Angebote zur Ortsprüfung.")
        return
    for row in rows:
        with st.container(border=True):
            profile = profiles.get(row.id)
            title = (profile.title if profile else None) or row.title or _headline(row.url)
            _listing_card(profile, title=title, platform="Kleinanzeigen",
                          status="Adresse prüfen", date_label=f"{_date(row.fresh_at)} · {row.id}",
                          note=row.reason)
            _offer_actions(db_path, row.id, title, row.url, view="address")


def _secondary_offers(
    rows: tuple[SecondaryListing, ...], hours: int, profiles: dict[str, ListingProfile], db_path: Path,
) -> None:
    st.caption("Bis zu 45 Min. mit dem ÖPNV ab Ostbahnhof für B oder ab Hauptbahnhof für C. Passt beides, gilt B.")
    if not rows:
        st.info(f"Keine Angebote der Kategorie B oder C in den letzten {hours} Stunden.")
        return
    page = _offer_page(sorted(rows, key=lambda row: row.tier), "secondary")
    for tier in ("b", "c"):
        offers = [row for row in page if row.tier == tier]
        if not offers:
            continue
        st.subheader(f"Kategorie {tier.upper()}", anchor=False)
        for row in offers:
            station = "Hauptbahnhof" if row.station == "Hbf" else row.station
            with st.container(border=True):
                profile = profiles.get(row.id)
                title = (profile.title if profile else None) or row.title or _headline(row.url)
                _listing_card(profile, title=title, platform=row.platform,
                              status="", date_label=f"{row.basis.capitalize()} · {_date(row.fresh_at)}",
                              route=(f"Ab {station}", f"{row.minutes} Min."), map_key=f"secondary_map_{row.id}")
                _offer_actions(db_path, row.id, title, row.url, view="secondary")


def _hidden_offers(rows: tuple[HiddenListing, ...], db_path: Path) -> None:
    st.subheader("Ausgeblendete Angebote", anchor=False)
    if not rows:
        st.info("Du hast noch keine Angebote ausgeblendet.")
        return
    for row in rows:
        with st.container(border=True):
            st.text(row.title)
            st.caption(f"{row.platform} · Ausgeblendet am {_date(row.hidden_at)}")
            _offer_actions(db_path, row.id, row.title, row.url, view="hidden", hidden=True)


def _sent_applications(rows: tuple[SentApplication, ...]) -> None:
    st.subheader("Gesendete Bewerbungen", anchor=False)
    if not rows:
        st.info("MietRadar hat noch keinen bestätigten Versand erfasst.")
        return
    for row in rows:
        with st.container(border=True):
            title = row.title or _headline(row.url)
            if title == "Mietangebot":
                title = f"Mietangebot {row.id.removeprefix('ka-')}"
            st.text(title)
            st.caption(f"{row.platform} · Versand bestätigt am {_date(row.sent_at)}")
            _location_link(row.url)


def _replies(rows: tuple[Reply, ...], *, wg_enabled: bool, wg_status: tuple[float, bool] | None) -> None:
    st.subheader("Antwortentwürfe", anchor=False)
    if wg_enabled:
        if wg_status is None:
            st.caption("MietRadar hat das WG-Gesucht-Postfach noch nicht geprüft.")
        elif not wg_status[1]:
            st.info("MietRadar kann das WG-Gesucht-Postfach gerade nicht lesen. Prüfe den WG-Gesucht-Login auf Railway.")
            _location_link(WG_REPLY_INBOX, "WG-Gesucht-Postfach öffnen")
        else:
            st.caption(f"WG-Gesucht-Postfach zuletzt geprüft · {_date(wg_status[0])}")
    if not rows:
        st.info("Keine Antwortentwürfe vorhanden.")
        return
    for row in rows:
        with st.container(border=True):
            when = _date(row.received_at)
            if row.platform == "WG-Gesucht":
                when = f"Erfasst {when}"
            details = [row.platform, row.listing_id, row.label, STATES.get(row.state, "Entwurf"), when]
            st.caption(" · ".join(detail for detail in details if detail))
            if row.summary:
                st.text(row.summary)
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

    directory = Path(os.getenv("MUNICH_DATA_DIR") or os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or ROOT / "data")
    try:
        hours = int(os.getenv("MUNICH_MAX_AGE_HOURS", "24"))
        if not 1 <= hours <= 72:
            raise ValueError
    except ValueError:
        st.error("MUNICH_MAX_AGE_HOURS muss zwischen 1 und 72 liegen.")
        return
    now = datetime.now(timezone.utc)
    with st.sidebar:
        st.header("MietRadar")
        st.button("Aktualisieren", icon=":material/refresh:")
        date_basis, date_window = _date_filter(now.astimezone(BERLIN).date())
        provider = st.selectbox("Anbietertyp", tuple(PROVIDER_FILTER_LABELS), format_func=PROVIDER_FILTER_LABELS.__getitem__)
        logout_button()

    db_path = directory / "munich_radar.sqlite3"
    data = load_dashboard(db_path, now=now, max_age_hours=hours,
                          posting_window=date_window, date_basis=date_basis or "published")
    if data.status == "missing":
        st.info("MietRadar hat hier noch keine Datenbank angelegt. Starte den Worker für den ersten Suchlauf.")
        return
    if data.status != "ready":
        st.warning("Die MietRadar-Daten konnten gerade nicht gelesen werden. Aktualisiere die Seite in Kürze.")
        return
    data = filter_listing_providers(data, provider)
    if notice := st.session_state.pop(_REVIEW_NOTICE, None):
        st.success(notice)
    if visibility_notice := st.session_state.pop(_VISIBILITY_NOTICE, None):
        success, message = visibility_notice
        if success:
            st.toast(message)
        else:
            st.error(message)

    matches, drafts, pending = st.columns(3)
    if date_basis:
        label = "Zuerst erfasste Angebote" if date_basis == "first_seen" else "Veröffentlichte Angebote"
        matches.metric(label, len(data.dated_listings), border=True)
    else:
        matches.metric("Neue Angebote", len(data.matches), border=True)
    drafts.metric("Bewerbungsentwürfe", sum(bool(row.message) for row in data.matches), border=True)
    pending.metric("Adressen zur Prüfung", len(data.address_checks), border=True)
    offers, applications, map_tab, secondary, address, replies, activity, hidden = st.tabs(
        ["Angebote", "Bewerbungen", "Karte", "B & C", "Adresse prüfen", "Antwortentwürfe", "Verlauf", "Ausgeblendet"],
        key="mietradar_tab", on_change="rerun",
    )
    with offers:
        attachment = None
        reviewed_send = os.getenv("MUNICH_REVIEWED_SEND", "").strip().lower() == "true"
        if reviewed_send and data.application_reviews_available:
            try:
                attachment = load_application_attachment(db_path)
            except ApplicationReviewError as error:
                st.warning(str(error))
                reviewed_send = False
            else:
                _credit_proof(db_path, attachment)
        if date_basis:
            _dated_offers(data.dated_listings, date_window, date_basis, data.wg_scan, data.listing_profiles, db_path,
                          provider_filtered=provider != "all", show_maps=bool(offers.open))
        else:
            _matches(data.matches, hours, data.wg_scan, data.listing_profiles, db_path,
                     review_available=data.application_reviews_available,
                     reviewed_send=reviewed_send, attachment=attachment, show_maps=bool(offers.open))
    if applications.open:
        with applications:
            _sent_applications(data.sent_applications)
    if map_tab.open:
        with map_tab:
            _listing_map(map_listings(data, date_filtered=date_basis is not None), db_path)
    if secondary.open:
        with secondary:
            _secondary_offers(data.secondary_offers, hours, data.listing_profiles, db_path)
    with address:
        _addresses(data.address_checks, data.listing_profiles, db_path)
    with replies:
        _replies(data.replies, wg_enabled=os.getenv("MUNICH_WG_REPLY_DRAFTS", "").strip().lower() == "true",
                 wg_status=data.wg_inbox_status)
    with activity:
        _activity(data.activity)
    if hidden.open:
        with hidden:
            _hidden_offers(data.hidden_listings, db_path)


if __name__ == "__main__":
    main()
