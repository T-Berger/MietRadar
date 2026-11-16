"""Password-protected dashboard for MietRadar's Munich listings and drafts."""

from __future__ import annotations

import os
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from html import escape
from math import isfinite, log2
from pathlib import Path
from urllib.parse import unquote, urlsplit

import streamlit as st
from dotenv import load_dotenv

from dashboard_auth import logout_button, require_authentication
from dashboard_data import (
    BERLIN, RESULT_LIMIT, WG_REPLY_INBOX, AddressCheck, DateBasis, DatedListing, ListingProfile, MapListing, Match,
    ProviderFilter, Reply, SecondaryListing, filter_listing_providers, load_dashboard, map_listings, weekend_dates,
)
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
SMALL_WORDS = {"am", "an", "auf", "für", "im", "in", "mit", "und", "von", "zu", "zur"}
WEEKDAYS = ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag")
RENT_LABELS = {"warm": "Warmmiete", "cold": "Kaltmiete", "total": "Gesamtmiete"}
FIT_LABELS = {"strong": "Passt gut", "possible": "Könnte passen", "unclear": "Noch unklar"}
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
  .radar-listing__photos { display: grid; gap: .55rem; }
  .radar-listing__photo { position: relative; display: block; overflow: hidden; border-radius: .7rem;
    background: rgba(78, 133, 123, .12); }
  .radar-listing__photo::before { content: 'Inseratsfoto'; position: absolute; inset: 0; display: grid;
    place-items: center; color: var(--text-color); opacity: .75; font-size: .85rem; }
  .radar-listing__photo img { position: relative; display: block; width: 100%; aspect-ratio: 16 / 9;
    object-fit: cover; font-size: 0; }
  .radar-listing__cover img { aspect-ratio: 2 / 1; max-height: 15rem; }
  .radar-listing__gallery summary { cursor: pointer; font-size: .88rem; font-weight: 650; }
  .radar-listing__gallery[open] summary { margin-bottom: .6rem; }
  .radar-listing__thumbnails { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(9rem, 100%), 1fr)); gap: .55rem; }
  @media (max-width: 600px) { .radar-listing__cover img { aspect-ratio: 4 / 3; } }
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
    provider = "Gewerblich" if row.is_commercial else None
    return " · ".join(filter(None, (row.platform, provider, f"{route} {direction}", rent, area)))


def _listing_map(rows: tuple[MapListing, ...]) -> None:
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
            "style": "white-bg",
            "center": {"lat": (min(latitudes) + max(latitudes)) / 2, "lon": (min(longitudes) + max(longitudes)) / 2},
            "zoom": min(11.4, log2(360 / extent) - 0.7),
            "layers": [{"sourcetype": "raster", "source": ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
                        "below": "traces", "sourceattribution": "© OpenStreetMap contributors"}],
        },
    )
    event = st.plotly_chart(
        fig, key="mietradar_offers_map", on_select="rerun", selection_mode="points",
        config={"displaylogo": False, "modeBarButtonsToRemove": ["toImage"]},
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
        st.caption(_map_facts(pins[chosen]))
        _location_link(pins[chosen].url)


def _listing_photos(photo_urls: tuple[str, ...]) -> str:
    if not photo_urls:
        return ""

    links = []
    for position, url in enumerate(photo_urls, start=1):
        source = escape(url, quote=True)
        decoration = " radar-listing__cover" if position == 1 else ""
        links.append(
            f'<a class="radar-listing__photo{decoration}" href="{source}" target="_blank" '
            f'rel="noopener noreferrer" aria-label="Inseratsfoto {position} in voller Größe öffnen">'
            f'<img src="{source}" alt="Inseratsfoto {position}" loading="lazy" decoding="async" '
            'referrerpolicy="no-referrer"></a>'
        )
    gallery = ""
    if len(links) > 1:
        gallery = ('<details class="radar-listing__gallery"><summary>Weitere Fotos</summary>'
                   f'<div class="radar-listing__thumbnails">{"".join(links[1:])}</div></details>')
    return f'<div class="radar-listing__photos">{links[0]}{gallery}</div>'


def _listing_card(
    profile: ListingProfile | None, *, title: str, platform: str, status: str, date_label: str,
    route: tuple[str, str] | None = None, note: str | None = None,
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
    provider = '<span class="radar-listing__badge">Gewerblich</span>' if profile and profile.is_commercial else ""
    photos = _listing_photos(profile.photo_urls) if profile else ""
    st.html(
        '<article class="radar-listing"><header>'
        f'<div class="radar-listing__meta"><span class="radar-listing__source">{escape(platform)}</span>'
        f'{provider}{badge}</div>'
        f'<h3>{escape(title)}</h3></header>{photos}<div class="radar-listing__facts">{facts}</div>'
        f'{"".join(sections)}<p class="radar-listing__foot">{escape(date_label)}</p></article>'
    )


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
    elif row.review_reason and row.review_reason != row.judge_reason:
        st.text(row.review_reason)
    if row.message is None:
        return
    if row.judge_approved is False:
        st.warning(row.judge_reason or "Die Textprüfung hat diese Fassung zurückgehalten.")
    elif row.judge_approved is True:
        st.caption(row.judge_reason or "Diese Fassung hat die Textprüfung bestanden.")

    editable = review_available and row.platform == "WG-Gesucht" and row.url is not None
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


def _matches(
    rows: tuple[Match, ...], hours: int, wg_scan: tuple[float, int] | None, profiles: dict[str, ListingProfile],
    db_path: Path, *, review_available: bool, reviewed_send: bool, attachment: ApplicationAttachmentInfo | None,
) -> None:
    st.subheader("Neue Mietangebote", anchor=False)
    _scan_status(wg_scan)
    platforms = sorted({row.platform for row in rows})
    if not rows:
        st.info(f"Keine passenden Angebote in den letzten {hours} Stunden.")
        return
    platform = st.selectbox("Plattform", ["Alle", *platforms]) if len(platforms) > 1 else "Alle"
    category = st.selectbox("ÖPNV-Kategorie", ["A* und A", "A*", "A"])
    shown = False
    for row in rows:
        if platform != "Alle" and row.platform != platform:
            continue
        profile = profiles.get(row.id)
        tier, route = _primary_route(profile)
        if category not in ("A* und A", tier):
            continue
        shown = True
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
            )
            _location_link(row.url)
            _application(row, db_path, hours, review_available=review_available, reviewed_send=reviewed_send,
                         attachment=attachment)
    if not shown:
        st.info("Keine Angebote mit diesen Filtern.")


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
    wg_scan: tuple[float, int] | None, profiles: dict[str, ListingProfile], *, provider_filtered: bool = False,
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
    for row in rows:
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
                          status=status, date_label=date_label, route=route)
            _location_link(row.url)


def _addresses(rows: tuple[AddressCheck, ...], profiles: dict[str, ListingProfile]) -> None:
    st.subheader("Adresse prüfen", anchor=False)
    if not rows:
        st.info("Keine Kleinanzeigen-Angebote zur Ortsprüfung.")
        return
    for row in rows:
        with st.container(border=True):
            _listing_card(profiles.get(row.id), title=row.title or _headline(row.url), platform="Kleinanzeigen",
                          status="Adresse prüfen", date_label=f"{_date(row.fresh_at)} · {row.id}",
                          note=row.reason)
            _location_link(row.url)


def _secondary_offers(rows: tuple[SecondaryListing, ...], hours: int, profiles: dict[str, ListingProfile]) -> None:
    st.caption("Bis zu 45 Min. mit dem ÖPNV ab Ostbahnhof für B oder ab Hauptbahnhof für C. Passt beides, gilt B.")
    if not rows:
        st.info(f"Keine Angebote der Kategorie B oder C in den letzten {hours} Stunden.")
        return
    for tier in ("b", "c"):
        offers = [row for row in rows if row.tier == tier]
        if not offers:
            continue
        st.subheader(f"Kategorie {tier.upper()}", anchor=False)
        for row in offers:
            station = "Hauptbahnhof" if row.station == "Hbf" else row.station
            with st.container(border=True):
                _listing_card(profiles.get(row.id), title=row.title or _headline(row.url), platform=row.platform,
                              status="", date_label=f"{row.basis.capitalize()} · {_date(row.fresh_at)}",
                              route=(f"Ab {station}", f"{row.minutes} Min."))
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

    matches, drafts, pending = st.columns(3)
    if date_basis:
        label = "Zuerst erfasste Angebote" if date_basis == "first_seen" else "Veröffentlichte Angebote"
        matches.metric(label, len(data.dated_listings), border=True)
    else:
        matches.metric("Neue Angebote", len(data.matches), border=True)
    drafts.metric("Bewerbungsentwürfe", sum(bool(row.message) for row in data.matches), border=True)
    pending.metric("Adressen zur Prüfung", len(data.address_checks), border=True)
    offers, map_tab, secondary, address, replies, activity = st.tabs(
        ["Angebote", "Karte", "B & C", "Adresse prüfen", "Antwortentwürfe", "Verlauf"],
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
            _dated_offers(data.dated_listings, date_window, date_basis, data.wg_scan, data.listing_profiles,
                          provider_filtered=provider != "all")
        else:
            _matches(data.matches, hours, data.wg_scan, data.listing_profiles, db_path,
                     review_available=data.application_reviews_available,
                     reviewed_send=reviewed_send, attachment=attachment)
    if map_tab.open:
        with map_tab:
            _listing_map(map_listings(data, date_filtered=date_basis is not None))
    with secondary:
        _secondary_offers(data.secondary_offers, hours, data.listing_profiles)
    with address:
        _addresses(data.address_checks, data.listing_profiles)
    with replies:
        _replies(data.replies, wg_enabled=os.getenv("MUNICH_WG_REPLY_DRAFTS", "").strip().lower() == "true",
                 wg_status=data.wg_inbox_status)
    with activity:
        _activity(data.activity)


if __name__ == "__main__":
    main()
