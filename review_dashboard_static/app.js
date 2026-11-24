const tabs = [
  ["all", "All"],
  ["ready", "Ready"],
  ["needs_review", "Needs Review"],
  ["needs_extraction", "Needs Extraction"],
  ["manual", "Manual"],
  ["duplicates", "Duplicates"],
  ["posted", "Posted"],
  ["rejected", "Rejected"],
  ["low_confidence", "Low Confidence"],
  ["missing_branch", "Missing Branch"],
  ["missing_account", "Missing Account"],
  ["unsupported_type", "Unsupported Type"],
];

const state = {
  filter: "all",
  queue: [],
  selectedIndex: -1,
  detail: null,
  metrics: null,
};

const $ = (id) => document.getElementById(id);

function toast(message) {
  const el = $("toast");
  el.textContent = message;
  el.classList.add("show");
  window.setTimeout(() => el.classList.remove("show"), 2200);
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const text = await res.text();
  let payload;
  try {
    payload = text ? JSON.parse(text) : {};
  } catch {
    payload = { ok: false, error: text };
  }
  if (!res.ok || payload.ok === false) {
    throw new Error(payload.error || `Request failed: ${res.status}`);
  }
  return payload;
}

function renderTabs() {
  const root = $("tabs");
  root.innerHTML = "";
  tabs.forEach(([key, label]) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = label;
    btn.className = key === state.filter ? "active" : "";
    btn.addEventListener("click", () => {
      state.filter = key;
      state.selectedIndex = -1;
      loadQueue();
    });
    root.appendChild(btn);
  });
}

function formatAmount(amount, currency) {
  if (amount === null || amount === undefined || amount === "") return `${currency || "AED"} ?`;
  const num = Number(amount);
  if (!Number.isFinite(num)) return `${currency || "AED"} ${amount}`;
  return `${currency || "AED"} ${num.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function renderMetrics(metrics) {
  state.metrics = metrics;
  const values = [
    ["Queue", metrics.total_items],
    ["Local Files", metrics.local_files_found],
    ["Matched", metrics.files_matched_to_extracted_json],
    ["No JSON", metrics.files_missing_extraction],
    ["Approved", metrics.approved],
    ["Needs", metrics.needs_review],
  ];
  $("metrics").innerHTML = values.map(([label, value]) => (
    `<div class="metric"><strong>${value}</strong><span>${label}</span></div>`
  )).join("");
  renderCounts(metrics);
}

function renderCounts(metrics) {
  const branchCounts = Object.entries(metrics.branch_counts || {}).sort((a, b) => b[1] - a[1]);
  const statusCounts = Object.entries(metrics.extraction_status_counts || {}).sort((a, b) => b[1] - a[1]);
  $("counts").innerHTML = `
    <div><strong>Branches</strong>${branchCounts.map(([name, count]) => `<span>${escapeHtml(name)} ${count}</span>`).join("") || "<span>No branch counts</span>"}</div>
    <div><strong>Extraction</strong>${statusCounts.map(([name, count]) => `<span>${escapeHtml(name)} ${count}</span>`).join("") || "<span>No extraction counts</span>"}</div>
  `;
}

function renderQueue() {
  const root = $("queueList");
  root.innerHTML = "";
  if (!state.queue.length) {
    root.innerHTML = "<p class=\"queue-meta\">No items in this view.</p>";
    return;
  }
  state.queue.forEach((item, index) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = `queue-item ${index === state.selectedIndex ? "selected" : ""}`;
    btn.innerHTML = `
      <div class="queue-main">
        <span class="queue-vendor">${escapeHtml(item.vendor)}</span>
        <span class="queue-amount">${escapeHtml(formatAmount(item.amount, item.currency))}</span>
      </div>
      <div class="queue-meta">${escapeHtml([item.date || "No date", item.branch || "No branch", item.document_type].join(" · "))}</div>
      <div class="queue-meta">${escapeHtml(item.posting_action)} · ${escapeHtml(item.extraction_status)} · ${escapeHtml(item.status)}</div>
      <div class="queue-flags">${renderQueueFlags(item)}</div>
    `;
    btn.addEventListener("click", () => selectIndex(index));
    root.appendChild(btn);
  });
}

function renderQueueFlags(item) {
  const buckets = item.warning_buckets || {};
  const parts = [];
  if (buckets.critical) parts.push(`${buckets.critical} critical`);
  if (buckets.review) parts.push(`${buckets.review} review`);
  if (buckets.info) parts.push(`${buckets.info} info`);
  return escapeHtml(parts.join(" · ") || "No warnings");
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    "\"": "&quot;",
    "'": "&#039;",
  })[char]);
}

async function loadQueue() {
  renderTabs();
  const [queue, metrics] = await Promise.all([
    api(`/api/queue?filter=${encodeURIComponent(state.filter)}`),
    api("/api/metrics"),
  ]);
  state.queue = queue.items || [];
  renderMetrics(metrics);
  if (state.queue.length) {
    selectIndex(Math.max(0, Math.min(state.selectedIndex, state.queue.length - 1)));
  } else {
    state.selectedIndex = -1;
    state.detail = null;
    renderQueue();
    renderEmpty();
  }
}

async function selectIndex(index) {
  if (index < 0 || index >= state.queue.length) return;
  state.selectedIndex = index;
  renderQueue();
  const key = state.queue[index].key;
  state.detail = await api(`/api/item/${encodeURIComponent(key)}`);
  renderDetail();
}

function setFormValues(values) {
  const form = $("editForm");
  for (const [key, value] of Object.entries(values || {})) {
    const field = form.elements.namedItem(key);
    if (field) field.value = value ?? "";
  }
}

function formValues() {
  const data = {};
  const form = $("editForm");
  Array.from(form.elements).forEach((field) => {
    if (field.name) data[field.name] = field.value;
  });
  return data;
}

function renderEmpty() {
  $("fileName").textContent = "No item selected";
  $("sourcePath").textContent = "";
  $("documentFrame").innerHTML = "<p>Select an item from the queue.</p>";
  $("reviewTitle").textContent = "AI Result";
  $("reviewSubtitle").textContent = "Extraction and accounting decision";
  $("statusPill").textContent = "pending";
  $("warnings").innerHTML = "";
  $("fieldConfidence").innerHTML = "";
  $("missingReasons").innerHTML = "";
  $("inferenceSources").innerHTML = "";
  setFormValues({});
  $("decisionExplanation").textContent = "-";
  updateActionState();
}

function renderDetail() {
  const detail = state.detail;
  const item = detail.item || {};
  const source = (detail.sources || [])[0] || {};
  $("fileName").textContent = source.name || detail.key;
  $("sourcePath").textContent = source.path || "Source missing";
  $("reviewTitle").textContent = item.supplier_name || "Unknown vendor";
  $("reviewSubtitle").textContent = `${item.invoice_number || "No invoice #"} · ${formatAmount(item.total_amount, item.currency)}`;
  $("statusPill").textContent = detail.status || "pending";
  setFormValues(detail.editable || {});
  $("decisionExplanation").textContent = item.decision_explanation || (item.accounting_decision || {}).decision_explanation || "-";
  renderWarnings(detail.warnings || []);
  renderFieldConfidence(detail.field_confidences || {});
  renderMissingReasons(detail.missing_reasons || {});
  renderInferenceSources(detail.inference_sources || []);
  renderDocument(source);
  updateActionState();
}

function renderWarnings(warnings) {
  const root = $("warnings");
  if (!warnings.length) {
    root.innerHTML = "<span class=\"warning\">No warnings</span>";
    return;
  }
  const groups = {
    critical: warnings.filter((warning) => warning.level === "critical"),
    review: warnings.filter((warning) => warning.level === "review" || !warning.level),
    info: warnings.filter((warning) => warning.level === "info"),
  };
  root.innerHTML = Object.entries(groups)
    .filter(([, items]) => items.length)
    .map(([level, items]) => `
      <div class="warning-group">
        <strong>${level === "critical" ? "Critical" : level === "info" ? "Informational" : "Needs Review"}</strong>
        <div>${items.map((warning) => `
          <span class="warning ${level}">
            ${escapeHtml(warning.label)}
            ${warning.reason ? `<small>${escapeHtml(warning.reason)}</small>` : ""}
          </span>
        `).join("")}</div>
      </div>
    `).join("");
}

function renderFieldConfidence(confidences) {
  const entries = Object.entries(confidences);
  if (!entries.length) {
    $("fieldConfidence").innerHTML = "";
    return;
  }
  $("fieldConfidence").innerHTML = `
    <h3>Field Confidence</h3>
    <div class="confidence-grid">
      ${entries.map(([, info]) => `<span class="confidence ${String(info.confidence || "").toLowerCase()}"><strong>${escapeHtml(info.label)}</strong>${escapeHtml(info.confidence)}<small>${escapeHtml(info.source || "")}</small></span>`).join("")}
    </div>
  `;
}

function renderMissingReasons(reasons) {
  const entries = Object.entries(reasons);
  if (!entries.length) {
    $("missingReasons").innerHTML = "";
    return;
  }
  $("missingReasons").innerHTML = `
    <h3>Missing Values</h3>
    ${entries.map(([field, reason]) => `<p><strong>${escapeHtml(field.replaceAll("_", " "))}</strong>: ${escapeHtml(reason)}</p>`).join("")}
  `;
}

function renderInferenceSources(sources) {
  if (!sources.length) {
    $("inferenceSources").innerHTML = "";
    return;
  }
  $("inferenceSources").innerHTML = `<strong>Inference sources</strong>${sources.map((source) => `<span>${escapeHtml(source)}</span>`).join("")}`;
}

function renderDocument(source) {
  const root = $("documentFrame");
  if (!source || !source.document_url) {
    root.innerHTML = "<p>Source file missing or outside project data folders.</p>";
    return;
  }
  const mime = source.mime_type || "";
  if (mime.startsWith("image/")) {
    root.innerHTML = `<img src="${source.document_url}" alt="Source document">`;
  } else if (mime === "application/pdf") {
    root.innerHTML = `<iframe src="${source.document_url}" title="Source PDF"></iframe>`;
  } else {
    root.innerHTML = "<p>This file type cannot be previewed here. Use Open File.</p>";
  }
}

async function saveEdits() {
  if (!state.detail) return;
  if (state.detail.read_only) {
    toast("No Extracted JSON: run extraction before saving.");
    return;
  }
  const key = state.detail.key;
  await api(`/api/item/${encodeURIComponent(key)}/save`, {
    method: "POST",
    body: JSON.stringify({ fields: formValues() }),
  });
  toast("Edits saved");
  await loadQueue();
}

async function action(actionName) {
  if (!state.detail) return;
  if (state.detail.read_only) {
    toast("No Extracted JSON: run extraction before review actions.");
    return;
  }
  if (actionName === "approve_learn") {
    return approveAndLearn();
  }
  const key = state.detail.key;
  const reason = $("editForm").elements.namedItem("review_reason")?.value || "";
  await api(`/api/item/${encodeURIComponent(key)}/action`, {
    method: "POST",
    body: JSON.stringify({ action: actionName, reason, fields: formValues() }),
  });
  toast(actionName.replace("_", " ") + " saved");
  const next = Math.min(state.selectedIndex + 1, Math.max(0, state.queue.length - 1));
  state.selectedIndex = next;
  await loadQueue();
}

async function approveAndLearn() {
  const key = state.detail.key;
  const reason = $("editForm").elements.namedItem("review_reason")?.value || "";
  const fields = formValues();
  const preview = await api(`/api/item/${encodeURIComponent(key)}/learn-preview`, {
    method: "POST",
    body: JSON.stringify({ action: "approve_learn", reason, fields }),
  });
  const changes = preview.changes || [];
  const lines = changes.map((change) => `${change.rule}: ${JSON.stringify(change.values)}`).join("\n");
  const confirmed = window.confirm(`Learning preview\n\n${lines || "No learnable supplier/account fields found."}\n\nConfirm Approve & Learn for this document only?`);
  if (!confirmed) {
    toast("Learning cancelled");
    return;
  }
  await api(`/api/item/${encodeURIComponent(key)}/action`, {
    method: "POST",
    body: JSON.stringify({ action: "approve_learn", reason, fields, confirm_learning: true }),
  });
  toast("approve learn saved");
  const next = Math.min(state.selectedIndex + 1, Math.max(0, state.queue.length - 1));
  state.selectedIndex = next;
  await loadQueue();
}

function updateActionState() {
  const disabled = Boolean(state.detail && state.detail.read_only);
  ["saveBtn", "approveBtn", "learnBtn", "rejectBtn", "duplicateBtn", "needsReviewBtn", "manualBtn"].forEach((id) => {
    $(id).disabled = disabled;
  });
}

async function openTarget(target) {
  if (!state.detail) return;
  await api(`/api/open/${encodeURIComponent(state.detail.key)}`, {
    method: "POST",
    body: JSON.stringify({ source: 0, target }),
  });
}

function bindActions() {
  $("saveBtn").addEventListener("click", saveEdits);
  $("approveBtn").addEventListener("click", () => action("approve"));
  $("learnBtn").addEventListener("click", () => action("approve_learn"));
  $("rejectBtn").addEventListener("click", () => action("reject"));
  $("duplicateBtn").addEventListener("click", () => action("duplicate"));
  $("needsReviewBtn").addEventListener("click", () => action("needs_review"));
  $("manualBtn").addEventListener("click", () => action("manual_allocation"));
  $("prevBtn").addEventListener("click", () => selectIndex(state.selectedIndex - 1));
  $("nextBtn").addEventListener("click", () => selectIndex(state.selectedIndex + 1));
  $("openFile").addEventListener("click", () => openTarget("file").catch((err) => toast(err.message)));
  $("openFolder").addEventListener("click", () => openTarget("folder").catch((err) => toast(err.message)));
  document.addEventListener("keydown", (event) => {
    if (["INPUT", "TEXTAREA"].includes(document.activeElement.tagName)) return;
    if (event.key === "ArrowRight") selectIndex(state.selectedIndex + 1);
    if (event.key === "ArrowLeft") selectIndex(state.selectedIndex - 1);
    const key = event.key.toLowerCase();
    if (key === "a") action("approve");
    if (key === "l") action("approve_learn");
    if (key === "s") saveEdits();
    if (key === "r") action("reject");
    if (key === "d") action("duplicate");
    if (key === "n") action("needs_review");
    if (key === "m") action("manual_allocation");
  });
}

bindActions();
loadQueue().catch((err) => toast(err.message));
