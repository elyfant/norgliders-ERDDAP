"""Push a mission's best L1 or L2 NetCDF to the ERDDAP server, and record in
OGDB exactly which file is live there.

    python ingest.py <mission_number> <L1|L2> <mission_slug> [--commit] [--confirm-live]

What it does, in order (main() follows these steps one by one):

1. Find the file in OGDB. The mission is looked up by its mission_number --
   the facility's permanent number, the NNN- prefix of its data folder (not
   the database's internal missions.id). Its best file for the level, and
   that file's processing stage, come from OGDB's mission_best_files view:
   the highest QC level among completed processing runs, then the latest.
   That can be the basestation's automatic product (BASESTATION), the
   reprocessed auto-QC one (AUTO_QC) or the manually QC'd one (MANUAL_QC) --
   ERDDAP always gets the best file currently available. Nobody types a
   path. OGDB stores it relative to the shared projects folder; config.json's
   projectsRoot says where that folder is mounted on this machine.
   --file/--stage override this for a file OGDB doesn't know about yet.

2. Check the file: it exists, it really is the requested level (L1 or L2),
   and its processing convention is recognised. Anything doubtful stops the
   run before anything is written or sent.

3. Show the plan. Without --commit, stop here (dry run).

4. Register the file in OGDB first (a `documents` row with the ERDDAP-side
   path, hash, size and NetCDF metadata) -- BEFORE the transfer. If the
   transfer then fails, OGDB shows a file that isn't on ERDDAP yet: a gap
   you can see. Transferring first risks the opposite -- a file live on
   ERDDAP that OGDB was never told about. A gap you can see beats a lie you
   can't.

5. Upload it to ERDDAP under a FIXED name per mission and level,
   <mission_slug>_<level>.nc, in <remoteBasePath>/<level>/<mission_slug>/ --
   creating that folder first if this is the mission's first push.
   The upload goes to a temporary name and is renamed into place in one
   step (see sftp_transfer.py), so a better file replaces the previous one
   atomically: ERDDAP never sees two files, or half of one. The original
   file name isn't lost -- OGDB records which file it was (step 6).

6. With --confirm-live only: record in OGDB that this level is now live on
   ERDDAP (erddap_pushes), linked to the processing run whose file was sent,
   so OGDB can always say which internal file is on ERDDAP and whether a
   better one exists (mission_erddap_status.is_current). Kept as a separate,
   explicit step: whether ERDDAP actually serves the new file yet (dataset
   fragment written, reloaded) is something this script can't verify.

Re-running for a mission is how ERDDAP gets updated when a better file
appears (e.g. after reprocessing): the new best file replaces the old one.
To push every mission at once, see batch_push.py -- it runs steps 2-6 here
(push_file) for each mission's best L1 and L2.

Configuration comes from ingest/config.json (see config.py), not
environment variables -- needed for a dry run too, since step 1 asks OGDB
(through the gateway), which needs a login.

This lives in norgliders-ERDDAP so ERDDAP-facing tooling is in one place.
It runs the same wherever it's executed; the repo it's checked out from
doesn't have to match the machine it runs on.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from config import load_config
from gateway_client import GatewayClient, GatewayError, NetcdfMetadata
from inspect_netcdf import inspect_netcdf
from sftp_transfer import sha256_of, upload

PUSHABLE_STAGES = ("BASESTATION", "AUTO_QC", "MANUAL_QC")


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(1)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "mission_number", type=int,
        help="the mission's number (missions.mission_number, the NNN- folder prefix) -- not the database id",
    )
    parser.add_argument("level", choices=["L1", "L2"], help="which product to push")
    parser.add_argument(
        "mission_slug",
        help="Directory name under <remoteBasePath>/<level>/ on the ERDDAP server for this mission "
        "(matches the fileDir in the mission's datasets.d fragment); also names the uploaded file",
    )
    parser.add_argument("--file", help="override: push this local file instead of OGDB's best file (requires --stage)")
    parser.add_argument("--stage", choices=PUSHABLE_STAGES, help="override: processing stage of --file")
    parser.add_argument("--commit", action="store_true", help="Actually register and transfer. Default is dry-run.")
    parser.add_argument(
        "--confirm-live", action="store_true",
        help="Also record in OGDB that this file is live on ERDDAP (erddap_pushes). Only with --commit.",
    )
    args = parser.parse_args()
    if bool(args.file) != bool(args.stage):
        parser.error("--file and --stage go together")
    return args


def stored_path(local_path: str, projects_root: str) -> str | None:
    """local path -> the form OGDB stores (relative to the projects folder),
    or None if the file is outside it."""
    rel = os.path.relpath(os.path.normpath(os.path.abspath(local_path)), os.path.normpath(projects_root))
    return None if rel.startswith("..") else rel.replace(os.sep, "/")


class PushError(RuntimeError):
    """A push stopped at one of steps 2-6; the message says where and why."""


def push_file(client, config, mission, level, slug, path, stage, file_in_ogdb,
              commit=False, confirm_live=False, log=print):
    """Steps 2-6 for one mission and level, once step 1 has chosen the file:
    check it, show the plan, and with commit register / upload / (with
    confirm_live) record it live. Raises PushError instead of exiting, so
    batch_push.py can carry on with the next file. Returns a short summary."""
    mission_id = mission["id"]
    number = mission["missionNumber"]

    if stage not in PUSHABLE_STAGES:
        raise PushError(f"the {level} file's stage is {stage}; only {', '.join(PUSHABLE_STAGES)} files are pushed")
    if confirm_live and not file_in_ogdb:
        raise PushError("--confirm-live needs a file OGDB knows (inside the projects folder) to link the push to")

    # --- 2. Check the file ------------------------------------------------
    if not os.path.isfile(path):
        raise PushError(f"no such file on this machine: {path} (check projectsRoot in config.json)")
    report = inspect_netcdf(path)
    log(f"Convention: {report.convention}")
    log(f"Level:      {report.level}")
    log(f"Dimensions: {report.dimensions}")
    log(f"Variables:  {len(report.variables)}")
    for w in report.warnings:
        log(f"  WARNING: {w}")
    if report.level != level:
        raise PushError(f"asked for {level}, but the file looks like {report.level} -- refusing to proceed")
    if report.convention == "unknown":
        raise PushError("could not classify the processing convention -- a human should look at this file first")
    # Build what step 4 will send to OGDB now, so a dry run catches anything
    # that can't be sent (it once found an unencodable NetCDF attribute only
    # mid-batch, at --commit).
    metadata = NetcdfMetadata(
        level=report.level,
        convention=report.convention,
        dimensions=report.dimensions,
        global_attrs=report.global_attrs,
        variables={k: {"dims": list(v.dims), "dtype": v.dtype} for k, v in report.variables.items()},
    )
    try:
        json.dumps(metadata.to_payload(), allow_nan=False)
    except (TypeError, ValueError) as e:
        raise PushError(f"the file's metadata can't be sent to OGDB: {e}") from e

    # --- 3. Show the plan -------------------------------------------------
    remote_dir = f"{config['remote_base_path']}/{level}/{slug}"
    remote_filename = f"{slug}_{level}.nc"
    remote_path = f"{remote_dir}/{remote_filename}"
    log("")
    log("Plan:")
    log(f"  1. Register in OGDB: mission {number}, stage={stage}, "
        f"document_type={stage.lower()}_output, file on ERDDAP {remote_path}")
    log(f"  2. Upload {path}")
    log(f"       -> {config['sftp_host']}:{remote_path}  (replaces whatever was there; "
        f"creates {level}/{slug}/ if it doesn't exist yet)")
    if confirm_live:
        log(f"  3. Record live on ERDDAP: level={level}, status={stage}, linked to {file_in_ogdb}")
    if not commit:
        return f"would push {stage} file"

    # --- 4. Register the file in OGDB (before the transfer) --------------
    log("")
    log("Registering in OGDB...")
    try:
        client.register_document(
            mission_id=mission_id,
            stage=stage,
            file_reference=remote_path,
            file_hash=sha256_of(path),
            file_size_bytes=os.path.getsize(path),
            metadata=metadata,
        )
    except GatewayError as e:
        raise PushError(f"{e} -- nothing was transferred") from e
    log("  registered.")

    # --- 5. Upload to ERDDAP (atomic replace) ----------------------------
    log("Uploading via SFTP...")
    try:
        transfer = upload(
            local_path=path,
            remote_dir=remote_dir,
            remote_filename=remote_filename,
            host=config["sftp_host"],
            username=config["sftp_user"],
            key_path=config["sftp_key_path"],
            log=print,  # retry notices always shown, even in batch_push's quiet mode
        )
    except Exception as e:
        raise PushError(
            f"transfer failed: {e}. OGDB already has the file registered but it is NOT on ERDDAP -- "
            "re-run to retry the upload."
        ) from e
    for d in transfer.created_dirs:
        log(f"  created folder on ERDDAP: {d}")
    log(f"  transferred: {transfer.remote_path} ({transfer.file_size_bytes} bytes, sha256={transfer.file_hash})")

    # --- 6. Record it live, linked to its processing run -----------------
    if confirm_live:
        try:
            client.confirm_erddap_push(mission_id=mission_id, level=level, status=stage, file=file_in_ogdb)
        except GatewayError as e:
            raise PushError(f"{e} -- the file IS on ERDDAP, but OGDB doesn't record it as live yet") from e
        log(f"  recorded live: level={level}, status={stage}, file={file_in_ogdb}")
        return f"pushed {stage} file, recorded live"
    return f"pushed {stage} file (not recorded live)"


def main() -> None:
    args = parse_args()
    try:
        config = load_config()
    except (FileNotFoundError, ValueError) as e:
        fail(str(e))

    # --- 1. Find the file in OGDB ---------------------------------------
    client = GatewayClient(base_url=config["gateway_url"])
    try:
        client.login(config["service_email"], config["service_password"])
        mission = client.find_mission_by_number(args.mission_number)
    except GatewayError as e:
        fail(str(e))
    if mission is None:
        fail(f"no mission with mission_number {args.mission_number} in OGDB")
    name = mission.get("stdMissionName") or mission.get("missionName")
    print(f"Mission:    {args.mission_number} ({name}), OGDB id {mission['id']}")

    if args.file:
        path, stage = args.file, args.stage
        file_in_ogdb = stored_path(path, config["projects_root"])
        print(f"File:       {path}  (given with --file, stage {stage})")
    else:
        file_in_ogdb = mission.get(f"best{args.level}File")
        stage = mission.get(f"best{args.level}Stage")
        if not file_in_ogdb:
            fail(f"OGDB has no {args.level} file recorded for mission {args.mission_number}")
        path = os.path.join(config["projects_root"], file_in_ogdb)
        print(f"Best {args.level}:    {file_in_ogdb}  (stage {stage})")

    try:
        push_file(client, config, mission, args.level, args.mission_slug, path, stage, file_in_ogdb,
                  commit=args.commit, confirm_live=args.confirm_live)
    except PushError as e:
        fail(str(e))
    if not args.commit:
        print()
        print("Dry run -- nothing written. Pass --commit to actually do this.")


if __name__ == "__main__":
    main()
