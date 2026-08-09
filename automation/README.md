# MTO Automation Pipeline — Setup

Polls a Gmail inbox for MTO offer emails from Andreina (Engelsa), runs the same
ROI + gating analysis as the Streamlit app, posts results to Slack
`#fb_purchase_es`. Runs on GitHub Actions every 30 min on weekdays.

Follows the ub-toolkit automation conventions (Gmail refresh-token creds,
`SLACK_BOT_TOKEN`, secrets in GitHub Actions). Final home after migration:
`ub-toolkit/scripts/purchasing/mto-analyzer/` + CATALOG.md entry.

## One-time setup

### 1. Mailbox (victoria.po.fb@gmail.com)
1. In **matteo.po.fb@gmail.com**: Settings → Forwarding → add
   `victoria.po.fb@gmail.com` → confirm → create a filter
   `from:andreina@engelsa.com` → "Forward to victoria.po.fb".
2. Google Cloud Console (any Google account): create a project → enable
   **Gmail API** → OAuth consent screen (internal/testing, add
   victoria.po.fb@gmail.com as test user) → Credentials → **OAuth client ID,
   Desktop app** → download the JSON.
3. While logged into **victoria.po.fb@gmail.com** in your browser, run the
   toolkit's helper (needs `pip install google-auth-oauthlib`):
   ```
   python3 gmail_auth_setup.py --credentials oauth_credentials.json \
       --scopes https://www.googleapis.com/auth/gmail.modify
   ```
   (script: ub-toolkit/scripts/purchasing/gmail_auth_setup.py; the `modify`
   scope is needed to label processed emails.)
   It prints `GMAIL_CLIENT_ID`, `GMAIL_APP_SECRET`, `GMAIL_REFRESH_TOKEN`.

### 2. Slack bot
1. api.slack.com/apps → **Create New App** → From scratch → name
   `MTO Analyzer`, pick the UB workspace.
2. OAuth & Permissions → Bot Token Scopes: add `chat:write` and `files:write`.
3. **Install to Workspace** → copy the `xoxb-…` Bot User OAuth Token.
4. In Slack, open `#fb_purchase_es` → `/invite @MTO Analyzer`.

### 3. GitHub Actions secrets
Repo → Settings → Secrets and variables → Actions → add:

| Secret | Value |
|---|---|
| `GMAIL_CLIENT_ID` | from step 1 |
| `GMAIL_APP_SECRET` | from step 1 |
| `GMAIL_REFRESH_TOKEN` | from step 1 |
| `KEEPA_API_KEY` | same key the app uses |
| `SLACK_BOT_TOKEN` | from step 2 |
| `SLACK_CHANNEL_ID` | `C01V52LDVFW` (#fb_purchase_es) |

### 4. Test
- Local, no Gmail/Slack:
  `KEEPA_API_KEY=... python3 automation/mto_pipeline.py --dry-run sample_offer.xlsx`
- Full run: repo → Actions → "MTO Analyzer Pipeline" → **Run workflow**.
  Send yourself a test email from any address, temporarily setting the repo
  secret/env `MTO_SENDER` to that address if you want to test end-to-end
  before Andreina's next real email.

## Behavior notes
- An email is marked with the Gmail label `mto-processed` after posting —
  that's the dedup mechanism; removing the label reprocesses the email.
- Bad file content → error message in Slack, email still marked processed.
- Transient failures (Keepa/Slack/network down) → email left unlabeled,
  retried on the next half-hour run; the Actions run shows red.
- Hard-gated-in-both-markets products are reported but cost 0 Keepa tokens.
- Exchange rates: live ECB at run time; falls back to code defaults with a
  loud "(!)" note in the Slack message if the rates feed is down.
