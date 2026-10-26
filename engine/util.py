"""URL normalisation, ID generation and small shared helpers."""

from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "ref", "ref_src", "fbclid", "gclid", "mc_cid", "mc_eid", "igshid",
    "si", "share", "share_id", "utm_id", "spm", "_ga",
}

_SLASHES = re.compile(r"/+")


def normalise_url(url: str) -> str:
    """Lowercase host, drop tracking params and trailing slash."""
    url = url.strip()
    parts = urlsplit(url)
    host = parts.netloc.lower()
    host = host.removeprefix("www.")
    path = _SLASHES.sub("/", parts.path)
    if path.endswith("/") and path != "/":
        path = path[:-1]
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False)
             if k.lower() not in TRACKING_PARAMS]
    query.sort(key=lambda kv: kv[0])
    return urlunsplit((parts.scheme, host, path, urlencode(query), ""))


def url_id(url: str) -> str:
    """Short stable hash of the normalised URL. Two spellings of one app hash the
    same: the scheme is ignored (http is hashed as https), and a bare root path
    equals "/" (https://a.app/ is hashed as https://a.app). Both used to produce
    duplicate items."""
    parts = urlsplit(normalise_url(url))
    path = "" if parts.path == "/" else parts.path
    key = urlunsplit(("https" if parts.scheme in ("http", "https") else parts.scheme,
                      parts.netloc, path, parts.query, ""))
    return hashlib.sha1(key.encode()).hexdigest()[:8]


def safe_http_url(url: str | None, base: str = "") -> str:
    """Absolute http(s) URL or ''. For URLs read from untrusted pages."""
    from urllib.parse import urljoin

    if not url:
        return ""
    absolute = urljoin(base, url.strip())
    return absolute if urlsplit(absolute).scheme in ("http", "https") else ""


def looks_like_parked(title: str, text: str | None, host: str) -> str | None:
    """Cheap parked/dead-page heuristics; returns a reason or None."""
    t = (title or "").lower()
    body = (text or "").lower()
    if len(body) < 80 and re.search(r"\b(for sale|domain|buy this domain|parked)\b", t + " " + body):
        return "parked-domain"
    if re.search(r"^(index of|login|sign in)$", t.strip()):
        return "login-wall"
    return None


def strip_text(html: str, limit: int = 8000) -> str:
    """HTML to plain text, length-capped. Untrusted data, never executed."""
    from selectolax.lexbor import LexborHTMLParser as HTMLParser

    tree = HTMLParser(html)
    for bad in tree.css("script, style, noscript, template, svg, iframe"):
        bad.decompose()
    text = re.sub(r"\s+", " ", tree.text(separator=" ", deep=True)).strip()
    return text[:limit]


def no_dashes(text: str, title: bool = False) -> str:
    """Remove em dashes (and spaced en dashes used as em dashes) from text the site
    shows. In a title a dash separates name and tagline, so it becomes " - ". In
    prose a pair of dashes marks an aside and becomes parentheses; a single dash
    introduces a list or an explanation and becomes a colon (a comma when the
    sentence already has one)."""
    if not text:
        return text
    dash = r"\s*—\s*|\s+–\s+"
    if title:
        return re.sub(dash, " - ", text).strip()
    text = re.sub(r"\s*(?:—|\s–)\s*([^—–.]+?)\s*(?:—|–\s)\s*", r" (\1) ", text)
    out = []
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        sep = ", " if ":" in sentence else ": "
        out.append(re.sub(dash, sep, sentence, count=1))
        out[-1] = re.sub(dash, ", ", out[-1])
    return " ".join(out).replace(" )", ")").strip()
