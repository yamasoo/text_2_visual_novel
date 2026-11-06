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

  const BOOK_PAGES = [
    ["", "章節"], ["cast", "角色表"], ["overview", "總覽"], ["bible", "設定集"],
    ["assets", "素材"], ["audio", "音樂"], ["voice", "語音"], ["export", "輸出"], ["jobs", "任務紀錄"],
  ];

  async function navItems(route) {
    const parts = route.parts || [];
    const bookId = parts[0] === "book" ? parts[1] : null;
    const items = [{ group: "書架" }, { label: "書架", href: "#/", page: "home" }];
    if (bookId) {
      items.push({ group: "這本書" });
      // 書名只是「這本書」的標題項：#/book/:id 由下面的「章節」負責亮，兩項才不會同時亮；
      // 校對頁（book/:id/ch/:n）沒有自己的導覽項，由書名代表這一本。
      items.push({ label: await bookName(bookId), href: `#/book/${bookId}`,
        page: ["book/:id/ch/:n"] });
      for (const [key, label] of BOOK_PAGES) {
        items.push({
          label,
          href: "#/book/" + bookId + (key ? "/" + key : ""),
          page: key ? "book/:id/" + key : "book/:id",
        });
      }
    }
    items.push({ group: "系統" }, { label: "設定", href: "#/settings", page: "settings" });
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

  const chip = h("a", { class: "chip", href: "#/settings", "data-llm-chip": "", title: "AI 設定" }, "AI");

  FE.shell.mount({
    brand: { label: "視覺小說工作台", mark: "幕" },
    nav: navItems,
    panel: mountPanel,
    panel_label: "AI",
    slots: { top: chip },
    sizes: { nav: { width: 190 }, panel: { max: 720, width: 380 } },
  });

  refreshChip();
})();
