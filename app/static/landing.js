(function () {
  "use strict";
  const { api, esc, pipelineHTML, updatePipeline, renderResult } = window.CI;
  const $ = (sel) => document.querySelector(sel);
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  // ------------------------------------------------ hero diff animation
  const DIFF = [
    [" ", 12, '<span class="tok-k">from</span> flask <span class="tok-k">import</span> request'],
    [" ", 13, ""],
    ["-", 14, '<span class="tok-k">def</span> <span class="tok-f">calc</span>():'],
    ["+", 14, '<span class="tok-k">def</span> <span class="tok-f">calc</span>(expr: str):'],
    ["+", 15, '    <span class="tok-c"># quick maths endpoint</span>'],
    ["+", 16, '    <span class="tok-k">return</span> <span class="tok-f">eval</span>(request.args[<span class="tok-s">"q"</span>])', "flag"],
    [" ", 17, ""],
    ["+", 18, 'STRIPE_KEY = <span class="tok-s">"sk_live_51Hx…9fQ"</span>', "flag"],
    ["+", 19, ""],
    ["+", 20, '<span class="tok-k">def</span> <span class="tok-f">charge</span>(user, amount):'],
    ["+", 21, '    <span class="tok-k">return</span> stripe.charge(user.id, amount)'],
  ];

  async function playDiff() {
    const body = $("#demo-diff");
    const badges = [...document.querySelectorAll(".float-badge")];
    body.innerHTML = DIFF.map(([sign, n, code, flag]) =>
      '<div class="dl ' + (sign === "+" ? "plus" : sign === "-" ? "minus" : "") + '" data-flag="' + (flag || "") +
      '"><span class="ln">' + n + '</span><span class="sg">' + (sign === " " ? "" : sign) + "</span><span>" + code + "</span></div>"
    ).join("");
    const lines = [...body.children];
    if (reduceMotion) {
      lines.forEach((l) => { l.classList.add("in"); if (l.dataset.flag) l.classList.add("flag"); });
      badges.forEach((b) => b.classList.add("in"));
      return;
    }
    for (;;) {
      lines.forEach((l) => l.classList.remove("in", "flag"));
      badges.forEach((b) => b.classList.remove("in"));
      await sleep(400);
      for (const l of lines) { l.classList.add("in"); await sleep(110); }
      await sleep(500);
      const flagged = lines.filter((l) => l.dataset.flag);
      flagged[0].classList.add("flag"); badges[0].classList.add("in"); await sleep(700);
      flagged[1].classList.add("flag"); badges[1].classList.add("in"); await sleep(900);
      badges[2].classList.add("in");
      await sleep(4200);
    }
  }

  // ------------------------------------------------ architecture: simulated run
  async function playArch() {
    const root = $("#arch-pipeline");
    root.innerHTML = pipelineHTML();
    const seq = [
      {}, { fetch_context: 0.9 }, { fetch_context: 0.9, security_scan: 6.2 },
      { fetch_context: 0.9, security_scan: 6.2, code_review: 8.7 },
      { fetch_context: 0.9, security_scan: 6.2, code_review: 8.7, generate_summary: 11.3 },
      { fetch_context: 0.9, security_scan: 6.2, code_review: 8.7, generate_summary: 11.3, post_review: 12.0 },
    ];
    const asSteps = (o) => Object.fromEntries(Object.entries(o).map(([k, v]) => [k, { finished: v }]));
    if (reduceMotion) { updatePipeline(root, { status: "done", steps: asSteps(seq[seq.length - 1]) }); return; }
    for (;;) {
      for (let i = 0; i < seq.length; i++) {
        const last = i === seq.length - 1;
        updatePipeline(root, { status: last ? "done" : "running", steps: asSteps(seq[i]) });
        await sleep(last ? 3200 : 1300);
      }
    }
  }

  // ------------------------------------------------ stats strip
  function countUp(el, to, suffix) {
    if (reduceMotion || !Number.isFinite(to)) { el.textContent = to + (suffix || ""); return; }
    const start = performance.now(), dur = 1100, decimals = Number.isInteger(to) ? 0 : 1;
    const tick = (t) => {
      const p = Math.min(1, (t - start) / dur), e = 1 - Math.pow(1 - p, 3);
      el.textContent = (to * e).toFixed(decimals) + (suffix || "");
      if (p < 1) requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  }
  async function loadStats() {
    try {
      const s = await api("/api/stats");
      if (!s.total) return;
      $("#stats-strip").classList.remove("hidden");
      countUp(document.querySelector('[data-stat="total"]'), s.total);
      countUp(document.querySelector('[data-stat="comments"]'), s.comments);
      countUp(document.querySelector('[data-stat="security"]'), s.security);
      const avg = document.querySelector('[data-stat="avg_duration"]');
      if (s.avg_duration != null) countUp(avg, s.avg_duration, "s");
    } catch (_) { /* the strip is decoration; stay hidden */ }
  }

  // ------------------------------------------------ live run
  let pollToken = 0;
  let timer = null;

  function log(html, cls) {
    const c = $("#console");
    const line = document.createElement("div");
    line.innerHTML = html;
    if (cls) line.className = cls;
    c.appendChild(line);
    c.scrollTop = c.scrollHeight;
  }

  function startTimer() {
    const t0 = performance.now();
    clearInterval(timer);
    timer = setInterval(() => { $("#run-elapsed").textContent = ((performance.now() - t0) / 1000).toFixed(1) + "s"; }, 100);
  }

  async function watch(id, fresh) {
    const token = ++pollToken;
    const section = $("#try");
    section.classList.remove("hidden");
    $("#pipeline").innerHTML = pipelineHTML();
    $("#console").innerHTML = "";
    $("#result").innerHTML = "";
    $("#run-kicker").textContent = "● live run";
    $("#run-title").textContent = "Inspecting…";
    section.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "start" });
    startTimer();

    const seen = new Set();
    let announced = false;
    for (;;) {
      if (token !== pollToken) return;
      let review;
      try { review = await api("/api/reviews/" + encodeURIComponent(id)); }
      catch (err) {
        clearInterval(timer);
        $("#run-title").textContent = "Review not found";
        log('<span class="err">✕ ' + esc(err.message) + "</span>");
        return;
      }
      if (!announced) {
        announced = true;
        $("#run-title").textContent = review.repo + " #" + review.pr_number;
        if (fresh) log('<span class="t">[0.0s]</span> queued <b>' + esc(review.repo) + "#" + review.pr_number + "</b> · dry run");
      }
      updatePipeline($("#pipeline"), review);
      Object.entries(review.steps || {})
        .sort((a, b) => a[1].finished - b[1].finished)
        .forEach(([node, info]) => {
          if (seen.has(node)) return;
          seen.add(node);
          log('<span class="t">[' + info.finished.toFixed(1) + 's]</span> <span class="ok">✓</span> ' + esc(node));
        });

      if (review.status === "done" || review.status === "failed") {
        clearInterval(timer);
        if (review.duration != null) $("#run-elapsed").textContent = review.duration.toFixed(1) + "s";
        $("#run-kicker").textContent = review.status === "done" ? "✓ complete" : "✕ failed";
        const r = review.result || {};
        if (review.status === "done") {
          log('<span class="ok">✓</span> verdict <b>' + esc(r.overall_verdict || "n/a") + "</b> · score " +
            esc(r.code_quality_score != null ? r.code_quality_score : "–") + " · " +
            (r.review_comments || []).length + " comments · " + (r.security_issues || []).length + " security findings");
        } else {
          log('<span class="err">✕ ' + esc((r.errors || []).join(" · ") || "review failed") + "</span>");
        }
        renderResult($("#result"), review);
        loadStats();
        return;
      }
      await sleep(900);
    }
  }

  function showError(message) {
    const el = $("#form-error");
    el.textContent = message;
    el.classList.toggle("hidden", !message);
  }

  $("#inspect-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const input = $("#pr-url");
    const btn = $("#inspect-btn");
    showError("");
    btn.disabled = true;
    btn.innerHTML = "Starting…";
    try {
      const res = await api("/api/reviews", { method: "POST", body: JSON.stringify({ pr_url: input.value.trim() }) });
      history.replaceState(null, "", "/?r=" + res.id + "#try");
      watch(res.id, true);
    } catch (err) {
      showError(err.message);
    } finally {
      btn.disabled = false;
      btn.innerHTML = "Inspect PR <kbd>↵</kbd>";
    }
  });

  $("#cta-try").addEventListener("click", (e) => {
    e.preventDefault();
    window.scrollTo({ top: 0, behavior: reduceMotion ? "auto" : "smooth" });
    setTimeout(() => $("#pr-url").focus({ preventScroll: true }), reduceMotion ? 0 : 500);
  });

  document.querySelectorAll("[data-copy]").forEach((btn) => btn.addEventListener("click", async () => {
    const text = btn.parentElement.querySelector("code").textContent;
    try { await navigator.clipboard.writeText(text); btn.textContent = "copied ✓"; }
    catch (_) { btn.textContent = "select + copy"; }
    setTimeout(() => (btn.textContent = "copy"), 1600);
  }));

  document.querySelectorAll(".feature").forEach((card) => card.addEventListener("pointermove", (e) => {
    const r = card.getBoundingClientRect();
    card.style.setProperty("--fx", e.clientX - r.left + "px");
    card.style.setProperty("--fy", e.clientY - r.top + "px");
  }));

  // ------------------------------------------------ GitHub App
  async function loadConfig() {
    let cfg;
    try { cfg = await api("/api/config"); } catch (_) { return; }
    if (cfg.install_url) {
      document.querySelectorAll("[data-install-link]").forEach((a) => { a.href = cfg.install_url; });
      ["#hero-install", "#app-install", "#self-host-label"].forEach((sel) => $(sel).classList.remove("hidden"));
      if (cfg.bot_login) document.querySelectorAll("[data-bot-name]").forEach((el) => { el.textContent = cfg.bot_login; });
    }
    if (cfg.demo_enabled === false) {
      $("#inspect-form").classList.add("hidden");
      document.querySelector(".hint").classList.add("hidden");
    }
  }

  // GitHub sends people back here after they install (the app's "Setup URL").
  const params = new URLSearchParams(location.search);
  if (params.get("setup_action") === "install" || params.get("setup_action") === "update") {
    $("#installed-banner").classList.remove("hidden");
    history.replaceState(null, "", "/");
  }
  $("#installed-close").addEventListener("click", () => $("#installed-banner").classList.add("hidden"));

  // ------------------------------------------------ boot
  playDiff();
  playArch();
  loadStats();
  loadConfig();
  const shared = params.get("r");
  if (shared) watch(shared, false);
})();
