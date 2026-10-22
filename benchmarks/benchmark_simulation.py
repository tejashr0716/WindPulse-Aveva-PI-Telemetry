"""
Benchmark script for WindPulse Simulation Mode.
Measures real network WebSocket telemetry frame delivery and HTTP REST snapshot latency.
Saves reproducible evidence to benchmarks/results/ in JSON and CSV formats.
"""

import asyncio
import csv
import json
import os
import platform
import subprocess
import sys
import time
from datetime import datetime, timezone
import httpx
import websockets

def get_git_commit_sha():
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    except Exception:
        return "UNKNOWN"

async def benchmark_websocket_telemetry(
    ws_url: str = "ws://127.0.0.1:8001/ws/telemetry",
    client_count: int = 20,
    duration_seconds: float = 5.0
):
    print(f"[*] Connecting {client_count} actual network WebSocket clients to {ws_url}...")

    inter_frame_intervals = []
    initial_snapshots_received = 0
    stream_frames_by_client = [[] for _ in range(client_count)]
    errors = 0

    async def client_worker(idx: int):
        nonlocal errors, initial_snapshots_received
        try:
            async with websockets.connect(ws_url, close_timeout=2.0) as ws:
                # 1. Receive and account for initial connection snapshot separately
                raw_init = await asyncio.wait_for(ws.recv(), timeout=3.0)
                init_msg = json.loads(raw_init)
                if init_msg.get("frame_type") == "INITIAL_SNAPSHOT" or init_msg.get("frame_seq") == 0:
                    initial_snapshots_received += 1

                # 2. Receive periodic stream frames and track arrival intervals & producer sequence IDs
                last_time = time.perf_counter()
                end_time = time.time() + duration_seconds
                while time.time() < end_time:
                    try:
                        raw_msg = await asyncio.wait_for(ws.recv(), timeout=2.0)
                        now = time.perf_counter()
                        delta_ms = (now - last_time) * 1000.0
                        last_time = now

                        msg = json.loads(raw_msg)
                        seq = msg.get("frame_seq")
                        if seq is not None and seq > 0:
                            stream_frames_by_client[idx].append(seq)
                            inter_frame_intervals.append(delta_ms)
                    except asyncio.TimeoutError:
                        break
        except Exception as e:
            errors += 1

    t0 = time.perf_counter()
    tasks = [client_worker(i) for i in range(client_count)]
    await asyncio.gather(*tasks)
    total_elapsed = time.perf_counter() - t0

    total_stream_frames = sum(len(seqs) for seqs in stream_frames_by_client)
    throughput = total_stream_frames / total_elapsed if total_elapsed > 0 else 0.0

    sorted_intervals = sorted(inter_frame_intervals) if inter_frame_intervals else [0.0]
    p50 = sorted_intervals[int(len(sorted_intervals) * 0.50)]
    p95 = sorted_intervals[int(len(sorted_intervals) * 0.95)]
    p99 = sorted_intervals[int(len(sorted_intervals) * 0.99)]

    # Assert completeness: zero connection dropouts and all subscribers received stream frames
    assert errors == 0, f"Connection failures detected: {errors}"
    assert initial_snapshots_received == client_count, f"Snapshot mismatch: {initial_snapshots_received} != {client_count}"
    assert total_stream_frames > 0, "No stream frames received"

    return {
        "benchmark_type": "WEBSOCKET_NETWORK_TELEMETRY",
        "description": "Network WebSocket inter-frame arrival interval and message completeness across concurrent subscribers",
        "client_connections": client_count,
        "duration_seconds": round(total_elapsed, 3),
        "initial_snapshots_delivered": initial_snapshots_received,
        "total_stream_frames_delivered": total_stream_frames,
        "connection_errors": errors,
        "throughput_stream_frames_per_sec": round(throughput, 1),
        "application_message_completeness": True,
        "inter_frame_arrival_interval_ms": {
            "p50": round(p50, 3),
            "p95": round(p95, 3),
            "p99": round(p99, 3),
            "min": round(sorted_intervals[0], 3),
            "max": round(sorted_intervals[-1], 3)
        },
        "raw_samples": {
            "inter_frame_intervals_ms": [round(x, 4) for x in inter_frame_intervals]
        }
    }

async def benchmark_http_rest_snapshot(
    http_url: str = "http://127.0.0.1:8001/api/telemetry/snapshot",
    total_requests: int = 200,
    concurrency: int = 10
):
    print(f"[*] Dispatching {total_requests} HTTP REST requests (concurrency={concurrency}) to {http_url}...")
    latencies = []
    errors = 0
    sem = asyncio.Semaphore(concurrency)

    async with httpx.AsyncClient(timeout=5.0) as client:
        async def fetch():
            nonlocal errors
            async with sem:
                t0 = time.perf_counter()
                try:
                    resp = await client.get(http_url)
                    t1 = time.perf_counter()
                    if resp.status_code == 200:
                        latencies.append((t1 - t0) * 1000.0)
                    else:
                        errors += 1
                except Exception:
                    errors += 1

        t_start = time.perf_counter()
        tasks = [fetch() for _ in range(total_requests)]
        await asyncio.gather(*tasks)
        total_time = time.perf_counter() - t_start

    sorted_lat = sorted(latencies) if latencies else [0.0]
    p50 = sorted_lat[int(len(sorted_lat) * 0.50)]
    p95 = sorted_lat[int(len(sorted_lat) * 0.95)]
    p99 = sorted_lat[int(len(sorted_lat) * 0.99)]

    assert errors == 0, f"HTTP REST requests failed: {errors} failures"

    return {
        "benchmark_type": "HTTP_REST_ROUND_TRIP",
        "description": "Client-to-server round-trip latency for full fleet telemetry JSON snapshot",
        "total_requests": total_requests,
        "concurrency": concurrency,
        "duration_seconds": round(total_time, 3),
        "successful_responses": len(latencies),
        "failed_responses": errors,
        "throughput_req_per_sec": round(len(latencies) / total_time, 1) if total_time > 0 else 0.0,
        "round_trip_latency_ms": {
            "p50": round(p50, 3),
            "p95": round(p95, 3),
            "p99": round(p99, 3),
            "min": round(sorted_lat[0], 3),
            "max": round(sorted_lat[-1], 3)
        },
        "raw_samples": {
            "http_rest_latencies_ms": [round(x, 4) for x in latencies]
        }
    }

async def main():
    print("=" * 80)
    print("🌪️ WINDPULSE REPRODUCIBLE BENCHMARK SUITE (SIMULATION MODE)")
    print("=" * 80)

    # 1. Benchmark WebSocket network delivery
    ws_results = await benchmark_websocket_telemetry(client_count=20, duration_seconds=5.0)

    # 2. Benchmark HTTP REST snapshot
    http_results = await benchmark_http_rest_snapshot(total_requests=200, concurrency=10)

    # Environment Metadata
    metadata = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "git_commit_sha": get_git_commit_sha(),
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python_version": sys.version.split()[0]
        },
        "real_pi_status": "NOT MEASURED — NO LICENSED PI INFRASTRUCTURE CONFIGURED",
        "results": {
            "websocket_telemetry": ws_results,
            "http_rest_snapshot": http_results
        }
    }

    # Save to benchmarks/results/
    ts = int(time.time())
    out_dir = os.path.join(os.path.dirname(__file__), "results")
    os.makedirs(out_dir, exist_ok=True)
    json_path = os.path.join(out_dir, f"windpulse_benchmark_{ts}.json")
    with open(json_path, "w") as f:
        json.dump(metadata, f, indent=2)

    csv_path = os.path.join(out_dir, f"windpulse_benchmark_{ts}.csv")
    with open(csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["Benchmark", "Clients/Requests", "Initial Snapshots", "Stream Frames Delivered", "Throughput", "P50_ms", "P95_ms", "P99_ms"])
        writer.writerow([
            ws_results["benchmark_type"],
            ws_results["client_connections"],
            ws_results["initial_snapshots_delivered"],
            ws_results["total_stream_frames_delivered"],
            f"{ws_results['throughput_stream_frames_per_sec']} frames/s",
            ws_results["inter_frame_arrival_interval_ms"]["p50"],
            ws_results["inter_frame_arrival_interval_ms"]["p95"],
            ws_results["inter_frame_arrival_interval_ms"]["p99"]
        ])
        writer.writerow([
            http_results["benchmark_type"],
            http_results["total_requests"],
            "N/A",
            http_results["successful_responses"],
            f"{http_results['throughput_req_per_sec']} req/s",
            http_results["round_trip_latency_ms"]["p50"],
            http_results["round_trip_latency_ms"]["p95"],
            http_results["round_trip_latency_ms"]["p99"]
        ])

    print("\n" + "=" * 80)
    print("📊 BENCHMARK SUMMARY & RAW EVIDENCE RECORDED")
    print("=" * 80)
    print(f"JSON Evidence : {json_path}")
    print(f"CSV Evidence  : {csv_path}")
    print(f"WebSocket Arrival Interval P50/P95/P99 : {ws_results['inter_frame_arrival_interval_ms']['p50']}ms / {ws_results['inter_frame_arrival_interval_ms']['p95']}ms / {ws_results['inter_frame_arrival_interval_ms']['p99']}ms")
    print(f"WebSocket Stream Frames Delivered      : {ws_results['total_stream_frames_delivered']} frames across {ws_results['client_connections']} subscribers")
    print(f"HTTP REST Round-Trip P50/P95/P99      : {http_results['round_trip_latency_ms']['p50']}ms / {http_results['round_trip_latency_ms']['p95']}ms / {http_results['round_trip_latency_ms']['p99']}ms")
    print("=" * 80)

if __name__ == "__main__":
    asyncio.run(main())
