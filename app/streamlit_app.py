"""Entry point of the Streamlit app (plan 13). Phase 2 ships the Market page; the deal radar, valuation and model
health pages follow with the model (Phases 3 and 4).

    uv run --group app streamlit run app/streamlit_app.py
"""

import market
import streamlit as st

st.set_page_config(page_title="iPhone Price Radar", page_icon="📱", layout="wide")
market.render()
st.caption(
    "Source code and method: [github.com/pnidatunca/iphone-price-radar](https://github.com/pnidatunca/iphone-price-radar)."
    " The free hosting puts the app to sleep when nobody visits; the first visit then takes a moment."
)
