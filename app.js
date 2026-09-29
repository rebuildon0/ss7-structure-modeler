const fileInput = document.querySelector("#csv-file");
const dropZone = document.querySelector("#drop-zone");
const browseButton = document.querySelector("#browse-button");
const selectedFilePanel = document.querySelector("#selected-file");
const selectedName = document.querySelector("#selected-name");
const selectedSize = document.querySelector("#selected-size");
const changeFileButton = document.querySelector("#change-file");
const prefixInput = document.querySelector("#output-prefix");
const convertButton = document.querySelector("#convert-button");
const progressPanel = document.querySelector("#progress-panel");
const resultPanel = document.querySelector("#result-panel");
const errorPanel = document.querySelector("#error-panel");
const liveStatus = document.querySelector("#live-status");

let selectedFile = null;
let conversionWorker = null;
let objectUrls = [];

function clearObjectUrls() {
  for (const url of objectUrls) URL.revokeObjectURL(url);
  objectUrls = [];
}

function fileUrl(file) {
  const url = URL.createObjectURL(new Blob([file.bytes], { type: file.type }));
  objectUrls.push(url);
  return url;
}

function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function defaultPrefix(filename) {
  return filename.replace(/\.csv$/i, "").replace(/[^A-Za-z0-9_-]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 80) || "ss7";
}

function chooseFile(file) {
  if (!file) return;
  if (!file.name.toLowerCase().endsWith(".csv")) {
    showError("CSVファイルを選んでください。");
    return;
  }
  if (file.size > 100 * 1024 * 1024) {
    showError("CSVは100MB以下にしてください。");
    return;
  }
  selectedFile = file;
  selectedName.textContent = file.name;
  selectedSize.textContent = `${formatBytes(file.size)}・元ファイルは変更されません`;
  selectedFilePanel.hidden = false;
  dropZone.hidden = true;
  prefixInput.value = defaultPrefix(file.name);
  convertButton.disabled = false;
  errorPanel.hidden = true;
  resultPanel.hidden = true;
  liveStatus.textContent = `${file.name}を選択しました`;
}

function resetSelection() {
  clearObjectUrls();
  selectedFile = null;
  fileInput.value = "";
  selectedFilePanel.hidden = true;
  dropZone.hidden = false;
  convertButton.disabled = true;
  progressPanel.hidden = true;
  resultPanel.hidden = true;
  errorPanel.hidden = true;
  dropZone.focus();
}

function showError(message, detail = "") {
  progressPanel.hidden = true;
  resultPanel.hidden = true;
  errorPanel.hidden = false;
  document.querySelector("#error-message").textContent = message;
  const details = document.querySelector("#error-details");
  details.hidden = !detail;
  document.querySelector("#error-detail-text").textContent = detail;
  liveStatus.textContent = message;
  errorPanel.scrollIntoView({ behavior: "smooth", block: "center" });
}

function labelForKind(kind) {
  return ({
    COLUMNS: "柱", BEAMS: "梁", SMALL_BEAMS: "小梁", WALLS: "壁", OFFFRAME_WALLS: "フレーム外雑壁",
    VBRACES: "鉛直ブレース", HBRACES: "水平ブレース", SLABS: "床",
    CANTILEVER_SLABS: "片持床", CORNER_SLABS: "出隅床"
  })[kind] || kind;
}

function artifactExtension(label) {
  if (label.includes("3DM")) return "3DM";
  if (label === "変換レポート") return "MD";
  if (label === "監査用JSON") return "JSON";
  return label;
}

function renderResult(data) {
  progressPanel.hidden = true;
  errorPanel.hidden = true;
  resultPanel.hidden = false;
  document.querySelector("#project-name").textContent = `${data.projectName}｜${data.stories.join("・")}｜${data.layers.join("・")}`;

  const verification = data.verification || {};
  const banner = document.querySelector("#verification-banner");
  if (verification.valid) {
    banner.classList.remove("warning");
    banner.textContent = `✓ 3DM検証正常：${verification.object_count_readback}オブジェクト・${verification.layer_count_readback}レイヤー・無効形状0・単位mm`;
  } else {
    banner.classList.add("warning");
    banner.textContent = "3DMの自動検証に注意事項があります。変換レポートを確認してください。";
  }

  const countGrid = document.querySelector("#count-grid");
  countGrid.replaceChildren();
  const kindOrder = [
    "COLUMNS", "BEAMS", "SMALL_BEAMS", "WALLS", "OFFFRAME_WALLS", "VBRACES",
    "HBRACES", "SLABS", "CANTILEVER_SLABS", "CORNER_SLABS"
  ];
  for (const kind of kindOrder) {
    const item = document.createElement("div");
    item.className = "count-item";
    const count = document.createElement("strong");
    count.textContent = data.counts[kind] || 0;
    const label = document.createElement("span");
    label.textContent = labelForKind(kind);
    item.append(count, label);
    countGrid.append(item);
  }

  const artifactList = document.querySelector("#artifact-list");
  artifactList.replaceChildren();
  for (const artifact of data.artifacts) {
    const link = document.createElement("a");
    link.className = "artifact-link";
    link.href = artifact.url;
    link.download = artifact.filename;
    link.target = artifact.label === "Rhino 3DM" ? "_self" : "_blank";
    const type = document.createElement("span");
    type.className = "artifact-type";
    type.textContent = artifactExtension(artifact.label);
    const copy = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = artifact.label;
    const description = document.createElement("small");
    description.textContent = artifact.description;
    copy.append(title, description);
    const size = document.createElement("span");
    size.className = "artifact-size";
    size.textContent = formatBytes(artifact.size);
    link.append(type, copy, size);
    artifactList.append(link);
  }

  const previewFigure = document.querySelector("#preview-figure");
  const previewImage = document.querySelector("#preview-image");
  previewFigure.hidden = !data.previewUrl;
  if (data.previewUrl) previewImage.src = data.previewUrl;

  const notices = document.querySelector("#notices");
  notices.replaceChildren();
  const noticeItems = [];
  for (const item of data.skippedSections || []) noticeItems.push(`${item.section}：${item.row_count}行は未展開`);
  for (const item of data.fallbackSections || []) noticeItems.push(`${item.member}：仮断面 ${item.row_count}行`);
  if (noticeItems.length) {
    const title = document.createElement("strong");
    title.textContent = "確認してください";
    const list = document.createElement("ul");
    for (const text of noticeItems) {
      const item = document.createElement("li");
      item.textContent = text;
      list.append(item);
    }
    notices.append(title, list);
    notices.hidden = false;
  } else {
    notices.hidden = true;
  }

  liveStatus.textContent = `変換が完了しました。${verification.object_count_readback || 0}オブジェクトです。`;
  resultPanel.scrollIntoView({ behavior: "smooth", block: "start" });
}

async function convert() {
  if (!selectedFile) return;
  convertButton.disabled = true;
  resultPanel.hidden = true;
  errorPanel.hidden = true;
  progressPanel.hidden = false;
  progressPanel.scrollIntoView({ behavior: "smooth", block: "center" });
  liveStatus.textContent = "変換を開始しました";
  try {
    const prefix = prefixInput.value.trim() || defaultPrefix(selectedFile.name);
    if (!conversionWorker) conversionWorker = new Worker("./converter-worker.mjs", { type: "module" });
    const buffer = await selectedFile.arrayBuffer();
    const data = await new Promise((resolve, reject) => {
      conversionWorker.onmessage = ({ data: message }) => {
        if (message.type === "status") {
          document.querySelector("#progress-title").textContent = message.message;
          return;
        }
        if (message.type === "error") reject(new Error(message.message));
        if (message.type === "complete") resolve(message.result);
      };
      conversionWorker.onerror = () => reject(new Error("変換エンジンを読み込めませんでした。通信環境を確認してください。"));
      conversionWorker.postMessage({ type: "convert", buffer, prefix }, [buffer]);
    });
    clearObjectUrls();
    data.artifacts = data.artifacts.map((file) => ({ ...file, url: fileUrl(file), size: file.bytes.byteLength }));
    data.previewUrl = data.preview ? fileUrl(data.preview) : null;
    renderResult(data);
  } catch (error) {
    showError(error.message || "変換できませんでした。", error.detail || "");
  } finally {
    convertButton.disabled = !selectedFile;
  }
}

browseButton.addEventListener("click", (event) => { event.stopPropagation(); fileInput.click(); });
changeFileButton.addEventListener("click", resetSelection);
document.querySelector("#new-conversion").addEventListener("click", resetSelection);
document.querySelector("#retry-button").addEventListener("click", resetSelection);
convertButton.addEventListener("click", convert);
fileInput.addEventListener("change", () => chooseFile(fileInput.files[0]));
dropZone.addEventListener("click", () => fileInput.click());
dropZone.addEventListener("keydown", (event) => {
  if (event.key === "Enter" || event.key === " ") { event.preventDefault(); fileInput.click(); }
});
for (const eventName of ["dragenter", "dragover"]) {
  dropZone.addEventListener(eventName, (event) => { event.preventDefault(); dropZone.classList.add("dragging"); });
}
for (const eventName of ["dragleave", "drop"]) {
  dropZone.addEventListener(eventName, (event) => { event.preventDefault(); dropZone.classList.remove("dragging"); });
}
dropZone.addEventListener("drop", (event) => chooseFile(event.dataTransfer.files[0]));
