"""Inspect an L1/L2 NetCDF file and classify its processing convention.

The fleet produces two structurally different conventions for the same
processing levels: Slocum/pyglider output (flat CF-DSG trajectory, scalar
`trajectory` variable) and Seaglider/basestation3 output (NODC Trajectory
Template, `trajectory` as a per-dive array, multiple dimension groups per
file). This module reads a file and reports enough structure to decide
which ERDDAP dataset type/template it needs -- it does not generate an
ERDDAP fragment itself.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass, field

import xarray as xr


@dataclass
class VariableInfo:
    dims: tuple[str, ...]
    dtype: str


@dataclass
class NetcdfReport:
    path: str
    dimensions: dict[str, int]
    global_attrs: dict[str, object]
    variables: dict[str, VariableInfo]
    convention: str
    level: str
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["variables"] = {k: asdict(v) for k, v in self.variables.items()}
        return d


def _classify(dims: dict[str, int], global_attrs: dict[str, object], variables: dict) -> tuple[str, str, list[str]]:
    warnings: list[str] = []

    is_seaglider = "nodc_template_version" in global_attrs or "base_station_version" in global_attrs
    has_trajectory_var = "trajectory" in variables

    if is_seaglider:
        convention = "seaglider_basestation3"
        if "sg_data_point" in dims:
            level = "L1"
        elif "profile" in dims and "depth" in dims:
            level = "L2"
        else:
            level = "unknown"
            warnings.append(
                "Seaglider convention detected but neither 'sg_data_point' "
                "(L1 timeseries) nor 'profile'+'depth' (L2 gridded profile) "
                "dimensions were found."
            )
        traj_var = variables.get("trajectory")
        if traj_var and len(traj_var.dims) == 1 and dims.get(traj_var.dims[0], 0) > 1:
            warnings.append(
                "'trajectory' is an array (one entry per dive), not a single "
                "mission-level scalar -- do not reuse the pyglider-style "
                "EDDTableFromNcCFFiles fragment template as-is for this file."
            )
    elif has_trajectory_var and variables["trajectory"].dims == ():
        # Scalar `trajectory` + cf_role=trajectory_id is pyglider's own marker
        # for its flat CF-DSG L1 output (confirmed against pyglider source:
        # ncprocess.py writes exactly this). Deliberately not checking for a
        # specific dimension name here (e.g. "time") -- pyglider's real
        # dimension name for the point/obs axis hasn't actually been
        # confirmed against a real file, only its variable names have.
        convention = "pyglider"
        level = "L1"
    elif not has_trajectory_var and "depth" in dims and any(len(v.dims) == 2 for v in variables.values()):
        # L2 grid files carry no trajectory variable at all in how these
        # fragments were designed -- a depth dimension plus any genuinely
        # 2-D data variable is the signal instead.
        convention = "pyglider"
        level = "L2"
    else:
        convention = "unknown"
        level = "unknown"
        warnings.append(
            "Could not classify: no NODC/basestation3 marker global attrs, "
            "and no scalar 'trajectory' variable with cf_role=trajectory_id."
        )

    return convention, level, warnings


def inspect_netcdf(path: str) -> NetcdfReport:
    ds = xr.open_dataset(path, decode_times=False)
    try:
        dimensions = dict(ds.sizes)
        global_attrs = dict(ds.attrs)
        variables = {
            name: VariableInfo(dims=var.dims, dtype=str(var.dtype))
            for name, var in ds.variables.items()
        }
    finally:
        ds.close()

    convention, level, warnings = _classify(dimensions, global_attrs, variables)

    return NetcdfReport(
        path=path,
        dimensions=dimensions,
        global_attrs=global_attrs,
        variables=variables,
        convention=convention,
        level=level,
        warnings=warnings,
    )


def _print_human(report: NetcdfReport) -> None:
    print(f"File:       {report.path}")
    print(f"Convention: {report.convention}")
    print(f"Level:      {report.level}")
    print(f"Dimensions: {report.dimensions}")
    print(f"Variables:  {len(report.variables)}")
    if report.warnings:
        print("Warnings:")
        for w in report.warnings:
            print(f"  - {w}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", help="Path to an L1/L2 NetCDF file")
    parser.add_argument("--json", action="store_true", help="Print the full report as JSON")
    args = parser.parse_args()

    report = inspect_netcdf(args.path)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2, default=str))
    else:
        _print_human(report)


if __name__ == "__main__":
    main()
