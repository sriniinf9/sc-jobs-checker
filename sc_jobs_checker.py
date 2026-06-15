#!/usr/bin/env python3
"""
SC-Cleared Jobs Checker — Vasu Kurri
Scrapes UK job boards for SC-cleared roles in:
  - Testing / QA
  - ServiceNow
  - Snowflake
  - Data Engineering

Sends HTML email digest at 11:30 AM and 4:30 PM on weekdays (Mon–Fri).

SETUP:
  pip install requests beautifulsoup4 schedule
  Set environment variables (or edit CONFIG below):
    EMAIL_FROM      your Gmail address
    EMAIL_PASSWORD  Gmail App Password (NOT your normal password)
    EMAIL_TO        recipient address (can be same as FROM)

  To generate a Gmail App Password:
    myaccount.google.com → Security → 2-Step Verification → App Passwords

  Run:
    python sc_jobs_checker.py
    (keep running in background, or deploy to a VPS / cron)
"""

import os
import re
import smtplib
import logging
import hashlib
import json
import time
import schedule
from datetime import datetime, date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ─── CONFIG ──────────────────────────────────────────────────────────────────
CONFIG = {
    "email_from":     os.getenv("EMAIL_FROM",     "your_gmail@gmail.com"),
    "email_password": os.getenv("EMAIL_PASSWORD", "your_app_password_here"),
    "email_to":       os.getenv("EMAIL_TO",       "vasur28@gmail.com"),
    "smtp_host":      "smtp.gmail.com",
    "smtp_port":      587,
    "seen_jobs_file": Path.home() / ".sc_jobs_seen.json",
    "send_times":     ["11:30", "16:30"],   # 24h weekday schedule
    "request_delay":  2,                    # seconds between HTTP requests
    "user_agent":     (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
}

# ─── SEARCH TARGETS ──────────────────────────────────────────────────────────
SEARCHES = [
    # ── Testing / QA ──────────────────────────────────────────────────────────
    {
        "label": "QA / Testing",
        "icon": "🧪",
        "sources": [
            {
                "name": "CWJobs",
                "url": "https://www.cwjobs.co.uk/jobs/qa-tester/in-uk?keywords=SC+clearance+QA+tester&radius=50&searchIn=description",
            },
            {
                "name": "SecurityClearedJobs",
                "url": "https://www.securityclearedjobs.com/jobs/?keywords=QA+tester+SC+cleared&location=UK",
            },
            {
                "name": "Indeed",
                "url": "https://uk.indeed.com/jobs?q=SC+cleared+QA+tester&l=United+Kingdom&sort=date",
            },
        ],
    },
    # ── ServiceNow ────────────────────────────────────────────────────────────
    {
        "label": "ServiceNow",
        "icon": "⚙️",
        "sources": [
            {
                "name": "CWJobs",
                "url": "https://www.cwjobs.co.uk/jobs/servicenow/in-uk?keywords=ServiceNow+SC+clearance&radius=50",
            },
            {
                "name": "ITJobsWatch",
                "url": "https://www.itjobswatch.co.uk/find/SC-Cleared-ServiceNow-jobs-in-UK",
            },
            {
                "name": "Indeed",
                "url": "https://uk.indeed.com/jobs?q=ServiceNow+SC+cleared+contract&l=United+Kingdom&sort=date",
            },
        ],
    },
    # ── Snowflake ─────────────────────────────────────────────────────────────
    {
        "label": "Snowflake",
        "icon": "❄️",
        "sources": [
            {
                "name": "CWJobs",
                "url": "https://www.cwjobs.co.uk/jobs/snowflake/in-uk?keywords=Snowflake+SC+clearance&radius=50",
            },
            {
                "name": "ITJobsWatch",
                "url": "https://www.itjobswatch.co.uk/find/Snowflake-SC-Cleared-jobs-in-UK",
            },
            {
                "name": "Indeed",
                "url": "https://uk.indeed.com/jobs?q=Snowflake+SC+cleared+data+engineer&l=United+Kingdom&sort=date",
            },
        ],
    },
    # ── Data Engineering ──────────────────────────────────────────────────────
    {
        "label": "Data Engineering",
        "icon": "📊",
        "sources": [
            {
                "name": "CWJobs",
                "url": "https://www.cwjobs.co.uk/jobs/data-engineer/in-uk?keywords=data+engineer+SC+clearance&radius=50",
            },
            {
                "name": "ITJobsWatch",
                "url": "https://www.itjobswatch.co.uk/find/SC-Cleared-Data-Engineer-jobs-in-UK",
            },
            {
                "name": "Indeed",
                "url": "https://uk.indeed.com/jobs?q=SC+cleared+data+engineer&l=United+Kingdom&sort=date",
            },
            {
                "name": "Jobsite",
                "url": "https://www.jobsite.co.uk/jobs/contract/data-engineer?keywords=SC+clearance",
            },
        ],
    },
]

# ─── KEYWORDS used to filter relevant results ─────────────────────────────────
SC_KEYWORDS = [
    "sc clearance", "sc cleared", "sc-cleared", "security clearance",
    "nppv3", "sc required", "active sc",
]
ROLE_KEYWORDS = [
    "qa", "tester", "testing", "servicenow", "service now",
    "snowflake", "data engineer", "data engineering",
]

# ─── LOGGING ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ─── SEEN-JOBS CACHE (avoid duplicate emails) ─────────────────────────────────
def load_seen() -> set:
    p = CONFIG["seen_jobs_file"]
    if p.exists():
        try:
            return set(json.loads(p.read_text()))
        except Exception:
            pass
    return set()


def save_seen(seen: set):
    CONFIG["seen_jobs_file"].write_text(json.dumps(list(seen)))


def job_id(title: str, url: str) -> str:
    raw = f"{title.lower().strip()}|{url.strip()}"
    return hashlib.md5(raw.encode()).hexdigest()


# ─── HTTP HELPERS ────────────────────────────────────────────────────────────
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": CONFIG["user_agent"]})


def fetch(url: str) -> BeautifulSoup | None:
    try:
        r = SESSION.get(url, timeout=15)
        r.raise_for_status()
        return BeautifulSoup(r.text, "html.parser")
    except Exception as e:
        log.warning(f"Fetch failed [{url}]: {e}")
        return None


# ─── SITE-SPECIFIC PARSERS ────────────────────────────────────────────────────
def parse_cwjobs(soup: BeautifulSoup) -> list[dict]:
    jobs = []
    for card in soup.select("article.job-result-card, div.job-card"):
        try:
            title_el = card.select_one("h2 a, h3 a, .job-result-title a")
            if not title_el:
                continue
            title = title_el.get_text(strip=True)
            href  = title_el.get("href", "")
            link  = href if href.startswith("http") else f"https://www.cwjobs.co.uk{href}"
            salary_el  = card.select_one(".job-result-salary, .salary")
            location_el= card.select_one(".job-result-location, .location")
            date_el    = card.select_one(".job-result-date, .date")
            snippet_el = card.select_one(".job-result-description, .description")
            jobs.append({
                "title":    title,
                "link":     link,
                "salary":   salary_el.get_text(strip=True)   if salary_el   else "",
                "location": location_el.get_text(strip=True) if location_el else "",
                "date":     date_el.get_text(strip=True)     if date_el     else "",
                "snippet":  snippet_el.get_text(strip=True)[:200] if snippet_el else "",
            })
        except Exception:
            continue
    return jobs


def parse_indeed(soup: BeautifulSoup) -> list[dict]:
    jobs = []
    for card in soup.select("div.job_seen_beacon, div.jobsearch-SerpJobCard, li.css-5lfssm"):
        try:
            title_el = card.select_one("h2.jobTitle a, a.jcs-JobTitle, h2 a")
            if not title_el:
                continue
            title = title_el.get_text(strip=True)
            href  = title_el.get("href", "")
            link  = href if href.startswith("http") else f"https://uk.indeed.com{href}"
            salary_el   = card.select_one(".salary-snippet-container, .metadata.salary-snippet-container span")
            location_el = card.select_one(".companyLocation, div[data-testid='text-location']")
            date_el     = card.select_one(".date, span.date")
            snippet_el  = card.select_one(".job-snippet, ul.css-9446pr")
            jobs.append({
                "title":    title,
                "link":     link,
                "salary":   salary_el.get_text(strip=True)   if salary_el   else "",
                "location": location_el.get_text(strip=True) if location_el else "UK",
                "date":     date_el.get_text(strip=True)     if date_el     else "",
                "snippet":  snippet_el.get_text(strip=True)[:200] if snippet_el else "",
            })
        except Exception:
            continue
    return jobs


def parse_itjobswatch(soup: BeautifulSoup) -> list[dict]:
    jobs = []
    for row in soup.select("table tr, div.job-list-item"):
        try:
            links = row.select("a")
            if not links:
                continue
            title_el = links[0]
            title = title_el.get_text(strip=True)
            href  = title_el.get("href", "")
            link  = href if href.startswith("http") else f"https://www.itjobswatch.co.uk{href}"
            cells = row.select("td")
            jobs.append({
                "title":    title,
                "link":     link,
                "salary":   cells[2].get_text(strip=True) if len(cells) > 2 else "",
                "location": cells[1].get_text(strip=True) if len(cells) > 1 else "UK",
                "date":     cells[0].get_text(strip=True) if len(cells) > 0 else "",
                "snippet":  "",
            })
        except Exception:
            continue
    return jobs


def parse_jobsite(soup: BeautifulSoup) -> list[dict]:
    jobs = []
    for card in soup.select("article.job-result, div.job-card, div[data-job-id]"):
        try:
            title_el = card.select_one("h2 a, h3 a, a.job-title")
            if not title_el:
                continue
            title = title_el.get_text(strip=True)
            href  = title_el.get("href", "")
            link  = href if href.startswith("http") else f"https://www.jobsite.co.uk{href}"
            salary_el   = card.select_one(".salary, .job-salary")
            location_el = card.select_one(".location, .job-location")
            date_el     = card.select_one(".date, .job-date")
            snippet_el  = card.select_one(".description, .job-description")
            jobs.append({
                "title":    title,
                "link":     link,
                "salary":   salary_el.get_text(strip=True)   if salary_el   else "",
                "location": location_el.get_text(strip=True) if location_el else "UK",
                "date":     date_el.get_text(strip=True)     if date_el     else "",
                "snippet":  snippet_el.get_text(strip=True)[:200] if snippet_el else "",
            })
        except Exception:
            continue
    return jobs


def parse_sec_cleared_jobs(soup: BeautifulSoup) -> list[dict]:
    jobs = []
    for card in soup.select("div.job-listing, article, div.vacancy"):
        try:
            title_el = card.select_one("h2 a, h3 a, a.job-title, .vacancy-title a")
            if not title_el:
                continue
            title = title_el.get_text(strip=True)
            href  = title_el.get("href", "")
            link  = href if href.startswith("http") else f"https://www.securityclearedjobs.com{href}"
            salary_el   = card.select_one(".salary, .rate")
            location_el = card.select_one(".location")
            date_el     = card.select_one(".date, .posted")
            snippet_el  = card.select_one(".description, .summary")
            jobs.append({
                "title":    title,
                "link":     link,
                "salary":   salary_el.get_text(strip=True)   if salary_el   else "",
                "location": location_el.get_text(strip=True) if location_el else "UK",
                "date":     date_el.get_text(strip=True)     if date_el     else "",
                "snippet":  snippet_el.get_text(strip=True)[:200] if snippet_el else "",
            })
        except Exception:
            continue
    return jobs


PARSER_MAP = {
    "CWJobs":              parse_cwjobs,
    "Indeed":              parse_indeed,
    "ITJobsWatch":         parse_itjobswatch,
    "Jobsite":             parse_jobsite,
    "SecurityClearedJobs": parse_sec_cleared_jobs,
}


# ─── RELEVANCE FILTER ─────────────────────────────────────────────────────────
def is_relevant(job: dict) -> bool:
    combined = (job["title"] + " " + job["snippet"]).lower()
    has_sc   = any(kw in combined for kw in SC_KEYWORDS)
    has_role = any(kw in combined for kw in ROLE_KEYWORDS)
    return has_sc or has_role   # at least one match (URL already targets SC roles)


# ─── SCRAPING ────────────────────────────────────────────────────────────────
def scrape_all() -> dict[str, list[dict]]:
    """Returns {category_label: [job, ...]} with only new unseen jobs."""
    seen    = load_seen()
    results = {}
    new_ids = set()

    for category in SEARCHES:
        label    = category["label"]
        cat_jobs = []

        for source in category["sources"]:
            name = source["name"]
            url  = source["url"]
            log.info(f"Fetching {label} / {name} ...")
            soup = fetch(url)
            time.sleep(CONFIG["request_delay"])

            if soup is None:
                continue

            parser = PARSER_MAP.get(name)
            if not parser:
                log.warning(f"No parser for {name}, skipping")
                continue

            raw_jobs = parser(soup)
            log.info(f"  → found {len(raw_jobs)} raw listings")

            for job in raw_jobs:
                if not job["title"] or not job["link"]:
                    continue
                if not is_relevant(job):
                    continue
                jid = job_id(job["title"], job["link"])
                if jid in seen:
                    continue
                job["source"] = name
                job["jid"]    = jid
                cat_jobs.append(job)
                new_ids.add(jid)

        if cat_jobs:
            results[label] = cat_jobs

    # Persist new IDs
    seen.update(new_ids)
    save_seen(seen)
    log.info(f"Total new jobs found: {sum(len(v) for v in results.values())}")
    return results


# ─── EMAIL BUILDER ───────────────────────────────────────────────────────────
CATEGORY_ICONS = {s["label"]: s["icon"] for s in SEARCHES}

def build_email_html(results: dict[str, list[dict]], slot: str) -> str:
    now        = datetime.now()
    date_str   = now.strftime("%A %d %B %Y")
    total_jobs = sum(len(v) for v in results.values())

    # header
    html = f"""
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body        {{ font-family: Calibri, Arial, sans-serif; background:#f4f6f9; margin:0; padding:0; }}
  .wrap       {{ max-width:700px; margin:0 auto; background:#fff; border-radius:8px;
                 box-shadow:0 2px 8px rgba(0,0,0,0.1); overflow:hidden; }}
  .header     {{ background:#1F4E79; padding:24px 28px; color:#fff; }}
  .header h1  {{ margin:0 0 4px; font-size:22px; }}
  .header p   {{ margin:0; font-size:13px; opacity:.85; }}
  .badge      {{ display:inline-block; background:#2E75B6; color:#fff;
                 border-radius:20px; padding:3px 12px; font-size:12px;
                 font-weight:bold; margin-left:8px; }}
  .section    {{ padding:16px 28px 8px; border-bottom:2px solid #e8eef5; }}
  .sec-title  {{ font-size:16px; font-weight:bold; color:#1F4E79; margin:0 0 10px; }}
  .job-card   {{ border:1px solid #dde5f0; border-radius:6px; padding:12px 14px;
                 margin-bottom:10px; background:#fafcff; }}
  .job-title  {{ font-size:14px; font-weight:bold; color:#1a1a1a; margin:0 0 4px; }}
  .job-title a {{ color:#1F4E79; text-decoration:none; }}
  .job-title a:hover {{ text-decoration:underline; }}
  .meta       {{ font-size:12px; color:#555; margin:0 0 5px; }}
  .meta span  {{ margin-right:14px; }}
  .snippet    {{ font-size:12px; color:#444; margin:4px 0 0; line-height:1.5; }}
  .src-badge  {{ display:inline-block; font-size:10px; background:#e8eef5;
                 color:#1F4E79; border-radius:4px; padding:1px 6px;
                 font-weight:bold; margin-left:6px; }}
  .no-jobs    {{ font-size:13px; color:#888; padding:8px 0; }}
  .footer     {{ padding:16px 28px; background:#f0f4f9; font-size:11px;
                 color:#888; text-align:center; }}
  .summary    {{ padding:14px 28px; background:#e8f0fb; border-bottom:1px solid #c9d9f0; }}
  .summary p  {{ margin:0; font-size:13px; color:#1F4E79; }}
</style>
</head>
<body>
<div class="wrap">
  <div class="header">
    <h1>SC-Cleared Jobs Digest &nbsp;<span class="badge">{slot}</span></h1>
    <p>{date_str} &nbsp;|&nbsp; {total_jobs} new role{"s" if total_jobs!=1 else ""} found across all categories</p>
  </div>
  <div class="summary">
    <p>Categories covered: <strong>QA/Testing</strong> &nbsp;|&nbsp;
       <strong>ServiceNow</strong> &nbsp;|&nbsp;
       <strong>Snowflake</strong> &nbsp;|&nbsp;
       <strong>Data Engineering</strong> &nbsp;&mdash;&nbsp;
       SC Clearance / NPPV3 roles only &nbsp;|&nbsp; UK-wide</p>
  </div>
"""

    if not results:
        html += """
  <div class="section">
    <p class="no-jobs">No new SC-cleared roles found since last check. All previously seen roles have been filtered out.</p>
  </div>
"""
    else:
        for category in SEARCHES:
            label = category["label"]
            icon  = category["icon"]
            jobs  = results.get(label, [])
            html += f"""
  <div class="section">
    <div class="sec-title">{icon} {label} <span style="font-size:12px;color:#888;font-weight:normal;">({len(jobs)} new)</span></div>
"""
            if not jobs:
                html += '    <p class="no-jobs">No new roles this check.</p>\n'
            else:
                for job in jobs[:15]:  # cap at 15 per category
                    title   = job.get("title",    "Untitled")
                    link    = job.get("link",     "#")
                    salary  = job.get("salary",   "")
                    loc     = job.get("location", "UK")
                    posted  = job.get("date",     "")
                    snippet = job.get("snippet",  "")
                    source  = job.get("source",   "")

                    meta_parts = []
                    if salary:   meta_parts.append(f"💷 {salary}")
                    if loc:      meta_parts.append(f"📍 {loc}")
                    if posted:   meta_parts.append(f"🗓 {posted}")

                    html += f"""
    <div class="job-card">
      <div class="job-title">
        <a href="{link}" target="_blank">{title}</a>
        <span class="src-badge">{source}</span>
      </div>
      <div class="meta">{'&nbsp;&nbsp;'.join(f'<span>{p}</span>' for p in meta_parts)}</div>
      {'<div class="snippet">' + snippet[:180] + ('...' if len(snippet) > 180 else '') + '</div>' if snippet else ''}
    </div>
"""
            html += "  </div>\n"

    html += f"""
  <div class="footer">
    Auto-generated by SC Jobs Checker &nbsp;|&nbsp; {now.strftime("%H:%M")} &nbsp;|&nbsp;
    Reply to vasur28@gmail.com to stop &nbsp;|&nbsp; Next check: {"4:30 PM" if slot=="11:30 AM" else "11:30 AM tomorrow"}
  </div>
</div>
</body>
</html>
"""
    return html


def build_email_text(results: dict[str, list[dict]], slot: str) -> str:
    lines = [f"SC-Cleared Jobs Digest — {slot}", "=" * 50]
    for category in SEARCHES:
        label = category["label"]
        jobs  = results.get(label, [])
        lines.append(f"\n{category['icon']} {label} ({len(jobs)} new)")
        lines.append("-" * 40)
        if not jobs:
            lines.append("  No new roles this check.")
        else:
            for job in jobs[:10]:
                lines.append(f"  • {job['title']}")
                if job.get("salary"):   lines.append(f"    Rate: {job['salary']}")
                if job.get("location"): lines.append(f"    Location: {job['location']}")
                lines.append(f"    {job['link']}")
    return "\n".join(lines)


# ─── EMAIL SENDER ────────────────────────────────────────────────────────────
def send_email(results: dict[str, list[dict]], slot: str):
    total = sum(len(v) for v in results.values())
    subject = (
        f"[SC Jobs] {total} New Role{'s' if total != 1 else ''} Found — {slot} | "
        f"{date.today().strftime('%d %b %Y')}"
    )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = CONFIG["email_from"]
    msg["To"]      = CONFIG["email_to"]

    msg.attach(MIMEText(build_email_text(results, slot), "plain"))
    msg.attach(MIMEText(build_email_html(results, slot), "html"))

    try:
        with smtplib.SMTP(CONFIG["smtp_host"], CONFIG["smtp_port"]) as server:
            server.ehlo()
            server.starttls()
            server.login(CONFIG["email_from"], CONFIG["email_password"])
            server.sendmail(CONFIG["email_from"], CONFIG["email_to"], msg.as_string())
        log.info(f"Email sent successfully to {CONFIG['email_to']} ({total} jobs, slot={slot})")
    except Exception as e:
        log.error(f"Failed to send email: {e}")


# ─── MAIN JOB ────────────────────────────────────────────────────────────────
def run_job(slot: str):
    today = datetime.now().weekday()
    if today >= 5:  # Saturday=5, Sunday=6
        log.info(f"Weekend — skipping {slot} check")
        return

    log.info(f"=== Starting {slot} job check ===")
    results = scrape_all()
    send_email(results, slot)
    log.info(f"=== {slot} check complete ===\n")


# ─── SCHEDULER ───────────────────────────────────────────────────────────────
def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-now", metavar="SLOT", nargs="?", const="auto",
                        help="Run once immediately (for GitHub Actions). "
                             "Pass '11:30 AM' or '4:30 PM', or omit for auto-detect.")
    args = parser.parse_args()

    if args.run_now is not None:
        slot = args.run_now
        if slot == "auto":
            hour = datetime.now().hour
            slot = "11:30 AM" if hour < 14 else "4:30 PM"
        run_job(slot)
        return

    log.info("SC Jobs Checker starting...")
    log.info(f"Email from: {CONFIG['email_from']}")
    log.info(f"Email to:   {CONFIG['email_to']}")
    log.info(f"Schedule:   {', '.join(CONFIG['send_times'])} weekdays (Mon–Fri)")
    log.info(f"Seen-jobs cache: {CONFIG['seen_jobs_file']}")

    schedule.every().day.at("11:30").do(run_job, slot="11:30 AM")
    schedule.every().day.at("16:30").do(run_job, slot="4:30 PM")

    log.info("Scheduler running. Press Ctrl+C to stop.")
    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()
