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
    # Amazon's digital services fee follows the SELLER's country of establishment,
    # not the marketplace: UB is Spain-registered, so 3% on UK and CA alike.
    # Japan is kept separate at 2.5% (see jp_dsf) per the JP cost model.
    "dsf":     3.0,
    "uk_ship": 0.80,  "uk_lab": 2.35,  "uk_fba": 3.09, "uk_ref": 15.0, "uk_vat": 20.0,
    "ca_ship": 3.12,  "ca_lab": 2.35,  "ca_fba": 7.33, "ca_ref": 15.0,
    # US: landed cost = (goods + shipping) x (1 + tariff) + additional, per the
    # toolkit's costs.cogs_local(); tariff is ad-valorem on goods + shipping.
    "us_ship": 3.34, "us_add": 2.35, "us_tariff": 10.0,
    "us_fba": 5.43, "us_ref": 15.0,
    # actual customs clearance per unit (from the sheet; 0 unless looked up)
    "us_customs": 0.0, "uk_customs": 0.0, "ca_customs": 0.0,
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
    cogs = (p_eur + P["uk_ship"] + P["uk_lab"] + P.get("uk_customs", 0.0)) * rate
    s    = s_gbp / (1 + P["uk_vat"] / 100)
    ref  = s * P["uk_ref"] / 100
    dsf  = (ref + P["uk_fba"]) * P.get("dsf", 0.0) / 100
    ppu  = s - cogs - P["uk_fba"] - ref - dsf
    return ppu / cogs if cogs > 0 else 0


def calc_ca(p_eur, s_cad, P, is_dg=True):
    """ROI for CA, computed in USD. DSF applies to referral + FBA fees."""
    cad_usd  = 1 / P["usd_cad"]
    cogs     = (p_eur + P["ca_ship"] + P["ca_lab"] + P.get("ca_customs", 0.0)) * P["eur_usd"]
    sell_usd = s_cad * cad_usd
    fba_usd  = P["ca_fba"] * cad_usd
    ref      = sell_usd * P["ca_ref"] / 100
    dsf      = (ref + fba_usd) * P["dsf"] / 100
    ppu      = sell_usd - cogs - ref - fba_usd - dsf
    return ppu / cogs if cogs > 0 else 0


def calc_us(p_eur, s_usd, P, is_dg=True):
    """ROI for Amazon US, computed in USD.

    Keepa's US price is tax-exclusive (Amazon collects and remits US sales tax
    as marketplace facilitator), so nothing is stripped from the sell price.
    Landed COGS follows the toolkit's cost model: the ad-valorem import tariff
    applies to goods + shipping, then the per-market additional cost is added.

    The digital services fee here is charged on the referral fee ALONE — that is
    what UB's Seller Snap Costs tab shows for US (3.001% of referral across 116
    rows, Aug 2026). UK/CA in this file charge it on referral + FBA; if the US
    basis turns out to be the correct one everywhere, those two should change too.
    """
    cogs = ((p_eur + P["us_ship"]) * (1 + P["us_tariff"] / 100)
            + P["us_add"] + P.get("us_customs", 0.0)) * P["eur_usd"]
    ref = s_usd * P["us_ref"] / 100
    dsf = ref * P.get("dsf", 0.0) / 100
    ppu = s_usd - cogs - ref - dsf - P["us_fba"]
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
    "CA": {"domain": 6, "currency": "CAD", "calc": calc_ca, "price_divisor": 100,
           "fba_key": "ca_fba", "ship_key": "ca_ship",
           "customs_key": "ca_customs"},
    "UK": {"domain": 2, "currency": "GBP", "calc": calc_uk, "price_divisor": 100,
           "fba_key": "uk_fba", "ship_key": "uk_ship",
           "customs_key": "uk_customs"},
    # JP has no fba_key: its fulfilment sits inside the all-in additional cost,
    # so Keepa's JP FBA fee must NOT be added on top.
    "JP": {"domain": 5, "currency": "JPY", "calc": calc_jp, "price_divisor": 1},
    "US": {"domain": 1, "currency": "USD", "calc": calc_us, "price_divisor": 100,
           "fba_key": "us_fba", "ship_key": "us_ship",
           "customs_key": "us_customs"},
}
KEEPA_DOMAINS = {m: cfg["domain"] for m, cfg in MARKETS.items()}


def fmt_roi(val):
    if val is None or pd.isna(val):
        return "—"
    icon = "🟢" if val >= 20 else ("🟡" if val >= 10 else ("🟠" if val > 0 else "🔴"))
    return f"{icon} {val:.1f}%"


# ─── BRAND GATING ─────────────────────────────────────────────────────────────

MATRIX_MARKETS = ["US", "CA", "UK", "AU", "JP"]
STATUS_OPTIONS = ["", "ok", "has path to apply", "Hard Gated", "do not sell"]

GATE_OK, GATE_APPLY, GATE_CHECK, GATE_HARD = 0, 1, 2, 3
GATE_LABELS = {GATE_OK: "✅ OK", GATE_APPLY: "🟠 Gated — can apply",
               GATE_CHECK: "❓ To be checked", GATE_HARD: "🚫 Hard gated"}
GATE_UNKNOWN = "❓ Gating status to be checked"
GATE_NO_SELL_LABEL = "🚫 we do not sell this brand"


def classify_gating(text):
    """'do not sell' is our own decision (import complexity, brand policy)
    rather than an Amazon gate, but it excludes the brand just as firmly — the
    reason belongs in the Notes column."""
    tl = str(text).strip().lower() if text is not None else ""
    if "hard" in tl or "do not sell" in tl or "not sold" in tl or "dont sell" in tl:
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
            raw = str(r.get(market, "") or "").lower()
            entry[market] = classify_gating(r.get(market, ""))
            # excluded by our own decision, not by Amazon — say which
            if "sell" in raw and ("not" in raw or "dont" in raw):
                entry[f"{market}_label"] = GATE_NO_SELL_LABEL
        matrix[norm_brand(brand)] = entry
    return matrix


def brand_from_title(title, matrix):
    """Supplier titles lead with the brand ('ABERCROMBIE & FITCH AWAY WEEKEND
    ... EDP 100 ML'). When a title starts with a brand we know, that beats
    whatever brand the offer line carried — big multi-brand offers often label
    every row with the offer's own name, which would make gating meaningless.
    Returns the matrix's own spelling, or None when there is no confident match."""
    t = norm_brand(title)
    if not t:
        return None
    best = None
    for key, entry in matrix.items():          # longest match wins
        if len(key) >= 4 and t.startswith(key) and (best is None or len(key) > len(best[0])):
            best = (key, entry["display"])
    return best[1] if best else None


def infer_brands(items, matrix):
    """Fill in each item's brand from its title where the title names a brand we
    know. Returns how many were changed."""
    changed = 0
    for it in items:
        found = brand_from_title(it.get("title", ""), matrix)
        if found and norm_brand(found) != norm_brand(it.get("brand") or ""):
            it["brand"] = found
            changed += 1
    return changed


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


# ─── PER-EAN FREIGHT (from the COGS Shipping Calculator sheet) ────────────────
# The flat per-market shipping parameters are averages; real freight per unit
# ranges widely (US: 0.64–7.26 EUR), which is the single biggest reason a
# modeled COGS drifts from Seller Snap's actual. When a freight table is
# available, the product's own cost is used instead of the flat rate.
#
# Source of truth: the "COGS Shipping Calculator" Google Sheet, Output tab
# (90-day weighted average, refreshed daily). Its market codes differ from ours.

SHIPPING_MARKET_ALIASES = {"USA": "US", "US": "US", "CA": "CA", "UK": "UK",
                           "AU": "AU", "WM": "WM"}

# "COGS Shipping Calculator" — refreshed daily by finance's Apps Script.
# Output = 90-day weighted average shipping; Output_Customs is a different
# thing (180-day customs) and must not be used here.
SHIPPING_SHEET_ID = "15-xKszQNrnbsfEf_zqMkjtac7SUuo8-hmD-J1SW4azs"
SHIPPING_SHEET_RANGE = "Output!A2:D"      # EAN | Market | Product | Avg Cost per Unit EUR
CUSTOMS_SHEET_RANGE = "Output_Customs!A2:D"   # same shape, customs clearance per unit


def fetch_shipping_sheet(creds_info, sheet_id=SHIPPING_SHEET_ID,
                         rng=SHIPPING_SHEET_RANGE):
    """Live per-EAN freight straight from the sheet, via a read-only service
    account. `creds_info` is the service-account JSON as a dict. Raises on
    failure so the caller can fall back to the CSV/flat rates."""
    from google.oauth2 import service_account            # optional dependency
    from googleapiclient.discovery import build

    creds = service_account.Credentials.from_service_account_info(
        dict(creds_info), scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"])
    svc = build("sheets", "v4", credentials=creds, cache_discovery=False)
    rows = svc.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=rng).execute().get("values", [])
    table = {}
    for row in rows:
        if len(row) < 4:
            continue
        ean = normalize_ean(row[0])
        market = SHIPPING_MARKET_ALIASES.get(str(row[1]).strip().upper())
        cost = pd.to_numeric(str(row[3]).replace(",", "."), errors="coerce")
        if ean and market and pd.notna(cost) and cost >= 0:
            table[(ean, market)] = float(cost)
    return table


def resolve_shipping_table(creds_info=None, csv_path=None):
    """(freight, customs, source) — live sheet first, then a local CSV, then
    nothing. Never raises: an unreachable sheet degrades to CSV or flat rates."""
    if creds_info:
        try:
            table = fetch_shipping_sheet(creds_info)
            customs = {}
            try:
                customs = fetch_shipping_sheet(creds_info, rng=CUSTOMS_SHEET_RANGE)
            except Exception:
                pass          # customs is a bonus; freight alone is still useful
            if table:
                return table, customs, (f"live sheet ({len(table)} freight / "
                                        f"{len(customs)} customs rows)")
        except Exception as e:
            csv_table = load_shipping_table(csv_path)
            if csv_table:
                return csv_table, {}, f"local CSV ({len(csv_table)} rows) — sheet unreachable: {e}"
            return {}, {}, f"flat rates — sheet unreachable: {e}"
    table = load_shipping_table(csv_path)
    if table:
        return table, {}, f"local CSV ({len(table)} rows)"
    return {}, {}, "flat rates (no freight data)"


# ─── BRAND GATING MATRIX ──────────────────────────────────────────────────────
# "Amazon Global Selling Restrictions & Approvals", tab "brand selling
# approval" — maintained by the listing team. The status lives in the cell
# COLOUR, not the text: an empty orange cell means "gated, can apply", and the
# text in those cells is usually the reference ASIN to apply with. So we read
# the formatting, not just the values.
BRAND_SHEET_ID = "1aJbNQ71fUffSAR54kf6eokShWdkY2FrSk2x7DCdwtHE"
BRAND_SHEET_TAB = "brand selling approval"
BRAND_SHEET_URL = (f"https://docs.google.com/spreadsheets/d/{BRAND_SHEET_ID}"
                   "/edit?gid=576947683")
BRAND_COLORS = {                       # fill colour -> status
    (0.85, 0.92, 0.83): "ok",
    (0.96, 0.80, 0.80): "Hard Gated",
    (0.99, 0.90, 0.80): "has path to apply",
    (1.00, 0.90, 0.60): "has path to apply",     # "Need Approval" yellow
}
BRAND_STATUS_TEXTS = {"ok", "hard gated", "has path to apply", "gated", "do not sell"}


def _status_from_color(color, tol=0.10):
    """Nearest legend colour, so a slightly re-picked shade still resolves."""
    best, best_d = None, tol
    for ref, status in BRAND_COLORS.items():
        d = max(abs(a - b) for a, b in zip(color, ref))
        if d < best_d:
            best, best_d = status, d
    return best


def fetch_brand_sheet(creds_info, sheet_id=BRAND_SHEET_ID, tab=BRAND_SHEET_TAB):
    """Live brand matrix as a DataFrame (Brand/US/CA/UK/AU/JP/Notes). Raises on
    failure so the caller can fall back to the bundled snapshot."""
    from google.oauth2 import service_account            # optional dependency
    from googleapiclient.discovery import build

    creds = service_account.Credentials.from_service_account_info(
        dict(creds_info), scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"])
    svc = build("sheets", "v4", credentials=creds, cache_discovery=False)
    data = svc.spreadsheets().get(
        spreadsheetId=sheet_id, ranges=[f"'{tab}'!A1:H1000"],
        includeGridData=True, fields=("sheets/data/rowData/values/formattedValue,"
                                      "sheets/data/rowData/values/effectiveFormat/backgroundColor")
    ).execute()
    rows = data["sheets"][0]["data"][0].get("rowData", [])
    return brand_rows_to_df(rows)


def brand_rows_to_df(rows):
    """Sheets rowData -> matrix DataFrame. Split out so it can be tested
    without a network call."""
    def text(cell):
        return str(cell.get("formattedValue", "") or "").strip()

    def color(cell):
        bg = (cell.get("effectiveFormat") or {}).get("backgroundColor") or {}
        return tuple(round(bg.get(k, 1.0), 2) for k in ("red", "green", "blue"))

    header = [text(c).upper() for c in (rows[0].get("values", []) if rows else [])]
    cols = {}                                   # market -> column index
    for i, h in enumerate(header):
        for market in MATRIX_MARKETS:
            if h.replace("AMZ", "").strip() == market:
                cols[market] = i
    if not cols:
        raise ValueError(f"no market columns found in header {header!r}")

    out = []
    for row in rows[1:]:
        cells = row.get("values", [])
        if not cells or not text(cells[0]):
            continue
        rec = {"Brand": text(cells[0]), "Notes": ""}
        notes = []
        for market, i in cols.items():
            cell = cells[i] if i < len(cells) else {}
            body = text(cell)
            status = _status_from_color(color(cell)) or ""
            # Colour is the status; free text that isn't a status (reference
            # ASINs, "partially hard gated") is a note, not an override.
            if body and body.lower() not in BRAND_STATUS_TEXTS:
                notes.append(f"{market}: {body}")
            elif not status and body:
                status = body
            rec[market] = status
        for market in MATRIX_MARKETS:
            rec.setdefault(market, "")
        rec["Notes"] = " · ".join(notes)
        out.append(rec)
    return pd.DataFrame(out, columns=["Brand"] + MATRIX_MARKETS + ["Notes"])


def dedupe_brand_rows(df):
    """(df, conflicts) — the sheet is hand-maintained and has the same brand
    twice ("Paco Rabanne" / "paco rabanne", "Giorgio Armani" / "GIORGIO
    ARMANI"), sometimes with contradictory statuses. Merge them strictest-wins:
    a blank says nothing, but between "ok" and "Hard Gated" we take the gate.
    Guessing permissive on a gating call is how you buy stock you can't list."""
    severity = {"": 0, "ok": 1, "has path to apply": 2, "Hard Gated": 3, "do not sell": 3}
    merged, order, conflicts = {}, [], []
    for _, r in df.fillna("").iterrows():
        brand = str(r["Brand"]).strip()
        key = brand.casefold()
        if key not in merged:
            merged[key] = {"Brand": brand, "Notes": "",
                           **{m: "" for m in MATRIX_MARKETS}}
            order.append(key)
        row = merged[key]
        for m in MATRIX_MARKETS:
            new = str(r.get(m, "")).strip()
            old = row[m]
            if severity.get(new, 1) > severity.get(old, 1):
                if old and new:
                    conflicts.append(f"{brand} {m}: {old!r} vs {new!r} → {new!r}")
                row[m] = new
            elif old and new and old != new:
                conflicts.append(f"{brand} {m}: {old!r} vs {new!r} → {old!r}")
        note = str(r.get("Notes", "")).strip()
        if note and note not in row["Notes"]:
            row["Notes"] = f"{row['Notes']} · {note}" if row["Notes"] else note
    out = pd.DataFrame([merged[k] for k in order],
                       columns=["Brand"] + MATRIX_MARKETS + ["Notes"])
    return out, conflicts


def apply_brand_overrides(matrix_df, overrides_df):
    """Our own decisions layered on Amazon's gating — e.g. a brand we choose
    not to import. Overrides win; brands not in the sheet get added."""
    if overrides_df is None or overrides_df.empty:
        return matrix_df
    df = matrix_df.copy()
    index = {}
    for i, b in enumerate(df["Brand"]):          # every duplicate, not just one
        index.setdefault(str(b).strip().casefold(), []).append(i)
    for _, o in overrides_df.fillna("").iterrows():
        brand = str(o.get("Brand", "")).strip()
        if not brand:
            continue
        cells = {m: str(o.get(m, "")).strip() for m in MATRIX_MARKETS}
        cells = {m: v for m, v in cells.items() if v}
        note = str(o.get("Notes", "")).strip()
        targets = index.get(brand.casefold(), [])
        if not targets:
            df.loc[len(df)] = {"Brand": brand, "Notes": note,
                               **{m: cells.get(m, "") for m in MATRIX_MARKETS}}
            continue
        for i in targets:
            for m, v in cells.items():
                df.at[df.index[i], m] = v
            if note:
                prev = str(df.at[df.index[i], "Notes"] or "")
                df.at[df.index[i], "Notes"] = f"{note} · {prev}" if prev else note
    return df


def fill_matrix_gaps(live_df, snapshot_df):
    """(df, filled) — the sheet wins wherever it has a status, and the local
    snapshot fills only what the sheet leaves blank. The sheet doesn't track
    Japan at all and is missing brands we have researched ourselves, so
    replacing the snapshot outright would throw that knowledge away; a blank
    cell means "not recorded", not "no restriction"."""
    if snapshot_df is None or snapshot_df.empty:
        return live_df, 0
    df = live_df.copy()
    index = {str(b).strip().casefold(): i for i, b in enumerate(df["Brand"])}
    filled = 0
    for _, s in snapshot_df.fillna("").iterrows():
        brand = str(s.get("Brand", "")).strip()
        if not brand:
            continue
        i = index.get(brand.casefold())
        if i is None:
            df.loc[len(df)] = {"Brand": brand,
                               "Notes": str(s.get("Notes", "")).strip(),
                               **{m: str(s.get(m, "")).strip() for m in MATRIX_MARKETS}}
            filled += 1
            continue
        for m in MATRIX_MARKETS:
            val = str(s.get(m, "")).strip()
            if val and not str(df.at[df.index[i], m] or "").strip():
                df.at[df.index[i], m] = val
                filled += 1
    return df, filled


def resolve_brand_matrix(creds_info=None, csv_path=None, overrides_path=None):
    """(df, source) — the listing team's sheet is the source of truth, the
    bundled snapshot fills its gaps (and stands in entirely if it is
    unreachable), and our own exclusions win over both."""
    df, source = None, ""
    snapshot = None
    if csv_path and os.path.exists(csv_path):
        try:
            snapshot = pd.read_csv(csv_path, dtype=str).fillna("")
        except Exception:
            snapshot = None
    if creds_info:
        try:
            df = fetch_brand_sheet(creds_info)
            source = f"live sheet ({len(df)} brands)"
        except Exception as e:
            source = f"snapshot — sheet unreachable: {e}"
    if df is None or df.empty:
        if snapshot is None:
            raise FileNotFoundError(f"no brand matrix: {csv_path}")
        df = snapshot
        source = source or f"local snapshot ({len(df)} brands)"
    else:
        df, filled = fill_matrix_gaps(df, snapshot)
        if filled:
            source += f" + {filled} gap(s) from local snapshot"
    df, conflicts = dedupe_brand_rows(df)
    BRAND_CONFLICTS[:] = conflicts
    if conflicts:
        source += f" · {len(conflicts)} duplicate-row conflict(s) resolved strictest-wins"
    overrides = load_brand_overrides(overrides_path)
    if overrides is not None and not overrides.empty:
        df = apply_brand_overrides(df, overrides)
        source += f" + {len(overrides)} local override(s)"
    return df.fillna(""), source

BRAND_CONFLICTS = []          # last resolve's conflict detail, for the UI


def load_brand_overrides(path):
    if not path or not os.path.exists(path):
        return None
    try:
        return pd.read_csv(path, dtype=str).fillna("")
    except Exception:
        return None


def load_shipping_table(path):
    """{(ean, market): eur_per_unit} from a csv with ean,market,cost_per_unit_eur.
    Returns {} when the file is absent — callers then use the flat rates."""
    if not path or not os.path.exists(path):
        return {}
    table = {}
    try:
        df = pd.read_csv(path, dtype={"ean": str})
    except Exception:
        return {}
    for _, r in df.iterrows():
        ean = normalize_ean(r.get("ean"))
        market = SHIPPING_MARKET_ALIASES.get(str(r.get("market", "")).strip().upper())
        cost = pd.to_numeric(r.get("cost_per_unit_eur"), errors="coerce")
        if ean and market and pd.notna(cost) and cost >= 0:
            table[(ean, market)] = float(cost)
    return table


# ─── KEEPA CLIENT ─────────────────────────────────────────────────────────────

IDX_SALES_RANK = 3      # stats array index: sales rank
IDX_BB_OOS = 18         # out-of-stock array index: Buy Box (100 = no Buy Box at all)
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


def _oos_to_bb_days(stats, key="outOfStockPercentage30"):
    """% of the last 30 days that had a Buy Box. Keepa reports out-of-stock
    percentage per price type; -1 means no data."""
    arr = (stats or {}).get(key) or []
    if len(arr) > IDX_BB_OOS and arr[IDX_BB_OOS] >= 0:
        return round(100 - arr[IDX_BB_OOS])
    return None


def extract_product(p, price_divisor=100):
    """Reduce a Keepa product object to the fields we need.

    Beyond price and rank this captures the signals the repricing team reads
    off the Keepa chart by hand (Rita's 'Reading Keepa for Order Quantities'):
    who is actually selling, whether a Buy Box exists, and how often the rank
    drops — a rank drop is a sale, so it is the velocity proxy.
    """
    stats = p.get("stats") or {}
    fba = (p.get("fbaFees") or {}).get("pickAndPackFee")

    def stat_num(key):
        v = stats.get(key)
        return v if isinstance(v, (int, float)) and v >= 0 else None

    bb_price = stats.get("buyBoxPrice")
    return {
        # --- what is actually being sold right now -----------------------
        # Offers and Buy Box are checked BEFORE rank: Amazon keeps updating the
        # rank of listings nobody sells, so rank alone invents demand.
        "offers": stat_num("totalOfferCount"),
        "offers_fba": stat_num("offerCountFBA"),      # needs the offers param
        "bb_now": bb_price / price_divisor if isinstance(bb_price, (int, float)) and bb_price > 0 else None,
        "bb_is_fba": stats.get("buyBoxIsFBA"),
        "bb_is_amazon": stats.get("buyBoxIsAmazon"),
        "bb_days_30": _oos_to_bb_days(stats),         # % of last 30d with a Buy Box
        # --- demand ------------------------------------------------------
        "rank_drops_30": stat_num("salesRankDrops30"),
        "rank_drops_90": stat_num("salesRankDrops90"),
        "monthly_sold": p.get("monthlySold"),         # the "N+ bought" badge
        # --- identity risk ------------------------------------------------
        "n_barcodes": len(p.get("eanList") or []),
        # Real per-product FBA fee in marketplace currency. A flat sidebar
        # default cannot know a product's size/weight band; this can.
        "fba_fee": fba / price_divisor if fba else None,
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


ROI_THRESHOLD = 17.0   # % — an "opportunity" needs at least this ROI

# ─── DEMAND / SELLABILITY (Rita's "Reading Keepa for Order Quantities") ───────
# Order of checks is fixed: offers and Buy Box FIRST, rank SECOND. Amazon keeps
# updating the rank of listings nobody sells, so reading demand off the rank of
# a dead listing invents it.

# The positive verdicts are split by whether we may actually sell the brand on
# that market: a great ROI on a brand we cannot list is not an opportunity, and
# a brand missing from the matrix is a lead, not a decision.
VERDICT_BUY = "🟢 buy candidate"                       # brand ungated on that market
VERDICT_SOFT = "🟠 buy candidate — ungating required"  # soft-gated, path to apply
VERDICT_POSSIBLE = "🔵 possible opportunity — gating unknown"
VERDICT_OPEN = "open field — we would set the price"
VERDICT_NO_DEMAND = "🔴 no demand"
VERDICT_PRICE_GAP = "🔴 sells, but not at a price that pays"
VERDICT_DEAD = "⚫ nobody selling"
VERDICT_LOW_ROI = "⚪ below ROI bar"
VERDICT_GATED = "🚫 gated"


def breakeven_sell(market, p_eur, P, is_dg=True, roi_target=None, fba_fee=None):
    """The sell price (market currency) at which ROI hits the bar — 'the price
    needed for 17% is only C$42.40'. Solved numerically so it stays correct for
    whatever each market's formula does."""
    roi_target = (ROI_THRESHOLD if roi_target is None else roi_target) / 100
    cfg = MARKETS[market]
    P = dict(P)
    if fba_fee is not None and cfg.get("fba_key"):
        P[cfg["fba_key"]] = fba_fee
    calc = cfg["calc"]
    lo, hi = 0.01, max(p_eur * 60, 1000.0)
    for _ in range(60):
        mid = (lo + hi) / 2
        if calc(p_eur, mid, P, is_dg) < roi_target:
            lo = mid
        else:
            hi = mid
    return round(hi, 2)


def _positive_verdict(gate_rank):
    """Which flavour of 'worth buying' applies, given what we know about gating."""
    return {GATE_OK: VERDICT_BUY, GATE_APPLY: VERDICT_SOFT}.get(gate_rank, VERDICT_POSSIBLE)


def market_verdict(d, roi, gate_rank, roi_threshold=ROI_THRESHOLD):
    """(verdict, est_units_per_month, note) for one product on one market.

    d is the extracted Keepa record; roi is the computed ROI %; gate_rank the
    gating level. Follows the fixed order: gated → selling? → demand? → money?
    """
    if gate_rank == GATE_HARD:
        return VERDICT_GATED, None, ""
    if not d:
        return VERDICT_DEAD, None, "no listing found"

    offers = d.get("offers")
    bb_days = d.get("bb_days_30")
    drops30, drops90 = d.get("rank_drops_30"), d.get("rank_drops_90")

    # 1. is anyone selling at all?
    nobody_selling = (offers in (0, None)) and (bb_days in (0, None))
    # 2. is anyone buying? (a rank drop is a sale)
    no_demand = (drops30 == 0 and (drops90 or 0) == 0)

    # Our share divides by FULFILMENT competitors, not by every offer: a
    # listing whose Buy Box is held by a distance seller hands us the market,
    # because fulfilment normally takes the box from a distance seller at a
    # comparable price. That is the optimistic end, so quote both (Rita §7.6).
    units = d.get("monthly_sold") or drops30
    fba_n = d.get("offers_fba")
    if fba_n is None:
        # exact FBA count needs the pricier offers call; infer from who holds
        # the box — no FBA Buy Box and no FBA data means no fulfilment rival
        fba_n = (offers or 0) if d.get("bb_is_fba") else 0
    share = units / (fba_n + 1) if units is not None else None
    cautious = round(share / 2, 1) if (share and not fba_n and offers) else None

    if nobody_selling:
        if no_demand:
            return VERDICT_DEAD, None, "no offers and no sales history"
        # Nobody is selling, so we would set the price — but the only evidence
        # of demand is the price it actually sold at. If the return there is
        # below the bar, real demand and a workable price do not overlap.
        if roi is not None and roi < roi_threshold:
            return (VERDICT_PRICE_GAP, share,
                    f"sold {units}/mo with nobody selling now, but only "
                    f"{roi:.0f}% ROI at the price it sold at")
        return (_positive_verdict(gate_rank), share,
                f"{VERDICT_OPEN}; confirm the price it actually sold at still "
                f"clears the bar")
    if no_demand:
        return VERDICT_NO_DEMAND, 0, f"{offers or 0} offer(s) but no rank drops in 90 days"
    if roi is None or roi < roi_threshold:
        return VERDICT_LOW_ROI, share, ""
    return (_positive_verdict(gate_rank), share,
            f"{units}/mo market, {offers or 0} offer(s)"
            + ("" if bb_days is None else f", Buy Box {bb_days}% of 30d")
            + ("" if cautious is None else
               f" — box held by a distance seller, cautious estimate {cautious}/mo"))


# ─── STATUS CLASSIFICATION ────────────────────────────────────────────────────


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


RESULT_COLUMNS = (["Product", "Brand", "EAN", "Purchase (EUR)", "Status",
                   "Verdict", "Why", "Est units/mo"]
                  + [f"{field} {m}" for m in MARKETS
                     for field in ("ASIN", "Sell", "Breakeven", "ROI", "Gating",
                                   "Offers", "BB days", "Drops30")]
                  + ["Notes"])


def build_result_df(items, market_data, matrix, params, skipped_pairs=None,
                    shipping_table=None, customs_table=None):
    """Assemble + rank the result table. Pure function of its inputs, so the UI
    can re-rank with new params without re-fetching."""
    skipped_pairs = skipped_pairs or set()
    shipping_table = shipping_table or {}
    customs_table = customs_table or {}
    P = {**DEFAULT_PARAMS, **(params or {})}
    rows = []
    for it in items:
        row = {"Product": it["title"], "EAN": it["ean"],
               "Purchase (EUR)": round(it["price_eur"], 2)}
        notes = []
        verdicts = {}

        # Brand resolution order: the offer's own brand if the matrix knows it,
        # otherwise Keepa's. Big multi-brand offers label every row with the
        # offer name ("PERFUMES & SKINCARE"), which tells us nothing about
        # gating — Keepa's brand for the matched ASIN does.
        brand = it.get("brand") or None
        keepa_title = ""
        keepa_brand = None
        for market in MARKETS:
            d = market_data.get(market, {}).get(it["ean"])
            if d and d.get("brand"):
                keepa_brand = d["brand"]
                break
        if keepa_brand and (not brand or gating_for_brand(matrix, brand) is None):
            brand = keepa_brand
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
                row[f"Gating {market}"] = gating.get(f"{market}_label") or GATE_LABELS[gating[market]]
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
            # Per-product cost inputs, built whether or not we have a sell
            # price — the break-even price needs them too.
            P_market = P
            fba_key = cfg.get("fba_key")
            if fba_key and d and d.get("fba_fee"):
                P_market = {**P, fba_key: d["fba_fee"]}
            elif fba_key and d and sell is not None:
                notes.append(f"{market}: FBA fee is the flat default")
            ship_key = cfg.get("ship_key")
            if ship_key:
                real_ship = shipping_table.get((it["ean"], market))
                if real_ship is not None:
                    P_market = {**P_market, ship_key: real_ship}
                elif shipping_table and sell is not None:
                    notes.append(f"{market}: freight is the flat default "
                                 f"({P[ship_key]:.2f} EUR)")
            customs_key = cfg.get("customs_key")
            if customs_key:
                real_customs = customs_table.get((it["ean"], market))
                if real_customs:
                    P_market = {**P_market, customs_key: real_customs}
            if sell is not None:
                roi = round(calc(it["price_eur"], sell, P_market, is_dg) * 100, 1)
            row[f"ASIN {market}"] = asin
            row[f"Sell {market} ({cur})"] = round(sell, 2) if sell is not None else None
            row[f"Sell {market}"] = row[f"Sell {market} ({cur})"]
            row[f"Rank {market}"] = rank
            row[f"ROI {market}"] = roi
            # what the repricing team reads off the Keepa chart by hand
            row[f"Offers {market}"] = (d or {}).get("offers")
            row[f"BB days {market}"] = (d or {}).get("bb_days_30")
            row[f"Drops30 {market}"] = (d or {}).get("rank_drops_30")
            row[f"Breakeven {market}"] = (
                breakeven_sell(market, it["price_eur"], P_market, is_dg,
                               fba_fee=(d or {}).get("fba_fee"))
                if (it["ean"], market) not in skipped_pairs and gate_ranks[market] != GATE_HARD
                else None)
            verdicts[market] = market_verdict(d, roi, gate_ranks[market])

        if gating is not None and gating.get("note"):
            notes.append(f"Matrix: {gating['note']}")
        row["Notes"] = "; ".join(notes)
        row["Status"] = product_status(row)

        # one verdict per product: the best market wins, and says which
        order = [VERDICT_BUY, VERDICT_SOFT, VERDICT_POSSIBLE, VERDICT_LOW_ROI,
                 VERDICT_PRICE_GAP, VERDICT_NO_DEMAND, VERDICT_DEAD, VERDICT_GATED]
        best = min(verdicts.items(), key=lambda kv: order.index(kv[1][0])) if verdicts else None
        if best:
            market, (verdict, units, why) = best
            row["Verdict"] = (f"{verdict} ({market})"
                              if verdict in (VERDICT_BUY, VERDICT_SOFT, VERDICT_POSSIBLE)
                              else verdict)
            row["Why"] = why
            row["Est units/mo"] = round(units, 1) if units else None
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
            cache_hours=24, progress=None, skip_hard_gated=True, buybox=True,
            shipping_path=None, shipping_creds=None, two_pass=True):
    """End-to-end: gating pre-check → Keepa fetch → ranked result table.

    Returns dict with result_df, market_data, skipped_pairs, tokens_left,
    fetched (per-market fetch counts)."""
    progress = progress or (lambda msg: None)
    matrix = matrix_from_df(matrix_df)
    infer_brands(items, matrix)      # supplier titles lead with the brand
    plan, skipped_pairs = build_fetch_plan(items, matrix, skip_hard_gated)
    cache = load_cache(cache_path)
    market_data, tokens_left = {}, None

    # Pass 1 is deliberately the cheap call (1 token vs 3). It already answers
    # the two questions that eliminate most products — is anyone selling
    # (offer count) and is anyone buying (rank drops) — so Buy Box data is only
    # bought for the few rows that survive. Same order of checks a human uses,
    # applied to the token budget.
    first_pass_buybox = buybox and not two_pass
    for market, eans in plan.items():
        market_data[market], tl = fetch_market(keepa_key, market, eans, cache,
                                               cache_hours, progress, cache_path,
                                               first_pass_buybox)
        if tl is not None:
            tokens_left = tl

    if buybox and two_pass:
        triage = build_result_df(items, market_data, matrix, params, skipped_pairs)
        roi_bar = (params or {}).get("roi_threshold", ROI_THRESHOLD)
        wanted = {m: [] for m in MARKETS}
        for _, r in triage.iterrows():
            for m in MARKETS:
                roi = r.get(f"ROI {m}")
                d = market_data.get(m, {}).get(str(r["EAN"]))
                promising = (roi is not None and pd.notna(roi) and roi >= roi_bar)
                open_field = bool(d) and not d.get("offers") and (d.get("rank_drops_90") or 0) > 0
                if promising or open_field:
                    wanted[m].append(str(r["EAN"]))
        n = sum(len(v) for v in wanted.values())
        progress(f"triage: {n} of {sum(len(v) for v in plan.values())} lookups worth "
                 f"Buy Box detail ({n * 2} extra tokens)")
        for market, eans in wanted.items():
            if not eans:
                continue
            detail, tl = fetch_market(keepa_key, market, eans, cache, cache_hours,
                                      progress, cache_path, True)
            market_data[market].update({k: v for k, v in detail.items() if v})
            if tl is not None:
                tokens_left = tl
    shipping_table, customs_table, shipping_source = resolve_shipping_table(
        shipping_creds, shipping_path)
    progress(f"freight: {shipping_source}")
    result_df = build_result_df(items, market_data, matrix, params, skipped_pairs,
                                shipping_table, customs_table)
    return {"result_df": result_df, "market_data": market_data,
            "skipped_pairs": skipped_pairs, "tokens_left": tokens_left,
            "fetched": {m: len(e) for m, e in plan.items()},
            "shipping_rows": len(shipping_table), "shipping_source": shipping_source}
