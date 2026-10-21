"""Streamlit review UI — Phase 1 placeholder.

The four review pages (FR-074) are built in Phase 6. This page exists only so
that `docker compose up` brings up a working UI service.
"""

from __future__ import annotations

import streamlit as st

from core.settings import get_settings

settings = get_settings()

st.set_page_config(page_title=settings.app_name, page_icon="📉", layout="wide")

st.title("MetricSentinel")
st.caption("Autonomous KPI anomaly detection and root-cause investigation.")

st.info(
    "**Phase 1 — skeleton.** No KPI, anomaly, investigation or report exists yet. "
    "The review pages are built in Phase 6.",
    icon="🧱",
)

st.subheader("Environment")
st.json(
    {
        "app_version": settings.app_version,
        "app_env": settings.app_env,
        "api": f"http://{settings.api_host}:{settings.api_port}",
        "offline_mode": settings.offline_mode,
    }
)
