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
        
        Guarantees:
        - NEVER substitutes hardcoded dummy measurements for live fields.
        - Unmapped, bad, questionable, or missing fields are explicitly represented as null (None)
          with transparent quality tracking in field_quality.
        - Calculates summary metrics strictly from reporting valid points and reports data_coverage_pct.
        - Raises PIWebAPIClientError on connection/batch failure; strictly forbids synthetic fallback.
        """
        if not self.pi_client:
            raise PIWebAPIClientError("PI Client not initialized.")

        tag_mappings = settings.PI_TURBINE_TAG_MAPPINGS
        if not tag_mappings:
            # Probe connection to verify responsiveness and raise actionable configuration error
            await self.pi_client.ping_connection()
            raise PIWebAPIClientError(
                "REAL_PI mode active and connection verified, but no turbine point WebIDs are mapped in PI_TURBINE_TAG_MAPPINGS."
            )

        # 1. Collect all distinct WebIDs to batch query
        all_webids = []
        for t_name, fields_map in tag_mappings.items():
            for f_name, wid in fields_map.items():
                if wid and wid not in all_webids:
                    all_webids.append(wid)

        if not all_webids:
            raise PIWebAPIClientError("PI_TURBINE_TAG_MAPPINGS contains no valid WebID endpoints.")

        # 2. Batch query all stream values in a single network round-trip via PI Web API Batch controller
        try:
            point_values = await self.pi_client.get_streamset_values(all_webids)
        except Exception as e:
            logger.error(f"Failed to fetch batch streamset values from PI Web API: {e}")
            raise RuntimeError(f"AVEVA PI Web API batch stream fetch failed: {e}") from e

        now_utc = datetime.now(timezone.utc).isoformat()
        turbines: List[TurbineTelemetry] = []
        valid_power_measurements: List[float] = []
        valid_wind_measurements: List[float] = []

        measurable_fields = [
            "active_power_kw",
            "wind_speed_mps",
            "rotor_rpm",
            "nacelle_yaw_deg",
            "gearbox_bearing_temp_c",
            "generator_winding_temp_c",
            "pitch_angle_deg"
        ]

        for idx, (t_name, field_map) in enumerate(tag_mappings.items()):
            field_values: Dict[str, Optional[float]] = {}
            field_quality: Dict[str, str] = {}
            field_units: Dict[str, str] = {}
            field_timestamps: Dict[str, str] = {}
            feeder = "Feeder A" if idx < 4 else "Feeder B"
            primary_timestamp = now_utc

            for f_name in measurable_fields:
                wid = field_map.get(f_name)
                if not wid:
                    field_values[f_name] = None
                    field_quality[f_name] = "NOT_CONFIGURED"
                    continue

                val_obj = point_values.get(wid)
                if not val_obj:
                    field_values[f_name] = None
                    field_quality[f_name] = "UNAVAILABLE"
                    continue

                if val_obj.Timestamp:
                    field_timestamps[f_name] = val_obj.Timestamp
                    primary_timestamp = val_obj.Timestamp
                if val_obj.UnitsAbbreviation:
                    field_units[f_name] = val_obj.UnitsAbbreviation

                # Strict quality assessment
                if not val_obj.Good:
                    field_values[f_name] = None
                    field_quality[f_name] = "BAD_QUALITY"
                elif val_obj.Questionable:
                    field_values[f_name] = None
                    field_quality[f_name] = "QUESTIONABLE"
                else:
                    num_val = val_obj.get_numeric_value()
                    if num_val is None:
                        field_values[f_name] = None
                        field_quality[f_name] = "NON_NUMERIC_DIGITAL_STATE"
                    else:
                        field_values[f_name] = num_val
                        field_quality[f_name] = "GOOD"

            active_power = field_values.get("active_power_kw")
            wind_speed = field_values.get("wind_speed_mps")

            if active_power is not None:
                valid_power_measurements.append(active_power)
            if wind_speed is not None:
                valid_wind_measurements.append(wind_speed)

            # Expected power is derived strictly from measured wind speed if valid
            expected_power = None
            power_loss_delta = None
            if wind_speed is not None:
                if wind_speed < 3.0 or wind_speed > 25.0:
                    expected_power = 0.0
                elif wind_speed >= 12.0:
                    expected_power = 2500.0
                else:
                    expected_power = round(2500.0 * ((wind_speed - 3.0) / 9.0) ** 3, 1)

                if active_power is not None:
                    power_loss_delta = round(max(0.0, expected_power - active_power), 1)

            status_val = "NORMAL"
            if active_power is None and wind_speed is None:
                status_val = "UNAVAILABLE"
            elif any(q in ("BAD_QUALITY", "QUESTIONABLE") for q in field_quality.values()):
                status_val = "WARNING"

            turbines.append(TurbineTelemetry(
                turbine_id=idx + 1,
                name=t_name,
                feeder=feeder,
                web_id=field_map.get("active_power_kw", f"PI_WTG_{idx+1}"),
                timestamp=primary_timestamp,
                active_power_kw=active_power,
                wind_speed_mps=wind_speed,
                rotor_rpm=field_values.get("rotor_rpm"),
                nacelle_yaw_deg=field_values.get("nacelle_yaw_deg"),
                gearbox_bearing_temp_c=field_values.get("gearbox_bearing_temp_c"),
                generator_winding_temp_c=field_values.get("generator_winding_temp_c"),
                pitch_angle_deg=field_values.get("pitch_angle_deg"),
                expected_power_kw=expected_power,
                power_loss_delta_kw=power_loss_delta,
                status=status_val,
                quality_good=(status_val == "NORMAL"),
                is_simulated=False,
                field_quality=field_quality,
                field_units=field_units,
                field_timestamps=field_timestamps
            ))

        total_mw = round(sum(valid_power_measurements) / 1000.0, 3) if valid_power_measurements else None
        avg_wind = round(sum(valid_wind_measurements) / len(valid_wind_measurements), 2) if valid_wind_measurements else None
        coverage_pct = round((len(valid_power_measurements) / max(1, len(turbines))) * 100.0, 1) if turbines else 0.0

        cap_factor = None
        if valid_power_measurements:
            cap_factor = round((sum(valid_power_measurements) / (len(valid_power_measurements) * 2500.0)) * 100.0, 1)

        summary = FleetSummary(
            timestamp=now_utc,
            total_active_power_mw=total_mw,
            average_wind_speed_mps=avg_wind,
            capacity_factor_pct=cap_factor,
            turbines_online=len(valid_power_measurements),
            active_event_frames_count=0,
            run_mode="REAL_PI",
            is_simulated=False,
            data_coverage_pct=coverage_pct
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
