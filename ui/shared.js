/* 專案共用：顯示文字對照與小工具。載入順序：core → globals → jobs → filepicker → 本檔 → 各頁 → shell。 */
"use strict";

const EXPRS = [["neutral", "平常"], ["serious", "認真"], ["worried", "不安"], ["angry", "生氣"],
  ["surprised", "驚訝"], ["sad", "難過"], ["embarrassed", "尷尬"], ["smile", "開心"]];
const TIMES = [["dawn", "清晨"], ["morning", "上午"], ["day", "白天"], ["dusk", "黃昏"], ["night", "夜晚"]];
const POS = [["left", "左"], ["center", "中"], ["right", "右"]];
const ROLES = [["protagonist", "主角"], ["main", "主要配角"], ["supporting", "配角"],
  ["antagonist", "反派"], ["minor", "路人"]];
const SHAPES = [["tall", "高"], ["broad", "壯"], ["small", "小"], ["bent", "彎"], ["round", "圓"]];
const EFFECTS = [["none", "無"], ["shake", "震動"], ["flash", "閃光"], ["fade_in", "淡入"], ["fade_out", "淡出"]];
const WEATHER = [["", "（天氣）"], ["clear", "晴"], ["cloudy", "陰"], ["rain", "雨"], ["storm", "暴風雨"], ["fog", "霧"]];

const STATE_LABEL = { none: ["未標註", ""], draft: ["待校對", "warn"], reviewed: ["已校對", "ok"] };
const OUTLINE_LABEL = { library: ["書庫章綱", "info"], extract: ["已抽取", "info"], none: ["無", ""] };
const KIND_LABEL = { annotate: "AI 標註", extract: "抽取章綱與角色", art: "出圖（ComfyUI）",
  tts: "語音生成", export: "匯出 Ren'Py", verify: "自動驗收", package: "打包 Windows 版" };
const ITEM_ICON = { pending: ["○", ""], running: ["●", "info"], done: ["✔", "ok"],
  skipped: ["↷", ""], error: ["✖", "bad"], stopped: ["■", "warn"] };

/* 舊 class 名稱 → fekit 的 class（P4 之後呼叫端直接寫 chipClass） */
const CHIP_KIND = { "": "", ok: "ok", warn: "warn", bad: "err", info: "info", err: "err" };
function chipClass(kind) {
  const key = CHIP_KIND[kind] === undefined ? kind : CHIP_KIND[kind];
  return key ? "chip chip-" + key : "chip";
}
function tabClass(on) { return on ? "tab active" : "tab"; }
function badge(label, kind) { return h("span", { class: chipClass(kind) }, label); }
function options(pairs, current) {
  return pairs.map(([value, label]) => h("option", { value, selected: value === current }, label));
}
function select(pairs, current, onchange, cls) {
  return h("select", { class: cls || "", onchange: (e) => onchange(e.target.value) }, options(pairs, current));
}

/* 舊的 toast 種類（""、"bad"）→ FE.toast 的種類（ok／warn／err／info）。
   這是舊呼叫端的轉接，不是 fekit 的一部分（§8.2-6：60+ 處呼叫端不值得一次改完）。 */
function toast(message, kind) {
  return FE.toast(message, kind === "bad" ? "err" : kind || "info");
}

/* 舊頁面（page_appearance.js）用 uploadFile；fekit 的同功能函式叫 FE.upload／全域 upload，這裡補舊名字 */
function uploadFile(path, file) { return FE.upload(path, file); }

/* 舊的 browse：檔案／資料夾選擇一律走 fekit 的選擇器（§8.2-2）。取消時回 null，這裡當成空字串。 */
async function browse(onPick, { dirs = false, start = "" } = {}) {
  const path = await FE.filepicker.open({ mode: dirs ? "dir" : "file", start: start || undefined });
  if (path) onPick(path);
}
const browseFiles = (onPick, start) => browse(onPick, { start });

/* 原生 prompt() 的取代品：回傳 Promise<string>（取消是空字串） */
function ask(message, value = "", { title = "輸入", ok = "確定" } = {}) {
  return new Promise((resolve) => {
    const input = h("input", { value, style: "width:100%", "data-autofocus": "" });
    let done = false;
    let closeModal = null;
    const finish = (result) => { if (!done) { done = true; resolve(result); } };
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && closeModal) closeModal(input.value.trim());
    });
    FE.modal({
      title,
      body: h("div", { class: "stack" }, h("p", { style: "margin:0" }, message), input),
      foot: (close) => {
        closeModal = close;
        return [
          h("button", { class: "btn", type: "button", onclick: () => close("") }, "取消"),
          h("button", { class: "btn btn-primary", type: "button", onclick: () => close(input.value.trim()) }, ok),
        ];
      },
      onclose: (result) => finish(typeof result === "string" ? result : ""),
    });
  });
}

/* 頂列的 AI 狀態 chip（shell.js 建立節點，這裡只更新） */
async function refreshChip() {
  const chip = document.querySelector("[data-llm-chip]");
  if (!chip) return;
  try {
    const state = await FE.api("/api/state");
    chip.className = chipClass(state.llm.key_ok ? "ok" : "bad");
    chip.textContent = state.llm.key_ok
      ? `AI：${state.default_executor || state.llm.provider} / ${state.llm.model || "（未選模型）"}`
      : "AI：沒有金鑰";
    chip.title = state.llm.key_ok ? "" : state.llm.message;
  } catch (_) {
    chip.textContent = "";
  }
}
