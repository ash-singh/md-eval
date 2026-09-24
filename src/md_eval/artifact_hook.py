"""Claude Code PreToolUse hook: check a page with md-eval before the Artifact tool publishes it.

Scores only a publish of one `.html`, `.htm` or `.md` file. Everything else (read, list,
asset uploads, typed artifacts) passes without an API call. The page's text is extracted
with its headings and list items kept, then one request asks the page-reader dimensions,
the flags, and whether the page is a written document at all. Apps, dashboards and games
are not judged as prose.

A page is sent back when it is a written document that scores below the threshold, or
when a blocking flag is raised (by default secrets, PII, unfinished content and
contradictions; unsupported claims are only mentioned). Each file is sent back at most
MD_EVAL_ARTIFACT_MAX_BLOCKS times per session (default 1), so publishing again after
that goes through (a revised page is still scored and logged, then allowed). Unchanged
text is never scored twice. Fails open on any error.

Claude Docs writes (`mcp__claude_ai_Claude_Docs__batch` / `update`) are checked for blocking
flags only (by default secrets and PII): a write often carries a skeleton or one section,
so it is not scored as prose. The same once-per-session budget applies, per doc.

Skips of a call that would publish content (typed artifact, too short, unchanged, no key)
are logged with outcome `skipped` and a reason code, so a skip is not mistaken for a hook
that never ran.
"""

import asyncio
import hashlib
import json
import os
import re
from html.parser import HTMLParser
from pathlib import Path

from typesafe_sdk import AsyncTypeSafeClient, Noul, TypeSafeError

from .dimensions import DIMENSIONS, FLAGS, dimensions_for
from .evaluate import MAX_STATE_TOKENS, DocResult, build_questions, build_state, evaluate_doc, fill_result, request_tokens
from .hookutil import check_failed, deny, env_float, flag_label, log_check, missing_key_notice, run_hook, session_file, weakest_lines
from .log import Timer, log_event, short_hash

HOOK = "artifact-hook"
READERS = ("page",)
EXTENSIONS = {".html", ".htm", ".md", ".markdown"}
DEFAULT_BLOCKING_FLAGS = "secrets,pii,unfinished_content,contradictions"
DOCS_TOOLS = {"mcp__claude_ai_Claude_Docs__batch", "mcp__claude_ai_Claude_Docs__update"}
DEFAULT_DOCS_BLOCKING_FLAGS = "secrets,pii"
# Keys whose string values are text written into a doc. `target`/`anchor` quote text already
# there, and `intent` describes a pending section, so those subtrees are left out.
DOCS_TEXT_KEYS = {"markdown", "content", "text", "body", "payload"}
DOCS_SKIP_KEYS = {"target", "anchor", "intent", "to"}
CHIP_TOKEN = re.compile(r"<\?claude block [^?]*\?>")

IS_DOCUMENT = "is_written_document"
IS_DOCUMENT_QUESTION = Noul(
    instructions="Is the page in `document.text` mainly written prose meant to be read, such as a "
    "report, write-up, explainer, proposal or guide?",
    criteria={
        "true": "Most of the page is sentences and paragraphs a reader reads through",
        "false": "It is mainly an app, dashboard, tool, game, form, chart or data listing, "
        "with text only as labels or short captions",
    },
)


class _TextExtractor(HTMLParser):
    """Visible text of an HTML page, keeping headings (#) and list items (-) as Markdown."""

    SKIP = {"script", "style", "svg", "noscript", "template", "head"}
    BLOCK = {"p", "div", "section", "article", "main", "header", "footer", "aside", "nav", "br",
             "tr", "table", "blockquote", "pre", "figure", "figcaption", "dt", "dd", "hr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_depth = 0
        self.title: list[str] = []
        self.in_title = False

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "title":
            self.in_title = True
        if tag in self.SKIP:
            self.skip_depth += 1
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.parts.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self.in_title = False
        if tag in self.SKIP:
            self.skip_depth = max(0, self.skip_depth - 1)
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"} or tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title.append(data)
        elif not self.skip_depth:
            # Collapse whitespace but keep a boundary space, so "<b>Summary:</b> p95" stays two words.
            text = " ".join(data.split())
            lead = " " if data[:1].isspace() else ""
            trail = " " if data[-1:].isspace() and text else ""
            self.parts.append(lead + text + trail)

    def text(self) -> str:
        lines = [" ".join(line.split()) for line in "".join(self.parts).splitlines()]
        body = "\n".join(line for line in lines if line and line not in {"-", "#"})
        body = "\n".join(line for line in body.splitlines() if line.strip("#- "))
        title = " ".join("".join(self.title).split())
        return (f"Page title: {title}\n\n" if title else "") + body


def extract_text(path: Path) -> str:
    raw = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix.lower() in {".md", ".markdown"}:
        return raw
    parser = _TextExtractor()
    parser.feed(raw)
    return parser.text()


def target_file(payload: dict) -> Path | None:
    """The file a publish would put on the page, or None when this call is not one to check."""
    tool_input = payload.get("tool_input") or {}
    if tool_input.get("action", "publish") != "publish":
        return None
    if tool_input.get("asset") or tool_input.get("type_url") or tool_input.get("from_url"):
        return None
    file_path = tool_input.get("file_path")
    if not isinstance(file_path, str) or Path(file_path).suffix.lower() not in EXTENSIONS:
        return None
    path = Path(file_path)
    if not path.is_absolute():
        path = Path(payload.get("cwd") or ".") / path
    return path if path.is_file() else None


def docs_text(value, key: str | None = None) -> str:
    """The text a Claude Docs write puts into a doc, chip tokens removed."""
    if isinstance(value, str):
        return CHIP_TOKEN.sub("", value).strip() if key in DOCS_TEXT_KEYS else ""
    if isinstance(value, dict):
        parts = [docs_text(v, k) for k, v in value.items() if k not in DOCS_SKIP_KEYS]
    elif isinstance(value, list):
        parts = [docs_text(v, key) for v in value]
    else:
        return ""
    return "\n\n".join(p for p in parts if p)


def docs_subject(tool_input: dict) -> str:
    """The doc a write goes to: its id, or the name of a doc being created."""
    container = tool_input.get("container") or {}
    if container.get("id"):
        return f"docs:{container['id']}"
    create = container.get("create") or {}
    return f"docs:new:{create.get('name', '')}"


def log_skip(session_id: str, subject: str, reason: str) -> None:
    log_event(HOOK, session=short_hash(session_id), subject=short_hash(subject), outcome="skipped", reason_codes=[reason])


def _feedback(result: DocResult, total: float, is_doc: bool, blocking: list[str], min_score: float, min_conf: float,
              what: str = "page") -> str:
    lines = []
    if is_doc and total < min_score:
        lines.append(
            f"md-eval (TypeSafe Jev) scored this page {total * 100:.0f}/100 for its readers "
            f"(threshold {min_score * 100:.0f})."
        )
        lines += weakest_lines(result, min_conf, "a strong page")
    if blocking:
        lines.append("Raised flags: " + ", ".join(f"{flag_label(f)} (P={result.flags[f]:.2f})" for f in blocking))
    advice = []
    if "secrets" in blocking:
        advice.append("remove anything that looks like a real credential")
    if "unfinished_content" in blocking:
        advice.append("finish or remove placeholder, TBD or TODO content")
    if "contradictions" in blocking:
        advice.append("resolve the contradicting statements")
    if "pii" in blocking:
        advice.append("ask the user whether the personal details are meant to be on the page before removing them")
    if advice:
        lines.append(("Before publishing: " if what == "page" else "Before saving: ") + "; ".join(advice) + ".")
    retry = "publish again" if what == "page" else "retry the edit"
    lines.append(
        f"Improve the {what} where you wrote the content yourself. Do not rewrite content the user "
        f"supplied or asked for verbatim, and do not invent facts. If the user wants the {what} "
        f"as it is, {retry}: it will not be sent back again this session. These are model "
        "judgments, not certainties."
    )
    return "\n".join(lines)


async def _score(path: Path, text: str) -> tuple[DocResult, float]:
    dims = dimensions_for(READERS)
    state = build_state(path, text, None)
    result = DocResult(path=str(path), tokens=request_tokens(state, dims))
    if result.tokens > MAX_STATE_TOKENS:
        result.error = "too long"
        return result, 0.0
    questions = {**build_questions(dims), IS_DOCUMENT: IS_DOCUMENT_QUESTION}
    async with AsyncTypeSafeClient() as client:
        try:
            response = await client.system_one(state, questions)
        except TypeSafeError as e:
            result.error = f"{type(e).__name__}: {e}"
            return result, 0.0
    fill_result(result, dims, response)
    return result, response.answers[IS_DOCUMENT].noul


async def _check_flags(text: str) -> DocResult:
    async with AsyncTypeSafeClient() as client:
        return await evaluate_doc(client, asyncio.Semaphore(1), Path("doc.md"), text, None, FLAGS)


def run_docs(payload: dict) -> dict | None:
    """Flags-only check of a Claude Docs write; sent back at most once per doc per session."""
    tool_input = payload.get("tool_input") or {}
    session_id = str(payload.get("session_id", ""))
    subject = docs_subject(tool_input)
    text = docs_text(tool_input)
    if not text:  # delete, move, rename, tab patch: nothing written
        return None
    if len(text.split()) < env_float("MD_EVAL_DOCS_MIN_WORDS", 20):
        log_skip(session_id, subject, "too_short")
        return None
    notice = missing_key_notice(HOOK, session_id, "doc")
    if notice is not None:
        log_skip(session_id, subject, "no_key")
        return notice or None

    key = hashlib.sha256(subject.encode()).hexdigest()[:16]
    state_file = session_file(HOOK, session_id, f"{key}.json")
    state = json.loads(state_file.read_text()) if state_file.exists() else {"blocks": 0, "text": ""}
    text_hash = hashlib.sha256(text.encode()).hexdigest()
    if state["text"] == text_hash:
        log_skip(session_id, subject, "unchanged")
        return None
    state["text"] = text_hash
    state_file.write_text(json.dumps(state))

    with Timer() as timer:
        result = asyncio.run(_check_flags(text))
    if not result.ok:
        return check_failed(HOOK, session_id, subject, text, result.error, timer.ms, "doc")

    flag_threshold = env_float("MD_EVAL_ARTIFACT_FLAG_THRESHOLD", 0.5)
    blocking_ids = {f.strip() for f in os.environ.get("MD_EVAL_DOCS_BLOCK_FLAGS", DEFAULT_DOCS_BLOCKING_FLAGS).split(",")}
    blocking = [f for f in result.raised_flags(flag_threshold) if f in blocking_ids]
    max_blocks = int(env_float("MD_EVAL_ARTIFACT_MAX_BLOCKS", 1))
    blocks = state["blocks"]
    outcome = "passed" if not blocking else "allowed_after_limit" if blocks >= max_blocks else "sent_back"
    # No prose score for a doc edit, so no `total` (it would drag the page mean in `md-eval stats`).
    log_event(HOOK, session=short_hash(session_id), subject=short_hash(subject), text=short_hash(text),
              flags=blocking, outcome=outcome, blocks=blocks, api_ms=timer.ms, docs=True)
    if outcome == "passed":
        return None  # silent, so each section fill doesn't print a message
    labels = ", ".join(flag_label(f) for f in blocking)
    if outcome == "allowed_after_limit":
        return {"systemMessage": f"md-eval: doc edit raised flags ({labels}), allowed (send-back limit reached)"}

    state["blocks"] += 1
    state_file.write_text(json.dumps(state))
    return deny(f"md-eval: doc edit raised flags ({labels}) — sent back",
                _feedback(result, 1.0, False, blocking, 0.0, 0.0, "doc"))


def run(payload: dict) -> dict | None:
    tool_name = payload.get("tool_name", "Artifact")
    if tool_name in DOCS_TOOLS:
        return run_docs(payload)
    if tool_name != "Artifact":
        return None
    session_id = str(payload.get("session_id", ""))
    tool_input = payload.get("tool_input") or {}
    path = target_file(payload)
    if path is None:
        if tool_input.get("type_url") and tool_input.get("action", "publish") == "publish":
            log_skip(session_id, str(tool_input["type_url"]), "typed_artifact")
        return None
    subject = str(path.resolve())
    text = extract_text(path)
    if len(text.split()) < env_float("MD_EVAL_ARTIFACT_MIN_WORDS", 150):
        log_skip(session_id, subject, "too_short")
        return None

    notice = missing_key_notice(HOOK, session_id, "page")
    if notice is not None:
        log_skip(session_id, subject, "no_key")
        return notice or None

    # Per-file state: how often it was sent back, and the text last scored.
    key = hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:16]
    state_file = session_file(HOOK, session_id, f"{key}.json")
    state = json.loads(state_file.read_text()) if state_file.exists() else {"blocks": 0, "text": ""}
    text_hash = hashlib.sha256(text.encode()).hexdigest()
    max_blocks = int(env_float("MD_EVAL_ARTIFACT_MAX_BLOCKS", 1))
    if state["text"] == text_hash:
        log_skip(session_id, subject, "unchanged")
        return None
    state["text"] = text_hash
    state_file.write_text(json.dumps(state))

    with Timer() as timer:
        result, p_doc = asyncio.run(_score(path, text))
    if not result.ok:
        return check_failed(HOOK, session_id, str(path.resolve()), text, result.error, timer.ms, "page")

    min_score = env_float("MD_EVAL_ARTIFACT_MIN_SCORE", 0.6)
    flag_threshold = env_float("MD_EVAL_ARTIFACT_FLAG_THRESHOLD", 0.5)
    min_conf = env_float("MD_EVAL_ARTIFACT_MIN_CONFIDENCE", 0.5)
    doc_threshold = env_float("MD_EVAL_ARTIFACT_DOC_THRESHOLD", 0.5)
    blocking_ids = {f.strip() for f in os.environ.get("MD_EVAL_ARTIFACT_BLOCK_FLAGS", DEFAULT_BLOCKING_FLAGS).split(",")}

    weights = {d.id: d.weight for d in DIMENSIONS if not d.is_flag}
    total = result.total("page", weights)
    is_doc = p_doc >= doc_threshold
    blocking = [f for f in result.raised_flags(flag_threshold) if f in blocking_ids]
    failing = (is_doc and total < min_score) or blocking

    summary = (
        f"md-eval: page scored {total * 100:.0f}/100 for readers"
        if is_doc
        else "md-eval: page checked for flags (not scored as prose)"
    )
    blocks = state["blocks"]
    outcome = "passed" if not failing else "allowed_after_limit" if blocks >= max_blocks else "sent_back"
    log_check(HOOK, session_id, str(path.resolve()), text, result, total, blocking, outcome, blocks, timer.ms,
              written_document=round(p_doc, 3))
    if outcome != "sent_back":
        return {"systemMessage": summary}

    state["blocks"] += 1
    state_file.write_text(json.dumps(state))
    return deny(summary + " — sent back before publishing", _feedback(result, total, is_doc, blocking, min_score, min_conf))


def main() -> None:
    run_hook(run, "artifact hook")


if __name__ == "__main__":
    main()
