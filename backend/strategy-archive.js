import { checkOrigin, jsonResponse, readLimited, requireSession } from './common.js';

const idPattern = /^[a-z0-9][a-z0-9._-]{0,95}$/;
const object = (x) => x !== null && typeof x === 'object' && !Array.isArray(x);
const string = (x) => typeof x === 'string' && x.trim().length > 0;
function requireValue(condition, message) {
  if (!condition) throw new RangeError(message);
}
function sorted(value) {
  if (Array.isArray(value)) return value.map(sorted);
  if (!object(value)) return value;
  return Object.fromEntries(
    Object.keys(value)
      .sort()
      .map((k) => [k, sorted(value[k])]),
  );
}
function fields(data, names) {
  for (const name of names) requireValue(string(data[name]), 'Missing ' + name);
}
function source(data) {
  fields(data, ['source']);
  requireValue(/^https?:\/\//i.test(data.source) && URL.canParse(data.source), 'Invalid source URL');
}
function validate(row) {
  requireValue(
    object(row) && string(row.id) && idPattern.test(row.id) && object(row.data),
    'Expected id and data',
  );
  const d = row.data;
  if (row.kind !== 'version')
    requireValue(string(row.version) && idPattern.test(row.version), 'Missing version');
  switch (row.kind) {
    case 'version':
      requireValue(row.version == null, 'Version definition cannot target another version');
      fields(d, ['family', 'originator']);
      source(d);
      requireValue(['documented', 'proposed'].includes(d.stage), 'Invalid stage');
      requireValue(
        d.parent == null ||
          (string(d.parent) && idPattern.test(d.parent) && d.parent !== row.id && string(d.reason)),
        'Fork needs a parent and reason',
      );
      requireValue(object(d.parameters), 'Expected parameters object');
      for (const [name, p] of Object.entries(d.parameters)) {
        requireValue(
          string(name) && object(p) && ['integer', 'number', 'category', 'boolean'].includes(p.type),
          'Invalid parameter ' + name,
        );
        requireValue(
          p.state == null || ['known', 'unknown', 'inapplicable'].includes(p.state),
          'Invalid state for ' + name,
        );
        if (p.unit != null) requireValue(string(p.unit), 'Expected unit for ' + name);
        if (p.when != null) requireValue(string(p.when), 'Expected applicability condition for ' + name);
        if (p.domain != null) {
          requireValue(object(p.domain), 'Expected domain for ' + name);
          const { min, max, choices } = p.domain;
          requireValue(
            (min == null || Number.isFinite(min)) &&
              (max == null || Number.isFinite(max)) &&
              (min == null || max == null || min <= max),
            'Invalid domain bounds for ' + name,
          );
          requireValue(
            choices == null || (Array.isArray(choices) && choices.length > 0),
            'Invalid choices for ' + name,
          );
        }
        if (['unknown', 'inapplicable'].includes(p.state)) {
          requireValue(p.value == null, 'Unavailable parameter has a value');
          continue;
        }
        const valid = {
          integer: Number.isInteger,
          number: Number.isFinite,
          category: string,
          boolean: (v) => typeof v === 'boolean',
        };
        requireValue(valid[p.type](p.value), 'Invalid value for ' + name);
      }
      break;
    case 'observation':
      source(d);
      fields(d, ['originator', 'asOf', 'summary']);
      break;
    case 'evidence':
      source(d);
      fields(d, ['resultRef', 'period', 'sample', 'summary', 'assumptions']);
      requireValue(
        ['external', 'local'].includes(d.scope) &&
          ['paper', 'backtest', 'live', 'observation'].includes(d.kind),
        'Invalid evidence classification',
      );
      break;
    case 'review':
      fields(d, ['reason', 'owner']);
      requireValue(
        ['adopt', 'try', 'modify', 'defer', 'reject'].includes(d.decision),
        'Invalid decision',
      );
      break;
    case 'trial':
      fields(d, ['owner', 'trialId', 'asOf']);
      source(d);
      requireValue(
        ['assigned', 'active', 'completed', 'paused', 'cancelled'].includes(d.status),
        'Invalid trial status',
      );
      requireValue(/^\d{4}-\d{2}-\d{2}$/.test(d.asOf), 'Expected trial date YYYY-MM-DD');
      break;
    default:
      throw new RangeError('Unknown record kind');
  }
}
export async function submitRecord(db, row, actor) {
  validate(row);
  const { id, kind, data } = row;
  const version = row.version ?? null;
  const target = version ?? data.parent;
  if (target) {
    const parent = await db
      .prepare("SELECT data FROM strategy_archive WHERE id=? AND kind='version'")
      .bind(target)
      .first();
    requireValue(parent, 'Unknown version ' + target);
    if (kind === 'version')
      requireValue(JSON.parse(parent.data).family === data.family, 'Fork family differs from parent');
  }
  const payload = JSON.stringify(sorted(data));
  const insert = db
    .prepare(
      'INSERT INTO strategy_archive(id,kind,version,data,submitted_by) VALUES (?,?,?,?,?) ON CONFLICT DO NOTHING',
    )
    .bind(id, kind, version, payload, actor);
  // Attribute a retry only when the stored immutable record actually matches.
  const match =
    kind === 'evidence'
      ? "kind='evidence' AND version=? AND json_extract(data,'$.source')=? AND json_extract(data,'$.resultRef')=?"
      : 'id=?';
  const keys = kind === 'evidence' ? [version, data.source, data.resultRef] : [id];
  const credit = db
    .prepare(
      'INSERT OR IGNORE INTO strategy_archive_credit SELECT id,? FROM strategy_archive WHERE ' +
        match +
        ' AND kind=? AND version IS ? AND data=?',
    )
    .bind(actor, ...keys, kind, version, payload);
  await db.batch([insert, credit]);
  const stored = await db
    .prepare('SELECT id,kind,version,data FROM strategy_archive WHERE ' + match)
    .bind(...keys)
    .first();
  requireValue(
    stored && stored.kind === kind && stored.version === version && stored.data === payload,
    'Conflicting record; inspect before merging',
  );
  if (kind === 'version' && !data.parent) {
    const aliases = await db
      .prepare(
        `SELECT id FROM strategy_archive
      WHERE kind='version' AND id<>? AND json_extract(data,'$.source')=?
      AND json_extract(data,'$.parent') IS NULL ORDER BY seq LIMIT 10`,
      )
      .bind(id, data.source)
      .all();
    if (aliases.results.length)
      return { id: stored.id, possibleAliases: aliases.results.map((r) => r.id) };
  }
  return { id: stored.id };
}
export async function archive(request, env) {
  const session = await requireSession(request, env);
  if (!session) return jsonResponse({ error: 'Sign in to Options View first' }, 401);
  const members = (env.STRATEGY_ARCHIVE_MEMBERS || '')
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean);
  if (!members.includes(session.username))
    return jsonResponse({ error: 'Archive membership required' }, 403);
  if (request.method === 'GET') {
    const cursor = Number(new URL(request.url).searchParams.get('after') || 0);
    if (!Number.isSafeInteger(cursor) || cursor < 0)
      return jsonResponse({ error: 'Invalid cursor' }, 400);
    const params = new URL(request.url).searchParams;
    const version = params.get('version'),
      family = params.get('family');
    const filters = ['a.seq>?'],
      bindings = [cursor];
    if (version) {
      filters.push('(a.id=? OR a.version=?)');
      bindings.push(version, version);
    }
    if (family) {
      filters.push(
        "COALESCE(a.version,a.id) IN (SELECT id FROM strategy_archive WHERE kind='version' AND json_extract(data,'$.family')=?)",
      );
      bindings.push(family);
    }
    const { results } = await env.DB.prepare(
      `SELECT a.*, (SELECT json_group_array(submitter) FROM strategy_archive_credit WHERE record_id=a.id) AS credits FROM strategy_archive a WHERE ${filters.join(' AND ')} ORDER BY seq LIMIT 100`,
    )
      .bind(...bindings)
      .all();
    const records = results.map((r) => ({
      ...r,
      data: JSON.parse(r.data),
      credits: JSON.parse(r.credits),
    }));
    return jsonResponse({ records, next: records.length === 100 ? records.at(-1).seq : null }, 200);
  }
  if (request.method !== 'POST') return jsonResponse({ error: 'Use GET or POST' }, 405);
  const forbidden = checkOrigin(request);
  if (forbidden) return forbidden;
  if (!request.headers.get('content-type')?.startsWith('application/json'))
    return jsonResponse({ error: 'Expected application/json' }, 415);
  let raw;
  try {
    raw = await readLimited(request, 192 * 1024);
  } catch {
    return jsonResponse({ error: 'Batch too large' }, 413);
  }
  let rows;
  try {
    rows = JSON.parse(raw);
  } catch {
    return jsonResponse({ error: 'Invalid JSON' }, 400);
  }
  if (!Array.isArray(rows)) rows = [rows];
  if (!rows.length || rows.length > 40)
    return jsonResponse({ error: 'Submit 1–40 records per batch' }, 400);
  const results = [];
  for (const row of rows) {
    try {
      results.push(await submitRecord(env.DB, row, session.username));
    } catch (e) {
      if (!(e instanceof RangeError)) throw e;
      results.push({ id: row?.id ?? null, error: e.message });
    }
  }
  return jsonResponse({ results }, 200);
}
