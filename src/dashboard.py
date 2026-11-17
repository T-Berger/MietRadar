"""Password-protected dashboard for MietRadar's Munich listings and drafts."""

from __future__ import annotations

import os
import re
import sqlite3
from collections.abc import Iterator, Sequence
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from html import escape
from itertools import groupby
from math import isfinite, log2
from pathlib import Path
from typing import TypeVar
from urllib.parse import unquote, urlsplit

import streamlit as st
from dotenv import load_dotenv

from application_revision import ApplicationRevisionError, MAX_REVISION_FOCUS_CHARS, regenerate_application
from dashboard_auth import logout_button, require_authentication
from dashboard_data import (
    BERLIN, RESULT_LIMIT, WG_REPLY_INBOX, AddressCheck, CreditProof, DateBasis, DatedListing, HiddenListing, ListingProfile,
    MapListing, Match, ProviderFilter, QueuedApplication, Reply, SecondaryListing, SentApplication,
    confirm_application_delivery,
    filter_listing_providers, load_dashboard, map_listings, set_listing_hidden, weekend_dates,
)
from listing_profiles import valid_listing_coordinates
from munich_delivery import application_recipient_role
from munich_transit import HAUPTBAHNHOF, OSTBAHNHOF
from reviewed_applications import (
    MAX_APPLICATION_ATTACHMENT_BYTES, ApplicationAttachmentInfo, ApplicationReviewError,
    cancel_application_review, load_application_attachment, remove_application_attachment,
    review_application, save_application_attachment,
)
from wg_credit_proof import CREDIT_PROOF_REQUIRED_REASON


ROOT = Path(__file__).resolve().parent.parent
CREDIT_PROOF_LABELS: dict[CreditProof, str] = {
    "requested": "Bonitätsauskunft gewünscht",
    "required": "SCHUFA-Datenkopie reicht nicht",
}
STATES = {
    "draft": "Entwurf", "held": "Zurückgehalten", "sent": "Gesendet", "done": "Erledigt",
    "unconfirmed": "Versand unbestätigt", "already_contacted": "Bereits kontaktiert", "unavailable": "Kontakt nicht verfügbar",
    "edited": "Entwurf bearbeitet", "queued": "Für Versand freigegeben", "failed": "Versand gestoppt",
    "credit_proof_required": CREDIT_PROOF_LABELS["required"],
}
_QUEUE_TAB = "Versandwarteschlange"
_TAB_KEY = "mietradar_tab"
_SHOW_QUEUE = "mietradar_show_queue"
_QUEUE_COLORS = {"queued": "blue", "failed": "red", "unconfirmed": "orange", "unavailable": "red",
                 "already_contacted": "gray", "credit_proof_required": "red"}
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
TRANSIT_MODE_LABELS = {
    "WALK": "Zu Fuß", "BUS": "Bus", "TRAM": "Tram", "SUBWAY": "U-Bahn", "SUBURBAN": "S-Bahn",
    "REGIONAL_RAIL": "Regionalzug",
}
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
  .radar-listing__credit-proof-required { background: #ffe4dc; color: #75291d; }
  .radar-listing__credit-proof-requested { background: #e9edf1; color: #35495c; }
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
  .radar-listing__routes { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(17rem, 100%), 1fr));
    align-items: start; gap: .6rem; }
  .radar-listing__route { background: rgba(78, 133, 123, .12); border-radius: .6rem; padding: .65rem .75rem; }
  .radar-listing__route summary { cursor: pointer; overflow-wrap: anywhere; }
  .radar-listing__route-duration { white-space: nowrap; }
  .radar-listing__route-preview { display: block; margin-top: .18rem; font-size: .84rem; }
  .radar-listing__legs { margin: .45rem 0 0; padding-left: 1.15rem; font-size: .84rem; }
  .radar-listing__legs li + li { margin-top: .3rem; }
  .radar-listing__leg-stops { display: block; opacity: .86; overflow-wrap: anywhere; }
  .radar-listing .radar-listing__transit-time { margin-bottom: .45rem; }
  .radar-listing .radar-listing__route-time { margin-top: .45rem; }
  .radar-listing .radar-listing__foot { font-size: .8rem; opacity: .86; }
  .radar-listing header .radar-listing__foot { margin-top: .35rem; }
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
    credit_proof = CREDIT_PROOF_LABELS[row.credit_proof] if row.credit_proof else None
    return " · ".join(filter(None, (row.platform, provider, recipient, f"{route} {direction}", rent, area, credit_proof)))


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


def _listing_connections(profile: ListingProfile | None) -> str:
    if not profile or not profile.transit_routes:
        return ""

    cards = []
    for route in sorted(profile.transit_routes, key=lambda item: (item.direction != "to_station", item.station)):
        direction = f"Zum {route.station}" if route.direction == "to_station" else f"Ab {route.station}"
        heading = (f'<strong>{escape(direction)}</strong> · '
                   f'<strong class="radar-listing__route-duration">{route.minutes} Min.</strong>')
        lines = []
        steps = []
        for leg in route.legs:
            mode = TRANSIT_MODE_LABELS.get(leg.mode, "ÖPNV")
            vehicle = f"{mode} {leg.line}" if leg.line else mode
            if leg.mode != "WALK":
                lines.append(leg.line or mode)
            instruction = f"Im selben Fahrzeug weiter mit {vehicle}" if leg.through_service else vehicle
            if leg.headsign:
                instruction += f" Richtung {leg.headsign}"
            if leg.minutes is not None:
                instruction += f" · {leg.minutes} Min."
            stops = " → ".join(filter(None, (leg.origin, leg.destination)))
            stop_label = f'<span class="radar-listing__leg-stops">{escape(stops)}</span>' if stops else ""
            steps.append(f'<li>{escape(instruction)}{stop_label}</li>')
        preview = f'<span class="radar-listing__route-preview">{escape(" → ".join(lines))}</span>' if lines else ""
        planned = ""
        if route.planned_for is not None:
            planned = (f'<p class="radar-listing__foot radar-listing__route-time">'
                       f'Berechnet für {_date(route.planned_for)} Uhr</p>')
        if steps:
            cards.append(f'<details class="radar-listing__route"><summary>{heading}{preview}</summary>'
                         f'{planned}<ol class="radar-listing__legs">{"".join(steps)}</ol></details>')
        else:
            cards.append(f'<div class="radar-listing__route">{heading}{planned}</div>')
    return ('<section class="radar-listing__section" aria-label="ÖPNV-Verbindungen">'
            '<span class="radar-listing__label">ÖPNV-Verbindungen</span>'
            '<p class="radar-listing__foot radar-listing__transit-time">Beispiel ab 08:00 Uhr (Mo–Fr) · inklusive Wartezeit und Fußweg</p>'
            f'<div class="radar-listing__routes">{"".join(cards)}</div></section>')


def _listing_card(
    profile: ListingProfile | None, *, title: str, platform: str, status: str, date_label: str,
    route: tuple[str, str] | None = None, note: str | None = None, map_key: str | None = None,
    credit_proof: CreditProof | None = None,
) -> None:
    if profile and profile.title:
        title = profile.title
    rent_label = RENT_LABELS.get(profile.rent_kind, "Miete") if profile else "Miete"
    facts = _fact(rent_label, _format_value(profile.rent_eur, "€") if profile else None)
    facts += _fact("Fläche", _format_value(profile.area_sqm, "m²") if profile else None)
    connections = _listing_connections(profile)
    if route and not connections:
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
    credit_proof_badge = (
        f'<span class="radar-listing__badge radar-listing__credit-proof-{credit_proof}">'
        f'{CREDIT_PROOF_LABELS[credit_proof]}</span>' if credit_proof else ""
    )
    photos = _listing_photos(profile.photo_urls) if profile else ""
    st.html(
        '<article class="radar-listing"><header>'
        f'<div class="radar-listing__meta"><span class="radar-listing__source">{escape(platform)}</span>'
        f'{provider}{recipient_badge}{badge}{credit_proof_badge}</div>'
        f'<h3>{escape(title)}</h3><p class="radar-listing__foot">{escape(date_label)}</p></header>'
        f'{connections}{photos}<div class="radar-listing__facts">{facts}</div>{"".join(sections)}</article>'
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
    row: Match, profile: ListingProfile | None, db_path: Path, hours: int, *,
    review_available: bool, reviewed_send: bool, attachment: ApplicationAttachmentInfo | None,
    credit_proof_required: bool,
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
    elif row.review_reason and row.review_reason not in {row.judge_reason, CREDIT_PROOF_REQUIRED_REASON}:
        st.text(row.review_reason)
    if row.message is None:
        return
    editable = (review_available and row.platform == "WG-Gesucht" and row.url is not None
                and row.state not in {"sent", "unconfirmed", "credit_proof_required"})
    can_rewrite = (editable or row.platform == "Kleinanzeigen") and row.state in {"draft", "held", "edited", "failed"}
    mistral_configured = (os.getenv("MUNICH_LLM_PROVIDER", "none").strip().lower() == "mistral"
                          and bool(os.getenv("MISTRAL_API_KEY", "").strip()))
    rewrite_enabled = can_rewrite and mistral_configured
    version = sha256(row.message.encode("utf-8")).hexdigest()
    preview_key = f"application-preview-{row.id}"
    saved_preview = st.session_state.get(preview_key)
    if saved_preview is not None and (saved_preview[0] != version or not rewrite_enabled):
        st.session_state.pop(preview_key)
        saved_preview = None
    _, revision, preview, focus_default = saved_preview or (version, 0, row.message, "")

    judge_prefix = "Gespeicherte Fassung · " if saved_preview else ""
    if row.judge_approved is False:
        st.warning(judge_prefix + (row.judge_reason or "Die Textprüfung hat diese Fassung zurückgehalten."))
    elif row.judge_approved is True:
        st.caption(judge_prefix + (row.judge_reason or "Diese Fassung hat die Textprüfung bestanden."))

    with st.expander("Bewerbung prüfen" if editable else "Bewerbung anzeigen", expanded=saved_preview is not None):
        if not editable:
            st.code(row.message, language=None, wrap_lines=True)
            if not rewrite_enabled:
                if can_rewrite:
                    st.caption("Zum Neuformulieren muss Mistral eingerichtet sein.")
                return

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
                may_approve = editable and reviewed_send and not credit_proof_required and row.fresh_at >= cutoff.timestamp()
                attachment_version = attachment.digest if attachment else "no-attachment"
                form_key = f"application-{row.id}-{version}-{attachment_version}-{revision}"
                if saved_preview:
                    if editable:
                        st.caption("Vorschlag · noch nicht gespeichert")
                    else:
                        st.caption("Vorschlag zum Kopieren")
                        st.code(preview, language=None, wrap_lines=True)
                with st.form(form_key, enter_to_submit=False):
                    message = (st.text_area("Deine Nachricht", value=preview, height=220, max_chars=6000)
                               if editable else preview)
                    focus = ""
                    rewrite = False
                    if rewrite_enabled:
                        focus = st.text_input("Was soll sich ändern? (optional)", value=focus_default,
                                              placeholder="Zum Beispiel: Den Einzug natürlicher formulieren",
                                              max_chars=MAX_REVISION_FOCUS_CHARS)
                        rewrite = st.form_submit_button("Entwurf neu formulieren")
                    elif can_rewrite:
                        st.caption("Zum Neuformulieren muss Mistral eingerichtet sein.")
                    save = st.form_submit_button("Entwurf speichern") if editable else False
                    approve = False
                    if may_approve:
                        if attachment:
                            st.text(f"Anhang · {attachment.name}")
                            approve = st.form_submit_button("Text und PDF zum Versand freigeben", type="primary")
                            st.caption("Text und PDF kommen zusammen in die Versandwarteschlange.")
                        else:
                            approve = st.form_submit_button("Zum Versand freigeben", type="primary")
                    elif editable and credit_proof_required:
                        st.caption("WG-Gesucht verlangt für diese Anzeige eine aktuelle Bonitätsauskunft.")
                    elif editable and reviewed_send:
                        st.caption("Das Zeitfenster für den Versand ist abgelaufen.")
                    elif editable:
                        st.caption("Der Versand nach Freigabe ist noch nicht eingerichtet.")
                if rewrite:
                    try:
                        with st.spinner("MietRadar formuliert den Entwurf neu …"):
                            suggestion = regenerate_application(
                                message, focus, listing_title=profile.title if profile else _headline(row.url),
                                recipient_role=application_recipient_role(
                                    profile.recipient_role if profile else None,
                                    commercial=bool(profile and profile.is_commercial),
                                ),
                            )
                    except ApplicationRevisionError as error:
                        st.error(str(error))
                        return
                    st.session_state[preview_key] = (version, revision + 1, suggestion, focus)
                    st.rerun()
                if not (save or approve):
                    return
                result = review_application(
                    db_path, row.id, row.message, message, approve=approve,
                    now=datetime.now(timezone.utc), max_age=timedelta(hours=hours),
                    attachment_digest=attachment.digest if approve and attachment else None,
                )
                if result == "queued":
                    notice = "Bewerbung freigegeben und in die Versandwarteschlange aufgenommen."
                    st.session_state[_SHOW_QUEUE] = True
                else:
                    notice = "Entwurf gespeichert."
        except ApplicationReviewError as error:
            st.error(str(error))
            return
        except (OSError, sqlite3.Error):
            st.error("Die Änderung konnte gerade nicht gespeichert werden. Versuch es nach dem Aktualisieren erneut.")
            return
        st.session_state.pop(preview_key, None)
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


def _reset_offer_pages() -> None:
    for view in ("current", "dated", "secondary"):
        st.session_state.pop(f"{view}_offer_page", None)


def _offer_date(row: Match | DatedListing | SecondaryListing, basis: DateBasis | None) -> tuple[date, float | None]:
    if basis == "first_seen":
        stamp, day = row.first_seen_at, None
    else:
        stamp, day = row.published_at, row.published_day
    if stamp is not None:
        return datetime.fromtimestamp(stamp, BERLIN).date(), stamp
    if day is not None:
        return day, None
    if isinstance(row, DatedListing):
        return row.day, None
    stamp = row.first_seen_at if row.first_seen_at is not None else row.fresh_at
    return datetime.fromtimestamp(stamp, BERLIN).date(), stamp


def _daily_offer_page(
    rows: Sequence[OfferRow], view: str, newest_first: bool, basis: DateBasis | None = None,
) -> Iterator[OfferRow]:
    direction = -1 if newest_first else 1

    def order(row: OfferRow) -> tuple[int, bool, float, str]:
        day, stamp = _offer_date(row, basis)
        return direction * day.toordinal(), stamp is None, direction * (stamp or 0), row.id

    page = _offer_page(sorted(rows, key=order), view)
    for day, offers in groupby(page, key=lambda row: _offer_date(row, basis)[0]):
        st.subheader(f"{WEEKDAYS[day.weekday()]}, {day:%d.%m.%Y}", anchor=False)
        yield from offers


def _offer_date_label(row: Match | DatedListing | SecondaryListing) -> str:
    if row.published_at is not None:
        posting = f"Veröffentlicht · {_date(row.published_at)}"
    elif row.published_day is not None:
        posting = f"{_posting_day(row.published_day)} · Uhrzeit unbekannt"
    else:
        posting = "Einstelldatum unbekannt"

    if row.first_seen_at is not None:
        return f"{posting} · Zuerst gesehen · {_date(row.first_seen_at)}"
    if not isinstance(row, DatedListing) and row.basis.casefold().startswith("suchalarm"):
        return f"{posting} · Suchalarm · {_date(row.fresh_at)}"
    return posting


def _application_status(row: Match) -> str:
    if row.judge_approved is False:
        return "Freigegeben · Text zurückgehalten" if row.state == "queued" else "Text zurückgehalten"
    if row.state == "queued" and row.review_reason:
        return "Freigegeben · Versand verzögert"
    return STATES.get(row.state, "Entwurf")


def _render_match(
    row: Match, profile: ListingProfile | None, db_path: Path, hours: int, *, view: str,
    review_available: bool, reviewed_send: bool, attachment: ApplicationAttachmentInfo | None,
    show_maps: bool, credit_proof: CreditProof | None,
) -> None:
    tier, route = _primary_route(profile)
    if row.state == "credit_proof_required":
        credit_proof = "required"
    with st.container(border=True):
        date_label = _offer_date_label(row)
        if row.basis == "zuerst gesehen" and row.message and row.state != "queued":
            can_send = review_available and reviewed_send and row.platform == "WG-Gesucht" and credit_proof != "required"
            date_label += " · Versand nur nach Freigabe" if can_send else " · nur als Entwurf"
        status = "Versand gestoppt" if row.state == "credit_proof_required" else _application_status(row)
        _listing_card(
            profile, title=_headline(row.url), platform=row.platform,
            status=f"{tier} · {status}", date_label=f"{date_label} · {row.id}", route=route,
            map_key=f"{view}_map_{row.id}" if show_maps and row.state != "unavailable" else None,
            credit_proof=credit_proof,
        )
        _offer_actions(db_path, row.id, profile.title if profile and profile.title else _headline(row.url),
                       row.url, view=view)
        if row.platform == "WG-Gesucht" and row.state in {"queued", "failed", "unconfirmed", "credit_proof_required"}:
            st.caption(f"Versanddetails findest du unter „{_QUEUE_TAB}“.")
        else:
            _application(row, profile, db_path, hours, review_available=review_available, reviewed_send=reviewed_send,
                         attachment=attachment, credit_proof_required=credit_proof == "required")


def _matches(
    rows: tuple[Match, ...], hours: int, wg_scan: tuple[float, int] | None, profiles: dict[str, ListingProfile],
    db_path: Path, *, newest_first: bool, review_available: bool, reviewed_send: bool,
    attachment: ApplicationAttachmentInfo | None, show_maps: bool, credit_proofs: dict[str, CreditProof],
) -> None:
    st.subheader("Neue Mietangebote", anchor=False)
    _scan_status(wg_scan)
    category = st.selectbox("ÖPNV-Kategorie", ("A* und A", "A*", "A"), key="current_offer_category",
                            on_change=_reset_offer_pages)
    if not rows:
        st.info(f"Keine passenden Angebote in den letzten {hours} Stunden.")
        return
    filtered = [row for row in rows if category == "A* und A" or _primary_route(profiles.get(row.id))[0] == category]
    if not filtered:
        st.info("Keine Angebote mit diesen Filtern.")
        return
    for row in _daily_offer_page(filtered, "current", newest_first):
        _render_match(row, profiles.get(row.id), db_path, hours, view="current", review_available=review_available,
                      reviewed_send=reviewed_send, attachment=attachment, show_maps=show_maps,
                      credit_proof=credit_proofs.get(row.id))


def _scan_status(wg_scan: tuple[float, int] | None) -> None:
    if wg_scan is not None:
        timestamp, count = wg_scan
        st.caption(f"Letzte WG-Gesucht-Suche: {_date(timestamp)} · Sichtbare Anzeigen erfasst: {count}")


def _date_filter(selected: str, today: date) -> tuple[DateBasis | None, tuple[date, date] | None]:
    if selected == "current":
        return None, None
    basis: DateBasis = st.radio(
        "Datum filtern nach", ("published", "first_seen"), horizontal=True, key="offer_date_basis",
        format_func={"published": "Veröffentlicht", "first_seen": "Zuerst gesehen (Einstelldatum fehlt)"}.__getitem__,
        on_change=_reset_offer_pages,
    )
    if selected == "today":
        return basis, (today, today)
    if selected == "yesterday":
        yesterday = today - timedelta(days=1)
        return basis, (yesterday, yesterday)
    if selected == "week":
        return basis, (today - timedelta(days=6), today)
    start, end = weekend_dates(today)
    if selected == "weekend":
        return basis, (start, min(end, today))
    days = st.date_input("Von – bis", (start, min(end, today)), max_value=today, format="DD.MM.YYYY",
                         key="offer_custom_dates", on_change=_reset_offer_pages)
    return basis, days if isinstance(days, tuple) and len(days) == 2 else None


def _dated_offers(
    rows: tuple[DatedListing, ...], days: tuple[date, date] | None, basis: DateBasis,
    wg_scan: tuple[float, int] | None, profiles: dict[str, ListingProfile], db_path: Path,
    *, newest_first: bool, current_matches: dict[str, Match], hours: int, review_available: bool,
    reviewed_send: bool, attachment: ApplicationAttachmentInfo | None, show_maps: bool,
    credit_proofs: dict[str, CreditProof],
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
    category = st.selectbox("ÖPNV-Kategorie", ("Alle", "A*", "A"), key="dated_offer_category",
                            on_change=_reset_offer_pages)
    filtered = []
    for row in rows:
        profile = profiles.get(row.id)
        tier = profile.route_tier.upper() if profile and profile.route_tier else None
        if category == "Alle" or (row.matched and tier == category):
            filtered.append(row)
    if not filtered:
        if not rows and not sightings:
            st.info("Keine Angebote mit bestätigtem Einstelldatum für diese Filter. Wähle „Zuerst gesehen (Einstelldatum fehlt)“, "
                    "um stattdessen nach der ersten Erfassung durch MietRadar zu filtern.")
        else:
            st.info("Keine Angebote mit diesen Filtern.")
        return
    if len(rows) == RESULT_LIMIT:
        st.caption(f"Anzeige auf {RESULT_LIMIT} Ergebnisse begrenzt.")
    for row in _daily_offer_page(filtered, "dated", newest_first, basis):
        profile = profiles.get(row.id)
        if match := current_matches.get(row.id):
            _render_match(match, profile, db_path, hours, view="dated", review_available=review_available,
                          reviewed_send=reviewed_send, attachment=attachment, show_maps=show_maps,
                          credit_proof=credit_proofs.get(row.id))
            continue
        with st.container(border=True):
            status = "Filter erfüllt" if row.matched else "Erfasst"
            route = None
            if row.matched and profile and profile.route_tier in {"a*", "a"}:
                tier, route = _primary_route(profile)
                status = f"{tier} · {status}"
            _listing_card(profile, title=_headline(row.url), platform=row.platform,
                          status=status, date_label=_offer_date_label(row), route=route,
                          map_key=f"dated_map_{row.id}" if show_maps else None,
                          credit_proof=credit_proofs.get(row.id))
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
    rows: tuple[SecondaryListing, ...], profiles: dict[str, ListingProfile], db_path: Path, *,
    newest_first: bool, basis: DateBasis | None, credit_proofs: dict[str, CreditProof],
) -> None:
    st.caption("Bis zu 45 Min. mit dem ÖPNV ab Ostbahnhof für B oder ab Hauptbahnhof für C. Passt beides, gilt B.")
    category = st.selectbox("ÖPNV-Kategorie", ("B und C", "B", "C"), key="secondary_offer_category",
                            on_change=_reset_offer_pages)
    filtered = [row for row in rows if category == "B und C" or row.tier.upper() == category]
    if not filtered:
        st.info("Keine Angebote mit diesen Filtern.")
        return
    for row in _daily_offer_page(filtered, "secondary", newest_first, basis):
        station = "Hauptbahnhof" if row.station == "Hbf" else row.station
        with st.container(border=True):
            profile = profiles.get(row.id)
            title = (profile.title if profile else None) or row.title or _headline(row.url)
            _listing_card(profile, title=title, platform=row.platform, status=f"Kategorie {row.tier.upper()}",
                          date_label=_offer_date_label(row), route=(f"Ab {station}", f"{row.minutes} Min."),
                          map_key=f"secondary_map_{row.id}", credit_proof=credit_proofs.get(row.id))
            _offer_actions(db_path, row.id, title, row.url, view="secondary")


def _hidden_offers(rows: tuple[HiddenListing, ...], db_path: Path, credit_proofs: dict[str, CreditProof]) -> None:
    st.subheader("Ausgeblendete Angebote", anchor=False)
    if not rows:
        st.info("Du hast noch keine Angebote ausgeblendet.")
        return
    for row in rows:
        with st.container(border=True):
            st.text(row.title)
            if credit_proof := credit_proofs.get(row.id):
                st.badge(CREDIT_PROOF_LABELS[credit_proof], color="red" if credit_proof == "required" else "gray")
            st.caption(f"{row.platform} · Ausgeblendet am {_date(row.hidden_at)}")
            _offer_actions(db_path, row.id, row.title, row.url, view="hidden", hidden=True)


def _application_queue(
    rows: tuple[QueuedApplication, ...], profiles: dict[str, ListingProfile], db_path: Path, hours: int, *,
    review_available: bool, reviewed_send: bool, attachment: ApplicationAttachmentInfo | None,
    wg_on_mac: bool, wg_inbox_status: tuple[float, bool] | None, credit_proofs: dict[str, CreditProof],
) -> None:
    if wg_on_mac:
        if wg_inbox_status is None:
            st.caption("Vom Mac liegt noch kein Postfachabgleich vor.")
        else:
            checked_at, available = wg_inbox_status
            st.caption(f"Letzter Postfachabgleich vom Mac · {_date(checked_at)}")
            if not available:
                st.warning("Das WG-Gesucht-Postfach auf dem Mac ist gerade nicht erreichbar.")
    if not rows:
        st.info("Keine Bewerbungen in der Versandwarteschlange.")
        return
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=min(hours, 24))).timestamp()
    for item in rows:
        row = item.application
        credit_proof = credit_proofs.get(row.id)
        with st.container(border=True):
            st.text(item.title or _headline(row.url))
            if item.relay_claimed:
                st.badge("Vom Mac übernommen", color="blue")
                date_label = "Versand begonnen"
            else:
                status = _application_status(row) if row.state == "queued" else STATES[row.state]
                color = _QUEUE_COLORS[row.state]
                if row.state == "queued" and row.fresh_at < cutoff:
                    status, color = "Versandfenster abgelaufen", "orange"
                st.badge(status, color=color)
                date_label = "Freigabe" if row.state == "queued" else "Stand"
            if credit_proof and row.state != "credit_proof_required":
                st.badge(CREDIT_PROOF_LABELS[credit_proof], color="red" if credit_proof == "required" else "gray")
            st.caption(f"{row.platform} · {date_label} {_date(item.updated_at)}")
            _location_link(row.url)
            if item.relay_claimed:
                if row.message:
                    with st.expander("Freigegebenen Text anzeigen"):
                        st.code(row.message, language=None, wrap_lines=True)
                continue
            _application(row, profiles.get(row.id), db_path, hours, review_available=review_available,
                         reviewed_send=reviewed_send, attachment=attachment,
                         credit_proof_required=credit_proof == "required")


def _sent_applications(rows: tuple[SentApplication, ...], *, has_queue: bool) -> None:
    st.subheader("Gesendete Bewerbungen", anchor=False)
    if not rows:
        st.info("MietRadar hat noch keinen bestätigten Versand erfasst.")
        if has_queue:
            st.caption(f"Freigaben und ihre Versanddetails findest du unter „{_QUEUE_TAB}“.")
        return
    for row in rows:
        with st.container(border=True):
            title = row.title or _headline(row.url)
            if title == "Mietangebot":
                title = f"Mietangebot {row.id.removeprefix('ka-')}"
            st.text(title)
            st.caption(f"{row.platform} · Versand bestätigt am {_date(row.sent_at)}")
            _location_link(row.url)


def _replies(
    rows: tuple[Reply, ...], *, wg_enabled: bool, wg_status: tuple[float, bool] | None, wg_on_mac: bool,
) -> None:
    st.subheader("Antwortentwürfe", anchor=False)
    if wg_enabled:
        if wg_status is None:
            st.caption("MietRadar hat das WG-Gesucht-Postfach noch nicht geprüft.")
        elif wg_on_mac and datetime.now(timezone.utc).timestamp() - wg_status[0] > 20 * 60:
            st.info("MietRadar hat das Postfach auf dem Mac länger nicht geprüft. Prüfe, ob der Mac wach und der Versanddienst aktiv ist.")
        elif not wg_status[1]:
            location = "im MietRadar-Browser auf deinem Mac" if wg_on_mac else "auf Railway"
            st.info(f"MietRadar kann das WG-Gesucht-Postfach gerade nicht lesen. Prüfe die WG-Gesucht-Anmeldung {location}.")
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
        st.info("Noch keine Aktivität.")
        return
    for row in rows:
        with st.container(border=True):
            st.text(row.subject)
            st.caption(f"{row.listing_id or 'MietRadar'} · {_date(row.occurred_at)}")
            st.text(row.summary)
            _location_link(row.url, "Auf der Plattform öffnen")


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
        provider = st.selectbox("Anbietertyp", tuple(PROVIDER_FILTER_LABELS),
                                format_func=PROVIDER_FILTER_LABELS.__getitem__, on_change=_reset_offer_pages)
        st.caption("ÖPNV-Daten · [Transitous und Datenquellen](https://transitous.org/sources/)")
        logout_button()

    today = now.astimezone(BERLIN).date()
    periods = {
        "current": f"Letzte {hours} Stunden", "today": "Heute", "yesterday": "Gestern",
        "weekend": "Dieses Wochenende" if today.weekday() >= 5 else "Letztes Wochenende",
        "week": "Letzte 7 Tage", "custom": "Eigener Zeitraum",
    }
    period_column, platform_column, sort_column = st.columns(3)
    selected_period = period_column.selectbox("Zeitraum", tuple(periods), format_func=periods.__getitem__,
                                              key="offer_period", on_change=_reset_offer_pages)
    platform = platform_column.selectbox("Plattform", ("Alle", "WG-Gesucht", "Kleinanzeigen"),
                                         key="offer_platform", on_change=_reset_offer_pages)
    newest_first = sort_column.selectbox("Sortierung", ("Neueste zuerst", "Älteste zuerst"),
                                          key="offer_order", on_change=_reset_offer_pages) == "Neueste zuerst"
    date_basis, date_window = _date_filter(selected_period, today)
    only_matches = False
    if date_basis is not None:
        only_matches = st.checkbox("Nur passende Angebote", value=True, key="offer_only_matches",
                                   help="Zeigt Treffer der ÖPNV-Kategorien A*, A, B und C.",
                                   on_change=_reset_offer_pages)

    db_path = directory / "munich_radar.sqlite3"
    data = load_dashboard(db_path, now=now, max_age_hours=hours,
                          posting_window=date_window, date_basis=date_basis or "published", newest_first=newest_first,
                          only_matches=only_matches)
    if data.status == "missing":
        st.info("MietRadar hat hier noch keine Datenbank angelegt. Starte den Worker für den ersten Suchlauf.")
        return
    if data.status != "ready":
        st.warning("Die MietRadar-Daten konnten gerade nicht gelesen werden. Aktualisiere die Seite in Kürze.")
        return
    data = filter_listing_providers(data, provider)
    credit_proofs = data.credit_proofs
    current_rows = tuple(row for row in data.matches if platform == "Alle" or row.platform == platform)
    dated_rows = tuple(row for row in data.dated_listings if platform == "Alle" or row.platform == platform)
    secondary_rows = data.secondary_offers if date_basis is None else data.dated_secondary_offers
    secondary_rows = tuple(row for row in secondary_rows if platform == "Alle" or row.platform == platform)
    if notice := st.session_state.pop(_REVIEW_NOTICE, None):
        st.success(notice)
    if st.session_state.pop(_SHOW_QUEUE, False):
        st.session_state[_TAB_KEY] = _QUEUE_TAB
    if visibility_notice := st.session_state.pop(_VISIBILITY_NOTICE, None):
        success, message = visibility_notice
        if success:
            st.toast(message)
        else:
            st.error(message)

    matches, drafts, pending = st.columns(3)
    if date_basis:
        label = "Zuerst erfasste Angebote" if date_basis == "first_seen" else "Veröffentlichte Angebote"
        matches.metric(label, len(dated_rows), border=True)
    else:
        matches.metric("Neue Angebote", len(current_rows), border=True)
    drafts.metric("Bewerbungsentwürfe", sum(bool(row.message) for row in current_rows), border=True)
    pending.metric("Adressen zur Prüfung", len(data.address_checks), border=True)
    offers, queue, applications, map_tab, secondary, address, replies, activity, hidden = st.tabs(
        ["Angebote", _QUEUE_TAB, "Bewerbungen", "Karte", "B & C", "Adresse prüfen", "Antwortentwürfe", "Verlauf",
         "Ausgeblendet"], key=_TAB_KEY, on_change="rerun",
    )
    attachment = None
    reviewed_send = os.getenv("MUNICH_REVIEWED_SEND", "").strip().lower() == "true"
    if reviewed_send and data.application_reviews_available:
        try:
            attachment = load_application_attachment(db_path)
        except ApplicationReviewError as error:
            st.warning(str(error))
            reviewed_send = False
        else:
            with offers:
                _credit_proof(db_path, attachment)
    with offers:
        if date_basis:
            _dated_offers(dated_rows, date_window, date_basis, data.wg_scan, data.listing_profiles, db_path,
                          newest_first=newest_first, current_matches={row.id: row for row in current_rows}, hours=hours,
                          review_available=data.application_reviews_available, reviewed_send=reviewed_send,
                          attachment=attachment, show_maps=bool(offers.open),
                          credit_proofs=credit_proofs)
        else:
            _matches(current_rows, hours, data.wg_scan, data.listing_profiles, db_path, newest_first=newest_first,
                     review_available=data.application_reviews_available,
                     reviewed_send=reviewed_send, attachment=attachment, show_maps=bool(offers.open),
                     credit_proofs=credit_proofs)
    wg_on_mac = bool(os.getenv("MUNICH_WG_RELAY_TOKEN", "").strip())
    if queue.open:
        with queue:
            _application_queue(data.application_queue, data.listing_profiles, db_path, hours,
                               review_available=data.application_reviews_available, reviewed_send=reviewed_send,
                               attachment=attachment, wg_on_mac=wg_on_mac, wg_inbox_status=data.wg_inbox_status,
                               credit_proofs=credit_proofs)
    if applications.open:
        with applications:
            _sent_applications(data.sent_applications, has_queue=bool(data.application_queue))
    if map_tab.open:
        with map_tab:
            map_rows = map_listings(data, date_filtered=date_basis is not None)
            _listing_map(tuple(row for row in map_rows if platform == "Alle" or row.platform == platform), db_path)
    if secondary.open:
        with secondary:
            _secondary_offers(secondary_rows, data.listing_profiles, db_path, newest_first=newest_first, basis=date_basis,
                              credit_proofs=credit_proofs)
    with address:
        _addresses(data.address_checks, data.listing_profiles, db_path)
    with replies:
        _replies(data.replies, wg_enabled=os.getenv("MUNICH_WG_REPLY_DRAFTS", "").strip().lower() == "true",
                 wg_status=data.wg_inbox_status, wg_on_mac=wg_on_mac)
    with activity:
        _activity(data.activity)
    if hidden.open:
        with hidden:
            _hidden_offers(data.hidden_listings, db_path, credit_proofs)


if __name__ == "__main__":
    main()
