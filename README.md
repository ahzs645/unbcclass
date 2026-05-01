# UNBC Course Catalogue Scraper

Python scraper for <https://tools.unbc.ca/course-catalogue>.

The site is a Blazor Server app. It does not expose a useful public JSON API for catalogue searches, so this scraper uses Playwright to drive the hydrated page with real DOM events and parse the rendered result list.

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
```

## Use

Full catalogue JSON. By default, this selects all subjects together once per term, so the full scrape is one search per available term:

```bash
python scrape_unbc.py --output data/unbc-course-catalogue.json
```

Single subject or term:

```bash
python scrape_unbc.py --subject CPSC --output data/cpsc-all-terms.json
python scrape_unbc.py --term 202601 --subject CPSC --output data/cpsc-202601.json
```

Force one search per subject instead of selecting all subjects together:

```bash
python scrape_unbc.py --split-subjects --output data/unbc-course-catalogue-by-subject.json
```

CSV:

```bash
python scrape_unbc.py --output data/unbc-course-catalogue.csv --format csv
```

Test with a small sample:

```bash
python scrape_unbc.py --limit 2 --headful --output data/sample.json
```

## Notes

- The script sets `#filter-term-code` and `#filter-subject`, dispatches bubbling `input` and `change` events, then clicks the `button.action` search button.
- It waits briefly after Blazor loads because the server circuit can overwrite early DOM changes during hydration. Adjust with `--hydration-delay` if needed.
- Results are parsed from `#results > ul > li`.
- JSON keeps each course as one object with a `sections` array and includes the rendered `rawText` and `rawHtml` for the course card.
- JSON also includes `renderedTables` so prerequisite/corequisite tables and other non-section tables are preserved.
- Each section includes the visible table cells, cell HTML, and parsed nested meeting-time rows when present.
- CSV flattens one row per course section and stringifies nested values.
- Full catalogue mode loops every non-`None` term and selects all subjects together for each term.
- Use `--split-subjects` if you need one search per term/subject pair.
- The script writes checkpoint output every 10 searches by default.
- See `SCRAPING_NOTES.md` for pre-2022 term tests and the SignalR replay experiment.
- A delay is kept between searches to reduce load on the Blazor Server circuit.
