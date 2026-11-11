# Steve → G4a preview-production-access negative tests

Attach to Musef's branch `musef/225-assets-preview` (base `90cdc56`) as
`tests/preview_storage_boundary.test.mjs`. One file, no existing file edited, no
production code touched. It is a patch, not an applied change: `frontend/**`,
`scripts/**` and `tests/preview_isolation.test.mjs` are Musef's files, and shared
configuration is Nova's.

## What it covers that `tests/preview_isolation.test.mjs` does not

Musef's suite proves the gateway refuses misconfigured environments, a production
origin, injected raw stores, a generic proxy, and a public read outside the
allowlist. It exercises one mutation path. This file covers the storage face the
G4a criterion names — *preview production access* — end to end:

1. Every reserved API path is driven with a body and must reach the local API
   binding, stay below a server error, and leave no object outside `system/` and
   `paper-trades/`.
2. Caller-supplied key material (`execution_request_id`, `sym`, `username`,
   `alias`) is driven at the key constructors; none may create a capture object,
   and the store refuses capture keys and traversal directly.
3. The refusal's *location*: a todo test that activates by itself when G4a's
   `backend/operational-storage.js` lands on the base, so preview cannot keep a
   private copy of the production rule once the rule exists.
4. With every outbound call denied, an execution fails instead of finding another
   route; with the fixture network installed, every URL the backend touches is
   the fixture quote origin and nothing else.
5. `PUBLIC_DATA` exposes no write method, every approved document reads 200 and
   refuses a POST, capture-shaped keys are not preview-readable, and `/r2-proxy/`
   is denied.
6. Binding mismatches (`ISOLATION`, `API_ENVIRONMENT`, `PUBLIC_DATA_ENVIRONMENT`,
   three bad `PREVIEW_ORIGIN` forms) and injected raw stores or secrets are
   refused with 403 before any route runs, and the environment carries no
   production identity.

## Executed evidence

Against the current branch (G4a not merged): 6 pass, 0 fail, 1 todo —
`the write refusal comes from the production operational boundary` is the todo.

Against the same harness with G4a's `backend/operational-storage.js` copied in:
7 pass, 0 fail, 0 todo. The todo activates and passes, so it is not vacuous; it
is waiting for the module rather than for a fix.

Neither run is provider evidence. This is application and emulation isolation
only: it says nothing about IAM, about whether a hosted preview's real resource
IDs are disposable (Musef's P4), or about an injected binding's identity. Nova
verifies those before hosted preview acceptance.

## Interface gaps returned, newest first

**1 · The preview store re-implements the boundary rather than importing it.**
`scripts/preview-local.mjs:60-62` repeats the prefix rule with a regex and
`.`/`..`/backslash checks. Until G4a's module is on the base, preview isolation
against capture keys lives in the harness, not in product code, and any later
tightening of the production rule leaves the preview behind. After G4a merges,
importing `assertOperationalKey` there costs one line, and test 3 above turns
green without edits. If Musef's lane should not depend on mine, the alternative
is a shared module both import; that is a file-ownership decision, not mine.

**2 · The public read allowlist is duplicated, with one behaviour difference.**
`frontend/preview-policy.mjs:7-25` repeats `backend/browser-data.js:14-32`
(`BROWSER_DATA_KEYS` and `isPublicArchiveKey`). Beyond drift, a bad query string
returns 400 in production and 404 in preview, because `publicReadAllowed`
collapses the cases. A browser that distinguishes them behaves differently in
preview. Either derive the set from one place, or record the divergence in the
compatibility table.

**3 · Preview policy is selected by `ENVIRONMENT` alone.** With
`ENVIRONMENT=production` the gateway runs no preview validation at all: no origin
check, no binding identity, no CSP, no banner, and `/r2-proxy/` is delegated
instead of denied. That is right for real production, but it means a preview
deployment carrying the wrong label silently loses every guard instead of failing
closed. Nothing escapes in the local harness — the backend's own origin check
rejects a localhost request — so this is hardening, not a live escape. Requiring
a second independent signal (`ISOLATION` absent in production, for instance)
would make the mislabelling loud.

**4 · The guarantee rests on the service-binding boundary, which only provider
configuration can enforce.** The gateway refuses `DB` and `PAPER_TRADES` when
they are present in its own env, and the local API env legitimately holds both.
So "preview cannot write production storage" means "the hosted gateway has no
production write binding and the API binding points at disposable resources" —
which is Musef's P4 and Nova's to verify. The tests here prove the code refuses
the obvious mistake; they cannot prove the resource IDs.

**5 · The unused recovery write binding is already covered.** My R2a return
flagged `wrangler.recovery.toml` binding `PAPER_TRADES` to the capture bucket
with no code referencing it. Musef's
`patches/musef-225/recovery-unneeded-write-binding.patch` removes exactly that
binding; nothing further from me. It remains Nova's to apply.

## Running it

From the branch root, with Node 24:

```
node --test tests/preview_storage_boundary.test.mjs
```

No credentials, no network, no provider access; the fixture network denies
everything except the fixture quote origin.
