// Local D1 fixtures only. Production has no endpoint to seed an identity/grant.
import { readFileSync } from 'node:fs';
import { accountId, hashSessionToken } from '../../backend/storage.js';
import { ownerSourceBinding } from '../../backend/btc-owner-link.js';
export function setupBtcOwner(env) {
  for (const file of ['0003_trade_journal.sql', '0005_btc_owner_link.sql', '0007_btc_github_link.sql']) {
    env.DB.db.exec(readFileSync(new URL('../../backend/migrations/' + file, import.meta.url), 'utf8'));
  }
  env.BTC_OWNER_LINK = 'enabled';
  env.BTC_OWNER_RIGHTS_RECEIPT = 'synthetic-local-owner-rights';
}
export async function seedBtcOwnerSession(env, token, username) {
  await env.DB.prepare("INSERT OR IGNORE INTO trade_journal_members(member,credential_id,public_key) VALUES('Owen','fixture-credential','fixture-public-key')").run();
  await env.DB.prepare(`INSERT OR REPLACE INTO btc_owner_sessions(token_hash,username,credential_id,source_binding,expires)
    VALUES(?,?,'fixture-credential',?,?)`).bind(await hashSessionToken(token), accountId(username),
    await ownerSourceBinding(env), Math.floor(Date.now()/1000)+28800).run();
}
