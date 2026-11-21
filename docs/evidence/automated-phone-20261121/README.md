# Automated phone default — Brian return

[PR #292](https://github.com/OwenTanzer/live-options-view/pull/292), exact candidate `b01aa4326774e675070d81ca88b5b4813c6304c8`, based on `8fe8aecefe0eecf3051be6bc6017df215fdb9d31`.

The change is three files: browser view initialization, its content hash in index.html, and regression tests. Clean phone-width browsers (max-width 600px, the site's existing breakpoint) start Automated in Floor; desktops start in Cards. Valid explicit saved Cards/Floor/Session lanes choices win. The original implementation at `111f01e` and current master both initialize with `persist:false`, so prior saved values are explicit choices and no migration is necessary. Invalid saved values use the viewport default. Initialization remains synchronous before tab handlers are installed; no extra fetch, data change or bot operation is introduced.

Validation:

- `node tests/bot_floor.test.js`: passed, including 10 new viewport/saved-choice combinations and zero initialization writes.
- `node tests/bots_panel.test.js`: passed.
- Chromium: 20 combinations at 390, 600, 601 and 1440px with no saved choice, each of three explicit modes, and invalid storage. Each performed actual Cards/Session lanes/Floor switching and reloads: 60 switch/reload checks passed, zero page errors. Initial selection was verified before opening Automated; 390px Floor has no horizontal overflow. [Machine-readable receipt](receipt.json).
- Exact-HEAD asset build and artifact verification passed.
- [Required Web CI](https://github.com/OwenTanzer/live-options-view/actions/runs/44876463089): completed successfully for exact candidate `b01aa4326774e675070d81ca88b5b4813c6304c8`.

The screenshots use locally served candidate assets and a single read-only snapshot of the existing public `/api/bots` roster. They are local rendering evidence, not a production deployment or bot-health claim. No production writes or auth operations were performed.

## Release completed

The initial eligible-review blocker was resolved by Owen's explicit one-time native admin-merge authorization (authenticated message Sentinel_4a1526e2c5a48191abfd2a9775a28aca). Before merging, Brian rechecked the same reviewed head, successful required CI, no conflicts, no independent approval and no concurrent release. Only the independent-approval gate was overridden through GitHub's built-in admin merge; no rules, permissions, CI or deployment protections changed.

PR #292 merged as `0a3853df0692e8c9bdff762fc0267fc4f9eda447` at 2026-11-21T06:51:35Z. Its tree equals the reviewed candidate.

- [Merged-revision Web CI 44877528477](https://github.com/OwenTanzer/live-options-view/actions/runs/44877528477): success.
- [Protected staged asset release 44877760688](https://github.com/OwenTanzer/live-options-view/actions/runs/44877760688): success after normal production environment approval.
- Cloudflare asset version: `53773594-4cf8-4638-9655-c8fb8be6528b`.
- [Live receipt](live-receipt.json), 2026-11-21T06:56:03.782Z: production environment and response headers match the exact merged revision; served bot-floor module hash matches merged bytes.
- Fresh production browser contexts: 390px defaults Floor; 1440px defaults Cards. Both have no initialization storage write, no horizontal overflow and no page errors. Actual Cards, Session lanes and Floor clicks each survive reload at both widths. Live screenshots and workflow receipts accompany this report.

The earlier phone-* images are local candidate evidence; live-* images and receipt are production verification. Production checks used read-only requests and browser-local choice storage. No API/backend, collector, bot activity, trading, auth, Workers KV, new spend or One Clock changes. #275 untouched. No email sent.

Checkpoint: [existing Options Renaissance release log](https://app.notion.com/p/4133c519147b815c9562c17155dda32a). No new event. Return to Chej for closeout; no remaining release blocker. The isolated automated-phone worktree is clean at the deployed revision; own browser/server processes have ended. This evidence branch needs no merge or deployment.

—Brian 🦡