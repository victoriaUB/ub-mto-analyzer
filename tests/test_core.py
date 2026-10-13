"""Tests for core.py — pure logic, no network.

Run with pytest (CI) or directly: python3 tests/test_core.py
"""

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import core  # noqa: E402

P = dict(core.DEFAULT_PARAMS)


# ─── EAN normalization ────────────────────────────────────────────────────────

def test_normalize_ean():
    assert core.normalize_ean("3348901234567") == "3348901234567"
    assert core.normalize_ean(3348901234567.0) == "3348901234567"
    assert core.normalize_ean("848901234567") == "0848901234567"   # 12-digit UPC padded
    assert core.normalize_ean(" 3348-9012-34567 ") == "3348901234567"
    assert core.normalize_ean("12345678") == "12345678"            # EAN-8 kept
    assert core.normalize_ean("abc") is None
    assert core.normalize_ean(None) is None


# ─── Column detection / file parsing ──────────────────────────────────────────

def test_detect_columns():
    df = pd.DataFrame(columns=["Brand", "EAN Code", "Offer Description", "Purchase price EUR"])
    assert core.detect_columns(df) == ("EAN Code", "Offer Description", "Purchase price EUR", "Brand")
    df2 = pd.DataFrame(columns=["barcode", "Product Name", "Cost"])
    assert core.detect_columns(df2) == ("barcode", "Product Name", "Cost", None)


def test_items_from_dataframe():
    df = pd.DataFrame({
        "EAN": ["3348901234567", "3348901234567", "bad", "8411061111321"],
        "Brand": ["Dior", "Dior", "X", "Carolina Herrera"],
        "Price EUR": [50.0, 50.0, 10.0, None],
    })
    items, skipped, cols = core.items_from_dataframe(df)
    assert len(items) == 1                      # dupe dropped, bad EAN + no-price skipped
    assert skipped == 2
    assert items[0]["brand"] == "Dior"
    assert cols["ean"] == "EAN"


def test_items_from_dataframe_missing_columns():
    try:
        core.items_from_dataframe(pd.DataFrame({"foo": [1]}))
        assert False, "should raise"
    except ValueError as e:
        assert "EAN" in str(e)


# ─── ROI formulas (pinned against the UB cost calculator reconciliation) ──────

def test_calc_uk_matches_calculator_when_dsf_off():
    """With uk_dsf=0 the formula reproduces the UB cost calculator exactly
    (-6.0%) — the calculator shows the digital services fee but does not
    deduct it from profit."""
    p = {**P, "eur_gbp": 0.8558, "uk_ship": 0.80, "uk_lab": 2.35,
         "uk_fba": 3.09, "uk_ref": 15.0, "uk_vat": 20.0, "dsf": 0.0}
    assert abs(core.calc_uk(85.45, 104.94, p) * 100 - (-6.0)) < 0.1


def test_calc_uk_deducts_digital_services_fee():
    """Default behaviour: the fee IS deducted, so ROI is ~1pp lower."""
    base = {**P, "eur_gbp": 0.8558, "uk_ship": 0.80, "uk_lab": 2.35,
            "uk_fba": 3.09, "uk_ref": 15.0, "uk_vat": 20.0}
    off = core.calc_uk(85.45, 104.94, {**base, "dsf": 0.0}) * 100
    on = core.calc_uk(85.45, 104.94, {**base, "dsf": 3.0}) * 100
    assert on < off
    assert abs((off - on) - 0.64) < 0.05        # 3% of (referral + FBA), over COGS
    assert core.DEFAULT_PARAMS["dsf"] == 3.0        # Spain-registered → 3% on UK + CA
    assert core.DEFAULT_PARAMS["jp_dsf"] == 2.5     # JP kept separate


def test_calc_ca_positive():
    roi = core.calc_ca(30.0, 119.99, P)
    assert roi > 0.4                            # sanity: healthy margin case


# ─── Gating ───────────────────────────────────────────────────────────────────

def _matrix():
    return core.matrix_from_df(pd.DataFrame({
        "Brand": ["Givenchy", "Thierry Mugler", "Christian Dior", "Armani Beauty"],
        "US": ["ok", "Hard Gated", "Hard Gated", "Hard Gated"],
        "CA": ["ok", "Hard Gated", "has path to apply", "Hard Gated"],
        "UK": ["ok", "Hard Gated", "has path to apply", "has path to apply"],
        "AU": ["", "", "", ""],
        "JP": ["", "", "", ""],
        "Notes": ["", "", "", "perfumes gated"],
    }))


def test_classify_gating():
    assert core.classify_gating("ok") == core.GATE_OK
    assert core.classify_gating("Hard Gated") == core.GATE_HARD
    assert core.classify_gating("has path to apply") == core.GATE_APPLY
    assert core.classify_gating("") == core.GATE_CHECK
    assert core.classify_gating(None) == core.GATE_CHECK


def test_gating_exact_and_accent_insensitive():
    m = _matrix()
    assert core.gating_for_brand(m, "GIVENCHY")["display"] == "Givenchy"
    assert core.gating_for_brand(m, "givenchy ")["display"] == "Givenchy"


def test_gating_substring_fallback():
    m = _matrix()
    # "Dior" should reach "Christian Dior" via substring
    entry = core.gating_for_brand(m, "Dior")
    assert entry is not None and entry["display"] == "Christian Dior"
    assert core.gating_for_brand(m, "Unknown Brand XYZ") is None


# ─── Fetch plan (token saving) ────────────────────────────────────────────────

def test_build_fetch_plan_skips_hard_gated():
    m = _matrix()
    items = [
        {"ean": "1", "brand": "Thierry Mugler", "title": "", "price_eur": 10},  # hard both
        {"ean": "2", "brand": "Givenchy", "title": "", "price_eur": 10},        # ok both
        {"ean": "3", "brand": "", "title": "", "price_eur": 10},                # no brand → fetch
    ]
    plan, skipped = core.build_fetch_plan(items, m, skip_hard_gated=True)
    assert plan["UK"] == ["2", "3"]
    assert plan["CA"] == ["2", "3"]
    assert ("1", "UK") in skipped and ("1", "CA") in skipped

    plan_all, skipped_none = core.build_fetch_plan(items, m, skip_hard_gated=False)
    assert plan_all["UK"] == ["1", "2", "3"] and not skipped_none


# ─── Keepa product extraction ─────────────────────────────────────────────────

def test_extract_product():
    p = core.extract_product({
        "asin": "B0TEST", "title": "T", "brand": "Givenchy",
        "eanList": ["3348901234567"],
        "stats": {"avg90": [0] * 18 + [8999], "avg30": [0, 0, 0, 4521]},
    })
    assert p["buybox90"] == 89.99 and p["rank30"] == 4521 and p["new90"] is None


def test_extract_product_null_handling():
    p = core.extract_product({"asin": "B0X", "title": "t", "eanList": [],
                              "stats": {"avg90": [None, 7550], "avg30": [None, None, None, -1]}})
    assert p["buybox90"] is None and p["new90"] == 75.50 and p["rank30"] is None


# ─── Result assembly + ranking ────────────────────────────────────────────────

def _mk(brand, bb, rank=500):
    return {"asin": "B0T", "title": "Some title", "brand": brand, "eans": [],
            "buybox90": bb, "new90": None, "rank30": rank}


def test_build_result_df_ranking_and_notes():
    m = _matrix()
    items = [
        {"ean": "1", "title": "", "price_eur": 50.0, "brand": "Givenchy"},
        {"ean": "2", "title": "", "price_eur": 40.0, "brand": "Thierry Mugler"},
        {"ean": "3", "title": "", "price_eur": 45.0, "brand": "Dior"},
    ]
    market_data = {
        "UK": {"1": _mk("GIVENCHY", 120.0), "3": _mk("Christian Dior", 90.0)},
        "CA": {"1": _mk("GIVENCHY", 180.0), "3": _mk("Christian Dior", 130.0)},
    }
    skipped = {("2", "UK"), ("2", "CA")}
    df = core.build_result_df(items, market_data, m, P, skipped)

    # Givenchy (OK) first, Dior (apply) second, Mugler (hard) last
    assert list(df["Brand"]) == ["Givenchy", "Dior", "Thierry Mugler"]
    mugler = df[df["Brand"] == "Thierry Mugler"].iloc[0]
    assert "skipped (hard-gated)" in mugler["Notes"]
    assert pd.isna(mugler["ROI CA"])
    dior = df[df["Brand"] == "Dior"].iloc[0]
    assert "matched matrix brand 'Christian Dior'" in dior["Notes"]


def test_build_result_df_manual_brand_beats_keepa():
    m = _matrix()
    items = [{"ean": "1", "title": "", "price_eur": 50.0, "brand": "Givenchy"}]
    market_data = {"UK": {"1": _mk("WrongBrand", 120.0)}, "CA": {"1": _mk("WrongBrand", 180.0)}}
    df = core.build_result_df(items, market_data, m, P)
    assert df.iloc[0]["Brand"] == "Givenchy"
    assert df.iloc[0]["Gating UK"] == core.GATE_LABELS[core.GATE_OK]


def test_build_result_df_keepa_brand_fallback():
    m = _matrix()
    items = [{"ean": "1", "title": "", "price_eur": 50.0, "brand": ""}]
    market_data = {"UK": {"1": _mk("GIVENCHY", 120.0)}, "CA": {}}
    df = core.build_result_df(items, market_data, m, P)
    assert df.iloc[0]["Brand"] == "GIVENCHY"


# ─── Offer status ─────────────────────────────────────────────────────────────

def _status_df(rows):
    """Fill in any missing market columns so rows are well-formed."""
    ok = core.GATE_LABELS[core.GATE_OK]
    full = []
    for r in rows:
        row = dict(r)
        for m in core.MARKETS:
            row.setdefault(f"ASIN {m}", None)
            row.setdefault(f"ROI {m}", None)
            row.setdefault(f"Gating {m}", ok)
        full.append(row)
    return pd.DataFrame(full)


def test_status_existing_listings():
    df = _status_df([{"ASIN UK": "B1", "ROI UK": 25.0, "Gating UK": core.GATE_LABELS[core.GATE_OK],
                      "ASIN CA": None, "ROI CA": None, "Gating CA": core.GATE_LABELS[core.GATE_HARD]}])
    status, counts = core.offer_status(df)
    assert status.startswith(core.STATUS_EXISTING) and counts[core.PSTATUS_EXISTING] == 1


def test_status_ungating_required():
    df = _status_df([{"ASIN UK": "B1", "ROI UK": 25.0, "Gating UK": core.GATE_LABELS[core.GATE_APPLY],
                      "ASIN CA": "B2", "ROI CA": 5.0, "Gating CA": core.GATE_LABELS[core.GATE_OK]}])
    status, _ = core.offer_status(df)
    assert status == core.STATUS_UNGATING


def test_status_new_launch():
    df = _status_df([{"ASIN UK": None, "ROI UK": None, "Gating UK": core.GATE_LABELS[core.GATE_OK],
                      "ASIN CA": None, "ROI CA": None, "Gating CA": core.GATE_LABELS[core.GATE_OK]}])
    status, _ = core.offer_status(df)
    assert status == core.STATUS_NEW_LAUNCH


def test_status_no_opportunities():
    df = _status_df([{"ASIN UK": "B1", "ROI UK": 3.0, "Gating UK": core.GATE_LABELS[core.GATE_OK],
                      "ASIN CA": "B2", "ROI CA": -5.0, "Gating CA": core.GATE_LABELS[core.GATE_OK]}])
    status, _ = core.offer_status(df)
    assert status == core.STATUS_NO_OPP


def test_status_mixed_offer_breakdown():
    ok = core.GATE_LABELS[core.GATE_OK]
    df = _status_df([
        {"ASIN UK": "B1", "ROI UK": 30.0, "Gating UK": ok, "ASIN CA": None, "ROI CA": None, "Gating CA": ok},
        {"ASIN UK": "B2", "ROI UK": 20.0, "Gating UK": core.GATE_LABELS[core.GATE_APPLY],
         "ASIN CA": None, "ROI CA": None, "Gating CA": ok},
        {"ASIN UK": None, "ROI UK": None, "Gating UK": ok, "ASIN CA": None, "ROI CA": None, "Gating CA": ok},
        {"ASIN UK": "B3", "ROI UK": 2.0, "Gating UK": ok, "ASIN CA": None, "ROI CA": None, "Gating CA": ok},
    ])
    counts = core.status_counts(df)
    assert counts[core.PSTATUS_EXISTING] == 1
    assert counts[core.PSTATUS_UNGATING] == 1
    assert counts[core.PSTATUS_NEW] == 1
    assert counts[core.PSTATUS_LOW] == 1
    lines = core.status_summary_lines(df)
    assert len(lines) == 4
    assert lines[0] == "1 EANs for ungated brands with ROI above 17%"
    assert "soft-gated" in lines[1]
    assert "no listings on target markets" in lines[2]


def test_product_status_column_in_results():
    m = _matrix()
    items = [{"ean": "1", "title": "", "price_eur": 10.0, "brand": "Givenchy"}]
    market_data = {"UK": {"1": _mk("GIVENCHY", 120.0)}, "CA": {}}
    df = core.build_result_df(items, market_data, m, P)
    assert "Status" in df.columns
    assert df.iloc[0]["Status"] == core.PSTATUS_EXISTING   # huge ROI, ungated


def test_status_threshold_boundary():
    df = _status_df([{"ASIN UK": "B1", "ROI UK": 17.0, "Gating UK": core.GATE_LABELS[core.GATE_OK],
                      "ASIN CA": None, "ROI CA": None, "Gating CA": core.GATE_LABELS[core.GATE_OK]}])
    assert core.offer_status(df)[0] == core.STATUS_EXISTING          # 17.0 counts
    assert core.offer_status(df, roi_threshold=17.1)[0] == core.STATUS_NO_OPP


def test_status_hard_gated_all_markets():
    hard = core.GATE_LABELS[core.GATE_HARD]
    df = _status_df([{f"Gating {m}": hard for m in core.MARKETS}])
    assert core.offer_status(df)[0].startswith("🚫")
    assert core.status_summary_lines(df)[0].startswith("1 EANs hard-gated")


# ─── United States ────────────────────────────────────────────────────────────

def test_calc_us_fee_stack_matches_seller_snap():
    """Fee side verified against UB's Seller Snap Costs tab (Aug 2026):
    price $98.99, landed cost $66.49, FBA $5.43 -> profit $11.77, ROI 17.71%.
    Fed the same landed cost by back-solving the goods price."""
    p = {**P, "eur_usd": 1.169}
    # goods price that produces exactly $66.49 landed
    goods = ((66.49 / p["eur_usd"]) - p["us_add"]) / 1.10 - p["us_ship"]
    roi = core.calc_us(goods, 98.99, p) * 100
    assert abs(roi - 17.71) < 0.05


def test_calc_us_tariff_is_in_cogs():
    p = {**P, "eur_usd": 1.169}
    with_tariff = core.calc_us(46.03, 98.99, p)
    without = core.calc_us(46.03, 98.99, {**p, "us_tariff": 0.0})
    assert without > with_tariff          # tariff raises COGS, lowers ROI


def test_calc_us_no_tax_stripped_from_sell_price():
    """US sell price is tax-exclusive: doubling it must roughly double revenue,
    with no VAT divisor involved."""
    p = {**P, "eur_usd": 1.169, "us_ref": 0.0, "us_fba": 0.0, "dsf": 0.0}
    cogs = ((46.03 + p["us_ship"]) * 1.10 + p["us_add"]) * p["eur_usd"]
    roi = core.calc_us(46.03, 100.0, p)
    assert abs(roi - (100.0 - cogs) / cogs) < 1e-9


def test_extract_product_captures_fba_fee():
    p = core.extract_product({"asin": "A", "title": "t", "eanList": [],
                              "fbaFees": {"pickAndPackFee": 611},
                              "stats": {"avg90": [0]*18 + [23494], "avg30": [0, 0, 0, 900]}}, 100)
    assert p["fba_fee"] == 6.11
    # JPY: whole-yen divisor applies to fees too
    j = core.extract_product({"asin": "A", "title": "t", "eanList": [],
                              "fbaFees": {"pickAndPackFee": 550}, "stats": {}}, 1)
    assert j["fba_fee"] == 550
    assert core.extract_product({"asin": "A", "eanList": [], "stats": {}})["fba_fee"] is None


def test_per_product_fba_fee_beats_flat_default():
    """Keepa's real fee must win; without it, the row says so."""
    m = core.matrix_from_df(pd.DataFrame({"Brand": ["Prada"], "US": ["ok"], "CA": ["ok"],
                                          "UK": ["ok"], "AU": [""], "JP": [""], "Notes": [""]}))
    items = [{"ean": "1", "title": "Prada EDP", "price_eur": 109.13, "brand": "Prada"}]

    def mk(fee):
        return {"asin": "A1", "title": "t", "brand": "Prada", "eans": [], "buybox90": 234.94,
                "new90": None, "rank30": 900, "fba_fee": fee}

    p = {**P, "eur_usd": 1.169}
    with_fee = core.build_result_df(items, {"US": {"1": mk(6.11)}}, m, p).iloc[0]
    without = core.build_result_df(items, {"US": {"1": mk(None)}}, m, p).iloc[0]
    assert with_fee["ROI US"] < without["ROI US"]          # 6.11 > 5.43 default
    assert "flat default" in without["Notes"]
    assert "flat default" not in (with_fee["Notes"] or "")


def test_jp_ignores_keepa_fba_fee():
    """JP fulfilment is inside the all-in additional cost — adding Keepa's JP
    FBA fee on top would double-count it."""
    assert "fba_key" not in core.MARKETS["JP"]
    assert all("fba_key" in core.MARKETS[m] for m in ("US", "UK", "CA"))


def test_us_market_registered():
    assert core.MARKETS["US"]["domain"] == 1          # Keepa domain for amazon.com
    assert core.MARKETS["US"]["price_divisor"] == 100  # USD has cents, unlike JPY
    assert "ROI US" in core.RESULT_COLUMNS and "Gating US" in core.RESULT_COLUMNS
    assert "US" in core.MATRIX_MARKETS                 # gating column already exists


# ─── Japan ────────────────────────────────────────────────────────────────────

def test_calc_jp_matches_worked_example():
    """Victoria's worked example: sell 145.82 EUR incl 10% tax, purchase 78.47
    EUR ex-VAT, DG cost 35.32 -> ROI 2.83%."""
    p = {**P, "eur_jpy": 170.0}
    roi = core.calc_jp(78.47, 145.82 * 170.0, p, is_dg=True)
    assert abs(roi * 100 - 2.83) < 0.05


def test_calc_jp_ndg_cheaper_than_dg():
    p = {**P, "eur_jpy": 170.0}
    dg = core.calc_jp(78.47, 145.82 * 170.0, p, is_dg=True)
    ndg = core.calc_jp(78.47, 145.82 * 170.0, p, is_dg=False)
    assert ndg > dg


def test_dangerous_goods_classification():
    assert core.is_dangerous_goods("GOOD GIRL EDP 80ML") == (True, True)
    assert core.is_dangerous_goods("L'INTERDIT ELIXIR 50ML") == (True, True)
    assert core.is_dangerous_goods("Hydrating Face Cream 50ml") == (False, True)
    is_dg, certain = core.is_dangerous_goods("GIFT SET 3 PCS")
    assert is_dg and not certain          # unclear -> conservative DG


def test_jp_market_registered():
    assert core.MARKETS["JP"]["domain"] == 5      # Keepa domain id for amazon.co.jp
    assert "Gating JP" in core.RESULT_COLUMNS and "ROI JP" in core.RESULT_COLUMNS


# ─── Multi-sheet attachments ──────────────────────────────────────────────────

def test_items_from_excel_all_sheets():
    import tempfile
    path = os.path.join(tempfile.mkdtemp(), "offer.xlsx")
    with pd.ExcelWriter(path) as xw:
        pd.DataFrame({"EAN": ["3348901234567"], "DESCRIPTION": ["EDP 50ML"],
                      "PRICE": [50.0]}).to_excel(xw, sheet_name="Perfumes", index=False)
        pd.DataFrame({"EAN": ["8411061111321"], "DESCRIPTION": ["Cream"],
                      "PRICE": [20.0]}).to_excel(xw, sheet_name="Cosmetics", index=False)
        pd.DataFrame({"note": ["terms and conditions"]}).to_excel(xw, sheet_name="Info", index=False)
    items, skipped, report = core.items_from_excel(path)
    assert len(items) == 2                       # both product tabs merged
    assert report["Info"].startswith("no EAN")    # non-table tab reported, not fatal


def test_reheader_finds_header_row():
    raw = pd.DataFrame([["GIVENCHY OFFER", None, None],
                        [None, None, None],
                        ["EAN", "DESCRIPTION", "PRICE"],
                        ["3348901234567", "EDP 50ML", "54,95€"]])
    items, skipped, cols = core.items_from_dataframe(raw)
    assert len(items) == 1 and items[0]["price_eur"] == 54.95   # comma price parsed


# ─── Per-EAN freight ─────────────────────────────────────────────────────────

def test_load_shipping_table():
    import tempfile
    path = os.path.join(tempfile.mkdtemp(), "ship.csv")
    with open(path, "w") as f:
        f.write("ean,market,cost_per_unit_eur\n"
                "8435137743179,USA,4.0133\n"        # sheet calls the US 'USA'
                "685428001534,CA,2.10\n"             # 12-digit UPC, leading zero dropped by the sheet
                "3614273790840,AU,6.27\n"
                "bad,US,1.00\n"                      # unusable EAN
                "1234567890123,US,\n")               # missing cost
    t = core.load_shipping_table(path)
    assert t[("8435137743179", "US")] == 4.0133      # USA -> US alias
    assert t[("0685428001534", "CA")] == 2.10        # padded to 13 digits
    assert ("bad", "US") not in t and len(t) == 3
    assert core.load_shipping_table("/nonexistent.csv") == {}   # optional file


def test_per_ean_freight_beats_flat_rate():
    m = core.matrix_from_df(pd.DataFrame({"Brand": ["Prada"], "US": ["ok"], "CA": ["ok"],
                                          "UK": ["ok"], "AU": [""], "JP": [""], "Notes": [""]}))
    items = [{"ean": "8435137743179", "title": "Prada EDP", "price_eur": 109.87, "brand": "Prada"}]
    md = {"US": {"8435137743179": {"asin": "A", "title": "t", "brand": "Prada", "eans": [],
                                    "buybox90": 234.94, "new90": None, "rank30": 900,
                                    "fba_fee": 6.11}}}
    p = {**P, "eur_usd": 1.1617}
    flat = core.build_result_df(items, md, m, p).iloc[0]
    real = core.build_result_df(items, md, m, p, None,
                                {("8435137743179", "US"): 4.0133}).iloc[0]
    # real freight (4.01) exceeds the 3.34 flat, so COGS rises and ROI falls
    assert real["ROI US"] < flat["ROI US"]
    assert abs(real["ROI US"] - 29.9) < 0.1        # Seller Snap reports 29.85%
    assert "freight is the flat default" not in (real["Notes"] or "")


def test_customs_clearance_added_to_cogs():
    """The sheet's per-EAN customs clearance is a real cash cost neither tool
    modelled; it lowers ROI slightly and is not itself dutiable."""
    p = {**P, "eur_usd": 1.1622, "us_ship": 4.0133, "us_fba": 6.11}
    without = core.calc_us(109.87, 214.95, p) * 100
    with_c = core.calc_us(109.87, 214.95, {**p, "us_customs": 0.4982}) * 100
    assert with_c < without
    assert abs(with_c - 17.95) < 0.05        # Seller Snap reports 17.61%
    for k in ("us_customs", "uk_customs", "ca_customs"):
        assert core.DEFAULT_PARAMS[k] == 0.0   # off unless looked up


def test_resolve_returns_freight_customs_and_source():
    f, c, src = core.resolve_shipping_table(None, "/nonexistent.csv")
    assert f == {} and c == {} and "flat rates" in src


def test_freight_note_when_ean_missing_from_table():
    m = core.matrix_from_df(pd.DataFrame({"Brand": ["Prada"], "US": ["ok"], "CA": ["ok"],
                                          "UK": ["ok"], "AU": [""], "JP": [""], "Notes": [""]}))
    items = [{"ean": "9999999999999", "title": "X", "price_eur": 50.0, "brand": "Prada"}]
    md = {"US": {"9999999999999": {"asin": "A", "title": "t", "brand": "Prada", "eans": [],
                                    "buybox90": 100.0, "new90": None, "rank30": 1, "fba_fee": 5.0}}}
    r = core.build_result_df(items, md, m, P, None, {("other", "US"): 1.0}).iloc[0]
    assert "US: freight is the flat default" in r["Notes"]


# ─── Cache pruning ────────────────────────────────────────────────────────────

def test_cache_prune(tmp_path=None):
    import json, tempfile, time
    path = os.path.join(tempfile.mkdtemp(), "c.json")
    old = time.time() - 8 * 86400
    core.save_cache({"fresh": {"ts": time.time(), "data": 1},
                     "stale": {"ts": old, "data": 2}}, path)
    kept = json.load(open(path))
    assert "fresh" in kept and "stale" not in kept



# ─── LIVE BRAND MATRIX ────────────────────────────────────────────────────────

def _cell(text="", color=None):
    c = {"formattedValue": text} if text else {}
    if color:
        c["effectiveFormat"] = {"backgroundColor": dict(zip(("red", "green", "blue"), color))}
    return c


GREEN, RED, ORANGE = (0.85, 0.92, 0.83), (0.96, 0.8, 0.8), (0.99, 0.9, 0.8)


def test_brand_sheet_reads_colour_not_text():
    rows = [{"values": [_cell(""), _cell("AMZ US"), _cell("AMZ CA"), _cell("AMZ UK"),
                        _cell("AMZ AU"), _cell("AMZ JP")]},
            {"values": [_cell("Balenciaga"), _cell("ok", GREEN), _cell("Hard Gated", RED),
                        _cell("B00EXW39NQ / 3607", ORANGE), _cell("ok", GREEN), _cell()]}]
    df = core.brand_rows_to_df(rows)
    r = df.iloc[0]
    # empty-but-orange is the real case: status from colour, ASIN kept as a note
    assert r["UK"] == "has path to apply", r["UK"]
    assert "B00EXW39NQ" in r["Notes"]
    assert (r["US"], r["CA"], r["AU"], r["JP"]) == ("ok", "Hard Gated", "ok", "")


def test_brand_sheet_colour_beats_contradicting_text():
    rows = [{"values": [_cell(""), _cell("AMZ US")]},
            {"values": [_cell("Lancome"), _cell("partially hard gated", ORANGE)]}]
    df = core.brand_rows_to_df(rows)
    assert df.iloc[0]["US"] == "has path to apply"


def test_duplicate_brand_rows_merge_strictest():
    df = pd.DataFrame([{"Brand": "Giorgio Armani", "US": "ok", "CA": "", "UK": "ok",
                        "AU": "", "JP": "", "Notes": ""},
                       {"Brand": "GIORGIO ARMANI", "US": "Hard Gated", "CA": "ok", "UK": "",
                        "AU": "", "JP": "", "Notes": "fragrance only"}])
    merged, conflicts = core.dedupe_brand_rows(df)
    assert len(merged) == 1
    r = merged.iloc[0]
    assert r["US"] == "Hard Gated"      # never resolve a gating clash permissively
    assert r["CA"] == "ok"              # blank carries no information
    assert r["UK"] == "ok"
    assert "fragrance only" in r["Notes"] and len(conflicts) == 1


def test_override_hits_every_duplicate_row():
    df = pd.DataFrame([{"Brand": "Paco Rabanne", "US": "ok", "CA": "ok", "UK": "", "AU": "",
                        "JP": "", "Notes": ""},
                       {"Brand": "paco rabanne", "US": "ok", "CA": "ok", "UK": "", "AU": "",
                        "JP": "", "Notes": ""}])
    ov = pd.DataFrame([{"Brand": "Paco Rabanne", "US": "do not sell", "CA": "do not sell",
                        "UK": "", "AU": "", "JP": "", "Notes": "import complexities"}])
    out = core.apply_brand_overrides(df, ov)
    assert list(out["US"]) == ["do not sell", "do not sell"]
    assert (out["CA"] == "do not sell").all()


def test_do_not_sell_excludes_and_is_labelled_honestly():
    m = core.matrix_from_df(pd.DataFrame([{"Brand": "Paco Rabanne", "US": "do not sell",
                                           "CA": "do not sell", "UK": "do not sell",
                                           "AU": "do not sell", "JP": "do not sell",
                                           "Notes": "import complexities"}]))
    e = m[core.norm_brand("Paco Rabanne")]
    assert e["US"] == core.GATE_HARD                 # excluded as firmly as a gate
    assert e["US_label"] == core.GATE_NO_SELL_LABEL  # but not called an Amazon gate


def test_unknown_brand_override_is_added():
    df = pd.DataFrame([{"Brand": "Dior", "US": "ok", "CA": "", "UK": "", "AU": "",
                        "JP": "", "Notes": ""}])
    ov = pd.DataFrame([{"Brand": "New Brand", "US": "do not sell", "CA": "", "UK": "",
                        "AU": "", "JP": "", "Notes": "no import route"}])
    out = core.apply_brand_overrides(df, ov)
    assert len(out) == 2 and out.iloc[1]["US"] == "do not sell"


def test_snapshot_fills_only_the_gaps_the_sheet_leaves():
    live = pd.DataFrame([{"Brand": "Dior", "US": "Hard Gated", "CA": "ok", "UK": "",
                          "AU": "", "JP": "", "Notes": ""}])
    snap = pd.DataFrame([{"Brand": "dior", "US": "ok", "CA": "", "UK": "ok", "AU": "",
                          "JP": "Hard Gated", "Notes": ""},
                         {"Brand": "Nars", "US": "ok", "CA": "", "UK": "", "AU": "",
                          "JP": "", "Notes": ""}])
    out, filled = core.fill_matrix_gaps(live, snap)
    r = out.iloc[0]
    assert r["US"] == "Hard Gated"     # the sheet is the source of truth...
    assert r["UK"] == "ok" and r["JP"] == "Hard Gated"   # ...but blank means "not recorded"
    assert len(out) == 2 and filled == 3                 # brands absent from the sheet survive


def test_gated_verdict_distinguishes_our_choice_from_amazons():
    assert core.market_verdict({}, 50, core.GATE_HARD)[0] == core.VERDICT_GATED
    assert core.market_verdict({}, 50, core.GATE_HARD,
                               gate_label=core.GATE_NO_SELL_LABEL)[0] == core.VERDICT_NO_SELL


# ─── gating from the seller count ─────────────────────────────────────────────

def test_extract_product_reads_seller_counts():
    stats = {"current": [-1] * 11 + [4], "avg90": [-1] * 11 + [3.5],
             "avg180": [-1] * 11 + [-1]}
    p = core.extract_product({"asin": "A", "eanList": [], "stats": stats})
    assert p["sellers_now"] == 4 and p["sellers_avg90"] == 3.5
    assert p["sellers_avg180"] is None                    # -1 = no data


def test_seller_gate_signal():
    sig = core.seller_gate_signal
    assert sig({"sellers_now": 3}) == (core.SIGNAL_OPEN, 3)
    assert sig({"sellers_now": 1, "sellers_avg90": 4, "sellers_avg180": 2}) == \
        (core.SIGNAL_COLLAPSED, 4)
    assert sig({"sellers_now": 1, "sellers_avg90": 1.2, "sellers_avg180": 1}) == (None, None)
    assert sig({"sellers_now": 0, "sellers_avg90": 5}) == (None, None)   # nobody selling ≠ gated
    assert sig({}) == (None, None) and sig(None) == (None, None)


def _seller_row(sellers_now, avg90, brand="Unknownbrand", matrix_df=None,
                amazon_now=False):
    d = {"asin": "B1", "title": "t", "brand": brand, "eans": ["3386460025508"],
         "buybox90": 60.0, "new90": None, "rank30": 5000, "offers": sellers_now,
         "bb_days_30": 100, "rank_drops_30": 20, "rank_drops_90": 60,
         "sellers_now": sellers_now, "sellers_avg90": avg90, "sellers_avg180": avg90,
         "amazon_now": amazon_now}
    items = [{"ean": "3386460025508", "title": "Jimmy Choo EDT 100", "brand": brand,
              "price_eur": 10.0}]
    market_data = {m: {"3386460025508": dict(d)} for m in core.MARKETS}
    matrix = core.matrix_from_df(matrix_df)
    return core.build_result_df(items, market_data, matrix, P).iloc[0]


def test_unknown_brand_several_sellers_is_not_hard_gated():
    r = _seller_row(5, 5)
    assert r["Gating UK"].startswith("👥") and "5 sellers" in r["Gating UK"]
    assert r["Status"] == core.PSTATUS_OPEN


def test_unknown_brand_collapsed_to_one_seller_is_likely_gated():
    r = _seller_row(1, 6)
    assert r["Gating UK"].startswith("🔒")
    assert r["Verdict UK"] == core.VERDICT_LIKELY_GATED
    assert r["Status"] == core.PSTATUS_HARD           # never a buy candidate


def test_sheet_wins_but_disagreement_is_flagged():
    sheet = pd.DataFrame([{"Brand": "Unknownbrand", "US": "ok", "CA": "ok", "UK": "ok",
                           "AU": "", "JP": "ok", "Notes": ""}])
    r = _seller_row(1, 6, matrix_df=sheet)
    assert r["Gating UK"] == core.GATE_LABELS[core.GATE_OK]
    assert "1 now" in r["Notes"]
    hard = sheet.assign(UK="Hard Gated")
    r = _seller_row(5, 5, matrix_df=hard)
    assert r["Gating UK"] == core.GATE_LABELS[core.GATE_HARD]
    assert "worth rechecking" in r["Notes"]


def test_amazon_as_last_seller_is_a_note_not_gating():
    assert core.seller_gate_signal({"sellers_now": 1, "sellers_avg90": 5,
                                    "amazon_now": True})[0] == core.SIGNAL_AMAZON_ONLY
    r = _seller_row(1, 6, amazon_now=True)
    assert not r["Gating UK"].startswith("🔒")
    assert r["Verdict UK"] != core.VERDICT_LIKELY_GATED
    assert "only Amazon" in r["Why UK"] and "only Amazon" in r["Notes"]


def test_offer_brand_kept_when_keepa_title_names_it():
    d = {"asin": "B1", "title": "Byphasse Activ Protect Elixir 250 ml", "brand": "URBAN SWING",
         "eans": ["8436097092895"], "buybox90": 20.0, "new90": None, "rank30": 9000,
         "offers": 1, "bb_days_30": 100, "rank_drops_30": 5, "rank_drops_90": 15}
    md = {m: {"8436097092895": dict(d)} for m in core.MARKETS}
    item = {"ean": "8436097092895", "title": "SUBLIM PROTECT 250 ml", "price_eur": 1.55}
    r = core.build_result_df([dict(item, brand="BYPHASSE")], md, {}, P).iloc[0]
    assert r["Brand"] == "BYPHASSE"
    # a sibling whose listing title omits the brand still keeps the offer's brand
    md2 = {m: {**md[m], "8436097095803": dict(d, title="Caresse Crema De Ducha 1L",
                                               eans=["8436097095803"])} for m in md}
    two = [dict(item, brand="BYPHASSE"),
           {"ean": "8436097095803", "title": "CARESSE 1000 ml", "price_eur": 1.85,
            "brand": "BYPHASSE"}]
    assert set(core.build_result_df(two, md2, {}, P)["Brand"]) == {"BYPHASSE"}
    r = core.build_result_df([dict(item, brand="PERFUMES & SKINCARE")], md, {}, P).iloc[0]
    assert r["Brand"] == "URBAN SWING"            # offer name isn't a brand — Keepa's wins


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ✓ {name}")
            except AssertionError as e:
                failed += 1
                print(f"  ✗ {name}: {e}")
    print(f"\n{'ALL TESTS PASSED' if not failed else f'{failed} FAILED'}")
    sys.exit(1 if failed else 0)
