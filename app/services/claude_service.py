"""Metered Claude API access — the ONLY place in this app that talks to Anthropic.

Nothing else may import the `anthropic` SDK directly. Every call funnels through
`complete_text()`, which enforces four independent cost/abuse controls BEFORE issuing a
request and records the real token usage AFTER one succeeds.

Why the limits live in SQLite (`ai_usage`) rather than process memory: under Gunicorn each
worker process has its own memory, so an in-process counter would let every worker grant
the full quota independently and would reset on restart. Every check below is a fresh
query against `ai_usage`, so all workers share one source of truth. This is deliberately
NOT the `rate_limit_events` table — that one is the generic abuse throttle for
auth/registration endpoints; AI spend is accounted separately, from the usage rows that
also carry token counts and cost.

Checks run in this order, each raising a distinct exception (all mapped to a generic
HTTP 429 so the response never leaks quota detail):

  1. Global daily cost ceiling  — ALL users' combined spend today (UTC day). Kill switch.
  2. Per-user hourly request count.
  3. Per-user daily request count.
  4. Per-user monthly request count, for `plan = 'free'` users only.

Configuration comes from the environment (see .env.example); every limit and window is a
named constant below — no magic numbers in the logic.
"""
import logging
import os

import anthropic

from app import db

logger = logging.getLogger(__name__)


def _int_env(name: str, default: int) -> int:
    """Read an integer from the environment, falling back to `default`."""
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _float_env(name: str, default: float) -> float:
    """Read a float from the environment, falling back to `default`."""
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Model + request shape
# ---------------------------------------------------------------------------
# Haiku 4.5 is a deliberate cost choice for this workload (short structured JSON out of a
# short prompt), locked during planning. Override per environment with CLAUDE_MODEL.
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5")

# Output cap per call. Five recommendations with a sentence of reasoning each fit well
# inside this; it also bounds the worst-case cost of any single call.
MAX_OUTPUT_TOKENS = _int_env("AI_MAX_OUTPUT_TOKENS", 2000)

# Hard cap on the free-text prompt a member may submit (characters, validated by Pydantic
# before the request ever reaches this module).
MAX_PROMPT_CHARS = _int_env("AI_MAX_PROMPT_CHARS", 1000)

# ---------------------------------------------------------------------------
# Pricing — claude-haiku-4-5, US dollars per MILLION tokens.
#
# !! REVISIT IF ANTHROPIC CHANGES PRICING !! These are hardcoded rates used to compute
# `ai_usage.est_cost_usd`, which in turn drives the global daily kill switch. If the
# published per-token price changes and these constants don't, the ceiling silently stops
# reflecting real spend. Verified against Anthropic's published pricing for
# claude-haiku-4-5 ($1.00 input / $5.00 output per MTok) as of 2026-07-30.
# ---------------------------------------------------------------------------
INPUT_COST_PER_MTOK_USD = 1.00
OUTPUT_COST_PER_MTOK_USD = 5.00
TOKENS_PER_MILLION = 1_000_000

# ---------------------------------------------------------------------------
# Cost / rate ceilings (env-configurable)
# ---------------------------------------------------------------------------
# Global kill switch: once ALL users' combined spend for the current UTC day reaches this,
# every AI request is refused for every user until the day rolls over.
AI_DAILY_COST_CEILING_USD = _float_env("AI_DAILY_COST_CEILING_USD", 5.0)

# Per-calling-user request counts. One request consumes one slot regardless of how many
# members' profiles it combines.
AI_RATE_PER_HOUR = _int_env("AI_RATE_PER_HOUR", 15)
AI_RATE_PER_DAY = _int_env("AI_RATE_PER_DAY", 50)

# Additional allowance for users on the free plan (everyone, today). Enforced over a
# rolling 30-day window rather than a calendar month: there is no billing cycle to anchor
# a calendar month to yet, and a rolling window can't be reset by waiting for the 1st.
FREE_TIER_MONTHLY_AI_REQUESTS = _int_env("FREE_TIER_MONTHLY_AI_REQUESTS", 15)

_SECONDS_PER_HOUR = 60 * 60
_HOUR_WINDOW_SECONDS = _SECONDS_PER_HOUR
_DAY_WINDOW_SECONDS = 24 * _SECONDS_PER_HOUR
_MONTH_WINDOW_DAYS = 30
_MONTH_WINDOW_SECONDS = _MONTH_WINDOW_DAYS * _DAY_WINDOW_SECONDS


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------
class AILimitError(Exception):
    """Base class for every AI cost/rate refusal. Mapped globally to HTTP 429.

    Carries no quota detail in the response, consistent with the project's other generic
    error messages; the specific subclass is logged server-side instead.
    """


class DailyCostCeilingError(AILimitError):
    """Global daily spend ceiling reached — AI is off for ALL users until UTC midnight."""


class HourlyRateLimitError(AILimitError):
    """This user has made too many AI requests in the trailing hour."""


class DailyRateLimitError(AILimitError):
    """This user has made too many AI requests in the trailing day."""


class FreeTierMonthlyLimitError(AILimitError):
    """This free-plan user has used their monthly AI request allowance."""


class ClaudeConfigError(Exception):
    """Claude is not configured (no ANTHROPIC_API_KEY). Mapped to HTTP 503.

    Raised loudly rather than degrading quietly: a missing key must never be mistaken for
    'AI is fine', and must never cause the cost-control checks to be skipped.
    """


class ClaudeServiceError(Exception):
    """The Claude call failed, or returned something we could not use. Mapped to HTTP 502."""


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------
_client: anthropic.AsyncAnthropic | None = None


def get_client() -> anthropic.AsyncAnthropic:
    """Return the lazily-constructed async Anthropic client.

    Lazy so the app can boot (and the test suite can run) without a key present; the key
    is required only when an AI call is actually attempted. Raises ClaudeConfigError if
    ANTHROPIC_API_KEY is missing or blank.
    """
    global _client
    if _client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            raise ClaudeConfigError(
                "ANTHROPIC_API_KEY is not set. Add it to .env before using AI features."
            )
        _client = anthropic.AsyncAnthropic(api_key=api_key)
    return _client


# ---------------------------------------------------------------------------
# Cost accounting
# ---------------------------------------------------------------------------
def estimate_cost_usd(input_tokens: int, output_tokens: int) -> float:
    """Return the US-dollar cost of one call from its real token counts.

    Uses the per-million-token pricing constants above. Callers must pass the token counts
    reported by the API response, never an estimate.
    """
    input_cost = (input_tokens / TOKENS_PER_MILLION) * INPUT_COST_PER_MTOK_USD
    output_cost = (output_tokens / TOKENS_PER_MILLION) * OUTPUT_COST_PER_MTOK_USD
    return input_cost + output_cost


async def enforce_limits(user_id: int) -> None:
    """Run all four cost/abuse checks for `user_id`, raising on the first that trips.

    Each check is an independent query against `ai_usage` — nothing is cached between
    calls or between worker processes. Order matters: the global kill switch is evaluated
    first so a runaway spend day stops everyone before any per-user accounting.
    """
    spent_today = await db.ai_cost_today_usd()
    if spent_today >= AI_DAILY_COST_CEILING_USD:
        logger.warning(
            "AI daily cost ceiling reached: $%.4f of $%.2f — refusing all AI requests",
            spent_today,
            AI_DAILY_COST_CEILING_USD,
        )
        raise DailyCostCeilingError()

    hourly = await db.count_ai_usage_for_user(user_id, _HOUR_WINDOW_SECONDS)
    if hourly >= AI_RATE_PER_HOUR:
        logger.info("User %s hit the hourly AI limit (%s)", user_id, AI_RATE_PER_HOUR)
        raise HourlyRateLimitError()

    daily = await db.count_ai_usage_for_user(user_id, _DAY_WINDOW_SECONDS)
    if daily >= AI_RATE_PER_DAY:
        logger.info("User %s hit the daily AI limit (%s)", user_id, AI_RATE_PER_DAY)
        raise DailyRateLimitError()

    plan = await db.get_user_plan(user_id)
    if plan == db.PLAN_FREE:
        monthly = await db.count_ai_usage_for_user(user_id, _MONTH_WINDOW_SECONDS)
        if monthly >= FREE_TIER_MONTHLY_AI_REQUESTS:
            logger.info(
                "User %s hit the free-tier monthly AI limit (%s)",
                user_id,
                FREE_TIER_MONTHLY_AI_REQUESTS,
            )
            raise FreeTierMonthlyLimitError()


def _extract_text(response) -> str:
    """Concatenate the text blocks of a Messages API response.

    The response content is a list of typed blocks; only `text` blocks carry model output.
    Raises ClaudeServiceError if the response contains no text at all.
    """
    parts = [block.text for block in response.content if getattr(block, "type", None) == "text"]
    text = "".join(parts).strip()
    if not text:
        raise ClaudeServiceError("Claude returned no text content.")
    return text


async def complete_text(
    *,
    user_id: int,
    group_id: int | None,
    endpoint: str,
    system_prompt: str,
    user_prompt: str,
) -> str:
    """Run ONE metered Claude call and return its text response.

    Enforces every cost/rate control first (see `enforce_limits`), issues the request, then
    records an `ai_usage` row with the REAL token counts from the response and the cost
    computed from the pricing constants. Usage is logged for every successful call —
    including retry calls made by the caller — so accounting can never drift from reality.

    Args:
        user_id: the calling user; all per-user limits and the usage row key off this.
        group_id: group the request was made in (nullable), recorded for later reporting.
        endpoint: short label for the calling route, stored on the usage row.
        system_prompt: system instructions (member profiles, output contract).
        user_prompt: the member's request text.

    Returns:
        The response text, stripped. Parsing/validating it is the caller's job.

    Raises:
        AILimitError subclass: a cost or rate control refused the request (HTTP 429).
        ClaudeConfigError: no API key configured (HTTP 503).
        ClaudeServiceError: the API call failed or returned unusable content (HTTP 502).
    """
    await enforce_limits(user_id)

    client = get_client()
    try:
        response = await client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=MAX_OUTPUT_TOKENS,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
    except anthropic.APIError as exc:
        # Log the detail internally; the route returns a clean generic message.
        logger.error("Claude API call failed on %s: %s", endpoint, exc)
        raise ClaudeServiceError("The recommendation service is unavailable.") from exc

    usage = getattr(response, "usage", None)
    input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
    cost = estimate_cost_usd(input_tokens, output_tokens)

    await db.record_ai_usage(
        user_id=user_id,
        group_id=group_id,
        model=CLAUDE_MODEL,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        est_cost_usd=cost,
        endpoint=endpoint,
    )
    logger.info(
        "AI call %s user=%s in=%s out=%s cost=$%.5f",
        endpoint,
        user_id,
        input_tokens,
        output_tokens,
        cost,
    )

    return _extract_text(response)
