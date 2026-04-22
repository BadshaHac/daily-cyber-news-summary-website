#!/usr/bin/env python3
"""Small web app for generating date-wise cyber news summaries."""

from __future__ import annotations

import datetime as dt
import email.utils
import html
import base64
import sys
import traceback
import textwrap
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import Flask, Response, request, send_file


USER_AGENT = "daily-cyber-news-summary-web/1.0"
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
    output_path: Path | None
    markdown_content: str
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


HOST = "127.0.0.1"
PORT = 8000
OUTPUT_DIR = "output"
DEFAULT_TIMEZONE = "Asia/Calcutta"
DEFAULT_MAX_ITEMS = 10
APP_VERSION = "2026-04-22-ui-v5"
app = Flask(__name__)


def source_names() -> list[str]:
    return [source.name for source in FEED_SOURCES]


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
            f"Invalid timezone '{timezone_label}'. Try Asia/Kolkata, Asia/Calcutta, UTC, or +05:30."
        )


def collect_matching_items(
    target_date: dt.date,
    timezone: dt.tzinfo,
    max_items_per_source: int,
    selected_sources: Iterable[str] | None = None,
) -> tuple[list[NewsItem], list[str]]:
    matching_items: list[NewsItem] = []
    failed_sources: list[str] = []
    source_filter = set(selected_sources or source_names())

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
        matching_items.extend(date_filtered[:max_items_per_source])

    matching_items.sort(key=lambda item: (item.source, -item.published_at.timestamp()))
    return matching_items, failed_sources


def build_markdown(
    target_date: dt.date,
    timezone: dt.tzinfo,
    timezone_label: str,
    items: list[NewsItem],
    selected_sources: Iterable[str],
) -> str:
    selected_source_names = set(selected_sources)
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


def generate_summary(
    target_date: dt.date,
    timezone_label: str,
    output_dir: str = "output",
    max_items_per_source: int = 10,
    selected_sources: Iterable[str] | None = None,
) -> SummaryResult:
    timezone = resolve_timezone(timezone_label)
    selected_source_names = list(selected_sources or source_names())
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
    resolved_output_path: Path | None = None
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(markdown, encoding="utf-8")
        resolved_output_path = output_path.resolve()
    except OSError:
        # Serverless deployments such as Vercel expose a read-only filesystem.
        resolved_output_path = None
    return SummaryResult(
        target_date=target_date,
        timezone_label=timezone_label,
        output_path=resolved_output_path,
        markdown_content=markdown,
        items=items,
        generated_at_utc=dt.datetime.now(dt.timezone.utc),
        selected_sources=selected_source_names,
        failed_sources=failed_sources,
    )


def default_form_values() -> dict[str, object]:
    return {
        "date": dt.date.today().isoformat(),
        "timezone": DEFAULT_TIMEZONE,
        "max_items_per_source": DEFAULT_MAX_ITEMS,
        "selected_sources": source_names(),
    }


def format_source_options(selected_sources: list[str]) -> str:
    cards: list[str] = []
    for source in FEED_SOURCES:
        checked = "checked" if source.name in selected_sources else ""
        cards.append(
            f"""
            <label class="source-option">
              <input type="checkbox" name="selected_sources" value="{html.escape(source.name)}" {checked}>
              <span>
                <strong>{html.escape(source.name)}</strong>
                <small>{html.escape(source.url)}</small>
              </span>
            </label>
            """
        )
    return "".join(cards)


def page_shell(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>
    :root {{
      --bg: #f3f6fb;
      --bg-accent: #e8eef8;
      --surface: rgba(255, 255, 255, 0.94);
      --surface-strong: #ffffff;
      --surface-muted: #f8fafc;
      --ink: #0f172a;
      --muted: #5b677a;
      --accent: #2563eb;
      --accent-strong: #1d4ed8;
      --accent-soft: rgba(37, 99, 235, 0.08);
      --success-soft: rgba(15, 118, 110, 0.08);
      --line: rgba(15, 23, 42, 0.08);
      --line-strong: rgba(15, 23, 42, 0.14);
      --shadow: 0 18px 48px rgba(15, 23, 42, 0.08);
      --radius-xl: 24px;
      --radius-lg: 18px;
      --radius-md: 14px;
    }}
    * {{ box-sizing: border-box; }}
    html {{ scroll-behavior: smooth; }}
    body {{
      margin: 0;
      color: var(--ink);
      font-family: "Segoe UI", "Helvetica Neue", Arial, sans-serif;
      background:
        linear-gradient(180deg, #0f172a 0 120px, var(--bg) 120px 100%);
      min-height: 100vh;
    }}
    .page {{
      width: min(1180px, calc(100% - 32px));
      margin: 0 auto;
      padding: 24px 0 56px;
    }}
    .masthead {{
      color: #eff6ff;
      display: flex;
      justify-content: space-between;
      gap: 18px;
      align-items: center;
      padding: 8px 4px 26px;
    }}
    .brand {{
      display: grid;
      gap: 4px;
    }}
    .brand span {{
      font-size: 0.76rem;
      letter-spacing: 0.16em;
      text-transform: uppercase;
      color: rgba(239, 246, 255, 0.68);
      font-weight: 700;
    }}
    .brand strong {{
      font-size: 1.08rem;
      font-weight: 700;
      letter-spacing: -0.02em;
    }}
    .top-actions {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
    }}
    .ghost-link {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      padding: 10px 15px;
      border-radius: 999px;
      border: 1px solid rgba(239, 246, 255, 0.16);
      background: rgba(255, 255, 255, 0.06);
      color: #eff6ff;
      text-decoration: none;
      font-weight: 700;
      font-size: 0.92rem;
    }}
    .hero {{
      display: grid;
      grid-template-columns: minmax(0, 1.15fr) minmax(360px, 0.85fr);
      gap: 22px;
      align-items: start;
    }}
    .hero-card, .panel {{
      background: var(--surface);
      border: 1px solid rgba(255, 255, 255, 0.7);
      border-radius: var(--radius-xl);
      box-shadow: var(--shadow);
      backdrop-filter: blur(12px);
    }}
    .hero-card {{
      padding: 34px 34px 30px;
      overflow: hidden;
      position: relative;
    }}
    .hero-card::before {{
      content: "";
      position: absolute;
      inset: 0 0 auto 0;
      height: 4px;
      background: linear-gradient(90deg, var(--accent) 0%, #60a5fa 100%);
    }}
    .eyebrow {{
      display: inline-flex;
      padding: 7px 11px;
      border-radius: 999px;
      background: var(--accent-soft);
      color: var(--accent-strong);
      font-size: 0.74rem;
      letter-spacing: 0.1em;
      text-transform: uppercase;
      font-weight: 700;
    }}
    h1 {{
      margin: 18px 0 14px;
      font-size: clamp(2.3rem, 4.8vw, 4rem);
      line-height: 1;
      letter-spacing: -0.045em;
      max-width: 12ch;
    }}
    .lead {{
      margin: 0;
      color: var(--muted);
      line-height: 1.7;
      font-size: 1rem;
      max-width: 60ch;
    }}
    .hero-grid {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
      margin-top: 24px;
    }}
    .metric {{
      padding: 16px 16px 14px;
      border-radius: var(--radius-md);
      background: var(--surface-muted);
      border: 1px solid var(--line);
    }}
    .metric strong {{
      display: block;
      font-size: 1.2rem;
      margin-bottom: 6px;
      letter-spacing: -0.02em;
    }}
    .metric span {{
      color: var(--muted);
      font-size: 0.9rem;
    }}
    .panel {{
      padding: 24px;
    }}
    .panel-title {{
      margin: 0 0 8px;
      font-size: 1.2rem;
      letter-spacing: -0.02em;
    }}
    .panel-copy {{
      margin: 0 0 18px;
      color: var(--muted);
      line-height: 1.65;
      font-size: 0.96rem;
    }}
    form {{
      display: grid;
      gap: 18px;
    }}
    .field-grid {{
      display: grid;
      gap: 14px;
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }}
    label {{
      display: grid;
      gap: 8px;
      color: var(--muted);
      font-size: 0.95rem;
      font-weight: 600;
    }}
    input[type="date"],
    input[type="text"],
    input[type="number"],
    input[type="search"] {{
      width: 100%;
      border-radius: 12px;
      border: 1px solid var(--line);
      background: var(--surface-strong);
      color: var(--ink);
      font: inherit;
      padding: 13px 14px;
      outline: none;
      transition: border-color 0.2s ease, box-shadow 0.2s ease;
    }}
    input:focus {{
      border-color: rgba(37, 99, 235, 0.28);
      box-shadow: 0 0 0 4px rgba(37, 99, 235, 0.08);
    }}
    .quick-dates {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
    }}
    .mini-btn, .primary-btn, .secondary-btn {{
      appearance: none;
      border: 1px solid transparent;
      cursor: pointer;
      font: inherit;
      font-weight: 700;
      border-radius: 999px;
      transition: transform 0.18s ease, box-shadow 0.18s ease, background 0.18s ease;
    }}
    .mini-btn {{
      padding: 9px 13px;
      background: #eef4ff;
      color: var(--accent-strong);
      border-color: rgba(37, 99, 235, 0.12);
    }}
    .mini-btn:hover,
    .primary-btn:hover,
    .secondary-btn:hover {{
      transform: translateY(-1px);
    }}
    .source-picker {{
      display: grid;
      gap: 10px;
    }}
    .source-toolbar {{
      display: flex;
      justify-content: space-between;
      flex-wrap: wrap;
      gap: 10px;
      align-items: center;
    }}
    .source-toolbar p {{
      margin: 0;
      color: var(--muted);
      font-size: 0.92rem;
    }}
    .source-grid {{
      display: grid;
      gap: 10px;
    }}
    .source-option {{
      display: grid;
      grid-template-columns: auto 1fr;
      gap: 12px;
      align-items: start;
      padding: 14px;
      border-radius: var(--radius-md);
      border: 1px solid var(--line);
      background: var(--surface-muted);
      cursor: pointer;
    }}
    .source-option input {{
      margin-top: 4px;
      width: 18px;
      height: 18px;
    }}
    .source-option strong {{
      display: block;
      color: var(--ink);
      margin-bottom: 3px;
    }}
    .source-option small {{
      color: var(--muted);
      word-break: break-all;
    }}
    .action-row {{
      display: flex;
      gap: 12px;
      flex-wrap: wrap;
      align-items: center;
    }}
    .primary-btn {{
      padding: 12px 18px;
      color: white;
      background: linear-gradient(135deg, var(--accent) 0%, #3b82f6 100%);
      box-shadow: 0 12px 26px rgba(37, 99, 235, 0.18);
    }}
    .secondary-btn {{
      padding: 10px 14px;
      background: #f8fafc;
      color: var(--ink);
      border-color: var(--line);
    }}
    .feature-strip {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 18px;
    }}
    .chip {{
      padding: 8px 12px;
      border-radius: 999px;
      background: var(--success-soft);
      color: #0f766e;
      font-size: 0.86rem;
      font-weight: 700;
    }}
    .stack {{
      display: grid;
      gap: 22px;
      margin-top: 24px;
    }}
    .toolbar {{
      display: grid;
      gap: 16px;
      grid-template-columns: 1fr auto;
      align-items: end;
      margin-bottom: 18px;
    }}
    .meta-row {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin: 14px 0 0;
    }}
    .meta-pill {{
      padding: 9px 12px;
      border-radius: 999px;
      background: #eef4ff;
      color: var(--muted);
      font-size: 0.88rem;
      font-weight: 700;
    }}
    .stats-grid {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
      margin-top: 18px;
    }}
    .result-grid {{
      display: grid;
      gap: 18px;
    }}
    .source-card {{
      border-radius: 20px;
      background: rgba(255, 255, 255, 0.9);
      border: 1px solid var(--line);
      overflow: hidden;
    }}
    .source-head {{
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: center;
      padding: 18px 20px;
      border-bottom: 1px solid var(--line);
      background: linear-gradient(180deg, rgba(37, 99, 235, 0.04), rgba(255, 255, 255, 0));
    }}
    .source-head h3 {{
      margin: 0;
      font-size: 1.2rem;
    }}
    .source-count {{
      color: var(--accent-strong);
      font-weight: 800;
      font-size: 0.88rem;
    }}
    .item-list {{
      display: grid;
      gap: 14px;
      padding: 18px;
    }}
    .item {{
      padding: 18px;
      border-radius: 18px;
      background: #fbfdff;
      border: 1px solid rgba(16, 33, 50, 0.07);
    }}
    .item a {{
      color: var(--ink);
      text-decoration-thickness: 2px;
      text-underline-offset: 3px;
      font-size: 1rem;
      font-weight: 700;
    }}
    .item p {{
      margin: 10px 0 0;
      line-height: 1.65;
      color: var(--muted);
    }}
    .item-meta {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 12px;
    }}
    .item-meta span {{
      display: inline-flex;
      padding: 7px 10px;
      border-radius: 999px;
      background: #eff6ff;
      color: var(--accent-strong);
      font-size: 0.82rem;
      font-weight: 700;
    }}
    .callout {{
      padding: 16px 18px;
      border-radius: 18px;
      border: 1px solid rgba(37, 99, 235, 0.14);
      background: rgba(37, 99, 235, 0.06);
      color: #1e40af;
      line-height: 1.6;
    }}
    .empty {{
      padding: 24px;
      border-radius: 20px;
      background: rgba(16, 33, 50, 0.03);
      border: 1px dashed var(--line-strong);
      color: var(--muted);
    }}
    .footer-link {{
      display: inline-flex;
      align-items: center;
      color: var(--accent-strong);
      font-weight: 800;
      text-decoration-thickness: 2px;
      text-underline-offset: 3px;
    }}
    .error {{
      padding: 16px 18px;
      border-radius: 18px;
      background: rgba(220, 38, 38, 0.07);
      color: #b91c1c;
      border: 1px solid rgba(220, 38, 38, 0.14);
      line-height: 1.6;
    }}
    .muted-note {{
      color: var(--muted);
      font-size: 0.92rem;
      line-height: 1.6;
    }}
    [data-hidden="true"] {{
      display: none;
    }}
    @media (max-width: 960px) {{
      .hero,
      .toolbar,
      .field-grid,
      .stats-grid {{
        grid-template-columns: 1fr;
      }}
      .hero-grid {{
        grid-template-columns: 1fr;
      }}
      .source-head {{
        align-items: start;
        flex-direction: column;
      }}
      .top-actions {{
        justify-content: flex-start;
      }}
    }}
  </style>
</head>
<body>
  <main class="page">
    {body}
  </main>
  <script>
    const dateInput = document.querySelector('[name="date"]');
    document.querySelectorAll('[data-set-date]').forEach((button) => {{
      button.addEventListener('click', () => {{
        if (!dateInput) return;
        const shift = Number(button.getAttribute('data-set-date'));
        const base = new Date();
        base.setDate(base.getDate() + shift);
        dateInput.value = base.toISOString().slice(0, 10);
      }});
    }});

    const sourceCheckboxes = Array.from(document.querySelectorAll('input[name="selected_sources"]'));
    document.querySelector('[data-select-all]')?.addEventListener('click', () => {{
      sourceCheckboxes.forEach((box) => box.checked = true);
    }});
    document.querySelector('[data-clear-all]')?.addEventListener('click', () => {{
      sourceCheckboxes.forEach((box) => box.checked = false);
    }});

    const form = document.querySelector('form');
    const submitButton = document.querySelector('[data-submit]');
    form?.addEventListener('submit', () => {{
      if (submitButton) {{
        submitButton.textContent = 'Generating...';
        submitButton.disabled = true;
      }}
    }});

    const searchInput = document.querySelector('[data-search]');
    const items = Array.from(document.querySelectorAll('[data-item]'));
    const sections = Array.from(document.querySelectorAll('[data-source-card]'));
    searchInput?.addEventListener('input', () => {{
      const term = searchInput.value.trim().toLowerCase();
      items.forEach((item) => {{
        const haystack = item.getAttribute('data-search-text') || '';
        item.setAttribute('data-hidden', term && !haystack.includes(term) ? 'true' : 'false');
      }});
      sections.forEach((section) => {{
        const visibleCount = section.querySelectorAll('[data-item][data-hidden="false"], [data-item]:not([data-hidden])').length;
        section.setAttribute('data-hidden', visibleCount === 0 ? 'true' : 'false');
      }});
    }});
  </script>
</body>
</html>
"""


def render_form(form_values: dict[str, object] | None = None, error_message: str = "") -> str:
    values = default_form_values()
    if form_values:
        values.update(form_values)
    selected_sources = list(values["selected_sources"])
    error_html = f'<div class="error">{html.escape(error_message)}</div>' if error_message else ""

    body = f"""
    <header class="masthead">
      <div class="brand">
        <span>Cyber Intelligence Workspace</span>
        <strong>Cyber News Summary Maker</strong>
      </div>
      <div class="top-actions">
        <a class="ghost-link" href="#generator">Create Summary</a>
      </div>
    </header>

    <section class="hero">
      <section class="hero-card">
        <span class="eyebrow">Daily Briefing</span>
        <h1>Professional cyber news summaries by date.</h1>
        <p class="lead">
          Generate a date-based cybersecurity briefing with source controls, timezone-aware filtering,
          and a clean reading experience designed for quick review.
        </p>
        <div class="hero-grid">
          <div class="metric"><strong>{len(FEED_SOURCES)}</strong><span>Security sources</span></div>
          <div class="metric"><strong>Custom filters</strong><span>Select only the feeds you need</span></div>
          <div class="metric"><strong>Saved output</strong><span>Markdown export generated automatically</span></div>
        </div>
        <div class="feature-strip">
          <span class="chip">Quick date presets</span>
          <span class="chip">Article search</span>
          <span class="chip">Feed status visibility</span>
        </div>
      </section>

      <aside class="panel" id="generator">
        <h2 class="panel-title">Create Summary</h2>
        <p class="panel-copy">
          Choose the date, confirm the timezone, and select the feeds that should appear in the report.
        </p>
        <form method="post" action="/generate">
          <div class="field-grid">
            <label>
              Date
              <input type="date" name="date" value="{html.escape(str(values['date']))}" required>
            </label>
            <label>
              Timezone
              <input type="text" name="timezone" value="{html.escape(str(values['timezone']))}" placeholder="Asia/Calcutta" required>
            </label>
          </div>

          <div class="quick-dates">
            <button class="mini-btn" type="button" data-set-date="0">Today</button>
            <button class="mini-btn" type="button" data-set-date="-1">Yesterday</button>
            <button class="mini-btn" type="button" data-set-date="-7">7 Days Back</button>
          </div>

          <label>
            Max items per source
            <input type="number" name="max_items_per_source" min="1" max="25" value="{html.escape(str(values['max_items_per_source']))}" required>
          </label>

          <section class="source-picker">
            <div class="source-toolbar">
              <p>Select one or more sources for this summary.</p>
              <div class="action-row">
                <button class="secondary-btn" type="button" data-select-all>Select all</button>
                <button class="secondary-btn" type="button" data-clear-all>Clear all</button>
              </div>
            </div>
            <div class="source-grid">
              {format_source_options(selected_sources)}
            </div>
          </section>

          <div class="action-row">
            <button class="primary-btn" type="submit" data-submit>Generate summary</button>
          </div>
        </form>
        <p class="muted-note">
          Each run also saves a dated markdown file for reuse outside the browser.
        </p>
        {error_html}
      </aside>
    </section>
    """
    return page_shell("Cyber News Summary Maker", body)


def render_result(result: SummaryResult, form_values: dict[str, object]) -> str:
    grouped: dict[str, list] = defaultdict(list)
    selected_timezone = resolve_timezone(result.timezone_label)
    markdown_download = (
        "data:text/markdown;base64,"
        + base64.b64encode(result.markdown_content.encode("utf-8")).decode("ascii")
    )
    markdown_filename = f"cyber-news-summary-{result.target_date.isoformat()}.md"
    for item in result.items:
      grouped[item.source].append(item)

    cards: list[str] = []
    if not result.items:
        cards.append(
            """
            <div class="empty">
              No matching stories were found for that exact date and filter set. Try another date,
              use more sources, or lower the specificity of the timezone.
            </div>
            """
        )
    else:
        for source_name, items in grouped.items():
            entries: list[str] = []
            for item in items:
                local_time = item.published_at.astimezone(selected_timezone).strftime(
                    "%Y-%m-%d %H:%M %Z"
                )
                search_text = " ".join(
                    [source_name, item.title, item.description, local_time]
                ).lower()
                summary = html.escape(item.description or "No description available.")
                entries.append(
                    f"""
                    <article class="item" data-item data-search-text="{html.escape(search_text)}">
                      <a href="{html.escape(item.link)}" target="_blank" rel="noreferrer">{html.escape(item.title)}</a>
                      <div class="item-meta">
                        <span>{html.escape(source_name)}</span>
                        <span>{html.escape(local_time)}</span>
                      </div>
                      <p>{summary}</p>
                    </article>
                    """
                )
            cards.append(
                f"""
                <section class="source-card" data-source-card>
                  <div class="source-head">
                    <h3>{html.escape(source_name)}</h3>
                    <span class="source-count">{len(items)} articles</span>
                  </div>
                  <div class="item-list">
                    {''.join(entries)}
                  </div>
                </section>
                """
            )

    warning_html = ""
    if result.failed_sources:
        warning_html = (
            '<div class="callout">Some feeds could not be read during this run: '
            + ", ".join(html.escape(name) for name in result.failed_sources)
            + ".</div>"
        )

    body = f"""
    <header class="masthead">
      <div class="brand">
        <span>Cyber Intelligence Workspace</span>
        <strong>Cyber News Summary Maker</strong>
      </div>
      <div class="top-actions">
        <a class="ghost-link" href="/">New Summary</a>
        <a class="ghost-link" href="{markdown_download}" download="{html.escape(markdown_filename)}">Download Markdown</a>
      </div>
    </header>

    <section class="hero">
      <section class="hero-card">
        <span class="eyebrow">Summary Ready</span>
        <h1>{html.escape(result.target_date.isoformat())}</h1>
        <p class="lead">
          The report has been grouped by source and saved locally. Use the search box below to narrow visible stories instantly.
        </p>
        <div class="meta-row">
          <span class="meta-pill">Timezone: {html.escape(result.timezone_label)}</span>
          <span class="meta-pill">Sources: {len(result.selected_sources)}</span>
          <span class="meta-pill">Articles: {len(result.items)}</span>
          <span class="meta-pill">Generated: {html.escape(result.generated_at_utc.strftime('%Y-%m-%d %H:%M UTC'))}</span>
        </div>
        <div class="stats-grid">
          <div class="metric"><strong>{len(result.items)}</strong><span>Total articles</span></div>
          <div class="metric"><strong>{len(grouped)}</strong><span>Sources with matches</span></div>
          <div class="metric"><strong>{len(result.failed_sources)}</strong><span>Feed issues</span></div>
          <div class="metric"><strong>{html.escape(markdown_filename)}</strong><span>{"Saved file" if result.output_path else "Download file"}</span></div>
        </div>
      </section>

      <aside class="panel">
        <h2 class="panel-title">Refine Next Run</h2>
        <p class="panel-copy">
          Update the date, timezone, or selected feeds and regenerate without starting over.
        </p>
        <form method="post" action="/generate">
          <div class="field-grid">
            <label>
              Date
              <input type="date" name="date" value="{html.escape(str(form_values['date']))}" required>
            </label>
            <label>
              Timezone
              <input type="text" name="timezone" value="{html.escape(str(form_values['timezone']))}" required>
            </label>
          </div>
          <label>
            Max items per source
            <input type="number" name="max_items_per_source" min="1" max="25" value="{html.escape(str(form_values['max_items_per_source']))}" required>
          </label>
          <section class="source-picker">
            <div class="source-toolbar">
              <p>Sources to include in the next run.</p>
              <div class="action-row">
                <button class="secondary-btn" type="button" data-select-all>Select all</button>
                <button class="secondary-btn" type="button" data-clear-all>Clear all</button>
              </div>
            </div>
            <div class="source-grid">
              {format_source_options(list(form_values['selected_sources']))}
            </div>
          </section>
          <div class="action-row">
            <button class="primary-btn" type="submit" data-submit>Regenerate</button>
            <a class="footer-link" href="/">Back to form</a>
          </div>
        </form>
        <p class="muted-note">
          {"Saved locally at " + html.escape(str(result.output_path)) if result.output_path else "Running on a hosted read-only environment, so the markdown is provided as a direct download instead of being saved on disk."}
        </p>
      </aside>
    </section>

    <section class="stack">
      {warning_html}
      <section class="panel">
        <div class="toolbar">
          <div>
            <h2 class="panel-title">Coverage</h2>
            <p class="panel-copy">Search article titles and summaries without reloading the page.</p>
          </div>
          <label>
            Search This Report
            <input type="search" placeholder="Search malware, Microsoft, CVE..." data-search>
          </label>
        </div>
        <div class="result-grid">
          {''.join(cards)}
        </div>
      </section>
    </section>
    """
    return page_shell(f"Cyber Summary - {result.target_date.isoformat()}", body)


def html_response(document: str, status_code: int = 200) -> Response:
    response = Response(document, status=status_code, mimetype="text/html")
    response.headers["X-App-Version"] = APP_VERSION
    return response


@app.get("/")
def home() -> Response:
    return html_response(render_form())


@app.post("/generate")
def generate() -> Response:
    selected_sources = request.form.getlist("selected_sources") or source_names()
    form_values = {
        "date": request.form.get("date", dt.date.today().isoformat()),
        "timezone": (request.form.get("timezone", DEFAULT_TIMEZONE) or DEFAULT_TIMEZONE).strip(),
        "max_items_per_source": request.form.get(
            "max_items_per_source", str(DEFAULT_MAX_ITEMS)
        ),
        "selected_sources": selected_sources,
    }

    try:
        target_date = dt.date.fromisoformat(str(form_values["date"]))
        max_items = max(1, min(25, int(str(form_values["max_items_per_source"]))))
        if not selected_sources:
            raise ValueError("Select at least one source before generating the report.")
        result = generate_summary(
            target_date=target_date,
            timezone_label=str(form_values["timezone"]),
            output_dir=OUTPUT_DIR,
            max_items_per_source=max_items,
            selected_sources=selected_sources,
        )
    except Exception as exc:
        return html_response(
            render_form(form_values=form_values, error_message=str(exc)),
            status_code=400,
        )

    return html_response(render_result(result, form_values))


@app.get("/output/<path:filename>")
def serve_output(filename: str):
    requested = (Path(OUTPUT_DIR) / filename).resolve()
    output_root = Path(OUTPUT_DIR).resolve()
    if output_root not in requested.parents and requested != output_root:
        return html_response(render_form(error_message="Invalid file path."), status_code=403)
    if not requested.exists():
        return html_response(render_form(error_message="File not found."), status_code=404)
    return send_file(requested, mimetype="text/markdown")


def run_server() -> None:
    print(f"Serving cyber news summary website at http://{HOST}:{PORT}")
    app.run(host=HOST, port=PORT, debug=False)


if __name__ == "__main__":
    try:
        run_server()
    except Exception:
        traceback.print_exc()
        raise
