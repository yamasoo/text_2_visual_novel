/* 語音頁（sherpa-onnx）：固定聲線、逐句生成、試聽、VoiceMap 狀態。#/book/<書id>/voice */
(function () {
  "use strict";

  const FLOW_TEXT =
    "流程：voice/cast_voice.json 設定每個角色的 sherpa-onnx speaker_id → 逐句生成（CPU，不需要 ComfyUI）→ " +
    "轉成 OGG 並寫入 VoiceMap.json → 重新匯出 Ren'Py 時自動加 voice 語句。";

  const STATUS = {
    generated: ["已生成", "ok"],
    stale: ["設定或台詞已變，待重生成", "warn"],
    pending: ["待生成", ""],
    missing: ["沒有聲線", "bad"],
  };

  const SOURCE_LABEL = {
    speaker_id: "sherpa-onnx 固定聲線",
  };

  function statusBadge(status) {
    const entry = STATUS[status] || [status || "", ""];
    return badge(entry[0], entry[1]);
  }

  async function render(bookId, ctx) {
    const sig = ctx ? { signal: ctx.signal } : {};
    const base = `/api/books/${bookId}/tts`;
    const wrap = h("div");
    const body = h("div");
    const progress = h("div", { class: "card", hidden: true });
    const chapterFrom = h("input", { type: "number", min: 1, value: "1", style: "width:80px" });
    const chapterTo = h("input", { type: "number", min: 1, value: "1", style: "width:80px" });
    let tracker = null;

    function chapterRange() {
      let start = Math.max(1, Number(chapterFrom.value) || 1);
      let end = Math.max(1, Number(chapterTo.value) || start);
      if (end < start) [start, end] = [end, start];
      const chapters = [];
      for (let chapter = start; chapter <= end; chapter += 1) chapters.push(chapter);
      return chapters;
    }

    async function start(payload) {
      try {
        const job = await api(`${base}/generate`, { method: "POST", body: payload });
        Panel.attach(job.id);
        watch(job.id);
      } catch (error) {
        fail(error);
      }
    }

    function watch(jobId) {
      progress.hidden = false;
      if (tracker) tracker.close();
      const running = (job) => job.status === "running" || job.status === "queued";
      const paint = (job) => {
        const active = running(job);
        const title = active
          ? "生成中"
          : ({ done: "生成完成", stopped: "已停止", error: "結束（有錯誤）" }[job.status] || job.status);
        const percent = job.progress
          ? Math.round((100 * (job.progress.done || 0)) / (job.progress.total || 1))
          : active ? 0 : 100;
        const result = job.result || {};
        const failed = Object.entries(result.failed || {});
        progress.replaceChildren(
          h(
            "div",
            { class: "row", style: "align-items:center;gap:8px" },
            h("strong", {}, title),
            h("span", { class: "grow" }),
            active
              ? h(
                  "button",
                  {
                    class: "sm",
                    onclick: async () => {
                      try {
                        if (tracker) await tracker.stop();
                      } catch (error) {
                        fail(error);
                      }
                    },
                  },
                  "■ 停止"
                )
              : ""
          ),
          h("div", { class: "bar" }, h("i", { style: `width:${percent}%` })),
          h("div", { class: "small" }, active ? job.note || "準備中…" : ""),
          !active && result.generated
            ? h("div", { class: "small muted" }, `本次生成 ${result.generated.length} 句、跳過 ${(result.skipped || []).length} 句`)
            : "",
          failed.length
            ? h("div", { class: "small bad" }, failed.slice(0, 8).map(([id, why]) => `${id}: ${why}`).join("；"))
            : "",
          !active ? h("div", { class: "row" }, h("button", { class: "sm", onclick: refresh }, "重新整理")) : ""
        );
      };
      tracker = FE.jobs.watch(jobId, {
        base: "/api/fe-jobs",
        onupdate: paint,
        onend: (job) => {
          paint(job);
          refresh().catch(fail);
        },
      });
      if (ctx && ctx.signal) {
        ctx.signal.addEventListener("abort", () => { if (tracker) tracker.close(); }, { once: true });
      }
    }

    function paintCast(doc, exists, data, modelData, speakerData) {
      const card = h("div", { class: "card" });
      const characters = (doc && doc.characters) || {};
      const roster = data.roster || [];
      const selectedModel = (modelData.models || []).find(
        (model) => model.path === ((doc && doc.sherpa_model) || modelData.current)
      );
      const maxSpeakers = selectedModel && selectedModel.speakers ? selectedModel.speakers : null;
      const rangeText = maxSpeakers ? `0–${maxSpeakers - 1}` : "從 0 起算";
      const catalog = (speakerData && speakerData.speakers) || [];
      const hasCatalog = catalog.length > 0;
      const rows = [];
      const inputs = {};

      for (const entry of roster) {
        const charId = entry.char_id;
        const prev = characters[charId] || {};
        const currentId = prev.source === "speaker_id" && prev.speaker_id != null
          ? String(prev.speaker_id) : "";
        const speed = h("input", {
          type: "number", min: 0.5, max: 2, step: 0.05,
          value: String(prev.speed ?? 1), style: "width:88px",
        });
        let genderSel;
        let ageSel;
        let idSel;
        let countText;
        let valueGetter;

        if (hasCatalog) {
          genderSel = h(
            "select",
            {},
            h("option", { value: "" }, "不拘"),
            h("option", { value: "male" }, "男"),
            h("option", { value: "female" }, "女"),
            h("option", { value: "child" }, "小孩")
          );
          ageSel = h(
            "select",
            {},
            h("option", { value: "" }, "不拘"),
            h("option", { value: "老年" }, "老年"),
            h("option", { value: "中年" }, "中年"),
            h("option", { value: "青年" }, "青年"),
            h("option", { value: "幼" }, "幼")
          );
          idSel = h("select", { style: "min-width:300px" });
          countText = h("span", { class: "small muted" });
          const rebuildIds = () => {
            const previous = idSel.value || currentId;
            const gender = genderSel.value;
            const age = ageSel.value;
            const options = [h("option", { value: "" }, "請選擇 ID")];
            const matching = catalog.filter(
              (speaker) => (!gender || speaker.gender === gender) && (!age || speaker.age_label === age)
            );
            if (!matching.length) {
              options.push(h("option", { value: "", disabled: true }, "沒有符合的 ID"));
            }
            for (const speaker of matching) {
              const years = speaker.age_years != null ? `・${Math.round(speaker.age_years)}歲` : "";
              options.push(h(
                "option",
                { value: String(speaker.id) },
                `${String(speaker.id).padStart(3, "0")}（${speaker.gender_label}・${speaker.age_label}${years}）`
              ));
            }
            if (currentId && !matching.some((speaker) => String(speaker.id) === currentId)
                && !gender && !age) {
              options.push(h("option", { value: currentId }, `${currentId}（未分類）`));
            }
            idSel.replaceChildren(...options);
            if (previous && matching.some((speaker) => String(speaker.id) === String(previous))) {
              idSel.value = String(previous);
            } else if (currentId && !gender && !age) {
              idSel.value = currentId;
            }
            countText.textContent = `符合 ${matching.length} 個 ID`;
          };
          genderSel.addEventListener("change", rebuildIds);
          ageSel.addEventListener("change", rebuildIds);
          rebuildIds();
          valueGetter = () => idSel.value;
        } else {
          genderSel = h("select", { disabled: true }, h("option", { value: "" }, "不拘"));
          ageSel = h("select", { disabled: true }, h("option", { value: "" }, "不拘"));
          idSel = h("input", {
            type: "number", min: 0, step: 1, value: currentId,
            placeholder: "speaker_id", style: "width:108px",
          });
          countText = h("span", { class: "small muted" }, "此模型尚無分類資料，直接輸入 ID");
          valueGetter = () => idSel.value.trim();
        }

        inputs[charId] = { value: valueGetter, speed };
        rows.push(
          h(
            "div",
            { class: "card", style: "margin:8px 0;padding:10px" },
            h(
              "div",
              { class: "row", style: "align-items:center;gap:8px;flex-wrap:wrap" },
              h("code", {}, charId),
              h("strong", {}, entry.name || charId),
              badge(entry.role || "", entry.role === "protagonist" ? "ok" : "info"),
              entry.generated || entry.stale
                ? badge(`已生成 ${entry.generated}/${entry.total}`, entry.generated ? "ok" : "")
                : badge("未設定", "warn"),
              entry.stale ? badge(`待重生成 ${entry.stale}`, "warn") : "",
              h("span", { class: "grow" }),
              h("span", { class: "small muted" }, `台詞 ${entry.total} 句`)
            ),
            h(
              "div",
              { class: "row", style: "align-items:center;gap:8px;flex-wrap:wrap;margin-top:8px" },
              h("span", { class: "small" }, "性別"),
              genderSel,
              h("span", { class: "small" }, "年齡"),
              ageSel,
              h("span", { class: "small" }, "ID"),
              idSel,
              h("span", { class: "small" }, "speed"),
              speed,
              countText
            ),
            h(
              "div",
              { class: "small muted", style: "margin-top:6px" },
              `先選性別／年齡，ID 只會列出符合的 speaker；留空＝暫時不配音。` +
                ` speed 0.5–2.0（1.0 正常）；模型 ID 範圍 ${rangeText}。`
            )
          )
        );
      }

      const save = h(
        "button",
        {
          class: "sm",
          onclick: async () => {
            const next = { ...(doc || {}), engine: "sherpa-onnx", characters: { ...(characters || {}) } };
            for (const [charId, spec] of Object.entries(next.characters)) {
              if (!spec || spec.source !== "speaker_id") delete next.characters[charId];
            }
            for (const [charId, refs] of Object.entries(inputs)) {
              const value = refs.value();
              if (value === "") {
                delete next.characters[charId];
                continue;
              }
              const prev = characters[charId] || {};
              next.characters[charId] = {
                source: "speaker_id",
                speaker_id: Number(value),
                speed: Number(refs.speed.value) || 1,
                language: prev.language || "zh",
              };
              if (prev.note) next.characters[charId].note = prev.note;
            }
            try {
              await api(`${base}/cast`, { method: "PUT", body: { cast_voice: next } });
              toast("聲線設定已存檔");
              await refresh();
            } catch (error) {
              fail(error);
            }
          },
        },
        "儲存聲線"
      );
      card.append(
        h(
          "div",
          { class: "row", style: "align-items:center;gap:8px" },
          h("strong", {}, "聲線設定"),
          exists ? badge("已存在", "ok") : badge("尚未建立", "warn"),
          h("span", { class: "grow" }),
          rows.length ? save : ""
        ),
        h(
          "div",
          { class: "small muted", style: "margin:8px 0" },
          "主角與主要配角（cast.json 的 protagonist／main／supporting／antagonist）都會列出來；" +
            (hasCatalog
              ? "性別／年齡／ID 三個下拉選單會串接。"
              : "此模型沒有 demographics.csv，先用數字輸入 ID。")
        ),
        ...(rows.length ? rows : [h("div", { class: "muted" }, "cast.json 沒有可配音的主角／配角。")])
      );
      return card;
    }

    function paintCharacters(data) {
      const sherpa = (data.characters || []).filter((c) => (c.spec || {}).source === "speaker_id");
      if (!sherpa.length) {
        return h("div", { class: "card muted" }, "還沒有 sherpa 聲線。先在下面建立 cast_voice.json。");
      }
      const rows = sherpa.map((c) => {
        const spec = c.spec || {};
        const how = SOURCE_LABEL[spec.source] || "sherpa-onnx";
        const detail = `speaker_id ${spec.speaker_id ?? ""} · speed ${spec.speed ?? 1}`;
        return h(
          "div",
          { class: "card" },
          h(
            "div",
            { class: "row", style: "align-items:center;gap:8px" },
            h("code", {}, c.char_id),
            badge(how),
            h("span", { class: "small muted" }, `${c.generated}/${c.total} 已生成`),
            c.stale ? badge(`待重生成 ${c.stale}`, "warn") : "",
            h("span", { class: "grow" }),
            h("span", { class: "small muted" }, `engine ${c.engine || ""}`)
          ),
          detail ? h("div", { class: "small", style: "margin-top:6px" }, detail) : ""
        );
      });
      return h("div", {}, ...rows);
    }

    function paintLines(data) {
      if (!data.lines || !data.lines.length) {
        return h("div", { class: "card muted" }, "這本書還沒有對白。先標註章節。");
      }
      return h(
        "div",
        {},
        ...data.lines.map((line) =>
          h(
            "div",
            { class: "card" },
            h(
              "div",
              { class: "row", style: "align-items:center;gap:8px" },
              h("code", { class: "small" }, line.line_id),
              statusBadge(line.status),
              h("span", { class: "small muted" }, line.speaker || ""),
              h("span", { class: "grow" }),
              line.seed != null ? h("span", { class: "small muted" }, `seed ${line.seed}`) : "",
              h(
                "button",
                {
                  class: "sm ghost",
                  onclick: () =>
                    start({
                      chapters: [Number(String(line.chapter || "ch001").slice(2))],
                      only: [line.line_id],
                      regenerate: true,
                    }),
                },
                "重生這句"
              )
            ),
            h("div", { style: "margin-top:6px" }, line.text || ""),
            line.status === "generated" && line.work_file
              ? h("audio", {
                  controls: true,
                  preload: "none",
                  style: "width:100%;margin-top:6px",
                  src: `${base}/file?path=${encodeURIComponent(line.work_file)}`,
                })
              : ""
          )
        )
      );
    }

    function paintModels(modelData, cast) {
      const models = modelData.models || [];
      const current = modelData.current || (cast && cast.sherpa_model) || "";
      const select = h(
        "select",
        { style: "min-width:360px" },
        ...models.map((model) =>
          h("option", { value: model.path }, `${model.label || model.id}（${model.path}）`)
        )
      );
      if (models.length && models.some((model) => model.path === current)) {
        select.value = current;
      }
      const custom = h("input", {
        value: current,
        placeholder: "models/sherpa/vits-zh-hf-fanchen-C",
        style: "min-width:360px",
      });
      select.addEventListener("change", () => {
        custom.value = select.value;
      });
      const apply = h(
        "button",
        {
          class: "sm",
          onclick: async () => {
            try {
              const latest = (await api(`${base}/cast`, sig)).cast_voice || {};
              const chosen = custom.value.trim() || select.value.trim();
              if (chosen) latest.sherpa_model = chosen;
              else delete latest.sherpa_model;
              await api(`${base}/cast`, { method: "PUT", body: { cast_voice: latest } });
              toast("模型已套用；下次生成會用新模型");
              await refresh();
            } catch (error) {
              fail(error);
            }
          },
        },
        "套用模型"
      );
      const currentModel = models.find((model) => model.path === current);
      const presets = (currentModel && currentModel.presets) || [];
      const presetTable = presets.length
        ? h(
            "details",
            { style: "margin-top:10px" },
            h("summary", { class: "small" }, "fanchen-C 實測估計（模型；點開查看）"),
            h(
              "table",
              { class: "small", style: "margin-top:6px;width:100%;border-collapse:collapse" },
              h(
                "tr",
                {},
                h("th", { style: "text-align:left;border-bottom:1px solid #ddd;padding:4px" }, "speaker_id"),
                h("th", { style: "text-align:left;border-bottom:1px solid #ddd;padding:4px" }, "模型估計"),
                h("th", { style: "text-align:left;border-bottom:1px solid #ddd;padding:4px" }, "實測備註")
              ),
              ...presets.map((preset) =>
                h(
                  "tr",
                  {},
                  h("td", { style: "padding:4px;border-bottom:1px solid #eee" }, String(preset.id)),
                  h("td", { style: "padding:4px;border-bottom:1px solid #eee" }, preset.label || ""),
                  h("td", { style: "padding:4px;border-bottom:1px solid #eee" }, preset.desc || "")
                )
              )
            )
          )
        : "";
      return h(
        "div",
        { class: "card" },
        h(
          "div",
          { class: "row", style: "align-items:center;gap:8px;flex-wrap:wrap" },
          h("strong", {}, "模型"),
          select,
          h("span", { class: "grow" }),
          apply
        ),
        h(
          "div",
          { class: "small muted", style: "margin-top:8px" },
          `已安裝 ${models.length} 個 sherpa VITS 模型（掃描 models/sherpa）。也可以直接貼資料夾路徑。` +
            " 換模型後 VoiceMap 的 config_hash 會變，舊句會重新生成。"
        ),
        h("div", { class: "row", style: "margin-top:8px;gap:8px" }, h("span", { class: "small" }, "自訂路徑"), custom),
        presetTable
      );
    }

    function paintSample(data, modelData) {
      const current = (modelData && modelData.current) || "";
      const first = (data.roster || []).find((entry) => entry.configured) || (data.roster || [])[0] || {};
      const text = h("input", {
        value: "這是一段測試語音，請參考。",
        style: "min-width:380px;flex:1",
        placeholder: "輸入要試聽的文字",
      });
      const sid = h("input", {
        type: "number", min: 0, step: 1, value: String(first.speaker_id ?? 0), style: "width:100px",
      });
      const speed = h("input", {
        type: "number", min: 0.5, max: 2, step: 0.05, value: "1", style: "width:86px",
      });
      const audio = h("audio", { controls: true, preload: "none", style: "width:100%;margin-top:8px", hidden: true });
      const button = h(
        "button",
        {
          class: "sm",
          onclick: async () => {
            try {
              const result = await api(`${base}/sample`, {
                method: "POST",
                body: {
                  text: text.value,
                  speaker_id: Number(sid.value),
                  speed: Number(speed.value) || 1,
                  model: current,
                },
              });
              audio.src = result.audio;
              audio.hidden = false;
              audio.play().catch(() => {});
              toast("範例語音已產生");
            } catch (error) {
              fail(error);
            }
          },
        },
        "產生範例語音"
      );
      return h(
        "div",
        { class: "card" },
        h(
          "div",
          { class: "row", style: "align-items:center;gap:8px;flex-wrap:wrap" },
          h("strong", {}, "試聽"),
          h("span", { class: "small muted" }, "用目前模型的某個 speaker 產生一句範例；不會寫入 VoiceMap。"),
          h("span", { class: "grow" }),
          button
        ),
        h(
          "div",
          { class: "row", style: "align-items:center;gap:8px;flex-wrap:wrap;margin-top:8px" },
          h("span", { class: "small" }, "文字"),
          text,
          h("span", { class: "small" }, "speaker_id"),
          sid,
          h("span", { class: "small" }, "speed"),
          speed
        ),
        audio
      );
    }

    async function refresh() {
      const [data, cast, modelData] = await Promise.all([
        api(base, sig),
        api(`${base}/cast`, sig).catch(() => ({ cast_voice: {}, exists: false })),
        api(`${base}/models`, sig).catch(() => ({ models: [], current: "" })),
      ]);
      const currentModel = (cast.cast_voice && cast.cast_voice.sherpa_model) || modelData.current || "";
      const speakerData = await api(`${base}/speakers?model=${encodeURIComponent(currentModel)}`, sig)
        .catch(() => ({ available: false, speakers: [], message: "" }));
      const totals = data.totals || {};
      const info = h(
        "div",
        { class: "card" },
        h(
          "div",
          { class: "row", style: "align-items:center;gap:8px" },
          h("strong", {}, "進度"),
          badge(`已生成 ${totals.generated || 0}`, "ok"),
          totals.stale ? badge(`待重生成 ${totals.stale}`, "warn") : "",
          badge(`待生成 ${totals.pending || 0}`, ""),
          totals.missing ? badge(`沒有聲線 ${totals.missing}`, "bad") : "",
          h("span", { class: "grow" }),
          data.missing_characters && data.missing_characters.length
            ? h("span", { class: "small muted" }, `缺聲線：${data.missing_characters.join("、")}`)
            : ""
        )
      );
      const errors = data.errors && data.errors.length
        ? h("div", { class: "card bad" }, h("strong", {}, "設定有問題"), h("div", { class: "small" }, data.errors.join("\n")))
        : "";
      body.replaceChildren(
        info,
        errors,
        paintModels(modelData, cast.cast_voice),
        paintSample(data, modelData),
        h("h2", {}, "聲線設定"),
        paintCast(cast.cast_voice, cast.exists, data, modelData, speakerData),
        h("h2", {}, "逐句"),
        paintLines(data)
      );
    }

    const toolbar = h("div", { class: "card" });
    toolbar.append(
      h("div", { class: "small", style: "margin-bottom:8px" }, FLOW_TEXT),
      h(
        "div",
        { class: "row", style: "align-items:center;gap:8px;flex-wrap:wrap" },
        h("span", { class: "small" }, "章節"),
        chapterFrom,
        h("span", { class: "small" }, "到"),
        chapterTo,
        h("button", { class: "sm", onclick: () => start({ chapters: chapterRange() }) }, "生成這個範圍"),
        h(
          "button",
          { class: "sm ghost", onclick: () => start({ chapters: chapterRange(), regenerate: true }) },
          "重新生成這個範圍"
        ),
        h("span", { class: "grow" }),
        h("button", { class: "sm ghost", onclick: refresh }, "重新整理")
      )
    );

    wrap.append(h("h1", {}, "語音"), toolbar, progress, body);
    try {
      await refresh();
    } catch (error) {
      body.replaceChildren(h("div", { class: "card bad" }, error.message));
    }
    return wrap;
  }

  FE.PAGES["book/:id/voice"] = async (ctx) => {
    const bookId = ctx.params.id;
    ctx.crumbs([
      { label: "書架", href: "#/" },
      { label: bookId, href: `#/book/${bookId}` },
      { label: "語音" },
    ]);
    ctx.view(h("div", { class: "muted" }, "載入中…"));
    try {
      ctx.view(await render(bookId, ctx));
    } catch (error) {
      ctx.view(h("div", { class: "card bad" }, error.message));
    }
  };
})();
