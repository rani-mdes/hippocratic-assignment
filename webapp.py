"""FastAPI server for the Good Night, Earth desktop UI.

Wraps the existing CLI pipeline (`run_pipeline`, `apply_user_feedback`) in
two thin JSON endpoints and serves a single static HTML page from
`static/`. The pipeline is unchanged; this is purely a presentation layer.

Run locally:

    uvicorn webapp:app --reload

Then open http://127.0.0.1:8000 in a desktop browser.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from client import MissingAPIKeyError
from prompts import ENVIRONMENT_FACTS, USER_FEEDBACK_PRESETS
from story_engine import (
    StoryPlan,
    StoryRequest,
    apply_user_feedback,
    run_pipeline,
)

# Per-revision logging is helpful for debugging the pipeline from the
# browser. INFO is verbose enough; warnings (e.g. unparseable agent JSON)
# are always visible regardless.
logging.basicConfig(level=logging.INFO, format="%(message)s")

app = FastAPI(title="Good Night, Earth")


# --- Request / response shapes ---------------------------------------------


class StoryReq(BaseModel):
    name: str = Field(..., min_length=1, max_length=40)
    age: int = Field(..., ge=5, le=10)
    environment: str


class StoryResp(BaseModel):
    story: str
    plan_outline: str
    plan_spine: str
    overall_score: int
    panel: dict[str, int]


class FeedbackReq(BaseModel):
    # Round-tripping the plan + original story through the client keeps
    # the server stateless. Not security-sensitive.
    name: str
    age: int
    environment: str
    plan_outline: str
    plan_spine: str
    story: str
    preset: str            # one of USER_FEEDBACK_PRESETS keys, or "custom"
    custom_text: Optional[str] = None


class FeedbackResp(BaseModel):
    accepted: bool
    story: str             # revised story if accepted, original otherwise
    overall_score: int
    panel: dict[str, int]


# --- Endpoints -------------------------------------------------------------


@app.get("/api/themes")
def list_themes() -> dict[str, list[str]]:
    """The CLI choices come from `ENVIRONMENT_FACTS`. Surface them to the
    UI so the theme picker stays in sync if we add more later."""
    return {"themes": sorted(ENVIRONMENT_FACTS)}


@app.get("/api/presets")
def list_presets() -> dict[str, list[dict[str, str]]]:
    """User-facing labels for the feedback chips. The LLM-facing
    instruction strings stay server-side."""
    return {
        "presets": [
            {"key": key, "label": spec["label"]}
            for key, spec in USER_FEEDBACK_PRESETS.items()
        ]
    }


@app.post("/api/story", response_model=StoryResp)
def generate_story(req: StoryReq) -> StoryResp:
    try:
        request = StoryRequest(name=req.name, age=req.age, environment=req.environment)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    try:
        result = run_pipeline(request)
    except MissingAPIKeyError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from None
    return StoryResp(
        story=result.story,
        plan_outline=result.plan.outline,
        plan_spine=result.plan.spine,
        overall_score=result.verdict.overall_score,
        panel={s.name: s.score for s in result.supports},
    )


@app.post("/api/feedback", response_model=FeedbackResp)
def submit_feedback(req: FeedbackReq) -> FeedbackResp:
    try:
        request = StoryRequest(name=req.name, age=req.age, environment=req.environment)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    if req.preset == "custom":
        feedback_text = (req.custom_text or "").strip()
    else:
        spec = USER_FEEDBACK_PRESETS.get(req.preset)
        if spec is None:
            raise HTTPException(status_code=400, detail=f"Unknown preset: {req.preset}")
        feedback_text = spec["feedback"]

    if not feedback_text:
        raise HTTPException(status_code=400, detail="Feedback text is empty.")

    plan = StoryPlan(outline=req.plan_outline, spine=req.plan_spine)
    try:
        outcome = apply_user_feedback(
            request, plan, story=req.story, feedback=feedback_text
        )
    except MissingAPIKeyError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from None

    return FeedbackResp(
        accepted=outcome.accepted,
        story=outcome.story if outcome.accepted else req.story,
        overall_score=outcome.verdict.overall_score,
        panel={s.name: s.score for s in outcome.supports},
    )


# --- Static UI -------------------------------------------------------------

# Serving the SPA at root with `html=True` makes `/` resolve to
# `static/index.html`. Mounted last so the API routes above take
# precedence.
app.mount("/", StaticFiles(directory="static", html=True), name="static")
