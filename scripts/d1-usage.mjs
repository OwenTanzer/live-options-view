// Account-wide D1 row usage for the current UTC quota day.
import assert from 'node:assert/strict';
import { pathToFileURL } from 'node:url';

export const limits = { rowsRead: 5_000_000, rowsWritten: 100_000 };
export const warningFraction = 0.8;

export function checkUsage(payload, day) {
  if (payload.errors?.length) throw new Error('Cloudflare Analytics rejected the query. Check Account Analytics Read permission.');
  const accounts = payload.data?.viewer?.accounts;
  assert.equal(accounts?.length, 1, 'Expected one Cloudflare account');
  const groups = accounts[0].d1AnalyticsAdaptiveGroups;
  assert.ok(Array.isArray(groups) && groups.length <= 1, 'Expected one account-wide sum');
  if (!groups.length) {
    console.log(day + ': Cloudflare has reported no D1 activity yet.');
    return;
  }
  let over = false;
  for (const [key, limit] of Object.entries(limits)) {
    const used = groups[0].sum?.[key];
    assert.ok(Number.isSafeInteger(used) && used >= 0, 'Missing or invalid ' + key);
    console.log(day + ' ' + key + ': ' + used + ' / ' + limit + ' (' + (100 * used / limit).toFixed(1) + '%)');
    over ||= used >= limit * warningFraction;
  }
  if (over) throw new Error('D1 usage reached 80% of the daily allowance. Inspect D1 Insights by rows read and written.');
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const token = process.env.CLOUDFLARE_API_TOKEN;
  assert.ok(token, 'Missing Cloudflare Analytics credential');
  const day = new Date().toISOString().slice(0, 10);
  const query = `query Usage($account: string!, $day: Date!) {
    viewer { accounts(filter: {accountTag: $account}) {
      d1AnalyticsAdaptiveGroups(limit: 1, filter: {date: $day}) { sum { rowsRead rowsWritten } }
    } }
  }`;
  const response = await fetch('https://api.cloudflare.com/client/v4/graphql', {
    method: 'POST',
    headers: {Authorization: 'Bearer ' + token, 'Content-Type': 'application/json'},
    body: JSON.stringify({query, variables: {account: process.env.CLOUDFLARE_ACCOUNT_ID, day}}),
    signal: AbortSignal.timeout(30_000),
  });
  if (!response.ok) throw new Error('Cloudflare Analytics HTTP ' + response.status);
  checkUsage(await response.json(), day);
}
