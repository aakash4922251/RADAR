"""Bits shared by the public app.py and pages/01_Internal_Dashboard.py — each
Streamlit page is its own script, so this holds only what would otherwise be
copy-pasted between them."""
from __future__ import annotations

import os

import streamlit as st

from db import database as db


def get_setting(name: str) -> str | None:
    """An environment variable, or (if unset) the matching top-level key in
    Streamlit's secrets.toml — so a secret set either way is found, whether
    or not this Streamlit version promotes secrets to os.environ itself."""
    value = os.environ.get(name)
    if value:
        return value
    try:
        return st.secrets.get(name)
    except Exception:
        return None


def close_page_connections():
    """Roll back and close every connection this page run opened. Anything
    left open would hold SQLite's write lock and block other sessions."""
    for conn in st.session_state.get("_db_connections", []):
        try:
            conn.rollback()
            conn.close()
        except Exception:
            pass
    st.session_state["_db_connections"] = []


def get_cursor():
    conn = db.get_connection()
    st.session_state.setdefault("_db_connections", []).append(conn)
    return conn.cursor(), conn


def init_page():
    """Call once at the top of each page: creates tables if needed and
    releases whatever a run cut short by st.rerun() or an error left open."""
    db.init_db()
    close_page_connections()
