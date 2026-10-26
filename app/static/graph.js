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
  const copyBtn = (a) => `<button type="button" class="copy-btn" data-copy="${esc(a)}" title="Copy ${esc(a)}" aria-label="Copy address">⧉</button>`;
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

  function nodeView(n) {
    const center = n.center || expanded.has(n.id);
    let label = n.registered ? `${n.label}\n${n.owner}` : n.labeled ? `🏷 ${n.label}` : n.label;
    if (n.unapproved) label += `\n⚠ ${n.unapproved} unapproved`;
    return {
      id: n.id, label,
      title: n.registered ? `${n.label} (${n.owner})\n${n.id}\nTotal ${fmt(n.total, 2)} τ`
        : n.labeled ? `${n.label}${n.note ? "\n" + n.note : ""}\n${n.id}` : n.id,
      shape: n.registered || n.labeled ? "box" : "dot",
      size: n.registered || n.labeled ? undefined : 10,
      color: n.registered
        ? { background: css("--node-reg"), border: n.unapproved ? css("--warn") : css("--node-reg-border") }
        : n.labeled
          ? { background: css("--node-label"), border: css("--node-label-border") }
          : { background: css("--node-ext"), border: css("--node-ext-border") },
      borderWidth: center ? 4 : n.unapproved ? 3 : 1,
      font: { color: n.registered || n.labeled ? "#ffffff" : css("--text"), multi: false },
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

  let busy = false;
  function setBusy(on, address) {
    busy = on;
    $("graph-busy-text").textContent = address ? `Loading connections of ${short(address)}…` : "Loading…";
    $("graph-busy").hidden = !on;
    $("graph").setAttribute("aria-busy", on ? "true" : "false");
    for (const b of document.querySelectorAll("#graph-form button, #panel button")) b.disabled = on;
  }

  async function load(address) {
    if (busy) return;  // one request at a time; the spinner shows one is running
    const kinds = [$("k-transfer").checked && "transfer", $("k-stake").checked && "stake_transfer"].filter(Boolean);
    if (!address) { toast("Pick a wallet or enter an address.", "error"); return; }
    const params = new URLSearchParams({ address, kinds: kinds.join(","), limit: $("limit").value });
    setBusy(true, address);
    let res, data;
    try {
      res = await fetch(`/api/graph?${params}`);
      data = await res.json();
    } catch (err) {
      toast("Could not reach the server. Please try again.", "error");
      return;
    } finally {
      setBusy(false);
    }
    if (!res.ok) { toast(data.detail || "Failed to load graph", "error"); return; }
    $("graph-msg")?.remove();
    expanded.add(address);
    merge(data);
    if (!data.edges.length) {
      const what = kinds.map((k) => (k === "transfer" ? "TAO" : "alpha")).join(" or ");
      panelMsg(`No ${what} transfers found for ${short(address)} in the indexed range.`);
    }
    if (data.truncated) toast(`Showing the latest ${data.rows} transfers — raise “Max transfers” to see more.`, "info");
    network.focus(address, { scale: 1, animation: true });
  }

  function panelMsg(text) { $("panel").innerHTML = `<p class="muted">${esc(text)}</p>`; }

  function showNode(id) {
    const n = raw.nodes.get(id);
    if (!n) return;
    // Anyone can label an unlabeled address; an existing label only by its author or an admin.
    const me = Number($("graph").dataset.userId);
    const canLabel = !n.tag || !!$("graph").dataset.admin || n.tag_author_id === me;
    $("panel").innerHTML = `
      <h3>${n.registered ? esc(n.label) : n.labeled ? `<span class="lname">${esc(n.label)}</span>` : "Unlabeled address"}</h3>
      <p class="mono small">${esc(n.id)}${copyBtn(n.id)}</p>
      ${n.tag && n.registered ? `<p class="small">Label: <span class="lname">${esc(n.tag)}</span></p>` : ""}
      ${n.note ? `<p class="small">${esc(n.note)}</p>` : ""}
      ${canLabel && !n.registered ? `<p><button class="small" id="label-btn" data-dialog="label-dialog"
          data-address="${esc(n.id)}" data-label="${esc(n.tag || "")}" data-note="${esc(n.note || "")}">🏷 ${n.labeled ? "Edit label" : "Label this address"}</button></p>` : ""}
      ${n.registered ? `<p>Owner <b>${esc(n.owner)}</b> · total <b>${fmt(n.total, 4)} τ</b></p>
        ${n.purpose ? `<p class="small">${esc(n.purpose)}</p>` : ""}
        ${n.unapproved ? `<p><a class="badge warn" href="/transfers?wallet=${n.wallet_id}&status=unapproved">${n.unapproved} unapproved</a></p>` : ""}
        <p><a href="/wallets/${n.wallet_id}">Open wallet →</a></p>` : ""}
      <p class="actions">
        ${expanded.has(id)
          ? `<button id="collapse">⊖ Collapse connections</button><button id="expand" class="ghost">↻ Reload</button>`
          : `<button id="expand">⊕ Expand connections</button>`}
        <a class="small" href="https://taostats.io/account/${esc(n.id)}" target="_blank" rel="noopener">taostats ↗</a></p>`;
    $("expand").onclick = () => load(id).then(() => showNode(id));
    if ($("collapse")) $("collapse").onclick = () => { collapse(id); showNode(id); };
  }

  // Undo an expansion: drop the edges only this node's expansion brought in (edges to other
  // expanded nodes stay, they were loaded by those too), then any node left unconnected.
  function collapse(id) {
    expanded.delete(id);
    const n = raw.nodes.get(id);
    if (n) { n.center = false; nodes.update(nodeView(n)); }
    for (const e of [...raw.edges.values()]) {
      const other = e.from === id ? e.to : e.to === id ? e.from : null;
      if (other !== null && !expanded.has(other)) { raw.edges.delete(e.id); edges.remove(e.id); }
    }
    const linked = new Set();
    for (const e of raw.edges.values()) { linked.add(e.from); linked.add(e.to); }
    for (const nid of [...raw.nodes.keys()]) {
      if (nid !== id && !expanded.has(nid) && !linked.has(nid)) { raw.nodes.delete(nid); nodes.remove(nid); }
    }
  }

  function showEdge(id) {
    const e = raw.edges.get(id);
    if (!e) return;
    const name = (a) => {
      const n = raw.nodes.get(a);
      if (n && n.registered) return esc(n.label);
      if (n && n.labeled) return `<span class="lname">${esc(n.label)}</span>`;
      return `<span class="mono">${short(a)}</span>`;
    };
    const named = (a) => `${name(a)}${copyBtn(a)}`;
    const alpha = e.kind === "stake_transfer";
    const related = [e.from, e.to].some((a) => raw.nodes.get(a)?.registered);
    const rows = e.items.map((i) => `
      <tr><td class="small">${i.timestamp ? esc(i.timestamp.replace("T", " ").slice(0, 19)) : "#" + i.block}</td>
        <td class="num mono small">${alpha && i.alpha != null ? fmt(i.alpha) + " α<br>" : ""}${fmt(i.amount)} τ</td>
        <td class="small"><a href="/transfers/${i.transfer_id}">${!related ? "details" : i.approved ? "✔ approved" : '<span class="warn-text">unapproved</span>'}</a></td></tr>`).join("");
    $("panel").innerHTML = `
      <h3>${named(e.from)} → ${named(e.to)}</h3>
      <p><span class="tag ${e.kind}">${alpha ? "Alpha" : "TAO"}</span> ${e.count} transfer(s) · <b>${fmt(e.amount)} τ</b>${alpha ? ` · ${fmt(e.alpha)} α` : ""}
        ${e.netuids.length ? `<br><span class="small muted">subnets ${e.netuids.map((n) => "SN" + n).join(", ")}</span>` : ""}</p>
      ${e.unapproved ? `<p><span class="badge warn">${e.unapproved}</span> unapproved between these wallets</p>` : ""}
      <table class="data"><thead><tr><th>Time</th><th class="num">Amount</th><th></th></tr></thead><tbody>${rows}</tbody></table>
      ${e.count > e.items.length ? `<p class="small muted">showing latest ${e.items.length}</p>` : ""}`;
  }

  network.on("click", (p) => { if (p.nodes.length) showNode(p.nodes[0]); else if (p.edges.length) showEdge(p.edges[0]); });
  // Double-click toggles: expand a node, or collapse one that is already expanded.
  network.on("doubleClick", (p) => {
    if (!p.nodes.length) return;
    const id = p.nodes[0];
    if (expanded.has(id)) { collapse(id); showNode(id); } else load(id).then(() => showNode(id));
  });

  $("graph-form").addEventListener("submit", (ev) => { ev.preventDefault(); load($("address").value.trim()); });
  $("clear").onclick = () => { nodes.clear(); edges.clear(); raw.nodes.clear(); raw.edges.clear(); expanded.clear(); panelMsg("Cleared."); };

  if ($("address").value.trim()) load($("address").value.trim());
})();
