/* 漫畫頁：漫畫專案（vendor/t2c）、章節、分鏡、嵌字、出圖、頁面與輸出。#/book/<id>/comic */
"use strict";

// 進行中的任務追蹤：離開這一頁時一起收掉。
const comicTrackers = new Set();
function closeComicTrackers() {
  for (const item of comicTrackers) item.close();
  comicTrackers.clear();
}

// bible 卡清單（GET …/bible）：每列回 placeholder 與 check_card 的 errors／warnings。
function bibleRows(bible) {
  return [
    ...(((bible && bible.characters) || []).map((row) => ({ row, kind: "角色" }))),
    ...(((bible && bible.locations) || []).map((row) => ({ row, kind: "地點" }))),
  ];
}

function bibleSummary(bible) {
  if (!bible) return "讀不到 bible 卡清單（GET …/bible 沒有回應）";
  const stat = (key, label) => {
    const rows = bible[key] || [];
    const bad = rows.filter((row) => (row.errors || []).length).length;
    const placeholders = rows.filter((row) => row.placeholder).length;
    const drafts = rows.filter((row) => row.draft).length;
    return label + " " + rows.length + " 張：" + (rows.length - bad) + " 張 0 error" +
      (bad ? "、" + bad + " 張有 error" : "") +
      (placeholders ? "（佔位 " + placeholders + "）" : "") +
      (drafts ? "（草稿 " + drafts + "）" : "");
  };
  return stat("characters", "角色") + "　" + stat("locations", "地點");
}

// 漫畫線的 t2c 人工值：標籤與 ui/page_appearance.js 同源，但那是另一個 classic script，這裡照抄不 import。
const COMIC_T2C_SECTIONS = [
  ["head", "頭：髮型髮色、眼型眼色、部位特徵；性別字與年齡一定要在這裡"],
  ["body", "身體：只放體型（slim build、muscular arms）"],
  ["upper", "上半身特徵（非服裝，如傷疤／刺青；無則留空）"],
  ["lower", "下半身特徵（非服裝；無則留空）"],
  ["accessory", "永久配件：眼鏡、髮夾、疤、痣（會隨服裝換的留在服裝）"],
];
const COMIC_T2C_OUTFIT_PARTS = [
  ["top", "上著"],
  ["bottom", "下著"],
  ["set", "整套：制服、圍裙、連身這類"],
];
const COMIC_T2C_HINT = "一行一個英文短語；動作詞（standing）與表情詞不要放進來";

function comicT2cText(items) {
  return Array.isArray(items) ? items.join("\n") : "";
}

function comicT2cLines(text) {
  return String(text || "")
    .split("\n")
    .map((line) => line.trim())
    .filter((line, index, all) => line && all.indexOf(line) === index);
}

// bible 角色列的 t2c 編輯器：第一次展開才打 GET …/art/cards/<id>/check，展開過就重用同一份節點。
function comicT2cEditor(row, opts) {
  const artBase = `/api/books/${opts.bookId}/art`;
  const host = h("div", {});
  const details = h("details", {},
    h("summary", { class: "small" }, "編輯 t2c 人工值（sections ＋ outfits.default）"), host);
  let loaded = false;
  let loading = false;

  const buildEditor = (data) => {
    const card = data.card || {};
    const cardSections = card.sections && typeof card.sections === "object" ? card.sections : {};
    const cardOutfit = (card.outfits && card.outfits.default) || {};
    const draft = data.draft || {};
    const makeInput = (table, key, label) => {
      const input = h("textarea", { rows: 2, style: "width:100%", placeholder: COMIC_T2C_HINT, value: comicT2cText(table[key]) });
      if (window.attachTagComplete) window.attachTagComplete(input, { sep: "\n" });
      return { key, input, node: h("div", { style: "margin-top:6px" }, h("div", { class: "small" }, label), input) };
    };
    const sectionInputs = COMIC_T2C_SECTIONS.map(([key, label]) => makeInput(cardSections, key, label));
    const outfitInputs = COMIC_T2C_OUTFIT_PARTS.map(([key, label]) => makeInput(cardOutfit, key, label));

    // 只放非空的段／部位；指紋（source_sha1）由伺服器處理，不送。
    const toPayload = () => {
      const sections = {};
      for (const item of sectionInputs) {
        const lines = comicT2cLines(item.input.value);
        if (lines.length) sections[item.key] = lines;
      }
      const outfit = {};
      for (const item of outfitInputs) {
        const lines = comicT2cLines(item.input.value);
        if (lines.length) outfit[item.key] = lines;
      }
      if (!Object.keys(sections).length && !Object.keys(outfit).length) return null;
      const t2c = {};
      if (Object.keys(sections).length) t2c.sections = sections;
      if (Object.keys(outfit).length) t2c.outfits = { default: outfit };
      return { t2c };
    };

    const sourceText = data.source === "t2c"
      ? "用外型卡的人工值（t2c）"
      : data.source === "derived"
        ? "還沒有人工值：bible 卡目前是提示詞粗拆"
        : "沒有外型卡：人工值無處可寫";
    const statusLine = h("div", { class: "row wrap" },
      h("span", { class: "small muted" }, sourceText),
      data.stale ? badge("人工值可能過期", "warn") : "");

    const issues = data.issues || [];
    const issuesHost = h("div", {}, issues.length
      ? issues.map((item) => h("div", { class: item.level === "error" ? "small bad" : "small" },
        (item.level === "error" ? "✖ " : "! ") + item.where + "：" + item.message))
      : h("div", { class: "small muted" }, "check_card 沒有問題"));

    const seedButton = h("button", { class: "sm", onclick: async () => {
      const draftSections = draft.sections || {};
      const draftOutfit = (draft.outfits && draft.outfits.default) || {};
      const filled = sectionInputs.some((item) => item.input.value.trim()) ||
        outfitInputs.some((item) => item.input.value.trim());
      if (filled && !(await FE.confirm("會蓋掉目前的內容，要填入粗拆初稿嗎？", { ok: "填入" }))) return;
      for (const item of sectionInputs) item.input.value = comicT2cText(draftSections[item.key]);
      for (const item of outfitInputs) item.input.value = comicT2cText(draftOutfit[item.key]);
      toast("已填入粗拆初稿（還沒存檔）");
    } }, "用提示詞粗拆初稿");

    const saveButton = h("button", { class: "sm primary", onclick: async () => {
      const payload = toPayload();
      if (!payload) { toast("至少要填一段", "warn"); return; }
      try {
        const result = await api(`${opts.base}/${opts.projectId}/bible/${row.id}/t2c`, { method: "PUT", body: payload });
        const bible = result.bible || {};
        const written = (bible.written || []).length;
        const drafts = (bible.drafts || []).length;
        const problems = (bible.issues || []).length;
        toast(`已存檔並重寫 bible：written ${written}／drafts ${drafts}／issues ${problems}`, "ok");
        if (opts.refresh) opts.refresh();
      } catch (error) { fail(error); }
    } }, "儲存");

    return h("div", { style: "padding:6px 0" },
      statusLine,
      h("div", { class: "row wrap" }, seedButton, saveButton),
      sectionInputs.map((item) => item.node),
      h("div", { class: "small muted", style: "margin-top:8px" }, "服裝（outfits.default）"),
      outfitInputs.map((item) => item.node),
      issuesHost);
  };

  async function load() {
    if (loaded || loading) return;
    loading = true;
    host.replaceChildren(h("div", { class: "small muted" }, "載入中…"));
    try {
      const data = await api(`${artBase}/cards/${row.id}/check`);
      loaded = true;
      loading = false;
      host.replaceChildren(buildEditor(data));
    } catch (error) {
      loading = false;
      host.replaceChildren(h("div", { class: "small muted" }, "載入失敗；收合再展開可以重試。"));
      fail(error);
    }
  }

  details.addEventListener("toggle", () => { if (details.open) load(); });
  return details;
}

function bibleCard(bible, opts = {}) {
  if (!bible) return h("div", { class: "card muted" }, "讀不到 bible 卡清單（GET …/bible 沒有回應）。");
  const rows = bibleRows(bible);
  if (!rows.length) return h("div", { class: "card muted" }, "這個專案還沒有 bible 卡；先跑 POST …/bible 產生角色卡與地點卡。");
  const cell = ({ row, kind }) => {
    const errors = row.errors || [];
    const warnings = row.warnings || [];
    const issues = errors.length + warnings.length;
    const editLink = opts.bookId
      ? (kind === "角色"
          ? h("a", { class: "small", style: "margin-left:8px", href: `#/book/${encodeURIComponent(opts.bookId)}/appearance?char=${encodeURIComponent(row.id)}` }, "去角色與外觀 →")
          : h("a", { class: "small", style: "margin-left:8px", href: `#/book/${encodeURIComponent(opts.bookId)}/bible` }, "去地點與設定集 →"))
      : "";
    const statusCell = h("td", {}, h("span", { class: "small muted" }, issues ? "有 " + issues + " 條" : "沒有問題"), editLink);
    // 佔位卡沒有外型卡，t2c 人工值無處可寫：不掛編輯器，只留一行說明。
    if (kind === "角色" && row.placeholder) {
      statusCell.append(h("div", { class: "small muted" }, "佔位卡沒有外型卡，人工值無處可寫"));
    }
    const head = h("tr", {},
      h("td", { style: "white-space:nowrap" }, row.id),
      h("td", { style: "white-space:nowrap" }, kind),
      h("td", {}, h("span", { class: "row wrap" },
        errors.length ? badge(errors.length + " error", "err") : badge("0 error", "ok"),
        warnings.length ? badge(warnings.length + " warn", "warn") : "",
        row.placeholder ? badge("佔位卡", "info") : "",
        row.draft ? badge("草稿", "warn") : "")),
      statusCell);
    const out = [head];
    if (kind === "角色" && !row.placeholder && opts.base && opts.bookId && opts.projectId) {
      out.push(h("tr", {}, h("td", { colspan: "4" }, comicT2cEditor(row, opts))));
    }
    if (!issues) return out;
    out.push(h("tr", {}, h("td", { colspan: "4" },
      h("details", {},
        h("summary", { class: "small" }, "看 " + issues + " 條"),
        errors.map((item) => h("div", { class: "small bad" }, "✖ " + item.where + "：" + item.message)),
        warnings.map((item) => h("div", { class: "small" }, "! " + item.where + "：" + item.message))))));
    return out;
  };
  const hasAnyErr = rows.some(({ row }) => (row.errors || []).length > 0);
  return h("details", { class: "card" },
    h("summary", { style: "cursor:pointer;font-weight:600" },
      (hasAnyErr ? "⚠ " : "") + "本專案角色／地點體檢快照（bible）：" + bibleSummary(bible)),
    h("div", { class: "small muted", style: "margin:8px 0" }, "這裡是共用資源衍生給漫畫引擎的唯讀檢查狀態；要修改外觀與設定建議點右側連結回「共用資源」。"),
    h("table", {},
      h("thead", {}, h("tr", {}, ["卡", "種類", "狀態", "檢查"].map((label) => h("th", {}, label)))),
      h("tbody", {}, rows.flatMap(cell))));
}

FE.PAGES["book/:id/comic"] = async (ctx) => {
  const bookId = ctx.params.id;
  const rest = ctx.args || [];
  closeComicTrackers();
  ctx.signal.addEventListener("abort", closeComicTrackers, { once: true });
  ctx.crumbs([{ label: "書架", href: "#/" }, { label: bookId, href: `#/book/${bookId}` }, { label: "漫畫" }]);

  const STAGES = ["annotate", "plan", "script", "storyboard"];
  const STAGE_LABEL = { annotate: "切對話", plan: "改編規劃", script: "劇本", storyboard: "分鏡" };
  const TAB_KEY = "comic_tab";
  const base = `/api/books/${bookId}/comic`;
  const body = h("div");
  const tabs = h("div", { class: "tabs" });
  let tab = (ctx.query && ctx.query.tab) || rest[0] || FE.store.get(TAB_KEY, "project") || "project";
  if (!["project", "chapter", "storyboard", "letters", "render", "pages"].includes(tab)) tab = "project";
  FE.store.set(TAB_KEY, tab);
  const PROJECT_KEY = `${TAB_KEY}_project_${bookId}`;   // 每本書分開記：A 書的專案 id 不該帶到 B 書
  let projectId = rest[1] || FE.store.get(PROJECT_KEY, "") || "";
  let chapter = Number(FE.store.get(`${TAB_KEY}_chapter`, 1)) || 1;
  let lastWarn = { chapter: 0, warnings: [] };   // 組版的 warnings 要撐過 show() 重畫
  let renderCandidates = "";   // 「候選張數」輸入框留空＝專案設定；值要撐過 show() 重畫
  let renderPage = null;       // 出圖頁目前看的頁碼（null＝自動挑第一頁未選滿、"all"＝整章）
  let renderFilter = "all";    // 出圖頁篩選：all | unpicked | empty
  let renderPageMore = 1;      // 全頁各多出幾張圖
  let boardPage = null;        // 分鏡頁目前看的頁碼（null＝第 1 頁、"all"＝整章）
  let projSubTab = FE.store.get("comic_proj_subtab", "overview") || "overview";   // 專案頁子分頁：overview | style | lettering
  let projComfyLoras = null;   // 專案頁按「抓 LoRA 清單」後快取的 ComfyUI LoRA 名單
  // 嵌字分頁的狀態：panels 是 letters 檔的內容（含沒改的 faces／sfx），拖曳就地改這一份。
  let lettersState = { chapter: 0, page: 1, panels: {}, plan: null, dirty: false };

  function busy(error) {
    if (error && error.code === "busy") {
      toast("ComfyUI 正在跑別的任務，等它結束再一次", "warn");
      return true;
    }
    return false;
  }
  function failIt(error) {
    if (!busy(error)) fail(error);
  }

  function drawTabs() {
    const items = [["project", "專案"], ["chapter", "章節"], ["storyboard", "分鏡"], ["render", "出圖"], ["letters", "嵌字"], ["pages", "頁面與輸出"]];
    tabs.replaceChildren(...items.map(([key, label]) =>
      h("div", { class: tabClass(key === tab), onclick: () => {
        tab = key;
        FE.store.set(TAB_KEY, key);
        FE.go(`#/book/${encodeURIComponent(bookId)}/comic?tab=${key}`);
      } }, label)));
  }

  async function show() {
    closeComicTrackers();
    body.replaceChildren(h("div", { class: "muted" }, "載入中…"));
    try {
      const pane = tab === "project" ? projectPane()
        : tab === "chapter" ? chapterPane()
          : tab === "storyboard" ? storyboardPane()
            : tab === "render" ? renderPane()
              : tab === "pages" ? pagesPane() : lettersPane();
      body.replaceChildren(await pane);
    } catch (error) {
      body.replaceChildren(h("div", { class: "card bad" }, error.message));
    }
  }

  function useProject(id) {
    projectId = id;
    FE.store.set(PROJECT_KEY, id);
    lettersState = { chapter: 0, page: 1, panels: {}, plan: null, dirty: false };
  }

  function forgetProject() {
    projectId = "";
    FE.store.set(PROJECT_KEY, "");
  }

  function projectPicker(projects) {
    if (!projects.length) return h("div", { class: "card muted" }, "這本書還沒有漫畫專案；先在下面建一個。");
    return h("div", { class: "row wrap" }, projects.map((row) =>
      h("button", { class: row.project_id === projectId ? "sm" : "sm ghost", onclick: () => { useProject(row.project_id); show(); } },
        `${row.title || row.project_id}${row.broken ? "（壞掉）" : ""}`)));
  }

  /* ---------------------------------------------------------------- 專案 */
  async function projectPane() {
    const data = await api(base, { signal: ctx.signal });
    const projects = data.projects || [];
    const known = projects.map((row) => row.project_id);
    if (projectId && !known.includes(projectId)) forgetProject();   // 別的書記住的、或被刪掉的：清掉再走，不要留著害整頁掛掉
    if (!projectId && projects.length) useProject(projects[0].project_id);
    const wrap = h("div");
    if (!projects.length) {
      wrap.append(h("div", { class: "card" },
        h("h3", { style: "margin-top:0" }, "這本書還沒有漫畫專案"),
        h("div", { class: "small muted", style: "margin-bottom:10px" }, "按一次就會用本書書名建立預設漫畫專案（main），並自動帶入既有角色外型卡。"),
        h("button", { class: "primary", onclick: async () => {
          try {
            const r = await api(base, { body: {} });
            useProject(r.project_id);
            toast("已啟用漫畫專案（已同步角色卡）", "ok");
            show();
          } catch (error) { failIt(error); }
        } }, "一鍵啟用漫畫線")));
      wrap.append(h("details", { class: "card" },
        h("summary", { style: "cursor:pointer;font-weight:600" }, "進階：自訂專案 ID 與名稱"),
        h("div", { class: "row wrap", style: "margin-top:10px" },
          h("input", { placeholder: "作品名（留空用本書書名）", id: "comic_new_title" }),
          h("input", { placeholder: "project_id（留空自動編號）", id: "comic_new_id" }),
          h("button", { class: "primary", onclick: async () => {
            const id = document.getElementById("comic_new_id").value.trim();
            const title = document.getElementById("comic_new_title").value.trim();
            try {
              const r = await api(base, { body: { project_id: id, title } });
              useProject(r.project_id);
              toast("已建立專案", "ok");
              show();
            } catch (error) { failIt(error); }
          } }, "建立"))));
      return wrap;
    }

    // 精簡頂部列：專案切換＋摺疊新建專案（不再各自佔一張大卡片）
    wrap.append(h("div", { class: "card", style: "padding:12px 16px" },
      h("div", { class: "row wrap", style: "align-items:center;gap:8px" },
        h("strong", { class: "small" }, "專案"),
        projectPicker(projects)),
      h("details", { style: "margin-top:8px" },
        h("summary", { class: "small muted", style: "cursor:pointer" }, "＋ 新建其他漫畫專案（多版本／不同畫風）"),
        h("div", { class: "row wrap", style: "margin-top:8px" },
          h("input", { placeholder: "作品名（留空用本書書名）", id: "comic_new_title" }),
          h("input", { placeholder: "project_id（留空自動編號）", id: "comic_new_id" }),
          h("button", { class: "sm primary", onclick: async () => {
            const id = document.getElementById("comic_new_id").value.trim();
            const title = document.getElementById("comic_new_title").value.trim();
            try {
              const r = await api(base, { body: { project_id: id, title } });
              useProject(r.project_id);
              toast("已建立專案", "ok");
              show();
            } catch (error) { failIt(error); }
          } }, "建立")))));

    if (!projectId) return wrap;
    let info = null;
    try { info = await api(`${base}/${projectId}`, { signal: ctx.signal }); } catch (error) { info = null; }
    if (!info) {
      forgetProject();
      wrap.append(h("div", { class: "card muted" }, `找不到漫畫專案「${projectId}」；上面可以新建一個，或從清單重挑。`));
      return wrap;
    }

    const [bible, settings] = await Promise.all([
      api(`${base}/${projectId}/bible`, { signal: ctx.signal }).catch(() => null),
      api(`${base}/${projectId}/settings`, { signal: ctx.signal }),
    ]);

    const SUBTABS = [
      ["overview", "總覽與章節"],
      ["style", "畫風與 LoRA"],
      ["lettering", "嵌字與字型"],
    ];
    if (!SUBTABS.some(([k]) => k === projSubTab)) projSubTab = "overview";

    // 子分頁 1：總覽與章節（章節表限高固定表頭＋bible 預設收合）
    const rows = (info.chapters || []).map((row) => ({
      chapter: row.chapter || 0,
      source: row.source_chars ? `${row.source_chars} 字` : "沒有",
      plan: row.plan_pages || "沒有",
      script: row.script_pages || "沒有",
      storyboard: row.storyboard_pages || "沒有",
      pages: row.pages_out || 0,
      rendered: row.rendered || 0,
    }));
    const overviewPane = h("div", {},
      h("div", { class: "card" },
        h("div", { class: "row wrap", style: "align-items:baseline;gap:10px" },
          h("h3", { style: "margin:0" }, info.title || projectId),
          h("span", { class: "muted small" }, info.root || "")),
        h("div", { class: "small", style: "margin:6px 0 10px" }, bibleSummary(bible)),
        rows.length
          ? h("div", { class: "scroll-table", style: "max-height:360px" },
              h("table", {},
                h("thead", {}, h("tr", {}, ["章", "原著", "規劃", "劇本", "分鏡", "出圖", "頁面"].map((t) => h("th", {}, t)))),
                h("tbody", {}, rows.map((row) => h("tr", {}, h("td", {}, row.chapter), h("td", {}, row.source),
                  h("td", {}, row.plan), h("td", {}, row.script), h("td", {}, row.storyboard),
                  h("td", {}, row.rendered), h("td", {}, row.pages))))))
          : h("div", { class: "muted" }, "還沒有章節的檔案")),
      bibleCard(bible, { bookId, base, projectId, refresh: show }));

    // 子分頁 2：本專案專屬畫風與 LoRA
    const projState = {
      comfy_profile: (settings.project && settings.project.comfy_profile) || "",
      style_preset: (settings.project && settings.project.style_preset) || "mono",
      style: (settings.project && settings.project.style) || "",
      meta: ((settings.project && settings.project.meta) || []).join(", "),
      look: (settings.project && settings.project.look) || "",
      sfx_mode: (settings.project && settings.project.sfx_mode) || "overlay",
      lora_mode: settings.lora_mode || "append",
      loras: (settings.loras || []).map((x) => ({ name: x.name || "", strength: x.strength ?? 1.0, on: x.on !== false })),
    };
    const stylePresets = settings.style_presets || {};
    const comfyProfiles = settings.comfy_profiles || [];
    const defaultProfLabel = settings.comfy_default_profile ? `跟隨設定頁預設（${settings.comfy_default_profile}）` : "跟隨設定頁預設";
    const presetHint = h("div", { class: "small muted", style: "margin-top:4px" });
    const updatePresetHint = () => {
      const infoP = stylePresets[projState.style_preset];
      presetHint.textContent = infoP ? `預設風格：${infoP.style}` : "";
    };
    updatePresetHint();

    const loraListHost = h("div");
    const loraFetchStatus = h("span", { class: "small muted" });
    const drawProjLoras = () => {
      const known = projComfyLoras || [];
      const bad = known.length
        ? projState.loras.map((x) => (x.name || "").trim()).filter((x) => x && known.indexOf(x) < 0)
        : [];
      const warn = bad.length
        ? h("div", { class: "small", style: "color:var(--bad);margin-top:4px" },
            "⚠ 不在 ComfyUI 清單裡的 LoRA：" + bad.join("、"))
        : "";
      loraListHost.replaceChildren(
        h("datalist", { id: "comic-proj-lora-list" }, known.map((m) => h("option", { value: m }))),
        projState.loras.length
          ? h("table", {},
              h("thead", {}, h("tr", {}, ["啟用", "LoRA 檔名", "強度", ""].map((t) => h("th", {}, t)))),
              h("tbody", {}, projState.loras.map((item, idx) => h("tr", {},
                h("td", { style: "text-align:center" }, h("input", {
                  type: "checkbox", checked: item.on !== false,
                  onchange: (e) => { item.on = Boolean(e.target.checked); },
                })),
                h("td", {}, h("input", {
                  value: item.name || "", list: "comic-proj-lora-list", style: "width:min(320px,100%)",
                  placeholder: "從 ComfyUI 清單選 LoRA 檔名",
                  oninput: (e) => { item.name = e.target.value.trim(); },
                })),
                h("td", {}, h("input", {
                  type: "number", step: "0.05", value: item.strength ?? 1.0, style: "width:80px",
                  oninput: (e) => {
                    const n = Number(e.target.value.trim());
                    if (e.target.value.trim() !== "" && Number.isFinite(n)) item.strength = n;
                  },
                })),
                h("td", {}, h("button", { class: "sm ghost", onclick: () => {
                  projState.loras.splice(idx, 1);
                  drawProjLoras();
                } }, "刪除列"))))))
          : h("div", { class: "small muted" }, "這個專案目前沒有專屬 LoRA（直接使用 ComfyUI profile 的設定）。"),
        warn,
        h("div", { class: "row wrap", style: "margin-top:8px;gap:8px;align-items:center" },
          h("button", { class: "sm", onclick: () => {
            projState.loras.push({ name: "", strength: 1.0, on: true });
            drawProjLoras();
          } }, "新增 LoRA"),
          h("button", { class: "sm ghost", onclick: async () => {
            loraFetchStatus.textContent = "抓清單中…";
            try {
              const m = await api("/api/settings/comfyui/models");
              projComfyLoras = (m && m.lora) || [];
              loraFetchStatus.textContent = `✔ 抓到 ${projComfyLoras.length} 個 LoRA`;
              drawProjLoras();
            } catch (error) {
              loraFetchStatus.textContent = "✖ " + error.message;
            }
          } }, "抓 ComfyUI LoRA 清單"),
          loraFetchStatus));
    };
    drawProjLoras();

    const stylePane = h("div", {},
      h("div", { class: "card" },
        h("h3", { style: "margin-top:0" }, "本專案畫風與出圖 Profile（寫入 project.yaml）"),
        h("div", { class: "row wrap", style: "gap:12px;align-items:center;margin-bottom:8px" },
          h("label", { class: "small" }, "出圖 Profile ",
            h("select", { onchange: (e) => { projState.comfy_profile = e.target.value; } },
              [["", defaultProfLabel]].concat(comfyProfiles.map((p) => [p, p]))
                .map(([v, label]) => h("option", { value: v, selected: projState.comfy_profile === v }, label)))),
          h("label", { class: "small" }, "畫風預設集 ",
            h("select", { onchange: (e) => { projState.style_preset = e.target.value; updatePresetHint(); } },
              (Object.keys(stylePresets).length ? Object.keys(stylePresets) : ["mono", "color", "american"])
                .map((k) => h("option", { value: k, selected: projState.style_preset === k },
                  stylePresets[k] ? `${stylePresets[k].name}（${k}）` : k)))),
          h("label", { class: "small" }, "狀聲詞繪製 ",
            h("select", { onchange: (e) => { projState.sfx_mode = e.target.value; } },
              [["overlay", "嵌字程式疊上（可在嵌字頁移動縮放）"], ["moss", "交給生圖模型直接畫"]]
                .map(([v, label]) => h("option", { value: v, selected: projState.sfx_mode === v }, label))))),
        presetHint,
        h("details", { style: "margin-top:10px", open: (projState.style || projState.meta || projState.look) ? true : undefined },
          h("summary", { class: "small", style: "cursor:pointer" }, "進階：自訂畫風覆蓋（留空＝沿用上面的畫風預設集）"),
          h("div", { style: "margin-top:8px" },
            h("div", { class: "small muted" }, "生圖畫風英文描述（style）"),
            (() => {
              const ta = h("textarea", { rows: 2, style: "width:100%", placeholder: "留空＝沿用畫風預設集",
                oninput: (e) => { projState.style = e.target.value; } }, projState.style);
              if (window.attachTagComplete) window.attachTagComplete(ta);
              return ta;
            })(),
            h("div", { class: "small muted", style: "margin-top:6px" }, "畫風標籤（meta，逗號分隔）"),
            (() => {
              const inp = h("input", { value: projState.meta, style: "width:100%", placeholder: "例如：manga panel, full color, watercolor",
                oninput: (e) => { projState.meta = e.target.value; } });
              if (window.attachTagComplete) window.attachTagComplete(inp);
              return inp;
            })(),
            h("div", { class: "small muted", style: "margin-top:6px" }, "給劇本／分鏡 AI 看的畫風描述（look）"),
            h("input", { value: projState.look, style: "width:100%", placeholder: "留空＝沿用畫風預設集",
              oninput: (e) => { projState.look = e.target.value; } })))),
      h("div", { class: "card" },
        h("h3", { style: "margin-top:0" }, "本專案專屬 LoRA（寫入 settings.json）"),
        h("div", { class: "row wrap", style: "gap:10px;align-items:center;margin-bottom:8px" },
          h("label", { class: "small" }, "與全域 Profile LoRA 的關係 ",
            h("select", { onchange: (e) => { projState.lora_mode = e.target.value; } },
              [["append", "疊加在全域 Profile LoRA 後面（預設）"], ["replace", "取代全域 Profile LoRA（只用本專案這份）"]]
                .map(([v, label]) => h("option", { value: v, selected: projState.lora_mode === v }, label))))),
        loraListHost,
        h("div", { class: "row wrap", style: "margin-top:12px;gap:10px;align-items:center" },
          h("button", { class: "primary", onclick: async () => {
            try {
              await api(`${base}/${projectId}/settings`, {
                method: "PUT",
                body: {
                  project: {
                    comfy_profile: projState.comfy_profile,
                    style_preset: projState.style_preset,
                    style: projState.style,
                    meta: projState.meta,
                    look: projState.look,
                    sfx_mode: projState.sfx_mode,
                  },
                  loras: projState.loras,
                  lora_mode: projState.lora_mode,
                },
              });
              toast("本專案畫風與 LoRA 已存", "ok");
              await show();
            } catch (error) { failIt(error); }
          } }, "儲存畫風與 LoRA"),
          h("span", { class: "small muted" }, "變更畫風或 LoRA 後，已出過圖的章節需重新出圖才會套用。"))));

    // 子分頁 3：嵌字與字型
    const lettering = settings.lettering || {};
    const fontRow = (key, label) => h("div", { class: "row wrap", style: "margin-bottom:6px" },
      h("span", { class: "small", style: "width:72px" }, label),
      h("input", { id: `comic_font_${key}`, value: lettering[key] || "", style: "flex:1;min-width:260px",
        placeholder: "留空＝自動找（Noto CJK／思源／正黑體）" }));
    const letteringPane = h("div", {},
      h("div", { class: "card" }, h("h3", { style: "margin-top:0" }, "漫畫設定（嵌字）"),
        h("div", { class: "small muted", style: "margin-bottom:4px" }, "字型填完整路徑（TTC 可用「檔名#索引」）；字型檔不進 git，只記路徑。可變字型（例如 NotoSansTC-VF.ttf）的字重由程式依角色套上。"),
        h("div", { class: "small", style: "margin-bottom:10px" }, "可隨成品發佈：Noto Sans TC、Noto Serif TC（SIL OFL 1.1）。不可發佈：微軟正黑體 msjh／msjhbd、細明體 mingliu（Windows 內建，只供自己看）。"),
        fontRow("dialogue_font", "對白"), fontRow("shout_font", "喊叫"),
        fontRow("narration_font", "旁白"), fontRow("sfx_font", "狀聲詞"),
        h("div", { class: "row wrap", style: "margin-top:10px" },
          h("label", {}, h("input", { type: "checkbox", id: "comic_vertical", checked: lettering.vertical !== false }), " 直排"),
          h("span", { class: "small" }, "對白字級"),
          h("input", { type: "number", id: "comic_dialogue_pt", min: 4, max: 48, step: 0.5, value: lettering.dialogue_pt || 9, style: "width:80px" }),
          h("span", { class: "small" }, "旁白字級"),
          h("input", { type: "number", id: "comic_narration_pt", min: 4, max: 48, step: 0.5, value: lettering.narration_pt || 8.5, style: "width:80px" }),
          h("button", { class: "primary", onclick: () => saveSettings() }, "儲存字型設定")),
        (settings.problems || []).length
          ? h("div", { class: "small", style: "margin-top:8px" }, settings.problems.map((text) => h("div", {}, text))) : ""));

    const subPanes = { overview: overviewPane, style: stylePane, lettering: letteringPane };
    const subBtns = {};
    const selectSub = (key) => {
      projSubTab = key;
      FE.store.set("comic_proj_subtab", key);
      for (const [k] of SUBTABS) {
        subPanes[k].hidden = k !== key;
        subBtns[k].className = tabClass(k === key);
      }
    };
    const subBar = h("div", { class: "tabs", style: "margin-bottom:10px" }, SUBTABS.map(([k, label]) => {
      const btn = h("div", { class: tabClass(k === projSubTab), onclick: () => selectSub(k) }, label);
      subBtns[k] = btn;
      return btn;
    }));
    selectSub(projSubTab);
    wrap.append(subBar, overviewPane, stylePane, letteringPane);
    return wrap;
  }

  /* ---------------------------------------------------------------- 章節 */
  function storyboardDoc(payload) {
    // /storyboard 回 {storyboard: {path, storyboard: {...}}}（沒有分鏡時內層是 null）。
    const inner = payload && payload.storyboard;
    return (inner && inner.storyboard) || inner || {};
  }

  async function chapterPane() {
    if (!projectId) return h("div", { class: "card muted" }, "先在「專案」分頁選一個專案。");
    const wrap = h("div");
    const picker = h("div", { class: "row wrap" },
      h("label", {}, "章號 "),
      h("input", { type: "number", min: 1, value: chapter, style: "width:80px", onchange: (event) => {
        chapter = Number(event.target.value) || 1;
        FE.store.set(`${TAB_KEY}_chapter`, chapter);
        show();
      } }));
    wrap.append(h("div", { class: "card" }, h("h3", {}, "章節"), picker,
      h("div", { class: "row wrap" },
        h("button", { onclick: async () => {
          try {
            const out = await api(`${base}/${projectId}/chapter/${chapter}/source`, { body: { from_book: true } });
            toast(`已寫入 ${out.paragraphs} 段`, "ok");
            show();
          } catch (error) { failIt(error); }
        } }, "從這本書的章節寫入原著"),
        h("button", { class: "sm", onclick: () => runJob(STAGES) }, "跑四個文字階段"),
        h("button", { class: "sm ghost", title: "覆蓋已存在的切對話／規劃／劇本／分鏡",
          onclick: () => runJob(STAGES, true) }, "重跑四個文字階段（覆蓋）"))));

    const board = await api(`${base}/${projectId}/chapter/${chapter}/storyboard`, { signal: ctx.signal });
    const pages = (storyboardDoc(board).pages) || [];
    wrap.append(pages.length
      ? h("div", { class: "card muted" }, `這一章已有分鏡：${pages.length} 頁、${pages.reduce((sum, page) => sum + (page.panels || []).length, 0)} 格。到「分鏡」分頁看內容。`)
      : h("div", { class: "card muted" }, "這一章還沒有分鏡；上面按「跑四個文字階段」（切對話 → 改編規劃 → 劇本 → 分鏡）。"));
    return wrap;
  }

  /* ---------------------------------------------------------------- 分鏡（唯讀看板） */
  const SHOT_LABEL = {
    extreme_close: "大特寫", close: "特寫", medium: "中景",
    full: "全身", wide: "遠景", extreme_wide: "大遠景",
  };

  function boardPanel(panel, row, image) {
    const rawPick = row && row.pick;
    const pick = typeof rawPick === "string" ? rawPick : (rawPick && (rawPick.image || rawPick.file)) || "";
    const bubbles = (panel.bubbles || []).map((bubble) => {
      const kind = LETTER_KIND_LABEL[(bubble && bubble.kind) || ""] || (bubble && bubble.kind) || "對白";
      const who = bubble && bubble.speaker ? `${bubble.speaker}：` : "";
      return `${kind}　${who}${(bubble && bubble.text) || ""}`;
    });
    const speeches = bubbles.length ? bubbles : ((row && row.lines) || []);
    const sfx = (panel.sfx || []).map((item) => (item && item.text) || "").filter(Boolean);
    return h("div", { class: "card", style: "margin:0" },
      h("div", { class: "row" },
        h("strong", {}, panel.id || ""),
        panel.shot ? h("span", { class: "chip chip-info" }, SHOT_LABEL[panel.shot] || panel.shot) : "",
        panel.angle ? h("span", { class: "chip" }, panel.angle) : "",
        h("span", { class: "grow" }),
        h("span", { class: "small muted" }, pick ? "已選圖" : "未選圖")),
      pick
        ? h("img", { src: image(pick, "render"), title: pick, style: "width:100%;max-height:220px;object-fit:contain;background:var(--bg);border-radius:8px;margin:8px 0" })
        : h("div", { class: "small muted", style: "margin:8px 0" }, "尚未選圖（到「出圖」分頁挑一張）"),
      panel.scene ? h("div", { class: "small", style: "margin-bottom:6px" }, panel.scene) : "",
      (panel.characters || []).length
        ? h("div", { class: "small" }, panel.characters.map((character) =>
          h("div", {}, `${character.id}${character.pose ? `　${character.pose}` : ""}${character.expression ? `（${character.expression}）` : ""}`)))
        : "",
      speeches.length
        ? h("div", { class: "small", style: "margin-top:6px" }, speeches.map((text) => h("div", {}, text)))
        : h("div", { class: "small muted", style: "margin-top:6px" }, "這一格沒有台詞"),
      sfx.length ? h("div", { class: "small muted", style: "margin-top:4px" }, `狀聲詞：${sfx.join("、")}`) : "",
      h("details", { style: "margin-top:8px" }, h("summary", { class: "small" }, "看這一格 JSON"),
        h("pre", { class: "log", style: "max-height:220px" }, JSON.stringify(panel, null, 2))));
  }

  async function storyboardPane() {
    if (!projectId) return h("div", { class: "card muted" }, "先在「專案」分頁選一個專案。");
    const image = (path, kind) => `${base}/${projectId}/chapter/${chapter}/image?kind=${kind}&path=${encodeURIComponent(path)}`;
    const payload = await api(`${base}/${projectId}/chapter/${chapter}/storyboard`, { signal: ctx.signal });
    const doc = storyboardDoc(payload);
    const pages = doc.pages || [];
    if (!pages.length) {
      return h("div", { class: "card muted" }, "這一章還沒有分鏡；先到「章節」分頁跑「切對話 → 改編規劃 → 劇本 → 分鏡」。");
    }
    const rows = {};
    let panelsBroken = false;
    try {
      const panelPayload = await api(`${base}/${projectId}/chapter/${chapter}/panels`, { signal: ctx.signal });
      for (const row of panelPayload.panels || []) rows[row.id] = row;
    } catch (error) { panelsBroken = true; }   // 選圖狀態讀不到不擋看板，只是沒有縮圖
    const total = pages.reduce((sum, page) => sum + (page.panels || []).length, 0);
    const pickedOf = (panel) => {
      const r = rows[panel.id];
      const raw = r && r.pick;
      return typeof raw === "string" ? raw : (raw && (raw.image || raw.file)) || "";
    };
    const pageNums = pages.map((p) => p.page);
    if (boardPage !== "all" && !pageNums.includes(boardPage)) {
      boardPage = pageNums[0];
    }

    const wrap = h("div");
    wrap.append(h("div", { class: "card" }, h("h3", {}, "分鏡（唯讀）"),
      h("div", { class: "small muted" }, `第 ${chapter} 章 · ${pages.length} 頁 · ${total} 格。台詞以分鏡檔為準；在「嵌字」分頁改過的文字請看嵌字頁。`),
      panelsBroken ? h("div", { class: "small muted" }, "選圖狀態讀不到，縮圖先不顯示。") : "",
      h("details", { style: "margin-top:8px" }, h("summary", { class: "small" }, "看整章 JSON"),
        h("pre", { class: "log", style: "max-height:360px" }, JSON.stringify(doc, null, 2)))));

    const pageBarHost = h("div", { class: "row wrap", style: "gap:6px;margin-bottom:8px;align-items:center" });
    const navBarHost = h("div", { class: "row wrap", style: "gap:8px;margin-bottom:10px;align-items:center" });
    const pagesHost = h("div");

    const drawBoard = () => {
      pageBarHost.replaceChildren(
        ...pages.map((page) => {
          const panels = page.panels || [];
          const doneCount = panelsBroken ? 0 : panels.filter((pl) => Boolean(pickedOf(pl))).length;
          const full = !panelsBroken && panels.length > 0 && doneCount === panels.length;
          const active = boardPage === page.page;
          const label = panelsBroken
            ? `第 ${page.page} 頁（${panels.length} 格）`
            : `第 ${page.page} 頁 ${doneCount}/${panels.length}${full ? " ✓" : ""}`;
          return h("button", {
            class: active ? "sm primary" : "sm",
            style: !active && full ? "border-color:var(--ok);color:var(--ok)" : "",
            onclick: () => { boardPage = page.page; drawBoard(); },
          }, label);
        }),
        h("button", {
          class: boardPage === "all" ? "sm primary" : "sm ghost",
          onclick: () => { boardPage = "all"; drawBoard(); },
        }, `全部（${total} 格）`)
      );

      if (boardPage !== "all") {
        const idx = pageNums.indexOf(boardPage);
        navBarHost.style.display = "";
        navBarHost.replaceChildren(
          h("button", { class: "sm ghost", disabled: idx <= 0,
            onclick: () => { boardPage = pageNums[idx - 1]; drawBoard(); } }, "← 上一頁"),
          h("button", { class: "sm ghost", disabled: idx < 0 || idx >= pageNums.length - 1,
            onclick: () => { boardPage = pageNums[idx + 1]; drawBoard(); } }, "下一頁 →"),
          h("span", { class: "small muted" }, `目前第 ${idx + 1} / ${pageNums.length} 頁`)
        );
      } else {
        navBarHost.style.display = "none";
        navBarHost.replaceChildren();
      }

      const shown = boardPage === "all" ? pages : pages.filter((p) => p.page === boardPage);
      pagesHost.replaceChildren(...shown.map((page) => {
        const panels = page.panels || [];
        return h("div", { class: "card", style: "margin-top:0" },
          h("h3", { style: "margin-top:0" }, `第 ${page.page} 頁`),
          h("div", { class: "small muted", style: "margin-bottom:8px" }, `${page.template || "（沒有模板）"}　${panels.length} 格`),
          h("div", { class: "grid" }, panels.map((panel) => boardPanel(panel, rows[panel.id], image))));
      }));
    };

    drawBoard();
    wrap.append(h("div", { class: "card" }, pageBarHost, navBarHost, pagesHost));
    return wrap;
  }

  /* ---------------------------------------------------------------- 出圖 */
  async function renderPane() {
    if (!projectId) return h("div", { class: "card muted" }, "先在「專案」分頁選一個專案。");
    const wrap = h("div");
    const image = (path, kind) => `${base}/${projectId}/chapter/${chapter}/image?kind=${kind}&path=${encodeURIComponent(path)}`;
    const status = await api(`${base}/${projectId}/chapter/${chapter}`, { signal: ctx.signal });
    const candidateInput = h("input", { id: "comic_render_candidates", type: "number", min: "1", max: "12",
      step: "1", value: renderCandidates, placeholder: "專案設定", style: "width:7em",
      title: "每格要畫幾張候選；留空＝專案設定（project.yaml 的 candidates）",
      oninput: (event) => { renderCandidates = event.target.value; } });
    const canCompile = Boolean(status.storyboard_pages);
    const rows = canCompile ? ((await api(`${base}/${projectId}/chapter/${chapter}/panels`, { signal: ctx.signal })).panels || []) : [];
    const pickable = rows.filter((row) => {
      const pickedFile = row.pick && typeof row.pick === "object" ? (row.pick.image || "") : (row.pick || "");
      return !pickedFile && (row.candidates || []).length > 0;
    });
    const autoPickBtn = h("button", {
      disabled: !pickable.length,
      title: "把還沒選圖、但已經有候選圖的格子，全部先選第 1 張（之後隨時可換）",
      onclick: async () => {
        if (!pickable.length) return;
        autoPickBtn.disabled = true;
        let ok = 0;
        try {
          for (const row of pickable) {
            const first = (row.candidates || [])[0];
            const file = (first && first.file) || String(first || "");
            if (!file) continue;
            await api(`${base}/${projectId}/chapter/${chapter}/pick`, { body: { panel: row.id, image: file } });
            ok += 1;
          }
          toast(`已替 ${ok} 格先選第 1 張`, "ok");
          show();
        } catch (error) {
          failIt(error);
          if (ok) show();
        } finally {
          autoPickBtn.disabled = false;
        }
      },
    }, pickable.length ? `未選圖先選第 1 張（${pickable.length} 格）` : "未選圖先選第 1 張");
    wrap.append(h("div", { class: "card" }, h("h3", {}, `第 ${chapter} 章 · 出圖`),
      h("div", { class: "row wrap" },
        h("button", { class: "primary", onclick: () => runRender({}, "整章") }, "出圖（ComfyUI）"),
        autoPickBtn,
        h("label", { class: "small muted" }, "候選張數 ", candidateInput)),
      h("div", { class: "small muted" }, "出完在下面每格挑一張；組版與下載在「頁面與輸出」分頁。")));
    if (!canCompile) {
      wrap.append(h("div", { class: "card muted" }, "這一章還沒有分鏡；先到「章節」分頁跑完四個文字階段。"));
    } else if (!rows.length) {
      wrap.append(h("div", { class: "card muted" }, "分鏡還沒有可編譯的格子。"));
    } else {
      const pickedOf = (r) => (r.pick && typeof r.pick === "object" ? (r.pick.image || "") : (r.pick || ""));
      const pageNums = [...new Set(rows.map((r) => r.page))].sort((a, b) => a - b);
      if (renderPage !== "all" && !pageNums.includes(renderPage)) {
        const firstIncomplete = pageNums.find((p) => rows.some((r) => r.page === p && !pickedOf(r)));
        renderPage = firstIncomplete !== undefined ? firstIncomplete : pageNums[0];
      }

      const pageBarHost = h("div", { class: "row wrap", style: "gap:6px;margin-bottom:8px" });
      const filterBarHost = h("div", { class: "row wrap", style: "gap:8px;align-items:center;margin-bottom:10px" });
      const gridHost = h("div", { style: "display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:12px" });

      const drawPanels = () => {
        pageBarHost.replaceChildren(
          ...pageNums.map((p) => {
            const pRows = rows.filter((r) => r.page === p);
            const doneCount = pRows.filter((r) => Boolean(pickedOf(r))).length;
            const full = doneCount === pRows.length;
            const active = renderPage === p;
            return h("button", {
              class: active ? "sm primary" : "sm",
              style: !active && full ? "border-color:var(--ok);color:var(--ok)" : "",
              onclick: () => { renderPage = p; drawPanels(); },
            }, `第 ${p} 頁 ${doneCount}/${pRows.length}${full ? " ✓" : ""}`);
          }),
          h("button", {
            class: renderPage === "all" ? "sm primary" : "sm ghost",
            onclick: () => { renderPage = "all"; drawPanels(); },
          }, `全部（${rows.length} 格）`)
        );

        const moreInput = h("input", {
          type: "number", min: "1", max: "8", step: "1", value: String(renderPageMore),
          style: "width:4.5em",
          oninput: (e) => { const n = Number(e.target.value); if (n >= 1 && n <= 8) renderPageMore = n; },
        });
        const pageActions = renderPage !== "all"
          ? [
              h("span", { class: "grow" }),
              h("button", { class: "sm", onclick: () => runRender({ page: renderPage }, `只補第 ${renderPage} 頁`) }, `補第 ${renderPage} 頁`),
              h("label", { class: "row small", style: "gap:4px;align-items:center" }, "全頁多出", moreInput, "張"),
              h("button", { class: "sm primary", onclick: () => runRender({ page: renderPage, more: renderPageMore }, `第 ${renderPage} 頁各加畫 ${renderPageMore} 張`) },
                `第 ${renderPage} 頁各加畫 ${renderPageMore} 張`),
            ]
          : [];
        moreInput.addEventListener("input", () => {
          const btn = filterBarHost.querySelector("[data-page-more-btn]");
          if (btn) btn.textContent = `第 ${renderPage} 頁各加畫 ${renderPageMore} 張`;
        });
        if (pageActions.length) pageActions[pageActions.length - 1].setAttribute("data-page-more-btn", "1");

        filterBarHost.replaceChildren(
          ...[["all", "全部"], ["unpicked", "只看未選圖"], ["empty", "只看缺圖（0 候選）"]].map(([k, label]) =>
            h("button", { class: renderFilter === k ? "sm primary" : "sm ghost", onclick: () => { renderFilter = k; drawPanels(); } }, label)),
          ...pageActions
        );

        const visible = rows.filter((r) => {
          if (renderPage !== "all" && r.page !== renderPage) return false;
          if (renderFilter === "unpicked") return !pickedOf(r);
          if (renderFilter === "empty") return !(r.candidates || []).length;
          return true;
        });

        if (!visible.length) {
          gridHost.replaceChildren(h("div", { class: "muted small" }, "這一頁沒有符合篩選條件的格子。"));
          return;
        }
        gridHost.replaceChildren(...visible.map((row) => {
          const candidates = row.candidates || [];
          const pickedFile = pickedOf(row);   // t2c 的 pick 是 {image, focus, zoom}
          return h("div", { class: "card", style: "margin:0" },
            h("div", { class: "row" }, h("strong", {}, row.id || ""), h("span", { class: "grow" }),
              h("span", { class: "small muted" },
                `第 ${row.page} 頁　${pickedFile ? "已選" : `${candidates.length} 張候選`}`)),
            h("div", { class: "row wrap", style: "margin:6px 0" },
              h("button", { class: "sm", onclick: () => runRender({ panel: row.id, page: row.page }, `只補 ${row.id}`) }, "補這一格"),
              h("button", { class: "sm", onclick: () => runRender({ panel: row.id, page: row.page, more: 1 }, `加畫 ${row.id}`) }, "加畫一張"),
              h("button", { class: "sm ghost", onclick: () => runRender({ page: row.page }, `只補第 ${row.page} 頁`) }, "補這一頁")),
            h("div", { class: "row wrap", style: "gap:6px" }, candidates.map((candidate) => {
              const file = (candidate && candidate.file) || String(candidate);   // /panels 給的是 {file, seed, ...}
              const picked = pickedFile === file;
              return h("div", { style: "position:relative" },
                h("img", { src: image(file, "render"), loading: "lazy",
                  style: "width:110px;height:110px;object-fit:cover;border-radius:6px;cursor:pointer" + (picked ? ";outline:3px solid var(--accent)" : ""),
                  title: file,
                  onclick: async () => {
                    try {
                      await api(`${base}/${projectId}/chapter/${chapter}/pick`, { body: { panel: row.id, image: file } });
                      toast("已選這張", "ok");
                      show();
                    } catch (error) { failIt(error); }
                  } }),
                h("button", { class: "sm ghost", disabled: picked,
                  style: "position:absolute;top:0;right:0;padding:0 5px;line-height:1.5;background:rgba(0,0,0,0.55);color:#fff",
                  title: picked ? "已選中的那張不能刪；先改選別張" : "刪掉這張候選圖",
                  onclick: async (event) => {
                    event.stopPropagation();
                    if (picked) return;
                    if (!(await FE.confirm(`刪掉 ${file}？不能復原。`, { danger: true, ok: "刪除" }))) return;
                    try {
                      await api(`${base}/${projectId}/chapter/${chapter}/renders/delete`, { body: { names: [file] } });
                      toast("已刪除 1 張", "ok");
                      show();
                    } catch (error) { failIt(error); }
                  } }, "✕"));
            })));
        }));
      };

      drawPanels();
      wrap.append(h("div", { class: "card" }, pageBarHost, filterBarHost, gridHost));
    }


    return wrap;
  }

  /* ---------------------------------------------------------------- 頁面與輸出 */
  async function pagesPane() {
    if (!projectId) return h("div", { class: "card muted" }, "先在「專案」分頁選一個專案。");
    const wrap = h("div");
    const image = (path, kind) => `${base}/${projectId}/chapter/${chapter}/image?kind=${kind}&path=${encodeURIComponent(path)}`;
    const status = await api(`${base}/${projectId}/chapter/${chapter}`, { signal: ctx.signal });
    const pagesOut = Number(status.pages_out) || 0;
    wrap.append(h("div", { class: "card" }, h("h3", {}, `第 ${chapter} 章 · 輸出`),
      h("div", { class: "row wrap" },
        h("button", { class: "sm", onclick: () => runCompose() }, "組版（產生頁面）"),
        ...[["pdf", "下載 PDF"], ["cbz", "下載 CBZ"], ["zip", "下載 PNG（ZIP）"]].map(([kind, label]) => (pagesOut > 0
          ? h("a", { class: "btn sm", href: `${base}/${projectId}/chapter/${chapter}/file?kind=${kind}`, target: "_blank" }, label)
          : h("button", { class: "sm", disabled: true, title: "先按「組版（產生頁面）」" }, label)))),
      pagesOut > 0 ? "" : h("div", { class: "small muted" }, "還沒有頁面圖：先到「出圖」分頁出圖，回來按「組版（產生頁面）」，才能下載 PDF／CBZ／PNG 壓縮檔。")));
    const pages = await api(`${base}/${projectId}/chapter/${chapter}/pages`, { signal: ctx.signal });
    const files = pages.files || [];
    wrap.append(h("div", { class: "card" }, h("h3", {}, "頁面"),
      files.length ? h("div", { class: "row wrap" }, files.map((file) =>
        h("img", { src: image(file, "page"), style: "width:160px", title: file })))
        : h("div", { class: "muted" }, "還沒有頁面圖。")));
    if (lastWarn.chapter === chapter && lastWarn.warnings.length) {
      const shown = lastWarn.warnings.slice(0, 30);
      wrap.append(h("div", { class: "card" }, h("h3", {}, `組版提醒（${lastWarn.warnings.length} 條）`),
        h("div", { class: "small" }, shown.map((text) => h("div", {}, text))),
        lastWarn.warnings.length > shown.length
          ? h("div", { class: "small muted" }, `還有 ${lastWarn.warnings.length - shown.length} 條`) : ""));
    }
    return wrap;
  }
  /* ---------------------------------------------------------------- 嵌字（N5） */

  const LETTER_KIND_LABEL = { speech: "對白", thought: "內心", shout: "喊叫", whisper: "耳語", radio: "廣播", narration: "旁白" };

  async function lettersPane() {
    if (!projectId) return h("div", { class: "card muted" }, "先在「專案」分頁選一個專案。");
    if (lettersState.chapter !== chapter) {
      lettersState = { chapter: chapter, page: 1, panels: {}, plan: null, dirty: false };
    }
    const board = await api(`${base}/${projectId}/chapter/${chapter}/storyboard`, { signal: ctx.signal });
    const boardPages = (storyboardDoc(board).pages) || [];
    if (!boardPages.length) {
      return h("div", { class: "card muted" }, "這一章還沒有分鏡；先跑「切對話 → 改編規劃 → 劇本 → 分鏡」。");
    }
    const pageNos = boardPages.map((page) => Number(page.page)).filter((number) => number > 0);
    if (!pageNos.includes(lettersState.page)) lettersState.page = pageNos[0];

    const saved = await api(`${base}/${projectId}/chapter/${chapter}/letters`, { signal: ctx.signal });
    lettersState.panels = saved.panels || {};     // 伺服器是真相；拖曳就地改這一份
    const planOut = await api(`${base}/${projectId}/chapter/${chapter}/letters/plan?page=${lettersState.page}`, { signal: ctx.signal });
    const plan = (planOut.pages || [])[0] || null;
    lettersState.plan = plan;
    const pageFiles = ((await api(`${base}/${projectId}/chapter/${chapter}/pages`, { signal: ctx.signal })).files) || [];

    const imageSrc = (path, kind) => `${base}/${projectId}/chapter/${chapter}/image?kind=${kind}&path=${encodeURIComponent(path)}&t=${Date.now()}`;
    const size = (plan && plan.size) || [1000, 1414];
    const pct = (value, total) => `${(value / total) * 100}%`;
    const round4 = (value) => Math.round(value * 10000) / 10000;
    const clamp01 = (value) => Math.min(1, Math.max(0, value));
    const status = h("span", { class: "small muted" });
    function markDirty() {
      lettersState.dirty = true;
      status.textContent = "有未存的變更（按「存檔並重組版」才寫檔）";
    }
    function boxStyle(rect, extra) {
      return `position:absolute;left:${pct(rect[0], size[0])};top:${pct(rect[1], size[1])};`
        + `width:${pct(rect[2] - rect[0], size[0])};height:${pct(rect[3] - rect[1], size[1])};${extra || ""}`;
    }
    function findBubble(panelId, index) {
      const entry = lettersState.panels[panelId];
      const bubbles = entry && Array.isArray(entry.bubbles) ? entry.bubbles : null;
      const item = bubbles && bubbles[index];
      return item && typeof item === "object" ? item : null;
    }
    function ensureBubble(panelId, index) {
      const panels = lettersState.panels || (lettersState.panels = {});
      if (!panels[panelId]) panels[panelId] = {};
      const entry = panels[panelId];
      if (!Array.isArray(entry.bubbles)) entry.bubbles = [];
      while (entry.bubbles.length <= index) entry.bubbles.push({});
      if (!entry.bubbles[index]) entry.bubbles[index] = {};
      return entry.bubbles[index];
    }
    function placeBubble(node, panel, bubble, center) {
      node.style.left = pct(center[0] - bubble.rx, size[0]);
      node.style.top = pct(center[1] - bubble.ry, size[1]);
      node.style.width = pct(bubble.rx * 2, size[0]);
      node.style.height = pct(bubble.ry * 2, size[1]);
    }
    function findSfx(panelId, index) {
      const entry = lettersState.panels[panelId];
      const list = entry && Array.isArray(entry.sfx) ? entry.sfx : null;
      const item = list && list[index];
      return item && typeof item === "object" ? item : null;
    }
    function ensureSfx(panelId, index) {
      const panels = lettersState.panels || (lettersState.panels = {});
      if (!panels[panelId]) panels[panelId] = {};
      const entry = panels[panelId];
      if (!Array.isArray(entry.sfx)) entry.sfx = [];
      while (entry.sfx.length <= index) entry.sfx.push({});
      if (!entry.sfx[index]) entry.sfx[index] = {};
      return entry.sfx[index];
    }
    function placeBox(node, rect) {
      node.style.left = pct(rect[0], size[0]);
      node.style.top = pct(rect[1], size[1]);
      node.style.width = pct(rect[2] - rect[0], size[0]);
      node.style.height = pct(rect[3] - rect[1], size[1]);
    }
    function startSfxDrag(event, mode, panel, sfx, node) {
      event.preventDefault();
      event.stopPropagation();
      const bounds = canvas.getBoundingClientRect();
      const pw = panel.rect[2] - panel.rect[0];
      const ph = panel.rect[3] - panel.rect[1];
      const startBox = [sfx.box[0], sfx.box[1], sfx.box[2], sfx.box[3]];
      const bw = startBox[2] - startBox[0];
      const bh = startBox[3] - startBox[1];
      const startPx = ((event.clientX - bounds.left) / bounds.width) * size[0];
      const startPy = ((event.clientY - bounds.top) / bounds.height) * size[1];
      const minW = Math.max(20, pw * 0.04);
      const minH = Math.max(20, ph * 0.04);
      const move = (moveEvent) => {
        const px = ((moveEvent.clientX - bounds.left) / bounds.width) * size[0];
        const py = ((moveEvent.clientY - bounds.top) / bounds.height) * size[1];
        let next;
        if (mode === "resize") {
          const x1 = Math.min(panel.rect[2], Math.max(startBox[0] + minW, startBox[2] + (px - startPx)));
          const y1 = Math.min(panel.rect[3], Math.max(startBox[1] + minH, startBox[3] + (py - startPy)));
          next = [startBox[0], startBox[1], x1, y1];
        } else {
          const dx = px - startPx;
          const dy = py - startPy;
          const x0 = Math.min(panel.rect[2] - bw, Math.max(panel.rect[0], startBox[0] + dx));
          const y0 = Math.min(panel.rect[3] - bh, Math.max(panel.rect[1], startBox[1] + dy));
          next = [x0, y0, x0 + bw, y0 + bh];
        }
        sfx.box = next;
        placeBox(node, next);
        const override = ensureSfx(panel.id, sfx.index);
        override.box = [
          round4(clamp01((next[0] - panel.rect[0]) / pw)),
          round4(clamp01((next[1] - panel.rect[1]) / ph)),
          round4(clamp01((next[2] - panel.rect[0]) / pw)),
          round4(clamp01((next[3] - panel.rect[1]) / ph)),
        ];
        markDirty();
      };
      const stop = () => {
        window.removeEventListener("pointermove", move);
        window.removeEventListener("pointerup", stop);
      };
      window.addEventListener("pointermove", move);
      window.addEventListener("pointerup", stop);
    }
    function startDrag(event, field, panel, bubble, node) {
      event.preventDefault();
      event.stopPropagation();
      const bounds = canvas.getBoundingClientRect();
      const width = panel.rect[2] - panel.rect[0];
      const height = panel.rect[3] - panel.rect[1];
      const move = (moveEvent) => {
        const px = ((moveEvent.clientX - bounds.left) / bounds.width) * size[0];
        const py = ((moveEvent.clientY - bounds.top) / bounds.height) * size[1];
        const override = ensureBubble(panel.id, bubble.index);
        if (field === "tail") {
          const rel = [round4(clamp01((px - panel.rect[0]) / width)), round4(clamp01((py - panel.rect[1]) / height))];
          override.tail = rel;
          node.style.left = pct(panel.rect[0] + rel[0] * width, size[0]);
          node.style.top = pct(panel.rect[1] + rel[1] * height, size[1]);
        } else {
          const minX = Math.min(0.5, bubble.rx / width);
          const minY = Math.min(0.5, bubble.ry / height);
          const rel = [round4(Math.min(1 - minX, Math.max(minX, (px - panel.rect[0]) / width))),
            round4(Math.min(1 - minY, Math.max(minY, (py - panel.rect[1]) / height)))];
          override.center = rel;
          placeBubble(node, panel, bubble, [panel.rect[0] + rel[0] * width, panel.rect[1] + rel[1] * height]);
        }
        markDirty();
      };
      const stop = () => {
        window.removeEventListener("pointermove", move);
        window.removeEventListener("pointerup", stop);
      };
      window.addEventListener("pointermove", move);
      window.addEventListener("pointerup", stop);
    }

    const canvas = h("div", { id: "comic_letters_canvas",
      style: `position:relative;width:100%;max-width:1080px;aspect-ratio:${size[0]} / ${size[1]};`
        + "background:#1c1c1c;border:1px solid var(--line);user-select:none;touch-action:none;overflow:hidden" });
    const pageName = `p${String(lettersState.page).padStart(3, "0")}.png`;
    const pageFile = pageFiles.find((name) => name === pageName);
    canvas.append(pageFile
      ? h("img", { src: imageSrc(pageFile, "page"), alt: "", draggable: false,
        style: "position:absolute;inset:0;width:100%;height:100%;object-fit:fill;pointer-events:none" })
      : h("div", { class: "small muted", style: "position:absolute;left:8px;top:8px" },
        "這一頁還沒有組版圖；框的位置仍可調，存檔會自動重組版。"));

    if (plan) {
      for (const panel of plan.panels) {
        canvas.append(h("div", { style: boxStyle(panel.rect, "border:1px dashed rgba(120,200,255,.55);pointer-events:none") }));
        canvas.append(h("div", { style: `position:absolute;left:${pct(panel.rect[0], size[0])};top:${pct(panel.rect[1], size[1])};`
          + "color:#8cf;font-size:11px;pointer-events:none" }, panel.id));
        for (const face of panel.faces || []) {
          canvas.append(h("div", { title: "臉框（唯讀）",
            style: boxStyle(face, "border:1px dashed rgba(120,255,170,.55);pointer-events:none") }));
        }
        for (const sfx of panel.sfx || []) {
          const box = sfx.box;
          const sfxNode = h("div", { title: `狀聲詞：${sfx.text || ""}（拖框身移動，拖右下角縮放）`,
            style: boxStyle(box, "border:2px dashed rgba(255,210,120,.9);background:rgba(255,190,70,.16);"
              + "cursor:grab;touch-action:none;display:flex;align-items:center;justify-content:center;"
              + "color:#ffd278;font-size:12px;font-weight:600;border-radius:6px") }, sfx.text || "");
          sfxNode.onpointerdown = (event) => startSfxDrag(event, "move", panel, sfx, sfxNode);
          const sfxHandle = h("div", { title: "拖我縮放狀聲詞框",
            style: "position:absolute;right:-4px;bottom:-4px;width:14px;height:14px;border-radius:3px;"
              + "background:#ffd278;border:1px solid #333;cursor:nwse-resize;touch-action:none" });
          sfxHandle.onpointerdown = (event) => startSfxDrag(event, "resize", panel, sfx, sfxNode);
          sfxNode.append(sfxHandle);
          canvas.append(sfxNode);
        }
        for (const bubble of panel.bubbles) {
          const node = h("div", { title: `${LETTER_KIND_LABEL[bubble.kind] || bubble.kind}${bubble.speaker ? "・" + bubble.speaker : ""}（拖我改位置）`,
            style: "position:absolute;border:2px solid rgba(255,255,255,.75);border-radius:8px;background:rgba(0,0,0,.35);"
              + "cursor:grab;touch-action:none;display:flex;align-items:center;justify-content:center;color:#fff;"
              + "font-size:11px;text-align:center;overflow:hidden" },
          `${LETTER_KIND_LABEL[bubble.kind] || bubble.kind}${bubble.order_issue ? " ⚠" : ""}`);
          placeBubble(node, panel, bubble, bubble.center);
          node.onpointerdown = (event) => startDrag(event, "center", panel, bubble, node);
          canvas.append(node);
          if (bubble.tail) {
            const handle = h("div", { title: "拖我改尾巴",
              style: "position:absolute;width:16px;height:16px;margin:-8px 0 0 -8px;border-radius:50%;"
                + "background:#ffd166;border:1px solid #333;cursor:crosshair;touch-action:none" });
            handle.style.left = pct(bubble.tail[0], size[0]);
            handle.style.top = pct(bubble.tail[1], size[1]);
            handle.onpointerdown = (event) => startDrag(event, "tail", panel, bubble, handle);
            canvas.append(handle);
          }
        }
      }
    }

    const list = h("div", { class: "card" }, h("h3", {}, "對話框與狀聲詞"),
      h("div", { class: "small muted" },
        "對話框拖框身改位置、拖黃點改尾巴；狀聲詞拖框身移動、拖右下角縮放。字級 0.5–2.0、行數 1–40；留空＝自動。faces 只顯示，不改。"));
    if (plan) {
      for (const panel of plan.panels) {
        list.append(h("div", { style: "border-top:1px solid var(--line);padding:10px 0" },
          h("div", { class: "row wrap" }, h("strong", {}, panel.id),
            h("span", { class: "small muted" }, `第 ${lettersState.page} 頁第 ${panel.slot} 格`))));
        for (const bubble of panel.bubbles) {
          const over = findBubble(panel.id, bubble.index) || {};
          const text = h("textarea", { rows: 2, style: "width:100%;min-height:3em", placeholder: bubble.text || "（這框沒有台詞）" });
          text.value = typeof over.text === "string" ? over.text : (bubble.text || "");
          text.onchange = () => {
            const target = ensureBubble(panel.id, bubble.index);
            if (text.value === (bubble.text || "")) delete target.text;
            else target.text = text.value;
            markDirty();
          };
          const scale = h("input", { type: "number", min: "0.5", max: "2", step: "0.1", style: "width:6em", placeholder: "自動" });
          scale.value = typeof over.scale === "number" ? over.scale : "";
          scale.onchange = () => {
            const target = ensureBubble(panel.id, bubble.index);
            const value = Number(scale.value);
            if (scale.value === "") delete target.scale;
            else if (value >= 0.5 && value <= 2) target.scale = value;
            else { scale.value = ""; delete target.scale; }
            markDirty();
          };
          const rows = h("input", { type: "number", min: "1", max: "40", step: "1", style: "width:6em", placeholder: "自動" });
          rows.value = typeof over.rows === "number" ? over.rows : "";
          rows.onchange = () => {
            const target = ensureBubble(panel.id, bubble.index);
            const value = Number(rows.value);
            if (rows.value === "") delete target.rows;
            else if (Number.isInteger(value) && value >= 1 && value <= 40) target.rows = value;
            else { rows.value = ""; delete target.rows; }
            markDirty();
          };
          list.append(h("div", { style: "padding:6px 0 2px" },
            h("div", { class: "row wrap" },
              h("span", { class: "small" }, `${bubble.index + 1}. ${LETTER_KIND_LABEL[bubble.kind] || bubble.kind}`),
              bubble.speaker ? h("span", { class: "small muted" }, bubble.speaker) : "",
              h("span", { class: "grow" }),
              h("span", { class: "small muted" }, bubble.auto_center ? "位置：自動" : "位置：手動"),
              bubble.order_issue ? h("span", { class: "small", style: "color:#e6b450" }, `會先讀到第 ${bubble.order_issue} 框`) : ""),
            text,
            h("div", { class: "row wrap" },
              h("label", { class: "small" }, "字級 ", scale),
              h("label", { class: "small" }, "行數 ", rows),
              h("button", { class: "sm ghost", onclick: () => {
                const target = ensureBubble(panel.id, bubble.index);
                delete target.text; delete target.center; delete target.tail; delete target.scale; delete target.rows;
                text.value = bubble.text || ""; scale.value = ""; rows.value = "";
                markDirty();
              } }, "重設這個框"),
              h("button", { class: "sm ghost", onclick: () => {
                delete ensureBubble(panel.id, bubble.index).tail;
                markDirty();
              } }, "尾巴回自動"))));
        }
        for (const sfx of panel.sfx || []) {
          const over = findSfx(panel.id, sfx.index) || {};
          const angle = h("input", { type: "number", min: "-45", max: "45", step: "1", style: "width:6em", placeholder: "-8" });
          angle.value = typeof over.angle === "number" ? over.angle : "";
          angle.onchange = () => {
            const target = ensureSfx(panel.id, sfx.index);
            const value = Number(angle.value);
            if (angle.value === "") delete target.angle;
            else if (Number.isFinite(value) && value >= -45 && value <= 45) target.angle = value;
            else { angle.value = ""; delete target.angle; }
            markDirty();
          };
          const dir = h("select", { onchange: () => {
            const target = ensureSfx(panel.id, sfx.index);
            if (dir.value === "") delete target.vertical;
            else target.vertical = dir.value === "vertical";
            markDirty();
          } },
            [["", "自動"], ["vertical", "直排"], ["horizontal", "橫排"]].map(([v, label]) =>
              h("option", { value: v, selected: (typeof over.vertical === "boolean" ? (over.vertical ? "vertical" : "horizontal") : "") === v }, label)));
          list.append(h("div", { style: "padding:6px 0 2px;border-top:1px dashed var(--line)" },
            h("div", { class: "row wrap", style: "gap:8px;align-items:center" },
              h("span", { class: "small", style: "color:var(--warn);font-weight:600" }, `狀聲詞 ${sfx.index + 1}. ${sfx.text || ""}`),
              h("label", { class: "small" }, "角度 ", angle, "°"),
              h("label", { class: "small" }, "方向 ", dir),
              h("button", { class: "sm ghost", onclick: () => {
                const target = ensureSfx(panel.id, sfx.index);
                delete target.box; delete target.angle; delete target.vertical;
                angle.value = ""; dir.value = "";
                markDirty();
              } }, "重設這個狀聲詞"))));
        }
      }
    }

    const pageSelect = h("select", { value: String(lettersState.page), title: "選一頁來調框", onchange: (event) => {
      const next = Number(event.target.value) || pageNos[0];
      if (next === lettersState.page) return;
      if (lettersState.dirty && !window.confirm("還沒存檔；換頁會丟掉這次的變更。要換頁嗎？")) {
        event.target.value = String(lettersState.page);
        return;
      }
      lettersState.page = next;
      lettersState.dirty = false;
      show();
    } }, pageNos.map((number) => h("option", { value: String(number) }, `第 ${number} 頁`)));

    async function saveLetters() {
      try {
        const out = await api(`${base}/${projectId}/chapter/${chapter}/letters`,
          { method: "PUT", body: { panels: lettersState.panels } });
        lettersState.panels = out.panels || {};
        lettersState.dirty = false;
        toast("嵌字已存", "ok");
        const composed = await api(`${base}/${projectId}/chapter/${chapter}/pages`, { body: {} });
        lastWarn = { chapter: chapter, warnings: (composed && composed.warnings) || [] };
        toast(`重組版：${((composed && composed.pages) || []).length} 頁`, "ok");
        await show();
      } catch (error) { failIt(error); }
    }

    const wrap = h("div");
    wrap.append(h("div", { class: "card" }, h("h3", {}, "嵌字"),
      h("div", { class: "row wrap" },
        h("label", {}, "章號 "),
        h("input", { type: "number", min: 1, value: chapter, style: "width:80px", onchange: (event) => {
          const next = Number(event.target.value) || 1;
          if (next === chapter) return;
          chapter = next;
          FE.store.set(`${TAB_KEY}_chapter`, chapter);
          lettersState = { chapter: chapter, page: 1, panels: {}, plan: null, dirty: false };
          show();
        } }),
        h("label", { class: "small" }, "頁 ", pageSelect),
        status,
        h("button", { class: "primary", onclick: saveLetters }, "存檔並重組版"),
        h("button", { class: "sm ghost", onclick: () => { lettersState.dirty = false; show(); } }, "重新載入")),
      h("div", { class: "small muted" }, "改的是 letters/chNNN.json；存檔後會重組版，頁面圖才是新字。")));
    wrap.append(h("div", { class: "card" }, canvas), list);
    if (lastWarn.chapter === chapter && lastWarn.warnings.length) {
      const shown = lastWarn.warnings.slice(0, 30);
      wrap.append(h("div", { class: "card" }, h("h3", {}, `組版提醒（${lastWarn.warnings.length} 條）`),
        h("div", { class: "small" }, shown.map((message) => h("div", {}, message))),
        lastWarn.warnings.length > shown.length
          ? h("div", { class: "small muted" }, `還有 ${lastWarn.warnings.length - shown.length} 條`) : ""));
    }
    return wrap;
  }

  async function runJob(stages, force) {
    try {
      const job = await api(`${base}/${projectId}/job`, { body: { chapters: [chapter], stages, force: Boolean(force) } });
      toast(`${force ? "重跑" : "開始"}：${stages.map((s) => STAGE_LABEL[s] || s).join("、")}`, "ok");
      if (typeof Panel !== "undefined" && Panel.attach) Panel.attach(job.id);
    } catch (error) { failIt(error); }
  }

  /* 出圖：body 可以只帶 page（補這一頁）或 panel＋page（補這一格）；候選張數留空就交給專案設定。 */
  async function runRender(body, label) {
    try {
      const payload = { ...body };
      const candidates = Number(renderCandidates);
      if (!payload.more && Number.isInteger(candidates) && candidates > 0) payload.candidates = candidates;
      const job = await api(`${base}/${projectId}/chapter/${chapter}/render`, { body: payload });
      toast(`已送 ComfyUI 出圖：${label || "整章"}`, "ok");
      if (typeof Panel !== "undefined" && Panel.attach) Panel.attach(job.id);
    } catch (error) { failIt(error); }
  }

  /* 組版＋嵌字：出圖之後要先有頁面圖，file?kind=pdf 才拿得到東西（F6）。 */
  async function runCompose() {
    try {
      const out = await api(`${base}/${projectId}/chapter/${chapter}/pages`, { body: {} });
      const pages = (out && out.pages) || [];
      const warnings = (out && out.warnings) || [];
      lastWarn = { chapter: chapter, warnings: warnings };
      toast(`組版完成：${pages.length} 頁`, "ok");
      if (warnings.length) toast(`組版有 ${warnings.length} 條提醒（列在下方）`, "warn");
      await show();
    } catch (error) { failIt(error); }
  }

  /* 字型等 t2c lettering 設定：存進專案自己的 settings.json（D12），不動 vendored default。 */
  async function saveSettings() {
    const value = (id) => { const node = document.getElementById(id); return node ? node.value : ""; };
    try {
      const out = await api(`${base}/${projectId}/settings`, { method: "PUT", body: { lettering: {
        dialogue_font: value("comic_font_dialogue_font"),
        shout_font: value("comic_font_shout_font"),
        narration_font: value("comic_font_narration_font"),
        sfx_font: value("comic_font_sfx_font"),
        vertical: Boolean(document.getElementById("comic_vertical") && document.getElementById("comic_vertical").checked),
        dialogue_pt: Number(value("comic_dialogue_pt")) || 9,
        narration_pt: Number(value("comic_narration_pt")) || 8.5,
      } } });
      toast("漫畫設定已存", "ok");
      const problems = out.problems || [];
      if (problems.length) toast(problems[0], "warn");
      await show();
    } catch (error) { failIt(error); }
  }

  ctx.view(h("h1", {}, "漫畫"), tabs, body);
  drawTabs();
  await show();
};
