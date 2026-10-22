"""
Contract and regression tests for WindPulse live AVEVA PI Web API snapshot mapping.
Verifies strict tag mapping, timestamp/UOM preservation, rejection of bad/questionable
quality flags, digital state resilience, partial coverage calculation, absence of
silent fallback to simulation, and honest error on missing configuration.
"""

from unittest.mock import AsyncMock
import aiohttp
import pytest
from src.config import settings
from src.models import PIStreamValue
from src.pi_web_api.client import AvevaPIWebApiClient, PIWebAPIClientError
from src.pi_web_api.service import TelemetryService

@pytest.fixture
def mock_pi_client():
    client = AsyncMock(spec=AvevaPIWebApiClient)
    client.base_url = "https://mock-pi.corp/piwebapi"
    client.allow_writes = False
    client.approved_write_points = set()
    return client

@pytest.fixture
def real_pi_service(mock_pi_client):
    service = TelemetryService()
    service.mode = "REAL_PI"
    service.pi_client = mock_pi_client
    service.initialized = True
    return service

@pytest.mark.asyncio
async def test_complete_live_pi_response_maps_to_turbine_models(real_pi_service, mock_pi_client):
    """1. Complete live PI response correctly maps to turbine models."""
    sample_ts = "2026-10-22T12:00:00Z"

    def make_values(web_ids):
        res = {}
        for wid in web_ids:
            if "ActivePower" in wid:
                res[wid] = PIStreamValue(Timestamp=sample_ts, Value=2100.0, UnitsAbbreviation="kW", Good=True, Questionable=False)
            elif "WindSpeed" in wid:
                res[wid] = PIStreamValue(Timestamp=sample_ts, Value=11.2, UnitsAbbreviation="m/s", Good=True, Questionable=False)
            elif "RotorRPM" in wid:
                res[wid] = PIStreamValue(Timestamp=sample_ts, Value=14.5, UnitsAbbreviation="RPM", Good=True, Questionable=False)
            elif "NacelleYaw" in wid:
                res[wid] = PIStreamValue(Timestamp=sample_ts, Value=228.0, UnitsAbbreviation="deg", Good=True, Questionable=False)
            elif "GearboxTemp" in wid:
                res[wid] = PIStreamValue(Timestamp=sample_ts, Value=68.5, UnitsAbbreviation="°C", Good=True, Questionable=False)
            else:
                res[wid] = PIStreamValue(Timestamp=sample_ts, Value=100.0, Good=True, Questionable=False)
        return res

    mock_pi_client.get_streamset_values.side_effect = lambda ids: make_values(ids)

    snapshot = await real_pi_service._fetch_real_pi_snapshot()

    assert snapshot.mode == "REAL_PI"
    assert snapshot.summary.is_simulated is False
    assert len(snapshot.turbines) == 8
    t1 = snapshot.turbines[0]
    assert t1.name == "WTG-01"
    assert t1.active_power_kw == 2100.0
    assert t1.wind_speed_mps == 11.2
    assert t1.rotor_rpm == 14.5
    assert t1.field_quality["active_power_kw"] == "GOOD"
    assert snapshot.summary.turbines_online == 8
    assert snapshot.summary.data_coverage_pct == 100.0
    assert snapshot.summary.total_active_power_mw == round(8 * 2100.0 / 1000.0, 3)

@pytest.mark.asyncio
async def test_partial_pi_response_reports_none_for_missing_tags(real_pi_service, mock_pi_client):
    """2. Partial PI response reports None for missing tags with accurate coverage percentage."""
    sample_ts = "2026-10-22T12:00:00Z"

    # Only supply active power for WTG-01, WTG-02, WTG-03, WTG-04 (4 of 8 turbines)
    def make_values(web_ids):
        res = {}
        for wid in web_ids:
            if any(prefix in wid for prefix in ["WTG-01", "WTG-02", "WTG-03", "WTG-04"]):
                res[wid] = PIStreamValue(Timestamp=sample_ts, Value=1800.0, UnitsAbbreviation="kW", Good=True, Questionable=False)
            # Other tags are completely omitted from PI response
        return res

    mock_pi_client.get_streamset_values.side_effect = lambda ids: make_values(ids)

    snapshot = await real_pi_service._fetch_real_pi_snapshot()

    assert len(snapshot.turbines) == 8
    # WTG-01 has power
    assert snapshot.turbines[0].active_power_kw == 1800.0
    # WTG-05 has missing power -> None, NOT hardcoded fallback
    t5 = snapshot.turbines[4]
    assert t5.name == "WTG-05"
    assert t5.active_power_kw is None
    assert t5.field_quality["active_power_kw"] == "UNAVAILABLE"
    # Coverage: exactly 4 out of 8 turbines reporting power = 50.0%
    assert snapshot.summary.data_coverage_pct == 50.0
    assert snapshot.summary.turbines_online == 4
    assert snapshot.summary.total_active_power_mw == round(4 * 1800.0 / 1000.0, 3)

@pytest.mark.asyncio
async def test_bad_quality_pi_flags_not_mapped_as_valid_values(real_pi_service, mock_pi_client):
    """3. Bad-quality PI flags (Good=False) are not mapped as valid values."""
    mock_pi_client.get_streamset_values.return_value = {
        settings.PI_TURBINE_TAG_MAPPINGS["WTG-01"]["active_power_kw"]: PIStreamValue(
            Timestamp="2026-10-22T12:00:00Z",
            Value=2400.0,
            Good=False,
            Questionable=False
        )
    }

    snapshot = await real_pi_service._fetch_real_pi_snapshot()
    t1 = snapshot.turbines[0]
    assert t1.active_power_kw is None
    assert t1.field_quality["active_power_kw"] == "BAD_QUALITY"
    assert t1.quality_good is False

@pytest.mark.asyncio
async def test_questionable_pi_flags_handled_and_flagged(real_pi_service, mock_pi_client):
    """4. Questionable PI flags (Questionable=True) are handled according to configuration."""
    mock_pi_client.get_streamset_values.return_value = {
        settings.PI_TURBINE_TAG_MAPPINGS["WTG-01"]["active_power_kw"]: PIStreamValue(
            Timestamp="2026-10-22T12:00:00Z",
            Value=1750.0,
            Good=True,
            Questionable=True
        )
    }

    snapshot = await real_pi_service._fetch_real_pi_snapshot()
    t1 = snapshot.turbines[0]
    assert t1.active_power_kw is None
    assert t1.field_quality["active_power_kw"] == "QUESTIONABLE"
    assert t1.quality_good is False

@pytest.mark.asyncio
async def test_digital_state_strings_do_not_cause_float_conversion_errors(real_pi_service, mock_pi_client):
    """5. Digital state strings (e.g. 'Shutdown', 'Bad Input') do not cause float conversion errors."""
    mock_pi_client.get_streamset_values.return_value = {
        settings.PI_TURBINE_TAG_MAPPINGS["WTG-01"]["active_power_kw"]: PIStreamValue(
            Timestamp="2026-10-22T12:00:00Z",
            Value="Shutdown",
            Good=True,
            Questionable=False
        ),
        settings.PI_TURBINE_TAG_MAPPINGS["WTG-01"]["wind_speed_mps"]: PIStreamValue(
            Timestamp="2026-10-22T12:00:00Z",
            Value={"Name": "Calc Failed", "Value": -255},
            Good=True,
            Questionable=False
        )
    }

    snapshot = await real_pi_service._fetch_real_pi_snapshot()
    t1 = snapshot.turbines[0]
    assert t1.active_power_kw is None
    assert t1.wind_speed_mps is None
    assert t1.field_quality["active_power_kw"] == "NON_NUMERIC_DIGITAL_STATE"
    assert t1.field_quality["wind_speed_mps"] == "NON_NUMERIC_DIGITAL_STATE"

@pytest.mark.asyncio
async def test_unreachable_pi_raises_error_without_silent_simulation_fallback(real_pi_service, mock_pi_client):
    """6. Unreachable PI raises appropriate error or returns honest empty state without fallback."""
    mock_pi_client.get_streamset_values.side_effect = aiohttp.ClientError("Connection refused by PI Web API")

    # In REAL_PI mode, get_current_snapshot() must fail honestly, never silently switch to simulation
    with pytest.raises(RuntimeError) as exc_info:
        await real_pi_service.get_current_snapshot()

    assert "batch stream fetch failed" in str(exc_info.value).lower()

@pytest.mark.asyncio
async def test_source_timestamps_and_units_preserved(real_pi_service, mock_pi_client):
    """7. Source timestamps and units are preserved in telemetry output."""
    tag_time = "2026-10-22T08:15:30.1234567Z"
    mock_pi_client.get_streamset_values.return_value = {
        settings.PI_TURBINE_TAG_MAPPINGS["WTG-01"]["active_power_kw"]: PIStreamValue(
            Timestamp=tag_time,
            Value=1942.3,
            UnitsAbbreviation="kW",
            Good=True,
            Questionable=False
        ),
        settings.PI_TURBINE_TAG_MAPPINGS["WTG-01"]["wind_speed_mps"]: PIStreamValue(
            Timestamp=tag_time,
            Value=10.4,
            UnitsAbbreviation="m/s",
            Good=True,
            Questionable=False
        )
    }

    snapshot = await real_pi_service._fetch_real_pi_snapshot()
    t1 = snapshot.turbines[0]
    assert t1.active_power_kw == 1942.3
    assert t1.timestamp == tag_time
    assert t1.field_timestamps["active_power_kw"] == tag_time
    assert t1.field_units["active_power_kw"] == "kW"
    assert t1.field_units["wind_speed_mps"] == "m/s"

@pytest.mark.asyncio
async def test_missing_configuration_reports_honest_error(real_pi_service, mock_pi_client, monkeypatch):
    """8. Missing tag mappings report honest configuration error."""
    monkeypatch.setattr(settings, "PI_TURBINE_TAG_MAPPINGS", {})
    mock_pi_client.ping_connection.return_value = {"status": "ok"}

    with pytest.raises(PIWebAPIClientError) as exc_info:
        await real_pi_service._fetch_real_pi_snapshot()

    assert "no turbine point webids are mapped" in str(exc_info.value).lower()
