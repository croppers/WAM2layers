import calendar
import datetime as dt
import os

import numpy as np
import pandas as pd
import scipy.io as sio
import xarray as xr
import yaml
from scipy.interpolate import interp1d

from getconstants_pressure_ECEarth import getconstants_pressure_ECEarth

# Read case configuration
with open("cases/ec-earth.yaml") as f:
    config = yaml.safe_load(f)

# Parse input from config file
# Reassignment not strictly needed but improves readability for often used vars
input_folder = config["input_folder"]
name_of_run = config["name_of_run"]
divt = config["divt"]
count_time = config["count_time"]
latnrs = np.arange(config["latnrs"])
lonnrs = np.arange(config["lonnrs"])
datelist = pd.date_range(
    start=config["start_date"], end=config["end_date"], freq="d", inclusive="left"
)


def _get_input_data(variable, date):
    """Get input data for variable."""
    filename = f"{name_of_run}{variable}_{date.year}{date.month:02d}_NH.nc"
    filepath = os.path.join(config["input_folder"], filename)
    return xr.open_dataset(filepath).sel(time=date.strftime("%Y%m%d"))


def get_input_data(variable, date):
    """Workaround for keeping original timestamps crossing midnight."""
    day1 = _get_input_data(variable, date)
    day2 = _get_input_data(variable, date + dt.timedelta(days=1))
    days = xr.concat([day1, day2], dim="time")
    if len(days.time) < 9:
        return days.isel(time=slice(0, 5))
    else:
        return days.isel(time=slice(0, 10))


def get_output_path(year, month, day, extension=".nc"):
    """Get data for output file."""
    filename = f"{year}-{month:02d}-{day:02d}fluxes_storages{extension}"
    save_path = os.path.join(config["interdata_folder"], filename)
    return save_path


def join_levels(pressure_level_data, surface_level_data):
    """Combine 3d pressure level and 2d surface level data.

    A dummy value of 1000 hPa is inserted for the surface pressure
    """
    bottom = pressure_level_data.isel(lev=0).copy()
    bottom.values = surface_level_data.values
    bottom["lev"] = 100000.0  # dummy value

    # NB: plevs needs to come first to preserve dimension order
    return xr.concat([pressure_level_data, bottom], dim="lev").sortby(
        "lev", ascending=False
    )


def repeat_top_level(pressure_level_data, fill_value=None):
    """Add one more level with the same value as the upper level.

    A dummy value of 100 hPa is inserted for the top level pressure
    """
    top_level_data = pressure_level_data.isel(lev=-1).copy()
    top_level_data["lev"] = 10000.0

    if fill_value is not None:
        top_level_data.values = np.ones_like(top_level_data) * fill_value

    return xr.concat([pressure_level_data, top_level_data], dim="lev")


def load_uvqpsp(latnrs, lonnrs, date):
    times_3hourly = slice(1, None, 2)
    times_lnsp = slice(None, None, 2)  # TODO should be slice(1, None, 2) ??

    q = get_input_data("Q", date).Q.isel(lat=latnrs, lon=lonnrs)
    u = get_input_data("U", date).U.isel(lat=latnrs, lon=lonnrs)
    v = get_input_data("V", date).V.isel(lat=latnrs, lon=lonnrs)
    q2m = get_input_data("Q2M", date).Q2M.isel(
        time=times_3hourly, lat=latnrs, lon=lonnrs
    )
    lnsp = get_input_data("LNSP", date).LNSP.isel(
        time=times_lnsp, lat=latnrs, lon=lonnrs
    )
    u10 = get_input_data("U10", date).U10M.isel(
        time=times_3hourly, lat=latnrs, lon=lonnrs
    )
    v10 = get_input_data("V10", date).V10M.isel(
        time=times_3hourly, lat=latnrs, lon=lonnrs
    )

    sp = np.exp(lnsp)  # log(sp) --> sp (Pa)

    # Create pressure array with the same dimensions as u, q, and v
    # Use the values of the "lev" coordinate to fill the array initally
    p = u.lev.broadcast_like(u)

    u = join_levels(u, u10)  # bottom level will be set to 1000 00 Pa
    v = join_levels(v, v10)
    q = join_levels(q, q2m)
    p = join_levels(p, sp.squeeze())

    u = repeat_top_level(u)  # top level will be set to 100 00 Pa
    v = repeat_top_level(v)
    q = repeat_top_level(q, fill_value=0)
    p = repeat_top_level(p, fill_value=0)

    # Mask data where the pressure level is higher than sp - 1000
    # as there is no valid data at those points
    # and we don't need a second layer that is very close to the surface
    mask = p > sp.values - 1000
    mask[:, 0, :, :] = False  # don't mask bottom (surface pressure values)
    mask[:, -1, :, :] = False  # don't mask top ("ghost cells"?)

    # TODO convert to nan instead of numpy masked array?
    # u_masked = u.where(mask)
    u = np.ma.masked_array(u, mask=mask)
    v = np.ma.masked_array(v, mask=mask)
    q = np.ma.masked_array(q, mask=mask)
    p = np.ma.masked_array(p, mask=mask)
    return q, u, v, sp, p


def get_new_target_levels(surface_pressure, p_boundary, n_levels=40):
    """Build a numpy array with new pressure levels including the boundary."""
    # remove xarray labels if present
    surface_pressure = np.array(surface_pressure)

    pressure_top = 20000
    dp = (surface_pressure - pressure_top) / (n_levels - 1)

    levels = np.arange(0, n_levels)[None, :, None, None]
    ntime, _, nlat, nlon = surface_pressure.shape

    # Note the extra layer of zeros at the bottom and top of the array
    # TODO: find out why
    new_p = np.zeros((ntime, n_levels + 2, nlat, nlon))
    new_p[:, 1:-1, :, :] = surface_pressure - dp * levels

    mask = np.where(new_p > p_boundary, 1, 0)
    mask[:, 0, :, :] = 1  # bottom value is always 1

    # Insert the boundary in the new levels, pushing higher layers one index up
    # e.g. with the boundary at 850 hPa:
    # [0, 1000, 900, 800, 700, 600, 0] --> [0, 1000, 900, 850, 800, 700, 600]
    new_p[:, :-1, :, :] = (
        mask[:, 1:, :, :] * new_p[:, 1:, :, :]
        + (1 - mask[:, 1:, :, :]) * new_p[:, :-1, :, :]
    )

    new_p[:, 1:, :, :] = np.where(
        new_p[:, :-1, :, :] == new_p[:, 1:, :, :],
        p_boundary,
        new_p[:, 1:, :, :],
    )
    return new_p


def interpolate(old_var, old_pressure_levels, new_pressure_levels, type="linear"):

    # allocate array
    new_var = np.zeros_like(new_pressure_levels)

    ntime, _, nlat, nlon = old_var.shape
    for t in range(ntime):
        for i in range(nlat):
            for j in range(nlon):

                pressure_1d = old_pressure_levels[t, :, i, j]
                var_1d = old_var[t, :, i, j]

                pressure_1d = pressure_1d[~pressure_1d.mask]
                var_1d = var_1d[~var_1d.mask]

                f_q = interp1d(pressure_1d, var_1d, type)
                new_var[t, :, i, j] = f_q(new_pressure_levels[t, :, i, j])

    return new_var


def getWandFluxes(
    uq_boundaries,
    vq_boundaries,
    q_boundaries,
    g,
    A_gridcell,
    p_boundary,
    new_pressure_levels,
):
    """Determine the fluxes and states."""
    q_midpoints = 0.5 * (q_boundaries[:, 1:, :, :] + q_boundaries[:, :-1, :, :])
    uq_midpoints = 0.5 * (uq_boundaries[:, 1:, :, :] + uq_boundaries[:, :-1, :, :])
    vq_midpoints = 0.5 * (vq_boundaries[:, 1:, :, :] + vq_boundaries[:, :-1, :, :])
    # for p we do not calculate the mean pressure but the pressure difference between two levels!
    p_diff = np.maximum(
        0, new_pressure_levels[:, :-1, :, :] - new_pressure_levels[:, 1:, :, :]
    )  # the maximum statement is necessary to avoid negative humidity values

    # eastward and northward fluxes
    Fa_E_p = uq_midpoints * p_diff / g
    Fa_N_p = vq_midpoints * p_diff / g

    column_water_vapor = q_midpoints * p_diff / g  # [kg/m2]
    # summed over the vertical
    total_column_water_vapor = np.squeeze(np.sum(column_water_vapor, 1))

    mask = np.where(new_pressure_levels > p_boundary, 1.0, 0.0)

    vapor_down = np.sum(mask[:, :-1, :, :] * q_midpoints * p_diff / g, axis=1)
    vapor_top = np.sum((1 - mask[:, :-1, :, :]) * q_midpoints * p_diff / g, axis=1)

    Fa_E_down = np.sum(mask[:, :-1, :, :] * Fa_E_p, axis=1)  # kg*m-1*s-1
    Fa_N_down = np.sum(mask[:, :-1, :, :] * Fa_N_p, axis=1)  # kg*m-1*s-1
    Fa_E_top = np.sum((1 - mask[:, :-1, :, :]) * Fa_E_p, axis=1)  # kg*m-1*s-1
    Fa_N_top = np.sum((1 - mask[:, :-1, :, :]) * Fa_N_p, axis=1)  # kg*m-1*s-1

    vapor_total = vapor_top + vapor_down

    check = np.sum(total_column_water_vapor - vapor_total)
    print(f"Check calculation water vapor, this value should be zero: {check}")

    # water volumes
    W_top = vapor_top * A_gridcell[None, ...] / density_water  # m3
    W_down = vapor_down * A_gridcell[None, ...] / density_water  # m3

    return column_water_vapor, W_top, W_down, Fa_E_top, Fa_N_top, Fa_E_down, Fa_N_down


def getEP(latnrs, lonnrs, date, A_gridcell):
    """Load and clean up precip and evap data."""

    # (accumulated after the forecast at 00.00 and 12.00 by steps of 3 hours in time
    evaporation = get_input_data("EVAP", date).E.isel(lat=latnrs, lon=lonnrs)  # m
    precipitation = get_input_data("TP", date).TP.isel(lat=latnrs, lon=lonnrs)  # m

    # change sign convention to all positive, transfer negative (originally positive) values of evap to precip
    precipitation = np.maximum(precipitation + np.maximum(evaporation, 0), 0)
    evaporation = np.abs(np.minimum(evaporation, 0))

    # calculate volumes
    E = evaporation * A_gridcell
    P = precipitation * A_gridcell

    # For now extract bare numpy arrays from the data
    return E.values, P.values


# within this new definition of refined I do a linear interpolation over time of my fluxes
def getrefined_new(
    Fa_E_top,
    Fa_N_top,
    Fa_E_down,
    Fa_N_down,
    W_top,
    W_down,
    E,
    P,
    divt,
    count_time,
    latitude,
    longitude,
):
    # This definition refines the timestep of the data
    # Imme: change the timesteps from 6-hourly and 3-hourly to 96 timesteps a day

    # for 3 hourly information
    divt2 = divt / 2.0
    oddvector2 = np.zeros((1, int(count_time * 2 * divt2)))
    partvector2 = np.zeros((1, int(count_time * 2 * divt2)))
    da = np.arange(1, divt2)

    for o in np.arange(0, int(count_time * 2 * divt2), int(divt2)):
        for i in range(len(da)):
            oddvector2[0, o + i] = (divt2 - da[i]) / divt2
            partvector2[0, o + i + 1] = da[i] / divt2

    E_small = np.nan * np.zeros(
        (int(count_time * 2 * divt2), len(latitude), len(longitude))
    )
    for t in range(1, int(count_time * 2 * divt2) + 1):
        E_small[t - 1] = (1.0 / divt2) * E[int(t / divt2 + oddvector2[0, t - 1] - 1)]
    E = E_small

    P_small = np.nan * np.zeros(
        (int(count_time * 2 * divt2), len(latitude), len(longitude))
    )
    for t in range(1, int(count_time * 2 * divt2) + 1):
        P_small[t - 1] = (1.0 / divt2) * P[int(t / divt2 + oddvector2[0, t - 1] - 1)]
    P = P_small

    # for 6 hourly info
    oddvector = np.zeros((1, int(count_time * divt)))
    partvector = np.zeros((1, int(count_time * divt)))
    da = np.arange(1, divt)
    divt = float(divt)
    for o in np.arange(0, int(count_time * divt), int(divt)):
        for i in range(len(da)):
            oddvector[0, o + i] = (divt - da[i]) / divt
            partvector[0, o + i + 1] = da[i] / divt

    W_top_small = np.nan * np.zeros(
        (int(count_time * divt + 1), len(latitude), len(longitude))
    )
    W_down_small = np.nan * np.zeros(
        (int(count_time * divt + 1), len(latitude), len(longitude))
    )

    Fa_E_down_small = np.nan * np.zeros(
        (int(count_time * divt), len(latitude), len(longitude))
    )
    Fa_N_down_small = np.nan * np.zeros(
        (int(count_time * divt), len(latitude), len(longitude))
    )
    Fa_E_top_small = np.nan * np.zeros(
        (int(count_time * divt), len(latitude), len(longitude))
    )
    Fa_N_top_small = np.nan * np.zeros(
        (int(count_time * divt), len(latitude), len(longitude))
    )

    for t in range(1, int(count_time * divt) + 1):
        W_top_small[t - 1] = W_top[
            int(t / divt + oddvector[0, t - 1] - 1)
        ] + partvector[0, t - 1] * (
            W_top[int(t / divt + oddvector[0, t - 1])]
            - W_top[int(t / divt + oddvector[0, t - 1] - 1)]
        )
        W_top_small[-1] = W_top[-1]
        W_down_small[t - 1] = W_down[
            int(t / divt + oddvector[0, t - 1] - 1)
        ] + partvector[0, t - 1] * (
            W_down[int(t / divt + oddvector[0, t - 1])]
            - W_down[int(t / divt + oddvector[0, t - 1] - 1)]
        )
        W_down_small[-1] = W_down[-1]

        Fa_E_down_small[t - 1] = Fa_E_down[
            int(t / divt + oddvector[0, t - 1] - 1)
        ] + partvector[0, t - 1] * (
            Fa_E_down[int(t / divt + oddvector[0, t - 1])]
            - Fa_E_down[int(t / divt + oddvector[0, t - 1] - 1)]
        )
        Fa_N_down_small[t - 1] = Fa_N_down[
            int(t / divt + oddvector[0, t - 1] - 1)
        ] + partvector[0, t - 1] * (
            Fa_N_down[int(t / divt + oddvector[0, t - 1])]
            - Fa_N_down[int(t / divt + oddvector[0, t - 1] - 1)]
        )
        Fa_E_top_small[t - 1] = Fa_E_top[
            int(t / divt + oddvector[0, t - 1] - 1)
        ] + partvector[0, t - 1] * (
            Fa_E_top[int(t / divt + oddvector[0, t - 1])]
            - Fa_E_top[int(t / divt + oddvector[0, t - 1] - 1)]
        )
        Fa_N_top_small[t - 1] = Fa_N_top[
            int(t / divt + oddvector[0, t - 1] - 1)
        ] + partvector[0, t - 1] * (
            Fa_N_top[int(t / divt + oddvector[0, t - 1])]
            - Fa_N_top[int(t / divt + oddvector[0, t - 1] - 1)]
        )

    W_top = W_top_small
    W_down = W_down_small
    Fa_E_down = Fa_E_down_small
    Fa_N_down = Fa_N_down_small
    Fa_E_top = Fa_E_top_small
    Fa_N_top = Fa_N_top_small

    return Fa_E_top, Fa_N_top, Fa_E_down, Fa_N_down, E, P, W_top, W_down


# Code
def change_units(
    Fa_E_top_kgpmps,
    Fa_E_down_kgpmps,
    Fa_N_top_kgpmps,
    Fa_N_down_kgpmps,
    timestep,
    divt,
    L_EW_gridcell,
    density_water,
    L_N_gridcell,
    L_S_gridcell,
    latitude,
):

    # convert to m3
    Fa_E_top_m3 = (
        Fa_E_top_kgpmps * timestep / float(divt) * L_EW_gridcell / density_water
    )  # [kg*m^-1*s^-1*s*m*kg^-1*m^3]=[m3]
    Fa_E_down_m3 = (
        Fa_E_down_kgpmps * timestep / float(divt) * L_EW_gridcell / density_water
    )  # [s*m*kg*m^-1*s^-1*kg^-1*m^3]=[m3]

    Fa_N_top_swap = np.zeros(
        (len(latitude), int(count_time * float(divt)), len(longitude))
    )
    Fa_N_down_swap = np.zeros(
        (len(latitude), int(count_time * float(divt)), len(longitude))
    )
    Fa_N_top_kgpmps_swap = np.swapaxes(Fa_N_top_kgpmps, 0, 1)
    Fa_N_down_kgpmps_swap = np.swapaxes(Fa_N_down_kgpmps, 0, 1)
    for c in range(len(latitude)):
        Fa_N_top_swap[c] = (
            Fa_N_top_kgpmps_swap[c]
            * timestep
            / float(divt)
            * 0.5
            * (L_N_gridcell[c] + L_S_gridcell[c])
            / density_water
        )  # [s*m*kg*m^-1*s^-1*kg^-1*m^3]=[m3]
        Fa_N_down_swap[c] = (
            Fa_N_down_kgpmps_swap[c]
            * timestep
            / float(divt)
            * 0.5
            * (L_N_gridcell[c] + L_S_gridcell[c])
            / density_water
        )  # [s*m*kg*m^-1*s^-1*kg^-1*m^3]=[m3]

    Fa_N_top_m3 = np.swapaxes(Fa_N_top_swap, 0, 1)
    Fa_N_down_m3 = np.swapaxes(Fa_N_down_swap, 0, 1)

    return Fa_E_top_m3, Fa_E_down_m3, Fa_N_top_m3, Fa_N_down_m3


def get_stablefluxes(Fa_E, Fa_N, W):
    """Stabilize the outfluxes / influxes.

    During the reduced timestep the water cannot move further than 1/x * the
    gridcell, In other words at least x * the reduced timestep is needed to
    cross a gridcell.
    """
    Fa_E_abs = np.abs(Fa_E)
    Fa_N_abs = np.abs(Fa_N)

    stab = 1.0 / 2.0

    Fa_E_corrected = (Fa_E_abs / (Fa_E_abs + Fa_N_abs)) * stab * W[:-1, :, :]
    Fa_E_stable = np.minimum(Fa_E_abs, Fa_E_corrected)

    Fa_N_corrected = (Fa_N_abs / (Fa_E_abs + Fa_N_abs)) * stab * W[:-1, :, :]
    Fa_N_stable = np.minimum(Fa_N_abs, Fa_N_corrected)

    # get rid of the nan values
    Fa_E_stable[np.isnan(Fa_E_stable)] = 0
    Fa_N_stable[np.isnan(Fa_N_stable)] = 0

    # redefine
    Fa_E = np.sign(Fa_E) * Fa_E_stable
    Fa_N = np.sign(Fa_N) * Fa_N_stable

    return Fa_E, Fa_N


# Code
def getFa_Vert(
    Fa_E_top,
    Fa_E_down,
    Fa_N_top,
    Fa_N_down,
    E,
    P,
    W_top,
    W_down,
):

    # total moisture in the column
    W = W_top + W_down

    def divergence_zonal(Fa_E):
        """Define the horizontal fluxes over the boundaries."""

        # TODO: Is this correct? It looks like the eastern and western
        # boundaries are mixed up. Also the inserted zeros might cause trouble.
        # I think the implementation should be exactly like the meridional one.
        # I have verified that this code does exactly the same as the original.

        # fluxes over the eastern boundary
        Fa_E_boundary = np.zeros_like(Fa_E)
        Fa_E_boundary[:, :, :-1] = 0.5 * (Fa_E[:, :, :-1] + Fa_E[:, :, 1:])

        Fa_W_boundary = np.roll(Fa_E_boundary, 1)
        return Fa_W_boundary - Fa_E_boundary

    def divergence_meridional(Fa_N):
        """Define the horizontal fluxes over the boundaries."""
        Fa_N_boundary = np.zeros_like(Fa_N)
        Fa_N_boundary[:, 1:, :] = 0.5 * (Fa_N[:, :-1, :] + Fa_N[:, 1:, :])

        Fa_S_boundary = np.roll(Fa_N_boundary, -1, axis=1)
        return Fa_S_boundary - Fa_N_boundary

    zonal_divergence_top = divergence_zonal(Fa_E_top)
    zonal_divergence_down = divergence_zonal(Fa_E_down)
    meridional_divergence_top = divergence_meridional(Fa_N_top)
    meridional_divergence_down = divergence_meridional(Fa_N_down)

    # check the water balance
    residual_down = np.zeros_like(P)  # residual factor [m3]
    residual_top = np.zeros_like(P)  # residual factor [m3]

    tendency_down = (  # TODO why skip the N/S but not W/E edges?
        +zonal_divergence_down[:, 1:-1, :]
        + meridional_divergence_down[:, 1:-1, :]
        - P[:, 1:-1, :] * (W_down[:-1, 1:-1, :] / W[:-1, 1:-1, :])
        + E[:, 1:-1, :]
    )

    tendency_top = (  # TODO why skip the N/S but not W/E edges?
        +zonal_divergence_top[:, 1:-1, :]
        + meridional_divergence_top[:, 1:-1, :]
        - P[:, 1:-1, :] * (W_top[:-1, 1:-1, :] / W[:-1, 1:-1, :])
    )

    residual_down[:, 1:-1, :] = (
        W_down[1:, 1:-1, :] - W_down[:-1, 1:-1, :] - tendency_down
    )
    residual_top[:, 1:-1, :] = W_top[1:, 1:-1, :] - W_top[:-1, 1:-1, :] - tendency_top

    # compute the resulting vertical moisture flux; the vertical velocity so
    # that the new residual_down/W_down = residual_top/W_top (positive downward)
    Fa_Vert_raw = (
        W_down[1:, :, :] / W[1:, :, :] * (residual_down + residual_top) - residual_down
    )

    # stabilize the outfluxes / influxes; during the reduced timestep the
    # vertical flux can maximally empty/fill 1/x of the top or down storage
    stab = 1.0 / 4.0
    Fa_Vert_stable = np.minimum(
        np.abs(Fa_Vert_raw), np.minimum(stab * W_top[1:, :, :], stab * W_down[1:, :, :])
    )

    # redefine the vertical flux
    Fa_Vert = np.sign(Fa_Vert_raw) * Fa_Vert_stable

    return Fa_Vert


# Runtime & Results
start1 = dt.datetime.now()
# obtain the constants
(
    latitude,
    longitude,
    lsm,
    g,
    density_water,
    timestep,
    A_gridcell,
    L_N_gridcell,
    L_S_gridcell,
    L_EW_gridcell,
    gridcell,
) = getconstants_pressure_ECEarth(latnrs, lonnrs, config["land_sea_mask"])


for date in datelist:
    start = dt.datetime.now()

    day = date.day
    year = date.year
    month = date.month

    begin_time = (day - 1) * count_time  # Python starts at 0
    final_time = calendar.monthrange(year, month)[1]

    print(f"Preprocessing data for {date}, {begin_time=}, {final_time=}")
    q, u, v, sp, p = load_uvqpsp(latnrs, lonnrs, date)

    # Imme: location of boundary is hereby hard defined at model level 47 which corresponds with about
    p_boundary = 0.72878581 * sp.values + 7438.803223
    new_pressure_levels = get_new_target_levels(sp, p_boundary, n_levels=40)

    print("before interpolation loop", dt.datetime.now().time())
    uq_boundaries = interpolate(u * q, p, new_pressure_levels, type="cubic")
    vq_boundaries = interpolate(v * q, p, new_pressure_levels, type="cubic")
    q_boundaries = interpolate(q, p, new_pressure_levels, type="linear")
    print("after interpolation loop", dt.datetime.now().time())

    # 1 integrate specific humidity to get the (total) column water (vapor) and calculate horizontal moisture fluxes
    cwv, W_top, W_down, Fa_E_top, Fa_N_top, Fa_E_down, Fa_N_down = getWandFluxes(
        uq_boundaries,
        vq_boundaries,
        q_boundaries,
        g,
        A_gridcell,
        p_boundary,
        new_pressure_levels,
    )
    print(
        f"Step 1, 2, 3 finished, elapsed time since start: {dt.datetime.now() - start}"
    )

    # 4 evaporation and precipitation
    E, P = getEP(latnrs, lonnrs, date, A_gridcell)
    print(f"Step 4 finished, elapsed time since start: {dt.datetime.now() - start}")

    # put data on a smaller time step
    (
        Fa_E_top_1,
        Fa_N_top_1,
        Fa_E_down_1,
        Fa_N_down_1,
        E,
        P,
        W_top,
        W_down,
    ) = getrefined_new(
        Fa_E_top,
        Fa_N_top,
        Fa_E_down,
        Fa_N_down,
        W_top,
        W_down,
        E,
        P,
        divt,
        count_time,
        latitude,
        longitude,
    )
    print(f"Step 5 finished, elapsed time since start: {dt.datetime.now() - start}")

    # change units to m3
    Fa_E_top_m3, Fa_E_down_m3, Fa_N_top_m3, Fa_N_down_m3 = change_units(
        Fa_E_top_1,
        Fa_E_down_1,
        Fa_N_top_1,
        Fa_N_down_1,
        timestep,
        divt,
        L_EW_gridcell,
        density_water,
        L_N_gridcell,
        L_S_gridcell,
        latitude,
    )
    print(f"Step 6a finished, elapsed time since start: {dt.datetime.now() - start}")

    # stabilize horizontal fluxes
    Fa_E_top, Fa_N_top = get_stablefluxes(Fa_E_top_m3, Fa_N_top_m3, W_top)
    Fa_E_down, Fa_N_down = get_stablefluxes(Fa_E_down_m3, Fa_N_down_m3, W_down)
    print(f"Step 6b finished, elapsed time since start: {dt.datetime.now() - start}")

    # determine the vertical moisture flux
    Fa_Vert = getFa_Vert(
        Fa_E_top,
        Fa_E_down,
        Fa_N_top,
        Fa_N_down,
        E,
        P,
        W_top,
        W_down,
    )
    print(f"Step 7 finished, elapsed time since start: {dt.datetime.now() - start}")

    # Save preprocessed data
    xr.Dataset(
        {  # TODO: would be nice to add coordinates and units as well
            "Fa_E_top": (["time", "lat", "lon"], Fa_E_top),
            "Fa_N_top": (["time", "lat", "lon"], Fa_N_top),
            "Fa_E_down": (["time", "lat", "lon"], Fa_E_down),
            "Fa_N_down": (["time", "lat", "lon"], Fa_N_down),
            "E": (["time", "lat", "lon"], E),
            "P": (["time", "lat", "lon"], P),
            "W_top": (["time2", "lat", "lon"], W_top),  # note different time
            "W_down": (["time2", "lat", "lon"], W_down),  # note different time
            "Fa_Vert": (["time", "lat", "lon"], Fa_Vert),
        }
    ).to_netcdf(get_output_path(year, month, day, extension=".nc"))

    end = dt.datetime.now()
    print(f"Runtime fluxes_and_storages for {day=}, {year=} is {end-start}")

end1 = dt.datetime.now()
print("The total runtime is {end1 - start1}")
