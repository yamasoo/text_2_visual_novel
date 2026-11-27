/* 任務紀錄頁：歷次 AI 標註／抽取的結果與完整紀錄。#/book/<id>/jobs */
"use strict";

FE.PAGES["book/:id/jobs"] = async (ctx) => {
  const bookId = ctx.params.id;
  ctx.crumbs([{ label: "書架", href: "#/" }, { label: bookId, href: `#/book/${bookId}` }, { label: "任務紀錄" }]);
  const { records } = await FE.api(`/api/books/${bookId}/job-records`, { signal: ctx.signal });
  const STATUS = { done: ["完成", "ok"], error: ["有錯誤", "bad"], stopped: ["已停止", "warn"], running: ["執行中", "info"] };
  const when = (t) => new Date(t * 1000).toLocaleString("zh-TW", { hour12: false });
  const detail = h("div", { class: "card", hidden: true });
  async function open(id) {
    try {
      const r = await FE.api(`/api/books/${bookId}/job-records/${id}`, { signal: ctx.signal });
      detail.hidden = false;
      detail.replaceChildren(h("div", { class: "row" }, h("strong", {}, `${KIND_LABEL[r.kind] || r.kind} · ${when(r.started)}`), h("span", { class: "muted small" }, `執行者 ${r.executor || "?"} · ${(r.tokens || 0).toLocaleString()} tokens`), h("span", { class: "grow" }), h("button", { class: "ghost sm", onclick: () => { detail.hidden = true; } }, "✕")),
        h("div", { class: "row", style: "margin:6px 0" }, (r.items || []).map((i) => { const [icon, kind] = ITEM_ICON[i.state] || ["?", ""]; return h("span", { class: chipClass(kind), title: i.note }, `${icon} ${i.label}${i.note ? "：" + i.note : ""}`); })),
        h("pre", { class: "log", style: "max-height:420px" }, (r.log || []).join("\n") || "（沒有紀錄）"));
      detail.scrollIntoView({ block: "nearest" });
    } catch (error) { fail(error); }
  }
  ctx.view(h("h1", {}, "任務紀錄"), h("p", { class: "muted small" }, "每個任務結束（成功、失敗、停止）都會留一份，最多保留最近 50 筆。失敗原因與模型修正過程都在紀錄裡。"), detail,
    records.length ? h("table", {}, h("thead", {}, h("tr", {}, ["時間", "種類", "章", "結果", "tokens", "紀錄行數", ""].map((t) => h("th", {}, t)))),
      h("tbody", {}, records.map((r) => { const [label, kind] = STATUS[r.status] || [r.status, ""];
        return h("tr", {}, h("td", {}, when(r.started)), h("td", {}, KIND_LABEL[r.kind] || r.kind), h("td", {}, (r.chapters || []).join("、")),
          h("td", {}, badge(label, kind), Array.isArray(r.failed) && r.failed.length ? h("span", { class: "small muted" }, ` 失敗：第 ${r.failed.join("、")} 章`) : ""),
          h("td", {}, (r.tokens || 0).toLocaleString()), h("td", {}, r.log_lines), h("td", {}, h("button", { class: "sm", onclick: () => open(r.id) }, "查看"))); })))
      : h("div", { class: "card muted" }, "還沒有任務紀錄。"));
};
