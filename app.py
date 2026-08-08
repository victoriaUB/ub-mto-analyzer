import io
import json
import os
import time
import unicodedata

import requests
import pandas as pd
import streamlit as st

st.set_page_config(page_title="MTO Batch Analyzer", page_icon="🔍", layout="wide",
                   initial_sidebar_state="expanded")
st.title("MTO Batch Analyzer")
st.caption("Upload a file with EAN / Title / Purchase price EUR — live Keepa lookup, ROI per market")

# ─── CONFIG: Load / Save ──────────────────────────────────────────────────────

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "config.json")
CACHE_FILE = os.path.join(os.path.dirname(__file__), "keepa_cache.json")

DEFAULTS = {
    "eur_gbp": 0.867, "eur_usd": 1.170, "usd_cad": 1.369,
    "dsf":     3.0,
    "uk_ship": 0.85,  "uk_lab": 2.58,  "uk_fba": 3.09, "uk_ref": 15.0, "uk_vat": 20.0,
    "ca_ship": 3.57,  "ca_lab": 2.58,  "ca_fba": 7.33, "ca_ref": 15.0,
    "keepa_key": "",  "cache_hours": 24,
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
    page = st.radio("Page", ["🔍 Analyzer", "🏷️ Brand Matrix"], label_visibility="collapsed")
    st.markdown("---")
    st.header("Parameters")

    with st.expander("Keepa API", expanded=not cfg["keepa_key"]):
        keepa_key = st.text_input("API key", value=cfg["keepa_key"], type="password", key="keepa_key")
        cache_hours = st.number_input("Cache lookups for (hours)", value=int(cfg["cache_hours"]),
                                      min_value=0, max_value=168, step=1, key="cache_hours")

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

# ─── ROI CALCULATIONS ─────────────────────────────────────────────────────────

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

# ─── BRAND GATING MATRIX (brand_matrix.csv next to the app, like config.json) ─

MATRIX_LOCAL = os.path.join(os.path.dirname(__file__), "brand_matrix.csv")
MATRIX_MARKETS = ["US", "CA", "UK", "AU", "JP"]
STATUS_OPTIONS = ["", "ok", "has path to apply", "Hard Gated"]

GATE_OK, GATE_APPLY, GATE_CHECK, GATE_HARD = 0, 1, 2, 3
GATE_LABELS = {GATE_OK: "✅ OK", GATE_APPLY: "🟠 Gated — can apply",
               GATE_CHECK: "❓ To be checked", GATE_HARD: "🚫 Hard gated"}
GATE_UNKNOWN = "❓ Gating status to be checked"

def classify_gating(text):
    tl = str(text).strip().lower() if text is not None else ""
    if "hard" in tl:
        return GATE_HARD
    if tl in ("ok", "ungated") or tl.startswith("ungated"):
        return GATE_OK
    if "apply" in tl or "gated" in tl:
        return GATE_APPLY
    return GATE_CHECK

def norm_brand(s):
    return "".join(ch for ch in unicodedata.normalize("NFKD", str(s)).casefold()
                   if ch.isalnum())

@st.cache_data(ttl=60, show_spinner=False)
def load_matrix_df():
    return pd.read_csv(MATRIX_LOCAL, dtype=str).fillna("")

def save_matrix_df(df):
    df.to_csv(MATRIX_LOCAL, index=False)

def matrix_from_df(df):
    """{normalized brand: {'display', 'note', market: rank}}"""
    matrix = {}
    for _, r in df.iterrows():
        brand = str(r.get("Brand", "")).strip()
        if not brand:
            continue
        entry = {"display": brand, "note": str(r.get("Notes", "")).strip()}
        for market in MATRIX_MARKETS:
            entry[market] = classify_gating(r.get(market, ""))
        matrix[norm_brand(brand)] = entry
    return matrix

def gating_for_brand(matrix, brand):
    """Exact normalized match first, then substring either way (min 4 chars)."""
    if not brand:
        return None
    nb = norm_brand(brand)
    if not nb:
        return None
    if nb in matrix:
        return matrix[nb]
    for key, entry in matrix.items():
        if len(key) >= 4 and len(nb) >= 4 and (key in nb or nb in key):
            return entry
    return None

# ─── KEEPA CLIENT ─────────────────────────────────────────────────────────────

def get_keepa_key():
    """Streamlit Cloud secrets take priority; sidebar input is the local fallback."""
    try:
        if "keepa_key" in st.secrets and st.secrets["keepa_key"]:
            return st.secrets["keepa_key"]
    except Exception:
        pass
    return st.session_state.get("keepa_key", "")

KEEPA_DOMAINS = {"UK": 2, "CA": 6}
IDX_SALES_RANK = 3      # stats array index: sales rank
IDX_NEW = 1             # stats array index: NEW price
IDX_BUY_BOX = 18        # stats array index: buy box incl. shipping
BATCH_SIZE = 100

def load_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE) as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_cache(cache):
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f)

def stat_price(stats, arr_name, idx):
    arr = (stats or {}).get(arr_name) or []
    if idx < len(arr) and arr[idx] is not None and arr[idx] > 0:
        return arr[idx] / 100.0
    return None

def stat_rank(stats, arr_name, idx):
    arr = (stats or {}).get(arr_name) or []
    if idx < len(arr) and arr[idx] is not None and arr[idx] > 0:
        return int(arr[idx])
    return None

def extract_product(p):
    """Reduce a Keepa product object to the fields we need."""
    stats = p.get("stats") or {}
    buybox = stat_price(stats, "avg90", IDX_BUY_BOX)
    new_avg = stat_price(stats, "avg90", IDX_NEW)
    return {
        "asin": p.get("asin"),
        "title": p.get("title"),
        "brand": p.get("brand"),
        "eans": p.get("eanList") or [],
        "buybox90": buybox,
        "new90": new_avg,
        "rank30": stat_rank(stats, "avg30", IDX_SALES_RANK),
    }

def keepa_request(key, domain, codes, status):
    """One /product call for up to 100 EANs. Waits for token refill on 429."""
    url = "https://api.keepa.com/product"
    params = {
        "key": key,
        "domain": domain,
        "code": ",".join(codes),
        "stats": 90,
        "history": 0,
    }
    for attempt in range(12):
        r = requests.get(url, params=params, timeout=60)
        if r.status_code == 429:
            try:
                refill_ms = r.json().get("refillIn", 60000)
            except Exception:
                refill_ms = 60000
            wait_s = max(refill_ms / 1000.0, 5) + 1
            status.update(label=f"Keepa tokens exhausted — waiting {int(wait_s)}s for refill…")
            time.sleep(wait_s)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError("Keepa: token refill wait exceeded retry limit")

def fetch_market(key, market, eans, cache, cache_hours, status):
    """Return {ean: {...}} for one market, using cache where fresh."""
    domain = KEEPA_DOMAINS[market]
    now = time.time()
    results, missing = {}, []
    for ean in eans:
        ck = f"v2:{domain}:{ean}"
        entry = cache.get(ck)
        if entry and cache_hours > 0 and now - entry["ts"] < cache_hours * 3600:
            results[ean] = entry["data"]
        else:
            missing.append(ean)

    tokens_left = None
    for i in range(0, len(missing), BATCH_SIZE):
        batch = missing[i:i + BATCH_SIZE]
        status.update(label=f"{market}: fetching {i + 1}–{i + len(batch)} of {len(missing)} from Keepa…")
        data = keepa_request(key, domain, batch, status)
        tokens_left = data.get("tokensLeft")
        products = [extract_product(p) for p in (data.get("products") or [])]

        for ean in batch:
            matches = [p for p in products if ean in p["eans"]]
            if not matches:
                results[ean] = None
            else:
                # Prefer listings with an active buy box, then the best (lowest) sales rank
                matches.sort(key=lambda p: (p["buybox90"] is None,
                                            p["rank30"] if p["rank30"] is not None else 10**9))
                best = matches[0]
                best["n_matches"] = len(matches)
                results[ean] = best
            cache[f"v2:{domain}:{ean}"] = {"ts": now, "data": results[ean]}
        save_cache(cache)

    return results, tokens_left

# ─── INPUT FILE PARSING ───────────────────────────────────────────────────────

def normalize_ean(val):
    if pd.isna(val):
        return None
    if isinstance(val, float):
        val = str(int(val))
    s = "".join(ch for ch in str(val).strip() if ch.isdigit())
    if not s or len(s) < 8:
        return None
    if 8 < len(s) < 13:
        s = s.zfill(13)  # Excel drops leading zeros
    return s

def detect_columns(df):
    ean_col = title_col = price_col = brand_col = None
    for c in df.columns:
        lc = str(c).lower()
        if ean_col is None and ("ean" in lc or "barcode" in lc or "gtin" in lc):
            ean_col = c
        elif brand_col is None and "brand" in lc:
            brand_col = c
        elif title_col is None and any(w in lc for w in ("title", "desc", "product", "name")):
            title_col = c
        elif price_col is None and any(w in lc for w in ("price", "purchase", "eur", "cost")):
            price_col = c
    return ean_col, title_col, price_col, brand_col

def read_input(uploaded):
    if uploaded.name.lower().endswith(".csv"):
        return pd.read_csv(uploaded)
    return pd.read_excel(uploaded, engine="openpyxl")

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
            **{m: st.column_config.SelectboxColumn(m, options=STATUS_OPTIONS, required=False)
               for m in MATRIX_MARKETS},
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
            save_matrix_df(clean)
            load_matrix_df.clear()
            st.success(f"Saved — {len(clean)} brands.")

    st.download_button("⬇️ Download matrix (.csv)", data=edited_df.fillna("").to_csv(index=False),
                       file_name="brand_matrix.csv", mime="text/csv")
    st.caption("Note: on the cloud app, saved edits last until Streamlit restarts the app "
               "(then it reverts to the repo copy). Download a backup after big edit sessions.")
    st.stop()

# ─── MAIN ─────────────────────────────────────────────────────────────────────

input_mode = st.radio("Input", ["📄 Upload file", "⌨️ Enter manually"],
                      horizontal=True, label_visibility="collapsed")

items = []
skipped = 0
seen = set()

if input_mode == "📄 Upload file":
    uploaded = st.file_uploader("Upload file with EAN, Title, Purchase price EUR", type=["xlsx", "csv"])

    if not uploaded:
        st.info("Upload an .xlsx or .csv with columns: EAN, Title, Purchase price (EUR) — "
                "and optionally Brand. Column names are detected automatically.")
        st.stop()

    try:
        raw = read_input(uploaded)
    except Exception as e:
        st.error(f"Could not read file: {e}")
        st.stop()

    raw.columns = [str(c).strip() for c in raw.columns]
    ean_col, title_col, price_col, brand_col = detect_columns(raw)

    if ean_col is None or price_col is None:
        st.error(f"Could not detect required columns. Found: EAN → {ean_col}, "
                 f"Title → {title_col}, Price → {price_col}. "
                 "Rename your columns to include 'EAN' and 'Price'.")
        st.stop()

    st.caption(f"Detected columns — EAN: **{ean_col}**, Title: **{title_col or '(none)'}**, "
               f"Purchase price EUR: **{price_col}**, Brand: **{brand_col or '(none — will use Keepa)'}**")

    for _, r in raw.iterrows():
        ean = normalize_ean(r[ean_col])
        price = pd.to_numeric(r[price_col], errors="coerce")
        if ean is None or pd.isna(price):
            skipped += 1
            continue
        if ean in seen:
            continue
        seen.add(ean)
        items.append({
            "ean": ean,
            "title": str(r[title_col]) if title_col and pd.notna(r[title_col]) else "",
            "price_eur": float(price),
            "brand": str(r[brand_col]).strip() if brand_col and pd.notna(r[brand_col]) else "",
        })
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
    for _, r in manual_df.iterrows():
        brand = str(r.get("Brand") or "").strip()
        ean_raw = str(r.get("EAN") or "").strip()
        price = pd.to_numeric(r.get("Purchase price EUR"), errors="coerce")
        if not brand and not ean_raw and pd.isna(price):
            continue                     # untouched empty row
        ean = normalize_ean(ean_raw)
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
            f"Estimated Keepa cost: up to **{len(items) * len(KEEPA_DOMAINS)} tokens** (less with cache).")

if st.button("🔍 Fetch from Keepa & Analyze", type="primary"):
    keepa_api_key = get_keepa_key()
    if not keepa_api_key:
        st.error("No Keepa API key found — add it in the sidebar (local) "
                 "or in Streamlit Cloud → App settings → Secrets as keepa_key = \"...\".")
        st.stop()

    cache = load_cache()
    eans = [it["ean"] for it in items]
    market_data = {}
    tokens_left = None

    with st.status("Fetching from Keepa…", expanded=False) as status:
        for market in KEEPA_DOMAINS:
            try:
                market_data[market], tl = fetch_market(
                    keepa_api_key, market, eans, cache,
                    st.session_state["cache_hours"], status)
                if tl is not None:
                    tokens_left = tl
            except requests.HTTPError as e:
                st.error(f"Keepa error for {market}: {e} — check your API key and token balance.")
                st.stop()
        status.update(label="Keepa fetch complete", state="complete")

    st.session_state["market_data"] = market_data
    st.session_state["tokens_left"] = tokens_left
    st.session_state["result_file"] = source_id

if "market_data" not in st.session_state:
    st.stop()

market_data = st.session_state["market_data"]
tokens_left = st.session_state["tokens_left"]
if st.session_state.get("result_file") != source_id:
    st.warning("Results below are from a previously fetched file — click the button above to re-fetch.")
if tokens_left is not None:
    st.caption(f"Keepa tokens left: {tokens_left}")

try:
    brand_matrix = matrix_from_df(load_matrix_df())
    matrix_error = None
except Exception as e:
    brand_matrix = {}
    matrix_error = str(e)
if matrix_error:
    st.warning(f"Could not load Brand Matrix — gating shown as 'to be checked'. ({matrix_error})")

rows = []
for it in items:
    row = {
        "Product": it["title"],
        "EAN": it["ean"],
        "Purchase (EUR)": round(it["price_eur"], 2),
    }
    notes = []

    brand = it.get("brand") or None
    if not brand:
        for market in KEEPA_DOMAINS:
            d = market_data.get(market, {}).get(it["ean"])
            if d and d.get("brand"):
                brand = d["brand"]
                break
    row["Brand"] = brand
    gating = gating_for_brand(brand_matrix, brand)
    gate_ranks = {}
    for market in ("UK", "CA"):
        if gating is None:
            row[f"Gating {market}"] = GATE_UNKNOWN
            gate_ranks[market] = GATE_CHECK
        else:
            rank_g = gating[market]
            row[f"Gating {market}"] = GATE_LABELS[rank_g]
            gate_ranks[market] = rank_g
    if gating is not None and gating.get("note"):
        notes_matrix = f"Matrix: {gating['note']}"
    else:
        notes_matrix = None
    row["_gate"] = min(gate_ranks.values())
    row["_gate_uk"] = gate_ranks["UK"]
    row["_gate_ca"] = gate_ranks["CA"]

    for market, cur, calc in [("UK", "GBP", calc_uk), ("CA", "CAD", calc_ca)]:
        d = market_data.get(market, {}).get(it["ean"])
        sell = rank = roi = None
        asin = None
        if d:
            asin = d["asin"]
            rank = d["rank30"]
            if d["buybox90"] is not None:
                sell = d["buybox90"]
            elif d["new90"] is not None:
                sell = d["new90"]
                notes.append(f"{market}: no Buy Box, used NEW avg")
            if d.get("n_matches", 1) > 1:
                notes.append(f"{market}: {d['n_matches']} ASINs matched")
            if not it["title"] and d.get("title"):
                row["Product"] = d["title"]
        if sell is not None:
            roi = round(calc(it["price_eur"], sell) * 100, 1)
        row[f"ASIN {market}"] = asin
        row[f"Sell {market} ({cur})"] = round(sell, 2) if sell is not None else None
        row[f"Rank {market}"] = rank
        row[f"ROI {market}"] = roi
    if notes_matrix:
        notes.append(notes_matrix)
    row["Notes"] = "; ".join(notes)
    rows.append(row)

result_df = pd.DataFrame(rows)

for col in ["Rank UK", "Rank CA"]:
    result_df[col] = pd.array(result_df[col], dtype=pd.Int64Dtype())

for col in ["ROI UK", "ROI CA"]:
    result_df[col] = pd.to_numeric(result_df[col], errors="coerce")

# Rank: sellable brands first (OK > can apply > to check > hard gated),
# within each group by ROI CA desc, then ROI UK desc.
# A market's ROI only counts toward ranking if the brand is sellable there
# (OK or can-apply) — a great ROI in a hard-gated market shouldn't lift a product.
result_df["_roi_ca"] = result_df["ROI CA"].where(result_df["_gate_ca"] <= GATE_CHECK).fillna(-10**9)
result_df["_roi_uk"] = result_df["ROI UK"].where(result_df["_gate_uk"] <= GATE_CHECK).fillna(-10**9)
result_df = (result_df
             .sort_values(["_gate", "_roi_ca", "_roi_uk"], ascending=[True, False, False])
             .drop(columns=["_gate", "_gate_uk", "_gate_ca", "_roi_ca", "_roi_uk"])
             .reset_index(drop=True))

col_order = ["Product", "Brand", "EAN", "Purchase (EUR)",
             "ASIN CA", "Sell CA (CAD)", "Rank CA", "ROI CA", "Gating CA",
             "ASIN UK", "Sell UK (GBP)", "Rank UK", "ROI UK", "Gating UK", "Notes"]
result_df = result_df[[c for c in col_order if c in result_df.columns]]

n_found = result_df[["ROI UK", "ROI CA"]].notna().any(axis=1).sum()
st.markdown(f"**{len(result_df)} products** ({n_found} found on Keepa) — "
            "sellable brands first, then by ROI CA, then ROI UK")

display_df = result_df.copy()
display_df["ROI UK"] = display_df["ROI UK"].apply(fmt_roi)
display_df["ROI CA"] = display_df["ROI CA"].apply(fmt_roi)

st.dataframe(display_df, use_container_width=True, hide_index=True)

buf = io.BytesIO()
result_df.to_excel(buf, index=False, engine="openpyxl")
st.download_button("⬇️ Download results (.xlsx)", data=buf.getvalue(),
                   file_name="mto_analysis.xlsx",
                   mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
