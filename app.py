import json
import os
import requests
import streamlit as st

st.set_page_config(page_title="UB Cost Calculator", page_icon="🐱", layout="wide",
                   initial_sidebar_state="expanded")
st.title("UB Cost Calculator")
st.caption("UK · AU · CA — side by side")

# ─── CONFIG: Load / Save ──────────────────────────────────────────────────────

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "config.json")

DEFAULTS = {
    "eur_gbp": 0.867, "eur_aud": 1.634, "eur_usd": 1.170, "usd_cad": 1.369,
    "dsf":     3.0,
    "uk_ship": 0.85,  "uk_lab": 2.58,  "uk_fba": 3.09, "uk_ref": 15.0, "uk_vat": 20.0,
    "au_ship": 10.40, "au_lab": 2.58,  "au_fba": 7.30, "au_ref": 13.0, "au_gst": 10.0, "au_tar": 5.0,
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
            "https://api.frankfurter.app/latest?from=EUR&to=GBP,AUD,USD,CAD",
            timeout=5,
        )
        data = r.json()
        rates = data["rates"]
        return {
            "eur_gbp": round(rates["GBP"], 4),
            "eur_aud": round(rates["AUD"], 4),
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
                save_config_from_dict({k: live[k] for k in ["eur_gbp", "eur_aud", "eur_usd", "usd_cad"]})
                st.rerun()
        eur_gbp = st.number_input("EUR → GBP", value=cfg["eur_gbp"], step=0.001, format="%.4f", key="eur_gbp")
        eur_aud = st.number_input("EUR → AUD", value=cfg["eur_aud"], step=0.001, format="%.4f", key="eur_aud")
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
        st.caption("Referral applied to sell ex-VAT. VAT not in COGS.")

    with st.expander("🇦🇺  AU Parameters", expanded=True):
        au_shipping = st.number_input("Shipping / unit (EUR)", value=cfg["au_ship"], step=0.10, format="%.2f", key="au_ship")
        au_labor    = st.number_input("Labor / unit (EUR)",    value=cfg["au_lab"],  step=0.10, format="%.2f", key="au_lab")
        fba_aud     = st.number_input("FBA fee (AUD)",         value=cfg["au_fba"],  step=0.01, format="%.2f", key="au_fba")
        ref_au      = st.number_input("Referral fee (%)",      value=cfg["au_ref"],  step=0.5,  format="%.1f", key="au_ref") / 100
        au_gst      = st.number_input("GST rate (%)",          value=cfg["au_gst"],  step=0.5,  format="%.1f", key="au_gst") / 100
        au_tariff   = st.number_input("Import tariff (%)",     value=cfg["au_tar"],  step=0.5,  format="%.1f", key="au_tar") / 100
        st.caption("Import tariff in COGS. Import GST not in COGS (reclaimable). Referral applied to sell ex-GST.")

    with st.expander("🇨🇦  CA Parameters", expanded=True):
        ca_shipping = st.number_input("Shipping / unit (EUR)", value=cfg["ca_ship"], step=0.10, format="%.2f", key="ca_ship")
        ca_labor    = st.number_input("Labor / unit (EUR)",    value=cfg["ca_lab"],  step=0.10, format="%.2f", key="ca_lab")
        fba_cad     = st.number_input("FBA fee (CAD)",         value=cfg["ca_fba"],  step=0.01, format="%.2f", key="ca_fba")
        ref_ca      = st.number_input("Referral fee (%)",      value=cfg["ca_ref"],  step=0.5,  format="%.1f", key="ca_ref") / 100
        st.caption("All results in USD. Sell price (CAD) and FBA (CAD) converted to USD internally.")

    st.markdown("---")
    if st.button("💾 Save Parameters", use_container_width=True):
        save_config()
        st.success("Saved! Will load automatically next time.")

# ─── PRODUCT INPUT ────────────────────────────────────────────────────────────

st.subheader("Product")
c1, c2, c3, c4 = st.columns([1.2, 1, 1, 1])

with c1:
    purchase_eur = st.number_input(
        "Purchase price (EUR)",
        min_value=0.0, value=0.00, step=0.50, format="%.2f"
    )
with c2:
    sell_gbp = st.number_input(
        "Sell UK (GBP, inc VAT)",
        min_value=0.0, value=0.0, step=1.0, format="%.2f",
        help="Amazon UK listing price — VAT stripped using rate set in parameters"
    )
with c3:
    sell_aud = st.number_input(
        "Sell AU (AUD, inc GST)",
        min_value=0.0, value=0.0, step=1.0, format="%.2f",
        help="Amazon AU listing price — GST stripped using rate set in parameters"
    )
with c4:
    sell_cad = st.number_input(
        "Sell CA (CAD)",
        min_value=0.0, value=0.0, step=1.0, format="%.2f",
        help="CAD listing price — converted to USD internally for all calculations"
    )

# ─── CALCULATION FUNCTIONS ────────────────────────────────────────────────────

def calc_uk(p_eur, s_gbp):
    p    = p_eur * eur_gbp
    sl   = (uk_shipping + uk_labor) * eur_gbp
    cogs = p + sl
    s    = s_gbp / (1 + uk_vat)
    ref  = s * ref_uk
    dsf  = (ref + fba_gbp) * dsf_rate
    # UK spreadsheet: PPU = sell_ex_vat - COGS - FBA - Referral (DSF shown but not deducted from PPU)
    ppu  = s - cogs - fba_gbp - ref
    roi  = ppu / cogs if cogs > 0 else 0
    return dict(cur="GBP", purchase=p, ship_labor=sl, tariff_gst=None,
                cogs=cogs, sell_ex=s, ref=ref, fba=fba_gbp, dsf=dsf,
                fees=ref + fba_gbp + dsf, ppu=ppu, roi=roi,
                tax_note=f"VAT {uk_vat:.0%} stripped from sell price")

def calc_au(p_eur, s_aud):
    p        = p_eur * eur_aud
    ship_aud = au_shipping * eur_aud
    lab_aud  = au_labor * eur_aud
    tariff   = (p + ship_aud) * au_tariff
    # Import GST is NOT in COGS — it is reclaimable as input tax credit
    cogs     = p + ship_aud + lab_aud + tariff
    s        = s_aud / (1 + au_gst)
    ref      = s * ref_au
    dsf      = (ref + fba_aud) * dsf_rate
    fees     = ref + fba_aud + dsf
    ppu      = s - cogs - fees
    roi      = ppu / cogs if cogs > 0 else 0
    return dict(cur="AUD", purchase=p, ship_labor=ship_aud + lab_aud,
                tariff_gst=tariff,
                cogs=cogs, sell_ex=s, ref=ref, fba=fba_aud, dsf=dsf,
                fees=fees, ppu=ppu, roi=roi,
                tax_note=f"Import tariff {au_tariff:.0%} in COGS; GST {au_gst:.0%} stripped from sell price only")

def calc_ca(p_eur, s_cad):
    # Everything in USD
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
    return dict(cur="USD", purchase=p_usd, ship_labor=ship_usd + lab_usd, tariff_gst=None,
                cogs=cogs, cogs_cad=cogs * usd_cad, sell_ex=sell_usd, ref=ref, fba=fba_usd, dsf=dsf,
                fees=fees, ppu=ppu, roi=roi,
                tax_note=f"Sell price {s_cad:.2f} CAD → {sell_usd:.2f} USD. All values in USD.")

uk = calc_uk(purchase_eur, sell_gbp)
au = calc_au(purchase_eur, sell_aud)
ca = calc_ca(purchase_eur, sell_cad)

# ─── RESULTS ──────────────────────────────────────────────────────────────────

st.divider()
st.subheader("Results")

def roi_icon(roi):
    if roi >= 0.20: return "🟢"
    if roi >= 0.10: return "🟡"
    if roi >  0:    return "🟠"
    return "🔴"

def render_market(title, d, has_sell):
    st.markdown(f"**{title}**")
    if not has_sell:
        st.caption("Enter a sell price above")
        return
    c    = d["cur"]
    roi  = d["roi"]
    icon = roi_icon(roi)

    st.metric("ROI", f"{roi:.1%}")
    m1, m2 = st.columns(2)
    m1.metric(f"Profit ({c})", f"{d['ppu']:.2f}")
    m2.metric(f"COGS ({c})",   f"{d['cogs']:.2f}")
    st.caption(d["tax_note"])

    with st.expander("Full breakdown"):
        lines = [
            (f"Purchase ({c})",         d["purchase"]),
            (f"Shipping + Labor ({c})", d["ship_labor"]),
        ]
        if d["tariff_gst"] is not None:
            lines.append((f"Import tariff ({c})", d["tariff_gst"]))
        lines += [
            (f"**COGS ({c})**",         d["cogs"]),
        ]
        if d.get("cogs_cad") is not None:
            lines.append(("COGS (CAD)", d["cogs_cad"]))
        lines += [
            ("---", None),
            (f"Sell ex-tax ({c})",      d["sell_ex"]),
            (f"Referral fee ({c})",     d["ref"]),
            (f"FBA fee ({c})",          d["fba"]),
            (f"Digital svc fee ({c})",  d["dsf"]),
            (f"**Total fees ({c})**",   d["fees"]),
            ("---", None),
            (f"**Profit / PPU ({c})**", d["ppu"]),
            ("**ROI**",                 roi),
        ]
        for label, val in lines:
            if val is None:
                st.markdown("---")
            elif label == "**ROI**":
                st.markdown(f"**ROI: {val:.1%}** {icon}")
            elif label.startswith("**"):
                st.markdown(f"{label}: **{val:.2f}**")
            else:
                st.write(f"{label}: {val:.2f}")

col1, col2, col3 = st.columns(3)
with col1:
    render_market("🇬🇧  United Kingdom", uk, sell_gbp > 0)
with col2:
    render_market("🇦🇺  Australia",      au, sell_aud > 0)
with col3:
    render_market("🇨🇦  Canada",         ca, sell_cad > 0)
