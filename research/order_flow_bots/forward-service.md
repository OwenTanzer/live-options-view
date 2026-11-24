# Running the QQQ forward paper strategies

Run one process on an always-on host with Node 22.13 or later, a persistent local disk and outbound HTTPS. The service shares the existing flow and chain artifacts and the public QQQ quote reader across all strategies. It requires no provider subscription, broker credentials or Cloudflare bindings. Every strategy has its own virtual one-share account in the same SQLite ledger. The service obtains holidays and early closes from the repository's One Clock NYSE calendar.

From the checked-out release, smoke-test startup with a disposable database:

```sh
testdir=$(mktemp -d "${TMPDIR:-$HOME}/qqq-forward-smoke.XXXXXXXX")
node research/order_flow_bots/forward-service.cjs --once "$testdir/paper.sqlite"
```

An off-hours smoke test prints `"status":"idle"` and performs zero network reads. An in-session cycle prints the accepted frame and decision counts, quote acceptance and any failed source. Starting mid-session leaves coverage-dependent rules abstaining until their input requirements are met. Each regular session has a two-minute pre-open warmup and a one-minute grace period to receive the closing inputs. The service requests a flow snapshot at most once per three seconds, a chain snapshot every thirty seconds and up to six shared quotes per minute beginning twenty seconds before the open. The late-minute sample supports the five-second spot freshness gate.

Beginning at the open, a separate bounded reader checks the session's immutable opening OI packet. Retrieval runs alongside ordinary polling. The feed receives the verified quantities and actual receipt time as they become available. A delayed packet leaves the associated rules subject to their coverage gates.

New positions require two minutes between their target exit and the scheduled close, including on half-days. This leaves time to receive both sides of an executable quote. If a genuine outage prevents an existing position from closing, the ledger records the later recovery fill at its observed price.

For decisions using a QQQ midpoint, both sides must have reached the collector by the decision cutoff. The later side sets the midpoint's availability and the older observation determines its freshness. A last trade also becomes usable only after the collector receives it.

Each decision minute has a fifteen-second receipt deadline. If a usable projection never arrives, the service writes eleven abstentions with the decision minute, deadline and actual detection time. A persistent session cursor covers missed polls and process restarts through the closing minute. A first launch partway through the day starts with the earliest deadline it can still meet. The service reports stale or wrong-session flow as degraded and keeps sampling current quotes for outstanding positions.

On a Linux host with systemd user services, pin the checkout to the reviewed commit, then install the unit as the persistent owner of a new ledger. Keep the release checkout at its installed path.

```sh
install -d -m 700 "$HOME/.local/state/qqq-forward-paper" "$HOME/.config/systemd/user"
cat > "$HOME/.config/systemd/user/qqq-forward-paper.service" <<EOF
[Unit]
Description=QQQ order-flow forward paper runner
[Service]
Type=simple
ExecStart="$(command -v node)" "$(pwd)/research/order_flow_bots/forward-service.cjs" "%h/.local/state/qqq-forward-paper/paper.sqlite"
Restart=on-failure
RestartSec=10
UMask=0077
[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now qqq-forward-paper.service
systemctl --user is-active qqq-forward-paper.service
journalctl --user -u qqq-forward-paper.service -n 5 --no-pager
```

Use a host whose user manager survives logout and starts at boot. Confirm with `loginctl show-user "$USER" -p Linger`. During upgrades stop the single owner, select the reviewed revision and restart against the same database. The SQLite journal preserves decisions, open positions and fill receipts across restarts. The new process reacquires market-data continuity and suppresses stale entries. Stop future collection with `systemctl --user disable --now qqq-forward-paper.service`; retain the ledger for inspection and recovery.

Inspect the eleven account balances, positions and latest decisions with `node research/order_flow_bots/forward-accounts.cjs "$HOME/.local/state/qqq-forward-paper/paper.sqlite"`.
