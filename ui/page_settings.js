/* 設定頁：AI 執行者（DeepSeek、本機 llama.cpp、自訂）、預設執行者、位置、其他。#/settings */
"use strict";

FE.PAGES.settings = async (ctx) => {
  ctx.crumbs([{ label: "書架", href: "#/" }, { label: "設定" }]);
  const st = await FE.api("/api/settings", { signal: ctx.signal });
  const ex = st.executors, paths = st.paths;
  const THINK = { off: "關閉（最快最省，預設）", low: "低", medium: "中", high: "高", max: "最高" };
  const form = { default: ex.default, providers: {}, remove: [], paths: { works_dir: paths.works_dir_is_default ? "" : paths.works_dir, book_library: paths.book_library },
    comfyui: { base_url: st.comfyui.base_url }, server: { port: st.server.port } };
  const edit = (id, key, value) => { (form.providers[id] ||= {})[key] = value; };
  const field = (label, control, hint) => h("div", { style: "margin:8px 0" }, h("div", { class: "small muted" }, label), control, hint ? h("div", { class: "small muted" }, hint) : "");

  function providerCard(p) {
    const testOut = h("div", { class: "small", style: "margin-top:4px" });
    const modelInput = h("input", { value: p.model || "", list: "models-" + p.id, placeholder: p.id === "llamacpp" ? "留空：按「測試連線」列出伺服器上的模型" : "模型名稱", style: "width:300px", oninput: (e) => edit(p.id, "model", e.target.value.trim()) });
    const known = (p.models || []).map((m) => m.id);
    const num = (key, min, max, step, width) => h("input", { type: "number", min, max, step, value: p[key], style: `width:${width || 110}px`, oninput: (e) => edit(p.id, key, Number(e.target.value)) });
    const keyInput = h("input", { type: "password", placeholder: p.key.needed ? "貼上 API 金鑰" : "（本機通常不需要）", autocomplete: "off", style: "width:280px" });
    const saveKey = async () => {
      if (!keyInput.value.trim()) return toast("先貼上金鑰", "bad");
      try { const r = await FE.api("/api/settings/key", { body: { provider: p.id, key: keyInput.value.trim() } }); keyInput.value = ""; toast("金鑰已存到 " + r.saved_to); FE.route(); refreshChip(); } catch (error) { fail(error); }
    };
    const test = async () => {
      testOut.style.color = ""; testOut.textContent = "測試中…（用已儲存的設定；改了欄位請先按最下面的「儲存設定」）";
      try {
        const r = await FE.api("/api/settings/test", { body: { executor: p.id } });
        testOut.style.color = r.ok ? "var(--ok)" : "var(--bad)";
        testOut.replaceChildren(document.createTextNode((r.ok ? "✔ " : "✖ ") + r.message + (r.tokens ? `（${r.model}，${r.tokens} tokens）` : "")),
          ...(r.models && r.models.length ? [h("div", { class: "row", style: "margin-top:4px" }, h("span", { class: "muted" }, "伺服器上的模型："), r.models.map((m) => h("button", { class: "sm", onclick: () => { modelInput.value = m; edit(p.id, "model", m); } }, m)))] : []));
      } catch (error) { testOut.style.color = "var(--bad)"; testOut.textContent = "✖ " + error.message; }
    };
    return h("details", { class: "prov", open: p.id === ex.default.split("/")[0] || undefined },
      h("summary", {}, h("strong", {}, p.name || p.id), h("code", {}, p.id), p.builtin ? "" : badge("自訂"),
        p.id === ex.default.split("/")[0] ? badge("預設", "ok") : "", h("span", { class: "grow" }),
        p.key.needed ? badge(p.key.found ? "金鑰已設定" : "沒有金鑰", p.key.found ? "ok" : "bad") : badge("不需金鑰")),
      h("div", { class: "prov-body" },
        p.builtin ? "" : field("顯示名稱", h("input", { value: p.name, style: "width:240px", oninput: (e) => edit(p.id, "name", e.target.value.trim()) })),
        field("伺服器位址（OpenAI 相容，結尾通常是 /v1）", h("input", { value: p.base_url, style: "width:min(420px,100%)", oninput: (e) => edit(p.id, "base_url", e.target.value.trim()) }),
          p.id === "llamacpp" ? "先啟動 llama-server（例如 llama-server -m 模型.gguf -c 32768 --port 8080）。" : ""),
        field("模型", h("div", { class: "row" }, modelInput, h("datalist", { id: "models-" + p.id }, known.map((m) => h("option", { value: m }))),
          known.map((m) => h("button", { class: "sm", onclick: () => { modelInput.value = m; edit(p.id, "model", m); } }, m === "deepseek-flash" ? "flash（便宜、快，預設）" : m === "deepseek-v4-pro" ? "pro（較強、約 4.5 倍價錢）" : m)))),
        h("div", { class: "prov-grid" },
          field("上下文長度（tokens）", num("context_tokens", 1000, 10000000, 1024), "要和伺服器的 -c 一致；送出內容超過會先擋下，不用等伺服器報錯。"),
          field("單次輸出上限（tokens）", num("max_output", 16, 1000000, 1024)),
          field("溫度", num("temperature", 0, 2, 0.1, 80), "標註是結構化工作，低一點比較穩。"),
          field("思考模式", select(Object.entries(THINK).filter(([k]) => ex.efforts.includes(k)), p.reasoning.structured, (v) => edit(p.id, "reasoning", { structured: v })), "開了會慢很多、也貴很多；標註通常不需要。"),
          field("要求 JSON 輸出", h("input", { type: "checkbox", checked: p.json_mode, onchange: (e) => edit(p.id, "json_mode", e.target.checked) }), "不支援的伺服器請關掉。")),
        field("API 金鑰", h("div", { class: "row" }, keyInput, h("button", { onclick: saveKey }, "儲存金鑰"), p.key.found ? h("span", { class: "small muted" }, "目前：" + p.key.where) : ""), "只存在這台電腦的系統 keyring，不寫進 config.yaml，也不會顯示在畫面上。"),
        h("div", { class: "row" }, h("button", { onclick: test }, "測試連線"),
          p.builtin ? "" : h("button", { class: "ghost sm", onclick: async () => { if (await FE.confirm(`刪除自訂供應商 ${p.id}？儲存後生效。`, { danger: true, ok: "刪除" })) { form.remove.push(p.id); toast("已標記刪除，按「儲存設定」生效"); } } }, "刪除此供應商")),
        testOut));
  }

  const defaultSelect = select(ex.choices.map((c) => [c.id, c.name || c.id]), form.default, (v) => { form.default = v; });
  const newId = h("input", { placeholder: "id（小寫英文，例如 my_server）", style: "width:210px" });
  const newUrl = h("input", { placeholder: "http://192.168.1.5:8000/v1", style: "width:260px" });
  const newModel = h("input", { placeholder: "模型名稱（可留空）", style: "width:170px" });
  const addCustom = h("div", { class: "row" }, newId, newUrl, newModel, h("button", { onclick: async () => {
    try { await FE.api("/api/settings", { method: "PUT", body: { executors: { providers: { [newId.value.trim()]: { base_url: newUrl.value.trim(), model: newModel.value.trim() || undefined, name: newId.value.trim() } } } } }); toast("已新增"); FE.route(); } catch (error) { fail(error); }
  } }, "新增自訂供應商"));

  const worksInput = h("input", { value: form.paths.works_dir, placeholder: `（預設）${paths.project_root}\\works`, style: "width:min(520px,100%)", oninput: (e) => { form.paths.works_dir = e.target.value.trim(); } });
  const libInput = h("input", { value: form.paths.book_library, style: "width:min(520px,100%)", oninput: (e) => { form.paths.book_library = e.target.value.trim(); } });
  const save = async () => {
    try {
      const body = { executors: { default: form.default, providers: form.providers, remove: form.remove }, paths: form.paths, comfyui: form.comfyui, server: form.server };
      const r = await FE.api("/api/settings", { method: "PUT", body });
      toast(r.restart_required ? "已存檔。埠號要重新啟動工作台才會生效" : "設定已儲存"); refreshChip(); Executors.reload(); FE.route();
    } catch (error) { fail(error); }
  };
  ctx.view(h("h1", {}, "設定"),
    h("div", { class: "card" }, h("h2", { style: "margin-top:0" }, "預設執行者"), h("div", { class: "row" }, defaultSelect, h("span", { class: "small muted" }, "沒特別指定時，標註與抽取用這個。右側 AI 面板可以臨時改用別的。"))),
    h("h2", {}, "AI 執行者"), ...ex.providers.map(providerCard),
    h("div", { class: "card" }, h("div", { class: "small muted" }, "自己的 OpenAI 相容伺服器（vLLM、LM Studio、Ollama 的 /v1 等）"), addCustom),
    h("div", { class: "card" }, h("h2", { style: "margin-top:0" }, "位置"),
      field("作品存放位置（匯入的章節、標註、原型、之後的圖片都放這裡）", h("div", { class: "row" }, worksInput, h("button", { onclick: () => browse((p) => { worksInput.value = p; form.paths.works_dir = p; }, { dirs: true, start: paths.works_dir }) }, "瀏覽…"),
        h("button", { onclick: () => { worksInput.value = ""; form.paths.works_dir = ""; } }, "用預設")), "目前：" + paths.works_dir + "。改位置不會搬移已有的書，要自己把舊資料夾整個複製過去。"),
      field("書庫位置（novel-studio 的書；唯讀，只會讀取）", h("div", { class: "row" }, libInput, h("button", { onclick: () => browse((p) => { libInput.value = p; form.paths.book_library = p; }, { dirs: true, start: paths.book_library }) }, "瀏覽…")), paths.library_exists ? "" : "⚠ 找不到這個資料夾"),
      field("專案資料夾（程式與設定檔所在，不能在這裡改）", h("code", {}, paths.project_root))),
    h("div", { class: "card" }, h("h2", { style: "margin-top:0" }, "其他"),
      field("ComfyUI 位址（之後產圖用）", h("input", { value: form.comfyui.base_url, style: "width:320px", oninput: (e) => { form.comfyui.base_url = e.target.value.trim(); } })),
      field("工作台埠號", h("input", { type: "number", min: 1024, max: 65535, value: form.server.port, style: "width:100px", oninput: (e) => { form.server.port = Number(e.target.value); } }), `預設 7866；目前執行在 ${st.server.running_port ?? "?"}。改了要關掉工作台再啟動。`)),
    h("div", { class: "row" }, h("button", { class: "primary", onclick: save }, "儲存設定"), h("span", { class: "small muted" }, `存到 ${st.config_path || "（沒有設定檔）"}；會重寫整個檔案（YAML 註解不保留），舊檔備份成 config.yaml.bak。`)));
};
