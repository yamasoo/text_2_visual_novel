/* 設定頁：AI 執行者（DeepSeek、本機 llama.cpp、自訂）、預設執行者、位置、其他。#/settings */
"use strict";

FE.PAGES.settings = async (ctx) => {
  ctx.crumbs([{ label: "書架", href: "#/" }, { label: "設定" }]);
  const st = await FE.api("/api/settings", { signal: ctx.signal });
  const ex = st.executors, paths = st.paths;
  const THINK = { off: "關閉（最快最省，預設）", low: "低", medium: "中", high: "高", max: "最高" };
  const comfy = st.comfyui || {};
  const tc = st.tagcomplete || {};
  const form = { default: ex.default, providers: {}, remove: [], paths: { works_dir: paths.works_dir_is_default ? "" : paths.works_dir, book_library: paths.book_library },
    comfyui: { base_url: comfy.base_url || "", default_profile: comfy.default_profile || "",
               profiles: JSON.parse(JSON.stringify(comfy.profiles || {})) },
    tagcomplete: { tag_csv: tc.tag_csv || "", tag_zh_csv: tc.tag_zh_csv || "", forge_dir: tc.forge_dir || "" },
    server: { port: st.server.port } };
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
        field(p.type === "gemini" ? "API 端點（Google Gemini v1beta）" : "伺服器位址（OpenAI 相容，結尾通常是 /v1）",
          h("input", { value: p.base_url, style: "width:min(420px,100%)", oninput: (e) => edit(p.id, "base_url", e.target.value.trim()) }),
          p.id === "llamacpp" ? "先啟動 llama-server（例如 llama-server -m 模型.gguf -c 32768 --port 8080）。" : ""),
        field("模型", h("div", { class: "row" }, modelInput, h("datalist", { id: "models-" + p.id }, known.map((m) => h("option", { value: m }))),
          known.map((m) => h("button", { class: "sm", onclick: () => { modelInput.value = m; edit(p.id, "model", m); } },
            m === "deepseek-flash" ? "flash（便宜、快，預設）"
              : m === "deepseek-v4-pro" ? "pro（較強、約 4.5 倍價錢）"
              : m === "gemini-3.8-flash" ? "3.8 Flash（預設）"
              : m === "gemini-4-neon" ? "4 Neon"
              : m)))),
        h("div", { class: "prov-grid" },
          field("上下文長度（tokens）", num("context_tokens", 1000, 10000000, 1024), "要和伺服器的 -c 一致；送出內容超過會先擋下，不用等伺服器報錯。"),
          field("單次輸出上限（tokens）", num("max_output", 16, 1000000, 1024)),
          field("溫度", num("temperature", 0, 2, 0.1, 80), "標註是結構化工作，低一點比較穩。"),
          field("思考模式", select(Object.entries(THINK).filter(([k]) => ex.efforts.includes(k)), p.reasoning.structured, (v) => edit(p.id, "reasoning", { structured: v })), "開了會慢很多、也貴很多；標註通常不需要。"),
          field("要求 JSON 輸出", h("input", { type: "checkbox", checked: p.json_mode, onchange: (e) => edit(p.id, "json_mode", e.target.checked) }), "不支援的伺服器請關掉。"),
          p.type === "gemini" ? field("放寬安全過濾（BLOCK_NONE）", h("input", { type: "checkbox", checked: p.safety_relax !== false, onchange: (e) => edit(p.id, "safety_relax", e.target.checked) }), "減少小說創作情節遭誤擋（預設開啟）。") : ""),
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
  /* ---------------------------------------------------------------- ComfyUI（QQ-4） */
  // 一份正本同時給漫畫與 VN：位址、預設 profile、profiles（kind／workflow／模型／負向）。
  let comfyModels = null;
  let comfyEdit = Object.keys(form.comfyui.profiles)[0] || "";
  const comfyOut = h("div");

  const drawComfy = () => {
    const names = Object.keys(form.comfyui.profiles);
    const choose = (name) => { comfyEdit = name; drawComfy(); };
    const list = h("select", { style: "width:220px", onchange: (e) => choose(e.target.value) },
      [["", "（沒有 profile：用內建預設）"]].concat(names.map((n) => [n, n]))
        .map(([value, label]) => h("option", { value, selected: comfyEdit === value }, label)));
    const presetSelect = names.length
      ? h("select", { style: "width:220px", onchange: (e) => { form.comfyui.default_profile = e.target.value; } },
          [["", "（不指定：用內建預設）"]].concat(names.map((n) => [n, n]))
            .map(([value, label]) => h("option", { value, selected: (form.comfyui.default_profile || "") === value }, label)))
      : h("span", { class: "small muted" }, "先新增一個 profile");
    const spec = form.comfyui.profiles[comfyEdit] || null;
    if (spec && spec.default_preset && !(spec.presets || {})[spec.default_preset]) spec.default_preset = "";   // 指向已被改名的 preset：清掉，存檔才不會又寫回去
    const set = (key, value) => { spec[key] = value; };
    const setModel = (key, value) => { spec.models = { ...(spec.models || {}), [key]: value }; };
    const picker = (key, label) => {
      const items = (comfyModels && comfyModels[key]) || [];
      return field(label, items.length
        ? h("select", { style: "width:min(420px,100%)", onchange: (e) => setModel(key, e.target.value) },
            [["", "（留空＝依 kind 自動挑）"]].concat(items.map((m) => [m, m]))
              .map(([value, text]) => h("option", { value, selected: ((spec.models || {})[key] || "") === value }, text)))
        : h("input", { value: (spec.models || {})[key] || "", style: "width:min(420px,100%)", placeholder: "留空＝依 kind 自動挑；先按「抓模型清單」", oninput: (e) => setModel(key, e.target.value.trim()) }));
    };
    const presetTable = (prof) => {
      prof.presets = prof.presets && typeof prof.presets === "object" ? prof.presets : {};
      const presets = prof.presets;
      const num = (row, key, width) => h("input", { type: "number", step: "any", value: row[key] ?? "", style: `width:${width}px`,
        oninput: (e) => { const raw = e.target.value.trim(); const n = Number(raw); if (raw !== "" && Number.isFinite(n)) row[key] = n; else delete row[key]; } });
      const str = (row, key, width, placeholder, listId) => h("input", { value: row[key] ?? "", list: listId || undefined, style: `width:${width}px`,
        placeholder: placeholder || "", oninput: (e) => { const v = e.target.value.trim(); if (v === "") delete row[key]; else row[key] = v; } });
      const row = (name) => {
        if (!presets[name] || typeof presets[name] !== "object") presets[name] = {};
        const row = presets[name];
        const nameInput = h("input", { value: name, style: "width:120px", onchange: (e) => {
          const next = e.target.value.trim();
          if (!next || next === name) { e.target.value = name; return; }
          if (presets[next]) { e.target.value = name; return toast("這一組已經有同名 preset", "bad"); }
          const rebuilt = {};
          for (const key of Object.keys(presets)) rebuilt[key === name ? next : key] = presets[key];
          prof.presets = rebuilt;
          if (String(prof.default_preset || "") === name) prof.default_preset = next;
          drawComfy();
        } });
        return h("tr", {},
          h("td", {}, nameInput),
          h("td", {}, num(row, "steps", 72)), h("td", {}, num(row, "cfg", 72)),
          h("td", {}, str(row, "sampler", 112, "", "comfy-sampler-list")),
          h("td", {}, str(row, "scheduler", 104, "", "comfy-scheduler-list")),
          h("td", {}, str(row, "lora", 200, "（不用就留空）", "comfy-lora-list")),
          h("td", {}, num(row, "lora_strength", 80)),
          h("td", {}, h("button", { class: "sm ghost", onclick: async () => {
            if (!(await FE.confirm(`刪掉 preset「${name}」？`, { danger: true, ok: "刪除" }))) return;
            delete presets[name];
            if (String(prof.default_preset || "") === name) prof.default_preset = "";
            drawComfy();
          } }, "刪除列")));
      };
      const knownLoras = (comfyModels && comfyModels.lora) || [];
      const badLoras = knownLoras.length
        ? Object.keys(presets).map((name) => (presets[name] || {}).lora).filter((x) => x && knownLoras.indexOf(x) < 0)
        : [];
      const loraWarning = badLoras.length
        ? h("div", { class: "small", style: "color:var(--bad);margin-top:4px" },
            "⚠ 這些 lora 檔名不在 ComfyUI 的清單裡（出圖會被 ComfyUI 退回）：" + badLoras.join("、")
            + "。請用欄位下拉或按上面的「抓模型清單」後重挑。")
        : "";
      return h("div", {},
        Object.keys(presets).length
          ? h("div", {},
              h("table", {},
                h("thead", {}, h("tr", {}, ["預設名稱", "steps", "cfg", "sampler", "scheduler", "lora", "lora_strength", ""].map((t) => h("th", {}, t)))),
                h("tbody", {}, Object.keys(presets).map((name) => row(name)))),
              h("datalist", { id: "comfy-sampler-list" }, ["euler", "euler_ancestral", "er_sde", "dpmpp_2m", "dpmpp_2m_sde", "dpmpp_sde", "ddim", "uni_pc"]
                .map((m) => h("option", { value: m }))),
              h("datalist", { id: "comfy-scheduler-list" }, ["simple", "normal", "beta", "karras", "sgm_uniform", "exponential"]
                .map((m) => h("option", { value: m }))),
              loraWarning)
          : h("div", { class: "small muted" }, "這一組還沒有 preset；留空＝用 workflow 的預設值。"),
        h("div", { class: "row", style: "margin-top:6px" }, h("button", { class: "sm", onclick: () => {
          let name = "preset"; let i = 2;
          while (presets[name]) name = "preset" + (i++);
          presets[name] = { steps: 20, cfg: 7 };
          drawComfy();
        } }, "新增預設列"), h("span", { class: "small muted" }, "欄位：steps、cfg、sampler、scheduler、lora（加速／蒸餾 LoRA）、lora_strength。")));
    };
    const loraTable = (prof) => {
      if (!Array.isArray(prof.loras)) prof.loras = [];
      const list = prof.loras;
      const knownLoras = (comfyModels && comfyModels.lora) || [];
      const badLoras = knownLoras.length
        ? list.map((x) => (x && x.name ? String(x.name).trim() : "")).filter((x) => x && knownLoras.indexOf(x) < 0)
        : [];
      const loraWarning = badLoras.length
        ? h("div", { class: "small", style: "color:var(--bad);margin-top:4px" },
            "⚠ 這些附加 LoRA 檔名不在 ComfyUI 的清單裡（出圖會被 ComfyUI 退回）：" + badLoras.join("、")
            + "。請用下拉清單或先按上面的「抓模型清單」後重挑。")
        : "";
      const row = (item, idx) => h("tr", {},
        h("td", { style: "text-align:center" }, h("input", {
          type: "checkbox", checked: item.on !== false,
          onchange: (e) => { item.on = Boolean(e.target.checked); },
        })),
        h("td", {}, h("input", {
          value: item.name || "", list: "comfy-lora-list", style: "width:min(280px,100%)",
          placeholder: "從 ComfyUI 清單選 LoRA 檔名",
          oninput: (e) => { item.name = e.target.value.trim(); },
        })),
        h("td", {}, h("input", {
          type: "number", step: "0.05", value: item.strength ?? 1.0, style: "width:80px",
          oninput: (e) => {
            const raw = e.target.value.trim();
            const n = Number(raw);
            if (raw !== "" && Number.isFinite(n)) item.strength = n;
          },
        })),
        h("td", {}, h("input", {
          value: item.trigger || "", style: "width:min(220px,100%)",
          placeholder: "選填；逗號分隔（僅 VN 自動附加）",
          oninput: (e) => { item.trigger = e.target.value.trim(); },
        })),
        h("td", {}, h("button", { class: "sm ghost", onclick: () => {
          list.splice(idx, 1);
          drawComfy();
        } }, "刪除列")));
      return h("div", { style: "margin-top:12px" },
        h("div", { class: "small", style: "font-weight:600;margin-bottom:4px" }, "附加 LoRA（畫風／細節／角色，依序串接在 preset 的 lora 後面）"),
        h("div", { class: "small muted", style: "margin-bottom:6px" }, "漫畫與 VN 都會套用勾選的 LoRA；「觸發詞」目前僅 VN 會自動附加到正向提示詞（漫畫端會忽略）。空白檔名存檔時會自動略過。"),
        list.length
          ? h("table", {},
              h("thead", {}, h("tr", {}, ["啟用", "LoRA 檔名", "強度", "觸發詞（VN）", ""].map((t) => h("th", {}, t)))),
              h("tbody", {}, list.map((item, idx) => row(item, idx))))
          : h("div", { class: "small muted" }, "這一組目前沒有附加 LoRA。"),
        loraWarning,
        h("div", { class: "row", style: "margin-top:6px" },
          h("button", { class: "sm", onclick: () => {
            list.push({ name: "", strength: 1.0, on: true, trigger: "" });
            drawComfy();
          } }, "新增 LoRA")));
    };
    const edits = spec ? [
      h("datalist", { id: "comfy-lora-list" }, ((comfyModels && comfyModels.lora) || []).map((m) => h("option", { value: m }))),
      field("種類（決定 workflow 與猜模型的方式）", h("select", { onchange: (e) => set("kind", e.target.value) },
        [["anima", "Anima"], ["moss", "MOSS-Image"]].map(([value, text]) => h("option", { value, selected: (spec.kind || "anima") === value }, text)))),
      field("workflow 檔", h("input", { value: spec.workflow || "", style: "width:min(420px,100%)", placeholder: "留空＝內建（anima.api.json／moss.api.json）", oninput: (e) => set("workflow", e.target.value.trim()) })),
      picker("unet", "主模型（unet）"), picker("clip", "文字編碼器（clip）"), picker("vae", "VAE"),
      field("負向提示詞", (() => {
        const ta = h("textarea", { rows: 2, style: "width:min(520px,100%)", oninput: (e) => set("negative", e.target.value) }, spec.negative || "");
        if (window.attachTagComplete) window.attachTagComplete(ta);
        return ta;
      })()),
      field("預設 preset", h("div", { class: "row" },
        h("select", { style: "width:220px", onchange: (e) => set("default_preset", e.target.value) },
          [["", "（不指定：照 workflow 內建；不會自動挑第一個）"]].concat(Object.keys(spec.presets || {}).map((n) => [n, n]))
            .map(([value, label]) => h("option", { value, selected: (spec.default_preset || "") === value }, label))),
        h("span", { class: "small muted" }, "presets 是這一組自己的，別組的同名 preset 互不影響。"))),
      presetTable(spec),
      loraTable(spec),
    ] : [h("div", { class: "small muted" }, "目前沒有 profile：下面可以新增一個。")];

    const newName = h("input", { placeholder: "profile 名稱（小寫英文，例如 moss）", style: "width:240px" });
    const testOut = h("div", { class: "small", style: "margin-top:6px" });
    comfyOut.replaceChildren(h("div", { class: "card" }, h("h2", { style: "margin-top:0" }, "ComfyUI（出圖）"),
      h("div", { class: "small muted" }, "漫畫與 VN 共用這一份設定：位址、profile、模型檔都只有這裡。漫畫的專案用哪一組由 project.yaml 的 comfy_profile 決定；VN 這本書用哪一組在「素材 → 畫風（這本書）」。"),
      field("位址", h("div", { class: "row" },
        h("input", { value: form.comfyui.base_url, style: "width:320px", oninput: (e) => { form.comfyui.base_url = e.target.value.trim(); } }),
        h("button", { class: "sm", onclick: async () => {
          testOut.style.color = ""; testOut.textContent = "測試中…";
          try { const r = await FE.api("/api/settings/comfyui/test", { body: { base_url: form.comfyui.base_url } });
            testOut.style.color = "var(--ok)"; testOut.textContent = "✔ " + r.message + (r.device ? `（${r.device}）` : "");
          } catch (error) { testOut.style.color = "var(--bad)"; testOut.textContent = "✖ " + error.message; }
        } }, "測試連線"),
        h("button", { class: "sm", onclick: async () => {
          testOut.style.color = ""; testOut.textContent = "抓清單…";
          try { comfyModels = await FE.api(`/api/settings/comfyui/models?base_url=${encodeURIComponent(form.comfyui.base_url)}`);
            const counts = ["unet", "clip", "vae", "lora"].map((k) => `${k} ${(comfyModels[k] || []).length}`).join("／");
            testOut.style.color = "var(--ok)"; testOut.textContent = "✔ 抓到 " + counts; drawComfy();
          } catch (error) { testOut.style.color = "var(--bad)"; testOut.textContent = "✖ " + error.message; }
        } }, "抓模型清單")),
        "同一臺 ComfyUI 一次只掛一組模型：換 profile 就是換掛那一組檔案。測試與抓清單用的都是上面這個位址（改完記得按最下面的「儲存設定」）。"),
      field("預設 profile（漫畫專案沒指定 comfy_profile 時用它；VN 這本書在「素材 → 畫風（這本書）」挑）", h("div", { class: "row" }, presetSelect)),
      field("選擇要編輯的 profile", h("div", { class: "row" }, list,
        h("button", { class: "sm", onclick: () => {
          const name = newName.value.trim();
          if (!/^[a-z0-9][a-z0-9_-]*$/.test(name)) return toast("名稱只能用小寫英文、數字、底線、連字號", "bad");
          if (form.comfyui.profiles[name]) return toast("已經有同名 profile", "bad");
          form.comfyui.profiles[name] = { kind: "anima", workflow: "", models: { unet: "", clip: "", vae: "" }, negative: "", loras: [] };
          newName.value = ""; comfyEdit = name; drawComfy();
        } }, "新增"),
        h("button", { class: "sm", onclick: () => {
          if (!comfyEdit) return toast("先選一個 profile", "bad");
          let name = comfyEdit + "_copy"; let i = 2;
          while (form.comfyui.profiles[name]) name = comfyEdit + "_copy" + (i++);
          form.comfyui.profiles[name] = JSON.parse(JSON.stringify(form.comfyui.profiles[comfyEdit]));
          comfyEdit = name; drawComfy();
        } }, "複製"),
        h("button", { class: "sm ghost", onclick: async () => {
          if (!comfyEdit) return;
          if (!(await FE.confirm(`刪除 profile「${comfyEdit}」？按「儲存設定」後生效。`, { danger: true, ok: "刪除" }))) return;
          delete form.comfyui.profiles[comfyEdit];
          if (form.comfyui.default_profile === comfyEdit) form.comfyui.default_profile = "";
          comfyEdit = Object.keys(form.comfyui.profiles)[0] || ""; drawComfy();
        } }, "刪除")), h("div", { class: "row" }, newName)),
      ...edits,
      testOut));
  };

  const comfyCard = () => comfyOut;

  const save = async () => {
    try {
      const body = { executors: { default: form.default, providers: form.providers, remove: form.remove }, paths: form.paths, comfyui: form.comfyui, tagcomplete: form.tagcomplete, server: form.server };
      const r = await FE.api("/api/settings", { method: "PUT", body });
      if (window.clearTagCompleteCache) window.clearTagCompleteCache();
      toast(r.restart_required ? "已存檔。埠號要重新啟動工作台才會生效" : "設定已儲存"); refreshChip(); Executors.reload(); FE.route();
    } catch (error) { fail(error); }
  };
  drawComfy();

  const TAB_KEY = "settings_tab";
  const TABS = [
    ["ai", "AI 執行者"],
    ["comfy", "ComfyUI（出圖）"],
    ["general", "一般與路徑"],
  ];
  let activeTab = FE.store.get(TAB_KEY, "ai");
  if (!TABS.some(([k]) => k === activeTab)) activeTab = "ai";

  const paneAi = h("div", {},
    h("div", { class: "card" }, h("h2", { style: "margin-top:0" }, "預設執行者"),
      h("div", { class: "row" }, defaultSelect, h("span", { class: "small muted" }, "沒特別指定時，標註與抽取用這個。右側 AI 面板可以臨時改用別的。"))),
    ...ex.providers.map(providerCard),
    h("div", { class: "card" }, h("div", { class: "small muted" }, "自己的 OpenAI 相容伺服器（vLLM、LM Studio、Ollama 的 /v1 等）"), addCustom));

  const paneComfy = h("div", {}, comfyCard());

  const paneGeneral = h("div", {},
    h("div", { class: "card" }, h("h2", { style: "margin-top:0" }, "位置"),
      field("作品存放位置（匯入的章節、標註、原型、之後的圖片都放這裡）", h("div", { class: "row" }, worksInput,
        h("button", { onclick: () => browse((p) => { worksInput.value = p; form.paths.works_dir = p; }, { dirs: true, start: paths.works_dir }) }, "瀏覽…"),
        h("button", { onclick: () => { worksInput.value = ""; form.paths.works_dir = ""; } }, "用預設")),
        "目前：" + paths.works_dir + "。改位置不會搬移已有的書，要自己把舊資料夾整個複製過去。"),
      field("書庫位置（novel-studio 的書；唯讀，只會讀取）", h("div", { class: "row" }, libInput,
        h("button", { onclick: () => browse((p) => { libInput.value = p; form.paths.book_library = p; }, { dirs: true, start: paths.book_library }) }, "瀏覽…")),
        paths.library_exists ? "" : "⚠ 找不到這個資料夾"),
      field("專案資料夾（程式與設定檔所在，不能在這裡改）", h("code", {}, paths.project_root))),
    (() => {
      const tcTagInput = h("input", { value: form.tagcomplete.tag_csv, style: "width:min(440px,100%)", placeholder: "例如 danbooru.csv（留空則由下方 Forge 資料夾自動偵測）", oninput: (e) => { form.tagcomplete.tag_csv = e.target.value.trim(); } });
      const tcZhInput = h("input", { value: form.tagcomplete.tag_zh_csv, style: "width:min(440px,100%)", placeholder: "例如 danbooru-0-zh.csv（支援中文反查英文標籤）", oninput: (e) => { form.tagcomplete.tag_zh_csv = e.target.value.trim(); } });
      const tcForgeInput = h("input", { value: form.tagcomplete.forge_dir, style: "width:min(440px,100%)", placeholder: "選填：Forge／WebUI 根資料夾（自動找 tagcomplete 擴充）", oninput: (e) => { form.tagcomplete.forge_dir = e.target.value.trim(); } });
      const resolved = tc.resolved || {};
      const statusText = (resolved.tag_csv || resolved.tag_zh_csv)
        ? `✔ 已啟用：${[resolved.tag_csv ? "英文表" : "", resolved.tag_zh_csv ? "中文表" : ""].filter(Boolean).join(" ＋ ")}`
        : "尚未偵測到標籤 CSV（填入路徑並按「儲存設定」後，各繪圖提示詞欄位即啟用自動補全）";
      return h("div", { class: "card" }, h("h2", { style: "margin-top:0" }, "標籤補全（TagComplete）"),
        h("div", { class: "small muted", style: "margin-bottom:8px" }, "在角色外觀、畫風、ComfyUI 提示詞欄位打英文前綴、別名、@畫師或中文時自動跳出 Danbooru 標籤補全。"),
        field("Danbooru 標籤 CSV（tag_csv）", h("div", { class: "row" }, tcTagInput,
          h("button", { onclick: () => browse((p) => { tcTagInput.value = p; form.tagcomplete.tag_csv = p; }, { start: form.tagcomplete.tag_csv }) }, "瀏覽…"))),
        field("中文對照 CSV（tag_zh_csv）", h("div", { class: "row" }, tcZhInput,
          h("button", { onclick: () => browse((p) => { tcZhInput.value = p; form.tagcomplete.tag_zh_csv = p; }, { start: form.tagcomplete.tag_zh_csv }) }, "瀏覽…"))),
        field("Forge 資料夾（forge_dir，選填）", h("div", { class: "row" }, tcForgeInput,
          h("button", { onclick: () => browse((p) => { tcForgeInput.value = p; form.tagcomplete.forge_dir = p; }, { dirs: true, start: form.tagcomplete.forge_dir }) }, "瀏覽…"))),
        h("div", { class: "small", style: (resolved.tag_csv || resolved.tag_zh_csv) ? "color:var(--ok)" : "color:var(--ink-2)" }, statusText));
    })(),
    h("div", { class: "card" }, h("h2", { style: "margin-top:0" }, "伺服器"),
      field("工作台埠號", h("input", { type: "number", min: 1024, max: 65535, value: form.server.port, style: "width:100px", oninput: (e) => { form.server.port = Number(e.target.value); } }),
        `預設 7866；目前執行在 ${st.server.running_port ?? "?"}。改了要關掉工作台再啟動。`)));

  const panes = { ai: paneAi, comfy: paneComfy, general: paneGeneral };
  const tabBtns = {};
  const selectTab = (key) => {
    activeTab = key;
    FE.store.set(TAB_KEY, key);
    for (const [k] of TABS) {
      panes[k].hidden = k !== key;
      tabBtns[k].className = tabClass(k === key);
      tabBtns[k].setAttribute("aria-selected", k === key ? "true" : "false");
    }
  };
  const tabBar = h("div", { class: "tabs", role: "tablist" }, TABS.map(([k, label]) => {
    const btn = h("div", { class: tabClass(k === activeTab), role: "tab", onclick: () => selectTab(k) }, label);
    tabBtns[k] = btn;
    return btn;
  }));
  selectTab(activeTab);

  const head = h("div", { class: "page-head-sticky" },
    h("h1", { style: "margin:0" }, "設定"),
    h("span", { class: "grow" }),
    h("span", { class: "small muted" }, `存到 ${st.config_path || "（沒有設定檔）"}（自動備份 .bak）`),
    h("button", { class: "primary", onclick: save }, "儲存設定"));

  ctx.view(head, tabBar, paneAi, paneComfy, paneGeneral);
};
