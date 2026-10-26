"""Source adapters registry."""

from __future__ import annotations

from engine.adapters import api_json, csv, html_selector, manual, rss, sitemap

_REGISTRY = {
    "rss": rss.RssAdapter,
    "api_json": api_json.ApiJsonAdapter,
    "html_selector": html_selector.HtmlSelectorAdapter,
    "sitemap": sitemap.SitemapAdapter,
    "csv": csv.CsvAdapter,
    "manual": manual.ManualAdapter,
}


def get_adapter(source: dict) -> object:
    kind = source.get("type", "")
    cls = _REGISTRY.get(kind)
    if cls is None:
        raise ValueError(f"unknown source type: {kind}")
    return cls(source)


def known_types() -> list[str]:
    return sorted(_REGISTRY)
