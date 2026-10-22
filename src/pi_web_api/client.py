"""
Production-grade Async AVEVA OSIsoft PI Web API Client.
Features:
- Strict TLS certificate verification by default.
- Bounded retries with exponential backoff for idempotent reads.
- Session lifecycle and resource cleanup.
- Quality flag parsing (never treats questionable or bad values as valid).
- Strict write guards (opt-in only, allowlist required, read-back verification).
"""

import asyncio
import base64
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import aiohttp

from src.config import settings
from src.models import PIPointWriteResponse, PIStreamValue

logger = logging.getLogger("windpulse.pi_client")

class PIWebAPIClientError(Exception):
    """Base exception for PI Web API client errors."""
    pass

class PIAuthenticationError(PIWebAPIClientError):
    """Authentication or authorization failure (HTTP 401/403)."""
    pass

class PINotFoundError(PIWebAPIClientError):
    """Resource not found (HTTP 404)."""
    pass

class PIWritePermissionError(PIWebAPIClientError):
    """Write operation rejected by security guardrails."""
    pass

class AvevaPIWebApiClient:
    def __init__(
        self,
        base_url: Optional[str] = None,
        auth_type: str = "BASIC",
        username: Optional[str] = None,
        password: Optional[str] = None,
        bearer_token: Optional[str] = None,
        verify_ssl: bool = True,
        timeout_seconds: float = 10.0,
        max_retries: int = 3,
        backoff_factor: float = 0.5,
        allow_writes: bool = False,
        approved_write_points: Optional[List[str]] = None
    ):
        self.base_url = (base_url or settings.PI_WEB_API_URL or "").rstrip("/")
        self.auth_type = auth_type
        self.username = username or settings.PI_USERNAME
        self.password = password or settings.PI_PASSWORD
        self.bearer_token = bearer_token or settings.PI_BEARER_TOKEN
        self.verify_ssl = verify_ssl
        self.timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self.allow_writes = allow_writes
        self.approved_write_points = set(approved_write_points or settings.PI_APPROVED_WRITE_POINTS)
        self.webid_cache: Dict[str, str] = {}
        self._session: Optional[aiohttp.ClientSession] = None

    def _build_headers(self) -> Dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Requested-With": "XMLHttpRequest",
            "User-Agent": "WindPulse-Telemetry-Client/1.0"
        }
        if self.auth_type == "BASIC" and self.username and self.password:
            raw_creds = f"{self.username}:{self.password}".encode("utf-8")
            headers["Authorization"] = f"Basic {base64.b64encode(raw_creds).decode('utf-8')}"
        elif self.auth_type == "BEARER" and self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        elif self.auth_type == "KERBEROS":
            # For Kerberos/Negotiate, headers are handled at OS/SPNEGO transport level
            pass
        return headers

    async def get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(
                ssl=self.verify_ssl,
                limit=50,
                enable_cleanup_closed=True
            )
            self._session = aiohttp.ClientSession(
                headers=self._build_headers(),
                timeout=self.timeout,
                connector=connector
            )
        return self._session

    async def _execute_with_retry(self, method: str, url: str, **kwargs) -> aiohttp.ClientResponse:
        """Executes HTTP request with bounded retry and exponential backoff for idempotent methods."""
        session = await self.get_session()
        attempts = self.max_retries if method.upper() in ("GET", "HEAD") else 1
        last_error = None

        for attempt in range(1, attempts + 1):
            try:
                resp = await session.request(method, url, **kwargs)
                if resp.status == 401 or resp.status == 403:
                    body = await resp.text()
                    raise PIAuthenticationError(f"PI Web API Authentication failed (HTTP {resp.status}): {body[:200]}")
                if resp.status == 404:
                    raise PINotFoundError(f"Resource not found at {url}")
                if resp.status >= 500 and attempt < attempts:
                    await asyncio.sleep(self.backoff_factor * (2 ** (attempt - 1)))
                    continue
                return resp
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last_error = e
                if attempt < attempts:
                    await asyncio.sleep(self.backoff_factor * (2 ** (attempt - 1)))
                else:
                    raise PIWebAPIClientError(f"PI Web API request failed after {attempts} attempts: {e}") from e

        raise PIWebAPIClientError(f"PI Web API request failed: {last_error}")

    async def get_database_by_path(self, path: str) -> Dict[str, Any]:
        """Discovers database and caches its WebID. GET /piwebapi/assetdatabases?path=\\\\{server}\\{db}"""
        if not self.base_url:
            raise PIWebAPIClientError("PI_WEB_API_URL is not configured.")
        url = f"{self.base_url}/assetdatabases"
        resp = await self._execute_with_retry("GET", url, params={"path": path})
        data = await resp.json()
        if "WebId" in data:
            self.webid_cache[path] = data["WebId"]
        return data

    async def get_stream_value(self, web_id: str) -> PIStreamValue:
        """Retrieves instantaneous point/attribute value. GET /piwebapi/streams/{webId}/value"""
        if not self.base_url:
            raise PIWebAPIClientError("PI_WEB_API_URL is not configured.")
        url = f"{self.base_url}/streams/{web_id}/value"
        resp = await self._execute_with_retry("GET", url)
        data = await resp.json()
        return PIStreamValue(**data)

    async def get_streamset_values(self, web_ids: List[str]) -> Dict[str, PIStreamValue]:
        """
        Retrieves multiple point values in a single round-trip using POST /piwebapi/batch.
        Returns a mapping of WebID -> PIStreamValue.
        """
        if not self.base_url:
            raise PIWebAPIClientError("PI_WEB_API_URL is not configured.")
        if not web_ids:
            return {}

        batch_payload = {}
        for idx, web_id in enumerate(web_ids):
            batch_payload[f"tag_{idx}"] = {
                "Method": "GET",
                "Resource": f"{self.base_url}/streams/{web_id}/value"
            }

        url = f"{self.base_url}/batch"
        resp = await self._execute_with_retry("POST", url, json=batch_payload)
        batch_results = await resp.json()

        output: Dict[str, PIStreamValue] = {}
        for idx, web_id in enumerate(web_ids):
            key = f"tag_{idx}"
            if key in batch_results and batch_results[key].get("Status") == 200:
                item_content = batch_results[key].get("Content", {})
                output[web_id] = PIStreamValue(**item_content)
            else:
                output[web_id] = PIStreamValue(
                    Timestamp=datetime.now(timezone.utc).isoformat(),
                    Value="No Data",
                    Good=False
                )
        return output

    async def write_point_value(
        self,
        web_id: str,
        value: float,
        timestamp: Optional[str] = None
    ) -> PIPointWriteResponse:
        """
        Writes a single timestamped numeric value to an approved test point.
        Safety guards:
        1. Refuses execution if allow_writes is False.
        2. Refuses execution if web_id is not in approved_write_points.
        3. Verifies write success by reading back the value.
        """
        if not self.base_url:
            raise PIWebAPIClientError("PI_WEB_API_URL is not configured.")

        # Safety Guard 1: Global Opt-in Check
        if not self.allow_writes:
            raise PIWritePermissionError(
                "WRITE BLOCKED: Writes are disabled by default. Set PI_ALLOW_WRITES=true to enable."
            )

        # Safety Guard 2: Whitelist Authorization
        if web_id not in self.approved_write_points:
            raise PIWritePermissionError(
                f"WRITE BLOCKED: Point WebID '{web_id}' is not in approved test points list."
            )

        ts = timestamp or datetime.now(timezone.utc).isoformat()
        payload = {
            "Timestamp": ts,
            "Value": float(value),
            "Good": True
        }

        url = f"{self.base_url}/streams/{web_id}/value"
        resp = await self._execute_with_retry("POST", url, json=payload)
        if resp.status not in (200, 202):
            body = await resp.text()
            raise PIWebAPIClientError(f"PI Point write failed with HTTP {resp.status}: {body[:200]}")

        # Verification: Read-back
        read_back_stream = await self.get_stream_value(web_id)
        numeric_read_back = read_back_stream.get_numeric_value()
        is_verified = (numeric_read_back is not None) and (abs(numeric_read_back - value) < 0.001)

        return PIPointWriteResponse(
            web_id=web_id,
            written_value=value,
            timestamp=ts,
            read_back_value=numeric_read_back,
            verified=is_verified,
            message="Write accepted and verified via read-back." if is_verified else "Write accepted but read-back verification diverged."
        )

    async def close(self):
        """Cleanly releases TCP connections and underlying connector."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def __aenter__(self):
        await self.get_session()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()
