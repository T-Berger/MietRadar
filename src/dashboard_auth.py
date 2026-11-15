"""Password access for the private MietRadar dashboard."""

import hmac
import os
import secrets
from math import ceil
from time import monotonic

import streamlit as st


PASSWORD_ENV = "MUNICH_DASHBOARD_PASSWORD"
_SESSION_TICKET = "mietradar_dashboard_ticket"
_FAILED_ATTEMPTS = "mietradar_dashboard_failed_attempts"
_RETRY_AFTER = "mietradar_dashboard_retry_after"
_SIGNING_KEY = secrets.token_bytes(32)
_ATTEMPT_LIMIT = 5
_COOLDOWN_SECONDS = 30


def require_authentication() -> None:
    """Stop this Streamlit run until the current session has signed in."""
    password = os.environ.get(PASSWORD_ENV, "")
    if not password.strip():
        st.warning("Der Zugang zum Dashboard ist noch nicht eingerichtet.")
        st.stop()

    password_bytes = password.encode("utf-8")
    ticket = hmac.digest(_SIGNING_KEY, password_bytes, "sha256")
    session = st.session_state
    previous_ticket = session.get(_SESSION_TICKET)
    if isinstance(previous_ticket, bytes) and hmac.compare_digest(previous_ticket, ticket):
        return
    session.pop(_SESSION_TICKET, None)

    st.subheader("Anmelden")
    retry_after = session.get(_RETRY_AFTER, 0)
    remaining = ceil(retry_after - monotonic())
    if remaining > 0:
        st.error(f"Zu viele Anmeldeversuche. Bitte warte noch {remaining} Sekunden.")
        st.button("Erneut versuchen", key="mietradar_dashboard_retry")
        st.stop()
    if retry_after:
        session.pop(_RETRY_AFTER, None)
        session.pop(_FAILED_ATTEMPTS, None)

    with st.form("mietradar_dashboard_login", clear_on_submit=True):
        entered = st.text_input("Passwort", type="password")
        submitted = st.form_submit_button("Anmelden", type="primary")

    if submitted:
        if hmac.compare_digest(entered.encode("utf-8"), password_bytes):
            session[_SESSION_TICKET] = ticket
            session.pop(_FAILED_ATTEMPTS, None)
            st.rerun()

        failures = session.get(_FAILED_ATTEMPTS, 0) + 1
        session[_FAILED_ATTEMPTS] = failures
        if failures >= _ATTEMPT_LIMIT:
            session[_RETRY_AFTER] = monotonic() + _COOLDOWN_SECONDS
            st.rerun()
        st.error("Das Passwort ist falsch.")

    st.stop()


def logout_button() -> None:
    """Render the logout button after the authenticated session check."""
    if st.sidebar.button("Abmelden", key="mietradar_dashboard_logout"):
        st.session_state.pop(_SESSION_TICKET, None)
        st.rerun()
