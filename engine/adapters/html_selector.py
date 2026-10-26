"""HTML selector adapter: CSS selectors declared in YAML for showcase pages."""

from __future__ import annotations

import re
from collections.abc import Iterable
from urllib.parse import urljoin, urlsplit

import httpx
from selectolax.lexbor import LexborHTMLParser as HTMLParser

from engine.http import USER_AGENT
from engine.models import Candidate


class HtmlSelectorAdapter:
    """source config:

    url: https://example.com/showcase
    selectors:
      item: "div.app-card"          # each match becomes one candidate
      link: "a@href"                # attribute selector (attr@name)
      title: "h3"
      text: "p"
      creator: ".author"

    Showcases split over many pages (one page per creator, say) list those pages in
    their sitemap. Read every page the sitemap lists instead of `url`:

    pages_from: { sitemap: https://example.com/sitemap.xml, include: ["/u/"], exclude: [], max: 200 }
    external_only: true             # drop links back into the showcase itself
    """

    type = "html_selector"

    def __init__(self, source: dict):
        self.source = source
        sel = source.get("selectors", {})
        self.item_sel = sel.get("item")
        self.fields = {k: v for k, v in sel.items() if k != "item"}

    def fetch(self, url: str, since) -> Iterable[Candidate]:
        with httpx.Client(timeout=30.0, follow_redirects=True,
                          headers={"User-Agent": USER_AGENT}) as client:
            seen: set[str] = set()
            for page_url in self._pages(client, self.source.get("url") or url):
                for cand in self._page(client, page_url):
                    if cand.url not in seen:
                        seen.add(cand.url)
                        yield cand

    def _pages(self, client: httpx.Client, url: str) -> list[str]:
        spec = self.source.get("pages_from")
        if not spec:
            return [url]
        try:
            r = client.get(spec["sitemap"])
        except httpx.HTTPError:
            return []
        if r.status_code != 200:
            return []
        inc, exc = spec.get("include", []), spec.get("exclude", [])
        locs = [u.strip() for u in re.findall(r"<loc>([^<]+)</loc>", r.text)]
        locs = [u for u in locs if (not inc or any(p in u for p in inc)) and not any(p in u for p in exc)]
        return locs[: int(spec.get("max", 200))]

    def _page(self, client: httpx.Client, page_url: str) -> Iterable[Candidate]:
        try:
            r = client.get(page_url)
        except httpx.HTTPError:
            return
        if r.status_code != 200:
            return
        tree = HTMLParser(r.text)
        home = urlsplit(page_url).hostname
        for node in tree.css(self.item_sel or "a"):
            link = self._pick(node, self.fields.get("link", "a@href"))
            if not link:
                continue
            link = urljoin(page_url, link)
            if not link.startswith(("http://", "https://")):
                continue
            if self.source.get("external_only") and urlsplit(link).hostname == home:
                continue
            text = self._pick(node, self.fields.get("text")) or ""
            note = self.source.get("listing_note", "")  # as in api_json: reaches the classifier
            yield Candidate(
                url=link,
                title=self._pick(node, self.fields.get("title")) or "",
                text=f"{text}\n\n{note}".strip() if note else (text or None),
                creator=self._pick(node, self.fields.get("creator")),
                published=None,
                source_id=self.source["id"],
                raw={"source_type": "html", "listing_url": page_url},
            )

    @staticmethod
    def _pick(node, rule: str | None) -> str | None:
        if not rule:
            return None
        if "@" in rule:
            sel, attr = rule.split("@", 1)
            if sel:
                found = node.css_first(sel)
            else:
                found = node
            return found.attributes.get(attr) if found else None
        found = node.css_first(rule)
        return found.text(strip=True)[:4000] if found else None
