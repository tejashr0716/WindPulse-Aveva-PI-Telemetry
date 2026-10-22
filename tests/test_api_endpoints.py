"""
Integration tests for FastAPI REST and WebSocket endpoints in WindPulse.
"""

import json
import pytest
from fastapi.testclient import TestClient

def test_health_check_endpoint(client: TestClient):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "healthy"
    assert data["run_mode"] == "SIMULATION"
    assert data["is_simulated"] is True
    assert "NOT CONNECTED TO PI" in data["disclaimer"]

def test_telemetry_snapshot_endpoint(client: TestClient):
    resp = client.get("/api/telemetry/snapshot")
    assert resp.status_code == 200
    data = resp.json()
    assert "summary" in data
    assert "turbines" in data
    assert len(data["turbines"]) == 8
    assert data["mode"] == "SIMULATION"
    assert data["disclaimer"] == "SIMULATED DATA — NOT CONNECTED TO PI"

def test_get_single_turbine_success(client: TestClient):
    resp = client.get("/api/turbines/1")
    assert resp.status_code == 200
    data = resp.json()
    assert data["turbine_id"] == 1
    assert data["name"] == "WTG-01"
    assert data["feeder"] == "Feeder A"

def test_get_single_turbine_not_found(client: TestClient):
    resp = client.get("/api/turbines/99")
    assert resp.status_code == 404

def test_simulation_wind_control(client: TestClient):
    resp = client.post("/api/simulation/wind", json={"wind_speed_mps": 14.5, "direction_deg": 240.0})
    assert resp.status_code == 200
    
    snap_resp = client.get("/api/telemetry/snapshot")
    snap = snap_resp.json()
    assert abs(snap["summary"]["average_wind_speed_mps"] - 14.5) < 0.5

def test_simulation_trip_and_reset(client: TestClient):
    # Trip turbine 3
    trip_resp = client.post("/api/simulation/trip/3")
    assert trip_resp.status_code == 200

    t3_resp = client.get("/api/turbines/3")
    t3 = t3_resp.json()
    assert t3["status"] == "TRIP"
    assert t3["active_power_kw"] == 0.0

    # Reset fleet
    reset_resp = client.post("/api/simulation/reset")
    assert reset_resp.status_code == 200

    t3_reset_resp = client.get("/api/turbines/3")
    t3_reset = t3_reset_resp.json()
    assert t3_reset["status"] == "NORMAL"

def test_websocket_telemetry_stream(client: TestClient):
    with client.websocket_connect("/ws/telemetry") as ws:
        data = ws.receive_text()
        snap = json.loads(data)
        assert "summary" in snap
        assert "turbines" in snap
        assert len(snap["turbines"]) == 8
        assert snap["mode"] == "SIMULATION"
