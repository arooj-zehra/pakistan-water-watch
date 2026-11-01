"""News outlets that run on WordPress, reached through the WordPress REST API.

Most Pakistani news sites that republish WAPDA/IRSA's daily figures run on
WordPress, which exposes posts as JSON. That lets us list years of stories in
a few requests (100 per page) and download their text in batches of 100,
instead of scraping one HTML page at a time.

Outlets are tried in the order below; when two outlets report the same day,
the more complete story wins and ties go to the outlet listed first.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Outlet:
    key: str       # short id, used as `source` in the database and folder name
    name: str
    site: str      # base URL, no trailing slash


OUTLETS: list[Outlet] = [
    Outlet("app", "Associated Press of Pakistan", "https://www.app.com.pk"),
    Outlet("profit", "Profit (Pakistan Today)", "https://profit.pakistantoday.com.pk"),
    Outlet("pakistantoday", "Pakistan Today", "https://www.pakistantoday.com.pk"),
    Outlet("dailytimes", "Daily Times", "https://dailytimes.com.pk"),
    Outlet("energyupdate", "Energy Update", "https://www.energyupdate.com.pk"),
    Outlet("customstoday", "Customs Today", "https://customstoday.media"),
    Outlet("dailyausaf", "Daily Ausaf (English)", "https://dailyausaf.com/en"),
    Outlet("arynews", "ARY News", "https://arynews.tv"),
    Outlet("pakobserver", "Pakistan Observer", "https://pakobserver.net"),
    Outlet("nation", "The Nation", "https://www.nation.com.pk"),
    Outlet("samaa", "Samaa", "https://www.samaa.tv"),
]
OUTLET_BY_KEY = {o.key: o for o in OUTLETS}

# Every daily release names a dam and "cusecs"; the parser drops anything else.
SEARCH_QUERIES = ["Tarbela cusecs", "Mangla cusecs"]
PER_PAGE = 100


class WordPressError(RuntimeError):
    pass


def endpoints(site: str) -> list[str]:
    """Pretty-permalink endpoint first, then the form that works without it."""
    return [f"{site}/wp-json/wp/v2/posts", f"{site}/?rest_route=/wp/v2/posts"]


def _looks_like_posts(r) -> bool:
    if r.status_code != 200:
        return False
    try:
        data = r.json()
    except Exception:
        return False
    return isinstance(data, list)


def find_endpoint(get, site: str) -> str:
    """The first endpoint that answers with a JSON list of posts."""
    errors = []
    for url in endpoints(site):
        try:
            r = get(url, delay=2.0, timeout=60, params={"per_page": 1, "_fields": "id"})
        except Exception as e:
            errors.append(f"{type(e).__name__}")
            continue
        if _looks_like_posts(r):
            return url
        errors.append(f"HTTP {r.status_code}")
    raise WordPressError("no WordPress API (" + ", ".join(errors) + ")")


def _title(p: dict) -> str:
    t = p.get("title", "")
    t = t.get("rendered", "") if isinstance(t, dict) else str(t)
    return re.sub(r"<[^>]+>", "", t)


def search(get, endpoint: str, query: str, *, after: str | None = None, max_pages: int = 40) -> list[dict]:
    """List posts matching `query` (newest first): id, date, link, title.

    `after` is an ISO datetime; only posts published after it are returned.
    """
    posts = []
    for page in range(1, max_pages + 1):
        params = {"search": query, "per_page": PER_PAGE, "page": page,
                  "_fields": "id,date,link,title", "orderby": "date", "order": "desc"}
        if after:
            params["after"] = after
        r = get(endpoint, delay=2.0, timeout=60, params=params)
        if r.status_code == 400 and page > 1:  # WordPress: page number past the end
            break
        if r.status_code != 200:
            raise WordPressError(f"HTTP {r.status_code} on page {page}")
        batch = r.json()
        if not isinstance(batch, list) or not batch:
            break
        posts += [{"id": int(p["id"]), "date": p.get("date", ""), "link": p.get("link", ""),
                   "title": _title(p)} for p in batch]
        total_pages = int(getattr(r, "headers", {}).get("X-WP-TotalPages", page) or page)
        if page >= total_pages:
            break
    return posts


def slim(post: dict) -> dict:
    def rendered(v):
        return v.get("rendered", "") if isinstance(v, dict) else (v or "")
    return {"id": int(post["id"]), "date": post.get("date", ""), "link": post.get("link", ""),
            "title": rendered(post.get("title")), "content": rendered(post.get("content"))}


def fetch(get, endpoint: str, ids: list[int], progress=None) -> list[dict]:
    """Download full posts by id, PER_PAGE at a time."""
    out = []
    for i in range(0, len(ids), PER_PAGE):
        chunk = ids[i:i + PER_PAGE]
        r = get(endpoint, delay=2.0, timeout=90, params={
            "include": ",".join(map(str, chunk)), "per_page": PER_PAGE,
            "_fields": "id,date,link,title,content",
        })
        if r.status_code != 200:
            raise WordPressError(f"HTTP {r.status_code} for batch starting at id {chunk[0]}")
        out += [slim(p) for p in r.json()]
        if progress:
            progress(min(i + PER_PAGE, len(ids)), len(ids))
    return out
