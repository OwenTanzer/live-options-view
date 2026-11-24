# Minute prices

Linux owns the two new objects, `crypto/prices.json` and
`nasdaq/prices.json`. Crypto quotes come from the existing MSI host over
host-key-pinned SSH. Both feeds use the established read-only hourly baseline.
The minute objects carry membership-matched prices, provider observation
times, receipt times, source, status and a baseline pointer. They omit candles,
news, market caps, predictions and other hourly fields. The tab reads the small
object when visible. When the minute capture expires after two minutes, the
tab falls back to the hourly snapshot. Percent changes and heatmap colours
continue to use hourly enrichment.

On the Linux publishing host, from a clean reviewed checkout, stage the full
commit SHA using the calendar interpreter already installed there. Staging
requires inactive, disabled minute timers. It verifies a digest on MSI, installs
two inactive user-unit templates, writes the NASDAQ calendar through December
of the second following year, and points the staged Linux directory at the
exact revision. Regenerate that calendar during maintenance or after an
announced unscheduled exchange closure. Missing coverage fails closed.

```bash
python3 ops/panel-minute/stage.py FULL_REVIEWED_SHA \
  --calendar-python ~/.local/share/ibit-skew-execution/venv/bin/python
export XDG_RUNTIME_DIR=/run/user/$(id -u)
export DBUS_SESSION_BUS_ADDRESS=unix:path=$XDG_RUNTIME_DIR/bus
systemctl --user show optionsview-minute@{crypto,nasdaq}.timer -p ActiveState -p UnitFileState
```

After approval, merge through the protected PR, verify Web CI on the exact
release commit, and use the repository's protected API and assets deployment
workflows in that order. Verify the two exact browser keys through the deployed
proxy, and retain the two active Worker version IDs before deployment for
rollback. An unpublished key can return 404. Stage the reviewed release commit
on the two capture hosts before enabling the timers. The only activation is:

```bash
systemctl --user enable --now optionsview-minute@crypto.timer optionsview-minute@nasdaq.timer
systemctl --user list-timers 'optionsview-minute@*'
journalctl --user -u optionsview-minute@crypto.service -u optionsview-minute@nasdaq.service --since today
```

Crypto is admitted around the clock. Stocks reads the exchange schedule before
contacting providers and again before publication; its half-open session ends
at the actual regular close. It tries six watchlist batches for 101 listings
and uses primary quotes to resolve stale, undated or missing session marks.
Four workers share the 32-second request admission window. A minute may have
gaps when provider response time exceeds that budget. A 429 or 503 persists
the provider's cooldown on its capture host. There is one locked writer per
feed, a 55-second outer service deadline, a bounded object read, and at most
one conditional PUT per run. A conflicting write fails and the next timer
evaluates the new state. Crypto's Windows capture also takes a process lock.
A whole-feed outage preserves the existing object, which expires in the UI.
Service logs expose coverage and HTTP call counts without credentials.

To stop minute publication, disable both timers and stop any running minute
services. Existing minute marks expire in the UI without deleting data.
If needed, restore the prior approved API and assets versions through the
established protected release lanes. The hourly service names are separate.

```bash
systemctl --user disable --now optionsview-minute@crypto.timer optionsview-minute@nasdaq.timer
systemctl --user stop optionsview-minute@crypto.service optionsview-minute@nasdaq.service
```

Crypto spot replay uses both confirmed Linux publications. After staging the
reviewed revision, install the hourly unit override on Linux before its next
scheduled run. Both publishers need the same private database and effective
user. A daemon reload picks up the command for the next invocation.

    mkdir -p ~/.config/systemd/user/optionsview-crypto-hourly.service.d
    install -m 0644 "$HOME/.local/share/optionsview-panel-minute/app/ops/panel-minute/crypto-hourly-receipts.conf" ~/.config/systemd/user/optionsview-crypto-hourly.service.d/crypto-hourly-receipts.conf
    systemctl --user daemon-reload
    systemctl --user cat optionsview-crypto-hourly.service

The hourly wrapper runs the installed capture command and retains the exact
hourly price payload. The minute publisher retains its final overlay. Both
journal their intent locally before the conditional R2 write, then confirm it
after acknowledgement. A journal failure before upload fails the job. An
ambiguous write or failed local confirmation leaves a visible replay gap and
fails the job. The next read of matching remote bytes confirms the pending
attempt at readback time. The gap up to that confirmation stays unmeasurable.

From the staged Linux checkout (Python and Node required):

    python -m market_data.crypto_collector.spot_receipts "$HOME/.local/share/optionsview-panel-minute/private/crypto-spot.sqlite3" --as-of 2026-11-24T07:00:00+00:00

Replay returns rows using the production panel selector, including the hourly
fallback at two minutes and during baseline rollover. Before both streams have
verified evidence, or during an uncertain write, it returns unmeasurable.
Legacy minute-only receipts do not establish an hourly baseline. Replay models
the prices available on refresh at the cutoff. It does not track individual
browser refreshes. To roll back the hourly change, remove only the
crypto-hourly-receipts.conf override and reload the user manager.
