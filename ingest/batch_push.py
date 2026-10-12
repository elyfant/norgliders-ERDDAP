"""Push every mission's best L1 and L2 file to ERDDAP in one go.

    python ingest/batch_push.py                      # dry run: check every file, show what would happen
    python ingest/batch_push.py --commit             # push them, and record each as live in OGDB
    python ingest/batch_push.py --mission 4 --mission 23 --commit   # just these missions

For each mission that has a best L1 and/or L2 file in OGDB (mission_best_files
-- missions whose L1 is unusable simply have none, so only their L2 goes),
this runs the same steps 2-6 as ingest.py (push_file), with:

- the mission name on ERDDAP = the mission's std_mission_name from OGDB, the
  naming decided for the first push (e.g. sg559_naco_lofoten_jul2012) -- so
  nobody types 76 names by hand;
- the mission's ERDDAP folder created on its first push (sftp_transfer.py);
- files already live on ERDDAP AND still the best file skipped, so a re-run
  only pushes what's new or better (e.g. after reprocessing). --force pushes
  them again anyway;
- with --commit, every push recorded live in OGDB (--confirm-live), linked to
  its processing run -- a batch that uploads without recording would leave
  OGDB not knowing what's on ERDDAP;
- one file failing (unreadable, wrong level, transfer error) reported and the
  rest carried on; a summary at the end; exit code 1 if anything failed.

Needs the same setup as ingest.py: ingest/config.json, and the SSH tunnel to
the portal API (see ingest/README.md).
"""

from __future__ import annotations

import argparse
import os
import sys

from config import load_config
from gateway_client import GatewayClient, GatewayError
from ingest import PushError, fail, push_file

LEVELS = ("L2", "L1")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--commit", action="store_true", help="Actually push (and record live). Default is dry-run.")
    parser.add_argument("--mission", type=int, action="append", metavar="NUMBER",
                        help="only this mission number (repeatable); default: every mission with a best file")
    parser.add_argument("--level", choices=LEVELS, help="only this level; default: both")
    parser.add_argument("--force", action="store_true", help="also push files that are already live and current")
    parser.add_argument("--verbose", action="store_true", help="print each file's full check and plan")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        config = load_config()
    except (FileNotFoundError, ValueError) as e:
        fail(str(e))

    client = GatewayClient(base_url=config["gateway_url"])
    try:
        client.login(config["service_email"], config["service_password"])
        missions = client.list_missions()
    except GatewayError as e:
        fail(str(e))

    wanted = set(args.mission or [])
    missions = sorted(
        (m for m in missions
         if (m.get("bestL1File") or m.get("bestL2File")) and (not wanted or m["missionNumber"] in wanted)),
        key=lambda m: m["missionNumber"],
    )
    if wanted - {m["missionNumber"] for m in missions}:
        print(f"Note: no best file in OGDB for mission(s) {sorted(wanted - {m['missionNumber'] for m in missions})}")
    levels = [args.level] if args.level else list(LEVELS)

    done, skipped, failed = [], [], []
    for m in missions:
        number, slug = m["missionNumber"], (m.get("stdMissionName") or "").lower()
        if not slug:
            for level in levels:
                failed.append((number, level, "mission has no std_mission_name in OGDB (glider/project/site/launch date missing)"))
            continue
        try:
            live = client.get_dataset_detail(m["id"])
        except GatewayError as e:
            for level in levels:
                failed.append((number, level, str(e)))
            continue

        for level in levels:
            file_in_ogdb, stage = m.get(f"best{level}File"), m.get(f"best{level}Stage")
            if not file_in_ogdb:
                continue  # e.g. an unusable L1 -- on the reprocessing list, nothing to push
            label = f"{number:03d} {slug} {level}"
            if live.get(f"erddap{level}File") == file_in_ogdb and not args.force:
                skipped.append((number, level, f"already live and current ({stage})"))
                print(f"{label}: skipped -- already live and current")
                continue
            path = os.path.join(config["projects_root"], file_in_ogdb)
            log = (lambda line, _l=label: print(f"    {line}")) if args.verbose else (lambda line: None)
            if args.verbose:
                print(f"{label}: {file_in_ogdb} ({stage})")
            try:
                result = push_file(client, config, m, level, slug, path, stage, file_in_ogdb,
                                   commit=args.commit, confirm_live=args.commit, log=log)
            except PushError as e:
                failed.append((number, level, str(e)))
                print(f"{label}: FAILED -- {e}")
                continue
            except Exception as e:  # anything unexpected: report it, carry on with the next file
                failed.append((number, level, f"unexpected error: {type(e).__name__}: {e}"))
                print(f"{label}: FAILED -- unexpected error: {type(e).__name__}: {e}")
                continue
            done.append((number, level, result))
            print(f"{label}: {result}")

    print()
    print("=" * 70)
    verb = "pushed" if args.commit else "would be pushed"
    print(f"{len(done)} file(s) {verb}, {len(skipped)} skipped (already live and current), {len(failed)} failed")
    for number, level, why in failed:
        print(f"  FAILED {number:03d} {level}: {why}")
    if not args.commit:
        print("Dry run -- nothing written or uploaded. Re-run with --commit to push.")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
