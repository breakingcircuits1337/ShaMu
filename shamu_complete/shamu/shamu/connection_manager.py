"""
ConnectionManager - manages active WebSocket connections and broadcasts events.
"""

import json
from fastapi import WebSocket


class ConnectionManager:
    def __init__(self):
        self._active: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self._active.append(ws)

    def disconnect(self, ws: WebSocket):
        self._active = [c for c in self._active if c is not ws]

    def count(self) -> int:
        return len(self._active)

    async def broadcast(self, data: dict):
        """Send a JSON event to all connected addon clients."""
        dead = []
        for ws in self._active:
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)
