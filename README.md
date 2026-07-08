# 🎯 Internship Tracker

A companion to [Morning-Briefing](https://github.com/akarev-maker/Morning-Briefing):
where the briefing *informs* you daily, this **tracks** cybersecurity internships
over time — remembering every posting it sees, your application status for each,
and (crucially) **application deadlines**, so you never miss one by applying late.

Runs daily via GitHub Actions and emails you an **urgency-sorted digest**:

- 🔴 **Closing soon** — postings with a known deadline you haven't acted on yet
- 🆕 **New since last run** — fresh postings (Massachusetts / remote first)
- 📋 **Your pipeline** — what you've applied to / are interviewing for / are watching

## How it works

1. `sources.py` fetches postings from curated GitHub internship lists (high volume)
   and **USAJOBS** (federal roles, which expose a real `ApplicationCloseDate`).
2. `store.py` folds them into a persistent store (`state/applications.json`),
   remembering your **status** and **notes** on each across runs.
3. `digest.py` sorts by deadline urgency and emails the digest.

The store is committed back to the repo each run, so your tracking history lives
in git.

### Setting a status

This phase has no UI — you set statuses by editing **`state/applications.json`**
and committing. Each record has a `status` field:

```
new · interested · applied · interviewing · offer · rejected · skip
```

- `new` / `interested` → still nagged about deadlines
- `applied` / `interviewing` → shown in your pipeline, not nagged
- `rejected` / `skip` → hidden
- `offer` → hidden from nags 🎉

> **Roadmap (phase 4):** a **GitHub Issues + Projects** board so you can triage
> status from your phone instead of editing JSON — each posting an issue, labels
> as columns.

## Setup

Same shape as Morning-Briefing. In a **new GitHub repo**:

1. Push this folder.
2. Add repo **Secrets** (Settings → Secrets and variables → Actions):
   | Secret | Value |
   |--------|-------|
   | `EMAIL_SENDER` | your Gmail address |
   | `EMAIL_RECIPIENT` | where the digest goes |
   | `EMAIL_PASSWORD` | Gmail **App Password** (needs 2FA) |
   | `USAJOBS_API_KEY` *(optional)* | free key from developer.usajobs.gov — unlocks deadline-bearing federal postings |
   | `USAJOBS_EMAIL` *(optional)* | the email you registered with USAJOBS |
3. **Actions tab → Internship Tracker → Run workflow** to test.

## Deadlines: the honest state

The **GitHub internship lists don't publish application deadlines** — so those
postings are tracked by freshness, and the 🔴 Closing Soon section is powered
mainly by **USAJOBS** (which does). The next big source to add (phase 2) is
company **ATS pages** (Greenhouse / Lever / Workday), many of which expose a
close date — that's where richer deadline coverage comes from.

## Development

```bash
pip install -r requirements.txt pytest
pytest
python sources.py   # inspect fetched postings
```

## Roadmap

- **Phase 1 (this):** ingest + persistent store + status + urgency digest ✅
- **Phase 2:** ATS-page deadline scraping (Greenhouse/Lever/Workday)
- **Phase 3:** a static dashboard (like the briefing's) — pipeline funnel + deadline calendar
- **Phase 4:** GitHub Issues/Projects board for phone-friendly status triage
