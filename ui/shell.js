/* 外殼設定：頂列、左導覽、右側任務面板。載入順序：core → globals → jobs → shell → shared → 頁面 → 本檔。 */
"use strict";

(function () {
  const nameCache = new Map();

  async function bookName(bookId) {
    if (!nameCache.has(bookId)) {
      try {
        const data = await FE.api(`/api/books/${bookId}/chapters`);
        nameCache.set(bookId, (data.book.display_name || {})[data.book.lang] || bookId);
      } catch (_) {
        nameCache.set(bookId, bookId);
      }
    }
    return nameCache.get(bookId);
  }

  const COMIC_TABS = [
    ["project", "專案與設定"],
    ["chapter", "章節與劇本"],
    ["storyboard", "分鏡"],
    ["render", "出圖"],
    ["letters", "嵌字"],
    ["pages", "頁面與輸出"],
  ];

  async function navItems(route) {
    const parts = route.parts || [];
    const bookId = parts[0] === "book" ? parts[1] : null;
    const items = [{ group: "書架" }, { label: "書架", href: "#/", page: "home" }];
    if (bookId) {
      const title = await bookName(bookId);
      const base = "#/book/" + bookId;
      items.push(
        { group: `${title} · 共用資源` },
        { label: "總覽", href: `${base}/overview`, page: "book/:id/overview" },
        { label: "角色與外觀", href: `${base}/appearance`, page: ["book/:id/appearance", "book/:id/cast"] },
        { label: "地點與設定集", href: `${base}/bible`, page: "book/:id/bible" },

        { group: "視覺小說（VN）" },
        { label: "章節與校對", href: base, page: ["book/:id", "book/:id/ch/:n"] },
        { label: "素材與畫風", href: `${base}/assets`, page: "book/:id/assets" },
        { label: "音樂", href: `${base}/audio`, page: "book/:id/audio" },
        { label: "語音", href: `${base}/voice`, page: "book/:id/voice" },
        { label: "匯出 Ren'Py", href: `${base}/export`, page: "book/:id/export" },

        { group: "漫畫（Comic）" },
        ...COMIC_TABS.map(([key, label]) => ({
          label,
          href: `${base}/comic?tab=${key}`,
          active: (r) => {
            if (r.page !== "book/:id/comic") return false;
            const cur = (r.query && r.query.tab) || (r.args && r.args[0]) || FE.store.get("comic_tab", "project") || "project";
            return cur === key;
          },
        }))
      );
    }
    items.push({ group: "系統" });
    if (bookId) {
      items.push({ label: "任務紀錄", href: `#/book/${bookId}/jobs`, page: "book/:id/jobs" });
    }
    items.push({ label: "設定", href: "#/settings", page: "settings" });
    return items;
  }

  /* ---- 右側面板：上面是執行者下拉，下面是 fekit 的任務面板 ---- */
  const Executors = (function () {
    const node = h("select", { title: "下一個任務要用的 AI", onchange: (event) => FE.store.set("executor", event.target.value) });
    async function reload() {
      try {
        const state = await FE.api("/api/settings");
        const choices = state.executors.choices;
        const saved = FE.store.get("executor", "");
        node.replaceChildren(
          h("option", { value: "" }, `預設執行者（${state.executors.default}）`),
          ...choices.map((c) => h("option", { value: c.id, selected: c.id === saved }, c.name || c.id)));
        if (saved && !choices.some((c) => c.id === saved)) FE.store.remove("executor");
      } catch (_) {
        node.replaceChildren(h("option", { value: "" }, "預設執行者"));
      }
    }
    return { node, value: () => node.value || "", reload };
  })();
  window.Executors = Executors;

  let panel = null;

  function mountPanel(el, shell) {
    const head = h("div", { class: "row", style: "padding:0 0 var(--sp-2)" },
      h("span", { class: "small muted" }, "執行者"), Executors.node);
    const body = h("div", { style: "flex:1;min-height:0;display:flex;flex-direction:column" });
    el.append(h("div", { class: "stack", style: "height:100%" }, head, body));
    Executors.reload().catch(FE.fail);

    panel = FE.jobs.panel({
      root: body,
      base: "/api/fe-jobs",
      kinds: KIND_LABEL,
      phases: {
        starting: "準備中", waiting: "已送出，等待模型開始回應", streaming: "模型輸出中",
        thinking: "模型思考中", checking: "檢查輸出是否符合規則", repair: "輸出有問題，請模型修正",
        idle: "待命",
      },
      empty: "目前沒有任務。到「章節」勾選章節後按「AI 標註」，模型輸出會在這裡即時顯示。",
      onreveal: () => shell.toggle("panel", true),
      onchange: (job) => shell.badge("panel",
        !job ? null
          : job.status === "running" || job.status === "queued" ? "busy"
          : job.status === "done" ? "ok"
          : job.status === "error" ? "err" : "warn"),
      onend: (job) => {
        if (job.status !== "done" || FE.dirty.is()) return;
        const now = FE.current();
        if (now && (now.page === "book/:id" || now.page === "book/:id/cast")) FE.route();
      },
    });
    window.Panel = panel;
    panel.resume().catch(FE.fail);
  }

  const suiteLink = h("a", {
    class: "chip",
    href: "#",
    target: "t2x_portal",
    rel: "noopener",
    hidden: true,
    title: "回到 t2x-suite 跨管線總覽 Portal",
  }, "Portal ↗");
  const nsLink = h("a", {
    class: "chip",
    href: "#",
    target: "novel_studio",
    rel: "noopener",
    hidden: true,
    title: "回到 Novel Studio 編輯原著與設定集",
  }, "Novel Studio ↗");
  const chip = h("a", { class: "chip", href: "#/settings", "data-llm-chip": "", title: "AI 設定" }, "AI");
  const topSlot = h("div", { class: "row", style: "gap:6px;align-items:center" }, suiteLink, nsLink, chip);

  FE.api("/api/settings").then((st) => {
    const suite = (st && st.suite) || {};
    if (suite.suite_url) {
      suiteLink.href = suite.suite_url + "/";
      suiteLink.hidden = false;
    }
    if (suite.ns_url) {
      nsLink.href = suite.ns_url + "/";
      nsLink.hidden = false;
    }
  }).catch(() => {});

  FE.shell.mount({
    brand: { label: "視覺小說工作台", mark: "幕" },
    nav: navItems,
    panel: mountPanel,
    panel_label: "AI",
    slots: { top: topSlot },
    sizes: { nav: { width: 190 }, panel: { max: 720, width: 380 } },
  });

  refreshChip();
})();
