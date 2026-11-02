/* 總覽頁：全書進度與統計。#/book/<id>/overview */
"use strict";

PAGES.overview = async function (bookId) {
  const data = await api(`/api/books/${bookId}/overview`);
  crumbs([{ label: "書架", href: "#/" }, { label: bookId, href: `#/book/${bookId}` }, { label: "總覽" }]);
  const sc = data.status_counts;
  const stat = (label, value, hint) => h("div", { class: "card stat" }, h("div", { class: "small muted" }, label), h("div", { class: "num" }, Number(value).toLocaleString()), hint ? h("div", { class: "small muted" }, hint) : "");
  const pct = data.chapters ? Math.round((100 * (sc.draft + sc.reviewed)) / data.chapters) : 0;
  const reviewedPct = data.chapters ? Math.round((100 * sc.reviewed) / data.chapters) : 0;
  const topSpeakers = data.speakers.slice(0, 12);
  const maxLines = topSpeakers.length ? topSpeakers[0].lines : 1;
  $("#view").replaceChildren(h("h1", {}, "總覽"),
    data.chapters ? "" : h("div", { class: "card" }, "還沒有章節資料。先到「章節」匯入並標註。"),
    h("div", { class: "stats" },
      stat("章數", data.chapters, `${data.chars.toLocaleString()} 字`),
      stat("已標註", sc.draft + sc.reviewed, `${pct}%`),
      stat("已校對", sc.reviewed, `${reviewedPct}%`),
      stat("場數", data.scenes), stat("句數", data.lines, `旁白 ${data.narration} · 台詞 ${data.dialogue} · 指示 ${data.direction}`)),
    h("div", { class: "card" }, h("div", { class: "small muted" }, "進度（綠＝已校對、橘＝待校對）"),
      h("div", { class: "bar stack" }, h("i", { class: "ok", style: `width:${reviewedPct}%` }), h("i", { class: "warn", style: `width:${pct - reviewedPct}%` }))),
    h("h2", {}, "誰說最多話"),
    h("div", { class: "card" }, topSpeakers.length ? topSpeakers.map((s) => h("div", { class: "row small", style: "margin:3px 0" },
      h("span", { style: "width:130px" }, s.id), h("div", { class: "bar grow", style: "margin:0" }, h("i", { style: `width:${Math.round((100 * s.lines) / maxLines)}%` })), h("span", {}, s.lines))) : h("span", { class: "muted" }, "（尚無台詞）")),
    h("h2", {}, "逐章"),
    h("table", {}, h("thead", {}, h("tr", {}, ["章", "章名", "字數", "狀態", "場", "句", "台詞", "旁白"].map((t) => h("th", {}, t)))),
      h("tbody", {}, data.per_chapter.map((c) => { const [label, kind] = STATE_LABEL[c.status] || [c.status, ""];
        return h("tr", {}, h("td", {}, c.chapter_no), h("td", {}, h("a", { href: `#/book/${bookId}/ch/${c.chapter_no}` }, c.title)), h("td", {}, c.char_count.toLocaleString()),
          h("td", {}, badge(label, kind)), h("td", {}, c.scenes || ""), h("td", {}, c.lines || ""), h("td", {}, c.dialogue || ""), h("td", {}, c.narration || "")); }))));
};
