// tab switching — extracted from docs/index.html (B2, readability quest,
// Nov 2026). Classic script, loaded by index.html in the same position the
// inline section occupied, so the page's script order — and the shared global
// scope — is unchanged. No ESM conversion.
// `historyLoaded` is declared here and assigned by the historical panel
// (loadHistoryManifest failure path) — the page's shared scope keeps it
// visible to the later section, same mechanism as `ranges` (slice 3) and
// `lastTs`/`lastData` (slice 5). `loadHistoryManifest` and `setBotsPolling`
// are defined by the later historical / bot sections and are only called
// at runtime, after all scripts load. `loadNewsPanel` is defined by
// js/news-panel.js, loaded just before this file. `showTab` is wired up by boot.
// ── tab switching ────────────────────────────────────────────────────────────
let historyLoaded = false;
// The News panel (js/news-panel.js) mounts once, on first open, like history.
let newsLoaded = false;

// tab id -> [panel element id, tab button id]
const TABS = {
  live:    ['live-panel',    'tab-live'],
  history: ['history-panel', 'tab-history'],
  bots:    ['bots-panel',    'tab-bots'],
  squeeze: ['squeeze-panel', 'tab-squeeze'],
  news:    ['news-panel',    'tab-news'],
};

function showTab(tab) {
  if (!TABS[tab]) tab = 'live';
  for (const [name, [panelId, btnId]] of Object.entries(TABS)) {
    const active = name === tab;
    document.getElementById(panelId).classList.toggle('hidden', !active);
    document.getElementById(btnId).classList.toggle('active', active);
  }
  if (tab === 'history' && !historyLoaded) {
    historyLoaded = true;
    loadHistoryManifest();
  }
  if (tab === 'news' && !newsLoaded) {
    newsLoaded = true;
    loadNewsPanel();
  }
  // The bot roster is only polled while its tab is on screen -- no point
  // holding an extra request cycle open behind a hidden panel.
  setBotsPolling(tab === 'bots');
}
