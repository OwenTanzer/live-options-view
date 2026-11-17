import { createHash } from 'node:crypto';
export async function seedJournalSession(db, token) {
  const digest = createHash('sha256').update(token).digest('hex');
  await db.prepare("INSERT INTO trade_journal_members(member,credential_id) VALUES('Owen','test-key')").run();
  await db.prepare("INSERT INTO trade_journal_sessions VALUES(?,'test-key','test-challenge',unixepoch()+300)").bind(digest).run();
}
