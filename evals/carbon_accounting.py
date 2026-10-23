# SPDX-FileCopyrightText: 2023-2026 Austrian Gas Grid Management AG
#
# SPDX-License-Identifier: MIT
# For license information, see the LICENSE.txt file in the project root.
"""
Compare runs of the carbon accounting configuration for one country.

Backs the notebook ``evals/notebooks/carbon_accounting.py``. Functions that
read networks take a mapping scenario -> horizon -> network and return frames
with a ``scenario`` and a ``horizon`` level, so they work for any number of
scenarios. Functions that compare scenarios take these frames and a reference
scenario. See ``docs-at/explanations/carbon-accounting-eval-spec.md``.

The carrier lists mirror ``mods.constants``; the evals do not import ``mods``.
"""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
import pypsa
import xarray as xr

from evals.constants import AVIATION_CARRIERS, FUEL_CLAIM_CARRIERS
from evals.fileio import read_networks
from evals.utils import (
    claimable_consumption,
    fair_share_caps,
    get_energy_totals_domestic_share,
    get_location,
    pool_inflow,
    scale_domestic_aviation,
)

Networks = Mapping[str, Mapping[str, pypsa.Network]]

ATMOSPHERE = "co2 atmosphere"
EU_CO2_LIMIT = "CO2Limit-upper"
# mirrors mods.constants (carbon accounting classes, spec section 3.2)
GREEN_BIO_PRODUCERS: tuple[str, ...] = (
    "biogas to gas",
    "biogas to gas CC",
    "BioSNG",
    "BioSNG CC",
    "biomass to liquid",
    "biomass to liquid CC",
    "electrobiofuels",
    "unsustainable bioliquids",
    "biomass-to-methanol",
    "biomass-to-methanol CC",
)
GREEN_SYNTHETIC_PRODUCERS: tuple[str, ...] = (
    "Fischer-Tropsch",
    "Sabatier",
    "methanolisation",
)
GREEN_IMPORTS: tuple[str, ...] = ("import gas", "import oil", "import methanol")
CAPTURE: tuple[str, ...] = (
    "DAC",
    "SMR CC",
    "gas for industry CC",
    "urban central gas CHP CC",
    "CCGT methanol CC",
    "Methanol steam reforming CC",
    "allam methanol",
    "waste CHP CC",
    "process emissions CC",
    "solid biomass for industry CC",
    "solid biomass to hydrogen",
    "urban central solid biomass CHP CC",
)
VENT: tuple[str, ...] = ("co2 vent",)
GAS_TRANSPORT: tuple[str, ...] = ("gas pipeline", "gas pipeline new")
TRANSFER_CARRIER = "renewable gas attribute transfer"
ATTRIBUTE_CARRIERS: tuple[str, ...] = (
    "renewable gas attribute",
    "renewable oil attribute",
    "renewable methanol attribute",
)
# bus carriers that are accounting, not trade
ACCOUNTING_BUS_CARRIERS: tuple[str, ...] = ("co2", "co2 import", *ATTRIBUTE_CARRIERS)
# bus carriers that are not traded; attributes are, see country_cost
NON_TRADE_BUS_CARRIERS: tuple[str, ...] = ("co2", "co2 import")
POOLED_FUELS: tuple[str, ...] = ("oil", "methanol")
# claim kinds as used in the constraint names, per claim carrier
CLAIM_KINDS: dict[str, str] = {
    "renewable gas claim": "gas",
    "renewable oil claim": "oil",
    "renewable oil claim aviation": "oil aviation",
    "renewable methanol claim": "methanol",
}

# categories of the national balance bridge (spec section 2)
FUEL_USE = "Fuel use"
PROCESS_AND_WASTE = "Process & waste"
CAPTURE_CREDIT = "Capture credit"
CREATION_CREDIT = "Creation credit"
CCU_CHARGE = "CCU charge"
CLAIM_CATEGORIES: dict[str, str] = {
    "renewable gas claim": "Claims gas",
    "renewable oil claim": "Claims oil",
    "renewable oil claim aviation": "Claims kerosene",
    "renewable methanol claim": "Claims methanol",
}
PHYSICAL_CATEGORIES: tuple[str, ...] = (FUEL_USE, PROCESS_AND_WASTE, CAPTURE_CREDIT)
ACCOUNTING_CATEGORIES: tuple[str, ...] = (
    CREATION_CREDIT,
    CCU_CHARGE,
    *CLAIM_CATEGORIES.values(),
)
CATEGORIES: tuple[str, ...] = (*PHYSICAL_CATEGORIES, *ACCOUNTING_CATEGORIES)
PROCESS_AND_WASTE_CARRIERS: tuple[str, ...] = (
    "process emissions",
    "process emissions CC",
    "HVC to air",
    "waste CHP",
    "waste CHP CC",
    "municipal solid waste",
)

# tolerances of the budget regime (spec section 3), to be checked against real runs
EPS_MU = 1e-3  # EUR/t, below this the national CO2 price counts as zero
EPS_BOUND = 1e-3  # EUR/MWh, estimated dual above which a credit bound binds
EPS_SLACK = 1e-3  # relative slack below which a credit bound counts as tight
EPS_POOL = 1e-6  # share of the pool inflow claimed by countries without a budget
# relative tolerance of the consistency checks
REL_TOL = 1e-6

# technologies of the physical system response: link carriers and product bus carrier
TECHNOLOGIES: dict[str, tuple[tuple[str, ...], str]] = {
    "biogas to gas": (("biogas to gas",), "gas"),
    "biogas to gas CC": (("biogas to gas CC",), "gas"),
    "BioSNG": (("BioSNG",), "gas"),
    "BioSNG CC": (("BioSNG CC",), "gas"),
    "electrolysis": (("H2 Electrolysis",), "H2"),
    "Sabatier": (("Sabatier",), "gas"),
    "Fischer-Tropsch": (("Fischer-Tropsch",), "oil"),
    "methanolisation": (("methanolisation",), "methanol"),
    "DAC": (("DAC",), "co2 stored"),
    "point-source capture": (tuple(c for c in CAPTURE if c != "DAC"), "co2 stored"),
}
FUEL_BALANCES: tuple[str, ...] = ("gas", "oil", "methanol", "H2")
CO2_TRANSPORT = "CO2 pipeline"

MT = 1e6  # t per Mt
TWH = 1e6  # MWh per TWh
BN = 1e9  # EUR per bn EUR


@dataclass
class Check:
    """Result of a consistency check: status ``ok``, ``warn`` or ``fail``."""

    name: str
    status: str
    message: str
    details: pd.DataFrame = field(default_factory=pd.DataFrame)

    @property
    def failed(self) -> bool:
        """Whether the check failed."""
        return self.status == "fail"


# --- discovery -----------------------------------------------------------------


def find_scenarios(root: str | Path) -> dict[str, dict[str, Path]]:
    """
    Return the solved networks of every scenario in a result root.

    Parameters
    ----------
    root
        Result root, e.g. ``results/carbon-accounting``.

    Returns
    -------
    :
        Network file per horizon per scenario, sorted. Scenarios without
        networks are left out.
    """
    scenarios = {}
    for directory in sorted(Path(root).iterdir()) if Path(root).is_dir() else []:
        files = sorted((directory / "networks").glob("*[0-9].nc"))
        if files:
            scenarios[directory.name] = {
                re.search(r"\d{4}", f.stem).group(): f for f in files
            }
    return scenarios


def read_meta(path: str | Path) -> dict:
    """Return the run configuration stored in a network file, without loading it."""
    with xr.open_dataset(path) as ds:
        return json.loads(ds.attrs["meta"])


def _method(meta: dict) -> tuple[str, bool, float, float]:
    """Return method, fair share flag, tolerance and claim cost from a run config."""
    config = meta.get("mods", {}).get("carbon_accounting")
    if not config or not config["enable"] or config["method"] != "carrier":
        return "technology", False, np.nan, np.nan
    fair_share = config["fair_share"]
    tolerance = fair_share["tolerance"] if fair_share["enable"] else np.nan
    return "carrier", fair_share["enable"], tolerance, config["claim_cost"]


def run_info(files: Mapping[str, Mapping[str, Path]]) -> pd.DataFrame:
    """
    Return the carbon accounting settings of every network file.

    Parameters
    ----------
    files
        Network file per horizon per scenario, see :func:`find_scenarios`.

    Returns
    -------
    :
        Columns ``method``, ``fair_share``, ``tolerance``, ``claim_cost``,
        ``budgets`` (countries with a national budget) and ``file``, indexed
        by ``scenario`` and ``horizon``.
    """
    rows = {}
    for scenario, horizons in files.items():
        for horizon, path in horizons.items():
            meta = read_meta(path)
            method, fair_share, tolerance, claim_cost = _method(meta)
            budgets = meta["solving"]["constraints"]["co2_budget_national"] or {}
            rows[(scenario, horizon)] = {
                "method": method,
                "fair_share": fair_share,
                "tolerance": tolerance,
                "claim_cost": claim_cost,
                "budgets": ", ".join(sorted(budgets)),
                "file": str(path),
            }
    info = pd.DataFrame.from_dict(rows, orient="index")
    info.index.names = ["scenario", "horizon"]
    return info


def default_reference(info: pd.DataFrame) -> str:
    """Return the first scenario with method ``technology``, else the first scenario."""
    technology = info.index.get_level_values("scenario")[info.method == "technology"]
    return technology[0] if len(technology) else info.index[0][0]


def load_networks(path: str | Path) -> dict[str, pypsa.Network]:
    """Return the solved networks of one scenario directory, sorted by horizon."""
    nc = read_networks(path)
    return {horizon: nc.networks[horizon] for horizon in sorted(nc.networks.index)}


def check_setup(info: pd.DataFrame, scenarios: list[str], country: str) -> Check:
    """
    Check that the selected scenarios can be compared.

    Fails for fewer than two scenarios or different horizons. Warns if the
    country has no national CO2 budget in a run.

    Parameters
    ----------
    info
        Settings per network, see :func:`run_info`.
    scenarios
        Selected scenarios.
    country
        Country code.

    Returns
    -------
    :
        The check result.
    """
    if len(scenarios) < 2:
        return Check("Setup", "fail", "Select at least two scenarios.")
    info = info.loc[scenarios]
    horizons = info.reset_index().groupby("scenario").horizon.apply(tuple)
    if horizons.nunique() > 1:
        return Check(
            "Setup",
            "fail",
            "The selected scenarios have different horizons.",
            horizons.to_frame(),
        )
    no_budget = info.index[~info.budgets.str.split(", ").apply(lambda b: country in b)]
    if len(no_budget):
        return Check(
            "Setup",
            "warn",
            f"{country} has no national CO2 budget in {len(no_budget)} networks; "
            f"the cost of the national target is undefined there.",
        )
    return Check(
        "Setup", "ok", f"{len(scenarios)} scenarios, horizons {horizons.iloc[0]}."
    )


# --- flows ---------------------------------------------------------------------


def _ports(static: pd.DataFrame) -> list[int]:
    return sorted(int(c[3:]) for c in static.columns if re.fullmatch(r"bus\d+", c))


def port_flows(n: pypsa.Network) -> pd.DataFrame:
    """
    Return the annual flow and its value at every port of every component.

    Flows are positive when the component withdraws from the bus (PyPSA's
    sign of branch ports), so generators and stores that supply are negative.

    Parameters
    ----------
    n
        A solved network.

    Returns
    -------
    :
        One row per connected port with the columns ``component``, ``name``,
        ``carrier``, ``port``, ``bus``, ``bus_carrier``, ``bus_country``,
        ``asset_country``, ``energy`` (weighted annual flow) and ``value``
        (weighted annual flow times the marginal price of the bus, EUR).
    """
    energy_weights = n.snapshot_weightings.generators
    cost_weights = n.snapshot_weightings.objective
    prices = n.buses_t.marginal_price
    bus_country = n.buses.location.fillna("").str[:2]

    def collect(
        c: str, port: str, sign: float, bus: pd.Series, country: pd.Series
    ) -> pd.DataFrame:
        static = n.components[c].static
        bus = bus.fillna("")
        bus = bus[bus != ""]
        p = n.components[c].dynamic[f"p{port}"].reindex(columns=bus.index, fill_value=0)
        p = sign * p.fillna(0.0)
        price = prices.reindex(columns=bus.to_numpy(), fill_value=0.0).to_numpy()
        return pd.DataFrame(
            {
                "component": c,
                "name": bus.index,
                "carrier": static.loc[bus.index, "carrier"].to_numpy(),
                "port": port or "0",
                "bus": bus.to_numpy(),
                "bus_carrier": bus.map(n.buses.carrier).to_numpy(),
                "bus_country": bus.map(bus_country).to_numpy(),
                "asset_country": country.reindex(bus.index).to_numpy(),
                "energy": p.mul(energy_weights, axis=0).sum().to_numpy(),
                "value": (p * price).mul(cost_weights, axis=0).sum().to_numpy(),
            }
        )

    frames = []
    for c in sorted(n.branch_components):
        static = n.components[c].static
        if static.empty:
            continue
        country = get_location(n, c).str[:2]
        for port in _ports(static):
            frames.append(collect(c, str(port), 1.0, static[f"bus{port}"], country))
    for c, sign in [("Generator", -1), ("Load", 1), ("Store", -1), ("StorageUnit", -1)]:
        static = n.components[c].static
        if not static.empty:
            country = get_location(n, c).str[:2]
            frames.append(collect(c, "", sign, static["bus"], country))
    flows = pd.concat(frames, ignore_index=True)
    flows["asset_country"] = flows["asset_country"].fillna("")
    return flows


def _link_flows(flows: pd.DataFrame, bus_carrier: str) -> pd.Series:
    """Return the annual injection of every link into buses of a carrier."""
    at_bus = (flows.component == "Link") & (flows.bus_carrier == bus_carrier)
    return -flows[at_bus].groupby("name").energy.sum()


def domestic_aviation_share(n: pypsa.Network, country: str) -> float:
    """Return the domestic share of aviation of a country, as in the national budget."""
    energy_totals = pd.DataFrame.from_dict(
        n.meta["resources"]["energy_totals"], orient="tight"
    )
    share = get_energy_totals_domestic_share(energy_totals, kind="aviation")
    return float(share.get(country, 0.0))


def national_co2_categories(
    n: pypsa.Network, country: str, flows: pd.DataFrame | None = None
) -> pd.Series:
    """
    Split the national CO2 balance of a country into the bridge categories.

    Selects links by name prefix and scales kerosene and kerosene claims with
    the domestic aviation share, as the national CO2 budget does
    (``mods.constraints.co2_budget``), so the categories add up to the
    left-hand side of ``co2_limit-{country}``. Links that capture CO2 split
    into the capture credit (the CO2 captured) and the rest of their
    emission, which goes to the category of the link.

    Parameters
    ----------
    n
        A solved network.
    country
        Country code.
    flows
        Port flows of ``n``, see :func:`port_flows`; computed if missing.

    Returns
    -------
    :
        Emissions in t CO2 per category of :data:`CATEGORIES`.
    """
    flows = port_flows(n) if flows is None else flows
    links = n.links[n.links.index.str.startswith(country)]
    emitted = _link_flows(flows, "co2").reindex(links.index, fill_value=0.0)
    captured = _link_flows(flows, "co2 stored").reindex(links.index, fill_value=0.0)
    carrier = links.carrier

    # credit + rest = emitted per link: the split moves CO2 between
    # categories, it never adds any (check I5 compares the sum with the LHS)
    is_capture = carrier.isin(CAPTURE + GREEN_BIO_PRODUCERS)
    credit = -captured.where(is_capture, 0.0)
    rest = emitted - credit

    category = pd.Series(FUEL_USE, index=links.index)
    category[carrier.isin(PROCESS_AND_WASTE_CARRIERS)] = PROCESS_AND_WASTE
    category[carrier.isin(GREEN_BIO_PRODUCERS + GREEN_IMPORTS)] = CREATION_CREDIT
    category[carrier.isin(GREEN_SYNTHETIC_PRODUCERS)] = CCU_CHARGE
    # venting releases captured CO2 again, so it nets against the capture
    # credit; only built with sector.co2_vent (off in the AT configs)
    category[carrier.isin(VENT)] = CAPTURE_CREDIT
    for claim, name in CLAIM_CATEGORIES.items():
        category[carrier == claim] = name

    alpha = domestic_aviation_share(n, country)
    rest = rest.where(~carrier.isin(AVIATION_CARRIERS), rest * alpha)
    balance = rest.groupby(category).sum()
    balance[CAPTURE_CREDIT] = balance.get(CAPTURE_CREDIT, 0.0) + credit.sum()
    return balance.reindex(list(CATEGORIES), fill_value=0.0)


def _country_frame(networks: Networks, func, **kwargs) -> pd.DataFrame:
    """Apply a function to every network and stack the results."""
    rows = {
        (scenario, horizon): func(n, **kwargs)
        for scenario, horizons in networks.items()
        for horizon, n in horizons.items()
    }
    frame = pd.DataFrame.from_dict(rows, orient="index")
    frame.index.names = ["scenario", "horizon"]
    return frame


def national_budget(n: pypsa.Network, country: str) -> float:
    """Return the national CO2 budget in t, NaN without a budget."""
    name = f"co2_limit-{country}"
    gc = n.global_constraints
    return gc.at[name, "constant"] if name in gc.index else np.nan


def balance_bridge(networks: Networks, country: str) -> pd.DataFrame:
    """
    Return the national CO2 balance per category for every run.

    Parameters
    ----------
    networks
        Solved networks per horizon per scenario.
    country
        Country code.

    Returns
    -------
    :
        Mt CO2 with one column per category of :data:`CATEGORIES`, plus
        ``Total`` and ``Budget``, indexed by ``scenario`` and ``horizon``.
    """

    def bridge(n: pypsa.Network) -> pd.Series:
        balance = national_co2_categories(n, country)
        balance["Total"] = balance.sum()
        balance["Budget"] = national_budget(n, country)
        return balance / MT

    return _country_frame(networks, bridge)


def waterfall(
    bridge: pd.DataFrame, horizon: str, a: str, b: str, cutoff: float = 0.0
) -> pd.DataFrame:
    """
    Return the steps of the waterfall from scenario ``a`` to scenario ``b``.

    Parameters
    ----------
    bridge
        Output of :func:`balance_bridge`.
    horizon
        Planning horizon.
    a, b
        Scenarios at the start and the end of the waterfall.
    cutoff
        Steps with an absolute change below this value are dropped.

    Returns
    -------
    :
        One row per step with the columns ``step``, ``group`` (``total``,
        ``physical`` or ``accounting``), ``value`` (the total or the change),
        ``base`` (where the bar starts) and ``end`` (where it ends), and the
        budget of ``b`` in ``attrs["budget"]``.
    """
    start, end = bridge.loc[(a, horizon)], bridge.loc[(b, horizon)]
    rows = [{"step": f"{a}", "group": "total", "value": start["Total"]}]
    for category in CATEGORIES:
        delta = end[category] - start[category]
        if abs(delta) > cutoff:
            group = "physical" if category in PHYSICAL_CATEGORIES else "accounting"
            rows.append({"step": f"Δ {category}", "group": group, "value": delta})
    rows.append({"step": f"{b}", "group": "total", "value": end["Total"]})
    steps = pd.DataFrame(rows)
    is_total = steps.group == "total"
    level = steps.value.where(~is_total, 0.0).cumsum() + start["Total"]
    steps["end"] = level.where(~is_total, steps.value)
    steps["base"] = (steps.end - steps.value).where(~is_total, 0.0)
    steps.attrs["budget"] = end["Budget"]
    return steps


# --- consistency ---------------------------------------------------------------


def _attribute_store_change(n: pypsa.Network) -> float:
    """Return the largest annual net change of an attribute store, in MWh."""
    stores = n.stores.index[n.stores.carrier.isin(ATTRIBUTE_CARRIERS)]
    if stores.empty:
        return 0.0
    weights = n.snapshot_weightings.stores
    change = n.stores_t.p[stores].mul(weights, axis=0).sum()
    return float(change.abs().max())


def _evals_balance(n: pypsa.Network, country: str) -> float:
    """Return the national CO2 balance of the evals (``view_balance_carbon``), in t."""
    balance = n.statistics.energy_balance(
        bus_carrier="co2", groupby=["location", "carrier"]
    )
    balance = balance[
        balance.index.get_level_values("location").str.startswith(country)
    ]
    share = pd.Series({country: domestic_aviation_share(n, country)})
    return float(scale_domestic_aviation(balance.to_frame(), share).sum().iloc[0])


def _fair_share_excess(n: pypsa.Network) -> float:
    """Return the largest relative excess of a claim over its fair share cap."""
    weightings = n.snapshot_weightings.generators
    p0 = n.links_t.p0.mul(weightings, axis=0).sum().reindex(n.links.index, fill_value=0)
    caps = fair_share_caps(n, p0)
    if caps.empty:
        return np.nan
    excess = (p0[caps.index] - caps) / caps.abs().clip(lower=1.0)
    return float(excess.max())


def consistency(networks: Networks, country: str) -> pd.DataFrame:
    """
    Return the quantities of the consistency checks for every run.

    Parameters
    ----------
    networks
        Solved networks per horizon per scenario.
    country
        Country code.

    Returns
    -------
    :
        Columns ``eu_total`` (t CO2 into ``co2 atmosphere``), ``retirement``
        (largest net change of an attribute store, MWh), ``national_lhs``
        (t CO2, as the national budget), ``evals_balance`` (t CO2, as the
        evals CO2 balance) and ``fair_share_excess`` (largest relative excess
        of a claim over its cap, NaN without fair share), indexed by
        ``scenario`` and ``horizon``.
    """

    def quantities(n: pypsa.Network) -> dict:
        flows = port_flows(n)
        return {
            "eu_total": _link_flows(flows, "co2").sum(),
            "retirement": _attribute_store_change(n),
            "national_lhs": national_co2_categories(n, country, flows).sum(),
            "evals_balance": _evals_balance(n, country),
            "fair_share_excess": _fair_share_excess(n),
        }

    return _country_frame(networks, quantities)


def _relative(a: pd.Series, b: pd.Series) -> pd.Series:
    return (a - b).abs() / np.maximum(a.abs(), b.abs()).clip(lower=1.0)


def consistency_checks(table: pd.DataFrame) -> list[Check]:
    """
    Evaluate the consistency checks P5, I2, I5 and I6.

    Parameters
    ----------
    table
        Output of :func:`consistency` for the selected scenarios.

    Returns
    -------
    :
        One check per test.
    """
    eu = table.eu_total.unstack("scenario")
    spread = eu.max(axis=1) - eu.min(axis=1)
    deviation = spread / eu.abs().max(axis=1).clip(lower=1.0)
    p5 = Check(
        "P5 EU total",
        "ok" if (deviation <= REL_TOL).all() else "fail",
        f"Largest relative spread of the EU total across runs: {deviation.max():.1e}.",
        (eu / MT).assign(**{"relative spread": deviation}),
    )

    retirement = table.retirement.unstack("scenario")
    i2 = Check(
        "I2 Retirement",
        "ok" if (retirement.abs() <= 1.0).all().all() else "fail",
        f"Largest net change of an attribute store: {retirement.abs().max().max():.2g} MWh.",
        retirement,
    )

    deviation = _relative(table.national_lhs, table.evals_balance)
    i5 = Check(
        "I5 National balance",
        "ok" if (deviation <= REL_TOL).all() else "fail",
        f"Largest relative deviation between the national budget LHS and the evals "
        f"balance: {deviation.max():.1e}.",
        (table[["national_lhs", "evals_balance"]] / MT).assign(deviation=deviation),
    )

    excess = table.fair_share_excess.dropna()
    if excess.empty:
        i6 = Check("I6 Fair share", "ok", "No run with fair share selected.")
    else:
        i6 = Check(
            "I6 Fair share",
            "ok" if (excess <= REL_TOL).all() else "fail",
            f"Largest relative excess of a claim over its cap: {excess.max():.1e}.",
            excess.unstack("scenario"),
        )
    return [p5, i2, i5, i6]


# --- cost of the national target -----------------------------------------------


def co2_price(n: pypsa.Network, constraint: str) -> float:
    """Return the CO2 price of a global constraint in EUR/t (minus its dual), NaN if missing."""
    gc = n.global_constraints
    return -gc.at[constraint, "mu"] if constraint in gc.index else np.nan


def credit_bounds(n: pypsa.Network, country: str) -> pd.DataFrame:
    """
    Return the credit bounds of a country with their flow, slack and dual.

    The duals of the carbon accounting constraints are not stored in the
    solved network. A bound counts as tight if its relative slack is below
    :data:`EPS_SLACK`. The dual of a tight bound is estimated as the value of
    one more MWh of attributes to the country, ``mu * credit - claim cost``,
    with ``mu`` the national CO2 price and ``credit`` the national credit per
    MWh (the emission factor, times the domestic aviation share for
    kerosene). Names follow the constraints in
    ``mods.constraints.carbon_accounting``.

    Parameters
    ----------
    n
        A solved network.
    country
        Country code.

    Returns
    -------
    :
        Columns ``flow`` and ``bound`` (MWh), ``slack`` (bound minus flow),
        ``tight``, ``credit`` (t CO2/MWh), ``claim cost`` (EUR/MWh) and
        ``dual`` (estimated, EUR/MWh, 0 unless tight), indexed by constraint
        name. Empty without claims.
    """
    weightings = n.snapshot_weightings.generators
    p0 = n.links_t.p0.mul(weightings, axis=0).sum().reindex(n.links.index, fill_value=0)
    alpha = domestic_aviation_share(n, country)
    claims = n.links[
        n.links.carrier.isin(CLAIM_KINDS) & n.links.index.str.startswith(country)
    ]
    credit = -claims.efficiency.where(claims.carrier != "renewable oil claim aviation")
    credit = credit.fillna(-claims.efficiency * alpha)
    rows = {}

    def add(name: str, link: str, bound: float, claim: str) -> None:
        cost = claims.at[claim, "marginal_cost"]
        rows[name] = (p0[link], bound, credit[claim], cost)

    consumption = claimable_consumption(n, p0)
    for link, carrier in claims.carrier.items():
        kind = CLAIM_KINDS[carrier].replace(" ", "-")
        bound = consumption.at[country, carrier] if country in consumption.index else 0
        add(f"carbon_accounting-claim-{country}-{kind}", link, bound, link)

    for link, cap in fair_share_caps(n, p0).items():
        if link.startswith(country):
            kind = CLAIM_KINDS[n.links.at[link, "carrier"]].replace(" ", "-")
            add(f"carbon_accounting-fair-share-{country}-{kind}", link, cap, link)

    location = n.buses.location.str[:2]
    transfers = n.links[n.links.carrier == TRANSFER_CARRIER]
    pipes = n.links[n.links.carrier.isin(GAS_TRANSPORT)]
    pipe_from, pipe_to = pipes.bus0.map(location), pipes.bus1.map(location)
    gas_claim = claims.index[claims.carrier == "renewable gas claim"]
    for link, transfer in transfers.iterrows():
        origin, destination = location[transfer.bus0], location[transfer.bus1]
        if destination != country or gas_claim.empty:
            continue
        idx = pipes.index[(pipe_from == origin) & (pipe_to == destination)]
        gas = (p0[idx] * pipes.loc[idx, "efficiency"]).sum()
        name = f"carbon_accounting-transfer-{origin}-{destination}"
        add(name, link, gas, gas_claim[0])

    columns = ["flow", "bound", "credit", "claim cost"]
    bounds = pd.DataFrame.from_dict(rows, orient="index", columns=columns)
    bounds["slack"] = bounds.bound - bounds.flow
    bounds["tight"] = (bounds.bound > 0) & (bounds.slack <= EPS_SLACK * bounds.bound)
    mu = co2_price(n, f"co2_limit-{country}")
    dual = mu * bounds.credit - bounds["claim cost"]
    bounds["dual"] = dual.where(bounds.tight, 0.0).fillna(0.0)
    return bounds


def pool_claims(n: pypsa.Network) -> pd.DataFrame:
    """
    Return inflow and claims of the pooled attributes, split by budget.

    Parameters
    ----------
    n
        A solved network.

    Returns
    -------
    :
        Columns ``inflow``, ``budget`` (claims of countries with a national
        budget) and ``other`` (claims of the other countries), in MWh per
        pooled fuel. Empty without pools.
    """
    weightings = n.snapshot_weightings.generators
    p0 = n.links_t.p0.mul(weightings, axis=0).sum().reindex(n.links.index, fill_value=0)
    budgets = list(n.meta["solving"]["constraints"]["co2_budget_national"] or {})
    claims = n.links[n.links.carrier.isin(FUEL_CLAIM_CARRIERS)]
    if claims.empty:
        return pd.DataFrame(columns=["inflow", "budget", "other"], dtype=float)
    fuel = claims.carrier.map(FUEL_CLAIM_CARRIERS)
    has_budget = claims.index.str[:2].isin(budgets)
    rows = {}
    for f in POOLED_FUELS:
        is_fuel = fuel == f
        rows[f] = {
            "inflow": pool_inflow(n, p0, f),
            "budget": p0[claims.index[is_fuel & has_budget]].sum(),
            "other": p0[claims.index[is_fuel & ~has_budget]].sum(),
        }
    return pd.DataFrame.from_dict(rows, orient="index")


def budget_regime(n: pypsa.Network, country: str) -> tuple[str, str]:
    """
    Classify how a country meets its national CO2 budget.

    The rules of the spec are checked in order: ``slack``, ``physical``,
    ``pool-scarce``, ``paper``. A credit bound binds if its estimated dual
    (see :func:`credit_bounds`) is at least :data:`EPS_BOUND`.

    The regime answers what one more t of budget reduction costs CT, and so
    how to read the national CO2 price ``mu``. The categories follow the
    cheapest source of the next credit: none needed (``slack``), none left in
    CT so CT must abate (``physical``), only from another budget country
    (``pool-scarce``), or spare credits at the claim cost (``paper``). The
    order matters: a binding bound of CT wins over a scarce pool, since CT
    cannot claim more even if the pool had spare attributes. ``paper`` is
    the fallback and not tested directly; ``mu`` next to the claim cost is
    its cross-check.

    The estimated dual assumes that an attribute costs its EU value
    ``EF * EU price``, which holds while countries without a budget take
    the rest of the pool. The exact dual would need the attribute price,
    which is not unique when every claim of a fuel sits at its bound.

    Parameters
    ----------
    n
        A solved network.
    country
        Country code.

    Returns
    -------
    :
        The regime and the binding bounds or the scarce pools, ``none`` if
        the country has no national budget.
    """
    mu = co2_price(n, f"co2_limit-{country}")
    if np.isnan(mu):
        return "none", "no national budget"
    if abs(mu) < EPS_MU:
        return "slack", ""
    if _method(n.meta)[0] == "technology":
        return "physical", "no credits under method technology"

    bounds = credit_bounds(n, country)
    binding = bounds.index[bounds.dual >= EPS_BOUND]
    if len(binding):
        return "physical", ", ".join(binding)

    pools = pool_claims(n)
    scarce = pools.index[(pools.inflow > 0) & (pools.other < EPS_POOL * pools.inflow)]
    if len(scarce):
        return "pool-scarce", ", ".join(f"{f} pool" for f in scarce)
    return "paper", ""


def country_cost(
    n: pypsa.Network, country: str, flows: pd.DataFrame | None = None
) -> pd.Series:
    """
    Return the cost carried by a country: asset cost plus net trade value.

    Assets are located with :func:`evals.utils.get_location`. Every port of an
    asset of the country at a bus outside the country, and every port of a
    foreign asset at a bus of the country, is a trade valued at the marginal
    price of that bus. Withdrawal is an import cost, injection an export
    revenue. ``co2 atmosphere`` and ``co2 import`` are accounting, not
    trade. Attributes are trade: a country pays for the attributes its
    claims draw from the pool and other countries. Where every claim sits at
    its bound, only the sum of the fuel and the attribute price is unique;
    valuing both keeps the cost unique.

    Parameters
    ----------
    n
        A solved network.
    country
        Country code.
    flows
        Port flows of ``n``, see :func:`port_flows`; computed if missing.

    Returns
    -------
    :
        ``capex``, ``opex``, ``trade`` and ``total`` in EUR.
    """
    flows = port_flows(n) if flows is None else flows
    capex = n.statistics.capex(groupby="location")
    opex = n.statistics.opex(groupby="location")
    in_country = {
        name: s[s.index.get_level_values("location").str[:2] == country].sum()
        for name, s in [("capex", capex), ("opex", opex)]
    }
    trade = flows[~flows.bus_carrier.isin(NON_TRADE_BUS_CARRIERS)]
    own = (trade.asset_country == country) & (trade.bus_country != country)
    foreign = (trade.asset_country != country) & (trade.bus_country == country)
    cost = pd.Series(
        {
            **in_country,
            "trade": trade.value[own].sum() - trade.value[foreign].sum(),
        }
    )
    cost["total"] = cost.sum()
    return cost


def system_cost(n: pypsa.Network) -> float:
    """Return the EU-wide capex plus opex in EUR."""
    return float(n.statistics.capex().sum() + n.statistics.opex().sum())


def target_cost(networks: Networks, country: str) -> pd.DataFrame:
    """
    Return the cost metrics of the national target for every run.

    Parameters
    ----------
    networks
        Solved networks per horizon per scenario.
    country
        Country code.

    Returns
    -------
    :
        Regime, prices (EUR/t), emissions (Mt CO2) and cost (bn EUR),
        indexed by ``scenario`` and ``horizon``. Differences to a reference
        are added by :func:`add_cost_deltas`.
    """

    def metrics(n: pypsa.Network) -> dict:
        flows = port_flows(n)
        balance = national_co2_categories(n, country, flows) / MT
        claims = -balance[list(CLAIM_CATEGORIES.values())].sum()
        pre_claim = balance.sum() + claims
        budget = national_budget(n, country) / MT
        effort = pre_claim - budget
        regime, reason = budget_regime(n, country)
        cost = country_cost(n, country, flows) / BN
        return {
            "regime": regime,
            "binding": reason,
            "national CO2 price": co2_price(n, f"co2_limit-{country}"),
            "claim cost": _method(n.meta)[3],
            "EU CO2 price": co2_price(n, EU_CO2_LIMIT),
            "pre-claim emissions": pre_claim,
            "claims": claims,
            "reported total": balance.sum(),
            "budget": budget,
            "credit-financed share": claims / effort if effort > 0 else np.nan,
            "system cost": system_cost(n) / BN,
            "cost in country": cost["total"],
            "capex in country": cost["capex"],
            "opex in country": cost["opex"],
            "trade balance": cost["trade"],
        }

    return _country_frame(networks, metrics)


def add_cost_deltas(table: pd.DataFrame, reference: str) -> pd.DataFrame:
    """
    Add the system cost and the cost in the country relative to a reference.

    Parameters
    ----------
    table
        Output of :func:`target_cost`.
    reference
        The reference scenario.

    Returns
    -------
    :
        A copy with the columns ``Δ system cost`` and ``Δ cost in country``.
    """
    table = table.copy()
    for column in ("system cost", "cost in country"):
        ref = table.xs(reference, level="scenario")[column]
        horizons = table.index.get_level_values("horizon")
        table[f"Δ {column}"] = table[column] - ref.reindex(horizons).to_numpy()
    return table


# --- physical system response --------------------------------------------------


def _technologies(n: pypsa.Network, country: str, flows: pd.DataFrame) -> list:
    links = flows[(flows.component == "Link") & (flows.asset_country == country)]
    in_country = n.links.index[get_location(n, "Link").str[:2] == country]
    rows = []
    for name, (carriers, product) in TECHNOLOGIES.items():
        idx = in_country[n.links.loc[in_country, "carrier"].isin(carriers)]
        capacity = n.links.loc[idx, "p_nom_opt"].sum()
        at_product = links.carrier.isin(carriers) & (links.bus_carrier == product)
        output = -links.energy[at_product].sum()
        unit = "Mt" if product.startswith("co2") else "TWh"
        rows.append(("capacity", name, "GW", capacity / 1e3))
        rows.append(("output", name, unit, output / (MT if unit == "Mt" else TWH)))
    return rows


def _crosses_border(flows: pd.DataFrame) -> pd.Series:
    """Return per component whether its physical ports lie in different countries."""
    physical = flows[
        ~flows.bus_carrier.isin(ACCOUNTING_BUS_CARRIERS) & (flows.bus_country != "EU")
    ]
    countries = physical.groupby(["component", "name"]).bus_country.nunique()
    return countries > 1


def _fuel_balances(country: str, flows: pd.DataFrame) -> list:
    crosses = _crosses_border(flows)
    key = pd.MultiIndex.from_frame(flows[["component", "name"]])
    is_border = crosses.reindex(key, fill_value=False).to_numpy()
    rows = []
    for fuel in FUEL_BALANCES:
        at_fuel = flows.bus_carrier == fuel
        at_home = at_fuel & (flows.bus_country == country)
        at_pool = (
            at_fuel & (flows.bus_country == "EU") & (flows.asset_country == country)
        )
        selected = flows[at_home | at_pool]
        label = selected.carrier.where(~is_border[at_home | at_pool], "border flows")
        balance = -selected.energy.groupby(label).sum() / TWH
        balance = balance[balance.abs() > 1e-6]
        closing = -balance.sum()
        if abs(closing) > 1e-6:
            balance["EU pool (net)"] = closing
        rows += [
            ("fuel " + fuel, item, "TWh", value) for item, value in balance.items()
        ]
    return rows


def _co2(n: pypsa.Network, country: str, flows: pd.DataFrame) -> list:
    own = flows[(flows.component == "Link") & (flows.asset_country == country)]
    stored = own[own.bus_carrier == "co2 stored"]
    captured = -stored.energy[
        stored.carrier.isin(CAPTURE + GREEN_BIO_PRODUCERS) & (stored.energy < 0)
    ].sum()
    used = stored.energy[stored.carrier.isin(GREEN_SYNTHETIC_PRODUCERS)].sum()
    sequestered = -own.energy[own.bus_carrier == "co2 sequestered"].sum()
    compressed = -own.energy[
        (own.carrier == "co2 compression") & (own.bus_carrier == "co2 dense")
    ].sum()
    crosses = _crosses_border(flows)
    pipes = flows[
        (flows.carrier == CO2_TRANSPORT)
        & (flows.bus_country == country)
        & crosses.reindex(
            pd.MultiIndex.from_frame(flows[["component", "name"]]), fill_value=False
        ).to_numpy()
    ]
    exported = pipes.energy.sum()
    return [
        ("co2", "captured", "Mt", captured / MT),
        ("co2", "used for CCU", "Mt", used / MT),
        ("co2", "compressed", "Mt", compressed / MT),
        ("co2", "sequestered", "Mt", sequestered / MT),
        ("co2", "net export via CO2 pipelines", "Mt", exported / MT),
    ]


def _borders(country: str, flows: pd.DataFrame) -> list:
    rows = []
    for group, carriers in [
        ("border gas", GAS_TRANSPORT),
        ("border gas attributes", (TRANSFER_CARRIER,)),
    ]:
        links = flows[
            (flows.component == "Link")
            & flows.carrier.isin(carriers)
            & flows.port.isin(["0", "1"])
        ]
        if links.empty:
            continue
        ends = links.pivot(index="name", columns="port", values="bus_country")
        crossing = ends.index[(ends["0"] != ends["1"]) & (ends == country).any(axis=1)]
        at_home = links[links.name.isin(crossing) & (links.bus_country == country)]
        neighbour = at_home.name.map(ends["0"].where(ends["0"] != country, ends["1"]))
        # injection at the home bus is an import, withdrawal an export
        imported = (-at_home.energy.clip(upper=0.0)).groupby(neighbour).sum()
        exported = at_home.energy.clip(lower=0.0).groupby(neighbour).sum()
        for ct in sorted(set(imported.index) | set(exported.index)):
            rows.append(
                (group, f"import from {ct}", "TWh", imported.get(ct, 0.0) / TWH)
            )
            rows.append((group, f"export to {ct}", "TWh", exported.get(ct, 0.0) / TWH))
    return rows


def physical_response(networks: Networks, country: str) -> pd.DataFrame:
    """
    Return the physical system of a country for every run.

    Covers capacities and output of green fuel and capture technologies,
    the fuel balances of gas, oil, methanol and H2, the CO2 chain and the
    gas flows and gas attribute transfers per border.

    Parameters
    ----------
    networks
        Solved networks per horizon per scenario.
    country
        Country code.

    Returns
    -------
    :
        Column ``value``, indexed by ``scenario``, ``horizon``, ``group``,
        ``item`` and ``unit``.
    """
    frames = []
    for scenario, horizons in networks.items():
        for horizon, n in horizons.items():
            flows = port_flows(n)
            rows = (
                _technologies(n, country, flows)
                + _fuel_balances(country, flows)
                + _co2(n, country, flows)
                + _borders(country, flows)
            )
            frame = pd.DataFrame(rows, columns=["group", "item", "unit", "value"])
            frames.append(frame.assign(scenario=scenario, horizon=horizon))
    response = pd.concat(frames, ignore_index=True)
    return response.set_index(["scenario", "horizon", "group", "item", "unit"]).value


def difference_table(
    values: pd.Series, reference: str, cutoff: float = 0.0
) -> pd.DataFrame:
    """
    Pivot a statistic to one column per scenario and add the differences.

    Parameters
    ----------
    values
        A series with a ``scenario`` level.
    reference
        The reference scenario.
    cutoff
        Rows whose absolute values are at most this in every scenario are
        dropped.

    Returns
    -------
    :
        One column per scenario, then ``Δ {scenario}`` for every other
        scenario. Rows missing in a run count as 0.
    """
    table = values.unstack("scenario").fillna(0.0)
    scenarios = [reference] + [s for s in table.columns if s != reference]
    table = table.loc[table.abs().gt(cutoff).any(axis=1), scenarios]
    for scenario in scenarios[1:]:
        table[f"Δ {scenario}"] = table[scenario] - table[reference]
    return table


def summary(bridge: pd.DataFrame, cost: pd.DataFrame) -> pd.DataFrame:
    """
    Return the summary table of the notebook.

    Parameters
    ----------
    bridge
        Output of :func:`balance_bridge`.
    cost
        Output of :func:`add_cost_deltas`.

    Returns
    -------
    :
        Pre-claim emissions, claims, reported total and budget (Mt CO2), and
        the cost differences to the reference (bn EUR), indexed by
        ``scenario`` and ``horizon``.
    """
    claims = -bridge[list(CLAIM_CATEGORIES.values())].sum(axis=1)
    return pd.DataFrame(
        {
            "pre-claim emissions [Mt]": bridge.Total + claims,
            "claims [Mt]": claims,
            "reported total [Mt]": bridge.Total,
            "budget [Mt]": bridge.Budget,
            "Δ EU system cost [bn €]": cost["Δ system cost"],
            "Δ cost in country [bn €]": cost["Δ cost in country"],
        }
    )
