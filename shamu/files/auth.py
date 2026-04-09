"""
shamu.auth - API key generation and validation.

Shamu generates a random token on first run and saves it to
~/.config/shamu/token (or the shamu data dir). Addon developers
include it as a header: X-Shamu-Token: <token>

This stops:
  - Malicious websites making localhost requests (CORS + token together)
  - Other processes on the machine calling the API without permission
  - Accidental exposure if someone mistakenly binds to 0.0.0.0
"""

import os
import secrets
import stat
from pathlib import Path


TOKEN_LENGTH = 32  # 256 bits of entropy


def get_token_path(data_dir: Path) -> Path:
    return data_dir / "token"


def load_or_create_token(data_dir: Path) -> str:
    """
    Load the existing token from disk, or generate a new one.
    The token file is created with 0600 permissions (owner read/write only).
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    token_path = get_token_path(data_dir)

    if token_path.exists():
        token = token_path.read_text().strip()
        if len(token) >= TOKEN_LENGTH:
            return token
        # Token file exists but is malformed — regenerate
        print("[Shamu] Token file malformed, regenerating...")

    token = secrets.token_urlsafe(TOKEN_LENGTH)
    token_path.write_text(token)

    # Restrict to owner-only read/write (chmod 600)
    try:
        os.chmod(token_path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass  # Windows doesn't support chmod the same way; acceptable

    return token


def get_shamu_data_dir() -> Path:
    """Return Shamu's own config/data directory."""
    import platform
    system = platform.system()
    if system == "Windows":
        base = Path(os.environ.get("APPDATA", Path.home()))
    elif system == "Darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "shamu"
