"""Story-generation pipeline: storyteller, parallel support panel, judge.

Outer flow is strictly sequential and easy to trace:

    request -> storyteller -> support panel -> judge -> (revise if needed) -> story

The only parallelism is the support panel fan-out, implemented as one
`ThreadPoolExecutor.map(...)` call. Bounded `for` loop always terminates.
"""

from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Optional

from client import chat
from config import (
    DEFAULT_MAX_REVISIONS,
    JUDGE_MAX_TOKENS,
    JUDGE_PASS_SCORE,
    JUDGE_TEMPERATURE,
    PLANNER_MAX_TOKENS,
    PLANNER_TEMPERATURE,
    STORYTELLER_MAX_TOKENS,
    STORYTELLER_TEMPERATURE,
    SUPPORT_MAX_TOKENS,
    SUPPORT_TEMPERATURE,
)
from prompts import (
    ENVIRONMENT_CONCERNS,
    ENVIRONMENT_FACTS,
    INITIAL_USER_TEMPLATE,
    JUDGE_SYSTEM,
    JUDGE_USER_TEMPLATE,
    OUTLINE_TYPES,
    PLANNER_SYSTEM,
    PLANNER_USER_TEMPLATE,
    REVISION_USER_TEMPLATE,
    STORYTELLER_SYSTEM,
    SUPPORT_ASPECTS,
    SUPPORT_SYSTEM_TEMPLATE,
    SUPPORT_USER_TEMPLATE,
)

logger = logging.getLogger("bedtime")


# --- Data shapes ------------------------------------------------------------


@dataclass(frozen=True)
class StoryRequest:
    """User-supplied input for a single story generation.

    Validation lives here so the CLI and any future caller share one
    source of truth for what counts as a valid request.
    """

    name: str
    age: int
    environment: str

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("Listener name must not be empty.")
        if not 5 <= self.age <= 10:
            raise ValueError(
                f"Listener age must be between 5 and 10 (got {self.age})."
            )
        if self.environment not in ENVIRONMENT_FACTS:
            valid = ", ".join(sorted(ENVIRONMENT_FACTS))
            raise ValueError(
                f"Environment must be one of: {valid}. Got: {self.environment!r}."
            )


@dataclass
class SupportAnalysis:
    name: str
    score: int
    issues: list[str]
    suggestions: str


@dataclass
class JudgeVerdict:
    overall_score: int
    passed: bool
    safety_concerns: list[str]
    synthesized_feedback: str


@dataclass(frozen=True)
class StoryPlan:
    """Output of the planner agent: which outline + a short spine."""

    outline: str  # one of OUTLINE_TYPES keys
    spine: str    # 3 to 5 sentence roadmap


@dataclass
class StoryResult:
    story: str
    verdict: JudgeVerdict
    supports: list[SupportAnalysis]
    revisions: int
    plan: StoryPlan


@dataclass
class UserRevisionOutcome:
    """Result of one post-pipeline user-feedback revision attempt."""

    accepted: bool                   # True if the revised story passed re-validation
    story: str                       # Revised story (only meaningful when accepted)
    verdict: JudgeVerdict            # Judge verdict on the revised story
    supports: list[SupportAnalysis]  # Panel analyses on the revised story


# --- Planner ----------------------------------------------------------------


def plan_story(request: StoryRequest) -> StoryPlan:
    """Pick an outline type and draft a 3-to-5 sentence story spine.

    Soft-pass on JSON parse / schema failure: log a warning and return a
    deterministic fallback plan so the pipeline never blocks on a flaky
    planner response. Same pattern as the support panel and judge.
    """
    raw = chat(
        [
            {"role": "system", "content": PLANNER_SYSTEM},
            {
                "role": "user",
                "content": PLANNER_USER_TEMPLATE.format(
                    name=request.name.strip(),
                    age=request.age,
                    environment=request.environment,
                    concern_topic=ENVIRONMENT_CONCERNS[request.environment],
                ),
            },
        ],
        temperature=PLANNER_TEMPERATURE,
        max_tokens=PLANNER_MAX_TOKENS,
    )
    data = _extract_json(raw)
    if data is None:
        logger.warning(
            "Planner returned unparseable JSON; falling back. Raw: %r",
            raw[:200],
        )
        return _fallback_plan(request)

    outline = str(data.get("outline", "")).strip()
    spine = str(data.get("spine", "")).strip()
    if outline not in OUTLINE_TYPES or not spine:
        logger.warning(
            "Planner returned invalid outline=%r or empty spine; falling back.",
            outline,
        )
        return _fallback_plan(request)
    return StoryPlan(outline=outline, spine=spine)


def _fallback_plan(request: StoryRequest) -> StoryPlan:
    """Deterministic backup plan if the planner agent fails parse/schema."""
    name = request.name.strip()
    return StoryPlan(
        outline="mistake_and_amends",
        spine=(
            f"{name} is in a quiet {request.environment} setting at "
            f"bedtime. {name} does one small thoughtless thing that "
            f"troubles the environment. {name} notices the small "
            f"consequence, gently makes it right, and settles into "
            f"sleep with a quiet, kind feeling."
        ),
    )


# --- Storyteller ------------------------------------------------------------


def generate_story(
    request: StoryRequest,
    plan: StoryPlan,
    *,
    previous_draft: Optional[str] = None,
    feedback: Optional[str] = None,
) -> str:
    """Produce a story for the request, optionally revising a prior draft.

    `plan` is the planner's outline + spine. On the initial pass we hand
    the storyteller the listener, theme, facts, topic, and spine. On a
    revision pass the previous draft already encodes all of that, so we
    only re-send the prior draft plus the panel's consolidated feedback.
    """
    if previous_draft is None:
        user_message = INITIAL_USER_TEMPLATE.format(
            name=request.name.strip(),
            age=request.age,
            environment=request.environment,
            facts=ENVIRONMENT_FACTS[request.environment],
            concern_topic=ENVIRONMENT_CONCERNS[request.environment],
            spine=plan.spine,
        )
    else:
        user_message = REVISION_USER_TEMPLATE.format(
            previous_draft=previous_draft.strip(),
            feedback=(feedback or "").strip() or "(no specific feedback provided)",
        )
    return chat(
        [
            {"role": "system", "content": STORYTELLER_SYSTEM},
            {"role": "user", "content": user_message},
        ],
        temperature=STORYTELLER_TEMPERATURE,
        max_tokens=STORYTELLER_MAX_TOKENS,
    ).strip()


# --- Support panel (parallel fan-out) ---------------------------------------


def run_support_agents(story: str, age: int) -> list[SupportAnalysis]:
    """Run all six support agents on the same draft in parallel.

    Order of the returned list matches the insertion order of
    `SUPPORT_ASPECTS` so the judge always sees the panel in a stable,
    predictable layout. The listener's age is passed to every reviewer so
    the vocab reviewer can scale its standard to the specific listener;
    the others see it as harmless context.
    """
    aspects = list(SUPPORT_ASPECTS.items())
    with ThreadPoolExecutor(max_workers=len(aspects)) as pool:
        return list(
            pool.map(lambda kv: _run_one_support(kv[0], kv[1], story, age), aspects)
        )


def _run_one_support(
    name: str,
    aspect: dict[str, str],
    story: str,
    age: int,
) -> SupportAnalysis:
    raw = chat(
        [
            {
                "role": "system",
                "content": SUPPORT_SYSTEM_TEMPLATE.format(
                    aspect_name=name,
                    aspect_role=aspect["role"],
                    aspect_focus=aspect["focus"],
                    aspect_output_expectations=aspect["output_expectations"],
                    age=age,
                ),
            },
            {"role": "user", "content": SUPPORT_USER_TEMPLATE.format(story=story)},
        ],
        temperature=SUPPORT_TEMPERATURE,
        max_tokens=SUPPORT_MAX_TOKENS,
    )
    data = _extract_json(raw)
    if data is None:
        # Soft-pass: a single broken agent must not block the whole pipeline.
        # The warning is always visible (default log level is WARNING).
        logger.warning(
            "Support agent %r returned unparseable JSON; treating as soft pass. Raw: %r",
            name,
            raw[:200],
        )
        return SupportAnalysis(name=name, score=5, issues=[], suggestions="")

    score = _coerce_score(data.get("score"), default=5, label=f"support[{name}].score")
    issues = [str(item).strip() for item in (data.get("issues") or []) if str(item).strip()]
    suggestions = str(data.get("suggestions") or "").strip()
    return SupportAnalysis(name=name, score=score, issues=issues, suggestions=suggestions)


# --- Judge ------------------------------------------------------------------


def judge_story(story: str, supports: list[SupportAnalysis]) -> JudgeVerdict:
    """Have the judge weigh the story plus all panel findings into a verdict."""
    raw = chat(
        [
            {"role": "system", "content": JUDGE_SYSTEM},
            {
                "role": "user",
                "content": JUDGE_USER_TEMPLATE.format(
                    story=story,
                    support_block=_format_support_block(supports),
                ),
            },
        ],
        temperature=JUDGE_TEMPERATURE,
        max_tokens=JUDGE_MAX_TOKENS,
    )
    return _parse_verdict(raw)


def _parse_verdict(raw: str) -> JudgeVerdict:
    data = _extract_json(raw)
    if data is None:
        # Bounded loop already protects us; soft-pass keeps the user from
        # getting nothing. Warning is always visible.
        logger.warning(
            "Judge returned unparseable JSON; accepting story without revision. Raw: %r",
            raw[:200],
        )
        return JudgeVerdict(
            overall_score=JUDGE_PASS_SCORE,
            passed=True,
            safety_concerns=[],
            synthesized_feedback="",
        )

    overall_score = _coerce_score(
        data.get("overall_score"), default=JUDGE_PASS_SCORE, label="judge.overall_score"
    )
    safety_concerns = [
        str(item).strip()
        for item in (data.get("safety_concerns") or [])
        if str(item).strip()
    ]
    synthesized_feedback = str(data.get("synthesized_feedback") or "").strip()
    declared_pass = bool(data.get("pass", False))

    # Trust but verify: a lenient judge can declare pass=true even when the
    # numbers don't support it. Enforce the threshold and zero-safety rule.
    passed = (
        declared_pass
        and overall_score >= JUDGE_PASS_SCORE
        and not safety_concerns
    )

    return JudgeVerdict(
        overall_score=overall_score,
        passed=passed,
        safety_concerns=safety_concerns,
        synthesized_feedback=synthesized_feedback,
    )


# --- Length gate (deterministic) -------------------------------------------

# Strict paragraph count enforcement. Word counts tend to fall in range once
# paragraph count is right, so we keep this surgical and avoid over-gating.
TARGET_PARAGRAPHS = 6


def check_length(story: str) -> Optional[str]:
    """Return None if the story has exactly TARGET_PARAGRAPHS paragraphs;
    otherwise return a short, deterministic feedback string the storyteller
    can act on directly. Paragraphs are blocks separated by one blank line."""
    paragraphs = [p for p in story.strip().split("\n\n") if p.strip()]
    count = len(paragraphs)
    if count == TARGET_PARAGRAPHS:
        return None
    return (
        f"Your previous draft had {count} paragraphs separated by blank "
        f"lines. The story MUST be exactly {TARGET_PARAGRAPHS} paragraphs, "
        "each separated by one blank line. Please rewrite the story as "
        f"exactly {TARGET_PARAGRAPHS} paragraphs while keeping the same "
        "characters, setting, conflict, and kind action; just redistribute "
        "the existing material across the correct number of paragraphs."
    )


# --- Orchestrator -----------------------------------------------------------


def run_pipeline(
    request: StoryRequest,
    *,
    max_revisions: int = DEFAULT_MAX_REVISIONS,
) -> StoryResult:
    """Run the full planner -> storyteller -> length gate -> panel -> judge -> revise loop."""
    if max_revisions < 0:
        raise ValueError("max_revisions must be >= 0.")

    plan = plan_story(request)
    logger.info("[plan] outline=%s | spine=%s", plan.outline, plan.spine)

    story = generate_story(request, plan)
    for revision in range(max_revisions + 1):
        # Deterministic length gate runs first. Length-failed drafts skip
        # the panel + judge entirely (saves 7 LLM calls per failure) and
        # revise straight from a deterministic feedback string.
        length_issue = check_length(story)
        if length_issue and revision < max_revisions:
            logger.info(
                "[length] revision %d: %s",
                revision + 1,
                length_issue.split(".")[0],
            )
            story = generate_story(
                request, plan, previous_draft=story, feedback=length_issue
            )
            continue

        supports = run_support_agents(story, request.age)
        verdict = judge_story(story, supports)
        logger.info(_format_step(revision, verdict, supports))

        if verdict.passed or revision == max_revisions:
            return StoryResult(
                story=story,
                verdict=verdict,
                supports=supports,
                revisions=revision,
                plan=plan,
            )

        feedback = (
            verdict.synthesized_feedback
            or "Strengthen the weakest aspects flagged by the panel."
        )
        story = generate_story(request, plan, previous_draft=story, feedback=feedback)

    raise RuntimeError("unreachable: bounded for-loop always returns")


# --- Post-pipeline user feedback (single shot) ------------------------------


def apply_user_feedback(
    request: StoryRequest,
    plan: StoryPlan,
    story: str,
    feedback: str,
) -> UserRevisionOutcome:
    """Run one storyteller revision driven by user feedback, then re-validate.

    The user has already received a judge-approved story; this is an
    explicit override they requested. We run the same panel + judge as
    the main loop so the safety / quality bar is unchanged. If the
    revised story passes, we surface it as the accepted result. If it
    fails (low score or any safety concern), we report `accepted=False`
    and the caller keeps the original story.

    Single shot by design: no length gate, no retry loop. The user gets
    exactly one attempt so the interaction stays predictable and short.
    """
    revised = generate_story(request, plan, previous_draft=story, feedback=feedback)
    supports = run_support_agents(revised, request.age)
    verdict = judge_story(revised, supports)
    logger.info(
        "[user_feedback] %s | judge=%d | %s",
        "PASS" if verdict.passed else "REVERT",
        verdict.overall_score,
        ", ".join(f"{s.name}={s.score}" for s in supports),
    )
    return UserRevisionOutcome(
        accepted=verdict.passed,
        story=revised,
        verdict=verdict,
        supports=supports,
    )


# --- Formatting helpers -----------------------------------------------------


def _format_support_block(supports: list[SupportAnalysis]) -> str:
    """Render the panel's findings into a block the judge model can read."""
    parts = []
    for s in supports:
        issues = (
            "\n".join(f"    - {issue}" for issue in s.issues)
            if s.issues
            else "    (none)"
        )
        suggestions = s.suggestions or "(none)"
        parts.append(
            f"[{s.name}] score={s.score}\n"
            f"  issues:\n{issues}\n"
            f"  suggestions: {suggestions}"
        )
    return "\n\n".join(parts)


def _format_step(
    revision: int,
    verdict: JudgeVerdict,
    supports: list[SupportAnalysis],
) -> str:
    """One INFO-level log message per revision, summarising the panel + judge."""
    label = "Initial draft" if revision == 0 else f"Revision {revision}"
    status = "PASS" if verdict.passed else "needs revision"
    panel = ", ".join(f"{s.name}={s.score}" for s in supports)
    lines = [f"[{label}] {status} | judge={verdict.overall_score} | {panel}"]
    if verdict.safety_concerns:
        lines.append(f"  safety concerns: {'; '.join(verdict.safety_concerns)}")
    if verdict.synthesized_feedback and not verdict.passed:
        lines.append(f"  feedback: {verdict.synthesized_feedback}")
    return "\n".join(lines)


# --- JSON extraction (shared by supports and judge) -------------------------

_JSON_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)
# Greedy on purpose: at temperature 0 with a strict schema, agents emit one
# JSON object. The wide match is robust against stray prose around it.
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json(raw: str) -> Optional[dict]:
    text = _JSON_FENCE_RE.sub("", raw.strip())
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = _JSON_OBJECT_RE.search(text)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def _coerce_score(value: object, *, default: int, label: str) -> int:
    """Clamp `value` to 1..5 if it's numeric; otherwise warn and return default."""
    if isinstance(value, bool):  # bool is an int subclass in Python; exclude.
        value = None
    if isinstance(value, (int, float)):
        return max(1, min(5, int(value)))
    logger.warning(
        "%s was non-numeric (%r); using default %d.", label, value, default
    )
    return default
