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


# ─── Cache pruning ────────────────────────────────────────────────────────────

def test_cache_prune(tmp_path=None):
    import json, tempfile, time
    path = os.path.join(tempfile.mkdtemp(), "c.json")
    old = time.time() - 8 * 86400
    core.save_cache({"fresh": {"ts": time.time(), "data": 1},
                     "stale": {"ts": old, "data": 2}}, path)
    kept = json.load(open(path))
    assert "fresh" in kept and "stale" not in kept


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
