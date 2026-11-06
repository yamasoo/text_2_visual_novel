"use strict";
/* 視覺小說工作台：單檔 vanilla JS，不需要建置。資料一律用 textContent 寫入（不拼 innerHTML）。 */

/* ---------------------------------------------------------------- 書架與匯入 */

FE.PAGES.home = async (ctx) => {
  ctx.crumbs([{ label: "書架" }]);
  const [state, data] = await Promise.all([
    FE.api("/api/state", { signal: ctx.signal }),
    FE.api("/api/books", { signal: ctx.signal }),
  ]);
  const shelf = h("div", { class: "shelf" }, data.books.map((b) => {
    const pct = b.chapters ? Math.round((100 * b.reviewed) / b.chapters) : 0;
    return h("a", { class: "card book", href: `#/book/${b.book_id}` },
      h("div", { class: "row" }, h("strong", { class: "grow" }, b.display_name), badge(b.source_kind === "library" ? "書庫" : "匯入", "info")),
      h("div", { class: "muted small" }, `${b.book_id} · ${b.chapters} 章 · 已標註 ${b.annotated} · 已校對 ${b.reviewed}`),
      h("div", { class: "bar" }, h("i", { style: `width:${pct}%` })));
  }));
  ctx.view(h("h1", {}, "書架"), data.books.length ? shelf : h("p", { class: "muted" }, "還沒有書。從下面匯入第一本。"),
    importPanel(data, state));
};

function importPanel(data, state) {
  const box = h("div", { class: "card" });
  const tabs = [["library", "從書庫（novel-studio）"], ["file", "TXT／EPUB／MD／DOCX"], ["paste", "貼上文字"]];
  let active = "library";
  const body = h("div", {});
  const tabBar = h("div", { class: "tabs" });
  function draw() {
    tabBar.replaceChildren(...tabs.map(([k, label]) => h("div", { class: tabClass(k === active), onclick: () => { active = k; draw(); } }, label)));
    body.replaceChildren(active === "library" ? libraryTab(data, state) : fileTab(active === "paste"));
  }
  box.append(h("h2", { style: "margin-top:0" }, "匯入新書"), tabBar, body);
  draw();
  return box;
}

function libraryTab(data, state) {
  if (!state.library_exists) return h("p", { class: "muted" }, `找不到書庫資料夾：${state.library}（請檢查 config.yaml 的 book_library.path）`);
  if (!data.library.length) return h("p", { class: "muted" }, `書庫 ${state.library} 裡沒有尚未匯入的書。書庫是唯讀的，只會讀取，不會修改。`);
  return h("table", {}, h("tbody", {}, data.library.map((entry) => h("tr", {},
    h("td", {}, entry.dir_name), h("td", { class: "muted" }, `${entry.chapters} 個章節檔`),
    h("td", {}, h("button", { class: "primary sm", onclick: () => importLibrary(entry) }, "匯入…"))))));
}

async function importLibrary(entry) {
  const bookId = await ask("書的 ID（小寫英文、數字、底線，例如 sample_book）：");
  if (!bookId) return;
  try {
    await FE.api("/api/import/library", { body: { dir_name: entry.dir_name, book_id: bookId, display_name: entry.dir_name } });
    toast("匯入完成");
    FE.go("#/book/" + encodeURIComponent(bookId));
  } catch (error) { fail(error); }
}

function fileTab(pasteOnly) {
  const form = { files: [], paths: [], text: "", opts: { lang: "zh-TW", convert: false, mode: "auto", numbering: "auto" } };
  const result = h("div", {});
  const names = h("div", { class: "muted small" });
  const fileInput = h("input", { type: "file", multiple: true, accept: ".txt,.md,.markdown,.docx,.epub", onchange: async (e) => {
    form.files = [];
    for (const f of e.target.files) form.files.push({ name: f.name, data: await fileToBase64(f) });
    showNames();
  } });
  const pathInput = h("input", { placeholder: "本機檔案路徑，例如 C:\\Users\\me\\Documents\\book.epub", style: "width:min(520px,100%)" });
  const textInput = h("textarea", { rows: 8, placeholder: "把整本或幾章的文字貼在這裡（章節標題用「第一章」「第1章」「Chapter 1」等寫法會自動拆章）", oninput: (e) => { form.text = e.target.value; } });
  function showNames() { names.textContent = [...form.files.map((f) => f.name), ...form.paths].join("、"); }
  const source = pasteOnly ? [textInput] : [
    h("div", { class: "row" }, h("span", { class: "muted" }, "選擇檔案："), fileInput),
    h("div", { class: "row", style: "margin-top:6px" }, h("span", { class: "muted" }, "或本機路徑："), pathInput,
      h("button", { onclick: () => { if (pathInput.value.trim()) { form.paths.push(pathInput.value.trim()); pathInput.value = ""; showNames(); } } }, "加入"),
      h("button", { onclick: () => browseFiles((p) => { form.paths.push(p); showNames(); }) }, "瀏覽…")), names];
  const opts = h("div", { class: "row", style: "margin:10px 0" },
    h("label", {}, "語言 ", select([["zh-TW", "繁體中文"], ["en", "English"], ["ja", "日本語"]], "zh-TW", (v) => { form.opts.lang = v; })),
    h("label", {}, "拆章方式 ", select([["auto", "自動"], ["chapter", "只認「第X章」"], ["markdown", "只認 # 標題"], ["spine", "EPUB：每個檔一章"]], "auto", (v) => { form.opts.mode = v; })),
    h("label", {}, "編號 ", select([["auto", "沿用標題（不連續就重編）"], ["sequential", "依順序重編"]], "auto", (v) => { form.opts.numbering = v; })),
    h("label", {}, h("input", { type: "checkbox", onchange: (e) => { form.opts.convert = e.target.checked; } }), " 簡體轉台灣繁體"),
    h("label", {}, h("input", { type: "checkbox", onchange: (e) => { form.opts.preface_as_chapter = e.target.checked; } }), " 開頭的文字當一章"),
    h("button", { class: "primary", onclick: preview }, "預覽拆章"));
  async function preview() {
    try {
      const data = await api("/api/import/preview", { body: { files: form.files, paths: form.paths, text: form.text, opts: form.opts } });
      result.replaceChildren(previewTable(data, form));
    } catch (error) { fail(error); }
  }
  return h("div", {}, ...source, opts, result);
}

function previewTable(data, form) {
  const skip = new Set(), titles = {};
  const bookId = h("input", { placeholder: "book_id（英文）", pattern: "[a-z][a-z0-9_]{1,30}", style: "width:160px" });
  const nameInput = h("input", { placeholder: "書名" });
  const rows = data.chapters.map((c) => h("tr", {},
    h("td", {}, h("input", { type: "checkbox", checked: true, onchange: (e) => { e.target.checked ? skip.delete(c.no) : skip.add(c.no); } })),
    h("td", {}, c.no),
    h("td", {}, h("input", { value: c.title, style: "width:200px", oninput: (e) => { titles[c.no] = e.target.value; } })),
    h("td", {}, c.chars),
    h("td", {}, c.flags.map((f) => badge(f, "warn"))),
    h("td", { class: "muted small" }, c.head)));
  const commit = async () => {
    const id = bookId.value.trim();
    if (!/^[a-z][a-z0-9_]{1,30}$/.test(id)) return toast("book_id 只能用小寫英文、數字、底線，並以英文字母開頭（2–31 字）", "bad");
    try {
      await api("/api/import/commit", { body: { files: form.files, paths: form.paths, text: form.text, book_id: id, display_name: nameInput.value.trim() || id,
        opts: { ...form.opts, skip: [...skip], titles } } });
      toast("匯入完成"); FE.go(`#/book/${id}`);
    } catch (error) { fail(error); }
  };
  return h("div", {},
    data.warnings.map((w) => h("div", { class: chipClass("warn"), style: "display:block;margin:4px 0;white-space:normal" }, w)),
    h("div", { style: "max-height:300px;overflow:auto;margin:8px 0" }, h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, "匯入"), h("th", {}, "章號"), h("th", {}, "章名"), h("th", {}, "字數"), h("th", {}, "提醒"), h("th", {}, "開頭"))), h("tbody", {}, rows))),
    h("div", { class: "row" }, bookId, nameInput, h("button", { class: "primary", onclick: commit }, `匯入 ${data.chapters.length} 章`)));
}

function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",")[1] || "");
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

/* ---------------------------------------------------------------- 書：章節、角色、任務 */

FE.PAGES["book/:id"] = async (ctx) => viewBook(ctx.params.id, "chapters", ctx);
FE.PAGES["book/:id/cast"] = async (ctx) => viewBook(ctx.params.id, "cast", ctx);

async function viewBook(bookId, tab, ctx) {
  const data = await FE.api(`/api/books/${bookId}/chapters`, { signal: ctx.signal });
  const lang = data.book.lang;
  const name = (data.book.display_name || {})[lang] || bookId;
  ctx.crumbs([{ label: "書架", href: "#/" }, { label: name }]);
  const selected = new Set();
  const force = h("input", { type: "checkbox" });

  const checks = [];
  const rows = data.chapters.map((c) => {
    const [stateLabel, stateKind] = STATE_LABEL[c.scenes_state];
    const [outlineLabel, outlineKind] = OUTLINE_LABEL[c.outline];
    const box = h("input", { type: "checkbox", onchange: (e) => { e.target.checked ? selected.add(c.chapter_no) : selected.delete(c.chapter_no); } });
    checks.push([c.chapter_no, box]);
    return h("tr", {}, h("td", {}, box), h("td", {}, c.chapter_no), h("td", {}, h("a", { href: `#/book/${bookId}/ch/${c.chapter_no}` }, c.title)),
      h("td", {}, c.char_count), h("td", {}, badge(outlineLabel, outlineKind)), h("td", {}, badge(stateLabel, stateKind)),
      h("td", {}, c.scene_count || ""), h("td", {}, h("a", { href: `#/book/${bookId}/ch/${c.chapter_no}` }, c.scenes_state === "none" ? "開啟" : "校對")));
  });
  const pickAll = h("input", { type: "checkbox", onchange: (e) => checks.forEach(([n, box]) => { box.checked = e.target.checked; e.target.checked ? selected.add(n) : selected.delete(n); }) });

  async function runJob(kind) {
    if (!selected.size) return toast("先勾選要處理的章節", "bad");
    try {
      const body = { kind, chapters: [...selected], force: force.checked };
      const executor = Executors.value();
      if (executor) body.executor = executor;
      const started = await FE.api(`/api/books/${bookId}/jobs`, { body });
      Panel.attach(started.id);
    } catch (error) { fail(error); }
  }
  async function buildProto() {
    try {
      const built = await api(`/api/books/${bookId}/prototype`, { body: { chapters: [...selected] } });
      toast(`原型已建好：${built.scenes} 場、${built.lines} 句` + (built.style_added.length ? `（新增 ${built.style_added.length} 個地點／時段到 style.json）` : ""));
      window.open(`/api/books/${bookId}/prototype/index.html`, "_blank");
    } catch (error) { fail(error); }
  }

  const chapterPane = h("div", {},
    h("div", { class: "row card" },
      h("button", { onclick: () => runJob("extract") }, "抽取章綱與角色"),
      h("button", { class: "primary", onclick: () => runJob("annotate") }, "AI 標註"),
      h("label", { class: "small" }, force, " 覆蓋已有結果"),
      h("span", { class: "grow" }),
      h("button", { onclick: buildProto }, "建立可玩原型（勾選章；不勾＝全部）")),
    h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, pickAll), h("th", {}, "章"), h("th", {}, "章名"), h("th", {}, "字數"), h("th", {}, "章綱"), h("th", {}, "標註"), h("th", {}, "場數"), h("th", {}))), h("tbody", {}, rows)));
  const tabs = h("div", { class: "tabs" },
    h("a", { class: tabClass(tab === "chapters"), href: `#/book/${bookId}` }, "章節"),
    h("a", { class: tabClass(tab === "cast"), href: `#/book/${bookId}/cast` }, "角色表"));
  ctx.view(h("h1", {}, name), tabs, tab === "cast" ? await castPane(bookId, lang, ctx) : chapterPane);
}

async function castPane(bookId, lang, ctx) {
  const doc = await FE.api(`/api/books/${bookId}/cast`, { signal: ctx.signal });
  const chars = doc.characters;
  const body = h("tbody", {});
  function draw() {
    body.replaceChildren(...Object.entries(chars).map(([id, c]) => {
      const isPlayer = c.role === "player";
      return h("tr", {},
        h("td", { class: "small" }, id),
        h("td", {}, h("input", { value: c.names[lang] || "", oninput: (e) => { c.names[lang] = e.target.value; } })),
        h("td", {}, h("input", { value: (c.aliases || []).join("、"), oninput: (e) => { c.aliases = e.target.value.split(/[、,，]/).map((s) => s.trim()).filter(Boolean); } })),
        h("td", {}, isPlayer ? "內心獨白" : select(ROLES, c.role, (v) => { c.role = v; })),
        h("td", {}, isPlayer ? "" : h("input", { type: "number", min: 0, max: 360, value: c.hue ?? 0, style: "width:64px", oninput: (e) => { c.hue = Number(e.target.value); } })),
        h("td", {}, isPlayer ? "" : select(SHAPES, c.shape || "small", (v) => { c.shape = v; })),
        h("td", {}, isPlayer ? "" : h("input", { value: (c.one_line || {})[lang] || "", oninput: (e) => { c.one_line = e.target.value ? { [lang]: e.target.value } : undefined; } })),
        h("td", {}, isPlayer ? "" : h("button", { class: "ghost sm", title: "刪除", onclick: () => { delete chars[id]; draw(); } }, "✕")));
    }));
  }
  draw();
  const newId = h("input", { placeholder: "新角色 id（英文）", style: "width:150px" });
  const add = () => {
    const id = newId.value.trim();
    if (!/^[a-z][a-z0-9_]{1,62}$/.test(id) || chars[id]) return toast("id 只能用小寫英文、數字、底線，且不可重複", "bad");
    chars[id] = { names: { [lang]: id }, aliases: [], role: "minor", hue: 0, shape: "small" };
    newId.value = ""; draw();
  };
  const save = async () => {
    try {
      for (const c of Object.values(chars)) if (!c.one_line) delete c.one_line;
      await api(`/api/books/${bookId}/cast`, { method: "PUT", body: doc }); toast("角色表已存檔");
    } catch (error) { fail(error); }
  };
  return h("div", {}, h("p", { class: "muted small" }, "id 是程式用的英文代號（建立後不要改）；名稱、別名是顯示與判斷用的原文寫法。色相與體型用在剪影立繪。"),
    h("table", { class: "cast-table" }, h("thead", {}, h("tr", {}, ["id", "名稱", "別名（、分隔）", "定位", "色相", "體型", "一句話", ""].map((t) => h("th", {}, t)))), body),
    h("div", { class: "row", style: "margin-top:10px" }, newId, h("button", { onclick: add }, "新增角色"), h("button", { class: "primary", onclick: save }, "存檔")));
}

/* ---------------------------------------------------------------- 校對編輯器 */

FE.PAGES["book/:id/ch/:n"] = async (ctx) => viewEditor(ctx.params.id, Number(ctx.params.n), ctx);

async function viewEditor(bookId, number, ctx) {
  const [data, castDoc, book] = await Promise.all([
    FE.api(`/api/books/${bookId}/chapters/${number}`, { signal: ctx.signal }),
    FE.api(`/api/books/${bookId}/cast`, { signal: ctx.signal }),
    FE.api(`/api/books/${bookId}/chapters`, { signal: ctx.signal }),
  ]);
  const lang = book.book.lang;
  const bible = await FE.api(`/api/books/${bookId}/bible`, { signal: ctx.signal }).catch(() => ({ locations: [], bgm: [] }));
  const labels = { locs: Object.fromEntries(bible.locations.map((e) => [e.id, e.label])), bgms: Object.fromEntries(bible.bgm.map((e) => [e.id, e.label])) };
  const bookName = (book.book.display_name || {})[lang] || bookId;
  const chapter = data.chapter;
  ctx.crumbs([{ label: "書架", href: "#/" }, { label: bookName, href: `#/book/${bookId}` }, { label: `第 ${number} 章 ${chapter.title}` }]);
  if (!data.scenes) {
    ctx.view(h("h1", {}, `第 ${number} 章 ${chapter.title}`), h("div", { class: "card" }, h("p", {}, "這一章還沒有標註。"),
      h("button", { class: "primary", onclick: async () => {
        try { const body = { kind: "annotate", chapters: [number] };
        const executor = Executors.value();
        if (executor) body.executor = executor;
        const job = await FE.api(`/api/books/${bookId}/jobs`, { body }); toast("已開始標註，完成後重新整理本頁；進度在章節頁看"); Panel.attach(job.id); FE.go(`#/book/${bookId}`); } catch (error) { fail(error); }
      } }, "用 AI 標註這一章")));
    return;
  }
  const doc = data.scenes;
  const paragraphs = chapter.paragraphs;
  const chars = castDoc.characters;
  const charPairs = Object.entries(chars).map(([id, c]) => [id, id === "player" ? "（主角內心）" : `${c.names[lang] || id}`]);
  let issues = data.issues || [];
  let status = data.status;
  let onlyFlagged = false;
  let hot = null;
  // 推斷說話者的標記跟著行物件走（_flag），不寫進檔案
  for (const [s, l] of data.flags || []) { const line = doc.scenes[s] && doc.scenes[s].lines[l]; if (line) line._flag = true; }

  const sourcePane = h("div", { class: "card source" });
  const scenesPane = h("div", {});
  const statusChip = h("span", {});
  const issueChip = h("span", {});
  const saveBtn = h("button", { class: "primary", onclick: () => save(null) }, "存檔 (Ctrl+S)");

  function touch() { FE.dirty.set(true); saveBtn.textContent = "存檔 (Ctrl+S) ●"; scheduleCheck(); }
  let checkTimer = null;
  function scheduleCheck() {
    clearTimeout(checkTimer);
    checkTimer = setTimeout(async () => {
      try { issues = (await api(`/api/books/${bookId}/chapters/${number}/check`, { body: { doc: strip(doc) } })).issues; drawIssues(); drawSource(); } catch (_) { /* 檢查失敗不打斷編輯 */ }
    }, 600);
  }
  const strip = (d) => JSON.parse(JSON.stringify(d, (k, v) => (k.startsWith("_") ? undefined : v)));

  async function save(newStatus) {
    try {
      const result = await api(`/api/books/${bookId}/chapters/${number}/scenes`, { method: "PUT", body: { doc: strip(doc), status: newStatus || (status === "none" ? "draft" : undefined) } });
      issues = result.issues;
      // 伺服器可能重編了 scene_id、補了出場名單：同步回來，保留 _flag
      result.doc.scenes.forEach((s, i) => { s.lines.forEach((line, j) => { const old = doc.scenes[i] && doc.scenes[i].lines[j]; if (old && old._flag) line._flag = true; }); });
      doc.scenes = result.doc.scenes;
      if (newStatus) status = newStatus;
      FE.dirty.set(false); saveBtn.textContent = "存檔 (Ctrl+S)";
      toast("已存檔"); drawAll();
    } catch (error) { fail(error); }
  }
  function onKey(e) { if ((e.ctrlKey || e.metaKey) && e.key === "s") { e.preventDefault(); save(null); } }
  document.addEventListener("keydown", onKey);
  ctx.signal.addEventListener("abort", () => {
    document.removeEventListener("keydown", onKey);
    clearTimeout(checkTimer);
  }, { once: true });

  function usedParas() {
    const map = new Map();
    doc.scenes.forEach((s, i) => s.lines.forEach((line) => { if (line.source_para != null && !map.has(line.source_para)) map.set(line.source_para, i); }));
    return map;
  }
  function drawSource() {
    const used = usedParas();
    sourcePane.replaceChildren(h("div", { class: "muted small", style: "margin-bottom:6px" }, `原文（${paragraphs.length} 段）。橘底＝還沒被任何一行用到。`),
      ...paragraphs.map((p) => h("div", { class: "para" + (used.has(p.para_no) ? "" : " unused") + (hot === p.para_no ? " hot" : ""), id: "para-" + p.para_no },
        h("span", { class: "no" }, p.para_no), h("span", {}, p.text))));
  }
  function focusPara(no) {
    hot = no; drawSource();
    const el = document.getElementById("para-" + no); if (el) el.scrollIntoView({ block: "center", behavior: "smooth" });
  }

  function drawIssues() {
    const errors = issues.filter((i) => i.level === "error").length, warns = issues.filter((i) => i.level === "warning").length;
    issueChip.className = chipClass(errors ? "bad" : warns ? "warn" : "ok");
    issueChip.textContent = errors ? `${errors} 個錯誤（不能存）` : warns ? `${warns} 個提醒` : "沒有問題";
    const [label, kind] = status === "none" ? ["未標註", ""] : STATE_LABEL[status];
    statusChip.className = chipClass(kind); statusChip.textContent = label;
    drawScenes();
  }

  function issuesFor(s, l) { return issues.filter((i) => i.scene === s && i.line === l); }

  function lineRow(scene, s, line, l) {
    const type = line.type;
    const mine = issuesFor(s, l);
    const row = h("div", { class: `line type-${type}` + (line._flag ? " flag" : "") + (mine.some((i) => i.level === "error") ? " has-error" : "") });
    const changed = () => { touch(); };
    const typeSel = select([["narration", "旁白"], ["dialogue", "台詞"], ["direction", "演出"]], type, (v) => { convertLine(scene, line, v); changed(); drawScenes(); }, "type");
    if (type === "choice") { row.append(h("div", { class: "idx" }, l + 1), h("div", {}, "選項"), h("div", { style: "grid-column:3/-1" }, "（互動選項請直接編輯檔案；編輯器暫不支援）")); return row; }
    const speaker = type === "dialogue" ? select(charPairs, line.speaker, (v) => { line.speaker = v; line._flag = false; changed(); drawScenes(); }) : h("span", {});
    const expr = type === "dialogue" && line.speaker !== "player" ? select(EXPRS, line.expr || "neutral", (v) => { line.expr = v; changed(); }) : h("span", {});
    if (type === "direction") {
      row.append(h("div", { class: "idx" }, l + 1), typeSel, select(EFFECTS, line.effect || "none", (v) => { line.effect = v; changed(); }), h("span", {}), h("span", {}), h("span", {}), acts());
    } else {
      const count = h("div", { class: "count" + ((line.text || "").length > 90 ? " over" : "") }, `${(line.text || "").length}/90`);
      const text = h("textarea", { rows: 1, oninput: (e) => { line.text = e.target.value; count.textContent = `${line.text.length}/90`; count.className = "count" + (line.text.length > 90 ? " over" : ""); autosize(e.target); changed(); } });
      text.value = line.text || ""; setTimeout(() => autosize(text), 0);
      const para = h("input", { type: "number", min: 1, max: paragraphs.length, value: line.source_para, style: "width:54px", title: "來源段落", oninput: (e) => { line.source_para = Number(e.target.value); changed(); }, onfocus: () => focusPara(line.source_para) });
      const cell = h("div", { class: "textcell" }, text, count);
      if (type === "narration") row.append(h("div", { class: "idx" }, l + 1), typeSel, cell, para, acts());
      else row.append(h("div", { class: "idx" }, (line._flag ? "?" : "") + (l + 1)), typeSel, speaker, expr, cell, para, acts());
    }
    function acts() {
      return h("div", { class: "acts" },
        h("button", { class: "ghost sm", title: "上移", disabled: l === 0, onclick: () => { move(scene.lines, l, -1); changed(); drawScenes(); } }, "↑"),
        h("button", { class: "ghost sm", title: "下移", disabled: l === scene.lines.length - 1, onclick: () => { move(scene.lines, l, 1); changed(); drawScenes(); } }, "↓"),
        h("button", { class: "ghost sm", title: "在下面新增一行旁白", onclick: () => { scene.lines.splice(l + 1, 0, { type: "narration", text: "", source_para: line.source_para || 1 }); changed(); drawScenes(); } }, "＋"),
        h("button", { class: "ghost sm", title: "從這一行開始另成一場", disabled: l === 0, onclick: () => splitScene(s, l) }, "✂"),
        h("button", { class: "ghost sm", title: "刪除這一行", onclick: () => { scene.lines.splice(l, 1); changed(); drawScenes(); } }, "✕"));
    }
    if (mine.length) row.append(h("div", { class: "notes" }, mine.map((i) => h("span", { class: "note " + i.level }, (i.level === "error" ? "✖ " : "⚠ ") + i.message))));
    return row;
  }

  function convertLine(scene, line, type) {
    const base = { source_para: line.source_para || 1 };
    for (const k of Object.keys(line)) delete line[k];
    Object.assign(line, base);
    line.type = type;
    if (type === "narration") line.text = "";
    if (type === "dialogue") { line.speaker = charPairs.find(([id]) => id !== "player")?.[0] || "player"; line.expr = "neutral"; line.text = ""; }
    if (type === "direction") { line.effect = "shake"; delete line.source_para; }
  }
  const move = (arr, i, d) => { const [item] = arr.splice(i, 1); arr.splice(i + d, 0, item); };
  const autosize = (el) => { el.style.height = "auto"; el.style.height = el.scrollHeight + 2 + "px"; };

  function splitScene(s, l) {
    const scene = doc.scenes[s];
    const copy = JSON.parse(JSON.stringify(scene));
    copy.title = undefined; delete copy.title;
    copy.lines = scene.lines.splice(l);
    copy.cast = JSON.parse(JSON.stringify(scene.cast || []));
    doc.scenes.splice(s + 1, 0, copy);
    touch(); drawScenes();
  }
  function mergeNext(s) {
    const a = doc.scenes[s], b = doc.scenes[s + 1];
    a.lines.push(...b.lines);
    for (const m of b.cast || []) if (!(a.cast || []).some((x) => x.char_id === m.char_id)) a.cast.push(m);
    doc.scenes.splice(s + 1, 1);
    touch(); drawScenes();
  }

  function sceneCard(scene, s) {
    const title = (scene.title && scene.title[lang]) || "";
    const head = h("div", { class: "scene-head" },
      h("strong", {}, `場 ${s + 1}`),
      h("input", { class: "title", value: title, placeholder: "場名", oninput: (e) => { scene.title = e.target.value ? { [lang]: e.target.value } : undefined; if (!scene.title) delete scene.title; touch(); } }),
      h("input", { value: scene.location_id, list: "locs", title: "地點 id（英文）", oninput: (e) => { scene.location_id = e.target.value.trim(); touch(); } }),
      select(TIMES, scene.time_of_day, (v) => { scene.time_of_day = v; touch(); }),
      select(WEATHER, scene.weather || "", (v) => { v ? (scene.weather = v) : delete scene.weather; touch(); }),
      h("input", { value: scene.bgm || "", list: "bgms", placeholder: "bgm id", title: "背景音樂 id（英文）", oninput: (e) => { e.target.value.trim() ? (scene.bgm = e.target.value.trim()) : delete scene.bgm; touch(); } }),
      h("span", { class: "grow" }),
      h("button", { class: "ghost sm", disabled: s === 0, onclick: () => { move(doc.scenes, s, -1); touch(); drawScenes(); } }, "↑場"),
      h("button", { class: "ghost sm", disabled: s === doc.scenes.length - 1, onclick: () => { move(doc.scenes, s, 1); touch(); drawScenes(); } }, "↓場"),
      h("button", { class: "ghost sm", disabled: s === doc.scenes.length - 1, onclick: () => mergeNext(s) }, "與下一場合併"),
      h("button", { class: "ghost sm", disabled: doc.scenes.length === 1, onclick: async () => { if (await FE.confirm("刪除整場？", { danger: true, ok: "刪除" })) { doc.scenes.splice(s, 1); touch(); drawScenes(); } } }, "刪除場"));
    const present = new Set((scene.cast || []).map((m) => m.char_id));
    const cast = h("div", { class: "scene-cast" }, h("span", { class: "muted" }, "出場："),
      ...(scene.cast || []).map((m, i) => h("span", { class: "member" }, (chars[m.char_id]?.names[lang]) || m.char_id,
        select(EXPRS, m.expr || "neutral", (v) => { m.expr = v; touch(); }), select(POS, m.pos || "center", (v) => { m.pos = v; touch(); }),
        h("button", { class: "ghost sm", onclick: () => { scene.cast.splice(i, 1); touch(); drawScenes(); } }, "✕"))),
      select([["", "＋加入角色"], ...charPairs.filter(([id]) => id !== "player" && !present.has(id))], "", (v) => { if (v) { scene.cast.push({ char_id: v, expr: "neutral", pos: "center" }); touch(); drawScenes(); } }));
    const lines = scene.lines.map((line, l) => ({ line, l })).filter(({ line, l }) => !onlyFlagged || line._flag || issuesFor(s, l).length)
      .map(({ line, l }) => lineRow(scene, s, line, l));
    return h("div", { class: "scene" }, head, cast, ...(lines.length ? lines : [h("div", { class: "muted small", style: "padding:10px" }, "（沒有符合的行）")]));
  }
  function drawScenes() {
    const generic = issues.filter((i) => i.scene == null);
    scenesPane.replaceChildren(
      ...(generic.length ? [h("div", { class: "card" }, generic.map((i) => h("div", { class: "note " + i.level }, (i.level === "error" ? "✖ " : "⚠ ") + i.message)))] : []),
      h("datalist", { id: "locs" }, [...new Set([...data.known_locations, ...bible.locations.map((e) => e.id)])].map((v) => h("option", { value: v }, labels.locs[v] || ""))),
      h("datalist", { id: "bgms" }, [...new Set([...data.known_bgm, ...bible.bgm.map((e) => e.id)])].map((v) => h("option", { value: v }, labels.bgms[v] || ""))),
      ...doc.scenes.map(sceneCard));
  }
  function drawAll() { drawIssues(); drawSource(); }

  const prev = number > 1 ? h("a", { href: `#/book/${bookId}/ch/${number - 1}` }, "← 上一章") : "";
  const next = book.chapters.some((c) => c.chapter_no === number + 1) ? h("a", { href: `#/book/${bookId}/ch/${number + 1}` }, "下一章 →") : "";
  const bar = h("div", { class: "editor-bar" }, h("strong", {}, `第 ${number} 章　${chapter.title}`), statusChip, issueChip, h("span", { class: "grow" }),
    h("label", { class: "small" }, h("input", { type: "checkbox", onchange: (e) => { onlyFlagged = e.target.checked; drawScenes(); } }), " 只看 ? 與有提醒的行"),
    prev, next, saveBtn, h("button", { onclick: () => save("reviewed") }, "存檔並標為已校對"));
  ctx.view(bar, h("div", { class: "editor" }, sourcePane, scenesPane));
  drawAll();
}

/* ---------------------------------------------------------------- 設定 */
