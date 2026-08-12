"""Shared business logic for both tools.

Pure Python — no Streamlit imports. Used by:
  * Products Analyzer (app.py) — interactive: manual EAN entry or file upload
  * MTO Analyzer (automation/mto_pipeline.py) — automated: Gmail → Slack
Both import this module, so the numbers a human sees and the numbers posted to
Slack come from the exact same code and can never drift.
"""

import io
import json
import os
import time
import unicodedata

import pandas as pd
import requests

# ─── PARAMETERS ───────────────────────────────────────────────────────────────

DEFAULT_PARAMS = {
    "eur_gbp": 0.867, "eur_usd": 1.170, "usd_cad": 1.369, "eur_jpy": 170.0,
    "dsf":     3.0,
    "uk_ship": 0.80,  "uk_lab": 2.35,  "uk_fba": 3.09, "uk_ref": 15.0, "uk_vat": 20.0,
    "uk_dsf": 3.0,    # digital services fee, % of (referral + FBA) — same basis as CA
    "ca_ship": 3.12,  "ca_lab": 2.35,  "ca_fba": 7.33, "ca_ref": 15.0,
    # Japan: one all-in additional cost per unit (shipping/3PL/FBA/duties),
    # split by dangerous goods (alcohol-based: EDT/EDP/perfume) vs not.
    "jp_add_dg": 35.32, "jp_add_ndg": 20.21,
    "jp_ref": 10.4, "jp_dsf": 2.5, "jp_vat": 10.0,
}

RATES_URL = "https://api.frankfurter.app/latest?from=EUR&to=GBP,USD,CAD,JPY"


def fetch_live_rates(timeout=8):
    """Live ECB rates. Returns dict or None (caller decides the fallback)."""
    try:
        r = requests.get(RATES_URL, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        rates = data["rates"]
        return {
            "eur_gbp": round(rates["GBP"], 4),
            "eur_usd": round(rates["USD"], 4),
            "usd_cad": round(rates["CAD"] / rates["USD"], 4),
            "eur_jpy": round(rates["JPY"], 2),
            "date": data.get("date", "unknown"),
        }
    except Exception:
        return None


# ─── DANGEROUS GOODS CLASSIFICATION (drives the JP additional cost) ───────────

DG_KEYWORDS = ("edt", "edp", "edc", "eau de toilette", "eau de parfum",
               "eau de cologne", "eau fraiche", "parfum", "perfume", "cologne",
               "elixir", "extrait", "aftershave", "after shave", "deo",
               "deodorant", "spray", "aerosol", "nail polish", "esmalte",
               "hairspray", "mousse", "fragrance", "toilette")

NDG_KEYWORDS = ("cream", "crema", "serum", "lotion", "mask", "mascarilla",
                "shampoo", "champu", "gel", "balm", "lipstick", "labial",
                "foundation", "powder", "polvo", "oil", "aceite", "soap",
                "jabon", "scrub", "moistur", "sunscreen", "spf")


def is_dangerous_goods(title, brand=""):
    """(is_dg, certain) — DG = alcohol/aerosol based, higher JP shipping cost.
    Unclear titles default to DG (conservative: higher cost, lower ROI)."""
    t = f"{title} {brand}".casefold()
    if any(k in t for k in DG_KEYWORDS):
        return True, True
    if any(k in t for k in NDG_KEYWORDS):
        return False, True
    return True, False


# ─── ROI CALCULATIONS ─────────────────────────────────────────────────────────

def calc_uk(p_eur, s_gbp, P, is_dg=True):
    """ROI for UK. Sell price incl. VAT; referral on ex-VAT price; VAT not in COGS.

    The digital services fee IS deducted here (% of referral + FBA, same basis as
    CA/AU). Note: the UB cost calculator displays this fee but does not subtract
    it from profit — it mirrors the original UK spreadsheet — so its UK ROI reads
    ~1pp higher than this one until that is changed. Set uk_dsf=0 to match it.
    """
    rate = P["eur_gbp"]
    cogs = (p_eur + P["uk_ship"] + P["uk_lab"]) * rate
    s    = s_gbp / (1 + P["uk_vat"] / 100)
    ref  = s * P["uk_ref"] / 100
    dsf  = (ref + P["uk_fba"]) * P.get("uk_dsf", 0.0) / 100
    ppu  = s - cogs - P["uk_fba"] - ref - dsf
    return ppu / cogs if cogs > 0 else 0


def calc_ca(p_eur, s_cad, P, is_dg=True):
    """ROI for CA, computed in USD. DSF applies to referral + FBA fees."""
    cad_usd  = 1 / P["usd_cad"]
    cogs     = (p_eur + P["ca_ship"] + P["ca_lab"]) * P["eur_usd"]
    sell_usd = s_cad * cad_usd
    fba_usd  = P["ca_fba"] * cad_usd
    ref      = sell_usd * P["ca_ref"] / 100
    dsf      = (ref + fba_usd) * P["dsf"] / 100
    ppu      = sell_usd - cogs - ref - fba_usd - dsf
    return ppu / cogs if cogs > 0 else 0


def calc_jp(p_eur, s_jpy, P, is_dg=True):
    """ROI for Amazon Japan, computed in EUR.

    Sell price from Keepa is JPY incl. 10% Japanese consumption tax.
    Referral fee is charged on the tax-inclusive price; the digital service
    fee is a percentage of the referral fee. Profit compares the ex-tax
    revenue against COGS + fees.
    """
    sell_incl = s_jpy / P["eur_jpy"]
    sell_excl = sell_incl / (1 + P["jp_vat"] / 100)
    additional = P["jp_add_dg"] if is_dg else P["jp_add_ndg"]
    cogs = p_eur + additional
    ref  = sell_incl * P["jp_ref"] / 100
    dsf  = ref * P["jp_dsf"] / 100
    ppu  = sell_excl - (cogs + ref + dsf)
    return ppu / cogs if cogs > 0 else 0


# ─── MARKETS ──────────────────────────────────────────────────────────────────
# Keepa domain ids: 1 US · 2 UK · 3 DE · 4 FR · 5 JP · 6 CA
MARKETS = {
    # price_divisor: Keepa returns prices in the currency's smallest unit —
    # cents for GBP/CAD (÷100), but JPY has no minor unit so values are whole yen.
    "CA": {"domain": 6, "currency": "CAD", "calc": calc_ca, "price_divisor": 100},
    "UK": {"domain": 2, "currency": "GBP", "calc": calc_uk, "price_divisor": 100},
    "JP": {"domain": 5, "currency": "JPY", "calc": calc_jp, "price_divisor": 1},
}
KEEPA_DOMAINS = {m: cfg["domain"] for m, cfg in MARKETS.items()}


def fmt_roi(val):
    if val is None or pd.isna(val):
        return "—"
    icon = "🟢" if val >= 20 else ("🟡" if val >= 10 else ("🟠" if val > 0 else "🔴"))
    return f"{icon} {val:.1f}%"


# ─── BRAND GATING ─────────────────────────────────────────────────────────────

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


def matrix_from_df(df):
    """{normalized brand: {'display', 'note', market: rank}}"""
    matrix = {}
    if df is None:
        return matrix
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
    """Exact normalized match first, then substring either way (min 4 chars).
    Substring matches are heuristic — callers should flag them for human review
    (compare norm_brand(brand) with norm_brand(entry['display']))."""
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

IDX_SALES_RANK = 3      # stats array index: sales rank
IDX_NEW = 1             # stats array index: NEW price
IDX_BUY_BOX = 18        # stats array index: buy box incl. shipping
BATCH_SIZE = 100
CACHE_MAX_AGE_S = 7 * 86400   # prune cache entries older than a week


def load_cache(path):
    if path and os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_cache(cache, path):
    if not path:
        return
    now = time.time()
    pruned = {k: v for k, v in cache.items()
              if isinstance(v, dict) and now - v.get("ts", 0) < CACHE_MAX_AGE_S}
    with open(path, "w") as f:
        json.dump(pruned, f)


def stat_price(stats, arr_name, idx, divisor=100):
    arr = (stats or {}).get(arr_name) or []
    if idx < len(arr) and arr[idx] is not None and arr[idx] > 0:
        return arr[idx] / divisor
    return None


def stat_rank(stats, arr_name, idx):
    arr = (stats or {}).get(arr_name) or []
    if idx < len(arr) and arr[idx] is not None and arr[idx] > 0:
        return int(arr[idx])
    return None


def extract_product(p, price_divisor=100):
    """Reduce a Keepa product object to the fields we need."""
    stats = p.get("stats") or {}
    return {
        "asin": p.get("asin"),
        "title": p.get("title"),
        "brand": p.get("brand"),
        "eans": p.get("eanList") or [],
        "buybox90": stat_price(stats, "avg90", IDX_BUY_BOX, price_divisor),
        "new90": stat_price(stats, "avg90", IDX_NEW, price_divisor),
        "rank30": stat_rank(stats, "avg30", IDX_SALES_RANK),
    }


def keepa_request(key, domain, codes, progress=None, buybox=True):
    """One /product call for up to 100 EANs.
    buybox=True asks Keepa for Buy Box stats — the accurate sell-price proxy,
    but it costs 3 tokens per product instead of 1.
    Waits for token refill on 429; retries transient network/5xx errors."""
    progress = progress or (lambda msg: None)
    params = {"key": key, "domain": domain, "code": ",".join(codes),
              "stats": 90, "history": 0}
    if buybox:
        params["buybox"] = 1
    refill_waits = 0
    transient = 0
    while True:
        try:
            r = requests.get("https://api.keepa.com/product", params=params, timeout=60)
        except requests.RequestException as e:
            transient += 1
            if transient > 3:
                raise RuntimeError(f"Keepa unreachable after 3 retries: {e}") from e
            progress(f"Keepa connection issue — retry {transient}/3…")
            time.sleep(5 * transient)
            continue
        if r.status_code == 429:
            refill_waits += 1
            if refill_waits > 12:
                raise RuntimeError("Keepa: token refill wait exceeded retry limit")
            try:
                refill_ms = r.json().get("refillIn", 60000)
            except Exception:
                refill_ms = 60000
            wait_s = max(refill_ms / 1000.0, 5) + 1
            progress(f"Keepa tokens exhausted — waiting {int(wait_s)}s for refill…")
            time.sleep(wait_s)
            continue
        if r.status_code >= 500:
            transient += 1
            if transient > 3:
                r.raise_for_status()
            progress(f"Keepa server error {r.status_code} — retry {transient}/3…")
            time.sleep(5 * transient)
            continue
        r.raise_for_status()
        return r.json()


def fetch_market(key, market, eans, cache, cache_hours, progress=None, cache_path=None,
                 buybox=True):
    """Return ({ean: product_or_None}, tokens_left) for one market, cache-first."""
    progress = progress or (lambda msg: None)
    domain = MARKETS[market]["domain"]
    divisor = MARKETS[market].get("price_divisor", 100)
    now = time.time()
    results, missing = {}, []
    for ean in eans:
        entry = cache.get(f"v4{'b' if buybox else ''}:{domain}:{ean}")
        if entry and cache_hours > 0 and now - entry["ts"] < cache_hours * 3600:
            results[ean] = entry["data"]
        else:
            missing.append(ean)

    tokens_left = None
    for i in range(0, len(missing), BATCH_SIZE):
        batch = missing[i:i + BATCH_SIZE]
        progress(f"{market}: fetching {i + 1}–{i + len(batch)} of {len(missing)} from Keepa…")
        data = keepa_request(key, domain, batch, progress, buybox)
        tokens_left = data.get("tokensLeft")
        products = [extract_product(p, divisor) for p in (data.get("products") or [])]

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
            cache[f"v4{'b' if buybox else ''}:{domain}:{ean}"] = {"ts": now, "data": results[ean]}
        save_cache(cache, cache_path)

    return results, tokens_left


# ─── INPUT PARSING ────────────────────────────────────────────────────────────

def normalize_ean(val):
    if val is None or (isinstance(val, float) and pd.isna(val)):
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
        elif brand_col is None and ("brand" in lc or "marca" in lc):
            brand_col = c
        elif title_col is None and any(w in lc for w in ("title", "desc", "product", "name", "articulo")):
            title_col = c
        elif price_col is None and any(w in lc for w in ("price", "purchase", "eur", "cost", "precio", "pvp")):
            price_col = c
    return ean_col, title_col, price_col, brand_col


def reheader(df, max_scan=15):
    """Supplier attachments often start with title/logo rows. If the header
    isn't in row 0, find the first row that looks like a header (contains an
    EAN-ish cell) and use it."""
    if detect_columns(df)[0] is not None:
        return df
    for i in range(min(max_scan, len(df))):
        cells = [str(v).strip().lower() for v in df.iloc[i].tolist()]
        if any("ean" in c or "barcode" in c or "gtin" in c for c in cells):
            out = df.iloc[i + 1:].copy()
            out.columns = [str(v).strip() for v in df.iloc[i].tolist()]
            return out.reset_index(drop=True)
    return df


def items_from_dataframe(raw, default_brand=""):
    """Parse an offer table into analysis items.
    Returns (items, n_skipped, columns_info). Raises ValueError if the
    required columns can't be detected."""
    raw = reheader(raw.copy())
    raw.columns = [str(c).strip() for c in raw.columns]
    ean_col, title_col, price_col, brand_col = detect_columns(raw)
    if ean_col is None or price_col is None:
        raise ValueError(f"Could not detect required columns "
                         f"(EAN → {ean_col}, Title → {title_col}, Price → {price_col}). "
                         "Columns must include 'EAN' and 'Price'.")
    items, skipped, seen = [], 0, set()
    for _, r in raw.iterrows():
        ean = normalize_ean(r[ean_col])
        price = pd.to_numeric(str(r[price_col]).replace("€", "").replace(",", ".").strip()
                              if isinstance(r[price_col], str) else r[price_col],
                              errors="coerce")
        if ean is None or pd.isna(price) or price <= 0:
            skipped += 1
            continue
        if ean in seen:
            continue
        seen.add(ean)
        brand = ""
        if brand_col and pd.notna(r[brand_col]):
            brand = str(r[brand_col]).strip()
        items.append({
            "ean": ean,
            "title": str(r[title_col]) if title_col and pd.notna(r[title_col]) else "",
            "price_eur": float(price),
            "brand": brand or default_brand,
        })
    cols = {"ean": ean_col, "title": title_col, "price": price_col, "brand": brand_col}
    return items, skipped, cols


def items_from_excel(data_or_path, default_brand=""):
    """Parse EVERY sheet/tab of a workbook and merge the results — supplier
    attachments often split brands or sizes across tabs, and the email body
    usually shows only part of the offer.
    Returns (items, n_skipped, per_sheet_report)."""
    src = io.BytesIO(data_or_path) if isinstance(data_or_path, bytes) else data_or_path
    sheets = pd.read_excel(src, sheet_name=None)
    items, skipped, report = [], 0, {}
    seen = set()
    for name, df in sheets.items():
        if df is None or df.empty:
            report[name] = "empty"
            continue
        try:
            sheet_items, sheet_skipped, _ = items_from_dataframe(df, default_brand)
        except ValueError:
            report[name] = "no EAN/price columns — skipped"
            continue
        added = 0
        for it in sheet_items:
            if it["ean"] in seen:
                continue
            seen.add(it["ean"])
            items.append(it)
            added += 1
        skipped += sheet_skipped
        report[name] = f"{added} products"
    if not items:
        raise ValueError(f"No product rows found in any sheet ({report}).")
    return items, skipped, report


# ─── STATUS CLASSIFICATION ────────────────────────────────────────────────────

ROI_THRESHOLD = 17.0   # % — an "opportunity" needs at least this ROI

# Per-product labels (Status column)
PSTATUS_EXISTING = "🟢 opportunity"
PSTATUS_UNGATING = "🟠 soft-gated, good ROI"
PSTATUS_CHECKGATE = "🔵 ROI ok — gating unknown"
PSTATUS_NEW = "🆕 no listing"
PSTATUS_LOW = "⚪ below threshold"
PSTATUS_HARD = "🚫 hard gated"

# Offer-level headlines (app display)
STATUS_EXISTING = "🟢 Opportunities with existing listings found"
STATUS_UNGATING = "🟠 Soft-gated brands with good ROI"
STATUS_NEW_LAUNCH = "🆕 No listing — check if worth creating"
STATUS_NO_OPP = "⚪ No opportunities — ROI below threshold"
STATUS_HARD = "🚫 Hard gated on all target markets"
STATUS_HEADLINES = {}   # filled after the per-product labels are defined

# Slack/report phrasing per category, "{n}" filled in
STATUS_SENTENCES = [
    (PSTATUS_EXISTING, "{n} EANs for ungated brands with ROI above {t:.0f}%"),
    (PSTATUS_UNGATING, "{n} EANs for soft-gated brands with ROI above {t:.0f}% — ungating required"),
    (PSTATUS_CHECKGATE, "{n} EANs with ROI above {t:.0f}% for brands missing from the gating matrix, gating status to be checked"),
    (PSTATUS_NEW,      "{n} EANs with no listings on target markets, check if worth creating"),
    (PSTATUS_LOW,      "{n} EANs listed but with ROI below {t:.0f}%"),
    (PSTATUS_HARD,     "{n} EANs hard-gated on all target markets — cannot sell"),
]

STATUS_CHECKGATE = "🔵 Opportunities found — gating status to be checked"
STATUS_HEADLINES.update({
    PSTATUS_EXISTING: STATUS_EXISTING, PSTATUS_UNGATING: STATUS_UNGATING,
    PSTATUS_CHECKGATE: STATUS_CHECKGATE,
    PSTATUS_NEW: STATUS_NEW_LAUNCH, PSTATUS_LOW: STATUS_NO_OPP,
    PSTATUS_HARD: STATUS_HARD,
})


def product_status(row, roi_threshold=ROI_THRESHOLD):
    """Classify one result row across all markets."""
    ok_label = GATE_LABELS[GATE_OK]
    apply_label = GATE_LABELS[GATE_APPLY]
    hard_label = GATE_LABELS[GATE_HARD]
    any_listing = False
    all_hard = True
    best = None
    for m in MARKETS:
        gate = row.get(f"Gating {m}")
        if gate != hard_label:
            all_hard = False
        asin = row.get(f"ASIN {m}")
        if asin is not None and pd.notna(asin):
            any_listing = True
        roi = row.get(f"ROI {m}")
        if roi is None or pd.isna(roi) or roi < roi_threshold:
            continue
        if gate == ok_label:
            return PSTATUS_EXISTING
        if gate == apply_label:
            best = best or PSTATUS_UNGATING
        elif gate != hard_label:
            # ROI clears the bar but we don't know if we may sell the brand
            best = best or PSTATUS_CHECKGATE
    if best:
        return best
    if all_hard:
        return PSTATUS_HARD
    return PSTATUS_LOW if any_listing else PSTATUS_NEW


def status_counts(result_df, roi_threshold=ROI_THRESHOLD):
    if "Status" in result_df.columns:
        statuses = list(result_df["Status"])
    else:
        statuses = [product_status(r, roi_threshold) for _, r in result_df.iterrows()]
    return {label: statuses.count(label) for label, _ in STATUS_SENTENCES}


def status_summary_lines(result_df, roi_threshold=ROI_THRESHOLD):
    """One plain sentence per non-empty category, most actionable first —
    this is what goes into the Slack post."""
    counts = status_counts(result_df, roi_threshold)
    return [tpl.format(n=counts[label], t=roi_threshold)
            for label, tpl in STATUS_SENTENCES if counts[label]]


def offer_status(result_df, roi_threshold=ROI_THRESHOLD):
    """Single headline (for the app) + counts."""
    counts = status_counts(result_df, roi_threshold)
    headline = STATUS_NO_OPP
    for label, _ in STATUS_SENTENCES:
        if counts[label]:
            headline = STATUS_HEADLINES[label]
            break
    breakdown = " · ".join(f"{n} {label}" for label, n in counts.items() if n)
    if len(result_df) > 1 and breakdown:
        headline = f"{headline}  ({breakdown})"
    return headline, counts


# ─── ANALYSIS ORCHESTRATION ───────────────────────────────────────────────────

def item_gate_ranks(item, matrix):
    """Gating entry + per-market rank for an item, using its provided brand."""
    gating = gating_for_brand(matrix, item.get("brand") or "")
    ranks = {m: (gating[m] if gating else GATE_CHECK) for m in MARKETS}
    return gating, ranks


def build_fetch_plan(items, matrix, skip_hard_gated=True):
    """Which EANs to fetch per market. Hard-gated markets are skipped only when
    the item's brand was explicitly provided (a Keepa-derived brand isn't known
    until after the fetch). Returns (plan, skipped_pairs)."""
    plan = {m: [] for m in MARKETS}
    skipped = set()
    for it in items:
        _, ranks = item_gate_ranks(it, matrix)
        for m in MARKETS:
            if skip_hard_gated and it.get("brand") and ranks[m] == GATE_HARD:
                skipped.add((it["ean"], m))
                continue
            plan[m].append(it["ean"])
    return plan, skipped


RESULT_COLUMNS = (["Product", "Brand", "EAN", "Purchase (EUR)", "Status"]
                  + [f"{field} {m}" for m in MARKETS
                     for field in ("ASIN", "Sell", "Rank", "ROI", "Gating")]
                  + ["Notes"])


def build_result_df(items, market_data, matrix, params, skipped_pairs=None):
    """Assemble + rank the result table. Pure function of its inputs, so the UI
    can re-rank with new params without re-fetching."""
    skipped_pairs = skipped_pairs or set()
    P = {**DEFAULT_PARAMS, **(params or {})}
    rows = []
    for it in items:
        row = {"Product": it["title"], "EAN": it["ean"],
               "Purchase (EUR)": round(it["price_eur"], 2)}
        notes = []

        brand = it.get("brand") or None
        keepa_title = ""
        if not brand:
            for market in MARKETS:
                d = market_data.get(market, {}).get(it["ean"])
                if d and d.get("brand"):
                    brand = d["brand"]
                    break
        row["Brand"] = brand
        gating = gating_for_brand(matrix, brand)
        if gating is not None and brand and norm_brand(brand) != norm_brand(gating["display"]):
            notes.append(f"Gating matched matrix brand '{gating['display']}' — verify")
        gate_ranks = {}
        for market in MARKETS:
            if gating is None:
                row[f"Gating {market}"] = GATE_UNKNOWN
                gate_ranks[market] = GATE_CHECK
            else:
                gate_ranks[market] = gating[market]
                row[f"Gating {market}"] = GATE_LABELS[gating[market]]
            row[f"_gate_{market}"] = gate_ranks[market]
        row["_gate"] = min(gate_ranks.values())

        # Dangerous-goods class drives the JP additional cost
        for market in MARKETS:
            d = market_data.get(market, {}).get(it["ean"])
            if d and d.get("title"):
                keepa_title = d["title"]
                break
        is_dg, dg_certain = is_dangerous_goods(it["title"] or keepa_title, brand or "")
        if not dg_certain:
            notes.append("JP cost: DG assumed (unclear product type)")

        for market, cfg in MARKETS.items():
            cur, calc = cfg["currency"], cfg["calc"]
            d = market_data.get(market, {}).get(it["ean"])
            sell = rank = roi = asin = None
            if (it["ean"], market) in skipped_pairs:
                notes.append(f"{market}: Keepa lookup skipped (hard-gated)")
            elif d:
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
                roi = round(calc(it["price_eur"], sell, P, is_dg) * 100, 1)
            row[f"ASIN {market}"] = asin
            row[f"Sell {market} ({cur})"] = round(sell, 2) if sell is not None else None
            row[f"Sell {market}"] = row[f"Sell {market} ({cur})"]
            row[f"Rank {market}"] = rank
            row[f"ROI {market}"] = roi

        if gating is not None and gating.get("note"):
            notes.append(f"Matrix: {gating['note']}")
        row["Notes"] = "; ".join(notes)
        row["Status"] = product_status(row)
        rows.append(row)

    result_df = pd.DataFrame(rows)
    for m in MARKETS:
        result_df[f"Rank {m}"] = pd.array(result_df[f"Rank {m}"], dtype=pd.Int64Dtype())
        result_df[f"ROI {m}"] = pd.to_numeric(result_df[f"ROI {m}"], errors="coerce")

    # Rank: sellable brands first (OK > can apply > to check > hard gated),
    # then by ROI CA, UK, JP desc. A market's ROI only counts toward ranking
    # if the brand is sellable there.
    sort_cols, ascending = ["_gate"], [True]
    for m in MARKETS:
        col = f"_roi_{m}"
        result_df[col] = result_df[f"ROI {m}"].where(
            result_df[f"_gate_{m}"] <= GATE_CHECK).fillna(-10**9)
        sort_cols.append(col)
        ascending.append(False)
    result_df = (result_df.sort_values(sort_cols, ascending=ascending)
                 .drop(columns=[c for c in result_df.columns if c.startswith("_")])
                 .reset_index(drop=True))

    cols = []
    for c in RESULT_COLUMNS:
        if c.startswith("Sell "):
            m = c.split()[1]
            cols.append(f"Sell {m} ({MARKETS[m]['currency']})")
        else:
            cols.append(c)
    return result_df[[c for c in cols if c in result_df.columns]]


def analyze(items, keepa_key, params=None, matrix_df=None, cache_path=None,
            cache_hours=24, progress=None, skip_hard_gated=True, buybox=True):
    """End-to-end: gating pre-check → Keepa fetch → ranked result table.

    Returns dict with result_df, market_data, skipped_pairs, tokens_left,
    fetched (per-market fetch counts)."""
    progress = progress or (lambda msg: None)
    matrix = matrix_from_df(matrix_df)
    plan, skipped_pairs = build_fetch_plan(items, matrix, skip_hard_gated)
    cache = load_cache(cache_path)
    market_data, tokens_left = {}, None
    for market, eans in plan.items():
        market_data[market], tl = fetch_market(keepa_key, market, eans, cache,
                                               cache_hours, progress, cache_path, buybox)
        if tl is not None:
            tokens_left = tl
    result_df = build_result_df(items, market_data, matrix, params, skipped_pairs)
    return {"result_df": result_df, "market_data": market_data,
            "skipped_pairs": skipped_pairs, "tokens_left": tokens_left,
            "fetched": {m: len(e) for m, e in plan.items()}}
