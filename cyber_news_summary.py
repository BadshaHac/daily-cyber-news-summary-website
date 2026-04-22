#!/usr/bin/env python3
"""Generate a date-wise cyber news summary from public RSS feeds."""

from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import sys
import textwrap
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


USER_AGENT = "daily-cyber-news-summary/1.0"
DEFAULT_TIMEOUT_SECONDS = 20


@dataclass(frozen=True)
class FeedSource:
    name: str
    url: str


@dataclass(frozen=True)
class NewsItem:
    source: str
    title: str
    link: str
    published_at: dt.datetime
    description: str


@dataclass(frozen=True)
class SummaryResult:
    target_date: dt.date
    timezone_label: str
    output_path: Path
    items: list[NewsItem]
    generated_at_utc: dt.datetime
    selected_sources: list[str]
    failed_sources: list[str]


FEED_SOURCES: tuple[FeedSource, ...] = (
    FeedSource("The Hacker News", "https://feeds.feedburner.com/TheHackersNews"),
    FeedSource("SecurityWeek", "https://feeds.feedburner.com/securityweek"),
    FeedSource("BleepingComputer", "https://www.bleepingcomputer.com/feed/"),
    FeedSource("CISA Alerts", "https://www.cisa.gov/cybersecurity-advisories/all.xml"),
)

TIMEZONE_FALLBACKS: dict[str, dt.tzinfo] = {
    "utc": dt.timezone.utc,
    "z": dt.timezone.utc,
    "asia/calcutta": dt.timezone(dt.timedelta(hours=5, minutes=30), name="IST"),
    "asia/kolkata": dt.timezone(dt.timedelta(hours=5, minutes=30), name="IST"),
    "ist": dt.timezone(dt.timedelta(hours=5, minutes=30), name="IST"),
}


def parse_args() -> argparse.Namespace:
    local_zone = dt.datetime.now().astimezone().tzinfo
    parser = argparse.ArgumentParser(
        description="Generate a daily cyber news summary for a specific date."
    )
    parser.add_argument(
        "--date",
        default=dt.date.today().isoformat(),
        help="Target date in YYYY-MM-DD format. Defaults to today.",
    )
    parser.add_argument(
        "--output-dir",
        default="output",
        help="Directory where the markdown summary will be written.",
    )
    parser.add_argument(
        "--max-items-per-source",
        type=int,
        default=10,
        help="Maximum matching items to keep from each source.",
    )
    parser.add_argument(
        "--timezone",
        default=str(local_zone) if local_zone is not None else "UTC",
        help="Timezone used to decide which calendar date an article belongs to.",
    )
    return parser.parse_args()


def fetch_xml(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=DEFAULT_TIMEOUT_SECONDS) as response:
        return response.read()


def get_child_text(element: ET.Element, tag_names: Iterable[str]) -> str:
    for tag_name in tag_names:
        child = element.find(tag_name)
        if child is not None and child.text:
            return child.text.strip()
    return ""


def parse_pub_date(raw_value: str) -> dt.datetime | None:
    if not raw_value:
        return None

    parsed = email.utils.parsedate_to_datetime(raw_value)
    if parsed is not None:
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)

    iso_candidate = raw_value.replace("Z", "+00:00")
    try:
        parsed_iso = dt.datetime.fromisoformat(iso_candidate)
    except ValueError:
        return None
    if parsed_iso.tzinfo is None:
        return parsed_iso.replace(tzinfo=dt.timezone.utc)
    return parsed_iso.astimezone(dt.timezone.utc)


def strip_html(raw_text: str) -> str:
    if not raw_text:
        return ""
    wrapped = f"<root>{raw_text}</root>"
    try:
        root = ET.fromstring(wrapped)
        text = "".join(root.itertext())
    except ET.ParseError:
        text = raw_text
    return " ".join(text.split())


def parse_feed(source: FeedSource) -> list[NewsItem]:
    xml_payload = fetch_xml(source.url)
    root = ET.fromstring(xml_payload)
    items: list[NewsItem] = []

    rss_items = root.findall(".//item")
    atom_entries = root.findall(".//{http://www.w3.org/2005/Atom}entry")

    for entry in rss_items:
        title = get_child_text(entry, ("title",))
        link = get_child_text(entry, ("link",))
        description = strip_html(get_child_text(entry, ("description", "summary")))
        pub_raw = get_child_text(entry, ("pubDate", "published", "updated"))
        published_at = parse_pub_date(pub_raw)
        if title and link and published_at:
            items.append(
                NewsItem(
                    source=source.name,
                    title=title,
                    link=link,
                    published_at=published_at,
                    description=description,
                )
            )

    for entry in atom_entries:
        title = get_child_text(entry, ("{http://www.w3.org/2005/Atom}title",))
        description = strip_html(
            get_child_text(
                entry,
                (
                    "{http://www.w3.org/2005/Atom}summary",
                    "{http://www.w3.org/2005/Atom}content",
                ),
            )
        )
        pub_raw = get_child_text(
            entry,
            (
                "{http://www.w3.org/2005/Atom}published",
                "{http://www.w3.org/2005/Atom}updated",
            ),
        )
        link = ""
        for candidate in entry.findall("{http://www.w3.org/2005/Atom}link"):
            href = candidate.attrib.get("href", "").strip()
            rel = candidate.attrib.get("rel", "alternate")
            if href and rel == "alternate":
                link = href
                break
        published_at = parse_pub_date(pub_raw)
        if title and link and published_at:
            items.append(
                NewsItem(
                    source=source.name,
                    title=title,
                    link=link,
                    published_at=published_at,
                    description=description,
                )
            )

    return items


def build_markdown(
    target_date: dt.date,
    timezone: dt.tzinfo,
    timezone_label: str,
    items: list[NewsItem],
    selected_sources: Iterable[str] | None = None,
) -> str:
    selected_source_names = set(selected_sources or [source.name for source in FEED_SOURCES])
    lines = [
        f"# Cyber News Summary - {target_date.isoformat()}",
        "",
        f"Generated on {dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        f"Date filter timezone: {timezone_label}",
        "",
    ]

    if not items:
        lines.extend(
            [
                "No matching articles were found for this date in the configured feeds.",
                "",
                "Checked sources:",
            ]
        )
        for source in FEED_SOURCES:
            if source.name in selected_source_names:
                lines.append(f"- {source.name}: {source.url}")
        lines.append("")
        return "\n".join(lines)

    current_source = None
    for item in items:
        if item.source != current_source:
            current_source = item.source
            lines.extend([f"## {item.source}", ""])
        lines.append(f"- [{item.title}]({item.link})")
        published_local = item.published_at.astimezone(timezone)
        lines.append(
            "  - Published: "
            f"{published_local.strftime('%Y-%m-%d %H:%M %Z')} "
            f"({item.published_at.strftime('%Y-%m-%d %H:%M UTC')})"
        )
        if item.description:
            summary = textwrap.shorten(item.description, width=220, placeholder="...")
            lines.append(f"  - Summary: {summary}")
        lines.append("")

    return "\n".join(lines)


def parse_utc_offset(timezone_label: str) -> dt.tzinfo | None:
    cleaned = timezone_label.strip().upper()
    if len(cleaned) != 6 or cleaned[0] not in "+-" or cleaned[3] != ":":
        return None
    try:
        hours = int(cleaned[1:3])
        minutes = int(cleaned[4:6])
    except ValueError:
        return None
    if hours > 23 or minutes > 59:
        return None
    delta = dt.timedelta(hours=hours, minutes=minutes)
    if cleaned[0] == "-":
        delta = -delta
    return dt.timezone(delta, name=cleaned)


def resolve_timezone(timezone_label: str) -> dt.tzinfo:
    normalized = timezone_label.strip()
    if not normalized:
        raise ValueError("Timezone is required.")

    try:
        return ZoneInfo(normalized)
    except ZoneInfoNotFoundError:
        fallback = TIMEZONE_FALLBACKS.get(normalized.lower())
        if fallback is not None:
            return fallback
        offset_timezone = parse_utc_offset(normalized)
        if offset_timezone is not None:
            return offset_timezone
        raise ValueError(
            f"Invalid timezone '{timezone_label}'. Try Asia/Calcutta, Asia/Kolkata, UTC, or an offset like +05:30."
        )


def collect_matching_items(
    target_date: dt.date,
    timezone: dt.tzinfo,
    max_items_per_source: int,
    selected_sources: Iterable[str] | None = None,
) -> tuple[list[NewsItem], list[str]]:
    matching_items: list[NewsItem] = []
    failed_sources: list[str] = []
    source_filter = set(selected_sources or [source.name for source in FEED_SOURCES])

    for source in FEED_SOURCES:
        if source.name not in source_filter:
            continue
        try:
            items = parse_feed(source)
        except (urllib.error.URLError, TimeoutError, ET.ParseError):
            failed_sources.append(source.name)
            continue

        date_filtered = [
            item
            for item in items
            if item.published_at.astimezone(timezone).date() == target_date
        ]
        date_filtered.sort(key=lambda item: item.published_at, reverse=True)
        matching_items.extend(date_filtered[: max_items_per_source])

    matching_items.sort(key=lambda item: (item.source, -item.published_at.timestamp()))
    return matching_items, failed_sources


def generate_summary(
    target_date: dt.date,
    timezone_label: str,
    output_dir: str = "output",
    max_items_per_source: int = 10,
    selected_sources: Iterable[str] | None = None,
) -> SummaryResult:
    timezone = resolve_timezone(timezone_label)
    selected_source_names = list(selected_sources or [source.name for source in FEED_SOURCES])
    items, failed_sources = collect_matching_items(
        target_date,
        timezone,
        max_items_per_source,
        selected_sources=selected_source_names,
    )
    markdown = build_markdown(
        target_date,
        timezone,
        timezone_label,
        items,
        selected_sources=selected_source_names,
    )
    output_path = Path(output_dir) / f"cyber-news-summary-{target_date.isoformat()}.md"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(markdown, encoding="utf-8")
    return SummaryResult(
        target_date=target_date,
        timezone_label=timezone_label,
        output_path=output_path.resolve(),
        items=items,
        generated_at_utc=dt.datetime.now(dt.timezone.utc),
        selected_sources=selected_source_names,
        failed_sources=failed_sources,
    )


def main() -> int:
    args = parse_args()

    try:
        target_date = dt.date.fromisoformat(args.date)
    except ValueError:
        print("Invalid --date value. Use YYYY-MM-DD.", file=sys.stderr)
        return 2

    try:
        result = generate_summary(
            target_date=target_date,
            timezone_label=args.timezone,
            output_dir=args.output_dir,
            max_items_per_source=args.max_items_per_source,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    print(result.output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
