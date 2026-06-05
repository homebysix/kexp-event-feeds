#!/usr/bin/env python3
"""Scrape events by category and emit per-category ICS calendars + RSS feeds,
plus an index.html subscribe page.

Source: https://kexp.org/events/kexp-events/?category=<slug>  (Aldryn Events / Django CMS).
Each event is an <article class="aldryn-events-article"> whose ".addeventatc" block
carries clean structured fields (start, end, timezone, title, location); the detail
link contains a stable numeric id (…_485603/) used as the iCal UID.

For every category in CATEGORIES we write into OUT_DIR:
  - <slug>.ics   subscribable calendar (DTSTART/DTEND in UTC, unambiguous)
  - <slug>.xml   RSS 2.0 feed of the same events
We also write events.json (all categories) and index.html (subscribe links).

A category that parses zero events keeps its PREVIOUS files untouched (a transient
scrape break or layout change never wipes a live feed) and is logged. The process
exits non-zero only if EVERY category failed, so the caller can skip the commit.
"""

from __future__ import annotations

import html as html_lib
import json
import os
import re
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from feedgen.feed import FeedGenerator
from icalendar import Calendar, Event

BASE_URL = "https://kexp.org"
LISTING = BASE_URL + "/events/kexp-events/"
OUT_DIR = Path(__file__).parent / "public"
PACIFIC_TZNAME = "America/Los_Angeles"
PACIFIC = ZoneInfo(PACIFIC_TZNAME)
DEFAULT_DURATION = timedelta(minutes=30)  # when an event omits its end time
REQUEST_DELAY = 1.0  # be polite to kexp.org
VERSION = "1.0"
USER_AGENT = f"kexp-event-feeds/{VERSION} (+https://github.com/homebysix/kexp-event-feeds)"
ID_RE = re.compile(r"_(\d+)/?$")

# slug -> label, from the site's own category filter.
CATEGORIES: dict[str, str] = {
    "public": "Public",
    "in-studio": "In-studio",
    "broadcast-only": "Broadcast Only",
    "artist-education": "Artist Education",
    "book-reading": "Book Reading",
    "gathering-space": "Gathering Space",
    "offsite": "Offsite",
}

# Absolute on GitHub Actions; relative when unset.
SITE_BASE = os.environ.get("SITE_BASE", "").rstrip("/")


def fetch(url: str) -> str:
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    return resp.text


def _txt(node) -> str:
    return node.get_text(strip=True) if node else ""


def _parse_dt(text: str, tz: ZoneInfo) -> datetime | None:
    """Parse the addeventatc 'MM/DD/YYYY HH:MM' format; None if unparseable."""
    try:
        return datetime.strptime(text, "%m/%d/%Y %H:%M").replace(tzinfo=tz)
    except ValueError:
        return None


def parse_events(html: str) -> tuple[list[dict], list[str], bool]:
    """Return (events, pagination_hrefs, valid) parsed from one listing page.

    `valid` is whether the page rendered as a real events listing (see below).
    """
    soup = BeautifulSoup(html, "html.parser")
    events: list[dict] = []

    for art in soup.select("article.aldryn-events-article"):
        atc = art.select_one(".addeventatc")
        link = art.select_one(".EventItem-body h3 a[href]") or art.select_one(
            "a[href*='/events/kexp-events/']"
        )
        if not atc or not link:
            continue

        href = link["href"]
        m = ID_RE.search(href)
        if not m:
            continue
        event_id = m.group(1)
        detail_url = urljoin(BASE_URL, href)

        tz_name = _txt(atc.select_one(".timezone")) or PACIFIC_TZNAME
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = PACIFIC

        start_local = _parse_dt(_txt(atc.select_one(".start")), tz)
        if start_local is None:
            continue
        end_local = (
            _parse_dt(_txt(atc.select_one(".end")), tz)
            or start_local + DEFAULT_DURATION
        )

        events.append(
            {
                "id": event_id,
                "uid": f"{event_id}@kexp.org",
                "title": _txt(atc.select_one(".title")) or _txt(link),
                "url": detail_url,
                "location": _txt(atc.select_one(".location")),
                "start_utc": start_local.astimezone(UTC),
                "end_utc": end_local.astimezone(UTC),
            }
        )

    # The listing always renders this container, even with zero events; its absence
    # means we got a non-listing (error/truncated page), so don't trust a 0-event parse.
    valid = soup.select_one(".aldryn-events-list") is not None
    pages = [a["href"] for a in soup.select(".aldryn-events-pagination a[href]")]
    return events, pages, valid


def scrape_category(slug: str) -> list[dict]:
    """Fetch a category, following pagination, returning de-duped sorted events.

    Raises RuntimeError if the first page doesn't look like a rendered events
    listing, so the caller treats it as a transient failure and preserves the
    category's existing feeds rather than overwriting them with an empty result.
    """
    seen_pages: set[str] = set()
    queue = [f"{LISTING}?category={slug}"]
    by_id: dict[str, dict] = {}
    first = True

    while queue:
        url = queue.pop(0)
        if url in seen_pages:
            continue
        seen_pages.add(url)
        events, pages, valid = parse_events(fetch(url))
        if first and not valid:
            raise RuntimeError(f"{url} is not a recognizable events listing")
        first = False
        for ev in events:
            by_id.setdefault(ev["id"], ev)
        for href in pages:
            full = urljoin(LISTING, href)
            if full not in seen_pages:
                queue.append(full)
        time.sleep(REQUEST_DELAY)

    return sorted(by_id.values(), key=lambda e: e["start_utc"])


def build_ics(events: list[dict], label: str, now: datetime) -> bytes:
    cal = Calendar()
    cal.add("prodid", f"-//homebysix//kexp-event-feeds {VERSION}//EN")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("method", "PUBLISH")
    cal.add("x-wr-calname", f"KEXP — {label}")
    cal.add("x-wr-timezone", PACIFIC_TZNAME)
    cal.add("x-published-ttl", "PT24H")
    cal.add("refresh-interval;value=duration", "PT24H")

    for ev in events:
        ve = Event()
        ve.add("uid", ev["uid"])
        ve.add("summary", ev["title"])
        ve.add("dtstart", ev["start_utc"])
        ve.add("dtend", ev["end_utc"])
        ve.add("dtstamp", now)
        ve.add("last-modified", now)
        if ev["location"]:
            ve.add("location", ev["location"])
        ve.add("url", ev["url"])
        ve.add("description", ev["url"])
        cal.add_component(ve)
    return cal.to_ical()


def build_rss(events: list[dict], slug: str, label: str, now: datetime) -> bytes:
    fg = FeedGenerator()
    fg.title(f"KEXP — {label}")
    fg.link(href=f"{LISTING}?category={slug}", rel="alternate")
    fg.description(f"Upcoming KEXP “{label}” events.")
    fg.language("en")
    fg.lastBuildDate(now)

    for ev in sorted(events, key=lambda e: e["start_utc"], reverse=True):
        fe = fg.add_entry()
        fe.id(ev["uid"])
        fe.guid(ev["uid"], permalink=False)
        when = ev["start_utc"].astimezone(PACIFIC)
        fe.title(f"{ev['title']} — {when:%a %b %-d, %Y %-I:%M %p %Z}")
        fe.link(href=ev["url"])
        fe.pubDate(ev["start_utc"])
        body = f"{when:%A, %B %-d, %Y at %-I:%M %p %Z}"
        if ev["location"]:
            body += f"<br>Location: {ev['location']}"
        body += f'<br><a href="{ev["url"]}">Event details</a>'
        fe.description(body)
    return fg.rss_str(pretty=True)


def build_index(stats: list[dict], now: datetime) -> str:
    """A static subscribe page: per-category ICS (webcal + https) and RSS links."""

    def feed_url(name: str) -> str:
        return f"{SITE_BASE}/{name}" if SITE_BASE else name

    def webcal(name: str) -> str:
        if SITE_BASE.startswith("https://"):
            return "webcal://" + SITE_BASE[len("https://") :] + "/" + name
        return feed_url(name)

    # (css tone class, label text) per status.
    status_meta = {
        "ok": ("ok", lambda s: f"{s['count']} upcoming"),
        "empty": ("muted", lambda s: "No upcoming events"),
        "stale": ("warn", lambda s: "Showing last known events"),
        "error": ("warn", lambda s: "Temporarily unavailable"),
    }
    cards = []
    for s in stats:
        ics, xml = f"{s['slug']}.ics", f"{s['slug']}.xml"
        tone, text = status_meta[s["status"]]
        label = html_lib.escape(s["label"])
        cards.append(f"""      <article class="card">
        <div class="card__top">
          <h2 class="card__title">{label}</h2>
          <span class="pill pill--{tone}">{text(s)}</span>
        </div>
        <a class="btn" href="{webcal(ics)}">
          <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><path fill="currentColor" d="M7 2v2H5a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V6a2 2 0 0 0-2-2h-2V2h-2v2H9V2H7Zm12 7v10H5V9h14Z"/></svg>
          Subscribe to calendar
        </a>
        <div class="card__links">
          <a href="{feed_url(ics)}">ICS file</a>
          <a href="{feed_url(xml)}">RSS feed</a>
        </div>
      </article>""")
    grid = "\n".join(cards)
    updated = now.astimezone(PACIFIC).strftime("%b %-d, %Y %-I:%M %p %Z")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>KEXP event feeds</title>
<style>
  :root {{
    --bg: #0e1311; --surface: #161b19; --surface-2: #1d2421;
    --text: #e9ece9; --muted: #9aa49e; --border: #2a322e;
    --accent: #2f9268; --accent-ink: #fff;
    --ok: #5cb0d6; --warn: #d98a5b; --shadow: 0 1px 2px rgba(0,0,0,.4), 0 8px 24px rgba(0,0,0,.25);
  }}
  @media (prefers-color-scheme: light) {{
    :root {{
      --bg: #f1f5f3; --surface: #fff; --surface-2: #e9efec;
      --text: #18201c; --muted: #5e6a64; --border: #dde5e0;
      --accent: #1f6b49; --ok: #1f6f8b; --warn: #b5683c;
      --shadow: 0 1px 2px rgba(0,0,0,.06), 0 8px 24px rgba(0,0,0,.08);
    }}
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 3rem 1.25rem 4rem;
    font: 16px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
    background: var(--bg); color: var(--text);
  }}
  .wrap {{ max-width: 880px; margin: 0 auto; }}
  header.page {{ margin-bottom: 2rem; }}
  h1 {{ margin: 0 0 .4rem; font-size: clamp(1.7rem, 4vw, 2.4rem); letter-spacing: -.02em; }}
  .grid {{
    display: grid; gap: 1rem; margin-top: 2rem;
    grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  }}
  .card {{
    display: flex; flex-direction: column; gap: 1rem;
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 14px; padding: 1.1rem 1.1rem 1.2rem; box-shadow: var(--shadow);
    transition: border-color .15s ease;
  }}
  .card:hover {{ border-color: var(--accent); }}
  .card__top {{ display: flex; align-items: flex-start; justify-content: space-between; gap: .5rem; }}
  .card__title {{ margin: 0; font-size: 1.08rem; font-weight: 650; letter-spacing: -.01em; }}
  .pill {{
    flex: none; font-size: .7rem; font-weight: 600; padding: .2rem .55rem;
    border-radius: 999px; white-space: nowrap; border: 1px solid var(--border);
    background: var(--surface-2); color: var(--muted);
  }}
  .pill--ok {{ color: var(--ok); border-color: color-mix(in srgb, var(--ok) 40%, transparent); }}
  .pill--warn {{ color: var(--warn); border-color: color-mix(in srgb, var(--warn) 45%, transparent); }}
  .btn {{
    display: inline-flex; align-items: center; justify-content: center; gap: .5rem;
    margin-top: auto; padding: .6rem .8rem; border-radius: 10px;
    background: var(--accent); color: var(--accent-ink); text-decoration: none;
    font-weight: 600; font-size: .92rem; transition: filter .15s ease;
  }}
  .btn:hover {{ filter: brightness(1.08); }}
  .card__links {{ display: flex; gap: 1.1rem; font-size: .82rem; }}
  .card__links a {{ color: var(--muted); text-decoration: none; }}
  .card__links a:hover {{ color: var(--accent); text-decoration: underline; }}
  footer {{ margin-top: 2.5rem; color: var(--muted); font-size: .82rem; }}
  footer a {{ color: inherit; }}
</style>
</head>
<body>
  <div class="wrap">
    <header class="page">
      <h1>KEXP event feeds</h1>
    </header>
    <main class="grid">
{grid}
    </main>
    <footer>
      Source: <a href="{LISTING}">kexp.org/events</a> · Not affiliated with or endorsed by KEXP.
      <br>Refreshes roughly once a day · Last updated {updated}.
      <br>Spot a bug or want to improve this? <a href="https://github.com/homebysix/kexp-event-feeds">Pull requests welcome</a>.
    </footer>
  </div>
</body>
</html>
"""


def future_event_count(path: Path, now: datetime) -> int:
    """Count VEVENTs in an existing ICS whose start is still in the future.

    Used to decide whether a freshly-empty parse is legitimate (the old events
    simply aged out / there never were any → 0) or suspicious (upcoming events
    vanished while the page still rendered → preserve the old feed).
    """
    if not path.exists():
        return 0
    try:
        cal = Calendar.from_ical(path.read_bytes())
    except Exception:
        return 0
    count = 0
    for ve in cal.walk("VEVENT"):
        dt = ve.get("dtstart")
        start = getattr(dt, "dt", None)
        if not isinstance(start, datetime):
            continue
        if start.tzinfo is None:
            start = start.replace(tzinfo=UTC)
        if start >= now:
            count += 1
    return count


def main() -> int:
    now = datetime.now(UTC)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    results: list[dict] = []
    all_events: dict[str, dict] = {}

    # Pass 1: fetch all; tell a fetch error apart from a genuinely empty result.
    for slug, label in CATEGORIES.items():
        try:
            events = scrape_category(slug)
            results.append(
                {"slug": slug, "label": label, "events": events, "errored": False}
            )
            print(f"[{slug}] {len(events)} events")
        except Exception as exc:  # network/HTTP error for this category
            results.append(
                {"slug": slug, "label": label, "events": [], "errored": True}
            )
            print(f"[{slug}] fetch failed: {exc}", file=sys.stderr)

    # Sitewide guard: nothing anywhere likely means a layout break/outage —
    # bail without touching existing files.
    if sum(len(r["events"]) for r in results) == 0:
        print(
            "ERROR: zero events across all categories — likely a layout change. "
            "Leaving existing feeds untouched.",
            file=sys.stderr,
        )
        return 2

    # Pass 2: write feeds. Per category:
    #   error    -> fetch/parse failed; keep existing files.
    #   stale    -> parsed empty but existing feed still has upcoming events; keep them.
    #   ok/empty -> trusted; write feeds (an empty feed is valid, keeps the link live).
    stats: list[dict] = []
    degraded = False  # any category errored or was kept stale
    for r in results:
        slug, label, events = r["slug"], r["label"], r["events"]
        ics_path = OUT_DIR / f"{slug}.ics"
        if r["errored"]:
            status = "error"
            degraded = True
        elif not events and future_event_count(ics_path, now) > 0:
            status = "stale"
            degraded = True
            print(
                f"[{slug}] parsed 0 events but existing feed has upcoming events — "
                f"preserving (possible category-specific markup change)",
                file=sys.stderr,
            )
        else:
            ics_path.write_bytes(build_ics(events, label, now))
            (OUT_DIR / f"{slug}.xml").write_bytes(build_rss(events, slug, label, now))
            status = "ok" if events else "empty"
            for ev in events:
                all_events.setdefault(ev["id"], ev)
        stats.append(
            {"slug": slug, "label": label, "count": len(events), "status": status}
        )

    # events.json is an all-categories union, so only rewrite it on a fully clean
    # run (else keep the last good copy). Always write if none exists yet.
    events_json = OUT_DIR / "events.json"
    if not degraded or not events_json.exists():
        events_json.write_text(
            json.dumps(
                [
                    {
                        **ev,
                        "start_utc": ev["start_utc"].isoformat(),
                        "end_utc": ev["end_utc"].isoformat(),
                    }
                    for ev in sorted(all_events.values(), key=lambda e: e["start_utc"])
                ],
                indent=2,
            )
        )
    else:
        print(
            "Preserving existing events.json (run was degraded; not writing partial data).",
            file=sys.stderr,
        )

    (OUT_DIR / "index.html").write_text(build_index(stats, now))
    print(
        f"Wrote index across {len(CATEGORIES)} categories"
        f"{' (degraded — some feeds preserved)' if degraded else ''}."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
