// backend/recovery.js -- OFFLINE recovery entry for issue #208.
//
// This is not the production Worker. It exists so the KV -> D1 migration and the
// reverse path remain executable against the preserved legacy namespaces after
// the normal deployment has dropped its KV bindings and its migration route.
// Deploy it only for a deliberate recovery window, with the recovery config
// (wrangler.recovery.toml), and delete it afterwards. It serves only the
// authenticated POST /api/admin/migrate route, never ordinary user traffic.
//
// Keeping this in a separate module is what lets the normal Worker graph stay
// free of KV bindings and of the migration code: backend/index.js does not
// import this file or backend/migrate.js.

import { handleMigration } from './migrate.js';

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method === 'POST' && url.pathname === '/api/admin/migrate') {
      return handleMigration(request, env);
    }
    return new Response('Not found', { status: 404 });
  },
};
