"""Find articles through sites' XML sitemaps (works for any site, WordPress or not).

News sites list every article URL in sitemaps, usually split by month or by
number, and announce them in robots.txt ("Sitemap: ..."). Walking those lists
lets us find years of daily water reports on sites that have no usable search
API, without crawling pages.

Everything here respects robots.txt: sitemaps and articles it disallows for
our User-Agent are skipped.
"""
from __future__ import annotations

import gzip
import re
import urllib.robotparser
from dataclasses import dataclass
from xml.etree import ElementTree as ET

from ..config import USER_AGENT


@dataclass(frozen=True)
class Site:
    key: str
    name: str
    site: str                     # base URL, no trailing slash
    extra_sitemaps: tuple = ()    # tried if robots.txt lists none


SITES: list[Site] = [
    Site("brecorder", "Business Recorder", "https://www.brecorder.com", ("/sitemap.xml",)),
    Site("urdupoint", "UrduPoint (English)", "https://www.urdupoint.com", ("/en/sitemap.xml",)),
    Site("thenews", "The News", "https://www.thenews.com.pk", ("/sitemap.xml",)),
    Site("radiopk", "Radio Pakistan", "https://www.radio.gov.pk", ("/sitemap.xml",)),
    Site("tribune", "Express Tribune", "https://tribune.com.pk", ("/sitemap.xml",)),
    Site("dunya", "Dunya News", "https://dunyanews.tv", ("/sitemap.xml",)),
    # WordPress sites whose search API was unusable: their sitemaps may still work
    Site("profit", "Profit (Pakistan Today)", "https://profit.pakistantoday.com.pk", ("/sitemap_index.xml", "/sitemap.xml")),
    Site("pakistantoday", "Pakistan Today", "https://www.pakistantoday.com.pk", ("/sitemap_index.xml", "/sitemap.xml")),
    Site("dailytimes", "Daily Times", "https://dailytimes.com.pk", ("/sitemap_index.xml", "/sitemap.xml")),
    Site("arynews", "ARY News", "https://arynews.tv", ("/sitemap_index.xml", "/sitemap.xml")),
    Site("nation", "The Nation", "https://www.nation.com.pk", ("/sitemap.xml",)),
    Site("samaa", "Samaa", "https://www.samaa.tv", ("/sitemap.xml",)),
]
SITE_BY_KEY = {s.key: s for s in SITES}

# Slugs/titles of daily water reports, e.g. irsa-releases-89400-cusecs-water,
# rivers-flow-reservoirs-level-report, wapda-issues-water-position-...
# Terms are matched as whole words: "irsa" must not hit "lok-virsa" or
# "irsaal-name-meaning", and a bare "reservoir" would catch oil and gas news
# (both seen in UrduPoint's sitemaps).
_W = r"(?<![a-z0-9])"
_E = r"(?![a-z])"
ARTICLE_SLUG_RE = re.compile(
    _W + r"(irsa" + _E + r"|cusecs?" + _E + r"|tarbela" + _E + r"|mangla" + _E
    + r"|rivers?[- ]flows?|river[- ]flows?|water[- ]position|rim[- ]stations?"
    + r"|water[- ]situation|reservoirs?[- ]levels?|indus[- ]river[- ]system)", re.I)
MIN_YEAR = 2017
_YEAR_RE = re.compile(r"(20[0-3]\d)")


class Robots:
    """robots.txt for one site (allows everything if it can't be read)."""

    def __init__(self, get, site: str):
        self.rp = urllib.robotparser.RobotFileParser()
        self.sitemaps: list[str] = []
        try:
            r = get(f"{site}/robots.txt", delay=2.0, timeout=30)
            lines = r.text.splitlines() if r.status_code == 200 else []
        except Exception:
            lines = []
        self.rp.parse(lines)
        self.sitemaps = [ln.split(":", 1)[1].strip() for ln in lines if ln.lower().startswith("sitemap:")]

    def allowed(self, url: str) -> bool:
        return self.rp.can_fetch(USER_AGENT, url)


def _xml_bytes(content: bytes) -> bytes:
    return gzip.decompress(content) if content[:2] == b"\x1f\x8b" else content


def parse_sitemap(content: bytes) -> tuple[str, list[dict]]:
    """('index' | 'urlset', [{loc, lastmod, title}])."""
    root = ET.fromstring(_xml_bytes(content))
    kind = "index" if root.tag.endswith("sitemapindex") else "urlset"
    out = []
    for el in root:
        loc = lastmod = title = None
        for child in el.iter():
            tag = child.tag.rsplit("}", 1)[-1]
            if tag == "loc" and loc is None:
                loc = (child.text or "").strip()
            elif tag == "lastmod":
                lastmod = (child.text or "").strip()
            elif tag in ("title",):  # Google News sitemaps carry <news:title>
                title = (child.text or "").strip()
            elif tag == "publication_date" and not lastmod:
                lastmod = (child.text or "").strip()
        if loc:
            out.append({"loc": loc, "lastmod": lastmod or "", "title": title or ""})
    return kind, out


def too_old(entry: dict) -> bool:
    """Skip child sitemaps that clearly cover years before MIN_YEAR."""
    years = [int(y) for y in _YEAR_RE.findall(entry["loc"])]
    if entry.get("lastmod"):
        years.append(int(entry["lastmod"][:4]))
    return bool(years) and max(years) < MIN_YEAR


def looks_like_report(entry: dict) -> bool:
    slug = entry["loc"].rstrip("/").rsplit("/", 1)[-1]
    return bool(ARTICLE_SLUG_RE.search(slug) or ARTICLE_SLUG_RE.search(entry.get("title", "")))


def walk(get, site: Site, *, max_sitemaps: int = 600, log=print) -> tuple[list[dict], str]:
    """All candidate article entries from a site's sitemaps. Returns (entries, status)."""
    robots = Robots(get, site.site)
    queue = list(dict.fromkeys(robots.sitemaps or [site.site + p for p in site.extra_sitemaps]))
    seen, found, fetched, errors = set(), {}, 0, 0
    while queue and fetched < max_sitemaps:
        url = queue.pop(0)
        if url in seen or not robots.allowed(url):
            continue
        seen.add(url)
        try:
            r = get(url, delay=2.0, timeout=60)
            fetched += 1
            if r.status_code != 200:
                errors += 1
                continue
            kind, entries = parse_sitemap(r.content)
        except Exception:
            errors += 1
            continue
        if kind == "index":
            # newest first, so a capped walk still reaches recent years
            children = sorted((e for e in entries if not too_old(e)),
                              key=lambda e: (e["lastmod"], e["loc"]), reverse=True)
            queue = [e["loc"] for e in children if e["loc"] not in seen] + queue
        else:
            for e in entries:
                if looks_like_report(e) and robots.allowed(e["loc"]):
                    found.setdefault(e["loc"], e)
        if fetched % 50 == 0:
            log(f"     {site.key}: {fetched} sitemaps read, {len(found)} candidates so far")
    if fetched == 0:
        return [], "no sitemap found (or disallowed by robots.txt)"
    status = "ok" if not queue else f"stopped after {max_sitemaps} sitemaps"
    if errors:
        status += f", {errors} sitemap errors"
    return sorted(found.values(), key=lambda e: (e["lastmod"], e["loc"])), status
