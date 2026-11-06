/* fekit filepicker.js — 檔案／資料夾選擇對話框。介面與合約見 docs/02_API合約.md 第 9.3 節，
 * 後端端點見第 5 節（GET /api/fs/list）。只加上 FE.filepicker；載入本身不動 DOM。 */
(function () {
  "use strict";

  const FE = window.FE;
  if (!FE) {
    console.error("fekit：filepicker.js 要在 core.js 之後載入");
    return;
  }
  if (FE.filepicker) {
    console.warn("fekit：filepicker.js 被載入了兩次，第二次已略過");
    return;
  }

  const { h } = FE;
  let active = null;   // 目前開著的選擇器，同一時間只開一個

  const join = (dir, name, sep) => (dir.endsWith(sep) || dir.endsWith("/") ? dir + name : dir + sep + name);

  function open(options = {}) {
    if (active) active.cancel();

    const mode = options.mode === "dir" ? "dir" : "file";
    const base = options.base || "/api/fs/list";
    const exts = mode === "file" && Array.isArray(options.exts) ? options.exts.join(",") : "";
    const lastKey = `filepicker.last.${mode}`;
    const start = options.start !== undefined && options.start !== null ? String(options.start) : FE.store.get(lastKey, "");

    let data = null;          // 最後一次成功的回應
    let selected = null;      // 檔案模式：選取的檔名
    let generation = 0;
    let ctrl = null;
    let closed = false;
    let closeModal = null;

    const pathInput = h("input", {
      type: "text",
      class: "fp-path",
      "aria-label": "路徑",
      spellcheck: "false",
      autocomplete: "off",
      onkeydown: (event) => {
        if (event.key === "Enter" && !event.isComposing) {
          event.preventDefault();
          go(pathInput.value.trim());
        }
      },
    });
    const upBtn = h("button", { class: "btn btn-sm", type: "button", onclick: () => data && data.parent !== null && go(data.parent) }, "上一層");
    const rootsBtn = h("button", { class: "btn btn-sm", type: "button", onclick: () => go("") }, "磁碟機");
    const listEl = h("ul", { class: "fp-list", "aria-busy": "false" });
    const noteEl = h("div", { class: "fp-note" });
    const okBtn = h("button", { class: "btn btn-primary", type: "button", disabled: true, onclick: () => accept() }, options.ok || "選擇");
    const cancelBtn = h("button", { class: "btn", type: "button", onclick: () => cancel() }, "取消");

    function currentChoice() {
      if (!data || data.path === "") return null;
      if (mode === "dir") return data.path;
      return selected === null ? null : join(data.path, selected, data.sep || "\\");
    }

    function finish(value) {
      if (closed) return;
      closed = true;
      generation += 1;
      if (ctrl) ctrl.abort();
      if (data && data.path) FE.store.set(lastKey, data.path);
      if (active === handle) active = null;
      closeModal(value);
    }

    function accept() {
      const choice = currentChoice();
      if (choice !== null) finish(choice);
    }

    function cancel() {
      finish(null);
    }

    function refreshButtons() {
      const choice = currentChoice();
      okBtn.disabled = choice === null;
      upBtn.disabled = !data || data.parent === null || data.parent === undefined;
      rootsBtn.disabled = !data || data.path === "" || !(data.roots && data.roots.length);
      noteEl.textContent = choice === null ? "" : choice;
    }

    function item(label, icon, extra) {
      return h("li", {}, h("button", { class: "fp-item", type: "button", ...extra },
        h("span", { class: "fp-icon", "aria-hidden": "true" }, icon), h("span", { class: "fp-name" }, label)));
    }

    function pick(name) {
      selected = name;
      for (const node of listEl.querySelectorAll(".fp-file")) node.setAttribute("aria-pressed", String(node.dataset.name === name));
      refreshButtons();
    }

    function draw() {
      listEl.replaceChildren();
      if (data.path === "") {
        for (const root of data.roots || []) listEl.append(item(root, "💽", { onclick: () => go(root) }));
        if (!(data.roots || []).length) listEl.append(h("li", { class: "fp-empty" }, "找不到可以瀏覽的位置"));
      } else {
        for (const name of data.dirs) listEl.append(item(name, "📁", { onclick: () => go(join(data.path, name, data.sep)) }));
        if (mode === "file") {
          for (const name of data.files) {
            const li = item(name, "📄", {
              class: "fp-item fp-file",
              "data-name": name,
              "aria-pressed": String(name === selected),
              onclick: () => pick(name),
              ondblclick: () => {
                pick(name);
                accept();
              },
            });
            listEl.append(li);
          }
        }
        if (!data.dirs.length && !(mode === "file" && data.files.length)) {
          listEl.append(h("li", { class: "fp-empty" }, mode === "file" ? "這個資料夾裡沒有可以選的檔案" : "這個資料夾裡沒有子資料夾"));
        }
      }
      listEl.scrollTop = 0;
    }

    /** 前往 path。成功 true；失敗 false；被後來的請求取代或已關閉 null。 */
    async function go(path) {
      if (closed) return null;
      generation += 1;
      const mine = generation;
      if (ctrl) ctrl.abort();
      ctrl = new AbortController();
      listEl.setAttribute("aria-busy", "true");
      try {
        const reply = await FE.api(base, { query: { path, mode, exts: exts || undefined }, signal: ctrl.signal });
        if (mine !== generation || closed) return null;
        data = reply;
        selected = null;
        pathInput.value = reply.path;
        // 輸入的是檔案路徑：後端回的是它所在的資料夾，順手把那個檔案選起來。
        if (mode === "file" && path && path.length > reply.path.length && path.toLowerCase().startsWith(reply.path.toLowerCase())) {
          const rest = path.slice(reply.path.length).replace(/^[\\/]+/, "");
          if (reply.files.includes(rest)) selected = rest;
        }
        draw();
        refreshButtons();
        return true;
      } catch (error) {
        if (mine !== generation || closed || (error && error.name === "AbortError")) return null;
        FE.fail(error);
        return false;
      } finally {
        if (mine === generation) listEl.setAttribute("aria-busy", "false");
      }
    }

    const handle = { cancel };
    active = handle;

    closeModal = FE.modal({
      title: options.title || (mode === "dir" ? "選擇資料夾" : "選擇檔案"),
      body: h("div", { class: "fp" },
        h("div", { class: "fp-bar" }, upBtn, rootsBtn, pathInput),
        listEl,
        noteEl),
      foot: () => [cancelBtn, okBtn],
      onclose: () => {
        // Esc、點背景關閉：視為取消。
        if (!closed) {
          closed = true;
          generation += 1;
          if (ctrl) ctrl.abort();
          if (data && data.path) FE.store.set(lastKey, data.path);
          if (active === handle) active = null;
        }
      },
    });

    refreshButtons();
    go(start).then((ok) => {
      // 起始位置打不開（例如不是完整路徑）：退回磁碟機清單。被使用者的操作取代時不要動。
      if (ok === false && start && !closed) return go("");
      return ok;
    });

    return closeModal.closed.then((value) => (typeof value === "string" ? value : null));
  }

  FE.filepicker = { open };
})();
