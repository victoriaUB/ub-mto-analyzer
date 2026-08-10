"""Core business logic for the MTO Analyzer.

Pure Python — no Streamlit imports. Used by both the Streamlit UI (app.py)
and the headless automation pipeline (automation/mto_pipeline.py), so the
numbers a human sees in the app and the numbers posted to Slack come from
the exact same code and can never drift.
"""

import json
import os
import time
import unicodedata

import pandas as pd
import requests

# ─── PARAMETERS ───────────────────────────────────────────────────────────────

DEFAULT_PARAMS = {
    "eur_gbp": 0.867, "eur_usd": 1.170, "usd_cad": 1.369,
    "dsf":     3.0,
    "uk_ship": 0.80,  "uk_lab": 2.35,  "uk_fba": 3.09, "uk_ref": 15.0, "uk_vat": 20.0,
    "ca_ship": 3.12,  "ca_lab": 2.35,  "ca_fba": 7.33, "ca_ref": 15.0,
}

RATES_URL = "https://api.frankfurter.app/latest?from=EUR&to=GBP,USD,CAD"


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
            "date": data.get("date", "unknown"),
        }
    except Exception:
        return None


# ─── ROI CALCULATIONS ─────────────────────────────────────────────────────────

def calc_uk(p_eur, s_gbp, P):
    """ROI for UK. Sell price incl. VAT; referral on ex-VAT price; VAT not in COGS."""
    rate = P["eur_gbp"]
    cogs = (p_eur + P["uk_ship"] + P["uk_lab"]) * rate
    s    = s_gbp / (1 + P["uk_vat"] / 100)
    ref  = s * P["uk_ref"] / 100
    ppu  = s - cogs - P["uk_fba"] - ref
    return ppu / cogs if cogs > 0 else 0


def calc_ca(p_eur, s_cad, P):
    """ROI for CA, computed in USD. DSF applies to referral + FBA fees."""
    cad_usd  = 1 / P["usd_cad"]
    cogs     = (p_eur + P["ca_ship"] + P["ca_lab"]) * P["eur_usd"]
    sell_usd = s_cad * cad_usd
    fba_usd  = P["ca_fba"] * cad_usd
    ref      = sell_usd * P["ca_ref"] / 100
    dsf      = (ref + fba_usd) * P["dsf"] / 100
    ppu      = sell_usd - cogs - ref - fba_usd - dsf
    return ppu / cogs if cogs > 0 else 0


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

KEEPA_DOMAINS = {"UK": 2, "CA": 6}
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
    return {
        "asin": p.get("asin"),
        "title": p.get("title"),
        "brand": p.get("brand"),
        "eans": p.get("eanList") or [],
        "buybox90": stat_price(stats, "avg90", IDX_BUY_BOX),
        "new90": stat_price(stats, "avg90", IDX_NEW),
        "rank30": stat_rank(stats, "avg30", IDX_SALES_RANK),
    }


def keepa_request(key, domain, codes, progress=None):
    """One /product call for up to 100 EANs.
    Waits for token refill on 429; retries transient network/5xx errors."""
    progress = progress or (lambda msg: None)
    params = {"key": key, "domain": domain, "code": ",".join(codes),
              "stats": 90, "history": 0}
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


def fetch_market(key, market, eans, cache, cache_hours, progress=None, cache_path=None):
    """Return ({ean: product_or_None}, tokens_left) for one market, cache-first."""
    progress = progress or (lambda msg: None)
    domain = KEEPA_DOMAINS[market]
    now = time.time()
    results, missing = {}, []
    for ean in eans:
        entry = cache.get(f"v2:{domain}:{ean}")
        if entry and cache_hours > 0 and now - entry["ts"] < cache_hours * 3600:
            results[ean] = entry["data"]
        else:
            missing.append(ean)

    tokens_left = None
    for i in range(0, len(missing), BATCH_SIZE):
        batch = missing[i:i + BATCH_SIZE]
        progress(f"{market}: fetching {i + 1}–{i + len(batch)} of {len(missing)} from Keepa…")
        data = keepa_request(key, domain, batch, progress)
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
        elif brand_col is None and "brand" in lc:
            brand_col = c
        elif title_col is None and any(w in lc for w in ("title", "desc", "product", "name")):
            title_col = c
        elif price_col is None and any(w in lc for w in ("price", "purchase", "eur", "cost")):
            price_col = c
    return ean_col, title_col, price_col, brand_col


def items_from_dataframe(raw):
    """Parse an offer table into analysis items.
    Returns (items, n_skipped, columns_info). Raises ValueError if the
    required columns can't be detected."""
    raw = raw.copy()
    raw.columns = [str(c).strip() for c in raw.columns]
    ean_col, title_col, price_col, brand_col = detect_columns(raw)
    if ean_col is None or price_col is None:
        raise ValueError(f"Could not detect required columns "
                         f"(EAN → {ean_col}, Title → {title_col}, Price → {price_col}). "
                         "Columns must include 'EAN' and 'Price'.")
    items, skipped, seen = [], 0, set()
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
    cols = {"ean": ean_col, "title": title_col, "price": price_col, "brand": brand_col}
    return items, skipped, cols


# ─── ANALYSIS ORCHESTRATION ───────────────────────────────────────────────────

def item_gate_ranks(item, matrix):
    """Gating entry + per-market rank for an item, using its provided brand."""
    gating = gating_for_brand(matrix, item.get("brand") or "")
    ranks = {m: (gating[m] if gating else GATE_CHECK) for m in KEEPA_DOMAINS}
    return gating, ranks


def build_fetch_plan(items, matrix, skip_hard_gated=True):
    """Which EANs to fetch per market. Hard-gated markets are skipped only when
    the item's brand was explicitly provided (a Keepa-derived brand isn't known
    until after the fetch). Returns (plan, skipped_pairs)."""
    plan = {m: [] for m in KEEPA_DOMAINS}
    skipped = set()
    for it in items:
        _, ranks = item_gate_ranks(it, matrix)
        for m in KEEPA_DOMAINS:
            if skip_hard_gated and it.get("brand") and ranks[m] == GATE_HARD:
                skipped.add((it["ean"], m))
                continue
            plan[m].append(it["ean"])
    return plan, skipped


RESULT_COLUMNS = ["Product", "Brand", "EAN", "Purchase (EUR)",
                  "ASIN CA", "Sell CA (CAD)", "Rank CA", "ROI CA", "Gating CA",
                  "ASIN UK", "Sell UK (GBP)", "Rank UK", "ROI UK", "Gating UK", "Notes"]


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
        if not brand:
            for market in KEEPA_DOMAINS:
                d = market_data.get(market, {}).get(it["ean"])
                if d and d.get("brand"):
                    brand = d["brand"]
                    break
        row["Brand"] = brand
        gating = gating_for_brand(matrix, brand)
        if gating is not None and brand and norm_brand(brand) != norm_brand(gating["display"]):
            notes.append(f"Gating matched matrix brand '{gating['display']}' — verify")
        gate_ranks = {}
        for market in KEEPA_DOMAINS:
            if gating is None:
                row[f"Gating {market}"] = GATE_UNKNOWN
                gate_ranks[market] = GATE_CHECK
            else:
                gate_ranks[market] = gating[market]
                row[f"Gating {market}"] = GATE_LABELS[gating[market]]
        row["_gate"] = min(gate_ranks.values())
        row["_gate_uk"] = gate_ranks["UK"]
        row["_gate_ca"] = gate_ranks["CA"]

        for market, cur, calc in [("UK", "GBP", calc_uk), ("CA", "CAD", calc_ca)]:
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
                roi = round(calc(it["price_eur"], sell, P) * 100, 1)
            row[f"ASIN {market}"] = asin
            row[f"Sell {market} ({cur})"] = round(sell, 2) if sell is not None else None
            row[f"Rank {market}"] = rank
            row[f"ROI {market}"] = roi

        if gating is not None and gating.get("note"):
            notes.append(f"Matrix: {gating['note']}")
        row["Notes"] = "; ".join(notes)
        rows.append(row)

    result_df = pd.DataFrame(rows)
    for col in ["Rank UK", "Rank CA"]:
        result_df[col] = pd.array(result_df[col], dtype=pd.Int64Dtype())
    for col in ["ROI UK", "ROI CA"]:
        result_df[col] = pd.to_numeric(result_df[col], errors="coerce")

    # Rank: sellable brands first (OK > can apply > to check > hard gated),
    # within each group by ROI CA desc, then ROI UK desc. A market's ROI only
    # counts toward ranking if the brand is sellable there.
    result_df["_roi_ca"] = result_df["ROI CA"].where(result_df["_gate_ca"] <= GATE_CHECK).fillna(-10**9)
    result_df["_roi_uk"] = result_df["ROI UK"].where(result_df["_gate_uk"] <= GATE_CHECK).fillna(-10**9)
    result_df = (result_df
                 .sort_values(["_gate", "_roi_ca", "_roi_uk"], ascending=[True, False, False])
                 .drop(columns=["_gate", "_gate_uk", "_gate_ca", "_roi_ca", "_roi_uk"])
                 .reset_index(drop=True))
    return result_df[[c for c in RESULT_COLUMNS if c in result_df.columns]]


ROI_THRESHOLD = 17.0   # % — an "opportunity" needs at least this ROI

STATUS_EXISTING = "🟢 Opportunities with existing listings found"
STATUS_UNGATING = "🟠 Opportunities found — ungating required"
STATUS_NEW_LAUNCH = "🆕 New launch — no listings on target markets; check if worth creating"
STATUS_NO_OPP = "⚪ No opportunities — ROI below threshold on existing listings"


def offer_status(result_df, roi_threshold=ROI_THRESHOLD):
    """Headline status for a processed MTO offer.

    Priority: sellable high-ROI products on existing listings > high-ROI behind
    a gating application > nothing listed at all (new launch) > listed but low ROI.
    Returns (status_label, counts dict).
    """
    ok_label = GATE_LABELS[GATE_OK]
    apply_label = GATE_LABELS[GATE_APPLY]
    n_existing = n_ungating = 0
    any_listing = False
    for _, r in result_df.iterrows():
        for m in ("UK", "CA"):
            asin = r.get(f"ASIN {m}")
            if asin is not None and pd.notna(asin):
                any_listing = True
            roi = r.get(f"ROI {m}")
            if roi is None or pd.isna(roi) or roi < roi_threshold:
                continue
            gate = r.get(f"Gating {m}")
            if gate == ok_label:
                n_existing += 1
            elif gate == apply_label:
                n_ungating += 1
    counts = {"existing": n_existing, "ungating": n_ungating,
              "any_listing": any_listing}
    if n_existing:
        return STATUS_EXISTING, counts
    if n_ungating:
        return STATUS_UNGATING, counts
    if not any_listing:
        return STATUS_NEW_LAUNCH, counts
    return STATUS_NO_OPP, counts


def analyze(items, keepa_key, params=None, matrix_df=None, cache_path=None,
            cache_hours=24, progress=None, skip_hard_gated=True):
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
                                               cache_hours, progress, cache_path)
        if tl is not None:
            tokens_left = tl
    result_df = build_result_df(items, market_data, matrix, params, skipped_pairs)
    return {"result_df": result_df, "market_data": market_data,
            "skipped_pairs": skipped_pairs, "tokens_left": tokens_left,
            "fetched": {m: len(e) for m, e in plan.items()}}
