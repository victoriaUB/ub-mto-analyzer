#!/usr/bin/env python3
"""MTO automation pipeline: Gmail → ROI/gating analysis → Slack.

What it does: polls a Gmail inbox for MTO offer emails from the supplier
(Andreina / Engelsa), downloads the attached offer file (xlsx/xls/csv), runs
the same ROI + brand-gating analysis as the Streamlit app (core.py), posts a
ranked summary + full Excel to Slack, then labels the email as processed.
When it runs: GitHub Actions cron, every 30 min on weekdays (see
.github/workflows/mto-analyzer.yml). Owner: Victoria.

Follows the ub-toolkit conventions: Gmail OAuth refresh-token credentials
(minted once with ub-toolkit's scripts/purchasing/gmail_auth_setup.py, scope
gmail.modify), SLACK_BOT_TOKEN posting, no secrets in code.

Env vars (GitHub Actions secrets):
  GMAIL_CLIENT_ID / GMAIL_APP_SECRET / GMAIL_REFRESH_TOKEN
  KEEPA_API_KEY
  SLACK_BOT_TOKEN            xoxb-… with chat:write + files:write
  SLACK_CHANNEL_ID           optional, default C01V52LDVFW (#fb_purchase_es)
  MTO_SENDER                 optional, default andreina@engelsa.com

Local test (no Gmail/Slack touched):
  KEEPA_API_KEY=... python3 automation/mto_pipeline.py --dry-run offer.xlsx
"""

import argparse
import base64
import io
import os
import sys

import pandas as pd
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import core  # noqa: E402

PROCESSED_LABEL = "mto-processed"
DEFAULT_SENDER = "andreina@engelsa.com"
DEFAULT_CHANNEL = "C01V52LDVFW"          # #fb_purchase_es
MATRIX_PATH = os.path.join(os.path.dirname(__file__), "..", "brand_matrix.csv")
GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"
ATTACHMENT_EXTS = (".xlsx", ".xls", ".csv")
SUMMARY_TOP_N = 10


# ─── Gmail (plain REST, no SDK) ───────────────────────────────────────────────

def gmail_token():
    r = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id": os.environ["GMAIL_CLIENT_ID"],
        "client_secret": os.environ["GMAIL_APP_SECRET"],
        "refresh_token": os.environ["GMAIL_REFRESH_TOKEN"],
        "grant_type": "refresh_token",
    }, timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]


def gmail_get(token, path, **params):
    r = requests.get(f"{GMAIL_API}/{path}", params=params,
                     headers={"Authorization": f"Bearer {token}"}, timeout=60)
    r.raise_for_status()
    return r.json()


def gmail_post(token, path, body):
    r = requests.post(f"{GMAIL_API}/{path}", json=body,
                      headers={"Authorization": f"Bearer {token}"}, timeout=60)
    r.raise_for_status()
    return r.json()


def ensure_label(token, name=PROCESSED_LABEL):
    for lb in gmail_get(token, "labels").get("labels", []):
        if lb["name"].lower() == name.lower():
            return lb["id"]
    return gmail_post(token, "labels", {"name": name})["id"]


def find_new_messages(token, sender):
    q = f"from:{sender} has:attachment newer_than:14d -label:{PROCESSED_LABEL}"
    resp = gmail_get(token, "messages", q=q, maxResults=10)
    return [m["id"] for m in resp.get("messages", [])]


def _walk_parts(part, found):
    fname = (part.get("filename") or "").lower()
    body = part.get("body", {})
    if fname.endswith(ATTACHMENT_EXTS) and body.get("attachmentId"):
        found.append((part["filename"], body["attachmentId"]))
    for sub in part.get("parts", []) or []:
        _walk_parts(sub, found)


def fetch_email(token, msg_id):
    """Returns (subject, [(filename, bytes), ...])."""
    msg = gmail_get(token, f"messages/{msg_id}", format="full")
    headers = {h["name"].lower(): h["value"]
               for h in msg.get("payload", {}).get("headers", [])}
    subject = headers.get("subject", "(no subject)")
    found = []
    _walk_parts(msg.get("payload", {}), found)
    attachments = []
    for filename, att_id in found:
        att = gmail_get(token, f"messages/{msg_id}/attachments/{att_id}")
        data = base64.urlsafe_b64decode(att["data"])
        attachments.append((filename, data))
    return subject, attachments


def mark_processed(token, msg_id, label_id):
    gmail_post(token, f"messages/{msg_id}/modify", {"addLabelIds": [label_id]})


# ─── Analysis ─────────────────────────────────────────────────────────────────

def read_offer_file(filename, data):
    if filename.lower().endswith(".csv"):
        return pd.read_csv(io.BytesIO(data))
    return pd.read_excel(io.BytesIO(data))     # engine auto-picked (openpyxl/xlrd)


def run_analysis(raw_df):
    items, skipped, cols = core.items_from_dataframe(raw_df)
    if not items:
        raise ValueError("No valid rows found (need EAN + purchase price per row).")

    params = dict(core.DEFAULT_PARAMS)
    live = core.fetch_live_rates()
    rates_note = "manual fallback rates(!)"
    if live:
        params.update({k: live[k] for k in ("eur_gbp", "eur_usd", "usd_cad")})
        rates_note = f"live ECB {live['date']}"

    matrix_df = pd.read_csv(MATRIX_PATH, dtype=str).fillna("")
    res = core.analyze(items, os.environ["KEEPA_API_KEY"], params=params,
                       matrix_df=matrix_df, skip_hard_gated=True,
                       progress=lambda m: print(f"  {m}"))
    res["skipped_rows"] = skipped
    res["rates_note"] = rates_note
    return res


def format_summary(subject, res):
    df = res["result_df"]
    n = len(df)
    n_found = int(df[["ROI UK", "ROI CA"]].notna().any(axis=1).sum())
    n_hard = int((df[["Gating UK", "Gating CA"]] == core.GATE_LABELS[core.GATE_HARD]).all(axis=1).sum())

    lines = [f"📦 *MTO offer analyzed* — {subject}",
             f"{n} products · {n_found} found on Keepa · rates: {res['rates_note']}"]
    if res.get("skipped_rows"):
        lines.append(f"⚠️ {res['skipped_rows']} row(s) skipped (missing/invalid EAN or price)")
    lines.append("")
    lines.append("*Top opportunities* (sellable first, ROI CA → UK):")
    for i, (_, r) in enumerate(df.head(SUMMARY_TOP_N).iterrows(), 1):
        roi_ca = core.fmt_roi(r["ROI CA"])
        roi_uk = core.fmt_roi(r["ROI UK"])
        product = (r["Product"] or "")[:60]
        lines.append(f"{i}. CA {roi_ca} | UK {roi_uk} — *{r['Brand'] or '?'}* — {product} "
                     f"(EAN {r['EAN']})")
    if n > SUMMARY_TOP_N:
        lines.append(f"…and {n - SUMMARY_TOP_N} more in the attached file.")
    if n_hard:
        lines.append(f"🚫 {n_hard} product(s) hard-gated in both markets (Keepa lookups skipped).")
    if res.get("tokens_left") is not None:
        lines.append(f"_Keepa tokens left: {res['tokens_left']}_")
    return "\n".join(lines)


# ─── Slack ────────────────────────────────────────────────────────────────────

def slack_call(method, payload=None, files=None, data=None):
    r = requests.post(f"https://slack.com/api/{method}",
                      headers={"Authorization": f"Bearer {os.environ['SLACK_BOT_TOKEN']}"},
                      json=payload if files is None and data is None else None,
                      data=data, files=files, timeout=60)
    r.raise_for_status()
    out = r.json()
    if not out.get("ok"):
        raise RuntimeError(f"Slack {method} failed: {out.get('error')}")
    return out


def slack_post(channel, text, thread_ts=None):
    payload = {"channel": channel, "text": text, "unfurl_links": False}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    return slack_call("chat.postMessage", payload)


def slack_upload_xlsx(channel, df, filename, thread_ts=None):
    buf = io.BytesIO()
    df.to_excel(buf, index=False, engine="openpyxl")
    content = buf.getvalue()
    up = slack_call("files.getUploadURLExternal",
                    data={"filename": filename, "length": len(content)})
    requests.post(up["upload_url"], files={"file": (filename, content)},
                  timeout=60).raise_for_status()
    complete = {"files": [{"id": up["file_id"], "title": filename}],
                "channel_id": channel}
    if thread_ts:
        complete["thread_ts"] = thread_ts
    slack_call("files.completeUploadExternal", data={
        "files": '[{"id":"%s","title":"%s"}]' % (up["file_id"], filename),
        "channel_id": channel, **({"thread_ts": thread_ts} if thread_ts else {}),
    })


# ─── Main ─────────────────────────────────────────────────────────────────────

def process_message(token, msg_id, channel, label_id):
    subject, attachments = fetch_email(token, msg_id)
    print(f"Processing: {subject!r} — {len(attachments)} attachment(s)")
    if not attachments:
        slack_post(channel, f"📭 MTO email *{subject}* has no parseable attachment "
                            f"(.xlsx/.xls/.csv) — check it manually.")
        mark_processed(token, msg_id, label_id)
        return

    for filename, data in attachments:
        try:
            raw = read_offer_file(filename, data)
            res = run_analysis(raw)
        except ValueError as e:
            # Bad file content: report + mark processed (retrying won't help)
            slack_post(channel, f"⚠️ Could not analyze *{filename}* from MTO email "
                                f"*{subject}*: {e}\nCheck the file manually.")
            continue
        msg = slack_post(channel, format_summary(subject, res))
        out_name = f"mto_analysis_{msg_id[:8]}.xlsx"
        try:
            slack_upload_xlsx(channel, res["result_df"], out_name,
                              thread_ts=msg.get("ts"))
        except Exception as e:
            slack_post(channel, f"(couldn't attach the Excel: {e})", thread_ts=msg.get("ts"))
    mark_processed(token, msg_id, label_id)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", metavar="FILE",
                        help="Analyze a local offer file and print the Slack "
                             "message instead of touching Gmail/Slack")
    args = parser.parse_args()

    if args.dry_run:
        with open(args.dry_run, "rb") as f:
            raw = read_offer_file(args.dry_run, f.read())
        res = run_analysis(raw)
        print("\n" + "═" * 60)
        print(format_summary(os.path.basename(args.dry_run), res))
        out = os.path.splitext(args.dry_run)[0] + "_analysis.xlsx"
        res["result_df"].to_excel(out, index=False, engine="openpyxl")
        print(f"\nFull table written to {out}")
        return

    sender = os.environ.get("MTO_SENDER", DEFAULT_SENDER)
    channel = os.environ.get("SLACK_CHANNEL_ID", DEFAULT_CHANNEL)
    token = gmail_token()
    label_id = ensure_label(token)
    msg_ids = find_new_messages(token, sender)
    print(f"{len(msg_ids)} new MTO email(s) from {sender}")

    failures = 0
    for msg_id in msg_ids:
        try:
            process_message(token, msg_id, channel, label_id)
        except Exception as e:
            # Transient (Keepa/Slack/network): leave unlabeled so the next
            # scheduled run retries it, but surface the error in the log.
            failures += 1
            print(f"ERROR processing {msg_id}: {e}", file=sys.stderr)
    if failures:
        sys.exit(1)   # make the Actions run red so it's visible


if __name__ == "__main__":
    main()
