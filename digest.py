"""
digest.py — turn the applications store into an urgency-sorted digest email.

Sections:
  🔴 CLOSING SOON   — postings with a deadline inside the window you haven't acted on
  🆕 NEW            — postings first seen this run (MA/remote first)
  📋 PIPELINE       — your applied / interviewing / interested items at a glance
"""

import logging
import os
import smtplib
import ssl
from datetime import date, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import markdown as md

from store import NEEDS_ACTION_STATUSES, active_records
from util import md_escape, safe_url

logger = logging.getLogger("tracker.digest")

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465
DEADLINE_WINDOW_DAYS = 21  # how far out "closing soon" looks

C_BG = "#0d1117"
C_ACCENT = "#58a6ff"


def _days_until(deadline_iso):
    if not deadline_iso:
        return None
    try:
        d = datetime.strptime(deadline_iso, "%Y-%m-%d").date()
        return (d - date.today()).days
    except ValueError:
        return None


def _posting_line(rec, extra=""):
    name = md_escape(rec.get("title", "(untitled)"))
    link = safe_url(rec.get("link"))
    title = f"**[{name}]({link})**" if link else f"**{name}**"
    company = f" — {md_escape(rec['company'])}" if rec.get("company") else ""
    meta = f"📍 {md_escape(rec.get('location_str', ''))} · {md_escape(str(rec.get('term', '')))}"
    line = f"- {title}{company}\n  <br>{meta}"
    if extra:
        line += f"\n  <br>{extra}"
    return line


def build_digest_markdown(store, new_ids):
    records = active_records(store)
    new_ids = set(new_ids)

    # --- closing soon ---
    closing = []
    for r in records:
        days = _days_until(r.get("deadline"))
        if days is None or r.get("status") not in NEEDS_ACTION_STATUSES:
            continue
        if 0 <= days <= DEADLINE_WINDOW_DAYS:
            closing.append((days, r))
    closing.sort(key=lambda t: t[0])

    new_recs = sorted(
        [r for r in records if r.get("id") in new_ids],
        key=lambda r: (r.get("rank", 2), -(r.get("date_posted") or 0)),
    )

    lines = [
        f"## 🎯 Internship Tracker — {date.today():%A, %B %d, %Y}",
        "",
        f"*{len(records)} active · {len(new_recs)} new · {len(closing)} closing "
        f"within {DEADLINE_WINDOW_DAYS} days*",
        "",
    ]

    lines.append("## 🔴 CLOSING SOON")
    if closing:
        for days, r in closing:
            when = "**closes today!**" if days == 0 else (
                "**closes tomorrow**" if days == 1 else f"**{days} days left**"
            )
            lines.append(_posting_line(r, extra=f"⏰ {when} (deadline {r['deadline']})"))
    else:
        lines.append("Nothing with a known deadline closing soon.")

    lines.append("\n## 🆕 NEW SINCE LAST RUN")
    if new_recs:
        for r in new_recs:
            dl = f" · ⏰ deadline {r['deadline']}" if r.get("deadline") else ""
            lines.append(_posting_line(r, extra=f"Status: `new`{dl}"))
    else:
        lines.append("No new postings today.")

    lines.append("\n## 📋 YOUR PIPELINE")
    pipeline = [
        ("🟢 Interviewing", "interviewing"),
        ("📨 Applied", "applied"),
        ("👀 Interested", "interested"),
    ]
    any_pipeline = False
    for label, status in pipeline:
        items = [r for r in records if r.get("status") == status]
        if items:
            any_pipeline = True
            lines.append(f"\n**{label}** ({len(items)})")
            for r in sorted(items, key=lambda r: r.get("company", "")):
                lines.append(_posting_line(r))
    untriaged = sum(1 for r in records if r.get("status") == "new")
    if untriaged:
        lines.append(
            f"\n*{untriaged} posting(s) still marked `new` — triage them in "
            "`state/applications.json` (set status to interested/applied/skip).*"
        )
    if not any_pipeline and not untriaged:
        lines.append("Pipeline is empty — apply to something above!")

    return "\n".join(lines)


# --- email rendering --------------------------------------------------------
STYLES = f"""<style>
  .body h2 {{ color:{C_ACCENT}; font-size:16px; margin:24px 0 10px;
             padding-bottom:6px; border-bottom:1px solid #21262d; }}
  .body a {{ color:#79c0ff; text-decoration:none; }}
  .body strong {{ color:#e6edf3; }}
  .body code {{ background:#161b22; color:#ff7b72; padding:1px 5px; border-radius:4px; }}
  .body ul {{ padding-left:18px; }} .body li {{ margin:8px 0; }}
</style>"""


def render_html(markdown_text):
    body = md.markdown(markdown_text, extensions=["extra", "sane_lists"])
    return f"""{STYLES}
<div style="background:{C_BG};padding:0;margin:0">
  <div class="body" style="max-width:680px;margin:0 auto;padding:24px 20px;
       font-family:'SFMono-Regular',Consolas,Menlo,monospace;color:#c9d1d9;
       background:{C_BG};font-size:14px;line-height:1.6">
    {body}
    <div style="border-top:1px solid #21262d;margin-top:26px;padding-top:12px;
         color:#6e7681;font-size:11px">
      Auto-generated by your Internship Tracker · edit statuses in
      state/applications.json
    </div>
  </div>
</div>"""


def render_text(markdown_text):
    return f"INTERNSHIP TRACKER\n{'=' * 40}\n\n{markdown_text}"


def _send(subject, html_body, text_body):
    sender = os.environ.get("EMAIL_SENDER")
    recipient = os.environ.get("EMAIL_RECIPIENT")
    password = os.environ.get("EMAIL_PASSWORD")
    missing = [n for n, v in (("EMAIL_SENDER", sender), ("EMAIL_RECIPIENT", recipient),
                              ("EMAIL_PASSWORD", password)) if not v]
    if missing:
        raise RuntimeError(f"Missing email env var(s): {', '.join(missing)}")
    message = MIMEMultipart("alternative")
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = recipient
    message.attach(MIMEText(text_body, "plain", "utf-8"))
    message.attach(MIMEText(html_body, "html", "utf-8"))
    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=context) as server:
        server.login(sender, password)
        server.sendmail(sender, recipient, message.as_string())
    logger.info("Digest email sent to %s.", recipient)


def send_digest(store, new_ids):
    markdown_text = build_digest_markdown(store, new_ids)
    subject = f"🎯 Internship Tracker — {date.today():%b %d}"
    _send(subject, render_html(markdown_text), render_text(markdown_text))


def send_failure_email(error_text):
    """Best-effort alert so a failed run isn't silent."""
    sender = os.environ.get("EMAIL_SENDER")
    recipient = os.environ.get("EMAIL_RECIPIENT")
    password = os.environ.get("EMAIL_PASSWORD")
    if not (sender and recipient and password):
        logger.error("Cannot send failure alert — email env vars missing.")
        return
    body = f"Your internship tracker run FAILED.\n\n{error_text}\n\nCheck the Actions logs."
    message = MIMEText(body, "plain", "utf-8")
    message["Subject"] = "⚠️ Internship Tracker FAILED"
    message["From"] = sender
    message["To"] = recipient
    try:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=context) as server:
            server.login(sender, password)
            server.sendmail(sender, recipient, message.as_string())
        logger.info("Failure alert sent.")
    except Exception as exc:  # noqa: BLE001
        logger.error("Could not send failure alert: %s", exc)
