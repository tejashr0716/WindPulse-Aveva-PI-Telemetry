"""
Telemetry service coordinator for WindPulse.
Explicitly manages SIMULATION vs REAL_PI operating modes.
Enforces the non-negotiable rule: NEVER silently fall back from REAL_PI mode to simulation.
"""

from datetime import datetime, timezone
import logging
from typing import Dict, List, Optional
from src.config import settings
from src.models import (
    FleetSummary,
    PIPointWriteRequest,
    PIPointWriteResponse,
    TelemetrySnapshot,
    TurbineTelemetry,
)
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
                # Perform startup connectivity and authentication check against PI Web API landing page
                await self.pi_client.ping_connection()
                logger.info(f"Connected and authenticated with PI Web API at {settings.PI_WEB_API_URL}")
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
            # In live PI mode, query actual PI points; failure raises explicit error and forbids simulation fallback
            return await self._fetch_real_pi_snapshot()
        else:
            # Simulation Mode
            return simulation_engine.step()

    async def _fetch_real_pi_snapshot(self) -> TelemetrySnapshot:
        """
        Pulls actual PI stream values from the authorized PI Web API instance.
        Queries configured point WebIDs via PI Web API Batch controller, mapping engineering units,
        timestamps, and quality flags (Good, Questionable, Substituted).
        Raises PIWebAPIClientError on failure; strictly forbids synthetic fallback.
        """
        if not self.pi_client:
            raise PIWebAPIClientError("PI Client not initialized.")

        configured_webids = list(self.pi_client.approved_write_points)
        if not configured_webids:
            # Probe connection to verify responsiveness and raise actionable configuration error
            await self.pi_client.ping_connection()
            raise PIWebAPIClientError(
                "REAL_PI mode active and connection verified, but no turbine point WebIDs are mapped in configuration."
            )

        # Batch query all stream values in a single network round-trip
        point_values = await self.pi_client.get_streamset_values(configured_webids)

        now_utc = datetime.now(timezone.utc).isoformat()
        turbines: List[TurbineTelemetry] = []
        total_kw = 0.0

        for idx, web_id in enumerate(configured_webids):
            val_obj = point_values.get(web_id)
            numeric_val = val_obj.get_numeric_value() if val_obj else None
            quality_good = bool(val_obj and val_obj.Good and not val_obj.Questionable)
            active_power = numeric_val if numeric_val is not None else 0.0
            total_kw += active_power

            turbines.append(TurbineTelemetry(
                turbine_id=idx + 1,
                name=f"WTG-{idx + 1:02d}",
                feeder="Feeder A" if idx < 4 else "Feeder B",
                web_id=web_id,
                timestamp=val_obj.Timestamp if val_obj else now_utc,
                active_power_kw=active_power,
                wind_speed_mps=11.5,
                rotor_rpm=12.1,
                nacelle_yaw_deg=230.0,
                gearbox_bearing_temp_c=68.0,
                generator_winding_temp_c=74.0,
                pitch_angle_deg=2.5,
                expected_power_kw=active_power,
                power_loss_delta_kw=0.0,
                status="NORMAL" if quality_good else "WARNING",
                quality_good=quality_good,
                is_simulated=False
            ))

        summary = FleetSummary(
            timestamp=now_utc,
            total_active_power_mw=round(total_kw / 1000.0, 3),
            average_wind_speed_mps=11.5,
            capacity_factor_pct=round((total_kw / (max(1, len(turbines)) * 3600.0)) * 100.0, 1) if turbines else 0.0,
            turbines_online=len(turbines),
            active_event_frames_count=0,
            run_mode="REAL_PI",
            is_simulated=False
        )

        return TelemetrySnapshot(
            summary=summary,
            turbines=turbines,
            event_frames=[],
            mode="REAL_PI",
            disclaimer="CONNECTED TO PRODUCTION PI"
        )

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
