import io
import json

import pytest

from md_eval import artifact_hook, plan_hook
from md_eval.artifact_hook import extract_text, target_file

THIN_PLAN = "# Plan\n\nAdd caching to the API so it is faster."
PROSE = " ".join(["This sentence carries a point about checkout latency and what we changed."] * 25)


def payload(tool_input: dict, session: str = "s1", cwd: str | None = None) -> dict:
    return {"session_id": session, "cwd": cwd or "/", "tool_input": tool_input}


def denied(out) -> bool:
    return bool(out) and out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


def run_main(module, stdin: str, monkeypatch, capsys) -> str:
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    with pytest.raises(SystemExit) as exit_:
        module.main()
    assert exit_.value.code == 0  # hooks always fail open
    return capsys.readouterr().out


# ---- plan gate -------------------------------------------------------------------


def test_plan_thin_is_sent_back_once_then_allowed(api, log_events):
    api.score = lambda qid, q: 0.0
    out = plan_hook.run(payload({"plan": THIN_PLAN}))
    assert denied(out)
    assert "Weakest dimensions" in out["hookSpecificOutput"]["permissionDecisionReason"]
    again = plan_hook.run(payload({"plan": THIN_PLAN}))
    assert not denied(again) and "scored" in again["systemMessage"]
    assert [e["outcome"] for e in log_events()] == ["sent_back", "allowed_after_limit"]


def test_plan_strong_passes(api, log_events):
    api.score = lambda qid, q: 4.0
    out = plan_hook.run(payload({"plan": THIN_PLAN}))
    assert not denied(out)
    [event] = log_events()
    assert event["outcome"] == "passed" and event["total"] == 1.0
    assert THIN_PLAN not in json.dumps(event)  # only hashes of the text


def test_plan_flag_sends_back_despite_high_score(api):
    api.score = lambda qid, q: 4.0
    api.noul = lambda qid, q: 0.9 if qid == "secrets" else 0.05
    out = plan_hook.run(payload({"plan": THIN_PLAN}))
    assert denied(out) and "Secrets" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_plan_empty_is_silent(api):
    assert plan_hook.run(payload({"plan": "  "})) is None
    assert api.requests == []


def test_plan_missing_key_notice_once(api, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY")
    first = plan_hook.run(payload({"plan": THIN_PLAN}))
    assert "TYPESAFE_API_KEY not set" in first["systemMessage"]
    assert plan_hook.run(payload({"plan": THIN_PLAN})) is None
    assert api.requests == []


# ---- artifact gate: text extraction ---------------------------------------------


def test_extract_keeps_structure_and_drops_code(tmp_path):
    page = tmp_path / "p.html"
    page.write_text(
        "<html><head><title>Q3 report</title><style>h1{}</style></head><body>"
        "<h1>Results</h1><p><strong>Summary:</strong> p95 fell.</p>"
        "<ul><li>Pooling</li><li>Caching</li></ul><script>var secret=1</script><svg><text>chart</text></svg>"
        "</body></html>"
    )
    text = extract_text(page)
    assert text.startswith("Page title: Q3 report")
    assert "# Results" in text and "- Pooling" in text and "- Caching" in text
    assert "Summary: p95 fell." in text  # space kept across the inline tag
    assert "secret" not in text and "chart" not in text and "h1{}" not in text


def test_extract_markdown_is_unchanged(tmp_path):
    page = tmp_path / "p.md"
    page.write_text("# Title\n\nBody")
    assert extract_text(page) == "# Title\n\nBody"


# ---- artifact gate: which calls are checked --------------------------------------


@pytest.fixture
def page(tmp_path):
    path = tmp_path / "report.html"
    path.write_text(f"<html><body><h1>Report</h1><p>{PROSE}</p></body></html>")
    return path


@pytest.mark.parametrize(
    "tool_input",
    [
        {"action": "list"},
        {"action": "read", "url": "https://claude.ai/code/artifact/x"},
        {"action": "delete", "url": "u"},
        {"action": "publish", "url": "u", "asset": True, "file_path": "PAGE"},
        {"type_url": "https://x", "title": "t"},
        {"from_url": "https://x", "asset_ids": ["a"], "url": "u", "asset": True},
        {"file_path": "missing.html"},
        {"file_path": "notes.txt"},
    ],
)
def test_artifact_skips_without_api_call_even_without_key(tool_input, page, api, monkeypatch, tmp_path):
    monkeypatch.delenv("TYPESAFE_API_KEY")
    (tmp_path / "notes.txt").write_text(PROSE)
    tool_input = {k: (str(page) if v == "PAGE" else v) for k, v in tool_input.items()}
    assert artifact_hook.run(payload(tool_input, cwd=str(tmp_path))) is None
    assert api.requests == []


def test_artifact_relative_path_resolves_against_cwd(page, tmp_path):
    assert target_file(payload({"file_path": "report.html"}, cwd=str(tmp_path))) == page


def test_artifact_short_page_is_skipped(tmp_path, api):
    short = tmp_path / "short.html"
    short.write_text("<p>Hello world</p>")
    assert artifact_hook.run(payload({"file_path": str(short)})) is None
    assert api.requests == []


# ---- artifact gate: outcomes -------------------------------------------------------


def test_artifact_weak_page_sent_back_once_then_revision_scored_and_allowed(page, api, log_events):
    api.score = lambda qid, q: 0.0
    api.noul = lambda qid, q: 0.9 if qid == "is_written_document" else 0.05
    first = artifact_hook.run(payload({"file_path": str(page)}))
    assert denied(first)
    reason = first["hookSpecificOutput"]["permissionDecisionReason"]
    assert "Do not rewrite content the user supplied" in reason

    # Republishing unchanged text: no API call, no second send-back.
    assert artifact_hook.run(payload({"file_path": str(page)})) is None
    assert len(api.requests) == 1

    # A revision is scored and logged, then allowed (budget spent).
    page.write_text(page.read_text().replace("Report", "Report, revised"))
    api.score = lambda qid, q: 3.0
    revised = artifact_hook.run(payload({"file_path": str(page)}))
    assert not denied(revised)
    assert [e["outcome"] for e in log_events()] == ["sent_back", "passed"]


def test_artifact_strong_page_passes(page, api):
    api.score = lambda qid, q: 4.0
    api.noul = lambda qid, q: 0.9 if qid == "is_written_document" else 0.05
    out = artifact_hook.run(payload({"file_path": str(page)}))
    assert not denied(out) and "scored 100/100" in out["systemMessage"]


def test_artifact_non_document_is_not_judged_as_prose(page, api):
    api.score = lambda qid, q: 0.0  # would fail as prose
    api.noul = lambda qid, q: 0.1  # not a written document, no flags
    out = artifact_hook.run(payload({"file_path": str(page)}))
    assert not denied(out) and "not scored as prose" in out["systemMessage"]


def test_artifact_secret_sends_back_any_page(page, api):
    api.score = lambda qid, q: 4.0
    api.noul = lambda qid, q: 0.95 if qid in {"secrets", "is_written_document"} else 0.05
    out = artifact_hook.run(payload({"file_path": str(page)}))
    assert denied(out) and "credential" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_artifact_unsupported_claims_alone_does_not_block(page, api):
    api.score = lambda qid, q: 4.0
    api.noul = lambda qid, q: 0.95 if qid in {"unsupported_claims", "is_written_document"} else 0.05
    assert not denied(artifact_hook.run(payload({"file_path": str(page)})))


def test_artifact_block_budget_is_per_file(page, tmp_path, api):
    api.score = lambda qid, q: 0.0
    api.noul = lambda qid, q: 0.9 if qid == "is_written_document" else 0.05
    other = tmp_path / "other.html"
    other.write_text(page.read_text())
    assert denied(artifact_hook.run(payload({"file_path": str(page)})))
    assert denied(artifact_hook.run(payload({"file_path": str(other)})))


# ---- fail open ------------------------------------------------------------------------


@pytest.mark.parametrize("module", [plan_hook, artifact_hook])
def test_malformed_stdin_exits_0_silently(module, monkeypatch, capsys):
    assert run_main(module, "not json", monkeypatch, capsys) == ""


def test_api_failure_lets_plan_through(api, monkeypatch, capsys, log_events):
    api.status = 500
    out = run_main(plan_hook, json.dumps(payload({"plan": THIN_PLAN})), monkeypatch, capsys)
    assert out == ""
    assert log_events() == []


def test_api_failure_lets_page_through(page, api, monkeypatch, capsys):
    api.status = 500
    out = run_main(artifact_hook, json.dumps(payload({"file_path": str(page)})), monkeypatch, capsys)
    assert out == ""


def test_hook_stdout_is_one_json_object(api, monkeypatch, capsys):
    api.score = lambda qid, q: 0.0
    out = run_main(plan_hook, json.dumps(payload({"plan": THIN_PLAN})), monkeypatch, capsys)
    assert denied(json.loads(out))
