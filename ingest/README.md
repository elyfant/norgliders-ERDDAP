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

2. **Your own SFTP key** — everyone pushing has their own key for the
   restricted `erddap-push` account on `nrec_erddap`, so one person's key
   can be removed (laptop lost, someone leaves) without touching anyone
   else's. Make it on your own machine, replacing `<yourname>` (e.g.
   `ailin`):
   ```bash
   ssh-keygen -t rsa -b 4096 -N "" -C "erddap-push:<yourname>" -f ~/.ssh/erddap-push_rsa
   ```
   - **RSA**, because the upload code only loads RSA keys.
   - **No passphrase** (`-N ""`), because the script can't stop to ask for
     one. That's acceptable here: the file is readable only by you (check
     with `ls -l ~/.ssh/erddap-push_rsa`, it should show `-rw-------`), and
     the account it opens can do nothing but upload into one folder.
   - This makes two files. `~/.ssh/erddap-push_rsa` is your **private**
     key: it never leaves your machine — don't email or share it.
     `~/.ssh/erddap-push_rsa.pub` is the **public** key: send that one to
     whoever administers `nrec_erddap` (currently Fiona) so they can add it
     — see "Managing team keys" below. It's safe to send by email or Teams.
   - On Windows, the same command works in PowerShell (OpenSSH is built
     in); the files land in `C:\Users\<you>\.ssh\`.

   Once it's been added, test it — you should get an `sftp>` prompt and
   nothing else (no shell):
   ```bash
   sftp -i ~/.ssh/erddap-push_rsa erddap-push@158.39.77.95
   ```

3. **Your config file** — copy the template and fill it in:
   ```bash
   cp config.example.json config.json   # gitignored, never commit this
   ```
   - `serviceEmail` / `servicePassword` — **your own portal login**, the
     same email and password you use for the OGDB dashboard
     (ogdb.gfi.uib.no). Despite the name, this has nothing to do with the
     ERDDAP server: the script uses it to talk to OGDB on `nrec_app`
     (through the tunnel in step 4). No separate service account; the
     gateway only knows human logins, so pushes get attributed to whoever
     actually ran the script.
   - `sftpUser` — `erddap-push`, a restricted account that can only SFTP
     into one directory on `nrec_erddap`, nothing else.
   - `sftpKeyPath` — full path to **your own private** key from step 2,
     e.g. `/home/<you>/.ssh/erddap-push_rsa` — not the `.pub` file, and not
     the key you use to SSH into the VMs as `ubuntu` (that admin key isn't
     accepted by `erddap-push`, and shouldn't be used by a script anyway).
   - `remoteBasePath` — leave as `""`. The `erddap-push` account is
     chroot'd on the server, so its own `/` already is the right starting
     point — don't point this at a host path like `/data/ogdp/processed`.
   - `projectsRoot` — wherever `/Data/gfi/projects` is mounted on *your*
     machine (differs on Linux vs. a mapped Windows/Mac drive).

   `config.json` now holds your portal password, so make it readable only
   by you:
   ```bash
   chmod 600 config.json
   ```

4. **Gateway access** — the gateway API only listens on `nrec_app`'s own
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

## Pushing every mission at once

`batch_push.py` runs the same steps for every mission's best L1 and L2 file
in OGDB, using each mission's `std_mission_name` as its ERDDAP name:

```bash
.venv/bin/python batch_push.py              # dry run: checks every file, shows what would be pushed
.venv/bin/python batch_push.py --commit     # push, and record each one live in OGDB
```

- Files already live on ERDDAP **and** still the best file are skipped, so
  re-running it later only pushes what's new or better (e.g. after
  reprocessing). `--force` pushes them again anyway.
- Missions whose L1 is unusable (on the reprocessing list) have no L1 in
  OGDB, so only their L2 is pushed.
- One file failing doesn't stop the rest; the summary at the end lists
  every failure, and the exit code is 1 if there were any.
- `--mission N` (repeatable) and `--level L1|L2` narrow it down;
  `--verbose` prints each file's full check and plan.

## Making the data visible on ERDDAP

A pushed file only appears on ERDDAP once ERDDAP has a **dataset
description** for it: one small XML file per mission and level in
`erddap-config/datasets.d/`, which ERDDAP joins into its config when its
container starts. `make_datasets.py` writes them for everything that's live:

```bash
.venv/bin/python make_datasets.py --check   # what would change
.venv/bin/python make_datasets.py           # write/refresh erddap-config/datasets.d/*.xml
```

- Dataset id = `<mission name>_l1` / `_l2` (e.g. `sg559_naco_lofoten_jul2012_l2`)
  — part of the public URL, so **never rename a published one**.
- Each dataset lists exactly the variables in its own file (missions carry
  different sensors), with the facility's metadata instead of the
  instrument maker's.
- A file that would break ERDDAP is skipped with a reason (e.g. corrupted
  time values).
- It needs the same `config.json` and portal tunnel as the push scripts.

Then commit the changed files, and on `nrec_erddap` (as `ubuntu`):
```bash
cd ~/norgliders-ERDDAP && git pull && rsync -a --delete erddap-config/datasets.d/ /data/erddap/datasets.d/ && docker compose restart erddap
```
`rsync --delete` makes the server's `datasets.d` an exact copy of the
repo's, so a dataset removed from the repo also disappears from ERDDAP.
After a restart ERDDAP first *defers* datasets it hasn't indexed yet
("deferring loading this dataset until after loading other datasets")
and loads them in a second pass -- that message is not a failure.
ERDDAP takes a few minutes to load everything after a restart; its status
page is http://158.39.77.95/erddap/status.html.

Once it has loaded, store each dataset's public URL in OGDB -- that fills the
portal's "NorGliders ERDDAP L1/L2" links:
```bash
.venv/bin/python make_datasets.py --set-urls
```
It checks every URL actually answers before storing it, and only changes
those links. Once stored, a dataset's id is taken from its stored URL, so it
stays the same even if the mission's name is later corrected in OGDB.

## First push for a brand-new mission

Nothing extra to do: on a mission's first push, `ingest.py` creates its
`L1/<mission_slug>` or `L2/<mission_slug>` folder on the server before
uploading (the dry-run plan says so, and the real run prints
`created folder on ERDDAP: ...`). The restricted `erddap-push` account owns
`L1/` and `L2/`, so it can create mission folders inside them — but
nothing else.

## Troubleshooting

- **"no such file on this machine"** — check `projectsRoot` matches where
  the share is actually mounted on your machine.
- **Gateway connection refused** — the SSH tunnel (step 4 above) isn't
  running, or died. Re-run the `ssh -N -L ...` command.
- **SFTP "Permission denied (publickey)"** — your public key hasn't been
  added on the server yet, or `sftpKeyPath` points at the wrong file (the
  `.pub` file, or your `ubuntu` admin key, instead of
  `~/.ssh/erddap-push_rsa`).
- **SFTP "Permission denied" creating a folder** — the level or name in
  the command is wrong (the account can only create folders inside `L1/`
  and `L2/`); check the command.

## Managing team keys (for whoever administers `nrec_erddap`)

The `erddap-push` account accepts any key listed in
`/home/erddap-push/.ssh/authorized_keys` on `nrec_erddap` — one line per
person, each ending in the label they chose (`erddap-push:<name>`). These
commands run from your own machine, using your **admin** SSH login
(`nrec_erddap`, as `ubuntu`), not the restricted account.

**Add someone** — they send you their `erddap-push_rsa.pub`; save it, then:
```bash
ssh nrec_erddap "sudo tee -a /home/erddap-push/.ssh/authorized_keys > /dev/null" < /path/to/their/erddap-push_rsa.pub
```
`-a` appends, so nobody else's key is touched. Check their line is a
single line starting with `ssh-rsa` and ending in `erddap-push:<name>`
before adding — never add a private key (it starts with
`-----BEGIN`).

**See who has access:**
```bash
ssh nrec_erddap "sudo awk '{print \$NF}' /home/erddap-push/.ssh/authorized_keys"
```

**Remove someone** (lost laptop, left the team) — delete their line by
its label, then put the file's ownership and permissions back (sshd
refuses an `authorized_keys` that others can write to):
```bash
ssh nrec_erddap "sudo sed -i '/ erddap-push:<name>\$/d' /home/erddap-push/.ssh/authorized_keys && sudo chown erddap-push:erddap-push /home/erddap-push/.ssh/authorized_keys && sudo chmod 600 /home/erddap-push/.ssh/authorized_keys"
```
Removal takes effect for new connections immediately; nothing to restart.

Their portal login is separate: removing their key stops them uploading,
but to stop them recording pushes in OGDB as well, also disable their
portal account.

The original shared key (label `erddap-push@norgliders`, created
2026-10-09) was lost before anyone used it — if it's still listed, remove
it the same way, with `erddap-push@norgliders` in place of
`erddap-push:<name>`.
