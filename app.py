"""Products Analyzer — Streamlit UI (manual/file product checks).

All business logic (ROI, gating, Keepa client, ranking) lives in core.py and is
shared with the headless automation pipeline (automation/mto_pipeline.py).
This file is only the interactive interface.
"""

import io
import json
import os

import pandas as pd
import requests
import streamlit as st

import core

st.set_page_config(page_title="Products Analyzer", page_icon="🔍", layout="wide",
                   initial_sidebar_state="expanded")
st.title("Products Analyzer")
st.caption("Check products by EAN — enter them manually or upload a file. "
           "Live Keepa lookup, ROI + gating per market (CA / UK / JP). "
           "The automated email→Slack pipeline is the *MTO Analyzer*.")

# ─── CONFIG: Load / Save ──────────────────────────────────────────────────────

APP_DIR = os.path.dirname(__file__)
CONFIG_FILE = os.path.join(APP_DIR, "config.json")
CACHE_FILE = os.path.join(APP_DIR, "keepa_cache.json")
MATRIX_FILE = os.path.join(APP_DIR, "brand_matrix.csv")

DEFAULTS = {
    **core.DEFAULT_PARAMS,
    "keepa_key": "", "cache_hours": 24,
    "auto_rates": True, "skip_hard_gated": True, "buybox": True,
}


def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            saved = json.load(f)
        return {**DEFAULTS, **saved}
    return DEFAULTS.copy()


def save_config():
    data = {k: st.session_state[k] for k in DEFAULTS if k in st.session_state}
    with open(CONFIG_FILE, "w") as f:
        json.dump(data, f, indent=2)


RATE_KEYS = ("eur_gbp", "eur_usd", "usd_cad", "eur_jpy")


@st.cache_data(ttl=3600)
def _fetch_rates_cached(shape):
    """`shape` is part of the cache key: when the set of rates we need changes,
    previously cached results (which lack the new keys) are discarded instead of
    being served with a missing key."""
    return core.fetch_live_rates()


def cached_live_rates():
    live = _fetch_rates_cached(",".join(RATE_KEYS))
    if not live or any(k not in live for k in RATE_KEYS):
        return None          # unusable payload — fall back to manual values
    return live


def get_keepa_key():
    """Streamlit Cloud secrets take priority; sidebar input is the local fallback."""
    try:
        if "keepa_key" in st.secrets and st.secrets["keepa_key"]:
            return st.secrets["keepa_key"]
    except Exception:
        pass
    return st.session_state.get("keepa_key", "")


@st.cache_data(ttl=60, show_spinner=False)
def load_matrix_df():
    return pd.read_csv(MATRIX_FILE, dtype=str).fillna("")


cfg = load_config()

# ─── SIDEBAR: Parameters ──────────────────────────────────────────────────────

with st.sidebar:
    page = st.radio("Page", ["🔍 Analyzer", "🏷️ Brand Matrix"], label_visibility="collapsed")
    st.markdown("---")
    st.header("Parameters")

    with st.expander("Keepa API", expanded=not cfg["keepa_key"]):
        st.text_input("API key", value=cfg["keepa_key"], type="password", key="keepa_key")
        st.number_input("Cache lookups for (hours)", value=int(cfg["cache_hours"]),
                        min_value=0, max_value=168, step=1, key="cache_hours")
        st.checkbox("Skip Keepa lookups for hard-gated markets (saves tokens)",
                    value=bool(cfg["skip_hard_gated"]), key="skip_hard_gated")
        st.checkbox("Request Buy Box prices (3 tokens/product instead of 1)",
                    value=bool(cfg.get("buybox", True)), key="buybox",
                    help="Buy Box is the accurate sell-price proxy. Off = cheaper, "
                         "uses the lowest-NEW-offer average instead.")

    with st.expander("Exchange Rates", expanded=True):
        live = cached_live_rates()
        st.checkbox("Auto-use live ECB rates", value=bool(cfg["auto_rates"]), key="auto_rates",
                    help="When on, every analysis uses the current ECB rate. "
                         "Turn off to use the manual values below.")
        if live:
            st.caption(f"Live ECB ({live.get('date', '?')}): JPY {live.get('eur_jpy')} · "
                       f"GBP {live.get('eur_gbp')} · USD {live.get('eur_usd')} · "
                       f"CAD/USD {live.get('usd_cad')}")
        else:
            st.caption("⚠️ Live rates unavailable — manual values below are used.")
        st.number_input("EUR → GBP", value=cfg["eur_gbp"], step=0.001, format="%.4f", key="eur_gbp")
        st.number_input("EUR → USD", value=cfg["eur_usd"], step=0.001, format="%.4f", key="eur_usd")
        st.number_input("USD → CAD", value=cfg["usd_cad"], step=0.001, format="%.4f", key="usd_cad")
        st.number_input("EUR → JPY", value=cfg["eur_jpy"], step=0.5, format="%.2f", key="eur_jpy")
    st.number_input("Digital Svc Fee (%)", value=cfg["dsf"], step=0.5, format="%.1f", key="dsf")

    st.markdown("---")

    with st.expander("🇬🇧  UK Parameters", expanded=True):
        st.number_input("Shipping / unit (EUR)", value=cfg["uk_ship"], step=0.10, format="%.2f", key="uk_ship")
        st.number_input("Labor / unit (EUR)",    value=cfg["uk_lab"],  step=0.10, format="%.2f", key="uk_lab")
        st.number_input("FBA fee (GBP)",         value=cfg["uk_fba"],  step=0.01, format="%.2f", key="uk_fba")
        st.number_input("Referral fee (%)",      value=cfg["uk_ref"],  step=0.5,  format="%.1f", key="uk_ref")
        st.number_input("VAT rate (%)",          value=cfg["uk_vat"],  step=0.5,  format="%.1f", key="uk_vat")

    with st.expander("🇨🇦  CA Parameters", expanded=True):
        st.number_input("Shipping / unit (EUR)", value=cfg["ca_ship"], step=0.10, format="%.2f", key="ca_ship")
        st.number_input("Labor / unit (EUR)",    value=cfg["ca_lab"],  step=0.10, format="%.2f", key="ca_lab")
        st.number_input("FBA fee (CAD)",         value=cfg["ca_fba"],  step=0.01, format="%.2f", key="ca_fba")
        st.number_input("Referral fee (%)",      value=cfg["ca_ref"],  step=0.5,  format="%.1f", key="ca_ref")

    with st.expander("🇯🇵  JP Parameters", expanded=True):
        st.caption("One all-in additional cost per unit (shipping, 3PL, FBA, duties), "
                   "split by dangerous goods (EDT/EDP/perfume/aerosol) vs not.")
        st.number_input("Additional cost DG (EUR)",  value=cfg["jp_add_dg"],  step=0.10, format="%.2f", key="jp_add_dg")
        st.number_input("Additional cost NDG (EUR)", value=cfg["jp_add_ndg"], step=0.10, format="%.2f", key="jp_add_ndg")
        st.number_input("Referral fee (%)",          value=cfg["jp_ref"],     step=0.1,  format="%.1f", key="jp_ref")
        st.number_input("Digital svc fee (% of referral)", value=cfg["jp_dsf"], step=0.1, format="%.1f", key="jp_dsf")
        st.number_input("Consumption tax (%)",       value=cfg["jp_vat"],     step=0.5,  format="%.1f", key="jp_vat")

    st.markdown("---")
    if st.button("💾 Save Parameters", use_container_width=True):
        save_config()
        st.success("Saved!")


def effective_params():
    """Parameters used for ROI: manual sidebar values, with live ECB rates on
    top when auto mode is enabled and the feed is reachable."""
    P = {k: st.session_state.get(k, cfg[k]) for k in core.DEFAULT_PARAMS}
    live = cached_live_rates()
    if st.session_state.get("auto_rates", True) and live:
        P.update({k: live[k] for k in RATE_KEYS})
        P["_rates_source"] = f"live ECB {live.get('date', '?')}"
    else:
        P["_rates_source"] = "manual"
    return P


# ─── PAGE: BRAND MATRIX EDITOR ────────────────────────────────────────────────

if page == "🏷️ Brand Matrix":
    st.subheader("Brand gating matrix")
    st.caption("ok = we can sell · has path to apply = gated but can apply · Hard Gated = can't sell · "
               "empty = to be checked. Add new brands in the last row.")
    try:
        matrix_df = load_matrix_df()
    except Exception as e:
        st.error(f"Could not load brand matrix: {e}")
        st.stop()

    edited_df = st.data_editor(
        matrix_df,
        num_rows="dynamic",
        use_container_width=True,
        height=600,
        column_config={
            "Brand": st.column_config.TextColumn("Brand", required=True),
            **{m: st.column_config.SelectboxColumn(m, options=core.STATUS_OPTIONS, required=False)
               for m in core.MATRIX_MARKETS},
            "Notes": st.column_config.TextColumn("Notes", width="large"),
        },
    )

    if st.button("💾 Save matrix", type="primary"):
        clean = edited_df.fillna("")
        clean = clean[clean["Brand"].astype(str).str.strip() != ""]
        dupes = clean["Brand"].astype(str).str.strip().str.casefold().duplicated()
        if dupes.any():
            st.error(f"Duplicate brand name(s): {', '.join(clean.loc[dupes, 'Brand'].unique())} — merge them first.")
        else:
            clean.to_csv(MATRIX_FILE, index=False)
            load_matrix_df.clear()
            st.success(f"Saved — {len(clean)} brands.")

    st.download_button("⬇️ Download matrix (.csv)", data=edited_df.fillna("").to_csv(index=False),
                       file_name="brand_matrix.csv", mime="text/csv")
    st.caption("Note: on the cloud app, saved edits last until Streamlit restarts the app "
               "(then it reverts to the repo copy). Download a backup after big edit sessions.")
    st.stop()

# ─── PAGE: ANALYZER ───────────────────────────────────────────────────────────

input_mode = st.radio("Input", ["📄 Upload file", "⌨️ Enter manually"],
                      horizontal=True, label_visibility="collapsed")

items = []
skipped = 0

if input_mode == "📄 Upload file":
    uploaded = st.file_uploader("Upload file with EAN, Title, Purchase price EUR", type=["xlsx", "csv"])

    if not uploaded:
        st.info("Upload an .xlsx or .csv with columns: EAN, Title, Purchase price (EUR) — "
                "and optionally Brand. Column names are detected automatically.")
        st.stop()

    try:
        if uploaded.name.lower().endswith(".csv"):
            items, skipped, cols = core.items_from_dataframe(pd.read_csv(uploaded))
            sheet_report = None
        else:
            items, skipped, sheet_report = core.items_from_excel(uploaded.read())
            cols = None
    except ValueError as e:
        st.error(str(e))
        st.stop()
    except Exception as e:
        st.error(f"Could not read file: {e}")
        st.stop()

    if cols:
        st.caption(f"Detected columns — EAN: **{cols['ean']}**, Title: **{cols['title'] or '(none)'}**, "
                   f"Purchase price EUR: **{cols['price']}**, Brand: **{cols['brand'] or '(none — will use Keepa)'}**")
    if sheet_report:
        st.caption("Sheets parsed — " + " · ".join(f"**{k}**: {v}" for k, v in sheet_report.items()))
    source_id = uploaded.name
else:
    st.caption("Add products below (EAN and Purchase price required). "
               "Brand is used for the gating check — if left empty, Keepa's brand is used.")
    manual_df = st.data_editor(
        pd.DataFrame([{"Brand": "", "EAN": "", "Purchase price EUR": None}]),
        num_rows="dynamic",
        use_container_width=True,
        key="manual_input",
        column_config={
            "Brand": st.column_config.TextColumn("Brand"),
            "EAN": st.column_config.TextColumn("EAN", required=True),
            "Purchase price EUR": st.column_config.NumberColumn("Purchase price EUR",
                                                                min_value=0.0, format="%.2f"),
        },
    )
    seen = set()
    for _, r in manual_df.iterrows():
        brand = str(r.get("Brand") or "").strip()
        ean_raw = str(r.get("EAN") or "").strip()
        price = pd.to_numeric(r.get("Purchase price EUR"), errors="coerce")
        if not brand and not ean_raw and pd.isna(price):
            continue                     # untouched empty row
        ean = core.normalize_ean(ean_raw)
        if ean is None or pd.isna(price):
            skipped += 1
            continue
        if ean in seen:
            continue
        seen.add(ean)
        items.append({"ean": ean, "title": "", "price_eur": float(price), "brand": brand})
    source_id = "manual entry"

if skipped:
    st.warning(f"{skipped} row(s) skipped — missing/invalid EAN or price.")
if not items:
    if input_mode == "⌨️ Enter manually":
        st.info("Fill in at least one row with EAN and purchase price.")
    else:
        st.error("No valid rows found.")
    st.stop()

st.markdown(f"**{len(items)} unique products** ready. "
            f"Estimated Keepa cost: up to **{len(items) * len(core.KEEPA_DOMAINS)} tokens** "
            "(less with cache and gating skips).")

try:
    matrix_df = load_matrix_df()
    matrix_error = None
except Exception as e:
    matrix_df = None
    matrix_error = str(e)
if matrix_error:
    st.warning(f"Could not load Brand Matrix — gating shown as 'to be checked'. ({matrix_error})")

if st.button("🔍 Fetch from Keepa & Analyze", type="primary"):
    keepa_api_key = get_keepa_key()
    if not keepa_api_key:
        st.error("No Keepa API key found — add it in the sidebar (local) "
                 "or in Streamlit Cloud → App settings → Secrets as keepa_key = \"...\".")
        st.stop()

    with st.status("Fetching from Keepa…", expanded=False) as status:
        try:
            res = core.analyze(
                items, keepa_api_key,
                params=effective_params(),
                matrix_df=matrix_df,
                cache_path=CACHE_FILE,
                cache_hours=st.session_state["cache_hours"],
                progress=lambda msg: status.update(label=msg),
                skip_hard_gated=st.session_state.get("skip_hard_gated", True),
                buybox=st.session_state.get("buybox", True),
            )
        except (requests.HTTPError, RuntimeError) as e:
            st.error(f"Keepa error: {e} — check your API key and token balance.")
            st.stop()
        status.update(label="Keepa fetch complete", state="complete")

    st.session_state["market_data"] = res["market_data"]
    st.session_state["skipped_pairs"] = res["skipped_pairs"]
    st.session_state["tokens_left"] = res["tokens_left"]
    st.session_state["result_file"] = source_id

if "market_data" not in st.session_state:
    st.stop()

if st.session_state.get("result_file") != source_id:
    st.warning("Results below are from a previously fetched file — click the button above to re-fetch.")
if st.session_state.get("tokens_left") is not None:
    st.caption(f"Keepa tokens left: {st.session_state['tokens_left']}")

# Re-built on every rerun so sidebar parameter changes update ROI instantly
P = effective_params()
result_df = core.build_result_df(items, st.session_state["market_data"],
                                 core.matrix_from_df(matrix_df), P,
                                 st.session_state.get("skipped_pairs"))

for _line in core.status_summary_lines(result_df):
    st.markdown(f"#### {_line}")

n_found = result_df[[f"ROI {m}" for m in core.MARKETS]].notna().any(axis=1).sum()
st.markdown(f"**{len(result_df)} products** ({n_found} found on Keepa) — "
            f"sellable brands first, then by ROI CA → UK → JP · rates: {P['_rates_source']}")

display_df = result_df.copy()
for _m in core.MARKETS:
    display_df[f"ROI {_m}"] = display_df[f"ROI {_m}"].apply(core.fmt_roi)


st.dataframe(display_df, use_container_width=True, hide_index=True)

buf = io.BytesIO()
result_df.to_excel(buf, index=False, engine="openpyxl")
st.download_button("⬇️ Download results (.xlsx)", data=buf.getvalue(),
                   file_name="products_analysis.xlsx",
                   mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
