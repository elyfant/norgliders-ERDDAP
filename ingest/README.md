# Pushing files to ERDDAP

`ingest.py` pushes a mission's best L1 or L2 NetCDF file to the public ERDDAP
server, and tells OGDB exactly which file is now live there. See the
docstring at the top of `ingest.py` for the full step-by-step; this is the
one-time setup plus the day-to-day commands.

Runs from **your own machine** (laptop or lab workstation) — wherever the
shared `/Data/gfi/projects` drive is mounted — not from `nrec_app` or
`nrec_erddap` themselves. Those two VMs don't share a filesystem with each
other or with your machine; this script is the thing that bridges them.

## One-time setup

1. **Python environment** (once per machine):
   ```bash
   cd norgliders-ERDDAP/ingest
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   ```

2. **Your config file** — copy the template and fill it in:
   ```bash
   cp config.example.json config.json   # gitignored, never commit this
   ```
   - `serviceEmail` / `servicePassword` — **your own portal login**, the
     same one you use for the OGDB dashboard. No separate service account;
     the gateway only knows human logins, so pushes get attributed to
     whoever actually ran the script.
   - `sftpUser` — `erddap-push`, a restricted account that can only SFTP
     into one directory on `nrec_erddap`, nothing else.
   - `sftpKeyPath` — path to the `erddap-push` private key. Ask Fiona for
     it (shared across the team; it's not personal like your portal
     password).
   - `remoteBasePath` — leave as `""`. The `erddap-push` account is
     chroot'd on the server, so its own `/` already is the right starting
     point — don't point this at a host path like `/data/ogdp/processed`.
   - `projectsRoot` — wherever `/Data/gfi/projects` is mounted on *your*
     machine (differs on Linux vs. a mapped Windows/Mac drive).

3. **Gateway access** — the gateway API only listens on `nrec_app`'s own
   loopback, not the network, so you reach it through an SSH tunnel using
   the same key you already use to SSH into `nrec_app`:
   ```bash
   ssh -N -L 3001:localhost:3001 nrec_app &
   ```
   Leave this running in a terminal (or background it) whenever you're
   pushing. `config.json`'s `gatewayUrl` should be `http://localhost:3001`
   — pick a different local port if something else on your machine is
   already using 3001 (e.g. a local dev gateway).

## Pushing a file

```bash
.venv/bin/python ingest.py <mission_number> <L1|L2> <mission_slug>
```

Always dry-runs first — it prints the plan (which file, what it'll register
in OGDB, where it'll upload to) without touching anything. Once it looks
right:

```bash
.venv/bin/python ingest.py <mission_number> <L1|L2> <mission_slug> --commit --confirm-live
```

- `--commit` actually registers the file in OGDB and transfers it.
- `--confirm-live` additionally records in OGDB that this file is the one
  now live on ERDDAP, linked to the processing run it came from. Always
  pass both together in normal use — `--commit` alone leaves OGDB knowing
  a file exists without saying it's actually live.

Re-run the same command any time a better file exists for a mission (e.g.
after reprocessing) — it replaces whatever was there.

## First push for a brand-new mission

`ingest.py` doesn't create directories on the server — only uploads into
ones that already exist. For a mission that's never been pushed before,
create its folders first with a plain `sftp` client (same restricted
account, so this is safe):

```bash
sftp -i /path/to/erddap-push_rsa erddap-push@158.39.77.95
sftp> mkdir L1/<mission_slug>
sftp> mkdir L2/<mission_slug>
```

(This is a known gap, not a workaround — see `design-notes.md`'s "Next
steps" for turning per-mission ERDDAP setup into a scripted step instead of
a manual one.)

## Troubleshooting

- **"no such file on this machine"** — check `projectsRoot` matches where
  the share is actually mounted on your machine.
- **Gateway connection refused** — the SSH tunnel (step 3 above) isn't
  running, or died. Re-run the `ssh -N -L ...` command.
- **SFTP "no such file" on upload** — the mission's `L1/<slug>` or
  `L2/<slug>` folder doesn't exist yet on the server; see the section
  above.
