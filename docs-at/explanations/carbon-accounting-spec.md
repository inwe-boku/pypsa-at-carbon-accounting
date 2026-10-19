# Specification: Carbon Accounting Methods

!!! note "Status: implemented"
    Specification of the implementation in `mods/network/carbon_accounting.py` and
    `mods/constraints/carbon_accounting.py`. AI-assisted draft. Decisions D0–D18 are
    agreed with the author; team review is pending. Inventory and figures are taken from
    `resources/carbon-accounting/CA_base/networks/composed_{2025,2050}.nc` and
    `results/carbon-accounting/CA_base/networks/solved_2050.nc`.

## 1. Goal

PyPSA-AT gets a switchable carbon accounting method. The chosen method is the
single standard for the whole model: national CO2 budgets, the EU-wide CO2
cap, and the reported emissions in `evals/`. Two methods are compared with
`config/config.carbon-accounting.yaml` (scenarios `CA_base` and `CA_carrier`):

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
- **P3 Removals stay negative.** Captured CO2 counts as removed in the capturing country,
  as in `technology`, unless it is used for a synthetic fuel (D17).
- **P4 No capture credit for utilisation.** CO2 that is captured and then used for a
  synthetic fuel gives no reduction: the synthetic fuel producer books the CO2 it takes
  as an emission in its own country. The synthetic fuel itself counts as green.
- **P5 Same total.** The EU-wide sum at `co2 atmosphere` is identical to the
  `technology` method, because all credits are retired (3.5).
- **P6 Mass balance.** A green gas credit can only be claimed in a country that the gas
  can physically reach. Oil and methanol credits are pooled EU-wide (D3).

### 3.2 Link inventory and transformation rules

Every link in the network falls into exactly one class. Classification goes by `carrier`.
The lists live in `mods/constants.py`. A test fails if a link with a port at `co2 atmosphere`,
`co2 stored`, `gas`, `oil` or `methanol` has an unclassified carrier (fail early on upstream changes).

Notation: `atm` = efficiency at the `co2 atmosphere` port (−1 for bus0), `stored` = efficiency
at the `co2 stored` port (positive = capture), `out` = efficiency at the fuel output port,
`EF_out` = EF of that output fuel.

| Class | Carriers (inventory 2025/2050) | `technology` | `carrier` transformation |
|---|---|---|---|
| **G1 Bio-fuel producer** | `biogas to gas`, `biogas to gas CC`, `BioSNG`, `BioSNG CC`, `biomass to liquid`, `biomass to liquid CC`, `electrobiofuels`, `unsustainable bioliquids`, `biomass-to-methanol`, `biomass-to-methanol CC` | `atm = −EF_out·out (− stored)` | `atm += EF_out·out` (the stored share keeps its credit); add attribute port (3.3) |
| **G2 Synthetic fuel producer** | `Fischer-Tropsch`, `Sabatier`, `methanolisation` | no `atm` port; `stored < 0` (input) | add `atm = −stored` (the CO2 used, D17); add attribute port |
| **G3 Green import** | `import gas`, `import oil`, `import methanol` | `bus0 = co2 atmosphere` | `bus0 → co2 import` (3.4); add attribute port |
| **C1 Capture** | `DAC`, `SMR CC`, `gas for industry CC`, `urban central gas CHP CC`, `CCGT methanol CC`, `Methanol steam reforming CC`, `allam methanol`, `waste CHP CC`, `process emissions CC`, `solid biomass for industry CC`, `solid biomass to hydrogen`, `urban central solid biomass CHP CC` | `atm` = residual (DAC −1 per t) | none (credit at capture, D17) |
| **S1 Sequestration** | `co2 sequestered` (`co2 dense` → `co2 sequestered`) | — | none |
| **V1 Vent** | `co2 vent` (not active in CA_base) | `atm = +1` | none |
| **U Unchanged** | all fuel users (`CCGT`, `OCGT`, boilers, CHP, `SMR`, `gas for industry`, `kerosene for aviation`, `agriculture machinery oil`, `land transport oil`, `shipping oil`, `shipping methanol`, `industry methanol`, `methanol-to-kerosene`, `OCGT/CCGT methanol`, `Methanol steam reforming`, ...), fossil (`coal`, `lignite`, `oil refining`, `nuclear`), `process emissions`, `HVC to air`, `waste CHP`, `municipal solid waste` (D13), transport (`gas pipeline*`, `CO2 pipeline`, `co2 compression`, `co2 expansion`) | | none |

The general rule for G1 and G2 is
`atm_carrier = atm_technology + EF_out·out + used`, with `out` the bio-fuel output of G1
(0 for G2) and `used = −stored` the CO2 that G2 takes from `co2 stored` (0 for G1).
It is **additive**, so tiny inconsistencies in the cost data stay as they are and P5 holds
exactly. Residuals measured for 2050: |atm_carrier + stored| ≤ 2·10⁻⁴ t/MWh for G1
(biomass-to-methanol), exactly 0 for G2.

The transformation classes are **disjoint**: every link is transformed once, by its class.

Capture keeps its **immediate credit** under `carrier` (D17): capture plants book their
residual emissions as in `technology` (DAC −1 per t). Every tonne in `co2 stored` ends either
in sequestration or in a synthetic fuel producer (CO2 stores are cyclic). The G2 charge cancels
the credit for the utilised part, so a country keeps the credit for the CO2 it stores or
exports, but not for the CO2 it uses itself. CO2 that is exported and used abroad is charged
to the importing country. This needs no constraint and no tracing of CO2 through the
CO2 network; exact tracing isn't linear, because CO2 pipelines are bidirectional.

Check of P5 per chain (carbon units):

| Chain | `technology` | `carrier` |
|---|---|---|
| Fossil + CCS (capture rate c) | 1−c | 1−c |
| Fossil + CCU → FT → burned | (1−c) + c = 1 | (1−c) + c (FT) + 1 (use) − 1 (claim) = 1 |
| DAC → FT → burned | −1 + 1 = 0 | −1 + 1 (FT) + 1 − 1 = 0 |
| DAC → storage | −1 | −1 |
| Biogas upgrading → burned | −1 + 1 = 0 | 0 + 1 − 1 = 0 |
| Biogas CC (stored share s) | −1 − s + 1 = −s | −s + 1 − 1 = −s |
| DAC → FT → naphtha → plastics (share p re-emitted) | −1 + p | −1 + 1 (FT) + p − 1 (claim) = p − 1 |

### 3.3 Accounting layer

Physical flows stay unchanged. Green producers still deliver to their `gas`, `oil` or
`methanol` bus, and the gas grid and CO2 network stay intact. The accounting layer is built
from ordinary PyPSA components, so `n.statistics` and the evals see it without special handling.

**Components** (`CT` = two-letter country code, from the first two characters of the node name):

| Component | Name | Bus(es) | Parameters |
|---|---|---|---|
| Bus | `{CT} renewable gas attribute` | — | `carrier="renewable gas attribute"`, `unit="MWh"`, `location=CT` |
| Bus (pool) | `EU renewable oil attribute`, `EU renewable methanol attribute` | — | carriers `renewable oil attribute`, `renewable methanol attribute`, `location=EU` |
| Bus (country) | `{CT} renewable oil attribute`, `{CT} renewable methanol attribute` | — | same carriers, `location=CT` |
| Link (distribution) | `EU renewable oil attribute -> {CT}`, and the same for methanol | pool → country bus | `efficiency = 1`, `p_nom = 1e9`, no bound (D3) |
| Store | one per pool bus and per `{CT} renewable gas attribute`, same name | attribute bus | `e_nom_extendable=True`, `capital_cost=0`, `e_cyclic=True` |
| Attribute port (G1–G3) | extra port `bus{k}` on the producer link | attribute bus of the producer's country (gas) or EU pool | `efficiency{k} = out` (MWh attribute per MWh fuel) |
| CCU port (G2) | extra port `bus{k}` on the synthetic fuel producer | `co2 atmosphere` | `efficiency{k} = −stored` (t CO2 used per unit of `p0`, 3.2) |
| Link (claim) | `{CT} renewable gas claim`, `{CT} renewable oil claim`, `{CT} renewable oil claim aviation`, `{CT} renewable methanol claim` | bus0 `{CT} … attribute` (country bus), bus1 `co2 atmosphere` | `efficiency = −EF`, `p_nom = 1e9` |
| Claim cost (D16) | `marginal_cost` of the claim links of countries with a national budget | | `claim_cost × EF`, i.e. EUR per t credited |
| Link (transfer) | `{X} renewable gas attribute -> {Y}` for every pair X≠Y connected by a gas pipeline | `{X} … attribute` → `{Y} … attribute` | `efficiency = 1`, `p_nom = 1e9` |
| Carriers | the attribute, claim and transfer carriers above, `{attribute carrier} distribution` and `renewable gas attribute transfer`, plus `co2 import` | | colours in `config/plotting.at.yaml` |

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
3. **Full retirement (D5)** follows from the cyclic attribute stores: over the year,
   attributes generated = attributes claimed. No extra constraint is needed. Attributes cannot
   leave the system any other way (no sink, no vent).
4. **Aviation (D8)** in `co2_budget.py`: carrier `renewable oil claim aviation` is excluded from
   the per-port loop and added scaled by α, like `kerosene for aviation`.
   `methanol-to-kerosene` stays unscaled (D11); green methanol for it is claimed via
   `renewable methanol claim` at full EF.

!!! note "Endogenous allocation"
    Within the bounds, the optimiser assigns credits to the country where they save the most,
    i.e. where the national budget binds. For oil and methanol this effectively works
    like emission trading between AT and DE. Countries without a national budget don't compete
    for credits (D14, accepted and reported in the evals).

    While no national budget binds, the optimiser is indifferent to who claims. A first run
    without a tie-break gave AT 28.6 Mt of oil credits in 2025 (1.1 Mt at the EU-average
    share). Therefore the claims of countries with a national budget carry a small cost
    (`claim_cost`, D16): they claim only what their budget needs.

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

- **Full retirement:** guaranteed by cyclic attribute stores (3.3, item 3).
- **Capture and utilisation:** every tonne captured into `co2 stored` is either sequestered
  (credited at capture) or used by a G2 producer (credited at capture, charged at G2), so the
  EU total stays that of `technology` (D17).
- **Consistent producers:** G1, G2 and G3 have carbon inputs that match `EF_out` by
  construction (measured residual ≤ 2·10⁻⁴ t/MWh). The technology-data value
  `Fischer-Tropsch` `carbondioxide-input = 0.326` is not used by the model.
- **`methanol-to-kerosene`:** consumes 1.0764 × 0.248 = 0.267 t of methanol carbon per MWh
  kerosene but books 0.2571 t, so 3.7 % of the carbon vanishes. This is kept (D12). It is the
  same in both methods, so it doesn't affect the comparison.

### 3.6 Fair share (optional, D18)

By default, countries with a national budget claim pooled credits up to their own consumption
(D14). In CA_carrier 2025, Austria claims 68 TWh of green oil, against 3.1 TWh at the fair
share defined below. The option `fair_share` caps these claims at the country's share of the
EU pool.

**Natural definition and why it isn't used.** The fair claim is
`consumption(CT, f) × green production(EU, f) / consumption(EU, f)`. Consumption and green
production are both decision variables, so the cap would be bilinear.

**Linear definition: key × pool.** For each claim kind k of a pooled fuel f(k)
(`oil`, `oil aviation`, `methanol`):

`P({CT} renewable k claim) ≤ tolerance · w(CT, k) · G_f`

- `G_f = Σ_l Σ_t w_t · efficiency_attr(l) · p0(l, t)` over all attribute ports into
  `EU renewable f attribute` (producers and imports): the annual green production of f.
  It is a decision variable, so the cap scales with the green fuel the model actually builds.
- `w(CT, k)` is a fixed **key**: the country's share of the EU's fixed demand of fuel f, which
  is known before the solve.

**Key.** The key is computed in the compose phase from the network's own loads (D18):

| Claim kind | Fixed demand counted (load on the bus of the consumer) |
|---|---|
| `oil` | `land transport oil`, `naphtha for industry`, `shipping oil`, `agriculture machinery oil` |
| `oil aviation` | `kerosene for aviation` |
| `methanol` | `industry methanol`, `shipping methanol` |

`D(CT, k)` = Σ_t w_t · p_set of these loads in CT. The key is normalised per fuel, over both oil kinds:
`w(CT, k) = D(CT, k) / Σ_CT' Σ_{k' of f} D(CT', k')`, so the keys of a fuel sum to 1 and the caps of
all countries together equal G_f.

- These loads have one supplying link each (consumer efficiency 1), so load = fuel consumption.
  They cover 88 % of EU oil consumption in 2030 and 100 % in 2050, and all fixed methanol demand.
- Not counted, because the model decides them: oil boilers and CHP (2030: 575 TWh, 11 %),
  methanol in power plants and steam reforming, and `methanol-to-kerosene`. The kerosene load
  counts towards oil in full, although `methanol-to-kerosene` can serve part of it.
- The key uses the current horizon, so it is available in the first horizon (2025) and doesn't lag.

**Scope.**

- Only countries with a national budget (AT, DE) are capped. The other countries take the rest
  of the pool, so full retirement (D5) stays feasible. Capping all countries at tolerance 1 would
  leave no slack.
- Only the pooled fuels oil (including kerosene) and methanol. Gas is already limited by the
  physical gas flows (D1).
- The cap applies in addition to the consumption bound (3.3, item 1) and the claim cost (D16).

**Implementation sketch.**

- Compose (`mods/network/carbon_accounting.py`): `fair_share_keys(n)` computes `w` from the loads
  and stores it in a new column `fair_share_key` of the claim links (NaN where no key applies).
  The column survives the netCDF round trip like the transformation marker. No new rule inputs.
  A budget country without fixed demand of a fuel gets key 0, so it can't claim that pool.
- Solve (`mods/constraints/carbon_accounting.py`): for every claim link of a budget country with
  a key, add `carbon_accounting-fair-share-{CT}-{kind}`. Guard: `fair_share.enable`.
- Evals: `view_balance_carbon_claims` additionally shows the cap (`tolerance · w · G_f`) as a third
  group next to the claimed and EU-average credits.

## 4. Configuration

```yaml
mods:
  carbon_accounting:
    enable: true
    method: technology   # technology | carrier
    claim_cost: 0.01     # EUR/t CO2 on claims of countries with a national budget (D16)
    fair_share:          # cap on pooled claims of budget countries (3.6, D18)
      enable: false      # false: D14, claims bounded by consumption only
      tolerance: 1.0     # cap = tolerance × key × EU green production
```

- `fair_share` only acts with `method: carrier`; under `technology` there are no claims.
- `tolerance < 1` is allowed (stricter than the fair share); `tolerance ≤ 0` raises a `ValueError`.

- `enable: false` or `method: technology` → the orchestrator returns early. The network is
  bit-identical to today. `technology` is the default in `config.at.yaml` (D6).
- An unknown `method` raises a `ValueError` (fail early).
- `config/scenarios.carbon-accounting.yaml`: `CA_base` (technology, the reference) and
  `CA_carrier` (`mods.carbon_accounting.method: carrier`), both listed under `run.name`.
  `CA_base` keeps its name so that existing results stay valid. A third scenario
  `CA_carrier_fair` (`method: carrier`, `fair_share.enable: true`) shows the effect of the cap.

## 5. Implementation design

### 5.1 Modules and functions

`mods/network/carbon_accounting.py` (compose phase):

```python
def apply_carbon_accounting(n: pypsa.Network, snakemake, costs: pd.DataFrame) -> None:
    """Orchestrator: guard on enable/method, check the classification, then call the steps below in order."""


def drop_accounting_layer(n) -> None:
    """Safety net: remove accounting buses/stores/links by carrier if present."""


def add_accounting_layer(n, ef: dict[str, float]) -> None:
    """Add attribute buses, stores, claim, distribution and transfer links, the co2 import bus/generator."""


def transform_emission_ports(n, ef: dict[str, float]) -> None:
    """Apply the rules of 3.2 to G1 and G2. Skips links marked as transformed."""


def add_attribute_ports(n) -> None:
    """Add the attribute port to G1–G3; re-point G3 bus0. Skips marked links."""


def mark_transformed(n) -> None:
    """Set the marker on all links of G1–G3."""


def price_claims(n, countries: list[str], claim_cost: float) -> None:
    """Put the claim cost (D16) on the claims of countries with a national budget."""
```

**Transformation marker:** every link of G1, G2 or G3 gets
`n.links["carbon_accounting"] = "carrier"` once all steps ran (`mark_transformed`). The
transformation steps only touch links without this value. Untransformed links hold NaN, which
becomes an empty string after a netCDF round trip, so the check is `!= "carrier"`. This keeps
the CCU port of G2 from being added twice.
Verified: the custom column survives `export_to_netcdf`/`Network(...)`, and `add_brownfield`
copies all static columns (`n.add(c.name, c.static.index, **c.static)`).

- Called in `mods.modify_prenetwork` (`mods/network/common.py`) as the **last** step before
  `clip_negative_loads_for_edge_cases`, so every link exists, including brownfield links.
- Port helper: use the next free `bus{k}` index per link; create the columns `bus{k}` and
  `efficiency{k}` on `n.links` if they're missing. Unconnected ports count as efficiency 0:
  links added by `add_brownfield` hold NaN in extra port columns.
- Fail early: a link carrier at a CO2 or fuel bus that is in no class raises a `ValueError`.
- EF from the cost data that `modify_prenetwork` already loaded and passes in.

`mods/constraints/carbon_accounting.py` (solve phase):

```python
def constraint_carbon_accounting(n, snakemake, investment_year: int) -> None:
    """Guard on enable/method; add constraints 3.3 (1) and (2) to n.model."""
```

- Registered in `mods/__init__.py` and called in `scripts/pypsa-at/additional_functionality.py`
  after `constraint_national_co2_budgets`.
- Constraint names: `carbon_accounting-claim-{CT}-{fuel}`, `carbon_accounting-transfer-{X}-{Y}`.

`mods/constraints/co2_budget.py`: exclude `renewable oil claim aviation` from the port loop,
add it scaled by α (the same code path as `kerosene for aviation`). In `technology` mode the
carrier doesn't exist, so the change is a no-op.

`mods/constants.py`: the carrier lists of 3.2 (`GREEN_BIO_PRODUCERS`, `GREEN_SYNTHETIC_PRODUCERS`,
`GREEN_IMPORTS`, `CAPTURE`, `SEQUESTRATION`, `VENT`, `FUEL_TRANSPORT`) and the accounting carriers.

`mods/constants.py`: also the marker column name.

`evals/`:

- CO2 balance view (`view_balance_carbon` in `evals/views/balances.py`): claim carriers appear
  as their own categories, with nice names and colours. The α scaling applies to
  `kerosene for aviation` **and** `renewable oil claim aviation` (same mask), so the national
  report matches the constraint.
- Country attribution follows from the country claim buses (3.3), so `get_location` needs no change.
- For D14, the view `view_balance_carbon_claims` reports per country and fuel the claimed
  credits next to the credits at the EU-average green share (EU claims / EU consumption, equal
  to EU green production with full retirement), in Mt CO2 (`evals.utils.carbon_accounting_claims`).
- Check that attribute buses don't show up in the energy balances or the sankey. They have their
  own bus carriers, so they shouldn't.

### 5.2 Myopic foresight and brownfield

`add_brownfield` copies the links of the previous horizon before `modify_prenetwork` runs.

- Copied G1–G3 links were already transformed and carry the marker (5.1), so they
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
| U2 | Rules per class, parametrised over the carriers of 3.2 | `atm` after transformation per the formula; capture, sequestration and vent unchanged; attribute ports present |
| U3 | Idempotency | running the transformation twice gives the same network, explicitly including the CCU port of G2 |
| U4 | Brownfield | copied, already transformed links (G1, G2) keep their coefficients and the marker; the accounting layer exists exactly once |
| U5 | Classification complete | every carrier with a port at a CO2 or fuel bus is in exactly one transformation class |
| U6 | Unknown method | `ValueError` |
| U7 | Toy solve (HiGHS): 2 countries, biogas in X, consumer in Y, one pipeline | X and Y each book 0; the claim is in Y; P5 holds |
| U8 | Toy solve: gas transfer bound | without a pipeline flow X→Y, Y can't claim X's attributes |
| U9 | Toy solve: capture and use | DAC in X, part used by Sabatier in X, the rest sequestered in Z → X books −(CO2 exported), Z books 0, P5 holds |
| U10 | Aviation | the `renewable oil claim aviation` contribution is scaled by α in the national LHS and in `view_balance_carbon` |
| U11 | Country attribution | `get_location` returns CT, not EU, for all claim links (gas, oil, methanol) |
| U12 | Fair share keys | keys from the fixed loads of 3.6; per fuel they sum to 1 (both oil kinds together); countries without fixed demand get key 0 (capped at zero); gas claims get no key |
| U13 | Toy solve: fair share | pool of 20 MWh green oil, AT key 0.25, a reward on AT claims → AT claims 5 MWh, DE takes the rest; with `tolerance: 2` AT claims 10 |
| U14 | Fair share guard | `fair_share.enable: false` or `method: technology` adds no fair share constraint; `tolerance ≤ 0` raises |

Integration tests (`-m AT`, `--result-path=results/carbon-accounting/CA_carrier`):

| # | Test | Asserts |
|---|---|---|
| I1 | P5 | EU sum of weighted flows at `co2 atmosphere` equals the technology equivalent, rebuilt from the inverse rule of 3.2; relative tolerance 1e-6 |
| I2 | Retirement | the annual net change of every attribute store is 0 |
| I3 | Sign | only claim links, capture (C1 and bio-CC) and the retained `municipal solid waste` credit (D13) have negative weighted flows into `co2 atmosphere` (G1 residuals ≤ 1e-3 t/MWh tolerated) |
| I4 | Gas circulation | per border, the smaller of the two directional annual gas flows is reported; a warning above 5 % of the larger one (see 3.3, item 2) |
| I5 | National budgets | the AT/DE LHS of `co2_limit-*` matches the evals CO2 balance per country |
| I6 | Fair share | on a run with `fair_share.enable`, the AT/DE pooled claims are ≤ `tolerance · w · G_f` (tolerance 1e-6 relative) |

## 7. Work packages

Each package is one PR with tests and passes `ruff`, the unit tests and a dry run.

1. **Config and scaffold:** the `mods.carbon_accounting` block, scenarios `CA_technology`/`CA_carrier`,
   the orchestrator with its guard, constants and classification test (U1, U5, U6).
2. **Emission port transformation:** `transform_emission_ports` with idempotency (U2, U3).
3. **Accounting layer, gas:** attribute buses/stores, ports, claims, transfers, the claim and
   transfer constraints (U7, U8).
4. **Oil and methanol pool, imports:** EU pools with country claim buses and distribution links,
   the aviation split, `co2 import` (U10, U11).
5. **Capture and utilisation:** the CCU charge on G2 (U9). It replaced a removal pool whose
   claims were bounded by gross capture (D15, superseded by D17).
6. **Brownfield, evals, docs:** `drop_accounting_layer` (U4), the evals views, a section in
   `co2-balancing.md`, a changelog entry; run `CA_technology` and `CA_carrier`, integration tests I1–I5.
   Evals priority: (1) national CO2 balance of AT/DE by sector under both methods, including
   claims and capture, (2) claimed green shares vs. EU average (D14). System changes and
   costs/CO2 prices come later.

## 8. Decisions

| ID | Topic | Status |
|---|---|---|
| D0 | Mechanism: mass balance | decided |
| D0b | CCU: synthetic fuels green, no capture credit for utilised CO2 | decided |
| D0c | Permanent removals stay negative | decided |
| D0d | Scope: method applies to the whole model | decided |
| D1 | Mass balance granularity: per country | decided |
| D2 | Removal credit is generated at sequestration; capture gets no immediate credit | superseded by D17 |
| D3 | Oil/methanol attributes transfer freely (EU pool) | decided |
| D4 | All bounds are annual | decided |
| D5 | Full retirement of all attributes | decided |
| D6 | Default in `config.at.yaml`: `technology` | decided |
| D7 | Claims cover all consumption of a carrier, including non-energy use | decided |
| D8 | α also applies to kerosene claims | decided |
| D9 | Removal credits go to the capturing country (implemented through D17) | decided |
| D10 | Green imports: credit goes to the country that burns the fuel | decided |
| D11 | `methanol-to-kerosene` stays unscaled by α | decided |
| D12 | The 3.7 % carbon loss in `methanol-to-kerosene` is kept | decided |
| D13 | The `municipal solid waste` → HVC → `waste CHP`/`HVC to air` chain stays as it is: the credit and the emission arise at the same node, so national results don't change | decided |
| D14 | No competition for pooled credits is modelled; by default no fair-share cap (optional cap: D18). Only AT and DE have national budgets, so they can claim pooled credits up to their own consumption. This mirrors the status quo with the sign flipped: under `technology`, producing green fuels for export is rewarded at home; under `carrier`, consuming them is. This asymmetry is one of the differences the comparison is meant to show, and the evals report it (5.1) | decided |
| D15 | Removal credits: EU pool, each country claims at most its own gross capture. The bound let a country claim credits for CO2 it used for synthetic fuels itself, taken from other countries' sequestration (AT 2050: 1.5 Mt) | superseded by D17 |
| D16 | Tie-break: claims of countries with a national budget cost `claim_cost` = 0.01 EUR/t, so they claim pooled credits only when their budget needs them. Without it, the split is arbitrary while no budget binds | decided |
| D17 | Captured CO2 counts as negative emissions in the capturing country unless it is used for a synthetic fuel. Capture keeps its `technology` coefficients; G2 producers book the CO2 they use at `co2 atmosphere` in their own country. No removal pool, claim or bound. CO2 that is exported and used abroad is charged to the importing country | decided |
| D18 | Optional fair share (`fair_share.enable`, default off): pooled oil, kerosene and methanol claims of budget countries ≤ tolerance × key × EU green production. The key is the country's share of the EU's fixed demand in the current horizon (3.6). Countries without a budget are not capped | decided |
