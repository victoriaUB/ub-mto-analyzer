---
name: mto-offers
description: Checks Gmail for MTO offer emails from Andreina (andreina@engelsa.com), runs the MTO Analyzer (ROI per market + brand gating), and posts results to #fb_purchase_es. Modeled on ub-toolkit's andreina-offers skill, adding the analysis step. Trigger phrase - "check MTO offers" or /mto-offers.
argument-hint: ""
---

# MTO Offers — analyze & post

Mirrors `ub-toolkit/skills/purchasing/andreina-offers` (Daria's skill) but adds
ROI + gating analysis before posting. Coordinate with Daria/Florencia so the
same offer isn't announced twice — this skill's post includes everything theirs
does, plus the analysis.

## Configuration

| Setting | Value |
|---------|-------|
| Sender | andreina@engelsa.com |
| Slack channel | C01V52LDVFW (#fb_purchase_es) |
| Processed log | ~/.claude/mto-offers-processed.txt |
| Analyzer repo | ~/Documents/ub-mto-analyzer |
| Tag always | `<@U0550FKCXEX>` Rita, `<@U05C2GHL7G8>` Sonya |
| Tag on "New launch" status | `<@U0795R217CJ>` Anastasia Kozyreva (listings creation), FIRST in the greeting |

Prerequisite: Andreina's emails must be visible to the connected Gmail account
(victoria@tweetybeauty.com) — i.e. the forward from the matteo.po.fb mailbox
must point there. If a Gmail search returns nothing, tell Victoria the forward
likely isn't set up and stop.

## Steps

### 1. Load processed IDs
Read `~/.claude/mto-offers-processed.txt` (one Gmail message ID per line).
Missing file = empty.

### 2. Search Gmail
`from:andreina@engelsa.com newer_than:7d`, maxResults 10. Exclude IDs already
processed. Stop if nothing new: "No new MTO emails from Andreina."

### 3. Per email (oldest first)

**3a. Is it a product offer?** Subject + first lines: product list with
prices / MOQ / delivery terms = offer. Forecast, question, reply, invoice,
shipping update = not an offer → mark processed, skip.

**3b. Extract products.** From the email body table and/or attachment
(.xlsx/.csv — download it). For each product: Brand, product name + size,
EAN, qty, price €. Also capture offer terms (MOQ, MOA, delivery, expiry,
"subject to unsold"). Brand can come from the subject line if not per-row.

**3c. Write input CSV** to the scratchpad: columns
`Brand,Product,EAN,Purchase price EUR` (one file per email).

**3d. Run the analyzer** (uses live ECB rates, brand matrix, Keepa;
hard-gated markets cost no tokens):
```bash
cd ~/Documents/ub-mto-analyzer && KEEPA_API_KEY=$(python3 -c "import json;print(json.load(open('config.json'))['keepa_key'])") python3 automation/mto_pipeline.py --dry-run <csv-path>
```
Capture the printed summary. The full result table is written next to the
CSV as `*_analysis.xlsx` — read it for the canvas.

### 4. Create Slack canvas
Title: `[BRAND] — MTO Analysis [D Mon YYYY]`. Content: offer terms, then the
full analysis table (Product, Brand, EAN, Purchase €, Status, Sell CA, ROI CA,
Gating CA, Sell UK, ROI UK, Gating UK, Notes) in result order (sellable
first, ROI CA → UK). **Max 150 table rows per canvas API call** — chunk with
`slack_update_canvas` (append) beyond that.

### 5. Determine the offer STATUS
The analyzer prints it ("Status:" line — computed by `core.offer_status`).
The four statuses:

| Status | Meaning |
|--------|---------|
| 🟢 Opportunities with existing listings found | ≥1 product with ROI ≥ 17% on an existing listing in a market where the brand is ✅ ungated |
| 🟠 Opportunities found — ungating required | ≥1 product with ROI ≥ 17% but the brand is 🟠 soft-gated (path to apply) there |
| 🆕 New launch — check if worth creating | none of the EANs have listings on the target markets |
| ⚪ No opportunities — ROI below threshold | listings exist but nothing clears 17% |

### 6. Post to Slack
`slack_send_message` to `C01V52LDVFW` (mentions as literal `<@U…>`, never
HTML-escaped). Greeting depends on status: if the offer contains ANY 🆕 new-launch
products, Anastasia comes first; otherwise only Rita + Sonya. For mixed
offers the Status line already carries the per-category breakdown — quote it
as-is.

```
Hi [<@U0795R217CJ> ]<@U0550FKCXEX>, <@U05C2GHL7G8>! Please check analyzed MTO from Perfumes Club for [BRAND] ([short context, e.g. "L'Interdit Elixir — NEW 2026 launch"])

Status: [STATUS line]

[OFFER TERMS — all caps, one per line, e.g. MOQ / DELIVERY]

Analysis summary: [1–3 sentences: gating verdict, top ROI lines if any, what action the status implies]

📋 Full analysis: [canvas_url]
```

### 7. Mark processed & report
Append all handled message IDs to the log in one write. Report one line per
offer: brand, canvas URL, message link. If the analyzer script fails, post
nothing for that email, leave it unlogged (retry next run), and tell Victoria
what broke.
