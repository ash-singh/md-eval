import json
import re
import subprocess
import tomllib
from pathlib import Path

import pytest

from md_eval.cli import main
from md_eval.log import log_event, log_path, summarize

ROOT = Path(__file__).parent.parent


# ---- decision log ----------------------------------------------------------------


def test_log_event_appends_json_lines(log_events):
    log_event("decide", status="proceed")
    log_event("decide", status="ask_user")
    events = log_events()
    assert [e["status"] for e in events] == ["proceed", "ask_user"]
    assert all(e["schema"] == 1 and e["tool"] == "decide" and e["ts"] for e in events)


def test_log_can_be_turned_off(monkeypatch, log_events):
    monkeypatch.setenv("MD_EVAL_LOG", "off")
    assert log_path() is None
    log_event("decide", status="proceed")
    assert log_events() == []


def test_log_default_path_respects_xdg(monkeypatch, tmp_path):
    monkeypatch.delenv("MD_EVAL_LOG")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    assert log_path() == tmp_path / "cache" / "md-eval" / "decisions.jsonl"


def test_log_failure_is_silent(monkeypatch, tmp_path, capsys):
    blocker = tmp_path / "file"
    blocker.write_text("")
    monkeypatch.setenv("MD_EVAL_LOG", str(blocker / "log.jsonl"))  # parent is a file
    log_event("decide", status="proceed")
    assert capsys.readouterr() == ("", "")


def test_summary_counts_outcomes_and_revisions():
    events = [
        {"ts": "t1", "tool": "plan-hook", "session": "a", "subject": "p", "total": 0.2, "outcome": "sent_back", "api_ms": 900},
        {"ts": "t2", "tool": "plan-hook", "session": "a", "subject": "p", "total": 0.7, "outcome": "allowed_after_limit", "api_ms": 1100},
        {"ts": "t3", "tool": "plan-hook", "session": "b", "subject": "p", "total": 0.9, "outcome": "passed", "api_ms": 1000},
        {"ts": "t4", "tool": "decide", "status": "ask_user", "reason_codes": ["close_margin"], "api_ms": 1500},
    ]
    text = summarize(events)
    assert "plan-hook: 3 runs" in text
    assert "revisions: 1 scored again, 1 improved, mean change +50 points" in text
    assert "decide: 1 runs" in text and "close_margin 1" in text
    assert "median 1000 ms" in text


def test_stats_subcommand(capsys, log_events):
    log_event("decide", status="proceed", api_ms=10)
    assert main(["stats"]) == 0
    out = capsys.readouterr().out
    assert "decide: 1 runs" in out and "proceed 1" in out


def test_stats_without_log(capsys):
    assert main(["stats"]) == 0
    assert "No log" in capsys.readouterr().out


def test_stats_json(capsys, log_events):
    log_event("plan-hook", session="a", subject="p", total=0.2, outcome="sent_back", api_ms=900)
    log_event("plan-hook", session="a", subject="p", total=0.7, outcome="allowed_after_limit", api_ms=1100)
    log_event("decide", status="proceed", reason_codes=["veto"], api_ms=10)
    assert main(["stats", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)  # stdout is only the JSON object
    assert data["events"] == 3
    plan = data["tools"]["plan-hook"]
    assert plan["runs"] == 2 and plan["outcomes"] == {"sent_back": 1, "allowed_after_limit": 1}
    assert plan["api_ms"] == {"median": 1000, "p90": 1100}
    assert plan["revisions"]["count"] == 1 and plan["revisions"]["improved"] == 1
    assert round(plan["revisions"]["mean_change"], 3) == 0.5
    decide = data["tools"]["decide"]
    assert decide["codes"] == {"veto": 1} and decide["mean_total"] is None and decide["revisions"] is None


@pytest.mark.parametrize("log", ["missing", "off"])
def test_stats_json_without_log(log, capsys, monkeypatch, tmp_path):
    if log == "off":
        monkeypatch.setenv("MD_EVAL_LOG", "off")
    assert main(["stats", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"events": 0, "tools": {}}


# ---- plugin manifests ------------------------------------------------------------------


def test_versions_and_pins_match_release():
    """The invariant scripts/release.sh maintains: one version, and every uvx source pinned to
    the same commit sha (the commit tagged v<version>). Releases up to v0.4.0 pinned the tag."""
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    manifests = list((ROOT / "plugins").glob("*/.claude-plugin/plugin.json"))
    assert len(manifests) >= 3
    for manifest in manifests:
        assert json.loads(manifest.read_text())["version"] == version, manifest

    pins = set()
    for path in (ROOT / "plugins").rglob("*"):
        if path.is_file():
            pins.update(re.findall(r"git\+https://github\.com/ash-singh/md-eval(@[^ `\"]+)?", path.read_text()))
    assert len(pins) == 1, pins
    [pin] = pins
    if pin == f"@v{version}":
        return
    assert re.fullmatch(r"@[0-9a-f]{40}", pin), pin
    # Where tags are available (not in a shallow CI checkout), the sha must be the tagged commit.
    tagged = subprocess.run(
        ["git", "rev-parse", "-q", "--verify", f"refs/tags/v{version}^{{commit}}"],
        cwd=ROOT, capture_output=True, text=True,
    )
    if tagged.returncode == 0:
        assert pin[1:] == tagged.stdout.strip()


def test_marketplace_lists_every_plugin():
    marketplace = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    listed = {p["name"]: p["source"] for p in marketplace["plugins"]}
    on_disk = {p.parent.parent.name for p in (ROOT / "plugins").glob("*/.claude-plugin/plugin.json")}
    assert set(listed) == on_disk
    for name, source in listed.items():
        manifest = json.loads((ROOT / source / ".claude-plugin" / "plugin.json").read_text())
        assert manifest["name"] == name


def test_hook_commands_fail_open():
    for hooks in (ROOT / "plugins").glob("*/hooks/hooks.json"):
        for entries in json.loads(hooks.read_text())["hooks"].values():
            for entry in entries:
                for hook in entry["hooks"]:
                    assert hook["command"].endswith("|| true"), hooks
