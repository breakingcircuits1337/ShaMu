<div align="center">

<img src="https://raw.githubusercontent.com/breakingcircuits1337/shamu/shamu_complete/ShaMu/1775754994779_image.png" width="160" alt="Shamu logo"/>

# SHAMU 🐋

**The OrcaSlicer API Bridge**

[![Python](https://img.shields.io/badge/Python_3.10+-3776AB?style=flat-square&logo=python&logoColor=white)](https://python.org)
[![License](https://img.shields.io/badge/License-MIT-22c55e?style=flat-square)](LICENSE)
[![Version](https://img.shields.io/badge/version-0.1.0-f59e0b?style=flat-square)](https://github.com/breakingcircuits1337/Shamu/releases)
[![REST + WebSocket](https://img.shields.io/badge/REST_+_WebSocket-00e5c8?style=flat-square)](#api-reference)
[![Platforms](https://img.shields.io/badge/macOS_·_Windows_·_Linux-555?style=flat-square)](#config-locations)

*OrcaSlicer has no plugin API. Shamu fixes that — without forking or touching a line of C++.*

[**Install**](#install) · [**API Reference**](#api-reference) · [**Build an Addon**](#building-an-addon) · [**Security**](#security) · [**Roadmap**](#roadmap)

</div>

---

## What is this?

OrcaSlicer stores all settings, profiles, and sliced G-code as plain JSON and text files on disk. Shamu is a small background service that watches those files and wraps them in a clean `HTTP + WebSocket` API at `localhost:7878`. Write your addon in Python, JavaScript, Rust — anything that speaks HTTP.

```
OrcaSlicer  ←── reads/writes JSON files ──→  Shamu  ←── HTTP / WebSocket ──→  Your Addon
```

No forking. No C++. No waiting for a native plugin system to land in core.

---

## Install

```bash
pip install shamu
```

Or from source:

```bash
git clone https://github.com/breakingcircuits1337/Shamu
cd Shamu && pip install -e .
```

**Start the bridge:**

```bash
shamu
```

```
[Shamu] Starting on  http://127.0.0.1:7878
[Shamu] Token file:  ~/.config/shamu/token
[Shamu] API docs:    http://127.0.0.1:7878/docs
[Shamu] OrcaSlicer: ~/.config/OrcaSlicer
[Shamu] Watching:    ~/.config/OrcaSlicer
```

> Open **http://localhost:7878/docs** for the full interactive API explorer.

**Options:**

| Flag | Default | Description |
|---|---|---|
| `--port` | `7878` | Port to listen on |
| `--host` | `127.0.0.1` | Bind address — `0.0.0.0` exposes to network, use with caution |
| `--config-dir` | auto-detected | Path to OrcaSlicer config directory |
| `--show-token` | — | Print the current API token and exit |

---

## API Reference

### `GET /status`
Health check. Public endpoint — no token required.

```json
{
  "shamu": "ok",
  "version": "0.1.0",
  "orca_detected": true,
  "connected_addons": 2
}
```

---

### `GET /settings`
Returns the merged active profile (process + printer + filament) as a single flat JSON object.

```bash
curl http://localhost:7878/settings \
  -H "X-Shamu-Token: $(cat ~/.config/shamu/token)"
```

```json
{
  "layer_height": 0.2,
  "infill_density": 15,
  "print_speed": 200,
  "nozzle_temperature": 220
}
```

---

### `PATCH /settings`
Merge changes into the active process profile. OrcaSlicer picks them up on the next slice. Keys must be `snake_case` — anything else is rejected.

```bash
curl -X PATCH http://localhost:7878/settings \
  -H "X-Shamu-Token: $(cat ~/.config/shamu/token)" \
  -H "Content-Type: application/json" \
  -d '{"layer_height": 0.15, "infill_density": 20}'
```

---

### `GET /settings/process` · `GET /settings/printer` · `GET /settings/filament`
Get individual profile types.

---

### `GET /profiles`
List all available profiles by type — both user-created and system defaults.

```json
{
  "process":  { "user": ["My Fast Profile"], "system": ["0.20mm Standard"] },
  "filament": { "user": [],                 "system": ["Generic PLA", "Generic PETG"] },
  "machine":  { "user": [],                 "system": ["Bambu P1S 0.4 nozzle"] }
}
```

---

### `GET /profiles/{type}/{name}`
Load a specific profile by type (`process`, `filament`, `printer`) and name.

---

### `GET /gcode/latest`
Raw G-code from the most recent slice. Add `?lines=N` to get only the first N lines.

---

### `GET /gcode/stats`
Parsed stats from the latest G-code header — no comment scraping required.

```json
{
  "estimated_time_str": "1h 23m 10s",
  "filament_used_g": 42.3,
  "layer_count": 412,
  "nozzle_temp": 220,
  "bed_temp": 65,
  "filament_type": "PLA",
  "printer_model": "Bambu P1S"
}
```

---

### `WS /events`
Real-time event stream. Connect once, receive events as they happen. Pass token as a query param.

```python
import asyncio, websockets, json
from pathlib import Path

token = Path("~/.config/shamu/token").expanduser().read_text().strip()

async def listen():
    async with websockets.connect(f"ws://localhost:7878/events?token={token}") as ws:
        async for msg in ws:
            event = json.loads(msg)
            print(event["event"], event)

asyncio.run(listen())
```

**Event types:**

| Event | Fires when | Key payload fields |
|---|---|---|
| `shamu_ready` | Client connects | `orca_detected` |
| `slice_complete` | OrcaSlicer writes a `.gcode` file | `gcode_path`, `stats` |
| `profile_changed` | Any profile JSON is saved | `profile_type`, `name` |
| `settings_changed` | `PATCH /settings` is called | `source`, `changes` |

---

## Building an Addon

Any program that can make HTTP requests is a Shamu addon. Read the token from `~/.config/shamu/token` and send it as `X-Shamu-Token` on every request.

### Python

```python
import httpx
from pathlib import Path

token   = Path("~/.config/shamu/token").expanduser().read_text().strip()
headers = {"X-Shamu-Token": token}

# Read current settings
settings = httpx.get("http://localhost:7878/settings", headers=headers).json()
print(f"Layer height: {settings['layer_height']}")

# Apply changes — OrcaSlicer picks these up on next slice
httpx.patch("http://localhost:7878/settings", headers=headers, json={
    "layer_height": 0.15,
    "print_speed": 150,
})
```

### JavaScript

```javascript
const fs    = require('fs');
const token = fs.readFileSync(require('os').homedir() + '/.config/shamu/token', 'utf8').trim();
const h     = { 'X-Shamu-Token': token };

// Get settings
const settings = await fetch('http://localhost:7878/settings', { headers: h }).then(r => r.json());

// Listen for slice events
const ws = new WebSocket(`ws://localhost:7878/events?token=${token}`);
ws.onmessage = (e) => {
  const event = JSON.parse(e.data);
  if (event.event === 'slice_complete') console.log('Slice done!', event.stats);
};
```

### AI advisor addon (included)

`addons/example_ai_addon/` ships a working Claude-powered settings advisor. Tell it what you want in plain English — it figures out which settings to change.

```bash
pip install shamu[addon]
export ANTHROPIC_API_KEY=sk-ant-...
python addons/example_ai_addon/addon_ai_advisor.py
```

```
Connected to Shamu v0.1.0

What do you want to optimize?
> reduce stringing with PETG

Suggested changes (3 settings):
  retraction_length: 0.8 → 1.4
  retraction_speed:  35  → 45
  print_speed:       200 → 160

Apply? [y/N] y
Applied. Re-slice in OrcaSlicer to see the effect.
```

---

## Security

Shamu only ever binds to `127.0.0.1` by default. Four layers of protection keep your settings safe:

| | Measure | What it prevents |
|---|---|---|
| 🔑 | **Token auth** on every endpoint | Unauthorized processes calling the API |
| 🛡️ | **CORS restricted to `null` origin** | Malicious websites silently reading/writing your settings via browser fetch |
| 🚫 | **Path traversal prevention** on profile inputs | Escaping the config directory via crafted profile names |
| ⏱️ | **Constant-time token comparison** (`secrets.compare_digest`) | Timing attacks on the auth header |

The token is generated with 256 bits of entropy on first run and stored at `~/.config/shamu/token` with `chmod 600` (owner read/write only).

---

## Config Locations

Shamu auto-detects the OrcaSlicer config directory per OS. Override with `--config-dir /your/path`.

| OS | Path |
|---|---|
| 🪟 Windows | `%APPDATA%\OrcaSlicer` |
| 🍎 macOS | `~/Library/Application Support/OrcaSlicer` |
| 🐧 Linux | `~/.config/OrcaSlicer` |

---

## Roadmap

- [x] REST API — read and write active OrcaSlicer settings
- [x] WebSocket events — `slice_complete`, `profile_changed`, `settings_changed`
- [x] Token auth + CORS hardening + path traversal prevention
- [x] AI advisor addon powered by Claude
- [ ] `POST /slice` — trigger a headless slice via OrcaSlicer CLI
- [ ] Profile diff endpoint — compare two profiles side-by-side
- [ ] Addon manifest format — declare dependencies, permissions, metadata
- [ ] Tray icon companion app
- [ ] Submit to OrcaSlicer upstream as a first-party companion tool

---

## Contributing

Pull requests welcome. If you build an addon using Shamu, open a PR to add it to the `addons/` directory.

Relevant upstream discussion: [OrcaSlicer/OrcaSlicer#9960 — Structured Addon/Extension Support](https://github.com/OrcaSlicer/OrcaSlicer/discussions/9960)

---

<div align="center">

**MIT License** · Built by [breakingcircuits1337](https://github.com/breakingcircuits1337)

*Is it a whale? Is it an API? Yes.*

</div>
