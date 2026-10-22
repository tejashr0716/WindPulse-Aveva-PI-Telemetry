"""
Configuration module for WindPulse telemetry system.
Explicitly distinguishes between SIMULATION mode and REAL_PI mode.
"""

from typing import List, Literal, Optional
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore"
    )

    RUN_MODE: Literal["SIMULATION", "REAL_PI"] = Field(
        default="SIMULATION",
        description="Operating mode. In SIMULATION mode, turbine data is generated locally by physics engine."
    )
    HOST: str = Field(default="0.0.0.0", description="API server host")
    PORT: int = Field(default=8001, description="API server port")

    # PI Web API Connection (Applicable in REAL_PI mode)
    PI_WEB_API_URL: Optional[str] = Field(
        default=None,
        description="Base URL for AVEVA PI Web API instance (e.g. https://pi-server/piwebapi)"
    )
    PI_AUTH_TYPE: Literal["BASIC", "BEARER", "KERBEROS"] = Field(
        default="BASIC",
        description="Authentication method configured on PI Web API"
    )
    PI_USERNAME: Optional[str] = Field(default=None, description="Username for Basic auth")
    PI_PASSWORD: Optional[str] = Field(default=None, description="Password for Basic auth")
    PI_BEARER_TOKEN: Optional[str] = Field(default=None, description="OAuth2 Bearer token")
    PI_VERIFY_SSL: bool = Field(default=True, description="Enforce strict TLS certificate verification")
    PI_REQUEST_TIMEOUT_SECONDS: float = Field(default=10.0, description="HTTP request timeout in seconds")
    PI_MAX_RETRIES: int = Field(default=3, description="Maximum bounded retries for idempotent requests")
    PI_RETRY_BACKOFF_FACTOR: float = Field(default=0.5, description="Exponential backoff factor")

    # Safety Guards for Write Operations
    PI_ALLOW_WRITES: bool = Field(
        default=False,
        description="CRITICAL SAFETY GUARD: Must be explicitly enabled to permit writing to PI Points."
    )
    PI_APPROVED_WRITE_POINTS: List[str] = Field(
        default_factory=list,
        description="Whitelist of specific PI Point WebIDs or tag names permitted for write testing."
    )

    # Telemetry and Simulation Parameters
    TELEMETRY_INTERVAL_HZ: float = Field(default=10.0, description="WebSocket streaming frequency in Hz")
    SIMULATED_TURBINES_COUNT: int = Field(default=8, description="Number of turbines in simulated fleet")
    AIR_DENSITY_KG_M3: float = Field(default=1.225, description="Standard sea-level air density")
    ROTOR_RADIUS_M: float = Field(default=60.0, description="Turbine blade radius (120m diameter)")

    @field_validator("PI_WEB_API_URL")
    @classmethod
    def validate_pi_url(cls, v, info):
        if info.data.get("RUN_MODE") == "REAL_PI" and not v:
            raise ValueError("PI_WEB_API_URL must be specified when RUN_MODE is REAL_PI")
        return v

settings = Settings()
