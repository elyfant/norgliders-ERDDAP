"""Client for OGDB-portal's gateway API.

The gateway (not raw Postgres) is the deliberate write path here --
dataset-processing writes have real domain logic (push status supersession,
DTO validation, version<->package integrity, audit fields) living in
DatasetsService, not just in the DB schema. Writing straight to Postgres
would mean reimplementing that logic in Python and risking drift.
Compare gliders/tracks-style tables, which have no such logic and are
written directly by other pipelines -- this one specifically isn't that
case.

Auth: the gateway currently only supports human password login (JWT with
a role claim, bcrypt against users.password_hash) -- there's no service-
account/API-key path yet. This client logs in once per run using the
serviceEmail/servicePassword values from config.json, which must belong
to a real user with role='editor' or 'admin'. Creating that user is a
one-time setup step outside this code (e.g. via the gateway's own
scripts/set-user-password.ts) -- not something this client can do itself.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np
import requests


def to_json(value: Any) -> Any:
    """NetCDF attribute values -> plain JSON. xarray keeps NumPy's own types
    (float32, int32, arrays, bytes), which json can't encode -- a Seaglider
    file's float32 attribute crashed a batch push (2026-10-12). NaN/inf
    become None: JSON has no NaN, and requests sends with allow_nan=False."""
    if isinstance(value, dict):
        return {str(k): to_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_json(v) for v in value]
    if isinstance(value, np.ndarray):
        return to_json(value.tolist())
    if isinstance(value, np.generic):
        return to_json(value.item())
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


class GatewayError(RuntimeError):
    pass


@dataclass
class NetcdfMetadata:
    level: str
    convention: str
    dimensions: dict[str, int]
    global_attrs: dict[str, Any]
    variables: dict[str, dict[str, Any]]

    def to_payload(self) -> dict:
        return to_json({
            "level": self.level,
            "convention": self.convention,
            "dimensions": self.dimensions,
            "globalAttrs": self.global_attrs,
            "variables": self.variables,
        })


class GatewayClient:
    def __init__(self, base_url: str, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._token: str | None = None

    def login(self, email: str, password: str) -> None:
        resp = requests.post(
            f"{self.base_url}/auth/login",
            json={"email": email, "password": password},
            timeout=self.timeout,
        )
        if resp.status_code != 201 and resp.status_code != 200:
            raise GatewayError(f"login failed: {resp.status_code} {resp.text}")
        # LoginResponse (@ogdb/types): { token, user }
        self._token = resp.json()["token"]

    def _headers(self) -> dict[str, str]:
        if self._token is None:
            raise GatewayError("not logged in -- call login() first")
        return {"Authorization": f"Bearer {self._token}"}

    def list_missions(self) -> list[dict]:
        """Every mission, with its best L1/L2 file and stage (GET /missions)."""
        resp = requests.get(f"{self.base_url}/missions", headers=self._headers(), timeout=self.timeout)
        if resp.status_code >= 400:
            raise GatewayError(f"list missions failed: {resp.status_code} {resp.text}")
        return resp.json()

    def get_dataset_detail(self, mission_id: int) -> dict:
        """The mission's dataset-processing detail (GET /datasets/:missionId),
        including erddapL1File / erddapL2File: the internal file currently
        live on ERDDAP for each level, or null."""
        resp = requests.get(f"{self.base_url}/datasets/{mission_id}", headers=self._headers(), timeout=self.timeout)
        if resp.status_code >= 400:
            raise GatewayError(f"dataset detail failed: {resp.status_code} {resp.text}")
        return resp.json()

    def set_erddap_url(self, mission_id: int, level: str, url: str) -> dict:
        """Store the dataset's public ERDDAP URL for L1/L2 in OGDB
        (dataset_processing.erddap_l1_url / erddap_l2_url) -- the portal's
        "NorGliders ERDDAP" links. Only that field is changed."""
        resp = requests.patch(
            f"{self.base_url}/datasets/{mission_id}/references",
            headers=self._headers(),
            json={f"erddap{level}Url": url},
            timeout=self.timeout,
        )
        if resp.status_code >= 400:
            raise GatewayError(f"set ERDDAP URL failed: {resp.status_code} {resp.text}")
        return resp.json()

    def find_mission_by_number(self, mission_number: int) -> dict | None:
        """The mission whose missions.mission_number is `mission_number` (the
        facility's permanent mission number, the NNN- prefix of its data
        folder) -- NOT missions.id, the database's internal key, which the
        other endpoints take. The returned row includes "id" for those, and
        bestL1File/bestL1Stage/bestL2File/bestL2Stage: the mission's best
        NetCDF per level from OGDB's mission_best_files view. None if no
        mission has that number."""
        matches = [m for m in self.list_missions() if m.get("missionNumber") == mission_number]
        return matches[0] if matches else None

    def register_document(
        self,
        mission_id: int,
        stage: str,
        file_reference: str,
        file_hash: str,
        file_size_bytes: int,
        metadata: NetcdfMetadata,
    ) -> dict:
        resp = requests.post(
            f"{self.base_url}/datasets/{mission_id}/documents",
            headers=self._headers(),
            json={
                "stage": stage,
                "fileReference": file_reference,
                "fileHash": file_hash,
                "fileSizeBytes": file_size_bytes,
                "netcdfMetadata": metadata.to_payload(),
            },
            timeout=self.timeout,
        )
        if resp.status_code >= 400:
            raise GatewayError(
                f"register_document failed: {resp.status_code} {resp.text}"
            )
        return resp.json()

    def confirm_erddap_push(self, mission_id: int, level: str, status: str, file: str | None = None) -> dict:
        """Record what's live on ERDDAP for `level`. `file` is the internal
        file that was pushed (relative to the projects folder, as stored on
        its processing run): OGDB links the push to that run."""
        payload = {"level": level, "status": status}
        if file:
            payload["file"] = file
        resp = requests.post(
            f"{self.base_url}/datasets/{mission_id}/erddap-status",
            headers=self._headers(),
            json=payload,
            timeout=self.timeout,
        )
        if resp.status_code >= 400:
            raise GatewayError(
                f"confirm_erddap_push failed: {resp.status_code} {resp.text}"
            )
        return resp.json()
