"""
Shamu - Local API bridge for OrcaSlicer addon development.

Security model:
  - Binds to 127.0.0.1 only by default (no network exposure)
  - All mutating endpoints require X-Shamu-Token header
  - CORS restricted to null origin (blocks browser-based CSRF attacks)
  - Profile name inputs are sanitized to prevent path traversal
  - Read-only endpoints are token-protected too (settings are private data)

Usage:
    python -m shamu.server
    python -m shamu.server --port 7878
"""

import asyncio
import json
import os
import platform
import time
import argparse
import secrets
from pathlib import Path
from typing import Any, Annotated

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
import uvicorn
from watchfiles import awatch

from .config_manager import ConfigManager
from .gcode_hook import GCodeHook
from .connection_manager import ConnectionManager
from .auth import load_or_create_token, get_shamu_data_dir


def get_default_orca_config_dir() -> Path:
    """Return OrcaSlicer's default config directory for the current OS."""
    system = platform.system()
    if system == "Windows":
        return Path(os.environ.get("APPDATA", "")) / "OrcaSlicer"
    elif system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "OrcaSlicer"
    else:
        return Path.home() / ".config" / "OrcaSlicer"


def create_app(config_dir: Path, token: str) -> FastAPI:
    app = FastAPI(
        title="Shamu",
        description="Local API bridge for OrcaSlicer addon development",
        version="0.1.0",
    )

    # ── CORS: restrict to same-origin / null origin only ───────────────────
    # Browsers send Origin: null for local file:// requests and some tools.
    # Blocking all cross-origin browser requests prevents malicious websites
    # from silently calling localhost:7878 via fetch/XHR.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["null"],          # Only allow null-origin (local tools)
        allow_methods=["GET", "PATCH"],
        allow_headers=["X-Shamu-Token", "Content-Type"],
        allow_credentials=False,
    )

    config_manager = ConfigManager(config_dir)
    gcode_hook = GCodeHook(config_dir)
    ws_manager = ConnectionManager()

    # ── Auth dependency ──────────────────────────────────────────────────────

    def require_token(x_shamu_token: Annotated[str | None, Header()] = None):
        """
        Validate the X-Shamu-Token header using constant-time comparison
        to prevent timing attacks.
        """
        if x_shamu_token is None:
            raise HTTPException(
                status_code=401,
                detail="Missing X-Shamu-Token header. See ~/.config/shamu/token for your token.",
            )
        if not secrets.compare_digest(x_shamu_token.strip(), token):
            raise HTTPException(status_code=403, detail="Invalid token.")

    # ── REST: settings ───────────────────────────────────────────────────────

    @app.get("/settings", dependencies=[Depends(require_token)])
    async def get_settings():
        """Merged active settings (process + printer + filament) as flat JSON."""
        try:
            return config_manager.get_active_settings()
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))

    @app.get("/settings/process", dependencies=[Depends(require_token)])
    async def get_process_settings():
        try:
            return config_manager.get_profile("process")
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))

    @app.get("/settings/printer", dependencies=[Depends(require_token)])
    async def get_printer_settings():
        try:
            return config_manager.get_profile("printer")
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))

    @app.get("/settings/filament", dependencies=[Depends(require_token)])
    async def get_filament_settings():
        try:
            return config_manager.get_profile("filament")
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))

    @app.patch("/settings", dependencies=[Depends(require_token)])
    async def patch_settings(request: Request):
        """
        Merge a partial JSON object into the active process profile.
        OrcaSlicer picks up changes on next slice.
        """
        try:
            changes: dict[str, Any] = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON body")

        if not isinstance(changes, dict):
            raise HTTPException(status_code=400, detail="Body must be a JSON object")

        # Reject any keys that look like path traversal or injection attempts
        _validate_setting_keys(changes)

        try:
            updated = config_manager.patch_active_process(changes)
            await ws_manager.broadcast({
                "event": "settings_changed",
                "source": "api",
                "changes": changes,
                "timestamp": time.time(),
            })
            return {"ok": True, "applied": list(changes.keys()), "profile": updated}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/profiles", dependencies=[Depends(require_token)])
    async def list_profiles():
        return config_manager.list_profiles()

    @app.get("/profiles/{profile_type}/{name}", dependencies=[Depends(require_token)])
    async def get_named_profile(profile_type: str, name: str):
        # Sanitize inputs to prevent path traversal
        profile_type = _sanitize_path_segment(profile_type)
        name = _sanitize_path_segment(name)

        valid_types = {"process", "filament", "printer", "machine"}
        if profile_type not in valid_types:
            raise HTTPException(status_code=400, detail=f"Invalid profile type. Must be one of: {valid_types}")

        try:
            return config_manager.get_named_profile(profile_type, name)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"Profile '{name}' not found")

    # ── REST: gcode ──────────────────────────────────────────────────────────

    @app.get("/gcode/latest", dependencies=[Depends(require_token)])
    async def get_latest_gcode(lines: int = 0):
        """Raw G-code from most recent slice. Pass ?lines=N for first N lines."""
        if lines < 0 or lines > 100_000:
            raise HTTPException(status_code=400, detail="lines must be between 0 and 100000")
        try:
            content = gcode_hook.get_latest()
            if lines > 0:
                content = "\n".join(content.splitlines()[:lines])
            return PlainTextResponse(content)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="No G-code found yet. Slice a model first.")

    @app.get("/gcode/stats", dependencies=[Depends(require_token)])
    async def get_gcode_stats():
        """Parsed stats from latest G-code: time, filament, layers, temps."""
        try:
            return gcode_hook.parse_stats()
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="No G-code found yet.")

    # ── REST: status (public — no token needed) ──────────────────────────────

    @app.get("/status")
    async def get_status():
        """Health check. Does not reveal settings or token."""
        return {
            "shamu": "ok",
            "version": "0.1.0",
            "orca_detected": config_manager.is_orca_present(),
            "connected_addons": ws_manager.count(),
            "timestamp": time.time(),
        }

    @app.get("/", include_in_schema=False)
    async def root():
        return {"message": "Shamu running. See /docs for API reference."}

    # ── WebSocket ─────────────────────────────────────────────────────────────

    @app.websocket("/events")
    async def websocket_events(websocket: WebSocket, token_param: str | None = None):
        """
        Real-time event stream. Pass token as query param:
            ws://localhost:7878/events?token=<your-token>
        """
        # WebSocket doesn't support custom headers in browsers,
        # so we accept the token as a query param for WS connections.
        from fastapi import Query
        raw_token = websocket.query_params.get("token")
        if not raw_token or not secrets.compare_digest(raw_token.strip(), token):
            await websocket.close(code=4003, reason="Invalid or missing token")
            return

        await ws_manager.connect(websocket)
        try:
            await websocket.send_json({
                "event": "shamu_ready",
                "orca_detected": config_manager.is_orca_present(),
                "timestamp": time.time(),
            })
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            ws_manager.disconnect(websocket)

    # ── Background: file watcher ──────────────────────────────────────────────

    @app.on_event("startup")
    async def start_file_watcher():
        asyncio.create_task(_watch_config(config_dir, gcode_hook, ws_manager))

    return app


# ── Input validation helpers ──────────────────────────────────────────────────

def _sanitize_path_segment(value: str) -> str:
    """
    Strip any path traversal characters from a user-supplied string.
    Allows only alphanumeric, spaces, hyphens, underscores, dots, and @
    (OrcaSlicer profile names use @ as a separator).
    """
    import re
    cleaned = re.sub(r"[^\w\s\-_.@]", "", value, flags=re.UNICODE)
    # Also block any remaining path separators just in case
    cleaned = cleaned.replace("/", "").replace("\\", "").replace("..", "")
    return cleaned.strip()


def _validate_setting_keys(changes: dict):
    """
    Reject any setting keys that contain path separators or look like
    injection attempts. OrcaSlicer setting keys are snake_case identifiers.
    """
    import re
    bad_key_pattern = re.compile(r"[^\w]")  # Allow only word chars (a-z, 0-9, _)
    for key in changes:
        if not isinstance(key, str):
            raise HTTPException(status_code=400, detail=f"Setting keys must be strings, got: {type(key)}")
        if bad_key_pattern.search(key):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid setting key '{key}'. Keys must be alphanumeric/underscore only.",
            )
        if len(key) > 128:
            raise HTTPException(status_code=400, detail=f"Setting key too long: '{key[:32]}...'")


# ── File watcher ──────────────────────────────────────────────────────────────

async def _watch_config(
    config_dir: Path,
    gcode_hook: GCodeHook,
    ws_manager: ConnectionManager,
):
    if not config_dir.exists():
        print(f"[Shamu] Config dir not found: {config_dir}")
        print("[Shamu] Waiting for OrcaSlicer to run first...")
        while not config_dir.exists():
            await asyncio.sleep(2)

    print(f"[Shamu] Watching: {config_dir}")

    async for changes in awatch(config_dir):
        for change_type, path_str in changes:
            path = Path(path_str)
            suffix = path.suffix.lower()

            if suffix == ".gcode":
                try:
                    stats = gcode_hook.parse_stats(path)
                    await ws_manager.broadcast({
                        "event": "slice_complete",
                        "gcode_path": str(path),
                        "stats": stats,
                        "timestamp": time.time(),
                    })
                except Exception as e:
                    print(f"[Shamu] Error parsing gcode: {e}")

            elif suffix == ".json":
                profile_type = _infer_profile_type(path)
                await ws_manager.broadcast({
                    "event": "profile_changed",
                    "profile_type": profile_type,
                    "name": path.stem,
                    "timestamp": time.time(),
                })


def _infer_profile_type(path: Path) -> str:
    parts = path.parts
    if "process" in parts:   return "process"
    if "filament" in parts:  return "filament"
    if "machine" in parts:   return "printer"
    return "unknown"


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Shamu - OrcaSlicer addon API bridge")
    parser.add_argument("--port",       type=int,  default=7878)
    parser.add_argument("--host",       default="127.0.0.1",
                        help="Host to bind (default: 127.0.0.1 — local only). "
                             "Setting this to 0.0.0.0 exposes Shamu to your network.")
    parser.add_argument("--config-dir", type=Path, default=None,
                        help="Path to OrcaSlicer config dir (auto-detected if omitted)")
    parser.add_argument("--show-token", action="store_true",
                        help="Print the API token and exit")
    args = parser.parse_args()

    data_dir = get_shamu_data_dir()
    token = load_or_create_token(data_dir)

    if args.show_token:
        print(token)
        return

    config_dir = args.config_dir or get_default_orca_config_dir()

    if args.host != "127.0.0.1":
        print(f"[Shamu] WARNING: Binding to {args.host} exposes Shamu to the network.")
        print("[Shamu] Anyone on your network can read/modify your print settings.")

    print(f"[Shamu] Starting on http://{args.host}:{args.port}")
    print(f"[Shamu] Token file:  {data_dir / 'token'}")
    print(f"[Shamu] API docs:    http://{args.host}:{args.port}/docs")
    print(f"[Shamu] OrcaSlicer: {config_dir}")

    app = create_app(config_dir, token)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
