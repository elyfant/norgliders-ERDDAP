# ERDDAP Server — Design Notes & Handoff

Context for picking this up in a fresh Claude Code session started inside this
folder. Written after a planning session in a different Claude Code session
(working directory `OGDB-portal`) — that session's memory doesn't carry over
here, so read this first.

## Goal

A file/storage/serving system for 100+ ocean glider mission datasets (3-4
NetCDF products per mission: L0 raw timeseries, L1 processed timeseries, L2
gridded), following the same approach several colleague institutions already
use (VOTO, IOOS Glider DAC, EGO) rather than inventing something new.

## Cross-project context: norgliders (facility planning)

`~/projects/norgliders` holds the facility-wide system map, open
cross-repo dependency questions, and architecture decisions. Claude Code
doesn't share memory or CLAUDE.md context across separate git repos, so
without help a session working here has no way to know about decisions
made there.

Fix: a symlink into `.claude/rules/`, which Claude Code loads automatically
every session. It's gitignored (machine-local, points at an absolute path
that only resolves on this machine) — recreate it after a fresh clone or on
a new machine:

```bash
mkdir -p .claude/rules
ln -s ~/projects/norgliders/dependencies.md .claude/rules/norgliders-dependencies.md
ln -s ~/projects/norgliders/decisions .claude/rules/norgliders-decisions
```

## Core decision: ERDDAP, Option A (one dataset per mission per level)

- **What ERDDAP actually is**: a Java web app (runs in Apache Tomcat), not an
  OS — installed on top of Ubuntu, not instead of it. Reads NetCDF files
  straight off disk (no ingestion step); a "dataset" entry in `datasets.xml`
  just points at a directory + describes the schema.
- **L0/L1/L2 map onto ERDDAP's two dataset types, not one file format**:
  - L0 (raw) and L1 (processed) timeseries are irregular along-track
    sampling → `tabledap`, CF Discrete Sampling Geometry "trajectory"
    featureType (`EDDTableFromNcCFFiles`).
  - L2 is a true regular time×depth grid (`pyglider`'s gridded output) →
    `griddap` (`EDDGridFromNcFiles`).
- **Granularity: Option A, one ERDDAP dataset per mission per level** (~300-
  400 dataset IDs total), matching VOTO's own pattern exactly — see
  `voto_erddap_data_cache/cache_info.csv` in projects/ for a live example
  (`delayed_SEA045_M69` / `nrt_SEA045_M69` as separate dataset IDs for the
  same mission). Rejected Option B (aggregating many missions into one
  dataset ID per platform/level) as a *later*, additive step — not before
  OG1 schema consistency across the whole fleet is actually proven, since a
  bad file in an aggregated dataset takes down everything sharing that ID.
- **Cross-cutting queries (region, date range, variable) don't require
  picking one grouping scheme.** ERDDAP's catalog search operates on each
  dataset's own global attributes (`geospatial_lat/lon_min/max`,
  `time_coverage_start/end`, variable list) regardless of how many datasets
  exist or how they're named. A query like "chl-a in the Iceland Sea in
  2024" is a two-step pattern: (1) catalog search across all datasets by
  bbox/time/variable → list of matching dataset IDs, (2) fetch + concatenate
  from each matching dataset. `erddapy` (IOOS-maintained, open source,
  Python) automates exactly this loop.
- **Region ("Iceland Sea" etc.) is metadata, not structure.** Don't group
  datasets by region on disk/by ID — add a free-text global attribute
  (e.g. `region = "iceland_sea"`) per mission file instead, registered as an
  ERDDAP `categoryAttribute` for browse/filter. Stackable with other tags
  (platform type, PI, project) without committing to one taxonomy. Plain
  bounding-box queries work with zero tagging at all.

## Variable naming: pyglider's actual output, not the OG1 spec's

**Corrected after the first pass got this wrong.** The first draft of
`datasets_example.xml` used uppercase short codes (`TEMP`, `PSAL`, `CNDC`,
`DOXY`, `CHLA`, `TRAJECTORY`) taken from
`SL_processing/OG1/OG-format-user-manual/OG_Format.adoc` (a local clone of
the OceanGliders OG1.0 format spec), on the assumption that OGDP's stated
OG1 output target meant literally those variable names.

That assumption was wrong, caught by actually reading the tool that
produces the files: **pyglider is installed in several of your own venvs**
(version `0.0.7`, e.g. `SL_processing/working/pyglider`), and its real
source — `pyglider/utils.py`, `pyglider/ncprocess.py`, and the example
`deployment*.yml` configs under `tests/example-data/example-slocum/` —
shows it emits **lowercase, CF-standard-name-derived variable names**:
`time`, `latitude`, `longitude`, `depth`, `pressure`, `temperature`,
`conductivity`, `salinity` (derived via TEOS-10/gsw, not a raw sensor
column), `potential_density`, `density`, `chlorophyll`, `cdom`,
`backscatter_700`, `oxygen_concentration`. This matches VOTO's real
production ERDDAP exactly — `voto_erddap_data_cache/cache_info.csv` shows
their actual tabledap URLs requesting `temperature,chlorophyll,salinity,
oxygen_concentration,potential_density`, not OG1-spec uppercase codes.
Makes sense: VOTO's glider processing is one of pyglider's original use
cases, so their real output is a better predictor of what OGDP will
actually produce than a spec-manual draft.

Verified specifics:
- Trajectory identifier: pyglider itself writes a **lowercase** `trajectory`
  variable (`ncprocess.py`: `dss['trajectory']`, `cf_role='trajectory_id'`,
  `long_name='Trajectory/Deployment Name'`) — not uppercase `TRAJECTORY`.
- QC companion variables use pyglider's own convention
  (`utils.fill_required_qcattrs`), **not** the OG1 spec's 0-4 flag scheme —
  pyglider uses QARTOD-style `flag_values = [1, 2, 3, 4, 9]`,
  `flag_meanings = "PASS NOT_EVALUATED SUSPECT FAIL MISSING"`, naming
  pattern `<var>_qc` (this part carried over correctly from the first
  draft — only the flag scheme was wrong).
- `salinity`/`potential_density`/`density` are **derived**
  (`utils.get_derived_eos_raw`, gsw/TEOS-10), not raw sensor output — absent
  from L0 files, present once that processing step has run (so present in
  L1, not L0).

**Still not fully verified** — two open questions to settle against real
OGDP output before treating this as final, not the format spec:
- Which pyglider version OGDP actually pins. `0.0.7` is what's in the local
  venvs checked; a newer release might add an explicit OG1-compliant export
  step with different naming — if so this is a mechanical rename, not a
  redesign.
- Whether OGDP's own processing renames or adds anything on top of raw
  pyglider output before a file counts as "L1"/"L2".

`erddap-config/datasets_example.xml` has been rewritten against this
verified convention. Global attributes worth carrying into `datasets.xml`
`<addAttributes>`: `institution`, `geospatial_lat/lon_min/max`,
`time_coverage_start/end`, `featureType = "trajectory"`,
`Conventions = "CF-1.10, ACDD-1.3"` (dropped `OG-1.0` from Conventions
pending the OG1-version question above).

**`erddap-config/datasets_example.xml`** — the worked template: one full L1
tabledap `<dataset>` block, L0 noted as a 4-line diff from it (different
`fileDir`, `l0_` ID prefix, shorter reload interval, no QC yet), one L2
griddap `<dataset>` block. Comments in the file explain the reasoning inline.
**Not final config** — see "Next steps" below for turning this into the real
~300-400 entries.

## NREC VM (provisioned)

Server: **University of Bergen / University of Oslo's own research cloud**
(NREC, OpenStack-based) — open-source-aligned, on-institution infrastructure,
consistent with the general preference for that over commercial cloud.

Live at `158.39.77.95` (SSH alias `nrec_erddap` in `~/.ssh/config`, user
`ubuntu`). Ubuntu 24.04.4 LTS.

- **Flavor actually provisioned: `m1.medium` (1 vCPU / ~4 GB RAM / 20 GB
  root disk)** — smaller than the `m1.large` (2 vCPU / 8 GB) recommended
  during planning. Not treated as a blocker since NREC's flavor-resize flow
  (reboot, no rebuild) makes this a cheap fix later if the JVM heap proves
  tight once Docker + Tomcat + Caddy are all running together at real
  dataset counts — but worth watching, and the `ERDDAP_MAX_RAM_PERCENTAGE`
  setting in `docker-compose.yml` was sized conservatively (80%) with this
  smaller flavor in mind, not the originally planned one. 20 GB root
  confirmed as expected either way.
- **Data storage: separate Cinder block volume, NOT the root disk.** Start
  at ~50-100 GB (covers the first several dozen missions at ~2 GB/mission),
  mount at `/data` (matches the `fileDir` paths already used in
  `datasets_example.xml`, e.g. `/data/ogdp/processed/L1/...`). Eventual
  target is ~200 GB as all 100+ missions land — get there via Cinder's
  online volume extend (`openstack volume set --size` or dashboard "Extend
  Volume") + `resize2fs` inside the instance, not by over-provisioning
  upfront. Confirmed NREC/Cinder supports extending an attached/in-use
  volume this way.
- **Networking: dualStack** (public IPv4 + public IPv6), not IPv6-only.
  Decided because external collaborators / the broader glider community need
  to reach this ERDDAP instance directly (same pattern as VOTO's public
  ERDDAP), and IPv6-only would silently fail for anyone without working
  IPv6 on their end. NREC's own default guidance is IPv6-only (their public
  IPv4 space is explicitly scarce/rationed) — dualStack was a deliberate
  exception for this specific instance, not a blanket default for every
  future NREC instance in this project. **Confirmed live**: instance has
  both a public IPv4 (`158.39.77.95`) and a public IPv6 address.
- **Security groups**: don't reuse an existing shared group (e.g. whatever
  `nrec-app` turns out to be) — NREC's own guidance is to create a new group
  per required ruleset and apply it *alongside* `default`, since a shared
  group's rules can silently change under you if another instance's needs
  change it later.
  - At instance creation: `default` + an SSH-access group (one of NREC's
    pre-made templates — UiB login hosts, or sector-wide if outside
    collaborators need shell access too).
  - New dedicated group **`erddap-web`**: ingress 80/443 from `0.0.0.0/0`
    and `::/0` (dualStack needs both families covered). Add this to the
    instance only once something is actually listening behind it — not at
    creation time.
  - Tomcat's raw port (8080) should never be in a security group at all.
    Plan: bind Tomcat to `localhost:8080`, put a reverse proxy in front
    handling 80/443 + TLS — same pattern the `OGDB-portal` repo already uses
    (`Caddyfile` there uses Caddy for automatic HTTPS). Reuse that approach
    here rather than configuring Tomcat's own SSL.
  - Security group membership can be added/removed from a running instance
    live (standard OpenStack behavior, enforced at the network-port level) —
    not confirmed explicitly in NREC's docs but true across effectively all
    OpenStack/Horizon deployments; worth a quick sanity check in the
    dashboard once there.

## Docker deployment

Running via Docker Compose, not a bare Tomcat install (plan changed after
finding https://hub.docker.com/r/erddap/erddap — see the reasoning below).

- **Image: `axiom/docker-erddap`, not the plain official `erddap/erddap`
  image.** Matches what VOTO's own ERDDAP is based on ("a docker image
  based on the one produced by axiom" — their public write-up). The
  concrete reason it matters here: axiom's image adds a **`datasets.d`
  mode** — individual dataset fragments (one bare `<dataset>...</dataset>`
  block per file, no `<erddapDatasets>` wrapper) dropped into a
  `/datasets.d` directory, concatenated into the real `datasets.xml` at
  container start (sorted by file path). This is exactly the "one file per
  mission × level, generated by a script" workflow already planned above —
  no datasets.xml merge logic of our own to write.
- **Repo `norgliders-ERDDAP` is the deploy config**, cloned onto the server
  at `~/norgliders-ERDDAP` (HTTPS clone, repo is public — no deploy key
  needed). `docker-compose.yml` and `Caddyfile` live at the repo root;
  dataset fragments at `erddap-config/datasets.d/`. Same pattern as
  `OGDB-portal`'s `docker-compose.yml` + `Caddyfile` at its root.
- **Bind mounts, not named volumes, for everything under `/data`** — see
  `docker-compose.yml` comments for the four paths (ERDDAP content dir,
  `bigParentDirectory`/`erddapData`, `datasets.d`, and the actual mission
  NetCDF `fileDir` paths). This is what makes the Cinder-volume-extend
  story from the VM section above still work once Docker is in the
  picture: none of this data is Docker-managed, so growing the underlying
  volume needs zero Docker/container changes. The trap avoided: a bare
  named volume (`docker volume create`, no host path) would default to
  living under `/var/lib/docker` on the 20 GB root disk, not the Cinder
  volume.
- **Caddy runs as a second compose service**, not a host-level install —
  publishes 80/443 to the host (all `erddap-web` needs to allow), proxies
  to the `erddap` container over the internal Docker network. Tomcat's
  8080 is `expose`d to the compose network only, never published to the
  host.
- **No domain yet** — `Caddyfile` currently serves plain HTTP on the bare
  IP (`:80`), since Caddy's automatic HTTPS can't issue a Let's Encrypt cert
  for a bare IP address. Switching to a real hostname (NREC's DNS service,
  or a `uib.no` subdomain) is a same-file edit — see the comment at the top
  of `Caddyfile`.
- **VOTO's `xml_edit` repo — deliberately not used.** Read it during
  planning: thin, informally maintained (sparse README, explicitly warns
  against running its core script directly), built around VOTO's
  monolithic-datasets.xml workflow. Axiom's `datasets.d` mode solves the
  same problem more directly for how we're already generating fragments.

**Outstanding blocker: the Cinder data volume from the VM section above has
not been created/attached yet** — `lsblk` on the server shows only the
root disk. `/data` currently doesn't exist as a mount point. Until it's
attached and mounted at `/data`, any bind-mounted paths under `/data` that
Docker auto-creates will land on the 20 GB root disk — fine for a
smoke-test (near-zero data right now) but must be swapped for the real
volume before any actual OGDP mission data shows up, or it'll fill the
root disk. Creating/attaching this volume requires the NREC dashboard —
not something doable from here without NREC credentials.

### Verified end-to-end (2026-08-23)

Deployed and debugged live on `nrec_erddap`, not just written and assumed
correct. Three real bugs were caught and fixed in the process, all now
pushed:

1. **Bind-mounting the whole ERDDAP content directory broke startup** —
   `NoSuchFileException` on `setup.xml`. A bind mount replaces a
   directory's entire contents rather than adding to them, which hid the
   image's baked-in default `setup.xml`. Fixed by not mounting that
   directory at all (axiom's own docs say this is correct for `datasets.d`
   mode) and relying on `ERDDAP_*` env vars instead.
2. **XML comments in both dataset fragments used `--` as a dash separator**
   — invalid per the XML spec (a literal double-hyphen inside a comment
   body is disallowed), which made `xmlstarlet` reject both fragments
   during `datasets.d` assembly. This was silent at the Docker level (both
   containers reported healthy) and only visible in ERDDAP's own log —
   worth remembering next time something looks up but a dataset doesn't.
   Fixed with a script that only touches comment interiors, not the `<!--`
   /`-->` delimiters, and validated with an actual XML parser before
   pushing.
3. **The griddap fragment was missing `<dataType>` on every axis/data
   variable** (the tabledap fragment had it, griddap didn't) — surfaced as
   `"Unspecified data type for var#0."` once the XML was valid. With no
   real file yet to infer types from, ERDDAP has nothing to fall back on.
   Fixed in both the deployed fragment and the reference copy.

After those fixes, confirmed genuinely end-to-end rather than assumed:
dropped one small synthetic NetCDF file (correct schema, obviously labeled
as test data, since deleted) into the L1 `fileDir`, restarted the `erddap`
container (dataset construction only fully retries on a *major* reload,
which happens on container start — the `setDatasetFlag`/`allDatasets`
mechanism only triggers *minor* reloads that don't retry previously-failed
datasets), and confirmed:
- `l1_durin_20240614T090000` tabledap went fully active (`HTTP 200` on
  `.das`, and a real constrained data query — `?time,latitude,...&time>=...`
  — returned correct subsetted rows).
- **Reachable from outside NREC entirely**, not just `localhost` on the
  server: `curl http://158.39.77.95/erddap/...` from an external network
  returned `HTTP 200` — proves the whole chain (`erddap-web` security
  group, dualStack networking, Caddy reverse proxy, Docker networking,
  ERDDAP itself) actually works together, not just each piece in isolation.
- Test file removed afterward; server is back to its honest state (both
  datasets deferred/empty, waiting on real data + the Cinder volume).

**L2 (griddap) was not re-verified with a real file** — only the
`dataType` fix was confirmed to clear the earlier hard error; a synthetic
grid file was not built to prove full activation the way L1 was. Worth
doing the same test once real L2 output exists, since griddap's axis
handling is more failure-prone than tabledap's.

**Minor, non-blocking**: the container logs a `MailConnectException` every
cycle (default report emails to a placeholder SMTP host from the image's
defaults) — cosmetic log noise, not a functional issue. Worth either
configuring real SMTP via `ERDDAP_email*` env vars or explicitly disabling
it at some point, just to quiet the logs.

### Security hardening (2026-08-23)

ERDDAP here is deliberately public and unauthenticated — that's correct for
its purpose (published glider data, same model as VOTO's ERDDAP), not a
gap. Hardening focused on what's actually appropriate given that: supply
chain and resource limits, not access control.

- **Image pinned to `axiom/docker-erddap:v2.30.0`**, not `:latest`. On a
  public-facing box, an unpinned tag means the next `docker compose pull`
  silently changes what's running — version bumps should be a deliberate,
  reviewed action instead.
- **Container memory limit added (`2500M`)** for the `erddap` service.
  Checked first, rather than assumed: pulled ERDDAP's own `setup.xml`
  request-throttling settings I'd planned to tune
  (`requestsPerMinute`, `partialRequestMaxBytes`, etc.) directly off the
  *live running container* to verify against the real file rather than
  trust search results — **none of those tags exist in ERDDAP 2.30.0's
  setup.xml at all**; that search result was simply wrong. Pivoted to what
  actually is real and available: without a memory limit, the JVM sizes
  its heap off the *host's* full RAM (confirmed in logs pre-fix:
  `Xmx ~= 3025 MB` on a ~3.8GB box) — almost nothing left for the OS,
  Caddy, or Docker itself, so one heavy public query risked the OOM killer
  taking down the whole instance, not just the container. With the limit
  set, `ERDDAP_MAX_RAM_PERCENTAGE=80` now computes off that 2.5GB ceiling
  instead of the host total (cgroup-aware JVM), leaving real headroom.
- **SSH security group scope not independently confirmed** — can't check
  from inside the guest (enforced at the network layer, not visible via
  SSH). Worth confirming in the NREC dashboard that whatever group covers
  port 22 is genuinely restricted, not `0.0.0.0/0` — matters more than
  usual here since the `ubuntu` user is in the `docker` group (root-
  equivalent via the Docker socket), so SSH key compromise = box compromise.
- Not done: Caddy-level rate limiting. Would require a custom Caddy build
  (the rate-limit module isn't in the stock `caddy:2-alpine` image) — a
  bigger lift than the memory cap above, and lower priority given the
  memory limit already bounds the worst case. Worth revisiting if actual
  abuse/scraping traffic becomes a real pattern once this is public.

## `ingest/` — L1/L2 → OGDB + ERDDAP pipeline (2026-08-25)

Python CLI: given a real L1/L2 NetCDF file, classifies it
(`inspect_netcdf.py`), registers its metadata in OGDB via the gateway API
(`gateway_client.py`), and SFTPs it to this server (`sftp_transfer.py`),
tied together by `ingest.py`. Dry-run by default, `--commit` to act —
matching OGDB's own backfill-script convention.

**Moved here from `ogdp`**, where it was originally built — consolidated
into this repo instead, since ERDDAP-facing tooling split across two
repos was confusing and this code is about getting finished output to a
specific downstream consumer (this server), not part of OGDP's own job
(processing raw glider data). Runs the same wherever it's actually
executed regardless of which repo it's checked out from — e.g. from
OGDP's own processing environment, which is where OGDB gateway access
already legitimately exists (see the reasoning below, carried over from
when this lived in `ogdp`).

Key decisions, carried over from the original build:
- **Gateway API, not raw Postgres** — `DatasetsService` in OGDB-portal
  owns real domain logic (DM/PUB supersession, DTO validation,
  version↔package integrity) that a direct-DB writer would have to
  reimplement and risk drifting from. Compare `tracks`, which OGDP does
  write to directly — that table has no such logic, this domain does.
- **Config from `ingest/config.json`** (gitignored, `config.example.json`
  committed as the template), not environment variables — flat, local to
  this directory, not reaching into `ogdp`'s own `config/app.json` (that
  cross-repo reach would itself have been exactly the kind of implicit
  coupling worth avoiding by moving this code at all).
- **`document_type = "<stage>_output"`** (`dm_output`/`pub_output`), not
  `l1_output`/`l2_output` — matches what the gateway's `findDetail()`
  already reads for the dashboard's "Internal download" indicator. A
  single DM/PUB run can produce both an L1 and L2 file; which is which
  lives in the stored `netcdf_metadata.level`, not the document type.
- **Register the OGDB document before the SFTP transfer**, not after — a
  failed transfer then leaves a visible gap (OGDB knows about a file
  that isn't live yet) rather than the opposite failure mode (file live
  on ERDDAP, OGDB never told), which is the actual failure mode this
  pipeline exists to prevent.
- **SFTP upload-to-temp-name + atomic `posix_rename`** — so ERDDAP's own
  reload cycle can run mid-transfer and see either nothing or the
  complete file, never a partial one.

Corresponding changes elsewhere, already made: OGDB migration
`xxxx_documents_netcdf_metadata` (adds `file_hash`/`file_size_bytes`/
`netcdf_metadata` to `documents`); OGDB-portal gateway endpoint
`POST /datasets/:missionId/documents`.

**Team access set up (2026-10-09):** the two credential gaps above are
closed, so any team member can now run `ingest.py` from their own machine
— see `ingest/README.md` for the actual steps. What changed:
- **No service account — each person logs in as themselves.** The gateway
  only ever supported human password login (role `editor`/`admin`); rather
  than build an API-key path, each team member's own portal login goes in
  their own `config.json`, so `erddap_pushes.changed_by` records exactly
  who pushed each file.
- **Gateway reachable via SSH tunnel.** `nrec_app`'s `docker-compose.yml`
  gateway service had no `ports:` entry at all — not even loopback, unlike
  postgres. Added `127.0.0.1:3001:3001` (same loopback-only pattern as
  postgres), so a team member tunnels through the `nrec_app` SSH access
  they already have: `ssh -N -L 3001:localhost:3001 nrec_app`.
- **Restricted SFTP account (`erddap-push`) on `nrec_erddap`.** SFTP-only
  (`ForceCommand internal-sftp`, no shell), `ChrootDirectory
  /data/ogdp/processed` — the chroot wall (that directory and its parents)
  is root-owned per OpenSSH's requirement; `L1/`/`L2/` inside it are owned
  by `erddap-push` so pushes can write/replace files. One shared keypair
  for the team (attribution comes from the gateway login above, not SSH).
  Because the chroot makes that directory the account's own `/`,
  `config.json`'s `remoteBasePath` must be `""`, not the host path — see
  the fix in `ingest/config.py`.

**Still a gap:** nothing on this server watches for a transferred file and
handles the dataset fragment/restart side — `ingest.py` only gets the file
and the OGDB record there. Related: `ingest.py`/`sftp_transfer.py` never
create remote directories, so a mission's first-ever push needs its
`L1/<slug>`/`L2/<slug>` folders created by hand first (documented as a
manual step in `ingest/README.md`, not yet scripted — see "Next steps" #5
below, which this folds into).

## Next steps

1. ~~Provision the NREC instance~~ — done (`m1.medium`, dualStack).
2. ~~Install Docker, set up Caddy reverse proxy~~ — done, see "Docker
   deployment" above.
3. **Create and attach the Cinder data volume, mount at `/data`** — blocks
   real data flowing in; needs the NREC dashboard (user-side).
4. Get one real OGDP-produced L0/L1/L2 file set for one mission — needed to
   both verify the variable names in `datasets_example.xml`/`datasets.d/`
   are exactly right, and to run ERDDAP's bundled `GenerateDatasetsXml.sh`
   against a real file (it auto-generates most of a `<dataset>` block;
   historically unreliable specifically on
   `cf_role`/`cdm_data_type`/`subsetVariables` for CF-DSG trajectory data —
   check those three by hand against the template every time).
5. Turn "one `<dataset>` block per mission × level" from a hand-editing
   problem into a scripting one — a small templated script (e.g. Jinja2)
   reading mission id / start date / platform serial straight out of OGDB's
   `missions` table, writing one fragment into `erddap-config/datasets.d/`
   per row, run as the last step after a mission's L0/L1/L2 files land.
   Natural home: alongside OGDP's other pipeline tooling.
6. Decide `datasetID` convention precisely (currently `l0_`/`l1_`/`l2_` +
   a `<platform_serial>_<start_date>` mission id) and keep it mechanically
   derived from OGDB's mission id, not invented per-file — same reasoning
   as OGDB's detail-table naming rule.
