# Specification: Carbon Accounting Methods

!!! note "Status: ready for implementation"
    Specification and basis for implementation. AI-assisted draft. Decisions D0–D12 are
    agreed with the author; team review is pending. Inventory and figures are taken from
    `resources/carbon-accounting/CA_base/networks/composed_{2025,2050}.nc` and
    `results/carbon-accounting/CA_base/networks/solved_2050.nc`.

## 1. Goal

PyPSA-AT gets a switchable carbon accounting method. The chosen method is the
single standard for the whole model: national CO2 budgets, the EU-wide CO2
cap, and the reported emissions in `evals/`. Two methods are compared with
`config/config.carbon-accounting.yaml`:

| Method | Key | Emissions attached to | Green fuel at creation | Green fuel at use |
|---|---|---|---|---|
| Technology-based (status quo) | `technology` | the technology that releases CO2 to the atmosphere | negative (−EF) | positive (+EF), like fossil |
| Carrier-based | `carrier` | the energy carrier that is consumed | zero | zero |

**Emission factors (EF)** are the CO2 intensities the model books at the consumer.
They are read from the cost file of the horizon, never hard-coded:

| Fuel | Cost entry | Value (2030) |
|---|---|---|
| gas | `costs.at["gas", "CO2 intensity"]` | 0.198 t/MWh |
| oil | `costs.at["oil", "CO2 intensity"]` | 0.2571 t/MWh |
| methanol | `costs.at["methanolisation", "carbondioxide-input"]` | 0.248 t/MWh |

## 2. Status quo (method `technology`)

This is how the model behaves today. It needs no changes and is the reference.

- Fuel users book `+EF × fuel input` at `co2 atmosphere`, whatever the origin of the fuel.
  Green and fossil molecules share one bus: `{node} gas` (51 nodes, gas grid),
  `EU oil` and `EU methanol` (one bus each). PyPSA-AT skips PyPSA-DE's
  `unravel_carbonaceous_fuels` and `unravel_gasbus` (`mods/network/common.py`).
- Bio-fuel producers book `−EF × output` at creation (table 3.2).
- Green fuel imports (`import gas|oil|methanol`) draw CO2 from `co2 atmosphere` via bus0.
  `EU oil import` and `EU methanol import` have the prefix `EU` and therefore count
  towards **no** national budget today.
- Synthetic fuels take carbon from `co2 stored`. The carbon came from DAC (−1 at the
  atmosphere) or from point-source capture (the emitter books only its residual).
- Aviation emissions are booked at the `kerosene for aviation` link (`EU oil` → `{node}
  kerosene for aviation`). `methanol-to-kerosene` books its emission itself.
- National constraint (`mods/constraints/co2_budget.py`): sums all links whose name starts
  with the country code and that have a port at `co2 atmosphere`. `kerosene for aviation`
  is scaled by the domestic aviation share α.
- **Austria has no CO2 sequestration potential** (`e_nom_max = 0` for all `AT* co2 sequestered`
  stores). In CA_base 2050 Austria captures 11.0 Mt CO2 and sequesters 0 Mt domestically.

## 3. Carrier-based method (`carrier`)

### 3.1 Principles

- **P1 Carrier neutrality.** Using green gas, oil or methanol books net zero.
  Using fossil fuel books +EF. The net figure is booked in the country that consumes the fuel.
- **P2 No creation credit.** Producing a green fuel books zero, never −EF.
- **P3 Removals stay negative.** CO2 that is permanently sequestered still reduces
  emissions. The capturing country gets the credit.
- **P4 No capture credit for utilisation.** CO2 that is captured and then used for a
  synthetic fuel gives no reduction. The synthetic fuel itself counts as green.
- **P5 Same total.** The EU-wide sum at `co2 atmosphere` is identical to the
  `technology` method, because all credits are retired (3.5).
- **P6 Mass balance.** A green gas credit can only be claimed in a country that the gas
  can physically reach. Oil, methanol and removal credits are pooled EU-wide (D3, D15).

### 3.2 Link inventory and transformation rules

Every link in the network falls into exactly one class. Classification goes by `carrier`.
The lists live in `mods/constants.py`. A test fails if a link with a port at `co2 atmosphere`,
`co2 stored`, `gas`, `oil` or `methanol` has an unclassified carrier (fail early on upstream changes).

Notation: `atm` = efficiency at the `co2 atmosphere` port (−1 for bus0), `stored` = efficiency
at the `co2 stored` port (positive = capture), `out` = efficiency at the fuel output port,
`EF_out` = EF of that output fuel.

| Class | Carriers (inventory 2025/2050) | `technology` | `carrier` transformation |
|---|---|---|---|
| **G1 Bio-fuel producer** | `biogas to gas`, `biogas to gas CC`, `BioSNG`, `BioSNG CC`, `biomass to liquid`, `biomass to liquid CC`, `electrobiofuels`, `unsustainable bioliquids`, `biomass-to-methanol`, `biomass-to-methanol CC` | `atm = −EF_out·out (− stored)` | `atm += stored + EF_out·out`; add attribute port (3.3) |
| **G2 Synthetic fuel producer** | `Fischer-Tropsch`, `Sabatier`, `methanolisation` | no `atm` port; `stored < 0` (input) | no change to `atm`; add attribute port |
| **G3 Green import** | `import gas`, `import oil`, `import methanol` | `bus0 = co2 atmosphere` | `bus0 → co2 import` (3.4); add attribute port |
| **C1 Capture** | `DAC`, `SMR CC`, `gas for industry CC`, `urban central gas CHP CC`, `CCGT methanol CC`, `Methanol steam reforming CC`, `allam methanol`, `waste CHP CC`, `process emissions CC`, `solid biomass for industry CC`, `solid biomass to hydrogen`, `urban central solid biomass CHP CC` | `atm` = residual (DAC −1 per t) | `atm += stored` (gross; DAC → 0) |
| **S1 Sequestration** | `co2 sequestered` (`co2 dense` → `co2 sequestered`) | — | add removal attribute port (3.3) |
| **V1 Vent** | `co2 vent` (not active in CA_base) | `atm = +1` | `atm = 0` |
| **U Unchanged** | all fuel users (`CCGT`, `OCGT`, boilers, CHP, `SMR`, `gas for industry`, `kerosene for aviation`, `agriculture machinery oil`, `land transport oil`, `shipping oil`, `shipping methanol`, `industry methanol`, `methanol-to-kerosene`, `OCGT/CCGT methanol`, `Methanol steam reforming`, ...), fossil (`coal`, `lignite`, `oil refining`, `nuclear`), `process emissions`, `HVC to air`, `waste CHP`, `municipal solid waste` (D13), transport (`gas pipeline*`, `CO2 pipeline`, `co2 compression`, `co2 expansion`) | | none |

The general rule for G1, C1 and V1 is
`atm_carrier = atm_technology + stored + EF_out·out` (with `out = 0` for non-producers).
It is **additive**, so tiny inconsistencies in the cost data stay as they are and P5 holds
exactly. Residuals measured for 2050: |atm_carrier| ≤ 2·10⁻⁴ t/MWh for G1 (biomass-to-methanol),
exactly 0 for G2 and C1.

The transformation classes are **disjoint**: every link is transformed once, by its class.
Capture eligibility for the removal bound (3.3, item 3) is a separate selector,
`CAPTURE_ELIGIBLE = C1 ∪ {G1 links with a co2 stored port}` (2050: `biogas to gas CC`,
`BioSNG CC`, `biomass to liquid CC`, `biomass-to-methanol CC`). It doesn't change any
coefficients.

Capture gets **no immediate credit** under `carrier`. Capture plants book their gross emissions
(fossil: +EF, biogenic: 0, DAC: 0). The reduction only arises through the removal claim when
CO2 is sequestered.

Check of P5 per chain (carbon units):

| Chain | `technology` | `carrier` |
|---|---|---|
| Fossil + CCS (capture rate c) | 1−c | 1 − c (removal claim) |
| Fossil + CCU → FT → burned | (1−c) + c = 1 | 1 + c (use) − c (claim) = 1 |
| DAC → FT → burned | −1 + 1 = 0 | 0 + 1 − 1 = 0 |
| DAC → storage | −1 | 0 − 1 (removal claim) = −1 |
| Biogas upgrading → burned | −1 + 1 = 0 | 0 + 1 − 1 = 0 |
| Biogas CC (stored share s) | −1 − s + 1 = −s | 0 + 1 − 1 − s = −s |
| DAC → FT → naphtha → plastics (share p re-emitted) | −1 + p | p − 1 (claim) |

### 3.3 Accounting layer

Physical flows stay unchanged. Green producers still deliver to their `gas`, `oil` or
`methanol` bus, and the gas grid and CO2 network stay intact. The accounting layer is built
from ordinary PyPSA components, so `n.statistics` and the evals see it without special handling.

**Components** (`CT` = two-letter country code, from the first two characters of the node name):

| Component | Name | Bus(es) | Parameters |
|---|---|---|---|
| Bus | `{CT} renewable gas attribute` | — | `carrier="renewable gas attribute"`, `unit="MWh"`, `location=CT` |
| Bus (pool) | `EU renewable oil attribute`, `EU renewable methanol attribute` | — | carriers `renewable oil attribute`, `renewable methanol attribute`, `location=EU` |
| Bus (pool) | `EU co2 removal attribute` | — | `carrier="co2 removal attribute"`, `unit="t_co2"`, `location=EU` |
| Bus (country) | `{CT} renewable oil attribute`, `{CT} renewable methanol attribute`, `{CT} co2 removal attribute` | — | same carriers, `location=CT` |
| Link (distribution) | `EU renewable oil attribute -> {CT}`, and the same for methanol and removals | pool → country bus | `efficiency = 1`, `p_nom = 1e9`, no bound (D3, D15) |
| Store | one per pool bus and per `{CT} renewable gas attribute`, same name | attribute bus | `e_nom_extendable=True`, `capital_cost=0`, `e_cyclic=True` |
| Attribute port (G1–G3) | extra port `bus{k}` on the producer link | attribute bus of the producer's country (gas) or EU pool | `efficiency{k} = out` (MWh attribute per MWh fuel) |
| Removal port (S1) | extra port `bus{k}` on `co2 sequestered` | `EU co2 removal attribute` | `efficiency{k} = 1` (t per t) |
| Link (claim) | `{CT} renewable gas claim`, `{CT} renewable oil claim`, `{CT} renewable oil claim aviation`, `{CT} renewable methanol claim` | bus0 `{CT} … attribute` (country bus), bus1 `co2 atmosphere` | `efficiency = −EF`, `p_nom = 1e9` |
| Link (claim) | `{CT} co2 removal claim` | bus0 `{CT} co2 removal attribute`, bus1 `co2 atmosphere` | `efficiency = −1`, `p_nom = 1e9` |
| Link (transfer) | `{X} renewable gas attribute -> {Y}` for every pair X≠Y connected by a gas pipeline | `{X} … attribute` → `{Y} … attribute` | `efficiency = 1`, `p_nom = 1e9` |
| Carriers | the attribute, claim and transfer carriers above, plus `co2 import` | | colours in `config/plotting.at.yaml` |

Claim links exist for every country that has nodes. They carry the country prefix
(`co2_budget.py` selects links by name). Their bus0 is always a country bus with
`location=CT`, so `evals/utils.py:get_location`, which goes by bus location and avoids EU,
assigns them to the country without special handling. That's why pooled credits pass
through a country bus first. The country buses balance per snapshot via the unbounded
distribution links, so they need no store.
Only AT and DE have national budgets; the other countries' claims only matter for retirement (D14).

**Annual constraints** (in `mods/constraints/carbon_accounting.py`). Let w_t be
`snapshot_weightings.generators` and `P(l) = Σ_t w_t · p0(l, t)`.

1. **Claim bound (D4, D7)**, per country CT and fuel f:
   `P({CT} renewable f claim) ≤ Σ_{l ∈ cons(CT, f)} P(l)`.
   `cons(CT, f)` = all links with `bus0` on a bus of carrier f whose name starts with CT,
   excluding transport (`gas pipeline`, `gas pipeline new`). For oil the aviation route is split:
    - `renewable oil claim aviation` ≤ consumption of `kerosene for aviation`
    - `renewable oil claim` ≤ all other oil consumption (2025: `land transport oil`,
      `shipping oil`, `naphtha for industry`, `agriculture machinery oil`, oil boilers and CHP, ...)
2. **Gas transfer bound (D1, D4)**, per ordered country pair (X, Y):
   `P({X} renewable gas attribute -> {Y}) ≤ Σ_{l ∈ pipe(X→Y)} Σ_t w_t · (−p1(l, t))`,
   where `pipe(X→Y)` are the `gas pipeline`/`gas pipeline new` links with bus0 in X and bus1 in Y.
   They are already split into one-way pairs (`p_min_pu = 0`), so the bound is linear.
   Transit works through chained transfers. Known weakness: the solver could send gas in both
   directions at once (circulation) to raise the bound. Pipeline losses and compression make
   this costly, and test I4 monitors it.
3. **Removal claim bound (D9, D15)**, per country CT:
   `P({CT} co2 removal claim) ≤ Σ_{l ∈ CAPTURE_ELIGIBLE, CT} Σ_k efficiency_k(l) · P(l)` over the
   `co2 stored` ports: the gross CO2 captured in CT (point source + DAC + bio-CC, 3.2).
4. **Full retirement (D5)** follows from the cyclic attribute stores: over the year,
   attributes generated = attributes claimed. No extra constraint is needed. Attributes cannot
   leave the system any other way (no sink, no vent).
5. **Aviation (D8)** in `co2_budget.py`: carrier `renewable oil claim aviation` is excluded from
   the per-port loop and added scaled by α, like `kerosene for aviation`.
   `methanol-to-kerosene` stays unscaled (D11); green methanol for it is claimed via
   `renewable methanol claim` at full EF.

!!! note "Endogenous allocation"
    Within the bounds, the optimiser assigns credits to the country where they save the most,
    i.e. where the national budget binds. For oil, methanol and removals this effectively works
    like emission trading between AT and DE. Countries without a national budget don't compete
    for credits (D14, accepted and reported in the evals).

### 3.4 Imports (D10)

Import links keep `p0` in t CO2 (their costs are defined per t). Only bus0 is re-pointed:

- New bus `co2 import` (`carrier="co2 import"`, `unit="t_co2"`) with one generator `co2 import`
  (`p_nom = 1e9`, `marginal_cost = 0`).
- `import gas|oil|methanol`: `bus0 → co2 import`; attribute port with `efficiency{k} = efficiency`.
- Oil and methanol attributes go to the EU pool. Gas attributes go to the country of the
  entry node (e.g. `NL gas import` → `NL renewable gas attribute`) and follow the gas transfers.
- Effect: the 245 TWh of `EU methanol import` (CA_base 2050) gain national attribution.
  Today they count towards no country.

### 3.5 Carbon consistency (D5, D11, D12)

- **Full retirement:** guaranteed by cyclic attribute stores (3.3, item 4).
- **Consistent producers:** G1, G2 and G3 have carbon inputs that match `EF_out` by
  construction (measured residual ≤ 2·10⁻⁴ t/MWh). The technology-data value
  `Fischer-Tropsch` `carbondioxide-input = 0.326` is not used by the model.
- **`methanol-to-kerosene`:** consumes 1.0764 × 0.248 = 0.267 t of methanol carbon per MWh
  kerosene but books 0.2571 t, so 3.7 % of the carbon vanishes. This is kept (D12). It is the
  same in both methods, so it doesn't affect the comparison.

## 4. Configuration

```yaml
mods:
  carbon_accounting:
    enable: true
    method: technology   # technology | carrier
```

- `enable: false` or `method: technology` → the orchestrator returns early. The network is
  bit-identical to today. `technology` is the default in `config.at.yaml` (D6).
- An unknown `method` raises a `ValueError` (fail early).
- `config/scenarios.carbon-accounting.yaml`: `CA_technology` (= `CA_base`) and
  `CA_carrier` (`mods.carbon_accounting.method: carrier`), both listed under `run.name`.

## 5. Implementation design

### 5.1 Modules and functions

`mods/network/carbon_accounting.py` (compose phase):

```python
def apply_carbon_accounting(n: pypsa.Network, snakemake) -> None:
    """Orchestrator: guard on enable/method, then call the steps below in order."""


def drop_accounting_layer(n) -> None:
    """Safety net: remove accounting buses/stores/links by carrier if present."""


def add_accounting_layer(n, countries, ef: dict[str, float]) -> None:
    """Add attribute buses, stores, claim, distribution and transfer links, the co2 import bus/generator."""


def transform_emission_ports(n, ef: dict[str, float]) -> None:
    """Apply the rules of 3.2 to G1, C1, V1. Skips links marked as transformed."""


def add_attribute_ports(n) -> None:
    """Add the attribute port to G1–G3 and the removal port to S1; re-point G3 bus0. Skips marked links."""
```

**Transformation marker:** every link changed by `transform_emission_ports` or
`add_attribute_ports` (G1, G2, G3, C1, S1, V1) gets `n.links["carbon_accounting"] = "carrier"`.
Links without the marker have NaN. Both functions only touch unmarked links and set the marker at
the end. This covers capture-only links such as DAC or SMR CC, which have no attribute port.
Verified: the custom column survives `export_to_netcdf`/`Network(...)`, and `add_brownfield`
copies all static columns (`n.add(c.name, c.static.index, **c.static)`).

- Called in `mods.modify_prenetwork` (`mods/network/common.py`) as the **last** step before
  `clip_negative_loads_for_edge_cases`, so every link exists, including brownfield links.
- Port helper: use the next free `bus{k}` index per link; create the columns `bus{k}` and
  `efficiency{k}` on `n.links` if they're missing.
- EF from `load_costs(snakemake.input.tech_costs)`, which is already loaded in the orchestrator.

`mods/constraints/carbon_accounting.py` (solve phase):

```python
def constraint_carbon_accounting(n, snakemake, investment_year: int) -> None:
    """Guard on enable/method; add constraints 3.3 (1)–(3) to n.model."""
```

- Registered in `mods/__init__.py` and called in `scripts/pypsa-at/additional_functionality.py`
  after `constraint_national_co2_budgets`.
- Constraint names: `carbon_accounting-claim-{CT}-{fuel}`, `carbon_accounting-transfer-{X}-{Y}`,
  `carbon_accounting-removal-{CT}`.

`mods/constraints/co2_budget.py`: exclude `renewable oil claim aviation` from the port loop,
add it scaled by α (the same code path as `kerosene for aviation`). In `technology` mode the
carrier doesn't exist, so the change is a no-op.

`mods/constants.py`: the carrier lists of 3.2 (`GREEN_BIO_PRODUCERS`, `GREEN_SYNTHETIC_PRODUCERS`,
`GREEN_IMPORTS`, `CAPTURE`, `SEQUESTRATION`, `VENT`, `FUEL_TRANSPORT`) and the accounting carriers.

`mods/constants.py`: also `CAPTURE_ELIGIBLE` (3.2) and the marker column name.

`evals/`:

- CO2 balance view (`view_balance_carbon` in `evals/views/balances.py`): claim carriers appear
  as their own categories, with nice names and colours. The α scaling applies to
  `kerosene for aviation` **and** `renewable oil claim aviation` (same mask), so the national
  report matches the constraint.
- Country attribution follows from the country claim buses (3.3), so `get_location` needs no change.
- For D14, a new view reports per country and fuel: the claimed green share (claims / consumption)
  next to the EU-average green share (EU green production / EU consumption), and removal claims
  next to gross capture.
- Check that attribute buses don't show up in the energy balances or the sankey. They have their
  own bus carriers, so they shouldn't.

### 5.2 Myopic foresight and brownfield

`add_brownfield` copies the links of the previous horizon before `modify_prenetwork` runs.

- Copied G1–G3/C1/S1/V1 links were already transformed and carry the marker (5.1), so they
  are skipped (no double transformation).
- Accounting components are **not** copied: `add_brownfield` drops every component with
  `lifetime = inf` from the previous network, which is the default for the accounting layer.
  `drop_accounting_layer` only acts as a safety net. The layer is rebuilt in every horizon with
  the current EF.
- Bus names are the same in every horizon, so the attribute ports of copied links stay valid.

### 5.3 EU CO2 cap

The EU cap (`apply_co2_budget_constraints`) tracks the `co2 atmosphere` store. Claim links
feed into the `co2 atmosphere` bus, so they're included automatically. With full retirement,
the total is the same as in `technology` (P5). No change is needed; a test verifies this.

## 6. Test plan

Unit tests on small synthetic networks (`test/test_mods/network/test_carbon_accounting.py`,
`test/test_mods/constraints/test_carbon_accounting.py`):

| # | Test | Asserts |
|---|---|---|
| U1 | `method: technology` and `enable: false` | `n.links`, `n.buses`, `n.stores` unchanged |
| U2 | Rules per class, parametrised over the carriers of 3.2 | `atm` after transformation per the formula; attribute ports present |
| U3 | Idempotency | running the transformation twice gives the same network, explicitly including capture-only links (`DAC`, `SMR CC`) |
| U4 | Brownfield | copied, already transformed links (G1 and capture-only) keep their coefficients and the marker; the accounting layer exists exactly once |
| U5 | Classification complete | every carrier with a port at a CO2 or fuel bus is in exactly one transformation class; `CAPTURE_ELIGIBLE` = C1 ∪ G1 links with a `co2 stored` port |
| U6 | Unknown method | `ValueError` |
| U7 | Toy solve (HiGHS): 2 countries, biogas in X, consumer in Y, one pipeline | X and Y each book 0; the claim is in Y; P5 holds |
| U8 | Toy solve: gas transfer bound | without a pipeline flow X→Y, Y can't claim X's attributes |
| U9 | Toy solve: removal | capture in X, sequestration in Z → credit in X, bounded by X's capture |
| U10 | Aviation | the `renewable oil claim aviation` contribution is scaled by α in the national LHS and in `view_balance_carbon` |
| U11 | Country attribution | `get_location` returns CT, not EU, for all claim links (gas, oil, methanol, removal) |

Integration tests (`-m AT`, `--result-path=results/carbon-accounting/CA_carrier`):

| # | Test | Asserts |
|---|---|---|
| I1 | P5 | EU sum of weighted flows at `co2 atmosphere` equals the technology equivalent, rebuilt from the inverse rule of 3.2; relative tolerance 1e-6 |
| I2 | Retirement | the annual net change of every attribute store is 0 |
| I3 | Sign | only claim links and the retained `municipal solid waste` credit (D13) have negative weighted flows into `co2 atmosphere` (G1 residuals ≤ 1e-3 t/MWh tolerated) |
| I4 | Gas circulation | per border, the smaller of the two directional annual gas flows is reported; a warning above 5 % of the larger one (see 3.3, item 2) |
| I5 | National budgets | the AT/DE LHS of `co2_limit-*` matches the evals CO2 balance per country |

## 7. Work packages

Each package is one PR with tests and passes `ruff`, the unit tests and a dry run.

1. **Config and scaffold:** the `mods.carbon_accounting` block, scenarios `CA_technology`/`CA_carrier`,
   the orchestrator with its guard, constants and classification test (U1, U5, U6).
2. **Emission port transformation:** `transform_emission_ports` with idempotency (U2, U3).
3. **Accounting layer, gas:** attribute buses/stores, ports, claims, transfers, the claim and
   transfer constraints (U7, U8).
4. **Oil and methanol pool, imports:** EU pools with country claim buses and distribution links,
   the aviation split, `co2 import` (U10, U11).
5. **Removal credits:** the sequestration port, removal claim and bound (U9).
6. **Brownfield, evals, docs:** `drop_accounting_layer` (U4), the evals views, a section in
   `co2-balancing.md`, a changelog entry; run `CA_technology` and `CA_carrier`, integration tests I1–I5.
   Evals priority: (1) national CO2 balance of AT/DE by sector under both methods, including
   claims and removals, (2) claimed green shares vs. EU average (D14). System changes and
   costs/CO2 prices come later.

## 8. Decisions

| ID | Topic | Status |
|---|---|---|
| D0 | Mechanism: mass balance | decided |
| D0b | CCU: synthetic fuels green, no capture credit for utilised CO2 | decided |
| D0c | Permanent removals stay negative | decided |
| D0d | Scope: method applies to the whole model | decided |
| D1 | Mass balance granularity: per country | decided |
| D2 | Removal credit is generated at sequestration. Capture gets no immediate credit: capture plants book their gross emissions under the rule of 3.2 (fossil +EF, biogenic and DAC 0) | decided |
| D3 | Oil/methanol attributes transfer freely (EU pool) | decided |
| D4 | All bounds are annual | decided |
| D5 | Full retirement of all attributes | decided |
| D6 | Default in `config.at.yaml`: `technology` | decided |
| D7 | Claims cover all consumption of a carrier, including non-energy use | decided |
| D8 | α also applies to kerosene claims | decided |
| D9 | Removal credits go to the capturing country | decided |
| D10 | Green imports: credit goes to the country that burns the fuel | decided |
| D11 | `methanol-to-kerosene` stays unscaled by α | decided |
| D12 | The 3.7 % carbon loss in `methanol-to-kerosene` is kept | decided |
| D13 | The `municipal solid waste` → HVC → `waste CHP`/`HVC to air` chain stays as it is: the credit and the emission arise at the same node, so national results don't change | decided |
| D14 | No competition for pooled credits is modelled; no fair-share cap. Only AT and DE have national budgets, so they can claim pooled credits up to their own consumption or capture. This mirrors the status quo with the sign flipped: under `technology`, producing green fuels for export is rewarded at home; under `carrier`, consuming them is. This asymmetry is one of the differences the comparison is meant to show, and the evals report it (5.1) | decided |
| D15 | Removal credits: EU pool, each country claims at most its own gross capture (consistent with the oil/methanol pools). CO2 pipelines are bidirectional, so a per-direction flow bound wouldn't be linear | decided |
