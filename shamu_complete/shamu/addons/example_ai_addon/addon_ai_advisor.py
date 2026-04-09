"""
Shamu Example Addon - AI Settings Advisor
==========================================
Uses the Anthropic API to suggest better print settings based on your goal.

Setup:
    pip install anthropic httpx websockets
    export ANTHROPIC_API_KEY=sk-ant-...

Usage:
    python addon_ai_advisor.py               # interactive mode
    python addon_ai_advisor.py watch         # auto-suggest on every slice
"""

import asyncio
import json
import os
import sys
from pathlib import Path

import httpx
import websockets
import anthropic

SHAMU_URL = "http://localhost:7878"
SHAMU_WS  = "ws://localhost:7878/events"


def get_token() -> str:
    """Load the Shamu token from the default location."""
    import platform
    system = platform.system()
    if system == "Windows":
        base = Path(os.environ.get("APPDATA", Path.home()))
    elif system == "Darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    token_path = base / "shamu" / "token"

    if not token_path.exists():
        print(f"ERROR: Token file not found at {token_path}")
        print("Start Shamu first: python -m shamu")
        sys.exit(1)

    return token_path.read_text().strip()


TOKEN = get_token()
HEADERS = {"X-Shamu-Token": TOKEN}


async def get_settings() -> dict:
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{SHAMU_URL}/settings", headers=HEADERS)
        r.raise_for_status()
        return r.json()


async def apply_settings(changes: dict) -> dict:
    async with httpx.AsyncClient() as client:
        r = await client.patch(f"{SHAMU_URL}/settings", json=changes, headers=HEADERS)
        r.raise_for_status()
        return r.json()


async def get_gcode_stats() -> dict | None:
    async with httpx.AsyncClient() as client:
        try:
            r = await client.get(f"{SHAMU_URL}/gcode/stats", headers=HEADERS)
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError:
            return None


def ask_ai(settings: dict, gcode_stats: dict | None, goal: str) -> dict:
    """Ask Claude to suggest setting changes for the given goal."""
    client = anthropic.Anthropic()

    key_settings = {k: settings[k] for k in [
        "layer_height", "initial_layer_height",
        "infill_density", "infill_pattern",
        "print_speed", "outer_wall_speed",
        "support_enable", "support_type",
        "nozzle_temperature", "bed_temperature",
        "retraction_length", "retraction_speed",
        "wall_loops", "top_shell_layers", "bottom_shell_layers",
    ] if k in settings}

    gcode_summary = ""
    if gcode_stats:
        gcode_summary = f"""
Last slice:
- Print time: {gcode_stats.get('estimated_time_str', '?')}
- Filament: {gcode_stats.get('filament_used_g', '?')}g  ({gcode_stats.get('filament_type', '?')})
- Layers: {gcode_stats.get('layer_count', '?')}
"""

    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1000,
        messages=[{"role": "user", "content": f"""You are an expert 3D printing advisor.

Current settings:
{json.dumps(key_settings, indent=2)}
{gcode_summary}
Goal: "{goal}"

Return ONLY a JSON object of setting changes (same key names, changed values only).
Be conservative. Example: {{"layer_height": 0.2, "print_speed": 150}}"""}]
    )

    raw = message.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw.strip())


async def interactive():
    # Check Shamu is running
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{SHAMU_URL}/status", timeout=3)
            s = r.json()
            print(f"Connected to Shamu v{s['version']}")
            if not s["orca_detected"]:
                print("WARNING: OrcaSlicer config not detected. Open OrcaSlicer first.")
    except Exception:
        print(f"ERROR: Can't reach Shamu at {SHAMU_URL}")
        print("Start it with: python -m shamu")
        sys.exit(1)

    print()
    while True:
        print("What do you want to optimize? (or 'quit')")
        goal = input("> ").strip()
        if goal.lower() in ("quit", "exit", "q"):
            break
        if not goal:
            continue

        print("Fetching settings...")
        settings  = await get_settings()
        stats     = await get_gcode_stats()

        print("Asking Claude...")
        try:
            suggestions = ask_ai(settings, stats, goal)
        except (json.JSONDecodeError, Exception) as e:
            print(f"ERROR: {e}")
            continue

        if not suggestions:
            print("No changes suggested.")
            continue

        print(f"\nSuggested changes ({len(suggestions)}):")
        for k, v in suggestions.items():
            print(f"  {k}: {settings.get(k, '?')} → {v}")

        if input("\nApply? [y/N] ").strip().lower() == "y":
            await apply_settings(suggestions)
            print("Applied. Re-slice in OrcaSlicer to see the effect.")
        print()


async def watch():
    """Auto-suggest improvements after every slice."""
    print("Watch mode — slice a model in OrcaSlicer to get suggestions...")
    async with websockets.connect(f"{SHAMU_WS}?token={TOKEN}") as ws:
        async for msg in ws:
            event = json.loads(msg)
            if event.get("event") == "slice_complete":
                stats    = event.get("stats", {})
                settings = await get_settings()
                print(f"\nSlice complete — time: {stats.get('estimated_time_str','?')}, "
                      f"filament: {stats.get('filament_used_g','?')}g")
                try:
                    suggestions = ask_ai(settings, stats, "optimize quality and reliability")
                    if suggestions:
                        print(f"Suggestions ({len(suggestions)}):")
                        for k, v in suggestions.items():
                            print(f"  {k}: {settings.get(k,'?')} → {v}")
                except Exception as e:
                    print(f"AI error: {e}")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "interactive"
    asyncio.run(watch() if mode == "watch" else interactive())
