"""Regression tests for the defects fixed in session 8.

Each test names the behaviour that previously broke: wiped review metadata,
records losing page text, the empty-id publish path, known URLs being
re-proposed, classification never being cached, and gate drift between `run`
and `reclassify`.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from engine.config import load_pack
from engine.models import Record
from engine.store import Store


@pytest.fixture()
def pack(tmp_path: Path, monkeypatch):
    """A minimal pack pointing at an empty data dir (data_dir is pack/../../data)."""
    monkeypatch.delenv("CATALOGUE_PRIVATE_DIR", raising=False)
    src = Path("packs/webapps").resolve()
    pack_dir = tmp_path / "packs" / "webapps"
    pack_dir.mkdir(parents=True)
    for name in ("pack.yml", "schema.yml", "taxonomy.yml", "facets.yml",
                 "rules.yml", "copy.yml", "outputs.yml", "classify.prompt.md", "decide.yml"):
        (pack_dir / name).write_text((src / name).read_text(encoding="utf-8"), encoding="utf-8")
    # One data folder for everything here; the private-folder layout has its own test.
    yml = pack_dir / "pack.yml"
    yml.write_text("\n".join(line for line in yml.read_text(encoding="utf-8").splitlines()
                             if not line.startswith("private_data:")) + "\n", encoding="utf-8")
    (pack_dir / "sources.yml").write_text("sources: []\n", encoding="utf-8")
    return load_pack(pack_dir)


def make_record(**over) -> Record:
    base = {
        "id": "abc12345", "url": "https://example.app/", "source_id": "hn-show", "tier": "B",
        "title": "Example", "summary": "Sum", "why": "Why", "category": "utilities",
        "tags": ["x"], "attrs": {"cost": "free"}, "dates": {"first_seen": "2026-10-22"},
        "provenance": [{"source": "hn-show", "url": "https://news.ycombinator.com/item?id=1",
                        "seen": "2026-10-22"}],
        "quality": {"confidence": 0.7}, "text": "page body here",
        "raw": {"source_tier": "B", "hn_id": 1},
    }
    base.update(over)
    return Record(**base)


class TestRecordRoundTrip:
    def test_nothing_is_dropped(self):
        rec = make_record()
        back = Record.from_json(rec.to_json(include_text=True))
        assert back.dates == {"first_seen": "2026-10-22"}
        assert back.provenance and back.provenance[0]["source"] == "hn-show"
        assert back.text == "page body here"
        assert back.raw["hn_id"] == 1
        assert back.source_id == "hn-show"
        assert back.tier == "B"

    def test_published_items_stay_lean(self):
        d = make_record().to_json()
        assert "text" not in d and "raw" not in d


class TestReviewStore:
    def test_save_review_keeps_page_text_and_provenance(self, pack):
        from engine.util import url_id

        store = Store(pack)
        rec = make_record(id="")  # fresh candidate: no id yet
        expected_id = url_id(rec.url)
        store.save_review(rec, 0.7)
        saved = json.loads((pack.data_dir / "review" / f"{expected_id}.json").read_text())
        assert saved["id"] == expected_id          # url-derived, never empty
        assert saved["text"] == "page body here"   # so reclassify can re-score
        assert saved["provenance"] and saved["dates"]["first_seen"]
        # round trip through from_json must not lose anything
        back = Record.from_json(saved)
        assert back.text == "page body here"
        assert back.provenance == rec.provenance

    def test_known_urls_covers_all_decisions(self, pack):
        store = Store(pack)
        store.save_record(make_record(id="aaa11111", url="https://pub.example/"))
        store.save_review(make_record(id="bbb22222", url="https://pend.example/"), 0.7)
        store.log_rejected("https://nope.example/", "hn-show", "not-a-web-app")
        known = store.known_urls()
        from engine.util import url_id
        assert url_id("https://pub.example/") in known
        assert url_id("https://pend.example/") in known
        assert url_id("https://nope.example/") in known
        assert url_id("https://other.example/") not in known

    def test_rejected_log_is_deduped(self, pack):
        store = Store(pack)
        store.log_rejected("https://x.example/", "hn-show", "low-confidence:0.30")
        store.log_rejected("https://x.example/", "hn-show", "low-confidence:0.30")
        lines = (pack.data_dir / "rejected.jsonl").read_text().splitlines()
        assert len(lines) == 1


class TestGatePolicy:
    def test_thresholds_match_pack_config(self, pack):
        from engine.stages import gate

        class Ctx:
            pass

        ctx = Ctx()
        ctx.pack = pack
        ctx.store = Store(pack)
        records = [
            make_record(id="p1", url="https://a.example/",
                        quality={"confidence": 0.9}, tier="A", raw={"source_tier": "A"}),
            make_record(id="r1", url="https://b.example/", quality={"confidence": 0.7}),
            make_record(id="x1", url="https://c.example/", quality={"confidence": 0.2}),
        ]
        out = gate.run(ctx, records)
        assert [r.id for r in out["publish"]] == ["p1"]      # tier A + >= auto_publish
        assert [r.id for r in out["review"]] == ["r1"]       # in the review band
        assert [r.id for r in out["rejected"]] == ["x1"]     # below the review floor
        # tier B never auto-publishes, however confident
        out = gate.run(ctx, [make_record(id="b9", url="https://d.example/",
                                         quality={"confidence": 0.99}, tier="B")])
        assert out["review"] and not out["publish"]


class TestLLMCache:
    def test_structured_calls_are_cached(self, tmp_path):
        from engine.llm.openrouter import OpenRouter

        llm = OpenRouter("model/x", cache_path=tmp_path / "cache.json")
        schema = {"type": "object"}
        key = llm._cache_key("sys", "usr", schema)
        llm._cache[key] = {"category": "games"}
        llm._unsaved = 1
        llm.flush()
        again = OpenRouter("model/x", cache_path=tmp_path / "cache.json")
        assert again.complete("sys", "usr", schema=schema) == {"category": "games"}
        # a different schema must not hit the same entry
        assert again._cache_key("sys", "usr", {"type": "string"}) != key


class TestQualifyIdentity:
    def test_provenance_points_at_the_discovery_thread(self, pack):
        from engine.models import Candidate
        from engine.stages import qualify

        cand = Candidate(url="https://example.app/", title="T", text="t",
                         source_id="hn-show", raw={"source_tier": "A"})
        assert qualify.rules_match(pack, cand) is None
        # with an HN id the provenance URL is the thread, not the app URL
        cand.raw["hn_id"] = "42"
        assert qualify._provenance_url(cand.raw, cand.url) == "https://news.ycombinator.com/item?id=42"
        del cand.raw["hn_id"]
        assert qualify._provenance_url(cand.raw, cand.url) == cand.url


def test_build_versions_every_module_and_shell_url(tmp_path: Path):
    """A build's modules must load as one set: stale cached modules mixed with new
    ones (a new app.js importing a name an old data.js lacks) blank the page."""
    import re

    from engine.stages.build import _copy_site

    _copy_site(tmp_path, "abc123")
    html = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert './app.js?v=abc123"' in html and './css/app.css?v=abc123"' in html
    for js in [tmp_path / "app.js", *(tmp_path / "js").rglob("*.js")]:
        for spec in re.findall(r"""from\s+['"](\.[^'"]+)['"]""", js.read_text(encoding="utf-8")):
            assert spec.endswith("?v=abc123"), (js, spec)
    assert "const VERSION = 'v4-abc123';" in (tmp_path / "sw.js").read_text(encoding="utf-8")
    # Rebuilding over an existing output must not stack version suffixes.
    _copy_site(tmp_path, "def456")
    assert "?v=abc123" not in (tmp_path / "app.js").read_text(encoding="utf-8")


def _ctx(pack):
    from engine.pipeline import Context

    return Context(pack=pack, store=Store(pack), today="2026-10-23", yesterday="2026-10-22")


def test_build_leaves_human_hidden_items_off_the_site(pack, tmp_path: Path):
    from engine.stages import build

    ctx = _ctx(pack)
    ctx.store.save_record(make_record())
    ctx.store.save_record(make_record(id="hidden01", url="https://hidden.app/", flags=["hidden"]))
    build.run(ctx, tmp_path / "out")
    ids = [it["id"] for it in json.loads((tmp_path / "out" / "catalogue.json").read_text())["items"]]
    assert ids == ["abc12345"]
    assert not (tmp_path / "out" / "items" / "hidden01.json").exists()


def test_lifecycle_failed_check_does_not_refresh_verified_date(pack, monkeypatch):
    from engine.http import FetchResult
    from engine.stages import lifecycle

    ctx = _ctx(pack)
    ctx.store.save_record(make_record(dates={"first_seen": "2026-10-01",
                                             "last_verified": "2026-10-01"}))
    monkeypatch.setattr(lifecycle, "fetch", lambda url, client: FetchResult(503, error="down"))
    lifecycle.run(ctx)
    item = ctx.store.load_item("abc12345")
    assert item["dates"]["last_verified"] == "2026-10-01"
    assert item["liveness"] == {"failures": 1, "last_check": "2026-10-23", "reason": "down"}


def test_transient_fetch_failures_are_retried_within_the_window(pack):
    store = Store(pack)
    log = [
        ("https://down.app/", "fetch-failed:ConnectError", "2026-10-22"),
        ("https://busy.app/", "fetch-failed:429", "2026-10-22"),
        ("https://oops.app/", "fetch-failed:503", "2026-10-22"),
        ("https://gone.app/", "fetch-failed:404", "2026-10-22"),
        ("https://robots.app/", "fetch-failed:403", "2026-10-22"),
        ("https://meh.app/", "low-confidence:0.30", "2026-10-22"),
        ("https://old.app/", "fetch-failed:ConnectTimeout", "2026-09-01"),
        # a later final answer wins over an earlier transient failure
        ("https://flaky.app/", "fetch-failed:ConnectError", "2026-10-20"),
        ("https://flaky.app/", "fetch-failed:404", "2026-10-22"),
    ]
    store.rejected_path.write_text("".join(
        json.dumps({"url": u, "source": "s", "reason": r, "seen": d}) + "\n" for u, r, d in log))
    known = store.known_urls("2026-10-23")
    from engine.util import url_id
    assert {url_id(u) for u in ("https://down.app/", "https://busy.app/", "https://oops.app/")} \
        .isdisjoint(known)
    assert {url_id(u) for u in ("https://gone.app/", "https://robots.app/", "https://meh.app/",
                                "https://old.app/", "https://flaky.app/")} <= known
    assert store.retrying == 3


def test_dry_run_does_not_write_the_rejected_log(pack):
    store = Store(pack, dry_run=True)
    store.log_rejected("https://x.app/", "s", "fetch-failed:404")
    assert not store.rejected_path.exists()


def test_second_source_sighting_is_kept_and_corroborates(pack, monkeypatch):
    """Discover used to drop the second sighting, so corroboration never fired."""
    from engine import adapters
    from engine.models import Candidate
    from engine.stages import discover, gate
    from engine.stages.qualify import provenance_entries

    class Fake:
        def __init__(self, src):
            self.src = src

        def fetch(self, url, since):
            yield Candidate(url="https://app.example/?utm_source=x", title="App",
                            source_id=self.src["id"], raw=dict(self.src.get("raw", {})))

    monkeypatch.setattr(adapters, "get_adapter", Fake)
    ctx = _ctx(pack)
    cands = discover.run(ctx, [{"id": "hn-show", "trust": "B", "raw": {"hn_id": "7"}},
                               {"id": "manual", "trust": "A"}])
    assert len(cands) == 1 and cands[0].raw["source_tier"] == "A"
    prov = provenance_entries(cands[0], ctx.today)
    assert [(p["source"], p["url"]) for p in prov] == [
        ("hn-show", "https://news.ycombinator.com/item?id=7"),
        ("manual", "https://app.example/?utm_source=x")]

    rec = make_record(provenance=prov, quality={"confidence": 0.7})
    gate.run(ctx, [rec])
    assert rec.quality["mentions"] == 2 and rec.quality["confidence"] == pytest.approx(0.8)


def test_known_app_found_by_a_new_source_gains_provenance(pack):
    store = Store(pack)
    store.save_record(make_record())
    new = [{"source": "manual", "url": "https://example.app/", "seen": "2026-10-23"}]
    assert store.add_sighting("abc12345", new) is True
    assert store.add_sighting("abc12345", new) is False  # idempotent
    item = store.load_item("abc12345")
    assert [p["source"] for p in item["provenance"]] == ["hn-show", "manual"]
    assert item["quality"]["mentions"] == 2


def test_url_id_ignores_scheme_but_keeps_https_ids_stable():
    from engine.util import url_id

    assert url_id("http://www.Example.app/") == url_id("https://example.app")
    assert url_id("https://a.app/x/") == url_id("http://a.app/x")
    # the hash of an https URL is what it always was
    import hashlib
    assert url_id("https://example.app/x") == hashlib.sha1(b"https://example.app/x").hexdigest()[:8]


def test_preview_image_is_resolved_and_only_http():
    from engine.stages.enrich import extract_page_meta, preview_image

    meta = extract_page_meta('<meta property="og:image" content="/og.png"><title>T</title>')
    assert preview_image(meta, "https://a.app/x/", "T") == {"src": "https://a.app/og.png",
                                                            "alt": "Preview of T"}
    assert preview_image({"image": "javascript:alert(1)"}, "https://a.app/", "T") is None


def test_hn_skips_text_posts_and_low_points(monkeypatch):
    from engine.adapters import api_json

    hits = [{"title": "Show HN: a", "url": "https://a.app", "points": 5, "objectID": "1"},
            {"title": "Show HN: text post", "url": None, "points": 50, "objectID": "2"},
            {"title": "Show HN: c", "url": "https://c.app", "points": 1, "objectID": "3"}]
    monkeypatch.setattr(api_json, "get_json", lambda *a, **k: {"hits": hits})
    adapter = api_json.ApiJsonAdapter({"id": "hn", "preset": "hn_search", "query": "x",
                                       "filters": {"min_points": 3}})
    assert [c.url for c in adapter.fetch("", None)] == ["https://a.app"]


def test_build_removes_its_own_stale_files_only(pack, tmp_path: Path):
    from engine.stages import build

    ctx = _ctx(pack)
    ctx.store.save_record(make_record())
    out = tmp_path / "out"
    build.run(ctx, out)
    (out / "CNAME").write_text("example.org")             # not ours: must survive
    stale_item = out / "items" / "abc12345.json"
    assert stale_item.exists()
    item = ctx.store.load_item("abc12345")
    ctx.store.save_item({**item, "status": "dead"})
    report = build.run(ctx, out)
    assert "items/abc12345.json" in report["removed_stale"] and not stale_item.exists()
    assert (out / "CNAME").exists() and (out / "index.html").exists()


def test_version_hash_tracks_data_and_code_hash_tracks_code(pack, tmp_path: Path):
    from engine.stages import build

    ctx = _ctx(pack)
    ctx.store.save_item(make_record(text="secret page text", raw={"x": 1}).to_json(include_text=True))
    a = build.run(ctx, tmp_path / "a")
    b = build.run(ctx, tmp_path / "b")
    assert a["version_hash"] == b["version_hash"] and a["code_hash"] == b["code_hash"]
    detail = json.loads((tmp_path / "a" / "items" / "abc12345.json").read_text())
    assert "text" not in detail and "raw" not in detail and detail["similar"] == []
    assert (tmp_path / "a" / "changelog" / "index.json").exists()
    assert "<title>Open Tab</title>" in (tmp_path / "a" / "index.html").read_text(encoding="utf-8")
    manifest = json.loads((tmp_path / "a" / "manifest.json").read_text())
    assert manifest["theme"]["default_color_mode"] == "system" and manifest["license"]


def test_review_lists_queue_most_confident_first(pack, monkeypatch):
    from engine import pipeline

    store = Store(pack)
    store.save_review(make_record(id="lo000001", url="https://lo.app/", title="Lo"), 0.61)
    store.save_review(make_record(id="hi000001", url="https://hi.app/", title="Hi"), 0.8)
    monkeypatch.setattr(pipeline, "_context", lambda path, dry_run=False: _ctx(pack))
    out = pipeline.cmd_review("unused")
    assert out["waiting"] == 2 and [r["title"] for r in out["items"]] == ["Hi", "Lo"]


def test_generic_json_source_resolves_relative_urls_and_listing_facts(monkeypatch):
    from engine.adapters import api_json
    from engine.stages.qualify import provenance_entries

    rows = [{"url": "/tools/fit/", "title": "Fit", "description": "A plan."}, {"title": "no url"}]
    monkeypatch.setattr(api_json, "get_json", lambda *a, **k: rows)
    src = {"id": "lib", "preset": "generic", "mapping": {"url": "url", "title": "title", "text": "description"},
           "listing_url": "https://x.site/library", "listing_note": "Built with the help of LLMs."}
    [cand] = list(api_json.ApiJsonAdapter(src).fetch("https://x.site/data.json", None))
    assert cand.url == "https://x.site/tools/fit/"
    assert cand.text == "A plan.\n\nBuilt with the help of LLMs."
    assert provenance_entries(cand, "2026-10-23")[0]["url"] == "https://x.site/library"


def test_private_working_data_folder(tmp_path, monkeypatch):
    """Working data (review queue, run report, source list, …) lives in a private folder
    when the pack names one; build-only commands work without it, the rest refuse."""
    from engine import pipeline

    root = tmp_path / "proj"
    pack_dir = root / "packs" / "webapps"
    pack_dir.mkdir(parents=True)
    src = Path("packs/webapps").resolve()
    for name in ("schema.yml", "taxonomy.yml", "facets.yml", "rules.yml", "copy.yml", "decide.yml"):
        (pack_dir / name).write_text((src / name).read_text(encoding="utf-8"), encoding="utf-8")
    (pack_dir / "pack.yml").write_text("id: webapps\nprivate_data: private\n", encoding="utf-8")
    monkeypatch.delenv("CATALOGUE_PRIVATE_DIR", raising=False)

    p = load_pack(pack_dir)
    assert p.private_dir == root / "private" and p.private_missing and p.sources == []
    with pytest.raises(SystemExit, match="working-data folder not found"):
        pipeline._context(str(pack_dir))
    assert pipeline._context(str(pack_dir), private=False).pack.id == "webapps"   # build, validate

    (root / "private" / "manual").mkdir(parents=True)
    (root / "private" / "sources.yml").write_text(
        "sources:\n  - { id: picks, type: manual, file: manual/list.yml }\n", encoding="utf-8")
    p = load_pack(pack_dir)
    assert not p.private_missing and p.sources[0]["file"] == str(root / "private" / "manual" / "list.yml")
    store = pipeline._context(str(pack_dir)).store
    store.log_rejected("https://x.app/", "picks", "not-a-web-app")
    assert (root / "private" / "rejected.jsonl").exists() and not (root / "data" / "rejected.jsonl").exists()

    monkeypatch.setenv("CATALOGUE_PRIVATE_DIR", str(tmp_path / "elsewhere"))
    assert load_pack(pack_dir).private_dir == tmp_path / "elsewhere"
