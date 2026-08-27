"""Client for OGDB-portal's gateway API.

The gateway (not raw Postgres) is the deliberate write path here --
dataset-processing writes have real domain logic (DM/PUB supersession,
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

from dataclasses import dataclass
from typing import Any

import requests


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
        return {
            "level": self.level,
            "convention": self.convention,
            "dimensions": self.dimensions,
            "globalAttrs": self.global_attrs,
            "variables": self.variables,
        }


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

    def confirm_erddap_push(self, mission_id: int, level: str, status: str) -> dict:
        resp = requests.post(
            f"{self.base_url}/datasets/{mission_id}/erddap-status",
            headers=self._headers(),
            json={"level": level, "status": status},
            timeout=self.timeout,
        )
        if resp.status_code >= 400:
            raise GatewayError(
                f"confirm_erddap_push failed: {resp.status_code} {resp.text}"
            )
        return resp.json()
