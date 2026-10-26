"""Self-hosted thumbnails (P3): download each item's preview image once, crop it to
16:9, and store a small WebP under data/img/<id>.webp. The build publishes them as
img/<id>.webp, so the site no longer hotlinks other people's servers.

Most small apps publish no social-preview image (in October 2026, 94 of 97 items had
none), so when there is no preview image, or it can't be downloaded, `thumbs` takes a
screenshot of the app itself in headless Chromium (Playwright, the optional
`screenshots` extra). The item then records `image.kind: "screenshot"` and when it was
taken; screenshots are retaken after `thumbs.screenshot_refresh_days`. Blank renders
(a page still loading, an error screen in one colour) are discarded.

Takedown: set `image: null` for the item in data/overrides.yml (or add the flag
`no-image`). The build then publishes no image, `thumbs` deletes the stored file on
its next run and never screenshots the item.
"""

from __future__ import annotations

import datetime as dt
import io
import logging
from contextlib import contextmanager
from pathlib import Path

import httpx

from engine.http import USER_AGENT
from engine.util import safe_http_url

log = logging.getLogger("engine")

WIDTH, HEIGHT = 640, 360
MAX_BYTES = 8 * 1024 * 1024
QUALITY = 72
SHOT_W, SHOT_H = 1280, 720
BLANK_STDDEV = 6.0   # a greyscale spread below this is a blank or single-colour page
DEFAULTS = {"screenshot": True, "screenshot_refresh_days": 60, "max_screenshots_per_run": 80,
            "screenshot_settle_ms": 2500}


def img_dir(pack) -> Path:
    return pack.data_dir / "img"


def taken_down(store, item: dict) -> bool:
    """A human took the picture down: the no-image flag, or `image: null` in overrides."""
    ov = store.overrides.get(item["id"]) or {}
    return "no-image" in (item.get("flags") or []) or ("image" in ov and not ov["image"])



def is_blank(png: bytes) -> bool:
    from PIL import Image, ImageStat

    with Image.open(io.BytesIO(png)) as im:
        return ImageStat.Stat(im.convert("L")).stddev[0] < BLANK_STDDEV


class Shooter:
    """One headless Chromium for the whole run. Each page gets a fresh context (no
    cookies or storage carried between apps), no downloads, and a hard timeout."""

    def __init__(self, browser, settle_ms: int):
        self.browser, self.settle_ms = browser, settle_ms

    def shoot(self, url: str) -> bytes | None:
        ctx = self.browser.new_context(viewport={"width": SHOT_W, "height": SHOT_H},
                                       user_agent=USER_AGENT + " (screenshot)",
                                       accept_downloads=False, reduced_motion="reduce",
                                       color_scheme="light", java_script_enabled=True)
        try:
            page = ctx.new_page()
            page.set_default_timeout(20_000)
            page.goto(url, wait_until="load")
            page.wait_for_timeout(self.settle_ms)   # JS apps draw after load
            png = page.screenshot(type="png")
            return None if is_blank(png) else png
        except Exception as e:  # noqa: BLE001 — any page failure just means no screenshot
            log.info("screenshot failed for %s: %s", url, str(e).splitlines()[0][:120])
            return None
        finally:
            ctx.close()


@contextmanager
def shooter(settle_ms: int):
    """Yields a Shooter, or None when Playwright or its browser isn't installed."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        yield None
        return
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception as e:  # noqa: BLE001 — browser binary missing
            log.warning("screenshots off: %s", str(e).splitlines()[0][:160])
            yield None
            return
        try:
            yield Shooter(browser, settle_ms)
        finally:
            browser.close()


def _stale_screenshot(image: dict | None, today: str, days: int) -> bool:
    if not image or image.get("kind") != "screenshot":
        return False
    try:
        return (dt.date.fromisoformat(today) - dt.date.fromisoformat(image.get("taken", ""))).days >= days
    except ValueError:
        return True


def make_thumb(data: bytes) -> bytes:
    """Cover-crop to 16:9 and encode as WebP."""
    from PIL import Image, ImageOps

    with Image.open(io.BytesIO(data)) as im:
        im = ImageOps.exif_transpose(im)
        if im.mode not in ("RGB", "RGBA"):
            im = im.convert("RGBA" if "transparency" in im.info else "RGB")
        thumb = ImageOps.fit(im, (WIDTH, HEIGHT), method=Image.Resampling.LANCZOS)
        out = io.BytesIO()
        thumb.save(out, "WEBP", quality=QUALITY, method=6)
        return out.getvalue()


def _download(client: httpx.Client, url: str) -> bytes | None:
    try:
        with client.stream("GET", url, headers={"User-Agent": USER_AGENT}, follow_redirects=True,
                           timeout=20.0) as r:
            if r.status_code != 200 or not r.headers.get("content-type", "").startswith("image/"):
                return None
            chunks, size = [], 0
            for chunk in r.iter_bytes():
                size += len(chunk)
                if size > MAX_BYTES:
                    return None
                chunks.append(chunk)
            return b"".join(chunks)
    except httpx.HTTPError:
        return None


def _thumb(iid: str, data: bytes | None) -> bytes | None:
    try:
        return make_thumb(data) if data else None
    except Exception as e:  # noqa: BLE001 — a bad image must never stop the run
        log.warning("thumbnail failed for %s: %s", iid, e)
        return None


def run(ctx, refresh: bool = False, screenshots: bool | None = None) -> dict:
    """Fetch missing thumbnails (all with `refresh`), screenshot apps without a usable
    preview image, and remove thumbnails no longer wanted."""
    cfg = {**DEFAULTS, **(ctx.pack.pack.get("thumbs") or {})}
    use_shots = cfg["screenshot"] if screenshots is None else screenshots
    items = ctx.store.load_items()  # overrides applied: a takedown wins
    folder = img_dir(ctx.pack)
    folder.mkdir(parents=True, exist_ok=True)
    made = shot = failed = removed = 0
    todo: list[tuple[str, dict]] = []   # (id, item) still without a thumbnail
    with ctx.http_client() as client:
        for iid, it in items.items():
            path = folder / f"{iid}.webp"
            if taken_down(ctx.store, it) or it.get("status") == "dead":
                if path.exists():
                    path.unlink()
                    removed += 1
                continue
            image = it.get("image") or {}
            if path.exists() and not refresh and not _stale_screenshot(
                    image, ctx.today, int(cfg["screenshot_refresh_days"])):
                continue
            if image.get("kind") != "screenshot":
                src = safe_http_url(image.get("origin") or image.get("src"))
                thumb = _thumb(iid, _download(client, src) if src else None)
                if thumb:
                    path.write_bytes(thumb)
                    made += 1
                    continue
            todo.append((iid, it))
    status = "off"
    if todo and use_shots:
        budget = int(cfg["max_screenshots_per_run"])
        with shooter(int(cfg["screenshot_settle_ms"])) as cam:
            status = "on" if cam else "unavailable"
            for iid, it in todo:
                url = safe_http_url(it.get("url"))
                thumb = _thumb(iid, cam.shoot(url)) if cam and url and shot < budget else None
                if thumb:
                    (folder / f"{iid}.webp").write_bytes(thumb)
                    _record_screenshot(ctx, iid, it, today=ctx.today)
                    shot += 1
                else:
                    failed += 1
    else:
        failed += len(todo)
    return {"made": made, "screenshots": shot, "failed": failed, "removed": removed,
            "screenshot_mode": status if use_shots else "off",
            "stored": len(list(folder.glob("*.webp")))}


def _record_screenshot(ctx, iid: str, merged: dict, today: str) -> None:
    """Mark the stored item (not the override-merged copy) as pictured by a screenshot."""
    raw = ctx.store.load_item(iid, apply_overrides=False)
    if raw is None or ctx.store.dry_run:
        return
    raw["image"] = {"src": merged["url"], "alt": f"Screenshot of {merged.get('title') or merged['url']}",
                    "kind": "screenshot", "taken": today}
    ctx.store.save_item(raw)
