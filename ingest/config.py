"""Load ingest/config.json.

Moved here from ogdp (which has its own config/app.json + js/src/config.js
convention) so this repo's ingest tooling doesn't reach back into a
sibling repo's config directory -- that cross-repo reach would be exactly
the kind of implicit coupling worth avoiding. config.json here is local
and flat (no "erddap" wrapper section -- the whole file is erddap-specific
already, unlike ogdp's app.json which covers SFMC too), gitignored, with
config.example.json committed as the template. Same "load JSON + validate
required fields with one error per missing field" shape as ogdp's loader,
kept for consistency even though the two are no longer the same file.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_CONFIG_PATH = Path(__file__).resolve().parent / "config.json"


def _require(d: dict, key: str) -> Any:
    if key not in d or d[key] in (None, ""):
        raise ValueError(f"Configuration error: missing {key} in {_CONFIG_PATH}.")
    return d[key]


def load_config() -> dict:
    if not _CONFIG_PATH.exists():
        raise FileNotFoundError(f"Missing configuration file: {_CONFIG_PATH}")
    with open(_CONFIG_PATH, encoding="utf-8") as f:
        config = json.load(f)

    return {
        "gateway_url": _require(config, "gatewayUrl"),
        "service_email": _require(config, "serviceEmail"),
        "service_password": _require(config, "servicePassword"),
        "sftp_host": _require(config, "sftpHost"),
        "sftp_user": _require(config, "sftpUser"),
        "sftp_key_path": _require(config, "sftpKeyPath"),
        "remote_base_path": config.get("remoteBasePath", "/data/ogdp/processed"),
        # Where the shared GFI projects folder is mounted on THIS machine.
        # OGDB stores NetCDF paths relative to it (e.g.
        # naco/data/delayed/095-.../basestation/x.nc), so they're the same
        # for everyone; only the mount point is per-machine.
        "projects_root": config.get("projectsRoot", "/Data/gfi/projects"),
    }
