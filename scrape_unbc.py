#!/usr/bin/env python3
"""
Scrape the UNBC course catalogue by driving the hydrated Blazor UI.

The catalogue does not expose a public search API, so this script uses
Playwright to select term/subject filters, click Search, and parse the rendered
DOM. JSON output preserves the richest data, including raw text and raw HTML.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError, async_playwright


CATALOGUE_URL = "https://tools.unbc.ca/course-catalogue"


@dataclass(frozen=True)
class Option:
    value: str
    label: str


@dataclass(frozen=True)
class Search:
    term_code: str
    term: str
    subject_code: str
    subject: str


def clean_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def clean_multiline(value: str | None) -> str:
    text = (value or "").replace("\r\n", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def camel_case(value: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", clean_text(value))
    if not words:
        return ""
    first, *rest = [word.lower() for word in words]
    return first + "".join(word[:1].upper() + word[1:] for word in rest)


def parse_course_title(title: str) -> dict[str, str]:
    match = re.match(r"^(.+?\s+\d+[A-Z]?)\s+-\s+(.+)$", title)
    if not match:
        return {"courseCode": "", "courseTitle": title}
    return {"courseCode": match.group(1), "courseTitle": match.group(2)}


async def get_options(page: Page, selector: str, *, skip_null: bool = False) -> list[Option]:
    raw_options = await page.locator(selector).evaluate(
        """(select) => Array.from(select.options).map((option) => ({
            value: option.value,
            label: option.textContent.trim()
        }))"""
    )
    options = [Option(value=item["value"], label=clean_text(item["label"])) for item in raw_options]
    if skip_null:
        options = [option for option in options if option.value and option.value != "null"]
    return options


def filter_options(options: list[Option], wanted: list[str] | None, label: str) -> list[Option]:
    if not wanted:
        return options

    wanted_set = {item.strip().lower() for item in wanted if item.strip()}
    selected = [
        option
        for option in options
        if option.value.lower() in wanted_set or option.label.lower() in wanted_set
    ]
    missing = sorted(wanted_set - {option.value.lower() for option in selected} - {option.label.lower() for option in selected})
    if missing:
        raise ValueError(f"Unknown {label}: {', '.join(missing)}")
    return selected


async def wait_for_page(page: Page, hydration_delay: float) -> None:
    await page.goto(CATALOGUE_URL, wait_until="domcontentloaded")
    await page.wait_for_selector("#filter-term-code")
    await page.wait_for_selector("#filter-subject")
    await page.wait_for_function("() => Boolean(window.Blazor)")
    if hydration_delay > 0:
        await page.wait_for_timeout(int(hydration_delay * 1000))


async def select_term(page: Page, term_code: str) -> None:
    await page.locator("#filter-term-code").evaluate(
        """(select, termCode) => {
            select.value = termCode;
        }""",
        term_code,
    )
    await dispatch_blazor_events(page, "#filter-term-code")
    await page.wait_for_function(
        """(termCode) => document.querySelector("#filter-term-code")?.value === termCode""",
        arg=term_code,
    )


async def select_subject(page: Page, subject_code: str) -> None:
    await page.locator("#filter-subject").evaluate(
        """(select, subjectCode) => {
            for (const option of select.options) {
                option.selected = option.value === subjectCode;
            }
        }""",
        subject_code,
    )
    await dispatch_blazor_events(page, "#filter-subject")
    await page.wait_for_function(
        """(subjectCode) => Array.from(document.querySelector("#filter-subject")?.selectedOptions || []).some((option) => option.value === subjectCode)""",
        arg=subject_code,
    )


async def dispatch_blazor_events(page: Page, selector: str) -> None:
    await page.locator(selector).evaluate(
        """(element) => {
            element.dispatchEvent(new Event("input", { bubbles: true }));
            element.dispatchEvent(new Event("change", { bubbles: true }));
        }"""
    )
    await page.wait_for_timeout(600)


async def run_search(page: Page, search: Search) -> list[dict[str, Any]]:
    before = await results_signature(page)
    await select_term(page, search.term_code)
    await select_subject(page, search.subject_code)
    await page.wait_for_function("""() => !document.querySelector("button.action")?.disabled""")
    await page.locator("button.action").click()
    await wait_for_results_change(page, before)
    return await parse_results(page, search)


async def results_signature(page: Page) -> str:
    return await page.locator("#results").evaluate(
        """(results) => {
            const items = Array.from(results.querySelectorAll("li"));
            const first = items[0]?.querySelector("header")?.textContent || "";
            const heading = items[0]?.querySelector(".w3-container > b")?.textContent || "";
            return `${results.textContent.length}:${items.length}:${first}:${heading}`;
        }"""
    )


async def wait_for_results_change(page: Page, before: str) -> None:
    try:
        await page.wait_for_function(
            """(beforeSignature) => {
                const results = document.querySelector("#results");
                const items = Array.from(results.querySelectorAll("li"));
                const first = items[0]?.querySelector("header")?.textContent || "";
                const heading = items[0]?.querySelector(".w3-container > b")?.textContent || "";
                const signature = `${results.textContent.length}:${items.length}:${first}:${heading}`;
                return signature !== beforeSignature;
            }""",
            arg=before,
            timeout=30_000,
        )
    except PlaywrightTimeoutError:
        # Some valid searches can return the same empty state. Give the Blazor
        # render cycle time to settle, then parse whatever is visible.
        pass
    await page.wait_for_timeout(900)


async def parse_results(page: Page, search: Search) -> list[dict[str, Any]]:
    rows = await page.locator("#results > ul > li").evaluate_all(
        """(items) => items.map((item) => {
            const clean = (value) => String(value || "").replace(/\\s+/g, " ").trim();
            const cleanMultiline = (value) => String(value || "")
                .replace(/\\r\\n/g, "\\n")
                .replace(/[ \\t]+\\n/g, "\\n")
                .replace(/\\n[ \\t]+/g, "\\n")
                .replace(/[ \\t]+/g, " ")
                .replace(/\\n{3,}/g, "\\n\\n")
                .trim();
            const camel = (value) => {
                const words = clean(value).match(/[a-zA-Z0-9]+/g) || [];
                return words.map((word, index) => {
                    const lower = word.toLowerCase();
                    return index === 0 ? lower : lower.charAt(0).toUpperCase() + lower.slice(1);
                }).join("");
            };
            const parseMeetingTimes = (cell) => {
                const nestedTable = cell.querySelector("table");
                if (!nestedTable) return [];
                return Array.from(nestedTable.rows).map((row) => {
                    const cells = Array.from(row.cells).map((nestedCell) => cleanMultiline(nestedCell.innerText || nestedCell.textContent));
                    return {
                        type: cells[0] || "",
                        days: cells[1] || "",
                        dateRange: cells[2] || "",
                        time: cells[3] || "",
                        room: cells[4] || "",
                        raw: cells.join(" | ")
                    };
                });
            };
            const tables = Array.from(item.querySelectorAll("table"));
            const tableToObject = (table) => {
                const headers = Array.from(table.querySelectorAll(":scope > thead th, :scope > tr:first-child th, :scope > tr:first-child td")).map((cell) => clean(cell.textContent));
                return {
                    headers,
                    rows: Array.from(table.rows).map((row) => Array.from(row.cells).map((cell) => cleanMultiline(cell.innerText || cell.textContent))),
                    html: table.outerHTML
                };
            };
            const renderedTables = tables.map(tableToObject);
            const sectionTable = tables.find((table) => {
                const headers = Array.from(table.querySelectorAll(":scope > thead th, :scope > tr:first-child th, :scope > tr:first-child td")).map((cell) => clean(cell.textContent).toLowerCase());
                return headers.includes("crn") || headers.includes("meeting times");
            });
            const headers = sectionTable
                ? Array.from(sectionTable.querySelectorAll(":scope > thead th, :scope > tr:first-child th, :scope > tr:first-child td")).map((cell) => clean(cell.textContent))
                : [];
            const bodyRows = sectionTable && sectionTable.tBodies.length ? Array.from(sectionTable.tBodies[0].rows) : [];
            const fallbackRows = sectionTable ? Array.from(sectionTable.rows).slice(1) : [];
            const tableRows = bodyRows.length ? bodyRows : fallbackRows;
            const sections = tableRows.map((row) => {
                const section = {};
                Array.from(row.cells).forEach((cell, index) => {
                    const key = camel(headers[index] || `column_${index + 1}`);
                    section[key] = cleanMultiline(cell.innerText || cell.textContent);
                    section[`${key}Html`] = cell.innerHTML;
                    if (key === "meetingTimes") {
                        section.meetingTimesParsed = parseMeetingTimes(cell);
                    }
                });
                return section;
            });
            return {
                title: clean(item.querySelector("header")?.textContent),
                level: clean(item.querySelector("footer")?.textContent),
                description: clean(item.querySelector("article")?.innerText),
                sectionHeading: Array.from(item.querySelectorAll(".w3-container > b")).map((node) => clean(node.innerText || node.textContent)).filter(Boolean).join(" "),
                sections,
                renderedTables,
                rawText: clean(item.innerText || item.textContent),
                rawHtml: item.innerHTML
            };
        })"""
    )

    scraped_at = datetime.now(timezone.utc).isoformat()
    output = []
    for row in rows:
        parsed_title = parse_course_title(row["title"])
        output.append(
            {
                "termCode": search.term_code,
                "term": search.term,
                "subjectCode": search.subject_code,
                "subject": search.subject,
                **parsed_title,
                **row,
                "sourceUrl": CATALOGUE_URL,
                "scrapedAt": scraped_at,
            }
        )
    return output


def flatten_for_csv(courses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for course in courses:
        base = {key: value for key, value in course.items() if key != "sections"}
        sections = course.get("sections") or []
        if not sections:
            rows.append(base)
            continue
        for section in sections:
            rows.append({**base, **section})
    return rows


def write_json(path: Path, courses: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(courses, indent=2, ensure_ascii=False), encoding="utf-8")


def write_csv(path: Path, courses: list[dict[str, Any]]) -> None:
    rows = flatten_for_csv(courses)
    keys: list[str] = []
    seen = set()
    for row in rows:
        for key in row:
            if key not in seen:
                keys.append(key)
                seen.add(key)

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=keys)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    key: json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value
                    for key, value in row.items()
                }
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scrape the UNBC course catalogue.")
    parser.add_argument("--term", action="append", help="Term code or label to scrape. Can be repeated. Defaults to all terms.")
    parser.add_argument("--subject", action="append", help="Subject code or label to scrape. Can be repeated. Defaults to all subjects.")
    parser.add_argument("--output", default="unbc-course-catalogue.json", help="Output path. Defaults to JSON.")
    parser.add_argument("--format", choices=["json", "csv"], default=None, help="Output format. Inferred from extension when omitted.")
    parser.add_argument("--delay", type=float, default=1.0, help="Delay in seconds between searches.")
    parser.add_argument("--headful", action="store_true", help="Show the browser window.")
    parser.add_argument("--limit", type=int, default=None, help="Limit number of term/subject searches for testing.")
    parser.add_argument("--checkpoint-every", type=int, default=10, help="Write partial output every N searches. Use 0 to disable.")
    parser.add_argument("--hydration-delay", type=float, default=3.0, help="Seconds to wait after Blazor loads before scraping.")
    parser.add_argument("--reuse-page", action="store_true", help="Reuse one Blazor page between searches instead of reloading before each search.")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    output_path = Path(args.output)
    output_format = args.format or ("csv" if output_path.suffix.lower() == ".csv" else "json")
    all_courses: list[dict[str, Any]] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=not args.headful)
        page = await browser.new_page()
        await wait_for_page(page, args.hydration_delay)

        terms = filter_options(await get_options(page, "#filter-term-code", skip_null=True), args.term, "term")
        subjects = filter_options(await get_options(page, "#filter-subject"), args.subject, "subject")
        searches = [Search(term.value, term.label, subject.value, subject.label) for term in terms for subject in subjects]
        if args.limit:
            searches = searches[: args.limit]

        print(f"Planned searches: {len(searches)} ({len(terms)} terms x {len(subjects)} subjects)")
        try:
            for index, search in enumerate(searches, start=1):
                if not args.reuse_page:
                    await wait_for_page(page, args.hydration_delay)

                print(f"[{index}/{len(searches)}] {search.term} / {search.subject_code} {search.subject}", flush=True)
                courses = await run_search(page, search)
                all_courses.extend(courses)
                print(f"  courses: {len(courses)} | total course records: {len(all_courses)}", flush=True)

                if args.checkpoint_every and index % args.checkpoint_every == 0:
                    write_output(output_path, output_format, all_courses)
                    print(f"  checkpoint written: {output_path}", flush=True)

                if args.delay > 0 and index < len(searches):
                    await page.wait_for_timeout(int(args.delay * 1000))
        except Exception:
            if all_courses:
                write_output(output_path, output_format, all_courses)
                print(f"Partial output written after failure: {output_path}", flush=True)
            await browser.close()
            raise

        await browser.close()

    write_output(output_path, output_format, all_courses)
    print(f"Done. Wrote {len(all_courses)} course records to {output_path}")


def write_output(path: Path, output_format: str, courses: list[dict[str, Any]]) -> None:
    if output_format == "csv":
        write_csv(path, courses)
    else:
        write_json(path, courses)


if __name__ == "__main__":
    asyncio.run(main())
