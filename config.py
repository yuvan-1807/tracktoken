"""Settings come from environment variables first, then Streamlit secrets.

Locally: put them in .streamlit/secrets.toml (see README).
On Streamlit Community Cloud: paste them into the app's Secrets box.
"""
import os


def get_setting(name, default=""):
    value = os.getenv(name)
    if value and value.strip():
        return value.strip()
    try:
        import streamlit as st

        if name in st.secrets:
            return str(st.secrets[name]).strip()
    except Exception:
        pass
    return default
