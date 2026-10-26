(() => {
  const $ = (id) => document.getElementById(id);
  const nodes = new vis.DataSet();
  const edges = new vis.DataSet();
  const raw = { nodes: new Map(), edges: new Map() };
  const expanded = new Set();
  const network = new vis.Network($("graph"), { nodes, edges }, {
    physics: { solver: "forceAtlas2Based", stabilization: { iterations: 150 }, forceAtlas2Based: { springLength: 140 } },
    interaction: { hover: true, tooltipDelay: 150 },
    edges: { arrows: { to: { enabled: true, scaleFactor: 0.6 } }, font: { size: 11, align: "middle", strokeWidth: 3 } },
    nodes: { font: { size: 13 } },
  });

  const fmt = (v, d = 4) => (v == null ? "–" : Number(v).toLocaleString(undefined, { maximumFractionDigits: d }));
  const short = (a) => `${a.slice(0, 6)}…${a.slice(-4)}`;
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

  function nodeView(n) {
    const center = n.center || expanded.has(n.id);
    let label = n.registered ? `${n.label}\n${n.owner}` : n.label;
    if (n.unapproved) label += `\n⚠ ${n.unapproved} unapproved`;
    return {
      id: n.id, label,
      title: n.registered ? `${n.label} (${n.owner})\n${n.id}\nTotal ${fmt(n.total, 2)} τ` : n.id,
      shape: n.registered ? "box" : "dot",
      size: n.registered ? undefined : 10,
      color: n.registered
        ? { background: css("--node-reg"), border: n.unapproved ? css("--warn") : css("--node-reg-border") }
        : { background: css("--node-ext"), border: css("--node-ext-border") },
      borderWidth: center ? 4 : n.unapproved ? 3 : 1,
      font: { color: n.registered ? css("--node-reg-text") : css("--text"), multi: false },
    };
  }

  function edgeView(e) {
    const alpha = e.kind === "stake_transfer";
    const width = 1 + Math.min(8, Math.log10(1 + e.amount) * 2);
    let label = alpha ? `${fmt(e.alpha, 2)} α` : `${fmt(e.amount, 2)} τ`;
    if (e.count > 1) label += ` ×${e.count}`;
    if (e.unapproved) label += `  ⚠${e.unapproved}`;
    return {
      id: e.id, from: e.from, to: e.to, label, width,
      color: { color: e.unapproved ? css("--warn") : alpha ? css("--alpha") : css("--tao") },
      dashes: alpha,
      smooth: { type: alpha ? "curvedCCW" : "curvedCW", roundness: 0.15 },
      title: `${alpha ? "Alpha" : "TAO"}: ${e.count} transfer(s), ${fmt(e.amount)} τ${alpha ? ` / ${fmt(e.alpha)} α` : ""}${e.unapproved ? `\n${e.unapproved} unapproved` : ""}`,
    };
  }

  function merge(data) {
    for (const n of data.nodes) {
      const prev = raw.nodes.get(n.id);
      raw.nodes.set(n.id, prev ? { ...prev, ...n, center: prev.center || n.center } : n);
      nodes.update(nodeView(raw.nodes.get(n.id)));
    }
    for (const e of data.edges) {
      const prev = raw.edges.get(e.id);
      // Different sources/depths can return the same edge; keep the richer one.
      if (!prev || e.count >= prev.count) raw.edges.set(e.id, e);
      edges.update(edgeView(raw.edges.get(e.id)));
    }
  }

  async function load(address) {
    const kinds = [$("k-transfer").checked && "transfer", $("k-stake").checked && "stake_transfer"].filter(Boolean);
    const params = new URLSearchParams({ address: address || "", source: $("source").value, kinds: kinds.join(","), limit: $("limit").value });
    $("graph-msg") && ($("graph-msg").textContent = "Loading…");
    const res = await fetch(`/api/graph?${params}`);
    const data = await res.json();
    if (!res.ok) { alert(data.detail || "Failed to load graph"); $("graph-msg") && ($("graph-msg").textContent = ""); return; }
    $("graph-msg")?.remove();
    if (address) expanded.add(address);
    merge(data);
    if (!data.edges.length) panelMsg(`No ${kinds.join(" / ")} history found${address ? " for " + short(address) : ""} (${data.source}).`);
    if (address) network.focus(address, { scale: 1, animation: true });
  }

  function panelMsg(text) { $("panel").innerHTML = `<p class="muted">${esc(text)}</p>`; }

  function showNode(id) {
    const n = raw.nodes.get(id);
    if (!n) return;
    $("panel").innerHTML = `
      <h3>${esc(n.registered ? n.label : "External address")}</h3>
      <p class="mono small">${esc(n.id)}</p>
      ${n.registered ? `<p>Owner <b>${esc(n.owner)}</b> · total <b>${fmt(n.total, 4)} τ</b></p>
        ${n.purpose ? `<p class="small">${esc(n.purpose)}</p>` : ""}
        ${n.unapproved ? `<p><a class="badge warn" href="/transfers?wallet=${n.wallet_id}&status=unapproved">${n.unapproved} unapproved</a></p>` : ""}
        <p><a href="/wallets/${n.wallet_id}">Open wallet →</a></p>` : ""}
      <p><button id="expand">${expanded.has(id) ? "Reload" : "Expand"} connections</button>
         <a class="small" href="https://taostats.io/account/${esc(n.id)}" target="_blank" rel="noopener">taostats ↗</a></p>`;
    $("expand").onclick = () => load(id);
  }

  function showEdge(id) {
    const e = raw.edges.get(id);
    if (!e) return;
    const name = (a) => { const n = raw.nodes.get(a); return n && n.registered ? esc(n.label) : `<span class="mono">${short(a)}</span>`; };
    const alpha = e.kind === "stake_transfer";
    const rows = e.items.map((i) => `
      <tr><td class="small">${i.timestamp ? esc(i.timestamp.replace("T", " ").slice(0, 19)) : "#" + i.block}</td>
        <td class="num mono small">${alpha && i.alpha != null ? fmt(i.alpha) + " α<br>" : ""}${fmt(i.amount)} τ</td>
        <td class="small">${i.transfer_id
          ? `<a href="/transfers/${i.transfer_id}">${i.approved ? "✔" : '<span class="warn-text">unapproved</span>'}</a>`
          : i.extrinsic_id ? `<a class="muted" href="https://taostats.io/extrinsic/${esc(i.extrinsic_id)}" target="_blank" rel="noopener">${esc(i.extrinsic_id)}</a>` : ""}</td></tr>`).join("");
    $("panel").innerHTML = `
      <h3>${name(e.from)} → ${name(e.to)}</h3>
      <p><span class="tag ${e.kind}">${alpha ? "Alpha" : "TAO"}</span> ${e.count} transfer(s) · <b>${fmt(e.amount)} τ</b>${alpha ? ` · ${fmt(e.alpha)} α` : ""}
        ${e.netuids.length ? `<br><span class="small muted">subnets ${e.netuids.map((n) => "SN" + n).join(", ")}</span>` : ""}</p>
      ${e.unapproved ? `<p><span class="badge warn">${e.unapproved}</span> unapproved between these wallets</p>` : ""}
      <table class="data"><thead><tr><th>Time</th><th class="num">Amount</th><th></th></tr></thead><tbody>${rows}</tbody></table>
      ${e.count > e.items.length ? `<p class="small muted">showing latest ${e.items.length}</p>` : ""}`;
  }

  network.on("click", (p) => { if (p.nodes.length) showNode(p.nodes[0]); else if (p.edges.length) showEdge(p.edges[0]); });
  network.on("doubleClick", (p) => { if (p.nodes.length) load(p.nodes[0]); });

  $("graph-form").addEventListener("submit", (ev) => { ev.preventDefault(); load($("address").value.trim()); });
  $("clear").onclick = () => { nodes.clear(); edges.clear(); raw.nodes.clear(); raw.edges.clear(); expanded.clear(); panelMsg("Cleared."); };

  const initial = $("address").value.trim();
  if (initial || $("source").value === "db") load(initial);
})();
