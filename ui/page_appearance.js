"use strict";
/* 角色外型卡頁 v2：兩個提示詞（頭像／立繪）、表情勾選、每個角色自己出圖。#/book/<id>/appearance */
(function () {
  const EXPRESSIONS = [
    ["neutral", "平靜"],
    ["smile", "喜"],
    ["angry", "怒"],
    ["sad", "哀"],
    ["serious", "認真"],
    ["worried", "擔心"],
    ["surprised", "驚訝"],
  ];
  const EXPRESSION_LABEL = Object.fromEntries(EXPRESSIONS);
  const CATEGORY_OPTIONS = [
    ["", "全部"],
    ["art_form", "藝術形式"],
    ["interior", "內文插圖風格"],
    ["cover", "封面藝術風格"],
  ];
  const PROMPT_PLACEHOLDER = "英文標籤，逗號分隔；打 __ 可叫出 wildcard";
  const HEADSHOT_TITLE = "頭像（頸部以上：髮型髮色、眼型眼色、頭飾、耳環、項鍊、特殊特徵；不含表情）";
  const SPRITE_TITLE = "立繪（頸部以下：衣服、身體、動作）";
  // 漫畫線（D26）：外型卡的 t2c 人工值，bible 角色卡直接吃這幾段。
  const T2C_SECTIONS = [
    ["head", "頭：髮型髮色、眼型眼色、部位特徵；性別字與年齡一定要在這裡"],
    ["body", "身體：只放體型（slim build、muscular arms）"],
    ["upper", "上半身特徵（非服裝，如傷疤／刺青；無則留空）"],
    ["lower", "下半身特徵（非服裝；無則留空）"],
    ["accessory", "永久配件：眼鏡、髮夾、疤、痣（會隨服裝換的留在服裝）"],
  ];
  const T2C_OUTFIT_PARTS = [
    ["top", "上著"],
    ["bottom", "下著"],
    ["set", "整套：制服、圍裙、連身這類"],
  ];
  const T2C_HINT = "一行一個英文短語；動作詞（standing）與表情詞不要放進來";
  const MAX_STYLE_OPTIONS = 200;
  const SUGGESTION_LIMIT = 12;
  const SUGGESTION_PATTERN = /__([a-z0-9_]*)$/;

  // 進行中的任務追蹤器：換頁時一起收掉，遲到的回呼才不會改到已卸下的節點。
  const trackers = new Set();

  function closeTrackers() {
    for (const item of trackers) item.close();
    trackers.clear();
  }

  function imgUrl(base, file) {
    return `${base}/file?path=${encodeURIComponent(file)}`;
  }

  function jobPercent(job, running) {
    const progress = job && job.progress ? job.progress : null;
    const total = progress && progress.total ? progress.total : 0;
    if (!total) return running ? 0 : 100;
    return Math.round((100 * (progress.done || 0)) / total);
  }

  function assetLabel(asset) {
    if (asset.kind === "sprite") return "立繪";
    if (asset.kind === "headshot") {
      const label = EXPRESSION_LABEL[asset.expr] || asset.expr || "";
      return label ? `頭像・${label}` : "頭像";
    }
    return asset.kind || "素材";
  }

  /* ---------------------------------------------------------------- wildcard 自動補全 */

  function attachAutocomplete(textarea, getNames) {
    const list = h("div", {
      style:
        "position:absolute;left:0;right:0;top:100%;z-index:50;display:none;" +
        "background:var(--bg);border:1px solid var(--line,#444);border-radius:6px;" +
        "max-height:220px;overflow:auto",
    });
    const wrap = h("div", { style: "position:relative" }, textarea, list);
    let items = [];
    let active = 0;
    let suppressNext = false;

    const close = () => {
      items = [];
      list.replaceChildren();
      list.style.display = "none";
    };
    const isOpen = () => items.length > 0;

    const paint = () => {
      list.replaceChildren(
        ...items.map((name, index) =>
          h(
            "div",
            {
              style: `padding:4px 8px;cursor:pointer${index === active ? ";background:var(--sel,#333)" : ""}`,
              onmousedown: (event) => {
                event.preventDefault();
                accept(index);
              },
            },
            `__${name}__`
          )
        )
      );
    };

    const open = () => {
      const value = textarea.value || "";
      const caret = typeof textarea.selectionStart === "number" ? textarea.selectionStart : value.length;
      const match = SUGGESTION_PATTERN.exec(value.slice(0, caret));
      if (!match) return close();
      const prefix = match[1];
      const names = getNames()
        .filter((name) => name.startsWith(prefix))
        .sort();
      if (!names.length) return close();
      items = names.slice(0, SUGGESTION_LIMIT);
      active = 0;
      paint();
      list.style.display = "block";
    };

    const accept = (index) => {
      const name = items[index];
      if (!name) return close();
      const value = textarea.value || "";
      const caret = typeof textarea.selectionStart === "number" ? textarea.selectionStart : value.length;
      const match = SUGGESTION_PATTERN.exec(value.slice(0, caret));
      if (!match) return close();
      const start = caret - match[0].length;
      const token = `__${name}__`;
      textarea.value = value.slice(0, start) + token + value.slice(caret);
      const position = start + token.length;
      if (typeof textarea.setSelectionRange === "function") {
        textarea.setSelectionRange(position, position);
      }
      if (typeof textarea.focus === "function") textarea.focus();
      close();
      suppressNext = true;
      textarea.dispatchEvent(new Event("input"));
    };

    textarea.addEventListener("input", () => {
      if (suppressNext) {
        suppressNext = false;
        return;
      }
      open();
    });
    textarea.addEventListener("keydown", (event) => {
      if (!isOpen()) return;
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        const step = event.key === "ArrowDown" ? 1 : -1;
        active = (active + step + items.length) % items.length;
        paint();
        return;
      }
      if (event.key === "Tab" || event.key === "Enter") {
        event.preventDefault();
        accept(active);
        return;
      }
      if (event.key === "Escape") {
        event.preventDefault();
        close();
      }
    });
    textarea.addEventListener("blur", () => setTimeout(close, 150));
    return wrap;
  }

  /* ---------------------------------------------------------------- 畫風 */

  function uploadStyles(base, file) {
    if (typeof FE.upload !== "function") {
      throw new Error("這個版本還沒有上傳功能（頁面缺少 FE.upload），請重新整理或回報");
    }
    return FE.upload(`${base}/styles/upload`, file);
  }

  function stylePane(state) {
    const badgeHost = h("span", {});
    const detail = h("div", { class: "small muted" });
    const controls = h("div", { class: "row", style: "align-items:center;gap:8px" });

    const categorySelect = h(
      "select",
      { onchange: () => paintOptions() },
      ...CATEGORY_OPTIONS.map(([value, label]) =>
        h("option", { value, selected: value === "" }, label)
      )
    );
    const searchInput = h("input", {
      placeholder: "搜尋畫風（名稱或標籤）",
      style: "width:220px",
      oninput: () => paintOptions(),
    });
    const listSelect = h("select", {
      size: 8,
      style: "width:100%",
      onchange: () => paintDetail(),
    });

    const styleName = (style) =>
      (style.names && (style.names["zh-TW"] || style.names.ja || style.names.en)) || style.style_id;

    const paintBadge = () => {
      const current = state.styles.find((style) => style.style_id === state.current);
      badgeHost.replaceChildren(badge(current ? styleName(current) : "未套用", current ? "ok" : ""));
    };

    const filtered = () => {
      const category = categorySelect.value;
      const query = searchInput.value.trim().toLowerCase();
      return state.styles.filter((style) => {
        if (category && style.category !== category) return false;
        if (!query) return true;
        const names = style.names || {};
        const haystack = `${names["zh-TW"] || ""} ${names.ja || ""} ${names.en || ""} ${style.tags || ""}`;
        return haystack.toLowerCase().includes(query);
      });
    };

    const paintDetail = () => {
      const current = state.styles.find((style) => style.style_id === listSelect.value);
      if (!current) {
        detail.replaceChildren("");
        return;
      }
      detail.replaceChildren(h("div", {}, current.tags || ""), h("div", {}, current.description || ""));
    };

    const paintOptions = () => {
      if (!state.styles.length) return;
      const rows = filtered().slice(0, MAX_STYLE_OPTIONS);
      const previous = listSelect.value;
      if (!rows.length) {
        listSelect.replaceChildren(
          h("option", { value: "", disabled: true, selected: true }, "沒有符合的畫風")
        );
        paintDetail();
        return;
      }
      listSelect.replaceChildren(
        ...rows.map((style) =>
          h("option", { value: style.style_id, selected: style.style_id === previous }, styleName(style))
        )
      );
      paintDetail();
    };

    const applyButton = h("button", {}, "套用");
    applyButton.addEventListener("click", async () => {
      const styleId = listSelect.value;
      if (!styleId) return toast("請先選一款畫風");
      try {
        const data = await api(`${state.base}/styles/apply`, {
          method: "POST",
          body: { style_id: styleId },
        });
        state.current = data.style_id || styleId;
        paintBadge();
        toast("已套用畫風");
        if (data.dropped && data.dropped.length) {
          toast(`略過了不適合的標籤：${data.dropped.join("、")}`);
        }
      } catch (error) {
        fail(error);
      }
    });

    const clearButton = h("button", { class: "ghost" }, "清除");
    clearButton.addEventListener("click", async () => {
      try {
        const data = await api(`${state.base}/styles/clear`, { method: "POST", body: {} });
        state.current = data.style_id || "";
        paintBadge();
        toast("已清除畫風");
      } catch (error) {
        fail(error);
      }
    });

    const fileInput = h("input", { type: "file", accept: ".xlsx,.xlsm" });
    const uploadButton = h("button", {}, "匯入畫風表");
    uploadButton.addEventListener("click", async () => {
      const file = fileInput.files && fileInput.files[0];
      if (!file) return toast("請先選擇畫風表檔案");
      try {
        const data = await uploadStyles(state.base, file);
        toast(`已匯入 ${data.count} 款`);
        const fresh = await api(`${state.base}/styles`);
        state.styles = fresh.styles || [];
        state.current = fresh.current || "";
        paintBadge();
        paintLibrary();
      } catch (error) {
        fail(error);
      }
    });

    const paintLibrary = () => {
      if (!state.styles.length) {
        controls.replaceChildren(
          h("div", { class: "muted" }, "還沒有畫風庫，請先選擇畫風表（.xlsx）匯入")
        );
        detail.replaceChildren("");
        return;
      }
      controls.replaceChildren(
        categorySelect,
        searchInput,
        applyButton,
        clearButton,
        h("div", { style: "width:100%" }, listSelect)
      );
      paintOptions();
    };

    const card = h("div", { class: "card" });
    card.append(
      h(
        "div",
        { class: "row", style: "align-items:center;gap:8px" },
        h("h2", { style: "margin:0" }, "畫風"),
        badgeHost
      ),
      controls,
      detail,
      h("div", { class: "row", style: "align-items:center;gap:8px;margin-top:10px" }, fileInput, uploadButton)
    );
    paintBadge();
    paintLibrary();
    return card;
  }

  /* ---------------------------------------------------------------- wildcard 啟用清單 */

  function wildcardPane(state) {
    const summary = h("summary", {});
    const filterInput = h("input", {
      placeholder: "篩選 wildcard 名稱",
      style: "width:240px;margin:8px 0",
    });
    const rowsHost = h("div", {});
    const saveButton = h("button", {}, "儲存啟用清單");
    const details = h(
      "details",
      {},
      summary,
      filterInput,
      h("div", { class: "row" }, saveButton),
      rowsHost
    );

    if (!state.wildcards.length) {
      rowsHost.replaceChildren(h("div", { class: "muted" }, "沒有 wildcard 檔（放在 vendor/wildcards/）"));
      summary.textContent = "Wildcard 啟用清單（已啟用 0／共 0）";
      saveButton.disabled = true;
      return details;
    }

    const checkboxes = new Map();
    const paintSummary = () => {
      const enabled = state.wildcards.filter((item) => item.enabled).length;
      summary.textContent = `Wildcard 啟用清單（已啟用 ${enabled}／共 ${state.wildcards.length}）`;
    };
    const paintRows = () => {
      const query = filterInput.value.trim().toLowerCase();
      rowsHost.replaceChildren(
        ...state.wildcards.map((item) => {
          let box = checkboxes.get(item.name);
          if (!box) {
            box = h("input", { type: "checkbox" });
            checkboxes.set(item.name, box);
          }
          box.checked = !!item.enabled;
          const sample = (item.sample || []).join("；");
          const row = h(
            "div",
            { class: "row", style: "align-items:center;gap:8px;margin:2px 0" },
            box,
            h("code", {}, `__${item.name}__`),
            h("span", { class: "small muted" }, `${item.count} 個選項，例如：${sample}`)
          );
          row.hidden = !!query && !item.name.toLowerCase().includes(query);
          return row;
        })
      );
    };

    saveButton.addEventListener("click", async () => {
      const names = state.wildcards
        .filter((item) => {
          const box = checkboxes.get(item.name);
          return box && box.checked;
        })
        .map((item) => item.name);
      try {
        const data = await api(`${state.base}/wildcards/enabled`, {
          method: "PUT",
          body: { names },
        });
        state.wildcards = data.wildcards || [];
        paintRows();
        paintSummary();
        toast("已儲存");
      } catch (error) {
        fail(error);
      }
    });
    filterInput.addEventListener("input", () => paintRows());

    paintRows();
    paintSummary();
    return details;
  }

  /* ---------------------------------------------------------------- 角色卡 */

  function characterCard(state, character) {
    const source = character.card || {};
    let confirmed = !!source.confirmed;
    let hasCard = !!character.has_card;
    const enabledNames = () =>
      state.wildcards.filter((item) => item.enabled).map((item) => item.name);

    const headshotInput = h("textarea", {
      rows: 3,
      style: "width:100%",
      placeholder: PROMPT_PLACEHOLDER,
      value: source.headshot_prompt || "",
    });
    const spriteInput = h("textarea", {
      rows: 3,
      style: "width:100%",
      placeholder: PROMPT_PLACEHOLDER,
      value: source.sprite_prompt || "",
    });
    if (window.attachTagComplete) {
      window.attachTagComplete(headshotInput);
      window.attachTagComplete(spriteInput);
    }
    const notesInput = h("textarea", {
      rows: 2,
      style: "width:100%",
      placeholder: "備註（AI 對沒有依據之處的提醒會出現在這裡）",
      value: source.notes || "",
    });
    /* ---- t2c 人工值（漫畫線 D26）：bible 角色卡直接吃 sections 與 outfits.default */
    const t2cSource = source.t2c && typeof source.t2c === "object" ? source.t2c : {};
    const t2cSections = t2cSource.sections && typeof t2cSource.sections === "object" ? t2cSource.sections : {};
    const t2cOutfit = (t2cSource.outfits && t2cSource.outfits.default) || {};
    const toText = (items) => (Array.isArray(items) ? items.join("\n") : "");
    const toLines = (text) =>
      String(text || "")
        .split("\n")
        .map((line) => line.trim())
        .filter((line, index, all) => line && all.indexOf(line) === index);
    const t2cInput = (table, key) => {
      const ta = h("textarea", { rows: 2, style: "width:100%", placeholder: T2C_HINT, value: toText(table[key]) });
      if (window.attachTagComplete) window.attachTagComplete(ta, { sep: "\n" });
      return ta;
    };
    const sectionInputs = T2C_SECTIONS.map(([key, label]) => {
      const input = t2cInput(t2cSections, key);
      return { key, input, node: h("div", { style: "margin-top:6px" }, h("div", { class: "small" }, label), input) };
    });
    const outfitInputs = T2C_OUTFIT_PARTS.map(([key, label]) => {
      const input = t2cInput(t2cOutfit, key);
      return { key, input, node: h("div", { style: "margin-top:6px" }, h("div", { class: "small" }, label), input) };
    });
    let shaOverride = "";
    let checkData = null;

    const t2cPayload = () => {
      const sections = {};
      for (const item of sectionInputs) {
        const lines = toLines(item.input.value);
        if (lines.length) sections[item.key] = lines;
      }
      const outfit = {};
      for (const item of outfitInputs) {
        const lines = toLines(item.input.value);
        if (lines.length) outfit[item.key] = lines;
      }
      if (!Object.keys(sections).length && !Object.keys(outfit).length) return null;
      const t2c = {};
      if (Object.keys(sections).length) t2c.sections = sections;
      if (Object.keys(outfit).length) t2c.outfits = { default: outfit };
      // 第一次在介面上寫 t2c 也要有指紋，否則過期判斷永遠不會亮
      const sha = shaOverride || t2cSource.source_sha1 || (checkData && checkData.current_sha1);
      if (typeof sha === "string" && sha) t2c.source_sha1 = sha;
      return t2c;
    };

    const errorBadge = h("span", {});
    const warnBadge = h("span", {});
    const staleBadge = h("span", {});
    const sourceHost = h("div", { class: "small muted" });
    const issuesHost = h("div", {});
    const seedButton = h("button", { class: "sm" }, "用提示詞粗拆初稿");
    const staleButton = h("button", { class: "sm", hidden: true }, "重設指紋");

    const paintCheck = (data) => {
      const issues = (data && data.issues) || [];
      const errors = issues.filter((item) => item.level === "error");
      const warnings = issues.filter((item) => item.level !== "error");
      errorBadge.replaceChildren(badge(errors.length ? errors.length + " error" : "0 error", errors.length ? "err" : "ok"));
      warnBadge.replaceChildren(warnings.length ? badge(warnings.length + " warn", "warn") : "");
      staleBadge.replaceChildren(data && data.stale ? badge("人工值可能過期", "warn") : "");
      staleButton.hidden = !(data && data.stale);
      sourceHost.textContent =
        !data || data.source === "placeholder"
          ? "沒有外型卡：bible 卡是佔位卡，不列入檢查"
          : data.source === "derived"
            ? "還沒有人工值：bible 卡目前用提示詞的文字規則粗拆"
            : "bible 卡用外型卡的人工值（t2c）";
      issuesHost.replaceChildren(
        ...(data && data.stale
          ? [h("div", { class: "small muted" }, "提示詞改過之後 t2c 指的是舊提示詞；看過沒問題再按「重設指紋」。")]
          : []),
        ...issues.map((item) =>
          h(
            "div",
            { class: item.level === "error" ? "small bad" : "small" },
            (item.level === "error" ? "✖ " : "! ") + item.where + "：" + item.message
          )
        ),
        ...(issues.length ? [] : [h("div", { class: "small muted" }, "check_card 沒有問題")])
      );
    };

    const runCheck = async () => {
      try {
        const data = await api(`${state.base}/cards/${character.id}/check`);
        checkData = data;
        paintCheck(data);
      } catch (error) {
        fail(error);
      }
    };

    seedButton.addEventListener("click", async () => {
      const draft = (checkData && checkData.draft) || {};
      if (!draft.sections && !draft.outfits) return toast("沒有提示詞可以粗拆（檢查還沒回來，或這張卡還沒有外型卡）");
      const busy =
        sectionInputs.some((item) => item.input.value.trim()) ||
        outfitInputs.some((item) => item.input.value.trim());
      if (busy && !(await FE.confirm("會蓋掉目前的 t2c 內容，要填入粗拆初稿嗎？", { ok: "填入" }))) return;
      for (const item of sectionInputs) item.input.value = toText((draft.sections || {})[item.key]);
      for (const item of outfitInputs) item.input.value = toText(((draft.outfits || {}).default || {})[item.key]);
      toast("已填入粗拆初稿（還沒存檔）");
    });

    staleButton.addEventListener("click", async () => {
      if (!checkData) return;
      shaOverride = checkData.current_sha1 || "";
      try {
        await save(confirmed, true);
        toast("已用目前提示詞重設指紋並存檔");
      } catch (error) {
        fail(error);
      }
    });
    const statusHost = h("span", {});
    const evidenceHost = h("div", {});
    const thumbHost = h("div", { style: "width:100%;display:flex;flex-wrap:wrap;gap:16px;align-items:flex-start" });
    const thumbsWrap = h("div", { class: "row", style: "flex-wrap:wrap;gap:12px;margin-top:6px" }, thumbHost);

    const paintStatus = () => {
      statusHost.replaceChildren(
        hasCard ? (confirmed ? badge("已確認", "ok") : badge("草稿", "warn")) : badge("未建立", "")
      );
    };

    const expressionBoxes = EXPRESSIONS.map(([value, label]) => {
      const box = h("input", {
        type: "checkbox",
        checked: (source.expressions || []).includes(value),
      });
      return { value, box, node: h("label", { class: "row", style: "align-items:center;gap:4px" }, box, label) };
    });
    const checkedExpressions = () =>
      expressionBoxes.filter((item) => item.box.checked).map((item) => item.value);
    const neutralBox = expressionBoxes[0].box;
    for (const item of expressionBoxes) {
      item.box.addEventListener("change", () => {
        if (!checkedExpressions().length) {
          neutralBox.checked = true;
          toast("至少要一個表情");
        }
      });
    }

    const gather = () => {
      const payload = {
        char_id: character.id,
        confirmed,
        headshot_prompt: headshotInput.value.trim(),
        sprite_prompt: spriteInput.value.trim(),
        expressions: checkedExpressions(),
        notes: notesInput.value.trim(),
      };
      const t2c = t2cPayload();
      if (t2c) payload.t2c = t2c; // 沒有內容就不送，存檔時沿用舊檔的 t2c
      return payload;
    };
    const signature = () =>
      JSON.stringify([
        headshotInput.value.trim(),
        spriteInput.value.trim(),
        checkedExpressions(),
        notesInput.value.trim(),
        sectionInputs.map((item) => item.input.value.trim()),
        outfitInputs.map((item) => item.input.value.trim()),
        shaOverride,
      ]);
    let baseline = signature();
    const isDirty = () => signature() !== baseline;

    const save = async (asConfirmed, silent) => {
      const payload = gather();
      payload.confirmed = asConfirmed;
      const saved = await api(`${state.base}/cards/${character.id}`, {
        method: "PUT",
        body: payload,
      });
      confirmed = !!saved.confirmed;
      hasCard = true;
      baseline = signature();
      paintStatus();
      await runCheck();
      if (!silent) toast(`${asConfirmed ? "已確認" : "已存草稿"} ${character.label}`);
      return saved;
    };

    const applyPrompts = (data) => {
      if (data && typeof data.headshot_prompt === "string") headshotInput.value = data.headshot_prompt;
      if (data && typeof data.sprite_prompt === "string") spriteInput.value = data.sprite_prompt;
    };

    const draftButton = h("button", { class: "sm" }, "AI 草擬");
    draftButton.addEventListener("click", async () => {
      const original = draftButton.textContent;
      draftButton.disabled = true;
      draftButton.textContent = "AI 草擬中…";
      try {
        const body = {};
        const executor = Executors.value();
        if (executor) body.executor = executor;
        const data = await api(`${state.base}/cards/${character.id}/draft`, {
          method: "POST",
          body,
        });
        applyPrompts(data.card || {});
        if (data.card && data.card.t2c && typeof data.card.t2c === "object") {
          const t2c = data.card.t2c;
          for (const item of sectionInputs) item.input.value = toText((t2c.sections || {})[item.key]);
          for (const item of outfitInputs) item.input.value = toText(((t2c.outfits || {}).default || {})[item.key]);
          if (t2c.source_sha1) shaOverride = t2c.source_sha1;
        }
        if (data.card && typeof data.card.notes === "string" && data.card.notes.trim()) {
          notesInput.value = data.card.notes;
        }
        confirmed = false;
        hasCard = true;
        paintStatus();
        if (data.warnings && data.warnings.length) toast(data.warnings.join("；"));
        if (data.evidence && data.evidence.length) {
          evidenceHost.replaceChildren(
            h(
              "details",
              { class: "small" },
              h("summary", {}, `AI 參考的小說段落（${data.evidence.length}）`),
              ...data.evidence.map((item) =>
                h("div", {}, `[${item.chapter_id} ¶${item.para_no}] ${item.text}`)
              )
            )
          );
        }
        toast("草稿好了，請檢查後按「確認」");
      } catch (error) {
        fail(error);
      } finally {
        draftButton.disabled = false;
        draftButton.textContent = original;
      }
    });

    const rollButton = h("button", { class: "sm" }, "擲骰");
    rollButton.addEventListener("click", async () => {
      try {
        const data = await api(`${state.base}/cards/roll`, {
          method: "POST",
          body: { card: gather() },
        });
        applyPrompts(data.card || {});
        if (data.warnings && data.warnings.length) toast(data.warnings.join("；"));
        toast("已擲骰固定（還沒存檔）");
      } catch (error) {
        fail(error);
      }
    });

    const draftSaveButton = h("button", { class: "sm" }, "存草稿");
    draftSaveButton.addEventListener("click", async () => {
      try {
        await save(false);
      } catch (error) {
        fail(error);
      }
    });
    const confirmButton = h("button", { class: "sm" }, "確認");
    confirmButton.addEventListener("click", async () => {
      try {
        await save(true);
      } catch (error) {
        fail(error);
      }
    });

    /* ---- 出圖區塊 */
    const headshotBox = h("input", { type: "checkbox", checked: true });
    const spriteBox = h("input", { type: "checkbox", checked: (character.sprite_scenes || 0) > 0 });
    const countInput = h("input", { type: "number", min: 1, max: 16, value: 1, style: "width:64px" });
    const generateButton = h("button", {}, "出圖");
    const progressTitle = h("strong", {});
    const barFill = h("i", { style: "width:0%" });
    const progressLine = h("div", { class: "small" });
    const progressDetails = h("div", { class: "small" });
    const progressError = h("div", { class: "bad small", hidden: true });
    const stopButton = h("button", { class: "sm", hidden: true }, "■ 停止");
    const progressCard = h(
      "div",
      { class: "card", hidden: true },
      h("div", { class: "row" }, progressTitle, h("span", { class: "grow" }), stopButton),
      h("div", { class: "bar" }, barFill),
      progressLine,
      progressDetails,
      progressError
    );
    let tracker = null;

    const paintProgress = (job) => {
      barFill.style.width = `${jobPercent(job, true)}%`;
      progressLine.textContent = job.note || "準備中…";
    };

    const paintFinished = (job) => {
      const status = job.status;
      progressTitle.textContent =
        status === "done" ? "出圖完成" : status === "stopped" ? "已停止" : "結束（有錯誤）";
      barFill.style.width = `${jobPercent(job, false)}%`;
      stopButton.hidden = true;
      const summary = job.result || {};
      const generated = summary.generated || [];
      const skipped = summary.skipped || [];
      const failed = summary.failed || {};
      const failedIds = Object.keys(failed);
      progressLine.textContent = `新出圖 ${generated.length} 項，沒變動 ${skipped.length} 項`;
      progressDetails.replaceChildren(
        ...failedIds.map((id) => h("div", { class: "bad" }, `${id}：${failed[id]}`))
      );
    };

    const paintThumbs = () => {
      const assets = character.assets || [];
      if (!assets.length) {
        thumbHost.replaceChildren(
          h("div", { class: "small muted", style: "width:100%" }, "還沒有素材項目（到「素材清單」按「掃描場景建立清單」）")
        );
      } else {
        thumbHost.replaceChildren(
          ...assets.map((asset) =>
            h(
              "div",
              {},
              h(
                "div",
                { class: "row", style: "align-items:center;gap:6px" },
                badge(assetLabel(asset)),
                asset.candidates && asset.candidates.length
                  ? ""
                  : h("span", { class: "small muted" }, "還沒出圖")
              ),
              asset.candidates && asset.candidates.length
                ? h(
                    "div",
                    { class: "row", style: "gap:6px;flex-wrap:wrap;margin-top:4px" },
                    ...asset.candidates.map((candidate) => {
                      const width = asset.kind === "sprite" ? 72 : 96;
                      const height = asset.kind === "sprite" ? 168 : 128;
                      return h("img", {
                        src: imgUrl(state.base, candidate.file),
                        title: candidate.name,
                        loading: "lazy",
                        style:
                          `width:${width}px;height:${height}px;object-fit:cover;border-radius:6px;` +
                          `cursor:zoom-in${candidate.selected ? ";outline:3px solid var(--ok)" : ""}`,
                        onclick: () => window.open(imgUrl(state.base, candidate.file), "_blank"),
                      });
                    })
                  )
                : ""
            )
          )
        );
      }
      thumbHost.append(
        h("div", { class: "small muted", style: "width:100%;margin-top:6px" }, "挑選與刪除請到「素材清單」")
      );
    };

    const reloadAssets = async () => {
      try {
        const data = await api(`${state.base}/cards`);
        const fresh = (data.characters || []).find((item) => item.id === character.id);
        if (fresh) {
          character.assets = fresh.assets || [];
          if (typeof fresh.sprite_scenes === "number") character.sprite_scenes = fresh.sprite_scenes;
        }
        paintThumbs();
      } catch (error) {
        fail(error);
      }
    };

    const closeTracker = () => {
      const active = tracker;
      tracker = null;
      if (!active) return;
      trackers.delete(active);
      active.close();
    };

    const watchJob = (jobId) => {
      closeTracker();
      const watcher = FE.jobs.watch(jobId, {
        base: "/api/fe-jobs",
        onupdate: (job) => {
          progressError.hidden = true;
          if (job.status === "running" || job.status === "queued") paintProgress(job);
        },
        onconn: (state, error) => {
          if (state !== "retrying" && state !== "gone") return;
          progressError.textContent = (error && error.message) || "任務連線中斷，重試中";
          progressError.hidden = false;
        },
        onend: async (job) => {
          tracker = null;
          trackers.delete(watcher);
          paintFinished(job);
          generateButton.disabled = false;
          await reloadAssets();
        },
      });
      tracker = watcher;
      trackers.add(watcher);
    };

    stopButton.addEventListener("click", async () => {
      if (!tracker) return;
      try {
        await tracker.stop();
      } catch (error) {
        fail(error);
      }
    });

    generateButton.addEventListener("click", async () => {
      if (!headshotBox.checked && !spriteBox.checked) {
        return toast("請至少勾選頭像或立繪");
      }
      const count = Number(countInput.value);
      if (!Number.isInteger(count) || count < 1 || count > 16) {
        return toast("張數必須是 1 到 16 的整數");
      }
      if (isDirty()) {
        try {
          await save(confirmed, true);
        } catch (error) {
          fail(error);
          return;
        }
      }
      try {
        const job = await api(`${state.base}/cards/generate`, {
          method: "POST",
          body: {
            char_id: character.id,
            headshot: headshotBox.checked,
            sprite: spriteBox.checked,
            candidates: count,
          },
        });
        Panel.attach(job.id);
        generateButton.disabled = true;
        progressCard.hidden = false;
        progressTitle.textContent = "出圖中";
        progressLine.textContent = "";
        progressDetails.replaceChildren();
        progressError.hidden = true;
        barFill.style.width = "0%";
        stopButton.hidden = false;
        watchJob(job.id);
      } catch (error) {
        fail(error);
      }
    });

    paintStatus();
    paintThumbs();
    runCheck();

    return h(
      "div",
      { class: "card" },
      h(
        "div",
        { class: "row", style: "align-items:center;gap:8px" },
        h("strong", {}, character.label),
        h("code", { class: "small" }, character.id),
        statusHost,
        h("span", { class: "grow" }),
        draftButton,
        rollButton,
        draftSaveButton,
        confirmButton
      ),
      character.card_text
        ? h("details", { class: "small" }, h("summary", {}, "設定卡「外在」"), h("div", {}, character.card_text))
        : "",
      h("div", { class: "small", style: "margin-top:8px" }, HEADSHOT_TITLE),
      attachAutocomplete(headshotInput, enabledNames),
      h("div", { class: "small", style: "margin-top:8px" }, SPRITE_TITLE),
      attachAutocomplete(spriteInput, enabledNames),
      h(
        "div",
        { class: "row", style: "align-items:center;gap:8px;margin-top:8px" },
        h("strong", {}, "頭像表情"),
        ...expressionBoxes.map((item) => item.node),
        h("span", { class: "small muted" }, "勾幾個就出幾張頭像，同一角色用同一個種子")
      ),
      h("div", { style: "margin-top:8px" }, notesInput),
      h(
        "div",
        { class: "row", style: "align-items:center;gap:8px;margin-top:12px" },
        h("strong", {}, "漫畫卡檢查"),
        errorBadge,
        warnBadge,
        staleBadge,
        h("span", { class: "grow" }),
        seedButton,
        staleButton
      ),
      sourceHost,
      issuesHost,
      h(
        "details",
        { style: "margin-top:8px" },
        h("summary", { class: "small" }, "編輯 t2c 人工值（sections ＋ outfits.default）"),
        h("div", { class: "small muted", style: "margin-top:6px" }, T2C_HINT),
        ...sectionInputs.map((item) => item.node),
        h("div", { class: "small", style: "margin-top:10px" }, "服裝 outfits.default"),
        ...outfitInputs.map((item) => item.node)
      ),
      h(
        "div",
        { class: "row", style: "align-items:center;gap:10px;margin-top:10px" },
        h("strong", {}, "出圖"),
        h("label", { class: "row", style: "align-items:center;gap:4px" }, headshotBox, "頭像"),
        h("label", { class: "row", style: "align-items:center;gap:4px" }, spriteBox, "立繪"),
        (character.sprite_scenes || 0) > 0
          ? ""
          : h("span", { class: "small muted" }, "劇情沒用到這個角色的立繪"),
        h("label", { class: "row", style: "align-items:center;gap:4px" }, "張數", countInput),
        generateButton
      ),
      progressCard,
      evidenceHost,
      h("div", { class: "row", style: "align-items:center;gap:8px;margin-top:10px" }, h("strong", {}, "已出的圖")),
      thumbsWrap
    );
  }

  /* ---------------------------------------------------------------- render */

  async function render(bookId, opts = {}) {
    const base = `/api/books/${bookId}/art`;
    const [cardsData, wildcardsData, stylesData] = await Promise.all([
      api(`${base}/cards`),
      api(`${base}/wildcards`),
      api(`${base}/styles`),
    ]);

    const state = {
      bookId,
      base,
      characters: cardsData.characters || [],
      wildcards: wildcardsData.wildcards || [],
      styles: stylesData.styles || [],
      current: stylesData.current || "",
    };

    const SEL_KEY = `appearance_sel_${bookId}`;
    let filter = "all";
    let selectedId = (opts && opts.charId) || FE.store.get(SEL_KEY, "") || "";
    if (!state.characters.some((c) => c.id === selectedId)) {
      selectedId = state.characters.length ? state.characters[0].id : "";
    }
    const checkedIds = new Set();
    let batchRunning = false;
    let batchStop = false;

    const listHost = h("div", { class: "md-list" });
    const detailHost = h("div", { class: "md-detail" });
    const filterHost = h("div", { class: "row", style: "gap:6px" });
    const batchHost = h("div", { class: "row wrap", style: "gap:6px;align-items:center" });
    const batchStatus = h("span", { class: "small muted" });

    const runBatchDraft = async (targets) => {
      if (batchRunning || !targets.length) return;
      batchRunning = true;
      batchStop = false;
      let ok = 0;
      let failed = 0;
      drawBatchBar();
      const executor = Executors.value();
      for (let i = 0; i < targets.length; i++) {
        if (batchStop) break;
        const c = targets[i];
        batchStatus.textContent = `AI 草擬中（${i + 1}/${targets.length}）：${c.label || c.id}…`;
        try {
          const body = executor ? { executor } : {};
          const data = await api(`${state.base}/cards/${c.id}/draft`, { method: "POST", body });
          if (data && data.card) {
            c.card = data.card;
            c.has_card = true;
          }
          checkedIds.delete(c.id);
          ok += 1;
          selectedId = c.id;
          drawMasterDetail();
        } catch (error) {
          failed += 1;
          toast(`${c.label || c.id} 草擬失敗：${error.message}`, "bad");
        }
      }
      batchRunning = false;
      batchStatus.textContent = batchStop
        ? `已停止（完成 ${ok}${failed ? `、失敗 ${failed}` : ""}）`
        : `批次草擬完成：成功 ${ok}${failed ? `、失敗 ${failed}` : ""}`;
      toast(batchStatus.textContent, failed ? "warn" : "ok");
      drawBatchBar();
      drawMasterDetail();
    };

    const drawBatchBar = () => {
      const missing = state.characters.filter((c) => !c.has_card);
      const picked = state.characters.filter((c) => checkedIds.has(c.id));
      const parts = [];
      if (missing.length) {
        parts.push(h("button", {
          class: "sm ghost", disabled: batchRunning,
          onclick: () => { for (const c of missing) checkedIds.add(c.id); drawMasterDetail(); },
        }, `勾選缺卡（${missing.length}）`));
      }
      if (picked.length) {
        parts.push(h("button", {
          class: "sm primary", disabled: batchRunning,
          onclick: () => runBatchDraft(picked),
        }, `逐一 AI 草擬已勾選（${picked.length}）`));
        parts.push(h("button", {
          class: "sm ghost", disabled: batchRunning,
          onclick: () => { checkedIds.clear(); drawMasterDetail(); },
        }, "清除勾選"));
      } else if (missing.length) {
        parts.push(h("button", {
          class: "sm", disabled: batchRunning,
          onclick: () => runBatchDraft(missing),
        }, `全部缺卡 AI 草擬（${missing.length}）`));
      }
      if (batchRunning) {
        parts.push(h("button", {
          class: "sm ghost",
          onclick: () => { batchStop = true; batchStatus.textContent = "等待目前角色完成後停止…"; },
        }, "停止"));
      }
      parts.push(batchStatus);
      batchHost.replaceChildren(...parts);
    };

    const drawMasterDetail = () => {
      const visible = state.characters.filter((c) => {
        const confirmed = !!(c.card && c.card.confirmed);
        if (filter === "confirmed") return confirmed;
        if (filter === "draft") return !confirmed;
        return true;
      });
      if (visible.length && !visible.some((c) => c.id === selectedId)) {
        selectedId = visible[0].id;
      }
      filterHost.replaceChildren(
        ...[["all", `全部（${state.characters.length}）`], ["confirmed", "已確認"], ["draft", "待確認"]].map(([k, label]) =>
          h("button", { class: filter === k ? "sm primary" : "sm ghost", onclick: () => { filter = k; drawMasterDetail(); } }, label))
      );
      drawBatchBar();
      if (!visible.length) {
        listHost.replaceChildren(h("div", { class: "small muted", style: "padding:10px" }, "沒有符合的角色"));
        detailHost.replaceChildren(h("div", { class: "card muted" }, state.characters.length ? "切換篩選條件或從左側選擇角色。" : "還沒有角色資料。請先「從書庫同步視覺卡」或到「章節與校對」執行抽取。"));
        return;
      }
      listHost.replaceChildren(...visible.map((c) => {
        const confirmed = !!(c.card && c.card.confirmed);
        const hasCustomExpr = !!(c.card && c.card.expression_prompts && Object.keys(c.card.expression_prompts).length);
        const active = c.id === selectedId;
        const pickBox = h("input", {
          type: "checkbox",
          checked: checkedIds.has(c.id),
          disabled: batchRunning,
          title: "勾選以批次 AI 草擬",
          onclick: (e) => e.stopPropagation(),
          onchange: (e) => {
            if (e.target.checked) checkedIds.add(c.id);
            else checkedIds.delete(c.id);
            drawBatchBar();
          },
        });
        return h("div", {
          class: "md-item" + (active ? " active" : ""),
          onclick: () => { selectedId = c.id; FE.store.set(SEL_KEY, c.id); drawMasterDetail(); },
        },
          h("div", { class: "row", style: "gap:6px;justify-content:space-between;align-items:center" },
            h("span", { class: "row", style: "gap:6px;align-items:center" },
              pickBox,
              h("strong", { class: "small" }, c.label || c.id)),
            badge(confirmed ? "已確認" : (c.has_card ? "待確認" : "缺卡"), confirmed ? "ok" : (c.has_card ? "" : "warn"))),
          h("div", { class: "small muted", style: "padding-left:20px" }, c.id + (hasCustomExpr ? " · 專屬表情" : "")));
      }));
      const chosen = visible.find((c) => c.id === selectedId) || visible[0];
      detailHost.replaceChildren(characterCard(state, chosen));
    };

    const syncBar = () => {
      if (!cardsData.has_library) return "";
      const overwriteBox = h("input", { type: "checkbox" });
      const statusOut = h("span", { class: "small muted" });
      const syncBtn = h("button", { class: "sm" }, "從書庫同步視覺卡");
      syncBtn.onclick = async () => {
        syncBtn.disabled = true;
        statusOut.textContent = "同步中…";
        try {
          const r = await api(`${base}/cards/sync-library`, {
            method: "POST",
            body: { overwrite: overwriteBox.checked },
          });
          let msg = `視覺卡 ${r.found} 張：新增 ${r.created.length}、更新 ${r.updated.length}、跳過 ${r.skipped.length}`;
          if (r.cast_added.length) msg += `；角色表新增 ${r.cast_added.length}`;
          if (r.missing_visual_cards && r.missing_visual_cards.length) {
            const preview = r.missing_visual_cards.slice(0, 4).join("、") + (r.missing_visual_cards.length > 4 ? "…" : "");
            msg += `（書庫共 ${r.library_characters} 位角色，另有 ${r.missing_visual_cards.length} 位尚未建視覺卡：${preview}）`;
          }
          if (r.comic_projects_synced && r.comic_projects_synced.length) {
            msg += `；同步漫畫專案 ${r.comic_projects_synced.length} 個`;
          }
          if (r.errors.length) msg += `；錯誤 ${r.errors.length}`;
          statusOut.textContent = msg;
          toast(msg, r.errors.length ? "bad" : "ok");
          if (r.created.length || r.updated.length || r.cast_added.length) {
            FE.route();
          }
        } catch (error) {
          statusOut.textContent = "✖ " + error.message;
          fail(error);
        } finally {
          syncBtn.disabled = false;
        }
      };
      return h(
        "div",
        { class: "row", style: "align-items:center;gap:8px;flex-wrap:wrap" },
        syncBtn,
        h("label", { class: "row small", style: "align-items:center;gap:4px" }, overwriteBox, "覆蓋既有（先備份）"),
        statusOut
      );
    };

    const toolbar = h("div", { class: "card", style: "padding:10px 14px" },
      h("div", { class: "row wrap", style: "align-items:center;justify-content:space-between;gap:10px" },
        h("div", { class: "row wrap", style: "gap:8px;align-items:center" },
          filterHost,
          h("a", { class: "btn sm", href: `#/book/${encodeURIComponent(bookId)}/cast` }, "編輯角色名冊（表單）")),
        syncBar()),
      h("div", { style: "margin-top:8px;padding-top:8px;border-top:1px solid var(--line)" }, batchHost));

    const toolsDrawer = h("details", { class: "card", style: "padding:10px 14px" },
      h("summary", { class: "small", style: "cursor:pointer;font-weight:600" }, "畫風庫與 Wildcard 設定（進階）"),
      h("div", { style: "margin-top:10px" }, stylePane(state), wildcardPane(state)));

    drawMasterDetail();
    return h("div", {}, toolbar, toolsDrawer, h("div", { class: "master-detail" }, listHost, detailHost));
  }

  FE.PAGES["book/:id/appearance"] = async (ctx) => {
    const bookId = ctx.params.id;
    const wantedChar = (ctx.query && ctx.query.char) || "";
    closeTrackers();
    ctx.signal.addEventListener("abort", closeTrackers, { once: true });
    ctx.crumbs([
      { label: "書架", href: "#/" },
      { label: bookId, href: `#/book/${bookId}` },
      { label: "角色與外觀" },
    ]);
    ctx.view(h("div", { class: "muted" }, "載入中…"));
    try {
      ctx.view(await render(bookId, { charId: wantedChar }));
    } catch (error) {
      ctx.view(h("div", { class: "card bad" }, error.message));
    }
  };

  window.AppearanceCards = { render };
})();
