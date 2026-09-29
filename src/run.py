from __future__ import annotations

import csv
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

HH_RSS_URL = "https://hh.ru/search/vacancy/rss"
USER_AGENT = "JobRadar/0.2 (github.com/vergulesov/JobRadar)"
PER_PAGE = 100


@dataclass
class Vacancy:
    vacancy_id: str
    title: str
    company: str | None
    salary: str | None
    location: str | None
    published_at: str | None
    url: str
    summary: str
    matched_queries: list[str]
    source: str = "hh_api"
    hard_filter_status: str = "keep"
    hard_filter_reason: str | None = None


def load_json(path: Path):
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def strip_html(text: str | None) -> str:
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def format_salary(salary: dict | None) -> str | None:
    if not salary:
        return None
    parts = []
    if salary.get("from") is not None:
        parts.append(f"от {salary['from']}")
    if salary.get("to") is not None:
        parts.append(f"до {salary['to']}")
    if salary.get("currency"):
        parts.append(str(salary["currency"]))
    if salary.get("gross") is True:
        parts.append("gross")
    elif salary.get("gross") is False:
        parts.append("net")
    return " ".join(parts) or None


def fetch_rss(query: str) -> bytes:
    params = {
        "text": query,
        "order_by": "publication_time",
    }
    url = HH_RSS_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read()


def text_of(node, name: str) -> str | None:
    child = node.find(name)
    if child is None or child.text is None:
        return None
    return strip_html(child.text)


def extract_vacancy_id(url: str) -> str:
    m = re.search(r"/vacancy/(\\d+)", url)
    return m.group(1) if m else url


def parse_rss(payload: bytes, query: str) -> list[Vacancy]:
    import xml.etree.ElementTree as ET
    from email.utils import parsedate_to_datetime

    root = ET.fromstring(payload)
    out = []
    for item in root.findall(".//item"):
        title = text_of(item, "title") or ""
        url = text_of(item, "link") or text_of(item, "guid") or ""
        description = text_of(item, "description") or ""
        pub_raw = text_of(item, "pubDate")
        published_at = None
        if pub_raw:
            try:
                published_at = parsedate_to_datetime(pub_raw).astimezone(timezone.utc).isoformat()
            except Exception:
                published_at = pub_raw
        if url:
            out.append(Vacancy(
                vacancy_id=extract_vacancy_id(url),
                title=title,
                company=None,
                salary=None,
                location=None,
                published_at=published_at,
                url=url,
                summary=description,
                matched_queries=[query],
                source="hh_rss",
            ))
    return out


def is_fresh(published_at: str | None, cutoff: datetime) -> bool:
    if not published_at:
        return True
    try:
        dt = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt >= cutoff
    except Exception:
        return True

def main() -> int:
    query_cfg = load_json(CONFIG_DIR / "queries.json")
    filter_cfg = load_json(CONFIG_DIR / "filters.json")
    fresh_days = int(query_cfg.get("fresh_days", 7))
    cutoff = datetime.now(timezone.utc) - timedelta(days=fresh_days)

    collected = []
    errors = []
    saturated_queries = []

    for idx, query in enumerate(query_cfg["queries"], 1):
        print(f"[{idx}/{len(query_cfg['queries'])}] {query}")
        try:
            rows = [v for v in parse_rss(fetch_rss(query), query) if is_fresh(v.published_at, cutoff)]
            collected.extend(rows)
            saturated = len(rows) >= 20
            if saturated:
                saturated_queries.append(query)
            marker = " [SATURATED]" if saturated else ""
            print(f"  + {len(rows)} fresh RSS items{marker}")
        except Exception as e:
            print(f"  ! {type(e).__name__}: {e}", file=sys.stderr)
            errors.append({"query": query, "error": f"{type(e).__name__}: {e}"})

    unique = deduplicate(collected)
    filtered = [hard_filter(v, filter_cfg) for v in unique]
    target = [v for v in filtered if v.hard_filter_status == "keep"]
    rejected = [v for v in filtered if v.hard_filter_status == "reject"]

    write_json(DATA_DIR / "raw.json", unique)
    write_json(DATA_DIR / "target.json", target)
    write_csv(DATA_DIR / "target.csv", target)
    write_json(DATA_DIR / "rejected.json", rejected)

    meta = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "collector": "hh_rss_broad_sweep",
        "fresh_days": fresh_days,
        "queries": len(query_cfg["queries"]),
        "rss_items": len(collected),
        "saturated_queries": saturated_queries,
        "unique": len(unique),
        "target": len(target),
        "rejected": len(rejected),
        "errors": errors,
        "html_downloads": 0,
        "vacancy_detail_downloads": 0,
        "ai_calls": 0,
    }
    with (DATA_DIR / "run_meta.json").open("w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    print("\\nDONE")
    print(json.dumps(meta, ensure_ascii=False, indent=2))
    if saturated_queries:
        print(f"\\nWARNING: {len(saturated_queries)} RSS queries hit the 20-item ceiling.")
        print("Coverage is expanded by overlapping narrow queries; saturated queries remain visible in run_meta.json.")
    return 0 if not errors else 2



if __name__ == "__main__":
    raise SystemExit(main())
