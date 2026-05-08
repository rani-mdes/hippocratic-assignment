"""Tunable constants for the bedtime-story pipeline.

All knobs (model id, temperatures, token budgets, loop bounds, pass
threshold) live here so prompt iteration and tuning happen in one file.
"""

MODEL_ID: str = "gpt-3.5-turbo"

# Storyteller is creative; supports and judge are deterministic so their
# scores are repeatable across runs of the same draft.
STORYTELLER_TEMPERATURE: float = 0.8
# 400 words is roughly 530 tokens; 700 leaves comfortable headroom so the
# storyteller never gets truncated mid-sentence at the top of its range.
STORYTELLER_MAX_TOKENS: int = 700

# The planner picks one of two outline types and drafts a 3-to-5
# sentence spine. Some variety in the spine is desirable, so we sit
# above the deterministic supports/judge but below the storyteller.
PLANNER_TEMPERATURE: float = 0.5
PLANNER_MAX_TOKENS: int = 350

SUPPORT_TEMPERATURE: float = 0.0
SUPPORT_MAX_TOKENS: int = 400

JUDGE_TEMPERATURE: float = 0.0
JUDGE_MAX_TOKENS: int = 600

DEFAULT_MAX_REVISIONS: int = 2

# A draft passes when the judge's overall score meets this bar AND no
# safety concerns were raised. Inclusive on the lower end (>=).
JUDGE_PASS_SCORE: int = 4
