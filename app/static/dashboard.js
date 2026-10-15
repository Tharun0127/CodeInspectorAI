(function () {
  "use strict";
  const { api, esc, timeAgo, verdictPill, pipelineHTML, updatePipeline, renderResult, bindTips, getToken, setToken } = window.CI;
  const $ = (sel) => document.querySelector(sel);
  const REFRESH_MS = 10000;

  let reviews = [];
  let stats = null;
  let filter = "all";
  let query = "";
  let knownIds = null;

  // ------------------------------------------------ charts (HTML marks, one hue for magnitude)
  const tableView = {};

  function dataTable(head, rows) {
    return '<table class="data-table"><thead><tr><th>' + esc(head[0]) + '</th><th class="num">' + esc(head[1]) +
      "</th></tr></thead><tbody>" + rows.map((r) => "<tr><td>" + esc(r[0]) + '</td><td class="num">' + esc(r[1]) + "</td></tr>").join("") +
      "</tbody></table>";
  }

  function niceMax(v) {
    if (v <= 4) return 4;
    const step = Math.pow(10, Math.floor(Math.log10(v)));
    return Math.ceil(v / step) * step;
  }

  function vbars(rows, unit) {
    const max = niceMax(Math.max(...rows.map((r) => r[1]), 0));
    const grid = [0.5, 1].map((f) => '<div class="gridline" style="bottom:' + f * 100 + '%"><span>' + Math.round(max * f) + "</span></div>").join("");
    const bars = rows.map(([label, v, tipLabel]) =>
      '<div class="vbar' + (v ? "" : " zero") + '" data-tip-html="' + esc(esc(tipLabel || label) + " · <b>" + v + "</b> " + unit) +
      '" data-tip="x"><i style="height:' + (v / max * 100) + '%"></i></div>').join("");
    const every = rows.length > 7 ? 3 : 1;
    const labels = rows.map(([label], i) => "<span>" + (i % every === 0 || i === rows.length - 1 ? esc(label) : "") + "</span>").join("");
    return '<div class="vbars">' + grid + '<div class="baseline"></div>' + bars + '</div><div class="xlabels">' + labels + "</div>";
  }

  function hbars(rows, unit) {
    const max = Math.max(...rows.map((r) => r[1]), 1);
    return '<div class="hbars">' + rows.map(([label, v]) =>
      '<div class="hbar" data-tip-html="' + esc(esc(label) + " · <b>" + v + "</b> " + unit) + '" data-tip="x"><span class="l">' + esc(label) +
      '</span><div class="track"><i style="width:' + (v / max * 100) + '%"></i></div><span class="v">' + v + "</span></div>").join("") + "</div>";
  }

  const VERDICT_ROWS = [
    ["approved", "good", "✓", "Approved"],
    ["approved_with_comments", "warning", "●", "Approved w/ comments"],
    ["changes_requested", "critical", "✕", "Changes requested"],
  ];

  function verdictChart(counts) {
    const total = VERDICT_ROWS.reduce((n, [k]) => n + (counts[k] || 0), 0);
    if (!total) return null;
    const stack = VERDICT_ROWS.filter(([k]) => counts[k]).map(([k, cls, , label]) =>
      '<div class="' + cls + '" style="flex-grow:' + counts[k] + '" data-tip-html="' + esc(label + " · <b>" + counts[k] + "</b>") + '" data-tip="x"></div>').join("");
    const legend = VERDICT_ROWS.map(([k, cls, ic, label]) => {
      const v = counts[k] || 0;
      return '<div class="legend-row"><span class="sw" style="background:var(--' + cls + ')"></span><span>' + ic + " " + label +
        '</span><span class="v">' + v + '</span><span class="p">' + Math.round(v / total * 100) + "%</span></div>";
    }).join("");
    return '<div class="stack" role="img" aria-label="Verdict mix">' + stack + '</div><div class="legend">' + legend + "</div>";
  }

  function chartSpecs(s) {
    const days = s.per_day.map((v, i) => {
      const d = new Date(Date.now() - (13 - i) * 86400000);
      const label = d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
      return [i === 13 ? "today" : label, v, label];
    });
    const bucketLabels = ["0–2", "2–4", "4–6", "6–8", "8–10"];
    const langs = Object.entries(s.languages || {});
    return {
      per_day: { head: ["Day", "Reviews"], rows: days.map((d) => [d[2], d[1]]), html: s.total ? vbars(days, "reviews") : null },
      verdicts: { head: ["Verdict", "Reviews"], rows: VERDICT_ROWS.map(([k, , , l]) => [l, (s.verdicts || {})[k] || 0]), html: verdictChart(s.verdicts || {}) },
      scores: { head: ["Score", "Reviews"], rows: s.score_buckets.map((v, i) => [bucketLabels[i], v]),
        html: s.score_buckets.some(Boolean) ? vbars(s.score_buckets.map((v, i) => [bucketLabels[i], v, "score " + bucketLabels[i]]), "reviews") : null },
      languages: { head: ["Language", "Reviews"], rows: langs, html: langs.length ? hbars(langs, "reviews") : null },
    };
  }

  function renderCharts() {
    const specs = chartSpecs(stats);
    document.querySelectorAll("[data-chart]").forEach((card) => {
      const key = card.dataset.chart;
      const spec = specs[key];
      const body = card.querySelector(".chart-body");
      if (!spec.html) body.innerHTML = '<div class="chart-empty">No data yet</div>';
      else body.innerHTML = tableView[key] ? dataTable(spec.head, spec.rows) : spec.html;
      bindTips(body);
    });
  }

  document.querySelectorAll(".tbl-toggle").forEach((btn) => btn.addEventListener("click", () => {
    const key = btn.closest("[data-chart]").dataset.chart;
    tableView[key] = !tableView[key];
    btn.setAttribute("aria-pressed", String(tableView[key]));
    btn.textContent = tableView[key] ? "chart" : "table";
    if (stats) renderCharts();
  }));

  // ------------------------------------------------ KPIs
  function renderKpis() {
    const set = (k, v) => { document.querySelector('[data-kpi="' + k + '"]').textContent = v; };
    set("total", stats.total);
    set("avg_score", stats.avg_score != null ? stats.avg_score.toFixed(1) : "–");
    set("comments", stats.comments);
    set("security", stats.security);
    set("avg_duration", stats.avg_duration != null ? stats.avg_duration + "s" : "–");
    document.querySelector('[data-kpi-sub="total"]').textContent = stats.failed ? stats.completed + " ok · " + stats.failed + " failed" : stats.completed + " completed";
    document.querySelector('[data-kpi-sub="security"]').innerHTML = stats.critical
      ? '<span class="crit">▲ ' + stats.critical + " high / critical</span>" : "none high / critical";
  }

  // ------------------------------------------------ table
  function matches(r) {
    if (filter === "active" && !(r.status === "running" || r.status === "queued")) return false;
    if (filter !== "all" && filter !== "active" && r.verdict !== filter) return false;
    if (!query) return true;
    return [r.repo, r.title, r.author, "#" + r.pr_number].join(" ").toLowerCase().includes(query);
  }

  function renderTable() {
    const rows = reviews.filter(matches);
    $("#rows").innerHTML = rows.map((r) => {
      const isNew = knownIds && !knownIds.has(r.id);
      return '<tr tabindex="0" data-id="' + esc(r.id) + '"' + (isNew ? ' class="new-row"' : "") + ">" +
        '<td><div class="pr-cell"><span class="t">' + (r.is_public ? "" : '<span title="Private repository">🔒 </span>') + esc(r.title || "Pull request #" + r.pr_number) +
        '</span><span class="r">' + esc(r.repo) + " #" + r.pr_number + (r.author ? " · @" + esc(r.author) : "") + "</span></div></td>" +
        "<td>" + verdictPill(r.verdict, r.status) + "</td>" +
        '<td class="num">' + (r.score != null ? r.score.toFixed(1) : "–") + "</td>" +
        '<td class="num find">' + r.comments + " · " + (r.critical ? '<span class="c">▲' + r.security + "</span>" : r.security) + "</td>" +
        '<td><span class="src">' + esc(r.source) + "</span></td>" +
        '<td class="num">' + (r.duration != null ? r.duration + "s" : "–") + "</td>" +
        '<td class="muted" title="' + esc(new Date(r.created_at * 1000).toLocaleString()) + '">' + timeAgo(r.created_at) + "</td></tr>";
    }).join("");
    knownIds = new Set(reviews.map((r) => r.id));

    const empty = $("#empty");
    if (!reviews.length) {
      empty.innerHTML = '<span class="big" aria-hidden="true">🛰️</span>No reviews yet. <a href="/">Run one from the home page</a> or connect a GitHub webhook.';
    } else if (!rows.length) {
      empty.textContent = "No reviews match this filter.";
    }
    empty.classList.toggle("hidden", rows.length > 0);
  }

  $("#search").addEventListener("input", (e) => { query = e.target.value.trim().toLowerCase(); renderTable(); });
  document.querySelectorAll(".chip").forEach((chip) => chip.addEventListener("click", () => {
    filter = chip.dataset.filter;
    document.querySelectorAll(".chip").forEach((c) => c.setAttribute("aria-pressed", String(c === chip)));
    renderTable();
  }));
  $("#rows").addEventListener("click", (e) => { const tr = e.target.closest("tr[data-id]"); if (tr) openDrawer(tr.dataset.id); });
  $("#rows").addEventListener("keydown", (e) => {
    const tr = e.target.closest("tr[data-id]");
    if (tr && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); openDrawer(tr.dataset.id); }
  });

  // ------------------------------------------------ drawer
  let drawerToken = 0;
  let lastFocus = null;

  async function openDrawer(id) {
    const token = ++drawerToken;
    lastFocus = document.activeElement;
    const drawer = $("#drawer");
    drawer.classList.add("open");
    drawer.setAttribute("aria-hidden", "false");
    $("#drawer-pipeline").innerHTML = pipelineHTML();
    $("#drawer-result").innerHTML = '<div class="empty">Loading…</div>';
    drawer.querySelector(".drawer-head [data-close]").focus();
    history.replaceState(null, "", "#" + id);

    for (;;) {
      let review;
      try { review = await api("/api/reviews/" + encodeURIComponent(id)); }
      catch (err) { if (token === drawerToken) $("#drawer-result").innerHTML = '<div class="notice error">' + esc(err.message) + "</div>"; return; }
      if (token !== drawerToken) return;
      $("#drawer-title").textContent = review.repo + " #" + review.pr_number + " · " + review.source;
      updatePipeline($("#drawer-pipeline"), review);
      if (review.status === "done" || review.status === "failed") { renderResult($("#drawer-result"), review); return; }
      $("#drawer-result").innerHTML = '<div class="empty"><span class="big" aria-hidden="true">⏳</span>Agents are working on it…</div>';
      await new Promise((r) => setTimeout(r, 1000));
    }
  }

  function closeDrawer() {
    drawerToken++;
    const drawer = $("#drawer");
    drawer.classList.remove("open");
    drawer.setAttribute("aria-hidden", "true");
    history.replaceState(null, "", location.pathname);
    if (lastFocus) lastFocus.focus();
  }
  document.querySelectorAll("[data-close]").forEach((el) => el.addEventListener("click", closeDrawer));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && $("#drawer").classList.contains("open")) closeDrawer(); });

  // ------------------------------------------------ private-repo unlock
  function renderScope(admin) {
    const btn = $("#lock-btn");
    btn.textContent = admin ? "🔓 Private repos visible · lock" : "🔒 Unlock private repos";
    $("#scope-note").textContent = admin ? "Showing all reviews, including private repositories." : "Showing reviews of public repositories.";
  }

  $("#lock-btn").addEventListener("click", () => {
    if (stats && stats.admin) { setToken(""); refresh(); return; }
    const form = $("#token-form");
    const open = form.classList.toggle("hidden") === false;
    $("#lock-btn").setAttribute("aria-expanded", String(open));
    if (open) $("#token-input").focus();
  });
  $("#token-cancel").addEventListener("click", () => { $("#token-form").classList.add("hidden"); $("#lock-btn").setAttribute("aria-expanded", "false"); });
  $("#token-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    setToken($("#token-input").value.trim());
    await refresh();
    const err = $("#token-error");
    if (stats && stats.admin) {
      $("#token-form").classList.add("hidden");
      $("#token-input").value = "";
      err.classList.add("hidden");
    } else {
      setToken("");
      err.textContent = "That token wasn't accepted.";
      err.classList.remove("hidden");
    }
  });

  // ------------------------------------------------ data loop
  async function refresh() {
    try {
      const [s, list] = await Promise.all([api("/api/stats"), api("/api/reviews?limit=100")]);
      stats = s;
      reviews = list.reviews;
      renderScope(s.admin);
      renderKpis();
      renderCharts();
      renderTable();
      $("#live-label").textContent = "live · refreshes every 10s";
      $("#live-dot").style.background = "";
    } catch (err) {
      $("#live-label").textContent = "offline · retrying";
      $("#live-dot").style.background = "var(--critical)";
    }
  }

  refresh();
  setInterval(() => { if (!document.hidden) refresh(); }, REFRESH_MS);
  if (location.hash.length > 1) openDrawer(location.hash.slice(1));
})();
