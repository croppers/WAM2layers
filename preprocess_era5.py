import os

import pandas as pd
import xarray as xr
import yaml

from preprocessing import (get_grid_info, get_stable_fluxes,
                           get_vertical_transport)


# Set constants
g = 9.80665  # [m/s2]
density_water = 1000  # [kg/m3]

# Read case configuration
with open("cases/era5_2013.yaml") as f:
    config = yaml.safe_load(f)


def load_data(variable, date):
    """Load data for given variable and date."""
    filename = f"FloodCase_201305_{variable}.nc"
    filepath = os.path.join(config["input_folder"], filename)
    da = xr.open_dataset(filepath)[variable]

    # Include midnight of the next day (if available)
    extra = date + pd.Timedelta(days=1)
    return da.sel(time=slice(date, extra))


datelist = pd.date_range(
    start=config["start_date"], end=config["end_date"], freq="d", inclusive="left"
)

for date in datelist:
    print(date)

    # Load data
    u = load_data("u", date)
    v = load_data("v", date)
    q = load_data("q", date)
    sp = load_data("sp", date)
    evap = load_data("e", date)
    cp = load_data("cp", date)
    lsp = load_data("lsp", date)
    precip = cp + lsp

    # Get grid info
    lat = u.latitude.values
    lon = u.longitude.values
    a_gridcell, l_ew_gridcell, l_mid_gridcell = get_grid_info(lat, lon)

    # Calculate volumes
    evap *= a_gridcell  # m3
    precip *= a_gridcell  # m3

    # Create pressure array
    levels = q.level
    p = levels.broadcast_like(u)  # hPa

    # Interpolate to new levels
    edges = 0.5 * (levels.values[1:] + levels.values[:-1])
    u = u.interp(level=edges)
    v = v.interp(level=edges)
    q = q.interp(level=edges)

    # Calculate pressure jump
    dp = p.diff(dim="level")
    dp["level"] = edges

    # Determine the fluxes and states
    fa_e = u * q * dp / g  # eastward atmospheric moisture flux
    fa_n = v * q * dp / g  # northward atmospheric moisture flux
    cwv = q * dp / g * a_gridcell / density_water  # column water vapor (m3)

    # Split in 2 layers
    P_boundary = 0.72878581 * sp + 7438.803223
    lower_layer = (dp.level < sp / 100) & (dp.level > P_boundary / 100)
    upper_layer = dp.level < P_boundary / 100

    # Integrate fluxes and state
    fa_e_lower = fa_e.where(lower_layer).sum(dim="level")
    fa_n_lower = fa_n.where(lower_layer).sum(dim="level")
    w_lower = cwv.where(lower_layer).sum(dim="level")

    fa_e_upper = fa_e.where(upper_layer).sum(dim="level")
    fa_n_upper = fa_n.where(upper_layer).sum(dim="level")
    w_upper = cwv.where(upper_layer).sum(dim="level")

    print(
        "Check calculation water vapor, this value should be zero:",
        (cwv.sum(dim="level") - (w_upper + w_lower)).sum().values,
    )

    # Change units to m3
    # TODO: Check this! Change units before interp is tricky, if not wrong
    total_seconds = config["timestep"] / config["divt"]
    fa_e_upper *= total_seconds * (l_ew_gridcell / density_water)
    fa_e_lower *= total_seconds * (l_ew_gridcell / density_water)
    fa_n_upper *= total_seconds * (l_mid_gridcell[None, :, None] / density_water)
    fa_n_lower *= total_seconds * (l_mid_gridcell[None, :, None] / density_water)

    # Put data on a smaller time step...
    time = w_upper.time.values
    newtime = pd.date_range(time[0], time[-1], freq="15Min")[:-1]
    w_upper = w_upper.interp(time=newtime).values
    w_lower = w_lower.interp(time=newtime).values

    # ... fluxes on the edges instead of midpoints
    newtime = newtime[:-1] + pd.Timedelta("6Min") / 2
    fa_e_upper = fa_e_upper.interp(time=newtime).values
    fa_n_upper = fa_n_upper.interp(time=newtime).values

    fa_e_lower = fa_e_lower.interp(time=newtime).values
    fa_n_lower = fa_n_lower.interp(time=newtime).values

    precip = (precip.reindex(time=newtime, method="bfill") / 4).values
    evap = (evap.reindex(time=newtime, method="bfill") / 4).values

    # Stabilize horizontal fluxes
    fa_e_upper, fa_e_upper = get_stable_fluxes(fa_e_upper, fa_n_upper, w_upper)
    fa_e_lower, fa_e_lower = get_stable_fluxes(fa_e_lower, fa_n_lower, w_lower)

    # Determine the vertical moisture flux
    fa_vert = get_vertical_transport(
        fa_e_upper,
        fa_e_lower,
        fa_n_upper,
        fa_n_lower,
        evap,
        precip,
        w_upper,
        w_lower,
    )

    # Save preprocessed data
    # Note: fluxes (dim: time) are at the edges of the timesteps,
    # while states (dim: time2) are at the midpoints and include next midnight
    # so the first state from day 2 will overlap with the last flux from day 1
    filename = f"{date.strftime('%Y-%m-%d')}_fluxes_storages.nc"
    output_path = os.path.join(config["interdata_folder"], filename)
    xr.Dataset(
        {  # TODO: would be nice to add coordinates and units as well
            "fa_e_upper": (["time", "lat", "lon"], fa_e_upper),
            "fa_n_upper": (["time", "lat", "lon"], fa_n_upper),
            "fa_e_lower": (["time", "lat", "lon"], fa_e_lower),
            "fa_n_lower": (["time", "lat", "lon"], fa_n_lower),
            "w_upper": (["time2", "lat", "lon"], w_upper),
            "w_lower": (["time2", "lat", "lon"], w_lower),
            "fa_vert": (["time", "lat", "lon"], fa_vert),
            "evap": (["time", "lat", "lon"], evap),
            "precip": (["time", "lat", "lon"], precip),
        }
    ).to_netcdf(output_path)