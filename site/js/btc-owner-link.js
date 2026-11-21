// Deliberate assertion with an existing passkey; never enroll or read cookies.
const btcOwnerLink = {
  busy: false, message: '', autoRevision: null,
  async post(body) {
    const response = await fetch('/api/btc-options/owner-link', { method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    const result = await response.json();
    if (!response.ok) throw Error(result.error || 'Owner verification unavailable');
    return result;
  },
  async github(automatic = false) {
    if (this.busy || !sessionVerified || !session) return false;
    const revision = authRevision, current = session;
    if (automatic && this.autoRevision === revision) return false;
    this.autoRevision = revision;
    this.busy = true;
    try {
      await this.post({ action: 'github', confirm: true, automatic, paper_account: current.username });
      if (authRevision !== revision || session !== current) return false;
      this.message = '';
      await refreshSessionFromServer();
      renderAuthGate(); renderPaper();
      return true;
    } catch (error) {
      if (!automatic && authRevision === revision && session === current) this.message = error.message;
      return false;
    } finally { this.busy = false; }
  },
  async link() {
    if (this.busy || !sessionVerified || !session) return;
    const revision = authRevision, current = session;
    const unchanged = () => authRevision === revision && session === current;
    this.busy = true; this.message = 'Waiting for your enrolled owner passkey…'; btcOptions.render();
    try {
      const options = await this.post({ action: 'options', confirm: true });
      if (!unchanged()) return;
      const decode = s => Uint8Array.from(atob(s.replace(/-/g, '+').replace(/_/g, '/')), c => c.charCodeAt(0));
      const encode = b => btoa(String.fromCharCode(...new Uint8Array(b))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
      options.challenge = decode(options.challenge);
      const credential = await navigator.credentials.get({ publicKey: options });
      if (!unchanged()) return;
      const r = credential.response;
      await this.post({ action: 'verify', confirm: true, response: { id: credential.id,
        rawId: encode(credential.rawId), type: credential.type, clientExtensionResults: credential.getClientExtensionResults(),
        response: { clientDataJSON: encode(r.clientDataJSON), authenticatorData: encode(r.authenticatorData),
          signature: encode(r.signature), ...(r.userHandle ? { userHandle: encode(r.userHandle) } : {}) } } });
      if (!unchanged()) return;
      this.message = '';
      await refreshSessionFromServer();
      renderAuthGate(); renderPaper(); await btcOptions.refresh();
    } catch (error) {
      if (unchanged()) this.message = error.name === 'NotAllowedError' ? 'Passkey verification cancelled. No access was linked.' : error.message;
    } finally { this.busy = false; btcOptions.render(); }
  },
  async unlink() {
    if (this.busy) return;
    const revision = authRevision;
    this.busy = true;
    try {
      await this.post({ action: 'unlink' });
      if (revision !== authRevision) return;
      btcOptions.quotes.clear(); btcOptions.data = null; closeTicket();
      session.trades = session.trades.filter(t => t.instrument_type !== 'futures_option');
      renderPaper(); await btcOptions.refresh();
    } catch (error) { if (revision === authRevision) this.message = error.message; }
    finally { this.busy = false; btcOptions.render(); }
  },
};
// A restored page must not paint an old private account/identity snapshot while
// the current paper and GitHub sessions are being checked again.
window.addEventListener('pageshow', event => {
  if (!event.persisted) return;
  authRevision++; sessionVerified = false;
  if (session) session.trades = (session.trades || []).filter(t => t.instrument_type !== 'futures_option');
  btcOptions.syncSession(); closeTicket(); renderPaper();
  // index.html's existing BFCache handler reloads and verifies both sessions.
});
