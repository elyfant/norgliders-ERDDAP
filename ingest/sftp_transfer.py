"""Transfer a NetCDF file to the ERDDAP server over SFTP.

Uploads to a temp name and SFTP-renames to the final name as the last
step -- atomicity via the SFTP protocol itself (OpenSSH's internal-sftp
supports rename within a chroot), not a separate server-side move script.
This is what makes it safe for ERDDAP's own file-scanning reload cycle to
run at any time during a transfer: it either sees nothing (temp name,
which the fileNameRegex in datasets.d fragments doesn't match) or the
complete file, never a partial one.

Credential is meant to be a dedicated, narrowly-scoped SSH key --
restricted via `command="internal-sftp"` + ChrootDirectory in the
server's sshd config to one specific directory, not the general-purpose
key used to administer the box. Setting up that restriction is a
server-side task, not something this client enforces or assumes --
this code works the same way regardless of how tightly the credential
it's given is scoped.
"""

from __future__ import annotations

import hashlib
import os
import posixpath
import time
from dataclasses import dataclass, field

import paramiko


@dataclass
class TransferResult:
    remote_path: str
    file_size_bytes: int
    file_hash: str
    created_dirs: list[str] = field(default_factory=list)


def sha256_of(local_path: str) -> str:
    h = hashlib.sha256()
    with open(local_path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def ensure_remote_dir(sftp, remote_dir: str) -> list[str]:
    """Create remote_dir and any missing parents (like `mkdir -p`); returns
    the folders it created. A mission's first push needs its
    <level>/<mission_slug> folder -- the restricted erddap-push account owns
    L1/ and L2/ inside its chroot, so it can create those itself; anything
    it may not create (e.g. a typo'd level) fails loudly with the server's
    permission error rather than being worked around."""
    created = []
    path = "/" if remote_dir.startswith("/") else ""
    for part in [p for p in remote_dir.split("/") if p]:
        path = posixpath.join(path, part) if path else part
        try:
            sftp.stat(path)
        except FileNotFoundError:
            sftp.mkdir(path)
            created.append(path)
    return created


# Connection failures worth retrying: the network dropping the connection
# before or during login. Seen 2026-10-12: during a batch push (one new SSH
# connection per file, ~21/minute) something between UiB and NREC reset 8
# consecutive connections pre-auth ("Error reading SSH protocol banner ...
# Connection reset by peer"; the ERDDAP VM logged "Connection reset by
# <client> [preauth]" -- both ends saw the other hang up, so a middlebox
# cut them), then let later ones through. Retrying the whole upload is safe:
# it writes to a temp name and only renames into place once complete.
RETRY_WAITS_SECONDS = (10, 30, 90)
RETRYABLE = (paramiko.SSHException, EOFError, ConnectionError, TimeoutError, OSError)


def upload(*args, retry_waits=RETRY_WAITS_SECONDS, log=print, **kwargs) -> TransferResult:
    """_upload_once(), retried after a pause if the connection fails --
    see RETRY_WAITS_SECONDS. Non-connection errors (e.g. a size mismatch, a
    permission error creating a folder) are not retried."""
    for attempt, wait in enumerate((*retry_waits, None), start=1):
        try:
            return _upload_once(*args, **kwargs)
        except (PermissionError, FileNotFoundError):
            raise
        except RETRYABLE as e:
            if wait is None or "size mismatch" in str(e):
                raise
            log(f"  connection failed ({e}); retrying in {wait}s (attempt {attempt + 1} of {len(retry_waits) + 1})")
            time.sleep(wait)


def _upload_once(
    local_path: str,
    remote_dir: str,
    remote_filename: str,
    host: str,
    username: str,
    key_path: str,
    port: int = 22,
) -> TransferResult:
    file_hash = sha256_of(local_path)
    file_size = os.path.getsize(local_path)

    remote_final = posixpath.join(remote_dir, remote_filename)
    remote_temp = posixpath.join(remote_dir, f".{remote_filename}.part")

    key = paramiko.RSAKey.from_private_key_file(key_path)
    transport = paramiko.Transport((host, port))
    try:
        transport.connect(username=username, pkey=key)
        sftp = paramiko.SFTPClient.from_transport(transport)
        try:
            created_dirs = ensure_remote_dir(sftp, remote_dir)
            sftp.put(local_path, remote_temp)

            remote_size = sftp.stat(remote_temp).st_size
            if remote_size != file_size:
                sftp.remove(remote_temp)
                raise IOError(
                    f"transfer size mismatch: local {file_size} bytes, "
                    f"remote {remote_size} bytes -- removed incomplete upload"
                )

            sftp.posix_rename(remote_temp, remote_final)
        finally:
            sftp.close()
    finally:
        transport.close()

    return TransferResult(
        remote_path=remote_final, file_size_bytes=file_size, file_hash=file_hash, created_dirs=created_dirs
    )
