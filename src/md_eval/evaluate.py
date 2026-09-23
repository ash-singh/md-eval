"""Build TypeSafe requests for a document and turn answers into results."""

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path

from typesafe_sdk import AsyncTypeSafeClient, Noul, Score, TypeSafeError

from .dimensions import FLAGS, SCORES, Dimension, Reader, scores_for

# jev: 32k tokens for state + the longest question. chars/4 is a rough estimate,
# so leave headroom rather than risk a rejected request.
MAX_STATE_TOKENS = 28_000


def estimate_tokens(text: str) -> int:
    return len(text) // 4 + 1


def build_state(doc: Path, text: str, audience: str | None) -> dict:
    state: dict = {"document": {"filename": doc.name, "text": text}}
    if audience:
        state["intended_audience"] = audience
    return state


def build_question(dim: Dimension) -> Score | Noul:
    if dim.is_flag:
        return Noul(instructions=dim.instructions)
    return Score(instructions=dim.instructions, criteria=list(dim.criteria))


def build_questions(dims: tuple[Dimension, ...]) -> dict[str, Score | Noul]:
    return {d.id: build_question(d) for d in dims}


def request_tokens(state: dict, dims: tuple[Dimension, ...]) -> int:
    longest_q = max(len(json.dumps(build_question(d).model_dump())) for d in dims)
    return estimate_tokens(json.dumps(state)) + longest_q // 4


@dataclass
class ScoreResult:
    score: float  # normalized 0-1
    level: float  # raw position, 0 .. len(criteria)-1
    confidence: float
    probabilities: dict[int, float]


@dataclass
class DocResult:
    path: str
    tokens: int
    scores: dict[str, ScoreResult] = field(default_factory=dict)
    flags: dict[str, float] = field(default_factory=dict)  # P(yes)
    model: str | None = None
    input_tokens: int | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def group_score(self, group: str, weights: dict[str, float]) -> float:
        dims = [d for d in SCORES if d.group == group]
        return _weighted(self.scores, dims, weights)

    def total(self, reader: Reader, weights: dict[str, float]) -> float:
        return _weighted(self.scores, scores_for(reader), weights)

    def raised_flags(self, threshold: float) -> list[str]:
        return [d.id for d in FLAGS if self.flags.get(d.id, 0.0) >= threshold]

    def low_confidence(self, threshold: float) -> list[str]:
        return [k for k, s in self.scores.items() if s.confidence < threshold]


def _weighted(scores: dict[str, ScoreResult], dims, weights: dict[str, float]) -> float:
    total_w = sum(weights[d.id] for d in dims)
    if not total_w:
        return 0.0
    return sum(scores[d.id].score * weights[d.id] for d in dims) / total_w


async def evaluate_doc(
    client: AsyncTypeSafeClient,
    sem: asyncio.Semaphore,
    doc: Path,
    text: str,
    audience: str | None,
    dims: tuple[Dimension, ...],
) -> DocResult:
    state = build_state(doc, text, audience)
    tokens = request_tokens(state, dims)
    result = DocResult(path=str(doc), tokens=tokens)
    if tokens > MAX_STATE_TOKENS:
        result.error = f"too long (~{tokens:,} tokens > {MAX_STATE_TOKENS:,}); not evaluated"
        return result

    async with sem:
        try:
            response = await client.system_one(state, build_questions(dims))
        except TypeSafeError as e:
            result.error = f"{type(e).__name__}: {e}"
            return result

    fill_result(result, dims, response)
    return result


def fill_result(result: DocResult, dims: tuple[Dimension, ...], response) -> None:
    """Copy the answers for `dims` from a TypeSafe response into `result`."""
    result.model = response.model
    result.input_tokens = response.usage.input_tokens
    for d in dims:
        answer = response.answers[d.id]
        if d.is_flag:
            result.flags[d.id] = answer.noul
        else:
            result.scores[d.id] = ScoreResult(
                score=answer.score / (len(d.criteria) - 1),
                level=answer.score,
                confidence=answer.confidence,
                probabilities=dict(answer.probabilities),
            )


async def evaluate_all(
    docs: list[tuple[Path, str]],
    audience: str | None,
    dims: tuple[Dimension, ...],
    concurrency: int,
    model: str | None,
) -> list[DocResult]:
    sem = asyncio.Semaphore(concurrency)
    async with AsyncTypeSafeClient(model=model) as client:
        return await asyncio.gather(
            *(evaluate_doc(client, sem, path, text, audience, dims) for path, text in docs)
        )
