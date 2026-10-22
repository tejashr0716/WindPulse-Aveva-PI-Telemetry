"""
Browser regression tests for WindPulse CSV export with nullable telemetry.
Verifies export functionality under null values, genuine zeros, and precision constraints
using headless Google Chrome via Playwright.
"""

import asyncio
import csv
import io
import os
import pytest
from playwright.async_api import async_playwright

CHROME_PATH = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

@pytest.fixture(scope="module")
def browser_instance():
    """Checks whether Google Chrome binary exists."""
    if not os.path.exists(CHROME_PATH):
        pytest.skip(f"Chrome executable not found at {CHROME_PATH}")
    return CHROME_PATH

@pytest.mark.asyncio
async def test_csv_export_handles_nulls_zeros_and_precision(browser_instance):
    """
    Regression test for CSV export:
    1. Exercises actual production export function.
    2. Tests null in every nullable exported numeric field.
    3. Confirms zero exports as zero (e.g. '0.0'), not an empty cell.
    4. Confirms ordinary numeric values retain intended precision.
    5. Downloads and inspects resulting CSV.
    6. Confirms export produces no uncaught browser errors.
    """
    html_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "public", "index.html"))
    assert os.path.exists(html_path), f"File not found: {html_path}"

    page_errors = []
    console_errors = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=browser_instance, headless=True)
        context = await browser.new_context(accept_downloads=True)
        page = await context.new_page()

        page.on("pageerror", lambda err: page_errors.append(str(err)))
        page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)

        await page.goto(f"file://{html_path}")

        # Inject specific test values across turbines:
        # Turbine 0: All nullable numeric fields set to null
        # Turbine 1: All fields set to genuine zero (0.0)
        # Turbine 2: High precision numbers to verify rounding / formatting
        # Turbine 3: Text with commas and quotes to verify escaping
        await page.evaluate("""() => {
            isStreaming = false; // Pause background tick loop

            turbines[0].active_power_kw = null;
            turbines[0].wind_speed_mps = null;
            turbines[0].rotor_rpm = null;
            turbines[0].nacelle_yaw_deg = null;
            turbines[0].gearbox_bearing_temp_c = null;

            turbines[1].active_power_kw = 0.0;
            turbines[1].wind_speed_mps = 0.0;
            turbines[1].rotor_rpm = 0.0;
            turbines[1].nacelle_yaw_deg = 0.0;
            turbines[1].gearbox_bearing_temp_c = 0.0;

            turbines[2].active_power_kw = 2450.567;
            turbines[2].wind_speed_mps = 12.345;
            turbines[2].rotor_rpm = 15.678;
            turbines[2].nacelle_yaw_deg = 230.123;
            turbines[2].gearbox_bearing_temp_c = 72.891;

            turbines[3].name = 'WTG-04, Special "Offshore"';
            turbines[3].feeder = 'Feeder A, Line 1';
            turbines[3].active_power_kw = 1200.0;
            turbines[3].wind_speed_mps = 8.5;
            turbines[3].rotor_rpm = 11.2;
            turbines[3].nacelle_yaw_deg = 180.0;
            turbines[3].gearbox_bearing_temp_c = 65.0;
        }""")

        # Trigger download via DOM button click (simulating real user interaction)
        async with page.expect_download() as download_info:
            await page.click('button[title="Export CSV Report"]')

        download = await download_info.value
        downloaded_path = await download.path()
        assert downloaded_path is not None, "CSV download path was None"

        with open(downloaded_path, "r", encoding="utf-8") as f:
            csv_content = f.read()

        reader = list(csv.reader(io.StringIO(csv_content)))
        header = reader[0]

        # Verify CSV headers & column order
        expected_header = [
            "Timestamp", "Turbine", "Feeder",
            "ActivePower_kW", "WindSpeed_mps", "RotorRPM",
            "NacelleYaw_deg", "GearboxTemp_C"
        ]
        assert header == expected_header, f"Header mismatch: {header} != {expected_header}"

        # Inspect Turbine 0 (Nulls): all 5 numeric fields must be empty cells, NOT 0 and NOT NaN
        row_nulls = reader[1]
        assert row_nulls[1] == "WTG-01"
        assert row_nulls[3] == "", f"Expected empty cell for active_power_kw, got '{row_nulls[3]}'"
        assert row_nulls[4] == "", f"Expected empty cell for wind_speed_mps, got '{row_nulls[4]}'"
        assert row_nulls[5] == "", f"Expected empty cell for rotor_rpm, got '{row_nulls[5]}'"
        assert row_nulls[6] == "", f"Expected empty cell for nacelle_yaw_deg, got '{row_nulls[6]}'"
        assert row_nulls[7] == "", f"Expected empty cell for gearbox_bearing_temp_c, got '{row_nulls[7]}'"

        # Inspect Turbine 1 (Zeros): genuine 0 must export as '0.0' or '0.00', NOT empty cell
        row_zeros = reader[2]
        assert row_zeros[1] == "WTG-02"
        assert row_zeros[3] == "0.0", f"Expected '0.0' for active_power_kw, got '{row_zeros[3]}'"
        assert row_zeros[4] == "0.00", f"Expected '0.00' for wind_speed_mps, got '{row_zeros[4]}'"
        assert row_zeros[5] == "0.0", f"Expected '0.0' for rotor_rpm, got '{row_zeros[5]}'"
        assert row_zeros[6] == "0.0", f"Expected '0.0' for nacelle_yaw_deg, got '{row_zeros[6]}'"
        assert row_zeros[7] == "0.0", f"Expected '0.0' for gearbox_bearing_temp_c, got '{row_zeros[7]}'"

        # Inspect Turbine 2 (Precision): confirm precision retention
        row_prec = reader[3]
        assert row_prec[1] == "WTG-03"
        assert row_prec[3] == "2450.6"  # 1 decimal
        assert row_prec[4] == "12.35"   # 2 decimals
        assert row_prec[5] == "15.7"    # 1 decimal
        assert row_prec[6] == "230.1"   # 1 decimal
        assert row_prec[7] == "72.9"    # 1 decimal

        # Inspect Turbine 3 (Text escaping): commas and quotes properly handled by CSV parser
        row_escaped = reader[4]
        assert row_escaped[1] == 'WTG-04, Special "Offshore"'
        assert row_escaped[2] == 'Feeder A, Line 1'

        # Verify that underlying turbine objects were not mutated
        underlying = await page.evaluate("() => turbines[0].rotor_rpm")
        assert underlying is None, "Underlying telemetry value was unexpectedly mutated"

        # Confirm zero uncaught browser errors occurred
        assert len(page_errors) == 0, f"Uncaught browser errors detected: {page_errors}"
        assert len(console_errors) == 0, f"Console error logs detected: {console_errors}"

        await browser.close()
