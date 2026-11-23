// auth — extracted from docs/index.html (B2, readability quest,
// Nov 2026). Classic script, loaded by index.html in the same position the
// inline section occupied, so the page's script order — and the shared global
// scope — is unchanged. No ESM conversion.
// `session`, `SESSION_CACHE_KEY` and `cacheSessionDisplay()` stay declared
// in the paper-trading section (the page's shared scope); this module reads
// and assigns them. `showMarginCallScreen` is called by the paper-trading
// section's settlement path; `renderAuthGate`, `submitAuth`,
// `refreshSessionFromServer` and `hideMarginCallScreen` are wired up by boot
// — all runtime-only, after all scripts load.
// ── auth ───────────────────────────────────────────────────────────────────────
let authMode = 'login';
let authRevision = 0;
let sessionVerified = false;

async function logoutPaperAccount() {
  authRevision++;
  sessionVerified = false;
  session = null;
  closeTicket();
  try { localStorage.removeItem(SESSION_CACHE_KEY); } catch {}
  renderAuthGate();
  renderSelectedChain();
  document.getElementById('auth-submit').disabled = true;
  try { await fetch('/api/logout', { method: 'POST', credentials: 'same-origin' }); }
  catch { /* Local private state stays cleared even when logout transport fails. */ }
  finally { document.getElementById('auth-submit').disabled = false; }
}

async function openPaperLogin() {
  authMode = 'login';
  if (session) await logoutPaperAccount();
  renderAuthGate();
  document.getElementById('auth-panel').scrollIntoView({ block: 'center' });
  document.getElementById('auth-username').focus();
}

function renderAuthGate() {
  if (typeof btcOptions !== 'undefined') btcOptions.syncSession();
  const authPanel = document.getElementById('auth-panel');
  const paperPanel = document.getElementById('paper-panel');
  if (session) {
    authPanel.classList.add('hidden');
    paperPanel.classList.remove('hidden');
  } else {
    authPanel.classList.remove('hidden');
    paperPanel.classList.add('hidden');
    document.getElementById('auth-heading').textContent = authMode === 'login' ? 'Log In' : 'Register';
    document.getElementById('auth-submit').textContent = authMode === 'login' ? 'Log In' : 'Create Account';
    document.getElementById('auth-toggle').textContent =
      authMode === 'login' ? 'Need an account? Register' : 'Have an account? Log in';
    document.getElementById('auth-warning').classList.toggle('hidden', authMode !== 'register');
    document.getElementById('auth-password').autocomplete = authMode === 'login' ? 'current-password' : 'new-password';
    document.getElementById('auth-error').textContent = '';
  }
}

function showMarginCallScreen(reason) {
  session = null;
  try { localStorage.removeItem(SESSION_CACHE_KEY); } catch (e) {}
  document.getElementById('margin-call-reason').textContent = reason;
  document.getElementById('margin-call-screen').classList.remove('hidden');
  renderAuthGate();
}

function hideMarginCallScreen() {
  document.getElementById('margin-call-screen').classList.add('hidden');
  authMode = 'login';
  renderAuthGate();
}

async function submitAuth(username, password) {
  const endpoint = authMode === 'login' ? '/api/login' : '/api/register';
  const response = await fetch(endpoint, {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  });
  const result = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(result.error || `Request failed (HTTP ${response.status})`);
  return result;
}

async function refreshSessionFromServer() {
  const revision = authRevision;
  try {
    const response = await fetch('/api/me', { credentials: 'same-origin' });
    if (revision !== authRevision) return;
    if (!response.ok) { session = null; sessionVerified = false; return; }
    const result = await response.json();
    if (revision !== authRevision) return;
    session = result;
    sessionVerified = true;
    cacheSessionDisplay();
  } catch (error) {
    if (revision !== authRevision) return;
    session = null; sessionVerified = false;
  }
}
