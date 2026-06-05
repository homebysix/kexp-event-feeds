# KEXP event feeds

[![Build & deploy](https://github.com/homebysix/kexp-event-feeds/actions/workflows/update-feeds.yml/badge.svg)](https://github.com/homebysix/kexp-event-feeds/actions/workflows/update-feeds.yml)

Unofficial, self-updating **iCalendar (ICS)** subscriptions and **RSS** feeds for [KEXP events](https://kexp.org/events/kexp-events/), broken out by category. A scheduled GitHub Action scrapes the listings once a day, regenerates the feeds, and publishes them to GitHub Pages.

Not affiliated with KEXP; just created this project because their website doesn't provide a lightweight way to subscribe to upcoming events.

## Local development

Requires Python 3.13 (CI runs 3.13; the code uses 3.11+ features like `datetime.UTC`).

```sh
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python scraper.py
# open public/index.html
```

Set `SITE_BASE=https://you.github.io/repo` to bake absolute `webcal://` subscribe links into `index.html` and RSS self links; without it the links are relative.
