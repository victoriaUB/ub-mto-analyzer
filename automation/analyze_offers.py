#!/usr/bin/env python3
"""Analyse one or more MTO offers from a CSV and report per offer.

Input is what parse_eml.py produces: Brand, Product, EAN, Purchase price EUR,
and optionally Offer / Terms columns. Every EAN is looked up on all markets in
one batched pass, then results are grouped back per offer so each can be posted
separately.

    KEEPA_API_KEY=... python3 automation/analyze_offers.py offers.csv

Writes <input>_analysis.xlsx plus one xlsx per offer, and prints the status
lines and the buy candidates for each.
"""
import json
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import core  # noqa: E402

APP_DIR = os.path.join(os.path.dirname(__file__), "..")
SA_PATH = os.path.expanduser("~/.config/mto-analyzer-sa.json")


def main():
    src = sys.argv[1]
    outdir = os.path.dirname(os.path.abspath(src))
    raw = pd.read_csv(src, dtype={"EAN": str})
    items, skipped, _ = core.items_from_dataframe(raw)
    offer_of = {core.normalize_ean(e): o for e, o in
                zip(raw["EAN"], raw.get("Offer", pd.Series([os.path.basename(src)] * len(raw))))}
    terms_of = dict(zip(raw.get("Offer", pd.Series([""] * len(raw))),
                        raw.get("Terms", pd.Series([""] * len(raw))).fillna("")))
    print(f"{len(items)} products, {skipped} skipped")

    params = dict(core.DEFAULT_PARAMS)
    live = core.fetch_live_rates()
    if live:
        params.update({k: live[k] for k in ("eur_gbp", "eur_usd", "usd_cad", "eur_jpy")})
        print(f"rates: live ECB {live['date']}")

    creds = json.load(open(SA_PATH)) if os.path.exists(SA_PATH) else None
    matrix_df, matrix_source = core.resolve_brand_matrix(
        creds, os.path.join(APP_DIR, "brand_matrix.csv"),
        os.path.join(APP_DIR, "brand_overrides.csv"))
    print(f"brand gating: {matrix_source}")
    res = core.analyze(
        items, os.environ["KEEPA_API_KEY"], params=params,
        matrix_df=matrix_df,
        cache_path=os.path.join(outdir, "keepa_cache.json"),
        shipping_creds=creds, shipping_path=os.path.join(APP_DIR, "shipping_costs.csv"),
        skip_hard_gated=True, buybox=True, two_pass=True,
        progress=lambda m: print(f"  {m}", flush=True))

    df = res["result_df"]
    df["Offer"] = df["EAN"].astype(str).map(lambda e: offer_of.get(core.normalize_ean(e)))
    df.to_excel(os.path.join(outdir, "all_offers_analysis.xlsx"), index=False)
    print(f"\ntokens left: {res['tokens_left']}")

    for offer, grp in df.groupby("Offer", dropna=False):
        print(f"\n{'=' * 70}\n{offer}   ({len(grp)} EANs)   {terms_of.get(offer, '')}")
        for line in core.status_summary_lines(grp):
            print(f"  {line}")
        verdicts = grp["Verdict"].fillna("—").value_counts()
        print("  verdicts: " + " · ".join(f"{n} {v}" for v, n in verdicts.items()))
        buys = grp[grp["Verdict"].fillna("").str.startswith(("🟢", "🔵"))]
        for _, r in buys.head(25).iterrows():
            rois = " | ".join(f"{m} {core.fmt_roi(r[f'ROI {m}'])}" for m in core.MARKETS
                              if pd.notna(r[f"ROI {m}"]))
            print(f"    • {str(r['Product'])[:46]:<46} {r['EAN']} buy {r['Purchase (EUR)']:>6.2f}")
            print(f"      {r['Verdict']} · {r['Why']}")
            print(f"      {rois}")
        if len(buys) > 25:
            print(f"    …and {len(buys) - 25} more candidates")
        safe = "".join(ch for ch in str(offer) if ch.isalnum() or ch in " -_")[:40]
        grp.to_excel(os.path.join(outdir, f"{safe or 'offer'}_analysis.xlsx"), index=False)


if __name__ == "__main__":
    main()
