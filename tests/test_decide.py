import asyncio
import json
from pathlib import Path

import pytest
from typesafe_sdk import AsyncTypeSafeClient

import md_eval.decide as decide_mod
from md_eval.cli import main
from md_eval.decide import DecisionError, Thresholds, decide, validate

EXAMPLE = Path(__file__).parent.parent / "examples" / "decisions" / "profile-cache.json"


@pytest.fixture
def decision() -> dict:
    d = json.loads(EXAMPLE.read_text())
    d["constraints"].append("Must not require a schema change")
    return validate(d)


def run(decision, api, levels, veto=None, vague=None, needs=0.1, best=None, conf=0.9):
    """levels: score level per option index. veto/vague: {option index: P}."""
    veto, vague = veto or {}, vague or {}
    api.score = lambda qid, q: levels[int(qid.split(":")[1])]
    api.confidence = conf if callable(conf) else (lambda qid, q: conf)

    def noul(qid, q):
        kind, *idx = qid.split(":")
        if kind == "veto":
            return veto.get(int(idx[0]), 0.05)
        if kind == "vague":
            return vague.get(int(idx[0]), 0.05)
        return needs

    api.noul = noul
    api.choice = lambda qid, q: best or decision["options"][max(range(len(levels)), key=levels.__getitem__)]["id"]

    async def go():
        async with decide_mod.AsyncTypeSafeClient() as client:
            return await decide(decision, Thresholds(), client)

    return asyncio.run(go())


def test_clear_winner_proceeds(decision, api):
    r = run(decision, api, [4, 2, 3], veto={2: 0.9})
    assert (r.status, r.recommendation) == ("proceed", "redis")
    assert [o.id for o in r.ranking] == ["redis", "memory", "memcached"]
    assert not r.ranking[-1].viable
    assert set(r.reason_codes) == {"veto"}
    assert len(api.requests) == 1


def test_one_request_with_every_question(decision, api):
    run(decision, api, [4, 2, 3])
    questions = api.requests[0]["questions"]
    n_opt, n_crit, n_con = 3, len(decision["criteria"]), len(decision["constraints"])
    assert len(questions) == n_opt * n_crit + n_opt * n_con + n_opt + 2
    assert "none_fit" in questions["best"]["criteria"]
    # Weights are policy applied in code, never sent to the model.
    assert all("weight" not in c for c in api.requests[0]["state"]["decision"]["criteria"])


@pytest.mark.parametrize(
    "kwargs, code",
    [
        ({"levels": [3, 3, 1]}, "close_margin"),
        ({"levels": [4, 1, 1], "needs": 0.8}, "needs_user"),
        ({"levels": [4, 1, 1], "best": "memory"}, "holistic_disagrees"),
        ({"levels": [4, 1, 1], "best": "none_fit"}, "none_fit"),
        ({"levels": [4, 1, 1], "conf": 0.3}, "could_flip"),
    ],
)
def test_ask_user(decision, api, kwargs, code):
    r = run(decision, api, **kwargs)
    assert r.status == "ask_user"
    assert code in r.reason_codes
    assert r.recommendation == "redis"


def test_uncertain_runner_up_far_behind_does_not_ask(decision, api):
    # One of the runner-up's criteria is uncertain, but even at its best it can't catch the leader.
    def conf(qid, q):
        return 0.3 if qid == "score:1:0" else 0.9

    r = run(decision, api, [4, 1, 0], conf=conf)
    assert r.status == "proceed", r.reasons


def test_vague_option_needs_clarifying(decision, api):
    r = run(decision, api, [4, 1, 1], vague={1: 0.9})
    assert (r.status, r.recommendation) == ("clarify_options", None)


def test_all_vetoed(decision, api):
    r = run(decision, api, [4, 1, 1], veto={0: 0.9, 1: 0.9, 2: 0.9})
    assert (r.status, r.recommendation) == ("no_viable_option", None)
    assert "all_vetoed" in r.reason_codes


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda d: d.pop("problem"), "problem"),
        (lambda d: d.update(options=d["options"][:1]), "at least 2 options"),
        (lambda d: d["options"].append(dict(d["options"][0])), "duplicated"),
        (lambda d: d["options"].append({"id": "none_fit", "summary": "x"}), "reserved"),
        (lambda d: d.update(criteria=[]), "at least 1 criterion"),
        (lambda d: d["criteria"][0].update(weight=0), "positive"),
        (lambda d: d.update(constraints="no"), "constraints"),
    ],
)
def test_invalid_input(mutate, message):
    d = json.loads(EXAMPLE.read_text())
    mutate(d)
    with pytest.raises(DecisionError, match=message):
        validate(d)


def test_cli_json_and_log(api, capsys, log_events, tmp_path):
    api.score = lambda qid, q: {0: 4, 1: 2, 2: 3}[int(qid.split(":")[1])]
    api.choice = lambda qid, q: "redis"
    assert main(["decide", str(EXAMPLE), "--json", "-"]) == 0
    out = json.loads(capsys.readouterr().out)  # stdout is exactly the JSON
    assert out["status"] == "proceed" and out["recommendation"] == "redis"
    [event] = log_events()
    assert event["tool"] == "decide" and event["status"] == "proceed"
    assert [o["id"] for o in event["options"]] == ["redis", "memcached", "memory"]
    # The log holds a hash of the problem, never its text.
    assert "latency" not in json.dumps(event)


def test_cli_invalid_input_exits_2(tmp_path, api):
    bad = tmp_path / "bad.json"
    bad.write_text('{"problem": "x"}')
    assert main(["decide", str(bad)]) == 2
    assert api.requests == []


def test_folder_named_decide_is_evaluated_not_dispatched(tmp_path, api, capsys):
    (tmp_path / "decide").mkdir()
    (tmp_path / "decide" / "spec.md").write_text("# Spec\n\nDo the thing.")
    assert main(["decide", "--dry-run"]) == 0
    assert "spec.md" in capsys.readouterr().out


def test_real_client_class_is_not_used(api):
    # Guard for the isolation fixture itself.
    assert decide_mod.AsyncTypeSafeClient is not AsyncTypeSafeClient


def test_eval_cases_are_valid():
    cases = sorted((Path(__file__).parent.parent / "evals" / "decide").glob("*.json"))
    assert len(cases) >= 10
    statuses = {"proceed", "ask_user", "clarify_options", "no_viable_option"}
    for path in cases:
        case = json.loads(path.read_text())
        validate(case)
        ids = {o["id"] for o in case["options"]}
        expected = case["expected"]
        assert set(expected.get("acceptable", [])) <= ids, path.name
        assert set(expected.get("status", [])) <= statuses, path.name
        assert expected.get("acceptable") or expected.get("status"), path.name
        assert expected.get("why"), path.name
