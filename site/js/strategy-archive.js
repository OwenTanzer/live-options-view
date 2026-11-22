import { ruleFields } from '../shared/strategy-rule.js';
const $ = (id) => document.getElementById(id);
const endpoint = '/api/strategy-archive';
let records = [],
  versions = [],
  selected = null,
  ranges = [];
const linked = new Map();
const related = (id) => linked.get(id) || [];
const evidence = (id) => related(id).filter((r) => r.kind === 'evidence');
function evidenceSummary(id, localOnly = false) {
  const items = evidence(id).filter((e) => !localOnly || e.data.scope === 'local');
  const samples = [...new Set(items.map((e) => e.data.sample))];
  return (
    items.length +
    (localOnly ? ' local results' : ' results') +
    (samples.length ? ' · ' + samples.slice(0, 2).join(', ') + (samples.length > 2 ? ' …' : '') : '')
  );
}
function trials(id) {
  const latest = new Map();
  for (const r of related(id).filter((r) => r.kind === 'trial')) {
    if ((latest.get(r.data.trialId)?.data.asOf || '') <= r.data.asOf) latest.set(r.data.trialId, r);
  }
  return [...latest.values()].filter((r) => ['active', 'assigned', 'paused'].includes(r.data.status));
}
const review = (id) =>
  related(id)
    .filter((r) => r.kind === 'review')
    .at(-1);
const display = (x) => (x == null ? 'Unknown' : typeof x === 'object' ? JSON.stringify(x) : String(x));
const parameter = (v, k) => {
  const p = v.data.parameters[k];
  return p ? p.state || [p.value, p.unit].filter((x) => x != null).join(' ') : 'Not recorded';
};
function node(tag, value, parent) {
  const el = document.createElement(tag);
  if (value != null) el.textContent = value;
  if (parent) parent.append(el);
  return el;
}
function options(el, values, first) {
  const previous = el.value;
  el.replaceChildren();
  if (first) el.add(new Option(first, ''));
  for (const v of values) el.add(new Option(v, v));
  if ([...el.options].some((o) => o.value === previous)) el.value = previous;
}
function action(parent, label, fn) {
  const b = node('button', label, parent);
  b.className = 'link';
  b.onclick = fn;
  return b;
}
function download(name, data) {
  const url = URL.createObjectURL(
    new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' }),
  );
  const a = node('a');
  a.href = url;
  a.download = name + '.json';
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
async function request(options = {}, suffix = '') {
  const response = await fetch(endpoint + suffix, options);
  if (!response.ok)
    throw Error(
      (await response.json().catch(() => ({}))).error ||
        'Archive request failed (' + response.status + ')',
    );
  return response.json();
}
async function load() {
  const all = [];
  let next = 0;
  do {
    const page = await request({}, '?after=' + next);
    all.push(...page.records);
    next = page.next;
  } while (next !== null);
  records = all;
  versions = all.filter((r) => r.kind === 'version');
  linked.clear();
  for (const r of all)
    if (r.version) {
      if (!linked.has(r.version)) linked.set(r.version, []);
      linked.get(r.version).push(r);
    }
  options($('family'), [...new Set(versions.map((r) => r.data.family))].sort(), 'All families');
  for (const key of Object.keys(ruleFields)) {
    const values = versions.map((v) => v.data.rule?.[key]).filter(Boolean);
    const select = $('filter-' + key);
    const previous = select.value;
    options(select, [...new Set(values)].sort(), 'Any');
    select.add(new Option('Unknown (undocumented)', '__missing__'));
    if (previous === '__missing__') select.value = previous;
  }
  document.querySelector('main').hidden = false;
  axes();
  render();
}
function axes() {
  const family = versions.filter((v) => !$('family').value || v.data.family === $('family').value);
  const names = [...new Set(family.flatMap((v) => Object.keys(v.data.parameters)))].sort();
  options($('param'), names, 'Any parameter');
  options($('axisX'), names);
  options($('axisY'), names);
  if ($('axisY').value === $('axisX').value && names.length > 1) $('axisY').value = names[1];
}
function currentRange() {
  return { key: $('param').value, min: $('min').value, max: $('max').value };
}
function filtered() {
  const query = $('search').value.trim().toLowerCase();
  return versions.filter(
    (v) =>
      (!$('family').value || v.data.family === $('family').value) &&
      Object.keys(ruleFields).every((key) => {
        const filter = $('filter-' + key).value,
          value = v.data.rule?.[key];
        return !filter || (filter === '__missing__' ? !value : value === filter);
      }) &&
      (!$('reviewFilter').value ||
        (review(v.id)?.data.decision || 'Awaiting review') === $('reviewFilter').value) &&
      (!$('evidenceFilter').value ||
        ($('evidenceFilter').value === 'none'
          ? !evidence(v.id).length
          : evidence(v.id).some((e) => e.data.scope === $('evidenceFilter').value))) &&
      (!query ||
        JSON.stringify([v, related(v.id)])
          .toLowerCase()
          .includes(query)) &&
      [...ranges, currentRange()].every(({ key, min, max }) => {
        if (!key || (!min && !max)) return true;
        const value = v.data.parameters[key]?.value;
        return (
          Number.isFinite(value) && (!min || value >= Number(min)) && (!max || value <= Number(max))
        );
      }),
  );
}
function showCoverage(list) {
  const target = $('coverage');
  target.replaceChildren();
  const x = $('axisX').value,
    y = $('axisY').value;
  if (!$('family').value || !x || !y || x === y) {
    node(
      'p',
      'Choose one family and two different parameters to compare recorded configurations.',
      target,
    );
    return;
  }
  const columns = [...new Set(list.map((v) => parameter(v, x)))].sort();
  const rows = new Map();
  for (const v of list) {
    const key = parameter(v, y);
    if (!rows.has(key)) rows.set(key, new Map());
    const col = parameter(v, x),
      group = rows.get(key);
    if (!group.has(col)) group.set(col, []);
    group.get(col).push(v);
  }
  if (rows.size * columns.length > 2500) {
    node(
      'p',
      'More than 2,500 cells. Narrow the range to show the matrix. Recorded points are listed below.',
      target,
    );
    for (const v of list)
      action(
        node('div', null, target),
        [parameter(v, x), parameter(v, y), v.id, v.data.stage, trialSummary(v.id)].join(' · '),
        () => open(v),
      );
    return;
  }
  const table = node('table', null, target),
    head = node('tr', null, table);
  node('th', y + ' / ' + x, head);
  columns.forEach((c) => node('th', c, head));
  for (const [key, group] of rows) {
    const tr = node('tr', null, table);
    node('th', key, tr);
    for (const col of columns) {
      const cell = node('td', null, tr),
        matches = group.get(col) || [];
      if (!matches.length) cell.textContent = 'No indexed record';
      for (const v of matches)
        action(
          node('div', null, cell),
          [v.id, v.data.stage, trialSummary(v.id), evidenceSummary(v.id, true)].join(' · '),
          () => open(v),
        );
    }
  }
}
function render() {
  const list = filtered();
  $('rows').replaceChildren();
  $('ranges').textContent = ranges
    .map((r) => r.key + ' ' + (r.min || '−∞') + ' to ' + (r.max || '∞'))
    .join(' · ');
  const groups = new Map();
  for (const v of list) {
    const fields = Object.keys(ruleFields).map((key) => v.data.rule?.[key]?.toLowerCase());
    const identity = fields.every(Boolean) ? JSON.stringify([v.data.family, ...fields]) : v.id;
    if (!groups.has(identity)) groups.set(identity, []);
    groups.get(identity).push(v);
  }
  for (const group of groups.values()) {
    const header = node(
      'th',
      group[0].data.family + ' · ' + group.length + (group.length === 1 ? ' version' : ' versions'),
      node('tr', null, $('rows')),
    );
    header.colSpan = 6;
    for (const v of group) {
      const tr = node('tr', null, $('rows')),
        title = node('td', null, tr);
      action(title, v.id, () => open(v));
      node('div', v.data.family, title);
      const d = v.data;
      for (const value of [
        [d.instrument, d.horizon].filter(Boolean).join(' / '),
        [d.rule?.entryCondition || 'Unknown entry', d.rule?.exitCondition || 'Unknown exit'].join(' / '),
        d.originator,
        evidenceSummary(v.id) + ' · ' + trialSummary(v.id),
        review(v.id)?.data.decision || 'Awaiting review',
      ])
        node('td', value || 'Unknown', tr);
    }
  }
  if (!list.length) node('p', 'No matching versions.', $('ranges'));
  showCoverage(list);
}
function trialSummary(id) {
  return (
    trials(id)
      .map((t) => t.data.owner + ' (' + t.data.status + ')')
      .join(', ') || 'No open trial'
  );
}
function compare() {
  const other = versions.find((v) => v.id === $('compare').value),
    box = $('diff');
  box.replaceChildren();
  if (!other || !selected) return;
  const table = node('table', null, box),
    h = node('tr', null, table);
  ['Changed field', other.id, selected.id].forEach((s) => node('th', s, h));
  const flatten = (d) => ({
    ...Object.fromEntries(Object.entries(d).filter(([k]) => k !== 'parameters' && k !== 'rule')),
    ...Object.fromEntries(Object.entries(ruleFields).map(([key, label]) => [label, d.rule?.[key]])),
    ...Object.fromEntries(Object.entries(d.parameters).map(([k, p]) => ['Parameter · ' + k, p])),
  });
  const left = flatten(other.data),
    right = flatten(selected.data);
  for (const k of new Set([...Object.keys(left), ...Object.keys(right)]))
    if (display(left[k]) !== display(right[k])) {
      const row = node('tr', null, table);
      [k, display(left[k]), display(right[k])].forEach((s) => node('td', s, row));
    }
}
function open(v) {
  selected = v;
  $('detail').hidden = false;
  $('title').textContent = v.id;
  const facts = $('facts');
  facts.replaceChildren();
  for (const r of [v, ...related(v.id)]) {
    node('h3', r.kind + ' · ' + r.id, facts);
    node('small', r.created_at + ' UTC · submitted by ' + r.credits.join(', '), facts);
    if (r.kind === 'handoff') {
      const link=node('a','Open hypothesis in Event Builder',facts);
      link.href='/one-clock.html?strategy_handoff='+encodeURIComponent(r.id);
    }
    if (r.kind === 'version')
      for (const [key, label] of Object.entries(ruleFields)) {
        const line = node('div', null, facts);
        node('strong', label + '  ', line);
        node('span', r.data.rule?.[key] || 'Unknown', line);
        $('fork-' + key).value = r.data.rule?.[key] || '';
      }
    for (const [k, value] of Object.entries(r.data)) {
      if (k === 'rule') continue;
      const line = node('div', null, facts);
      node('strong', k + '  ', line);
      if (k === 'source' && /^https?:\/\//i.test(value)) {
        const a = node('a', value, line);
        a.href = value;
      } else node('span', display(value), line);
    }
  }
  $('forkParams').value = JSON.stringify(v.data.parameters, null, 2);
  for (const key of ['trialId', 'trialOwner', 'trialSource', 'trialDate']) $(key).value = '';
  $('trialStatus').value = 'assigned';
  $('forkReason').value = '';
  $('reason').value = '';
  for (const input of $('plan').querySelectorAll('input')) input.value = '';
  options(
    $('compare'),
    versions.filter((r) => r.data.family === v.data.family && r.id !== v.id).map((r) => r.id),
    'Choose version',
  );
  $('compare').value = v.data.parent || '';
  compare();
  $('detail').scrollIntoView();
}
async function submit(input) {
  const { results } = await request({
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  });
  await load();
  if (selected) open(versions.find((v) => v.id === selected.id));
  $('message').textContent = results
    .map(
      (r, i) =>
        i +
        1 +
        '. ' +
        (r.id || 'Record') +
        ' · ' +
        (r.error ||
          (r.differs
            ? 'Credited to the existing result; submitted wording differs'
            : r.possibleAliases
              ? 'Saved · review possible aliases: ' + r.possibleAliases.join(', ')
              : 'Saved')),
    )
    .join('\n');
  return results;
}
// Keep the same submission identity until an acknowledgement arrives.
const pendingForms = new Map();
async function submitForm(record) {
  const key = JSON.stringify(record);
  if (!pendingForms.has(key)) pendingForms.set(key, crypto.randomUUID());
  const result = await submit({ ...record, id: pendingForms.get(key) });
  pendingForms.delete(key);
  return result;
}
function run(fn) {
  return async (event) => {
    const button = event?.currentTarget;
    if (button) button.disabled = true;
    try {
      await fn();
    } catch (e) {
      $('message').textContent = e.message;
    } finally {
      if (button) button.disabled = false;
    }
  };
}
for (const [key, label] of Object.entries(ruleFields)) {
  const select = node('select', null, node('label', label, $('ruleFilters')));
  select.id = 'filter-' + key;
  select.onchange = render;
  const input = node('input', null, node('label', label, $('forkRule')));
  input.id = 'fork-' + key;
  input.placeholder = 'Unknown';
}
$('family').onchange = () => {
  axes();
  render();
};
for (const k of ['search', 'min', 'max', 'param', 'axisX', 'axisY', 'evidenceFilter', 'reviewFilter'])
  $(k).oninput = render;
$('compare').onchange = compare;
$('addFilter').onclick = () => {
  const r = currentRange();
  if (r.key && (r.min || r.max)) ranges.push(r);
  $('min').value = $('max').value = '';
  render();
};
$('reset').onclick = () => {
  ranges = [];
  for (const key of Object.keys(ruleFields)) $('filter-' + key).value = '';
  for (const id of ['min', 'max', 'search', 'family', 'param', 'evidenceFilter', 'reviewFilter'])
    $(id).value = '';
  axes();
  render();
};
$('export').onclick = () =>
  download(
    'strategy-archive',
    filtered().map((v) => ({ version: v, records: related(v.id) })),
  );
$('batch').value = JSON.stringify(
  {
    id: 'replace-with-stable-id',
    kind: 'version',
    data: {
      family: 'Family name',
      originator: 'Source author',
      stage: 'documented',
      source: 'https://example.org/strategy',
      rule: Object.fromEntries(Object.keys(ruleFields).map((k) => [k, null])),
      parameters: {},
    },
  },
  null,
  2,
);
$('submit').onclick = run(() => submit(JSON.parse($('batch').value)));
$('fork').onclick = run(async () => {
  const parent = selected;
  const [result] = await submitForm({
    kind: 'version',
    data: {
      ...parent.data,
      rule: Object.fromEntries(
        Object.keys(ruleFields).map((key) => [key, $('fork-' + key).value.trim() || null]),
      ),
      parent: parent.id,
      stage: 'proposed',
      reason: $('forkReason').value,
      parameters: JSON.parse($('forkParams').value),
    },
  });
  if (!result.error) open(versions.find((v) => v.id === result.id));
});
$('trial').onclick = run(() =>
  submitForm({
    kind: 'trial',
    version: selected.id,
    data: {
      owner: $('trialOwner').value,
      trialId: $('trialId').value,
      status: $('trialStatus').value,
      asOf: $('trialDate').value,
      source: $('trialSource').value,
    },
  }),
);
$('review').onclick = run(() =>
  submitForm({
    kind: 'review',
    version: selected.id,
    data: { decision: $('decision').value, reason: $('reason').value, owner: $('owner').value },
  }),
);
const design = ['Population', 'Trigger', 'Comparator', 'Outcome', 'Horizon', 'Evaluation plan'];
for (const name of design) {
  const label = node('label', name, $('plan'));
  node('input', null, label);
}
$('handoff').onclick = run(async () => {
  const values = [...$('plan').querySelectorAll('input')].map((i) => i.value.trim());
  if (values.some((v) => !v)) throw Error('Complete each hypothesis field before export.');
  const version = selected;
  const designRecord = Object.fromEntries(design.map((k, i) => [k, values[i]]));
  const [record] = await submitForm({
    kind: 'handoff',
    version: version.id,
    data: { design: designRecord },
  });
  if (record.error) return;
  download(version.id + '-hypothesis', {
    handoffId: record.id,
    version: version.id,
    configuration: version.data,
    design: designRecord,
    trials: related(version.id).filter((r) => r.kind === 'trial'),
    evidence: evidence(version.id),
  });
});
run(load)();
