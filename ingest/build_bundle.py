"""Build the mission data bundles (Zarr) behind the portal's mission data page,
and upload them to the portal.

    python ingest/build_bundle.py                      # dry run: which missions need a new bundle, and why
    python ingest/build_bundle.py --commit             # build those and upload them
    python ingest/build_bundle.py --mission 23 --commit --force   # rebuild + upload one, even if current
    python ingest/build_bundle.py --local --mission 23 # just build locally to inspect, no portal check/upload

Spec: OGDB-portal/docs/mission-data-bundle.md -- the contract with the UI.
This script implements it; change the spec first, then this.

For each mission it reads **the L2 file live on ERDDAP** (OGDB knows which:
the dataset detail's erddapL2File, relative to projectsRoot) from /Data on
this machine, and writes to <bundleDir>/<NNN>-<mission name>/:

    public.zarr/   coordinates, public variables, statistics, MLD, thermocline
    staff.zarr/    raw + engineering variables, QC availability (staff only)
    bundle.zip     both stores, the upload body for the portal (spec 8.4)

A mission needs a new bundle when the portal has none, or has one built
from a different L2 file than the one now live on ERDDAP (e.g. after
reprocessing + batch_push.py). So the routine is: batch_push.py --commit,
then build_bundle.py --commit. Up-to-date missions are skipped; --force
rebuilds them anyway (e.g. after a builder change). Like the push scripts:
dry run by default, one mission failing doesn't stop the rest, exit code 1
if anything failed. Needs ingest/config.json and the SSH tunnel to the
portal API (see README.md); uploading needs an editor/admin login.

What happens to the data (spec sections 3-6):
- profile x 5 m level grid: mean of the source bins in each 5 m bin (older
  001-011 files are already 5 m). Missing stays missing, no gap filling.
- standard variable names, mapped from each file's own (STANDARD below).
- TEOS-10 (gsw) absolute salinity, conservative temperature, sigma0 and,
  if the file has none, sound speed -- computed the same way for every
  mission, from the gridded T, S, p.
- int16 with scale_factor/add_offset/_FillValue for profile x level arrays.
- cast direction (0 down / 1 up) from the up-and-down record order, checked
  against the files' own dive pairing; -1 if the check fails.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import gsw
import numpy as np
import zarr
from netCDF4 import Dataset
from zarr.codecs import GzipCodec

from config import load_config
from gateway_client import GatewayClient, GatewayError

BUNDLE_FORMAT_VERSION = 1
BUILDER_VERSION = "0.1.0"
BIN_M = 5.0
GZIP = GzipCodec(level=6)
FILL16 = -32768

# Standard name -> (group, units, long_name, source names tried in order).
# Public profile x level variables (spec 4.1). Computed ones are added below.
STANDARD = {
    "temperature": ("Core CTD", "degree_C", "Sea water temperature", ["temperature", "temp"]),
    "salinity": ("Core CTD", "1e-3", "Sea water practical salinity", ["salinity"]),
    "pressure": ("Core CTD", "dbar", "Sea water pressure", ["pressure"]),
    "conductivity": ("Core CTD", "S m-1", "Sea water electrical conductivity", ["conductivity"]),
    "sound_velocity": ("Core CTD", "m s-1", "Speed of sound in sea water", ["sound_velocity", "soundvel"]),
    "oxygen": ("Oxygen", "umol kg-1", "Dissolved oxygen",
               ["aanderaa4330_dissolved_oxygen", "aanderaa4831_dissolved_oxygen", "contopt_dissolved_oxygen"]),
    "oxygen_saturation_concentration": ("Oxygen", "umol kg-1", "Oxygen saturation concentration", ["dissolved_oxygen_sat"]),
    "chlorophyll": ("Optics", "ug L-1", "Chlorophyll fluorescence", ["wlbb2fl_sig695nm_adjusted"]),
    "backscatter_700": ("Optics", "m-1 sr-1", "Optical backscatter, 700 nm", ["wlbb2fl_sig700nm_adjusted"]),
    "backscatter_470": ("Optics", "m-1 sr-1", "Optical backscatter, 470 nm", ["wlbb2fl_sig470nm_adjusted"]),
}
COMPUTED = {
    "absolute_salinity": ("Core CTD", "g kg-1", "Absolute salinity (TEOS-10)"),
    "conservative_temperature": ("Core CTD", "degree_C", "Conservative temperature (TEOS-10)"),
    "sigma0": ("Core CTD", "kg m-3", "Potential density anomaly, 0 dbar reference (TEOS-10)"),
    "oxygen_saturation": ("Oxygen", "percent", "Oxygen saturation"),
}
# Staff profile x level variables (spec 4.2): standard name -> source names.
STAFF = {
    "temperature_raw": ["temperature_raw"],
    "salinity_raw": ["salinity_raw"],
    "conductivity_raw": ["conductivity_raw"],
    "source_sigma_theta": ["sigma_theta"],
    "source_sigma_t": ["sigma_t"],
    "source_theta": ["theta"],
    "source_density": ["density"],
    "glider_speed": ["speed"],
    "glider_vertical_speed": ["vert_speed"],
    "glider_horizontal_speed": ["horz_speed"],
}


class BuildError(RuntimeError):
    pass


# --------------------------------------------------------------- reading


def read_l2(path: str) -> dict:
    """The L2 file -> per-profile coordinates and per-variable 2-D arrays on
    the source depth bins, independent of the file layout."""
    with Dataset(path) as ds:
        layout = "L2_new" if "profile" in ds.dimensions else "L2_old"
        pdim = "profile" if layout == "L2_new" else "time"
        if "depth" not in ds.variables:
            raise BuildError("no depth coordinate")

        def one(name):
            return np.ma.filled(ds[name][:].astype("float64"), np.nan) if name in ds.variables else None

        def two(name):
            var = ds.variables.get(name)
            if var is None or var.dimensions != (pdim, "depth") or var.dtype.kind not in "fiu":
                return None
            return np.ma.filled(var[:].astype("float32"), np.nan)

        time, lat, lon = one("time"), one("latitude"), one("longitude")
        if time is None or lat is None or lon is None:
            raise BuildError("missing time/latitude/longitude")
        n = len(time)
        dive = one("dive_number")
        u = one("depth_avg_curr_east") if "depth_avg_curr_east" in ds.variables else one("u_da")
        v = one("depth_avg_curr_north") if "depth_avg_curr_north" in ds.variables else one("v_da")
        up_and_down = "up and down" in str(getattr(ds, "file_data_type", "")).lower()

        fields = {}
        for name, var in ds.variables.items():
            arr = two(name)
            if arr is not None:
                fields[name] = (arr, str(getattr(var, "units", "")))
        return {
            "layout": layout, "n": n, "depth": one("depth"), "time": time, "lat": lat, "lon": lon,
            "dive": dive, "u": u, "v": v, "up_and_down": up_and_down, "fields": fields,
        }


def cast_and_dive(src: dict) -> tuple[np.ndarray, np.ndarray, str]:
    """(cast int8, dive int32, note). Up-and-down files alternate down/up
    records (even index = down). Verified against the file's own pairing:
    equal dive_number (newer layout) or equal depth-averaged current (older)
    within each pair; if more than 1 % of pairs disagree -> cast = -1."""
    n = src["n"]
    if not src["up_and_down"]:
        dive = src["dive"] if src["dive"] is not None else np.arange(1, n + 1)
        return np.full(n, -1, "int8"), np.nan_to_num(dive, nan=-1).astype("int32"), "not an up-and-down file"
    pairs = n // 2
    key = src["dive"] if src["dive"] is not None else src["u"]
    if key is None:
        ok = False
    else:
        a, b = key[0:2 * pairs:2], key[1:2 * pairs:2]
        mism = ~((a == b) | (np.isnan(a) & np.isnan(b)) | np.isclose(a, b, equal_nan=True))
        ok = pairs > 0 and mism.mean() <= 0.01
    cast = (np.arange(n) % 2).astype("int8") if ok else np.full(n, -1, "int8")
    if src["dive"] is not None:
        dive = np.nan_to_num(src["dive"], nan=-1).astype("int32")
    else:
        dive = (np.arange(n) // 2 + 1).astype("int32")  # older layout: dives counted from the file
    return cast, dive, "pairing verified" if ok else "pairing NOT verified -- cast set to -1"


def distance_km(lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
    """Cumulative great-circle distance along the profile positions."""
    out = np.full(len(lat), np.nan, "float32")
    total, last = 0.0, None
    for i, (la, lo) in enumerate(zip(lat, lon)):
        if not (np.isfinite(la) and np.isfinite(lo)):
            continue
        if last is not None:
            p1, p2 = np.radians(last[0]), np.radians(la)
            dl = np.radians(lo - last[1])
            h = np.sin((p2 - p1) / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
            total += 2 * 6371.0088 * np.arcsin(np.sqrt(min(1.0, h)))
        out[i] = total
        last = (la, lo)
    return out


# --------------------------------------------------------------- gridding


def bin_index(depth: np.ndarray) -> np.ndarray:
    return np.floor(np.where(np.isfinite(depth), depth, -1) / BIN_M).astype(int)


def regrid(arr: np.ndarray, idx: np.ndarray, nlev: int) -> np.ndarray:
    """Mean of the source bins falling in each 5 m bin; NaN where none valid."""
    out = np.full((arr.shape[0], nlev), np.nan, "float32")
    for k in range(nlev):
        cols = np.where(idx == k)[0]
        if cols.size == 0:
            continue
        block = arr[:, cols]
        cnt = np.isfinite(block).sum(axis=1)
        with np.errstate(invalid="ignore"):
            out[:, k] = np.where(cnt > 0, np.nansum(block, axis=1) / np.maximum(cnt, 1), np.nan)
    return out


# --------------------------------------------------------------- products


def mixed_layer_depth(sigma0: np.ndarray, depth: np.ndarray) -> np.ndarray:
    """sigma0 threshold 0.03 kg/m3 relative to the 10 m bin (de Boyer Montegut et al. 2004)."""
    ref_k = int(np.argmax(depth >= 10.0))
    out = np.full(sigma0.shape[0], np.nan, "float32")
    for p in range(sigma0.shape[0]):
        ref = sigma0[p, ref_k]
        if not np.isfinite(ref):
            continue
        below = np.where(np.isfinite(sigma0[p, ref_k:]) & (sigma0[p, ref_k:] - ref >= 0.03))[0]
        if below.size:
            out[p] = depth[ref_k + below[0]]
    return out


def thermocline_depth(temp: np.ndarray, depth: np.ndarray, mld: np.ndarray) -> np.ndarray:
    """Depth of maximum -dT/dz below the mixed layer, after a 3-bin running mean."""
    out = np.full(temp.shape[0], np.nan, "float32")
    k = np.ones(3) / 3
    for p in range(temp.shape[0]):
        t = temp[p]
        ok = np.isfinite(t)
        if ok.sum() < 5:
            continue
        ts = np.convolve(np.where(ok, t, np.nan), k, mode="same")
        grad = -np.diff(ts) / np.diff(depth)
        zmid = (depth[:-1] + depth[1:]) / 2
        valid = np.isfinite(grad) & (zmid > (mld[p] if np.isfinite(mld[p]) else 0))
        if valid.any():
            out[p] = zmid[np.where(valid)[0][np.nanargmax(grad[valid])]]
    return out


def stats(arr: np.ndarray) -> dict[str, np.ndarray]:
    with np.errstate(invalid="ignore"), np.testing.suppress_warnings() as sup:
        sup.filter(RuntimeWarning)
        return {
            "mean": np.nanmean(arr, axis=0).astype("float32"),
            "p05": np.nanpercentile(arr, 5, axis=0).astype("float32"),
            "p95": np.nanpercentile(arr, 95, axis=0).astype("float32"),
            "min": np.nanmin(arr, axis=0).astype("float32"),
            "max": np.nanmax(arr, axis=0).astype("float32"),
            "count": np.isfinite(arr).sum(axis=0).astype("int32"),
        }


# --------------------------------------------------------------- writing


def pack16(arr: np.ndarray) -> tuple[np.ndarray, float, float]:
    """float -> int16 with CF scale_factor/add_offset; FILL16 = missing."""
    finite = arr[np.isfinite(arr)]
    if finite.size == 0:
        return np.full(arr.shape, FILL16, "int16"), 1.0, 0.0
    lo, hi = float(finite.min()), float(finite.max())
    margin = 0.02 * (hi - lo) or 1e-6
    lo, hi = lo - margin, hi + margin
    scale = (hi - lo) / 65534.0
    offset = (hi + lo) / 2.0
    q = np.round((arr - offset) / scale)
    q = np.where(np.isfinite(arr), np.clip(q, -32767, 32767), FILL16).astype("int16")
    return q, scale, offset


def put(group, name: str, data: np.ndarray, dims: list[str], attrs: dict | None = None, packed: bool = False):
    attrs = dict(attrs or {})
    if packed:
        data, scale, offset = pack16(data.astype("float64"))
        attrs.update({"scale_factor": scale, "add_offset": offset, "_FillValue": FILL16})
        fill = FILL16
    else:
        kind = data.dtype.kind  # missing-value marker per type; availability is uint8, sensor names are strings
        fill = np.nan if kind == "f" else -1 if kind == "i" else np.iinfo(data.dtype).max if kind == "u" else ""
    parts = name.split("/")
    for p in parts[:-1]:
        group = group.require_group(p)
    group.create_array(parts[-1], data=data, chunks=data.shape, compressors=GZIP,
                       dimension_names=dims, fill_value=fill, attributes=attrs)


def valid_range(arr: np.ndarray) -> tuple[float | None, float | None]:
    f = arr[np.isfinite(arr)]
    return (round(float(f.min()), 6), round(float(f.max()), 6)) if f.size else (None, None)


def write_coords(g, c: dict):
    put(g, "time", c["time"].astype("float64"), ["profile"],
        {"units": "seconds since 1970-01-01T00:00:00Z", "standard_name": "time"})
    put(g, "latitude", c["lat"].astype("float32"), ["profile"], {"units": "degrees_north"})
    put(g, "longitude", c["lon"].astype("float32"), ["profile"], {"units": "degrees_east"})
    put(g, "dive", c["dive"], ["profile"], {"long_name": "Dive number"})
    put(g, "cast", c["cast"], ["profile"], {"flag_values": [-1, 0, 1], "flag_meanings": "unknown descent ascent"})
    put(g, "distance_km", c["dist"], ["profile"], {"units": "km", "long_name": "Distance along track"})
    put(g, "depth", c["depth"], ["level"], {"units": "m", "positive": "down", "long_name": "Depth bin centre"})
    put(g, "depth_bounds", c["bounds"], ["level", "bnds"], {"units": "m"})


# --------------------------------------------------------------- one mission


def build_mission(m: dict, live: dict, projects_root: str, out_root: Path) -> dict:
    rel = live.get("erddapL2File")
    if not rel:
        raise BuildError("no L2 live on ERDDAP")
    path = os.path.join(projects_root, rel)
    if not os.path.isfile(path):
        raise BuildError(f"no such file on this machine: {path}")
    src = read_l2(path)

    # --- grid
    idx = bin_index(src["depth"])
    key = next((s for s in ("temperature", "temp") if s in src["fields"]), None)
    if key is None:
        raise BuildError("no temperature variable")
    has = np.isfinite(src["fields"][key][0]).any(axis=0)
    deepest = idx[has].max() if has.any() else -1
    if deepest < 0:
        raise BuildError("no valid temperature at any depth")
    nlev = int(deepest) + 1
    depth = ((np.arange(nlev) + 0.5) * BIN_M).astype("float32")
    bounds = np.stack([np.arange(nlev) * BIN_M, (np.arange(nlev) + 1) * BIN_M], axis=1).astype("float32")
    cast, dive, cast_note = cast_and_dive(src)
    coords = {"time": src["time"], "lat": src["lat"], "lon": src["lon"], "dive": dive, "cast": cast,
              "dist": distance_km(src["lat"], src["lon"]), "depth": depth, "bounds": bounds}

    def gridded(source_names):
        for s in source_names:
            if s in src["fields"]:
                return regrid(src["fields"][s][0], idx, nlev), s, src["fields"][s][1]
        return None, None, None

    public, staff, catalogue = {}, {}, []
    for std, (group, units, long_name, names) in STANDARD.items():
        arr, s, s_units = gridded(names)
        if arr is not None and np.isfinite(arr).any():
            public[std] = (arr, {"units": units, "long_name": long_name, "group": group,
                                 "source_variable": s, "source_units": s_units, "computed": False})

    # --- TEOS-10, computed the same way for every mission
    if "temperature" in public and "salinity" in public:
        t, sp = public["temperature"][0], public["salinity"][0]
        lat2 = np.broadcast_to(src["lat"][:, None], t.shape)
        lon2 = np.broadcast_to(src["lon"][:, None], t.shape)
        p = public["pressure"][0] if "pressure" in public else gsw.p_from_z(-np.broadcast_to(depth, t.shape), lat2)
        sa = gsw.SA_from_SP(sp, p, lon2, lat2)
        ct = gsw.CT_from_t(sa, t, p)
        derived = {"absolute_salinity": sa, "conservative_temperature": ct, "sigma0": gsw.sigma0(sa, ct)}
        if "sound_velocity" not in public:
            public["sound_velocity"] = (gsw.sound_speed(sa, ct, p).astype("float32"),
                                        {"units": "m s-1", "long_name": "Speed of sound in sea water (TEOS-10)",
                                         "group": "Core CTD", "source_variable": None, "computed": True})
        for std, arr in derived.items():
            group, units, long_name = COMPUTED[std]
            public[std] = (arr.astype("float32"), {"units": units, "long_name": long_name, "group": group,
                                                   "source_variable": None, "computed": True,
                                                   "method": "gsw (TEOS-10) from gridded temperature, salinity, pressure"})
    if "oxygen" in public and "oxygen_saturation_concentration" in public:
        with np.errstate(invalid="ignore", divide="ignore"):
            sat = 100.0 * public["oxygen"][0] / public["oxygen_saturation_concentration"][0]
        group, units, long_name = COMPUTED["oxygen_saturation"]
        public["oxygen_saturation"] = (sat.astype("float32"), {"units": units, "long_name": long_name, "group": group,
                                                               "source_variable": None, "computed": True})

    for std, names in STAFF.items():
        arr, s, s_units = gridded(names)
        if arr is not None and np.isfinite(arr).any():
            staff[std] = (arr, {"source_variable": s, "source_units": s_units, "group": "Raw / engineering"})
    for name in sorted(src["fields"]):
        if name.startswith("eng_"):
            arr = regrid(src["fields"][name][0], idx, nlev)
            if np.isfinite(arr).any():
                staff[f"eng/{name[4:]}"] = (arr, {"source_variable": name, "source_units": src["fields"][name][1],
                                                  "group": "Raw / engineering"})

    # --- products
    mld = mixed_layer_depth(public["sigma0"][0], depth) if "sigma0" in public else None
    thermo = thermocline_depth(public["temperature"][0], depth, mld if mld is not None else np.full(src["n"], np.nan))

    # --- write
    name = m["stdMissionName"].lower()
    out = out_root / f"{m['missionNumber']:03d}-{name}"
    tmp = out.with_name(out.name + ".building")
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    file_sha = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    root_attrs = {
        "bundle_format_version": BUNDLE_FORMAT_VERSION, "builder_version": BUILDER_VERSION,
        "mission_number": m["missionNumber"], "mission_name": name, "ogdb_mission_id": m["id"],
        "source": {"stage": live.get("erddapL2Status"), "l2_file": rel, "file_layout": src["layout"],
                   "file_sha256": file_sha, "file_mtime": datetime.fromtimestamp(os.path.getmtime(path), timezone.utc).isoformat(),
                   "cast_direction": cast_note},
        "erddap_l2_url": live.get("erddapL2Url"),
        "grid": {"bin_m": BIN_M, "n_profile": src["n"], "n_level": nlev},
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "licence": "CC BY 4.0", "citation": "University of Bergen Ocean Glider Facility",
    }

    pub = zarr.open_group(tmp / "public.zarr", mode="w", zarr_format=3)
    write_coords(pub, coords)
    pub_vars = []
    for std, (arr, a) in public.items():
        put(pub, std, arr, ["profile", "level"], {k: v for k, v in a.items() if v is not None}, packed=True)
        lo, hi = valid_range(arr)
        pub_vars.append({"name": std, "group": a["group"], "units": a["units"], "long_name": a["long_name"],
                         "valid_min": lo, "valid_max": hi, "source_variable": a.get("source_variable"),
                         "computed": a.get("computed", False)})
        for stat, values in stats(arr).items():
            put(pub, f"stats/{std}/{stat}", values, ["level"], {"units": a["units"]} if stat != "count" else {})
    if "dac_east" not in public and src["u"] is not None:
        put(pub, "dac_east", src["u"].astype("float32"), ["profile"], {"units": "m s-1", "long_name": "Depth-averaged current, eastward", "group": "Currents"})
        put(pub, "dac_north", src["v"].astype("float32"), ["profile"], {"units": "m s-1", "long_name": "Depth-averaged current, northward", "group": "Currents"})
        pub_vars.append({"name": "dac_east", "group": "Currents", "units": "m s-1", "long_name": "Depth-averaged current, eastward", "per_profile": True})
        pub_vars.append({"name": "dac_north", "group": "Currents", "units": "m s-1", "long_name": "Depth-averaged current, northward", "per_profile": True})
    if mld is not None:
        put(pub, "mld", mld, ["profile"], {"units": "m", "long_name": "Mixed layer depth",
                                           "method": "sigma0 threshold 0.03 kg m-3 relative to the 10 m bin (de Boyer Montegut et al. 2004)"})
    put(pub, "thermocline_depth", thermo, ["profile"], {"units": "m", "long_name": "Thermocline depth",
                                                        "method": "max -dT/dz below the mixed layer, 3-bin running mean"})
    pub.attrs.update({**root_attrs, "variables": pub_vars})

    stf = zarr.open_group(tmp / "staff.zarr", mode="w", zarr_format=3)
    write_coords(stf, coords)
    staff_vars = []
    for std, (arr, a) in staff.items():
        put(stf, std, arr, ["profile", "level"], {k: v for k, v in a.items() if v is not None}, packed=True)
        lo, hi = valid_range(arr)
        staff_vars.append({"name": std, "group": a["group"], "units": a.get("source_units"),
                           "valid_min": lo, "valid_max": hi, "source_variable": a["source_variable"]})
    # availability: % valid bins per variable per dive (both casts)
    all_vars = {**{k: v[0] for k, v in public.items()}, **{k: v[0] for k, v in staff.items()}}
    dives = np.unique(dive[dive >= 0])
    avail = np.zeros((len(all_vars), len(dives)), "uint8")
    for i, arr in enumerate(all_vars.values()):
        for j, d in enumerate(dives):
            block = arr[dive == d]
            avail[i, j] = int(round(100 * np.isfinite(block).mean())) if block.size else 0
    put(stf, "availability", avail, ["sensor", "dive_index"], {"units": "percent", "long_name": "Valid bins per dive"})
    put(stf, "sensor", np.array(list(all_vars), dtype="U64"), ["sensor"])
    put(stf, "dive_index", dives.astype("int32"), ["dive_index"])
    stf.attrs.update({**root_attrs, "variables": staff_vars})

    # --- zip (the upload body, spec 8.4), then swap into place
    with zipfile.ZipFile(tmp / "bundle.zip", "w", zipfile.ZIP_STORED) as z:  # chunks are already gzip-compressed
        for store in ("public.zarr", "staff.zarr"):
            for f in sorted((tmp / store).rglob("*")):
                if f.is_file():
                    z.write(f, f.relative_to(tmp).as_posix())
    if out.exists():
        shutil.rmtree(out)
    tmp.rename(out)

    size = lambda p: sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
    return {"dir": out, "public_mb": size(out / "public.zarr") / 1e6, "staff_mb": size(out / "staff.zarr") / 1e6,
            "n_public": len(pub_vars), "n_staff": len(staff_vars), "n_profile": src["n"], "n_level": nlev,
            "layout": src["layout"], "cast": cast_note}


def plan(client: GatewayClient, m: dict, live: dict, force: bool) -> str | None:
    """Why this mission needs a new bundle, or None if the portal's is current."""
    if force:
        return "forced"
    summary = client.get_bundle_summary(m["id"])
    if not summary.get("available"):
        return "no bundle in the portal yet"
    if summary.get("sourceL2File") != live.get("erddapL2File"):
        return f"portal's bundle is from {summary.get('sourceL2File')}, ERDDAP now has {live.get('erddapL2File')}"
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mission", type=int, action="append", metavar="NUMBER", help="only this mission (repeatable)")
    parser.add_argument("--commit", action="store_true", help="build and upload. Default is a dry run.")
    parser.add_argument("--force", action="store_true", help="also rebuild missions whose bundle is current")
    parser.add_argument("--local", action="store_true", help="build locally only: no portal check, no upload")
    parser.add_argument("--out", help="output folder (default: bundleDir in config.json, else ~/ogdb-bundles)")
    args = parser.parse_args()
    if args.local and args.commit:
        parser.error("--local and --commit don't go together")
    try:
        config = load_config()
    except (FileNotFoundError, ValueError) as e:
        sys.exit(f"ERROR: {e}")
    out_root = Path(args.out or config.get("bundle_dir") or "~/ogdb-bundles").expanduser()
    client = GatewayClient(base_url=config["gateway_url"])
    try:
        client.login(config["service_email"], config["service_password"])
        missions = client.list_missions()
    except GatewayError as e:
        sys.exit(f"ERROR: {e}")

    wanted = set(args.mission or [])
    done, current, failed = [], [], []
    for m in sorted(missions, key=lambda m: m["missionNumber"]):
        if (wanted and m["missionNumber"] not in wanted) or not m.get("bestL2File") or not m.get("stdMissionName"):
            continue
        label = f"{m['missionNumber']:03d} {m['stdMissionName'].lower()}"
        try:
            live = client.get_dataset_detail(m["id"])
            if not live.get("erddapL2File"):
                continue  # nothing live on ERDDAP -> no bundle (spec 9)
            reason = "local build" if args.local else plan(client, m, live, args.force)
            if reason is None:
                current.append(label)
                print(f"{label}: up to date")
                continue
            if not (args.local or args.commit):
                done.append((label, None))
                print(f"{label}: would build and upload -- {reason}")
                continue
            r = build_mission(m, live, config["projects_root"], out_root)
            line = (f"{r['layout']}, {r['n_profile']} profiles x {r['n_level']} levels, "
                    f"{r['n_public']} public + {r['n_staff']} staff variables, "
                    f"public {r['public_mb']:.1f} MB, staff {r['staff_mb']:.1f} MB ({r['cast']})")
            if args.commit:
                summary = client.upload_bundle(m["id"], str(r["dir"] / "bundle.zip"))
                line += f" -- uploaded, build {summary['buildId']} ({reason})"
            done.append((label, r))
            print(f"{label}: {line}")
        except (BuildError, GatewayError) as e:
            failed.append((label, str(e)))
            print(f"{label}: FAILED -- {e}")
        except Exception as e:  # report, carry on
            failed.append((label, f"unexpected error: {type(e).__name__}: {e}"))
            print(f"{label}: FAILED -- unexpected error: {type(e).__name__}: {e}")

    print()
    verb = "built locally in " + str(out_root) if args.local else "built and uploaded" if args.commit else "would be built and uploaded"
    print(f"{len(done)} bundle(s) {verb}, {len(current)} up to date, {len(failed)} failed")
    if done and done[0][1] is not None:
        print(f"  ({sum(r['public_mb'] + r['staff_mb'] for _, r in done):.0f} MB in total)")
    for label, why in failed:
        print(f"  FAILED {label}: {why}")
    if not (args.local or args.commit):
        print("Dry run -- nothing built or uploaded. Re-run with --commit.")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
