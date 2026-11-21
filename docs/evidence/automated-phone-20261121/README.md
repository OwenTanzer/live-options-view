# Automated phone default — Brian return

[PR #292](https://github.com/OwenTanzer/live-options-view/pull/292), exact candidate `b01aa4326774e675070d81ca88b5b4813c6304c8`, based on `8fe8aecefe0eecf3051be6bc6017df215fdb9d31`.

The change is three files: browser view initialization, its content hash in index.html, and regression tests. Clean phone-width browsers (max-width 600px, the site's existing breakpoint) start Automated in Floor; desktops start in Cards. Valid explicit saved Cards/Floor/Session lanes choices win. The original implementation at `111f01e` and current master both initialize with `persist:false`, so prior saved values are explicit choices and no migration is necessary. Invalid saved values use the viewport default. Initialization remains synchronous before tab handlers are installed; no extra fetch, data change or bot operation is introduced.

Validation:

- `node tests/bot_floor.test.js`: passed, including 10 new viewport/saved-choice combinations and zero initialization writes.
- `node tests/bots_panel.test.js`: passed.
- Chromium: 20 combinations at 390, 600, 601 and 1440px with no saved choice, each of three explicit modes, and invalid storage. Each performed actual Cards/Session lanes/Floor switching and reloads: 60 switch/reload checks passed, zero page errors. Initial selection was verified before opening Automated; 390px Floor has no horizontal overflow. [Machine-readable receipt](receipt.json).
- Exact-HEAD asset build and artifact verification passed.
- [Required Web CI](https://github.com/OwenTanzer/live-options-view/actions/runs/44876463089): consult the recorded final result in the return/checkpoint.

The screenshots use locally served candidate assets and a single read-only snapshot of the existing public `/api/bots` roster. They are local rendering evidence, not a production deployment or bot-health claim. No production writes or auth operations were performed.

Release is blocked by required eligible review. GitHub reports `REVIEW_REQUIRED` / `BLOCKED`; the PR author and authenticated account are both OwenTanzer, which cannot approve its own PR. No merge attempt or bypass was used. No release dispatched; live assets remain `8fe8aecefe0eecf3051be6bc6017df215fdb9d31` unless another authorized release changes them.

Checkpoint: [existing Options Renaissance release log](https://app.notion.com/p/4133c519147b815c9562c17155dda32a), saved and read back. No new Notion event created.

Chej: obtain an eligible independent approval for the exact PR head, then return this same session to normal merge, exact merged-revision CI, protected asset-only release and live desktop/phone/saved-choice verification. No admin bypass, backend/API/collector/bot/trading/auth changes, new spend or Workers KV operations. Leave #275 and One Clock untouched. No email needed.

Use $brian and $handoff to resume https://github.com/OwenTanzer/live-options-view/pull/292 at the eligible-review gate. Recheck current head, approval, master and in-flight releases before merging; return deployment and exact live verification receipts to Chej. The isolated worktree is `C:\Users\Owen\Documents\Codex\2026-11-20\task-4\automated-phone`; it is clean. No browser/server work remains active. This evidence branch needs no merge or deployment.

—Brian 🦡
