// Sept 25 exploratory: the two visits to 744, inputs only (no price after the anchor is read).
//   node research/oa241/strike_test/sept25_744.js > research/oa241/strike_test/sept25_744.json
// At each 5-minute anchor 09:45-15:50: spot, the old G5c reference strike K* (largest |100% flow
// gamma| within $2), whether its scenarios agree in sign, and whether the old distance-matched
// control strike (another covered strike within $0.50 of K*'s distance) exists.
const fs = require('fs');
const path = require('path');
const root = path.join(__dirname, '..', '..', '..');
const M = require(path.join(root, 'site/shared/order-flow-model.js'));
const r = M.decodeReplay(JSON.parse(fs.readFileSync(path.join(root, 'site/order-flow-replay.json'), 'utf8')));
const et = (t) => new Date(t - 4 * 3600e3).toISOString().slice(11, 16); // EDT on Sept 25

const rows = [];
for (let m = 15; m <= 380; m += 5) {
  const t = r.coverage.open + m * 60000, v = M.replayAt(r, t);
  const S = v.contracts.find((c) => c.spot != null)?.spot ?? null;
  if (S == null) { rows.push({ t: et(t), spot: null }); continue; }
  const elig = v.strikes.filter((s) => Math.abs(S - s.strike) <= 2 && s.flow[1].base != null);
  const k = [...elig].sort((a, b) => Math.abs(b.flow[1].base) - Math.abs(a.flow[1].base))[0];
  const d = k ? Math.abs(S - k.strike) : null;
  const control = k ? v.strikes.filter((s) => s.strike !== k.strike && Math.abs(Math.abs(S - s.strike) - d) <= 0.5)
    .sort((a, b) => Math.abs(Math.abs(S - a.strike) - d) - Math.abs(Math.abs(S - b.strike) - d))[0]?.strike ?? null : null;
  rows.push({
    t: et(t), spot: +S.toFixed(2), kstar: k?.strike ?? null, distance: d == null ? null : +d.toFixed(2),
    flow_m: k ? Math.round(k.flow[1].base / 1e6) : null, sign_uncertain: k ? k.flow[1].uncertain : null,
    agreeing_strikes_within_2: elig.filter((s) => !s.flow[1].uncertain).length, old_control: control,
  });
}
const ok = rows.filter((x) => x.kstar != null);
const near = (x) => Math.abs(x.spot - 744) <= 0.5;
const summary = {
  anchors: rows.length, kstar_events: ok.length,
  kstar_sign_uncertain: ok.filter((x) => x.sign_uncertain).length,
  anchors_with_any_agreeing_strike: ok.filter((x) => x.agreeing_strikes_within_2 > 0).length,
  kstar_within_025: ok.filter((x) => x.distance < 0.25).length,
  old_control_missing_when_within_025: ok.filter((x) => x.distance < 0.25 && x.old_control == null).length,
  anchors_spot_within_050_of_744: ok.filter(near).length,
  kstar_while_near_744: [...new Set(ok.filter(near).map((x) => x.kstar))].sort(),
};
process.stdout.write(JSON.stringify({ summary, anchors: rows }, null, 1) + '\n');
