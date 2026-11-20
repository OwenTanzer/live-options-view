"use strict";
(() => {
  const tbody = document.getElementById("rows");
  const health = document.getElementById("health");
  let lastSeq = 0;
  let first = true;

  const cell = (text, cls) => {
    const td = document.createElement("td");
    td.textContent = text == null || text === "" ? "—" : String(text);
    if (cls) td.className = cls;
    return td;
  };
  const t = (s) => (s ? String(s).replace("T", " ").replace(/\.\d+/, "").replace("+00:00", "Z") : "");

  function addRow(ev) {
    const tr = document.createElement("tr");
    if (ev.kind !== "publication") {
      tr.className = "sys";
      tr.append(cell(t(ev.first_seen)), cell(ev.source), cell(ev.kind));
      const td = cell(ev.error || (ev.slice ? `${ev.slice.fileName}: ${ev.decoded_rows} decoded / ${ev.slice.rowCount} published${ev.restored ? " (backfill · restored)" : ev.replay ? " (replay)" : ""}` : "") ||
        (ev.rows ? ev.rows + " rows, no overlap with previous poll" : ""));
      td.colSpan = 7;
      tr.append(td);
    } else {
      const s = ev.summary || {};
      const notional = [s.notionalAmountLeg1, s.notionalCurrencyLeg1].filter(Boolean).join(" ");
      tr.append(
        cell(t(ev.first_seen)),
        cell(`${ev.source.replace("dtcc-", "").replace("-credits", "").toUpperCase()} · ${ev.jurisdiction}${ev.route ? " · " + ev.route : ""}`),
        cell(ev.action + (ev.restored ? " (backfill · restored)" : ev.replay ? " (replay)" : ev.backfill ? " (backfill)" : ""),
             ev.backfill || ev.replay ? "dim" : ""),
        cell(t(ev.execution_ts)),
        cell(t(ev.dissemination_ts)),
        cell(notional, /\+$/.test(s.notionalAmountLeg1 || "") ? "capped" : ""),
        cell(s.underlierIDLeg1 || s.underlyingAssetName || s.uniqueProductIdentifierShortName || s.productName),
        cell(s.expirationDate),
        cell(s.fixedRateLeg1),
      );
      const td = document.createElement("td");
      const a = document.createElement("a");
      a.href = "/api/raw?ref=" + encodeURIComponent(ev.raw_ref);
      a.target = "_blank";
      a.rel = "noopener";
      a.textContent = `row ${ev.row_index}`;
      a.title = `${ev.id} in ${ev.raw_ref}`;
      if (ev.slice) a.textContent = `${ev.slice.fileName} row ${ev.row_index}`;
      td.append(a);
      tr.append(td);
    }
    if (!first) tr.classList.add("new");
    tbody.prepend(tr);
  }

  async function tick() {
    try {
      const r = await fetch("/api/trades?after=" + lastSeq, { credentials: "same-origin", cache: "no-store" });
      if (r.status === 401) { location.reload(); return; }
      const data = await r.json();
      data.rows.forEach(addRow);
      lastSeq = data.last_seq;
      first = false;
      const h = data.health || {};
      health.textContent = "collector " + (h.t ? t(h.t) : "unknown") + " · " +
        Object.entries(h.sources || {}).map(([k, v]) => `${k}: ${v.polls_ok} ok${v.not_modified != null ? " / " + v.not_modified + " unchanged" : ""} / ${v.polls_err} err`).join(" · ");
    } catch (e) {
      health.textContent = "panel cannot reach server";
    }
  }

  document.getElementById("export").addEventListener("submit", (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    location.href = "/api/export?" + new URLSearchParams({ from: f.get("from"), to: f.get("to") });
  });

  tick();
  setInterval(tick, 5000);
})();
