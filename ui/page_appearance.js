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
  const POLL_MS = 1500;
  const MAX_STYLE_OPTIONS = 200;
  const SUGGESTION_LIMIT = 12;
  const SUGGESTION_PATTERN = /__([a-z0-9_]*)$/;

  function imgUrl(base, file) {
    return `${base}/file?path=${encodeURIComponent(file)}`;
  }

  function jobPercent(current, running) {
    const total = current && current.total ? current.total : 0;
    if (!total) return running ? 0 : 100;
    const index = (current && current.index) || 1;
    const bonus = current && current.event === "asset_done" ? 1 : 0;
    return Math.round((100 * (index - 1 + bonus)) / total);
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
    if (typeof uploadFile !== "function") {
      throw new Error("這個版本還沒有上傳功能（頁面缺少 uploadFile），請重新整理或回報");
    }
    return uploadFile(`${base}/styles/upload`, file);
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
    const notesInput = h("textarea", {
      rows: 2,
      style: "width:100%",
      placeholder: "備註（AI 對沒有依據之處的提醒會出現在這裡）",
      value: source.notes || "",
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

    const gather = () => ({
      char_id: character.id,
      confirmed,
      headshot_prompt: headshotInput.value.trim(),
      sprite_prompt: spriteInput.value.trim(),
      expressions: checkedExpressions(),
      notes: notesInput.value.trim(),
    });
    const signature = () =>
      JSON.stringify([
        headshotInput.value.trim(),
        spriteInput.value.trim(),
        checkedExpressions(),
        notesInput.value.trim(),
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
        const executor = AiPanel.executor();
        if (executor) body.executor = executor;
        const data = await api(`${state.base}/cards/${character.id}/draft`, {
          method: "POST",
          body,
        });
        applyPrompts(data.card || {});
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
    let timer = null;
    let jobId = "";

    const paintProgress = (current) => {
      barFill.style.width = `${jobPercent(current, true)}%`;
      const asset = current && current.asset ? current.asset : "";
      if (!asset) {
        progressLine.textContent = "準備中…";
        return;
      }
      const total = current.total || 0;
      let text = `${asset}（第 ${current.index}/${total} 項`;
      if (current.of) text += `，這項第 ${current.n}/${current.of} 張`;
      progressLine.textContent = `${text}）`;
    };

    const paintFinished = (job) => {
      const status = job.status;
      progressTitle.textContent =
        status === "done" ? "出圖完成" : status === "stopped" ? "已停止" : "結束（有錯誤）";
      barFill.style.width = `${jobPercent(job.current || {}, false)}%`;
      stopButton.hidden = true;
      const summary = job.summary || {};
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

    const tick = async () => {
      let job;
      try {
        job = await api(`/api/jobs/${jobId}`);
      } catch (error) {
        progressError.textContent = error.message || String(error);
        progressError.hidden = false;
        timer = setTimeout(tick, POLL_MS);
        return;
      }
      progressError.hidden = true;
      if (job.status === "running") {
        paintProgress(job.current || {});
        timer = setTimeout(tick, POLL_MS);
        return;
      }
      paintFinished(job);
      generateButton.disabled = false;
      await reloadAssets();
    };

    stopButton.addEventListener("click", async () => {
      try {
        await api(`/api/jobs/${jobId}/stop`, { method: "POST", body: {} });
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
        AiPanel.poll();
        jobId = job.id;
        generateButton.disabled = true;
        progressCard.hidden = false;
        progressTitle.textContent = "出圖中";
        progressLine.textContent = "";
        progressDetails.replaceChildren();
        progressError.hidden = true;
        barFill.style.width = "0%";
        stopButton.hidden = false;
        if (timer) clearTimeout(timer);
        timer = setTimeout(tick, POLL_MS);
      } catch (error) {
        fail(error);
      }
    });

    paintStatus();
    paintThumbs();

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

  async function render(bookId) {
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

    const charactersHost = h("div", {});
    const root = h("div", {});
    root.append(
      h(
        "div",
        { class: "card small muted" },
        "流程：AI 草擬 → 你修改 → 確認 → 在角色卡上按出圖。頭像框放頸部以上（髮型髮色、眼型眼色、頭飾、耳環、項鍊、特殊特徵），立繪框放衣服、身體與動作；兩框都可以打 __ 叫出 wildcard 名單。"
      ),
      stylePane(state),
      wildcardPane(state),
      charactersHost
    );

    if (!state.characters.length) {
      charactersHost.replaceChildren(h("div", { class: "muted" }, "還沒有標註好的角色"));
    } else {
      charactersHost.append(h("h2", {}, `角色（${state.characters.length}）`));
      charactersHost.append(...state.characters.map((character) => characterCard(state, character)));
    }
    return root;
  }

  PAGES.appearance = async function (bookId) {
    crumbs([
      { label: "書架", href: "#/" },
      { label: bookId, href: `#/book/${bookId}` },
      { label: "角色外型卡" },
    ]);
    $("#view").replaceChildren(h("div", { class: "muted" }, "載入中…"));
    try {
      $("#view").replaceChildren(await render(bookId));
    } catch (error) {
      $("#view").replaceChildren(h("div", { class: "card bad" }, error.message));
    }
  };

  window.AppearanceCards = { render };
})();
