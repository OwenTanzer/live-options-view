# Compare manager packages and dealer hedges

Run `python -m research.options_replay.participant_models` from the repository root. It emits four JSON lines, one per manager/dealer pairing facing identical inputs. Supply a JSON path as the sole argument to change the scenario. The bundled vertical call spread illustrates the accounting, not a reconstruction of a particular fund or its full payout.

A scheduled manager takes the supplied package count at each reset and holds through the period. A flow-adjusted manager also fills the supplied **primary-market cash flow**, measured in package units, at each tick. Secondary-market turnover does not imply such a flow. A cash-settled series expires at reset without printing a closing trade. The replacement series starts a fresh holdings ledger. Leg names are roles within each successive series.

A dealer takes the opposite side of every simulated option leg. Given each leg's current signed long-option delta, the dealer either neutralizes its total delta at each observation or trades underlying shares only to the nearest edge of a no-trade band. The hedge persists across resets. Positive stock orders buy shares. Each matched option leg contributes its absolute size to options volume once. Stock hedges and settled positions contribute zero option volume.

The example changes outstanding flow-adjusted units from 2 to 3 to 2 while the scheduled manager stays at 2. At the first tick both have a 100-share neutralizing target. The immediate hedge buys 100 shares and the 10-share band buys 90. Both produce the same option tape for a given manager.

## Scenario fields

`multiplier` converts one option delta into shares per contract, and `band_shares` is the assumed half-width. `legs` maps each leg role to its signed integer number of contracts per package. Each step has a strictly increasing numeric `tick` and a `deltas` map for all current legs. A `reset_units` value begins a new period; the first row must have one. `cash_flow_units` is a signed creation/redemption scenario input. If present at a reset it is incremental to the new base. Both models initially have zero stock and zero options. All fills are hypothetical, fractional stock hedges are permitted, and execution and matching are immediate.

Input deltas, resets, cash flows and counterparty assignment are conditioned assumptions. The model does not estimate these fields, simulate price impact, or identify a trader in recorded prints. Prospectus resets motivate the manager variants; actual creations can be in kind. The fixed-width band illustrates the proportional-cost policy family, not an estimated optimal width.

Sources: [Innovator prospectus, investment strategy and creation units](https://www.sec.gov/Archives/edgar/data/1415726/000121390026021926/ea0276070-02_497.htm) and [Whalley and Wilmott, hedging with transaction costs](https://users.ox.ac.uk/~ofrcinfo/file_links/mf_papers/1999mf09.pdf).
