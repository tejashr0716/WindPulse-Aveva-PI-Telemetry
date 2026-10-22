"""
Real AVEVA OSIsoft PI Web API integration test suite.
Non-negotiable rule:
If an authorized, reachable PI environment is not present,
these tests are marked SKIPPED with an explicit explanation.
Live integration is NEVER reported as verified without an actual PI system.
"""

import os
import pytest
from src.config import settings
from src.pi_web_api.client import AvevaPIWebApiClient

pytestmark = pytest.mark.skipif(
    settings.RUN_MODE != "REAL_PI" or not settings.PI_WEB_API_URL,
    reason="SKIPPED: No authorized, reachable AVEVA PI Web API environment configured (RUN_MODE != 'REAL_PI')."
)

@pytest.mark.asyncio
async def test_real_pi_system_connectivity():
    """Attempts handshake with actual PI Web API server."""
    client = AvevaPIWebApiClient(
        base_url=settings.PI_WEB_API_URL,
        auth_type=settings.PI_AUTH_TYPE,
        username=settings.PI_USERNAME,
        password=settings.PI_PASSWORD,
        bearer_token=settings.PI_BEARER_TOKEN,
        verify_ssl=settings.PI_VERIFY_SSL
    )
    async with client:
        # Query root endpoint
        session = await client.get_session()
        async with session.get(f"{client.base_url}/") as resp:
            assert resp.status == 200

@pytest.mark.asyncio
async def test_real_pi_point_guarded_write_and_verify():
    """Tests live point write only if explicit opt-in and whitelist are present."""
    if not settings.PI_ALLOW_WRITES or not settings.PI_APPROVED_WRITE_POINTS:
        pytest.skip("SKIPPED: Live PI Point writes are disabled (PI_ALLOW_WRITES=false or no approved points).")

    test_point_web_id = settings.PI_APPROVED_WRITE_POINTS[0]
    client = AvevaPIWebApiClient(
        base_url=settings.PI_WEB_API_URL,
        auth_type=settings.PI_AUTH_TYPE,
        username=settings.PI_USERNAME,
        password=settings.PI_PASSWORD,
        allow_writes=True,
        approved_write_points=settings.PI_APPROVED_WRITE_POINTS
    )
    async with client:
        result = await client.write_point_value(test_point_web_id, 42.0)
        assert result.verified is True
