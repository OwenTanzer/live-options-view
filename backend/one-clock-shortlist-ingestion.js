// Publication boundaries, including periods where a pending pointer update
// prevents certification of the previously displayed shortlist.
import {publicationCatalogPage} from './one-clock-source-catalog.js';
import {squeezeCatalog} from './one-clock-catalog-sources.js';
import {readSqueezePublications} from './one-clock-squeeze.js';
import {sessionInstant} from '../site/shared/one-clock-session.js';
import {observation} from '../site/shared/one-clock.js';

export const shortlistIngestion = {
  id: 'squeeze-shortlist', owner: squeezeCatalog.owner, version: '1',
  revisionPolicy: 'retained-artifact', demandDriven: true, reuseObjects: true,
  outputs: ['shortlist'], instruments: ['QQQ', 'SPY', 'IBIT'],
  fields: {shortlist: {listed: 'Published shortlist membership', rank: 'Published rank',
    combined: 'Combined score', factor: 'Factor score', options: 'Options score'}},
  async discover(bucket, {session, cutoff}, db) {
    const state = await db.prepare('SELECT complete_through FROM one_clock_catalog_state WHERE source=?')
      .bind(squeezeCatalog.id).first();
    const start = sessionInstant(session, 0);
    const through = Math.min(cutoff, sessionInstant(session, 23, 59, 59, 999), state?.complete_through ?? 0);
    if (through < start) return {artifacts: [], complete: false, reason: 'catalog-catching-up'};
    const catalog = await publicationCatalogPage(db, squeezeCatalog, {start, cutoff: through});
    if (catalog.truncated) return {artifacts: [], complete: false, reason: catalog.reason};
    // One immutable evaluation per distinct publication boundary. Keep prior
    // records to resolve overlapping uploads in the producer's pointer order.
    const entries = catalog.objects.toSorted((a,b) => +a.uploaded - +b.uploaded || a.key.localeCompare(b.key));
    const boundaries = [...new Set(entries.map(o => +o.uploaded).filter(at => at >= start))];
    const artifacts = [];
    for (const at of boundaries) {
      const objects = entries.filter(o => +o.uploaded <= at);
      const metadata = JSON.stringify(objects.map(o => [o.key, +o.uploaded, o.etag]));
      const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(metadata));
      const version = Array.from(new Uint8Array(digest), b => b.toString(16).padStart(2,'0')).join('');
      artifacts.push({key: 'squeeze-pointer:'+at, version, eventAt: at, catalog: {objects, truncated:false}});
    }
    return {artifacts, complete:true, through};
  },
  async read(bucket, scope, artifact) {
    const cutoff = artifact.eventAt;
    const result = await readSqueezePublications(bucket, {...scope, cutoff}, {catalog:artifact.catalog});
    const selected = result.rows.at(-1);
    const reason = result.coverage.reason;
    const row = observation({source:'shortlist', session:scope.session, instrument:scope.instrument, origin:'recorded',
      id:artifact.key, revision:1, eventAt:cutoff, availableAt:cutoff,
      ...(selected?{valueAt:selected.eventAt}:{}),
      evidence:selected?.evidence ?? artifact.catalog.objects.at(-1).key,
      value:selected?.value ?? null,
      details:{...selected?.details, publishedAt:selected?.availableAt ?? null, coverageReason:reason}});
    return {rows:[row], reason, gap:!selected};
  }
};
