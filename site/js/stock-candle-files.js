import {readPublishedCandles, selectCandlesAsOf} from './candle-cache-one-clock.js';

const form = document.getElementById('stock-candle-replay');
const status = document.getElementById('stock-candle-reading');
let generation = 0;
form.addEventListener('input', () => { generation++; status.textContent = ''; });
form.addEventListener('submit', async event => {
  event.preventDefault();
  const current = ++generation;
  const fields = new FormData(form);
  status.textContent = 'Reading archive…';
  try {
    const [bytes, text] = await Promise.all([
      fields.get('interval').arrayBuffer(), fields.get('receipt').text(),
    ]);
    const receipt = JSON.parse(text);
    const observations = await readPublishedCandles(bytes, receipt);
    if (current !== generation) return;
    if (!observations.length) {
      status.textContent = 'Unavailable. The receipt must identify these published interval bytes.';
      return;
    }
    const cutoff = Date.parse(fields.get('cutoff') + 'Z');
    const {rows, labels} = selectCandlesAsOf(observations, {
      session: fields.get('session'), instrument: receipt.symbol, cutoff,
    }, {maxAgeMs: 60000, sessionHours: receipt.sessions?.[fields.get('session')]});
    const last = rows.at(-1);
    status.textContent = last
      ? `${receipt.symbol} · ${rows.length} eligible bars · last close ${last.value.close} · bar ${new Date(last.eventAt).toISOString()} · available ${new Date(last.availableAt).toISOString()}${labels.length ? ' · ' + labels.join(', ') : ''}`
      : 'No bars were available for this trading session by the selected cutoff.';
  } catch (error) {
    if (current === generation) status.textContent = 'Archive unavailable. ' + error.message;
  }
});
