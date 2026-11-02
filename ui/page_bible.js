/* 設定集頁：場景（地點）與 BGM 清單，可改中文名與備註。#/book/<id>/bible */
"use strict";

PAGES.bible = async function (bookId) {
  crumbs([{ label: "書架", href: "#/" }, { label: bookId, href: `#/book/${bookId}` }, { label: "設定集" }]);
  const data = await api(`/api/books/${bookId}/bible`);
  const SECTIONS = [["locations", "場景（地點）", "地點 id 會用在場景標頭與背景圖。"], ["bgm", "BGM（背景音樂）", "之後匯出時，每個 id 對應一首曲子。"]];
  const SOURCE = { scenes: "場景用到", registry: "設定集", style: "原型風格" };

  function row(kind, e) {
    const label = h("input", { value: e.label, placeholder: "中文名", style: "width:140px" });
    const note = h("input", { value: e.note, placeholder: "備註", style: "width:100%" });
    const save = async () => { try { await api(`/api/books/${bookId}/bible/${kind}/${e.id}`, { method: "PUT", body: { label: label.value.trim(), note: note.value.trim() } }); toast("已儲存 " + e.id); } catch (error) { fail(error); } };
    const del = async () => {
      if (!confirm(`從設定集刪除 ${e.id}？（場景檔裡的用法不受影響）`)) return;
      try { await api(`/api/books/${bookId}/bible/${kind}/${e.id}`, { method: "DELETE" }); PAGES.bible(bookId); } catch (error) { fail(error); }
    };
    const chapters = e.chapters.map((c) => String(c).replace(/^ch0*/, "")).join("、");
    return h("tr", {}, h("td", {}, h("code", {}, e.id)), h("td", {}, label), h("td", {}, note),
      h("td", {}, e.used ? `${e.count} 次`  : h("span", { class: "muted" }, "未使用"), chapters ? h("div", { class: "small muted" }, "第 " + chapters + " 章") : ""),
      h("td", {}, e.sources.map((s) => badge(SOURCE[s] || s, s === "scenes" ? "info" : ""))),
      h("td", {}, h("button", { class: "sm", onclick: save }, "儲存"), " ", e.sources.includes("registry") ? h("button", { class: "ghost sm", onclick: del }, "刪除") : ""));
  }
  function adder(kind) {
    const id = h("input", { placeholder: "新 id（小寫英文、數字、底線）", style: "width:230px" });
    const label = h("input", { placeholder: "中文名", style: "width:140px" });
    return h("div", { class: "row", style: "margin-top:10px" }, id, label, h("button", { onclick: async () => {
      try { await api(`/api/books/${bookId}/bible/${kind}/${id.value.trim()}`, { method: "PUT", body: { label: label.value.trim() } }); PAGES.bible(bookId); } catch (error) { fail(error); }
    } }, "新增"));
  }
  $("#view").replaceChildren(h("h1", {}, "設定集"),
    ...SECTIONS.map(([kind, title, hint]) => h("div", { class: "card" }, h("h2", { style: "margin-top:0" }, `${title}（${data[kind].length}）`), h("div", { class: "small muted" }, hint),
      data[kind].length ? h("table", {}, h("thead", {}, h("tr", {}, ["id", "中文名", "備註", "使用", "來源", ""].map((t) => h("th", {}, t)))), h("tbody", {}, data[kind].map((e) => row(kind, e)))) : h("p", { class: "muted" }, "（還沒有）"),
      adder(kind))));
};
