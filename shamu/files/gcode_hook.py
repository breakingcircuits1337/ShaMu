"""
GCodeHook - finds and parses OrcaSlicer's sliced G-code output.

OrcaSlicer writes G-code to a temp/output directory. We find the most
recently written .gcode file and parse useful stats from its header
comments (OrcaSlicer embeds metadata as ; comments at the top).
"""

import re
from pathlib import Path
from typing import Optional
import tempfile
import os


# Patterns OrcaSlicer (and BambuStudio heritage) embeds in gcode headers
_STAT_PATTERNS = {
    "estimated_time_s":    re.compile(r";\s*estimated printing time.*?=\s*(\d+)s", re.I),
    "estimated_time_str":  re.compile(r";\s*estimated printing time[^=\n]*=\s*([^\n]+)", re.I),
    "filament_used_mm":    re.compile(r";\s*filament used \[mm\]\s*=\s*([\d.]+)", re.I),
    "filament_used_g":     re.compile(r";\s*filament used \[g\]\s*=\s*([\d.]+)", re.I),
    "filament_used_cm3":   re.compile(r";\s*filament used \[cm3\]\s*=\s*([\d.]+)", re.I),
    "layer_count":         re.compile(r";\s*total layer(?:s| count)\s*=\s*(\d+)", re.I),
    "layer_height":        re.compile(r";\s*layer_height\s*=\s*([\d.]+)", re.I),
    "nozzle_temp":         re.compile(r";\s*nozzle_temperature\s*=\s*(\d+)", re.I),
    "bed_temp":            re.compile(r";\s*bed_temperature\s*=\s*(\d+)", re.I),
    "print_speed":         re.compile(r";\s*print_speed\s*=\s*(\d+)", re.I),
    "infill_density":      re.compile(r";\s*sparse_infill_density\s*=\s*([\d.]+)", re.I),
    "support_enabled":     re.compile(r";\s*enable_support\s*=\s*(\d)", re.I),
    "printer_model":       re.compile(r";\s*printer_model\s*=\s*([^\n]+)", re.I),
    "filament_type":       re.compile(r";\s*filament_type\s*=\s*([^\n]+)", re.I),
}

# How many bytes of the gcode header to read for stat parsing (avoid reading huge files)
HEADER_READ_BYTES = 32_768  # 32KB is plenty for all header comments


class GCodeHook:
    def __init__(self, config_dir: Path):
        self.config_dir = config_dir
        self._search_dirs = self._build_search_dirs(config_dir)

    def _build_search_dirs(self, config_dir: Path) -> list[Path]:
        """Build list of directories to search for .gcode files."""
        candidates = [
            config_dir / "temp",
            config_dir / "output",
            Path(tempfile.gettempdir()),
            Path.home() / "Downloads",
            Path.home() / "Documents" / "3D Prints",
        ]
        return candidates

    def find_latest_gcode(self) -> Optional[Path]:
        """Find the most recently written .gcode file across all search dirs."""
        all_gcode: list[Path] = []

        for d in self._search_dirs:
            if d.exists():
                all_gcode.extend(d.glob("**/*.gcode"))
                all_gcode.extend(d.glob("**/*.3mf"))  # 3MF can contain gcode

        if not all_gcode:
            return None

        return max(all_gcode, key=lambda p: p.stat().st_mtime)

    def get_latest(self, path: Optional[Path] = None) -> str:
        """Return the full content of the latest (or specified) gcode file."""
        target = path or self.find_latest_gcode()
        if not target or not target.exists():
            raise FileNotFoundError("No G-code file found.")
        return target.read_text(encoding="utf-8", errors="replace")

    def parse_stats(self, path: Optional[Path] = None) -> dict:
        """
        Parse stats from a gcode file's header comments.
        Returns a dict with print time, filament usage, layer info, etc.
        """
        target = path or self.find_latest_gcode()
        if not target or not target.exists():
            raise FileNotFoundError("No G-code file found.")

        # Read only the header portion for efficiency
        with open(target, "r", encoding="utf-8", errors="replace") as f:
            header = f.read(HEADER_READ_BYTES)

        stats: dict = {
            "file": str(target),
            "file_size_kb": round(target.stat().st_size / 1024, 1),
        }

        for key, pattern in _STAT_PATTERNS.items():
            match = pattern.search(header)
            if match:
                raw = match.group(1).strip()
                # Coerce numeric fields
                if key in ("estimated_time_str", "printer_model", "filament_type"):
                    stats[key] = raw
                elif "." in raw:
                    try:
                        stats[key] = float(raw)
                    except ValueError:
                        stats[key] = raw
                else:
                    try:
                        stats[key] = int(raw)
                    except ValueError:
                        stats[key] = raw

        # Derive a human-readable time string if we only got seconds
        if "estimated_time_s" in stats and "estimated_time_str" not in stats:
            s = int(stats["estimated_time_s"])
            h, rem = divmod(s, 3600)
            m, sec = divmod(rem, 60)
            stats["estimated_time_str"] = f"{h}h {m}m {sec}s" if h else f"{m}m {sec}s"

        # Boolean coercion for support_enabled
        if "support_enabled" in stats:
            stats["support_enabled"] = bool(int(stats["support_enabled"]))

        # Count actual layers from G-code if layer_count not in header
        if "layer_count" not in stats:
            stats["layer_count"] = self._count_layers(header)

        return stats

    def _count_layers(self, gcode_text: str) -> Optional[int]:
        """Count layer change comments as a fallback layer counter."""
        # OrcaSlicer uses ; CHANGE_LAYER or ; layer_num = N
        patterns = [
            re.compile(r"^; CHANGE_LAYER", re.MULTILINE),
            re.compile(r"^;LAYER_CHANGE", re.MULTILINE),
            re.compile(r"^; layer_num =", re.MULTILINE),
        ]
        for p in patterns:
            matches = p.findall(gcode_text)
            if matches:
                return len(matches)
        return None
