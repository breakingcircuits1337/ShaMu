"""
ConfigManager - reads and writes OrcaSlicer's JSON profile files.

OrcaSlicer stores settings in a layered hierarchy:
  config_dir/
    user/
      <id>/
        process/   ← print process profiles  (layer height, infill, speeds...)
        filament/  ← filament profiles        (temperatures, retraction...)
        machine/   ← printer variant profiles (bed size, firmware flavour...)
    system/        ← read-only vendor defaults (we never write here)

The "active" profile is tracked in a metadata file. We read that to know
which profile JSON to load and patch.
"""

import json
from pathlib import Path
from typing import Any


PROFILE_TYPES = ("process", "filament", "machine")


class ConfigManager:
    def __init__(self, config_dir: Path):
        self.config_dir = config_dir
        self._user_dir = config_dir / "user"
        self._system_dir = config_dir / "system"

    # ── Detection ────────────────────────────────────────────────────────────

    def is_orca_present(self) -> bool:
        return self.config_dir.exists()

    # ── Active profile ───────────────────────────────────────────────────────

    def get_active_profile_name(self) -> dict | None:
        """
        Read OrcaSlicer's app_config.json (or similar) to find which
        profiles are currently selected. Falls back to the most recently
        modified profile if the metadata file isn't found.
        """
        # OrcaSlicer stores the last-used settings in app_config.json
        app_config_path = self.config_dir / "app_config.json"
        if app_config_path.exists():
            try:
                data = json.loads(app_config_path.read_text(encoding="utf-8"))
                return {
                    "process":  data.get("print",    {}).get("name"),
                    "filament": data.get("filament", {}).get("name"),
                    "printer":  data.get("printer",  {}).get("name"),
                }
            except Exception:
                pass

        # Fallback: most recently modified process profile
        process_files = self._find_user_profiles("process")
        if process_files:
            newest = max(process_files, key=lambda p: p.stat().st_mtime)
            return {"process": newest.stem, "filament": None, "printer": None}

        return None

    def get_active_settings(self) -> dict:
        """
        Return merged active settings. Process profile is the primary source;
        printer + filament profiles fill in the rest.
        """
        result = {}

        for profile_type in ("machine", "filament", "process"):
            try:
                profile = self.get_profile(profile_type)
                result.update(profile)
            except FileNotFoundError:
                pass

        if not result:
            raise FileNotFoundError(
                "No active profiles found. Open OrcaSlicer and select a printer/profile first."
            )

        return result

    def get_profile(self, profile_type: str) -> dict:
        """Load the most recently modified profile of the given type."""
        key_map = {"printer": "machine", "process": "process", "filament": "filament"}
        folder_name = key_map.get(profile_type, profile_type)

        files = self._find_user_profiles(folder_name)
        if not files:
            # Try system profiles as fallback
            files = self._find_system_profiles(folder_name)
        if not files:
            raise FileNotFoundError(f"No {profile_type} profiles found in {self.config_dir}")

        newest = max(files, key=lambda p: p.stat().st_mtime)
        return self._load_json(newest)

    def get_named_profile(self, profile_type: str, name: str) -> dict:
        """Load a specific profile by name (filename without .json)."""
        key_map = {"printer": "machine", "process": "process", "filament": "filament"}
        folder_name = key_map.get(profile_type, profile_type)

        # Search user profiles first, then system
        for search_root in (self._user_dir, self._system_dir):
            for path in search_root.rglob(f"{folder_name}/*.json"):
                if path.stem == name or path.stem.lower() == name.lower():
                    return self._load_json(path)

        raise FileNotFoundError(f"Profile '{name}' not found in {profile_type}")

    def patch_active_process(self, changes: dict[str, Any]) -> dict:
        """
        Merge `changes` into the active process profile JSON and save.
        Returns the full updated profile.
        """
        files = self._find_user_profiles("process")
        if not files:
            raise FileNotFoundError(
                "No user process profiles found. Save a custom profile in OrcaSlicer first."
            )

        target = max(files, key=lambda p: p.stat().st_mtime)
        profile = self._load_json(target)
        profile.update(changes)

        target.write_text(
            json.dumps(profile, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return profile

    # ── Profile listing ──────────────────────────────────────────────────────

    def list_profiles(self) -> dict:
        result = {}
        for ptype in PROFILE_TYPES:
            user_files = self._find_user_profiles(ptype)
            system_files = self._find_system_profiles(ptype)
            result[ptype] = {
                "user": [p.stem for p in user_files],
                "system": [p.stem for p in system_files],
            }
        return result

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _find_user_profiles(self, folder_name: str) -> list[Path]:
        if not self._user_dir.exists():
            return []
        return list(self._user_dir.rglob(f"{folder_name}/*.json"))

    def _find_system_profiles(self, folder_name: str) -> list[Path]:
        if not self._system_dir.exists():
            return []
        return list(self._system_dir.rglob(f"{folder_name}/*.json"))

    def _load_json(self, path: Path) -> dict:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            # Resolve inheritance: if profile has "inherits", merge parent first
            if "inherits" in data:
                try:
                    parent_name = data["inherits"]
                    parent = self._resolve_parent(parent_name, path)
                    merged = {**parent, **data}
                    merged.pop("inherits", None)
                    return merged
                except Exception:
                    pass  # If parent resolution fails, just return as-is
            return data
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON in {path}: {e}")

    def _resolve_parent(self, parent_name: str, child_path: Path) -> dict:
        """Find and load a parent profile by name (for inheritance resolution)."""
        search_dir = child_path.parent
        parent_path = search_dir / f"{parent_name}.json"
        if parent_path.exists():
            return json.loads(parent_path.read_text(encoding="utf-8"))

        # Search system profiles
        for path in self._system_dir.rglob(f"**/{parent_name}.json"):
            return json.loads(path.read_text(encoding="utf-8"))

        return {}
