"""
AVEVA OSIsoft PI Web API Python Client (Async)
Designed for high-throughput streaming, batching, and WebSockets channels.
"""

import asyncio
import aiohttp
import json
import base64
from typing import List, Dict, Any, Optional

class AvevaPIWebApiClient:
    def __init__(self, base_url: str, username: str = None, password: str = None, verify_ssl: bool = True):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.verify_ssl = verify_ssl
        self.webid_cache: Dict[str, str] = {}
        self._session: Optional[aiohttp.ClientSession] = None

    def _get_headers(self) -> Dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Requested-With": "XMLHttpRequest"
        }
        if self.username and self.password:
            creds = base64.b64encode(f"{self.username}:{self.password}".encode()).decode()
            headers["Authorization"] = f"Basic {creds}"
        return headers

    async def get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers=self._get_headers(),
                connector=aiohttp.TCPConnector(ssl=self.verify_ssl, limit=100)
            )
        return self._session

    async def get_database_by_path(self, path: str) -> Dict[str, Any]:
        """GET /piwebapi/assetdatabases?path=\\{server}\{db}"""
        session = await self.get_session()
        url = f"{self.base_url}/assetdatabases"
        params = {"path": path}
        async with session.get(url, params=params) as resp:
            resp.raise_for_status()
            data = await resp.json()
            if "WebId" in data:
                self.webid_cache[path] = data["WebId"]
            return data

    async def get_streamset_values(self, web_ids: List[str]) -> Dict[str, Any]:
        """POST /piwebapi/batch for ultra-fast StreamSet bulk retrieval."""
        session = await self.get_session()
        batch_payload = {}
        for idx, web_id in enumerate(web_ids):
            batch_payload[f"tag_{idx}"] = {
                "Method": "GET",
                "Resource": f"{self.base_url}/streams/{web_id}/value"
            }
        
        async with session.post(f"{self.base_url}/batch", json=batch_payload) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def subscribe_websocket_channel(self, web_id: str, callback):
        """WSS /piwebapi/channels?webId={web_id} for sub-second telemetry."""
        ws_url = self.base_url.replace("http://", "ws://").replace("https://", "wss://") + f"/channels?webId={web_id}"
        session = await self.get_session()
        async with session.ws_connect(ws_url) as ws:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    await callback(data)
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    break

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
