/* 輸出頁：標題畫面素材、匯出到 Ren'Py、自動驗收、打包 Windows 版、看報告。#/book/<書id>/export */
(function () {
  "use strict";

  const POLL_MS = 1500;
  const MAX_LIST = 10;
  const CASE_ROWS = [
    ["launch", "啟動"],
    ["screens", "畫面"],
    ["input", "輸入"],
    ["main_loop_ending", "主循環與結局"],
    ["restart", "重開"],
  ];
  const JOB_TITLE = {
    running: "執行中",
    done: "完成",
    error: "結束（有錯誤）",
    stopped: "已停止",
  };
  const EXPORT_TITLE = {
    running: "匯出中",
    done: "匯出完成",
    error: "匯出失敗",
    stopped: "匯出已停止",
  };
  const VERIFY_TITLE = {
    running: "驗收中",
    done: "驗收完成",
    error: "驗收失敗",
    stopped: "驗收已停止",
  };

  const PACKAGE_TITLE = {
    running: "打包中",
    done: "打包完成",
    error: "打包失敗",
    stopped: "打包已停止",
  };

  function sizeText(bytes) {
    const mb = (Number(bytes) || 0) / (1024 * 1024);
    return mb >= 1 ? `${mb.toFixed(1)} MB` : `${Math.max(1, Math.round((Number(bytes) || 0) / 1024))} KB`;
  }

  function caseBadge(value) {
    if (value === true) return badge("通過", "ok");
    if (value === false) return badge("失敗", "bad");
    return badge("未知", "");
  }

  function titleBadge(state, kind) {
    const label = kind === "bg" ? "標題圖" : "標題音樂";
    if (!state) return badge(`${label}：尚未加入素材清單`, "");
    if (state.present) return badge(`${label}：已有成品`, "ok");
    if (state.in_manifest) return badge(`${label}：待出圖`, "warn");
    return badge(`${label}：尚未加入素材清單`, "");
  }

  function listBlock(title, rows) {
    if (!rows.length) return "";
    const shown = rows.slice(0, MAX_LIST);
    return h(
      "div",
      { class: "small", style: "margin-top:6px" },
      h("strong", {}, `${title}（${rows.length}）`),
      ...shown.map((row) => h("div", { class: "small" }, String(row))),
      rows.length > MAX_LIST
        ? h("div", { class: "small muted" }, `…另有 ${rows.length - MAX_LIST} 條`)
        : ""
    );
  }

  function lastLogLine(job) {
    const log = job && Array.isArray(job.log) ? job.log : [];
    return log.length ? String(log[log.length - 1]) : "";
  }

  function progressCard(host, title, job) {
    const current = (job && job.current) || {};
    const percent = current.total
      ? Math.round((100 * ((current.index || 1) - 1 + (current.event === "asset_done" ? 1 : 0))) / current.total)
      : 0;
    const line =
      current.chapter || current.asset
        ? `${current.chapter || current.asset}（第 ${current.index || 1}/${current.total || 1} 步）`
        : lastLogLine(job) || "準備中…";
    host.hidden = false;
    host.replaceChildren(
      h(
        "div",
        { class: "row", style: "align-items:center;gap:8px" },
        h("strong", {}, title),
        h("span", { class: "grow" }),
        job && job.id
          ? h(
              "button",
              {
                class: "sm",
                onclick: async () => {
                  try {
                    await api(`/api/jobs/${job.id}/stop`, { method: "POST", body: {} });
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
      h("div", { class: "small" }, line)
    );
  }

  async function render(bookId) {
    const base = `/api/books/${bookId}`;
    let data = await api(`${base}/export`);

    let running = "";        // "" | "export" | "verify" | "package"
    let liveJob = null;      // 目前執行中的 job
    let exportJob = null;    // 最近一次匯出任務（完成後保留摘要）
    let verifyJob = null;    // 最近一次驗收任務
    let packageJob = null;   // 最近一次打包任務
    let timer = null;
    let chapterNumbers = null;   // 已勾選的章號（null＝預設全選）
    let reportView = null;

    const wrap = h("div");
    const titleHost = h("div");
    const settingsHost = h("div");
    const exportHost = h("div");
    const verifyHost = h("div");
    const packageHost = h("div");
    const reportsHost = h("div");
    const exportProgress = h("div", { class: "card", hidden: true });
    const verifyProgress = h("div", { class: "card", hidden: true });
    const packageProgress = h("div", { class: "card", hidden: true });

    function selection() {
      const rows = (data.chapters || []).filter((row) => row.exportable);
      const valid = new Set(rows.map((row) => row.chapter_no));
      if (chapterNumbers === null) {
        chapterNumbers = new Set(rows.map((row) => row.chapter_no));
      } else {
        for (const number of Array.from(chapterNumbers)) {
          if (!valid.has(number)) chapterNumbers.delete(number);
        }
      }
      return chapterNumbers;
    }

    async function reload() {
      data = await api(`${base}/export`);
      paintAll();
    }

    async function startJob(kind, path, payload) {
      if (running) return;
      try {
        const job = await api(`${base}${path}`, { method: "POST", body: payload });
        AiPanel.poll();
        running = kind;
        liveJob = job;
        paintAll();
        poll(job.id, kind);
      } catch (error) {
        fail(error);
      }
    }

    function poll(jobId, kind) {
      if (timer) clearTimeout(timer);
      const tick = async () => {
        let job;
        try {
          job = await api(`/api/jobs/${jobId}`);
        } catch (error) {
          fail(error);
          return;
        }
        liveJob = job;
        if (job.status === "running") {
          if (kind === "export") progressCard(exportProgress, EXPORT_TITLE.running, job);
          else if (kind === "verify") progressCard(verifyProgress, VERIFY_TITLE.running, job);
          else progressCard(packageProgress, PACKAGE_TITLE.running, job);
          timer = setTimeout(tick, POLL_MS);
          return;
        }
        running = "";
        liveJob = null;
        if (kind === "export") exportJob = job;
        else if (kind === "verify") verifyJob = job;
        else packageJob = job;
        try {
          await reload();
        } catch (error) {
          fail(error);
          paintAll();
        }
      };
      tick();
    }

    /* ---------------------------------------------------------------- 標題畫面 */
    function paintTitle() {
      const title = data.title || {};
      const prompt = title.prompt || {};
      const tagsInput = h("input", {
        value: prompt.tags || "",
        placeholder: "標題圖標籤（英文逗號分隔）",
        style: "width:100%",
      });
      const descInput = h("textarea", {
        rows: 3,
        style: "width:100%",
        placeholder: "標題圖描述（英文 2–3 句）",
        value: prompt.description || "",
      });
      const saveButton = h("button", { class: "sm" }, "存標題圖提示詞");
      saveButton.addEventListener("click", async () => {
        try {
          await api(`${base}/export/title`, {
            method: "PUT",
            body: { tags: tagsInput.value.trim(), description: descInput.value.trim() },
          });
          toast("已存標題圖提示詞");
          await reload();
        } catch (error) {
          fail(error);
        }
      });
      const refreshButton = h("button", { class: "sm" }, "加入素材清單");
      refreshButton.addEventListener("click", async () => {
        try {
          await api(`${base}/art/refresh`, { method: "POST", body: {} });
          toast("已加入素材清單，到素材頁／音樂頁出圖");
          await reload();
        } catch (error) {
          fail(error);
        }
      });

      titleHost.replaceChildren(
        h(
          "div",
          { class: "card" },
          h("h2", { style: "margin-top:0" }, "標題畫面"),
          h(
            "div",
            { class: "row", style: "align-items:center;gap:8px" },
            titleBadge(title.bg, "bg"),
            titleBadge(title.bgm, "bgm"),
            h("span", { class: "grow" }),
            h("a", { href: `#/book/${bookId}/assets` }, "去素材頁出標題圖"),
            " ",
            h("a", { href: `#/book/${bookId}/audio` }, "去音樂頁出標題音樂")
          ),
          h("div", { class: "small", style: "margin-top:8px" }, "標題圖標籤"),
          tagsInput,
          h("div", { class: "small", style: "margin-top:6px" }, "標題圖描述"),
          descInput,
          h("div", { class: "row", style: "align-items:center;gap:8px;margin-top:8px" }, saveButton, refreshButton)
        )
      );
    }

    /* ---------------------------------------------------------------- Ren'Py 設定 */
    function paintSettings() {
      const settings = data.settings || {};
      const sdkInput = h("input", { value: settings.sdk || "", placeholder: "例如 C:\\renpy-8.5.3-sdk", style: "width:100%" });
      const projectInput = h("input", { value: settings.project || "", placeholder: "Ren'Py 專案資料夾（裡面要有 game）", style: "width:100%" });
      const saveButton = h("button", { class: "sm" }, "儲存設定");
      saveButton.addEventListener("click", async () => {
        try {
          await api(`${base}/export/settings`, {
            method: "PUT",
            body: { sdk: sdkInput.value.trim(), project: projectInput.value.trim() },
          });
          toast("已存 Ren'Py 設定");
          await reload();
        } catch (error) {
          fail(error);
        }
      });
      settingsHost.replaceChildren(
        h(
          "div",
          { class: "card" },
          h(
            "div",
            { class: "row", style: "align-items:center;gap:8px" },
            h("h2", { style: "margin:0" }, "Ren'Py 設定"),
            data.project_ready ? badge("專案已就緒", "ok") : badge("找不到專案（要有 game 資料夾）", "bad")
          ),
          h("div", { class: "small", style: "margin-top:8px" }, "Ren'Py SDK 資料夾"),
          sdkInput,
          h("div", { class: "small", style: "margin-top:6px" }, "Ren'Py 專案資料夾"),
          projectInput,
          h("div", { class: "row", style: "margin-top:8px" }, saveButton)
        )
      );
    }

    /* ---------------------------------------------------------------- 匯出 */
    function exportSummaryBlock() {
      if (!exportJob) return "";
      const summary = exportJob.summary || {};
      const status = exportJob.status;
      const warnings = Array.isArray(summary.warnings) ? summary.warnings : [];
      const issues = Array.isArray(summary.issues) ? summary.issues : [];
      const log = lastLogLine(exportJob);
      const placeholderCounts = summary.placeholders && typeof summary.placeholders === "object"
        ? Object.entries(summary.placeholders).map(([kind, count]) => `${kind} ${count} 張`).join("、")
        : "";
      return h(
        "div",
        { class: "card" },
        h(
          "div",
          { class: "row", style: "align-items:center;gap:8px" },
          h("strong", {}, EXPORT_TITLE[status] || status || JOB_TITLE[status] || "結果"),
          status === "error" ? badge("有錯誤", "bad") : status === "stopped" ? badge("已停止", "warn") : badge("完成", "ok")
        ),
        h(
          "div",
          { class: "small", style: "margin-top:6px" },
          `專案：${summary.project || "（未提供）"}｜匯出檔案 ${summary.files ?? 0} 個｜章節 ${(summary.chapters || []).length} 章`
        ),
        h(
          "div",
          { class: "small" },
          `前置檢查：${summary.preflight_errors ?? 0} 個錯誤、${summary.preflight_warnings ?? 0} 個警告`
        ),
        placeholderCounts ? h("div", { class: "small" }, `補的佔位圖：${placeholderCounts}`) : "",
        listBlock("警告", warnings),
        listBlock("問題", issues),
        status === "error" && log ? h("div", { class: "bad small", style: "margin-top:6px" }, log) : ""
      );
    }

    function paintExport() {
      const rows = (data.chapters || []).filter((row) => row.exportable);
      const chosen = selection();
      const boxes = rows.map((row) => {
        const box = h("input", { type: "checkbox", checked: chosen.has(row.chapter_no) });
        box.addEventListener("change", () => {
          if (box.checked) chosen.add(row.chapter_no);
          else chosen.delete(row.chapter_no);
        });
        return {
          row,
          box,
          node: h(
            "label",
            { class: "row", style: "align-items:center;gap:6px;margin:2px 0" },
            box,
            `第 ${row.chapter_no} 章 ${row.title}（${row.scene_count} 場）`
          ),
        };
      });
      const placeholdersBox = h("input", { type: "checkbox" });
      const cleanBox = h("input", { type: "checkbox" });
      const selectAll = () => {
        for (const item of boxes) {
          item.box.checked = true;
          chosen.add(item.row.chapter_no);
        }
      };
      const selectNone = () => {
        for (const item of boxes) {
          item.box.checked = false;
          chosen.delete(item.row.chapter_no);
        }
      };
      const exportButton = h("button", { disabled: !!running }, "匯出");
      exportButton.addEventListener("click", () => {
        const chapters = Array.from(chosen).sort((a, b) => a - b);
        if (!chapters.length) return toast("請至少選一章");
        startJob("export", "/export/run", {
          chapters,
          placeholders: placeholdersBox.checked,
          clean: cleanBox.checked,
        });
      });

      const exportCard = h("div", { class: "card" });
      exportCard.append(
        h(
          "div",
          { class: "row", style: "align-items:center;gap:8px" },
          h("h2", { style: "margin:0" }, "匯出"),
          h("span", { class: "muted small" }, `可匯出 ${rows.length} 章`)
        ),
        boxes.length
          ? h("div", { class: "row", style: "flex-wrap:wrap;gap:12px;margin-top:8px" }, ...boxes.map((item) => item.node))
          : h("div", { class: "muted small", style: "margin-top:8px" }, "沒有可匯出的章（章節要有場景）"),
        h(
          "div",
          { class: "row", style: "align-items:center;gap:8px;margin-top:8px" },
          h("button", { class: "ghost sm", onclick: selectAll }, "全選"),
          h("button", { class: "ghost sm", onclick: selectNone }, "全不選"),
          h("label", { class: "row", style: "align-items:center;gap:4px" }, placeholdersBox, "補佔位圖"),
          h("label", { class: "row", style: "align-items:center;gap:4px" }, cleanBox, "先清掉舊檔"),
          h("span", { class: "grow" }),
          exportButton
        ),
        exportProgress,
        exportSummaryBlock()
      );
      exportHost.replaceChildren(exportCard);
      if (running === "export" && liveJob) progressCard(exportProgress, EXPORT_TITLE.running, liveJob);
      else exportProgress.hidden = true;
    }

    /* ---------------------------------------------------------------- 自動驗收 */
    function verifySummaryBlock() {
      if (!verifyJob) return "";
      const summary = verifyJob.summary || {};
      const cases = summary.cases || {};
      const status = verifyJob.status;
      const log = lastLogLine(verifyJob);
      return h(
        "div",
        { class: "card" },
        h(
          "div",
          { class: "row", style: "align-items:center;gap:8px" },
          h("strong", {}, VERIFY_TITLE[status] || status || "結果"),
          badge(status === "error" ? "有錯誤" : status === "stopped" ? "已停止" : "完成", status === "error" ? "bad" : status === "stopped" ? "warn" : "ok")
        ),
        h(
          "table",
          {},
          h("thead", {}, h("tr", {}, h("th", {}, "項目"), h("th", {}, "結果"))),
          h(
            "tbody",
            {},
            ...CASE_ROWS.map(([key, label]) => h("tr", {}, h("td", {}, label), h("td", {}, caseBadge(cases[key]))))
          )
        ),
        summary.report ? h("div", { class: "small muted", style: "margin-top:6px" }, `報告：${summary.report}`) : "",
        status === "error" && log ? h("div", { class: "bad small", style: "margin-top:6px" }, log) : ""
      );
    }

    function paintVerify() {
      const verifyButton = h("button", { disabled: !!running }, "跑驗收");
      verifyButton.addEventListener("click", () => startJob("verify", "/export/verify", {}));
      const verifyCard = h("div", { class: "card" });
      verifyCard.append(
        h(
          "div",
          { class: "row", style: "align-items:center;gap:8px" },
          h("h2", { style: "margin:0" }, "自動驗收"),
          h("span", { class: "grow" }),
          verifyButton
        ),
        h(
          "div",
          { class: "small muted", style: "margin-top:6px" },
          "會開啟 Ren'Py 視窗自動操作約 1–3 分鐘，請不要點它或關掉。"
        ),
        verifyProgress,
        verifySummaryBlock()
      );
      verifyHost.replaceChildren(verifyCard);
      if (running === "verify" && liveJob) progressCard(verifyProgress, VERIFY_TITLE.running, liveJob);
      else verifyProgress.hidden = true;
    }

    /* ---------------------------------------------------------------- 打包 Windows 版 */
    function packageSummaryBlock() {
      const dist = data.dist || {};
      const artifacts = Array.isArray(dist.artifacts) ? dist.artifacts : [];
      const status = packageJob ? packageJob.status : "";
      const log = packageJob ? packageJobLog(packageJob) : [];
      return h(
        "div",
        {},
        packageJob
          ? h(
              "div",
              { class: "row", style: "align-items:center;gap:8px;margin-top:8px" },
              h("strong", {}, PACKAGE_TITLE[status] || status || "結果"),
              badge(status === "error" ? "有錯誤" : status === "stopped" ? "已停止" : "完成", status === "error" ? "bad" : status === "stopped" ? "warn" : "ok")
            )
          : "",
        status === "error" && log.length
          ? h("pre", { class: "log bad small", style: "white-space:pre-wrap;margin-top:6px" }, log.join("\n"))
          : "",
        artifacts.length
          ? h(
              "div",
              { style: "margin-top:8px" },
              h("div", { class: "small muted" }, `成品資料夾：${dist.dir || ""}`),
              ...artifacts.map((item) =>
                h(
                  "div",
                  { class: "small", style: "margin:2px 0" },
                  h("code", {}, item.name),
                  `　${sizeText(item.bytes)}`,
                  item.entries ? `　${item.entries} 個檔案` : "",
                  item.has_exe === false ? h("span", { class: "bad" }, "　⚠ 裡面沒有 .exe") : "",
                  Array.isArray(item.forbidden) && item.forbidden.length
                    ? h("div", { class: "bad" }, `⚠ 裡面有不該出貨的檔案：${item.forbidden.slice(0, 5).join("、")}${item.forbidden.length > 5 ? "…" : ""}`)
                    : ""
                )
              )
            )
          : h("div", { class: "muted small", style: "margin-top:8px" }, "還沒有打包成品")
      );
    }

    function packageJobLog(job) {
      const log = job && Array.isArray(job.log) ? job.log : [];
      return log.slice(-12).map((line) => String(line));
    }

    function paintPackage() {
      const packageButton = h("button", { disabled: !!running }, "打包 Windows 版");
      packageButton.addEventListener("click", () => startJob("package", "/export/package", {}));
      const packageCard = h("div", { class: "card" });
      packageCard.append(
        h(
          "div",
          { class: "row", style: "align-items:center;gap:8px" },
          h("h2", { style: "margin:0" }, "打包 Windows 版"),
          h("span", { class: "grow" }),
          packageButton
        ),
        h(
          "div",
          { class: "small muted", style: "margin-top:6px" },
          "用 Ren'Py 內建的打包功能做出 Windows 版壓縮檔（zip），可能要幾分鐘。建議先跑過自動驗收；匯出內容改了要重新打包。"
        ),
        packageProgress,
        packageSummaryBlock()
      );
      packageHost.replaceChildren(packageCard);
      if (running === "package" && liveJob) progressCard(packageProgress, PACKAGE_TITLE.running, liveJob);
      else packageProgress.hidden = true;
    }

    /* ---------------------------------------------------------------- 驗收報告 */
    async function showReport(report) {
      try {
        const data = await api(`${base}/export/report?name=${encodeURIComponent(report.name)}`);
        const screens = Array.isArray(report.screens) ? report.screens : [];
        reportView.hidden = false;
        reportView.replaceChildren(
          h("h3", {}, `報告：${report.name}`),
          h("pre", { class: "log", style: "white-space:pre-wrap" }, data.text || ""),
          h(
            "div",
            { class: "row", style: "flex-wrap:wrap;gap:8px;margin-top:6px" },
            ...screens.map((screen) => {
              const src = `${base}/export/shot?report=${encodeURIComponent(report.name)}&name=${encodeURIComponent(screen)}`;
              return h("img", {
                src,
                title: screen,
                alt: screen,
                loading: "lazy",
                style: "width:220px;border-radius:6px;cursor:zoom-in",
                onclick: () => window.open(src, "_blank"),
              });
            })
          )
        );
      } catch (error) {
        fail(error);
      }
    }

    function paintReports() {
      const reports = data.reports || [];
      reportView = h("div", { class: "card", hidden: true });
      const rows = reports.map((report) =>
        h(
          "div",
          { class: "row", style: "align-items:center;gap:8px;margin:2px 0" },
          h("code", { class: "small" }, report.name),
          report.has_report ? badge("有報告", "ok") : badge("只有截圖", "warn"),
          h("span", { class: "grow" }),
          h("button", { class: "sm", onclick: () => showReport(report) }, "看報告")
        )
      );
      reportsHost.replaceChildren(
        h(
          "div",
          { class: "card" },
          h("h2", { style: "margin-top:0" }, "驗收報告"),
          reports.length
            ? h("div", {}, ...rows)
            : h("div", { class: "muted" }, "還沒有驗收報告"),
          reportView
        )
      );
    }

    function paintAll() {
      paintTitle();
      paintSettings();
      paintExport();
      paintVerify();
      paintPackage();
      paintReports();
    }

    wrap.append(
      h("h1", {}, "輸出"),
      h(
        "div",
        { class: "card small muted" },
        "流程：先做標題素材（標題圖與標題音樂）→ 匯出到 Ren'Py 專案 → 跑自動驗收看報告 → 打包 Windows 版。"
      ),
      titleHost,
      settingsHost,
      exportHost,
      verifyHost,
      packageHost,
      reportsHost
    );
    paintAll();
    return wrap;
  }

  PAGES.export = async function (bookId) {
    crumbs([
      { label: "書架", href: "#/" },
      { label: bookId, href: `#/book/${bookId}` },
      { label: "輸出" },
    ]);
    $("#view").replaceChildren(h("div", { class: "muted" }, "載入中…"));
    try {
      $("#view").replaceChildren(await render(bookId));
    } catch (error) {
      $("#view").replaceChildren(h("div", { class: "card bad" }, error.message));
    }
  };
})();
