"""
Pytest configuration and test doubles for WindPulse.
"""

import sys
import os
import pytest
from fastapi.testclient import TestClient

# Add project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.main import app
from src.simulation import simulation_engine

@pytest.fixture(autouse=True)
def reset_simulation():
    """Resets the simulation engine state before each test."""
    simulation_engine.reset_fleet()
    simulation_engine.ambient_wind_speed = 11.5
    simulation_engine.ambient_wind_direction = 230.0

@pytest.fixture
def client():
    """FastAPI TestClient fixture."""
    with TestClient(app) as test_client:
        yield test_client
