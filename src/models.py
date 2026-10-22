"""
Data schemas and models for WindPulse telemetry system.
Includes explicit models for PI Web API time-series values with quality flags,
and models for simulated turbine telemetry.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional, Union
from pydantic import BaseModel, Field

# --- AVEVA PI WEB API SCHEMAS ---

class PIStreamValue(BaseModel):
    """
    Standard AVEVA PI Web API Stream Value response schema.
    PI status flags:
      - Good: Indicates whether the value is valid. If False, Value may contain a PI System Digital State (e.g. 'Scan Off', 'I/O Timeout').
      - Questionable: Indicates uncertain accuracy.
      - Substituted: Indicates manual or synthetic override.
    """
    Timestamp: str = Field(..., description="ISO 8601 UTC timestamp of the measurement")
    Value: Union[float, int, str, bool, Dict[str, Any]] = Field(..., description="Measurement value or digital state")
    UnitsAbbreviation: Optional[str] = Field(default=None, description="Engineering units")
    Good: bool = Field(default=True, description="True if value is valid engineering measurement")
    Questionable: bool = Field(default=False, description="True if measurement accuracy is questionable")
    Substituted: bool = Field(default=False, description="True if value was manually substituted")
    Annotated: bool = Field(default=False, description="True if an annotation is attached")

    def get_numeric_value(self) -> Optional[float]:
        """
        Safely extracts numeric value. Returns None if the value is bad or a system state.
        Never treats failed or questionable values as valid readings.
        """
        if not self.Good:
            return None
        if isinstance(self.Value, (int, float)):
            return float(self.Value)
        try:
            return float(self.Value)
        except (ValueError, TypeError):
            return None

class PIPointWriteRequest(BaseModel):
    web_id: str = Field(..., description="WebID of the target PI Point or Attribute")
    value: float = Field(..., description="Numeric value to write")
    timestamp: Optional[str] = Field(default=None, description="Optional ISO timestamp; defaults to current UTC")

class PIPointWriteResponse(BaseModel):
    web_id: str
    written_value: float
    timestamp: str
    read_back_value: Optional[float] = None
    verified: bool = False
    message: str

# --- TURBINE FLEET TELEMETRY SCHEMAS ---

class TurbineTelemetry(BaseModel):
    turbine_id: int
    name: str
    feeder: str
    web_id: str
    timestamp: str
    active_power_kw: float
    wind_speed_mps: float
    rotor_rpm: float
    nacelle_yaw_deg: float
    gearbox_bearing_temp_c: float
    generator_winding_temp_c: float
    pitch_angle_deg: float
    expected_power_kw: float
    power_loss_delta_kw: float
    status: Literal["NORMAL", "WARNING", "TRIP", "CURTAILED"]
    quality_good: bool = True
    is_simulated: bool = True

class FleetSummary(BaseModel):
    timestamp: str
    total_active_power_mw: float
    average_wind_speed_mps: float
    capacity_factor_pct: float
    turbines_online: int
    active_event_frames_count: int
    run_mode: str
    is_simulated: bool

class EventFrameRecord(BaseModel):
    id: str
    turbine_id: int
    turbine_name: str
    event_type: str
    trigger_condition: str
    start_time: str
    duration_seconds: float
    severity: Literal["INFO", "WARNING", "CRITICAL"]
    acknowledged: bool = False

class TelemetrySnapshot(BaseModel):
    summary: FleetSummary
    turbines: List[TurbineTelemetry]
    event_frames: List[EventFrameRecord]
    mode: str
    disclaimer: str = "SIMULATED DATA — NOT CONNECTED TO PI"
