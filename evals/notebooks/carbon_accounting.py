# SPDX-FileCopyrightText: 2023-2026 Austrian Gas Grid Management AG
#
# SPDX-License-Identifier: MIT
# For license information, see the LICENSE.txt file in the project root.
"""
Carbon accounting comparison notebook (marimo).

Compares any number of runs of the carbon accounting configuration for one
country. All computations live in ``evals/carbon_accounting.py``; this file
only does UI, caching and plotting. Run it from the repository root with

    pixi run marimo edit evals/notebooks/carbon_accounting.py

Specification: ``docs-at/explanations/carbon-accounting-eval-spec.md``.
"""

import marimo

__generated_with = "0.25.0"
app = marimo.App(width="full", app_title="Carbon accounting comparison")


@app.cell
def _():
    import sys
    from pathlib import Path

    import marimo as mo
    import pandas as pd
    import plotly.express as px
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    REPO = Path(__file__).resolve().parents[2]
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))

    from evals import carbon_accounting as ca

    return REPO, ca, go, make_subplots, mo, pd, px


@app.cell
def _(go, mo):
    # categorical slots in fixed order; a scenario keeps its colour across selections
    PALETTE = [
        "#2a78d6",
        "#eb6834",
        "#1baf7a",
        "#eda100",
        "#e87ba4",
        "#008300",
        "#6250d6",
        "#e34948",
    ]
    GROUP_COLOURS = {"total": "#8a8984", "physical": "#2a78d6", "accounting": "#eb6834"}
    ICONS = {"ok": "✓", "warn": "⚠", "fail": "✗"}
    KINDS = {"ok": "success", "warn": "warn", "fail": "danger"}

    def badge(check) -> mo.Html:
        """Return a callout with the status icon, name and message of a check."""
        text = mo.md(f"**{ICONS[check.status]} {check.name}** — {check.message}")
        return mo.callout(text, kind=KINDS[check.status])

    def step_status(checks) -> str:
        """Return the worst status of several checks."""
        statuses = {c.status for c in checks}
        return next((s for s in ("fail", "warn") if s in statuses), "ok")

    def add_waterfall(fig, steps, row=1, col=1, legend=True):
        """Add the bars of a waterfall (see ``ca.waterfall``) to a figure."""
        for group, colour in GROUP_COLOURS.items():
            part = steps[steps.group == group]
            fig.add_trace(
                go.Bar(
                    x=part.step,
                    y=part.end - part.base,
                    base=part.base,
                    name=group,
                    legendgroup=group,
                    showlegend=legend,
                    marker=dict(color=colour, line=dict(width=0)),
                    customdata=part.value,
                    hovertemplate="%{x}<br>%{customdata:+.2f} Mt<extra></extra>",
                ),
                row=row,
                col=col,
            )
        fig.add_hline(
            y=steps.attrs["budget"],
            line=dict(color="#0b0b0b", dash="dash", width=1),
            annotation_text="budget",
            annotation_position="top left",
            row=row,
            col=col,
        )
        fig.update_xaxes(
            categoryorder="array", categoryarray=list(steps.step), row=row, col=col
        )

    def layout(fig, title: str, yaxis: str):
        fig.update_layout(
            title=title,
            template="plotly_white",
            bargap=0.3,
            legend=dict(orientation="h", y=-0.25),
            hovermode="closest",
            margin=dict(t=60, b=40),
        )
        fig.update_yaxes(title_text=yaxis, zeroline=True, zerolinecolor="#c3c2b7")
        return fig

    return PALETTE, add_waterfall, badge, layout, step_status


@app.cell
def _(mo):
    mo.md(r"""
    # Carbon accounting comparison

    Compares runs of the carbon accounting configuration for one country. Every Δ is
    measured against the **reference** scenario. Steps that fail a check stop the
    steps after them. Specification: `docs-at/explanations/carbon-accounting-eval-spec.md`.
    """)
    return


@app.cell
def _(REPO, mo):
    root_input = mo.ui.text(
        value=str(REPO / "results" / "carbon-accounting"),
        label="Result root",
        full_width=True,
    )
    root_input
    return (root_input,)


@app.cell
def _(PALETTE, ca, mo, root_input):
    files = ca.find_scenarios(root_input.value)
    mo.stop(
        not files,
        mo.callout(
            mo.md(f"✗ No solved networks found in `{root_input.value}`."), kind="danger"
        ),
    )
    info = ca.run_info(files)
    colours = {s: PALETTE[i % len(PALETTE)] for i, s in enumerate(files)}
    scenario_select = mo.ui.multiselect(
        options=list(files), value=list(files), label="Scenarios"
    )
    countries = sorted({c for b in info.budgets for c in b.split(", ") if c} | {"AT"})
    country_select = mo.ui.dropdown(options=countries, value="AT", label="Country")
    return colours, country_select, files, info, scenario_select


@app.cell
def _(ca, info, mo, scenario_select):
    _selected = list(scenario_select.value) or list(info.index.unique("scenario"))
    _default = ca.default_reference(info.loc[_selected])
    reference_select = mo.ui.dropdown(
        options=_selected, value=_default, label="Reference"
    )
    return (reference_select,)


@app.cell
def _(badge, ca, country_select, info, mo, reference_select, scenario_select):
    setup = ca.check_setup(info, list(scenario_select.value), country_select.value)
    mo.output.replace(
        mo.vstack(
            [
                mo.md("## 0 Setup"),
                mo.hstack(
                    [scenario_select, reference_select, country_select],
                    justify="start",
                    gap=2,
                ),
                badge(setup),
                mo.ui.table(
                    info.loc[list(scenario_select.value)]
                    .drop(columns="file")
                    .reset_index(),
                    selection=None,
                    label="Networks per scenario and horizon",
                ),
            ]
        )
    )
    mo.stop(setup.failed)
    selected = list(scenario_select.value)
    reference = reference_select.value
    country = country_select.value
    return country, reference, selected


@app.cell
def _(ca, mo):
    @mo.cache
    def load(path: str) -> dict:
        """Load the solved networks of one scenario, once."""
        return ca.load_networks(path)

    @mo.cache
    def scenario_frames(path: str, scenario: str, country: str) -> dict:
        """Compute every statistic of one scenario, once per country."""
        networks = {scenario: load(path)}
        return {
            "bridge": ca.balance_bridge(networks, country),
            "consistency": ca.consistency(networks, country),
            "target": ca.target_cost(networks, country),
            "physical": ca.physical_response(networks, country),
            "surplus": ca.green_surpluses(networks, country),
            "transfers": ca.implicit_transfers(networks),
            "values": ca.attribute_values(networks, country),
        }

    return (scenario_frames,)


@app.cell
def _(country, files, mo, pd, scenario_frames, selected):
    _frames = {}
    for _scenario in mo.status.progress_bar(
        selected, title="Loading and evaluating scenarios", remove_on_exit=True
    ):
        _path = str(files[_scenario][next(iter(files[_scenario]))].parents[1])
        _frames[_scenario] = scenario_frames(_path, _scenario, country)
    bridge = pd.concat([f["bridge"] for f in _frames.values()])
    consistency = pd.concat([f["consistency"] for f in _frames.values()])
    target = pd.concat([f["target"] for f in _frames.values()])
    physical = pd.concat([f["physical"] for f in _frames.values()])
    surplus = pd.concat([f["surplus"] for f in _frames.values()])
    transfers = pd.concat([f["transfers"] for f in _frames.values()])
    values = pd.concat([f["values"] for f in _frames.values()])
    horizons = sorted(bridge.index.unique("horizon"))
    return (
        bridge,
        consistency,
        horizons,
        physical,
        surplus,
        target,
        transfers,
        values,
    )


@app.cell
def _(badge, ca, consistency, mo, step_status):
    checks = ca.consistency_checks(consistency)
    _status = step_status(checks)
    mo.output.replace(
        mo.vstack(
            [
                mo.md(f"## 1 Consistency {dict(ok='✓', warn='⚠', fail='✗')[_status]}"),
                *[badge(c) for c in checks],
                mo.accordion(
                    {
                        c.name: mo.ui.table(
                            c.details.reset_index(), selection=None, show_download=False
                        )
                        for c in checks
                        if not c.details.empty
                    }
                ),
            ]
        )
    )
    mo.stop(_status == "fail")
    consistent = True
    return (consistent,)


@app.cell
def _(consistent, horizons, mo, reference, selected):
    assert consistent
    horizon_select = mo.ui.dropdown(
        options=horizons, value=horizons[-1], label="Horizon"
    )
    _others = [s for s in selected if s != reference]
    a_select = mo.ui.dropdown(options=selected, value=reference, label="Scenario A")
    b_select = mo.ui.dropdown(options=selected, value=_others[0], label="Scenario B")
    return a_select, b_select, horizon_select


@app.cell
def _(
    a_select,
    add_waterfall,
    b_select,
    bridge,
    ca,
    country,
    horizon_select,
    layout,
    make_subplots,
    mo,
    reference,
    selected,
):
    _horizon = horizon_select.value
    _steps = ca.waterfall(bridge, _horizon, a_select.value, b_select.value, cutoff=1e-3)
    _main = make_subplots(rows=1, cols=1)
    add_waterfall(_main, _steps)
    layout(
        _main,
        f"{country} {_horizon}: {a_select.value} → {b_select.value}",
        "Mt CO2",
    )
    _main.add_vrect(
        x0=0.5,
        x1=0.5 + (_steps.group == "physical").sum(),
        fillcolor="#2a78d6",
        opacity=0.05,
        line_width=0,
        annotation_text="physical",
        annotation_position="top left",
    )

    _others = [s for s in selected if s != reference]
    _multiples = make_subplots(
        rows=1, cols=len(_others), subplot_titles=_others, shared_yaxes=True
    )
    for _i, _other in enumerate(_others, start=1):
        add_waterfall(
            _multiples,
            ca.waterfall(bridge, _horizon, reference, _other, cutoff=1e-3),
            col=_i,
            legend=_i == 1,
        )
    layout(_multiples, f"Every scenario against {reference}, {_horizon}", "Mt CO2")
    _multiples.update_layout(height=420)

    _table = bridge.xs(_horizon, level="horizon").T
    mo.vstack(
        [
            mo.md(
                "## 2 National balance bridge\n\n"
                "Physical steps (blue) change because the system changed; accounting "
                "steps (orange) change because the method changed. Kerosene and kerosene "
                "claims are scaled by the domestic aviation share, as in the national budget.\n"
                "### Elements\n"
                "- Fuel use (physical): Default emissions from fuels\n"
                "- Capture credit (physical): negative emissions from capture\n"
                "- Creation credit: negative Emissions for biofuel production (negative in technology-driven accounting)\n"
                "- CCU charge: emissions when creating a fuel from stored co2 (positive in carrier-driven accounting)\n"
                "- Claims gas/oil etc.: negative emission claims to balance renewable fuels emissions to zero (only in carrier-driven accounting)\n"
            ),
            mo.hstack([horizon_select, a_select, b_select], justify="start", gap=2),
            mo.ui.plotly(_main),
            mo.ui.plotly(_multiples),
            mo.accordion(
                {
                    "Categories (Mt CO2)": mo.ui.table(
                        _table.round(3).reset_index(names="category"), selection=None
                    )
                }
            ),
        ]
    )
    return


@app.cell
def _(ca, colours, consistent, go, layout, mo, reference, target):
    assert consistent
    cost = ca.add_cost_deltas(target, reference)
    _prices = go.Figure()
    for _scenario, _rows in cost.groupby(level="scenario", sort=False):
        _x = _rows.index.get_level_values("horizon")
        _prices.add_trace(
            go.Scatter(
                x=_x,
                y=_rows["national CO2 price"],
                name=_scenario,
                mode="lines+markers",
                line=dict(color=colours[_scenario], width=2),
                marker=dict(size=8),
                customdata=_rows.regime,
                hovertemplate="%{x}: %{y:.3f} €/t (%{customdata})<extra>"
                + _scenario
                + "</extra>",
            )
        )
    layout(
        _prices,
        "National CO2 price μ (an abatement cost only for physical and pool-scarce)",
        "€/t",
    )

    def _delta_bars(column: str, title: str):
        fig = go.Figure()
        for scenario, rows in cost.groupby(level="scenario", sort=False):
            if scenario == reference:
                continue
            fig.add_trace(
                go.Bar(
                    x=rows.index.get_level_values("horizon"),
                    y=rows[column],
                    name=scenario,
                    marker=dict(color=colours[scenario], line=dict(width=0)),
                    hovertemplate="%{x}: %{y:+.3f} bn €<extra>" + scenario + "</extra>",
                )
            )
        return layout(fig, title, "bn €")

    _columns = [
        "regime",
        "binding",
        "national CO2 price",
        "claim cost",
        "EU CO2 price",
        "credit-financed share",
        "pre-claim emissions",
        "claims",
        "budget",
        "system cost",
        "Δ system cost",
        "cost in country",
        "Δ cost in country",
        "capex in country",
        "opex in country",
        "trade balance",
    ]
    mo.vstack(
        [
            mo.md(
                "## 3 Cost of the national target\n\n"
                "The regime says how the country meets its budget: `slack` (under budget), "
                "`physical` (no more credits, μ is the marginal abatement cost), "
                "`pool-scarce` (budget countries compete for a pool), `paper` (spare credits, "
                "μ ≈ claim cost). Duals of the credit bounds are not stored in the solved "
                "networks; they are estimated from μ (see `ca.credit_bounds`). Emissions in "
                "Mt CO2, cost in bn €."
            ),
            mo.ui.table(cost[_columns].round(4).reset_index(), selection=None),
            mo.ui.plotly(_prices),
            mo.hstack(
                [
                    mo.ui.plotly(
                        _delta_bars("Δ system cost", f"EU system cost Δ vs {reference}")
                    ),
                    mo.ui.plotly(
                        _delta_bars(
                            "Δ cost in country", f"Cost in country Δ vs {reference}"
                        )
                    ),
                ],
                widths="equal",
            ),
        ]
    )
    return (cost,)


@app.cell
def _(cost, mo, reference, selected):
    # default to the horizon with the highest national CO2 price outside the reference
    _mu = cost["national CO2 price"].drop(index=reference, level="scenario")
    _horizon = _mu.groupby(level="horizon").max().fillna(0.0).idxmax()
    transfer_horizon = mo.ui.dropdown(
        options=sorted(cost.index.unique("horizon")), value=_horizon, label="Horizon"
    )
    _others = [s for s in selected if s != reference]
    transfer_scenario = mo.ui.dropdown(
        options=selected, value=_others[-1], label="Scenario (Europe)"
    )
    return transfer_horizon, transfer_scenario


@app.cell(expand_output=True)
def _(
    bridge,
    ca,
    cost,
    country,
    go,
    layout,
    mo,
    pd,
    reference,
    selected,
    surplus,
    transfer_horizon,
    transfer_scenario,
    transfers,
    values,
):
    _PRODUCER, _CLAIMANT, _CCU = "#2a78d6", "#eb6834", "#6250d6"
    _technology = [
        s for s in selected if values.xs(s, level="scenario").price.isna().all()
    ]
    _horizon = transfer_horizon.value

    # 4.1 value of the green property of one MWh, per fuel and scenario
    _v = values.xs(_horizon, level="horizon").reindex(selected, level="scenario")
    _v = _v.reset_index()
    _is_tech = _v.scenario.isin(_technology)
    _producer = _v.price.where(~_is_tech, _v["national value"])
    _x = [_v.fuel.to_list(), _v.scenario.to_list()]
    _per_mwh = go.Figure()
    _per_mwh.add_trace(
        go.Bar(
            x=_x,
            y=_producer,
            name="to the producer (anywhere)",
            marker=dict(color=_PRODUCER, line=dict(width=0)),
            customdata=_is_tech.map(
                {True: "credit booked at the producer", False: "attribute price"}
            ),
            hovertemplate="%{x}<br>%{customdata}: %{y:.2f} €/MWh<extra></extra>",
        )
    )
    _per_mwh.add_trace(
        go.Bar(
            x=_x,
            y=_v["claimant rent"],
            name=f"to the claimant in {country} (rent)",
            marker=dict(color=_CLAIMANT, line=dict(width=0)),
            hovertemplate="%{x}<br>rent: %{y:.2f} €/MWh<extra></extra>",
        )
    )
    for _column, _symbol, _colour in [
        ("EU value", "line-ew-open", "#8a8984"),
        ("national value", "line-ew-open", "#0b0b0b"),
    ]:
        _per_mwh.add_trace(
            go.Scatter(
                x=_x,
                y=_v[_column],
                name=f"{_column} EF·(EU price{' + μ' if _column != 'EU value' else ''})",
                mode="markers",
                marker=dict(symbol=_symbol, size=28, line=dict(width=2, color=_colour)),
                hovertemplate="%{x}<br>" + _column + ": %{y:.2f} €/MWh<extra></extra>",
            )
        )
    _unpriced = _v[~_v.unique & ~_is_tech]
    if not _unpriced.empty:
        _per_mwh.add_trace(
            go.Scatter(
                x=[_unpriced.fuel.to_list(), _unpriced.scenario.to_list()],
                y=_unpriced["national value"] / 2,
                mode="text",
                text=["price not<br>unique"] * len(_unpriced),
                textfont=dict(color="#8a8984", size=11),
                showlegend=False,
                hoverinfo="skip",
            )
        )
    layout(
        _per_mwh,
        f"{country} {_horizon}: who receives the value of one MWh of green fuel",
        "€/MWh",
    )
    _per_mwh.update_layout(barmode="stack", height=460)

    # 4.2 where the national CO2 price ends up
    _rent = ca.national_rent(bridge, cost).reindex(selected, level="scenario")
    _claims = _rent[list(ca.CLAIM_CATEGORIES.values())].sum(axis=1)
    _rx = [
        _rent.index.get_level_values("horizon").to_list(),
        _rent.index.get_level_values("scenario").to_list(),
    ]
    _rents = go.Figure()
    for _y, _name, _colour in [
        (
            _rent[ca.CREATION_CREDIT],
            "green producers in the country (creation credit)",
            _PRODUCER,
        ),
        (_claims, "claimants in the country (claims)", _CLAIMANT),
        (_rent[ca.CCU_CHARGE], "synthetic producers pay (CCU charge)", _CCU),
    ]:
        _rents.add_trace(
            go.Bar(
                x=_rx,
                y=_y,
                name=_name,
                marker=dict(color=_colour, line=dict(width=0)),
                hovertemplate="%{x}<br>%{y:+.3f} bn €<extra>" + _name + "</extra>",
            )
        )
    layout(
        _rents,
        f"{country}: what the national CO2 price μ is worth, and to whom",
        "bn €/a",
    )
    _rents.update_layout(barmode="relative", height=440)

    # 4.3 money through the attribute pools for the country
    _index = pd.MultiIndex.from_product(
        [selected, sorted(cost.index.unique("horizon"))], names=["scenario", "horizon"]
    )
    _own = (
        transfers[transfers.index.get_level_values("country") == country]
        .droplevel("country")
        .reindex(_index, fill_value=0.0)
        .swaplevel()
        .sort_index(level="horizon", sort_remaining=False)
    )
    _ox = [
        _own.index.get_level_values("horizon").to_list(),
        _own.index.get_level_values("scenario").to_list(),
    ]
    _money = go.Figure()
    _money.add_trace(
        go.Bar(
            x=_ox,
            y=_own.sales,
            name=f"received by producers in {country}",
            marker=dict(color=_PRODUCER, line=dict(width=0)),
            hovertemplate="%{x}<br>sales: %{y:.3f} bn €<extra></extra>",
        )
    )
    _money.add_trace(
        go.Bar(
            x=_ox,
            y=-_own.purchases,
            name=f"paid by claimants in {country}",
            marker=dict(color=_CLAIMANT, line=dict(width=0)),
            hovertemplate="%{x}<br>purchases: %{y:.3f} bn €<extra></extra>",
        )
    )
    # under technology the credit of green fuel burnt abroad stays at home
    _kept = (
        surplus[surplus.index.get_level_values("scenario").isin(_technology)]
        .groupby(level=["scenario", "horizon"])
        .sum()
        .reindex(_index, fill_value=0.0)
        .swaplevel()
        .reindex(_own.index)
    )
    _money.add_trace(
        go.Bar(
            x=_ox,
            y=_kept.value,
            name=f"kept by producers in {country}: green surplus burnt abroad "
            "(technology, not traded)",
            marker=dict(
                color="rgba(0,0,0,0)",
                line=dict(color=_PRODUCER, width=1.5),
                pattern=dict(shape="/", fgcolor=_PRODUCER, solidity=0.4),
            ),
            customdata=_kept.surplus,
            hovertemplate="%{x}<br>surplus %{customdata:.2f} TWh at EU value: "
            "%{y:.3f} bn €<extra></extra>",
        )
    )
    _money.add_trace(
        go.Scatter(
            x=_ox,
            y=_own.net,
            name="net transfer",
            mode="markers",
            marker=dict(symbol="diamond", size=10, color="#0b0b0b"),
            hovertemplate="%{x}<br>net: %{y:+.3f} bn €<extra></extra>",
        )
    )
    layout(_money, f"{country}: money through the attribute pools", "bn €/a")
    _money.update_layout(barmode="relative", height=440)

    # 4.4 net transfer of every country in one run
    _scenario = transfer_scenario.value
    _has_trade = (_scenario, _horizon) in transfers.index.droplevel("country")
    if _has_trade:
        _eu = transfers.xs((_scenario, _horizon), level=["scenario", "horizon"])
        _eu = _eu[(_eu.sales.abs() + _eu.purchases.abs()) > 1e-6].sort_values("net")
        _labels = _eu.index.to_series().replace({"EU": "imports (outside Europe)"})
        _europe = go.Figure(
            go.Bar(
                x=_labels,
                y=_eu.net,
                marker=dict(
                    color=[_CLAIMANT if c == country else "#8a8984" for c in _eu.index],
                    line=dict(width=0),
                ),
                customdata=_eu[["sales", "purchases"]].to_numpy(),
                hovertemplate="%{x}: %{y:+.3f} bn €<br>sales %{customdata[0]:.3f}, "
                "purchases %{customdata[1]:.3f}<extra></extra>",
            )
        )
        layout(
            _europe,
            f"{_scenario} {_horizon}: net transfer per country (+ receives, − pays)",
            "bn €/a",
        )
        _europe.update_layout(height=420, showlegend=False)
        _europe_view = mo.ui.plotly(_europe)
    else:
        _europe_view = mo.callout(
            mo.md(f"`{_scenario}` has no attribute pools: nothing is traded."),
            kind="neutral",
        )

    _unique = values.drop(index=_technology, level="scenario")
    _unique = _unique[~_unique.unique].reset_index()
    _unpriced_share = transfers.unpriced.groupby(
        level=["scenario", "horizon"]
    ).sum() / (transfers.produced.groupby(level=["scenario", "horizon"]).sum())
    _warning = (
        mo.callout(
            mo.md(
                "**⚠ Some attribute prices are not unique.** For "
                + ", ".join(sorted(_unique.fuel.unique()))
                + f" in {country}, every claim in Europe sits at its bound: all consumption "
                "is already green. The LP then shifts value freely between the attribute "
                "price, the fuel price and the duals of the claim bounds (not stored); only "
                "their sum is determined. These attributes are shown as *price not unique* "
                "and left out of the money flows "
                f"({_unpriced_share.min():.0%}–{_unpriced_share.max():.0%} of all attributes "
                "per run). Step 3 values fuel and attribute together, whose sum is unique."
            ),
            kind="warn",
        )
        if not _unique.empty
        else mo.md("")
    )

    mo.vstack(
        [
            mo.md(
                f"""
    ## 4 Implicit transfers

    **What is the green property of a fuel worth, and who receives it?** Burning one
    MWh of fossil fuel in {country} emits EF t CO2. That costs the EU CO2 price under
    the EU cap and the national price μ under the national budget. One MWh of green
    fuel avoids both, so in {country} its green property is worth
    **EF · (EU price + μ)**.

    - **Technology ({reference}):** the credit is booked where the carbon leaves the
      atmosphere, at the producer and in the producer's country. The producer gets the
      full value; the credit cannot be sold apart from the fuel.
    - **Carrier:** the green property becomes an *attribute* that the producer sells
      into a pool (gas per country, linked along pipelines; oil and methanol EU-wide).
      Countries without a budget buy any attribute offered below the **EU value**
      EF · EU price, so the attribute price settles there. The producer, wherever it
      is, gets the EU value. The national part EF · μ stays with the **claimant** in
      the budget country as a rent.
    - So carrier accounting does not add value; it moves μ from domestic producers to
      domestic claimants, who can buy attributes made anywhere in the pool.

    Shadow prices are not payments: the model has no money flows between countries.
    Read them as the prices of a competitive market that clears at the optimum.
    """
            ),
            _warning,
            transfer_horizon,
            mo.md(
                f"### 4.1 Value of one MWh of green fuel ({_horizon})\n\n"
                "The full bar is the national value. Under technology it goes to the "
                "producer; under carrier the producer gets the attribute price (≈ EU value) "
                "and the claimant keeps the rest. When μ = 0 both markers coincide and "
                "there is no rent."
            ),
            mo.ui.plotly(_per_mwh),
            mo.md(
                "### 4.2 Where the national CO2 price ends up\n\n"
                "μ times the national credits of each accounting category. Under "
                "technology μ rewards green production *in* the country; under carrier it "
                "rewards claims, whatever their origin. μ is the national CO2 price of "
                "step 3 and is zero in the `slack` regime."
            ),
            mo.ui.plotly(_rents),
            mo.md(
                f"### 4.3 Money through the attribute pools for {country}\n\n"
                f"Attributes sold by producers in {country} and bought by its claimants, at "
                f"the attribute price. Under technology ({reference}) nothing is traded. A "
                "negative net transfer means the country pays producers elsewhere for "
                "green properties it books at home.\n\n"
                f"The hatched bar is a different mechanism. Under technology, green fuel "
                f"made in {country} beyond its own use of that fuel is burnt elsewhere: "
                f"the importer books the emission, {country} keeps the creation credit. "
                "No money flows, but the importer gives up a green property worth the EU "
                "value EF · EU price per MWh, what the attribute would sell for under "
                "carrier. Fuel mixes in the grid, so the surplus is a lower bound. The "
                "net transfer counts the attribute pools only."
            ),
            mo.ui.plotly(_money),
            mo.md(
                "### 4.4 Who pays whom in Europe\n\n"
                "Net transfer per country in one run. *Imports (outside Europe)* are the "
                "green imports, which sell attributes but claim none. The transfers add up "
                "to zero."
            ),
            transfer_scenario,
            _europe_view,
            mo.accordion(
                {
                    "Values per MWh (€/MWh)": mo.ui.table(
                        values.round(3).reset_index(), selection=None
                    ),
                    f"Attribute trade of {country} (TWh, bn €)": mo.ui.table(
                        _own.round(4).reset_index(),
                        selection=None,
                    ),
                    f"Green surplus of {country} (TWh, bn €)": mo.ui.table(
                        surplus.round(4).reset_index(), selection=None
                    ),
                    "National rent (bn €)": mo.ui.table(
                        ca.national_rent(bridge, cost).round(4).reset_index(),
                        selection=None,
                    ),
                }
            ),
        ]
    )
    return


@app.cell
def _(consistent, mo, physical):
    assert consistent
    group_select = mo.ui.dropdown(
        options=list(physical.index.unique("group")),
        value="capacity",
        label="Group",
    )
    return (group_select,)


@app.cell
def _(ca, colours, country, group_select, mo, physical, px, reference):
    _group = physical.xs(group_select.value, level="group")
    _table = ca.difference_table(_group, reference, cutoff=1e-3)
    _long = _group.reset_index()
    _long = _long[_long.set_index(["horizon", "item", "unit"]).index.isin(_table.index)]
    _unit = ", ".join(_long.unit.unique())
    _items = list(_table.index.unique("item"))

    def _horizon_figure(horizon: str):
        fig = px.bar(
            _long[_long.horizon == horizon],
            x="item",
            y="value",
            color="scenario",
            barmode="group",
            color_discrete_map=colours,
            category_orders={"item": _items, "scenario": list(colours)},
            template="plotly_white",
            labels={"value": _unit, "item": ""},
            title=f"{country} {horizon}: {group_select.value} ({_unit})",
        )
        fig.update_traces(marker_line_width=0)
        fig.update_layout(
            bargap=0.25, legend=dict(orientation="h", y=-0.35), height=420
        )
        return mo.ui.plotly(fig)

    _figures = [_horizon_figure(h) for h in sorted(_long.horizon.unique())]
    mo.vstack(
        [
            mo.md(
                "## 7 Physical system response\n\n"
                "Values of every scenario and Δ against the reference. Rows that are zero "
                "in every scenario are hidden. Capacities are at the input (bus0) of the link."
            ),
            group_select,
            mo.ui.table(_table.round(3).reset_index(), selection=None),
            *_figures,
        ]
    )
    return


@app.cell
def _(bridge, ca, cost, mo):
    _summary = ca.summary(bridge, cost)
    mo.vstack(
        [
            mo.md("## Summary"),
            mo.ui.table(_summary.round(3).reset_index(), selection=None),
        ]
    )
    return


@app.cell
def _(bridge):
    bridge.xs("2030", level="horizon")
    return


@app.cell
def _():
    return


if __name__ == "__main__":
    app.run()
