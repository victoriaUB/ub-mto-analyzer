# UB product & MTO analysis

Two tools, one shared calculation engine (`core.py`) — ROI per market
(Amazon CA / UK / JP via Keepa) plus brand gating from `brand_matrix.csv`.

| Tool | What it is | Entry point |
|---|---|---|
| **Products Analyzer** | Interactive Streamlit app. Check products by EAN — type Brand/EAN/price by hand or upload an .xlsx/.csv. Also hosts the Brand Matrix editor. | `streamlit run app.py` |
| **MTO Analyzer** | Automated pipeline. Reads MTO offer emails from Andreina (Engelsa), analyzes them, posts the summary + full table to Slack `#fb_purchase_es`. Runs daily on GitHub Actions, or on demand. | `automation/mto_pipeline.py` (see `automation/README.md`) |

The Claude Code skill `/mto-offers` runs the MTO flow interactively from a laptop
(Gmail + Slack connectors), for days when the unattended pipeline isn't wanted.

## Tests

    python3 tests/test_core.py        # or: pytest tests/

## Files

- `core.py` — markets, ROI formulas, gating, Keepa client, statuses, ranking
- `app.py` — Products Analyzer UI
- `automation/mto_pipeline.py` — MTO Analyzer pipeline (`--dry-run FILE` to test)
- `brand_matrix.csv` — brand gating per market, edited in the app
- `config.json`, `keepa_cache.json` — local only, gitignored
