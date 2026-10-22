"""
Synthetic Turbine Physics & Telemetry Generator for WindPulse.
Implements first-principles aerodynamics (Betz limit, cubic power curve, yaw cosine losses)
and automated Event Frame generation.
All output is explicitly marked as simulated data.
"""

import logging
import math
import random
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional
from src.config import settings
from src.models import EventFrameRecord, FleetSummary, TelemetrySnapshot, TurbineTelemetry

logger = logging.getLogger("windpulse.simulation")

class TurbineSimulationEngine:
    def __init__(self, count: int = 8):
        self.count = count
        self.ambient_wind_speed: float = 11.5
        self.ambient_wind_direction: float = 230.0 # degrees SW
        self.gust_decay: float = 0.0
        self.start_epoch: float = time.time()
        self.step_counter: int = 0
        self.event_frames: List[EventFrameRecord] = []
        
        # State per turbine
        self.turbines_state: List[Dict] = []
        for i in range(1, count + 1):
            feeder = "Feeder A" if i <= 4 else "Feeder B"
            self.turbines_state.append({
                "id": i,
                "name": f"WTG-{i:02d}",
                "feeder": feeder,
                "web_id": f"SIM_P0_BIGHORN_{feeder.replace(' ', '')}_WTG{i:02d}",
                "nacelle_yaw_deg": 228.0 + (i * 0.5),
                "gearbox_temp_c": 68.0,
                "gen_temp_c": 74.0,
                "status": "NORMAL",
                "forced_trip": False,
                "forced_yaw_error": 0.0,
                "blade_angle": random.uniform(0, 360)
            })

    def trigger_wind_gust(self, delta_mps: float = 7.5):
        """Simulates an offshore atmospheric wind gust."""
        self.gust_decay = delta_mps
        self._check_event_frames()

    def set_ambient_wind(self, speed_mps: float, direction_deg: float):
        """Updates ambient meteorological conditions."""
        self.ambient_wind_speed = max(0.0, min(35.0, float(speed_mps)))
        self.ambient_wind_direction = float(direction_deg) % 360.0

    def simulate_yaw_error(self, turbine_id: int, error_deg: float = 25.0):
        """Forces yaw tracking misalignment on a specific turbine."""
        for t in self.turbines_state:
            if t["id"] == turbine_id:
                t["forced_yaw_error"] = error_deg
                t["status"] = "WARNING"
                self._record_event_frame(
                    turbine_id=turbine_id,
                    turbine_name=t["name"],
                    event_type="YAW_MISALIGNMENT_EXCURSION",
                    trigger=f"NacelleYaw - WindDir ({error_deg:.1f}°) exceeds 15.0° threshold",
                    severity="WARNING"
                )

    def trip_turbine(self, turbine_id: int):
        """Forces an emergency shutdown / trip (e.g. bearing thermal exceedance)."""
        for t in self.turbines_state:
            if t["id"] == turbine_id:
                t["forced_trip"] = True
                t["gearbox_temp_c"] = 88.5
                t["status"] = "TRIP"
                self._record_event_frame(
                    turbine_id=turbine_id,
                    turbine_name=t["name"],
                    event_type="GEARBOX_OVERHEAT_TRIP",
                    trigger="Gearbox bearing temp (88.5°C) exceeds safety limit (80.0°C)",
                    severity="CRITICAL"
                )

    def reset_fleet(self):
        """Clears all induced faults and realigns yaw systems."""
        for t in self.turbines_state:
            t["status"] = "NORMAL"
            t["forced_trip"] = False
            t["forced_yaw_error"] = 0.0
            t["gearbox_temp_c"] = 68.0
            t["nacelle_yaw_deg"] = self.ambient_wind_direction
        self.gust_decay = 0.0

    def _record_event_frame(self, turbine_id: int, turbine_name: str, event_type: str, trigger: str, severity: str):
        ef_id = f"EF-{int(time.time() * 1000) % 100000:05d}"
        now_str = datetime.now(timezone.utc).isoformat()
        logger.info(f"[SIMULATED EVENT FRAME] {event_type} on {turbine_name}: {trigger}")
        self.event_frames.insert(0, EventFrameRecord(
            id=ef_id,
            turbine_id=turbine_id,
            turbine_name=turbine_name,
            event_type=event_type,
            trigger_condition=trigger,
            start_time=now_str,
            duration_seconds=0.0,
            severity=severity,
            acknowledged=False,
            is_simulated=True
        ))
        if len(self.event_frames) > 20:
            self.event_frames = self.event_frames[:20]

    def _check_event_frames(self):
        effective_wind = self.ambient_wind_speed + self.gust_decay
        if effective_wind > 25.0:
            for t in self.turbines_state:
                if t["status"] != "TRIP":
                    t["status"] = "CURTAILED"
                    self._record_event_frame(
                        turbine_id=t["id"],
                        turbine_name=t["name"],
                        event_type="HIGH_WIND_CUTOUT",
                        trigger=f"Wind speed ({effective_wind:.1f} m/s) exceeded cut-out limit (25.0 m/s)",
                        severity="WARNING"
                    )

    def step(self) -> TelemetrySnapshot:
        """Executes one simulation tick and returns standard telemetry snapshot."""
        self.step_counter += 1
        now_utc = datetime.now(timezone.utc).isoformat()
        
        # Slowly decay wind gust back to ambient
        if self.gust_decay > 0.05:
            self.gust_decay *= 0.95
        else:
            self.gust_decay = 0.0

        effective_base_wind = self.ambient_wind_speed + self.gust_decay
        
        turbines_telemetry: List[TurbineTelemetry] = []
        total_kw = 0.0
        online_count = 0

        for t in self.turbines_state:
            # Turbine spatial micro-turbulence variation
            local_wind = max(0.0, effective_base_wind + math.sin(self.step_counter * 0.1 + t["id"]) * 0.4)
            
            # Yaw alignment
            target_yaw = self.ambient_wind_direction + t["forced_yaw_error"]
            yaw_diff = abs(target_yaw - t["nacelle_yaw_deg"])
            if yaw_diff > 0.2:
                # Motor yaw slewing at 0.5 deg/sec
                slew_step = 0.1 if target_yaw > t["nacelle_yaw_deg"] else -0.1
                t["nacelle_yaw_deg"] += slew_step

            # Aerodynamic Power Curve Model (2.5 MW OEM turbine profile)
            # Cut-in: 3.0 m/s, Rated: 12.0 m/s, Cut-out: 25.0 m/s
            rated_power_kw = 2500.0
            cp_aerodynamic = 0.442 # Typical OEM efficiency (Betz max is 0.593)
            swept_area_m2 = math.pi * (settings.ROTOR_RADIUS_M ** 2)

            # Theoretical raw kinetic wind power
            theoretical_wind_power_kw = 0.5 * settings.AIR_DENSITY_KG_M3 * swept_area_m2 * (local_wind ** 3) * (0.593) / 1000.0
            
            # Operating curve power
            if local_wind < 3.0 or local_wind > 25.0 or t["forced_trip"]:
                active_power_kw = 0.0
                rotor_rpm = 0.0
                pitch_angle = 90.0 if (local_wind > 25.0 or t["forced_trip"]) else 0.0
            elif local_wind >= 12.0:
                active_power_kw = rated_power_kw
                rotor_rpm = 16.5
                # Blade pitch active regulation to shed excess aerodynamic lift
                pitch_angle = (local_wind - 12.0) * 1.8
            else:
                # Cubic increase region between cut-in and rated
                active_power_kw = math.pow((local_wind - 3.0) / 9.0, 3) * rated_power_kw
                rotor_rpm = 8.0 + ((local_wind - 3.0) / 9.0) * 8.5
                pitch_angle = 0.0

            # Apply yaw misalignment loss factor: cos^2(theta)
            yaw_loss_rad = math.radians(abs(t["nacelle_yaw_deg"] - self.ambient_wind_direction))
            yaw_efficiency_factor = max(0.5, math.cos(yaw_loss_rad) ** 2)
            active_power_kw *= yaw_efficiency_factor

            # Temperature and cooling simulation
            load_factor = active_power_kw / rated_power_kw
            if t["forced_trip"]:
                t["gearbox_temp_c"] = max(65.0, t["gearbox_temp_c"] - 0.1)
                t["gen_temp_c"] = max(70.0, t["gen_temp_c"] - 0.1)
            else:
                target_gb_temp = 65.0 + (load_factor * 12.0)
                t["gearbox_temp_c"] += (target_gb_temp - t["gearbox_temp_c"]) * 0.05
                target_gen_temp = 70.0 + (load_factor * 15.0)
                t["gen_temp_c"] += (target_gen_temp - t["gen_temp_c"]) * 0.05

            expected_power_kw = min(rated_power_kw, theoretical_wind_power_kw * (cp_aerodynamic / 0.593))
            power_loss_delta_kw = max(0.0, expected_power_kw - active_power_kw)

            if t["status"] != "TRIP":
                online_count += 1
            total_kw += active_power_kw

            turbines_telemetry.append(TurbineTelemetry(
                turbine_id=t["id"],
                name=t["name"],
                feeder=t["feeder"],
                web_id=t["web_id"],
                timestamp=now_utc,
                active_power_kw=round(active_power_kw, 2),
                wind_speed_mps=round(local_wind, 2),
                rotor_rpm=round(rotor_rpm, 2),
                nacelle_yaw_deg=round(t["nacelle_yaw_deg"], 1),
                gearbox_bearing_temp_c=round(t["gearbox_temp_c"], 1),
                generator_winding_temp_c=round(t["gen_temp_c"], 1),
                pitch_angle_deg=round(pitch_angle, 1),
                expected_power_kw=round(expected_power_kw, 2),
                power_loss_delta_kw=round(power_loss_delta_kw, 2),
                status=t["status"],
                quality_good=True,
                is_simulated=True
            ))

        total_mw = round(total_kw / 1000.0, 3)
        max_possible_mw = (self.count * 2500.0) / 1000.0
        cap_factor = round((total_mw / max_possible_mw) * 100.0, 1) if max_possible_mw > 0 else 0.0

        summary = FleetSummary(
            timestamp=now_utc,
            total_active_power_mw=total_mw,
            average_wind_speed_mps=round(effective_base_wind, 2),
            capacity_factor_pct=cap_factor,
            turbines_online=online_count,
            active_event_frames_count=len([ef for ef in self.event_frames if not ef.acknowledged]),
            run_mode="SIMULATION",
            is_simulated=True
        )

        return TelemetrySnapshot(
            summary=summary,
            turbines=turbines_telemetry,
            event_frames=self.event_frames,
            mode="SIMULATION",
            disclaimer="SIMULATED DATA — NOT CONNECTED TO PI"
        )

simulation_engine = TurbineSimulationEngine(count=settings.SIMULATED_TURBINES_COUNT)
