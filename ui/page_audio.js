/* 音樂頁（BGM／環境音）：AI 擬稿、出曲、試聽候選、挑選、掃描手動放入的音檔。#/book/<書id>/audio */
(function () {
  "use strict";

  const STATUS = {
    planned: ["待出曲", ""],
    generated: ["已出曲，待挑選", "warn"],
    selected: ["已挑選", "info"],
    processed: ["已處理", "ok"],
    final: ["定稿", "ok"],
  };
  const KINDS = { bgm: "BGM", ambience: "環境音" };
  const FLOW_TEXT =
    "流程：AI 擬稿（曲風＋歌詞，純配樂）→ 出曲（YuE2 背景任務，每首要幾分鐘）→ 試聽候選 → 挑選（後端轉成 opus 並正規化音量）。";
  const STYLE_PLACEHOLDER =
    "英文標籤，逗號分隔，例如：calm acoustic guitar, 72 bpm, soft room tone, pure instrumental, no vocals";
  const LYRICS_PLACEHOLDER = "[Intro]\n(Soft guitar)\n\n[Chorus]\n(Light percussion)";

  /* 進行中的出曲任務（照 page_assets.js 的 assetTrackers）：同一頁再開新任務、換分頁或
     離開這一頁時一起收掉，遲到的回呼才不會改到已卸下的節點。識別字帶頁面前綴——
     多支 classic script 共用全域。 */
  const audioTrackers = new Set();

  function closeAudioTrackers() {
    for (const item of audioTrackers) item.close();
    audioTrackers.clear();
  }

  function kindBadge(item) {
    return badge(KINDS[item.kind] || item.kind, item.kind === "ambience" ? "info" : "");
  }

  function statusBadge(item) {
    const entry = STATUS[item.status] || [item.status || "", ""];
    return badge(entry[0], entry[1]);
  }

  async function render(bookId, ctx) {
    const base = `/api/books/${bookId}/art/audio`;
    const sig = ctx ? { signal: ctx.signal } : {};
    const audioUrl = (relPath) => `${base}/file?path=${encodeURIComponent(relPath)}`;

    const wrap = h("div");
    const toolbar = h("div", { class: "card" });
    const progress = h("div", { class: "card", hidden: true });
    const list = h("div");
    let kindFilter = "";
    let items = [];
    let lastData = null;

    async function load() {
      const data = await api(base, sig);
      items = data.items || [];
      lastData = data;
      return data;
    }

    async function refresh() {
      const data = await load();
      paintToolbar(data);
      paintList();
    }

    async function startJob(payload) {
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
      let tracker = null;
      const paint = (job) => {
        if (job.gone) {
          progress.replaceChildren(h("div", { class: "bad small" }, "找不到這個任務，後端可能重新啟動過。"));
          return;
        }
        const running = job.status === "running" || job.status === "queued";
        const title = running
          ? "出曲中"
          : ({ done: "出曲完成", stopped: "已停止", error: "結束（有錯誤）" }[job.status] || job.status);
        const done = job.progress ? job.progress.done : 0;
        const total = job.progress ? job.progress.total : 0;
        const percent = total ? Math.round((100 * done) / total) : running ? 0 : 100;
        const currentLine = running ? job.note || "準備中…" : "";
        const result = job.result || {};
        const generated = result.generated || [];
        const skipped = result.skipped || [];
        const failed = Object.entries(result.failed || {});

        progress.replaceChildren(
          h(
            "div",
            { class: "row", style: "align-items:center;gap:8px" },
            h("strong", {}, title),
            h("span", { class: "grow" }),
            running
              ? h(
                  "button",
                  {
                    class: "sm",
                    onclick: async () => {
                      try {
                        await tracker.stop();
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
          h("div", { class: "small" }, currentLine),
          !running
            ? h(
                "div",
                { class: "small" },
                `新出曲 ${generated.length} 首，沒變動 ${skipped.length} 首` +
                  (failed.length ? `，失敗 ${failed.length} 首` : "")
              )
            : "",
          failed.length
            ? h(
                "div",
                { class: "small bad" },
                ...failed.map(([id, reason]) => h("div", {}, `${id}：${reason}`))
              )
            : ""
        );
      };
      closeAudioTrackers();   // 先收掉前一個任務的追蹤器，再換成新的
      tracker = FE.jobs.watch(jobId, {
        base: "/api/fe-jobs",
        onupdate: paint,
        onend: (job) => {
          audioTrackers.delete(tracker);
          paint(job);
          refresh().catch(fail);
        },
      });
      audioTrackers.add(tracker);
    }

    async function scanFiles() {
      try {
        const data = await api(`${base}/scan`, { method: "POST", body: {} });
        const updated = (data.updated || []).length;
        const orphans = data.orphans || [];
        toast(`已更新 ${updated} 項` + (orphans.length ? `；${orphans.length} 個孤兒檔案` : ""));
        if (orphans.length) {
          toast(`孤兒檔案：${orphans.slice(0, 3).join("、")}${orphans.length > 3 ? "…" : ""}`);
        }
        await refresh();
      } catch (error) {
        fail(error);
      }
    }

    function paintToolbar(data) {
      const ready = !!(data && data.ready);
      const batchable = items.filter(
        (item) => item.status === "planned" && String((item.prompt || {}).style || "").trim()
      );
      toolbar.replaceChildren(
        h(
          "div",
          { class: "row", style: "align-items:center;gap:8px" },
          "種類：",
          select(
            [
              ["", "全部"],
              ["bgm", "BGM"],
              ["ambience", "環境音"],
            ],
            kindFilter,
            (value) => {
              kindFilter = value;
              paintList();
            }
          ),
          h(
            "button",
            {
              disabled: batchable.length === 0,
              onclick: () => startJob({ only: batchable.map((item) => item.asset_id), candidates: 1 }),
            },
            `有提示詞的未出曲全部各出 1 首（${batchable.length}）`
          ),
          h("button", { class: "ghost sm", onclick: () => scanFiles() }, "掃描手動放入的音檔"),
          h("span", { class: "grow" }),
          h("span", { class: "small muted" }, ready ? `共 ${items.length} 項` : "還沒有音樂清單")
        )
      );
    }

    function paintList() {
      const shown = items.filter((item) => !kindFilter || item.kind === kindFilter);
      if (!shown.length) {
        list.replaceChildren(
          h("div", { class: "card muted" }, "沒有符合的音樂項目。")
        );
        return;
      }
      list.replaceChildren(...shown.map((item) => itemCard(item)));
    }

    function itemCard(item) {
      const promptValue = item.prompt || {};
      const styleInput = h("textarea", {
        rows: 3,
        style: "width:100%",
        placeholder: STYLE_PLACEHOLDER,
        value: promptValue.style || "",
      });
      const lyricsInput = h("textarea", {
        rows: 5,
        style: "width:100%",
        placeholder: LYRICS_PLACEHOLDER,
        value: promptValue.lyrics || "",
      });
      const loopBox = h("input", { type: "checkbox", checked: true });

      const draftButton = h("button", { class: "sm" }, "AI 擬稿");
      draftButton.addEventListener("click", async () => {
        const original = draftButton.textContent;
        draftButton.disabled = true;
        draftButton.textContent = "AI 擬稿中…";
        try {
          const body = {};
          const executor = Executors.value();
          if (executor) body.executor = executor;
          const data = await api(`${base}/${item.asset_id}/draft`, { method: "POST", body });
          const prompt = data.prompt || {};
          styleInput.value = prompt.style || "";
          lyricsInput.value = prompt.lyrics || "";
          item.prompt = { style: styleInput.value, lyrics: lyricsInput.value };
          paintToolbar(lastData);
          const warnings = data.warnings || [];
          toast(`提示詞擬好了${warnings.length ? `（${warnings.join("；")}）` : ""}`);
        } catch (error) {
          fail(error);
        } finally {
          draftButton.disabled = false;
          draftButton.textContent = original;
        }
      });

      const saveButton = h("button", { class: "sm" }, "存提示詞");
      saveButton.addEventListener("click", async () => {
        try {
          await api(`${base}/${item.asset_id}/prompt`, {
            method: "PUT",
            body: { style: styleInput.value.trim(), lyrics: lyricsInput.value.trim() },
          });
          item.prompt = { style: styleInput.value.trim(), lyrics: lyricsInput.value.trim() };
          paintToolbar(lastData);
          toast("已存提示詞");
        } catch (error) {
          fail(error);
        }
      });

      const againButton = h("button", { class: "sm" }, "再出 1 首");
      againButton.addEventListener("click", () =>
        startJob({ only: [item.asset_id], candidates: 1, regenerate: true })
      );

      async function pick(name) {
        try {
          await api(`${base}/select`, {
            method: "POST",
            body: { asset_id: item.asset_id, name, loop: loopBox.checked },
          });
          toast("已挑選（後端已轉成 opus）");
          await refresh();
        } catch (error) {
          fail(error);
        }
      }

      async function finish() {
        const license = await ask(
          "定稿前請填授權／來源說明（例如：AI 生成，自用）",
          item.license || "AI 生成（YuE2），自用"
        );
        if (!license) return;
        try {
          await api(`/api/books/${bookId}/art/status`, {
            method: "POST",
            body: { asset_id: item.asset_id, status: "final", license },
          });
          toast("已定稿");
          await refresh();
        } catch (error) {
          fail(error);
        }
      }

      const finalButton =
        item.status === "processed"
          ? h("button", { class: "sm", onclick: () => finish() }, "定稿")
          : "";

      const candidates = (item.candidates || []).map((candidate) =>
        h(
          "div",
          { style: "min-width:260px;max-width:340px" },
          h("audio", { controls: true, preload: "none", src: audioUrl(candidate.file), style: "width:100%" }),
          h(
            "div",
            { class: "row", style: "align-items:center;gap:6px;margin-top:4px" },
            h("span", { class: "small muted" }, `種子 ${candidate.seed ?? ""}`),
            h("span", { class: "grow" }),
            h("button", { class: "sm", onclick: () => pick(candidate.name) }, "選這首")
          )
        )
      );

      const fileBlock =
        item.has_file && item.file
          ? h(
              "div",
              { class: "card", style: "margin-top:8px" },
              h(
                "div",
                { class: "row", style: "align-items:center;gap:8px" },
                badge("已有成品", "ok"),
                h("code", { class: "small" }, item.file)
              ),
              h("audio", { controls: true, preload: "none", src: audioUrl(item.file), style: "width:100%;margin-top:6px" })
            )
          : "";

      const candidatesBlock = candidates.length
        ? h(
            "div",
            { style: "margin-top:10px" },
            h(
              "div",
              { class: "row", style: "align-items:center;gap:6px" },
              h("strong", {}, "候選"),
              h("span", { class: "grow" }),
              loopBox,
              "做成可循環"
            ),
            h("div", { class: "row", style: "flex-wrap:wrap;gap:14px;margin-top:6px" }, ...candidates)
          )
        : h(
            "div",
            { class: "small muted", style: "margin-top:8px" },
            "還沒有候選：按「再出 1 首」或上面的批次按鈕出曲。"
          );

      return h(
        "div",
        { class: "card" },
        h(
          "div",
          { class: "row", style: "align-items:center;gap:8px" },
          h("code", { class: "small" }, item.asset_id),
          kindBadge(item),
          statusBadge(item),
          h("span", { class: "small muted" }, `用到 ${item.scene_count || 0} 場`),
          h("span", { class: "grow" }),
          draftButton,
          saveButton,
          againButton,
          finalButton
        ),
        h("div", { class: "small", style: "margin-top:8px" }, "曲風（英文標籤，結尾要有 pure instrumental, no vocals）"),
        styleInput,
        h("div", { class: "small", style: "margin-top:6px" }, "歌詞欄（只放 [段落] 與 (編曲註記)，不要放要唱的字）"),
        lyricsInput,
        fileBlock,
        candidatesBlock
      );
    }

    const data = await load();
    wrap.append(
      h("h1", {}, "音樂"),
      h("div", { class: "card small muted" }, FLOW_TEXT),
      toolbar,
      progress,
      list
    );
    paintToolbar(data);
    paintList();
    if (!data.ready) {
      list.replaceChildren(
        h(
          "div",
          { class: "card muted" },
          "還沒有音樂清單：先到「素材」頁（會自動重新整理素材清單），這裡就會列出 BGM 與環境音。"
        )
      );
    }
    return wrap;
  }

  FE.PAGES["book/:id/audio"] = async (ctx) => {
    const bookId = ctx.params.id;
    // 離頁就停止追蹤（晚回來的結果不會蓋掉別頁）；監聽只掛一次，換任務時靠上面的 close。
    closeAudioTrackers();
    ctx.signal.addEventListener("abort", closeAudioTrackers, { once: true });
    ctx.crumbs([
      { label: "書架", href: "#/" },
      { label: bookId, href: `#/book/${bookId}` },
      { label: "音樂" },
    ]);
    try {
      ctx.view(await render(bookId, ctx));
    } catch (error) {
      ctx.view(h("div", { class: "card bad" }, error.message));
    }
  };
})();
