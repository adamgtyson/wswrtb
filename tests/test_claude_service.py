"""Metered Claude service: cost maths, usage logging, and all four AI limit checks.

Every test here mocks the Anthropic client — no test in this suite may make a real API
call or spend real money. Limits are exercised by inserting `ai_usage` rows directly and
monkeypatching the module's limit constants low (the service reads them as module
attributes at call time, so patching the attribute takes effect immediately).
"""
import asyncio

import anthropic
import httpx
import pytest

from app import db
from app.services import claude_service
from app.services.claude_service import (
    DailyCostCeilingError,
    DailyRateLimitError,
    FreeTierMonthlyLimitError,
    HourlyRateLimitError,
)

from .conftest import claude_response, register

JSON_BODY = '[{"title": "A Book", "author": "An Author", "year": 2001, "reason": "Fits."}]'


def _run(coro):
    return asyncio.run(coro)


def _member_id(fetchone, email):
    """Resolve a user id by email."""
    return fetchone("SELECT id FROM users WHERE email = ?", (email,))[0]


def _add_usage(user_id, count=1, cost=0.0):
    """Insert `count` ai_usage rows for a user at the given per-row cost."""

    async def go():
        for _ in range(count):
            await db.record_ai_usage(
                user_id=user_id,
                group_id=None,
                model="test-model",
                input_tokens=10,
                output_tokens=10,
                est_cost_usd=cost,
                endpoint="test",
            )

    _run(go())


def _age_usage(user_id, seconds):
    """Push a user's usage rows into the past so they fall outside shorter windows."""

    async def go():
        async with db.connect() as conn:
            await conn.execute(
                "UPDATE ai_usage SET created_at = datetime('now', ?) WHERE user_id = ?",
                (f"-{int(seconds)} seconds", user_id),
            )
            await conn.commit()

    _run(go())


@pytest.fixture()
def two_users(client, seed, fetchone):
    """Seed a group and register a second member. Returns (owner_id, member_id)."""
    info = seed(code="AICLUB", seats=8)
    register(client, code="AICLUB", email="second@example.com")
    return info["owner_user_id"], _member_id(fetchone, "second@example.com")


# ---------------------------------------------------------------------------
# Cost accounting
# ---------------------------------------------------------------------------
def test_estimate_cost_uses_named_pricing_constants():
    """Cost is derived from the per-million-token constants, not a hardcoded number."""
    cost = claude_service.estimate_cost_usd(1_000_000, 1_000_000)
    expected = (
        claude_service.INPUT_COST_PER_MTOK_USD + claude_service.OUTPUT_COST_PER_MTOK_USD
    )
    assert cost == pytest.approx(expected)


def test_estimate_cost_scales_with_tokens():
    """Half a million input tokens costs half the per-million input rate."""
    cost = claude_service.estimate_cost_usd(500_000, 0)
    assert cost == pytest.approx(claude_service.INPUT_COST_PER_MTOK_USD / 2)


# ---------------------------------------------------------------------------
# Usage logging
# ---------------------------------------------------------------------------
def test_successful_call_logs_real_tokens_and_cost(two_users, fake_claude, fetchone):
    """The usage row carries the API's own token counts and the computed cost."""
    owner_id, _ = two_users
    fake_claude(claude_response(JSON_BODY, input_tokens=1234, output_tokens=567))

    text = _run(
        claude_service.complete_text(
            user_id=owner_id,
            group_id=None,
            endpoint="unit",
            system_prompt="sys",
            user_prompt="hello",
        )
    )
    assert text == JSON_BODY

    row = fetchone(
        """SELECT input_tokens, output_tokens, est_cost_usd, model, endpoint, user_id
           FROM ai_usage ORDER BY id DESC LIMIT 1"""
    )
    assert row[0] == 1234
    assert row[1] == 567
    assert row[2] == pytest.approx(claude_service.estimate_cost_usd(1234, 567))
    assert row[3] == claude_service.CLAUDE_MODEL
    assert row[4] == "unit"
    assert row[5] == owner_id


def test_call_sends_configured_model_and_max_tokens(two_users, fake_claude):
    """max_tokens and the model come from the named constants, not inline literals."""
    owner_id, _ = two_users
    fake = fake_claude(claude_response(JSON_BODY))

    _run(
        claude_service.complete_text(
            user_id=owner_id, group_id=None, endpoint="unit",
            system_prompt="sys", user_prompt="hello",
        )
    )
    sent = fake.calls[0]
    assert sent["model"] == claude_service.CLAUDE_MODEL
    assert sent["max_tokens"] == claude_service.MAX_OUTPUT_TOKENS
    assert sent["system"] == "sys"
    assert sent["messages"] == [{"role": "user", "content": "hello"}]


def test_api_failure_raises_service_error_and_logs_no_usage(two_users, fake_claude, fetchone):
    """An upstream failure surfaces as ClaudeServiceError; nothing is billed."""
    owner_id, _ = two_users
    fake_claude(
        anthropic.APIConnectionError(
            request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        )
    )

    with pytest.raises(claude_service.ClaudeServiceError):
        _run(
            claude_service.complete_text(
                user_id=owner_id, group_id=None, endpoint="unit",
                system_prompt="sys", user_prompt="hello",
            )
        )
    assert fetchone("SELECT COUNT(*) FROM ai_usage")[0] == 0


def test_empty_response_raises_service_error(two_users, fake_claude):
    """A response with no text content is a service failure, not a silent empty result."""
    owner_id, _ = two_users
    fake_claude(claude_response("   "))

    with pytest.raises(claude_service.ClaudeServiceError):
        _run(
            claude_service.complete_text(
                user_id=owner_id, group_id=None, endpoint="unit",
                system_prompt="sys", user_prompt="hello",
            )
        )


# ---------------------------------------------------------------------------
# Limit 1 — global daily cost ceiling (kill switch)
# ---------------------------------------------------------------------------
def test_global_daily_ceiling_blocks_every_user(two_users, monkeypatch):
    """One user's spend trips the ceiling for ALL users, not just the spender."""
    owner_id, member_id = two_users
    monkeypatch.setattr(claude_service, "AI_DAILY_COST_CEILING_USD", 1.0)

    # The owner alone burns the whole daily budget.
    _add_usage(owner_id, count=2, cost=0.60)

    with pytest.raises(DailyCostCeilingError):
        _run(claude_service.enforce_limits(owner_id))
    # A different user who has spent nothing is refused too.
    with pytest.raises(DailyCostCeilingError):
        _run(claude_service.enforce_limits(member_id))


def test_global_ceiling_prevents_the_api_call_entirely(two_users, fake_claude, monkeypatch, fetchone):
    """When the ceiling is tripped, no request is issued and nothing new is logged."""
    owner_id, member_id = two_users
    monkeypatch.setattr(claude_service, "AI_DAILY_COST_CEILING_USD", 0.5)
    _add_usage(owner_id, count=1, cost=0.75)
    fake = fake_claude()  # any call at all would fail on the empty response queue

    with pytest.raises(DailyCostCeilingError):
        _run(
            claude_service.complete_text(
                user_id=member_id, group_id=None, endpoint="unit",
                system_prompt="sys", user_prompt="hello",
            )
        )
    assert fake.call_count == 0
    assert fetchone("SELECT COUNT(*) FROM ai_usage")[0] == 1  # only the seeded row


def test_under_the_ceiling_is_allowed(two_users, monkeypatch):
    """Spend below the ceiling does not block."""
    owner_id, _ = two_users
    monkeypatch.setattr(claude_service, "AI_DAILY_COST_CEILING_USD", 5.0)
    _add_usage(owner_id, count=1, cost=0.01)
    _run(claude_service.enforce_limits(owner_id))  # must not raise


# ---------------------------------------------------------------------------
# Limit 2 — per-user hourly
# ---------------------------------------------------------------------------
def test_hourly_limit_trips_for_that_user_only(two_users, monkeypatch):
    """The hourly count is per calling user; other members are unaffected."""
    owner_id, member_id = two_users
    monkeypatch.setattr(claude_service, "AI_RATE_PER_HOUR", 2)
    _add_usage(owner_id, count=2)

    with pytest.raises(HourlyRateLimitError):
        _run(claude_service.enforce_limits(owner_id))
    _run(claude_service.enforce_limits(member_id))  # untouched user still allowed


def test_hourly_window_slides(two_users, monkeypatch):
    """Calls older than an hour no longer count against the hourly limit."""
    owner_id, _ = two_users
    monkeypatch.setattr(claude_service, "AI_RATE_PER_HOUR", 2)
    _add_usage(owner_id, count=2)
    _age_usage(owner_id, 2 * 60 * 60)  # two hours ago
    _run(claude_service.enforce_limits(owner_id))  # must not raise


# ---------------------------------------------------------------------------
# Limit 3 — per-user daily
# ---------------------------------------------------------------------------
def test_daily_limit_trips_after_the_hourly_window_passes(two_users, monkeypatch):
    """Rows outside the hour but inside the day still count against the daily limit."""
    owner_id, _ = two_users
    monkeypatch.setattr(claude_service, "AI_RATE_PER_HOUR", 100)
    monkeypatch.setattr(claude_service, "AI_RATE_PER_DAY", 3)
    _add_usage(owner_id, count=3)
    _age_usage(owner_id, 2 * 60 * 60)  # outside the hour window, inside the day

    with pytest.raises(DailyRateLimitError):
        _run(claude_service.enforce_limits(owner_id))


# ---------------------------------------------------------------------------
# Limit 4 — free-plan monthly
# ---------------------------------------------------------------------------
def test_free_plan_monthly_limit_trips(two_users, monkeypatch):
    """Free-plan users are additionally capped over the rolling 30-day window."""
    owner_id, _ = two_users
    monkeypatch.setattr(claude_service, "AI_RATE_PER_HOUR", 100)
    monkeypatch.setattr(claude_service, "AI_RATE_PER_DAY", 100)
    monkeypatch.setattr(claude_service, "FREE_TIER_MONTHLY_AI_REQUESTS", 3)
    _add_usage(owner_id, count=3)
    _age_usage(owner_id, 3 * 24 * 60 * 60)  # three days ago: only the monthly window sees it

    assert _run(db.get_user_plan(owner_id)) == db.PLAN_FREE
    with pytest.raises(FreeTierMonthlyLimitError):
        _run(claude_service.enforce_limits(owner_id))


def test_monthly_limit_applies_only_to_the_free_plan(two_users, monkeypatch, fetchone):
    """A user on any other plan is not subject to the free-tier monthly allowance."""
    owner_id, _ = two_users
    monkeypatch.setattr(claude_service, "AI_RATE_PER_HOUR", 100)
    monkeypatch.setattr(claude_service, "AI_RATE_PER_DAY", 100)
    monkeypatch.setattr(claude_service, "FREE_TIER_MONTHLY_AI_REQUESTS", 3)
    _add_usage(owner_id, count=3)
    _age_usage(owner_id, 3 * 24 * 60 * 60)

    async def upgrade():
        async with db.connect() as conn:
            await conn.execute("UPDATE users SET plan = 'paid' WHERE id = ?", (owner_id,))
            await conn.commit()

    _run(upgrade())
    _run(claude_service.enforce_limits(owner_id))  # must not raise


def test_usage_older_than_the_month_window_is_ignored(two_users, monkeypatch):
    """Rows beyond the rolling 30-day window stop counting."""
    owner_id, _ = two_users
    monkeypatch.setattr(claude_service, "AI_RATE_PER_HOUR", 100)
    monkeypatch.setattr(claude_service, "AI_RATE_PER_DAY", 100)
    monkeypatch.setattr(claude_service, "FREE_TIER_MONTHLY_AI_REQUESTS", 3)
    _add_usage(owner_id, count=5)
    _age_usage(owner_id, 40 * 24 * 60 * 60)  # 40 days ago
    _run(claude_service.enforce_limits(owner_id))  # must not raise


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
def test_missing_api_key_fails_loudly(monkeypatch):
    """No ANTHROPIC_API_KEY must raise, never silently skip the cost-control path."""
    monkeypatch.setattr(claude_service, "_client", None)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(claude_service.ClaudeConfigError):
        claude_service.get_client()


def test_blank_api_key_fails_loudly(monkeypatch):
    """A blank key is treated as missing rather than passed to the SDK."""
    monkeypatch.setattr(claude_service, "_client", None)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "   ")
    with pytest.raises(claude_service.ClaudeConfigError):
        claude_service.get_client()
