# SPDX-FileCopyrightText: 2023-2026 Austrian Gas Grid Management AG
#
# SPDX-License-Identifier: MIT
# For license information, see the LICENSE.txt file in the project root.
"""
Evaluate the emissions of any run under every carbon accounting method.

Backs the notebook ``evals/notebooks/carbon_accounting_pathways.py``. A run is
optimised under one method (its *run method*), but its physical flows can be
evaluated ex post under any method (the *evaluator*):

``technology``
    The technology that releases CO2 books it; green fuel producers book
    ``-EF`` at creation. A pure function of the physical flows, so it applies
    to every run. For carrier runs, the transformation of the spec (section
    3.2) is inverted: ``atm_technology = atm_carrier - EF_out * out - used``,
    green imports draw ``p0`` from the atmosphere, and claims vanish.
``carrier (optimised)``
    The emissions as booked in the run, with the claims chosen by the
    optimiser. Only defined for runs with the method ``carrier``.
``carrier (EU average)``
    The carrier rules applied ex post, with an allocation rule for the green
    attributes: every consumer of a fuel gets the EU-wide green share of that
    fuel. Applies to every run.

Claims are spread onto the consuming links of the claiming country in
proportion to their consumption, so emissions can be shown by sector. The
national totals stay those of the national CO2 budget (name prefix, kerosene
and kerosene claims scaled by the domestic aviation share).
"""

from collections.abc import Mapping

import numpy as np
import pandas as pd
import pypsa

from evals.carbon_accounting import (
    GAS_TRANSPORT,
    GREEN_BIO_PRODUCERS,
    GREEN_IMPORTS,
    GREEN_SYNTHETIC_PRODUCERS,
    MT,
    REL_TOL,
    VENT,
    Check,
    _method,
    domestic_aviation_share,
    national_budget,
    national_co2_categories,
    port_flows,
)
from evals.constants import AVIATION_CARRIERS, FUEL_CLAIM_CARRIERS

Networks = Mapping[str, Mapping[str, pypsa.Network]]

TECHNOLOGY = "technology"
CARRIER_OPTIMISED = "carrier (optimised)"
CARRIER_AVERAGE = "carrier (EU average)"
EVALUATORS: tuple[str, ...] = (TECHNOLOGY, CARRIER_OPTIMISED, CARRIER_AVERAGE)
FUELS: tuple[str, ...] = ("gas", "oil", "methanol")
GREEN_PRODUCERS: tuple[str, ...] = (
    *GREEN_BIO_PRODUCERS,
    *GREEN_SYNTHETIC_PRODUCERS,
    *GREEN_IMPORTS,
)
EU = "EU"

# sectors of the pathway charts, checked in order; the first match wins
SECTORS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Fuel supply", (*GREEN_PRODUCERS, "oil refining")),
    ("DAC & vent", ("DAC", *VENT)),
    ("Transport", ("land transport", "shipping", "aviation", "methanol-to-kerosene")),
    ("Agriculture", ("agriculture",)),
    ("Waste", ("waste", "municipal solid waste")),
    ("Heat", ("boiler", "CHP")),
    (
        "Industry",
        ("industry", "process emissions", "HVC", "naphtha", "SMR", "steam reforming"),
    ),
    ("Power", ("CCGT", "OCGT", "coal", "lignite", "nuclear", "allam", "oil")),
)


def sector(carrier: str) -> str:
    """Return the sector of a link carrier, ``Other`` if none matches."""
    for name, keys in SECTORS:
        if carrier in keys or any(key in carrier for key in keys):
            return name
    return "Other"


def emission_factors(n: pypsa.Network) -> dict[str, float]:
    """
    Return the emission factors of gas, oil and methanol in t/MWh.

    Green imports convert one t of CO2 into ``1 / EF`` MWh of fuel
    (spec section 3.4), so the factors are read from their efficiency.

    Parameters
    ----------
    n
        A solved network.

    Returns
    -------
    :
        Emission factor per fuel.

    Raises
    ------
    ValueError
        If a fuel has no import link or the import links disagree.
    """
    ef = {}
    for fuel in FUELS:
        efficiency = n.links.efficiency[n.links.carrier == f"import {fuel}"]
        if efficiency.empty or not np.allclose(efficiency, efficiency.iloc[0]):
            raise ValueError(f"Cannot read the emission factor of {fuel}.")
        ef[fuel] = 1 / efficiency.iloc[0]
    return ef


def link_table(n: pypsa.Network, flows: pd.DataFrame | None = None) -> pd.DataFrame:
    """
    Return the annual quantities that define the emissions of every link.

    Parameters
    ----------
    n
        A solved network.
    flows
        Port flows of ``n``, see :func:`evals.carbon_accounting.port_flows`.

    Returns
    -------
    :
        One row per link with ``carrier``, ``country`` (name prefix),
        ``sector``, ``native`` (t CO2 booked at ``co2 atmosphere`` in the
        run), ``fuel_out`` and ``out`` (green fuel output of producers, MWh),
        ``used`` (CO2 taken from ``co2 stored`` by synthetic fuel producers,
        t), ``p0`` (MWh or t), ``fuel_in`` (the fuel consumed at ``bus0``, empty
        for transport) and ``claim`` (the claim kind of consumers and claim
        links, e.g. ``renewable oil claim aviation``).
    """
    flows = port_flows(n) if flows is None else flows
    flows = flows[flows.component == "Link"]
    links = n.links
    table = pd.DataFrame(
        {
            "carrier": links.carrier,
            "country": links.index.str[:2],
            "sector": links.carrier.map(sector),
        },
        index=links.index,
    )
    by_link = flows.groupby(["name", "bus_carrier"]).energy.sum().unstack(fill_value=0)
    by_link = by_link.reindex(links.index, fill_value=0.0)
    table["native"] = -by_link.get("co2", 0.0)
    table["p0"] = (
        flows[flows.port == "0"].groupby("name").energy.sum().reindex(links.index)
    ).fillna(0.0)

    is_producer = links.carrier.isin(GREEN_PRODUCERS)
    output = -by_link.reindex(columns=list(FUELS), fill_value=0.0).clip(upper=0.0)
    table["fuel_out"] = output.idxmax(axis=1).where(
        is_producer & output.gt(0).any(axis=1), ""
    )
    table["out"] = output.max(axis=1).where(table.fuel_out != "", 0.0)
    is_synthetic = links.carrier.isin(GREEN_SYNTHETIC_PRODUCERS)
    table["used"] = (
        by_link.get("co2 stored", 0.0).clip(lower=0.0).where(is_synthetic, 0.0)
    )

    fuel_in = links.bus0.map(n.buses.carrier)
    is_consumer = fuel_in.isin(FUELS) & ~links.carrier.isin(GAS_TRANSPORT)
    table["fuel_in"] = fuel_in.where(is_consumer, "")
    claim = ("renewable " + fuel_in + " claim").where(is_consumer, "")
    claim = claim.where(
        links.carrier != "kerosene for aviation", "renewable oil claim aviation"
    )
    is_claim = links.carrier.isin(FUEL_CLAIM_CARRIERS)
    table["claim"] = claim.where(~is_claim, links.carrier)
    table["is_claim"] = is_claim
    # links transformed by the carrier method (marker of mods.network.carbon_accounting)
    marker = links.get("carbon_accounting", pd.Series("", index=links.index))
    table["transformed"] = marker.fillna("").eq("carrier")
    return table


def technology_emissions(table: pd.DataFrame, ef: dict[str, float]) -> pd.Series:
    """
    Return the emissions of every link under the method ``technology``, in t.

    Inverts the carrier transformation where it was applied; links of a
    technology run come out unchanged.

    Parameters
    ----------
    table
        Output of :func:`link_table`.
    ef
        Emission factor per fuel.

    Returns
    -------
    :
        Emissions per link, claims are 0.
    """
    carrier = table.carrier
    creation = table.fuel_out.map(ef).fillna(0.0) * table.out
    bio = carrier.isin(GREEN_BIO_PRODUCERS) & table.transformed
    synthetic = carrier.isin(GREEN_SYNTHETIC_PRODUCERS) & table.transformed
    emissions = table.native.copy()
    emissions[bio] -= creation[bio]
    emissions[synthetic] -= table.used[synthetic]
    # green imports draw their carbon from the atmosphere
    imports = carrier.isin(GREEN_IMPORTS)
    emissions[imports] = -table.p0[imports]
    emissions[table.is_claim] = 0.0
    return emissions


def carrier_emissions(table: pd.DataFrame, ef: dict[str, float]) -> pd.Series:
    """
    Return the emissions of every link under the carrier rules, before claims.

    Bio-fuel producers lose their creation credit, synthetic fuel producers
    book the CO2 they use and green imports book zero (spec section 3.2).

    Parameters
    ----------
    table
        Output of :func:`link_table`.
    ef
        Emission factor per fuel.

    Returns
    -------
    :
        Emissions per link in t, claims are 0.
    """
    emissions = technology_emissions(table, ef)
    carrier = table.carrier
    is_bio = carrier.isin(GREEN_BIO_PRODUCERS)
    creation = table.fuel_out.map(ef).fillna(0.0) * table.out
    emissions[is_bio] += creation[is_bio]
    is_synthetic = carrier.isin(GREEN_SYNTHETIC_PRODUCERS)
    emissions[is_synthetic] += table.used[is_synthetic]
    emissions[carrier.isin(GREEN_IMPORTS)] = 0.0
    return emissions


def green_shares(table: pd.DataFrame, rule: str) -> pd.Series:
    """
    Return the share of green fuel that each consumer link claims.

    Parameters
    ----------
    table
        Output of :func:`link_table`.
    rule
        ``optimised``: the claims of the run, spread over the consumers of
        the claiming country in proportion to their consumption.
        ``average``: the EU-wide green share of each fuel for every consumer.

    Returns
    -------
    :
        Share between 0 and 1 per consumer link, 0 elsewhere.
    """
    consumers = table[table.fuel_in != ""]
    consumption = consumers.p0.groupby([consumers.country, consumers.claim]).sum()
    if rule == "optimised":
        claims = table[table.is_claim]
        claimed = claims.p0.groupby([claims.country, claims.carrier]).sum()
        claimed.index.names = consumption.index.names
        share = (claimed / consumption.replace(0.0, np.nan)).reindex(consumption.index)
        keys = pd.MultiIndex.from_frame(consumers[["country", "claim"]])
        values = share.reindex(keys).fillna(0.0).to_numpy()
    elif rule == "average":
        green = table[table.fuel_out != ""].out.groupby(table.fuel_out).sum()
        total = consumers.p0.groupby(consumers.fuel_in).sum()
        share = (green / total.replace(0.0, np.nan)).reindex(list(FUELS)).fillna(0.0)
        values = consumers.fuel_in.map(share).to_numpy()
    else:
        raise ValueError(f"Unknown allocation rule '{rule}'.")
    return pd.Series(values, index=consumers.index).reindex(table.index, fill_value=0.0)


def evaluate(
    n: pypsa.Network,
    evaluator: str,
    table: pd.DataFrame | None = None,
    scaled: bool = True,
) -> pd.Series:
    """
    Return the emissions of every link of a run under an evaluator, in t.

    Kerosene for aviation and its claims are scaled by the domestic aviation
    share of the link's country, as in the national CO2 budget.

    Parameters
    ----------
    n
        A solved network.
    evaluator
        One of :data:`EVALUATORS`.
    table
        Output of :func:`link_table`; computed if missing.
    scaled
        Whether to scale aviation by the domestic share; ``False`` for EU
        totals.

    Returns
    -------
    :
        Emissions per link, all NaN for ``carrier (optimised)`` on a run
        without claims.
    """
    table = link_table(n) if table is None else table
    ef = emission_factors(n)
    if evaluator == TECHNOLOGY:
        emissions = technology_emissions(table, ef)
    elif evaluator in (CARRIER_OPTIMISED, CARRIER_AVERAGE):
        if evaluator == CARRIER_OPTIMISED and not table.is_claim.any():
            return pd.Series(np.nan, index=table.index)
        rule = "optimised" if evaluator == CARRIER_OPTIMISED else "average"
        share = green_shares(table, rule)
        credit = table.fuel_in.map(ef).fillna(0.0) * table.p0 * share
        emissions = carrier_emissions(table, ef) - credit
    else:
        raise ValueError(f"Unknown evaluator '{evaluator}'.")
    if not scaled:
        return emissions

    is_aviation = table.carrier.isin(AVIATION_CARRIERS)
    alpha = {
        ct: domestic_aviation_share(n, ct) for ct in table.country[is_aviation].unique()
    }
    scale = table.country.map(alpha).where(is_aviation, 1.0).fillna(1.0)
    return emissions * scale


def emissions(networks: Networks) -> pd.DataFrame:
    """
    Return the emissions of every run under every evaluator, by country and sector.

    Parameters
    ----------
    networks
        Solved networks per horizon per scenario.

    Returns
    -------
    :
        Columns ``run method`` and ``emissions`` (Mt CO2), indexed by
        ``scenario``, ``horizon``, ``evaluator``, ``country`` and ``sector``.
        The country ``EU`` holds the links without a country prefix. Empty
        rows for ``carrier (optimised)`` on technology runs are left out.
    """
    frames = []
    for scenario, horizons in networks.items():
        for horizon, n in horizons.items():
            table = link_table(n)
            for evaluator in EVALUATORS:
                values = evaluate(n, evaluator, table)
                if values.isna().all():
                    continue
                grouped = values.groupby([table.country, table.sector]).sum() / MT
                frame = grouped.rename("emissions").reset_index()
                frames.append(
                    frame.assign(
                        scenario=scenario,
                        horizon=horizon,
                        evaluator=evaluator,
                        **{"run method": _method(n.meta)[0]},
                    )
                )
    frame = pd.concat(frames, ignore_index=True)
    index = ["scenario", "horizon", "evaluator", "country", "sector"]
    return frame.set_index(index)[["run method", "emissions"]]


def budgets(networks: Networks, countries: list[str]) -> pd.DataFrame:
    """Return the national CO2 budgets in Mt, indexed by ``horizon``, one column per country."""
    first = next(iter(networks.values()))
    return pd.DataFrame(
        {
            ct: {h: national_budget(n, ct) / MT for h, n in first.items()}
            for ct in countries
        }
    ).rename_axis("horizon")


def national_totals(frame: pd.DataFrame, country: str) -> pd.DataFrame:
    """
    Return the national totals of a country, one column per evaluator.

    Parameters
    ----------
    frame
        Output of :func:`emissions`.
    country
        Country code.

    Returns
    -------
    :
        Mt CO2, indexed by ``scenario``, ``run method`` and ``horizon``;
        NaN where an evaluator does not apply.
    """
    rows = frame.xs(country, level="country")
    totals = rows.groupby(
        ["scenario", "run method", "horizon", "evaluator"]
    ).emissions.sum()
    return totals.unstack("evaluator").reindex(columns=list(EVALUATORS))


def own_method(totals: pd.DataFrame) -> pd.Series:
    """
    Return the national totals of every run under its own method.

    Carrier runs are evaluated with their optimised claims, technology runs
    with the technology method.

    Parameters
    ----------
    totals
        Output of :func:`national_totals`.

    Returns
    -------
    :
        Mt CO2 indexed like ``totals``.
    """
    method = totals.index.get_level_values("run method")
    return totals[TECHNOLOGY].where(method == "technology", totals[CARRIER_OPTIMISED])


def checks(
    networks: Networks, frame: pd.DataFrame, countries: list[str]
) -> list[Check]:
    """
    Check the evaluators against the runs.

    - The technology evaluator reproduces every link of a technology run.
    - The optimised carrier evaluator reproduces the national budget LHS of
      carrier runs (spreading the claims over consumers keeps the totals).
    - All evaluators give the same EU total per run (P5); computed without
      the aviation scaling, which is national.

    Parameters
    ----------
    networks
        Solved networks per horizon per scenario.
    frame
        Output of :func:`emissions`.
    countries
        Countries whose national totals are checked.

    Returns
    -------
    :
        One check per test.
    """
    link_error, national, eu = {}, {}, {}
    for scenario, horizons in networks.items():
        for horizon, n in horizons.items():
            table = link_table(n)
            ef = emission_factors(n)
            key = (scenario, horizon)
            if _method(n.meta)[0] == "technology":
                technology = technology_emissions(table, ef)
                link_error[key] = (technology - table.native).abs().max()
            else:
                for ct in countries:
                    lhs = national_co2_categories(n, ct).sum() / MT
                    total = frame.loc[
                        (scenario, horizon, CARRIER_OPTIMISED, ct)
                    ].emissions.sum()
                    national[(*key, ct)] = abs(lhs - total) / max(abs(lhs), 1.0)
            unscaled = {}
            for evaluator in EVALUATORS:
                values = evaluate(n, evaluator, table, scaled=False)
                if not values.isna().all():
                    unscaled[evaluator] = values.sum()
            spread = max(unscaled.values()) - min(unscaled.values())
            eu[key] = spread / max(abs(unscaled[TECHNOLOGY]), 1.0)

    def check(name: str, values: dict, tolerance: float, what: str) -> Check:
        if not values:
            return Check(name, "ok", f"Nothing to check: no {what}.")
        series = pd.Series(values)
        status = "ok" if (series <= tolerance).all() else "fail"
        return Check(
            name,
            status,
            f"Largest deviation: {series.max():.1e}.",
            series.to_frame("deviation"),
        )

    return [
        check(
            "Technology evaluator = technology runs (t per link)",
            link_error,
            1e-3,
            "technology runs",
        ),
        check(
            "Optimised carrier evaluator = national budget LHS",
            national,
            REL_TOL,
            "carrier runs",
        ),
        check("Same EU total under every evaluator (P5)", eu, REL_TOL, "runs"),
    ]


def merge_checks(results: list[Check]) -> Check:
    """
    Merge the results of one check over several scenarios.

    Scenarios with nothing to check (no details) are left out, so a carrier
    run is not hidden behind a technology run that skipped the check.

    Parameters
    ----------
    results
        One result of the same check per scenario, see :func:`checks`.

    Returns
    -------
    :
        Failed if any scenario failed, with the details of all scenarios.
    """
    checked = [c for c in results if not c.details.empty]
    if not checked:
        return results[0]
    details = pd.concat([c.details for c in checked])
    return Check(
        results[0].name,
        "fail" if any(c.failed for c in checked) else "ok",
        f"Largest deviation: {details.deviation.max():.1e}.",
        details,
    )


def against_budget(totals: pd.DataFrame, budget: pd.Series) -> pd.DataFrame:
    """
    Return the national totals minus the national budget of the horizon.

    Parameters
    ----------
    totals
        Output of :func:`national_totals`.
    budget
        Budget in Mt per horizon, a column of :func:`budgets`.

    Returns
    -------
    :
        Mt CO2 above (positive) or below (negative) the budget, like ``totals``.
    """
    horizons = totals.index.get_level_values("horizon")
    return totals.sub(budget.reindex(horizons).to_numpy(), axis=0)


def sector_pathway(
    frame: pd.DataFrame,
    country: str,
    scenario: str,
    evaluator: str,
    cutoff: float = 0.0,
) -> pd.DataFrame:
    """
    Return the emissions of a country by sector over the horizons.

    Parameters
    ----------
    frame
        Output of :func:`emissions`.
    country
        Country code.
    scenario
        The run.
    evaluator
        One of :data:`EVALUATORS`.
    cutoff
        Sectors whose absolute emissions are at most this in every horizon
        are left out.

    Returns
    -------
    :
        Mt CO2, indexed by ``horizon``, one column per sector in the order of
        :data:`SECTORS`.
        Empty if the evaluator does not apply to the run.
    """
    index = frame.index
    rows = frame[
        (index.get_level_values("scenario") == scenario)
        & (index.get_level_values("evaluator") == evaluator)
        & (index.get_level_values("country") == country)
    ]
    pathway = rows.emissions.groupby(["horizon", "sector"]).sum().unstack("sector")
    order = [name for name, _ in SECTORS] + ["Other"]
    pathway = pathway.reindex(columns=[s for s in order if s in pathway.columns])
    return pathway.loc[:, pathway.abs().gt(cutoff).any()].fillna(0.0)


def reference_run(totals: pd.DataFrame) -> str:
    """Return the first run optimised with the method ``technology``, else the first run."""
    methods = totals.index.droplevel("horizon").unique()
    technology = [s for s, m in methods if m == "technology"]
    return technology[0] if technology else methods[0][0]
