"""Evaluation dimensions for technical documents / RFCs.

Every dimension is data: the CLI, request builder and report all iterate over
DIMENSIONS. To add, remove or reword a dimension, edit this file only.

Scores are graded judgments combined into a weighted total per reader:
  - human: writing + substance groups (a person reading and reviewing the doc)
  - agent: agent group (an AI coding agent implementing from the doc)
Flags are yes/no risk conditions shared by both readers and reported
separately (they never average away into a total).
"""

from dataclasses import dataclass
from typing import Literal

Group = Literal["writing", "substance", "agent", "flag"]
Reader = Literal["human", "agent"]

READER_GROUPS: dict[Reader, tuple[Group, ...]] = {
    "human": ("writing", "substance"),
    "agent": ("agent",),
}


@dataclass(frozen=True)
class Dimension:
    id: str
    label: str
    short: str  # compact column header
    group: Group
    instructions: str
    # Score: ordered levels, lowest first. Flag (Noul): unused.
    criteria: tuple[str, ...] = ()
    # Relative weight within the composite total (Scores only).
    weight: float = 1.0

    @property
    def is_flag(self) -> bool:
        return self.group == "flag"


DOC = "`document.text`"

DIMENSIONS: tuple[Dimension, ...] = (
    # ---- Writing quality -------------------------------------------------
    Dimension(
        id="clarity",
        label="Clarity",
        short="Clar",
        group="writing",
        instructions=(
            f"How easily could a software engineer who is new to this project "
            f"understand the technical document in {DOC} on a first read?"
        ),
        criteria=(
            "Mostly incomprehensible: the main idea cannot be determined",
            "Hard to follow: key terms undefined, sentences ambiguous, reader must guess at meaning",
            "Understandable with effort: main idea is clear but several passages need re-reading",
            "Clear: nearly every passage is understood on first read, with occasional vague spots",
            "Very clear: precise wording, terms defined, every passage understood on first read",
        ),
    ),
    Dimension(
        id="structure",
        label="Structure",
        short="Strc",
        group="writing",
        instructions=(
            f"How well organized is the technical document in {DOC}? Consider section "
            "headings, logical ordering (context and problem before solution before details), "
            "and whether related information is grouped together."
        ),
        criteria=(
            "No discernible organization: a single undifferentiated block or random ordering",
            "Weak organization: some sections exist but ordering is confusing or topics are scattered",
            "Adequate organization: sensible sections, but some content is misplaced or out of order",
            "Well organized: clear sections in a logical order with minor misplacements",
            "Excellently organized: clear headings, logical flow from problem to solution to details, easy to navigate",
        ),
    ),
    Dimension(
        id="concision",
        label="Concision",
        short="Conc",
        group="writing",
        instructions=(
            f"How concise is the technical document in {DOC}? Penalize padding, "
            "repetition, filler phrases and off-topic tangents. Do not penalize length "
            "that carries necessary technical detail."
        ),
        criteria=(
            "Extremely bloated: most of the text is filler, repetition or tangents",
            "Wordy: substantial repetition or tangents obscure the content",
            "Somewhat wordy: noticeable padding or repetition in several places",
            "Mostly concise: little padding, a few sentences could be cut",
            "Tight: every sentence carries information, no repetition or filler",
        ),
    ),
    Dimension(
        id="audience_fit",
        label="Audience fit",
        short="Aud",
        group="writing",
        instructions=(
            f"How well does the document in {DOC} match its intended readers' background? "
            "The intended readers are given in `intended_audience` if present; otherwise use "
            "the audience the document states or implies. Judge whether jargon, assumed "
            "knowledge and depth of background explanation suit those readers."
        ),
        criteria=(
            "Badly mismatched: readers could not follow it, or it is aimed at a completely different audience",
            "Poor fit: frequent unexplained jargon or excessive explanation of basics for these readers",
            "Partial fit: generally suitable but with some sections pitched too high or too low",
            "Good fit: pitched appropriately with only minor lapses",
            "Excellent fit: consistently pitched at exactly the right level for the readers",
        ),
    ),
    # ---- Content substance -----------------------------------------------
    Dimension(
        id="problem_motivation",
        label="Problem & motivation",
        short="Prob",
        group="substance",
        instructions=(
            f"How well does the document in {DOC} establish the problem it addresses: "
            "what the problem is, who or what it affects, how severe it is, and why it "
            "should be solved now?"
        ),
        criteria=(
            "No problem statement: the document jumps straight to a solution",
            "Problem only hinted at: vague or implied, with no impact described",
            "Problem stated but impact or urgency is not explained",
            "Problem and impact clearly explained, with limited evidence or no explanation of why now",
            "Problem, affected parties, impact with evidence, and urgency are all clearly established",
        ),
        weight=1.25,
    ),
    Dimension(
        id="design_completeness",
        label="Design completeness",
        short="Design",
        group="substance",
        instructions=(
            f"How complete is the proposed design in {DOC}? Could an engineer implement it "
            "from this document? Consider interfaces/APIs, data models, component "
            "interactions, and important edge cases."
        ),
        criteria=(
            "No concrete design: only goals or a one-line idea",
            "Sketch only: high-level idea with no interfaces, data or flows specified",
            "Partial design: main components described, but key interfaces, data or flows are missing",
            "Mostly complete: an engineer could implement it with a few clarifying questions",
            "Implementation-ready: interfaces, data, flows and edge cases are specified",
        ),
        weight=1.5,
    ),
    Dimension(
        id="alternatives_tradeoffs",
        label="Alternatives & trade-offs",
        short="Alts",
        group="substance",
        instructions=(
            f"How well does the document in {DOC} consider alternative approaches and explain "
            "the trade-offs that led to the chosen approach?"
        ),
        criteria=(
            "No alternatives mentioned",
            "Alternatives named but not evaluated",
            "Alternatives briefly evaluated, but the reason for choosing the proposal is weak or missing",
            "Alternatives evaluated with clear reasons for the choice, though some trade-offs are glossed over",
            "Several credible alternatives compared on explicit criteria, with the proposal's downsides acknowledged",
        ),
    ),
    Dimension(
        id="risks_rollout",
        label="Risks & rollout",
        short="Risk",
        group="substance",
        instructions=(
            f"How well does the document in {DOC} cover risks and delivery: failure modes, "
            "security or operational risks, migration, testing, rollout and rollback?"
        ),
        criteria=(
            "No discussion of risks, testing or rollout",
            "Risks or rollout mentioned in passing with no plan",
            "Some risks and a basic rollout plan, but no testing or rollback strategy",
            "Main risks identified with mitigations, plus a rollout plan covering testing or rollback",
            "Thorough: failure modes, mitigations, testing, staged rollout, rollback and monitoring are all covered",
        ),
    ),
    # ---- AI coding agent readiness ------------------------------------------
    Dimension(
        id="agent_actionability",
        label="Actionability",
        short="Act",
        group="agent",
        instructions=(
            f"If an AI coding agent were given only the document in {DOC} and access to the "
            "codebase, how directly could it turn the document into a concrete sequence of "
            "implementation steps without asking a person for direction?"
        ),
        criteria=(
            "Not actionable: describes goals or opinions but no work to be done",
            "Vague direction: the work is implied but the agent would have to invent the steps",
            "Partially actionable: some steps are clear, others need a person's decision",
            "Mostly actionable: the steps are clear, with a few open decisions left to the agent",
            "Fully actionable: clear ordered tasks with decisions already made",
        ),
        weight=1.5,
    ),
    Dimension(
        id="agent_explicitness",
        label="Explicitness",
        short="Expl",
        group="agent",
        instructions=(
            f"How explicitly are the requirements in {DOC} stated? Penalize requirements "
            "that rely on unstated team knowledge, references like 'as discussed' or 'the "
            "usual way', and hedged wording that leaves it unclear whether something is "
            "required. Reward precise, unambiguous statements of what must be true."
        ),
        criteria=(
            "Implicit: requirements must be inferred from context the document does not contain",
            "Mostly implicit: key requirements are hedged, vague or depend on unstated knowledge",
            "Mixed: main requirements are explicit but several details are vague or assumed",
            "Mostly explicit: requirements are precise with only minor ambiguities",
            "Fully explicit: every requirement is stated precisely, with required and optional clearly distinguished",
        ),
        weight=1.25,
    ),
    Dimension(
        id="agent_grounding",
        label="Codebase grounding",
        short="Grnd",
        group="agent",
        instructions=(
            f"How concretely does the document in {DOC} point to the parts of the codebase "
            "and tooling involved, such as file paths, modules, classes, functions, "
            "services, config keys, commands, or existing patterns to follow?"
        ),
        criteria=(
            "No references to code, systems or tooling",
            "Only broad system names (for example 'the backend' or 'the API gateway')",
            "Names specific services or components but no files, symbols or commands",
            "Names specific files, modules or functions for most of the work",
            "Precise references for all work: files, symbols, commands and existing patterns to follow",
        ),
    ),
    Dimension(
        id="agent_interfaces",
        label="Interface precision",
        short="Intf",
        group="agent",
        instructions=(
            f"How precisely does the document in {DOC} specify the interfaces and data an "
            "implementation must match: function or API signatures, request and response "
            "schemas, data types, field names, error behavior, and example inputs and outputs?"
        ),
        criteria=(
            "No interfaces or data shapes specified",
            "Interfaces described in prose only, with no names, types or shapes",
            "Some names and shapes given, but types, errors or examples are missing",
            "Most interfaces specified with names, types and shapes; minor gaps in errors or examples",
            "Exact signatures, schemas, types, error behavior and worked examples for all interfaces",
        ),
    ),
    Dimension(
        id="agent_verifiability",
        label="Verifiability",
        short="Verif",
        group="agent",
        instructions=(
            f"How well does the document in {DOC} let an agent verify its own work? "
            "Consider testable acceptance criteria, expected behaviors, test cases, "
            "and commands to run tests or checks."
        ),
        criteria=(
            "No way to tell when the work is done or correct",
            "Only a vague goal (for example 'should be faster' or 'should work well')",
            "Some success criteria, but not concrete enough to test",
            "Concrete, testable acceptance criteria for most of the work",
            "Testable acceptance criteria for all work, plus specific test cases or commands to run",
        ),
        weight=1.25,
    ),
    Dimension(
        id="agent_boundaries",
        label="Scope boundaries",
        short="Bnd",
        group="agent",
        instructions=(
            f"How clearly does the document in {DOC} bound the work for an agent: what is "
            "in scope, what is out of scope, what must not be changed, and which "
            "constraints and conventions to respect (dependencies, compatibility, style)?"
        ),
        criteria=(
            "No boundaries: nothing says what is out of scope or what must not change",
            "Scope only implied by the topic of the document",
            "In-scope work is stated, but no non-goals or constraints",
            "Scope and non-goals stated, with some constraints or conventions",
            "Scope, non-goals, things that must not change, and constraints are all explicit",
        ),
    ),
    Dimension(
        id="agent_self_contained",
        label="Self-contained",
        short="Self",
        group="agent",
        instructions=(
            f"Is everything an agent needs to act on the document in {DOC} either written "
            "in the document or linked explicitly? Penalize dependence on meetings, chat "
            "threads, people's memory, or unnamed documents."
        ),
        criteria=(
            "Depends almost entirely on context outside the document that is not linked",
            "Key context lives in unlinked meetings, threads or people's heads",
            "Some needed context is missing or referenced without a link",
            "Nearly self-contained; one or two minor references are not linked",
            "Fully self-contained: all needed context is in the document or explicitly linked",
        ),
    ),
    # ---- Risk / compliance flags (Noul) ------------------------------------
    Dimension(
        id="unfinished_content",
        label="Unfinished",
        short="TBD",
        group="flag",
        instructions=(
            f"Does the document in {DOC} contain unfinished content, such as placeholder "
            "sections, 'TBD' or 'TODO' markers, empty headings, or sections that stop mid-thought?"
        ),
    ),
    Dimension(
        id="unsupported_claims",
        label="Unsupported claims",
        short="Claims",
        group="flag",
        instructions=(
            f"Does the document in {DOC} make at least one specific claim about performance, "
            "cost, scale or impact (for example '10x faster' or 'saves $1M') without "
            "supporting data, a benchmark, a calculation or a stated reasoning?"
        ),
    ),
    Dimension(
        id="secrets",
        label="Secrets",
        short="Secrets",
        group="flag",
        instructions=(
            f"Does the document in {DOC} contain what appears to be a real credential or secret: "
            "an API key, password, access token, private key, or connection string with "
            "embedded credentials? Obvious placeholders like '<YOUR_API_KEY>' or 'xxxx' do not count."
        ),
    ),
    Dimension(
        id="pii",
        label="PII",
        short="PII",
        group="flag",
        instructions=(
            f"Does the document in {DOC} contain personal information about real individuals, "
            "such as email addresses, phone numbers, home addresses, government IDs, or "
            "customer records? The names of the document's authors or reviewers alone do not count."
        ),
    ),
    Dimension(
        id="contradictions",
        label="Contradictions",
        short="Contra",
        group="flag",
        instructions=(
            f"Does the document in {DOC} contain two or more statements or requirements "
            "that contradict each other, so that following one would violate the other?"
        ),
    ),
)

SCORES = tuple(d for d in DIMENSIONS if not d.is_flag)
FLAGS = tuple(d for d in DIMENSIONS if d.is_flag)


def scores_for(reader: Reader) -> tuple[Dimension, ...]:
    return tuple(d for d in SCORES if d.group in READER_GROUPS[reader])


def dimensions_for(readers: tuple[Reader, ...]) -> tuple[Dimension, ...]:
    """Scores for the given readers plus all flags, in definition order."""
    groups = {g for r in readers for g in READER_GROUPS[r]} | {"flag"}
    return tuple(d for d in DIMENSIONS if d.group in groups)
