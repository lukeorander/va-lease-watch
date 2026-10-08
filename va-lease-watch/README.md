# VA Lease Watch

A daily SAM.gov pull of VA lease notices (CBOCs, clinics, Vet Centers, and similar) for The Avens Group. Every weekday morning it:

1. Queries the official SAM.gov Get Opportunities API (2 calls per run).
2. Keeps VA notices that are NAICS 531120 or have "lease" in the title, and drops equipment and vehicle leases.
3. Emails a digest: new notices, amendments, responses due in the next 14 days, and recent awards.
4. Updates the data behind a web portal where you can filter, mark notices Watching / Pursuing / Passed, keep notes, and export to CSV.

It runs free on GitHub Actions. No server is needed.

## One-time setup (about 30 minutes)

### 1. Get a SAM.gov API key
Sign in to SAM.gov, open **Account Details**, enter your password, and request a **Public API Key**. Copy it right away because it's only shown once.
Personal keys expire every 90 days. Set a calendar reminder to regenerate it and update the `SAM_API_KEY` secret.

### 2. Create a GitHub repository
Create a new repo (for example `va-lease-watch`) and upload every file in this folder, including the hidden `.github` folder.

Public or private: The notice data is public SAM.gov information, and your statuses and notes never leave your browser, so a public repo is fine. Your API key and email password are stored as encrypted secrets either way. If you want the repo private, GitHub Pages requires a paid plan (GitHub Pro or Team).

### 3. Add secrets
Go to **Settings > Secrets and variables > Actions > New repository secret** and add:

| Secret | Value |
|---|---|
| `SAM_API_KEY` | Your SAM.gov key |
| `SMTP_HOST` | `smtp.office365.com` (Microsoft 365) or `smtp.gmail.com` |
| `SMTP_PORT` | `587` |
| `SMTP_USER` | The mailbox that sends the digest |
| `SMTP_PASS` | That mailbox's password or app password |
| `EMAIL_FROM` | Usually the same as `SMTP_USER` |
| `EMAIL_TO` | Recipients, comma-separated (for example `luke@goavens.com,bennett@goavens.com`) |

**Microsoft 365 note:** Many tenants turn off "Authenticated SMTP" by default. In the Microsoft 365 admin center, open the sending mailbox, go to **Mail > Manage email apps**, and check **Authenticated SMTP**. If the mailbox uses MFA, use an app password, or create a dedicated sending mailbox such as `alerts@goavens.com`. A Gmail account with an app password also works.

### 4. Turn on the portal
Go to **Settings > Pages**, choose **Deploy from a branch**, select branch `main` and folder `/docs`, then save. GitHub shows your portal URL (something like `https://yourname.github.io/va-lease-watch/`).

Then go to **Settings > Secrets and variables > Actions > Variables** and add `PORTAL_URL` with that URL, so the email links to the portal.

### 5. Run it the first time
Go to **Actions > Daily SAM.gov VA lease pull > Run workflow**. The first run backfills the last 90 days and sends an "initial load" email. After that it runs automatically at 7:00 AM Denver time (6:00 AM in winter), Monday through Friday.

## Tuning
Edit the top of `sam_daily.py`:

- `STATES = ["CO", "WY", "MT", "ID"]` limits results to those states (empty means nationwide).
- `EXCLUDE_TITLE_WORDS` controls which non-real-estate "leases" are dropped.
- `NAICS_CODES` and `TITLE_QUERIES` control what's searched. Each entry costs one API call per run; personal keys allow about 10 calls per day.
- `CLOSING_SOON_DAYS` sets the deadline window in the email and portal.

Set a repository variable or secret `SEND_EMPTY=false` (and add it to the workflow env) if you only want emails on days with new or amended notices.

To change the schedule, edit the `cron` line in `.github/workflows/daily-pull.yml`.

## Test locally without calling SAM.gov
```
pip install requests
python sam_daily.py --sample tests/sample_response.json --no-email
```
This writes `email_preview.html` and `docs/data/opportunities.json` from fake sample data. Delete both before uploading.

## Troubleshooting
- **"SAM.gov rejected the API key"**: The key expired. Regenerate it and update the secret.
- **"daily API limit reached"**: Too many manual runs today. It resets tomorrow.
- **No email but the run succeeded**: Check the SMTP secrets and the Microsoft 365 SMTP setting above.
- **Portal says the data file hasn't been created**: Run the workflow once from the Actions tab.
