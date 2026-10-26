"""P3 build features: changelog-driven markers, self-hosted thumbnails and takedown,
per-category feeds, per-item source ids."""
from __future__ import annotations

import io
import json

from engine.stages import build, thumbs

from .test_engine import _ctx, make_record, pack  # noqa: F401


def _png() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (1200, 900), (200, 80, 40)).save(buf, "PNG")
    return buf.getvalue()


def test_updated_marker_from_changelog_hashes(pack, tmp_path):  # noqa: F811
    ctx = _ctx(pack)
    ctx.store.save_record(make_record(category="games"))
    ctx.today = "2026-10-22"
    build.run(ctx, tmp_path / "a")                          # baseline with hashes
    item = ctx.store.load_item("abc12345")
    ctx.store.save_item({**item, "summary": "Now does something else."})
    ctx.today = "2026-10-23"
    build.run(ctx, tmp_path / "b")
    cat = json.loads((tmp_path / "b" / "catalogue.json").read_text())["items"][0]
    assert cat["markers"] == ["updated"] and cat["sources"] == ["hn-show"]
    log = json.loads((pack.data_dir / "changelog" / "2026-10-23.json").read_text())
    assert log["counts"]["changed"] == 1
    published = json.loads((tmp_path / "b" / "changelog" / "2026-10-23.json").read_text())
    assert "hashes" not in published and "active_ids" not in published
    # this pack publishes no feeds and keeps its sources private ...
    assert not (tmp_path / "b" / "feeds").exists() and not (tmp_path / "b" / "sources.json").exists()
    assert "application/rss+xml" not in (tmp_path / "b" / "index.html").read_text()
    manifest = json.loads((tmp_path / "b" / "manifest.json").read_text())
    assert manifest["outputs"] == ["changelog"] and "sources" not in manifest["views"]
    # ... but the engine still writes both for a pack that asks
    pack.outputs = ["rss", "jsonfeed", "changelog"]
    pack.pack["views"] = ["grid", "digest", "sources"]
    build.run(ctx, tmp_path / "c")
    assert (tmp_path / "c" / "feeds" / "games.xml").exists() and (tmp_path / "c" / "sources.json").exists()
    assert "application/rss+xml" in (tmp_path / "c" / "index.html").read_text()


def test_thumbnail_is_published_and_takedown_removes_it(pack, tmp_path, monkeypatch):  # noqa: F811
    ctx = _ctx(pack)
    ctx.store.save_item({**make_record().to_json(), "image": {"src": "https://example.app/og.png", "alt": "x"}})
    monkeypatch.setattr(thumbs, "_download", lambda client, url: _png())
    assert thumbs.run(ctx)["made"] == 1
    webp = pack.data_dir / "img" / "abc12345.webp"
    from PIL import Image
    assert Image.open(webp).size == (640, 360)
    build.run(ctx, tmp_path / "out")
    item = json.loads((tmp_path / "out" / "catalogue.json").read_text())["items"][0]
    assert item["image"]["src"] == "img/abc12345.webp" and item["image"]["origin"] == "https://example.app/og.png"
    detail = json.loads((tmp_path / "out" / "items" / "abc12345.json").read_text())
    assert detail["image"]["src"] == "img/abc12345.webp" and "fingerprint" not in detail
    assert (tmp_path / "out" / "img" / "abc12345.webp").exists()

    # takedown: flag in the stored item (overrides.yml works the same way)
    ctx.store.save_item({**ctx.store.load_item("abc12345"), "flags": ["no-image"]})
    assert thumbs.run(ctx)["removed"] == 1 and not webp.exists()
    report = build.run(ctx, tmp_path / "out")
    assert json.loads((tmp_path / "out" / "catalogue.json").read_text())["items"][0]["image"] is None
    assert "img/abc12345.webp" in report["removed_stale"]


def test_summary_and_validate_exit_code(pack, tmp_path, capsys):  # noqa: F811
    from engine import cli
    from engine.summary import summarise

    run = tmp_path / "run.json"
    run.write_text(json.dumps({"date": "2026-10-23", "candidates": 4, "publish": 1, "review": 2, "rejected": 1,
                               "classifier": {"mode": "decide", "decide_calls": 4, "decide_errors": 0,
                                              "decide_cost_usd": 0.0004},
                               "sources": {"a": {"status": "error"}, "b": {"status": "ok"}}}))
    md = summarise(pack, str(run))
    assert "1 published, 2 to review" in md and "Sources failing:** a" in md and "$0.0004" in md

    # validate must fail the job on bad data (it used to exit 0 regardless)
    ctx = _ctx(pack)
    ctx.store.save_item({**make_record().to_json(), "category": "not-a-category"})
    assert cli.main(["validate", str(pack.path)]) == 1


def _noisy_png() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.effect_noise((1280, 720), 60).convert("RGB").save(buf, "PNG")
    return buf.getvalue()


def test_screenshot_fallback_when_no_preview_image(pack, tmp_path, monkeypatch):  # noqa: F811
    from contextlib import contextmanager

    ctx = _ctx(pack)
    ctx.store.save_record(make_record())               # no og:image at all
    shots = []

    class Cam:
        def shoot(self, url):
            shots.append(url)
            return _noisy_png()

    @contextmanager
    def fake_shooter(settle_ms):
        yield Cam()

    monkeypatch.setattr(thumbs, "shooter", fake_shooter)
    rep = thumbs.run(ctx)
    assert (rep["screenshots"], rep["failed"]) == (1, 0) and shots == ["https://example.app/"]
    stored = ctx.store.load_item("abc12345")
    assert stored["image"]["kind"] == "screenshot" and stored["image"]["taken"] == ctx.today
    build.run(ctx, tmp_path / "out")
    img = json.loads((tmp_path / "out" / "catalogue.json").read_text())["items"][0]["image"]
    assert img["src"] == "img/abc12345.webp" and img["kind"] == "screenshot"
    # retaken only once it is older than screenshot_refresh_days
    assert thumbs.run(ctx)["screenshots"] == 0
    ctx.today = "2099-01-01"
    assert thumbs.run(ctx)["screenshots"] == 1
    # without its file, a screenshot record publishes no image (its src is the app itself)
    (pack.data_dir / "img" / "abc12345.webp").unlink()
    build.run(ctx, tmp_path / "out2")
    assert json.loads((tmp_path / "out2" / "catalogue.json").read_text())["items"][0]["image"] is None
    # a takedown stops screenshots too
    ctx.store.save_item({**ctx.store.load_item("abc12345"), "flags": ["no-image"]})
    shots.clear()
    assert thumbs.run(ctx)["screenshots"] == 0 and not shots


def test_blank_screenshot_is_discarded():
    assert thumbs.is_blank(_png()) and not thumbs.is_blank(_noisy_png())
