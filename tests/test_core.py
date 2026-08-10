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

def test_calc_uk_known_value():
    p = {**P, "eur_gbp": 0.8558, "uk_ship": 0.80, "uk_lab": 2.35,
         "uk_fba": 3.09, "uk_ref": 15.0, "uk_vat": 20.0}
    roi = core.calc_uk(85.45, 104.94, p)
    assert abs(roi * 100 - (-6.0)) < 0.1        # matches UB calculator: -6.0%


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
    return pd.DataFrame(rows)


def test_status_existing_listings():
    df = _status_df([{"ASIN UK": "B1", "ROI UK": 25.0, "Gating UK": core.GATE_LABELS[core.GATE_OK],
                      "ASIN CA": None, "ROI CA": None, "Gating CA": core.GATE_LABELS[core.GATE_HARD]}])
    status, counts = core.offer_status(df)
    assert status == core.STATUS_EXISTING and counts["existing"] == 1


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


def test_status_threshold_boundary():
    df = _status_df([{"ASIN UK": "B1", "ROI UK": 17.0, "Gating UK": core.GATE_LABELS[core.GATE_OK],
                      "ASIN CA": None, "ROI CA": None, "Gating CA": core.GATE_LABELS[core.GATE_OK]}])
    assert core.offer_status(df)[0] == core.STATUS_EXISTING          # 17.0 counts
    assert core.offer_status(df, roi_threshold=17.1)[0] == core.STATUS_NO_OPP


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
