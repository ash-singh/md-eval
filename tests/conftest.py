"""Test isolation: no network, no real key, no writes outside tmp_path.

Every AsyncTypeSafeClient is built on a MockTransport, so the SDK's real request
serialization and response parsing run, and `api.requests` counts calls. Answers are
generated per question type; tests steer them through `api.score`, `api.noul` and
`api.choice` (functions of the question id and body).
"""

import json
import tempfile

import httpx2
import pytest
from typesafe_sdk import AsyncTypeSafeClient

import md_eval.artifact_hook
import md_eval.decide
import md_eval.evaluate


class MockAPI:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.status = 200
        self.score = lambda qid, q: 2.0  # level, 0..len(criteria)-1
        self.confidence = lambda qid, q: 0.9
        self.noul = lambda qid, q: 0.05
        self.choice = lambda qid, q: next(iter(q["criteria"]))

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        if self.status != 200:
            return httpx2.Response(self.status, json={"error": {"message": "mock failure"}})
        answers = {}
        for qid, q in body["questions"].items():
            if q["type"] == "score":
                level = self.score(qid, q)
                n = len(q["criteria"])
                probs = {str(i): (1.0 if i == round(level) else 0.0) for i in range(n)}
                answers[qid] = {
                    "type": "score",
                    "score": float(level),
                    "confidence": self.confidence(qid, q),
                    "legend": {str(i): c for i, c in enumerate(q["criteria"])},
                    "probabilities": probs,
                }
            elif q["type"] == "noul":
                answers[qid] = {"type": "noul", "noul": self.noul(qid, q)}
            else:
                pick = self.choice(qid, q)
                others = (0.2 / (len(q["criteria"]) - 1)) if len(q["criteria"]) > 1 else 0.0
                probs = {k: (0.8 if k == pick else others) for k in q["criteria"]}
                answers[qid] = {"type": "choice", "choice": pick, "confidence": 0.8, "probabilities": probs}
        return httpx2.Response(
            200, json={"model": "mock", "answers": answers, "usage": {"input_tokens": 1, "output_tokens": 1}}
        )


@pytest.fixture(autouse=True)
def api(monkeypatch, tmp_path) -> MockAPI:
    mock = MockAPI()
    transport = httpx2.MockTransport(mock.handler)

    def client(**kwargs):
        kwargs.pop("transport", None)
        return AsyncTypeSafeClient(**{"api_key": "test-key", **kwargs}, transport=transport)

    for module in (md_eval.evaluate, md_eval.artifact_hook, md_eval.decide):
        monkeypatch.setattr(module, "AsyncTypeSafeClient", client)

    # The repo root has a real .env; run from tmp_path so find_dotenv can't reach it.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    monkeypatch.setenv("MD_EVAL_LOG", str(tmp_path / "log.jsonl"))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    return mock


@pytest.fixture
def log_events(tmp_path):
    def read() -> list[dict]:
        path = tmp_path / "log.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    return read
