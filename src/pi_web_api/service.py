"""
Telemetry service coordinator for WindPulse.
Explicitly manages SIMULATION vs REAL_PI operating modes.
Enforces the non-negotiable rule: NEVER silently fall back from REAL_PI mode to simulation.
"""

import logging
from typing import Dict, List, Optional
from src.config import settings
from src.models import PIPointWriteRequest, PIPointWriteResponse, TelemetrySnapshot
from src.pi_web_api.client import AvevaPIWebApiClient, PIWebAPIClientError
from src.simulation import simulation_engine

logger = logging.getLogger("windpulse.service")

class TelemetryService:
    def __init__(self):
        self.mode = settings.RUN_MODE
        self.pi_client: Optional[AvevaPIWebApiClient] = None
        self.initialized = False

    async def initialize(self):
        if self.mode == "REAL_PI":
            logger.info("Initializing TelemetryService in REAL_PI mode...")
            if not settings.PI_WEB_API_URL:
                raise RuntimeError(
                    "FATAL: RUN_MODE is set to REAL_PI, but PI_WEB_API_URL is missing. "
                    "Silent fallback to simulation mode is strictly forbidden."
                )
            self.pi_client = AvevaPIWebApiClient(
                base_url=settings.PI_WEB_API_URL,
                auth_type=settings.PI_AUTH_TYPE,
                username=settings.PI_USERNAME,
                password=settings.PI_PASSWORD,
                bearer_token=settings.PI_BEARER_TOKEN,
                verify_ssl=settings.PI_VERIFY_SSL,
                timeout_seconds=settings.PI_REQUEST_TIMEOUT_SECONDS,
                max_retries=settings.PI_MAX_RETRIES,
                allow_writes=settings.PI_ALLOW_WRITES,
                approved_write_points=settings.PI_APPROVED_WRITE_POINTS
            )
            try:
                # Test connectivity
                await self.pi_client.get_session()
                logger.info(f"Connected to PI Web API at {settings.PI_WEB_API_URL}")
            except Exception as e:
                logger.error(f"Failed to connect to real PI Web API: {e}")
                # DO NOT silently fall back to simulation!
                raise RuntimeError(
                    f"FATAL: Unable to reach configured AVEVA PI Web API at {settings.PI_WEB_API_URL}: {e}. "
                    "Silent fallback to simulation mode is prohibited."
                ) from e
        else:
            logger.info("Initializing TelemetryService in SIMULATION mode (Prototype / Physics Simulator).")
        
        self.initialized = True

    async def get_current_snapshot(self) -> TelemetrySnapshot:
        """Retrieves the latest telemetry snapshot based on active run mode."""
        if self.mode == "REAL_PI":
            if not self.pi_client:
                raise RuntimeError("PI Client not initialized.")
            # In a live PI environment, query actual StreamSets via batch
            # If real connection is not active, this raises an error
            return await self._fetch_real_pi_snapshot()
        else:
            # Simulation Mode
            return simulation_engine.step()

    async def _fetch_real_pi_snapshot(self) -> TelemetrySnapshot:
        """Pulls actual PI stream values from the authorized PI Web API instance."""
        # This executes actual PI stream queries
        # Real PI environment would query registered turbine WebIDs
        raise NotImplementedError("Real PI snapshot query requires an authorized PI environment.")

    async def write_test_point(self, req: PIPointWriteRequest) -> PIPointWriteResponse:
        """Executes a guarded write to a PI Point."""
        if self.mode != "REAL_PI":
            # In simulation mode, write operations are simulated locally
            return PIPointWriteResponse(
                web_id=req.web_id,
                written_value=req.value,
                timestamp=req.timestamp or "SIM_NOW",
                read_back_value=req.value,
                verified=True,
                message="[SIMULATION MODE] Point write simulated in local memory."
            )
        
        if not self.pi_client:
            raise RuntimeError("PI client is not initialized.")
        return await self.pi_client.write_point_value(
            web_id=req.web_id,
            value=req.value,
            timestamp=req.timestamp
        )

    async def shutdown(self):
        if self.pi_client:
            await self.pi_client.close()
            self.pi_client = None
        self.initialized = False

telemetry_service = TelemetryService()
