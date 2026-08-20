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

## NREC VM (not yet provisioned)

Server: **University of Bergen / University of Oslo's own research cloud**
(NREC, OpenStack-based) — open-source-aligned, on-institution infrastructure,
consistent with the general preference for that over commercial cloud.

- **Flavor: `m1.large` (2 vCPU / 8 GB RAM / 20 GB root disk).** 2 GB
  (`m1.small`) ruled out as too tight for JVM+Tomcat+OS overhead at the
  scale of hundreds of datasets. RAM/flavor isn't a locked-in choice — NREC
  documents a "Resizing a Server" flavor-change flow (reboot required, no
  rebuild), so under-provisioning here is a cheap mistake to fix later.
  20 GB root matches NREC's own flavor default exactly — no need to second-
  guess it.
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
  future NREC instance in this project.
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

## Next steps (not started)

1. Provision the NREC instance per the spec above (`m1.large`, dualStack,
   20 GB root + separate data volume).
2. Install Java + Tomcat + ERDDAP `.war`, set up Caddy reverse proxy
   (80/443 → localhost:8080).
3. Get one real OGDP-produced L0/L1/L2 file set for one mission — needed to
   both verify the OG1 variable names in `datasets_example.xml` are exactly
   right, and to run ERDDAP's bundled `GenerateDatasetsXml.sh` against a
   real file (it auto-generates most of a `<dataset>` block; historically
   unreliable specifically on `cf_role`/`cdm_data_type`/`subsetVariables`
   for CF-DSG trajectory data — check those three by hand against the
   template every time).
4. Turn "one `<dataset>` block per mission × level" from a hand-editing
   problem into a scripting one — a small templated script (e.g. Jinja2)
   reading mission id / start date / platform serial straight out of OGDB's
   `missions` table, run as the last step after a mission's L0/L1/L2 files
   land. Natural home: alongside OGDP's other pipeline tooling.
5. Decide `datasetID` convention precisely (currently `l0_`/`l1_`/`l2_` +
   OG1's `<platform_serial>_<start_date>` mission id) and keep it
   mechanically derived from OGDB's mission id, not invented per-file — same
   reasoning as OGDB's detail-table naming rule.
