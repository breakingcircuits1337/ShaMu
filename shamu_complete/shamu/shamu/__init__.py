"""Shamu - Local API bridge for OrcaSlicer addon development."""
from .server import create_app, get_default_orca_config_dir
__version__ = "0.1.0"
__all__ = ["create_app", "get_default_orca_config_dir"]
