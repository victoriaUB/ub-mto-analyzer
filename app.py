import json
import os
import requests
import pandas as pd
import streamlit as st

st.set_page_config(page_title="MTO Batch Analyzer", page_icon="🔍", layout="wide",
                   initial_sidebar_state="expanded")
st.title("MTO Batch Analyzer")
st.caption("Batch ROI check across markets — upload a Keepa Processed file to start")

# ─── CONFIG: Load / Save ──────────────────────────────────────────────────────

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "config.json")

DEFAULTS = {
    "eur_gbp": 0.867, "eur_usd": 1.170, "usd_cad": 1.369,
    "dsf":     3.0,
    "uk_ship": 0.85,  "uk_lab": 2.58,  "uk_fba": 3.09, "uk_ref": 15.0, "uk_vat": 20.0,
    "ca_ship": 3.57,  "ca_lab": 2.58,  "ca_fba": 7.33, "ca_ref": 15.0,
}

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            saved = json.load(f)
        return {**DEFAULTS, **saved}
    return DEFAULTS.copy()

def save_config():
    data = {k: st.session_state[k] for k in DEFAULTS}
    with open(CONFIG_FILE, "w") as f:
        json.dump(data, f, indent=2)

def save_config_from_dict(updates):
    current = load_config()
    current.update(updates)
    with open(CONFIG_FILE, "w") as f:
        json.dump(current, f, indent=2)

@st.cache_data(ttl=3600)
def fetch_live_rates():
    try:
        r = requests.get(
            "https://api.frankfurter.app/latest?from=EUR&to=GBP,USD,CAD",
            timeout=5,
        )
        data = r.json()
        rates = data["rates"]
        return {
            "eur_gbp": round(rates["GBP"], 4),
            "eur_usd": round(rates["USD"], 4),
            "usd_cad": round(rates["CAD"] / rates["USD"], 4),
            "date": data.get("date", "unknown"),
        }
    except Exception:
        return None

cfg = load_config()

# ─── SIDEBAR: Parameters ──────────────────────────────────────────────────────

with st.sidebar:
    st.header("Parameters")

    with st.expander("Exchange Rates", expanded=True):
        live = fetch_live_rates()
        if live:
            st.caption(f"Live rates available (ECB, {live['date']})")
            if st.button("🔄 Use live rates", use_container_width=True):
                save_config_from_dict({k: live[k] for k in ["eur_gbp", "eur_usd", "usd_cad"]})
                st.rerun()
        eur_gbp = st.number_input("EUR → GBP", value=cfg["eur_gbp"], step=0.001, format="%.4f", key="eur_gbp")
        eur_usd = st.number_input("EUR → USD", value=cfg["eur_usd"], step=0.001, format="%.4f", key="eur_usd")
        usd_cad = st.number_input("USD → CAD", value=cfg["usd_cad"], step=0.001, format="%.4f", key="usd_cad")
    dsf_rate = st.number_input("Digital Svc Fee (%)", value=cfg["dsf"], step=0.5, format="%.1f", key="dsf") / 100

    st.markdown("---")

    with st.expander("🇬🇧  UK Parameters", expanded=True):
        uk_shipping = st.number_input("Shipping / unit (EUR)", value=cfg["uk_ship"], step=0.10, format="%.2f", key="uk_ship")
        uk_labor    = st.number_input("Labor / unit (EUR)",    value=cfg["uk_lab"],  step=0.10, format="%.2f", key="uk_lab")
        fba_gbp     = st.number_input("FBA fee (GBP)",         value=cfg["uk_fba"],  step=0.01, format="%.2f", key="uk_fba")
        ref_uk      = st.number_input("Referral fee (%)",      value=cfg["uk_ref"],  step=0.5,  format="%.1f", key="uk_ref") / 100
        uk_vat      = st.number_input("VAT rate (%)",          value=cfg["uk_vat"],  step=0.5,  format="%.1f", key="uk_vat") / 100

    with st.expander("🇨🇦  CA Parameters", expanded=True):
        ca_shipping = st.number_input("Shipping / unit (EUR)", value=cfg["ca_ship"], step=0.10, format="%.2f", key="ca_ship")
        ca_labor    = st.number_input("Labor / unit (EUR)",    value=cfg["ca_lab"],  step=0.10, format="%.2f", key="ca_lab")
        fba_cad     = st.number_input("FBA fee (CAD)",         value=cfg["ca_fba"],  step=0.01, format="%.2f", key="ca_fba")
        ref_ca      = st.number_input("Referral fee (%)",      value=cfg["ca_ref"],  step=0.5,  format="%.1f", key="ca_ref") / 100

    st.markdown("---")
    if st.button("💾 Save Parameters", use_container_width=True):
        save_config()
        st.success("Saved!")

# ─── CALCULATION FUNCTIONS ────────────────────────────────────────────────────

def calc_uk(p_eur, s_gbp):
    p    = p_eur * eur_gbp
    sl   = (uk_shipping + uk_labor) * eur_gbp
    cogs = p + sl
    s    = s_gbp / (1 + uk_vat)
    ref  = s * ref_uk
    ppu  = s - cogs - fba_gbp - ref
    roi  = ppu / cogs if cogs > 0 else 0
    return roi

def calc_ca(p_eur, s_cad):
    cad_usd  = 1 / usd_cad
    p_usd    = p_eur * eur_usd
    ship_usd = ca_shipping * eur_usd
    lab_usd  = ca_labor * eur_usd
    cogs     = p_usd + ship_usd + lab_usd
    sell_usd = s_cad * cad_usd
    fba_usd  = fba_cad * cad_usd
    ref      = sell_usd * ref_ca
    dsf      = (ref + fba_usd) * dsf_rate
    fees     = ref + fba_usd + dsf
    ppu      = sell_usd - cogs - fees
    roi      = ppu / cogs if cogs > 0 else 0
    return roi

def fmt_roi(val):
    if pd.isna(val):
        return "—"
    icon = "🟢" if val >= 20 else ("🟡" if val >= 10 else ("🟠" if val > 0 else "🔴"))
    return f"{icon} {val:.1f}%"

# ─── MAIN: FILE UPLOAD & RESULTS ──────────────────────────────────────────────

uploaded = st.file_uploader("Upload Excel file (.xlsx)", type=["xlsx"])

if uploaded:
    try:
        df = pd.read_excel(uploaded, sheet_name="Matching Analysis", engine="openpyxl")
    except Exception as e:
        st.error(f"Could not read 'Matching Analysis' sheet: {e}")
        df = None

    if df is not None:
        df.columns = [str(c).strip() for c in df.columns]

        df = df[df["Market"].isin(["DIPTYQUE UK", "DIPTYQUE CA"])].copy()
        df["_market"] = df["Market"].str.replace("DIPTYQUE ", "", regex=False)
        df = df.dropna(subset=["Buy Box: 90d avg (local currency)", "Purchase price EUR (offer)"])

        rows = []
        for ean, group in df.groupby("EAN"):
            desc         = group["Offer Description"].iloc[0]
            purchase_val = float(group["Purchase price EUR (offer)"].iloc[0])

            row = {
                "Product":        desc,
                "EAN":            str(ean),
                "Purchase (EUR)": round(purchase_val, 2),
            }

            uk_g = group[group["_market"] == "UK"]
            ca_g = group[group["_market"] == "CA"]

            if not uk_g.empty:
                sell_val = float(uk_g["Buy Box: 90d avg (local currency)"].iloc[0])
                rank_val = uk_g["Sales Rank: 30d avg"].iloc[0]
                row["Sell UK (GBP)"] = round(sell_val, 2)
                row["Rank UK"]       = int(rank_val) if pd.notna(rank_val) else None
                row["ROI UK"]        = round(calc_uk(purchase_val, sell_val) * 100, 1)
            else:
                row["Sell UK (GBP)"] = None
                row["Rank UK"]       = None
                row["ROI UK"]        = None

            if not ca_g.empty:
                sell_val = float(ca_g["Buy Box: 90d avg (local currency)"].iloc[0])
                rank_val = ca_g["Sales Rank: 30d avg"].iloc[0]
                row["Sell CA (CAD)"] = round(sell_val, 2)
                row["Rank CA"]       = int(rank_val) if pd.notna(rank_val) else None
                row["ROI CA"]        = round(calc_ca(purchase_val, sell_val) * 100, 1)
            else:
                row["Sell CA (CAD)"] = None
                row["Rank CA"]       = None
                row["ROI CA"]        = None

            rows.append(row)

        result_df = pd.DataFrame(rows)

        for col in ["Rank UK", "Rank CA"]:
            result_df[col] = pd.array(result_df[col], dtype=pd.Int64Dtype())

        result_df["_best"] = result_df[["ROI UK", "ROI CA"]].max(axis=1)
        result_df = result_df.sort_values("_best", ascending=False).drop(columns=["_best"]).reset_index(drop=True)

        st.markdown(f"**{len(result_df)} products** — sorted by best ROI")

        display_df = result_df.copy()
        display_df["ROI UK"] = display_df["ROI UK"].apply(fmt_roi)
        display_df["ROI CA"] = display_df["ROI CA"].apply(fmt_roi)

        st.dataframe(display_df, use_container_width=True, hide_index=True)
else:
    st.info("Upload a Keepa Processed Excel file above to see batch ROI results.")
