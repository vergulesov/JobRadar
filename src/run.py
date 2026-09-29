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

HH_API_URL = "https://api.hh.ru/vacancies"
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


def fetch_api_page(query: str, page: int) -> dict:
    params = {
        "text": query,
        "order_by": "publication_time",
        "per_page": PER_PAGE,
        "page": page,
        "host": "hh.ru",
    }
    url = HH_API_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "HH-User-Agent": USER_AGENT,
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode("utf-8"))


def parse_api_item(item: dict, query: str) -> Vacancy:
    snippet = item.get("snippet") or {}
    summary = " ".join(filter(None, [
        strip_html(snippet.get("requirement")),
        strip_html(snippet.get("responsibility")),
    ])).strip()
    employer = item.get("employer") or {}
    area = item.get("area") or {}
    return Vacancy(
        vacancy_id=str(item.get("id") or ""),
        title=item.get("name") or "",
        company=employer.get("name"),
        salary=format_salary(item.get("salary")),
        location=area.get("name"),
        published_at=item.get("published_at"),
        url=item.get("alternate_url") or item.get("url") or "",
        summary=summary,
        matched_queries=[query],
    )


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


def deduplicate(items: list[Vacancy]) -> list[Vacancy]:
    by_id: dict[str, Vacancy] = {}
    for v in items:
        if not v.vacancy_id:
            continue
        if v.vacancy_id not in by_id:
            by_id[v.vacancy_id] = v
        else:
            existing = by_id[v.vacancy_id]
            for q in v.matched_queries:
                if q not in existing.matched_queries:
                    existing.matched_queries.append(q)
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
    cutoff = datetime.now(timezone.utc) - timedelta(days=fresh_days)

    collected: list[Vacancy] = []
    errors: list[dict] = []
    api_requests = 0
    total_found_by_queries = 0

    for idx, query in enumerate(query_cfg["queries"], 1):
        print(f"[{idx}/{len(query_cfg['queries'])}] {query}")
        query_rows: list[Vacancy] = []
        page = 0
        found = None

        try:
            while True:
                payload = fetch_api_page(query, page)
                api_requests += 1
                if found is None:
                    found = int(payload.get("found", 0))
                    total_found_by_queries += found
                    print(f"  found by HH: {found}")

                items = payload.get("items") or []
                if not items:
                    break

                parsed = [parse_api_item(item, query) for item in items]
                fresh = [v for v in parsed if is_fresh(v.published_at, cutoff)]
                query_rows.extend(fresh)

                pages = int(payload.get("pages", 1))
                oldest_is_stale = any(not is_fresh(v.published_at, cutoff) for v in parsed)

                # Sorted newest first: once a page crosses the freshness cutoff,
                # older pages cannot add useful vacancies.
                if oldest_is_stale or page + 1 >= pages:
                    break

                page += 1
                time.sleep(0.08)

            collected.extend(query_rows)
            print(f"  + {len(query_rows)} fresh API items ({page + 1} page(s))")
        except Exception as e:
            print(f"  ! {type(e).__name__}: {e}", file=sys.stderr)
            errors.append({"query": query, "page": page, "error": f"{type(e).__name__}: {e}"})

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
        "collector": "hh_api_list",
        "fresh_days": fresh_days,
        "queries": len(query_cfg["queries"]),
        "api_requests": api_requests,
        "total_found_by_queries_before_dedup": total_found_by_queries,
        "fresh_items_before_dedup": len(collected),
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

    print("\nDONE")
    print(json.dumps(meta, ensure_ascii=False, indent=2))
    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
