"""
FastAPI application entry point for WindPulse Industrial Telemetry Engine.
Serves REST and WebSocket endpoints for turbine fleet monitoring.
"""

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from typing import Dict, List, Optional
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from src.config import settings
from src.models import (
    FleetSummary,
    PIPointWriteRequest,
    PIPointWriteResponse,
    TelemetrySnapshot,
    TurbineTelemetry,
)
from src.pi_web_api.client import PIWritePermissionError
from src.pi_web_api.service import telemetry_service
from src.simulation import simulation_engine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("windpulse.main")

# Active WebSocket subscriber connections
active_connections: List[WebSocket] = []

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"Starting WindPulse Telemetry Engine in {settings.RUN_MODE} mode...")
    await telemetry_service.initialize()
    
    # Background broadcaster task for WebSockets
    broadcaster_task = asyncio.create_task(telemetry_broadcast_loop())
    yield
    logger.info("Shutting down WindPulse Telemetry Engine...")
    broadcaster_task.cancel()
    try:
        await broadcaster_task
    except asyncio.CancelledError:
        pass
    await telemetry_service.shutdown()

async def telemetry_broadcast_loop():
    """Streams continuous telemetry snapshots to connected WebSocket clients."""
    interval = 1.0 / max(1.0, min(60.0, settings.TELEMETRY_INTERVAL_HZ))
    while True:
        try:
            if active_connections:
                snapshot = await telemetry_service.get_current_snapshot()
                payload = snapshot.model_dump_json()
                disconnected = []
                for ws in active_connections:
                    try:
                        await ws.send_text(payload)
                    except Exception:
                        disconnected.append(ws)
                for ws in disconnected:
                    if ws in active_connections:
                        active_connections.remove(ws)
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in telemetry broadcast loop: {e}")
            await asyncio.sleep(1.0)

app = FastAPI(
    title="WindPulse Telemetry Engine",
    description="AVEVA OSIsoft PI Telemetry Engine for Offshore Wind Turbines",
    version="1.0.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- REST ENDPOINTS ---

@app.get("/api/health")
async def health_check():
    return {
        "status": "healthy",
        "run_mode": settings.RUN_MODE,
        "is_simulated": (settings.RUN_MODE == "SIMULATION"),
        "disclaimer": "SIMULATED DATA — NOT CONNECTED TO PI" if settings.RUN_MODE == "SIMULATION" else "CONNECTED TO PRODUCTION PI",
        "active_ws_connections": len(active_connections),
        "telemetry_hz": settings.TELEMETRY_INTERVAL_HZ
    }

@app.get("/api/telemetry/snapshot", response_model=TelemetrySnapshot)
async def get_snapshot():
    return await telemetry_service.get_current_snapshot()

@app.get("/api/turbines", response_model=List[TurbineTelemetry])
async def list_turbines():
    snap = await telemetry_service.get_current_snapshot()
    return snap.turbines

@app.get("/api/turbines/{turbine_id}", response_model=TurbineTelemetry)
async def get_turbine(turbine_id: int):
    snap = await telemetry_service.get_current_snapshot()
    for t in snap.turbines:
        if t.turbine_id == turbine_id:
            return t
    raise HTTPException(status_code=404, detail=f"Turbine WTG-{turbine_id:02d} not found")

@app.get("/api/event-frames")
async def list_event_frames():
    snap = await telemetry_service.get_current_snapshot()
    return snap.event_frames

@app.get("/api/af/hierarchy")
async def get_af_hierarchy():
    """Returns the Asset Framework element structure."""
    return {
        "database": "GlobalCleanEnergy",
        "server": "PISYSTEM01",
        "description": "Bighorn Offshore Wind Farm Asset Hierarchy",
        "is_simulated_model": True,
        "root": {
            "name": "Bighorn Offshore Wind Farm",
            "feeders": [
                {
                    "name": "Feeder A",
                    "turbines": [f"WTG-0{i}" for i in range(1, 5)],
                    "template": "WindTurbine_Template"
                },
                {
                    "name": "Feeder B",
                    "turbines": [f"WTG-0{i}" for i in range(5, 9)],
                    "template": "WindTurbine_Template"
                }
            ]
        }
    }

class WindControlRequest(BaseModel):
    wind_speed_mps: float
    direction_deg: float

@app.post("/api/simulation/wind")
async def set_simulation_wind(req: WindControlRequest):
    simulation_engine.set_ambient_wind(req.wind_speed_mps, req.direction_deg)
    return {"message": "Ambient wind conditions updated", "wind_speed": req.wind_speed_mps, "direction": req.direction_deg}

@app.post("/api/simulation/gust")
async def trigger_wind_gust():
    simulation_engine.trigger_wind_gust(delta_mps=7.5)
    return {"message": "Wind gust (+7.5 m/s) injected"}

class YawErrorRequest(BaseModel):
    turbine_id: int
    error_deg: float = 25.0

@app.post("/api/simulation/yaw-error")
async def trigger_yaw_error(req: YawErrorRequest):
    simulation_engine.simulate_yaw_error(req.turbine_id, req.error_deg)
    return {"message": f"Yaw error (+{req.error_deg}°) injected on turbine {req.turbine_id}"}

@app.post("/api/simulation/trip/{turbine_id}")
async def trigger_turbine_trip(turbine_id: int):
    simulation_engine.trip_turbine(turbine_id)
    return {"message": f"Emergency trip injected on turbine {turbine_id}"}

@app.post("/api/simulation/reset")
async def reset_simulation():
    simulation_engine.reset_fleet()
    return {"message": "Fleet simulation reset to normal baseline"}

@app.post("/api/pi/write-test", response_model=PIPointWriteResponse)
async def write_pi_point(req: PIPointWriteRequest):
    """Guarded endpoint to write a value to a test PI point with read-back verification."""
    try:
        return await telemetry_service.write_test_point(req)
    except PIWritePermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))

# --- WEBSOCKET ENDPOINT ---

@app.websocket("/ws/telemetry")
async def websocket_telemetry_endpoint(websocket: WebSocket):
    await websocket.accept()
    active_connections.append(websocket)
    logger.info(f"WebSocket client connected. Active connections: {len(active_connections)}")
    try:
        # Send initial snapshot immediately upon connect
        initial_snap = await telemetry_service.get_current_snapshot()
        await websocket.send_text(initial_snap.model_dump_json())
        
        while True:
            # Handle client commands if sent
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
                cmd = msg.get("command")
                if cmd == "GUST":
                    simulation_engine.trigger_wind_gust(delta_mps=7.5)
                elif cmd == "RESET":
                    simulation_engine.reset_fleet()
            except Exception:
                pass
    except WebSocketDisconnect:
        pass
    finally:
        if websocket in active_connections:
            active_connections.remove(websocket)
        logger.info(f"WebSocket client disconnected. Active connections: {len(active_connections)}")

# Mount static frontend
app.mount("/static", StaticFiles(directory="public"), name="static")

@app.get("/")
async def serve_index():
    return FileResponse("public/index.html")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("src.main:app", host=settings.HOST, port=settings.PORT, reload=False)
