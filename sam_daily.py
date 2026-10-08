#!/usr/bin/env python3
"""
VA & GSA Lease Watch - daily SAM.gov pull for The Avens Group.

Every run:
  1. Queries the SAM.gov Get Opportunities API for recent VA lease notices
     (uses 2-3 API calls; personal keys allow ~10/day).
  2. Merges them into docs/data/opportunities.json (feeds the web portal).
  3. Emails a digest: new notices, amendments, responses due soon, recent awards.

Settings come from environment variables (GitHub Actions secrets), see README.md.
Local test without hitting SAM.gov:  python sam_daily.py --sample tests/sample_response.json --no-email
"""
import argparse
import datetime as dt
import html
import json
import os
import smtplib
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# What to watch for. Edit these lists to tune the feed.
# ---------------------------------------------------------------------------
NAICS_CODES = ["531120"]          # Lessors of Nonresidential Buildings (VA lease NAICS)
TITLE_QUERIES = ["lease"]          # Extra title searches to catch miscoded notices
# Each feed gets its own email, recipients, and portal page. Both share the same API calls.
FEEDS = [
    {"key": "va", "name": "VA Lease Watch", "noun": "VA lease notice",
     "agency": "VETERANS AFFAIRS",                       # must appear in the notice's agency path
     "data": "docs/data/opportunities.json", "to_env": "EMAIL_TO", "portal_suffix": ""},
    {"key": "gsa", "name": "GSA Lease Watch", "noun": "GSA lease notice",
     "agency": "GENERAL SERVICES ADMINISTRATION",
     "data": "docs/data/gsa.json", "to_env": "EMAIL_TO_GSA", "portal_suffix": "?feed=gsa"},
]
# Words that mark a "lease" as NOT real estate live in exclude_words.txt (one per line),
# so you can edit them in the GitHub website without touching this script.
STATES = []                        # e.g. ["CO", "WY", "MT", "ID"]; empty = nationwide
CLOSING_SOON_DAYS = 60
# Deadline groups in the email and portal: (last day of group, label)
DUE_BUCKETS = [(5, "Due within 5 days"), (10, "Due in 6 to 10 days"), (15, "Due in 11 to 15 days"),
               (30, "Due in 16 to 30 days"), (45, "Due in 31 to 45 days"), (60, "Due in 46 to 60 days")]

# ---------------------------------------------------------------------------
API_URL = "https://api.sam.gov/opportunities/v2/search"
ROOT = Path(__file__).resolve().parent
EXCLUDE_FILE = ROOT / "exclude_words.txt"


def load_excludes():
    if not EXCLUDE_FILE.exists():
        return []
    out = []
    for line in EXCLUDE_FILE.read_text().splitlines():
        w = line.split("#")[0].strip()          # allow trailing comments
        if "/opp/" in w:                         # a pasted SAM.gov link -> its notice ID
            w = w.split("/opp/")[1].split("/")[0]
        if w:
            out.append(w.upper())
    return out


EXCLUDES = load_excludes()


def is_excluded(title, notice_id="", solnum=""):
    """True if the title contains an excluded word, or the notice ID / solicitation # is listed."""
    t = (title or "").upper()
    ids = {(notice_id or "").upper(), (solnum or "").upper()} - {""}
    return any(w in ids or w in t for w in EXCLUDES)
NAVY, ORANGE, ORANGE_TINT, NAVY_TINT, GRAY, SILVER = (
    "#44546A", "#ED7D31", "#FAD7BE", "#D6DCE4", "#E7E6E6", "#A5A5A5")


def env(name, default=""):
    return os.environ.get(name, default).strip()


# ----------------------------- Fetching ------------------------------------
def fetch_page_set(params, api_key):
    """Fetch all pages for one query. Each page is one API call."""
    out, offset = [], 0
    while True:
        query = dict(params, api_key=api_key, limit=1000, offset=offset)
        resp = requests.get(API_URL, params=query, timeout=90)
        if resp.status_code == 429:
            sys.exit("SAM.gov daily API limit reached. It resets daily; try again tomorrow.")
        if resp.status_code in (401, 403):
            sys.exit("SAM.gov rejected the API key. Check the SAM_API_KEY secret "
                     "(keys expire every 90 days; regenerate on SAM.gov > Account Details).")
        resp.raise_for_status()
        body = resp.json()
        batch = body.get("opportunitiesData") or []
        out.extend(batch)
        offset += len(batch)
        if not batch or offset >= int(body.get("totalRecords") or 0):
            return out


def fetch_all(api_key, days_back):
    today = dt.date.today()
    window = {"postedFrom": (today - dt.timedelta(days=days_back)).strftime("%m/%d/%Y"),
              "postedTo": today.strftime("%m/%d/%Y")}
    raw = []
    for code in NAICS_CODES:
        raw += fetch_page_set(dict(window, ncode=code), api_key)
    for title in TITLE_QUERIES:
        raw += fetch_page_set(dict(window, title=title), api_key)
    return raw


# ----------------------------- Filtering -----------------------------------
def matches_feed(o, feed):
    path = (o.get("fullParentPathName") or "").upper()
    title = (o.get("title") or "").upper()
    if feed["agency"] not in path:
        return False
    if is_excluded(title, o.get("noticeId"), o.get("solicitationNumber")):
        return False
    return o.get("naicsCode") in NAICS_CODES or "LEASE" in title


def parse_date(value):
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return dt.datetime.strptime(value[:10], "%Y-%m-%d").date()
        except ValueError:
            return None


def normalize(o):
    pop = o.get("placeOfPerformance") or {}
    pocs = o.get("pointOfContact") or []
    primary = next((p for p in pocs if p.get("type") == "primary"), pocs[0] if pocs else {})
    award = o.get("award") or {}
    path = o.get("fullParentPathName") or ""
    deadline = parse_date(o.get("responseDeadLine"))
    return {
        "id": o.get("noticeId"),
        "title": " ".join((o.get("title") or "").split()),
        "solnum": (o.get("solicitationNumber") or "").strip(),
        "type": o.get("type") or o.get("baseType") or "",
        "posted": o.get("postedDate") or "",
        "deadline": deadline.isoformat() if deadline else "",
        "office": path.split(".")[-1].strip().title() if path else "",
        "city": ((pop.get("city") or {}).get("name") or "").title(),
        "state": (pop.get("state") or {}).get("code") or "",
        "setAside": o.get("typeOfSetAsideDescription") or "",
        "naics": o.get("naicsCode") or "",
        "active": (o.get("active") or "").lower() == "yes",
        "poc": {"name": primary.get("fullName") or "",
                "email": primary.get("email") or "",
                "phone": primary.get("phone") or ""},
        "awardee": (award.get("awardee") or {}).get("name") or "",
        "awardAmount": award.get("amount") or "",
        "link": o.get("uiLink") or f"https://sam.gov/opp/{o.get('noticeId')}/view",
    }


def is_award(rec):
    return "award" in rec["type"].lower()


# ----------------------------- Storage -------------------------------------
def load_store(path):
    if path.exists():
        return json.loads(path.read_text())
    return {"updated": None, "items": {}}


def merge(store, records, today):
    items = store["items"]
    known_sols = {r["solnum"] for r in items.values() if r.get("solnum")}
    new, amended = [], []
    for rec in records:
        if not rec["id"]:
            continue
        if rec["id"] in items:
            first_seen = items[rec["id"]].get("firstSeen")
            items[rec["id"]] = dict(rec, firstSeen=first_seen or today)
            continue
        rec["firstSeen"] = today
        rec["isUpdate"] = bool(rec["solnum"] and rec["solnum"] in known_sols)
        (amended if rec["isUpdate"] else new).append(rec)
        if rec["solnum"]:
            known_sols.add(rec["solnum"])
        items[rec["id"]] = rec
    # Prune anything long dead so the file stays small.
    cutoff = (dt.date.today() - dt.timedelta(days=180)).isoformat()
    for key in [k for k, r in items.items()
                if (r.get("deadline") or r.get("posted", "")[:10] or "9999") < cutoff]:
        del items[key]
    store["updated"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    return new, amended


def closing_soon(store):
    today = dt.date.today()
    horizon = today + dt.timedelta(days=CLOSING_SOON_DAYS)
    soon = [r for r in store["items"].values()
            if r.get("deadline") and not is_award(r)
            and today <= dt.date.fromisoformat(r["deadline"]) <= horizon]
    latest = {}
    for r in soon:  # keep only the newest notice per solicitation number
        key = r.get("solnum") or r["id"]
        if key not in latest or r.get("posted", "") > latest[key].get("posted", ""):
            latest[key] = r
    return sorted(latest.values(), key=lambda r: r["deadline"])


# ----------------------------- Email ---------------------------------------
def esc(s):
    return html.escape(str(s or ""))


def days_left(rec):
    if not rec.get("deadline"):
        return ""
    n = (dt.date.fromisoformat(rec["deadline"]) - dt.date.today()).days
    return "due today" if n == 0 else (f"{n} days left" if n > 0 else "closed")


def row_html(rec, stripe):
    where = ", ".join(x for x in [rec["city"], rec["state"]] if x) or "Location not listed"
    due = (f"Due {dt.date.fromisoformat(rec['deadline']).strftime('%b %d, %Y')} ({days_left(rec)})"
           if rec.get("deadline") else "No response date")
    extra = ""
    if is_award(rec) and rec.get("awardee"):
        amt = f" | ${float(rec['awardAmount']):,.0f}" if str(rec.get("awardAmount")).replace(".", "").isdigit() else ""
        extra = f"<br>Awarded to {esc(rec['awardee'])}{amt}"
    bg = ORANGE_TINT if stripe else "#FFFFFF"
    return f"""
    <tr><td style="background:{bg};padding:12px 14px;border-bottom:1px solid {GRAY};
        font-family:'Century Gothic',Arial,sans-serif;font-size:13px;color:#000;">
      <a href="{esc(rec['link'])}" style="color:{NAVY};font-weight:bold;font-size:14px;text-decoration:none;">{esc(rec['title'])}</a><br>
      <span style="color:#333;">{esc(where)} | {esc(rec['type'])}{' | ' + esc(rec['setAside']) if rec['setAside'] else ''}</span><br>
      <span style="color:#333;">{esc(due)}{' | Sol. ' + esc(rec['solnum']) if rec['solnum'] else ''}</span>{extra}
    </td></tr>"""


def section_html(title, recs, empty_text):
    head = f"""<tr><td style="background:{ORANGE};color:#fff;padding:8px 14px;
        font-family:'Century Gothic',Arial,sans-serif;font-weight:bold;font-size:14px;">
        {esc(title)} ({len(recs)})</td></tr>"""
    if not recs:
        body = f"""<tr><td style="padding:10px 14px;color:{SILVER};font-family:'Century Gothic',Arial,sans-serif;
            font-size:13px;border-bottom:1px solid {GRAY};">{esc(empty_text)}</td></tr>"""
    else:
        body = "".join(row_html(r, i % 2 == 1) for i, r in enumerate(recs))
    return head + body + '<tr><td style="height:14px;"></td></tr>'


def due_section_html(soon):
    title = f"Responses due in the next {CLOSING_SOON_DAYS} days"
    if not soon:
        return section_html(title, [], "No upcoming deadlines.")
    head = f"""<tr><td style="background:{ORANGE};color:#fff;padding:8px 14px;
        font-family:'Century Gothic',Arial,sans-serif;font-weight:bold;font-size:14px;">
        {esc(title)} ({len(soon)})</td></tr>"""
    body, lo = "", -1
    for hi, label in DUE_BUCKETS:
        group = [r for r in soon if lo < days_until(r) <= hi]
        lo = hi
        if not group:
            continue
        body += f"""<tr><td style="background:{NAVY_TINT};color:{NAVY};padding:6px 14px;
            font-family:'Century Gothic',Arial,sans-serif;font-weight:bold;font-size:12px;">
            {esc(label)} ({len(group)})</td></tr>"""
        body += "".join(row_html(r, i % 2 == 1) for i, r in enumerate(group))
    return head + body + '<tr><td style="height:14px;"></td></tr>'


def days_until(rec):
    return (dt.date.fromisoformat(rec["deadline"]) - dt.date.today()).days


def build_email(feed, new, amended, soon, portal_url, first_run):
    name = feed["name"]
    fresh = [r for r in new if not is_award(r)]
    awards = [r for r in new + amended if is_award(r)]
    amended = [r for r in amended if not is_award(r)]
    today = dt.date.today().strftime("%A, %B %d, %Y")
    urgent = sum(1 for r in soon if days_until(r) <= 15)
    intro = ("Initial load: everything posted in the backfill window is listed below."
             if first_run else
             f"{len(fresh)} new {feed['noun']}{'s' if len(fresh) != 1 else ''}. "
             f"{len(soon)} response{'s' if len(soon) != 1 else ''} due in the next {CLOSING_SOON_DAYS} days, "
             f"{urgent} of them within 15 days.")
    portal = (f'<p style="margin:0 0 16px;"><a href="{esc(portal_url)}" style="color:{ORANGE};font-weight:bold;">'
              f'Open the {name} portal</a></p>') if portal_url else ""
    body = f"""<html><body style="margin:0;background:#F5F6F8;">
    <table width="100%" cellpadding="0" cellspacing="0" style="background:#F5F6F8;"><tr><td align="center" style="padding:20px 8px;">
    <table width="640" cellpadding="0" cellspacing="0" style="max-width:640px;width:100%;background:#fff;">
      <tr><td style="background:{NAVY};padding:18px 20px;">
        <div style="font-family:'Calibri Light',Calibri,Arial,sans-serif;color:#fff;font-size:22px;">{esc(name)}</div>
        <div style="font-family:'Century Gothic',Arial,sans-serif;color:{NAVY_TINT};font-size:12px;">The Avens Group | SAM.gov daily pull | {today}</div>
      </td></tr>
      <tr><td style="padding:16px 20px 4px;font-family:'Century Gothic',Arial,sans-serif;font-size:14px;color:#000;">
        <p style="margin:0 0 10px;">{esc(intro)}</p>{portal}
      </td></tr>
      <tr><td style="padding:0 20px;"><table width="100%" cellpadding="0" cellspacing="0">
        {section_html("New notices", fresh, "Nothing new posted since the last pull.")}
        {section_html("Amendments and updates", amended, "No amendments to notices you're tracking.")}
        {due_section_html(soon)}
        {section_html("Recent awards", awards, "No new award notices.")}
      </table></td></tr>
      <tr><td style="border-top:3px solid {ORANGE};padding:12px 20px;font-family:'Century Gothic',Arial,sans-serif;
          font-size:11px;color:{SILVER};">The Avens Group | 303-731-0530 | GoAvens.com<br>
          Source: SAM.gov Get Opportunities API. Always confirm details on SAM.gov before responding.</td></tr>
    </table></td></tr></table></body></html>"""
    subject = (f"{name}: initial load ({len(fresh)} notices)" if first_run else
               f"{name}: {len(fresh)} new, {urgent} due within 15 days ({dt.date.today():%b %d})")
    return subject, body


def recipients(var):
    return [a.strip() for a in env(var).split(",") if a.strip()]


def send_via_graph(subject, html_body, to):
    """Send through Microsoft 365 using Microsoft Graph and an Entra app registration (OAuth)."""
    tenant, client_id, secret = env("MS_TENANT_ID"), env("MS_CLIENT_ID"), env("MS_CLIENT_SECRET")
    sender = env("EMAIL_FROM")
    if not sender:
        raise RuntimeError("EMAIL_FROM must be set to the mailbox that sends the digest (e.g. alerts@goavens.com).")
    tok = requests.post(
        f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
        data={"client_id": client_id, "client_secret": secret, "grant_type": "client_credentials",
              "scope": "https://graph.microsoft.com/.default"}, timeout=60)
    if tok.status_code != 200:
        raise RuntimeError("Microsoft sign-in failed. Check MS_TENANT_ID, MS_CLIENT_ID and MS_CLIENT_SECRET "
                 f"(client secrets expire). Details: {tok.text[:300]}")
    resp = requests.post(
        f"https://graph.microsoft.com/v1.0/users/{sender}/sendMail",
        headers={"Authorization": f"Bearer {tok.json()['access_token']}"},
        json={"message": {"subject": subject,
                          "body": {"contentType": "HTML", "content": html_body},
                          "toRecipients": [{"emailAddress": {"address": a}} for a in to]},
              "saveToSentItems": False},
        timeout=60)
    if resp.status_code != 202:
        hint = (" Make sure the Mail.Send application permission was added AND admin consent was granted."
                if resp.status_code == 403 else "")
        raise RuntimeError(f"Microsoft Graph could not send the email ({resp.status_code}).{hint} Details: {resp.text[:300]}")


def send_via_smtp(subject, html_body, to):
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, env("EMAIL_FROM") or env("SMTP_USER"), ", ".join(to)
    msg.attach(MIMEText(html_body, "html"))
    with smtplib.SMTP(env("SMTP_HOST", "smtp.office365.com"), int(env("SMTP_PORT", "587")), timeout=60) as s:
        s.starttls()
        s.login(env("SMTP_USER"), env("SMTP_PASS"))
        s.sendmail(msg["From"], to, msg.as_string())


def send_email(subject, html_body, to_var):
    to = recipients(to_var)
    if not to:
        print(f"{to_var} not set; skipping email.")
        return
    if env("MS_CLIENT_ID"):
        send_via_graph(subject, html_body, to)
    else:
        send_via_smtp(subject, html_body, to)
    print(f"Emailed {', '.join(to)}")


# ----------------------------- Main ----------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", help="Use a saved API response JSON instead of calling SAM.gov")
    ap.add_argument("--no-email", action="store_true", help="Skip sending; write email preview to email_preview.html")
    args = ap.parse_args()

    stores = {}
    for feed in FEEDS:
        path = ROOT / feed["data"]
        store = load_store(path)
        # Re-apply the exclude list to saved notices, so new entries clean up the portal too.
        removed = [k for k, r in store["items"].items()
                   if is_excluded(r.get("title"), r.get("id"), r.get("solnum"))]
        for k in removed:
            del store["items"][k]
        if removed:
            print(f"{feed['name']}: removed {len(removed)} saved notices matching exclude_words.txt.")
        stores[feed["key"]] = (path, store, not store["items"])
    any_first = any(first for _, _, first in stores.values())
    days_back = int(env("BACKFILL_DAYS", "90")) if any_first else int(env("LOOKBACK_DAYS", "4"))

    if args.sample:
        raw = json.loads(Path(args.sample).read_text()).get("opportunitiesData", [])
    else:
        api_key = env("SAM_API_KEY")
        if not api_key:
            sys.exit("SAM_API_KEY is not set.")
        raw = fetch_all(api_key, days_back)
    print(f"Pulled {len(raw)} notices from SAM.gov.")

    errors = []
    for feed in FEEDS:
        path, store, first_run = stores[feed["key"]]
        seen, records = set(), []
        for o in raw:
            if o.get("noticeId") in seen or not matches_feed(o, feed):
                continue
            seen.add(o.get("noticeId"))
            rec = normalize(o)
            if STATES and rec["state"] not in STATES:
                continue
            records.append(rec)

        new, amended = merge(store, records, dt.date.today().isoformat())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(store, indent=1, sort_keys=True))
        print(f"{feed['name']}: {len(records)} matches, {len(new)} new, {len(amended)} amended. "
              f"Store holds {len(store['items'])} notices.")

        portal = env("PORTAL_URL")
        portal = portal + feed["portal_suffix"] if portal else ""
        subject, body = build_email(feed, new, amended, closing_soon(store), portal, first_run)
        if args.no_email:
            (ROOT / f"email_preview_{feed['key']}.html").write_text(body)
            print(f"Wrote email_preview_{feed['key']}.html")
        elif new or amended or env("SEND_EMPTY", "true").lower() == "true":
            try:
                send_email(subject, body, feed["to_env"])
            except Exception as e:  # keep going so the other feed still sends
                print(f"{feed['name']} email failed: {e}")
                errors.append(feed["name"])
    if errors:
        sys.exit(f"Email failed for: {', '.join(errors)}. Portal data was still saved.")

if __name__ == "__main__":
    main()
