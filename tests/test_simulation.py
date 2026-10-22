"""
Unit tests for WindPulse turbine physics simulation engine.
Verifies Betz limit calculations, operating power curves, yaw losses, and Event Frames.
"""

import pytest
from src.simulation import simulation_engine

def test_aerodynamic_power_curve_cut_in():
    """Below 3.0 m/s (cut-in), power and rotor RPM must be 0."""
    simulation_engine.set_ambient_wind(speed_mps=2.5, direction_deg=230.0)
    snap = simulation_engine.step()
    for t in snap.turbines:
        assert t.active_power_kw == 0.0
        assert t.rotor_rpm == 0.0

def test_aerodynamic_power_curve_rated():
    """At rated wind speed (12.0 m/s), turbines with aligned yaw should produce rated power ~2500 kW."""
    simulation_engine.set_ambient_wind(speed_mps=12.0, direction_deg=230.0)
    # Step a few times to let yaw settle
    for _ in range(5):
        snap = simulation_engine.step()
    
    # Active power should approach or reach 2500 kW (accounting for micro-turbulence)
    avg_power = sum(t.active_power_kw for t in snap.turbines) / len(snap.turbines)
    assert 2200.0 <= avg_power <= 2500.0
    assert snap.summary.capacity_factor_pct > 80.0

def test_aerodynamic_power_curve_cut_out():
    """Above 25.0 m/s (cut-out), turbines must feather blades and shut down to 0 kW."""
    simulation_engine.set_ambient_wind(speed_mps=26.0, direction_deg=230.0)
    snap = simulation_engine.step()
    for t in snap.turbines:
        assert t.active_power_kw == 0.0
        assert t.pitch_angle_deg == 90.0 # Feathered

def test_yaw_misalignment_induces_event_frame_and_loss():
    """Forced yaw misalignment must reduce output power and trigger an Event Frame."""
    simulation_engine.set_ambient_wind(speed_mps=10.0, direction_deg=230.0)
    snap_baseline = simulation_engine.step()
    baseline_power = snap_baseline.turbines[1].active_power_kw

    # Induce 25 degree yaw error on Turbine 2
    simulation_engine.simulate_yaw_error(turbine_id=2, error_deg=25.0)
    snap_misaligned = simulation_engine.step()
    misaligned_turbine = [t for t in snap_misaligned.turbines if t.turbine_id == 2][0]

    assert misaligned_turbine.status == "WARNING"
    assert misaligned_turbine.active_power_kw < baseline_power
    assert any(ef.event_type == "YAW_MISALIGNMENT_EXCURSION" for ef in snap_misaligned.event_frames)

def test_emergency_trip_stops_turbine():
    """An emergency trip must drop power and RPM to 0 and record a CRITICAL Event Frame."""
    simulation_engine.trip_turbine(turbine_id=4)
    snap = simulation_engine.step()
    tripped_turbine = [t for t in snap.turbines if t.turbine_id == 4][0]

    assert tripped_turbine.status == "TRIP"
    assert tripped_turbine.active_power_kw == 0.0
    assert tripped_turbine.rotor_rpm == 0.0
    assert any(ef.event_type == "GEARBOX_OVERHEAT_TRIP" and ef.severity == "CRITICAL" for ef in snap.event_frames)
