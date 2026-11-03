/* 外殼：左導覽、可拖曳調寬的三欄、右側 AI 面板（任務進度＋模型串流輸出）。載入順序：app.js → page_*.js → 本檔。 */
"use strict";

const store = {
  get(key, fallback) { try { const v = localStorage.getItem("t2vn." + key); return v == null ? fallback : v; } catch (_) { return fallback; } },
  set(key, value) { try { localStorage.setItem("t2vn." + key, value); } catch (_) { /* 私密視窗等：忽略 */ } },
};

/* ---------------------------------------------------------------- 欄寬與收合 */

const Shell = (() => {
  const shell = $("#shell");
  const LIMITS = { nav: [140, 360, 190], ai: [260, 720, 380] };

  function width(name, value) {
    const [lo, hi] = LIMITS[name];
    const w = Math.max(lo, Math.min(hi, Math.round(value)));
    shell.style.setProperty(name === "nav" ? "--navw" : "--aiw", w + "px");
    return w;
  }
  function restore() {
    for (const name of ["nav", "ai"]) {
      width(name, Number(store.get("w." + name, LIMITS[name][2])));
      shell.classList.toggle(name + "-off", store.get("off." + name, "0") === "1");
    }
  }
  function drag(name, splitter) {
    splitter.addEventListener("pointerdown", (event) => {
      event.preventDefault();
      splitter.setPointerCapture(event.pointerId);
      splitter.classList.add("drag"); document.body.classList.add("dragging");
      const move = (e) => {
        const box = shell.getBoundingClientRect();
        width(name, name === "nav" ? e.clientX - box.left : box.right - e.clientX);
      };
      const up = () => {
        splitter.classList.remove("drag"); document.body.classList.remove("dragging");
        splitter.removeEventListener("pointermove", move); splitter.removeEventListener("pointerup", up);
        store.set("w." + name, parseInt(getComputedStyle(shell).getPropertyValue(name === "nav" ? "--navw" : "--aiw"), 10));
      };
      splitter.addEventListener("pointermove", move); splitter.addEventListener("pointerup", up);
    });
    splitter.addEventListener("dblclick", () => store.set("w." + name, width(name, LIMITS[name][2])));
  }
  function toggle(name) {
    if (matchMedia("(max-width: 900px)").matches) { shell.classList.toggle(name + "-open"); return; }
    const off = !shell.classList.contains(name + "-off");
    shell.classList.toggle(name + "-off", off); store.set("off." + name, off ? "1" : "0");
  }

  /* ---- 左導覽 */
  const names = new Map();
  async function bookName(id) {
    if (!names.has(id)) {
      try { const d = await api(`/api/books/${id}/chapters`); names.set(id, (d.book.display_name || {})[d.book.lang] || id); } catch (_) { names.set(id, id); }
    }
    return names.get(id);
  }
  const BOOK_PAGES = [
    ["", "章節"], ["cast", "角色表"], ["overview", "總覽"], ["bible", "設定集"], ["assets", "素材"], ["audio", "音樂"], ["voice", "語音"], ["export", "輸出"], ["jobs", "任務紀錄"],
  ];
  async function updateNav() {
    const parts = (location.hash || "#/").replace(/^#\/?/, "").split("/").filter(Boolean);
    const bookId = parts[0] === "book" ? parts[1] : null;
    const page = bookId ? (parts[2] === "ch" ? "" : parts[2] || "") : null;
    const link = (href, label, on) => h("a", { class: "nav-link" + (on ? " on" : ""), href }, label);
    const groups = [h("div", { class: "nav-group" }, link("#/", "書架", !bookId && parts[0] !== "settings"))];
    if (bookId) {
      groups.push(h("div", { class: "nav-group" }, h("h3", {}, "這本書"), h("div", { class: "nav-book", title: bookId }, await bookName(bookId)),
        BOOK_PAGES.map(([key, label]) => link(`#/book/${bookId}` + (key ? "/" + key : ""), label, page === key))));
    }
    groups.push(h("div", { class: "nav-group" }, h("h3", {}, "系統"), link("#/settings", "設定", parts[0] === "settings")));
    $("#nav").replaceChildren(...groups);
  }

  function init() {
    restore(); drag("nav", $("#split-nav")); drag("ai", $("#split-ai"));
    $("#nav-toggle").addEventListener("click", () => toggle("nav"));
    $("#ai-toggle").addEventListener("click", () => toggle("ai"));
    window.addEventListener("hashchange", updateNav);
    updateNav();
  }
  return { init, updateNav, width };
})();

/* ---------------------------------------------------------------- 右側 AI 面板 */

const PHASE = { starting: "準備中", waiting: "已送出，等待模型開始回應", streaming: "模型輸出中", thinking: "模型思考中", checking: "檢查輸出是否符合規則", repair: "輸出有問題，請模型修正", idle: "待命", finished: "結束" };
const ITEM_ICON = { pending: ["○", ""], running: ["●", "info"], done: ["✔", "ok"], skipped: ["↷", ""], error: ["✖", "bad"], stopped: ["■", "warn"] };
const KIND_LABEL = { annotate: "AI 標註", extract: "抽取章綱與角色", art: "出圖（ComfyUI）" };

const AiPanel = (() => {
  const root = $("#ai");
  const head = h("div", { class: "ai-head" });
  const status = h("div", { class: "ai-status" });
  const stream = h("div", { class: "ai-stream" });
  const logBox = h("pre", { class: "log ai-log", hidden: true });
  const follow = h("input", { type: "checkbox", checked: true });
  const logToggle = h("button", { class: "ghost sm", onclick: () => { logBox.hidden = !logBox.hidden; logToggle.textContent = logBox.hidden ? "紀錄" : "收起紀錄"; } }, "紀錄");
  const foot = h("div", { class: "ai-foot" }, h("label", {}, follow, " 自動捲到最新"), h("span", { class: "grow" }), logToggle,
    h("button", { class: "ghost sm", onclick: () => { stream.replaceChildren(); segs.clear(); } }, "清空畫面"));
  root.append(head, h("div", { class: "ai-body" }, status, stream, logBox, foot));

  let job = null, timer = null, jobId = "", streamLast = 0, logLen = 0, lastStatus = "", phaseStart = Date.now(), lastKey = "";
  const segs = new Map();
  const log = [];

  /* ---- 執行者下拉（只影響下一次按下「AI 標註」）*/
  const select_ = h("select", { title: "下一個任務要用的 AI", onchange: (e) => store.set("executor", e.target.value) });
  head.append(h("strong", {}, "AI 面板"), select_);
  async function loadChoices() {
    try {
      const st = await api("/api/settings");
      const ex = st.executors, saved = store.get("executor", "");
      const rows = ex.choices;
      select_.replaceChildren(h("option", { value: "" }, `預設執行者（${ex.default}）`),
        ...rows.map((c) => h("option", { value: c.id, selected: c.id === saved }, c.name || c.id)));
      if (saved && !rows.some((c) => c.id === saved)) store.set("executor", "");
    } catch (_) { select_.replaceChildren(h("option", { value: "" }, "預設執行者")); }
  }
  const executor = () => select_.value || "";

  /* ---- 串流輸出 */
  const nearBottom = () => stream.scrollHeight - stream.scrollTop - stream.clientHeight < 40;
  function segEl(id) {
    if (!segs.has(id)) {
      const label = h("div", { class: "seg-label" });
      const text = h("div", { class: "seg-text" });
      const el = h("div", { class: "seg live" }, label, text);
      stream.append(el);
      segs.set(id, { el, label, text, think: null, thinkBody: null });
    }
    return segs.get(id);
  }
  function apply(events) {
    const stick = follow.checked && nearBottom();
    for (const ev of events) {
      const seg = segEl(ev.seg);
      if (ev.type === "begin") seg.label.textContent = ev.label || `第 ${ev.seg} 段`;
      else if (ev.type === "text") seg.text.append(document.createTextNode(ev.text));
      else if (ev.type === "thinking") {
        if (!seg.think) {
          seg.thinkBody = h("pre", {});
          seg.think = h("details", {}, h("summary", {}, "思考過程"), seg.thinkBody);
          seg.el.insertBefore(seg.think, seg.text);
        }
        seg.thinkBody.append(document.createTextNode(ev.text));
      } else if (ev.type === "end") {
        seg.el.classList.remove("live");
        if (ev.note) seg.el.append(h("div", { class: "seg-note" }, ev.note));
      }
    }
    if (stick) stream.scrollTop = stream.scrollHeight;
  }

  /* ---- 輪詢 */
  async function poll() {
    clearTimeout(timer);
    let delay = 5000;
    try {
      const active = (await api("/api/jobs/active")).job;
      if (active) {
        if (active.id !== jobId) { jobId = active.id; streamLast = 0; logLen = 0; log.length = 0; stream.replaceChildren(); segs.clear(); logBox.textContent = ""; }
        const full = await api(`/api/jobs/${active.id}?since=${logLen}&stream_since=${streamLast}`);
        if (full.stream.truncated) stream.append(h("div", { class: "seg-note" }, "（較早的輸出已被丟掉，緩衝只保留最近一段）"));
        apply(full.stream.events); streamLast = full.stream.last;
        log.push(...full.log); logLen = full.log_total; logBox.textContent = log.join("\n");
        const key = `${full.id}|${full.current.chapter}|${full.current.phase}|${full.current.round}|${full.current.asset}|${full.current.n}`;
        if (key !== lastKey) { lastKey = key; phaseStart = Date.now(); }
        const finishedNow = lastStatus === "running" && full.status !== "running";
        job = full; lastStatus = full.status;
        if (finishedNow) {
          toast(full.status === "done" ? "任務完成" : full.status === "stopped" ? "任務已停止" : "任務結束，有錯誤，請看右側面板", full.status === "error" ? "bad" : "");
          if (/^#\/book\/[^/]+(\/cast)?$/.test(location.hash) && !dirty) route();
        }
        if (full.status === "running") delay = 700;
      } else { job = null; }
    } catch (_) { /* 伺服器暫時沒回應：下一輪再試 */ }
    drawStatus();
    timer = setTimeout(poll, delay);
  }

  function drawStatus() {
    if (!job) { status.replaceChildren(h("div", { class: "muted" }, "目前沒有任務。到「章節」勾選章節後按「AI 標註」，模型輸出會在這裡即時顯示。")); return; }
    const running = job.status === "running", cur = job.current;
    const isArt = job.kind === "art";
    const total = isArt ? (cur.total || 0) : job.items.length;
    const finished = isArt ? Math.max(0, (cur.index || 1) - 1 + (cur.event === "asset_done" ? 1 : 0)) : job.items.filter((i) => ["done", "skipped", "error", "stopped"].includes(i.status)).length;
    const secs = Math.round((Date.now() - phaseStart) / 1000);
    const step = running && isArt
      ? (cur.asset ? `${cur.asset}` + (cur.of ? `（第 ${cur.n}/${cur.of} 張）` : "") : "準備中") + (job.stop_requested ? " · 停止中…" : "")
      : running
      ? `第 ${cur.chapter ?? "?"} 章 · ` + (cur.chunk ? `第 ${cur.chunk} 塊（第 ${cur.chunk_range[0]}–${cur.chunk_range[1]} 段）· ` : "") + (cur.round ? `第 ${cur.round} 輪 · ` : "") + (PHASE[cur.phase] || cur.phase)
        + (cur.phase === "streaming" ? `（${cur.chars.toLocaleString()} 字）` : "") + ` · ${secs} 秒` + (job.stop_requested ? " · 停止中…" : "")
      : ({ done: "完成", stopped: "已停止", error: "結束（有錯誤）" }[job.status] || job.status) + ` · 用時 ${Math.round(job.elapsed)} 秒`;
    status.replaceChildren(
      h("div", { class: "row" }, h("strong", {}, `${KIND_LABEL[job.kind] || job.kind} · ${job.book_id}`), h("span", { class: "grow" }),
        running ? h("button", { class: "sm", disabled: job.stop_requested, onclick: async () => { try { await api(`/api/jobs/${job.id}/stop`, { method: "POST", body: {} }); poll(); } catch (e) { fail(e); } } }, job.stop_requested ? "停止中…" : "■ 停止") : ""),
      h("div", { class: "small muted" }, `執行者：${job.executor || "預設"} · ${isArt ? "" : job.tokens.toLocaleString() + " tokens · "}${finished}/${total} ${isArt ? "項" : "章"}`),
      h("div", { class: "bar" }, h("i", { style: `width:${total ? Math.round((100 * finished) / total) : 0}%` })),
      h("div", { class: "small", style: "margin-top:4px" }, step),
      h("div", { class: "chips" }, job.items.map((i) => { const [icon, kind] = ITEM_ICON[i.status] || ["?", ""]; return h("span", { class: "chip " + kind, title: i.note || i.status }, `${icon} 第 ${i.no} 章`); })));
  }
  setInterval(() => { if (job && job.status === "running") drawStatus(); }, 1000);

  loadChoices();
  return { poll, executor, reloadChoices: loadChoices };
})();

Shell.init();
refreshChip();
route();
AiPanel.poll();
