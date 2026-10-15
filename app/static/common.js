/* Shared helpers for the landing page and dashboard. No build step, no framework. */
(function () {
  "use strict";

  const TOKEN_KEY = "ci-dashboard-token";

  function getToken() {
    try { return localStorage.getItem(TOKEN_KEY) || ""; } catch (_) { return ""; }
  }
  function setToken(value) {
    try { value ? localStorage.setItem(TOKEN_KEY, value) : localStorage.removeItem(TOKEN_KEY); } catch (_) {}
  }

  async function api(path, options = {}) {
    const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
    const token = getToken();
    if (token) headers["X-Dashboard-Token"] = token;
    const res = await fetch(path, { ...options, headers });
    let body = null;
    try { body = await res.json(); } catch (_) {}
    if (!res.ok) {
      const detail = body && body.detail;
      const message = typeof detail === "string" ? detail
        : Array.isArray(detail) ? detail.map((d) => d.msg).join(", ")
        : "Request failed (" + res.status + ")";
      const err = new Error(message);
      err.status = res.status;
      throw err;
    }
    return body;
  }

  const esc = (value) => String(value == null ? "" : value)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");

  function timeAgo(epochSeconds) {
    const s = Math.max(0, Date.now() / 1000 - epochSeconds);
    if (s < 60) return "just now";
    if (s < 3600) return Math.floor(s / 60) + "m ago";
    if (s < 86400) return Math.floor(s / 3600) + "h ago";
    return Math.floor(s / 86400) + "d ago";
  }

  function renderMarkdown(md) {
    if (window.marked && window.DOMPurify) {
      return window.DOMPurify.sanitize(window.marked.parse(md || "", { gfm: true }));
    }
    return '<pre class="raw">' + esc(md) + "</pre>";
  }

  // ---- status vocab: colour is never alone, every pill has an icon + label
  const VERDICTS = {
    approved: { cls: "s-good", ic: "✓", label: "Approved" },
    approved_with_comments: { cls: "s-warning", ic: "●", label: "Approved w/ comments" },
    changes_requested: { cls: "s-critical", ic: "✕", label: "Changes requested" },
  };
  const SEVERITY = {
    critical: { cls: "s-critical", ic: "▲", label: "critical", rank: 0 },
    high: { cls: "s-serious", ic: "▲", label: "high", rank: 1 },
    warning: { cls: "s-warning", ic: "●", label: "warning", rank: 2 },
    medium: { cls: "s-warning", ic: "●", label: "medium", rank: 2 },
    low: { cls: "s-info", ic: "○", label: "low", rank: 3 },
    info: { cls: "s-info", ic: "○", label: "info", rank: 3 },
  };

  function pill(meta) {
    return '<span class="pill ' + meta.cls + '"><span class="ic" aria-hidden="true">' +
      meta.ic + "</span>" + esc(meta.label) + "</span>";
  }
  function verdictPill(verdict, status) {
    if (status === "running" || status === "queued") return pill({ cls: "s-neutral", ic: "◌", label: status });
    if (status === "failed") return pill({ cls: "s-critical", ic: "!", label: "failed" });
    return verdict && VERDICTS[verdict] ? pill(VERDICTS[verdict]) : pill({ cls: "s-neutral", ic: "–", label: "no verdict" });
  }

  // ---- pipeline
  const NODES = [
    ["fetch_context", "Fetch context", "diff · files · intent", "📥"],
    ["code_review", "Code review agent", "quality · bugs · style", "📝"],
    ["security_scan", "Security agent", "LLM + regex secrets", "🛡️"],
    ["generate_summary", "Summary", "deterministic verdict", "🧩"],
    ["post_review", "Post review", "one idempotent comment", "💬"],
  ];

  function pipelineHTML() {
    const node = ([id, name, sub, ic]) =>
      '<div class="node" data-node="' + id + '"><div class="n-top"><span class="n-ic" aria-hidden="true">' + ic +
      '</span><div><div class="n-name">' + name + '</div><div class="n-sub">' + sub +
      '</div></div><span class="n-state">waiting</span></div></div>';
    return '<div class="pipeline">' +
      '<div class="pipe-col">' + node(NODES[0]) + "</div>" +
      '<div class="pipe-col split">' + node(NODES[1]) + node(NODES[2]) + "</div>" +
      '<div class="pipe-col join">' + node(NODES[3]) + "</div>" +
      '<div class="pipe-col">' + node(NODES[4]) + "</div></div>";
  }

  function nodeStates(review) {
    const steps = review.steps || {};
    const done = (id) => id in steps;
    const live = review.status === "running" || review.status === "queued";
    const states = {};
    const parallelReady = done("fetch_context");
    for (const [id] of NODES) {
      if (done(id)) { states[id] = "done"; continue; }
      if (!live) { states[id] = "skipped"; continue; }
      let running = false;
      if (id === "fetch_context") running = review.status === "running";
      else if (id === "code_review" || id === "security_scan") running = parallelReady;
      else if (id === "generate_summary") running = done("code_review") && done("security_scan");
      else if (id === "post_review") running = done("generate_summary");
      states[id] = running ? "running" : "pending";
    }
    return states;
  }

  function updatePipeline(root, review) {
    const states = nodeStates(review);
    const steps = review.steps || {};
    root.querySelectorAll(".node").forEach((el) => {
      const id = el.dataset.node;
      const state = states[id];
      el.className = "node " + state + (state === "running" && (id === "code_review" || id === "security_scan") ? " scan" : "");
      el.querySelector(".n-state").textContent =
        state === "done" ? "✓ " + steps[id].finished.toFixed(1) + "s"
        : state === "running" ? "running"
        : state === "skipped" ? "skipped" : "waiting";
    });
  }

  // Derive start/end per node from finish times: proves the two agents overlap.
  function timeline(steps) {
    const f = (id) => (steps[id] ? steps[id].finished : null);
    const fetch = f("fetch_context");
    if (fetch == null) return null;
    const rows = [["fetch_context", 0, fetch]];
    const r = f("code_review"), s = f("security_scan");
    if (r != null) rows.push(["code_review", fetch, r]);
    if (s != null) rows.push(["security_scan", fetch, s]);
    const sum = f("generate_summary");
    if (sum != null) rows.push(["generate_summary", Math.max(r || fetch, s || fetch), sum]);
    const post = f("post_review");
    if (post != null && sum != null) rows.push(["post_review", sum, post]);
    return rows;
  }

  function ganttHTML(steps) {
    const rows = timeline(steps || {});
    if (!rows) return "";
    const total = Math.max(...rows.map((row) => row[2]), 0.1);
    const html = rows.map(([id, a, b]) => {
      const par = id === "code_review" || id === "security_scan";
      return '<div class="g-row" data-tip="' + esc(id) + ": " + a.toFixed(2) + "s → " + b.toFixed(2) + 's"><span class="g-label">' + id +
        '</span><div class="g-track"><div class="g-bar' + (par ? " par" : "") + '" style="left:' + (a / total * 100) +
        "%;width:" + ((b - a) / total * 100) + '%"></div></div><span class="g-val">' + (b - a).toFixed(1) + "s</span></div>";
    }).join("");
    const r = steps.code_review, s = steps.security_scan, fetch = steps.fetch_context;
    let note = "";
    if (r && s && fetch) {
      const seq = (r.finished - fetch.finished) + (s.finished - fetch.finished);
      const par = Math.max(r.finished, s.finished) - fetch.finished;
      if (seq > 0) note = '<div class="g-note">Running the agents in parallel saved <b>' + (seq - par).toFixed(1) +
        "s</b> of LLM time compared with running them one after the other.</div>";
    }
    return '<div class="gantt">' + html + "</div>" + note;
  }

  function scoreRing(score) {
    const v = typeof score === "number" ? score : null;
    const pct = v == null ? 0 : Math.max(0, Math.min(1, v / 10));
    const C = 2 * Math.PI * 46;
    const color = v == null ? "var(--axis)" : v >= 8 ? "var(--accent)" : v >= 5 ? "var(--warning)" : "var(--critical)";
    return '<div class="score" role="img" aria-label="Code quality score ' + (v == null ? "unknown" : v + " out of 10") + '">' +
      '<svg viewBox="0 0 104 104"><circle cx="52" cy="52" r="46" fill="none" stroke="var(--grid)" stroke-width="8"/>' +
      '<circle cx="52" cy="52" r="46" fill="none" stroke="' + color + '" stroke-width="8" stroke-linecap="round" ' +
      'stroke-dasharray="' + C + '" stroke-dashoffset="' + C * (1 - pct) + '" style="transition:stroke-dashoffset 1s ease"/></svg>' +
      '<div class="val"><div><b>' + (v == null ? "–" : v.toFixed(1)) + "</b><span>/ 10 quality</span></div></div></div>";
  }

  function findingsHTML(result) {
    const items = [];
    (result.security_issues || []).forEach((i) => items.push({
      kind: "security", sev: i.severity, file: i.file, line: i.line, msg: i.issue, fix: i.recommendation,
    }));
    (result.review_comments || []).forEach((c) => items.push({
      kind: "review", sev: c.severity, file: c.file, line: c.line, msg: c.message, fix: c.suggestion,
    }));
    if (!items.length) {
      return '<div class="empty"><span class="big" aria-hidden="true">✨</span>No issues found. Clean PR, nothing invented.</div>';
    }
    const rank = (sev) => (SEVERITY[sev] || SEVERITY.info).rank;
    items.sort((a, b) => rank(a.sev) - rank(b.sev) || (a.kind === "security" ? -1 : 1));
    return items.map((it) => {
      const meta = SEVERITY[it.sev] || SEVERITY.info;
      const loc = it.file ? '<span class="loc">' + esc(it.file) + (it.line ? ":" + esc(it.line) : "") + "</span>" : "";
      return '<div class="finding"><div class="finding-top">' + pill(meta) + loc +
        '<span class="kind">' + (it.kind === "security" ? "🛡 security" : "📝 review") + "</span></div>" +
        "<p>" + esc(it.msg) + "</p>" +
        (it.fix ? '<div class="fix"><b>fix →</b>' + esc(it.fix) + "</div>" : "") + "</div>";
    }).join("");
  }

  /** Render a finished review into `root`. */
  function renderResult(root, review) {
    const r = review.result || {};
    const meta = r.pr_metadata || {};
    const security = r.security_issues || [];
    const crit = security.filter((i) => i.severity === "critical" || i.severity === "high").length;
    const prUrl = meta.url || "https://github.com/" + review.repo + "/pull/" + review.pr_number;
    const errors = (r.errors || []).filter(Boolean);

    root.innerHTML =
      (review.status === "failed"
        ? '<div class="notice error" style="margin-bottom:16px"><span aria-hidden="true">✕</span><div><b>Review failed.</b> ' +
          esc(errors.join(" · ") || "Unknown error") + "</div></div>"
        : errors.length
        ? '<div class="notice" style="margin-bottom:16px"><span aria-hidden="true">⚠</span><div><b>Partial review.</b> ' +
          esc(errors.join(" · ")) + "</div></div>" : "") +
      '<div class="card" style="overflow:hidden">' +
        '<div class="result-head"><div>' + verdictPill(r.overall_verdict, review.status) +
          '<h3><a href="' + esc(prUrl) + '" target="_blank" rel="noopener">' + esc(meta.title || review.repo + " #" + review.pr_number) + "</a></h3>" +
          '<div class="meta-row"><span>' + esc(review.repo) + " #" + esc(review.pr_number) + "</span>" +
            (meta.author ? "<span>@" + esc(meta.author) + "</span>" : "") +
            (r.language ? "<span>" + esc(r.language) + "</span>" : "") +
            (r.files_changed != null ? "<span>" + r.files_changed + " files</span>" : "") +
            (r.additions != null ? '<span><span class="add">+' + r.additions + '</span> <span class="del">−' + r.deletions + "</span></span>" : "") +
            (review.duration ? "<span>⏱ " + review.duration + "s</span>" : "") +
          "</div></div>" + scoreRing(r.code_quality_score) + "</div>" +
        '<div class="counters">' +
          '<div class="counter"><b>' + (r.review_comments || []).length + "</b><span>review comments</span></div>" +
          '<div class="counter"><b>' + security.length + "</b><span>security findings</span></div>" +
          '<div class="counter"><b>' + crit + "</b><span>high / critical</span></div>" +
          '<div class="counter"><b>' + (r.files_changed != null ? r.files_changed : "–") + "</b><span>files analysed</span></div>" +
        "</div></div>" +
      '<div style="display:flex;flex-wrap:wrap;gap:12px;align-items:center;justify-content:space-between;margin:26px 0 14px">' +
        '<div class="tabs" role="tablist">' +
          '<button class="tab" role="tab" aria-selected="true" data-tab="findings">Findings</button>' +
          '<button class="tab" role="tab" aria-selected="false" data-tab="comment">PR comment preview</button>' +
          '<button class="tab" role="tab" aria-selected="false" data-tab="timing">Agent timing</button>' +
        "</div>" +
        '<button class="btn btn-ghost" data-copy-link>🔗 Copy share link</button>' +
      "</div>" +
      '<div data-panel="findings" class="card">' + findingsHTML(r) + "</div>" +
      '<div data-panel="comment" class="hidden">' +
        '<div class="gh-comment"><div class="gh-comment-head"><b>codeinspector-ai</b><span class="gh-bot">bot</span>' +
        "<span>commented" + (r.posted_comment_url ? ' · <a href="' + esc(r.posted_comment_url) + '" target="_blank" rel="noopener">view on GitHub</a>' : " · preview, not posted") +
        '</span></div><div class="markdown">' + renderMarkdown(r.formatted_comment || r.final_summary || "_No comment generated._") + "</div></div></div>" +
      '<div data-panel="timing" class="card hidden" style="padding:22px">' + (ganttHTML(review.steps) || '<div class="empty">No timing recorded.</div>') + "</div>";

    root.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => {
      root.querySelectorAll(".tab").forEach((t) => t.setAttribute("aria-selected", String(t === tab)));
      root.querySelectorAll("[data-panel]").forEach((p) => p.classList.toggle("hidden", p.dataset.panel !== tab.dataset.tab));
    }));
    const copy = root.querySelector("[data-copy-link]");
    copy.addEventListener("click", async () => {
      const link = location.origin + "/?r=" + review.id + "#try";
      try { await navigator.clipboard.writeText(link); copy.textContent = "✓ Link copied"; }
      catch (_) { copy.textContent = link; }
      setTimeout(() => (copy.textContent = "🔗 Copy share link"), 2200);
    });
    bindTips(root);
  }

  // ---- hover tooltip, shared by charts and gantt rows
  let tipEl;
  function bindTips(root) {
    if (!tipEl) { tipEl = document.createElement("div"); tipEl.className = "tip"; document.body.appendChild(tipEl); }
    root.querySelectorAll("[data-tip]").forEach((el) => {
      el.addEventListener("pointermove", (e) => {
        tipEl.innerHTML = el.dataset.tipHtml || esc(el.dataset.tip);
        tipEl.style.left = e.clientX + "px"; tipEl.style.top = e.clientY + "px";
        tipEl.classList.add("show");
      });
      el.addEventListener("pointerleave", () => tipEl.classList.remove("show"));
    });
  }

  // Cursor-following glow
  window.addEventListener("pointermove", (e) => {
    document.documentElement.style.setProperty("--mx", e.clientX + "px");
    document.documentElement.style.setProperty("--my", e.clientY + "px");
  }, { passive: true });

  window.CI = { api, esc, timeAgo, renderMarkdown, pill, verdictPill, VERDICTS, SEVERITY,
    pipelineHTML, updatePipeline, renderResult, ganttHTML, bindTips, getToken, setToken };
})();
