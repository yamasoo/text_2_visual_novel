/* 素材頁：圖片素材清單與出圖、外觀（AI 草擬後由你確認）、畫風設定。#/book/<id>/assets */
"use strict";

// 進行中的任務追蹤器：換分頁或離開這一頁時一起收掉，遲到的回呼才不會改到已卸下的節點。
const assetTrackers = new Set();

function closeAssetTrackers() {
  for (const item of assetTrackers) item.close();
  assetTrackers.clear();
}

FE.PAGES["book/:id/assets"] = async (ctx) => {
  const bookId = ctx.params.id;
  const rest = ctx.args;
  closeAssetTrackers();
  ctx.signal.addEventListener("abort", closeAssetTrackers, { once: true });
  ctx.crumbs([{ label: "書架", href: "#/" }, { label: bookId, href: `#/book/${bookId}` }, { label: "素材" }]);
  const TAB_KEY = "assets_tab";
  let tab = (rest && rest[0]) || FE.store.get(TAB_KEY, "list") || "list";
  if (!["list", "look", "style"].includes(tab)) tab = "list";
  const base = `/api/books/${bookId}/art`;
  const STATUS = { planned: ["待出圖", ""], generated: ["已出圖，待挑選", "info"], selected: ["已挑選", "info"], processed: ["已處理", "ok"], final: ["定稿", "ok"] };
  const KIND = { bg: "背景", sprite: "立繪", headshot: "頭像" };
  const body = h("div");
  const tabs = h("div", { class: "tabs" });

  function drawTabs() {
    tabs.replaceChildren(...[["list", "素材清單"], ["look", "外觀"], ["style", "畫風與出圖設定"]].map(([k, label]) =>
      h("div", { class: tabClass(k === tab), onclick: () => { tab = k; FE.store.set(TAB_KEY, k); drawTabs(); show(); } }, label)));
  }
  async function show() {
    closeAssetTrackers();
    body.replaceChildren(h("div", { class: "muted" }, "載入中…"));
    try {
      body.replaceChildren(await (tab === "list" ? listPane() : tab === "look" ? lookPane() : stylePane()));
    } catch (error) { body.replaceChildren(h("div", { class: "card bad" }, error.message)); }
  }
  const imgUrl = (path) => `${base}/file?path=${encodeURIComponent(path)}`;

  /* ---------------------------------------------------------------- 素材清單 */
  async function listPane() {
    const data = await api(`${base}/assets`, { signal: ctx.signal });
    const style = await api(`${base}/style`, { signal: ctx.signal });
    const wrap = h("div");
    const progress = h("div", { class: "card", hidden: true });
    let kindFilter = "", statusFilter = "";

    async function startJob(payload) {
      try {
        const job = await api(`${base}/generate`, { body: payload });
        Panel.attach(job.id);
        watch(job.id);
      } catch (error) { fail(error); }
    }
    function watch(jobId) {
      progress.hidden = false;
      let tracker = null;
      const paint = (job) => {
        const running = !job.gone && (job.status === "running" || job.status === "queued");
        const done = job.progress ? job.progress.done : 0;
        const total = job.progress ? job.progress.total : 0;
        const percent = total ? Math.round((100 * done) / total) : (running ? 0 : 100);
        progress.replaceChildren(
          h("div", { class: "row" }, h("strong", {}, running ? "出圖中" : { done: "出圖完成", stopped: "已停止", error: "結束（有錯誤）" }[job.status] || "結束（有錯誤）"), h("span", { class: "grow" }),
            running ? h("button", { class: "sm", onclick: () => { if (tracker) tracker.stop().catch(fail); } }, "■ 停止") : ""),
          h("div", { class: "bar" }, h("i", { style: `width:${percent}%` })),
          h("div", { class: "small" }, running ? job.note || "準備中…" : ""),
          !running && job.result ? h("div", { class: "small" }, `新出圖 ${(job.result.generated || []).length} 項，沒變動 ${(job.result.skipped || []).length} 項` + (Object.keys(job.result.failed || {}).length ? `，失敗 ${Object.keys(job.result.failed).length} 項（看右側面板的紀錄）` : "")) : "");
      };
      closeAssetTrackers();
      tracker = FE.jobs.watch(jobId, { base: "/api/fe-jobs", onupdate: paint, onend: (job) => {
        assetTrackers.delete(tracker);
        paint(job);
        show();
      } });
      assetTrackers.add(tracker);
    }
    async function pick(asset, name) {
      try { await api(`${base}/select`, { body: { asset_id: asset.asset_id, name } }); toast(`${asset.asset_id} 已挑選並處理`); show(); } catch (error) { fail(error); }
    }
    async function setStatus(asset, status) {
      let license = asset.license || "";
      if (status === "final") {
        license = await ask("定稿前請填授權／來源說明（例如：AI 生成，自用）", license || "AI 生成（Anima），自用");
        if (!license) return;
      }
      try { await api(`${base}/status`, { body: { asset_id: asset.asset_id, status, license } }); show(); } catch (error) { fail(error); }
    }

    async function bulkStatus(ids, status, license) {
      let failed = 0;
      for (const id of ids) {
        try { await api(`${base}/status`, { body: { asset_id: id, status, license } }); } catch (error) { failed += 1; }
      }
      toast(failed ? `完成，但有 ${failed} 項失敗` : `已更新 ${ids.length} 項`);
      show();
    }
    async function bulkFinalize() {
      const ids = data.assets.filter((a) => a.status === "processed").map((a) => a.asset_id);
      if (!ids.length) return;
      const license = await ask(`一次定稿 ${ids.length} 項已挑選好的素材。請填授權／來源說明`, "AI 生成（Anima），自用");
      if (license) bulkStatus(ids, "final", license);
    }
    async function bulkUnfinalize() {
      const ids = data.assets.filter((a) => a.status === "final").map((a) => a.asset_id);
      if (ids.length && (await FE.confirm(`把 ${ids.length} 項已定稿的素材全部退回「已處理」？`, { danger: true, ok: "退回" }))) bulkStatus(ids, "processed", undefined);
    }
    async function removeSelected(asset, picked) {
      const names = Array.from(picked);
      if (!names.length) return;
      if (!(await FE.confirm(`確定刪除 ${names.length} 張候選圖？不能復原。`, { danger: true, ok: "刪除" }))) return;
      try {
        const r = await api(`${base}/candidates/delete`, { body: { asset_id: asset.asset_id, names } });
        const deleted = (r.deleted || []).length;
        let message = `已刪除 ${deleted} 張`;
        if ((r.protected || []).length) message += "（已選中的不能刪，請先改選別張）";
        toast(message);
        show();
      } catch (error) { fail(error); }
    }

    function card(asset) {
      const [label, kind] = STATUS[asset.status] || [asset.status, ""];
      const done = ["processed", "final"].includes(asset.status) && asset.file;
      const picked = new Set();
      const entries = [];
      const deleteButton = h("button", { class: "sm", disabled: true, onclick: () => removeSelected(asset, picked) }, "刪除所選（0）");
      const refreshDelete = () => {
        deleteButton.textContent = `刪除所選（${picked.size}）`;
        deleteButton.disabled = picked.size === 0;
      };
      const cands = asset.candidates.map((c) => {
        const box = h("input", { type: "checkbox", disabled: !!c.selected });
        entries.push({ name: c.name, box, locked: !!c.selected });
        box.addEventListener("change", () => {
          if (box.checked) picked.add(c.name);
          else picked.delete(c.name);
          refreshDelete();
        });
        return h("div", { class: "cand" },
          h("img", { src: imgUrl(c.file), loading: "lazy", title: `種子 ${c.seed ?? ""}`, onclick: () => window.open(imgUrl(c.file), "_blank"), style: "width:100%;display:block;border-radius:6px;cursor:zoom-in" }),
          h("div", { class: "row", style: "margin-top:4px;align-items:center" },
            box,
            c.selected ? badge("已選", "ok") : "",
            h("span", { class: "grow" }),
            h("button", { class: "sm", onclick: () => pick(asset, c.name) }, "選這張")));
      });
      function selectAll() {
        for (const entry of entries) {
          if (entry.locked) continue;
          entry.box.checked = true;
          picked.add(entry.name);
        }
        refreshDelete();
      }
      function selectNone() {
        for (const entry of entries) {
          entry.box.checked = false;
          picked.delete(entry.name);
        }
        refreshDelete();
      }
      const toolbar = cands.length ? h("div", { class: "row", style: "margin-top:8px" },
        h("button", { class: "ghost sm", onclick: selectAll }, "全選"),
        h("button", { class: "ghost sm", onclick: selectNone }, "全不選"),
        deleteButton) : "";
      return h("div", { class: "card" },
        h("div", { class: "row" }, h("strong", {}, Object.values(asset.names || {})[0] || asset.asset_id), h("code", { class: "small" }, asset.asset_id), badge(KIND[asset.kind] || asset.kind), badge(label, kind),
          h("span", { class: "muted small" }, asset.kind !== "bg" && !(asset.used_in || []).length ? "角色固定項目" : `${(asset.used_in || []).length} 個場景用到`), h("span", { class: "grow" }),
          h("button", { class: "sm", onclick: () => startJob({ only: [asset.asset_id], regenerate: asset.candidates.length > 0 }) }, asset.candidates.length ? "再出一批" : "出圖"),
          asset.status === "processed" ? h("button", { class: "ghost sm", onclick: () => setStatus(asset, "final") }, "標為定稿") : "",
          asset.status === "final" ? h("button", { class: "ghost sm", onclick: () => setStatus(asset, "processed") }, "取消定稿") : ""),
        done ? h("div", { style: "margin:8px 0;max-width:" + (asset.kind === "bg" ? "480px" : "200px") }, h("img", { src: imgUrl(asset.file) + `&t=${Date.now()}`, style: "width:100%;border-radius:6px;background:repeating-conic-gradient(#ccc 0 25%,#eee 0 50%) 50%/16px 16px" })) : "",
        asset.prompt_en ? h("details", { class: "small" }, h("summary", { class: "muted" }, "提示詞"), h("pre", { class: "log" }, asset.prompt_en + (asset.negative_prompt ? "\n\n負面：" + asset.negative_prompt : ""))) : "",
        toolbar,
        cands.length ? h("div", { style: "display:grid;grid-template-columns:repeat(auto-fill,minmax(" + (asset.kind === "bg" ? "220px" : "130px") + ",1fr));gap:10px;margin-top:8px" }, cands) : "");
    }
    function draw() {
      const shown = data.assets.filter((a) => (!kindFilter || a.kind === kindFilter) && (!statusFilter || a.status === statusFilter));
      const blocks = [];
      let current = null;
      for (const asset of shown) {
        const owner = asset.char_id || "";
        if (owner && owner !== current) {
          blocks.push(h("h3", { style: "margin:18px 0 6px" }, `角色：${owner}`));
        }
        current = owner;
        blocks.push(card(asset));
      }
      list.replaceChildren(...(blocks.length ? blocks : [h("div", { class: "card muted" }, "沒有符合的素材。")]));
    }
    const list = h("div");
    if (!data.ready) {
      wrap.append(h("div", { class: "card" }, h("p", {}, "還沒有素材清單。先標註好章節，再按下面的按鈕，系統會掃描場景列出需要的背景與立繪。"),
        h("button", { onclick: async () => { try { await api(`${base}/refresh`, { method: "POST", body: {} }); show(); } catch (e) { fail(e); } } }, "掃描場景建立清單")));
      return wrap;
    }
    const plannedIds = data.assets.filter((a) => a.status === "planned").map((a) => a.asset_id);
    const processedCount = data.assets.filter((a) => a.status === "processed").length;
    const finalCount = data.assets.filter((a) => a.status === "final").length;
    const counts = Object.entries(data.counts).map(([k, v]) => badge(`${(STATUS[k] || [k])[0]} ${v}`, (STATUS[k] || [])[1]));
    wrap.append(
      h("div", { class: "card" },
        h("div", { class: "row" }, h("strong", {}, `圖片素材 ${data.assets.length} 項`), ...counts, h("span", { class: "grow" }),
          h("button", { class: "ghost sm", onclick: async () => { try { await api(`${base}/refresh`, { method: "POST", body: {} }); toast("已重新掃描"); show(); } catch (e) { fail(e); } } }, "重新掃描場景"),
          h("button", { onclick: () => startJob({}) }, `把待出圖的全部出圖（每項 ${style.candidates} 張）`)),
        h("div", { class: "row", style: "margin-top:8px" },
          h("button", { class: "sm", disabled: !plannedIds.length, title: "只處理還沒出過圖的項目，每項只出 1 張", onclick: () => startJob({ only: plannedIds, candidates: 1 }) }, `未出圖的全部各出 1 張（${plannedIds.length}）`),
          h("button", { class: "sm", disabled: !processedCount, title: "把所有「已處理」的素材一次標為定稿", onclick: bulkFinalize }, `一括定稿（${processedCount}）`),
          h("button", { class: "ghost sm", disabled: !finalCount, title: "把所有「已定稿」的素材退回「已處理」", onclick: bulkUnfinalize }, `全部退回定稿前（${finalCount}）`)),
        h("div", { class: "small muted", style: "margin-top:6px" }, "出圖前建議先到「外觀」分頁確認地點與角色的描述，否則圖會很泛。出圖可隨時按停止，下次會接著做。需要先開著 ComfyUI。"),
        h("div", { class: "row", style: "margin-top:8px" }, "篩選：", select([["", "全部種類"], ["bg", "背景"], ["sprite", "立繪"], ["headshot", "頭像"]], "", (v) => { kindFilter = v; draw(); }),
          select([["", "全部狀態"], ...Object.entries(STATUS).map(([k, v]) => [k, v[0]])], "", (v) => { statusFilter = v; draw(); }))),
      progress, list);
    draw();
    const active = (await api(`/api/books/${bookId}/jobs/active`, { signal: ctx.signal })).job;
    if (active && active.kind === "art" && active.status === "running") watch(active.id);
    return wrap;
  }

  /* ---------------------------------------------------------------- 外觀 */
  async function lookPane() {
    const data = await api(`${base}/appearance`, { signal: ctx.signal });
    const wrap = h("div");
    wrap.append(h("div", { class: "card small muted" }, "每個地點和角色要有「外觀」才畫得準：先按「AI 草擬」（會參考設定卡的「外在」和小說裡的描寫段落），你改到滿意再按「確認」。標籤用英文 Danbooru 標籤，描述用英文短句。沒有依據的地方 AI 會在備註提醒，請自己補。"));

    /* 地點出圖：先把目前的標籤與描述存成已確認，再替這個地點的背景（每個時段一項）出候選圖。 */
    function locationGen(item, getFields, showState) {
      const ids = (item.assets || []).map((a) => a.asset_id);
      const count = h("input", { type: "number", min: 1, max: 8, value: 2, style: "width:56px" });
      const line = h("div", { class: "small muted", style: "margin-top:6px" });
      const button = h("button", { class: "sm" }, "出圖");
      const status = (item.assets || []).map((a) => `${a.time ? a.time + "：" : ""}${a.in_manifest ? `${(STATUS[a.status] || [a.status])[0]}${a.candidates ? `，${a.candidates} 張候選` : ""}` : "尚未加入素材清單"}`).join("；");
      line.textContent = ids.length ? `背景素材：${status}` : "這個地點還沒有背景素材（先按「掃描場景建立清單」）";
      const watch = (jobId) => {
        const paint = (job) => {
          if (job.gone) { button.disabled = false; line.textContent = "出圖結束（找不到任務）"; return; }
          if (job.status === "running" || job.status === "queued") {
            line.textContent = job.note ? `出圖中：${job.note}` : "出圖準備中…";
            return;
          }
          button.disabled = false;
          const failed = Object.keys((job.result && job.result.failed) || {}).length;
          line.textContent = job.status === "done" ? `出圖完成${failed ? `，但有 ${failed} 項失敗（看右側面板）` : ""}，到「素材清單」挑選候選圖` : `出圖結束（${job.status}）`;
        };
        closeAssetTrackers();
        const tracker = FE.jobs.watch(jobId, { base: "/api/fe-jobs", onupdate: paint, onend: (job) => {
          assetTrackers.delete(tracker);
          paint(job);
          if (job.status === "done") toast(`${item.label} 出圖完成，到「素材清單」挑選`);
        } });
        assetTrackers.add(tracker);
      };
      button.addEventListener("click", async () => {
        if (!ids.length) return toast("還沒有背景素材，先到「素材清單」按「掃描場景建立清單」");
        button.disabled = true;
        try {
          const fields = getFields();
          await api(`${base}/appearance/location/${item.id}`, { method: "PUT", body: { ...fields, confirmed: !!(fields.tags || fields.description) } });
          showState(!!(fields.tags || fields.description));
          await api(`${base}/refresh`, { method: "POST", body: {} });
          const job = await api(`${base}/generate`, { body: { only: ids, candidates: Math.max(1, Math.min(8, Number(count.value) || 1)), regenerate: true } });
          Panel.attach(job.id);
          line.textContent = "出圖準備中…";
          watch(job.id);
        } catch (error) { button.disabled = false; fail(error); }
      });
      return h("div", {}, h("div", { class: "row", style: "margin-top:8px;align-items:center;gap:8px" }, h("strong", { class: "small" }, "出圖"), h("span", { class: "small muted" }, "每項張數"), count, button), line);
    }

    function row(kind, item) {
      const art = item.art || {};
      const tags = h("input", { value: art.tags || "", placeholder: "英文標籤，逗號分隔，例如 1girl, short brown hair, apron", style: "width:100%" });
      const desc = h("textarea", { rows: 2, placeholder: "英文描述 2–3 句" }, art.description || "");
      const notes = h("div", { class: "small muted" }, art.notes || "");
      const evidence = h("details", { class: "small", hidden: true });
      const state = h("span", {});
      const showState = (confirmed) => state.replaceChildren(badge(confirmed ? "已確認" : "未確認", confirmed ? "ok" : "warn"));
      showState(!!art.confirmed);
      const save = async (confirmed) => {
        try { await api(`${base}/appearance/${kind}/${item.id}`, { method: "PUT", body: { tags: tags.value, description: desc.value, notes: notes.textContent, confirmed } }); showState(confirmed); toast(confirmed ? `已確認 ${item.label}` : `已存草稿 ${item.label}`); } catch (error) { fail(error); }
      };
      const draft = h("button", { class: "sm", onclick: async (e) => {
        const btn = e.target; btn.disabled = true; btn.textContent = "AI 草擬中…";
        try {
          const body = {}; const ex = Executors.value(); if (ex) body.executor = ex;
          const r = await api(`${base}/appearance/${kind}/${item.id}/draft`, { body });
          tags.value = r.tags; desc.value = r.description; notes.textContent = r.notes || "";
          showState(false);
          if (r.evidence && r.evidence.length) { evidence.hidden = false; evidence.replaceChildren(h("summary", { class: "muted" }, `AI 參考的小說段落（${r.evidence.length}）`), ...r.evidence.map((x) => h("div", {}, `[${x.chapter_id} ¶${x.para_no}] ${x.text}`))); }
          toast("草稿好了，請檢查後按「確認」");
        } catch (error) { fail(error); } finally { btn.disabled = false; btn.textContent = "AI 草擬"; }
      } }, "AI 草擬");
      return h("div", { class: "card" },
        h("div", { class: "row" }, h("strong", {}, item.label), h("code", { class: "small" }, item.id), state, h("span", { class: "muted small" }, kind === "location" ? `${item.scenes} 個場景` : ""), h("span", { class: "grow" }), draft,
          h("button", { class: "ghost sm", onclick: () => save(false) }, "存草稿"), h("button", { class: "sm", onclick: () => save(true) }, "確認")),
        item.card_text ? h("details", { class: "small" }, h("summary", { class: "muted" }, "設定卡「外在」"), h("div", {}, item.card_text)) : "",
        h("div", { style: "margin-top:6px" }, tags), h("div", { style: "margin-top:6px" }, desc), notes, evidence,
        kind === "location" ? locationGen(item, () => ({ tags: tags.value, description: desc.value, notes: notes.textContent }), showState) : "");
    }
    // 角色改用外型卡流程（頭像／立繪、wildcard、畫風庫，見 page_appearance.js）；地點維持原本的標籤＋描述
    wrap.textContent = "";
    wrap.append(h("div", { class: "card small muted" }, "角色：用外型卡（頭像＋立繪）出圖。地點：先「AI 草擬」，改到滿意再按「確認」。標籤用英文 Danbooru 標籤，描述用英文短句。"));
    wrap.append(await AppearanceCards.render(bookId));
    wrap.append(h("h2", {}, `地點（${data.locations.length}）`), ...data.locations.map((x) => row("location", x)));
    if (!data.locations.length && !data.characters.length) wrap.append(h("div", { class: "card muted" }, "還沒有標註好的場景或角色。"));
    return wrap;
  }

  /* ---------------------------------------------------------------- 畫風與出圖設定 */
  async function stylePane() {
    const s = await api(`${base}/style`, { signal: ctx.signal });
    const field = (key, label, hint, big) => {
      const el = big ? h("textarea", { rows: 2 }, s[key] || "") : h("input", { value: s[key] ?? "", style: "width:100%" });
      return { key, el, row: h("div", { style: "margin-bottom:10px" }, h("div", {}, h("strong", {}, label), h("span", { class: "muted small" }, "　" + hint)), el) };
    };
    const fields = [
      field("quality", "品質標籤", "預設 masterpiece, best quality, score_7"),
      field("artist", "畫師／畫風標籤", "留空＝Anima 預設動漫風；想指定畫風可填畫師標籤，例如 @name"),
      field("extra", "全部圖共用的標籤", "例如 watercolor, soft colors"),
      field("negative", "負面提示詞", "預設已含 worst quality, watermark 等"),
      field("unet", "主模型檔名（unet）", "留空＝自動找 ComfyUI 裡的 Anima"),
      field("clip", "文字編碼器（clip）", "留空＝自動找"),
      field("vae", "VAE", "留空＝自動找"),
      field("candidates", "每項出幾張候選圖", "1–16，預設 4"),
    ];
    return h("div", { class: "card" },
      h("p", { class: "small muted" }, "目前沒有指定畫風，用 Anima 的預設動漫風出圖。想換風格時改這裡的畫師／共用標籤即可，已經出過的圖不受影響，重新出圖才會套用。"),
      ...fields.map((f) => f.row),
      h("button", { onclick: async () => {
        const payload = {}; for (const f of fields) payload[f.key] = f.el.value;
        try { await api(`${base}/style`, { method: "PUT", body: payload }); toast("已儲存"); } catch (error) { fail(error); }
      } }, "儲存"));
  }

  ctx.view(h("h1", {}, "素材"), tabs, body);
  drawTabs();
  await show();
};
