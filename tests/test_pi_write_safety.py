"""
Unit tests for PI Point write operations and safety guardrails.
Enforces the non-negotiable rules:
- Writes must be disabled by default.
- Writes must require an explicit whitelist of approved test points.
- Writes must be verified via read-back.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from src.pi_web_api.client import (
    AvevaPIWebApiClient,
    PIStreamValue,
    PIWritePermissionError
)

@pytest.mark.asyncio
async def test_write_blocked_when_disabled_by_default():
    """Default client instantiation (allow_writes=False) must strictly refuse write attempts."""
    client = AvevaPIWebApiClient(
        base_url="https://mock-pi/piwebapi",
        allow_writes=False,
        approved_write_points=["APPROVED_TEST_POINT_01"]
    )
    with pytest.raises(PIWritePermissionError) as exc_info:
        await client.write_point_value("APPROVED_TEST_POINT_01", 123.45)
    assert "WRITE BLOCKED: Writes are disabled by default" in str(exc_info.value)

@pytest.mark.asyncio
async def test_write_blocked_when_point_not_in_allowlist():
    """Even if allow_writes=True, writing to an unapproved point must be rejected."""
    client = AvevaPIWebApiClient(
        base_url="https://mock-pi/piwebapi",
        allow_writes=True,
        approved_write_points=["APPROVED_TEST_POINT_01"]
    )
    with pytest.raises(PIWritePermissionError) as exc_info:
        await client.write_point_value("UNAPPROVED_PRODUCTION_POINT", 500.0)
    assert "is not in approved test points list" in str(exc_info.value)

@pytest.mark.asyncio
async def test_write_succeeds_and_verifies_read_back():
    """When authorized and whitelisted, write is executed and verified via read-back."""
    target_web_id = "APPROVED_TEST_POINT_01"
    client = AvevaPIWebApiClient(
        base_url="https://mock-pi/piwebapi",
        allow_writes=True,
        approved_write_points=[target_web_id]
    )

    # Mock POST /streams/{webId}/value
    mock_post_resp = MagicMock()
    mock_post_resp.status = 202

    # Mock GET /streams/{webId}/value for read-back
    mock_get_value = PIStreamValue(
        Timestamp="2026-10-22T14:35:00Z",
        Value=98.75,
        Good=True
    )

    with patch.object(client, "_execute_with_retry", AsyncMock(return_value=mock_post_resp)):
        with patch.object(client, "get_stream_value", AsyncMock(return_value=mock_get_value)):
            result = await client.write_point_value(target_web_id, 98.75)
            assert result.web_id == target_web_id
            assert result.written_value == 98.75
            assert result.read_back_value == 98.75
            assert result.verified is True
            assert "verified via read-back" in result.message

@pytest.mark.asyncio
async def test_write_divergence_flagged():
    """If read-back does not match written value, verified flag must be False."""
    target_web_id = "APPROVED_TEST_POINT_01"
    client = AvevaPIWebApiClient(
        base_url="https://mock-pi/piwebapi",
        allow_writes=True,
        approved_write_points=[target_web_id]
    )

    mock_post_resp = MagicMock()
    mock_post_resp.status = 202

    # Divergent read-back value (e.g. 50.0 instead of 98.75)
    mock_get_value = PIStreamValue(
        Timestamp="2026-10-22T14:35:00Z",
        Value=50.0,
        Good=True
    )

    with patch.object(client, "_execute_with_retry", AsyncMock(return_value=mock_post_resp)):
        with patch.object(client, "get_stream_value", AsyncMock(return_value=mock_get_value)):
            result = await client.write_point_value(target_web_id, 98.75)
            assert result.verified is False
            assert "read-back verification diverged" in result.message

@pytest.mark.asyncio
async def test_write_read_back_flagged_unverified_if_quality_questionable_or_bad():
    """
    Regression Test for Audit Issue E.2:
    If read-back matches numerically but Questionable=True or Good=False, verified must be False.
    """
    target_web_id = "APPROVED_TEST_POINT_01"
    client = AvevaPIWebApiClient(
        base_url="https://mock-pi/piwebapi",
        allow_writes=True,
        approved_write_points=[target_web_id]
    )

    mock_post_resp = MagicMock()
    mock_post_resp.status = 202

    # Matches numeric value 98.75 but marked Questionable
    mock_get_value = PIStreamValue(
        Timestamp="2026-10-22T14:35:00Z",
        Value=98.75,
        Good=True,
        Questionable=True
    )

    with patch.object(client, "_execute_with_retry", AsyncMock(return_value=mock_post_resp)):
        with patch.object(client, "get_stream_value", AsyncMock(return_value=mock_get_value)):
            result = await client.write_point_value(target_web_id, 98.75)
            assert result.verified is False
            assert "diverged" in result.message
