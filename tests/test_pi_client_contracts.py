"""
Mocked contract tests for AvevaPIWebApiClient.
Verifies request format, header composition, quality flag parsing, and error handling
without requiring access to licensed PI infrastructure.
"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from src.pi_web_api.client import (
    AvevaPIWebApiClient,
    PIAuthenticationError,
    PINotFoundError,
    PIStreamValue,
    PIWebAPIClientError
)

def test_headers_composition_basic_auth():
    client = AvevaPIWebApiClient(
        base_url="https://mock-pi/piwebapi",
        auth_type="BASIC",
        username="pi_user",
        password="secret_password"
    )
    headers = client._build_headers()
    assert headers["Accept"] == "application/json"
    assert headers["X-Requested-With"] == "XMLHttpRequest"
    assert headers["Authorization"].startswith("Basic ")

def test_headers_composition_bearer_auth():
    client = AvevaPIWebApiClient(
        base_url="https://mock-pi/piwebapi",
        auth_type="BEARER",
        bearer_token="eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.dummy"
    )
    headers = client._build_headers()
    assert headers["Authorization"] == "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.dummy"

def test_pi_stream_value_quality_flags_good():
    """Valid reading with Good=True must return numeric value."""
    payload = {
        "Timestamp": "2026-10-22T14:30:00Z",
        "Value": 2415.8,
        "UnitsAbbreviation": "kW",
        "Good": True,
        "Questionable": False
    }
    sv = PIStreamValue(**payload)
    assert sv.Good is True
    assert sv.get_numeric_value() == 2415.8

def test_pi_stream_value_quality_flags_bad_digital_state():
    """When PI System reports a digital state (e.g. Scan Off, Bad Input) with Good=False, numeric value must be None."""
    payload = {
        "Timestamp": "2026-10-22T14:30:00Z",
        "Value": {"Name": "Scan Off", "Value": 248, "IsSystem": True},
        "Good": False,
        "Questionable": True
    }
    sv = PIStreamValue(**payload)
    assert sv.Good is False
    assert sv.get_numeric_value() is None

def test_pi_stream_value_quality_flags_bad_string():
    """When PI reports a string failure with Good=False, numeric value must be None."""
    payload = {
        "Timestamp": "2026-10-22T14:30:00Z",
        "Value": "I/O Timeout",
        "Good": False
    }
    sv = PIStreamValue(**payload)
    assert sv.Good is False
    assert sv.get_numeric_value() is None

@pytest.mark.asyncio
async def test_get_stream_value_contract():
    """Mock test verifying GET /streams/{webId}/value deserialization."""
    client = AvevaPIWebApiClient(base_url="https://mock-pi/piwebapi")
    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value={
        "Timestamp": "2026-10-22T14:00:00Z",
        "Value": 1850.5,
        "UnitsAbbreviation": "kW",
        "Good": True
    })

    with patch.object(client, "_execute_with_retry", AsyncMock(return_value=mock_response)):
        res = await client.get_stream_value("P0_TEST_WEBID")
        assert res.Good is True
        assert res.get_numeric_value() == 1850.5
        assert res.UnitsAbbreviation == "kW"

@pytest.mark.asyncio
async def test_batch_streamsets_contract():
    """Mock test verifying POST /piwebapi/batch handles multi-tag streamsets."""
    client = AvevaPIWebApiClient(base_url="https://mock-pi/piwebapi")
    mock_batch_data = {
        "tag_0": {
            "Status": 200,
            "Content": {
                "Timestamp": "2026-10-22T14:00:00Z",
                "Value": 11.4,
                "UnitsAbbreviation": "m/s",
                "Good": True
            }
        },
        "tag_1": {
            "Status": 200,
            "Content": {
                "Timestamp": "2026-10-22T14:00:00Z",
                "Value": 1850.0,
                "UnitsAbbreviation": "kW",
                "Good": True
            }
        }
    }
    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value=mock_batch_data)

    with patch.object(client, "_execute_with_retry", AsyncMock(return_value=mock_response)):
        results = await client.get_streamset_values(["WEBID_WIND", "WEBID_POWER"])
        assert len(results) == 2
        assert results["WEBID_WIND"].get_numeric_value() == 11.4
        assert results["WEBID_POWER"].get_numeric_value() == 1850.0

@pytest.mark.asyncio
async def test_authentication_error_contract():
    """HTTP 401 must raise PIAuthenticationError."""
    client = AvevaPIWebApiClient(base_url="https://mock-pi/piwebapi")
    mock_response = MagicMock()
    mock_response.status = 401
    mock_response.text = AsyncMock(return_value="Authorization Denied")

    session_mock = MagicMock()
    session_mock.request = AsyncMock(return_value=mock_response)

    with patch.object(client, "get_session", AsyncMock(return_value=session_mock)):
        with pytest.raises(PIAuthenticationError):
            await client.get_stream_value("P0_TEST_WEBID")

@pytest.mark.asyncio
async def test_not_found_error_contract():
    """HTTP 404 must raise PINotFoundError."""
    client = AvevaPIWebApiClient(base_url="https://mock-pi/piwebapi")
    mock_response = MagicMock()
    mock_response.status = 404

    session_mock = MagicMock()
    session_mock.request = AsyncMock(return_value=mock_response)

    with patch.object(client, "get_session", AsyncMock(return_value=session_mock)):
        with pytest.raises(PINotFoundError):
            await client.get_stream_value("P0_NONEXISTENT_WEBID")
