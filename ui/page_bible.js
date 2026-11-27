/* 地點與設定集頁：場景（地點）與 BGM 清單，支援改名、備註、書庫同步與合併重複條目。#/book/<id>/bible */
"use strict";

FE.PAGES["book/:id/bible"] = async (ctx) => {
  const bookId = ctx.params.id;
  ctx.crumbs([{ label: "書架", href: "#/" }, { label: bookId, href: `#/book/${bookId}` }, { label: "地點與設定集" }]);
  const data = await FE.api(`/api/books/${bookId}/bible`, { signal: ctx.signal });
  const SECTIONS = [
    ["locations", "場景（地點）", "地點 id 會用在場景標頭、VN 背景圖與漫畫地點卡。勾選 2 筆以上可合併重複條目。"],
    ["bgm", "BGM（背景音樂）", "之後匯出時，每個 id 對應一首曲子。"],
  ];
  const SOURCE = { scenes: "場景用到", registry: "設定集", style: "原型風格" };

  function openMergeModal(kind, selectedEntries) {
    // 預設優先保留「場景用到（使用次數最高）」的那一個 ID
    const sorted = [...selectedEntries].sort((a, b) => (b.count || 0) - (a.count || 0));
    let targetId = sorted[0].id;
    const selectNode = h("select", { style: "width:100%", onchange: (e) => { targetId = e.target.value; } },
      sorted.map((e) => h("option", { value: e.id, selected: e.id === targetId },
        `${e.id}${e.label ? `（${e.label}）` : ""} — 使用 ${e.count || 0} 次`)));
    FE.modal({
      title: `合併 ${selectedEntries.length} 筆${kind === "locations" ? "地點" : " BGM"}`,
      body: h("div", { class: "stack" },
        h("div", { class: "small muted" }, "選擇要保留的 ID（建議保留「場景用到」的那筆）；其餘條目的名稱、備註與外觀描述會自動併入，場景檔引用也會同步改寫。"),
        h("div", {}, h("div", { class: "small muted" }, "保留目標 ID"), selectNode)),
      foot: (close) => [
        h("button", { class: "btn", type: "button", onclick: () => close() }, "取消"),
        h("button", { class: "btn btn-primary", type: "button", onclick: async () => {
          const sources = selectedEntries.map((e) => e.id).filter((id) => id !== targetId);
          close();
          try {
            const r = await FE.api(`/api/books/${bookId}/bible/${kind}/merge`, {
              method: "POST",
              body: { target: targetId, sources },
            });
            toast(`已合併 ${r.merged.length} 筆到 ${r.target}` + (r.scenes_updated ? `（同步改寫 ${r.scenes_updated} 章場景）` : ""), "ok");
            FE.route();
          } catch (error) { fail(error); }
        } }, "確認合併"),
      ],
    });
  }

  function sectionCard(kind, title, hint) {
    const items = data[kind] || [];
    const picked = new Set();
    const mergeBtn = h("button", { class: "sm", disabled: true, onclick: () => {
      const chosen = items.filter((e) => picked.has(e.id));
      if (chosen.length < 2) return;
      openMergeModal(kind, chosen);
    } }, "合併勾選項目");
    const syncMergeBtn = () => {
      mergeBtn.disabled = picked.size < 2;
      mergeBtn.textContent = picked.size >= 2 ? `合併勾選項目（${picked.size} 筆）` : "合併勾選項目";
    };

    const row = (e) => {
      const cb = h("input", { type: "checkbox", onchange: (ev) => {
        if (ev.target.checked) picked.add(e.id); else picked.delete(e.id);
        syncMergeBtn();
      } });
      const label = h("input", { value: e.label, placeholder: e.hint || "顯示名稱", style: "width:160px" });
      const note = h("input", { value: e.note, placeholder: "備註", style: "width:100%" });
      const save = async () => { try { await FE.api(`/api/books/${bookId}/bible/${kind}/${e.id}`, { method: "PUT", body: { label: label.value.trim(), note: note.value.trim() } }); toast("已儲存 " + e.id); } catch (error) { fail(error); } };
      const del = async () => {
        if (!(await FE.confirm(`從設定集刪除 ${e.id}？（場景檔裡的用法不受影響）`, { danger: true, ok: "刪除" }))) return;
        try { await FE.api(`/api/books/${bookId}/bible/${kind}/${e.id}`, { method: "DELETE" }); FE.route(); } catch (error) { fail(error); }
      };
      const chapters = e.chapters.map((c) => String(c).replace(/^ch0*/, "")).join("、");
      return h("tr", {},
        h("td", {}, cb),
        h("td", {}, h("code", {}, e.id)),
        h("td", {}, label),
        h("td", {}, note),
        h("td", {}, e.used ? `${e.count} 次` : h("span", { class: "muted" }, "未使用"), chapters ? h("div", { class: "small muted" }, "第 " + chapters + " 章") : ""),
        h("td", {}, e.sources.map((s) => badge(SOURCE[s] || s, s === "scenes" ? "info" : ""))),
        h("td", { style: "white-space:nowrap" }, h("button", { class: "sm", onclick: save }, "儲存"), " ", e.sources.includes("registry") ? h("button", { class: "ghost sm", onclick: del }, "刪除") : ""));
    };

    const idInput = h("input", { placeholder: "新 id（小寫英文、數字、底線）", style: "width:230px" });
    const nameInput = h("input", { placeholder: "名稱", style: "width:160px" });
    const adder = h("div", { class: "row", style: "margin-top:10px" }, idInput, nameInput, h("button", { onclick: async () => {
      try { await FE.api(`/api/books/${bookId}/bible/${kind}/${idInput.value.trim()}`, { method: "PUT", body: { label: nameInput.value.trim() } }); FE.route(); } catch (error) { fail(error); }
    } }, "新增"));

    return h("div", { class: "card" },
      h("div", { class: "row", style: "align-items:center;justify-content:space-between;margin-bottom:4px" },
        h("h2", { style: "margin:0" }, `${title}（${items.length}）`),
        items.length >= 2 ? mergeBtn : ""),
      h("div", { class: "small muted", style: "margin-bottom:8px" }, hint),
      items.length
        ? h("table", {},
            h("thead", {}, h("tr", {}, ["", "id", "名稱", "備註", "使用", "來源", ""].map((t) => h("th", {}, t)))),
            h("tbody", {}, items.map(row)))
        : h("p", { class: "muted" }, "（還沒有）"),
      adder);
  }

  const syncBar = () => {
    if (!data.has_library) return "";
    const overwriteBox = h("input", { type: "checkbox" });
    const statusOut = h("span", { class: "small muted" });
    const btn = h("button", { class: "sm" }, "從書庫同步地點（bible/world）");
    btn.onclick = async () => {
      btn.disabled = true;
      statusOut.textContent = "同步中…";
      try {
        const r = await FE.api(`/api/books/${bookId}/bible/sync-library`, { method: "POST", body: { overwrite: overwriteBox.checked } });
        let msg = `找到 ${r.found} 個世界觀檔：新增 ${r.created.length}、更新 ${r.updated.length}、跳過 ${r.skipped.length}`;
        if (r.comic_projects_synced && r.comic_projects_synced.length) {
          msg += `；同步漫畫專案 ${r.comic_projects_synced.length} 個`;
        }
        statusOut.textContent = msg;
        toast(msg, r.errors.length ? "bad" : "ok");
        if (r.created.length || r.updated.length) FE.route();
      } catch (error) {
        statusOut.textContent = "✖ " + error.message;
        fail(error);
      } finally {
        btn.disabled = false;
      }
    };
    return h("div", { class: "card row", style: "align-items:center;gap:10px" },
      btn,
      h("label", { class: "row small", style: "align-items:center;gap:4px" }, overwriteBox, "覆蓋既有條目"),
      statusOut);
  };
  const worldContextCard = () => {
    const box = h("textarea", {
      rows: 2,
      placeholder: "例：中世紀歐洲奇幻，麻布／羊毛／革靴／外套為主，不出現現代衣物與拉鍊（留空時自動讀取書庫 story-core.md）",
    }, data.world_context || "");
    const saveBtn = h("button", { class: "sm primary", onclick: async () => {
      saveBtn.disabled = true;
      try {
        await FE.api(`/api/books/${bookId}/bible/world-context`, { method: "PUT", body: { world_context: box.value.trim() } });
        toast("已儲存時代與服飾基調", "ok");
      } catch (error) { fail(error); } finally { saveBtn.disabled = false; }
    } }, "儲存基調");
    const fallbackNote = !data.world_context && data.world_context_fallback
      ? h("details", { style: "margin-top:6px" },
          h("summary", { class: "small muted", style: "cursor:pointer" }, "目前留空，AI 草擬時將自動引用書庫 story-core.md 前段（點此預覽）"),
          h("pre", { class: "log", style: "max-height:140px;margin-top:6px" }, data.world_context_fallback))
      : "";
    return h("div", { class: "card" },
      h("div", { class: "row", style: "align-items:center;justify-content:space-between;margin-bottom:6px" },
        h("h2", { style: "margin:0" }, "本書時代與服飾基調"),
        saveBtn),
      h("div", { class: "small muted", style: "margin-bottom:6px" }, "給 AI 草擬角色外型卡與服裝材質時的共同背景；有填就優先採用，留空則自動讀取書庫故事核心。"),
      box,
      fallbackNote);
  };

  ctx.view(h("h1", {}, "地點與設定集"),
    worldContextCard(),
    syncBar(),
    ...SECTIONS.map(([kind, title, hint]) => sectionCard(kind, title, hint)));
};
