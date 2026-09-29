from __future__ import annotations

import csv
import json
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

HH_RSS_URL = "https://hh.ru/search/vacancy/rss"
USER_AGENT = "JobRadar/0.1 (+https://github.com/vergulesov/JobRadar)"


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
    source: str = "hh_rss"
    hard_filter_status: str = "keep"
    hard_filter_reason: str | None = None


def load_json(path: Path):
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def strip_html(text: str | None) -> str:
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def extract_vacancy_id(url: str) -> str:
    m = re.search(r"/vacancy/(\d+)", url)
    return m.group(1) if m else url


def fetch_rss(query: str) -> bytes:
    params = {
        "text": query,
        "order_by": "publication_time",
        "items_on_page": 100,
    }
    url = HH_RSS_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read()


def text_of(node, name: str) -> str | None:
    child = node.find(name)
    if child is None or child.text is None:
        return None
    return strip_html(child.text)


def parse_items(payload: bytes, query: str) -> list[Vacancy]:
    root = ET.fromstring(payload)
    out: list[Vacancy] = []
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

        # HH RSS commonly embeds company/region/salary in description. Keep parsing deliberately loose.
        company = None
        location = None
        salary = None
        m = re.search(r"(?:Вакансия компании|Компания):\s*([^.;]+)", description, re.I)
        if m:
            company = m.group(1).strip()
        m = re.search(r"(?:Регион|Город):\s*([^.;]+)", description, re.I)
        if m:
            location = m.group(1).strip()
        m = re.search(r"(?:уровень месячного дохода|зарплата):\s*([^.;]+)", description, re.I)
        if m:
            salary = m.group(1).strip()

        if not url:
            continue
        out.append(Vacancy(
            vacancy_id=extract_vacancy_id(url),
            title=title,
            company=company,
            salary=salary,
            location=location,
            published_at=published_at,
            url=url,
            summary=description,
            matched_queries=[query],
        ))
    return out


def within_days(v: Vacancy, days: int) -> bool:
    if not v.published_at:
        return True
    try:
        dt = datetime.fromisoformat(v.published_at.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt >= datetime.now(timezone.utc) - timedelta(days=days)
    except Exception:
        return True


def deduplicate(items: list[Vacancy]) -> list[Vacancy]:
    by_id: dict[str, Vacancy] = {}
    for v in items:
        if v.vacancy_id not in by_id:
            by_id[v.vacancy_id] = v
        else:
            existing = by_id[v.vacancy_id]
            for q in v.matched_queries:
                if q not in existing.matched_queries:
                    existing.matched_queries.append(q)
            # Prefer any non-empty metadata we saw across RSS queries.
            existing.company = existing.company or v.company
            existing.salary = existing.salary or v.salary
            existing.location = existing.location or v.location
            if len(v.summary) > len(existing.summary):
                existing.summary = v.summary
    return list(by_id.values())


def hard_filter(v: Vacancy, cfg: dict) -> Vacancy:
    title = v.title.lower()
    haystack = " ".join([
        v.title or "", v.company or "", v.salary or "", v.location or "", v.summary or ""
    ]).lower()

    for token in cfg.get("reject_title_contains", []):
        if token.lower() in title:
            v.hard_filter_status = "reject"
            v.hard_filter_reason = f"title contains: {token}"
            return v

    for token in cfg.get("reject_text_contains", []):
        if token.lower() in haystack:
            v.hard_filter_status = "reject"
            v.hard_filter_reason = f"text contains: {token}"
            return v

    return v


def write_json(path: Path, rows: list[Vacancy]):
    with path.open("w", encoding="utf-8") as f:
        json.dump([asdict(v) for v in rows], f, ensure_ascii=False, indent=2)


def write_csv(path: Path, rows: list[Vacancy]):
    fields = [
        "vacancy_id", "title", "company", "salary", "location", "published_at",
        "url", "summary", "matched_queries", "source", "hard_filter_status", "hard_filter_reason"
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for v in rows:
            row = asdict(v)
            row["matched_queries"] = " | ".join(v.matched_queries)
            w.writerow(row)


def main() -> int:
    query_cfg = load_json(CONFIG_DIR / "queries.json")
    filter_cfg = load_json(CONFIG_DIR / "filters.json")
    fresh_days = int(query_cfg.get("fresh_days", 7))

    collected: list[Vacancy] = []
    errors: list[dict] = []

    for idx, query in enumerate(query_cfg["queries"], 1):
        print(f"[{idx}/{len(query_cfg['queries'])}] {query}")
        try:
            payload = fetch_rss(query)
            rows = [v for v in parse_items(payload, query) if within_days(v, fresh_days)]
            collected.extend(rows)
            print(f"  + {len(rows)} fresh RSS items")
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
        "fresh_days": fresh_days,
        "queries": len(query_cfg["queries"]),
        "rss_items": len(collected),
        "unique": len(unique),
        "target": len(target),
        "rejected": len(rejected),
        "errors": errors,
        "html_downloads": 0,
        "ai_calls": 0,
    }
    with (DATA_DIR / "run_meta.json").open("w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    print("\nDONE")
    print(json.dumps(meta, ensure_ascii=False, indent=2))
    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
