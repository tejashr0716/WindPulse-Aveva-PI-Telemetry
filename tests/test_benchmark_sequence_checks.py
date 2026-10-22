"""
Regression tests for WindPulse benchmark sequence continuity validation.
Verifies subscriber-level gap detection, out-of-order rejection, duplicate tracking,
metadata validation, and snapshot interval exclusion.
"""

import asyncio
import json
import time
import pytest
from benchmarks.benchmark_simulation import (
    validate_subscriber_sequences,
    benchmark_websocket_telemetry
)

def test_consecutive_sequences_pass():
    """1. Consecutive sequences pass completeness check."""
    client_seqs = [
        [1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
        [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    ]
    res = validate_subscriber_sequences(client_seqs, min_frames_per_client=10)
    assert res["is_complete"] is True
    assert res["total_gaps"] == 0
    assert res["total_duplicates"] == 0
    assert res["total_out_of_order"] == 0
    assert res["insufficient_clients_count"] == 0

def test_missing_sequence_fails_with_detected_gap():
    """2. Missing sequence fails with detected gap."""
    client_seqs = [
        [1, 2, 3, 5, 6, 7, 8, 9, 10]  # Missing 4
    ]
    res = validate_subscriber_sequences(client_seqs, min_frames_per_client=5)
    assert res["is_complete"] is False
    assert res["total_gaps"] == 1
    assert res["details"]["gaps"][0]["expected_seq"] == 4
    assert res["details"]["gaps"][0]["received_seq"] == 5
    assert res["details"]["gaps"][0]["gap_size"] == 1

def test_duplicate_sequence_fails():
    """3. Duplicate sequence fails."""
    client_seqs = [
        [1, 2, 3, 3, 4, 5, 6, 7, 8, 9, 10]  # Duplicate 3
    ]
    res = validate_subscriber_sequences(client_seqs, min_frames_per_client=5)
    assert res["is_complete"] is False
    assert res["total_duplicates"] == 1
    assert res["details"]["duplicates"][0]["duplicate_seq"] == 3

def test_out_of_order_frames_fail():
    """4. Out-of-order frames fail."""
    client_seqs = [
        [1, 2, 4, 3, 5, 6, 7, 8, 9, 10]  # Out of order: 4 then 3
    ]
    res = validate_subscriber_sequences(client_seqs, min_frames_per_client=5)
    assert res["is_complete"] is False
    assert res["total_out_of_order"] == 1
    assert res["details"]["out_of_order"][0]["prev_seq"] == 4
    assert res["details"]["out_of_order"][0]["curr_seq"] == 3

def test_client_receiving_no_periodic_frames_fails():
    """5. Client receiving no periodic frames fails."""
    client_seqs = [
        [],  # Zero frames
        [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    ]
    res = validate_subscriber_sequences(client_seqs, min_frames_per_client=5)
    assert res["is_complete"] is False
    assert res["insufficient_clients_count"] == 1
    assert res["details"]["insufficient_clients"][0]["client_idx"] == 0

def test_missing_or_invalid_sequence_metadata_fails():
    """6. Missing or invalid sequence metadata fails."""
    # None value
    res_none = validate_subscriber_sequences([[1, 2, None, 4, 5]], min_frames_per_client=5)
    assert res_none["is_complete"] is False
    assert res_none["total_invalid_metadata"] == 1

    # String value
    res_str = validate_subscriber_sequences([[1, 2, "3", 4, 5]], min_frames_per_client=5)
    assert res_str["is_complete"] is False
    assert res_str["total_invalid_metadata"] == 1

def test_different_starting_sequences_pass_if_contiguous():
    """7. Different starting sequences between clients still pass if each client's own sequence is contiguous."""
    client_seqs = [
        [100, 101, 102, 103, 104, 105],
        [550, 551, 552, 553, 554, 555]
    ]
    res = validate_subscriber_sequences(client_seqs, min_frames_per_client=5)
    assert res["is_complete"] is True
    assert res["total_gaps"] == 0
    assert res["total_duplicates"] == 0

@pytest.mark.asyncio
async def test_connection_interruption_causes_completeness_failure(monkeypatch):
    """8. Connection interruption causes completeness failure."""
    import benchmarks.benchmark_simulation as b_mod

    class FaultyWs:
        def __init__(self, fail: bool):
            self.fail = fail
            self.n = 0

        async def __aenter__(self):
            if self.fail:
                raise ConnectionResetError("Injected network disconnection")
            return self

        async def __aexit__(self, *args):
            pass

        async def recv(self):
            await asyncio.sleep(0.005)
            self.n += 1
            if self.n == 1:
                return json.dumps({"frame_type": "INITIAL_SNAPSHOT", "frame_seq": 0})
            return json.dumps({"frame_type": "STREAM_FRAME", "frame_seq": self.n - 1})

    call_count = 0
    def mock_connect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return FaultyWs(fail=(call_count == 2))

    monkeypatch.setattr(b_mod.websockets, "connect", mock_connect)

    results = await b_mod.benchmark_websocket_telemetry(
        client_count=2,
        duration_seconds=0.03,
        min_frames_per_client=1
    )

    assert results["connection_errors"] == 1
    assert results["application_message_completeness"] is False

@pytest.mark.asyncio
async def test_initial_snapshot_excluded_from_arrival_interval_calculations(monkeypatch):
    """9. Initial snapshot excluded from arrival interval calculations."""
    import benchmarks.benchmark_simulation as b_mod

    class DelayedFirstFrameWs:
        def __init__(self):
            self.n = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def recv(self):
            self.n += 1
            if self.n == 1:
                # Initial snapshot arrives immediately
                return json.dumps({"frame_type": "INITIAL_SNAPSHOT", "frame_seq": 0})
            elif self.n == 2:
                # 80ms delay between initial snapshot and first periodic stream frame
                await asyncio.sleep(0.08)
                return json.dumps({"frame_type": "STREAM_FRAME", "frame_seq": 1})
            else:
                # 20ms delay between periodic stream frames
                await asyncio.sleep(0.02)
                return json.dumps({"frame_type": "STREAM_FRAME", "frame_seq": self.n - 1})

    monkeypatch.setattr(b_mod.websockets, "connect", lambda *args, **kwargs: DelayedFirstFrameWs())

    results = await b_mod.benchmark_websocket_telemetry(
        client_count=1,
        duration_seconds=0.15,
        min_frames_per_client=2
    )

    # Initial snapshot took 80ms to transition to first stream frame.
    # The first stream frame initializes `last_stream_frame_time`, so NO 80ms interval should be recorded.
    # All measured intervals must be between periodic stream frames (~20ms each, well below 70ms).
    recorded_intervals = results["raw_samples"]["inter_frame_intervals_ms"]
    assert len(recorded_intervals) >= 1
    for interval in recorded_intervals:
        assert interval < 60.0, f"Snapshot interval leaked into measurements: {interval}ms"
