# Specification: Carbon Accounting Comparison Notebook

Status: implemented in `evals/carbon_accounting.py` and `evals/notebooks/carbon_accounting.py`
(AI-assisted). Deviations found during implementation are marked *Implementation note*.

## Goal

Compare any number (≥ 2) of runs of the carbon-accounting config, focused on one country (AT by
default). The current runs of interest are:

| Scenario | Method |
|---|---|
| `CA_base` | technology-based (status quo) |
| `CA_carrier` | carrier-based, claims bounded by consumption (D14) |
| `CA_carrier_fair` | carrier-based with fair-share cap (D18) |

Nothing in the notebook or the module is hard-coded to these scenarios. Scenarios are found in
the result root. The method and fair-share flag come from `n.meta`. One scenario is the
**reference**: every Δ in the notebook is measured against it.

Priorities: **(2) national balance bridge**, **(3) cost of the national target**, **(7) physical system response**.

Prerequisite (met): `CA_carrier` re-run with D17, `CA_carrier_fair` solved.

## Location and structure

- Notebook: `evals/notebooks/carbon_accounting.py` (marimo).
- **All** computation functions go into **one new module**: `evals/carbon_accounting.py`.
  Reusing existing functions is fine (`collect_myopic_statistics`, `scale_domestic_aviation`,
  `carbon_accounting_claims`, `get_location`, colours from `evals/constants.py`).
- Tests: `test/test_evals/test_carbon_accounting.py` (unit tests on toy networks from
  `test/test_mods/conftest.py`).
- The notebook only does UI, caching and plotting. No business logic in the notebook.

## Notebook flow (progress steps)

Numbered sections, each with a status badge (✓ / ⚠ / ✗). A failed check calls `mo.stop`, so the
later steps don't run on broken inputs.

| Step | Content |
|---|---|
| 0 Setup | Inputs: result root (default `results/carbon-accounting`); scenario multiselect over the subdirectories with solved networks (default: all); reference selector (default: the first scenario with method `technology`); country selector (default `AT`). Shows the networks found per scenario × horizon, with method and fair-share flag from `n.meta`. ✗ if fewer than two scenarios are selected or the selected scenarios have different horizons. |
| 1 Consistency | Per horizon: EU total at `co2 atmosphere` equal across all selected runs (P5, rel. tol. 1e-6); attribute stores net zero (I2); national LHS = evals balance (I5); fair-share caps respected in every run with fair share enabled (I6). |
| 2 National balance bridge | Waterfall, horizon selector. |
| 3 Cost of the national target | All horizons. |
| 4 Implicit transfers | Value of the green property and who receives it; horizon selector. |
| 7 Physical system response | All horizons. |
| Summary | One table, scenario × horizon: pre-claim emissions, claims, reported total and budget of CT; EU system cost Δ and cost in CT Δ vs the reference. |

Statistics are computed once per scenario and cached (`mo.cache` or `functools.cache`), so the
selectors respond fast. The module functions take a mapping scenario → horizon → network and
return frames with a `scenario` level, so they work for any number of scenarios.

## (2) National balance bridge

### Categories

Each annual flow into `co2 atmosphere` of a country gets exactly one category. Kerosene and
kerosene claims are scaled by α, as in the national constraint.

| Category | Contents | method `technology` | method `carrier` |
|---|---|---|---|
| Fuel use | +EF at all fuel users (fossil and green molecules) | ✓ | ✓ |
| Process & waste | `process emissions`, `HVC to air`, waste CHP, `municipal solid waste` | ✓ | ✓ |
| Capture credit | CO2 captured into `co2 stored` by capture links (C1) and bio-CC links; vent | ✓ | ✓ |
| Creation credit | −EF·out of G1 bio-fuel producers | ✓ | 0 |
| CCU charge | +CO2 used by G2 (FT, Sabatier, methanolisation) | 0 | ✓ |
| Claims (gas, oil, kerosene, methanol) | claim links, one category per fuel | 0 | ✓ |

*Implementation note:* a capture link books only the CO2 it captures as capture credit. The
rest of its emission goes to the category of the link: residual emissions of `SMR CC` or
`allam methanol` to fuel use, `process emissions CC` and `waste CHP CC` to process & waste, the
rest of bio-CC links to the creation credit (≈ 0 under `carrier`). Green imports (G3) count as
creation credit. Links are selected by name prefix, as in the national budget.

The categories add up exactly to the country's reported total, so a waterfall between two scenarios
closes exactly. The category definitions are expected to need several interactive rounds once
real results are available.

### Waterfall (horizon selector)

Two selectors pick scenario A and scenario B from the selected scenarios (default: A = reference,
B = the first other scenario), so any pair can be compared, e.g. `CA_base` → `CA_carrier`,
`CA_carrier` → `CA_carrier_fair` or `CA_base` → `CA_carrier_fair`. Next to it, a small-multiples
row shows every scenario against the reference for the same horizon.

Step order: total A → Δ fuel use → Δ process & waste → Δ capture credit → Δ creation credit →
Δ CCU charge → Δ claims per fuel → total B.

- The first three steps are **physical** (the system changed); the rest are **accounting**
  (the method changed). The chart marks the two groups visually.
- A reference line shows the national budget of the horizon.

## (3) Cost of the national target

### Why the shadow price alone isn't enough

`claim_cost` (0.01 €/t, D16) is exogenous. If the budget is met with pooled credits, the dual
of `co2_limit-{CT}` equals `claim_cost`: it then signals "met on paper", not an abatement cost.
The dual is meaningful only when credits run out: a CT credit bound binds, or the budget
countries compete for a scarce pool (see *Budget regime*).

### Budget regime

The regime says how CT meets its budget, so the national CO2 price μ (−dual of
`co2_limit-{CT}`, €/t) can be read correctly. The rules are checked in this order, and the
first that applies wins:

| Regime | Rule | What happens | Reading of μ |
|---|---|---|---|
| `slack` | \|μ\| < ε_μ | CT is under budget anyway | the target costs nothing |
| `physical` | method `technology` and μ ≥ ε_μ, **or** a CT credit bound binds: `carbon_accounting-claim-{CT}-*`, `carbon_accounting-fair-share-{CT}-*` or a gas transfer into CT (\|dual\| ≥ ε_b) | CT has no more credits; further cuts need real changes in CT | marginal abatement cost of CT |
| `pool-scarce` | an oil or methanol pool is fully claimed by budget countries (claims of countries without a budget from that pool < ε_p · pool inflow) | the budget countries compete for a scarce pool; one more claim by CT takes it from another budget country | price of the scarce pool, set by the competing country's abatement cost |
| `paper` | otherwise | the budget is met by claiming spare credits; nothing physical changes | ≈ `claim_cost`, not an abatement cost |

- The table names the binding bound (`physical`) or the scarce pool (`pool-scarce`).
- μ is shown next to `claim_cost` as a cross-check: `paper` should give μ ≈ `claim_cost`.
- Tolerances are module constants: ε_μ = 1e-3 €/t, ε_b = 1e-3 (dual units), ε_p = 1e-6.
  They are to be checked against the real duals.
- *Implementation note:* the duals of the `carbon_accounting-*` constraints exist only in the
  linopy model and are not stored in the solved network. A bound counts as binding if it is
  tight (relative slack ≤ 1e-3) and its estimated dual `μ · credit − claim cost` is ≥ ε_b
  (EUR/MWh), with `credit` the national credit per MWh of attributes (EF, times α for
  kerosene). In CA_carrier 2025/2030 μ ≈ 0.01 €/t and the methanol claim bound is tight, but its
  estimated dual is below ε_b, so the regime is `paper`. Storing the duals at solve time would
  make the estimate unnecessary.
- Why the pool test works: every attribute must be claimed within the year (cyclic stores, no
  sink), so countries without a budget take the rest of the pool. While they still claim
  something, CT can take more at `claim_cost`.
- Why these four regimes: they follow where the next credit of CT comes from. None is needed
  (`slack`), none is left in CT so CT must abate (`physical`), it can only be taken from
  another budget country (`pool-scarce`), or spare credits are available at `claim_cost`
  (`paper`). A binding CT bound is checked before the pools, because CT cannot claim more even
  from a pool with spare attributes. `paper` is the fallback; μ ≈ `claim_cost` confirms it.
- Assumption of the estimated dual: an attribute costs its EU value `EF · EU price`, which holds
  while countries without a budget take the rest of the pool. The exact dual would need the
  attribute price, which is not unique when every claim of a fuel sits at its bound.
- Observed in the carbon-accounting runs (AT, DE): `pool-scarce` never occurs (countries
  without a budget always claim most of both pools). Borderline: CA_carrier_fair 2050 AT is
  `physical` with μ = 0.07 €/t and estimated duals of ~0.004 €/MWh, just above ε_b; read μ
  there as close to `paper`.

### Metrics (all horizons, all selected scenarios)

| Metric | Definition | Reading |
|---|---|---|
| Budget regime | see *Budget regime* | classification, not a price |
| National CO2 price | μ = −dual of `co2_limit-{CT}` (€/t) | an abatement cost only for `physical` and `pool-scarce` |
| Credit-financed share | claims / (pre-claim emissions − budget) | how much of the effort is met by credits |
| Pre-claim emissions | national total without claim categories | "physical" national emissions |
| Binding credit bounds | duals of `carbon_accounting-claim-{CT}-*`, `-fair-share-{CT}-*`, gas transfers into CT | which credit limit sets the price |
| EU CO2 price | −dual of `CO2Limit-upper` | reference; should be ≈ equal across scenarios |
| System cost | EU total cost Δ vs the reference (bn €) | real resource cost of the method |
| Cost in CT | asset cost + trade balance of CT, Δ vs the reference (bn €), see below | who carries the cost of the method |

### Cost in the country

Cost in CT = capex + opex of the assets located in CT + net trade value of CT.

- **Location** of an asset: `get_location` (a branch takes its `bus0` location unless that is `EU`).
- **Trade:** every port of a CT asset at a bus outside CT (other countries and `EU` buses), and
  every port of a foreign asset at a CT bus, is a trade across the CT boundary. It is valued at
  the marginal price of that bus: withdrawal is an import cost, injection an export revenue.
  Oil and methanol consumed from the `EU` buses therefore count as imports, and domestic
  Fischer-Tropsch or methanol output into them as exports.
- **Excluded:** `co2 atmosphere` and `co2 import`. Emissions are accounting, not trade.
  `co2 stored` flows (CO2 pipelines) are physical and count as trade.
- **Attributes are trade:** under the carrier method a country pays for the attributes its
  claims draw from the EU pool (via the pool's distribution link) and from neighbours (gas
  transfers), and earns for the attributes its producers sell. Where every claim of a fuel sits
  at its bound (all methanol is green by construction), the LP prices of the fuel and of its
  attribute are not unique, only their sum is; valuing both keeps the cost in the country unique.
- Without the trade term, a country that imports e-fuels instead of building plants would
  look cheap.

## (4) Implicit transfers

The green property of one MWh of fuel is worth `EF · (EU price + μ)` in CT. Under carrier the
producer sells it as an attribute at the shadow price of the attribute bus and the claimant keeps
the rest as a rent (`attribute_prices`, `attribute_trade`, `implicit_transfers`). Under
technology nothing is traded, but green fuel that CT makes beyond its own use of that fuel is
burnt elsewhere: the importer books the emission, CT keeps the creation credit. `green_surplus`
gives this surplus per fuel (production less own use, at least zero) and values it at the EU
value `EF · EU price`, what the attribute would fetch under carrier. The notebook shows it as a
hatched bar next to the pool sales and leaves it out of the net transfer. Fuel mixes in the grid,
so the surplus is a lower bound of the green fuel burnt abroad.

## (7) Physical system response (all horizons)

For the selected country, values of each selected scenario and Δ vs the reference:

- **Capacities and output:** biogas/BioSNG (with and without CC), electrolysis, Sabatier,
  Fischer-Tropsch, methanolisation, DAC, capture links.
- **Fuel balances:** gas (fossil import vs domestic green), oil, methanol, H2.
- **CO2:** captured, used for CCU, exported via CO2 pipelines (`co2 compression`).
- **Borders:** physical gas flows per border; in runs with method `carrier`, attribute
  transfers next to them.
- **Format:** a difference table (one column per scenario) plus small multiples per horizon.

## Decisions

| # | Topic | Decision |
|---|---|---|
| 1 | Location | `evals/notebooks/carbon_accounting.py` |
| 2 | Physical vs accounting split | category approach above; refined iteratively |
| 3 | Country | selector, default `AT` |
| 4 | Horizons | waterfall: horizon selector; target cost and physical changes: all horizons |
| 5 | Cost in the country | asset cost + trade value at boundary-bus prices, see *Cost in the country* |
| 6 | Code layout | all functions in one new module `evals/carbon_accounting.py`, reusing existing functions |
| 7 | Summary table | emissions vs budget and cost Δ (EU and CT); no prices |
| 8 | Docs nav | neither this spec nor the notebook gets a `mkdocs.yml` nav entry |
| 9 | Budget regime | four regimes (`slack`, `physical`, `pool-scarce`, `paper`), classified from duals and pool claims, see *Budget regime* |
| 10 | Number of scenarios | any number ≥ 2, found in the result root; Δ always vs a selectable reference |

## Open points

None. The category definitions (2) and the regime tolerances (3) are refined once real results
are available.