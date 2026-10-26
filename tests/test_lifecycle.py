"""Lifecycle (P1-3, P1-4): fingerprint drift sends an item back to review; new items
are re-checked daily for a week."""
from __future__ import annotations

import pytest

from engine.http import FetchResult
from engine.stages import lifecycle

from .test_engine import _ctx, make_record, pack  # noqa: F401

PAGE = ('<html><head><title>Unit Wizard</title><meta property="og:site_name" content="Unit Wizard">'
        '<meta name="description" content="Convert cooking and engineering units with visual scales">'
        '</head><body>units</body></html>')
SPAM = ('<html><head><title>Best Casino Bonuses 2026</title>'
        '<meta name="description" content="Claim free spins and jackpot offers today"></head></html>')


def test_drift_rules():
    fp = lifecycle.fingerprint({"title": "Unit Wizard", "description": "Convert cooking units"}, "https://unit.app/")
    assert lifecycle.drift(fp, lifecycle.fingerprint({"title": "Unit Wizard 2", "description": "Convert cooking units fast"},
                                                     "https://www.unit.app/x")) is None
    assert lifecycle.drift(fp, {**fp, "host": "parking.example.com"}).startswith("host-changed")
    assert lifecycle.drift(fp, {**fp, "host": "app.unit.app"}) is None          # same site
    assert lifecycle.drift(fp, lifecycle.fingerprint({"title": "Casino bonuses", "description": "free spins jackpot"},
                                                     "https://unit.app/")) == "content-changed"


@pytest.fixture()
def published(pack):  # noqa: F811
    ctx = _ctx(pack)
    ctx.store.save_item({**make_record().to_json(), "dates": {"first_seen": "2026-09-01", "last_verified": "2026-09-01"},
                         "fingerprint": lifecycle.fingerprint(
                             {"title": "Unit Wizard", "site_name": "Unit Wizard",
                              "description": "Convert cooking and engineering units with visual scales"},
                             "https://example.app/")})
    return ctx


def _serve(monkeypatch, body, url="https://example.app/"):
    monkeypatch.setattr(lifecycle, "fetch", lambda u, client: FetchResult(200, body, url=url))


def test_unchanged_page_stays_published(published, monkeypatch):
    _serve(monkeypatch, PAGE)
    report = lifecycle.run(published)
    assert report["sent_to_review_for_drift"] == 0
    assert published.store.load_item("abc12345")["dates"]["last_verified"] == "2026-10-23"


def test_drifted_page_goes_to_review_and_approve_accepts_it(published, monkeypatch):
    from engine import pipeline

    _serve(monkeypatch, SPAM)
    assert lifecycle.run(published)["sent_to_review_for_drift"] == 1
    assert published.store.load_item("abc12345") is None                 # off the site
    review = published.pack.data_dir / "review" / "abc12345.json"
    assert review.exists() and "content-drift" in review.read_text()

    monkeypatch.setattr(pipeline, "_context", lambda path, dry_run=False: published)
    assert pipeline.cmd_approve("unused", "abc12345")["approved"]
    item = published.store.load_item("abc12345")
    assert item["fingerprint"]["title"] == "Best Casino Bonuses 2026" and "content-drift" not in item["flags"]


def test_redirect_to_another_site_is_drift(published, monkeypatch):
    _serve(monkeypatch, PAGE, url="https://domain-parking.example.net/lander")
    assert lifecycle.run(published)["sent_to_review_for_drift"] == 1


def test_new_items_are_checked_daily_old_ones_weekly(pack, monkeypatch):  # noqa: F811
    ctx = _ctx(pack)
    ctx.store.save_item({**make_record(id="new00001", url="https://n.app/").to_json(),
                         "dates": {"first_seen": "2026-10-20"}, "liveness": {"last_check": "2026-10-22"}})
    ctx.store.save_item({**make_record(id="old00001", url="https://o.app/").to_json(),
                         "dates": {"first_seen": "2026-08-01"}, "liveness": {"last_check": "2026-10-22"}})
    seen = []
    monkeypatch.setattr(lifecycle, "fetch", lambda u, c: seen.append(u) or FetchResult(500, error="x"))
    lifecycle.run(ctx)
    assert seen == ["https://n.app/"]


def test_blocked_or_busy_checks_never_take_an_app_down(published, monkeypatch):
    """403/429/5xx and timeouts are recorded, not counted; after a run of them the app
    stays listed and is named for a human ("needs a look"). Only a definite failure
    (404, domain gone) counts, and a success clears everything."""
    def answer(status=0, error=None):
        monkeypatch.setattr(lifecycle, "fetch", lambda u, c: FetchResult(status, error=error))

    store = published.store
    for i, (status, error) in enumerate([(403, None), (0, "ReadTimeout"), (503, None), (429, None)]):
        published.today = f"2026-11-0{i + 1}"
        answer(status, error)
        report = lifecycle.run(published, everything=True)
        item = store.load_item("abc12345")
        assert item["status"] == "active" and not item["liveness"].get("failures")
    assert item["liveness"]["unsure"] == 4 and item["liveness"]["reason"] == "http-429"
    assert [x["id"] for x in report["needs_look"]] == ["abc12345"] and report["failed"] == 0

    answer(404)                                    # a definite failure counts
    published.today = "2026-11-05"
    assert lifecycle.run(published, everything=True)["failed"] == 1
    assert store.load_item("abc12345")["liveness"]["failures"] == 1

    _serve(monkeypatch, PAGE)                      # back: everything cleared
    published.today = "2026-11-06"
    lifecycle.run(published, everything=True)
    assert store.load_item("abc12345")["liveness"] == {"failures": 0, "last_check": "2026-11-06"}


def test_failure_reasons():
    assert lifecycle.failure_reason(FetchResult(404)) == ("http-404", False)
    assert lifecycle.failure_reason(FetchResult(0, error="ConnectError")) == ("ConnectError", False)
    assert lifecycle.failure_reason(FetchResult(403, error="blocked-by-robots")) == ("blocked-by-robots", True)
    assert lifecycle.failure_reason(FetchResult(502)) == ("http-502", True)
