"""Decision-model pass (P1): the client contract, generic mapping from decide.yml,
reject rules, confidence, the claim-evidence rule and code-side cross-checks.
The Decisions endpoint is mocked; no key or network is needed."""
from __future__ import annotations

import json

import httpx
import pytest

from engine.llm.decisions import DecisionClient, DecisionError, build_request
from engine.stages import decide

from .test_engine import _ctx, make_record, pack  # noqa: F401  (fixture re-export)


def answers_for(questions, **over):
    """A well-formed response for every question; override per question id."""
    out = {}
    for qid, q in questions.items():
        if qid in over:
            out[qid] = over[qid]
        elif q["type"] == "noul":
            out[qid] = {"type": "noul", "noul": 0.1}
        elif q["type"] == "choice":
            first = next(iter(q["criteria"]))
            out[qid] = {"type": "choice", "choice": first, "probabilities": {first: 0.9}}
        else:
            out[qid] = {"type": "score", "score": 2, "probabilities": {"2": 1.0}}
    return out


def transport(handler):
    calls = []

    def wrapped(request: httpx.Request):
        calls.append(json.loads(request.content))
        return handler(request, len(calls))
    return httpx.MockTransport(wrapped), calls


def test_request_shape_and_answer_normalisation(tmp_path):
    qs = {"is_app": {"type": "noul", "instructions": "Is it?", "maps_to_ignored": 1},
          "cat": {"type": "choice", "instructions": "Which?", "criteria": {"a": "A", "b": "B"}},
          "q": {"type": "score", "instructions": "How good?", "criteria": ["bad", "ok", "great"]}}
    t, calls = transport(lambda req, n: httpx.Response(200, json={
        "model": "vendor/model-1-20261014",
        "answers": {"is_app": {"type": "noul", "noul": 0.9},
                    "cat": {"type": "choice", "choice": "b", "probabilities": {"a": 0.2, "b": 0.8}},
                    "q": {"type": "score", "score": 1.7, "probabilities": {"0": 0, "1": 0.3, "2": 0.7}}},
        "usage": {"cost": 0.00002}}))
    client = DecisionClient("vendor/model-1", cache_path=tmp_path / "c.json", transport=t)
    d = client.decide({"url": "https://a.app"}, qs)
    assert calls[0]["model"] == "vendor/model-1"
    assert "maps_to_ignored" not in calls[0]["questions"]["is_app"]   # only API keys are sent
    assert d.answers == {"is_app": 0.9, "cat": {"choice": "b", "probabilities": {"a": 0.2, "b": 0.8}},
                         "q": {"level": 2, "legend": None, "probabilities": {"0": 0, "1": 0.3, "2": 0.7}}}
    assert d.model == "vendor/model-1-20261014" and client.spent == pytest.approx(0.00002)
    client.decide({"url": "https://a.app"}, qs)                       # cached: no second call
    assert len(calls) == 1


def test_any_model_id_is_just_config(tmp_path):
    """Swapping the decider changes the request's model field and nothing else."""
    qs = {"x": {"type": "noul", "instructions": "?"}}
    a = build_request("typesafe/jev-1.14", {"s": 1}, qs)
    b = build_request("perplexity/pplx-decider-flash-v1.2", {"s": 1}, qs)
    assert {k: v for k, v in a.items() if k != "model"} == {k: v for k, v in b.items() if k != "model"}


def test_contract_violations_are_errors_not_defaults(tmp_path):
    qs = {"x": {"type": "noul", "instructions": "?"}}
    t, _ = transport(lambda req, n: httpx.Response(200, json={"answers": {"x": {"type": "choice", "choice": "y"}}}))
    with pytest.raises(DecisionError):
        DecisionClient("m", transport=t).decide({}, qs)
    t, _ = transport(lambda req, n: httpx.Response(200, json={"answers": {}}))
    with pytest.raises(DecisionError):
        DecisionClient("m", transport=t).decide({}, qs)


def test_4xx_is_not_retried_but_5xx_is(tmp_path):
    qs = {"x": {"type": "noul", "instructions": "?"}}
    t, calls = transport(lambda req, n: httpx.Response(400, text="not a decision model"))
    with pytest.raises(DecisionError, match="400"):
        DecisionClient("bad/model", transport=t).decide({}, qs)
    assert len(calls) == 1
    t, calls = transport(lambda req, n: httpx.Response(503) if n == 1 else
                         httpx.Response(200, json={"answers": {"x": {"type": "noul", "noul": 0.7}}}))
    assert DecisionClient("m", transport=t).decide({}, qs).answers["x"] == 0.7
    assert len(calls) == 2


def test_questions_expand_taxonomy_and_config_validates(pack):  # noqa: F811
    from engine.pipeline import _check_decide

    qs = decide.questions(pack)
    assert set(qs["category"]["criteria"]) == set(pack.categories)
    assert _check_decide(pack) == []
    pack.decide = {**pack.decide, "model": "~vendor/latest",
                   "maps": {**pack.decide["maps"], "nope": {"flag": "x"}},
                   "reject": {"missing_q": {"below": 0.1}}}
    errs = " | ".join(_check_decide(pack))
    assert "alias" in errs and "maps.nope" in errs and "reject.missing_q" in errs


def test_apply_maps_answers_rejects_and_scores(pack):  # noqa: F811
    qs = decide.questions(pack)
    rec = make_record(attrs={})
    ans = {qid: (0.1 if q["type"] == "noul" else None) for qid, q in qs.items()}
    ans.update({"is_web_app": 0.95, "is_free_to_use": 0.9, "is_chat_wrapper": 0.1, "needs_sign_in": 0.8,
                "is_game": 0.7, "is_open_source": 0.2,
                "category": {"choice": "games", "probabilities": {"games": 0.8}},
                "cost": {"choice": "paid", "probabilities": {"paid": 0.6}},
                "ai_claim": {"choice": "none", "probabilities": {"none": 0.9}},
                "listing_quality": {"level": 2, "probabilities": {}}})
    decide.apply(pack, rec, ans, "vendor/m-20261014")
    assert rec.category == "games" and "game" in rec.flags and "chat-wrapper" not in rec.flags
    assert rec.attrs["access"] == "required" and rec.attrs["cost"] == "freemium"
    assert rec.attrs["open_source"] is False
    assert rec.reasons == []
    assert rec.quality["confidence"] == pytest.approx(0.95 * 0.9 * 0.9, abs=1e-4)
    assert rec.quality["decide_model"] == "vendor/m-20261014"
    assert rec.quality["decisions"]["is_web_app"] == 0.95

    ans.update({"is_web_app": 0.1, "listing_quality": {"level": 0, "probabilities": {}}})
    pack.decide["reject"] = {**pack.decide["reject"], "listing_quality": {"level_below": 1}}  # score rules still work
    decide.apply(pack, rec, ans, "m")
    assert {"decide:is_web_app<0.25", "decide:listing_quality-level<1"} <= set(rec.reasons)


def test_ai_claim_needs_evidence_outside_the_page():
    rec = make_record(ai_claim="self-declared", raw={"source_title": "Show HN: My planner",
                                                    "source_text": "I made a planner."},
                      text="This app was built with Claude!", evidence=[])
    decide.require_claim_evidence(rec)
    assert rec.ai_claim == "inferred"          # only the page itself says so
    rec = make_record(ai_claim="self-declared", evidence=[],
                      raw={"source_title": "Show HN: I vibe coded a planner with Claude"})
    decide.require_claim_evidence(rec)
    assert rec.ai_claim == "self-declared"
    assert rec.evidence == [{"kind": "creator-statement", "url": "https://news.ycombinator.com/item?id=1"}]


def test_cross_check_caps_confidence_and_logs(pack):  # noqa: F811
    ctx = _ctx(pack)
    rec = make_record(attrs={"access": "none"}, raw={"signals": {"login_form": True}},
                      quality={"confidence": 0.97})
    assert decide.cross_check(ctx, rec) == ["access:none-but-login-detected"]
    assert rec.quality["confidence"] < decide.gate_thresholds(pack)["auto_publish"]
    line = json.loads((pack.data_dir / "disagreements.jsonl").read_text().splitlines()[0])
    assert line["checks"] == ["access:none-but-login-detected"]


def test_classifier_decide_path_and_fallback(pack, monkeypatch):  # noqa: F811
    from engine.stages import enrich, gate

    monkeypatch.setenv("OPENROUTER_API_KEY", "test")
    ctx = _ctx(pack)
    qs = decide.questions(pack)
    good = answers_for(qs, is_web_app={"type": "noul", "noul": 0.97},
                       is_free_to_use={"type": "noul", "noul": 0.95},
                       category={"type": "choice", "choice": "utilities", "probabilities": {"utilities": 0.9}})
    t, _ = transport(lambda req, n: httpx.Response(200, json={"model": "x-build", "answers": good}))
    clf = enrich.Classifier(ctx)
    clf.decider._transport = t
    written = []
    monkeypatch.setattr(clf.writer, "complete", lambda s, u, schema=None: written.append(schema) or {
        "subcategory": "converters", "tags": ["units"], "summary": "Converts units.", "why": "Shows its working."})
    rec = make_record(attrs={}, raw={"signals": {"offline_capable": True, "mobile_viewport": True}})
    clf.classify(rec)
    assert clf.stats["decide_calls"] == 1 and len(written) == 1
    assert rec.quality["classify_mode"] == "decide" and rec.subcategory == "converters"
    assert rec.attrs["offline"] is True and rec.attrs["device"] == ["desktop", "mobile"]  # detected
    out = gate.run(ctx, [rec])
    assert out["review"] == [rec]              # tier B never auto-publishes

    # A failing decision call is counted and falls back to the chat path, not silently to rules.
    clf.decider._transport, _ = transport(lambda req, n: httpx.Response(404, text="no such model"))
    clf.decider._cache.clear()
    monkeypatch.setattr(enrich, "_classify_llm", lambda ctx, llm, r: {**enrich._classify_rules(ctx, r), "_mode": "llm"})
    rec2 = make_record(id="b2", url="https://b.app/", attrs={})
    clf.classify(rec2)
    assert clf.stats["decide_errors"] == 1 and rec2.quality["classify_mode"] == "llm"


def test_rejected_by_pass_one_skips_the_text_pass(pack, monkeypatch):  # noqa: F811
    from engine.stages import enrich

    monkeypatch.setenv("OPENROUTER_API_KEY", "test")
    ctx = _ctx(pack)
    qs = decide.questions(pack)
    bad = answers_for(qs, is_web_app={"type": "noul", "noul": 0.05})
    t, _ = transport(lambda req, n: httpx.Response(200, json={"answers": bad}))
    clf = enrich.Classifier(ctx)
    clf.decider._transport = t
    monkeypatch.setattr(clf.writer, "complete", lambda *a, **k: pytest.fail("text pass ran"))
    rec = make_record(attrs={})
    clf.classify(rec)
    assert "decide:is_web_app<0.25" in rec.reasons





def test_eval_scores_rules_mode_from_labels(pack):  # noqa: F811
    import yaml

    from engine.eval import run_eval

    ctx = _ctx(pack)
    ctx.dry_run = True
    ctx.store.dry_run = True
    d = pack.data_dir / "eval"
    d.mkdir(parents=True)
    cases = [{"id": "r1", "url": "https://github.com/a/b", "page_meta": {"title": "repo"}, "text": "code"},
             {"id": "a1", "url": "https://calc.app/", "page_meta": {"title": "Unit converter calculator"},
              "text": "converter calculator for units", "signals": {"mobile_viewport": True}}]
    (d / "cases.jsonl").write_text("\n".join(json.dumps(c) for c in cases))
    (d / "labels.yml").write_text(yaml.safe_dump({"labels": {
        "r1": {"listable": False}, "a1": {"listable": True, "category": "utilities"}}}))
    run = run_eval(ctx, mode="rules")["runs"][0]
    assert run["keep_vs_reject_accuracy"] == 1.0          # repo rejected by rules.yml, app kept
    assert run["field_accuracy"]["category"] == 1.0
    assert not (pack.data_dir / "rejected.jsonl").exists()   # eval never writes decisions


def test_decisions_file_and_bulk_rules(pack, monkeypatch, tmp_path):  # noqa: F811
    from engine import pipeline, review

    ctx = _ctx(pack)
    monkeypatch.setattr(pipeline, "_context", lambda path, dry_run=False: ctx)
    ctx.store.save_review(make_record(id="aaaa0001", url="https://a.app/"), 0.9)
    ctx.store.save_review(make_record(id="bbbb0001", url="https://github.com/x/y"), 0.9)
    ctx.store.save_review(make_record(id="cccc0001", url="https://c.app/",
                                      quality={"classify_mode": "rules"}), 0.95)
    # bulk: rules.yml rejects the repo; rules-mode confidence never approves
    plan = review.bulk("p", approve_at=0.5, apply_rules=True)
    assert plan["reject"] == 1 and plan["approve"] == 0 and not plan["applied"]
    review.bulk("p", apply_rules=True, yes=True)
    assert not (pack.data_dir / "review" / "bbbb0001.json").exists()
    # the review page's decisions file
    f = tmp_path / "decisions.json"
    f.write_text(json.dumps({"decisions": [{"id": "aaaa0001", "action": "approve"},
                                           {"id": "cccc0001", "action": "reject", "reason": "paid-only"},
                                           {"id": "zzzz0001", "action": "approve"}]}))
    out = review.apply_decisions("p", str(f))
    assert (out["approved"], out["rejected"], out["missing"]) == (1, 1, 1)
    assert ctx.store.load_item("aaaa0001")["status"] == "active"
    assert "paid-only" in (pack.data_dir / "rejected.jsonl").read_text()
    page = review.write_page(ctx, tmp_path / "r" / "index.html")
    assert page["items"] == 0 and "</script>" in (tmp_path / "r" / "index.html").read_text()


def test_reclassify_keep_holds_items_in_review_without_logging(pack, monkeypatch):  # noqa: F811
    from engine import pipeline
    from engine.stages import enrich

    monkeypatch.setenv("OPENROUTER_API_KEY", "test")
    ctx = _ctx(pack)
    monkeypatch.setattr(pipeline, "_context", lambda path, dry_run=False: ctx)
    for i, url in (("keep0001", "https://keep.app/"), ("drop0001", "https://drop.app/")):
        rec = make_record(id=i, url=url, raw={"signals": {"final_host": url[8:-1]}, "page_meta": {"title": "x"}})
        ctx.store.save_review(rec, 0.5)
    qs = decide.questions(pack)
    bad = answers_for(qs, is_web_app={"type": "noul", "noul": 0.05})
    t, _ = transport(lambda req, n: httpx.Response(200, json={"answers": bad}))
    real_init = enrich.Classifier.__init__

    def init(self, c, decide_model=None):
        real_init(self, c, decide_model)
        self.decider._transport = t
    monkeypatch.setattr(enrich.Classifier, "__init__", init)
    out = pipeline.cmd_reclassify("p", keep=["keep0001"])
    assert out["rejected"] == 1 and out["kept_in_review"] == 1 and out["held_for_human"] == ["keep0001"]
    held = json.loads((pack.data_dir / "review" / "keep0001.json").read_text())
    assert held["quality"]["held_for_human"]["would_be"] == "rejected"
    log = (pack.data_dir / "rejected.jsonl").read_text()
    assert "drop.app" in log and "keep.app" not in log


def test_decision_exports_merge_latest_wins(tmp_path):
    from engine import review

    a = [{"id": "x", "action": "approve", "at": "2026-10-20T10:00"},
         {"id": "y", "action": "reject", "reason": "broken"},           # no timestamp: oldest
         {"id": "z", "action": "approve", "at": "2026-10-21T09:00"}]
    b = [{"id": "x", "action": "reject", "at": "2026-10-19T10:00"},     # older: loses
         {"id": "y", "action": "approve", "at": "2026-10-22T08:00"},    # newer: wins
         {"id": "z", "action": "clear", "at": "2026-10-22T09:00"}]      # an undo wins too
    got = {d["id"]: d["action"] for d in review.merge_decisions(a, b)}
    assert got == {"x": "approve", "y": "approve", "z": "clear"}
    f = tmp_path / "d.json"
    f.write_text(json.dumps({"pack": "p", "decisions": a}))
    assert len(review.read_decisions(f)) == 3


def test_page_meta_decodes_entities_once_or_twice():
    from engine.stages.enrich import extract_page_meta

    m = extract_page_meta('<meta property="og:title" content="Flowchart &amp;amp; Diagrams">'
                          '<meta name="description" content="Cut &amp;  paste">')
    assert m["title"] == "Flowchart & Diagrams" and m["description"] == "Cut & paste"
    assert extract_page_meta("<title>Tom &amp; Jerry &lt;3</title>")["title"] == "Tom & Jerry <3"


def test_no_dashes_in_titles_and_prose():
    from engine.util import no_dashes

    assert no_dashes("Seaquel — Fast SQL Client", title=True) == "Seaquel - Fast SQL Client"
    assert no_dashes("61 tools — merge, split, sign — plus OCR.") == "61 tools (merge, split, sign) plus OCR."
    assert no_dashes("No server needed — useful for debugging.") == "No server needed: useful for debugging."
    assert no_dashes("Note: it works — mostly.") == "Note: it works, mostly."
    assert no_dashes("Pages 1–5 only.") == "Pages 1–5 only."


def test_partial_run_keeps_other_sources_last_checked(pack, monkeypatch):  # noqa: F811
    from engine import pipeline
    from engine.config import write_json

    ctx = _ctx(pack)
    write_json(pack.data_dir / "last_report.json",
               {"date": "2026-10-23", "sources": {"hn-show": {"status": "ok", "found": 5}}})
    ctx.source_results = {"manual": {"status": "ok", "found": 1, "checked": "2026-10-24T10:00:00Z"}}
    merged = pipeline._merged_source_results(ctx)
    assert merged["hn-show"]["checked"] == "2026-10-23" and merged["manual"]["found"] == 1


def test_sources_page_is_local_with_three_columns(pack, tmp_path):  # noqa: F811
    from engine import review
    from engine.config import write_json

    ctx = _ctx(pack)
    pack.sources = [{"id": "hn-show", "type": "api_json", "trust": "B", "notes": "Show HN"}]
    write_json(pack.data_dir / "last_report.json",
               {"date": "2026-10-23", "sources": {"hn-show": {"status": "error", "error": "timeout"}}})
    out = review.write_sources_page(ctx, tmp_path / "review" / "sources.html")
    html = (tmp_path / "review" / "sources.html").read_text()
    assert out["sources"] == 1 and "2026-10-23" in html and "Last check failed: timeout" in html
    assert "<th>Source</th><th>Last checked</th><th class=num>Items</th>" in html and "Trust" not in html
