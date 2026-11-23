// backend/fence.js -- the cutover fence, shared by the normal Worker and the
// offline recovery Worker.
//
// It fails CLOSED whenever a DB binding exists: an unreadable migration_state, a
// missing table, a malformed row or a thrown query all count as fenced, because
// an armed barrier must not evaporate when the thing that stores it is briefly
// unavailable. The only false-by-default case is no binding at all, which means
// this deployment cannot be mid-cutover.
//
// The normal Worker keeps this check after the cutover: a stale armed fence
// should still pause writes rather than be ignored, and the check costs one D1
// read per mutating request. It does not need the migration route or the KV
// bindings.

export async function cutoverFenceActive(env) {
  if (!env?.DB) return false;
  try {
    const row = await env.DB.prepare('SELECT value FROM migration_state WHERE name = ?')
      .bind('fence').first();
    if (!row) return false;
    const fence = JSON.parse(row.value);
    // Anything that is not an explicit `armed: false` is treated as armed.
    return fence?.armed !== false;
  } catch (error) {
    console.error(JSON.stringify({
      event: 'cutover_fence_unreadable', fail_closed: true, error: String(error?.message ?? error),
    }));
    return true;
  }
}
