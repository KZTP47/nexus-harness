"use strict";

const $ = (id) => document.getElementById(id);
const svgNS = "http://www.w3.org/2000/svg";
const agentTypes = new Set(["planner", "coder", "evaluator", "merge"]);
const SYSTEM_PROMPT_CHARACTER_LIMIT = 100000;
const VAULT_BODY_CHARACTER_LIMIT = 200000;
const AGENT_JOB_CHARACTER_LIMIT = 100000;
const BOARD_TASK_CHARACTER_LIMIT = 200000;
const SHARED_PAGE_CHARACTER_LIMIT = 200000;
const PIPELINE_AI_INSTRUCTION_CHARACTER_LIMIT = 200000;
let token = "";
let graph = {schema_version: 2, name: "Untitled", entry: "", nodes: [], edges: []};
let template = null;
let catalog = {providers: [], agents: [], capabilities: []};
let selected = null;
let focusedNodeId = "";
let connectSource = null;
let edgeDrag = null;
let suppressPortClickUntil = 0;
let zoom = 1;
let pan = {x: 0, y: 0};
let drag = null;
let panDrag = null;
let nextId = 1;
let lastEvent = 0;
let startedId = "";
let pollTimer = null;
let undoStack = [];
let dialogPosition = {x: 360, y: 300};
let dialogInvoker = null;
let usageRecords = [];
let promptRecords = [];
let nodeStatuses = new Map();
let lastRunAnnouncementAt = 0;
let lastLiveDataRefreshAt = 0;
let nexusProjectName = "this project";

function systemPromptCharacters(value) {
  // Array.from counts Unicode code points as Python len() does, so the count
  // shown beside the field and the backend decision cannot disagree on emoji.
  return Array.from(String(value || "")).length;
}

function renderSystemPromptCount(fieldId, countId) {
  const field = $(fieldId);
  const count = $(countId);
  const length = systemPromptCharacters(field.value);
  const over = Math.max(0, length - SYSTEM_PROMPT_CHARACTER_LIMIT);
  count.classList.toggle("over-limit", Boolean(over));
  count.textContent = over
    ? `${length.toLocaleString()} / ${SYSTEM_PROMPT_CHARACTER_LIMIT.toLocaleString()} characters — ${over.toLocaleString()} over the limit. Nothing was clipped; shorten the prompt to save it.`
    : `${length.toLocaleString()} / ${SYSTEM_PROMPT_CHARACTER_LIMIT.toLocaleString()} characters. Text is never clipped.`;
  if (over) field.setAttribute("aria-invalid", "true");
  else field.removeAttribute("aria-invalid");
  return over;
}

function systemPromptProblem(fieldId, countId) {
  const over = renderSystemPromptCount(fieldId, countId);
  if (!over) return "";
  const length = systemPromptCharacters($(fieldId).value);
  return `This system prompt is ${length.toLocaleString()} characters; the disclosed limit is ${SYSTEM_PROMPT_CHARACTER_LIMIT.toLocaleString()}. Nexus did not truncate it. Shorten it by ${over.toLocaleString()} characters and try again.`;
}

function renderDisclosedTextCount(fieldId, countId, limit, label) {
  const field = $(fieldId);
  const count = $(countId);
  if (!field || !count) return 0;
  const length = systemPromptCharacters(field.value);
  const over = Math.max(0, length - limit);
  count.classList.toggle("over-limit", Boolean(over));
  count.textContent = over
    ? `${length.toLocaleString()} / ${limit.toLocaleString()} characters — ${over.toLocaleString()} over the limit. Nothing was clipped; shorten ${label} to save it.`
    : `${length.toLocaleString()} / ${limit.toLocaleString()} characters. Text and line breaks are never clipped.`;
  if (over) field.setAttribute("aria-invalid", "true");
  else field.removeAttribute("aria-invalid");
  return over;
}

function disclosedTextProblem(fieldId, countId, limit, label) {
  const over = renderDisclosedTextCount(fieldId, countId, limit, label);
  if (!over) return "";
  const length = systemPromptCharacters($(fieldId).value);
  return `${label[0].toUpperCase()}${label.slice(1)} is ${length.toLocaleString()} characters; the disclosed limit is ${limit.toLocaleString()}. Nexus did not truncate it. Shorten it by ${over.toLocaleString()} characters and try again.`;
}

function renderVaultBodyCount() {
  const field = $("vaultFormBody");
  const count = $("vaultFormBodyCount");
  const length = systemPromptCharacters(field.value);
  const over = Math.max(0, length - VAULT_BODY_CHARACTER_LIMIT);
  count.classList.toggle("over-limit", Boolean(over));
  count.textContent = over
    ? `${length.toLocaleString()} / ${VAULT_BODY_CHARACTER_LIMIT.toLocaleString()} characters — ${over.toLocaleString()} over the limit. Nothing was clipped; shorten the note to save it.`
    : `${length.toLocaleString()} / ${VAULT_BODY_CHARACTER_LIMIT.toLocaleString()} characters. Text is never clipped.`;
  if (over) field.setAttribute("aria-invalid", "true");
  else field.removeAttribute("aria-invalid");
  return over;
}

function vaultBodyProblem() {
  const over = renderVaultBodyCount();
  if (!over) return "";
  const length = systemPromptCharacters($("vaultFormBody").value);
  return `This note is ${length.toLocaleString()} characters; the disclosed limit is ${VAULT_BODY_CHARACTER_LIMIT.toLocaleString()}. Nexus did not truncate it. Shorten it by ${over.toLocaleString()} characters and try again.`;
}

function announce(message, urgent = false) {
  const target = urgent ? $("liveAlert") : $("liveStatus");
  target.textContent = "";
  requestAnimationFrame(() => { target.textContent = message; });
}

async function request(path, options = {}) {
  const headers = {"Content-Type": "application/json", ...(options.headers || {})};
  if (token && path !== "/api/bootstrap") headers["X-Harness-Token"] = token;
  const response = await fetch(path, {...options, headers});
  let value;
  try { value = await response.json(); } catch (_) {
    const error = new Error(`HTTP ${response.status}`);
    error.status = response.status;
    error.responseReceived = true;
    throw error;
  }
  if (!response.ok) {
    const error = new Error(value.error || `HTTP ${response.status}`);
    error.status = response.status;
    error.responseReceived = true;
    throw error;
  }
  return value;
}

// Asking somebody for one line of text.
//
// Not window.prompt. Electron does not have it - it is the one browser thing
// they took out on purpose - so every button that used it did nothing at all
// in the app: no box, no error, no sign anything had happened. In a browser
// they all worked, which is exactly why nothing caught it for so long.
//
// Answers with the text, or null when somebody said no, which is what prompt
// did and what every caller was already written for.
function askForOneLine(title, question, value = "", help = null, browseFolder = false) {
  return new Promise((finish) => {
    const box = $("askDialog");
    $("askDialogTitle").textContent = title;
    $("askDialogWhy").textContent = question || "";
    $("askDialogWhy").hidden = !question;
    const helpLink = $("askDialogHelp");
    helpLink.textContent = help?.label || "";
    helpLink.hidden = !help?.href;
    if (help?.href) helpLink.href = help.href;
    else helpLink.removeAttribute("href");
    const input = $("askDialogInput");
    input.value = value == null ? "" : String(value);
    const browse = $("askDialogBrowse");
    browse.hidden = !(browseFolder && canWeBrowseForAFolder());
    browse.disabled = false;
    browse.onclick = browse.hidden ? null : async () => {
      browse.disabled = true;
      try {
        const chosen = await window.harnessDesktop.pickAFolder();
        if (chosen) {
          input.value = chosen;
          input.focus();
          input.select();
        }
      } catch (error) {
        showError(error.message);
      } finally {
        browse.disabled = false;
      }
    };
    const done = () => {
      box.removeEventListener("close", done);
      browse.onclick = null;
      browse.hidden = true;
      finish(box.returnValue === "ok" ? input.value : null);
    };
    box.addEventListener("close", done);
    box.showModal();
    input.focus();
    input.select();
  });
}

function make(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

function migrateGraph(value) {
  const migrated = structuredClone(value);
  if (migrated.schema_version === 1) {
    migrated.schema_version = 2;
    for (const node of migrated.nodes || []) {
      if (!agentTypes.has(node.type)) continue;
      node.config = {...(node.config || {}), agent_ref: node.config?.agent_ref || "", provider_route: node.config?.provider_route || "", model: node.config?.model || "", role_name: node.config?.role_name || node.label || node.type, system_prompt: node.config?.system_prompt || "", capabilities: node.config?.capabilities || [], data_class: node.config?.data_class || "project_private"};
      if (node.type === "merge") node.config.output_contract = node.config.output_contract || "implementation_plan";
    }
    for (const edge of migrated.edges || []) {
      edge.mode = edge.mode || "state";
      edge.return_fields = edge.return_fields || [];
    }
  }
  return migrated;
}

function nodeById(id) { return graph.nodes.find((node) => node.id === id); }
function position(node) { return node.position || {x: 0, y: 0}; }
function uniqueId(prefix) {
  let id;
  do id = `${prefix}-${nextId++}`; while (nodeById(id) || graph.edges.some((edge) => edge.id === id));
  return id;
}

function noteGraphEdit() { if (typeof markWorkflowChanged === "function") markWorkflowChanged(); }

function pushHistory() {
  noteGraphEdit();
  const snapshot = JSON.stringify(graph);
  if (undoStack[undoStack.length - 1] !== snapshot) undoStack.push(snapshot);
  if (undoStack.length > 50) undoStack.shift();
  $("undoButton").disabled = undoStack.length === 0;
}

function undo() {
  const prior = undoStack.pop();
  if (!prior) return;
  graph = JSON.parse(prior);
  selected = null;
  focusedNodeId = graph.nodes[0]?.id || "";
  $("undoButton").disabled = undoStack.length === 0;
  render();
  announce("Last graph edit restored.");
}

function updateViewport() {
  $("viewport").style.transform = `translate(${pan.x}px, ${pan.y}px) scale(${zoom})`;
  $("zoomValue").value = `${Math.round(zoom * 100)}%`;
}

function focusSnapshot() {
  const active = document.activeElement;
  if (active?.dataset?.nodeFocus) return {kind: "node", id: active.dataset.nodeFocus};
  if (active?.dataset?.outlineNode) return {kind: "outline-node", id: active.dataset.outlineNode};
  if (active?.dataset?.outlineEdge) return {kind: "outline-edge", id: active.dataset.outlineEdge};
  return null;
}

function restoreFocus(snapshot) {
  if (!snapshot) return;
  const selectors = {node: "node-focus", "outline-node": "outline-node", "outline-edge": "outline-edge"};
  document.querySelector(`[data-${selectors[snapshot.kind]}="${CSS.escape(snapshot.id)}"]`)?.focus();
}

function render() {
  const focus = focusSnapshot();
  renderNodes();
  renderEdges();
  renderOutline();
  renderInspector();
  updateViewport();
  restoreFocus(focus);
}

function renderNodes() {
  const layer = $("nodeLayer");
  layer.replaceChildren();
  if (!focusedNodeId || !nodeById(focusedNodeId)) focusedNodeId = graph.nodes[0]?.id || "";
  for (const node of graph.nodes) {
    const box = make("div", `graph-node type-${node.type}`);
    box.dataset.id = node.id;
    box.style.left = `${position(node).x}px`;
    box.style.top = `${position(node).y}px`;
    if (selected?.kind === "node" && selected.id === node.id) box.classList.add("selected");
    const body = make("button", "node-body");
    body.type = "button";
    body.dataset.nodeFocus = node.id;
    body.tabIndex = focusedNodeId === node.id ? 0 : -1;
    body.setAttribute("aria-pressed", String(selected?.kind === "node" && selected.id === node.id));
    const statusText = nodeStatuses.get(node.id) || "Idle";
    body.setAttribute("aria-label", `${node.label || node.id}, ${node.type} node${node.config?.provider_route ? `, provider ${node.config.provider_route}` : ""}, status ${statusText}`);
    body.append(make("strong", "", node.label || node.id), make("span", "node-kind", node.type));
    if (agentTypes.has(node.type)) body.append(make("span", "node-route", `${node.config?.provider_route || "default"} · ${node.config?.model || "configured model"}`));
    body.append(make("span", "node-status", statusText));
    body.addEventListener("focus", () => { focusedNodeId = node.id; });
    body.addEventListener("click", () => selectNode(node.id));
    body.addEventListener("keydown", (event) => nodeKeydown(event, node.id));
    body.addEventListener("pointerdown", (event) => startNodeDrag(event, node.id));
    const inputPort = make("button", "port input-port");
    inputPort.type = "button";
    inputPort.tabIndex = -1;
    inputPort.dataset.inputPort = node.id;
    inputPort.setAttribute("aria-hidden", "true");
    inputPort.title = connectSource ? `Connect to ${node.label || node.id}` : `Input for ${node.label || node.id}`;
    inputPort.addEventListener("click", (event) => { event.stopPropagation(); if (connectSource) connectPort(node.id); });
    const outputPort = make("button", "port output-port");
    outputPort.type = "button";
    outputPort.tabIndex = -1;
    outputPort.dataset.outputPort = node.id;
    outputPort.setAttribute("aria-hidden", "true");
    outputPort.title = `Start connection from ${node.label || node.id}`;
    if (connectSource === node.id) outputPort.classList.add("connecting");
    outputPort.addEventListener("click", (event) => { event.stopPropagation(); if (performance.now() < suppressPortClickUntil) return; connectPort(node.id); });
    outputPort.addEventListener("pointerdown", (event) => startEdgeDrag(event, node.id));
    box.append(body, inputPort, outputPort);
    layer.append(box);
  }
}

function edgeLabel(edge) {
  const condition = edge.condition ? ` when ${edge.condition}` : "";
  const mode = edge.mode && edge.mode !== "state" ? `, ${edge.mode.replace("_", " ")}` : "";
  const slot = edge.target_slot ? ` to slot ${edge.target_slot}` : "";
  const loop = edge.loop ? `, loop limit ${edge.loop.max_iterations}` : "";
  return `${edge.source} to ${edge.target}${mode}${slot}${condition}${loop}`;
}

function renderEdges() {
  const layer = $("edgeLayer");
  layer.replaceChildren();
  for (const edge of graph.edges) {
    const source = nodeById(edge.source);
    const target = nodeById(edge.target);
    if (!source || !target) continue;
    const start = {x: position(source).x + 190, y: position(source).y + 48};
    const end = {x: position(target).x, y: position(target).y + 48};
    const bend = Math.max(70, Math.abs(end.x - start.x) * .45);
    const d = `M ${start.x} ${start.y} C ${start.x + bend} ${start.y}, ${end.x - bend} ${end.y}, ${end.x} ${end.y}`;
    const visible = document.createElementNS(svgNS, "path");
    visible.setAttribute("d", d);
    visible.setAttribute("class", `edge-path${edge.loop ? " edge-loop" : ""}`);
    const hit = document.createElementNS(svgNS, "path");
    hit.setAttribute("d", d);
    hit.setAttribute("class", "edge-hit");
    hit.addEventListener("click", () => selectEdge(edge.id));
    layer.append(visible, hit);
  }
  if (edgeDrag) {
    const source = nodeById(edgeDrag.source);
    if (source) {
      const start = {x: position(source).x + 190, y: position(source).y + 48};
      const target = edgeDrag.target && nodeById(edgeDrag.target);
      const end = target
        ? {x: position(target).x, y: position(target).y + 48}
        : edgeDrag.point;
      const bend = Math.max(70, Math.abs(end.x - start.x) * .45);
      const preview = document.createElementNS(svgNS, "path");
      preview.setAttribute("d", `M ${start.x} ${start.y} C ${start.x + bend} ${start.y}, ${end.x - bend} ${end.y}, ${end.x} ${end.y}`);
      preview.setAttribute("class", `edge-preview ${edgeDrag.valid === false ? "invalid" : edgeDrag.valid ? "valid" : ""}`);
      layer.append(preview);
    }
  }
}

function renderOutline() {
  const nodes = $("nodeList");
  const edges = $("edgeList");
  nodes.replaceChildren();
  edges.replaceChildren();
  nodes.append(make("li", "hint", "Nodes"));
  for (const node of graph.nodes) {
    const item = document.createElement("li");
    const button = make("button", "", `${node.label || node.id}: ${node.type}`);
    button.type = "button";
    button.dataset.outlineNode = node.id;
    button.addEventListener("click", () => selectNode(node.id, true));
    item.append(button); nodes.append(item);
  }
  edges.append(make("li", "hint", "Connections"));
  if (!graph.edges.length) edges.append(make("li", "hint", "No connections."));
  for (const edge of graph.edges) {
    const item = document.createElement("li");
    const button = make("button", "", edgeLabel(edge));
    button.type = "button";
    button.dataset.outlineEdge = edge.id;
    button.addEventListener("click", () => selectEdge(edge.id, true));
    item.append(button); edges.append(item);
  }
}

function selectNode(id, preserveOutlineFocus = false) {
  selected = {kind: "node", id}; focusedNodeId = id;
  const focus = preserveOutlineFocus ? focusSnapshot() : null;
  render(); restoreFocus(focus);
}
function selectEdge(id, preserveOutlineFocus = false) { const focus = preserveOutlineFocus ? focusSnapshot() : null; selected = {kind: "edge", id}; render(); restoreFocus(focus); }
function clearSelection() { selected = null; render(); }

function nearestNode(id, key) {
  const current = nodeById(id);
  if (!current) return null;
  const origin = position(current);
  const order = graph.nodes.map((node) => node.id);
  const place = order.indexOf(id);
  const candidates = graph.nodes.filter((node) => node.id !== id).map((node) => {
    const point = position(node); const dx = point.x - origin.x; const dy = point.y - origin.y;
    // Two agents can sit in exactly the same spot, for example when several
    // are added from the palette without being dragged. Without this they
    // could never be reached from one another with the arrow keys, so the
    // order they were added in stands in for a direction.
    const together = dx === 0 && dy === 0;
    const later = order.indexOf(node.id) > place;
    const forward = key === "ArrowRight" || key === "ArrowDown";
    const valid = together
      ? later === forward
      : key === "ArrowLeft" ? dx < 0 : key === "ArrowRight" ? dx > 0 : key === "ArrowUp" ? dy < 0 : dy > 0;
    return {node, valid, score: Math.hypot(dx, dy) + (key === "ArrowLeft" || key === "ArrowRight" ? Math.abs(dy) : Math.abs(dx))};
  }).filter((item) => item.valid).sort((a, b) => a.score - b.score || a.node.id.localeCompare(b.node.id));
  return candidates[0]?.node || null;
}

function nodeKeydown(event, id) {
  if ((event.key === "c" || event.key === "C") && !event.ctrlKey && !event.metaKey) {
    event.preventDefault(); connectSource = id; announce(`Connection started from ${nodeById(id).label || id}. Choose a node and press Enter.`); render(); focusNode(id); return;
  }
  if (event.key === "Enter" && connectSource && connectSource !== id) { event.preventDefault(); connectPort(id); focusNode(id); return; }
  const arrows = new Set(["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"]);
  if (arrows.has(event.key)) {
    event.preventDefault();
    if (event.ctrlKey || event.metaKey) {
      pushHistory();
      const delta = event.shiftKey ? 32 : 8;
      const moves = {ArrowLeft: [-delta, 0], ArrowRight: [delta, 0], ArrowUp: [0, -delta], ArrowDown: [0, delta]};
      const node = nodeById(id); node.position = node.position || {x: 0, y: 0};
      node.position.x = Math.max(0, node.position.x + moves[event.key][0]); node.position.y = Math.max(0, node.position.y + moves[event.key][1]);
      render(); focusNode(id); announce(`${node.label || id} moved to ${Math.round(node.position.x)}, ${Math.round(node.position.y)}.`);
    } else {
      const target = nearestNode(id, event.key);
      if (target) { focusedNodeId = target.id; renderNodes(); focusNode(target.id); }
    }
  } else if (event.key === "Delete") { event.preventDefault(); removeNode(id); }
  else if (event.key === "Escape") { connectSource = null; clearSelection(); focusNode(id); }
}

function focusNode(id) { document.querySelector(`[data-node-focus="${CSS.escape(id)}"]`)?.focus(); }
function startNodeDrag(event, id) {
  if (event.button !== 0 || event.target.closest(".port")) return;
  const node = nodeById(id);
  drag = {id, startX: event.clientX, startY: event.clientY, x: position(node).x, y: position(node).y, history: false};
  event.currentTarget.setPointerCapture(event.pointerId); event.preventDefault();
}

function connectionCompatibility(source, target) {
  if (!nodeById(source) || !nodeById(target)) return {valid: false, reason: "Connection endpoint is missing."};
  if (source === target) return {valid: false, reason: "A node cannot connect to itself."};
  if (nodeById(source).type === "end") return {valid: false, reason: "An end node cannot have outgoing connections."};
  if (nodeById(target).type === "start") return {valid: false, reason: "A start node cannot have incoming connections."};
  if (graph.edges.some((edge) => edge.source === source && edge.target === target)) return {valid: false, reason: "That connection already exists."};
  if (nodeById(target).type === "merge") {
    const required = nodeById(target).config?.required_slots || [];
    const used = new Set(graph.edges.filter((edge) => edge.target === target && edge.mode === "merge_input").map((edge) => edge.target_slot));
    if (!required.some((slot) => !used.has(slot))) return {valid: false, reason: "The merge node has no unconnected input slot."};
  }
  return {valid: true, reason: "Release to create the connection."};
}

function graphPoint(clientX, clientY) {
  const rect = $("canvas").getBoundingClientRect();
  return {x: (clientX - rect.left - pan.x) / zoom, y: (clientY - rect.top - pan.y) / zoom};
}

function clearPortFeedback() {
  document.querySelectorAll(".graph-node.valid-drop, .graph-node.invalid-drop").forEach((item) => item.classList.remove("valid-drop", "invalid-drop"));
  $("canvas").classList.remove("connecting-edge", "invalid-drop");
}

function edgeDropTarget(clientX, clientY) {
  return document.elementFromPoint(clientX, clientY)?.closest?.("[data-input-port]")?.dataset.inputPort || "";
}

function startEdgeDrag(event, source) {
  if (event.button !== 0) return;
  event.stopPropagation(); event.preventDefault();
  event.currentTarget.setPointerCapture(event.pointerId);
  edgeDrag = {source, pointerId: event.pointerId, startClientX: event.clientX, startClientY: event.clientY, point: graphPoint(event.clientX, event.clientY), target: "", valid: null, moved: false};
  $("canvas").classList.add("connecting-edge");
  announce(`Dragging a connection from ${nodeById(source).label || source}. Release on an input port.`);
  renderEdges();
}

function updateEdgeDrag(event) {
  if (!edgeDrag || edgeDrag.pointerId !== event.pointerId) return;
  edgeDrag.point = graphPoint(event.clientX, event.clientY);
  edgeDrag.moved ||= Math.hypot(event.clientX - edgeDrag.startClientX, event.clientY - edgeDrag.startClientY) > 3;
  const target = edgeDropTarget(event.clientX, event.clientY);
  const check = target ? connectionCompatibility(edgeDrag.source, target) : {valid: null, reason: "Release on an input port."};
  edgeDrag.target = target; edgeDrag.valid = check.valid; edgeDrag.reason = check.reason;
  clearPortFeedback(); $("canvas").classList.add("connecting-edge");
  if (target) document.querySelector(`[data-id="${CSS.escape(target)}"]`)?.classList.add(check.valid ? "valid-drop" : "invalid-drop");
  if (check.valid === false) $("canvas").classList.add("invalid-drop");
  renderEdges();
}

function finishEdgeDrag(event, cancelled = false) {
  if (!edgeDrag || (event.pointerId !== undefined && edgeDrag.pointerId !== event.pointerId)) return;
  const current = edgeDrag;
  if (!cancelled && event.clientX !== undefined) updateEdgeDrag(event);
  if (!cancelled && !current.moved) {
    edgeDrag = null; clearPortFeedback(); renderEdges();
    return;
  }
  const target = edgeDrag?.target || "";
  const check = target ? connectionCompatibility(current.source, target) : {valid: false, reason: "Connection cancelled. Release on an input port."};
  edgeDrag = null; clearPortFeedback(); renderEdges();
  if (cancelled) { announce("Connection drag cancelled."); return; }
  if (current.moved) suppressPortClickUntil = performance.now() + 500;
  if (check.valid) { addEdge(current.source, target); render(); return; }
  announce(check.reason, true);
}

function pathExists(start, target, seen = new Set()) {
  if (start === target) return true;
  if (seen.has(start)) return false;
  seen.add(start);
  return graph.edges.filter((edge) => edge.source === start).some((edge) => pathExists(edge.target, target, seen));
}

function connectPort(id) {
  if (!connectSource) { connectSource = id; announce(`Connection started from ${nodeById(id).label || id}.`); render(); return; }
  if (connectSource === id) { connectSource = null; announce("Connection cancelled."); render(); return; }
  addEdge(connectSource, id); connectSource = null; render();
}

function addEdge(source, target) {
  const compatibility = connectionCompatibility(source, target);
  if (!compatibility.valid) { announce(compatibility.reason, true); return; }
  pushHistory();
  const cyclical = pathExists(target, source);
  const targetNode = nodeById(target);
  const mode = targetNode?.type === "merge" ? "merge_input" : "state";
  const used = new Set(graph.edges.filter((edge) => edge.target === target).map((edge) => edge.target_slot));
  const slot = targetNode?.config?.required_slots?.find((item) => !used.has(item)) || "";
  const edge = {id: uniqueId("edge"), source, target, mode, condition: "", variables: [], return_fields: [], ...(mode === "merge_input" ? {target_slot: slot} : {}), ...(cyclical ? {loop: {max_iterations: 4, temperature_decay: .75, timeout_seconds: 600}} : {})};
  graph.edges.push(edge); selected = {kind: "edge", id: edge.id};
  announce(`Connected ${source} to ${target}${cyclical ? " with a four-iteration limit" : ""}.`);
}

function providerById(id) { return catalog.providers.find((item) => item.route_id === id); }
function agentById(id) { return catalog.agents.find((item) => item.agent_id === id); }
function fillAgentSelect(select, chosen = "") {
  select.replaceChildren(); const none = make("option", "", "None"); none.value = ""; select.append(none);
  for (const agent of catalog.agents) { const option = make("option", "", `${agent.agent_id} / ${agent.role}${agent.graph_routing_allowed ? "" : " (blocked)"}`); option.value = agent.agent_id; option.disabled = !agent.graph_routing_allowed; select.append(option); }
  select.value = chosen;
}
function fillProviderSelect(select, chosen = "") {
  select.replaceChildren();
  if (!catalog.providers.length) {
    // With no model route set up, the form could never be filled in. Say what
    // to do instead of leaving an empty box a person cannot get past.
    const option = make("option", "", "No model is set up yet. Open Start here first.");
    option.value = "";
    option.disabled = true;
    select.append(option);
    select.value = "";
    return;
  }
  if (catalog.providers.every((provider) => provider.graph_routing_allowed === false)) {
    // Every route this project has refuses to run a workflow built here. The
    // form is unusable until that is changed, so say exactly what to change.
    const option = make(
      "option", "",
      "No model route may run a workflow yet. Set allow_project_graphs to true for one route."
    );
    option.value = "";
    option.disabled = true;
    select.append(option);
  }
  for (const provider of catalog.providers) {
    const suffix = provider.credential?.environment_variable && !provider.credential.present ? " (key missing)" : "";
    const blocked = provider.graph_routing_allowed === false;
    const reason = blocked && provider.routing_block_reason ? ` (${provider.routing_block_reason})` : "";
    const option = make("option", "", `${provider.label} / ${provider.max_data_class || "project_private"}${reason}${suffix}`); option.value = provider.route_id; option.disabled = blocked; select.append(option);
  }
  if (chosen && ![...select.options].some((item) => item.value === chosen)) { const option = make("option", "", chosen); option.value = chosen; select.append(option); }
  select.value = chosen || catalog.providers[0]?.route_id || "";
}
function renderCapabilityChecks(container, chosen = [], allowedIds = null) {
  container.replaceChildren();
  for (const capability of catalog.capabilities) {
    const assigned = allowedIds === null || allowedIds.includes(capability.id); const allowed = capability.allowed && assigned;
    const label = document.createElement("label"); const input = document.createElement("input"); input.type = "checkbox"; input.value = capability.id; input.checked = allowed && chosen.includes(capability.id); input.disabled = !allowed;
    label.append(input, document.createTextNode(`${capability.label}${allowed ? "" : " (not assigned)"}`)); container.append(label);
  }
}
function selectedCapabilities(container) { return [...container.querySelectorAll("input:checked")].map((item) => item.value); }
function csv(value) { return value.split(",").map((item) => item.trim()).filter(Boolean); }

function openAgentDialog(type = "planner", x = 360, y = 300, invoker = document.activeElement) {
  dialogPosition = {x, y}; dialogInvoker = invoker;
  $("agentDialogTitle").textContent = type === "merge" ? "Add merge agent" : "Add agent";
  $("agentType").value = type; $("agentLabel").value = `${type[0].toUpperCase()}${type.slice(1)} ${nextId}`; $("agentRoleName").value = type === "merge" ? "Findings synthesizer" : `${type[0].toUpperCase()}${type.slice(1)} agent`; $("agentPrompt").value = "";
  renderSystemPromptCount("agentPrompt", "agentPromptCount");
  fillAgentSelect($("agentRef")); fillProviderSelect($("agentProvider")); updateModelSuggestions($("agentProvider").value, $("agentModel")); renderCapabilityChecks($("agentCapabilities"), type === "coder" ? ["workspace.read", "workspace.write"] : ["workspace.read"]);
  $("agentMergeFields").hidden = type !== "merge"; $("agentFormError").hidden = true; $("agentFormError").replaceChildren();
  for (const field of $("agentForm").querySelectorAll("[aria-invalid]")) { field.removeAttribute("aria-invalid"); field.removeAttribute("aria-describedby"); }
  $("agentPrompt").setAttribute("aria-describedby", "agentPromptCount");
  $("agentDialog").showModal(); $("agentDialogTitle").focus();
}
function closeAgentDialog() { $("agentDialog").close(); dialogInvoker?.focus?.(); }
function updateModelSuggestions(routeId, input) {
  const provider = providerById(routeId); const models = provider?.models || [];
  $("modelSuggestions").replaceChildren(...models.map((model) => { const option = document.createElement("option"); option.value = model; return option; }));
  input.value = provider?.default_model || models[0] || input.value || "";
}
function applyAgentAssignment(select, providerSelect, modelInput, roleInput, capabilityBox) {
  const agent = agentById(select.value); if (!agent) { providerSelect.disabled = false; modelInput.disabled = false; renderCapabilityChecks(capabilityBox, selectedCapabilities(capabilityBox)); return; }
  fillProviderSelect(providerSelect, agent.provider_route); providerSelect.disabled = true; modelInput.value = agent.model; modelInput.disabled = true; roleInput.value = agent.role; renderCapabilityChecks(capabilityBox, agent.capabilities, agent.capabilities);
}
function submitAgent(event) {
  event.preventDefault();
  const type = $("agentType").value; const label = $("agentLabel").value.trim(); const provider = $("agentProvider").value; const model = $("agentModel").value.trim(); const role = $("agentRoleName").value.trim();
  const promptProblem = systemPromptProblem("agentPrompt", "agentPromptCount");
  const invalid = !label ? ["agentLabel", "Enter a label."] : !provider ? ["agentProvider", "Choose a provider route."] : providerById(provider)?.graph_routing_allowed === false ? ["agentProvider", "This route does not allow submitted workflow graphs."] : !model ? ["agentModel", "Enter a model."] : !role ? ["agentRoleName", "Enter a role."] : promptProblem ? ["agentPrompt", promptProblem] : null;
  if (invalid) {
    const [fieldId, message] = invalid; const field = $(fieldId); const link = make("a", "", message); link.href = `#${fieldId}`; link.addEventListener("click", (click) => { click.preventDefault(); field.focus(); });
    field.setAttribute("aria-invalid", "true"); const described = new Set(String(field.getAttribute("aria-describedby") || "").split(/\s+/).filter(Boolean)); described.add("agentFormError"); field.setAttribute("aria-describedby", [...described].join(" ")); $("agentFormError").replaceChildren(link); $("agentFormError").hidden = false; $("agentFormError").focus(); return;
  }
  pushHistory();
  const agentRef = $("agentRef").value; const id = uniqueId(type); const config = {agent_ref: agentRef, provider_route: agentRef ? "" : provider, model: agentRef ? "" : model, role_name: role, system_prompt: $("agentPrompt").value, capabilities: selectedCapabilities($("agentCapabilities")), data_class: "project_private"};
  if (type === "merge") { config.required_slots = csv($("agentMergeSlots").value); config.output_field = $("agentMergeOutput").value.trim() || "merged_output"; config.output_contract = "implementation_plan"; }
  graph.nodes.push({id, type, label, position: {x: Math.max(0, dialogPosition.x), y: Math.max(0, dialogPosition.y)}, config});
  if (!graph.entry) graph.entry = id; selected = {kind: "node", id}; focusedNodeId = id; closeAgentDialog(); render(); focusNode(id); announce(`${label} added.`);
}

function freeSpot(x, y) {
  // Step a new agent down and across until it is not exactly on top of one
  // that is already there, so nothing is hidden behind anything else.
  let spot = {x: Math.max(0, x), y: Math.max(0, y)};
  for (let tries = 0; tries < 40; tries += 1) {
    if (!graph.nodes.some((node) => position(node).x === spot.x && position(node).y === spot.y)) break;
    spot = {x: spot.x + 40, y: spot.y + 30};
  }
  return spot;
}

function addNode(type, x = 360, y = 300, invoker = document.activeElement) {
  if (agentTypes.has(type)) { openAgentDialog(type, x, y, invoker); return; }
  if (type === "gauntlet") { addGauntlet(x, y); return; }
  pushHistory(); const id = uniqueId(type); graph.nodes.push({id, type, label: `Tool ${nextId - 1}`, position: {x: Math.max(0, x), y: Math.max(0, y)}, config: {role: "generic"}}); if (!graph.entry) graph.entry = id; selected = {kind: "node", id}; focusedNodeId = id; render(); announce("Tool added.");
}
function addGauntlet(x, y) {
  if (!template) return; pushHistory(); const prefix = uniqueId("gauntlet"); const minX = Math.min(...template.nodes.map((node) => position(node).x)); const minY = Math.min(...template.nodes.map((node) => position(node).y)); const idMap = new Map();
  for (const source of template.nodes) { const node = structuredClone(source); const id = `${prefix}-${node.id}`; idMap.set(node.id, id); node.id = id; node.position = {x: x + position(source).x - minX, y: y + position(source).y - minY}; graph.nodes.push(node); }
  for (const source of template.edges) { const edge = structuredClone(source); edge.id = `${prefix}-${edge.id}`; edge.source = idMap.get(edge.source); edge.target = idMap.get(edge.target); graph.edges.push(edge); }
  if (!graph.entry) graph.entry = idMap.get(template.entry); const id = idMap.get("coder") || idMap.get(template.entry); selected = {kind: "node", id}; focusedNodeId = id; render(); announce("Gauntlet macro added.");
}
function removeNode(id) { const node = nodeById(id); if (!node) return; pushHistory(); graph.nodes = graph.nodes.filter((item) => item.id !== id); graph.edges = graph.edges.filter((edge) => edge.source !== id && edge.target !== id); if (graph.entry === id) graph.entry = graph.nodes[0]?.id || ""; selected = null; focusedNodeId = graph.nodes[0]?.id || ""; render(); announce(`${node.label || id} deleted. Press Control+Z to restore it.`); }
function removeEdge(id) { pushHistory(); graph.edges = graph.edges.filter((edge) => edge.id !== id); selected = null; render(); announce("Connection deleted. Press Control+Z to restore it."); }

function renderInspector() {
  const nodeForm = $("nodeInspector"); const edgeForm = $("edgeInspector"); $("emptyInspector").hidden = Boolean(selected); nodeForm.hidden = selected?.kind !== "node"; edgeForm.hidden = selected?.kind !== "edge";
  if (selected?.kind === "node") {
    const node = nodeById(selected.id); if (!node) return; const isAgent = agentTypes.has(node.type);
    $("nodeLabel").value = node.label || ""; $("agentInspectorFields").hidden = !isAgent; $("nodeRole").hidden = node.type !== "tool"; $("nodeRole").previousElementSibling.hidden = node.type !== "tool"; $("nodeRole").value = node.config?.role || "generic";
    if (isAgent) { const agentRef = node.config?.agent_ref || ""; const assignment = agentById(agentRef); fillAgentSelect($("nodeAgentRef"), agentRef); fillProviderSelect($("nodeProvider"), assignment?.provider_route || node.config?.provider_route || ""); $("nodeProvider").disabled = Boolean(assignment); $("nodeModel").value = assignment?.model || node.config?.model || ""; $("nodeModel").disabled = Boolean(assignment); $("nodeRoleName").value = node.config?.role_name || assignment?.role || ""; $("nodePrompt").value = node.config?.system_prompt || ""; renderSystemPromptCount("nodePrompt", "nodePromptCount"); renderCapabilityChecks($("nodeCapabilities"), node.config?.capabilities || [], assignment?.capabilities || null); $("mergeFields").hidden = node.type !== "merge"; $("mergeSlots").value = (node.config?.required_slots || []).join(", "); $("mergeOutput").value = node.config?.output_field || "merged_output"; }
  }
  if (selected?.kind === "edge") { const edge = graph.edges.find((item) => item.id === selected.id); if (!edge) return; $("edgeMode").value = edge.mode || "state"; $("edgeCondition").value = edge.condition || ""; $("edgeVariables").value = (edge.variables || []).join(", "); $("edgeTargetSlot").value = edge.target_slot || ""; $("edgeReturnFields").value = (edge.return_fields || []).join(", "); $("maxIterations").value = edge.loop?.max_iterations || ""; $("temperatureDecay").value = edge.loop?.temperature_decay || ""; $("loopTimeout").value = edge.loop?.timeout_seconds || ""; }
}
function updateSelectedNode() {
  if (selected?.kind !== "node") return; const node = nodeById(selected.id);
  if (agentTypes.has(node.type)) {
    const promptProblem = systemPromptProblem("nodePrompt", "nodePromptCount");
    if (promptProblem) { showError(promptProblem); $("nodePrompt").focus(); return; }
  }
  pushHistory(); node.label = $("nodeLabel").value.trim() || node.id; node.config = {...(node.config || {})};
  if (agentTypes.has(node.type)) { node.config.agent_ref = $("nodeAgentRef").value; node.config.provider_route = node.config.agent_ref ? "" : $("nodeProvider").value; node.config.model = node.config.agent_ref ? "" : $("nodeModel").value.trim(); node.config.role_name = $("nodeRoleName").value.trim(); node.config.system_prompt = $("nodePrompt").value; node.config.capabilities = selectedCapabilities($("nodeCapabilities")); node.config.data_class = node.config.data_class || "project_private"; if (node.type === "merge") { node.config.required_slots = csv($("mergeSlots").value); node.config.output_field = $("mergeOutput").value.trim() || "merged_output"; node.config.output_contract = "implementation_plan"; } }
  else if (node.type === "tool") node.config.role = $("nodeRole").value;
  renderNodes(); renderOutline();
}
function updateSelectedEdge() {
  if (selected?.kind !== "edge") return; const edge = graph.edges.find((item) => item.id === selected.id); pushHistory(); edge.mode = $("edgeMode").value; edge.condition = $("edgeCondition").value.trim(); edge.variables = csv($("edgeVariables").value); edge.return_fields = csv($("edgeReturnFields").value); if (edge.mode === "merge_input") edge.target_slot = $("edgeTargetSlot").value.trim(); else delete edge.target_slot;
  const max = Number.parseInt($("maxIterations").value, 10); if (Number.isFinite(max) && max > 0) edge.loop = {max_iterations: max, temperature_decay: Number.parseFloat($("temperatureDecay").value) || 1, timeout_seconds: Number.parseInt($("loopTimeout").value, 10) || 600}; else if (!pathExists(edge.target, edge.source)) delete edge.loop; render();
}

function showIssues(result) {
  const status = $("validationStatus"); const list = $("issueList"); list.replaceChildren(); status.className = result.valid ? "status-pass" : "status-fail"; status.textContent = result.valid ? "This workflow can run." : `This workflow has ${result.issues.length} problem${result.issues.length === 1 ? "" : "s"} to fix.`; for (const issue of result.issues) list.append(make("li", "", `${issue.path}: ${issue.message}`)); announce(status.textContent, !result.valid);
}
async function validate(candidate = graph) { try { const result = await request("/api/validate", {method: "POST", body: JSON.stringify({graph: candidate})}); showIssues(result); return result; } catch (error) { showError(error.message); return {valid: false, issues: []}; } }
async function simulate() { const checked = await validate(); if (!checked.valid) return; try { const result = await request("/api/simulate", {method: "POST", body: JSON.stringify({graph, state: {test_failures_remaining: 1, temperature: .2}})}); await animateTransitions(result.transitions); appendEvent("simulation", result.complete ? "Complete" : result.error || "Stopped"); announce(result.complete ? "Simulation completed." : `Simulation stopped: ${result.error || result.stopped_at}`); } catch (error) { showError(error.message); } }
async function animateTransitions(transitions) { const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches; for (const transition of transitions.filter((item) => item.node)) { nodeStatuses.set(transition.node, transition.state?.stage_passed === false ? "Failed" : "Passed"); renderNodes(); appendEvent(transition.node, transition.state?.stage_passed === false ? "Failed; routed to coder" : "Passed"); if (!reduced) await new Promise((resolve) => setTimeout(resolve, 220)); } }
function appendEvent(state, result) { const row = document.createElement("tr"); row.append(make("td", "", new Date().toLocaleTimeString()), make("td", "", state), make("td", "", typeof result === "string" ? result : JSON.stringify(result))); $("eventBody").prepend(row); while ($("eventBody").children.length > 500) $("eventBody").lastElementChild.remove(); }
function showError(message) { $("validationStatus").className = "status-fail"; $("validationStatus").textContent = message; announce(`Error: ${message}`, true); appendEvent("error", message); }
async function startRun() { if (pipelineCannotRun) { showError(executionPauseWords("Project execution", pipelineCannotRun)); return; } const task = $("taskInput").value.trim(); if (!task) { showError("Enter a task before starting a run."); $("taskInput").focus(); return; } const checked = await validate(); if (!checked.valid) return; try { await request("/api/run", {method: "POST", body: JSON.stringify({task, dry_run: $("dryRunInput").checked, graph})}); announce("Run accepted. Events will appear in the run log."); appendEvent("run", "Accepted"); } catch (error) { showError(error.message); } }

async function pollEvents() {
  clearTimeout(pollTimer);
  try {
    const result = await request(`/api/events?after=${lastEvent}&meta=1`); $("connectionStatus").textContent = "Connected"; $("connectionStatus").className = "status-pass";
    // The harness gives every start its own mark. A different mark means it
    // was restarted under this page, and its numbering began again, so the
    // page must listen from the beginning or it would wait for ever.
    if (result.started_id && startedId && result.started_id !== startedId) {
      lastEvent = 0;
      startedId = result.started_id;
      appendEvent("connection", "The harness was restarted. Listening again from the beginning.");
      announce("The harness was restarted. The panel is listening again.", true);
      pollTimer = setTimeout(pollEvents, 200);
      return;
    }
    if (result.started_id) startedId = result.started_id;
    if (result.gap) { appendEvent("connection", `${result.gap} older event(s) were dropped.`); announce(`${result.gap} older run events were dropped.`, true); }
    for (const event of result.events) {
      // One odd event must never stop the rest of the batch being read.
      try {
        lastEvent = Math.max(lastEvent, Number(event?.sequence) || lastEvent);
        const kind = String(event?.kind || "");
        const detail = event?.payload?.error || event?.payload?.summary || kind || "an update";
        appendEvent(event?.node || kind || "update", detail);
        nodeStatuses.set(String(event?.node || ""), kind === "failure" || kind === "run_error" ? "Failed" : kind === "node_start" ? "Running" : "Updated");
        // Every kind of news the checks view knows how to draw. A kind missing
        // from this list is not an error anywhere: the button works, the answer
        // arrives, and the page quietly never shows it.
        if (["qa_", "pick_", "record_", "coverage_"].some((start) => kind.startsWith(start))) applyCheckEvent(event);
        if (kind === "agent_message") applyTeamEvent(event);
        if (kind === "the_list" || kind === "a_word_of_warning") applyDoingEvent(event);
        if (kind.startsWith("pipeline_")) applyPipelineEvent(event);
      } catch (error) {
        appendEvent("update", `One update could not be read: ${error.message}`);
      }
    }
    if (result.events.length) {
      const now = Date.now();
      renderNodes(); await refreshUsage();
      // The picture on the first screen lights up from the same news, so
      // somebody watching it sees the run move without opening Workflow.
      // Real news beats a walk through that has already finished.
      if (howStages.length && !howWalk) { howWalkStates = new Map(); renderHowItWorks(); }
      if (now - lastLiveDataRefreshAt >= 1000) {
        lastLiveDataRefreshAt = now;
        if (!$("memoryView").hidden) await refreshMemory();
        if (!$("promptsView").hidden) await refreshPrompts();
      }
      if (now - lastRunAnnouncementAt >= 2000) { const latest = result.events.at(-1); announce(`${result.events.length} run update${result.events.length === 1 ? "" : "s"}. Latest: ${latest.node || latest.kind}, ${latest.kind}.`); lastRunAnnouncementAt = now; }
    }
  } catch (_) { $("connectionStatus").textContent = "Disconnected; retrying"; $("connectionStatus").className = "status-fail"; }
  pollTimer = setTimeout(pollEvents, document.hidden ? 3000 : 700);
}

function formatCost(record) { const hasMicro = record.cost_microusd != null; const hasNanos = record.cost_nanos != null; const label = record.price_status || record.cost_basis || "unpriced"; const snapshot = record.price_snapshot_id || record.rate_id; if (!hasMicro && !hasNanos) return `Unavailable (${label}${snapshot ? ` / ${snapshot}` : ""})`; const dollars = hasMicro ? Number(record.cost_microusd) / 1e6 : Number(record.cost_nanos) / 1e9; return `${label === "estimated" ? "Est. " : ""}$${dollars.toFixed(dollars < .01 ? 6 : 4)} (${label}${snapshot ? ` / ${snapshot}` : ""})`; }
async function collectRecordPages(path, limit) { const records = []; let after = 0; for (let page = 0; page < 100; page += 1) { const separator = path.includes("?") ? "&" : "?"; const result = await request(`${path}${separator}limit=${limit}&after=${after}`); records.push(...result.records); if (!result.has_more || result.next_cursor === after) break; after = result.next_cursor; } return records; }
async function refreshUsage() { try { usageRecords = await collectRecordPages("/api/usage", 500); renderUsage(); } catch (_) { /* live events continue */ } }
function renderUsage() { const totals = usageRecords.reduce((sum, item) => sum + Number(item.input_tokens || 0) + Number(item.output_tokens || 0), 0); const cache = usageRecords.reduce((sum, item) => sum + Number(item.cached_input_tokens || 0), 0); $("usageSummary").replaceChildren(make("span", "metric", `${usageRecords.length} requests`), make("span", "metric", `${totals.toLocaleString()} tokens`), make("span", "metric", `${cache.toLocaleString()} cached input tokens`)); const body = $("usageBody"); body.replaceChildren(); for (const item of usageRecords.slice().reverse()) { const row = document.createElement("tr"); const classes = [`${Number(item.input_tokens || 0).toLocaleString()} in`, `${Number(item.output_tokens || 0).toLocaleString()} out`, `${Number(item.reasoning_tokens || 0).toLocaleString()} reasoning`, `${Number(item.tool_use_tokens || 0).toLocaleString()} tool`, `${Number(item.billed_output_tokens || 0).toLocaleString()} billed out`]; row.append(make("td", "", item.agent_role || item.node_id), make("td", "", item.provider_route || item.provider), make("td", "", item.model), make("td", "", classes.join(" / ")), make("td", "", item.latency_ms == null ? "Unavailable" : `${item.latency_ms} ms`), make("td", "", formatCost(item))); body.append(row); } }

async function refreshMemory() { try { const query = encodeURIComponent($("memoryQuery").value.trim()); const kind = encodeURIComponent($("memoryKind").value); const nodes = []; const links = []; let after = 0; for (let page = 0; page < 100; page += 1) { const result = await request(`/api/memory?limit=200&after=${after}&query=${query}&kind=${kind}`); nodes.push(...result.nodes); links.push(...result.links); if (!result.has_more || result.next_cursor === after) break; after = result.next_cursor; } renderMemory({nodes, links}); } catch (error) { showError(error.message); } }
function renderMemory(result) {
  const graphBox = $("memoryGraph"); const table = $("memoryBody"); graphBox.replaceChildren(); table.replaceChildren(); const agents = [...new Set(result.links.map((item) => item.node_id).filter(Boolean))]; const agentPos = new Map(); const recordPos = new Map(); let height = Math.max(300, Math.max(agents.length, result.nodes.length) * 82 + 20); graphBox.style.height = `${Math.min(1200, height)}px`;
  agents.forEach((id, index) => { const y = 18 + index * 82; agentPos.set(id, {x: 18, y}); const card = make("div", "memory-card", ""); card.style.left = "18px"; card.style.top = `${y}px`; card.append(make("strong", "", id), make("span", "", "Agent")); graphBox.append(card); });
  result.nodes.forEach((node, index) => { const x = agents.length ? 310 : 18; const y = 18 + index * 82; recordPos.set(`${node.kind}:${node.id}`, {x, y}); const card = make("div", "memory-card", ""); card.style.left = `${x}px`; card.style.top = `${y}px`; card.append(make("strong", "", node.label), make("span", "", node.kind)); graphBox.append(card); const provenance = result.links.filter((item) => item.memory_kind === node.kind && item.memory_id === node.id).map((item) => `${item.relation.replace("_", " ")} ${item.node_id}`).join("; ") || "No agent provenance recorded"; const row = document.createElement("tr"); row.append(make("td", "", node.kind), make("td", "", node.label), make("td", "", node.summary || ""), make("td", "", provenance)); table.append(row); });
  for (const link of result.links) { const a = agentPos.get(link.node_id); const b = recordPos.get(`${link.memory_kind}:${link.memory_id}`); if (!a || !b) continue; const startX = a.x + 180; const startY = a.y + 34; const endX = b.x; const endY = b.y + 34; const line = make("div", `memory-link ${link.relation}`); const length = Math.hypot(endX - startX, endY - startY); line.style.left = `${startX}px`; line.style.top = `${startY}px`; line.style.width = `${length}px`; line.style.transform = `rotate(${Math.atan2(endY - startY, endX - startX)}rad)`; line.title = `${link.node_id} ${link.relation.replace("_", " ")} ${link.memory_id}`; graphBox.prepend(line); }
  graphBox.setAttribute("aria-label", `Memory provenance map with ${result.nodes.length} records and ${result.links.length} links. Details follow in the table.`);
}

async function refreshPrompts() { try { promptRecords = await collectRecordPages("/api/prompts", 200); renderPrompts(); } catch (error) { showError(error.message); } }
function renderPrompts() { const lineage = $("promptLineage"); const body = $("promptBody"); lineage.replaceChildren(); body.replaceChildren(); for (const item of promptRecords) { const card = make("article", "lineage-card", ""); card.append(make("strong", "", item.name), make("p", "", `${item.active ? "Active" : "Inactive"} / ${item.id.slice(0, 10)}`), make("p", "field-help", item.parent_id ? `Parent ${item.parent_id.slice(0, 10)}` : "First version")); lineage.append(card); const evidence = Array.isArray(item.metadata?.evidence) ? item.metadata.evidence.join("; ") : ""; const row = document.createElement("tr"); row.append(make("td", "", item.name), make("td", "", item.id), make("td", "", item.parent_id || "None"), make("td", "", item.active ? "Active" : "Inactive"), make("td", "", evidence)); body.append(row); } fillPromptSelect($("promptLeft"), promptRecords[0]?.id); fillPromptSelect($("promptRight"), promptRecords.at(-1)?.id); renderPromptCompare(); }
function fillPromptSelect(select, chosen) { select.replaceChildren(); for (const item of promptRecords) { const option = make("option", "", `${item.name} / ${item.id.slice(0, 10)}`); option.value = item.id; select.append(option); } if (chosen) select.value = chosen; }
function renderPromptCompare() { $("promptLeftBody").textContent = promptRecords.find((item) => item.id === $("promptLeft").value)?.body || "No version selected."; $("promptRightBody").textContent = promptRecords.find((item) => item.id === $("promptRight").value)?.body || "No version selected."; }

function fitGraph() { if (!graph.nodes.length) return; const xs = graph.nodes.map((node) => position(node).x); const ys = graph.nodes.map((node) => position(node).y); const width = Math.max(...xs) - Math.min(...xs) + 240; const height = Math.max(...ys) - Math.min(...ys) + 150; const rect = $("canvas").getBoundingClientRect(); zoom = Math.max(.35, Math.min(1.25, Math.min(rect.width / width, rect.height / height))); pan = {x: 30 - Math.min(...xs) * zoom, y: 30 - Math.min(...ys) * zoom}; updateViewport(); }
function exportGraph() { const blob = new Blob([JSON.stringify(graph, null, 2) + "\n"], {type: "application/json"}); const link = document.createElement("a"); link.href = URL.createObjectURL(blob); link.download = "harness-graph.json"; link.click(); URL.revokeObjectURL(link.href); announce("Graph JSON exported."); }
async function importGraph(file) { try { const candidate = migrateGraph(JSON.parse(await file.text())); const result = await validate(candidate); if (!result.valid) throw new Error("Imported graph failed validation. The current graph was not changed."); pushHistory(); graph = result.graph || candidate; selected = null; focusedNodeId = graph.nodes[0]?.id || ""; render(); fitGraph(); announce("Graph imported."); } catch (error) { showError(error.message); } finally { $("importInput").value = ""; } }

let userViewSelectionRevision = 0;

function switchView(name, options = {}) {
  if (options.userInitiated) userViewSelectionRevision += 1;
  document.querySelectorAll("[data-view]").forEach((button) => {
    button.setAttribute("aria-pressed", String(button.dataset.view === name));
  });
  let activePanel = null;
  document.querySelectorAll("[data-view-panel]").forEach((panel) => {
    panel.hidden = panel.dataset.viewPanel !== name;
    if (!panel.hidden) activePanel = panel;
  });
  // The skip link follows the selected workspace. A fixed target sent keyboard
  // users into the hidden Start view after another tab had been selected.
  if (activePanel?.id) $("skipToWorkspace").href = `#${activePanel.id}`;
  $("workflowActions").hidden = name !== "workflow";
  if (name === "memory") refreshMemory();
  if (name === "prompts") refreshPrompts();
  if (name === "start") { refreshCheckup(); refreshHowItWorks(); }
  if (name === "checks") { refreshChecks(); $("starterUrl").placeholder = window.location.origin + "/"; }
  if (name === "workflow") { fitGraph(); refreshTeamNotes(); renderWhatItIsDoing(); refreshWorkflows(); }
  if (name === "history") refreshHistory();
  if (name === "pipelines") refreshPipelines(undefined, {replaceDrawing: !pipelineBaselineReady});
  if (name === "settings") {
    refreshSettings();
    void refreshWebChatSettingsRecoveryStatus().catch(() => {});
  }
  if (name === "vault") refreshVault(vaultOpen);
  if (name === "team") refreshTeam(teamOpen);
  if (name === "lookup") refreshLookup();
  if (name === "talk") refreshTalk();
  if (name === "swarm") {
    refreshSwarm(undefined, {recoveryOnly: Boolean(options.recoveryOnly)});
  }
}

/* ---- Start here: one plain-language answer to "is this ready?" ---- */

let checkup = null;
let qaSuite = {present: false, cases: [], tags: []};
let qaResult = null;

async function refreshCheckup(fresh = false) {
  try {
    checkup = await request(`/api/checkup${fresh ? "?refresh=1" : ""}`);
    pipelineCannotRun = String(checkup.cannot_run || "");
    showProjectAuthorityPause(checkup.authority, pipelineCannotRun);
    renderCheckup();
  } catch (error) { showError(error.message); }
}

function renderCheckup() {
  if (!checkup) return;
  const list = $("checkupSteps");
  list.replaceChildren();
  const remaining = checkup.steps.filter((step) => !step.done);
  const summary = $("checkupSummary");
  summary.textContent = remaining.length === 0
    ? `${checkup.project} is ready. Ask for a change below, or open Checks.`
    : `${remaining.length} of ${checkup.steps.length} steps still to do in ${checkup.project}.`;
  summary.className = remaining.length === 0 ? "callout good" : "callout";
  for (const step of checkup.steps) {
    const item = make("li", `step ${step.done ? "done" : "todo"}`);
    const heading = make("div", "step-head");
    heading.append(make("span", "step-mark", step.done ? "Done" : "To do"), make("strong", "", step.title));
    item.append(heading, make("p", "", step.detail));
    if (!step.done) item.append(make("p", "field-help", `Run this in a terminal: ${step.action}`));
    if (step.id === "suite" && !step.done) {
      const button = make("button", "", "Write starter checks for me");
      button.type = "button";
      button.addEventListener("click", createSuite);
      item.append(button);
    }
    if (step.id === "provider") item.append(modelSetup());
    list.append(item);
  }
  updateQuickReadiness();
}

function updateQuickReadiness() {
  const button = $("quickRun");
  const message = $("quickReadiness");
  if (!button || !message || !checkup) return;
  const bootstrap = Boolean($("quickBootstrap")?.checked);
  const missing = checkup.steps
    .filter((step) => !step.done && !(bootstrap && ["stack", "commands", "suite"].includes(step.id)))
    .map((step) => step.title);
  const executionPause = executionPauseWords("Project execution", pipelineCannotRun);
  setExecutionControl(button, missing.length > 0, pipelineCannotRun,
    "Start a verified long-horizon run", "Project execution");
  setExecutionControl($("runButton"), false, pipelineCannotRun,
    "Start this workflow run", "Project execution");
  if (executionPause) {
    message.className = "callout";
    message.textContent = executionPause;
  } else if (missing.length) {
    message.className = "callout";
    message.textContent = `Finish setup before starting: ${missing.join(", ")}. Use the steps above, then press Check again.`;
    button.title = message.textContent;
  } else {
    message.className = "callout good";
    message.textContent = bootstrap
      ? "Bootstrap ready: Nexus will create runnable test infrastructure first, then use it to verify the goal."
      : "Ready: every effective model route is connected and Nexus has a real test command and check suite for verifying the result.";
    button.title = "Start a verified long-horizon run";
  }
}

// Whether somebody has opened or closed "Ways to connect a model" by hand.
// null means they have not touched it, so the panel decides.
let modelSetupOpen = null;

function modelSetup() {
  const advice = checkup?.model_setup;
  const box = make("details", "model-setup");
  if (!advice) return box;
  // Open by itself when there is still a model to connect. Once somebody has
  // opened or closed it themselves, that choice is kept: the first screen
  // redraws on its own, and a panel that shuts while you are reading it is a
  // panel nobody can use.
  box.open = modelSetupOpen === null
    ? !checkup.steps.find((step) => step.id === "provider")?.done
    : modelSetupOpen;
  box.addEventListener("toggle", () => { modelSetupOpen = box.open; });
  box.append(make("summary", "", "Ways to connect a model"));
  box.append(make("p", "", advice.headline));
  const list = make("ul", "model-list");
  for (const option of advice.options) {
    const item = make("li", `model-option ${option.state === "ready" ? "ready" : "todo"}`);
    const head = make("p", "model-head");
    const stateLabel = {
      ready: "Connected",
      installed: "Installed",
      "needs attention": "Needs attention",
      "needs setup": "To do",
    }[option.state] || option.state;
    head.append(
      make("span", "step-mark", stateLabel),
      make("strong", "", option.label),
      make("span", "", option.in_use ? " (this project uses it)" : "")
    );
    item.append(head, make("p", "", option.summary), make("p", "field-help", `${option.reason} ${option.cost}`));
    if (option.steps.length) {
      const steps = make("ol", "model-steps");
      for (const line of option.steps) steps.append(make("li", "", line));
      item.append(steps);
    }
    if (canDoForYou.includes(option.id)) {
      // The whole list above, done for somebody who does not want to read it.
      const buttonLabel = option.state === "needs attention"
        ? "Repair this connection"
        : (option.state === "installed" ? "Connect to this project" : "Set this up for me");
      const button = make("button", "do-it-button", buttonLabel);
      button.type = "button";
      button.dataset.option = option.id;
      button.addEventListener("click", () => doItForMe(option.id));
      item.append(button);
      const said = make("div", "do-it-said");
      said.id = `doIt-${option.id}`;
      said.hidden = true;
      // A job already running or just finished for this one goes straight back
      // on screen, so a redraw never swallows the answer.
      if (lastDoItJob && lastDoItJob.option === option.id) fillDoItBox(said, lastDoItJob);
      item.append(said);
    }
    list.append(item);
  }
  box.append(list, make("p", "field-help", advice.note));
  return box;
}

/* ---- I don't care, just do it for me ---- */

// Which ways of connecting a model the harness can set up on its own. Asked
// for once, rather than written down here where it would go stale.
let canDoForYou = [];
let doItTimer = null;
// The last answer, kept so a redraw of the first screen can put it back.
let lastDoItJob = null;

async function loadWhatCanBeDoneForYou() {
  try {
    const said = await request("/api/setup/do-it");
    canDoForYou = said.can_do || [];
  } catch (error) { canDoForYou = []; }
}

async function doItForMe(option) {
  const box = $(`doIt-${option}`);
  if (!box) return;
  document.querySelectorAll(".do-it-button").forEach((button) => { button.disabled = true; });
  box.hidden = false;
  box.replaceChildren(make("p", "field-help", "Starting."));
  try {
    renderDoItJob(await request("/api/setup/do-it", {
      method: "POST", body: JSON.stringify({option}),
    }), option);
    watchDoItJob(option);
  } catch (error) {
    showError(error.message);
    box.replaceChildren(make("p", "do-it-cannot", error.message));
    document.querySelectorAll(".do-it-button").forEach((button) => { button.disabled = false; });
  }
}

function watchDoItJob(option) {
  if (doItTimer) window.clearTimeout(doItTimer);
  const askAgain = async () => {
    try {
      const said = await request("/api/setup/do-it");
      if (said.job) renderDoItJob(said.job, option);
      if (said.busy) { doItTimer = window.setTimeout(askAgain, 1000); return; }
    } catch (error) { showError(error.message); }
    doItTimer = null;
    document.querySelectorAll(".do-it-button").forEach((button) => { button.disabled = false; });
    refreshCheckup();
  };
  doItTimer = window.setTimeout(askAgain, 1000);
}

function renderDoItJob(job, option) {
  lastDoItJob = job;
  const box = $(`doIt-${job.option || option}`);
  if (!box) return;
  fillDoItBox(box, job);
}

// Drawn into a box handed in, rather than one looked up by name, because the
// first screen redraws itself while a job is running and the card holding the
// answer is thrown away and built again. What somebody is reading has to
// survive that, or the answer disappears mid-sentence.
function fillDoItBox(box, job) {
  box.hidden = false;
  box.replaceChildren();
  const steps = make("ol", "do-it-steps");
  for (const step of job.steps || []) {
    const item = make("li", `do-it-step ${step.state}`);
    item.append(make("strong", "", step.text));
    if (step.detail) item.append(make("p", "", step.detail));
    steps.append(item);
  }
  if (job.steps && job.steps.length) box.append(steps);
  if (job.running) box.append(make("p", "field-help", "Working on it. This can take a while."));
  if (job.said) box.append(make("p", job.worked ? "do-it-worked" : "do-it-said-line", job.said));
  for (const line of job.left_for_you || []) {
    box.append(make("p", "do-it-cannot", `Left for you: ${line}`));
  }
  if (job.finished) announce(job.said);
}

async function quickRun() {
  if (pipelineCannotRun) {
    showError(executionPauseWords("Project execution", pipelineCannotRun));
    return;
  }
  const task = $("quickTask").value.trim();
  if (!task) { showError("Say what the harness should do first."); $("quickTask").focus(); return; }
  const bootstrap = Boolean($("quickBootstrap")?.checked);
  const missing = (checkup?.steps || []).filter(
    (step) => !step.done && !(bootstrap && ["stack", "commands", "suite"].includes(step.id))
  );
  if (!checkup || missing.length) {
    updateQuickReadiness();
    showError("Finish the setup steps above before starting. Nexus will not begin work it cannot verify.");
    $("quickReadiness").scrollIntoView({behavior: "smooth", block: "center"});
    return;
  }
  try {
    await request("/api/run", {method: "POST", body: JSON.stringify({
      task, dry_run: $("quickDryRun").checked, bootstrap_tests: bootstrap,
    })});
    announce("Started. Open Workflow to watch the run log.");
    appendEvent("run", "Accepted");
    switchView("workflow");
  } catch (error) { showError(error.message); }
}

/* ---- Checks ---- */

async function refreshChecks() {
  try {
    qaSuite = await request("/api/qa/suite");
    pipelineCannotRun = String(qaSuite.cannot_run || "");
    showProjectAuthorityPause(qaSuite.authority, pipelineCannotRun);
    const stored = await request("/api/qa/result");
    if (stored.result) qaResult = stored.result;
    renderChecks();
    await refreshUnstable();
    await refreshChanged();
  } catch (error) { showError(error.message); }
}

function renderChecks() {
  const select = $("checkTag");
  const chosen = select.value;
  select.replaceChildren(make("option", "", "Every check"));
  select.firstChild.value = "";
  for (const tag of qaSuite.tags || []) { const option = make("option", "", `Only ${tag}`); option.value = tag; select.append(option); }
  if ([...select.options].some((option) => option.value === chosen)) select.value = chosen;
  const resultById = new Map((qaResult?.cases || []).map((item) => [item.id, item]));
  const body = $("checkBody");
  body.replaceChildren();
  if (!qaSuite.present) {
    const row = document.createElement("tr");
    const cell = make("td", "", "This project has no checks yet. Use \"Write starter checks\" to make some from the commands the harness already found.");
    cell.colSpan = 5;
    row.append(cell);
    body.append(row);
  }
  // Choosing a tag narrows the list as well as the run, because showing every
  // check while only some of them would run is confusing.
  const shown = (qaSuite.cases || []).filter((item) => !select.value || (item.tags || []).includes(select.value));
  if (qaSuite.present && !shown.length) {
    const row = document.createElement("tr");
    const cell = make("td", "", `No check carries the tag ${select.value}.`);
    cell.colSpan = 5;
    row.append(cell);
    body.append(row);
  }
  for (const item of shown) {
    const found = resultById.get(item.id);
    const row = document.createElement("tr");
    const status = found ? found.status : "not run yet";
    const outcome = make("td", "");
    outcome.append(make("p", "", found && found.reasons.length ? found.reasons.join(" ") : found ? "As expected" : "Press Run all checks to try this one."));
    // A failure says what happened. What to do about it is a different
    // question, and the one somebody new actually has.
    if (found && !found.passed) {
      const why = make("button", "explain-button", "What does this mean?");
      why.type = "button";
      why.addEventListener("click", () => explainThis(why, found));
      outcome.append(why);
      const meaning = make("div", "explain");
      meaning.id = `explain-${item.id}`;
      meaning.hidden = true;
      outcome.append(meaning);
    }
    const evidence = found?.attempts?.at(-1)?.evidence || "";
    if (evidence) {
      const box = make("details", "");
      box.append(make("summary", "", "Show what the check saw"), make("pre", "", evidence));
      outcome.append(box);
    }
    // A picture of the page as it was when a step went wrong says more than
    // any wording, so it is offered right there under the reason.
    const pictures = (found?.artifacts || []).filter((name) => name.endsWith(".png"));
    if (pictures.length && qaResult?.run_id) {
      const box = make("details", "");
      box.append(make("summary", "", pictures.length === 1 ? "Show the picture" : `Show ${pictures.length} pictures`));
      box.addEventListener("toggle", () => box.open && showPictures(box, qaResult.run_id, pictures), {once: true});
      outcome.append(box);
    }
    const remove = make("button", "", "Remove");
    remove.type = "button";
    remove.title = `Take ${item.id} out of the checks`;
    remove.addEventListener("click", () => removeCheck(item.id, item.title));
    outcome.append(remove);
    if (found && (found.status === "failed" || found.status === "flaky")) {
      // A beginner staring at a failure can ask the model the project is
      // already set up with what it means and what to try.
      const ask = make("button", "", "Ask why this failed");
      ask.type = "button";
      setExecutionControl(ask, false, pipelineCannotRun,
        "Ask the connected model to explain this failure", "Provider contact");
      ask.addEventListener("click", () => explainFailure(found.id, outcome, ask));
      outcome.append(ask);
    }
    row.append(
      make("td", "", `${item.title}\n${item.id}`),
      make("td", "", item.kind),
      make("td", `status-${status.replace(/\s/g, "-")}`, status),
      make("td", "", found ? `${found.duration_ms} ms` : ""),
      outcome
    );
    body.append(row);
  }
  // The button only appears once there is something to photograph.
  $("saveBaselines").hidden = !shown.some((item) => item.kind === "visual");
  renderCheckSummary();
}

function renderCheckSummary() {
  const box = $("checkSummary");
  box.replaceChildren();
  if (!qaResult) return;
  const counts = qaResult.counts || {};
  box.append(
    make("span", `metric ${qaResult.passed ? "metric-good" : "metric-bad"}`, qaResult.passed ? "All checks passed" : "Some checks failed"),
    make("span", "metric", `${counts.passed || 0} passed`),
    make("span", "metric", `${counts.failed || 0} failed`),
    make("span", "metric", `${counts.flaky || 0} flaky`),
    make("span", "metric", `${counts.skipped || 0} skipped`),
    make("span", "metric", `${qaResult.duration_ms} ms`)
  );
}

async function runChecks() {
  const tag = $("checkTag").value;
  try {
    const answer = await request("/api/qa/run", {method: "POST", body: JSON.stringify({tags: tag ? [tag] : []})});
    $("checkStatus").textContent = `Running ${answer.cases} check${answer.cases === 1 ? "" : "s"}.`;
    announce(`Running ${answer.cases} checks.`);
  } catch (error) { showError(error.message); }
}

async function saveBaselines() {
  const shots = (qaSuite?.cases || []).filter((item) => item.kind === "visual").length;
  const ask = `Take a new picture of every screenshot check and keep it?\n\n`
    + `${shots} check${shots === 1 ? "" : "s"} will be photographed. `
    + `The pictures you keep are what every later run is judged against, so only do this when the pages look right.`;
  if (!window.confirm(ask)) return;
  try {
    const answer = await request("/api/qa/baseline", {method: "POST", body: JSON.stringify({})});
    $("checkStatus").textContent = `Taking ${answer.cases} picture${answer.cases === 1 ? "" : "s"}.`;
    announce(`Saving ${answer.cases} screenshots.`);
  } catch (error) { showError(error.message); }
}

async function createSuite() {
  try {
    const answer = await request("/api/qa/init", {method: "POST", body: JSON.stringify({replace: false})});
    announce(`Wrote ${answer.cases} starter check${answer.cases === 1 ? "" : "s"}.`);
    await refreshChecks();
    await refreshCheckup();
    switchView("checks");
  } catch (error) { showError(error.message); }
}

async function refreshChanged() {
  // What moved since last time is the useful question most mornings, so the
  // panel answers it without being asked.
  const box = $("changedSince");
  try {
    const found = await request("/api/qa/changed");
    box.replaceChildren();
    if (found.nothing_to_compare) {
      box.append(make("p", "quiet", found.nothing_to_compare));
      return;
    }
    const groups = [
      ["Started failing", found.broke],
      ["Fixed", found.fixed],
      ["New", found.added],
      ["Gone", found.gone],
      ["Much slower", found.slower],
    ].filter(([, items]) => (items || []).length);
    if (!groups.length) {
      box.append(make("p", "quiet", "Nothing changed since the run before."));
      return;
    }
    for (const [label, items] of groups) {
      box.append(make("h3", "", label));
      const list = make("ul", "changed-list");
      for (const item of items) list.append(make("li", "", `${item.title || item.case_id}: ${item.detail}`));
      box.append(list);
    }
  } catch (error) { showError(error.message); }
}

async function refreshUnstable() {
  try {
    const history = await request("/api/qa/history");
    const advice = $("adviceList");
    advice.replaceChildren();
    if (!(history.advice || []).length) {
      const item = make("li", "step done");
      const head = make("div", "step-head");
      head.append(make("span", "step-mark", "Good"), make("strong", "", "Nothing needs attention"));
      item.append(head, make("p", "", "Your checks look healthy, or there is not enough history yet. Run them a few more times and look again."));
      advice.append(item);
    }
    for (const finding of history.advice || []) {
      const item = make("li", "step todo");
      const head = make("div", "step-head");
      head.append(make("span", "step-mark", "Look"), make("strong", "", `${finding.id} ${finding.problem}`));
      item.append(head, make("p", "", finding.why), make("p", "field-help", finding.what_to_do));
      advice.append(item);
    }
    const body = $("unstableBody");
    body.replaceChildren();
    if (!history.unstable.length) {
      const row = document.createElement("tr");
      const cell = make("td", "", "Nothing looks unstable. Run the checks a few more times to build a history.");
      cell.colSpan = 5;
      row.append(cell);
      body.append(row);
      return;
    }
    for (const item of history.unstable) {
      const row = document.createElement("tr");
      row.append(make("td", "", item.id), make("td", "", String(item.runs)), make("td", "", String(item.failures)), make("td", "", `${Math.round(item.instability * 100)}%`), make("td", "", item.why));
      body.append(row);
    }
  } catch (error) { showError(error.message); }
}

/* ---- Workflows: keep several, and switch between them like tabs ---- */

let savedWorkflows = [];
let currentWorkflow = "";
let workflowDirty = false;

function markWorkflowChanged() {
  workflowDirty = true;
  renderWorkflowTabs();
}

async function refreshWorkflows() {
  try {
    const answer = await request("/api/workflows");
    savedWorkflows = answer.workflows || [];
    renderWorkflowTabs();
  } catch (error) { showError(error.message); }
}

function renderWorkflowTabs() {
  const strip = $("workflowTabs");
  strip.replaceChildren();
  if (!savedWorkflows.length) {
    strip.append(make("p", "field-help", "No saved workflows yet. Press Save to keep this one."));
  }
  for (const item of savedWorkflows) {
    const tab = make("button", `workflow-tab${item.name === currentWorkflow ? " current" : ""}`);
    tab.type = "button";
    tab.setAttribute("role", "tab");
    tab.setAttribute("aria-selected", String(item.name === currentWorkflow));
    tab.append(make("span", "", item.name));
    tab.append(make("span", "tab-count", `${item.nodes} agents`));
    if (!item.valid) {
      tab.append(make("span", "tab-warning", "needs a fix"));
      tab.title = item.issues.join("; ");
    }
    tab.addEventListener("click", () => openWorkflow(item.name));
    strip.append(tab);
  }
  const status = $("workflowStatus");
  if (!currentWorkflow) status.textContent = workflowDirty ? "Unsaved workflow." : "Not saved yet.";
  else status.textContent = workflowDirty ? `${currentWorkflow}: changed, not saved.` : `${currentWorkflow}: saved.`;
}

async function openWorkflow(name) {
  if (workflowDirty && !confirm(`Leave ${currentWorkflow || "this workflow"} without saving?`)) return;
  try {
    const answer = await request(`/api/workflows?name=${encodeURIComponent(name)}`);
    pushHistory();
    graph = migrateGraph(answer.workflow.graph);
    currentWorkflow = answer.workflow.name;
    workflowDirty = false;
    selected = null;
    focusedNodeId = graph.nodes[0]?.id || "";
    nextId = graph.nodes.length + graph.edges.length + 1;
    render();
    fitGraph();
    renderWorkflowTabs();
    await validate();
    announce(`Opened the workflow named ${currentWorkflow}.`);
  } catch (error) { showError(error.message); }
}

async function saveWorkflow() {
  const suggested = currentWorkflow || "My workflow";
  const name = await askForOneLine(
       "Save this workflow", "What should it be called?", suggested);
  if (name === null) return;
  const checked = await validate();
  if (!checked.valid) {
    showError("Fix the workflow before saving it. The problems are listed under Validation.");
    return;
  }
  try {
    const answer = await request("/api/workflows/save", {
      method: "POST", body: JSON.stringify({name, graph}),
    });
    currentWorkflow = answer.saved.name;
    workflowDirty = false;
    await refreshWorkflows();
    announce(`Saved the workflow named ${currentWorkflow}.`);
  } catch (error) { showError(error.message); }
}

async function renameWorkflow() {
  if (!currentWorkflow) { showError("Save this workflow first, then you can rename it."); return; }
  const name = await askForOneLine(
       "Rename this workflow", `What should ${currentWorkflow} be called now?`,
       currentWorkflow);
  if (name === null || name === currentWorkflow) return;
  try {
    const answer = await request("/api/workflows/rename", {
      method: "POST", body: JSON.stringify({name: currentWorkflow, new_name: name}),
    });
    currentWorkflow = answer.saved.name;
    await refreshWorkflows();
    announce(`Renamed to ${currentWorkflow}.`);
  } catch (error) { showError(error.message); }
}

async function deleteWorkflow() {
  if (!currentWorkflow) { showError("There is no saved workflow open to delete."); return; }
  if (!confirm(`Delete the workflow named ${currentWorkflow}? This cannot be undone.`)) return;
  try {
    await request("/api/workflows/delete", {
      method: "POST", body: JSON.stringify({name: currentWorkflow}),
    });
    announce(`Deleted ${currentWorkflow}.`);
    currentWorkflow = "";
    await refreshWorkflows();
  } catch (error) { showError(error.message); }
}

function newWorkflow() {
  if (workflowDirty && !confirm("Start a new workflow without saving this one?")) return;
  pushHistory();
  graph = structuredClone(template);
  currentWorkflow = "";
  workflowDirty = false;
  selected = null;
  focusedNodeId = graph.nodes[0]?.id || "";
  nextId = graph.nodes.length + graph.edges.length + 1;
  render();
  fitGraph();
  renderWorkflowTabs();
  announce("Started a new workflow from the built-in one.");
}

/* ---- History: what past runs did, step by step ---- */

let historyRuns = [];

async function refreshHistory() {
  try {
    const answer = await request("/api/timeline?limit=10");
    historyRuns = answer.runs || [];
    renderHistory();
  } catch (error) { showError(error.message); }
}

function renderHistory() {
  const list = $("historyList");
  const body = $("historyBody");
  list.replaceChildren();
  body.replaceChildren();
  if (!historyRuns.length) {
    list.append(make("p", "empty-state", "No runs yet. Start one from the Start here tab and it will appear here."));
    return;
  }
  for (const run of historyRuns) {
    const card = make("article", "history-run");
    const heading = make("h3", "", run.task || run.run_id);
    const longest = Math.max(1, ...run.steps.map((step) => step.duration_ms || 0));
    card.append(heading, make("p", "field-help", `${run.steps.length} step${run.steps.length === 1 ? "" : "s"}, ${Math.round((run.duration_ms || 0) / 1000)} seconds in total`));
    const bars = make("ol", "history-bars");
    for (const step of run.steps) {
      const item = make("li", "history-bar");
      const width = Math.max(4, Math.round(((step.duration_ms || 0) / longest) * 100));
      const bar = make("span", `bar ${step.result || "unknown"}`);
      bar.style.width = `${width}%`;
      const seconds = Math.round((step.duration_ms || 0) / 1000);
      const outcome = step.result === "failed" ? "failed" : step.result === "passed" ? "passed" : "no result recorded";
      // The bar says everything by its width and colour. Spell the same thing
      // out for anyone reading with a screen reader.
      bar.setAttribute("role", "img");
      bar.setAttribute("aria-label", `${step.node} ${outcome} after ${seconds} seconds`);
      item.append(make("span", "bar-name", step.node), bar, make("span", "bar-time", `${seconds} s`));
      bars.append(item);
      const row = document.createElement("tr");
      row.append(
        make("td", "", run.task || run.run_id),
        make("td", "", step.node),
        make("td", `status-${step.result === "failed" ? "failed" : step.result === "passed" ? "passed" : "skipped"}`, step.result || "no result"),
        make("td", "", `${seconds} s`)
      );
      body.append(row);
    }
    card.append(bars);
    list.append(card);
  }
}

/* ---- Team notes: what the agents told each other ---- */

/* ==========================================================================
   What the agent says it is doing, and anything the harness had to say to it.

   A run used to be a wall of tool calls with no plan behind it, and the only
   sign of one going nowhere was it stopping when the budget ran out. These two
   put both in front of the person watching: the steps the agent means to take,
   and the moment the harness told it that it was going round in circles.
   ========================================================================== */

let whatItIsDoing = [];
let wordsOfWarning = [];

// The most kept on screen. A run that keeps being warned has one problem, not
// forty, and forty lines of it push everything else off the page.
const MOST_WARNINGS = 8;

const HOW_IT_IS_GOING = {
  waiting: "Waiting",
  going: "Going",
  done: "Done",
  dropped: "Dropped",
};

function applyDoingEvent(event) {
  const said = event.payload || {};
  if (event.kind === "the_list") {
    whatItIsDoing = (said.steps || []).map((one) => ({
      what: String(one.what || ""),
      howItIsGoing: String(one.how_it_is_going || "waiting"),
    }));
    renderWhatItIsDoing();
    const going = whatItIsDoing.find((one) => one.howItIsGoing === "going");
    announce(going ? `Now: ${going.what}` : `${whatItIsDoing.length} steps planned.`);
    return;
  }
  wordsOfWarning.push({
    node: String(event.node || "an agent"),
    said: String(said.said || ""),
  });
  wordsOfWarning = wordsOfWarning.slice(-MOST_WARNINGS);
  renderWhatItIsDoing();
  announce(`A word of warning to ${event.node || "an agent"}: ${said.said || ""}`, true);
}

function renderWhatItIsDoing() {
  const list = $("doingList");
  list.replaceChildren();
  const done = whatItIsDoing.filter((one) => one.howItIsGoing === "done").length;
  $("doingCount").textContent = whatItIsDoing.length
    ? `${done} of ${whatItIsDoing.length} done`
    : "Nothing yet.";
  if (!whatItIsDoing.length) {
    const empty = make("li", "doing-one empty-state");
    empty.append(make("p", "", "The agent has not said what it is doing yet. Its steps appear here while a run is going, and stay after it ends."));
    list.append(empty);
  }
  for (const one of whatItIsDoing) {
    const item = make("li", `doing-one ${one.howItIsGoing}`);
    item.append(make("span", "doing-state", HOW_IT_IS_GOING[one.howItIsGoing] || one.howItIsGoing));
    item.append(make("p", "", one.what));
    list.append(item);
  }
  const warnings = $("warningList");
  warnings.replaceChildren();
  for (const one of wordsOfWarning) {
    const item = make("li", "warning-one");
    item.append(make("strong", "", `${one.node}: `), make("span", "", one.said));
    warnings.append(item);
  }
}

/* ==========================================================================
   Which project this is, and getting to another one.

   The harness has always worked on one project at a time, and everything it
   keeps belongs to the folder of that project. What was missing was anywhere
   saying which one you were looking at, and any way to another without
   stopping the harness and starting it again.

   The list itself is about this machine. What a project is called lives in the
   project, so it travels with it.
   ========================================================================== */

let projectsHere = {};
let projectsList = [];
let projectsSidebar = "slide-out";

async function refreshProjects() {
  try {
    const said = await request("/api/projects");
    projectsHere = said.here || {};
    projectsList = said.projects || [];
    projectsSidebar = said.sidebar || "slide-out";
    $("projectBarName").textContent = projectsHere.name || "This project";
    $("projectBarPath").textContent = projectsHere.shortened || projectsHere.path || "";
    $("projectBar").title = projectsHere.path
      ? `${projectsHere.name} - ${projectsHere.path}. Press it to see the others.`
      : "Which project this is.";
    $("projectSidebarHow").value = projectsSidebar;
    sayWhetherWeCanBrowse();
    showTheSidebarTheWayTheyLikeIt();
    renderProjects();
  } catch (error) { showError(error.message); }
}

function showTheSidebarTheWayTheyLikeIt() {
  // Always means it stays there. Slide-out means it is only there while it is
  // wanted, and the page keeps its whole width the rest of the time.
  const always = projectsSidebar === "always";
  document.body.classList.toggle("projects-always", always);
  // The close button is no use when the list is meant to stay, and leaving it
  // there offers somebody a button that undoes their own setting. Decided here
  // rather than only when the list is opened: going back to slide-out never
  // opens anything, and the button stayed gone.
  $("projectSidebarClose").hidden = always;
  if (always) openTheProjects(true);
}

function sayAboutProjects(words) { $("projectSaid").textContent = words; }

function openTheProjects(open) {
  const sidebar = $("projectSidebar");
  sidebar.hidden = !open;
  $("projectBar").setAttribute("aria-expanded", String(Boolean(open)));
  $("projectSidebarClose").hidden = projectsSidebar === "always";
  if (open && !$("projectSidebarClose").hidden) {
    $("projectSidebarClose").focus({preventScroll: true});
  }
}

function renderProjects() {
  const list = $("projectList");
  list.replaceChildren();
  if (!projectsList.length) {
    const empty = make("li", "project-one empty-state");
    empty.append(make("p", "", "Only this one so far. Add a folder below."));
    list.append(empty);
    return;
  }
  for (const one of projectsList) {
    const here = one.path === projectsHere.path;
    const row = make("li", `project-one${here ? " here" : ""}${one.is_there ? "" : " missing"}`);
    row.append(make("strong", "", one.name));
    row.append(make("p", "project-one-path", one.path));
    if (!one.is_there) {
      row.append(make("p", "project-one-path", "That folder is not there any more."));
    }
    const buttons = make("div", "button-row");
    if (here) {
      buttons.append(make("span", "hint", "You are working on this one."));
    } else {
      const open = make("button", "", "Work on this");
      open.type = "button";
      open.disabled = !one.is_there;
      open.addEventListener("click", () => workOnThisProject(one, open));
      buttons.append(open);
    }
    const rename = make("button", "", "Rename");
    rename.type = "button";
    rename.disabled = !one.is_there;
    rename.addEventListener("click", () => renameThisProject(one));
    const forget = make("button", "", "Take off the list");
    forget.type = "button";
    forget.title = "Nothing is deleted. The folder stays where it is.";
    forget.addEventListener("click", () => forgetThisProject(one));
    buttons.append(rename, forget);
    row.append(buttons);
    list.append(row);
  }
}

async function workOnThisProject(one, button) {
  button.disabled = true;
  sayAboutProjects(`Moving to ${one.name}...`);
  try {
    const said = await request("/api/projects/open", {
      method: "POST", body: JSON.stringify({path: one.path}),
    });
    sayAboutProjects(said.note || "");
    if (window.harnessDesktop?.rememberProject && said.here?.path) {
      // The server has already moved. Tell Electron which project it now owns
      // so closing and reopening the app comes back here, not to the folder
      // that happened to start this server.
      try {
        await window.harnessDesktop.rememberProject(said.here.path);
      } catch (error) {
        // Still reload: the server has already moved, so leaving the old
        // project's controls on screen would be more misleading than losing
        // the startup convenience for this one switch.
        showError(error.message);
      }
    }
    // Everything on this page belongs to the project it came from - the
    // workflow, the checks, the automations, what it knows. Reading it fresh
    // is the honest thing, and it is what somebody expects to see.
    window.location.reload();
  } catch (error) {
    showError(error.message);
    sayAboutProjects(error.message);
    button.disabled = false;
  }
}

async function renameThisProject(one) {
  const wanted = await askForOneLine(
    "Rename this project",
    "The name is kept inside the project, so anybody who copies it gets the "
    + "same name. Leave it empty to go back to the name of the folder.",
    one.name
  );
  if (wanted === null) return;
  try {
    const said = await request("/api/projects/rename", {
      method: "POST", body: JSON.stringify({path: one.path, name: wanted}),
    });
    await refreshProjects();
    sayAboutProjects(`Now called ${said.name}.`);
  } catch (error) { showError(error.message); sayAboutProjects(error.message); }
}

async function forgetThisProject(one) {
  if (one.path === projectsHere.path) {
    sayAboutProjects("This is the one you are working on. Move to another one first.");
    return;
  }
  if (!window.confirm(
    `Take ${one.name} off this list?\n\nNothing is deleted. The folder and `
    + `everything in it stays exactly where it is.`
  )) return;
  try {
    const said = await request("/api/projects/forget", {
      method: "POST", body: JSON.stringify({path: one.path}),
    });
    await refreshProjects();
    sayAboutProjects(said.note || "");
  } catch (error) { showError(error.message); sayAboutProjects(error.message); }
}

function canWeBrowseForAFolder() {
  return Boolean(window.harnessDesktop && window.harnessDesktop.pickAFolder);
}

function sayWhetherWeCanBrowse() {
  // In the app there is a real folder picker. In a browser there is not: a page
  // is not allowed to learn where a folder really is on the machine, which is
  // the whole point of that rule. So the button is only offered where it works,
  // and where it does not, the reason is said rather than left to be guessed.
  const can = canWeBrowseForAFolder();
  $("projectBrowse").hidden = !can;
  $("projectBrowseWhyNot").hidden = can;
}

async function browseForAProject() {
  try {
    const chosen = await window.harnessDesktop.pickAFolder();
    if (!chosen) { sayAboutProjects("Nothing was picked."); return; }
    $("projectAddPath").value = chosen;
    sayAboutProjects(`Picked ${chosen}. Press Add it to put it on the list.`);
  } catch (error) { showError(error.message); sayAboutProjects(error.message); }
}

async function addAProject() {
  const path = $("projectAddPath").value.trim();
  if (!path) { sayAboutProjects("Type the folder your project is in."); return; }
  try {
    const said = await request("/api/projects/add", {
      method: "POST", body: JSON.stringify({path}),
    });
    $("projectAddPath").value = "";
    await refreshProjects();
    sayAboutProjects(`${said.project.name} is on the list. Press Work on this to open it.`);
  } catch (error) { showError(error.message); sayAboutProjects(error.message); }
}

async function chooseHowTheSidebarLooks() {
  try {
    const said = await request("/api/projects/sidebar", {
      method: "POST", body: JSON.stringify({how: $("projectSidebarHow").value}),
    });
    projectsSidebar = said.sidebar;
    showTheSidebarTheWayTheyLikeIt();
    sayAboutProjects(projectsSidebar === "always"
      ? "This list will stay where it is."
      : "This list will only come out when you ask for it.");
  } catch (error) { showError(error.message); sayAboutProjects(error.message); }
}

let teamNotes = [];

async function refreshTeamNotes() {
  try {
    const answer = await request("/api/team?limit=100");
    teamNotes = (answer.notes || []).map((note) => ({sequence: note.sequence, from: note.from, to: note.to, subject: note.subject}));
    renderTeamNotes();
  } catch (_) { /* the panel stays empty until a run writes something */ }
}

function applyTeamEvent(event) {
  if (event.kind !== "agent_message") return;
  const note = event.payload || {};
  teamNotes.push({sequence: note.sequence, from: note.from, to: note.to, subject: note.subject});
  teamNotes = teamNotes.slice(-100);
  renderTeamNotes();
  announce(`${note.from} wrote to ${note.to}: ${note.subject}`);
}

function renderTeamNotes() {
  const board = $("teamBoard");
  board.replaceChildren();
  $("teamCount").textContent = teamNotes.length
    ? `${teamNotes.length} note${teamNotes.length === 1 ? "" : "s"}`
    : "No notes yet";
  if (!teamNotes.length) {
    const empty = make("li", "team-note empty-state");
    empty.append(make("p", "", "The agents have not written to each other yet. Notes appear here while a run is going, and stay after it ends."));
    board.append(empty);
    return;
  }
  for (const note of teamNotes) {
    const item = make("li", "team-note");
    const who = make("p", "team-who");
    who.append(make("strong", "", note.from || "an agent"), make("span", "", " told "), make("strong", "", note.to || "everyone"));
    item.append(who, make("p", "", note.subject || ""));
    board.append(item);
  }
}

let starterList = [];

async function refreshStarters() {
  if (starterList.length) return;
  try {
    const answer = await request("/api/qa/starters");
    starterList = answer.starters || [];
  } catch (error) { showError(error.message); return; }
  const box = $("starterList");
  box.replaceChildren();
  for (const item of starterList) {
    const row = make("li", "");
    const head = make("div", "starter-head");
    const add = make("button", "", "Add this");
    add.type = "button";
    add.addEventListener("click", () => addStarter(item.key, add));
    head.append(make("strong", "", item.title), add);
    row.append(
      head,
      make("p", "", item.what_it_does),
      make("p", "starter-needs", `To use it: ${item.change_this}  ·  Needs: ${item.needs}`)
    );
    box.append(row);
  }
}

async function addStarter(key, button) {
  const url = $("starterUrl").value.trim();
  button.disabled = true;
  try {
    const answer = await request("/api/qa/add", {
      method: "POST",
      body: JSON.stringify({starter: key, url}),
    });
    $("checkStatus").textContent = `Added the check ${answer.added}. Press Run all checks to try it.`;
    announce($("checkStatus").textContent);
    await refreshChecks();
  } catch (error) { showError(error.message); }
  button.disabled = false;
}

async function explainFailure(caseId, holder, button) {
  if (pipelineCannotRun) {
    showError(executionPauseWords("Provider contact", pipelineCannotRun));
    return;
  }
  button.disabled = true;
  button.textContent = "Asking...";
  try {
    const answer = await request("/api/qa/explain", {
      method: "POST",
      body: JSON.stringify({case: caseId}),
    });
    // The answer is written in as text, never as page code.
    const box = make("div", "explain-box", answer.answer);
    holder.append(box);
    button.textContent = "Asked";
    announce("The model answered about this check.");
  } catch (error) {
    showError(error.message);
    button.disabled = false;
    button.textContent = "Ask why this failed";
  }
}

async function showPictures(box, runId, names) {
  // The pictures are fetched with the session key in a header, never in the
  // address, so nothing sensitive ends up in a link or a log line.
  for (const name of names) {
    const line = make("p", "quiet", name);
    box.append(line);
    try {
      const answer = await fetch(`/api/qa/picture?path=${encodeURIComponent(runId + "/" + name)}`, {
        headers: {"X-Harness-Token": token},
      });
      if (!answer.ok) throw new Error(`HTTP ${answer.status}`);
      const picture = document.createElement("img");
      picture.className = "run-picture";
      picture.alt = `The page during ${name}`;
      picture.src = URL.createObjectURL(await answer.blob());
      picture.addEventListener("load", () => URL.revokeObjectURL(picture.src), {once: true});
      box.append(picture);
    } catch (error) {
      box.append(make("p", "quiet", `That picture could not be shown: ${error.message}`));
    }
  }
}

async function removeCheck(id, title) {
  if (!window.confirm(`Take the check "${title || id}" out of this project?`)) return;
  try {
    const answer = await request("/api/qa/remove", {method: "POST", body: JSON.stringify({case: id})});
    $("checkStatus").textContent = `Took ${answer.removed} out. ${answer.cases} checks left.`;
    announce($("checkStatus").textContent);
    await refreshChecks();
  } catch (error) { showError(error.message); }
}

async function makeBundle() {
  try {
    $("checkStatus").textContent = "Packing up the checks, the last few runs, and the settings.";
    const answer = await request("/api/bundle", {method: "POST", body: JSON.stringify({})});
    const left = (answer.left_out || []).length;
    $("checkStatus").textContent = `Wrote ${answer.path} with ${answer.files} file${answer.files === 1 ? "" : "s"}.`
      + (left ? ` ${left} thing${left === 1 ? " was" : "s were"} left out; the list is inside the zip.` : "")
      + " Credentials were taken out. Read it before sending it on.";
    announce($("checkStatus").textContent);
  } catch (error) { showError(error.message); }
}

async function recordSteps() {
  const address = await askForOneLine(
    "Which page should open?",
    "Do the thing you want to check in the window that opens, then press Done "
    + "in the bar at the top.",
    window.location.origin + "/"
  );
  if (!address) return;
  try {
    await request("/api/qa/record", {method: "POST", body: JSON.stringify({url: address})});
    $("checkStatus").textContent = "A browser window is opening. Do the thing you want to check, then press Done.";
    announce($("checkStatus").textContent);
  } catch (error) { showError(error.message); }
}

async function pickElement() {
  const address = await askForOneLine(
    "Which page should open?",
    "Click the thing you want to check in the window that opens. Press Escape "
    + "there to give up.",
    window.location.origin + "/"
  );
  if (!address) return;
  try {
    await request("/api/qa/pick", {method: "POST", body: JSON.stringify({url: address})});
    $("checkStatus").textContent = "A browser window is opening. Click the thing you want to check.";
    announce("A browser window is opening. Click the thing you want to check.");
  } catch (error) { showError(error.message); }
}

async function makeSharePage() {
  try {
    const made = await request("/api/qa/share", {method: "POST", body: JSON.stringify({})});
    const pictures = Number(made.pictures || 0);
    $("checkStatus").textContent = `Wrote ${made.path}. It is one file with `
      + `${pictures} picture${pictures === 1 ? "" : "s"} inside it, so you can send it to anyone.`
      + (made.left_out && made.left_out.length ? ` Left out: ${made.left_out.slice(0, 3).join("; ")}` : "");
    announce($("checkStatus").textContent);
  } catch (error) { showError(error.message); }
}

/* ---- Setting up a subscription you already pay for ---- */

let seatsFound = null;

function markSeatStep(id, state) {
  const step = $(id);
  step.classList.remove("doing", "waiting", "done");
  step.classList.add(state);
}

function renderSeats(found) {
  seatsFound = found;
  const list = $("seatList");
  list.replaceChildren();
  for (const seat of found.seats || []) {
    const row = make("li", `seat ${seat.ready ? "ready" : "not-ready"}`);
    row.append(make("span", "seat-state", seat.ready ? "Installed" : "Not here"));
    const detail = make("div", "");
    detail.append(make("strong", "", seat.label));
    detail.append(make("p", "", seat.ready
      ? `${seat.version}${seat.found_via ? ` — from ${seat.found_via}` : ""}`
      : seat.why_not));
    if (!seat.ready && seat.install_hint) detail.append(make("p", "field-help", seat.install_hint));
    if (seat.ready && seat.already_set_up) detail.append(make("p", "field-help", "A route for this is already in your settings."));
    row.append(detail);
    list.append(row);
  }
  const ready = (found.seats || []).filter((seat) => seat.ready);
  $("seatFindSaid").textContent = ready.length
    ? `${ready.length} of ${(found.seats || []).length} are installed. Connect them below to verify their subscriptions.`
    : "None of them are on this machine yet. Install one, then press the button again.";
  markSeatStep("seatStepFind", "done");
  markSeatStep("seatStepWrite", ready.length ? "doing" : "waiting");
  $("setUpSeats").disabled = ready.length === 0;
  $("setUpSeats").textContent = ready.length > 1
    ? `Set up all ${ready.length} for me`
    : "Set it up for me";
  announce($("seatFindSaid").textContent);
}

/* ---- What does this mean? ----

   A failing check says what happened. This says what it means and what to try,
   in the words somebody who has not seen it before would use.
*/

async function explainThis(button, found) {
  const box = $(`explain-${found.id}`);
  if (!box) return;
  if (!box.hidden) { box.hidden = true; button.textContent = "What does this mean?"; return; }
  button.disabled = true;
  try {
    const said = [
      ...(found.reasons || []),
      found.attempts?.at(-1)?.evidence || "",
    ].join("\n");
    const meaning = await request("/api/explain", {
      method: "POST", body: JSON.stringify({said, kind: found.kind}),
    });
    box.replaceChildren();
    box.append(make("strong", "", meaning.headline));
    if (meaning.because) box.append(make("p", "", meaning.because));
    if ((meaning.try_this || []).length) {
      box.append(make("p", "field-help", "Worth trying:"));
      const list = make("ul", "explain-list");
      for (const line of meaning.try_this) list.append(make("li", "", line));
      box.append(list);
    }
    if (!meaning.sure) {
      box.append(make("p", "field-help",
        "That is a guess at what to look at, not a diagnosis. The harness did not "
        + "recognise this one."));
    }
    box.hidden = false;
    button.textContent = "Hide that";
    announce(meaning.headline);
  } catch (error) { showError(error.message); }
  button.disabled = false;
}

/* ---- What it knows ----

   Everything the harness has learned, drawn the way people already draw notes:
   a circle for each note, a line for each link, and the whole thing settling
   into a shape by pushing every circle apart and pulling linked ones together.

   The picture is the point, so it says as much as a picture can: a note used
   often is bigger, a note nothing has touched for months is dimmed, a link to
   a note nobody has written yet is drawn as an outline. None of that needs
   reading a word.
*/

let vaultNotes = [];
let vaultLinks = [];
let vaultMissing = [];
let vaultTags = [];
let vaultKinds = [];
let vaultOpen = "";
let vaultLooking = "";
// What was asked of the harness, when the vault is too large to sift here.
let vaultAskingFor = "";
let vaultKindsWanted = new Set();
let vaultTagWanted = "";
let vaultPlaces = new Map();
let vaultSettling = null;
let vaultEditing = "";
let vaultReached = "";

async function refreshVault(name) {
  try {
    // With more notes than the picture draws, the sifting happens where the
    // notes are; below that the page sifts what it already has, which is
    // quicker than asking.
    const parts = [];
    if (name) parts.push(`name=${encodeURIComponent(name)}`);
    if (vaultAskingFor) parts.push(`q=${encodeURIComponent(vaultAskingFor)}`);
    const asked = parts.length ? `?${parts.join("&")}` : "";
    const said = await request(`/api/vault${asked}`);
    vaultNotes = said.notes || [];
    vaultLinks = said.links || [];
    vaultMissing = said.not_written_yet || [];
    vaultTags = said.tags || [];
    vaultKinds = said.kinds || [];
    const counts = said.counts || {};
    $("vaultCounts").textContent =
      `${counts.notes || 0} notes, ${counts.links || 0} links`
      + (counts.not_written_yet ? `, ${counts.not_written_yet} not written yet` : "")
      + (counts.stale ? `, ${counts.stale} going stale` : "");
    renderVaultKinds();
    renderVaultTags();
    renderVaultList();
    settleTheVault();
    offerToStartTheVault();
    if (said.open) showVaultNote(said.open);
    else if (said.gone) {
      // Removed here, or in an editor, since this page last looked.
      vaultOpen = "";
      $("vaultNote").hidden = true;
      $("vaultSaid").textContent = `${said.gone} is not here any more.`;
      renderVaultList();
    }
  } catch (error) { showError(error.message); $("vaultSaid").textContent = error.message; }
}

// An empty vault is a blank page, which is the hardest thing to hand anybody.
// It is offered a start rather than given one: opening a tab must never leave
// files behind in somebody's project.
function offerToStartTheVault() {
  const said = $("vaultSaid");
  if (vaultNotes.length) {
    const offer = document.getElementById("vaultStart");
    if (offer) offer.remove();
    return;
  }
  if (document.getElementById("vaultStart")) return;
  said.textContent = "Nothing has been written down yet.";
  const start = make("button", "primary", "Write two notes to start me off");
  start.type = "button";
  start.id = "vaultStart";
  start.addEventListener("click", async () => {
    start.disabled = true;
    try {
      const answer = await request("/api/vault/start", {method: "POST", body: "{}"});
      await refreshVault();
      $("vaultSaid").textContent = answer.note;
      announce(answer.note);
    } catch (error) { showError(error.message); start.disabled = false; }
  });
  said.after(start);
}

function vaultShown() {
  const looking = vaultLooking.trim().toLowerCase();
  return vaultNotes.filter((note) => {
    if (vaultKindsWanted.size && !vaultKindsWanted.has(note.kind)) return false;
    if (vaultTagWanted && !(note.tags || []).includes(vaultTagWanted)) return false;
    if (looking) {
      const haystack = `${note.title} ${note.body} ${(note.tags || []).join(" ")}`.toLowerCase();
      if (!haystack.includes(looking)) return false;
    }
    if ($("vaultOnlyNear").checked && vaultOpen) {
      if (note.name === vaultOpen) return true;
      return vaultLinks.some((link) =>
        (link.from === vaultOpen && link.to === note.name)
        || (link.to === vaultOpen && link.from === note.name));
    }
    return true;
  });
}

function renderVaultKinds() {
  const box = $("vaultKinds");
  box.replaceChildren();
  for (const kind of vaultKinds) {
    const many = vaultNotes.filter((note) => note.kind === kind.kind).length;
    const button = make("button", `vault-kind kind-${kind.kind}${vaultKindsWanted.has(kind.kind) ? " chosen" : ""}`,
      `${kind.name} (${many})`);
    button.type = "button";
    button.title = kind.means;
    button.dataset.kind = kind.kind;
    button.setAttribute("aria-pressed", String(vaultKindsWanted.has(kind.kind)));
    button.addEventListener("click", () => {
      if (vaultKindsWanted.has(kind.kind)) vaultKindsWanted.delete(kind.kind);
      else vaultKindsWanted.add(kind.kind);
      renderVaultKinds();
      renderVaultList();
      settleTheVault();
    });
    box.append(button);
  }
}

function renderVaultTags() {
  const box = $("vaultTags");
  box.replaceChildren();
  if (!vaultTags.length) {
    box.append(make("p", "hint", "No tags yet."));
    return;
  }
  for (const tag of vaultTags) {
    const button = make("button", `vault-tag${vaultTagWanted === tag.tag ? " chosen" : ""}`,
      `#${tag.tag} ${tag.notes}`);
    button.type = "button";
    button.dataset.tag = tag.tag;
    button.setAttribute("aria-pressed", String(vaultTagWanted === tag.tag));
    button.addEventListener("click", () => {
      vaultTagWanted = vaultTagWanted === tag.tag ? "" : tag.tag;
      renderVaultTags();
      renderVaultList();
      settleTheVault();
    });
    box.append(button);
  }
}

function renderVaultList() {
  const list = $("vaultList");
  list.replaceChildren();
  const shown = vaultShown();
  if (!shown.length) {
    list.append(make("li", "hint", "No notes match that."));
    return;
  }
  for (const note of shown) {
    const item = make("li", "");
    const button = make("button", `vault-list-one kind-${note.kind}${note.name === vaultOpen ? " chosen" : ""}${note.stale ? " stale" : ""}`, note.title);
    button.type = "button";
    button.dataset.note = note.name;
    button.addEventListener("click", () => openVaultNote(note.name));
    item.append(button);
    list.append(item);
  }
}

/* ---- the picture ---- */

// Every circle pushes every other away, every link pulls its two together, and
// the middle pulls gently on everything so nothing drifts off the page. Run a
// few hundred times, that settles into a shape where linked notes sit together
// - which is the whole point of drawing it at all.
// The most notes worth drawing at once. Beyond this the picture is a cloud
// nobody can read anyway, and the settling is slow enough to be felt.
const MOST_TO_DRAW = 300;

// Settling is heavy, and a search box sends a letter at a time. Waiting for
// somebody to stop typing turns twelve settlings into one.
function settleTheVaultSoon() {
  if (vaultSettling) window.clearTimeout(vaultSettling);
  vaultSettling = window.setTimeout(() => { vaultSettling = null; settleTheVault(); }, 180);
}

function settleTheVault() {
  if (vaultSettling) { window.clearTimeout(vaultSettling); vaultSettling = null; }
  const all = vaultShown();
  const shown = all.slice(0, MOST_TO_DRAW);
  if (all.length > shown.length) {
    $("vaultSaid").textContent =
      `Drawing the first ${shown.length} of ${all.length} notes. Search, or choose a kind, to see fewer.`;
  }
  const names = shown.map((note) => note.name);
  const box = $("vaultGraph").getBoundingClientRect();
  const wide = Math.max(320, box.width - 40);
  const tall = Math.max(260, box.height - 40);
  const places = new Map();
  shown.forEach((note, spot) => {
    const already = vaultPlaces.get(note.name);
    const round = (spot / Math.max(1, shown.length)) * Math.PI * 2;
    places.set(note.name, already || {
      x: wide / 2 + Math.cos(round) * Math.min(wide, tall) * 0.32,
      y: tall / 2 + Math.sin(round) * Math.min(wide, tall) * 0.32,
    });
  });
  const near = vaultLinks.filter((link) => names.includes(link.from) && names.includes(link.to));
  const rounds = shown.length > 120 ? 60 : shown.length > 60 ? 120 : 260;
  for (let round = 0; round < rounds; round += 1) {
    for (const one of names) {
      for (const other of names) {
        if (one === other) continue;
        const a = places.get(one), b = places.get(other);
        let dx = a.x - b.x, dy = a.y - b.y;
        let far = Math.sqrt(dx * dx + dy * dy) || 0.01;
        if (far > 260) continue;
        const push = 900 / (far * far);
        a.x += (dx / far) * push;
        a.y += (dy / far) * push;
      }
    }
    for (const link of near) {
      const a = places.get(link.from), b = places.get(link.to);
      const dx = b.x - a.x, dy = b.y - a.y;
      const far = Math.sqrt(dx * dx + dy * dy) || 0.01;
      const pull = (far - 150) * 0.012;
      a.x += (dx / far) * pull; a.y += (dy / far) * pull;
      b.x -= (dx / far) * pull; b.y -= (dy / far) * pull;
    }
    for (const one of names) {
      const at = places.get(one);
      at.x += (wide / 2 - at.x) * 0.006;
      at.y += (tall / 2 - at.y) * 0.006;
      at.x = Math.max(30, Math.min(wide - 30, at.x));
      at.y = Math.max(30, Math.min(tall - 30, at.y));
    }
  }
  // Settling gives a shape; it does not give a size. Left alone, a handful of
  // notes ends up huddled in one corner of a large board with their names on
  // top of each other. So the shape is stretched to fill the space it has,
  // keeping every distance in proportion.
  vaultPlaces = spreadOut(places, wide, tall);
  drawTheVault();
}

// Take a settled shape and fit it to the board, without changing the shape.
function spreadOut(places, wide, tall) {
  const all = [...places.values()];
  if (all.length < 2) {
    if (all.length === 1) { all[0].x = wide / 2; all[0].y = tall / 2; }
    return places;
  }
  const left = Math.min(...all.map((at) => at.x));
  const right = Math.max(...all.map((at) => at.x));
  const top = Math.min(...all.map((at) => at.y));
  const bottom = Math.max(...all.map((at) => at.y));
  // Room for the names, which sit under each circle.
  const edge = 70;
  const room = {wide: Math.max(80, wide - edge * 2), tall: Math.max(80, tall - edge * 2)};
  const spread = Math.min(
    room.wide / Math.max(1, right - left),
    room.tall / Math.max(1, bottom - top),
    2.4,
  );
  const middleX = (left + right) / 2, middleY = (top + bottom) / 2;
  for (const at of all) {
    at.x = wide / 2 + (at.x - middleX) * spread;
    at.y = tall / 2 + (at.y - middleY) * spread;
  }
  return places;
}

function drawTheVault() {
  // Only what has a place: the settling draws the first so many, and a circle
  // with nowhere to be would land in the corner.
  const shown = vaultShown().filter((note) => vaultPlaces.has(note.name));
  const names = new Set(shown.map((note) => note.name));
  const wires = $("vaultWires");
  const box = $("vaultNodes");
  wires.replaceChildren();
  box.replaceChildren();

  for (const link of vaultLinks) {
    if (!names.has(link.from) || !names.has(link.to)) continue;
    const a = vaultPlaces.get(link.from), b = vaultPlaces.get(link.to);
    if (!a || !b) continue;
    const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
    line.setAttribute("x1", String(a.x)); line.setAttribute("y1", String(a.y));
    line.setAttribute("x2", String(b.x)); line.setAttribute("y2", String(b.y));
    const touching = vaultOpen && (link.from === vaultOpen || link.to === vaultOpen);
    line.setAttribute("class", `vault-wire${touching ? " near" : ""}`);
    wires.append(line);
  }

  for (const note of shown) {
    const at = vaultPlaces.get(note.name);
    if (!at) continue;
    const many = vaultLinks.filter((link) => link.from === note.name || link.to === note.name).length;
    const size = Math.round(13 + Math.min(16, many * 2.2 + note.uses * 1.2));
    const dot = make("button", `vault-dot kind-${note.kind}${note.name === vaultOpen ? " chosen" : ""}${note.stale ? " stale" : ""}`);
    dot.type = "button";
    dot.dataset.note = note.name;
    dot.style.left = `${at.x}px`;
    dot.style.top = `${at.y}px`;
    dot.style.width = `${size}px`;
    dot.style.height = `${size}px`;
    dot.title = `${note.title} - ${note.kind}${note.stale ? ", going stale" : ""}`;
    dot.setAttribute("aria-label",
      `${note.title}. ${note.kind}. ${many} link${many === 1 ? "" : "s"}.`
      + (note.stale ? " Going stale." : ""));
    dot.addEventListener("click", () => openVaultNote(note.name));
    dot.addEventListener("focus", () => { vaultReached = note.name; });
    box.append(dot);
    const label = make("span", `vault-label${note.name === vaultOpen ? " chosen" : ""}`, note.title);
    label.style.left = `${at.x}px`;
    label.style.top = `${at.y + size / 2 + 3}px`;
    label.setAttribute("aria-hidden", "true");
    box.append(label);
  }

  // A link to a note nobody has written yet, drawn as an outline. These are the
  // most useful thing in the picture: what somebody meant to write down.
  for (const link of vaultMissing) {
    if (!names.has(link.from)) continue;
    const a = vaultPlaces.get(link.from);
    if (!a) continue;
    const spot = {x: Math.max(24, a.x + 70), y: Math.max(24, a.y - 46)};
    const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
    line.setAttribute("x1", String(a.x)); line.setAttribute("y1", String(a.y));
    line.setAttribute("x2", String(spot.x)); line.setAttribute("y2", String(spot.y));
    line.setAttribute("class", "vault-wire not-yet");
    wires.append(line);
    const dot = make("button", "vault-dot not-yet", "");
    dot.type = "button";
    dot.dataset.note = `not-yet-${link.to}`;
    dot.style.left = `${spot.x}px`;
    dot.style.top = `${spot.y}px`;
    dot.title = `${link.to} - nobody has written this one yet. Press to write it.`;
    dot.setAttribute("aria-label", `${link.to}, not written yet. Press to write it.`);
    dot.addEventListener("click", () => writeTheMissingNote(link.to));
    box.append(dot);
  }
}

function openVaultNote(name) {
  vaultOpen = name;
  renderVaultList();
  drawTheVault();
  refreshVault(name);
}

async function writeTheMissingNote(name) {
  openVaultDialog({
    title: String(name || "").replace(/-/g, " "),
    kind: "about-this-project",
    tags: [],
    sure: 0.5,
    body: "",
  }, "");
}

function showVaultNote(around) {
  const note = around.note;
  vaultOpen = note.name;
  $("vaultNote").hidden = false;
  $("vaultNoteTitle").textContent = note.title;
  const kind = (vaultKinds.find((one) => one.kind === note.kind) || {}).name || note.kind;
  $("vaultNoteAbout").textContent =
    `${kind}. Learned ${note.learned || "at some point"}, last touched ${note.touched || "then"}.`
    + (note.came_from ? ` From ${note.came_from}.` : "")
    + (note.uses ? ` Used ${note.uses} time${note.uses === 1 ? "" : "s"}, helped ${note.worked}.` : "")
    + (note.stale ? " Nothing has touched this for a long time, so it may no longer be true." : "");
  const body = $("vaultNoteBody");
  body.replaceChildren();
  for (const line of (note.body || "").split("\n")) {
    if (!line.trim()) continue;
    body.append(vaultLine(line));
  }
  const links = $("vaultNoteLinks");
  links.replaceChildren();
  links.append(vaultSideList("This points at", around.points_at));
  links.append(vaultSideList("These point here", around.points_here));
  const tags = make("p", "vault-note-tags", "");
  for (const tag of note.tags || []) {
    const one = make("button", "vault-tag", `#${tag}`);
    one.type = "button";
    one.addEventListener("click", () => {
      vaultTagWanted = tag; renderVaultTags(); renderVaultList(); settleTheVault();
    });
    tags.append(one);
  }
  if ((note.tags || []).length) links.append(tags);
  $("vaultSaid").textContent = `${note.title} is open.`;
}

// A line of a note, with [[links]] turned into something you can press.
function vaultLine(line) {
  const held = make("p", "");
  const parts = String(line).split(/(\[\[[^\]]+\]\])/g);
  for (const part of parts) {
    const found = /^\[\[([^\]|]+)(?:\|([^\]]+))?\]\]$/.exec(part);
    if (!found) { held.append(document.createTextNode(part)); continue; }
    const name = found[1].trim();
    const words = (found[2] || found[1]).trim();
    const there = vaultNotes.some((note) => note.name === asAVaultName(name));
    const link = make("button", `vault-inline-link${there ? "" : " not-yet"}`, words);
    link.type = "button";
    link.title = there ? `Open ${name}` : `${name} is not written yet. Press to write it.`;
    link.addEventListener("click", () =>
      there ? openVaultNote(asAVaultName(name)) : writeTheMissingNote(asAVaultName(name)));
    held.append(link);
  }
  return held;
}

function asAVaultName(title) {
  return String(title || "").trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
}

function vaultSideList(said, notes) {
  const box = make("div", "vault-side-list");
  box.append(make("h4", "", `${said} (${(notes || []).length})`));
  if (!(notes || []).length) {
    box.append(make("p", "hint", "Nothing yet."));
    return box;
  }
  const list = make("ul", "");
  for (const note of notes) {
    const item = make("li", "");
    const button = make("button", `vault-list-one kind-${note.kind}`, note.title);
    button.type = "button";
    button.addEventListener("click", () => openVaultNote(note.name));
    item.append(button);
    list.append(item);
  }
  box.append(list);
  return box;
}

/* ---- writing one ---- */

function openVaultDialog(note, changing) {
  vaultEditing = changing || "";
  const kinds = $("vaultFormKind");
  kinds.replaceChildren();
  for (const kind of vaultKinds) {
    const option = make("option", "", kind.name);
    option.value = kind.kind;
    kinds.append(option);
  }
  $("vaultDialogTitle").textContent = vaultEditing ? "Change this note" : "A new note";
  $("vaultFormTitle").value = note.title || "";
  $("vaultFormKind").value = note.kind || "about-this-project";
  $("vaultFormTags").value = (note.tags || []).join(", ");
  $("vaultFormSure").value = String(note.sure ?? 0.5);
  $("vaultFormBody").value = note.body || "";
  renderVaultBodyCount();
  $("vaultFormError").hidden = true;
  $("vaultFormError").textContent = "";
  $("vaultDialog").showModal();
}

function newVaultNote() {
  openVaultDialog({title: "", kind: "about-you", tags: [], sure: 0.5, body: ""}, "");
}

function editVaultNote() {
  const note = vaultNotes.find((one) => one.name === vaultOpen);
  if (!note) return;
  openVaultDialog(note, note.name);
}

async function saveVaultNote() {
  const tags = $("vaultFormTags").value.split(",").map((tag) => tag.trim()).filter(Boolean);
  const bodyProblem = vaultBodyProblem();
  if (bodyProblem) {
    $("vaultFormError").textContent = bodyProblem;
    $("vaultFormError").hidden = false;
    $("vaultFormError").focus();
    announce(bodyProblem, true);
    return;
  }
  $("vaultFormError").hidden = true;
  $("vaultFormError").textContent = "";
  try {
    const said = await request("/api/vault/write", {
      method: "POST",
      body: JSON.stringify({
        // Which note this is, when it is one that already exists. Without it a
        // change of title would leave the old file behind and the vault would
        // quietly hold two.
        was: vaultEditing,
        title: $("vaultFormTitle").value,
        kind: $("vaultFormKind").value,
        tags,
        sure: Number($("vaultFormSure").value),
        body: $("vaultFormBody").value,
      }),
    });
    $("vaultDialog").close();
    vaultEditing = "";
    await refreshVault(said.note.name);
    $("vaultSaid").textContent = `${said.note.title} is written down.`;
    announce($("vaultSaid").textContent);
  } catch (error) {
    showError(error.message);
    $("vaultFormError").textContent = error.message;
    $("vaultFormError").hidden = false;
    $("vaultFormError").focus();
    $("vaultSaid").textContent = error.message;
  }
}

async function removeVaultNote() {
  const note = vaultNotes.find((one) => one.name === vaultOpen);
  if (!note) return;
  if (!window.confirm(`Remove the note "${note.title}"? The file is deleted.`)) return;
  try {
    const said = await request("/api/vault/remove", {
      method: "POST", body: JSON.stringify({name: note.name}),
    });
    vaultOpen = "";
    $("vaultNote").hidden = true;
    await refreshVault();
    $("vaultSaid").textContent = said.note;
  } catch (error) { showError(error.message); }
}

// Saying a note helped is what turns a pile of them into something that gets
// better: the ones that earn their place grow, and the ones that do not fade.
async function vaultNoteWasUsed(wentWell) {
  const note = vaultNotes.find((one) => one.name === vaultOpen);
  if (!note) return;
  try {
    const said = await request("/api/vault/used", {
      method: "POST", body: JSON.stringify({name: note.name, went_well: wentWell}),
    });
    await refreshVault(said.note.name);
    $("vaultSaid").textContent = wentWell
      ? `${said.note.title} has helped ${said.note.worked} of ${said.note.uses} times.`
      : `${said.note.title} did not help that time. It is marked down.`;
    announce($("vaultSaid").textContent);
  } catch (error) { showError(error.message); }
}

async function vaultLearnFromRuns() {
  const button = $("vaultLearn");
  button.disabled = true;
  $("vaultSaid").textContent = "Reading what the harness remembers.";
  try {
    const said = await request("/api/vault/learn", {method: "POST", body: "{}"});
    await refreshVault();
    $("vaultSaid").textContent = said.note;
    announce(said.note);
  } catch (error) { showError(error.message); $("vaultSaid").textContent = error.message; }
  button.disabled = false;
}

// The picture without a pointer: arrow keys walk the notes, Enter opens one.
function vaultGraphKey(event) {
  const dots = [...$("vaultNodes").querySelectorAll(".vault-dot:not(.not-yet)")];
  if (!dots.length) return;
  const at = dots.findIndex((dot) => dot.dataset.note === vaultReached);
  const forward = ["ArrowRight", "ArrowDown"].includes(event.key);
  const back = ["ArrowLeft", "ArrowUp"].includes(event.key);
  if (forward || back) {
    event.preventDefault();
    const next = at < 0 ? 0 : (at + (forward ? 1 : -1) + dots.length) % dots.length;
    vaultReached = dots[next].dataset.note;
    dots[next].focus();
    const note = vaultNotes.find((one) => one.name === vaultReached);
    if (note) $("vaultSaid").textContent = `${note.title}. Press Enter to open it.`;
    return;
  }
  if (event.key === "Enter" && vaultReached) {
    event.preventDefault();
    openVaultNote(vaultReached);
  }
}

/* ---- Settings ----

   Every setting the harness has, in plain words, with what it is set to, what
   it shipped as, and which file that value came from. Changing one writes it
   and reads the whole thing back; anything the harness refuses is put straight
   back, with the reason.
*/

let settingsHeld = [];
let settingsGroups = [];
const MORE_OPTIONS_KEY = "nexus-harness-more-options";

function moreOptionsAreEnabled() {
  try { return window.localStorage.getItem(MORE_OPTIONS_KEY) === "yes"; }
  catch (_error) { return false; }
}

function applyMoreOptionsPreference({open = false} = {}) {
  const enabled = moreOptionsAreEnabled();
  const menu = $("moreOptionsMenu");
  menu.hidden = !enabled;
  if (!enabled) menu.open = false;
  else if (open) menu.open = true;
  $("moreOptionsEnabled").checked = enabled;
}

function changeMoreOptionsPreference() {
  const enabled = $("moreOptionsEnabled").checked;
  try { window.localStorage.setItem(MORE_OPTIONS_KEY, enabled ? "yes" : "no"); }
  catch (_error) {
    $("moreOptionsEnabled").checked = false;
    announce("This browser could not remember the More options setting.", true);
  }
  applyMoreOptionsPreference({open: enabled});
  announce(enabled ? "More options is enabled." : "More options is disabled.");
}

async function refreshSettings() {
  applyMoreOptionsPreference();
  try {
    const said = await request("/api/settings");
    settingsHeld = said.settings || [];
    settingsGroups = said.groups || [];
    renderSettings();
    $("settingsSaid").textContent =
      `${settingsHeld.length} settings. ${settingsHeld.filter((one) => one.changed).length} are not as they shipped.`;
  } catch (error) { showError(error.message); $("settingsSaid").textContent = error.message; }
}

function renderSettings() {
  const list = $("settingsList");
  list.replaceChildren();
  const looking = $("settingsFilter").value.trim().toLowerCase();
  const onlyChanged = $("settingsChangedOnly").checked;
  const shown = settingsHeld.filter((one) => {
    if (onlyChanged && !one.changed) return false;
    if (!looking) return true;
    const current = typeof one.value === "string" ? one.value : JSON.stringify(one.value ?? "");
    return `${one.key} ${one.label} ${one.means} ${current}`.toLowerCase().includes(looking);
  });
  if (!shown.length) {
    list.append(make("p", "field-help", "Nothing matches that."));
    return;
  }
  const byGroup = [];
  for (const one of shown) {
    let group = byGroup.find((item) => item.name === one.group);
    if (!group) { group = {name: one.group, items: []}; byGroup.push(group); }
    group.items.push(one);
  }
  for (const group of byGroup) {
    const box = make("details", "settings-group");
    box.open = Boolean(looking) || onlyChanged || group.items.some((one) => one.changed);
    const about = settingsGroups.find((item) => item.name === group.name)?.about || "";
    const heading = make("summary", "");
    heading.append(make("strong", "", group.name), make("span", "settings-count", ` ${group.items.length}`));
    box.append(heading);
    if (about) box.append(make("p", "field-help", about));
    for (const one of group.items) box.append(settingRow(one));
    list.append(box);
  }
}

function settingRow(one) {
  const row = make("div", `setting${one.changed ? " changed" : ""}`);
  row.dataset.setting = one.key;
  const head = make("div", "setting-head");
  head.append(make("strong", "", one.label), make("code", "setting-key", one.key));
  if (one.changed) head.append(make("span", "setting-mark", "changed"));
  if (one.needs_your_own_file) head.append(make("span", "setting-mark yours", "your own file"));
  row.append(head);
  if (one.means) row.append(make("p", "", one.means));
  row.append(make("p", "field-help", `Now: ${short(one.value)} - from ${one.came_from}. It shipped as ${short(one.shipped)}.`));

  const line = make("div", "setting-line");
  let input;
  if (one.kind === "yes or no") {
    input = make("select", "");
    for (const choice of ["yes", "no"]) {
      const option = make("option", "", choice);
      option.value = choice;
      input.append(option);
    }
    input.value = one.value ? "yes" : "no";
  } else if (one.kind === "list" || one.kind === "settings of its own") {
    input = make("textarea", "");
    input.rows = 2;
    input.value = asText(one);
  } else {
    input = make("input", "");
    input.type = one.kind === "number" ? "number" : "text";
    input.step = "any";
    input.value = one.value === null || one.value === undefined ? "" : String(one.value);
  }
  input.id = `setting-${one.key}`;
  input.dataset.for = one.key;
  const label = make("label", "sr-only", `${one.label}, ${one.key}`);
  label.htmlFor = input.id;
  const save = make("button", "setting-save", "Save");
  save.type = "button";
  save.addEventListener("click", () => changeSetting(one.key, input));
  const back = make("button", "setting-reset", "Put it back");
  back.type = "button";
  back.disabled = !one.changed;
  back.addEventListener("click", () => resetSetting(one.key));
  line.append(label, input, save, back);
  row.append(line);
  const said = make("p", "setting-said");
  said.id = `settingSaid-${one.key}`;
  row.append(said);
  return row;
}

function asText(one) {
  if (one.kind === "list" && Array.isArray(one.value)) {
    // A command is a program and its arguments. Shown as somebody would type
    // it, one per line, rather than as the nest of lists it is kept as.
    if (one.key.endsWith("_commands")) {
      return one.value.map((command) => (Array.isArray(command) ? command.join(" ") : String(command))).join("\n");
    }
    return one.value.join(", ");
  }
  return JSON.stringify(one.value);
}

function short(value) {
  const said = typeof value === "string" ? value : JSON.stringify(value);
  if (said === undefined || said === null) return "nothing";
  return said.length > 80 ? `${said.slice(0, 77)}...` : said || "nothing";
}

async function changeSetting(key, input) {
  const said = $(`settingSaid-${key}`);
  said.className = "setting-said";
  said.textContent = "Saving.";
  try {
    const done = await request("/api/settings/change", {
      method: "POST", body: JSON.stringify({key, value: input.value}),
    });
    said.className = "setting-said good";
    said.textContent = done.note;
    announce(done.note);
    await refreshSettings();
    refreshCheckup();
  } catch (error) {
    said.className = "setting-said wrong";
    said.textContent = error.message;
    announce(error.message, true);
  }
}

async function resetSetting(key) {
  const said = $(`settingSaid-${key}`);
  said.className = "setting-said";
  said.textContent = "Putting it back.";
  try {
    const done = await request("/api/settings/reset", {
      method: "POST", body: JSON.stringify({key}),
    });
    said.className = "setting-said good";
    said.textContent = done.note;
    announce(done.note);
    await refreshSettings();
    refreshCheckup();
  } catch (error) {
    said.className = "setting-said wrong";
    said.textContent = error.message;
  }
}

/* ---- Pipelines ----

   Many jobs wired together, with gates between them. A pipeline is nodes and
   arrows: an arrow means "after", and a gate looks at what came before it and
   decides whether the work goes on.

   The picture is drawn from what the harness says a pipeline may hold, so a
   kind of step added to the engine turns up here without this file changing.
*/

let pipeline = {name: "First pipeline", nodes: [], edges: []};
let pipelineKinds = [];
let pipelineSaved = [];
let pipelineSavedProblems = [];
let pipelineStarters = [];
let pipelineWhens = [];   // when a step runs: always, only on failure, either way
let pipelineWaits = [];   // how long it waits before trying again
// Which saved pipeline is on the board, if it came from one. Older versions
// belong to a saved name, so without this there is nothing to look back at.
let pipelineSavedName = "";
// Live state and every control bind to the immutable server run identity.
let pipelineActiveRunName = "";
let pipelineActiveRunId = "";
let pipelineProjectionRunId = "";
let pipelineExactRun = null;
let pipelineNewestRefresh = 0;
let pipelineJoining = "";      // the node an arrow is being drawn from
let pipelineDragging = null;
let pipelineEditing = "";
let pipelineStates = new Map();
const PIPELINE_ZOOM_MIN = 0.35;
const PIPELINE_ZOOM_MAX = 1.8;
let pipelineZoom = 1;
let pipelineIsFullScreen = false;
let pipelineFullScreenHomes = [];
const PIPELINE_PENDING_KEY_PREFIX = "nexus.pipeline.pending.v2:";
const AGENT_RUN_PANEL_KEY = "nexus.pipeline.agent-instructions-open.v1";
const AUTHORITY_REPAIR_SUCCESS_KEY = "nexus.authority-repair-success.v1";
let pipelineAuthorityId = "";
let pipelinePendingRequest = null;
let pipelineCannotRun = "";
let currentAuthorityRepair = null;
let pipelineBaselineReady = false;
let pipelineBaselineSnapshot = "";

function canonicalPipelineValue(value) {
  if (Array.isArray(value)) return value.map(canonicalPipelineValue);
  if (!value || typeof value !== "object") return value;
  return Object.fromEntries(Object.keys(value).sort().map(
    (key) => [key, canonicalPipelineValue(value[key])]
  ));
}

function currentPipelineSnapshot() {
  return JSON.stringify(canonicalPipelineValue(pipelineOnScreen()));
}

function rememberPipelineBaseline() {
  pipelineBaselineReady = true;
  pipelineBaselineSnapshot = currentPipelineSnapshot();
  renderPipelineDirtyState();
}

function markPipelineDrawingUnsaved() {
  pipelineBaselineReady = true;
  // Canonical snapshots are JSON objects, so this marker cannot equal one.
  pipelineBaselineSnapshot = "unsaved-drawing";
  renderPipelineDirtyState();
}

function pipelineHasUnsavedChanges() {
  return pipelineBaselineReady && currentPipelineSnapshot() !== pipelineBaselineSnapshot;
}

function renderPipelineDirtyState() {
  const status = $("pipelineDirtyState");
  if (!status) return;
  const dirty = pipelineHasUnsavedChanges();
  status.classList.toggle("is-dirty", dirty);
  status.textContent = dirty ? "Unsaved changes" : "All changes saved";
}

function askHowToReplaceUnsavedPipeline(action) {
  if (!pipelineHasUnsavedChanges()) return Promise.resolve("discard");
  return new Promise((finish) => {
    const dialog = $("pipelineUnsavedDialog");
    $("pipelineUnsavedWhy").textContent =
      `You have unsaved changes. Save them before ${action}? Cancel keeps this exact drawing open.`;
    $("pipelineUnsavedSaid").hidden = true;
    $("pipelineUnsavedSaid").textContent = "";
    dialog.returnValue = "cancel";
    const done = () => {
      dialog.removeEventListener("close", done);
      $("pipelineUnsavedSave").onclick = null;
      finish(dialog.returnValue || "cancel");
    };
    dialog.addEventListener("close", done);
    $("pipelineUnsavedSave").onclick = async () => {
      const button = $("pipelineUnsavedSave");
      button.disabled = true;
      const saved = await savePipeline();
      button.disabled = false;
      if (saved) dialog.close("save");
      else {
        $("pipelineUnsavedSaid").textContent =
          "Nexus could not save this automation. The drawing is still intact; fix the error or cancel.";
        $("pipelineUnsavedSaid").hidden = false;
        $("pipelineUnsavedSaid").focus();
      }
    };
    dialog.showModal();
  });
}

function askForLongPageText(title, question, value = "") {
  return new Promise((finish) => {
    const box = $("longTextDialog");
    $("longTextDialogTitle").textContent = title;
    $("longTextDialogWhy").textContent = question || "";
    const input = $("longTextDialogInput");
    input.value = value == null ? "" : String(value);
    renderDisclosedTextCount(
      "longTextDialogInput", "longTextDialogCount",
      SHARED_PAGE_CHARACTER_LIMIT, "the page entry");
    const count = () => renderDisclosedTextCount(
      "longTextDialogInput", "longTextDialogCount",
      SHARED_PAGE_CHARACTER_LIMIT, "the page entry");
    input.addEventListener("input", count);
    const done = () => {
      box.removeEventListener("close", done);
      input.removeEventListener("input", count);
      if (box.returnValue !== "ok") { finish(null); return; }
      const problem = disclosedTextProblem(
        "longTextDialogInput", "longTextDialogCount",
        SHARED_PAGE_CHARACTER_LIMIT, "the page entry");
      if (problem) {
        // A method=dialog form closes before validation. Reopen it with every
        // character still present; the backend will make the same decision.
        box.showModal();
        input.addEventListener("input", count);
        box.addEventListener("close", done);
        input.focus();
        return;
      }
      finish(input.value);
    };
    box.addEventListener("close", done);
    box.showModal();
    input.focus();
  });
}

async function mayReplacePipeline(action) {
  const choice = await askHowToReplaceUnsavedPipeline(action);
  return choice === "save" || choice === "discard";
}

function applyAgentRunPanelPreference() {
  try { $("agentRunPanel").open = window.localStorage.getItem(AGENT_RUN_PANEL_KEY) === "yes"; }
  catch (_error) { $("agentRunPanel").open = false; }
}

function rememberAgentRunPanelPreference() {
  try { window.localStorage.setItem(AGENT_RUN_PANEL_KEY, $("agentRunPanel").open ? "yes" : "no"); }
  catch (_error) { /* The native disclosure still works when storage is unavailable. */ }
}

function showProjectAuthorityPause(authority, cannotRun) {
  const notice = $("authorityRepairNotice");
  const reason = String(cannotRun || authority?.reason || "");
  if (!notice) return;
  notice.hidden = !reason;
  $("authorityRepairReason").textContent = reason;
  currentAuthorityRepair = authority?.repairable ? authority : null;
  $("authorityRepairButton").hidden = !currentAuthorityRepair;
  if (!reason) $("authorityRepairSaid").textContent = "";
}

function showAuthorityRepairSuccess(words) {
  const notice = $("authorityRepairNotice");
  const message = String(words || "This folder is registered as a new local project. Project execution is ready.");
  if (!notice) return;
  currentAuthorityRepair = null;
  notice.hidden = false;
  $("authorityRepairButton").hidden = true;
  $("authorityRepairReason").textContent = message;
  $("authorityRepairSaid").textContent = "You can start the project work again now.";
  notice.tabIndex = -1;
  notice.focus();
  announce(message);
}

function restoreAuthorityRepairSuccess() {
  try {
    const words = window.sessionStorage.getItem(AUTHORITY_REPAIR_SUCCESS_KEY);
    if (!words) return;
    window.sessionStorage.removeItem(AUTHORITY_REPAIR_SUCCESS_KEY);
    showAuthorityRepairSuccess(words);
  } catch (_error) { /* Repair still succeeded when browser storage is unavailable. */ }
}

async function useFolderAsNewLocalProject() {
  const authority = currentAuthorityRepair;
  if (!authority) return;
  const accepted = window.confirm(
    "Use this folder as a new local project?\n\n"
    + "Nexus will replace only this folder’s ignored local authority descriptor. "
    + "The original project keeps its identity. Board and automation files are not deleted."
  );
  if (!accepted) return;
  const button = $("authorityRepairButton");
  button.disabled = true;
  $("authorityRepairSaid").textContent = "Registering this folder locally…";
  try {
    const said = await request("/api/projects/use-as-new-local", {
      method: "POST",
      body: JSON.stringify({
        confirmation: "USE THIS FOLDER AS A NEW LOCAL PROJECT",
        fingerprint: authority.fingerprint,
      }),
    });
    const note = said.note || "This folder is registered as a new local project.";
    $("authorityRepairSaid").textContent = note;
    try {
      window.sessionStorage.setItem(AUTHORITY_REPAIR_SUCCESS_KEY, note);
      window.location.reload();
    } catch (_storageError) {
      await refreshCheckup();
      await refreshChecks();
      showAuthorityRepairSuccess(note);
    }
  } catch (error) {
    $("authorityRepairSaid").textContent = error.message;
    button.disabled = false;
  }
}

function pipelinePendingKey(authorityId = pipelineAuthorityId) {
  return `${PIPELINE_PENDING_KEY_PREFIX}${authorityId}`;
}

function readPipelinePendingRequest(authorityId) {
  if (!authorityId) return null;
  try {
    const value = JSON.parse(localStorage.getItem(pipelinePendingKey(authorityId)) || "null");
    return value && value.request_id && value.project_authority_id === authorityId ? value : null;
  } catch (_) { return null; }
}

function usePipelineAuthority(authorityId) {
  const next = String(authorityId || "");
  if (!next) throw new Error("The server did not identify this project's automation authority.");
  if (pipelineAuthorityId === next) return;
  pipelineAuthorityId = next;
  pipelinePendingRequest = readPipelinePendingRequest(next);
  // Version 1 was not authority-scoped and can never be safely adopted.
  try { localStorage.removeItem("nexus.pipeline.pending.v1"); } catch (_) { /* optional cleanup */ }
}

function rememberPipelinePendingRequest(value) {
  if (value && value.project_authority_id !== pipelineAuthorityId) {
    throw new Error("An automation request cannot move between project authorities.");
  }
  pipelinePendingRequest = value;
  try {
    if (value) localStorage.setItem(pipelinePendingKey(), JSON.stringify(value));
    else if (pipelineAuthorityId) localStorage.removeItem(pipelinePendingKey());
  } catch (_) { /* Private browsing can deny storage; in-memory recovery remains. */ }
}

function newPipelineRequestId() {
  return globalThis.crypto?.randomUUID?.()
    || `pipeline-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function pipelineRequestFor(mode, name) {
  const pending = pipelinePendingRequest;
  if (pending && !pending.terminal && pending.mode === mode && pending.name === name) return pending;
  if (!pipelineAuthorityId) throw new Error("Wait for this project's automation authority to load.");
  const made = {request_id: newPipelineRequestId(), run_id: "", mode, name,
    project_authority_id: pipelineAuthorityId,
    created_at: new Date().toISOString()};
  rememberPipelinePendingRequest(made);
  return made;
}

function pipelineRequestWasDefinitelyRejected(error) {
  const status = Number(error?.status);
  return Boolean(error?.responseReceived && status >= 400 && status < 500
    && ![408, 409, 425, 429].includes(status));
}

function pipelineRunIsTerminal(run) {
  return Boolean(run && !run.running
    && ["passed", "warning", "failed", "incomplete", "cancelled", "timed_out", "interrupted"]
      .includes(String(run.state || "").toLowerCase()));
}

function showPipelineActiveRun(run, pending = pipelinePendingRequest) {
  const box = $("pipelineActiveRun");
  if (!box) return;
  const runId = String(run?.run_id || pending?.run_id || "");
  const requestId = String(run?.request_id || pending?.request_id || "");
  const name = String(run?.name || pending?.name || "Automation");
  const state = String(run?.state || (runId ? "accepted" : "awaiting acknowledgement"));
  box.hidden = !(runId || requestId);
  if (box.hidden) return;
  $("pipelineActiveRunSaid").textContent = `${name}: ${state}.`;
  $("pipelineActiveRunIdentity").textContent = `${runId ? `Run ${runId}` : "Run identity not acknowledged yet"}${requestId ? ` · Request ${requestId}` : ""}`;
  $("pipelineOpenActiveRun").disabled = !runId;
  $("pipelineStopActive").disabled = !runId || !run?.running;
}

async function fetchExactPipelineRun(runId) {
  if (!runId) return null;
  const run = await request(`/api/pipeline-runs/${encodeURIComponent(runId)}`);
  if (String(run.run_id || "") !== String(runId)) throw new Error("The server returned a different automation run.");
  if (String(run.project_authority_id || "") !== pipelineAuthorityId) {
    throw new Error("That automation run belongs to a different project authority.");
  }
  pipelineExactRun = run;
  pipelineActiveRunId = run.running ? String(run.run_id) : "";
  pipelineActiveRunName = run.running ? String(run.name || "") : "";
  showPipelineActiveRun(run);
  if (pipelineRunIsTerminal(run) && pipelinePendingRequest?.run_id === String(run.run_id)) {
    rememberPipelinePendingRequest(null);
  }
  return run;
}

async function lookupPipelineRunByRequest() {
  const pending = pipelinePendingRequest;
  if (!pending) return null;
  if (pending.project_authority_id !== pipelineAuthorityId) {
    throw new Error("The pending automation request belongs to a different project authority.");
  }
  const found = await request(`/api/pipeline-runs/by-request?request_id=${encodeURIComponent(pending.request_id)}`);
  if (String(found.request_id || "") !== pending.request_id
      || String(found.project_authority_id || "") !== pipelineAuthorityId) {
    throw new Error("The request lookup returned a different project or request identity.");
  }
  const runId = String(found.run_id || "");
  if (!runId) throw new Error("The request lookup did not return an exact run identity.");
  rememberPipelinePendingRequest({...pending, run_id: runId});
  return fetchExactPipelineRun(runId);
}

async function openExactPipelineRun() {
  const runId = String(pipelineExactRun?.run_id || pipelinePendingRequest?.run_id || pipelineActiveRunId || "");
  const run = await fetchExactPipelineRun(runId);
  if (!run) return;
  const snapshot = run.definition || run.snapshot || run.frozen_definition;
  if (!snapshot) {
    say(`Run ${runId} is ${run.state || "known"}, but this server did not provide its immutable automation snapshot.`);
    return;
  }
  if (!await mayReplacePipeline("opening this immutable run snapshot")) return;
  pipeline = structuredClone(snapshot);
  pipelineSavedName = "";
  pipelineProjectionRunId = runId;
  pipelineStates = new Map();
  $("pipelineLog").replaceChildren();
  $("pipelineName").value = pipeline.name || run.name || "Automation run";
  markPipelineDrawingUnsaved();
  renderPipeline();
  if (run.result) showPipelineRun(run.result);
  say(`Showing immutable snapshot for run ${runId}. Saving creates or updates a separate automation; it cannot change this run.`);
}

function sizeThePipelineCanvas() {
  const canvas = $("pipelineCanvas");
  const surface = $("pipelineSurface");
  const nodes = $("pipelineNodes");
  let width = Math.max(1200, canvas.clientWidth / pipelineZoom);
  let height = Math.max(600, canvas.clientHeight / pipelineZoom);
  for (const one of nodes.querySelectorAll(".pipeline-node")) {
    width = Math.max(width, one.offsetLeft + one.offsetWidth + 80);
    height = Math.max(height, one.offsetTop + one.offsetHeight + 80);
  }
  nodes.style.width = `${Math.ceil(width)}px`;
  nodes.style.height = `${Math.ceil(height)}px`;
  nodes.style.transform = `scale(${pipelineZoom})`;
  surface.style.width = `${Math.ceil(width * pipelineZoom)}px`;
  surface.style.height = `${Math.ceil(height * pipelineZoom)}px`;
}

function sayThePipelineZoom() {
  $("pipelineZoomValue").textContent = `${Math.round(pipelineZoom * 100)}%`;
  $("pipelineZoomOut").disabled = pipelineZoom <= PIPELINE_ZOOM_MIN;
  $("pipelineZoomIn").disabled = pipelineZoom >= PIPELINE_ZOOM_MAX;
}

function setThePipelineZoom(wanted, keepTheMiddle = true) {
  const canvas = $("pipelineCanvas");
  const middle = {
    x: (canvas.scrollLeft + canvas.clientWidth / 2) / pipelineZoom,
    y: (canvas.scrollTop + canvas.clientHeight / 2) / pipelineZoom,
  };
  pipelineZoom = Math.max(PIPELINE_ZOOM_MIN, Math.min(PIPELINE_ZOOM_MAX, wanted));
  sizeThePipelineCanvas();
  if (keepTheMiddle) {
    canvas.scrollLeft = middle.x * pipelineZoom - canvas.clientWidth / 2;
    canvas.scrollTop = middle.y * pipelineZoom - canvas.clientHeight / 2;
  }
  sayThePipelineZoom();
}

function fitTheWholePipeline() {
  const canvas = $("pipelineCanvas");
  const things = [...$("pipelineNodes").querySelectorAll(".pipeline-node")];
  if (!things.length) { setThePipelineZoom(1, false); return; }
  const left = Math.min(...things.map((one) => one.offsetLeft));
  const top = Math.min(...things.map((one) => one.offsetTop));
  const right = Math.max(...things.map((one) => one.offsetLeft + one.offsetWidth));
  const bottom = Math.max(...things.map((one) => one.offsetTop + one.offsetHeight));
  const wanted = Math.min((canvas.clientWidth - 40) / Math.max(1, right - left),
    (canvas.clientHeight - 40) / Math.max(1, bottom - top));
  setThePipelineZoom(wanted, false);
  canvas.scrollLeft = Math.max(0, left * pipelineZoom - 20);
  canvas.scrollTop = Math.max(0, top * pipelineZoom - 20);
}

function putTheFlowStepsInFullScreen() {
  for (const home of pipelineFullScreenHomes) $("pipelineFocusSide").append(home.element);
}

function putTheFlowStepsBack() {
  for (const home of pipelineFullScreenHomes) home.parent.insertBefore(home.element, home.next);
}

function showHowThePipelineFillsTheScreen(full) {
  pipelineIsFullScreen = Boolean(full);
  $("pipelineStage").classList.toggle("is-fullscreen", pipelineIsFullScreen);
  document.body.classList.toggle("workspace-is-fullscreen", pipelineIsFullScreen);
  $("pipelineFullScreen").textContent = pipelineIsFullScreen ? "Exit full screen" : "Full screen";
  $("pipelineFullScreen").setAttribute("aria-pressed", String(pipelineIsFullScreen));
  if (pipelineIsFullScreen) putTheFlowStepsInFullScreen();
  else putTheFlowStepsBack();
  sizeThePipelineCanvas();
}

async function toggleThePipelineFullScreen() {
  const wanted = !pipelineIsFullScreen;
  try {
    if (window.harnessDesktop?.setFullScreen) {
      showHowThePipelineFillsTheScreen(wanted);
      const changed = await window.harnessDesktop.setFullScreen(wanted);
      if (changed !== wanted) showHowThePipelineFillsTheScreen(changed);
    } else if (document.fullscreenElement === $("pipelineStage")) {
      await document.exitFullscreen();
    } else {
      putTheFlowStepsInFullScreen();
      await $("pipelineStage").requestFullscreen();
    }
  } catch (error) {
    showHowThePipelineFillsTheScreen(false);
    say(`Full screen could not be opened: ${error.message || error}`);
  }
}

async function refreshPipelines(name, options = {}) {
  const mine = ++pipelineNewestRefresh;
  let replaceDrawing = options.replaceDrawing !== false;
  let replacementSkipped = false;
  const requestedName = name === undefined ? pipelineSavedName : name;
  const previousName = pipelineSavedName;
  try {
    const said = await request(`/api/pipelines?recover_missing=1${requestedName ? `&name=${encodeURIComponent(requestedName)}` : ""}`);
    if (mine !== pipelineNewestRefresh) return;
    if (replaceDrawing && options.expectedSnapshot
        && currentPipelineSnapshot() !== options.expectedSnapshot) {
      // A slow library read must not win over an edit made while it was in
      // flight. Keep the exact current drawing and only refresh inventory.
      replaceDrawing = false;
      replacementSkipped = true;
    }
    pipelineCannotRun = String(said.cannot_run || "");
    showProjectAuthorityPause(said.authority, pipelineCannotRun);
    if (said.project_authority_id) usePipelineAuthority(said.project_authority_id);
    else {
      pipelineAuthorityId = "";
      pipelinePendingRequest = null;
    }
    pipelineKinds = said.kinds || [];
    pipelineSaved = said.saved || [];
    pipelineSavedProblems = said.saved_problems || [];
    pipelineStarters = said.starters || [];
    pipelineWhens = said.when_it_runs || [];
    pipelineWaits = said.waits || [];
    const receivedPipeline = said.pipeline;
    const resolvedName = pipelineSaved.length ? (
      (pipelineSaved.includes(said.selected_name) ? said.selected_name : "")
      || (pipelineSaved.includes(requestedName) ? requestedName : "")
      || (pipelineSaved.includes(receivedPipeline?.name) ? receivedPipeline.name : "")
    ) : "";
    const priorAgentChoice = $("agentRunAutomation").value;
    fillOneChoice("agentRunAutomation", pipelineSaved.map((one) => ({name: one, label: one})),
                  "name", resolvedName || priorAgentChoice || pipelineSaved[0] || "");
    const activeRunId = String(said.active_run?.run_id || "");
    const activeRunName = String(said.active_run?.name || "");
    const hadPendingRequest = Boolean(pipelinePendingRequest);
    let reconciledRun = null;
    let reconciliationError = null;
    if (hadPendingRequest) {
      try { reconciledRun = await lookupPipelineRunByRequest(); }
      catch (error) { reconciliationError = error; }
    }
    const exactRunId = String(
      (reconciledRun?.running ? reconciledRun.run_id : "")
      || (!hadPendingRequest ? activeRunId : "")
    );
    const projectionRunId = String(reconciledRun?.run_id || exactRunId);
    const preservingLiveProjection = Boolean(projectionRunId
      && pipelineProjectionRunId === projectionRunId && previousName === resolvedName);
    if (replaceDrawing) {
      pipeline = receivedPipeline;
      if (!preservingLiveProjection) pipelineStates = new Map();
      pipelineSavedName = resolvedName;
    } else if (pipelineSavedName && !pipelineSaved.includes(pipelineSavedName)) {
      // The file disappeared outside the editor. Keep the live drawing and
      // treat it as unsaved so the next replacement is guarded.
      pipelineSavedName = "";
      markPipelineDrawingUnsaved();
    }
    pipelineActiveRunId = exactRunId;
    pipelineActiveRunName = reconciledRun?.running
      ? String(reconciledRun.name || pipelinePendingRequest?.name || "")
      : (!hadPendingRequest ? activeRunName : "");
    if (replaceDrawing || requestedName === pipelineSavedName) {
      pipelineOlderOnes = said.older_ones || [];
    }
    if (replaceDrawing) $("pipelineName").value = pipeline.name || "";
    $("pipelineStop").disabled = !exactRunId;
    renderPipelinePalette();
    renderPipelineStarters();
    renderPipelineSaved();
    renderPipeline();
    if (replaceDrawing) {
      // A server-provided starter is a clean drawing, but it is not a saved
      // automation.  Calling it "All changes saved" would hide the first
      // possible loss.  Only a definition actually present in the library is
      // allowed to establish a saved baseline.
      if (pipelineSavedName && pipelineSaved.includes(pipelineSavedName)) {
        rememberPipelineBaseline();
      } else {
        markPipelineDrawingUnsaved();
      }
    }
    if (!$("agentRunAutomation").dataset.bound) {
      $("agentRunAutomation").dataset.bound = "true";
      $("agentRunAutomation").addEventListener("change", refreshAgentContract);
      $("agentRunCopyContract").addEventListener("click", async () => { const said = await refreshAgentContract(); if (said) await navigator.clipboard.writeText(JSON.stringify(said, null, 2)); });
      $("agentRunNow").addEventListener("click", runAgentAutomation);
      $("pipelineStopActive").addEventListener("click", stopPipeline);
      $("pipelineOpenActiveRun").addEventListener("click", openExactPipelineRun);
    }
    refreshAgentContract();
    if (!reconciledRun && exactRunId) {
      try { reconciledRun = await fetchExactPipelineRun(exactRunId); }
      catch (error) {
        showPipelineActiveRun(null);
        say(`Run ${exactRunId} is still remembered, but its current status could not be loaded: ${error.message}`);
      }
    } else if (!reconciledRun) showPipelineActiveRun(null, pipelinePendingRequest);
    if (reconciliationError) {
      showPipelineActiveRun(null, pipelinePendingRequest);
      say(`This project's pending request is still remembered, but its exact run is not available yet: ${reconciliationError.message}`);
    }
    else if (reconciledRun && pipelineRunIsTerminal(reconciledRun)) {
      say(`${reconciledRun.name || "Automation"} finished with ${reconciledRun.state}. Open this run to view its immutable automation and results; the current editor is unchanged.`);
    }
    else if (pipelineActiveRunId && pipelineProjectionRunId !== pipelineActiveRunId) {
      say("An exact automation run is active. Open its immutable snapshot to project its step updates on this board.");
    }
    if (replacementSkipped) {
      say("The automation library finished loading, but the drawing changed meanwhile, so Nexus kept your newer edits. Choose the saved automation again when you are ready.");
    }
  } catch (error) {
    if (mine !== pipelineNewestRefresh) return;
    showError(error.message);
  }
}

async function refreshAgentContract() {
  const name = $("agentRunAutomation").value;
  if (!name) { $("agentRunContract").textContent = "Save an automation first."; return null; }
  try {
    const said = await request(`/api/pipelines/agent-contract?name=${encodeURIComponent(name)}`);
    $("agentRunContract").textContent = JSON.stringify(said, null, 2);
    return said;
  } catch (error) { $("agentRunContract").textContent = error.message; return null; }
}

async function runAgentAutomation() {
  if (pipelineCannotRun) {
    $("agentRunSaid").textContent = `Automation is paused: ${pipelineCannotRun}`;
    return;
  }
  const name = $("agentRunAutomation").value;
  if (!name) { $("agentRunSaid").textContent = "Choose a saved automation first."; return; }
  const pending = pipelineRequestFor("agent", name);
  try {
    const accepted = await request("/api/pipelines/agent-run", {method: "POST", body: JSON.stringify({
      automation: name, request_id: pending.request_id,
    })});
    pipelineActiveRunName = accepted.name || name;
    pipelineActiveRunId = accepted.run_id || "";
    pipelineProjectionRunId = "";
    rememberPipelinePendingRequest({...pending, run_id: pipelineActiveRunId});
    $("agentRunSaid").textContent = `Started exactly “${name}”. Watch the run status above for completion.`;
    await refreshPipelines(name, {replaceDrawing: false});
  } catch (error) {
    if (pipelineRequestWasDefinitelyRejected(error)) rememberPipelinePendingRequest(null);
    $("agentRunSaid").textContent = pipelineRequestWasDefinitelyRejected(error)
      ? error.message
      : `${error.message} The outcome is unknown; Retry reuses request ${pending.request_id} and cannot start a duplicate.`;
    showPipelineActiveRun(null, pipelinePendingRequest);
  }
}

function renderPipelineSaved() {
  const list = $("pipelineList");
  list.replaceChildren();
  $("pipelineSavedCount").textContent = pipelineSaved.length
    ? `${pipelineSaved.length} saved automation${pipelineSaved.length === 1 ? "" : "s"} in ${nexusProjectName}`
    : `No saved automations in ${nexusProjectName}. Automations are kept with each project.`;
  const problems = $("pipelineSavedProblems");
  problems.hidden = !pipelineSavedProblems.length;
  problems.textContent = pipelineSavedProblems.length
    ? `${pipelineSavedProblems.length} automation library notice${pipelineSavedProblems.length === 1 ? "" : "s"}: ${pipelineSavedProblems.join("; ")}`
    : "";
  const hasSavedSelection = Boolean(
    pipelineSavedName && pipelineSaved.includes(pipelineSavedName)
  );
  $("pipelineExport").disabled = !hasSavedSelection;
  $("pipelineDelete").disabled = !hasSavedSelection;
  $("pipelineRun").disabled = Boolean(pipelineCannotRun);
  $("pipelineRun").title = pipelineCannotRun;
  $("agentRunCopyContract").disabled = !pipelineSaved.length;
  $("agentRunNow").disabled = Boolean(pipelineCannotRun) || !pipelineSaved.length;
  $("agentRunNow").title = pipelineCannotRun;
  if (!pipelineSaved.length) {
    list.append(make("li", "hint", "None saved yet. Draw one and press Save."));
    return;
  }
  for (const name of pipelineSaved) {
    const item = make("li", "");
    const button = make("button", `pipeline-saved-one${name === pipelineSavedName ? " chosen" : ""}`, name);
    button.type = "button";
    button.addEventListener("click", () => openSavedPipeline(name));
    item.append(button);
    list.append(item);
  }
}

async function openSavedPipeline(name) {
  if (!await mayReplacePipeline(`opening “${name}”`)) return;
  await refreshPipelines(name, {expectedSnapshot: currentPipelineSnapshot()});
}

// Ready-made pipelines. A blank board is the hardest thing to hand somebody
// who has not done this before, so these are the shapes people actually want,
// each made of steps anybody could have dragged out themselves.
function renderPipelineStarters() {
  const list = $("pipelineStarters");
  list.replaceChildren();
  const shown = pipelineStartersShown();
  if (!shown.length) {
    list.append(make("li", "hint", `Nothing matches "${pipelineStarterLooking}".`));
    return;
  }
  let group = "";
  for (const starter of shown) {
    if (starter.group && starter.group !== group) {
      group = starter.group;
      list.append(make("li", "pipeline-starter-group", group));
    }
    const item = make("li", "");
    const button = make("button", "pipeline-starter", starter.title);
    button.type = "button";
    button.title = `${starter.when} ${starter.steps} steps.`;
    button.dataset.starter = starter.key;
    button.addEventListener("click", () => usePipelineStarter(starter));
    item.append(button, make("p", "field-help", starter.when));
    list.append(item);
  }
}

async function usePipelineStarter(starter) {
  if (!await mayReplacePipeline(`starting from “${starter.title}”`)) return;
  const beforeRequest = currentPipelineSnapshot();
  try {
    const said = await request("/api/pipelines/starter", {
      method: "POST", body: JSON.stringify({key: starter.key}),
    });
    if (currentPipelineSnapshot() !== beforeRequest) {
      say("The starter loaded, but the drawing changed meanwhile, so Nexus kept your newer edits. Choose the starter again when you are ready.");
      return;
    }
    pipeline = said.pipeline;
    pipelineStates = new Map();
    $("pipelineName").value = pipeline.name;
    $("pipelineLog").replaceChildren();
    pipelineSavedName = "";
    markPipelineDrawingUnsaved();
    renderPipeline();
    say(`${starter.title} is on the board. Press Run, or change it first.`);
  } catch (error) { showError(error.message); say(error.message); }
}

function renderPipelinePalette() {
  const box = $("pipelinePalette");
  box.replaceChildren();
  const groups = [];
  for (const kind of pipelineKinds) {
    let group = groups.find((item) => item.name === kind.group);
    if (!group) { group = {name: kind.group, kinds: []}; groups.push(group); }
    group.kinds.push(kind);
  }
  for (const group of groups) {
    box.append(make("h4", "pipeline-group", group.name));
    for (const kind of group.kinds) {
      const button = make("button", `pipeline-add colour-${kind.colour}`, kind.label);
      button.type = "button";
      button.title = kind.summary;
      button.dataset.kind = kind.id;
      button.addEventListener("click", () => addPipelineNode(kind.id));
      box.append(button);
    }
  }
}

function kindOf(id) {
  return pipelineKinds.find((kind) => kind.id === id) || {label: id, colour: "grey", settings: []};
}

function addPipelineNode(kindId) {
  const kind = kindOf(kindId);
  let number = pipeline.nodes.length + 1;
  while (pipeline.nodes.some((node) => node.id === `${kindId}-${number}`)) number += 1;
  const spot = pipeline.nodes.length;
  pipeline.nodes.push({
    id: `${kindId}-${number}`,
    kind: kindId,
    label: kind.label,
    settings: {},
    at: {x: 40 + (spot % 4) * 250, y: 40 + Math.floor(spot / 4) * 150},
  });
  renderPipeline();
  say(`Added ${kind.label}. Press Connect on one box, then another, to join them.`);
}

function say(words) {
  $("pipelineSaid").textContent = words;
  announce(words);
}

function renderPipeline() {
  const focused = document.activeElement?.closest?.(".pipeline-node");
  const focusNodeId = focused?.dataset.node || "";
  const focusAction = document.activeElement?.dataset.pipelineAction || "card";
  const box = $("pipelineNodes");
  const wires = $("pipelineWires");
  box.replaceChildren(wires);
  for (const node of pipeline.nodes) {
    const kind = kindOf(node.kind);
    const card = make("div", `pipeline-node colour-${kind.colour}`);
    card.dataset.node = node.id;
    card.dataset.pipelineAction = "card";
    card.style.left = `${node.at?.x || 0}px`;
    card.style.top = `${node.at?.y || 0}px`;
    const state = pipelineStates.get(node.id);
    if (state) card.dataset.state = state.state;

    const head = make("div", "pipeline-node-head");
    head.append(make("strong", "", node.label || kind.label));
    card.append(head);
    if ((node.label || "") !== kind.label) card.append(make("p", "pipeline-node-kind", kind.label));
    if (state) {
      card.append(make("p", "pipeline-node-state", state.said || state.state));
    }
    if (node.settings?.when === "when-something-failed") {
      card.append(make("p", "pipeline-node-when", "Only if something failed"));
    } else if (node.settings?.when === "whatever-happens") {
      card.append(make("p", "pipeline-node-when", "Runs either way"));
    }
    if (node.settings?.longest) {
      card.append(make("p", "pipeline-node-wait",
        `Given ${node.settings.longest} seconds, then stopped.`));
    }
    if (node.settings?.even_if_it_fails) {
      card.append(make("p", "pipeline-node-wait",
        "The rest carries on even if this one fails."));
    }
    if ((node.settings?.wait || "no-wait") !== "no-wait") {
      card.append(make("p", "pipeline-node-wait",
        node.settings.wait === "growing-wait"
          ? "Waits longer after each try" : "Waits a few seconds between tries"));
    }
    if ((node.settings?.asks || []).length) {
      card.append(make("p", "pipeline-node-asks",
        `Asks first: ${node.settings.asks.join(", ")}`));
    }
    const buttons = make("div", "pipeline-node-buttons");
    const join = make("button", "pipeline-node-button", pipelineJoining === node.id ? "Joining" : "Connect");
    join.type = "button";
    join.dataset.pipelineAction = "connect";
    join.title = "Draw an arrow from this step to another";
    join.addEventListener("click", (event) => { event.stopPropagation(); joinPipelineNodes(node.id); });
    const settings = make("button", "pipeline-node-button", "Settings");
    settings.type = "button";
    settings.dataset.pipelineAction = "settings";
    settings.addEventListener("click", (event) => { event.stopPropagation(); openPipelineNode(node.id); });
    const alone = make("button", "pipeline-node-button", "Run only this");
    alone.type = "button";
    alone.dataset.pipelineAction = "run-only";
    alone.title = "Run this one step and nothing else, while you are building it";
    alone.disabled = Boolean(pipelineCannotRun);
    if (pipelineCannotRun) alone.title = pipelineCannotRun;
    alone.addEventListener("click", (event) => {
      event.stopPropagation();
      runPipeline({only: node.id});
    });
    const onward = make("button", "pipeline-node-button", "Carry on from here");
    onward.type = "button";
    onward.dataset.pipelineAction = "run-from";
    onward.title = "Run this step and everything after it, leaving the earlier ones alone";
    onward.disabled = Boolean(pipelineCannotRun);
    if (pipelineCannotRun) onward.title = pipelineCannotRun;
    onward.addEventListener("click", (event) => {
      event.stopPropagation();
      runPipeline({from_here: node.id});
    });
    const remove = make("button", "pipeline-node-button danger", "Remove step");
    remove.type = "button";
    remove.dataset.pipelineAction = "remove";
    remove.addEventListener("click", (event) => { event.stopPropagation(); removePipelineNode(node.id); });
    buttons.append(join, settings, alone, onward, remove);
    card.append(buttons);

    // The board is usable without a pointer. A box takes the keyboard, the
    // arrow keys move it, and the same three things it can do are on keys.
    card.tabIndex = 0;
    card.setAttribute("role", "group");
    card.setAttribute("aria-label",
      `${node.label || kind.label}, ${kind.label}. Arrow keys move it. `
      + "C connects, S for settings, Delete removes it.");
    card.addEventListener("keydown", (event) => pipelineKey(event, node));
    card.addEventListener("pointerdown", (event) => startPipelineDrag(event, node));
    card.addEventListener("click", () => { if (pipelineJoining && pipelineJoining !== node.id) joinPipelineNodes(node.id); });
    box.append(card);
  }
  sizeThePipelineCanvas();
  drawPipelineWires();
  renderPipelineStructure();
  $("pipelineScopePreview").textContent = pipelineSavedName
    ? `Selected saved automation: “${pipelineSavedName}”. A run and its status are shown here only when that exact name matches.`
    : `Unsaved drawing: ${pipeline.nodes.length} step${pipeline.nodes.length === 1 ? "" : "s"} and ${pipeline.edges.length} connection${pipeline.edges.length === 1 ? "" : "s"}. Run uses exactly this drawing.`;
  if (focusNodeId) {
    const restoredCard = box.querySelector(`[data-node="${CSS.escape(focusNodeId)}"]`);
    const restored = focusAction === "card"
      ? restoredCard
      : restoredCard?.querySelector(`[data-pipeline-action="${CSS.escape(focusAction)}"]`);
    if (restored) restored.focus({preventScroll: true});
  }
  renderPipelineDirtyState();
}

function pipelineNodeName(nodeId) {
  const node = pipeline.nodes.find((one) => one.id === nodeId);
  return node?.label || node?.id || nodeId;
}

function removePipelineEdge(edge) {
  const cameFromStructure = Boolean(document.activeElement?.closest?.("#pipelineStructure"));
  pipeline.edges = pipeline.edges.filter(
    (item) => !(item.from === edge.from && item.to === edge.to));
  renderPipeline();
  if (cameFromStructure) {
    $("pipelineStructure").closest("details")?.querySelector("summary")?.focus({preventScroll: true});
  }
  say(`Removed the connection from ${pipelineNodeName(edge.from)} to ${pipelineNodeName(edge.to)}.`);
}

function renderPipelineStructure() {
  const held = $("pipelineStructure");
  held.replaceChildren();
  if (!pipeline.nodes.length) {
    held.append(make("p", "hint", "No steps or connections yet."));
    return;
  }
  const list = make("ol", "semantic-list");
  for (const node of pipeline.nodes) {
    const item = make("li", "semantic-item");
    item.append(make("strong", "", pipelineNodeName(node.id)),
      make("span", "", ` — ${kindOf(node.kind).label}`));
    const outgoing = pipeline.edges.filter((edge) => edge.from === node.id);
    if (outgoing.length) {
      const edges = make("ul", "semantic-edge-list");
      for (const edge of outgoing) {
        const row = make("li", "semantic-edge");
        row.append(make("span", "", `Then ${pipelineNodeName(edge.to)}. `));
        const remove = make("button", "danger compact", "Remove connection");
        remove.type = "button";
        remove.setAttribute("aria-label",
          `Remove connection from ${pipelineNodeName(edge.from)} to ${pipelineNodeName(edge.to)}`);
        remove.addEventListener("click", () => removePipelineEdge(edge));
        row.append(remove);
        edges.append(row);
      }
      item.append(edges);
    } else item.append(make("p", "hint", "No following step."));
    list.append(item);
  }
  held.append(list);
}

function drawPipelineWires() {
  const wires = $("pipelineWires");
  wires.replaceChildren();
  for (const edge of pipeline.edges) {
    const from = pipeline.nodes.find((node) => node.id === edge.from);
    const to = pipeline.nodes.find((node) => node.id === edge.to);
    if (!from || !to) continue;
    const x1 = (from.at?.x || 0) + 200, y1 = (from.at?.y || 0) + 34;
    const x2 = (to.at?.x || 0), y2 = (to.at?.y || 0) + 34;
    const middle = (x1 + x2) / 2;
    const line = document.createElementNS("http://www.w3.org/2000/svg", "path");
    line.setAttribute("d", `M ${x1} ${y1} C ${middle} ${y1}, ${middle} ${y2}, ${x2} ${y2}`);
    line.setAttribute("class", "pipeline-wire");
    wires.append(line);
    // A small cross in the middle of the arrow removes it, the way the old
    // project did it: the thing you want to get rid of is what you press.
    const cut = document.createElementNS("http://www.w3.org/2000/svg", "circle");
    cut.setAttribute("cx", String(middle));
    cut.setAttribute("cy", String((y1 + y2) / 2));
    cut.setAttribute("r", "9");
    cut.setAttribute("class", "pipeline-cut");
    cut.addEventListener("click", () => removePipelineEdge(edge));
    const cross = document.createElementNS("http://www.w3.org/2000/svg", "text");
    cross.setAttribute("x", String(middle));
    cross.setAttribute("y", String((y1 + y2) / 2 + 4));
    cross.setAttribute("class", "pipeline-cut-mark");
    cross.textContent = "x";
    wires.append(cut, cross);
  }
}

function joinPipelineNodes(nodeId) {
  if (!pipelineJoining) {
    pipelineJoining = nodeId;
    renderPipeline();
    say(`Joining from ${nodeId}. Press another box to finish the arrow.`);
    return;
  }
  if (pipelineJoining === nodeId) {
    pipelineJoining = "";
    renderPipeline();
    say("Stopped joining.");
    return;
  }
  const already = pipeline.edges.some((edge) => edge.from === pipelineJoining && edge.to === nodeId);
  if (!already) pipeline.edges.push({from: pipelineJoining, to: nodeId});
  const from = pipelineJoining;
  pipelineJoining = "";
  renderPipeline();
  say(already ? "That arrow is already there." : `Joined ${from} to ${nodeId}.`);
}

function removePipelineNode(nodeId) {
  pipeline.nodes = pipeline.nodes.filter((node) => node.id !== nodeId);
  pipeline.edges = pipeline.edges.filter((edge) => edge.from !== nodeId && edge.to !== nodeId);
  if (pipelineJoining === nodeId) pipelineJoining = "";
  renderPipeline();
  say(`Took ${nodeId} out.`);
}

// The keyboard, on a box. Everything the mouse can do, a key can do.
function pipelineKey(event, node) {
  const step = event.shiftKey ? 40 : 10;
  const moves = {
    ArrowLeft: [-step, 0], ArrowRight: [step, 0],
    ArrowUp: [0, -step], ArrowDown: [0, step],
  };
  if (moves[event.key]) {
    event.preventDefault();
    node.at = {
      x: Math.max(0, (node.at?.x || 0) + moves[event.key][0]),
      y: Math.max(0, (node.at?.y || 0) + moves[event.key][1]),
    };
    renderPipeline();
    keepPipelineFocus(node.id);
    say(`${node.label} is at ${Math.round(node.at.x)}, ${Math.round(node.at.y)}.`);
    return;
  }
  const key = event.key.toLowerCase();
  if (key === "c") { event.preventDefault(); joinPipelineNodes(node.id); keepPipelineFocus(node.id); return; }
  if (key === "s") { event.preventDefault(); openPipelineNode(node.id); return; }
  if (event.key === "Delete" || event.key === "Backspace") {
    event.preventDefault();
    removePipelineNode(node.id);
    const first = $("pipelineNodes").querySelector(".pipeline-node");
    if (first) first.focus();
  }
}

// Redrawing the board builds new boxes, and the keyboard would otherwise be
// left on nothing at all.
function keepPipelineFocus(nodeId) {
  const card = $("pipelineNodes").querySelector(`[data-node="${CSS.escape(nodeId)}"]`);
  if (card) card.focus();
}

function startPipelineDrag(event, node) {
  if (event.target.closest("button")) return;
  const card = event.currentTarget;
  const canvas = $("pipelineCanvas");
  const box = canvas.getBoundingClientRect();
  pipelineDragging = {
    node,
    grabX: (event.clientX - box.left + canvas.scrollLeft) / pipelineZoom - (node.at?.x || 0),
    grabY: (event.clientY - box.top + canvas.scrollTop) / pipelineZoom - (node.at?.y || 0),
  };
  card.setPointerCapture(event.pointerId);
  card.classList.add("moving");
}

function movePipelineDrag(event) {
  if (!pipelineDragging) return;
  const canvas = $("pipelineCanvas");
  const box = canvas.getBoundingClientRect();
  const node = pipelineDragging.node;
  node.at = {
    x: Math.max(0, (event.clientX - box.left + canvas.scrollLeft) / pipelineZoom
      - pipelineDragging.grabX),
    y: Math.max(0, (event.clientY - box.top + canvas.scrollTop) / pipelineZoom
      - pipelineDragging.grabY),
  };
  const card = $("pipelineNodes").querySelector(`[data-node="${CSS.escape(node.id)}"]`);
  if (card) { card.style.left = `${node.at.x}px`; card.style.top = `${node.at.y}px`; }
  drawPipelineWires();
}

function endPipelineDrag() {
  if (!pipelineDragging) return;
  const card = $("pipelineNodes").querySelector(`[data-node="${CSS.escape(pipelineDragging.node.id)}"]`);
  if (card) card.classList.remove("moving");
  pipelineDragging = null;
}

function makePipelineCanvasPannable() {
  const canvas = $("pipelineCanvas");
  let panning = null;
  canvas.addEventListener("pointerdown", (event) => {
    const paper = ["pipelineCanvas", "pipelineSurface", "pipelineNodes"];
    if (event.button !== 0 || !paper.includes(event.target.id)) return;
    panning = {x: event.clientX, y: event.clientY, left: canvas.scrollLeft,
      top: canvas.scrollTop, pointer: event.pointerId};
    canvas.classList.add("panning");
    canvas.setPointerCapture(event.pointerId);
    event.preventDefault();
  });
  canvas.addEventListener("pointermove", (event) => {
    if (!panning || event.pointerId !== panning.pointer) return;
    canvas.scrollLeft = panning.left - (event.clientX - panning.x);
    canvas.scrollTop = panning.top - (event.clientY - panning.y);
  });
  const stop = (event) => {
    if (!panning || event.pointerId !== panning.pointer) return;
    panning = null;
    canvas.classList.remove("panning");
    if (canvas.hasPointerCapture(event.pointerId)) canvas.releasePointerCapture(event.pointerId);
  };
  canvas.addEventListener("pointerup", stop);
  canvas.addEventListener("pointercancel", stop);
  canvas.addEventListener("wheel", (event) => {
    if (!event.ctrlKey) return;
    event.preventDefault();
    setThePipelineZoom(pipelineZoom + (event.deltaY < 0 ? 0.1 : -0.1));
  }, {passive: false});
}

function wireUpPipelineView() {
  pipelineFullScreenHomes = [$("pipelineLibraryControls"), $("pipelinePaletteTitle"), $("pipelinePalette")]
    .map((element) => ({element, parent: element.parentNode, next: element.nextSibling}));
  makePipelineCanvasPannable();
  $("pipelineFullScreen").addEventListener("click", toggleThePipelineFullScreen);
  $("pipelineZoomOut").addEventListener("click", () => setThePipelineZoom(pipelineZoom - 0.1));
  $("pipelineZoomReset").addEventListener("click", () => setThePipelineZoom(1));
  $("pipelineZoomIn").addEventListener("click", () => setThePipelineZoom(pipelineZoom + 0.1));
  $("pipelineFit").addEventListener("click", fitTheWholePipeline);
  document.addEventListener("fullscreenchange", () => {
    if (pipelineIsFullScreen || document.fullscreenElement === $("pipelineStage")) {
      showHowThePipelineFillsTheScreen(document.fullscreenElement === $("pipelineStage"));
    }
  });
  sayThePipelineZoom();
}

/* ---- one step's settings ---- */

const PIPELINE_FIELDS = {
  suite: {label: "Suite file, if not the usual one", placeholder: ".harness/qa/suite.json"},
  tag: {label: "Only checks with this tag", placeholder: "fast"},
  case: {label: "Only this one check", placeholder: "readme"},
  paths: {label: "Only these files or folders, separated by commas", placeholder: "src, tests"},
  needs: {label: "How much has to pass", choices: ["all", "any"]},
  command_kind: {label: "Which command", choices: ["test", "lint", "build"]},
  instructions: {label: "What the model should write", long: true,
                 characterLimit: PIPELINE_AI_INSTRUCTION_CHARACTER_LIMIT,
                 placeholder: "Write a test for the basket total, covering an empty basket."},
  write_to: {label: "Save the draft as", placeholder: "basket-total.test.js"},
  question: {label: "What to ask", long: true,
             placeholder: "Is anything else in this project still using the old parser?"},
  who: {label: "Which assistant, if not the usual one", placeholder: "Leave empty for the usual one"},
  pipeline: {label: "Which saved pipeline to run", placeholder: "Before a commit"},
};

function openPipelineNode(nodeId) {
  const node = pipeline.nodes.find((item) => item.id === nodeId);
  if (!node) return;
  pipelineEditing = nodeId;
  const kind = kindOf(node.kind);
  $("pipelineNodeDialogTitle").textContent = kind.label;
  $("pipelineNodeSummary").textContent = kind.summary || "";
  $("pipelineNodeLabel").value = node.label || kind.label;
  $("pipelineNodeTries").value = String(node.settings?.tries || 1);
  $("pipelineNodeLongest").value = String(node.settings?.longest || 0);
  $("pipelineNodeEvenIfItFails").checked = Boolean(node.settings?.even_if_it_fails);
  const box = $("pipelineNodeSettings");
  box.replaceChildren();
  for (const name of kind.settings || []) {
    const field = PIPELINE_FIELDS[name] || {label: name};
    const id = `pipelineSetting-${name}`;
    box.append(Object.assign(make("label", "", field.label), {htmlFor: id}));
    let input;
    if (field.choices) {
      input = make("select", "");
      for (const choice of field.choices) {
        const option = make("option", "", choice);
        option.value = choice;
        input.append(option);
      }
    } else if (field.long) {
      input = make("textarea", "");
      input.rows = 4;
    } else {
      input = make("input", "");
      input.type = "text";
    }
    input.id = id;
    input.dataset.setting = name;
    if (field.placeholder) input.placeholder = field.placeholder;
    input.value = node.settings?.[name] || "";
    box.append(input);
    if (field.characterLimit) {
      const countId = `${id}-count`;
      input.setAttribute("aria-describedby", countId);
      const count = make("p", "field-help", "");
      count.id = countId;
      count.setAttribute("role", "status");
      box.append(count);
      const update = () => renderDisclosedTextCount(
        id, countId, field.characterLimit, "the model-writing instructions");
      input.addEventListener("input", update);
      update();
    }
  }
  // Which of this step's settings should be asked about when the run starts
  // rather than fixed now. One saved pipeline then covers more than one job.
  const asking = $("pipelineNodeAskList");
  asking.replaceChildren();
  const alreadyAsked = node.settings?.asks || [];
  for (const name of kind.settings || []) {
    const field = PIPELINE_FIELDS[name] || {label: name};
    const row = make("label", "pipeline-ask-one");
    const tick = make("input");
    tick.type = "checkbox";
    tick.dataset.asks = name;
    tick.checked = alreadyAsked.includes(name);
    row.append(tick, make("span", "", field.label));
    asking.append(row);
  }
  $("pipelineNodeAsks").hidden = !(kind.settings || []).length;
  fillOneChoice("pipelineNodeWhen", pipelineWhens, "when",
                node.settings?.when || "when-all-is-well");
  fillOneChoice("pipelineNodeWait", pipelineWaits, "wait",
                node.settings?.wait || "no-wait");
  sayWhatTheStepChoicesMean();
  $("pipelineNodeDialog").showModal();
}

// The two choices every step has, filled from what the harness said they are.
// A list the page made up itself could offer something the harness cannot run.
function fillOneChoice(id, from, key, chosen) {
  const box = $(id);
  box.replaceChildren();
  for (const one of from) {
    const option = make("option", "", one.label);
    option.value = one[key];
    box.append(option);
  }
  box.value = chosen;
}

function sayWhatTheStepChoicesMean() {
  $("pipelineNodeWhenMeans").textContent =
    pipelineWhens.find((one) => one.when === $("pipelineNodeWhen").value)?.means || "";
  $("pipelineNodeWaitMeans").textContent =
    pipelineWaits.find((one) => one.wait === $("pipelineNodeWait").value)?.means || "";
  // Waiting only means anything when there is a second try to wait for.
  const tries = Number($("pipelineNodeTries").value) || 1;
  $("pipelineNodeWait").disabled = tries < 2;
  if (tries < 2) {
    $("pipelineNodeWaitMeans").textContent =
      "Nothing to wait for: this step is only tried once. Raise the number above first.";
  }
}

function savePipelineNode() {
  const node = pipeline.nodes.find((item) => item.id === pipelineEditing);
  if (!node) return;
  for (const input of $("pipelineNodeSettings").querySelectorAll("[data-setting]")) {
    const field = PIPELINE_FIELDS[input.dataset.setting] || {};
    if (!field.characterLimit) continue;
    const problem = disclosedTextProblem(
      input.id, `${input.id}-count`, field.characterLimit,
      "the model-writing instructions");
    if (problem) { say(problem); showError(problem); input.focus(); return; }
  }
  node.label = $("pipelineNodeLabel").value.trim() || kindOf(node.kind).label;
  const settings = {};
  for (const input of $("pipelineNodeSettings").querySelectorAll("[data-setting]")) {
    const value = input.value.trim();
    if (value) settings[input.dataset.setting] = value;
  }
  const tries = Number($("pipelineNodeTries").value) || 1;
  if (tries > 1) settings.tries = Math.min(5, Math.max(1, Math.round(tries)));
  const asks = [...$("pipelineNodeAskList").querySelectorAll("[data-asks]")]
    .filter((tick) => tick.checked)
    .map((tick) => tick.dataset.asks);
  if (asks.length) settings.asks = asks;
  const when = $("pipelineNodeWhen").value;
  if (when && when !== "when-all-is-well") settings.when = when;
  const wait = $("pipelineNodeWait").value;
  if (wait && wait !== "no-wait" && (settings.tries || 1) > 1) settings.wait = wait;
  // How long this one may take, and whether the rest carries on without it.
  // Both are left out when they say nothing, so a pipeline written before these
  // existed reads back exactly as it was written.
  const longest = Math.round(Number($("pipelineNodeLongest").value) || 0);
  if (longest > 0) settings.longest = Math.min(14400, longest);
  if ($("pipelineNodeEvenIfItFails").checked) settings.even_if_it_fails = true;
  node.settings = settings;
  $("pipelineNodeDialog").close();
  renderPipeline();
  say(`Saved the settings for ${node.label}.`);
}

/* ---- saving, checking, running ---- */

function pipelineOnScreen() {
  return {name: $("pipelineName").value.trim() || "Pipeline", nodes: pipeline.nodes, edges: pipeline.edges};
}

async function checkPipeline() {
  try {
    await request("/api/pipelines/check", {
      method: "POST", body: JSON.stringify({pipeline: pipelineOnScreen()}),
    });
    say("This pipeline can run.");
  } catch (error) { say(error.message); showError(error.message); }
}

async function savePipeline() {
  try {
    const said = await request("/api/pipelines/save", {
      method: "POST", body: JSON.stringify({pipeline: pipelineOnScreen()}),
    });
    pipeline = said.pipeline;
    pipelineSavedName = pipeline.name;
    $("pipelineName").value = pipeline.name;
    rememberPipelineBaseline();
    say(`Saved ${pipeline.name}.`);
    try {
      const list = await request(`/api/pipelines?name=${encodeURIComponent(pipeline.name)}`);
      pipelineSaved = list.saved || [];
      pipelineSavedProblems = list.saved_problems || [];
      pipelineOlderOnes = list.older_ones || [];
      renderPipelineSaved();
      if (pipelineLooking === "before") listHowItLookedBefore();
    } catch (inventoryError) {
      // The write is already acknowledged and must not be reported as a
      // failure merely because the follow-up library refresh was interrupted.
      say(`Saved ${pipeline.name}. The library list could not refresh yet: ${inventoryError.message}`);
    }
    return true;
  } catch (error) {
    say(error.message);
    showError(error.message);
    return false;
  }
}

async function savePipelineAs() {
  const name = await askForOneLine(
       "Save a copy", "What should the copy be called?",
       `${$("pipelineName").value} copy`);
  if (!name) return;
  $("pipelineName").value = name;
  renderPipelineDirtyState();
  await savePipeline();
}

async function deletePipeline() {
  const name = pipelineSavedName;
  if (!name || !pipelineSaved.includes(name)) {
    pipelineSavedName = "";
    renderPipelineSaved();
    say("Choose a saved automation before removing one. The drawing on screen was not changed.");
    return;
  }
  if (!window.confirm(`Remove the saved pipeline "${name}"? The drawing on screen stays.`)) return;
  try {
    const said = await request("/api/pipelines/delete", {method: "POST", body: JSON.stringify({name})});
    pipelineSaved = said.saved || [];
    pipelineSavedProblems = said.saved_problems || [];
    if (!pipelineSaved.includes(pipelineSavedName)) {
      pipelineSavedName = "";
      markPipelineDrawingUnsaved();
    }
    renderPipelineSaved();
    say(said.note);
  } catch (error) { say(error.message); showError(error.message); }
}

async function newPipeline() {
  if (!await mayReplacePipeline("creating a new automation")) return;
  const name = await askForOneLine(
    "Create new automation", "What should the new automation be called?", "New automation");
  if (!name) return;
  const beforeRequest = currentPipelineSnapshot();
  try {
    const said = await request("/api/pipelines/create", {
      method: "POST", body: JSON.stringify({name}),
    });
    if (currentPipelineSnapshot() !== beforeRequest) {
      pipelineSaved = said.saved || [];
      pipelineSavedProblems = said.saved_problems || [];
      renderPipelineSaved();
      say(`Created and saved ${said.pipeline?.name || name}, but the drawing changed meanwhile, so Nexus kept your newer edits. Open the new automation from the library when you are ready.`);
      return;
    }
    pipeline = said.pipeline;
    pipelineSavedName = pipeline.name;
    pipelineSaved = said.saved || [];
    pipelineSavedProblems = said.saved_problems || [];
    pipelineOlderOnes = [];
    pipelineStates = new Map();
    $("pipelineName").value = pipeline.name;
    $("pipelineLog").replaceChildren();
    rememberPipelineBaseline();
    renderPipeline();
    renderPipelineSaved();
    say(`Created and saved ${pipeline.name}. Add steps from the left.`);
  } catch (error) { say(error.message); showError(error.message); }
}

function pipelineImportName(document, file) {
  const imported = document?.schema === "nexus-harness.visual-automation"
    ? document.automation : document;
  const fromDocument = typeof imported?.name === "string" ? imported.name.trim() : "";
  const fromFile = String(file?.name || "Imported automation").replace(/\.json$/i, "");
  return fromDocument || fromFile || "Imported automation";
}

function unusedPipelineName(wanted) {
  if (!pipelineSaved.some((one) => one.toLowerCase() === wanted.toLowerCase())) return wanted;
  let number = 1;
  let candidate = `${wanted} copy`;
  while (pipelineSaved.some((one) => one.toLowerCase() === candidate.toLowerCase())) {
    number += 1;
    candidate = `${wanted} copy ${number}`;
  }
  return candidate;
}

async function importPipeline(file) {
  if (!file) return;
  try {
    if (file.size > 10_000_000) {
      throw new Error("That JSON file is larger than 10 MB. Nothing was imported.");
    }
    let written;
    try {
      written = new TextDecoder("utf-8", {fatal: true}).decode(
        await file.arrayBuffer()
      );
    } catch (_) {
      throw new Error("That automation file is not valid UTF-8. Nothing was imported.");
    }
    let document;
    try { document = JSON.parse(written); }
    catch (_) { throw new Error("That file is not valid JSON. Nothing was imported."); }
    const originalName = pipelineImportName(document, file);
    let name = originalName;
    if (pipelineSaved.some((one) => one.toLowerCase() === originalName.toLowerCase())) {
      name = await askForOneLine(
        "Import as a new automation",
        `“${originalName}” is already saved. Choose a name for the imported copy.`,
        unusedPipelineName(originalName),
      );
      if (!name) return;
    }
    const said = await request("/api/pipelines/import", {
      method: "POST", body: JSON.stringify({document, name}),
    });
    pipelineSaved = said.saved || [];
    pipelineSavedProblems = said.saved_problems || [];
    renderPipelineSaved();
    say(`${said.note || `Imported and saved ${name}.`} The current drawing was not changed; open the imported automation from the library when you are ready.`);
  } catch (error) {
    say(error.message);
    showError(error.message);
  } finally {
    $("pipelineImportFile").value = "";
  }
}

async function exportPipeline() {
  if (!pipelineSavedName || !pipelineSaved.includes(pipelineSavedName)) {
    say("Choose or save an automation before exporting it.");
    return;
  }
  try {
    const said = await request(
      `/api/pipelines/export?name=${encodeURIComponent(pipelineSavedName)}`
    );
    const written = JSON.stringify(said.document, null, 2) + "\n";
    if (window.harnessDesktop?.saveJsonFile) {
      const saved = await window.harnessDesktop.saveJsonFile(
        said.filename || "visual-automation.json", written
      );
      say(saved?.saved
        ? `Exported ${pipelineSavedName} as ${saved.filename || "JSON"}.`
        : "Export cancelled; nothing was written.");
      return;
    }
    const blob = new Blob([written], {
      type: "application/json",
    });
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = said.filename || "visual-automation.json";
    link.click();
    URL.revokeObjectURL(link.href);
    say(`Exported ${pipelineSavedName} as JSON.`);
  } catch (error) { say(error.message); showError(error.message); }
}

async function runPipeline(options = {}) {
  if (pipelineCannotRun) {
    say(`Automation is paused: ${pipelineCannotRun}`);
    return;
  }
  const definition = pipelineOnScreen();
  const pending = pipelineRequestFor("panel", definition.name);
  try {
    pipelineActiveRunName = definition.name;
    pipelineActiveRunId = pending.run_id || "";
    pipelineStates = new Map();
    showWhatIsBeingAsked("");
    $("pipelineLog").replaceChildren();
    renderPipeline();
    const accepted = await request("/api/pipelines/run", {
      method: "POST",
      body: JSON.stringify({
        pipeline: definition,
        request_id: pending.request_id,
        // Three ways to run less than the whole thing. Left out when not
        // asked for, so an ordinary Run is exactly what it always was.
        ...(options.from_here ? {from_here: options.from_here} : {}),
        ...(options.only ? {only: options.only} : {}),
        ...(options.answers ? {answers: options.answers} : {}),
      }),
    });
    pipelineActiveRunId = accepted.run_id || "";
    pipelineProjectionRunId = pipelineActiveRunId;
    rememberPipelinePendingRequest({...pending, run_id: pipelineActiveRunId});
    $("pipelineStop").disabled = false;
    showPipelineActiveRun({run_id: pipelineActiveRunId, request_id: pending.request_id,
      name: pipelineActiveRunName, state: "accepted", running: true});
    say(options.only
      ? "Running that one step on its own."
      : options.from_here
        ? "Carrying on from that step. The ones before it are left as they were."
        : "Running. Each step lights up as it goes.");
  } catch (error) {
    if (pipelineRequestWasDefinitelyRejected(error)) {
      rememberPipelinePendingRequest(null);
      pipelineActiveRunName = "";
      pipelineActiveRunId = "";
      pipelineProjectionRunId = "";
    }
    $("pipelineStop").disabled = !pipelineActiveRunId;
    say(pipelineRequestWasDefinitelyRejected(error) ? error.message
      : `${error.message} The outcome is unknown; Retry reuses request ${pending.request_id} and cannot start a duplicate.`);
    showPipelineActiveRun(null, pipelinePendingRequest);
    showError(error.message);
  }
}

async function stopPipeline() {
  try {
    if (!pipelineActiveRunId) {
      say("There is no exact active run to stop.");
      return;
    }
    const said = await request("/api/pipelines/stop", {
      method: "POST", body: JSON.stringify({run_id: pipelineActiveRunId}),
    });
    say(said.note);
  } catch (error) { showError(error.message); }
}

// News from a run, arriving while it happens.
function applyPipelineEvent(event) {
  if (event.kind === "pipeline_started") {
    const startedRunId = String(event.run_id || event.payload?.run_id || "");
    if (pipelineActiveRunId && startedRunId !== pipelineActiveRunId) return;
    pipelineActiveRunName = event.payload?.name || "";
    pipelineActiveRunId = startedRunId;
    if (pipelinePendingRequest?.request_id === event.payload?.request_id) {
      rememberPipelinePendingRequest({...pipelinePendingRequest, run_id: startedRunId});
    }
    const visible = startedRunId && pipelineProjectionRunId === startedRunId;
    $("pipelineStop").disabled = !startedRunId;
    showPipelineActiveRun({run_id: startedRunId, request_id: event.payload?.request_id,
      name: pipelineActiveRunName, state: "running", running: true});
    say(visible
      ? `Running ${pipelineActiveRunName}.`
      : `Running ${pipelineActiveRunName || "an automation"}. Open its immutable snapshot to show step updates here.`);
    return;
  }
  if (event.kind === "pipeline_node") {
    const eventRunId = String(event.run_id || event.payload?.run_id || "");
    if (!pipelineProjectionRunId || eventRunId !== pipelineProjectionRunId) return;
    const result = event.payload || {};
    pipelineStates.set(String(result.id), result);
    renderPipeline();
    addPipelineLogLine(result);
    if (result.kind === "wait_for_a_person" && result.state === "running") {
      showWhatIsBeingAsked(String(result.id));
    } else if (String(result.id) === pipelineWaitingAt) {
      showWhatIsBeingAsked("");
    }
    if (pipelineLooking === "timeline") drawThePipelineTimeline();
    return;
  }
  if (event.kind === "pipeline_finished") {
    const finished = event.payload || {};
    const finishedRunId = String(event.run_id || finished.run_id || "");
    if (!finishedRunId || finishedRunId !== pipelineProjectionRunId) {
      if (finishedRunId && finishedRunId === pipelineActiveRunId) {
        if (pipelinePendingRequest?.run_id === finishedRunId) rememberPipelinePendingRequest(null);
        showPipelineActiveRun({...finished, run_id: finishedRunId, running: false,
          state: finished.state || "completed"}, null);
        pipelineActiveRunName = "";
        pipelineActiveRunId = "";
      }
      say(`${finished.name || "Another automation"} finished. This board stayed unchanged.`);
      return;
    }
    $("pipelineStop").disabled = true;
    showWhatIsBeingAsked("");
    showPipelineRun(finished);
    if (pipelinePendingRequest?.run_id === finishedRunId) rememberPipelinePendingRequest(null);
    showPipelineActiveRun({...finished, run_id: finishedRunId, running: false,
      state: finished.state || "completed"}, null);
    pipelineActiveRunName = "";
    pipelineActiveRunId = "";
    pipelineProjectionRunId = "";
    if (pipelineLooking === "timeline") drawThePipelineTimeline();
  }
}

function addPipelineLogLine(result) {
  const log = $("pipelineLog");
  const already = log.querySelector(`[data-node="${CSS.escape(String(result.id))}"]`);
  const line = already || make("li", "");
  line.dataset.node = String(result.id);
  line.className = `pipeline-log-line ${result.state}`;
  line.replaceChildren(
    make("strong", "", result.label || result.id),
    make("span", "", ` ${result.state}`),
    make("span", "pipeline-log-said", result.said ? ` - ${result.said}` : "")
  );
  if (result.tries > 1) line.append(make("span", "pipeline-log-said", ` (try ${result.tries})`));
  if (!already) log.append(line);
}

function showPipelineRun(run) {
  for (const result of run.nodes || []) {
    pipelineStates.set(String(result.id), result);
    addPipelineLogLine(result);
  }
  renderPipeline();
  if (run.said) say(run.said);
}

/* ---- Show me around ----

   Six short stops, one per tab, each opening the tab it is talking about. A
   person who has just opened this has no idea which of these words mean
   anything, and reading a page of documentation to find out is exactly what
   they will not do.

   Every stop names a tab that is really in the panel: a stop pointing at a tab
   nobody built would be a tour of somewhere else.
*/

const TOUR = [
  {
    view: "start",
    where: "Start here",
    title: "This screen gets the project ready",
    said: "Four short steps, in any order. When they are all done, you can ask for a "
      + "change in your own words at the bottom of this screen.",
  },
  {
    view: "checks",
    where: "Checks",
    title: "Your checks live here",
    said: "A check says what a working project looks like. Write one from a ready-made "
      + "shelf, record yourself using the site, or point at something on a page.",
  },
  {
    view: "pipelines",
    where: "Pipelines",
    title: "Many jobs, wired together",
    said: "Run your checks and a security scan side by side, and only carry on if they "
      + "passed. Start from a ready-made one on the left.",
  },
  {
    view: "workflow",
    where: "Workflow",
    title: "Who does what, when you ask for a change",
    said: "The agents that plan, change the files, and review the result. You can rewire "
      + "them, and the picture on the first screen follows what you do here.",
  },
  {
    view: "settings",
    where: "Settings",
    title: "Everything the harness can be told",
    said: "Every setting in plain words, what it is now, and where that came from. "
      + "Nothing has to be edited by hand.",
  },
  {
    view: "history",
    where: "History",
    title: "What past runs did",
    said: "Every run, step by step, with what each agent said and what it cost. This is "
      + "where to look when something went differently from last time.",
  },
];

let tourAt = -1;

function showMeAround() {
  tourAt = 0;
  drawTour();
}

function drawTour() {
  const box = $("tour");
  if (tourAt < 0 || tourAt >= TOUR.length) {
    box.hidden = true;
    box.replaceChildren();
    return;
  }
  const stop = TOUR[tourAt];
  // The tour sits above the tabs, so opening the tab it is talking about shows
  // that tab and nothing else. Holding the first screen open as well drew two
  // whole views one after the other, which is exactly the confusion a tour is
  // supposed to save somebody from.
  switchView(stop.view);
  box.replaceChildren();
  box.hidden = false;
  box.append(make("span", "tour-where", `${stop.where} - ${tourAt + 1} of ${TOUR.length}`));
  box.append(make("strong", "", stop.title));
  box.append(make("p", "", stop.said));
  const buttons = make("div", "tour-buttons");
  if (tourAt > 0) {
    const back = make("button", "", "Back");
    back.type = "button";
    back.addEventListener("click", () => { tourAt -= 1; drawTour(); });
    buttons.append(back);
  }
  const on = make("button", "primary", tourAt === TOUR.length - 1 ? "That is the lot" : "Next");
  on.type = "button";
  on.addEventListener("click", () => { tourAt += 1; drawTour(); });
  const stop_it = make("button", "", "Stop the tour");
  stop_it.type = "button";
  stop_it.addEventListener("click", () => { tourAt = -1; switchView("start"); drawTour(); });
  buttons.append(on, stop_it);
  box.append(buttons);
  announce(`${stop.where}. ${stop.title}. ${stop.said}`);
}

/* ---- What happens when you ask for a change ---- */

// The picture on the first screen. Drawn from the workflow the harness will
// really run, so it can never describe something the harness does not do.
let howStages = [];
let howWalk = null;
// How far the walk through has got, kept as a name against a state rather than
// a handful of boxes. Anything that redraws the picture — pressing "Draw it
// again", news arriving from a run — replaces every box on the page, and a
// walk holding those boxes would carry on lighting up ones nobody can see.
let howWalkStates = new Map();

async function refreshHowItWorks() {
  try {
    // The workflow on screen when there is one, so editing it changes this too.
    const drawn = graph && Array.isArray(graph.nodes) && graph.nodes.length ? graph : null;
    const said = await request("/api/how-it-works", {
      method: "POST",
      body: JSON.stringify(drawn ? {graph: drawn} : {}),
    });
    howStages = said.stages || [];
    $("howHeadline").textContent = said.headline || "";
    renderHowItWorks();
    const loops = $("howLoops");
    loops.replaceChildren();
    for (const line of said.loops || []) loops.append(make("li", "", line));
  } catch (error) { showError(error.message); }
}

function renderHowItWorks() {
  const list = $("howStages");
  list.replaceChildren();
  howStages.forEach((stage, spot) => {
    const item = make("li", `how-stage ${stage.kind || ""}`);
    item.dataset.stage = stage.id;
    // One set of words for the box's look, another for the person reading it.
    // These used to be the same string, so a real run wrote "Working on it"
    // into the state and nothing lit up, because the styles key on "doing".
    const walking = howWalkStates.get(stage.id);
    const live = walking ? "" : howState(stage.id);
    if (walking) item.dataset.state = walking;
    else if (live) item.dataset.state = live.look;
    const head = make("div", "how-head");
    head.append(make("span", "how-number", String(spot + 1)), make("strong", "", stage.title));
    item.append(head, make("p", "", stage.detail));
    if (live) item.append(make("p", "how-state", live.said));
    if ((stage.goes_back_to || []).length) {
      item.append(make("p", "how-back", "If this goes wrong, the work goes back a step."));
    }
    list.append(item);
  });
  hideArrowsAtTheEndOfARow();
}

// The boxes wrap onto as many rows as the window needs. The arrow between two
// boxes is drawn to the right of the first one, so the last box on a row would
// otherwise point off the edge at nothing.
function hideArrowsAtTheEndOfARow() {
  const boxes = [...$("howStages").children];
  boxes.forEach((box, spot) => {
    const next = boxes[spot + 1];
    box.classList.toggle("row-end", !next || next.offsetTop > box.offsetTop);
  });
}

function howState(nodeId) {
  // The same news the workflow canvas gets, so both agree about what is
  // happening without either being told twice. Two things come back: the word
  // the styles colour the box by, and the words a person reads.
  const said = nodeStatuses.get(String(nodeId));
  if (!said) return null;
  const known = {
    Running: {look: "doing", said: "Working on it"},
    Updated: {look: "doing", said: "Working on it"},
    Passed: {look: "done", said: "Done"},
    Failed: {look: "wrong", said: "Something was wrong"},
  };
  return known[said] || {look: "doing", said};
}

async function demoHowItWorks() {
  const button = $("howDemo");
  if (howWalk) { window.clearTimeout(howWalk); howWalk = null; }
  howWalkStates = new Map();
  if (!howStages.length) await refreshHowItWorks();
  if (!howStages.length) { $("howSaid").textContent = "There is no workflow to walk through yet."; return; }
  button.disabled = true;
  const walking = howStages.map((stage) => stage.id);
  const quick = matchMedia("(prefers-reduced-motion: reduce)").matches;
  let spot = 0;
  const step = () => {
    if (spot > 0) howWalkStates.set(walking[spot - 1], "done");
    if (spot >= walking.length) {
      renderHowItWorks();
      howWalk = null;
      button.disabled = false;
      $("howSaid").textContent = "That is the whole thing. Ask for a change below and it really happens.";
      announce("The walk through finished.");
      return;
    }
    howWalkStates.set(walking[spot], "doing");
    renderHowItWorks();
    $("howSaid").textContent = `Step ${spot + 1} of ${walking.length}: ${howStages[spot].title}`;
    spot += 1;
    howWalk = window.setTimeout(step, quick ? 60 : 900);
  };
  step();
}

async function findSeats() {
  const button = $("findSeats");
  button.disabled = true;
  $("seatFindSaid").textContent = "Looking, and asking each one its version.";
  try {
    renderSeats(await request("/api/seats"));
  } catch (error) { showError(error.message); $("seatFindSaid").textContent = error.message; }
  button.disabled = false;
}

async function setUpSeats() {
  const ready = ((seatsFound && seatsFound.seats) || []).filter((seat) => seat.ready);
  if (!ready.length) return;
  const button = $("setUpSeats");
  button.disabled = true;
  try {
    const done = await request("/api/seats/setup", {
      method: "POST",
      body: JSON.stringify({kinds: ready.map((seat) => seat.kind)}),
    });
    $("seatWriteSaid").textContent = done.trusted
      ? `Written to ${done.settings_file} and trusted. Routes: ${done.routes.join(", ")}.`
      : `Written to ${done.settings_file}. ${done.note}`;
    if (done.kept && done.kept.length) {
      $("seatWriteSaid").textContent += ` Your other settings were kept: ${done.kept.join(", ")}.`;
    }
    if (done.replaced && done.replaced.length) {
      // Writing over somebody's earlier decision is said out loud, not left
      // for them to notice in the file.
      $("seatWriteSaid").textContent += ` Written over: ${done.replaced.join(", ")}.`;
    }
    $("seatFile").textContent = done.contents;
    $("seatFileBox").hidden = false;
    showTheChoiceAboutTrusting(done);
    markSeatStep("seatStepWrite", "done");
    $("undoSeats").hidden = false;
    markSeatStep("seatStepShare", "doing");
    $("shareTheWork").disabled = false;
    $("shareTheWork").dataset.routes = JSON.stringify(done.routes);
    announce($("seatWriteSaid").textContent);
    refreshCheckup();
  } catch (error) { showError(error.message); $("seatWriteSaid").textContent = error.message; }
  button.disabled = false;
}

// A settings file that was already here and never trusted is not this panel's
// to trust on its own, and not its place to refuse either. So it says what
// trusting would allow, in the plainest words there are, and puts the choice
// where it belongs.
function showTheChoiceAboutTrusting(done) {
  const box = $("trustChoice");
  box.replaceChildren();
  box.hidden = !done.needs_your_say;
  if (!done.needs_your_say) return;
  // The mark of the file being shown, handed back when the person says they
  // have read it, so the harness can refuse if the file changed in between.
  box.dataset.mark = done.mark || "";
  box.append(make("strong", "", "This file was already here. Only you can say it is yours."));
  box.append(make("p", "", done.note || ""));
  if ((done.risky_parts || []).length) {
    box.append(make("p", "", "Trusting it would allow this:"));
    const list = make("ul", "trust-risks");
    for (const line of done.risky_parts) list.append(make("li", "", line));
    box.append(list);
  } else {
    // Never a clean bill of health. This says what was looked for and what was
    // not found, which is a different thing from "there is nothing to worry
    // about" - and saying the second while a file quietly starts programs is
    // the worst thing this panel could do.
    box.append(make("p", "", "Nothing in it matched the things this knows to look for."
      + " That is not the same as safe. Read the file above yourself."));
  }
  box.append(make("p", "field-help", "The whole file is shown above. Nothing else was changed."));
  const button = make("button", "danger", "I have read it - trust it anyway");
  button.type = "button";
  button.addEventListener("click", () => trustItAnyway(button));
  box.append(button);
}

async function trustItAnyway(button) {
  button.disabled = true;
  try {
    const said = await request("/api/settings/trust-anyway", {
      method: "POST", body: JSON.stringify({seen: $("trustChoice").dataset.mark || ""}),
    });
    $("trustChoice").replaceChildren(make("p", "do-it-worked", said.note));
    $("seatWriteSaid").textContent += " Trusted, because you said so.";
    announce(said.note);
    refreshCheckup();
  } catch (error) { showError(error.message); button.disabled = false; }
}

async function undoSeats() {
  const button = $("undoSeats");
  button.disabled = true;
  try {
    const said = await request("/api/seats/undo", {method: "POST", body: "{}"});
    $("seatWriteSaid").textContent = said.note;
    $("seatFileBox").hidden = true;
    button.hidden = true;
    markSeatStep("seatStepWrite", "doing");
    markSeatStep("seatStepShare", "waiting");
    $("shareTheWork").disabled = true;
    announce(said.note);
    refreshCheckup();
  } catch (error) { showError(error.message); $("seatWriteSaid").textContent = error.message; }
  button.disabled = false;
}

async function shareTheWork() {
  const routes = JSON.parse($("shareTheWork").dataset.routes || "[]");
  if (!routes.length) return;
  const button = $("shareTheWork");
  button.disabled = true;
  try {
    const answer = await request("/api/seats/share-the-work", {
      method: "POST",
      body: JSON.stringify({graph, routes}),
    });
    graph = answer.graph;
    render();
    const list = $("seatJobs");
    list.replaceChildren();
    for (const node of graph.nodes || []) {
      const route = (node.config || {}).provider_route;
      if (!route) continue;
      const row = make("li", "seat ready");
      row.append(make("span", "seat-state", route));
      const detail = make("div", "");
      detail.append(make("strong", "", node.label || node.id));
      detail.append(make("p", "", "Can send and read notes."));
      row.append(detail);
      list.append(row);
    }
    $("seatShareSaid").textContent =
      "Done. Open the Workflow tab to see it, then press Start run.";
    markSeatStep("seatStepShare", "done");
    announce($("seatShareSaid").textContent);
  } catch (error) { showError(error.message); $("seatShareSaid").textContent = error.message; }
  button.disabled = false;
}

let coverageFound = null;

async function findGaps() {
  const address = await askForOneLine(
    "Which address should the walk start from?",
    "It opens your site, follows the links, and tells you which pages have "
    + "no check at all.",
    window.location.origin + "/"
  );
  if (!address) return;
  try {
    await request("/api/qa/coverage", {method: "POST", body: JSON.stringify({url: address})});
    $("checkStatus").textContent = "Walking your site. This takes a moment.";
    announce($("checkStatus").textContent);
  } catch (error) { showError(error.message); }
}

function renderCoverage(found) {
  coverageFound = found;
  const box = $("coverageResult");
  const bar = $("coverageBar");
  const list = $("coverageList");
  const button = $("addMissingChecks");
  box.hidden = false;
  list.textContent = "";
  bar.textContent = "";
  const pages = found.pages || [];
  const missing = found.missing || [];
  const percent = Number(found.percent || 0);
  bar.setAttribute(
    "aria-label",
    `${percent} out of every 100 pages has a check of its own`
  );
  // One block per page, so the gap is something you see rather than count.
  for (const page of pages) {
    const block = make("span", `coverage-block coverage-${(page.state || "").replace(/ /g, "-")}`);
    block.title = `${page.address} — ${page.state}`;
    bar.append(block);
  }
  const parts = [
    `Walked ${pages.length} page${pages.length === 1 ? "" : "s"}.`,
    `${(found.checked || []).length} have a check of their own (${percent}%).`,
  ];
  if (found.note) parts.push(found.note);
  if (found.more_pages) parts.push(`${found.more_pages} more were still waiting.`);
  $("coverageSummary").textContent = parts.join(" ");
  for (const page of pages) {
    const row = make("li", `coverage-row coverage-${(page.state || "").replace(/ /g, "-")}`);
    row.append(make("span", "coverage-state", page.state));
    row.append(make("span", "coverage-address", page.address));
    if ((page.checked_by || []).length) {
      row.append(make("span", "quiet", `checked by ${page.checked_by.join(", ")}`));
    }
    list.append(row);
  }
  button.hidden = missing.length === 0;
  button.textContent = missing.length === 1
    ? "Write a check for that page"
    : `Write a check for all ${missing.length} pages nobody looks at`;
  $("checkStatus").textContent = missing.length
    ? `${missing.length} page${missing.length === 1 ? " has" : "s have"} no check at all.`
    : "Every page that was walked has a check.";
  announce($("checkStatus").textContent, missing.length > 0);
}

async function addMissingChecks() {
  const missing = (coverageFound && coverageFound.missing) || [];
  if (!missing.length) return;
  try {
    const answer = await request("/api/qa/coverage/add", {
      method: "POST",
      body: JSON.stringify({addresses: missing}),
    });
    const added = answer.added || [];
    $("checkStatus").textContent = `Added ${added.length} check${added.length === 1 ? "" : "s"}: `
      + added.join(", ");
    announce($("checkStatus").textContent);
    $("addMissingChecks").hidden = true;
    refreshChecks();
  } catch (error) { showError(error.message); }
}

function renderPick(picked) {
  const box = $("pickResult");
  const names = $("pickNames");
  const thrown = $("pickThrown");
  names.replaceChildren();
  thrown.textContent = "";
  box.hidden = false;
  if (picked.gave_up) {
    $("pickSummary").textContent = "Nothing was picked.";
    return;
  }
  const said = picked.text ? ` that says "${picked.text}"` : "";
  $("pickSummary").textContent = picked.offered.length
    ? `You picked a <${picked.tag || "thing"}>${said}. Best name first.`
    : `You picked a <${picked.tag || "thing"}>${said}, but no name matches it on its own. `
      + "Ask whoever wrote the page to add data-testid=\"something\" to it.";
  for (const name of picked.offered) {
    const row = make("li", "");
    // Everything here is put in as text, never as page code, because these
    // words come from whatever page was opened.
    row.append(make("code", "", name.selector), make("span", "pick-why", name.reason));
    const copy = make("button", "", "Copy step");
    copy.type = "button";
    copy.addEventListener("click", async () => {
      const step = JSON.stringify({do: "expect_visible", target: name.selector});
      try {
        await navigator.clipboard.writeText(step);
        copy.textContent = "Copied";
        announce("Step copied.");
      } catch (error) { showError("This browser would not let the harness copy. Select the name and copy it yourself."); }
    });
    row.append(copy);
    if (name.warning) row.append(make("span", "pick-warning", name.warning));
    names.append(row);
  }
  const others = picked.thrown_away || [];
  if (others.length) {
    thrown.textContent = "Also tried, but each matches the wrong number of things: "
      + others.slice(0, 5).map((item) => `${item.selector} (${item.matches})`).join(", ");
  }
}

function applyCheckEvent(event) {
  if (event.kind === "record_started") { $("checkStatus").textContent = "Recording. Press Done in the browser window when you have finished."; return; }
  if (event.kind === "record_error") { $("checkStatus").textContent = `Nothing was recorded: ${event.payload?.error || "unknown reason"}`; return; }
  if (event.kind === "record_result") {
    const left = (event.payload?.left_out || []).length;
    $("checkStatus").textContent = `Added the check ${event.payload?.added} with ${event.payload?.steps} steps.`
      + (left ? ` ${left} action${left === 1 ? " was" : "s were"} left out; see the run log.` : "");
    for (const line of event.payload?.left_out || []) appendEvent("recording", line);
    refreshChecks();
    return;
  }
  if (event.kind === "pick_started") { $("checkStatus").textContent = "Waiting for you to click something."; return; }
  if (event.kind === "pick_error") { $("checkStatus").textContent = `Could not open the page: ${event.payload?.error || "unknown reason"}`; return; }
  if (event.kind === "pick_result") { $("checkStatus").textContent = "Picked."; renderPick(event.payload); return; }
  if (event.kind === "coverage_started") { $("checkStatus").textContent = "Walking your site to see which pages are checked."; return; }
  if (event.kind === "coverage_error") { $("checkStatus").textContent = `Could not walk the site: ${event.payload?.error || "unknown reason"}`; return; }
  if (event.kind === "coverage_result") { renderCoverage(event.payload); return; }
  if (event.kind === "qa_started") { $("checkStatus").textContent = "Running."; return; }
  if (event.kind === "qa_error") { $("checkStatus").textContent = `Could not run: ${event.payload?.error || "unknown reason"}`; return; }
  if (event.kind !== "qa_result") return;
  qaResult = event.payload;
  const counts = qaResult.counts || {};
  $("checkStatus").textContent = qaResult.passed
    ? `All ${counts.total} checks passed.`
    : `${counts.failed} of ${counts.total} checks failed.`;
  announce($("checkStatus").textContent, !qaResult.passed);
  renderChecks();
  refreshUnstable();
}

function bindEvents() {
  applyMoreOptionsPreference();
  // Startup performs several local reads. Any real pointer or keyboard action
  // means the person has begun using the current workspace, so a late recovery
  // result may announce itself but must not move them to another view.
  const markUserInteraction = (event) => {
    if (event.isTrusted) userViewSelectionRevision += 1;
  };
  document.addEventListener("pointerdown", markUserInteraction, {capture: true});
  document.addEventListener("keydown", markUserInteraction, {capture: true});
  applyAgentRunPanelPreference();
  $("agentRunPanel").addEventListener("toggle", rememberAgentRunPanelPreference);
  document.querySelectorAll("[data-node-type]").forEach((button) => { button.addEventListener("click", () => addNode(button.dataset.nodeType, 360, 300, button)); button.addEventListener("dragstart", (event) => event.dataTransfer.setData("application/x-harness-node", button.dataset.nodeType)); });
  document.querySelectorAll("[data-view]").forEach((button) => button.addEventListener(
    "click", () => switchView(button.dataset.view, {userInitiated: true}),
  ));
  $("canvas").addEventListener("dragover", (event) => { if (event.dataTransfer.types.includes("application/x-harness-node")) event.preventDefault(); });
  $("canvas").addEventListener("drop", (event) => { event.preventDefault(); const type = event.dataTransfer.getData("application/x-harness-node"); const rect = $("canvas").getBoundingClientRect(); addNode(type, (event.clientX - rect.left - pan.x) / zoom, (event.clientY - rect.top - pan.y) / zoom); });
  $("canvas").addEventListener("pointerdown", (event) => { if (event.target !== $("canvas") && event.target !== $("viewport") && event.target !== $("nodeLayer")) return; panDrag = {startX: event.clientX, startY: event.clientY, x: pan.x, y: pan.y}; $("canvas").classList.add("panning"); clearSelection(); });
  window.addEventListener("pointermove", (event) => { if (edgeDrag) updateEdgeDrag(event); else if (drag) { if (!drag.history && (event.clientX !== drag.startX || event.clientY !== drag.startY)) { pushHistory(); drag.history = true; } const node = nodeById(drag.id); node.position.x = Math.max(0, drag.x + (event.clientX - drag.startX) / zoom); node.position.y = Math.max(0, drag.y + (event.clientY - drag.startY) / zoom); renderNodes(); renderEdges(); } else if (panDrag) { pan.x = panDrag.x + event.clientX - panDrag.startX; pan.y = panDrag.y + event.clientY - panDrag.startY; updateViewport(); } });
  window.addEventListener("pointerup", (event) => { if (edgeDrag) finishEdgeDrag(event); drag = null; panDrag = null; $("canvas").classList.remove("panning"); });
  window.addEventListener("pointercancel", (event) => finishEdgeDrag(event, true));
  $("canvas").addEventListener("wheel", (event) => { event.preventDefault(); zoom = Math.max(.3, Math.min(1.8, zoom * (event.deltaY > 0 ? .9 : 1.1))); updateViewport(); }, {passive: false});
  $("zoomIn").addEventListener("click", () => { zoom = Math.min(1.8, zoom + .1); updateViewport(); }); $("zoomOut").addEventListener("click", () => { zoom = Math.max(.3, zoom - .1); updateViewport(); }); $("fitButton").addEventListener("click", fitGraph); $("undoButton").addEventListener("click", undo);
  $("addAgentButton").addEventListener("click", () => openAgentDialog("planner", 360, 300, $("addAgentButton"))); $("validateButton").addEventListener("click", () => validate()); $("simulateButton").addEventListener("click", simulate); $("runButton").addEventListener("click", startRun); $("exportButton").addEventListener("click", exportGraph); $("importInput").addEventListener("change", (event) => event.target.files[0] && importGraph(event.target.files[0])); $("clearLog").addEventListener("click", () => $("eventBody").replaceChildren());
  $("agentForm").addEventListener("submit", submitAgent); $("closeAgentDialog").addEventListener("click", closeAgentDialog); $("cancelAgent").addEventListener("click", closeAgentDialog); $("agentType").addEventListener("change", () => { $("agentMergeFields").hidden = $("agentType").value !== "merge"; }); $("agentProvider").addEventListener("change", () => updateModelSuggestions($("agentProvider").value, $("agentModel"))); $("agentRef").addEventListener("change", () => applyAgentAssignment($("agentRef"), $("agentProvider"), $("agentModel"), $("agentRoleName"), $("agentCapabilities")));
  for (const [fieldId, countId] of [["agentPrompt", "agentPromptCount"], ["nodePrompt", "nodePromptCount"], ["teamCustomPrompt", "teamCustomPromptCount"]]) {
    $(fieldId).addEventListener("input", () => renderSystemPromptCount(fieldId, countId));
  }
  $("agentDialog").addEventListener("close", () => dialogInvoker?.focus?.());
  ["nodeLabel", "nodeProvider", "nodeModel", "nodeRoleName", "nodePrompt", "nodeRole", "mergeSlots", "mergeOutput"].forEach((id) => $(id).addEventListener("change", updateSelectedNode)); $("nodeCapabilities").addEventListener("change", updateSelectedNode); $("nodeAgentRef").addEventListener("change", () => { applyAgentAssignment($("nodeAgentRef"), $("nodeProvider"), $("nodeModel"), $("nodeRoleName"), $("nodeCapabilities")); updateSelectedNode(); });
  ["edgeMode", "edgeCondition", "edgeVariables", "edgeTargetSlot", "edgeReturnFields", "maxIterations", "temperatureDecay", "loopTimeout"].forEach((id) => $(id).addEventListener("change", updateSelectedEdge)); $("deleteNode").addEventListener("click", () => selected?.kind === "node" && removeNode(selected.id)); $("deleteEdge").addEventListener("click", () => selected?.kind === "edge" && removeEdge(selected.id));
  $("newWorkflow").addEventListener("click", newWorkflow); $("saveWorkflow").addEventListener("click", saveWorkflow); $("renameWorkflow").addEventListener("click", renameWorkflow); $("deleteWorkflow").addEventListener("click", deleteWorkflow);
  $("refreshHistory").addEventListener("click", refreshHistory); $("refreshCheckup").addEventListener("click", () => refreshCheckup(true)); $("quickRun").addEventListener("click", quickRun); $("quickBootstrap").addEventListener("change", updateQuickReadiness); $("quickChecks").addEventListener("click", () => { switchView("checks"); runChecks(); });
  document.querySelectorAll("[data-example]").forEach((button) => button.addEventListener("click", () => { $("quickTask").value = button.dataset.example; $("quickTask").focus(); }));
  $("pipelineName").addEventListener("input", renderPipelineDirtyState);
  window.addEventListener("resize", () => { if (howStages.length) hideArrowsAtTheEndOfARow(); }); $("showMeAround").addEventListener("click", showMeAround); $("vaultNew").addEventListener("click", newVaultNote); $("vaultLearn").addEventListener("click", vaultLearnFromRuns); $("vaultRedraw").addEventListener("click", () => { vaultPlaces = new Map(); settleTheVault(); }); $("vaultEdit").addEventListener("click", editVaultNote); $("vaultRemove").addEventListener("click", removeVaultNote); $("vaultUsedWell").addEventListener("click", () => vaultNoteWasUsed(true)); $("vaultUsedBadly").addEventListener("click", () => vaultNoteWasUsed(false)); $("vaultClose").addEventListener("click", () => { $("vaultNote").hidden = true; vaultOpen = ""; renderVaultList(); drawTheVault(); }); $("vaultFormSave").addEventListener("click", saveVaultNote); $("vaultFormCancel").addEventListener("click", () => $("vaultDialog").close()); $("vaultFormBody").addEventListener("input", renderVaultBodyCount); $("vaultSearch").addEventListener("input", (event) => { vaultLooking = event.target.value; renderVaultList(); settleTheVaultSoon(); if (vaultNotes.length >= MOST_TO_DRAW || vaultAskingFor) { vaultAskingFor = event.target.value.trim(); refreshVault(vaultOpen); } }); $("vaultOnlyNear").addEventListener("change", () => { renderVaultList(); settleTheVault(); }); $("vaultGraph").addEventListener("keydown", vaultGraphKey); $("refreshSettings").addEventListener("click", refreshSettings); $("settingsFilter").addEventListener("input", renderSettings); $("settingsChangedOnly").addEventListener("change", renderSettings); $("moreOptionsEnabled").addEventListener("change", changeMoreOptionsPreference); $("pipelineSave").addEventListener("click", savePipeline); $("pipelineSaveAs").addEventListener("click", savePipelineAs); $("pipelineImport").addEventListener("click", () => $("pipelineImportFile").click()); $("pipelineImportFile").addEventListener("change", (event) => importPipeline(event.target.files?.[0])); $("pipelineExport").addEventListener("click", exportPipeline); $("pipelineRun").addEventListener("click", () => runPipelineAsking()); $("pipelineStop").addEventListener("click", stopPipeline); $("pipelineDelete").addEventListener("click", deletePipeline); $("pipelineNew").addEventListener("click", newPipeline); $("pipelineCheck").addEventListener("click", checkPipeline); $("pipelineNodeSave").addEventListener("click", savePipelineNode); $("pipelineNodeCancel").addEventListener("click", () => $("pipelineNodeDialog").close()); document.addEventListener("pointermove", movePipelineDrag); document.addEventListener("pointerup", endPipelineDrag); $("howDemo").addEventListener("click", demoHowItWorks); $("howRefresh").addEventListener("click", refreshHowItWorks); $("findSeats").addEventListener("click", findSeats); $("setUpSeats").addEventListener("click", setUpSeats); $("shareTheWork").addEventListener("click", shareTheWork); $("undoSeats").addEventListener("click", undoSeats); $("createSuite").addEventListener("click", createSuite); $("runChecks").addEventListener("click", runChecks); $("saveBaselines").addEventListener("click", saveBaselines); $("pickElement").addEventListener("click", pickElement); $("findGaps").addEventListener("click", findGaps); $("makeSharePage").addEventListener("click", makeSharePage); $("addMissingChecks").addEventListener("click", addMissingChecks);$("recordSteps").addEventListener("click", recordSteps); $("makeBundle").addEventListener("click", makeBundle); $("starterBox").addEventListener("toggle", () => $("starterBox").open && refreshStarters()); $("refreshUnstable").addEventListener("click", () => { refreshUnstable(); refreshChanged(); }); $("checkTag").addEventListener("change", renderChecks);
  $("teamLookAgain").addEventListener("click", () => refreshTeam(teamOpen));
  $("teamSetUp").addEventListener("click", setUpTheTeam);
  // This one says what went wrong. Without it, a request that failed threw
  // where nobody was listening: the button was pressed, nothing happened, and
  // there was nothing on the screen to say why.
  $("teamStarting").addEventListener("click", async () => {
    try {
      const said = await request("/api/who-is-on-it");
      useTheStartingTeam(said.starting_team);
      teamSay("This is the ready-made team. Change anything you like, then save it.");
    } catch (error) { showError(error.message); teamSay(error.message); }
  });
  // Not this one: checkTheTeam catches its own failures and answers false, so
  // wrapping it again would be a catch that can never run.
  $("teamCheck").addEventListener("click", async () => {
    const fine = await checkTheTeam();
    teamSay(fine ? "Nothing is in the way. This team would run." : "Have a look at what is in the way, below.");
  });
  $("teamSave").addEventListener("click", saveTheTeam);
  $("teamRemove").addEventListener("click", removeTheTeam);
  document.querySelectorAll("[data-pipeline-tab]").forEach((tab) => {
    tab.addEventListener("click", () => showPipelinePane(tab.dataset.pipelineTab));
    tab.addEventListener("keydown", moveBetweenPipelineTabs);
  });
  $("pipelineNodeWhen").addEventListener("change", sayWhatTheStepChoicesMean);
  $("pipelineNodeWait").addEventListener("change", sayWhatTheStepChoicesMean);
  $("pipelineNodeTries").addEventListener("input", sayWhatTheStepChoicesMean);
  $("pipelineCodeApply").addEventListener("click", useTheTypedPipeline);
  $("pipelineCodeCopy").addEventListener("click", copyThePipelineText);
  $("pipelineStarterSearch").addEventListener("input", (event) => {
    pipelineStarterLooking = event.target.value;
    renderPipelineStarters();
  });
  $("pipelineAskRun").addEventListener("click", runWithTheAnswers);
  $("pipelineAskCancel").addEventListener("click", () => $("pipelineAskDialog").close());
  $("projectBar").addEventListener("click", () => openTheProjects($("projectSidebar").hidden));
  $("projectSidebarClose").addEventListener("click", () => openTheProjects(false));
  $("projectAdd").addEventListener("click", addAProject);
  $("projectBrowse").addEventListener("click", browseForAProject);
  $("projectSidebarHow").addEventListener("change", chooseHowTheSidebarLooks);
  // Escape closes it, the way it closes everything else that sits over a page.
  // Not when it is meant to stay: closing it then is undoing somebody's own
  // setting with a key they pressed for something else.
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || projectsSidebar === "always") return;
    if (!$("projectSidebar").hidden) { openTheProjects(false); $("projectBar").focus(); }
  });
  $("tellingAdd").addEventListener("click", addTelling);
  $("tellingKind").addEventListener("change", sayWhatItNeeds);
  $("timerAdd").addEventListener("click", addATimer);
  $("timerHowOften").addEventListener("change", sayWhatTheTimerMeans);
  $("timerCopyLine").addEventListener("click", copyTheMachineLine);
  $("talkRefresh").addEventListener("click", () => refreshTalk(talkOpen));
  wireUpPipelineView();
  wireUpTheSwarmBoard();
  window.harnessDesktop?.onFullScreenChanged?.((on) => {
    if (on) return;
    if (swarmIsFullScreen) showHowTheSwarmFillsTheScreen(false);
    if (pipelineIsFullScreen) showHowThePipelineFillsTheScreen(false);
  });
  wireUpMicrosoft();
  wireUpTheTray();
  if ($("thePageRefresh")) {
    $("thePageRefresh").addEventListener("click", refreshThePage);
    $("thePageWhich").addEventListener("change", (event) => {
      thePageFolder = event.target.value;
      refreshThePage();
    });
    $("thePageStandsSave").addEventListener("click", saveWhereItStands);
    $("thePageStands").addEventListener("input", () => renderDisclosedTextCount(
      "thePageStands", "thePageStandsCount",
      SHARED_PAGE_CHARACTER_LIMIT, "where it stands"));
    $("thePageAdd").addEventListener("click", addSomethingOfMyOwn);
    $("thePagePutAway").addEventListener("click", putThePageAway);
    $("thePage").addEventListener("toggle", () => $("thePage").open && refreshThePage());
  }
  $("swarmKeep").addEventListener("click", keepThisBoard);
  $("authorityRepairButton").addEventListener("click", useFolderAsNewLocalProject);
  $("swarmImport").addEventListener("click", () => $("swarmImportFile").click());
  $("swarmImportFile").addEventListener(
    "change", (event) => importKeptBoard(event.target.files?.[0]));
  $("talkStartAgain").addEventListener("click", startTalkingAgain);
  $("talkAskEveryone").addEventListener("click", askEveryone);
  $("talkStop").addEventListener("click", stopTalking);
  $("talkForm").addEventListener("submit", (event) => { event.preventDefault(); sendWhatIsTyped(); });
  $("talkBox").addEventListener("input", countWhatIsTyped);
  $("talkBox").addEventListener("keydown", (event) => {
    // Enter sends it, which is what everybody expects of a box like this.
    // Shift and Enter is how you write a second line.
    if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); sendWhatIsTyped(); }
  });
  $("lookupRefresh").addEventListener("click", refreshLookup);
  $("lookupWhere").addEventListener("click", () => lookSomethingUp("where-is-it"));
  $("lookupUses").addEventListener("click", () => lookSomethingUp("what-uses-it"));
  $("lookupWhat").addEventListener("click", () => lookSomethingUp("what-is-it"));
  $("teamAddCustom").addEventListener("click", openTheCustomWindow);
  $("teamAddModel").addEventListener("click", openTheModelWindow);
  $("teamCustomSave").addEventListener("click", saveTheCustomOne);
  $("teamCustomCancel").addEventListener("click", () => $("teamCustomDialog").close());
  $("teamCustomJob").addEventListener("change", sayWhatTheChoiceMeans);
  $("teamCustomAsking").addEventListener("change", sayWhatTheChoiceMeans);
  $("teamModelSave").addEventListener("click", saveTheModel);
  $("teamModelCancel").addEventListener("click", () => $("teamModelDialog").close());
  $("teamModelWayIn").addEventListener("change", sayWhatTheWayInMeans);
  $("teamModelProvider").addEventListener("change", useTheProviderDefaults);
  $("teamNodeSave").addEventListener("click", saveTeamNode);
  $("teamNodeCancel").addEventListener("click", () => $("teamNodeDialog").close());
  $("teamNodeJob").addEventListener("change", () => {
    $("teamNodeJobMeans").textContent =
      teamJobs.find((one) => one.job === $("teamNodeJob").value)?.means || "";
  });
  document.addEventListener("pointermove", moveTeamDrag);
  document.addEventListener("pointerup", endTeamDrag);
  $("refreshMemory").addEventListener("click", refreshMemory); $("memoryQuery").addEventListener("change", refreshMemory); $("memoryKind").addEventListener("change", refreshMemory); $("refreshPrompts").addEventListener("click", refreshPrompts); $("promptLeft").addEventListener("change", renderPromptCompare); $("promptRight").addEventListener("change", renderPromptCompare);
  window.addEventListener("keydown", (event) => { if (event.key === "Escape" && edgeDrag) { event.preventDefault(); finishEdgeDrag({pointerId: edgeDrag.pointerId}, true); } else if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "z" && !$("agentDialog").open) { event.preventDefault(); undo(); } });
}

/* ==========================================================================
   Your team: who is on this machine, and how they work together.

   The picture is an ordinary saved workflow, so a team is something the rest
   of the harness already knows how to run. What is different here is that the
   boxes are people rather than jobs: each one says who does it, and the panel
   only ever offers assistants it really found on this machine.
   ========================================================================== */

let teamGraph = {schema_version: 2, name: "Your team", entry: "start", nodes: [], edges: []};
let teamWho = [];              // the assistants found on this machine
let teamJobs = [];             // the jobs a box can be given
let teamSavedNames = [];       // the teams already written down
let teamOpen = "";             // the saved team on screen, if it came from one
let teamJoining = "";          // the box an arrow is being drawn from
let teamDragging = null;       // the box a pointer is moving
let teamNodeOpen = "";         // the box whose settings window is open
let teamNextId = 1;

function teamSay(words) {
  $("teamSaid").textContent = words;
  announce(words);
}

async function refreshTeam(name) {
  try {
    const asked = name ? `?name=${encodeURIComponent(name)}` : "";
    const said = await request(`/api/who-is-on-it${asked}`);
    teamWho = said.who?.members || [];
    localModels = said.who?.on_this_machine || [];
    teamJobs = said.jobs || [];
    teamWaysOfAsking = said.ways_of_asking || [];
    teamWaysIn = said.ways_in || [];
    teamModelProviders = said.model_providers || [];
    teamSavedNames = said.teams || [];
    $("teamWhoNote").textContent = said.who?.note || "";
    renderTeamWho();
    renderLocalModels();
    renderTeamSaved();
    renderTeamJobs();
    if (said.gone) {
      // Removed here, or in an editor, since this page last looked.
      teamOpen = "";
      teamSay(`${said.gone} is not here any more.`);
      useTheStartingTeam(said.starting_team);
      return;
    }
    if (said.open) {
      teamOpen = said.open.name;
      $("teamName").value = said.open.name;
      teamGraph = laidOut(said.open.graph);
      renderTeam();
      await checkTheTeam();
      return;
    }
    if (!teamGraph.nodes.length) useTheStartingTeam(said.starting_team);
    else renderTeam();
  } catch (error) { showError(error.message); $("teamSaid").textContent = error.message; }
}

function useTheStartingTeam(starting) {
  if (!starting) return;
  teamGraph = laidOut(starting);
  teamOpen = "";
  $("teamName").value = "Your team";
  renderTeam();
  const ready = teamWho.filter((one) => one.ready).length;
  teamSay(ready > 1
    ? "This is the ready-made team: one assistant plans and reads the work back, another writes it."
    : "This is the ready-made team. Only one assistant is ready here, so it does every job for now.");
  checkTheTeam();
}

// A saved workflow says nothing about where its boxes sit, so anything without
// a place gets one: left to right in the order the work reaches them.
function laidOut(graph) {
  const copy = structuredClone(graph || {});
  copy.nodes = (copy.nodes || []).map((node, spot) => ({
    ...node,
    at: node.at || {x: 30 + spot * 235, y: 30 + (spot % 2) * 155},
  }));
  copy.edges = copy.edges || [];
  teamNextId = copy.nodes.length + copy.edges.length + 1;
  return copy;
}

function renderTeamWho() {
  const list = $("teamWho");
  list.replaceChildren();
  if (!teamWho.length) {
    list.append(make("li", "hint", "Nothing was found yet. Press Look again."));
    return;
  }
  for (const one of teamWho) {
    const row = make("li", `team-who-one ${one.ready ? "ready" : "not-ready"}`);
    row.dataset.who = one.route;
    row.append(make("strong", "", one.label));
    let state = one.ready
      ? (one.already_set_up ? "Installed and set up." : "Installed. It is not set up yet.")
      : (one.why_not || "Not on this machine.");
    if (one.connection_state === "authenticated") {
      state = one.already_set_up
        ? "Command-line sign-in confirmed; connected and ready."
        : "Command-line sign-in confirmed; ready to connect.";
    } else if (one.connection_state === "needs-login") {
      state = "Installed, but its separate command line needs sign-in.";
    } else if (one.ready && one.connection_state === "installed") {
      state += " Sign-in will be verified by its first request.";
    }
    row.append(make("p", "team-who-state", state));
    if (one.version) row.append(make("p", "hint", one.version));
    if (one.connection_state === "needs-login" && one.can_login) {
      const signIn = make("button", "team-sign-in", "Open sign-in");
      signIn.type = "button";
      signIn.addEventListener("click", () => signInThisAssistant(one.kind, signIn));
      row.append(signIn);
    }
    if (one.ready && one.kind === "gemini-cli") {
      const projectHelp = make("button", "team-project-help", "Find Cloud project ID");
      projectHelp.type = "button";
      projectHelp.addEventListener("click", showGeminiProjectHelp);
      row.append(projectHelp);
    }
    if (!one.ready && one.install_hint) row.append(make("p", "hint", one.install_hint));
    list.append(row);
  }
}

function renderTeamSaved() {
  const list = $("teamSaved");
  list.replaceChildren();
  if (!teamSavedNames.length) {
    list.append(make("li", "hint", "None saved yet."));
    return;
  }
  for (const one of teamSavedNames) {
    const row = make("li", "team-saved-one");
    const open = make("button", "link", one.name);
    open.type = "button";
    open.dataset.team = one.name;
    open.addEventListener("click", () => refreshTeam(one.name));
    row.append(open);
    row.append(make("span", "hint", `${one.nodes} boxes, ${one.edges} arrows`));
    if (!one.valid) row.append(make("span", "team-broken", "will not run"));
    list.append(row);
  }
}

function renderTeamJobs() {
  const box = $("teamJobs");
  box.replaceChildren();
  for (const job of teamJobs) {
    const button = make("button", "team-job-add", job.label);
    button.type = "button";
    button.dataset.job = job.job;
    button.title = job.means;
    button.addEventListener("click", () => addToTheTeam(job));
    box.append(button);
  }
}

function addToTheTeam(job) {
  const ready = teamWho.filter((one) => one.ready);
  if (!ready.length) {
    teamSay("No assistant on this machine is ready yet, so there is nobody to give a job to.");
    return;
  }
  const id = `who-${teamNextId++}`;
  const who = ready[teamGraph.nodes.length % ready.length];
  teamGraph.nodes.push({
    id,
    type: job.job,
    label: `${who.label} ${job.label.toLowerCase()}`,
    config: {provider_route: who.route},
    at: {
      x: 30 + (teamGraph.nodes.length % 4) * 235,
      y: 30 + Math.floor(teamGraph.nodes.length / 4) * 155,
    },
  });
  renderTeam();
  checkTheTeam();
  teamSay(`Added ${job.label}. Press Connect on one box, then another, to hand work between them.`);
}

function renderTeam() {
  const box = $("teamNodes");
  const wires = $("teamWires");
  box.replaceChildren(wires);
  for (const node of teamGraph.nodes) {
    const job = teamJobs.find((one) => one.job === node.type);
    const card = make("div", `team-node kind-${node.type}`);
    card.dataset.node = node.id;
    card.style.left = `${node.at?.x || 0}px`;
    card.style.top = `${node.at?.y || 0}px`;
    card.append(make("strong", "", node.label || node.id));
    if (job) card.append(make("p", "team-node-job", job.label));
    const route = (node.config || {}).provider_route || "";
    if (route) {
      const known = teamWho.find((one) => one.route === route);
      const who = make("p", "team-node-who", known ? known.label : route);
      if (!known || !known.ready) who.classList.add("not-ready");
      card.append(who);
    } else if (job) {
      card.append(make("p", "team-node-who not-ready", "Nobody chosen"));
    }
    const settings = node.config || {};
    if (settings.asking === "conversation") {
      card.append(make("p", "team-node-asking", "Stops here to talk"));
    } else if (settings.system_prompt) {
      const said = String(settings.system_prompt);
      card.append(make("p", "team-node-prompt",
        said.length > 60 ? `${said.slice(0, 60)}...` : said));
    }
    if (settings.model) card.append(make("p", "team-node-model", settings.model));
    const buttons = make("div", "team-node-buttons");
    const join = make("button", "team-node-button", teamJoining === node.id ? "Joining" : "Connect");
    join.type = "button";
    join.title = "Draw an arrow from this one to another";
    join.addEventListener("click", (event) => { event.stopPropagation(); joinTeamNodes(node.id); });
    buttons.append(join);
    if (job) {
      const settings = make("button", "team-node-button", "Settings");
      settings.type = "button";
      settings.addEventListener("click", (event) => { event.stopPropagation(); openTeamNode(node.id); });
      const remove = make("button", "team-node-button", "Remove");
      remove.type = "button";
      remove.addEventListener("click", (event) => { event.stopPropagation(); removeTeamNode(node.id); });
      buttons.append(settings, remove);
    }
    card.append(buttons);
    card.tabIndex = 0;
    card.setAttribute("role", "group");
    card.setAttribute("aria-label",
      `${node.label || node.id}${job ? `, ${job.label}` : ""}. Arrow keys move it. `
      + "C connects, S for settings, Delete removes it.");
    card.addEventListener("keydown", (event) => teamKey(event, node));
    card.addEventListener("pointerdown", (event) => startTeamDrag(event, node));
    card.addEventListener("click", () => { if (teamJoining && teamJoining !== node.id) joinTeamNodes(node.id); });
    box.append(card);
  }
  drawTeamWires();
}

function drawTeamWires() {
  const wires = $("teamWires");
  wires.replaceChildren();
  for (const edge of teamGraph.edges) {
    const from = teamGraph.nodes.find((node) => node.id === edge.source);
    const to = teamGraph.nodes.find((node) => node.id === edge.target);
    if (!from || !to) continue;
    const x1 = (from.at?.x || 0) + 200, y1 = (from.at?.y || 0) + 40;
    const x2 = (to.at?.x || 0), y2 = (to.at?.y || 0) + 40;
    const middle = (x1 + x2) / 2;
    const line = document.createElementNS("http://www.w3.org/2000/svg", "path");
    line.setAttribute("d", `M ${x1} ${y1} C ${middle} ${y1}, ${middle} ${y2}, ${x2} ${y2}`);
    line.setAttribute("class", edge.condition ? "team-wire only-when" : "team-wire");
    wires.append(line);
    if (edge.condition) {
      const words = document.createElementNS("http://www.w3.org/2000/svg", "text");
      words.setAttribute("x", String(middle));
      words.setAttribute("y", String((y1 + y2) / 2 - 12));
      words.setAttribute("class", "team-wire-words");
      words.textContent = edge.condition.includes("!=") ? "if it is not right yet" : "if it is right";
      wires.append(words);
    }
    // The cross in the middle of an arrow takes it out: the thing you want
    // rid of is the thing you press.
    const cut = document.createElementNS("http://www.w3.org/2000/svg", "circle");
    cut.setAttribute("cx", String(middle));
    cut.setAttribute("cy", String((y1 + y2) / 2));
    cut.setAttribute("r", "9");
    cut.setAttribute("class", "team-cut");
    cut.addEventListener("click", () => {
      teamGraph.edges = teamGraph.edges.filter((one) => one !== edge);
      renderTeam();
      checkTheTeam();
      teamSay(`Took the arrow from ${from.label} to ${to.label} out.`);
    });
    const cross = document.createElementNS("http://www.w3.org/2000/svg", "text");
    cross.setAttribute("x", String(middle));
    cross.setAttribute("y", String((y1 + y2) / 2 + 4));
    cross.setAttribute("class", "team-cut-mark");
    cross.textContent = "x";
    wires.append(cut, cross);
  }
}

function joinTeamNodes(nodeId) {
  if (!teamJoining) {
    teamJoining = nodeId;
    renderTeam();
    teamSay("Press another box to finish the arrow.");
    return;
  }
  if (teamJoining === nodeId) {
    teamJoining = "";
    renderTeam();
    teamSay("Stopped joining.");
    return;
  }
  const already = teamGraph.edges.some((edge) => edge.source === teamJoining && edge.target === nodeId);
  if (!already) {
    teamGraph.edges.push({
      id: `hand-${teamNextId++}`,
      source: teamJoining,
      target: nodeId,
      variables: ["task"],
    });
  }
  teamJoining = "";
  renderTeam();
  checkTheTeam();
  teamSay(already ? "That arrow is already there." : "They can hand work along that arrow now.");
}

function removeTeamNode(nodeId) {
  teamGraph.nodes = teamGraph.nodes.filter((node) => node.id !== nodeId);
  teamGraph.edges = teamGraph.edges.filter((edge) => edge.source !== nodeId && edge.target !== nodeId);
  if (teamJoining === nodeId) teamJoining = "";
  renderTeam();
  checkTheTeam();
  teamSay("Took that one off the team.");
}

function teamKey(event, node) {
  const step = event.shiftKey ? 40 : 10;
  const moves = {
    ArrowLeft: [-step, 0], ArrowRight: [step, 0],
    ArrowUp: [0, -step], ArrowDown: [0, step],
  };
  if (moves[event.key]) {
    event.preventDefault();
    node.at = {
      x: Math.max(0, (node.at?.x || 0) + moves[event.key][0]),
      y: Math.max(0, (node.at?.y || 0) + moves[event.key][1]),
    };
    renderTeam();
    keepTeamFocus(node.id);
    teamSay(`${node.label} is at ${Math.round(node.at.x)}, ${Math.round(node.at.y)}.`);
    return;
  }
  const key = event.key.toLowerCase();
  if (key === "c") { event.preventDefault(); joinTeamNodes(node.id); keepTeamFocus(node.id); return; }
  if (key === "s") { event.preventDefault(); openTeamNode(node.id); return; }
  if (event.key === "Delete" || event.key === "Backspace") {
    event.preventDefault();
    removeTeamNode(node.id);
    const first = $("teamNodes").querySelector(".team-node");
    if (first) first.focus();
  }
}

// Redrawing builds new boxes, and the keyboard would otherwise be left on
// nothing at all.
function keepTeamFocus(nodeId) {
  const card = $("teamNodes").querySelector(`[data-node="${CSS.escape(nodeId)}"]`);
  if (card) card.focus();
}

function startTeamDrag(event, node) {
  if (event.target.closest("button")) return;
  const card = event.currentTarget;
  const box = $("teamCanvas").getBoundingClientRect();
  teamDragging = {
    node,
    grabX: event.clientX - box.left - (node.at?.x || 0),
    grabY: event.clientY - box.top - (node.at?.y || 0),
  };
  card.setPointerCapture(event.pointerId);
  card.classList.add("moving");
}

function moveTeamDrag(event) {
  if (!teamDragging) return;
  const box = $("teamCanvas").getBoundingClientRect();
  const node = teamDragging.node;
  node.at = {
    x: Math.max(0, event.clientX - box.left - teamDragging.grabX),
    y: Math.max(0, event.clientY - box.top - teamDragging.grabY),
  };
  const card = $("teamNodes").querySelector(`[data-node="${CSS.escape(node.id)}"]`);
  if (card) { card.style.left = `${node.at.x}px`; card.style.top = `${node.at.y}px`; }
  drawTeamWires();
}

function endTeamDrag() {
  if (!teamDragging) return;
  const card = $("teamNodes").querySelector(`[data-node="${CSS.escape(teamDragging.node.id)}"]`);
  if (card) card.classList.remove("moving");
  teamDragging = null;
}

/* ---- one box's settings ---- */

function openTeamNode(nodeId) {
  const node = teamGraph.nodes.find((one) => one.id === nodeId);
  if (!node) return;
  teamNodeOpen = nodeId;
  $("teamNodeLabel").value = node.label || "";
  const who = $("teamNodeWho");
  who.replaceChildren();
  for (const one of teamWho) {
    const option = make("option", "", one.ready ? one.label : `${one.label} (not ready)`);
    option.value = one.route;
    option.disabled = !one.ready;
    who.append(option);
  }
  who.value = (node.config || {}).provider_route || "";
  const jobs = $("teamNodeJob");
  jobs.replaceChildren();
  for (const job of teamJobs) {
    const option = make("option", "", job.label);
    option.value = job.job;
    jobs.append(option);
  }
  jobs.value = node.type;
  $("teamNodeJobMeans").textContent = teamJobs.find((one) => one.job === node.type)?.means || "";
  $("teamNodeSummary").textContent =
    "The one that reads the work back is best not being the one that wrote it.";
  $("teamNodeDialog").showModal();
}

function saveTeamNode() {
  const node = teamGraph.nodes.find((one) => one.id === teamNodeOpen);
  if (!node) { $("teamNodeDialog").close(); return; }
  node.label = $("teamNodeLabel").value.trim() || node.label;
  node.type = $("teamNodeJob").value;
  node.config = {...(node.config || {}), provider_route: $("teamNodeWho").value};
  $("teamNodeDialog").close();
  renderTeam();
  checkTheTeam();
  teamSay(`${node.label} is set.`);
}

/* ---- checking, saving, removing ---- */

async function checkTheTeam() {
  try {
    const said = await request("/api/who-is-on-it/check", {
      method: "POST",
      body: JSON.stringify({team: forSaving()}),
    });
    const problems = $("teamProblems");
    problems.replaceChildren();
    if (!(said.problems || []).length) {
      problems.append(make("li", "team-ok", "Nothing. This team would run."));
    } else {
      for (const one of said.problems) problems.append(make("li", "team-problem", one));
    }
    const plain = $("teamPlain");
    plain.replaceChildren();
    for (const hand of (said.plain?.hand_overs || [])) {
      const words = hand.only_when
        ? `${hand.who} passes ${hand.what} back to ${hand.to_whom}, but only when the work is not right yet.`
        : `${hand.who} passes ${hand.what} to ${hand.to_whom}.`;
      plain.append(make("li", "team-plain-one", words));
    }
    if (said.plain?.note) plain.append(make("li", "team-plain-note", said.plain.note));
    return (said.problems || []).length === 0;
  } catch (error) { showError(error.message); return false; }
}

// The panel keeps where each box sits; the harness does not care. Both are
// written down, because a team that forgot its own layout every time it was
// opened would be a picture nobody could keep tidy.
function forSaving() {
  return {
    schema_version: teamGraph.schema_version || 2,
    name: $("teamName").value.trim() || "Your team",
    entry: teamGraph.entry || "start",
    nodes: teamGraph.nodes.map((node) => ({...node})),
    edges: teamGraph.edges.map((edge) => ({...edge})),
  };
}

async function saveTheTeam() {
  const name = $("teamName").value.trim();
  if (!name) { teamSay("Give the team a name first."); return; }
  try {
    const said = await request("/api/who-is-on-it/save", {
      method: "POST",
      body: JSON.stringify({name, team: forSaving(), was: teamOpen}),
    });
    teamOpen = said.team?.name || name;
    teamSavedNames = said.teams || teamSavedNames;
    renderTeamSaved();
    teamSay(`${teamOpen} is saved. Anything that runs a workflow can run it.`);
  } catch (error) { showError(error.message); $("teamSaid").textContent = error.message; }
}

async function removeTheTeam() {
  const name = teamOpen || $("teamName").value.trim();
  if (!name) { teamSay("There is nothing saved to remove."); return; }
  if (!window.confirm(`Remove the team called ${name}?`)) return;
  try {
    const said = await request("/api/who-is-on-it/remove", {
      method: "POST",
      body: JSON.stringify({name}),
    });
    teamOpen = "";
    teamSavedNames = said.teams || [];
    renderTeamSaved();
    teamSay(said.note || `${name} was removed.`);
  } catch (error) { showError(error.message); $("teamSaid").textContent = error.message; }
}

// Setting them up is the seats setup, which is the only thing that writes
// routes. Doing it twice in two places is how two answers start to disagree.
async function setUpTheTeam() {
  $("teamSetUp").disabled = true;
  teamSay("Setting up every assistant that is really here. This runs their own tools, so give it a moment.");
  try {
    const said = await request("/api/seats/setup", {method: "POST", body: "{}"});
    teamSay(said.note || "Set up.");
    await refreshTeam(teamOpen);
  } catch (error) { showError(error.message); $("teamSaid").textContent = error.message; }
  finally { $("teamSetUp").disabled = false; }
}

/* ---- one of your own -----------------------------------------------------
   The ready-made jobs cover the usual team. This is for everything else: your
   own name for a box, your own model behind it, and your own way of asking -
   one set prompt, or a conversation the run stops for. */

let teamWaysOfAsking = [];
let teamWaysIn = [];
let teamModelProviders = [];

function fillTeamChoices() {
  const jobs = $("teamCustomJob");
  jobs.replaceChildren();
  for (const job of teamJobs) {
    const option = make("option", "", job.label);
    option.value = job.job;
    jobs.append(option);
  }
  const asking = $("teamCustomAsking");
  asking.replaceChildren();
  for (const way of teamWaysOfAsking) {
    const option = make("option", "", way.label);
    option.value = way.asking;
    asking.append(option);
  }
  const who = $("teamCustomWho");
  who.replaceChildren();
  for (const one of teamWho) {
    const option = make("option", "", one.ready ? one.label : `${one.label} (not ready)`);
    option.value = one.route;
    option.disabled = !one.ready;
    who.append(option);
  }
  sayWhatTheChoiceMeans();
}

function sayWhatTheChoiceMeans() {
  $("teamCustomJobMeans").textContent =
    teamJobs.find((one) => one.job === $("teamCustomJob").value)?.means || "";
  const way = teamWaysOfAsking.find((one) => one.asking === $("teamCustomAsking").value);
  $("teamCustomAskingMeans").textContent = way?.means || "";
  // A conversation has no set prompt to write, so the box for one steps aside
  // rather than sitting there asking to be filled in for no reason.
  const talking = $("teamCustomAsking").value === "conversation";
  $("teamCustomPrompt").placeholder = talking
    ? "What to open the conversation with. You can say the rest while it runs."
    : "Read the change and say whether it really does what was asked.";
}

function openTheCustomWindow() {
  // It opens either way. On a machine with nothing set up yet the window still
  // shows what it would offer, and says plainly why it cannot be saved -
  // refusing to open at all leaves somebody guessing what they are missing.
  const anybody = teamWho.some((one) => one.ready);
  fillTeamChoices();
  $("teamCustomLabel").value = "";
  $("teamCustomModel").value = "";
  $("teamCustomPrompt").value = "";
  renderSystemPromptCount("teamCustomPrompt", "teamCustomPromptCount");
  $("teamCustomSaid").textContent = anybody
    ? ""
    : "No assistant on this machine is ready yet, so there is nobody to give this job to. "
      + "Set one up on the first screen, or add a model of your own.";
  $("teamCustomSave").disabled = !anybody;
  $("teamCustomDialog").showModal();
}

async function saveTheCustomOne() {
  const one = {
    label: $("teamCustomLabel").value.trim(),
    job: $("teamCustomJob").value,
    asking: $("teamCustomAsking").value,
    prompt: $("teamCustomPrompt").value,
    route: $("teamCustomWho").value,
    model: $("teamCustomModel").value.trim(),
  };
  if (!one.label) { $("teamCustomSaid").textContent = "Give it a name first."; return; }
  const promptProblem = systemPromptProblem(
    "teamCustomPrompt", "teamCustomPromptCount"
  );
  if (promptProblem) {
    $("teamCustomSaid").textContent = promptProblem;
    $("teamCustomPrompt").focus();
    return;
  }
  if (one.asking === "set-prompt" && !one.prompt.trim()) {
    $("teamCustomSaid").textContent =
      "A box with one set prompt needs the prompt written down, or choose a conversation instead.";
    return;
  }
  const id = `who-${teamNextId++}`;
  teamGraph.nodes.push({
    id,
    type: one.job,
    label: one.label,
    config: {
      provider_route: one.route,
      ...(one.model ? {model: one.model} : {}),
      asking: one.asking,
      system_prompt: one.prompt,
    },
    at: {
      x: 30 + (teamGraph.nodes.length % 4) * 235,
      y: 30 + Math.floor(teamGraph.nodes.length / 4) * 155,
    },
  });
  $("teamCustomDialog").close();
  renderTeam();
  await checkTheTeam();
  teamSay(one.asking === "conversation"
    ? `${one.label} is on the team. The run will stop there so you can talk to it.`
    : `${one.label} is on the team.`);
}

/* ---- a model of your own ---- */

function openTheModelWindow() {
  const ways = $("teamModelWayIn");
  ways.replaceChildren();
  for (const way of teamWaysIn.filter((one) => one.way_in !== "seat")) {
    const option = make("option", "", way.label);
    option.value = way.way_in;
    ways.append(option);
  }
  $("teamModelRoute").value = "";
  $("teamModelModel").value = "";
  $("teamModelSaid").textContent = "";
  sayWhatTheWayInMeans();
  $("teamModelDialog").showModal();
}

function sayWhatTheWayInMeans() {
  const selectedWay = $("teamModelWayIn").value;
  const way = teamWaysIn.find((one) => one.way_in === selectedWay);
  $("teamModelWayMeans").textContent = way?.means || "";
  // Only one of the two needs a key, and asking for one where it cannot be
  // used is how somebody ends up pasting a key that nothing reads.
  const needsAKey = selectedWay === "with-a-key";
  $("teamModelKeyName").closest("form").querySelectorAll("label").forEach((label) => {
    if (label.getAttribute("for") === "teamModelKeyName") label.hidden = !needsAKey;
  });
  $("teamModelKeyName").hidden = !needsAKey;
  $("teamModelKeyHelp").hidden = !needsAKey;

  const choices = teamModelProviders.filter(
    (one) => Array.isArray(one.ways_in) && one.ways_in.includes(selectedWay));
  const providers = $("teamModelProvider");
  providers.replaceChildren();
  for (const choice of choices) {
    const option = make("option", "", choice.label);
    option.value = choice.kind;
    providers.append(option);
  }
  useTheProviderDefaults();
}

function useTheProviderDefaults() {
  const provider = teamModelProviders.find(
    (one) => one.kind === $("teamModelProvider").value);
  $("teamModelProviderMeans").textContent = provider?.means || "";
  $("teamModelEndpoint").value = provider?.default_endpoint || "";
  $("teamModelEndpoint").placeholder = provider?.default_endpoint || "https://provider.example/v1";
  $("teamModelModel").placeholder = provider?.model_hint || "model-name";
  const needsAKey = $("teamModelWayIn").value === "with-a-key";
  $("teamModelKeyName").value = needsAKey ? (provider?.default_key_name || "") : "";
  $("teamModelKeyName").placeholder = provider?.default_key_name || "PROVIDER_API_KEY";
}

async function saveTheModel() {
  try {
    const said = await request("/api/who-is-on-it/add-a-model", {
      method: "POST",
      body: JSON.stringify({
        model: {
          route: $("teamModelRoute").value.trim(),
          way_in: $("teamModelWayIn").value,
          provider: $("teamModelProvider").value,
          model: $("teamModelModel").value.trim(),
          endpoint: $("teamModelEndpoint").value.trim(),
          key_name: $("teamModelKeyName").value.trim(),
        },
      }),
    });
    $("teamModelDialog").close();
    await refreshTeam(teamOpen);
    teamSay(said.note || "The model was added.");
    if (said.needs_your_say) {
      // The same choice the seat setup puts in front of somebody, in the same
      // window, rather than a second way of asking the same question.
      switchView("start");
      showTheChoiceAboutTrusting(said);
    }
  } catch (error) {
    $("teamModelSaid").textContent = error.message;
  }
}

/* ==========================================================================
   The pipelines view, with the parts Kestra gets right.

   Four ways of looking at the same pipeline, side by side rather than one
   instead of the other: the picture, the same thing as text you can edit, a
   timeline of the last run, and what every kind of step is for. Plus the two
   things people ask for the first afternoon: start again from the step that
   broke, and ask me a couple of questions before you run.
   ========================================================================== */

let pipelineLooking = "board";     // which of the four panels is on screen
let pipelineStarterLooking = "";   // what is typed in the gallery search
let pipelineWaitingAt = "";        // the step that has stopped to ask

function showPipelinePane(which) {
  pipelineLooking = which;
  document.querySelectorAll("[data-pipeline-tab]").forEach((tab) => {
    const selected = tab.dataset.pipelineTab === which;
    tab.setAttribute("aria-selected", String(selected));
    tab.tabIndex = selected ? 0 : -1;
  });
  document.querySelectorAll("[data-pipeline-pane]").forEach((pane) => {
    pane.hidden = pane.dataset.pipelinePane !== which;
  });
  if (which === "code") writeThePipelineOut();
  if (which === "timeline") drawThePipelineTimeline();
  if (which === "help") listWhatEachStepIsFor();
  if (which === "before") listHowItLookedBefore();
  if (which === "timer") refreshTimers();
  if (which === "telling") refreshTelling();
}

function moveBetweenPipelineTabs(event) {
  const tabs = [...document.querySelectorAll("[data-pipeline-tab]")];
  const at = tabs.indexOf(event.currentTarget);
  let next = at;
  if (event.key === "ArrowRight" || event.key === "ArrowDown") next = (at + 1) % tabs.length;
  else if (event.key === "ArrowLeft" || event.key === "ArrowUp") next = (at - 1 + tabs.length) % tabs.length;
  else if (event.key === "Home") next = 0;
  else if (event.key === "End") next = tabs.length - 1;
  else return;
  event.preventDefault();
  const target = tabs[next];
  showPipelinePane(target.dataset.pipelineTab);
  target.focus();
}

/* ---- the same thing as text ---- */

function writeThePipelineOut() {
  $("pipelineCode").value = JSON.stringify(forSavingThePipeline(), null, 2);
  $("pipelineCodeSaid").textContent =
    "Change anything here, then press Use what I typed. Nothing is saved until you press Save.";
}

// The picture and the text are one pipeline, so this is the one place that
// says what that pipeline is. Both panels read it.
function forSavingThePipeline() {
  return {
    name: $("pipelineName").value.trim() || pipeline.name || "First pipeline",
    nodes: pipeline.nodes.map((node) => ({...node})),
    edges: pipeline.edges.map((edge) => ({...edge})),
  };
}

async function useTheTypedPipeline() {
  let read;
  try {
    read = JSON.parse($("pipelineCode").value);
  } catch (error) {
    $("pipelineCodeSaid").textContent = `That is not readable as text a pipeline is written in: ${error.message}`;
    return;
  }
  try {
    // Checked by the harness, not by the page: the page believing something
    // would run is not the same as it running.
    const said = await request("/api/pipelines/check", {
      method: "POST",
      body: JSON.stringify({pipeline: read}),
    });
    pipeline = said.pipeline;
    $("pipelineName").value = pipeline.name;
    renderPipeline();
    $("pipelineCodeSaid").textContent = "The picture now shows what you typed.";
    say("The picture now shows what you typed.");
  } catch (error) {
    $("pipelineCodeSaid").textContent = error.message;
  }
}

async function copyThePipelineText() {
  try {
    await navigator.clipboard.writeText($("pipelineCode").value);
    $("pipelineCodeSaid").textContent = "Copied.";
  } catch (error) {
    // A browser that will not copy for us is not a failure worth shouting
    // about; the text is on the screen and can be selected.
    $("pipelineCodeSaid").textContent = "This browser would not copy it. Select it and copy it yourself.";
  }
}

/* ---- how long each step took ---- */

function drawThePipelineTimeline() {
  const list = $("pipelineTimeline");
  list.replaceChildren();
  const steps = [...pipelineStates.values()].filter((one) => !one.skipped_this_time);
  if (!steps.length) {
    list.append(make("li", "hint", "Nothing has run yet. Press Run and this fills in."));
    return;
  }
  const longest = Math.max(
    1,
    ...steps.map((one) => (one.started_after || 0) + (one.milliseconds || 0)),
  );
  for (const one of steps) {
    const row = make("li", `pipeline-timeline-row ${one.state}`);
    row.dataset.step = one.id;
    row.append(make("span", "pipeline-timeline-name", one.label || one.id));
    const track = make("div", "pipeline-timeline-track");
    const bar = make("div", `pipeline-timeline-bar ${one.state}`);
    const from = ((one.started_after || 0) / longest) * 100;
    const wide = Math.max(1.5, ((one.milliseconds || 0) / longest) * 100);
    bar.style.marginLeft = `${from}%`;
    bar.style.width = `${Math.min(100 - from, wide)}%`;
    bar.title = `${one.label}: ${prettyTime(one.milliseconds || 0)}`;
    track.append(bar);
    row.append(track);
    row.append(make("span", "pipeline-timeline-time", prettyTime(one.milliseconds || 0)));
    list.append(row);
  }
  const slowest = steps.reduce((worst, one) =>
    (one.milliseconds || 0) > (worst.milliseconds || 0) ? one : worst, steps[0]);
  list.append(make("li", "pipeline-timeline-note",
    `The slowest step was ${slowest.label}, at ${prettyTime(slowest.milliseconds || 0)}.`));
}

function prettyTime(milliseconds) {
  if (milliseconds < 1000) return `${milliseconds} ms`;
  if (milliseconds < 60000) return `${(milliseconds / 1000).toFixed(1)} seconds`;
  return `${Math.floor(milliseconds / 60000)} min ${Math.round((milliseconds % 60000) / 1000)} s`;
}

/* ---- what each step is for ---- */

function listWhatEachStepIsFor() {
  const list = $("pipelineHelpList");
  list.replaceChildren();
  for (const kind of pipelineKinds) {
    const row = make("li", "pipeline-help-one");
    row.append(make("strong", "", kind.label));
    row.append(make("span", "pipeline-help-group", kind.group));
    row.append(make("p", "", kind.summary));
    if ((kind.settings || []).length) {
      row.append(make("p", "hint", `It can be told: ${kind.settings.join(", ")}.`));
    }
    list.append(row);
  }
}

/* ---- how it looked before ----
   Saving over a pipeline used to lose the old one. Now each save keeps it, and
   this is where they are: what changed, when, and a button to bring it back. */

let pipelineOlderOnes = [];

function listHowItLookedBefore() {
  const list = $("pipelineOlderOnes");
  list.replaceChildren();
  if (!pipelineSavedName) {
    list.append(make("li", "hint",
      "This pipeline has not been saved yet, so there is nothing before it."));
    return;
  }
  if (!pipelineOlderOnes.length) {
    list.append(make("li", "hint",
      `${pipelineSavedName} has only been saved once. The next time you save, what is `
      + "here now is kept."));
    return;
  }
  for (const [where, one] of pipelineOlderOnes.entries()) {
    const row = make("li", "pipeline-older-one");
    row.append(make("strong", "", where === 0 ? "The one before this" : `${where + 1} saves ago`));
    row.append(make("p", "", one.what_changed || "nothing that shows here"));
    row.append(make("p", "hint",
      `${one.steps} steps, ${one.arrows} arrows. Saved ${one.saved_at}.`));
    const back = make("button", "", "Put this one back");
    back.type = "button";
    back.dataset.older = String(where);
    back.addEventListener("click", () => putAnOldOneBack(where));
    row.append(back);
    list.append(row);
  }
}

async function putAnOldOneBack(which) {
  if (!await mayReplacePipeline("restoring an older saved version")) return;
  if (!window.confirm(
    "Put this older version back? What is on the board now is kept too, so you can "
    + "swap back again.")) return;
  const beforeRequest = currentPipelineSnapshot();
  try {
    const said = await request("/api/pipelines/put-one-back", {
      method: "POST",
      body: JSON.stringify({name: pipelineSavedName, which}),
    });
    if (currentPipelineSnapshot() !== beforeRequest) {
      pipelineOlderOnes = said.older_ones || [];
      say("The older version was restored in the saved library, but the drawing changed meanwhile, so Nexus kept your newer edits. Open the saved automation when you are ready.");
      return;
    }
    pipeline = said.pipeline;
    pipelineOlderOnes = said.older_ones || [];
    $("pipelineName").value = pipeline.name;
    rememberPipelineBaseline();
    renderPipeline();
    listHowItLookedBefore();
    say(said.note || "Put it back.");
  } catch (error) { showError(error.message); say(error.message); }
}

/* ---- the gallery, and searching it ---- */

function pipelineStartersShown() {
  const looking = pipelineStarterLooking.trim().toLowerCase();
  if (!looking) return pipelineStarters;
  return pipelineStarters.filter((one) => {
    const words = [one.title, one.when, one.group, ...(one.found_by || [])].join(" ").toLowerCase();
    return words.includes(looking);
  });
}

/* ---- asking before a run ---- */

// Everything this pipeline said it would ask about, as one flat list.
function whatThisPipelineAsks() {
  const asked = [];
  for (const node of pipeline.nodes) {
    for (const name of (node.settings?.asks || [])) {
      asked.push({
        key: `${node.id}.${name}`,
        step: node.label || node.id,
        setting: name,
        now: node.settings?.[name] ?? "",
      });
    }
  }
  return asked;
}

async function runPipelineAsking(options = {}) {
  const asked = whatThisPipelineAsks();
  if (!asked.length || options.answers) {
    await runPipeline(options);
    return;
  }
  const fields = $("pipelineAskFields");
  fields.replaceChildren();
  for (const one of asked) {
    const label = make("label", "", `${one.step}: ${one.setting}`);
    label.setAttribute("for", `ask-${one.key}`);
    const box = make("input");
    box.id = `ask-${one.key}`;
    box.type = "text";
    box.value = String(one.now || "");
    box.dataset.askKey = one.key;
    fields.append(label, box);
  }
  $("pipelineAskDialog").showModal();
}

async function runWithTheAnswers() {
  const answers = {};
  $("pipelineAskFields").querySelectorAll("[data-ask-key]").forEach((box) => {
    answers[box.dataset.askKey] = box.value;
  });
  $("pipelineAskDialog").close();
  await runPipeline({answers});
}

/* ---- answering a step that stopped to ask ---- */

function showWhatIsBeingAsked(step) {
  pipelineWaitingAt = step || "";
  const box = $("pipelineAsk");
  box.replaceChildren();
  if (!step) { box.hidden = true; return; }
  const state = pipelineStates.get(step);
  const node = pipeline.nodes.find((one) => one.id === step);
  box.hidden = false;
  box.append(make("strong", "", (node && node.label) || step));
  box.append(make("p", "", (state && state.said) || "This step is waiting for you."));
  const buttons = make("div", "button-row");
  const yes = make("button", "primary", "Carry on");
  yes.type = "button";
  yes.addEventListener("click", () => answerTheStep(step, true));
  const no = make("button", "", "Stop here");
  no.type = "button";
  no.addEventListener("click", () => answerTheStep(step, false));
  buttons.append(yes, no);
  box.append(buttons);
  announce(`${(node && node.label) || step} is waiting for you.`);
}

async function answerTheStep(step, carryOn) {
  try {
    if (!pipelineActiveRunId || pipelineProjectionRunId !== pipelineActiveRunId) {
      throw new Error("This question no longer belongs to the exact run shown here. Refresh before answering.");
    }
    const said = await request("/api/pipelines/answer", {
      method: "POST",
      body: JSON.stringify({run_id: pipelineActiveRunId, step, carry_on: carryOn}),
    });
    showWhatIsBeingAsked("");
    say(said.note || "Answered.");
  } catch (error) { showError(error.message); }
}

/* ==========================================================================
   Being told when a run finishes.

   The one part of the harness that cannot work on its own: every way of this
   needs a key, a token or an address somebody has to go and get. So the panel
   says that before anything else, says which ones are ready and which are
   waiting, and never asks anybody to type a secret into it - only the name of
   the variable holding one.
   ========================================================================== */

let tellingKinds = [];

async function refreshTelling() {
  try {
    const said = await request("/api/telling");
    tellingKinds = said.kinds || [];
    fillOneChoice("tellingKind", tellingKinds, "kind",
                  $("tellingKind").value || (tellingKinds[0] || {}).kind || "");
    sayWhatItNeeds();
    renderTelling(said.ways || []);
  } catch (error) { showError(error.message); sayAboutTelling(error.message); }
}

function sayAboutTelling(words) { $("tellingSaid").textContent = words; }

function theKindPicked() {
  return tellingKinds.find((one) => one.kind === $("tellingKind").value) || {};
}

function sayWhatItNeeds() {
  const one = theKindPicked();
  $("tellingWhatItNeeds").textContent = one.kind
    ? `${one.label} needs ${one.secret_is}. ${one.where_to_get_one}`
    : "";
  if (one.usually_called && !$("tellingSecretIn").value) {
    $("tellingSecretIn").value = one.usually_called;
  }
  // Emptied when it is not asked for, so a value typed for one kind does not
  // travel with the next one.
  $("tellingWhereHolder").hidden = !one.needs_a_server;
  if (!one.needs_a_server) $("tellingWhere").value = "";
  else if (one.server_usually_called && !$("tellingWhere").value) {
    $("tellingWhere").value = one.server_usually_called;
  }
  $("tellingToHolder").hidden = !one.needs_to;
  $("tellingSentFromHolder").hidden = !one.needs_sent_from;
}

function renderTelling(ways) {
  const list = $("tellingList");
  list.replaceChildren();
  if (!ways.length) {
    const empty = make("li", "telling-one empty-state");
    empty.append(make("p", "", "Nobody is told yet. Fill in the boxes above."));
    list.append(empty);
    return;
  }
  for (const one of ways) {
    const row = make("li", `telling-one ${one.ready ? "ready" : "waiting"}`);
    row.append(make("strong", "", one.name));
    row.append(make("p", "", `${one.label}, key kept in ${one.secret_in}`));
    row.append(make("p", one.ready ? "hint" : "telling-waiting",
      one.ready ? "Ready." : one.why_not));
    const buttons = make("div", "button-row");
    const tryIt = make("button", "", "Send one now");
    tryIt.type = "button";
    tryIt.disabled = !one.ready;
    tryIt.title = one.ready
      ? "Say hello, so you can see it arrive"
      : "There is no key for this one yet";
    tryIt.addEventListener("click", () => tryTelling(one, tryIt));
    const off = make("button", "", "Take it off");
    off.type = "button";
    off.addEventListener("click", () => removeTelling(one));
    buttons.append(tryIt, off);
    row.append(buttons);
    list.append(row);
  }
}

async function addTelling() {
  const name = $("tellingName").value.trim();
  if (!name) { sayAboutTelling("Give it a name first."); return; }
  try {
    const said = await request("/api/telling/save", {
      method: "POST",
      body: JSON.stringify({way: {
        name,
        kind: $("tellingKind").value,
        secret_in: $("tellingSecretIn").value.trim(),
        server_in: $("tellingWhereHolder").hidden ? "" : $("tellingWhere").value.trim(),
        to: $("tellingTo").value.trim(),
        sent_from: $("tellingSentFrom").value.trim(),
        turned_on: true,
      }}),
    });
    $("tellingName").value = "";
    await refreshTelling();
    sayAboutTelling(said.why_not
      ? `${name} is set up. ${said.why_not}`
      : `${name} is set up and ready.`);
  } catch (error) { showError(error.message); sayAboutTelling(error.message); }
}

async function tryTelling(one, button) {
  button.disabled = true;
  sayAboutTelling(`Saying hello to ${one.name}...`);
  try {
    const said = await request("/api/telling/try", {
      method: "POST", body: JSON.stringify({name: one.name}),
    });
    sayAboutTelling(said.note);
  } catch (error) {
    showError(error.message);
    sayAboutTelling(error.message);
  } finally { button.disabled = false; }
}

async function removeTelling(one) {
  if (!window.confirm(`Stop telling ${one.name}?`)) return;
  try {
    const said = await request("/api/telling/remove", {
      method: "POST", body: JSON.stringify({name: one.name}),
    });
    await refreshTelling();
    sayAboutTelling(said.note || "Taken off.");
  } catch (error) { showError(error.message); sayAboutTelling(error.message); }
}

/* ==========================================================================
   When an automation runs on its own.

   The harness does not sit in the background waiting for two in the morning.
   The machine is asked to look every so often, and it handles being asleep and
   being restarted. This panel says what is on a timer, when each one is next
   due, what it did last time, and gives you the one line to hand your machine.
   ========================================================================== */

let timers = [];
let timerHowOften = [];
let timerMachine = {};

function executionPauseWords(kind, reason) {
  const why = String(reason || "").trim();
  return why ? `${kind} is paused: ${why}` : "";
}

function setExecutionControl(button, ordinarilyDisabled, reason, ordinaryTitle = "",
                             kind = "Project execution", describedById = "authorityRepairReason") {
  if (!button) return;
  const pause = executionPauseWords(kind, reason);
  button.disabled = Boolean(ordinarilyDisabled) || Boolean(pause);
  button.title = pause || ordinaryTitle;
  if (pause && describedById) button.setAttribute("aria-describedby", describedById);
  else button.removeAttribute("aria-describedby");
}

async function refreshTimers() {
  try {
    const said = await request("/api/timers");
    timers = said.timers || [];
    timerHowOften = said.how_often || [];
    timerMachine = said.how_to_ask_this_machine || {};
    pipelineCannotRun = String(said.cannot_run || "");
    showProjectAuthorityPause(said.authority, pipelineCannotRun);
    fillOneChoice("timerAutomation", (said.automations || []).map((one) => ({name: one, label: one})), "name",
                  $("timerAutomation").value || (said.automations || [])[0] || "");
    fillOneChoice("timerHowOften", timerHowOften, "how_often",
                  $("timerHowOften").value || "every-day");
    fillOneChoice("timerOnDay", (said.days || []).map((one) => ({day: one, label: one[0].toUpperCase() + one.slice(1)})),
                  "day", $("timerOnDay").value || "monday");
    sayWhatTheTimerMeans();
    renderTimers();
    setExecutionControl($("timerAdd"), false, pipelineCannotRun,
      "Put this automation on a timer", "Automation execution");
    $("timerMachineLine").textContent = timerMachine.what || "";
    $("timerMachineOff").textContent = timerMachine.to_take_it_off || "";
    if (said.could_not_be_read) sayAboutTimers(said.could_not_be_read);
    if (!(said.automations || []).length) {
      sayAboutTimers("Save an automation first, then it can be put on a timer.");
    }
  } catch (error) { showError(error.message); sayAboutTimers(error.message); }
}

function sayAboutTimers(words) { $("timerSaid").textContent = words; }

function sayWhatTheTimerMeans() {
  const picked = $("timerHowOften").value;
  const one = timerHowOften.find((held) => held.how_often === picked);
  $("timerHowOftenMeans").textContent = one ? one.means : "";
  // A day of the week only means anything for a weekly one, and a time of day
  // means nothing at all for an hourly one.
  $("timerOnDayHolder").hidden = picked !== "every-week";
  $("timerAt").disabled = picked === "every-hour";
}

function renderTimers() {
  const list = $("timerList");
  list.replaceChildren();
  if (!timers.length) {
    list.append(make("li", "hint",
      "Nothing runs on its own yet. Fill in the boxes above and press Put it on a timer."));
    return;
  }
  for (const one of timers) {
    const row = make("li", `timer-one ${one.turned_on ? "on" : "off"}`);
    row.append(make("strong", "", one.name));
    row.append(make("p", "", `${one.automation} — ${one.in_plain_words.toLowerCase()}`));
    row.append(make("p", "hint", one.turned_on ? `Next: ${one.next_run}` : "Turned off."));
    const last = (one.runs || [])[one.runs.length - 1];
    if (last) {
      const missed = last.missed
        ? ` (${last.missed >= 1000 ? "more than 1000" : last.missed} missed while the machine was off)`
        : "";
      row.append(make("p", `timer-last ${last.passed ? "passed" : "failed"}`,
        `Last ran ${last.at}: ${last.passed ? "passed" : "did not pass"}${missed}. ${last.said}`));
    }
    const buttons = make("div", "button-row");
    const turn = make("button", "", one.turned_on ? "Turn it off" : "Turn it on");
    turn.type = "button";
    setExecutionControl(turn, false, one.turned_on ? "" : pipelineCannotRun,
      one.turned_on ? "Stop this timer from running" : "Let this timer run on its schedule",
      "Automation execution");
    turn.addEventListener("click", () => turnTheTimer(one, !one.turned_on));
    const now = make("button", "", "Run it now");
    now.type = "button";
    setExecutionControl(now, false, pipelineCannotRun,
      "Do what the timer would do, without waiting for it", "Automation execution");
    now.addEventListener("click", () => runTheTimerNow(one, now));
    const off = make("button", "", "Take it off");
    off.type = "button";
    off.addEventListener("click", () => takeTheTimerOff(one));
    buttons.append(turn, now, off);
    row.append(buttons);
    list.append(row);
  }
}

async function addATimer() {
  if (pipelineCannotRun) {
    sayAboutTimers(executionPauseWords("Automation execution", pipelineCannotRun));
    return;
  }
  const name = $("timerName").value.trim();
  const automation = $("timerAutomation").value;
  if (!name) { sayAboutTimers("Give the timer a name first."); return; }
  if (!automation) { sayAboutTimers("Save an automation first, then it can be put on a timer."); return; }
  // Asked before it is put on, not told after. From a terminal this is a
  // refusal you have to say --anyway to; the panel used to save it regardless
  // and mention it afterwards, which is not the same thing.
  if (!await saySoBeforeItRunsAlone(automation, "Put it on a timer anyway?")) {
    sayAboutTimers("Not put on a timer.");
    return;
  }
  // The server refuses this too, so it is told they said yes.
  await saveATimer({
    name,
    automation,
    how_often: $("timerHowOften").value,
    at: $("timerAt").value || "02:00",
    on: $("timerOnDay").value || "monday",
    turned_on: true,
  }, `${name} is on a timer.`);
  $("timerName").value = "";
}

async function saveATimer(timer, wordsWhenDone, anyway = true) {
  try {
    const said = await request("/api/timers/save", {
      method: "POST", body: JSON.stringify({timer, anyway}),
    });
    await refreshTimers();
    // Said after the refresh, so the tidy-up cannot write over it.
    // The warning belongs to a timer that is going to run. Saying it while
    // somebody turns one off is telling them about a problem they have just
    // taken away.
    if (said.why_not && timer.turned_on) {
      sayAboutTimers(`${wordsWhenDone} But: ${said.why_not}`);
    } else if (timer.turned_on) {
      sayAboutTimers(
        `${wordsWhenDone} ${said.in_plain_words}. One more step below: tell your machine to look.`);
    } else {
      sayAboutTimers(wordsWhenDone);
    }
  } catch (error) { showError(error.message); sayAboutTimers(error.message); }
}

async function whyItShouldNotRunAlone(automation) {
  try {
    const said = await request(
      `/api/pipelines/why-not-alone?name=${encodeURIComponent(automation)}`);
    return {why_not: said.why_not || "", asked: true};
  } catch (error) {
    // Could not ask is not the same as nothing wrong. Read as nothing wrong,
    // one hiccup on the way to the question turned "ask before" into "tell
    // afterwards", which is the whole point of asking.
    return {why_not: "", asked: false};
  }
}

async function saySoBeforeItRunsAlone(automation, what) {
  const {why_not, asked} = await whyItShouldNotRunAlone(automation);
  if (!asked) {
    return window.confirm(
      `The harness could not check whether ${automation} stops to ask a `
      + `person. One that does will sit there all night with nobody to `
      + `answer.\n\n${what}`);
  }
  if (!why_not) return true;
  return window.confirm(`${why_not}\n\n${what}`);
}

async function turnTheTimer(one, on) {
  if (on && pipelineCannotRun) {
    sayAboutTimers(executionPauseWords("Automation execution", pipelineCannotRun));
    return;
  }
  // Asked again. Turning one back on is putting it on a timer just as much as
  // adding it was, and the reason it should not run alone has not gone away.
  if (on && !await saySoBeforeItRunsAlone(one.automation, "Turn it on anyway?")) {
    sayAboutTimers("Left turned off.");
    return;
  }
  // Only the switch is sent. Sending the whole timer back from a panel left
  // open put back the old time and the old automation along with it, over
  // whatever somebody else had changed in the meantime.
  try {
    const said = await request("/api/timers/turn", {
      method: "POST",
      body: JSON.stringify({name: one.name, turned_on: on, anyway: true}),
    });
    await refreshTimers();
    sayAboutTimers(said.note || "");
  } catch (error) { showError(error.message); sayAboutTimers(error.message); }
}

async function takeTheTimerOff(one) {
  if (!window.confirm(`Take ${one.name} off the timer? The automation itself stays.`)) return;
  try {
    const said = await request("/api/timers/remove", {
      method: "POST", body: JSON.stringify({name: one.name}),
    });
    await refreshTimers();
    sayAboutTimers(said.note || "Taken off.");
  } catch (error) { showError(error.message); sayAboutTimers(error.message); }
}

async function runTheTimerNow(one, button) {
  if (pipelineCannotRun) {
    sayAboutTimers(executionPauseWords("Automation execution", pipelineCannotRun));
    return;
  }
  button.disabled = true;
  sayAboutTimers(`Running ${one.automation} now...`);
  try {
    const said = await request("/api/timers/run-now", {
      method: "POST", body: JSON.stringify({name: one.name}),
    });
    sayAboutTimers(`${said.passed ? "Passed" : "Did not pass"}: ${said.said}`);
  } catch (error) {
    showError(error.message);
    sayAboutTimers(error.message);
  } finally { button.disabled = false; }
}

async function copyTheMachineLine() {
  const line = $("timerMachineLine").textContent;
  try {
    await navigator.clipboard.writeText(line);
    sayAboutTimers("Copied. Paste it into a terminal and run it yourself.");
  } catch (error) {
    // Some browsers will not copy without a gesture they recognise. Select it
    // instead, so it can still be copied by hand.
    const range = document.createRange();
    range.selectNodeContents($("timerMachineLine"));
    window.getSelection().removeAllRanges();
    window.getSelection().addRange(range);
    sayAboutTimers("Selected it for you - copy it with your keyboard.");
  }
}

/* ==========================================================================
   Talking to the assistants you have hooked up.

   One box, one assistant, and the conversation kept. Nothing here can read a
   file or run anything - it is a conversation, and anything that changes the
   project goes through a run, where there is a record of it.
   ========================================================================== */

let talkWho = [];        // everyone on this machine, ready or not
let talkOpen = "";       // whose conversation is on screen
let talkBusy = false;    // waiting for an answer
let talkStopping = false;
let talkBusyRequest = null;
let talkCannotRun = "";
// Updated from the backend on every chat read.  The generous default only
// covers startup before that response lands; the server remains authoritative.
let chatLimits = {
  input_characters: 200000,
  answer_characters: 8000000,
  configured_provider_output_tokens: 65536,
  provider_capture_bytes: 100000000,
  turn_timeout_seconds: 600,
  overflow_policy: "reject_without_truncation",
};
const swarmChatLimits = new Map();

function limitsForSwarmChat(agentId) {
  return swarmChatLimits.get(String(agentId || "")) || chatLimits;
}

function outputBudgetFact(limits) {
  if (limits.output_token_control === "provider_page_uncontrolled") {
    return "the connected provider page controls its own output tokens";
  }
  if (limits.output_token_control === "nexus_requested_maximum") {
    return `${Number(limits.configured_provider_output_tokens).toLocaleString()} requested output tokens`;
  }
  return "this provider CLI/service controls its own output tokens";
}

function captureBudgetFact(limits) {
  if (limits.provider_capture_policy === "provider_page_answer_bridge") {
    return `${Number(limits.answer_characters || 8000000).toLocaleString()}-character web answer bridge`;
  }
  if (limits.provider_capture_policy === "cli_plain_response_fixed") {
    return `${Number(limits.provider_capture_bytes || 2000000).toLocaleString()}-byte ordinary CLI capture; structured work uses schema-derived capture`;
  }
  return `${Number(limits.provider_capture_bytes || 100000000).toLocaleString()}-byte provider transport capture`;
}

function longHorizonContextFact(limits) {
  const policy = limits?.long_horizon_context;
  if (!policy) return "";
  return `${Number(policy.prompt_transcript_characters || 120000).toLocaleString()}-character conversation-history projection per long-horizon phase with deterministic older-turn semantic summaries; surrounding goal, project, and turn instructions are additional; full history stays in the paged ledger`;
}
// Which refresh is the newest. Looking over the machine runs each assistant's
// own tool, which takes about a second, so an old one can land long after
// somebody has moved on - and used to write its words over theirs.
let talkNewestRefresh = 0;

async function refreshTalk(who, quietly) {
  const mine = ++talkNewestRefresh;
  try {
    const asked = who === undefined ? talkOpen : who;
    const said = await request(`/api/chat?who=${encodeURIComponent(asked || "")}`);
    if (mine !== talkNewestRefresh) return;  // a newer look started while this one ran
    talkWho = said.who || [];
    talkOpen = said.open || "";
    talkCannotRun = String(said.cannot_run || "");
    showProjectAuthorityPause(said.authority, talkCannotRun);
    if (said.limits && typeof said.limits === "object") chatLimits = said.limits;
    const outputFact = outputBudgetFact(chatLimits);
    $("talkBudget").textContent =
      `Effective budget: ${Number(chatLimits.input_characters || 200000).toLocaleString()} input characters, `
      + `${outputFact}, `
      + `${Number(chatLimits.answer_characters || 8000000).toLocaleString()} answer characters, `
      + `${captureBudgetFact(chatLimits)}, `
      + `${Number(chatLimits.turn_timeout_seconds || 600).toLocaleString()} seconds. `
      + "Overflow is rejected without truncation; attach text files for larger reference material.";
    renderTalkWho();
    renderTalkThread(said.said || []);
    // Quietly, when this is a tidy-up after something the person just did.
    // Otherwise the line saying what happened is replaced, a moment later, by
    // a line saying nothing happened.
    if (!quietly && !talkBusy) {
      sayInTalk(talkTheOpenOne()
        ? `Talking to ${talkTheOpenOne().label}. Type below and press Send.`
        : "Nobody is set up to talk to yet. Open Your team and press Set them up.");
    }
    setWhatCanBePressed();
    countWhatIsTyped();
  } catch (error) {
    if (mine !== talkNewestRefresh) return;
    showError(error.message);
    sayInTalk(error.message);
  }
}

function talkTheOpenOne() {
  return talkWho.find((one) => one.ready && one.route === talkOpen)
    || talkWho.find((one) => one.ready)
    || null;
}

function sayInTalk(words) { $("talkSaid").textContent = words; }

// One place decides what can be pressed, so no path can leave a button off.
// Leaving "Ask all of them" disabled after a send that failed is exactly the
// kind of thing two places setting the same buttons produces.
function setWhatCanBePressed() {
  const somebody = talkTheOpenOne();
  $("talkBox").disabled = !somebody;
  $("talkSend").disabled = talkBusy || !somebody;
  $("talkSend").title = "Send this message to the selected assistant";
  $("talkStop").disabled = !talkBusy || talkStopping;
  $("talkStop").textContent = talkStopping ? "Stopping…" : "Stop";
  $("talkAskEveryone").disabled = talkBusy || !talkWho.some((one) => one.ready);
  $("talkAskEveryone").title = "Ask every ready assistant";
  $("talkStartAgain").disabled = talkBusy || !somebody;
  // The names on the left too, and now rather than the next time the list is
  // drawn: switching while an answer is on its way leaves "Asking them..." on
  // screen over somebody else's conversation.
  for (const pick of $("talkWho").querySelectorAll(".talk-pick")) {
    pick.disabled = talkBusy || pick.dataset.ready !== "yes";
  }
}

function renderTalkWho() {
  const list = $("talkWho");
  list.replaceChildren();
  if (!talkWho.length) {
    list.append(make("li", "hint", "Nobody found on this machine yet."));
    return;
  }
  for (const one of talkWho) {
    const row = make("li", `talk-one ${one.ready ? "ready" : "not-ready"}`
      + (one.ready && one.route === talkOpen ? " open" : ""));
    const pick = make("button", "talk-pick", one.label || one.route || "The usual one");
    pick.type = "button";
    pick.dataset.ready = one.ready ? "yes" : "no";
    pick.disabled = !one.ready;
    pick.setAttribute("aria-pressed", String(one.ready && one.route === talkOpen));
    // Not while an answer is on its way. Switching then left the answer
    // arriving under the new one's name, as if they had said it.
    pick.disabled = pick.disabled || talkBusy;
    pick.addEventListener("click", () => {
      if (talkBusy) return;
      talkOpen = one.route;
      refreshTalk(one.route);
    });
    row.append(pick);
    if (one.model) row.append(make("p", "hint", one.model));
    if (!one.ready) {
      row.append(make("p", "talk-why-not", one.why_not));
      if (one.how_to_fix_it) row.append(make("p", "hint", one.how_to_fix_it));
    }
    list.append(row);
  }
}

function renderTalkThread(said) {
  const list = $("talkThread");
  list.replaceChildren();
  if (!said.length) {
    list.append(make("li", "hint",
      "Nothing said yet. Whatever you type stays on this machine, and goes only to "
      + "the assistant you picked."));
    return;
  }
  for (const one of said) {
    const row = make("li", `talk-turn ${one.who}`);
    row.append(make("strong", "talk-turn-who",
      one.who === "you" ? "You" : (talkTheOpenOne()?.label || "Them")));
    row.append(make("p", "talk-turn-text", one.text));
    const under = [];
    if (one.at) under.push(one.at);
    if (one.milliseconds) under.push(prettyTime(one.milliseconds));
    if (one.model) under.push(one.model);
    if (under.length) row.append(make("p", "hint", under.join(" | ")));
    list.append(row);
  }
  list.lastElementChild.scrollIntoView({block: "nearest"});
}

function countWhatIsTyped() {
  const box = $("talkBox");
  const typed = box.value.length;
  const limit = Number(chatLimits.input_characters || 200000);
  $("talkCount").textContent = typed > limit
    ? `${(typed - limit).toLocaleString()} characters over the ${limit.toLocaleString()} limit — nothing will be truncated`
    : `${typed.toLocaleString()} / ${limit.toLocaleString()} characters`;
}

async function sendWhatIsTyped() {
  const box = $("talkBox");
  const words = box.value.trim();
  const one = talkTheOpenOne();
  if (!words) { sayInTalk("Type something first."); return; }
  if (words.length > Number(chatLimits.input_characters || 200000)) {
    sayInTalk("This message is over the displayed limit. Nexus did not truncate it; split it or attach a file.");
    return;
  }
  if (!one) { sayInTalk("Nobody is set up to talk to yet."); return; }
  if (talkBusy) { sayInTalk("Still waiting for the last answer."); return; }
  talkBusy = true;
  talkStopping = false;
  talkBusyRequest = {who: one.route, everyone: false};
  setWhatCanBePressed();
  sayInTalk(`Asking ${one.label}...`);
  // Shown straight away, so the words are on screen while the answer is coming.
  renderTalkThread([
    ...[...$("talkThread").querySelectorAll(".talk-turn")].map(readOneTurnBack),
    {who: "you", text: words, at: ""},
  ]);
  try {
    const said = await request("/api/chat/say", {
      method: "POST", body: JSON.stringify({who: one.route, text: words}),
    });
    if (talkOpen !== one.route) {
      // Somebody switched while this was on its way. It is kept and will be
      // there when they switch back; what it must not do is appear under
      // whoever is on screen now.
      sayInTalk(`${one.label} answered. Pick them again to read it.`);
      return;
    }
    box.value = "";
    countWhatIsTyped();
    renderTalkThread(said.said || []);
    sayInTalk(`${one.label} answered.`);
  } catch (error) {
    // Read back from what was really kept, so the message that did not go
    // through stops looking like one that did. The words stay in the box.
    // Quietly: the reason it failed is the last word, not "type below".
    await refreshTalk(one.route, true);
    sayInTalk(error.message);
    if (!stoppedChatError(error)) showError(error.message);
  } finally {
    talkBusy = false;
    talkStopping = false;
    talkBusyRequest = null;
    setWhatCanBePressed();
    renderTalkWho();
  }
}

// Reading a turn back off the screen, so what is already there survives having
// the one being typed added under it.
function readOneTurnBack(row) {
  return {
    who: row.classList.contains("you") ? "you" : "them",
    text: row.querySelector(".talk-turn-text")?.textContent || "",
    at: "",
  };
}

async function askEveryone() {
  const box = $("talkBox");
  const words = box.value.trim();
  if (!words) { sayInTalk("Type something first."); return; }
  if (talkBusy) { sayInTalk("Still waiting for the last answer."); return; }
  talkBusy = true;
  talkStopping = false;
  talkBusyRequest = {who: "", everyone: true};
  setWhatCanBePressed();
  sayInTalk("Asking every one of them at once...");
  try {
    const said = await request("/api/chat/ask-everyone", {
      method: "POST", body: JSON.stringify({text: words}),
    });
    renderWhatEveryoneSaid(said.answers || []);
    box.value = "";
    countWhatIsTyped();
    await refreshTalk(talkOpen, true);
    sayInTalk(`${(said.answers || []).length} of them were asked.`);
  } catch (error) {
    sayInTalk(error.message);
    if (!stoppedChatError(error)) showError(error.message);
  } finally {
    talkBusy = false;
    talkStopping = false;
    talkBusyRequest = null;
    setWhatCanBePressed();
    renderTalkWho();
  }
}

async function stopTalking() {
  if (!talkBusy || talkStopping || !talkBusyRequest) return;
  talkStopping = true;
  setWhatCanBePressed();
  sayInTalk("Stopping this AI request...");
  try {
    const jobs = [request("/api/chat/stop", {
      method: "POST", body: JSON.stringify(talkBusyRequest),
    })];
    const routes = talkBusyRequest.everyone
      ? talkWho.filter((one) => one.ready).map((one) => one.route)
      : [talkBusyRequest.who];
    if (window.harnessDesktop?.stopWebChat) {
      for (const route of routes.filter((one) => String(one).startsWith("web:"))) {
        jobs.push(window.harnessDesktop.stopWebChat(route));
      }
    }
    const [serverResult] = await Promise.allSettled(jobs);
    if (serverResult.status === "rejected") throw serverResult.reason;
    if (!serverResult.value?.stopped) {
      talkStopping = false;
      setWhatCanBePressed();
      sayInTalk(serverResult.value?.note || "This chat is not waiting for an answer.");
    }
  } catch (error) {
    talkStopping = false;
    setWhatCanBePressed();
    showError(String(error?.message || error));
  }
}

function renderWhatEveryoneSaid(answers) {
  const box = $("talkEveryone");
  const list = $("talkEveryoneList");
  list.replaceChildren();
  box.hidden = !answers.length;
  for (const one of answers) {
    const row = make("li", `talk-everyone-one ${one.went_wrong ? "went-wrong" : ""}`);
    row.append(make("strong", "", one.label || one.route));
    row.append(make("p", "talk-turn-text", one.went_wrong || one.answer));
    if (one.milliseconds) row.append(make("p", "hint", prettyTime(one.milliseconds)));
    list.append(row);
  }
}

async function startTalkingAgain() {
  const one = talkTheOpenOne();
  if (!one) return;
  if (!window.confirm(
    `Throw away the conversation with ${one.label}? What was said is gone for good.`)) return;
  try {
    const said = await request("/api/chat/start-again", {
      method: "POST", body: JSON.stringify({who: one.route}),
    });
    if (said.web_chat_id && said.web_conversation_key
        && window.harnessDesktop?.resetWebChat) {
      await window.harnessDesktop.resetWebChat(
        one.route, said.web_conversation_key);
    }
    renderTalkThread([]);
    $("talkEveryone").hidden = true;
    sayInTalk(said.note || "That conversation is gone.");
  } catch (error) { showError(error.message); sayInTalk(error.message); }
}

/* ==========================================================================
   Looking things up in the code.

   Where is it, what uses it, what is it. The answer says whether it is exact -
   a tool built for that language was asked - or a guess from reading the files.
   Those are different things, and only one is worth acting on without checking.
   ========================================================================== */

let lookupTools = [];

async function refreshLookup() {
  try {
    const said = await request("/api/look-up");
    lookupTools = said.servers || [];
    renderLookupTools();
  } catch (error) { showError(error.message); }
}

function renderLookupTools() {
  const list = $("lookupTools");
  list.replaceChildren();
  const ready = lookupTools.filter((one) => one.ready);
  for (const one of lookupTools) {
    const row = make("li", `lookup-tool ${one.ready ? "ready" : "not-ready"}`);
    row.append(make("strong", "", one.label));
    row.append(make("p", "", one.ready
      ? `Ready. Exact answers for ${one.for_files.join(", ")}.`
      : `Not installed. To get it: ${one.how_to_get_it}`));
    if (one.ready && one.found_at) row.append(make("p", "hint", one.found_at));
    list.append(row);
  }
  $("lookupSaid").textContent = ready.length
    ? `${ready.length} of these are installed, so answers about those files are exact.`
    : "None of these are installed yet, so answers will be a search through your files. "
      + "That is often enough, and it says so every time.";
}

async function lookSomethingUp(asking) {
  const name = $("lookupName").value.trim();
  const path = $("lookupPath").value.trim();
  if (!name && !path) {
    $("lookupSaid").textContent = "Type a name, or a file and a line, first.";
    return;
  }
  $("lookupSaid").textContent = "Looking...";
  $("lookupPlaces").replaceChildren();
  try {
    const said = await request("/api/look-up", {
      method: "POST",
      body: JSON.stringify({
        asking,
        name,
        path,
        line: Number($("lookupLine").value) || 0,
        column: Number($("lookupColumn").value) || 0,
      }),
    });
    renderLookupAnswer(said);
  } catch (error) {
    $("lookupSaid").textContent = error.message;
    showError(error.message);
  }
}

function renderLookupAnswer(said) {
  const list = $("lookupPlaces");
  list.replaceChildren();
  const places = said.places || [];
  const mark = make("p", said.exact ? "lookup-exact" : "lookup-guess",
    said.exact
      ? `Exact: ${said.how}.`
      : `A guess: ${said.how}. ${said.note || ""}`);
  $("lookupSaid").textContent = places.length
    ? `${places.length} place${places.length === 1 ? "" : "s"} found.`
    : (said.note || "Nothing found.");
  const first = make("li", "lookup-mark");
  first.append(mark);
  list.append(first);
  for (const place of places) {
    const row = make("li", "lookup-place");
    if (place.what) {
      row.append(make("strong", "", place.path ? `${place.path}:${place.line}` : "What it is"));
      row.append(make("pre", "lookup-what", place.what));
    } else {
      const open = make("button", "link", `${place.path}:${place.line}`);
      open.type = "button";
      open.title = "Put this file and line in the boxes above, to ask about it exactly";
      open.addEventListener("click", () => {
        $("lookupPath").value = place.path;
        $("lookupLine").value = String(place.line);
        $("lookupColumn").value = String(place.column || 1);
        $("lookupSaid").textContent =
          `Asking about ${place.path}:${place.line} now. Press one of the three again.`;
      });
      row.append(open);
      if (place.text) row.append(make("code", "lookup-line", place.text));
    }
    list.append(row);
  }
}

async function boot() {
  bindEvents();
  const untouchedViewRevision = userViewSelectionRevision;
  try {
    await loadNexusAppIcon();
    const value = await request("/api/bootstrap");
    token = value.token;
    startedId = value.started_id || "";
    nexusProjectName = String(value.project || "this project");
    if (value.runtime) {
      $("runtimeIdentity").textContent = `Nexus ${value.runtime.version} · ${value.runtime.commit || "unknown commit"} · Python ${value.runtime.python_version} · local port ${value.runtime.port} · process ${value.runtime.process_id}`;
      $("runtimeIdentity").title = `Build: ${value.runtime.build_kind || "unknown"}\nProject root: ${value.runtime.project_root}\nRuntime: ${value.runtime.python_executable}`;
    }
    // Event wiring happens before bootstrap, but the token-protected web-chat
    // courier cannot start until this point. Start it explicitly now instead
    // of relying on a later timer tick to rescue the first no-token attempt.
    startWebChatBridge();
    template = migrateGraph(value.template);
    graph = structuredClone(template);
    catalog = await request("/api/catalog");
    nextId = graph.nodes.length + graph.edges.length + 1;
    focusedNodeId = graph.nodes[0]?.id || "";
    render();
    renderTeamNotes();
    renderWhatItIsDoing();
    await refreshProjects();
    await validate();
    await refreshUsage();
    await loadWhatCanBeDoneForYou();
    await refreshCheckup();
    await refreshHowItWorks();
    await refreshChecks();
    restoreAuthorityRepairSuccess();
    await refreshTeamNotes();
    await refreshWorkflows();
    // Event polling must not wait behind a slow loopback read or Electron IPC
    // while the two recovery journals are inventoried below.
    pollEvents();
    // A goal request is written to two durable journals before the composer is
    // cleared. If admission was interrupted, make that recovery the first
    // visible workspace after restart. Merely inventorying it is read-only:
    // provider work still requires the explicit reconcile button below.
    const directGoalRecoveryState = await refreshDirectLongGoalRecoveries();
    if (directGoalRecoveryState.hasPending) {
      if (userViewSelectionRevision === untouchedViewRevision) {
        // Recovery-only hydration draws the saved board and chats but never
        // resumes an unrelated board run or advances its durable goal queue.
        switchView("swarm", {recoveryOnly: true});
      }
      announce(
        "A saved project goal needs your attention. Open its exact chat or reconcile the saved request.",
      );
    } else if (directGoalRecoveryState.inventoryError) {
      showError(directGoalRecoveryState.inventoryError);
    }
  } catch (error) {
    showError(error.message);
  }
}

// ---- the board of agents -------------------------------------------------
//
// One picture of every agent you have, every project you want worked on, and
// the lines between them. The other tabs each show one of those things; this
// shows all of it at once, and lets you change any of it.
//
// Everything is changed where it is. Every box carries its own gear and its
// own chat button. Every line carries a gear, because "may these two talk" is
// a fact about the line and asking somebody to find it in a side panel is
// asking them to hold the picture in their head. The side panel is what the
// gear opens, not the only way in.
//
// Lines are never dragged between boxes. A line you have to aim at is a line
// somebody with a trackpad cannot draw, and a gear that says YES or NO out
// loud beats an arrow that only implies it.
//
// Nothing here starts anything by itself. Adding an agent, turning a line on
// and writing a job down all change the board and nothing else. "Set them
// going", further down, is the one part that reaches an assistant, and only
// when somebody presses it.

let swarmSaid = {
  board: {agents: [], projects: [], works_on: [], talks_to: []},
  who_can_be_used: [],
  projects_on_this_machine: [],
  most: {agents: 24, projects: 12, tasks: 40},
  what_is_not_ready: [],
};
// What is picked: a box, or one of the lines. Lines are picked as
// {kind: "works", agent, project} or {kind: "talks", one, other}.
let swarmPicked = null;
// Runtime ownership is per exact saved chat, never per lead agent. One lead
// can therefore have Chat 1 and Chat 2 in flight at the same time without a
// late answer, Stop press, or progress poll crossing between them.
let swarmBusy = new Set();   // canonical `chat:<id>` (or legacy-agent) runtime keys waiting now
let swarmStopping = new Set(); // exact chat stops already being delivered
let swarmNewestRefresh = 0;  // so a slow look cannot overwrite a newer one
// Connection recovery belongs to the selected agent and exact route. Board
// redraws must not erase a diagnosis or a successful live verification, and a
// delayed answer for an old route must never decorate the newly selected one.
const swarmAgentRepairPlans = new Map();
const swarmAgentRepairTests = new Map();
// The value above is deliberately an empty placeholder until /api/swarm has
// answered.  Startup also starts the web-chat heartbeat.  That heartbeat may
// add connected chats to the board, so it must never mistake the placeholder
// for the durable board and save a two-agent replacement over somebody's work.
let swarmBoardHydrated = false;
// The chats open on the board, in the order they were opened. Kept here rather
// than written down with the board: which boxes you have open is about this
// window and this minute, and two windows should not fight over it.
let swarmChats = [];
const swarmChatAttachments = new Map();
// Compact cards share one DOM composer per lead agent as the selected saved
// chat changes. Keep the actual draft and caret per exact chat so jumping away
// from an in-flight turn cannot let its delayed completion erase a sibling's
// draft.
const swarmChatComposerDrafts = new Map();
const swarmChatComposerKeys = new Map();
// A maximised composer belongs to one exact pair-chat. Keeping its draft and
// caret outside the rendered DOM means conversation switches, board redraws,
// and full-screen reparenting cannot turn typing into lost or misplaced text.
const theBigChatComposerDrafts = new Map();
let theBigChatComposerKey = "";
// Round policy belongs to the exact saved pair chat, just like attachments.
// New chats stop the legacy relay after three rounds. Unlimited remains an
// explicit advanced opt-in; the engine's no-progress guard still applies.
const swarmChatRoundPolicies = new Map();
const DEFAULT_FINITE_TEAM_ROUNDS = 3;
// A paused or not-yet-verified project run belongs to one exact saved pair
// chat. Keep its opaque resume token and mechanically enforced destinations
// outside the rendered cards so minimising, maximising, switching chats, or
// reloading the panel cannot strand work which the server says is resumable.
const SWARM_WORK_RECOVERIES_KEY = "nexus.swarm.work-recoveries.v1";
const SWARM_RECOVERABLE_WORK_STATUSES = new Set([
  "paused_provider", "paused_for_user", "paused_tool_budget", "incomplete",
  "applied_unverified", "needs_verification",
]);
const swarmWorkRecoveries = loadSwarmWorkRecoveries();
let swarmWorkRecoveryRefreshRevision = 0;
// The authenticated backend journal is the admission authority. The desktop
// main process owns a bounded, atomic pre-network outbox in app userData so a
// random loopback port or renderer restart cannot strand the exact payload.
// Attachment bytes never enter localStorage, and replay always needs an
// explicit recovery press.
const directLongGoalRecoveries = new Map(); // exact chat id -> public pending metadata
const directLongGoalRecoveryBusy = new Set();
// Exact pre-network payload text is loaded only after the user presses Open
// exact saved chat. Keep that bounded read in renderer memory (never
// localStorage), and retain attachment metadata rather than base64 bytes.
const directLongGoalRecoveryPayloadViews = new Map();
let directLongGoalRecoveryRefreshRevision = 0;
let directLongGoalRecoveryError = "";
let directLongGoalRecoveryInventoryReady = false;
// A long provider request remains one HTTP call, but its truthful Nexus stages
// arrive through a second, lightweight activity feed.  This map lets both the
// movable and maximised views show the same live state.
const swarmChatActivity = new Map(); // exact chat key -> live/terminal activity
const AGENT_ICONS = new Set(["robot", "person", "code", "star", "brain"]);
const AGENT_COLOUR = /^#[0-9a-f]{6}$/i;
const AGENT_PICTURE = /^data:image\/(?:png|jpeg|webp);base64,/i;
const MAX_AGENT_PICTURE_LENGTH = 400000;
let nexusAppIconDataUrl = "";
let swarmAgentPictureDraft = "";
const SWARM_AGENT_AUTOSAVE_DELAY = 450;
// Unsaved form state lives outside the rendered board. Every successful board
// change redraws this panel, so keeping a draft only in its inputs allowed an
// unrelated drag or checkbox save to replace what somebody was still typing.
// Drafts are per agent so switching cards can flush one without losing another.
const swarmAgentSettingDrafts = new Map();

function safeAgentPicture(value) {
  return typeof value === "string" && value.length <= MAX_AGENT_PICTURE_LENGTH
    && AGENT_PICTURE.test(value) ? value : "";
}

function agentAppearance(agent) {
  const bubble = AGENT_COLOUR.test(agent?.bubble_colour || "")
    ? agent.bubble_colour : "#173b49";
  const rgb = [1, 3, 5].map((at) => parseInt(bubble.slice(at, at + 2), 16));
  const luminance = (0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]) / 255;
  return {
    icon: AGENT_ICONS.has(agent?.icon) ? agent.icon : "robot",
    colour: AGENT_COLOUR.test(agent?.colour || "") ? agent.colour : "#52d5ea",
    bubble,
    bubbleInk: luminance > 0.58 ? "#07151d" : "#f4fbff",
    picture: safeAgentPicture(agent?.profile_picture),
    pictureZoom: Math.max(100, Math.min(300, Number(agent?.picture_zoom) || 100)),
    pictureHue: Math.max(0, Math.min(360, Number(agent?.picture_hue) || 0)),
  };
}

function styleForAgent(node, agent) {
  const appearance = agentAppearance(agent);
  node.style.setProperty("--agent-colour", appearance.colour);
  node.style.setProperty("--agent-bubble-colour", appearance.bubble);
  node.style.setProperty("--agent-bubble-ink", appearance.bubbleInk);
  return appearance;
}

function putAgentFaceIn(node, appearance, iconSize = 24) {
  node.replaceChildren();
  node.style.setProperty("--agent-picture-zoom", String(appearance.pictureZoom / 100));
  node.style.setProperty("--agent-picture-hue", `${appearance.pictureHue}deg`);
  if (appearance.picture) {
    const picture = document.createElement("img");
    picture.src = appearance.picture;
    picture.alt = "";
    picture.draggable = false;
    node.append(picture);
  } else {
    node.append(aSwarmDrawing(appearance.icon, iconSize));
  }
  node.classList.toggle("has-picture", Boolean(appearance.picture));
  return node;
}

function anAgentFace(agent, className = "", iconSize = 24) {
  const face = make("span", `swarm-agent-face ${className}`.trim());
  const appearance = styleForAgent(face, agent);
  return putAgentFaceIn(face, appearance, iconSize);
}

async function loadNexusAppIcon() {
  if (typeof window.harnessDesktop?.appIconDataUrl !== "function") return "";
  try {
    const icon = String(await window.harnessDesktop.appIconDataUrl() || "");
    nexusAppIconDataUrl = /^data:image\/x-icon;base64,/i.test(icon) ? icon : "";
  } catch (_error) {
    nexusAppIconDataUrl = "";
  }
  return nexusAppIconDataUrl;
}

function isNexusChatTurn(one) {
  // Older saved failure turns predate speaker_id. Their visible attribution is
  // still authoritative enough to keep Nexus from borrowing the selected
  // agent's appearance when those conversations are reopened.
  return one?.speaker_id === "nexus"
    || (one?.speaker_name === "Nexus" && one?.recipient_name === "You");
}

function aChatTurnFace(one, agent, className = "", iconSize = 24) {
  if (!isNexusChatTurn(one)) return anAgentFace(agent, className, iconSize);
  const face = make("span", `swarm-agent-face nexus-app-face ${className}`.trim());
  const icon = nexusAppIconDataUrl;
  if (/^data:image\/x-icon;base64,/i.test(icon)) {
    const picture = document.createElement("img");
    picture.src = icon;
    picture.alt = "Nexus Harness";
    picture.draggable = false;
    face.append(picture);
  } else {
    // Browser-only development has no packaged desktop asset. It still gets a
    // distinct Nexus mark instead of borrowing the connected agent's face.
    face.append(aSwarmDrawing("star", iconSize));
  }
  return face;
}

function agentForChatTurn(one, fallback) {
  if (isNexusChatTurn(one)) return null;
  return theSwarmAgent(one?.speaker_id) || fallback || null;
}

function chatTurnsWhileWorking(agentId, saved = []) {
  const activity = visibleSwarmChatActivity(swarmChatActivityFor(agentId));
  if (!activity) return saved;
  const identity = (one) => JSON.stringify([
    one?.who || "", one?.speaker_id || "", one?.phase || "", one?.text || "",
  ]);
  const alreadySaved = new Set(saved.map(identity));
  const live = [
    ...(activity.localTurns || []),
    ...(activity.remoteTurns || []),
  ].filter((one) => !alreadySaved.has(identity(one)));
  return [
    ...saved,
    ...live,
  ];
}

function renderTurnsThatArrived(agentId, chatKey = swarmChatRuntimeKey(agentId)) {
  // A canonical pair chat can be open through either participant. Refresh
  // every visible view of this exact chat, not just the agent which sent it.
  for (const held of swarmChats) {
    if (swarmChatRuntimeKey(held.agent) !== chatKey) continue;
    renderTheChatThreadFor(held.agent, keptTranscriptFor(held.agent));
    if (theBigOne === held.agent) renderTheBigChat();
  }
}
// A chat read can still be in flight when an answer or "start again" changes
// the transcript.  Only the newest operation may replace the copy both chat
// views render; otherwise a slow, older read makes a fresh answer disappear.
const swarmChatRevisions = new Map();
// Conversation metadata is fetched independently from its transcript. Keep a
// latest-only counter for those reads, and briefly lock selection while the
// server records it, so an older list can never restore a previous chat.
const swarmConversationListRevisions = new Map();
const swarmConversationSwitching = new Set();
// Metadata-only refreshes can race the first full chat hydration. Preserve the
// strongest requested operation so a later list read cannot win the revision
// race and accidentally suppress the only transcript read.
const swarmConversationTranscriptRefreshes = new Set();
// Conversation metadata arrives after a board card is first drawn. Until its
// saved chat ID is known, drafting/navigation are safe but any provider or
// lifecycle action would run under the temporary legacy agent identity.
const swarmConversationHydrating = new Set();
// Metadata and transcript fetches are independent renderer-owned lanes.
// Routine same-lane overlap is revision-guarded rather than aborted, so a
// metadata read cannot strand transcript intent (or vice versa). Closing the
// card or changing boards aborts every tracked request in both lanes.
const swarmConversationListControllers = new Map();
const swarmConversationTranscriptControllers = new Map();
let swarmConversationBoardVersion = null;
// Reset is a lifecycle mutation on one exact saved chat. It may continue in
// Electron after the server transcript reset has returned, so keep a separate
// exact-chat lease until both halves have finished.
const swarmChatResetting = new Set();

function activeConversationFor(agentId) {
  const held = swarmChats.find((one) => one.agent === agentId);
  return (held?.conversations || []).find((one) => one.id === held?.conversation) || null;
}

function isLoneAgentChat(agentId) {
  const conversation = activeConversationFor(agentId);
  if (!conversation) return false;
  const pair = Array.isArray(conversation.pair_agents) && conversation.pair_agents.length
    ? conversation.pair_agents : conversation.pair;
  return Array.isArray(pair) && pair.length === 1;
}

function chatRecipientWords(agentId) {
  const selected = theSwarmAgent(agentId);
  const conversation = activeConversationFor(agentId);
  let participants = Array.isArray(conversation?.pair_agents)
    ? conversation.pair_agents.filter((one) => one && (one.id || one.name)) : [];
  if (!participants.length && Array.isArray(conversation?.pair)) {
    participants = conversation.pair.map((id) => {
      const found = theSwarmAgent(id);
      return found || {id, name: String(id || "Agent")};
    });
  }
  if (!participants.length && selected) participants = [selected];
  const selectedName = String(selected?.name || "selected agent");
  const expected = Math.max(1, participants.length || 1);
  const direct = `Ask ${selectedName} only`;
  const team = expected === 2 ? "Ask both agents"
    : expected > 2 ? `Ask all ${expected} agents` : "Ask connected agents";
  const help = expected > 1
    ? `${direct} sends to one assistant. ${team} starts team collaboration; expected initial replies: ${expected}.`
    : `${direct} sends to this one assistant. Add a connected agent before starting team collaboration.`;
  return {direct, team, help, expected, selectedName};
}

function syncChatRecipientWords(agentId, card = null) {
  const words = chatRecipientWords(agentId);
  if (card) {
    const send = card.querySelector(".swarm-chat-send");
    const team = card.querySelector(".swarm-chat-collaborate");
    const help = card.querySelector(".swarm-chat-scope");
    if (send) {
      send.textContent = words.direct;
      send.setAttribute("aria-label", words.direct);
    }
    if (team) {
      team.textContent = words.team;
      team.setAttribute("aria-label", words.team);
    }
    if (help) help.textContent = words.help;
  }
  if (theBigOne === agentId) {
    const send = $("theBigChatSend");
    const team = $("theBigChatCollaborate");
    const help = $("theBigChatScopeHint");
    if (send) {
      send.textContent = words.direct;
      send.setAttribute("aria-label", words.direct);
    }
    if (team) {
      team.textContent = words.team;
      team.setAttribute("aria-label", words.team);
    }
    if (help) help.textContent = words.help
      + " Project-file work always asks for confirmation.";
  }
  return words;
}

function unavailableChatParticipants(agentId) {
  const conversation = activeConversationFor(agentId);
  const pair = Array.isArray(conversation?.pair_agents) && conversation.pair_agents.length
    ? conversation.pair_agents
    : (Array.isArray(conversation?.pair) ? conversation.pair.map((id) => ({id})) : []);
  return pair.map((saved) => {
    const current = theSwarmAgent(saved?.id);
    // Readiness is live board state. A participant removed after this chat was
    // saved is unavailable even if older conversation metadata said otherwise.
    return current || {...saved, ready: false};
  }).filter((one) => !one?.ready);
}

function fillChatTeamReadiness(panel, agentId) {
  if (!panel) return [];
  const unavailable = unavailableChatParticipants(agentId);
  panel.hidden = !unavailable.length;
  panel.replaceChildren();
  if (!unavailable.length) return unavailable;
  panel.append(make("strong", "", "Team action unavailable"));
  panel.append(make("p", "hint",
    `${unavailable.map((one) => one.name || one.id || "An agent").join(", ")} ${unavailable.length === 1 ? "is" : "are"} not ready. Repair the exact connection before asking the team.`));
  const actions = make("div", "button-row chat-team-readiness-actions");
  for (const one of unavailable) {
    const repair = make("button", "swarm-repair", `Repair ${one.name || "agent"}`);
    repair.type = "button";
    repair.title = one.who
      ? `Diagnose ${one.name || "this agent"}'s exact route: ${one.who}`
      : `Choose a provider route for ${one.name || "this agent"}`;
    repair.addEventListener("click", () => void openAgentRepairFlow(one.id, repair));
    actions.append(repair);
  }
  panel.append(actions);
  return unavailable;
}

function syncChatTeamReadiness(agentId, card = null) {
  const unavailable = unavailableChatParticipants(agentId);
  if (card) fillChatTeamReadiness(
    card.querySelector(".swarm-chat-team-readiness"), agentId,
  );
  if (theBigOne === agentId) fillChatTeamReadiness($("theBigChatTeamReadiness"), agentId);
  return unavailable;
}

function loneAgentActionMessage(mode) {
  return mode === "work"
    ? "This is a lone-agent chat. Project-file teamwork is available only in a connected-agent chat."
    : "This is a lone-agent chat. Open a connected-agent chat before asking agents to collaborate.";
}

function swarmChatKeyFor(agentId, chatId = undefined) {
  const exact = chatId === undefined
    ? activeConversationFor(agentId)?.id || ""
    : String(chatId || "");
  return `${agentId}:${exact || "legacy"}`;
}

function swarmChatKey(agentId) {
  return swarmChatKeyFor(agentId);
}

function swarmChatRuntimeKeyFor(agentId, chatId = undefined) {
  const exact = chatId === undefined
    ? activeConversationFor(agentId)?.id || ""
    : String(chatId || "");
  // A saved pair chat can be opened through either participant. Runtime
  // ownership follows the canonical chat ID so both views observe the same
  // in-flight turn; unsaved legacy chats fall back to their one agent owner.
  return exact ? `chat:${exact}` : `legacy:${agentId}`;
}

function swarmChatRuntimeKey(agentId) {
  return swarmChatRuntimeKeyFor(agentId);
}

function swarmChatIsBusy(agentId, chatId = undefined) {
  return swarmBusy.has(swarmChatRuntimeKeyFor(agentId, chatId));
}

function swarmChatIsStopping(agentId, chatId = undefined) {
  return swarmStopping.has(swarmChatRuntimeKeyFor(agentId, chatId));
}

function swarmChatIsResetting(agentId, chatId = undefined) {
  return swarmChatResetting.has(swarmChatRuntimeKeyFor(agentId, chatId));
}

function swarmChatIsHydrating(agentId) {
  return swarmConversationHydrating.has(agentId);
}

function swarmChatActivityFor(agentId, chatId = undefined) {
  return swarmChatActivity.get(swarmChatRuntimeKeyFor(agentId, chatId));
}

function visibleSwarmChatActivity(activity) {
  // A terminal feed result remains in the runtime map as a non-rendered
  // reconciliation tombstone until the original HTTP request settles. Keeping
  // that transport identity must not keep its progress card or optimistic
  // transcript turns visible.
  return activity?.collapsed ? null : activity;
}

function swarmActivityIsCurrent(activity) {
  return Boolean(activity?.chatKey && swarmChatActivity.get(activity.chatKey) === activity);
}

function swarmActivityCanSettle(activity) {
  return swarmActivityIsCurrent(activity) && activity.settled !== true;
}

function swarmActivityCanReconcileSuccess(activity) {
  return swarmActivityIsCurrent(activity) && (
    activity.settled !== true
    || (activity.settledBy === "feed" && activity.terminalState === "complete")
  );
}

function selectedChatIs(activity) {
  return Boolean(activity && swarmChats.some(
    (held) => swarmChatRuntimeKey(held.agent) === activity.chatKey,
  ));
}

function rememberSwarmChatComposer(agentId) {
  const card = theChatCardFor(agentId);
  const box = card?.querySelector(".swarm-chat-box");
  if (!box) return null;
  const key = swarmChatComposerKeys.get(agentId) || swarmChatKey(agentId);
  const state = {
    value: box.value,
    start: box.selectionStart,
    end: box.selectionEnd,
    direction: box.selectionDirection || "none",
  };
  swarmChatComposerDrafts.set(key, state);
  swarmChatComposerKeys.set(agentId, key);
  return state;
}

function syncSwarmChatComposer(agentId) {
  const card = theChatCardFor(agentId);
  const box = card?.querySelector(".swarm-chat-box");
  if (!box) return;
  const nextKey = swarmChatKey(agentId);
  const previousKey = swarmChatComposerKeys.get(agentId) || "";
  if (previousKey === nextKey) return;
  const wasFocused = document.activeElement === box;
  rememberSwarmChatComposer(agentId);
  const legacyKey = swarmChatKeyFor(agentId, "");
  const state = swarmChatComposerDrafts.get(nextKey)
    || (previousKey === legacyKey ? swarmChatComposerDrafts.get(previousKey) : null);
  if (state && previousKey === legacyKey && !swarmChatComposerDrafts.has(nextKey)) {
    swarmChatComposerDrafts.set(nextKey, state);
    swarmChatComposerDrafts.delete(previousKey);
  }
  swarmChatComposerKeys.set(agentId, nextKey);
  box.value = state?.value || "";
  if (state) box.setSelectionRange(state.start, state.end, state.direction);
  countWhatIsTypedTo(agentId);
  if (wasFocused) box.focus({preventScroll: true});
}

function restoreSwarmChatDraft(chatKey, words) {
  const existing = swarmChatComposerDrafts.get(chatKey);
  if (!existing?.value) {
    swarmChatComposerDrafts.set(chatKey, {
      value: words, start: words.length, end: words.length, direction: "none",
    });
  }
  const activity = [...swarmChatActivity.values()].find(
    (one) => one.stateKey === chatKey,
  );
  // syncSwarmChatComposer deliberately returns early when the identity did not
  // change. Feed failure restores the same identity, so refresh that visible
  // textarea explicitly instead of waiting for a switch or full redraw.
  const held = swarmChats.find((one) => swarmChatKey(one.agent) === chatKey);
  const box = held ? theChatCardFor(held.agent)?.querySelector(".swarm-chat-box") : null;
  if (box && !box.value) {
    box.value = words;
    box.setSelectionRange(words.length, words.length);
    rememberSwarmChatComposer(held.agent);
    countWhatIsTypedTo(held.agent);
  } else if (activity && selectedChatIs(activity)) {
    syncSwarmChatComposer(activity.agentId);
  }
}

function restoreSwarmActivityDraft(activity) {
  const words = String(activity?.localTurns?.find(
    (one) => one?.who === "you" && String(one?.text || "").trim()
  )?.text || "");
  if (!activity?.stateKey || !words) return;
  restoreSwarmChatDraft(activity.stateKey, words);
  const existing = theBigChatComposerDrafts.get(activity.stateKey);
  if (!existing?.value) {
    theBigChatComposerDrafts.set(activity.stateKey, {
      value: words, start: words.length, end: words.length, direction: "none",
    });
  }
  if (theBigOne && swarmChatKey(theBigOne) === activity.stateKey
      && !$("theBigChatBox").value) {
    $("theBigChatBox").value = words;
    $("theBigChatBox").setSelectionRange(words.length, words.length);
    rememberTheBigChatComposer();
  }
}

function clearSwarmActivityAttachments(activity) {
  // Feed completion can release the composer before the original HTTP response
  // arrives. Cleanup belongs to that immutable activity and runs once: a late
  // response must never delete files the user attached for the next turn.
  if (!activity?.stateKey || activity.attachmentsCleared) return false;
  activity.attachmentsCleared = true;
  swarmChatAttachments.delete(activity.stateKey);
  for (const held of swarmChats) {
    if (swarmChatRuntimeKey(held.agent) !== activity.chatKey) continue;
    renderChatAttachments(held.agent);
    countWhatIsTypedTo(held.agent);
  }
  return true;
}

function normalizedUserQuestions(value) {
  if (!Array.isArray(value)) return [];
  const used = new Set();
  return value.slice(0, 6).map((raw, index) => {
    const source = typeof raw === "string" ? {prompt: raw} : raw;
    if (!source || typeof source !== "object") return null;
    const prompt = String(source.prompt || "").trim().slice(0, 500);
    if (!prompt) return null;
    let id = String(source.id || `question-${index + 1}`)
      .replace(/[^A-Za-z0-9_-]/g, "-").slice(0, 120) || `question-${index + 1}`;
    if (used.has(id.toLowerCase())) id = `${id}-${index + 1}`;
    used.add(id.toLowerCase());
    let recommendationKept = false;
    const options = (Array.isArray(source.options) ? source.options : [])
      .slice(0, 8).map((option) => {
        const recommended = option?.recommended === true && !recommendationKept;
        recommendationKept ||= recommended;
        return {
          label: String(option?.label || "").trim().slice(0, 160),
          description: String(option?.description || "").trim().slice(0, 500),
          recommended,
        };
      }).filter((option) => option.label);
    return Object.freeze({
      id, prompt, options: Object.freeze(options),
      multiple: source.multiple === true && options.length > 1,
      allowOther: source.allow_other !== false || !options.length,
    });
  }).filter(Boolean);
}

function frozenQuestionAnswers(value) {
  const found = {};
  if (!value || typeof value !== "object") return Object.freeze(found);
  for (const [id, raw] of Object.entries(value).slice(0, 6)) {
    if (!raw || typeof raw !== "object") continue;
    found[String(id).slice(0, 120)] = Object.freeze({
      selected: Object.freeze((Array.isArray(raw.selected) ? raw.selected : [])
        .map((one) => String(one).slice(0, 160)).filter(Boolean).slice(0, 8)),
      other: String(raw.other || "").trim().slice(0, 2000),
      text: String(raw.text || "").trim().slice(0, 4000),
    });
  }
  return Object.freeze(found);
}

function compiledQuestionAnswers(questions, answers) {
  const lines = [];
  const missing = [];
  for (const question of normalizedUserQuestions(questions)) {
    const answer = answers?.[question.id] || {};
    const selected = (Array.isArray(answer.selected) ? answer.selected : [])
      .map((one) => String(one).trim()).filter(Boolean);
    const other = String(answer.other || "").trim();
    const textAnswer = String(answer.text || "").trim();
    const values = question.options.length
      ? [...selected, ...(other ? [`Other: ${other}`] : [])]
      : (textAnswer ? [textAnswer] : []);
    if (!values.length) {
      missing.push(question.prompt);
      continue;
    }
    lines.push(`- [${question.id}] ${question.prompt}\n  Answer: ${values.join("; ")}`);
  }
  return {text: lines.length ? `Answers to your questions:\n${lines.join("\n")}` : "", missing};
}

function userQuestionFields(question, current, onChange, namespace) {
  const fieldset = make("fieldset", "agent-question-fields");
  fieldset.append(make("legend", "agent-question-prompt", question.prompt));
  const answer = {
    selected: [...(current?.selected || [])],
    other: String(current?.other || ""),
    text: String(current?.text || ""),
  };
  const changed = () => onChange({
    selected: [...answer.selected], other: answer.other, text: answer.text,
  });
  if (question.options.length) {
    for (const [index, option] of question.options.entries()) {
      const label = make("label", `agent-question-option${option.recommended ? " recommended" : ""}`);
      const input = document.createElement("input");
      input.type = question.multiple ? "checkbox" : "radio";
      input.name = `${namespace}-${question.id}`;
      input.value = option.label;
      input.checked = answer.selected.includes(option.label);
      input.addEventListener("change", () => {
        answer.selected = question.multiple
          ? [...fieldset.querySelectorAll("input[data-question-choice]:checked")]
            .map((one) => one.value)
          : [input.value];
        if (!question.multiple) answer.other = "";
        changed();
      });
      input.dataset.questionChoice = "true";
      const words = make("span", "agent-question-option-words");
      words.append(make("strong", "", option.label));
      if (option.recommended) words.append(make("span", "agent-question-recommended", "Recommended"));
      if (option.description) words.append(make("small", "", option.description));
      label.append(input, words);
      fieldset.append(label);
    }
  }
  if (question.allowOther || !question.options.length) {
    const label = make("label", "agent-question-other");
    label.append(make("span", "", question.options.length ? "Other answer" : "Your answer"));
    const input = question.options.length ? make("input", "") : make("textarea", "");
    if (!question.options.length) input.rows = 2;
    input.value = question.options.length ? answer.other : answer.text;
    input.placeholder = question.options.length ? "Type your own answer" : "Type your answer";
    input.addEventListener("input", () => {
      if (question.options.length) {
        answer.other = input.value;
        if (answer.other.trim() && !question.multiple) {
          answer.selected = [];
          for (const choice of fieldset.querySelectorAll("input[data-question-choice]")) {
            choice.checked = false;
          }
        }
      } else {
        answer.text = input.value;
      }
      changed();
    });
    label.append(input);
    fieldset.append(label);
  }
  return fieldset;
}

function frozenWorkRecovery(value) {
  const roots = Array.isArray(value?.allowedWriteRoots)
    ? value.allowedWriteRoots.map((one) => String(one)).filter(Boolean) : [];
  const questions = normalizedUserQuestions(value?.questions);
  const remaining = Array.isArray(value?.remaining)
    ? value.remaining.map((one) => String(one)).filter(Boolean) : [];
  return Object.freeze({
    status: String(value?.status || ""),
    resumeToken: String(value?.resumeToken || ""),
    // This array is never exposed through an editable control. Freezing it also
    // makes accidental in-memory widening fail instead of silently changing the
    // authority later sent back to the server.
    allowedWriteRoots: Object.freeze(roots),
    writeScopeRestricted: Boolean(value?.writeScopeRestricted),
    contextToolBudget: Object.freeze({...value?.contextToolBudget}),
    questions: Object.freeze(questions),
    questionAnswers: frozenQuestionAnswers(value?.questionAnswers),
    remaining: Object.freeze(remaining),
    objective: String(value?.objective || ""),
    answerDraft: String(value?.answerDraft || ""),
    projectId: String(value?.projectId || ""),
    projectName: String(value?.projectName || ""),
    updatedAt: String(value?.updatedAt || new Date().toISOString()),
  });
}

function loadSwarmWorkRecoveries() {
  const found = new Map();
  try {
    const saved = JSON.parse(window.localStorage.getItem(SWARM_WORK_RECOVERIES_KEY) || "{}");
    for (const [key, value] of Object.entries(saved || {}).slice(-50)) {
      const recovery = frozenWorkRecovery(value);
      if (key && SWARM_RECOVERABLE_WORK_STATUSES.has(recovery.status)
          && recovery.resumeToken && recovery.objective) found.set(key, recovery);
    }
  } catch (_) {
    // Storage is a recovery aid. A denied or corrupt local store must not stop
    // ordinary chat from opening.
  }
  return found;
}

function saveSwarmWorkRecoveries() {
  try {
    const entries = [...swarmWorkRecoveries.entries()].slice(-50);
    window.localStorage.setItem(SWARM_WORK_RECOVERIES_KEY,
      JSON.stringify(Object.fromEntries(entries)));
  } catch (_) { /* local persistence may be unavailable */ }
}

async function refreshDurableSwarmWorkRecoveries() {
  // localStorage keeps the card fast inside one renderer, but the signed
  // backend journal is what survives a desktop restart or a cleared browser
  // cache. This inventory contains recovery metadata only, not transcripts.
  const mine = ++swarmWorkRecoveryRefreshRevision;
  try {
    const saved = await request("/api/swarm/recoveries");
    if (mine !== swarmWorkRecoveryRefreshRevision) return;
    for (const key of saved.resolved_recovery_keys || []) {
      swarmWorkRecoveries.delete(String(key));
    }
    for (const one of saved.recoveries || []) {
      const key = String(one.recovery_key || "");
      if (!key) continue;
      rememberWorkRecoveryForKey(
        key, one, String(one.objective || ""),
        {project: one.project?.id || ""},
      );
    }
    saveSwarmWorkRecoveries();
    for (const held of swarmChats) renderWorkRecovery(held.agent);
    setWhatCanBePressedInSwarm();
  } catch (_) {
    // A recovery inventory failure must not hide the saved board. The local
    // cache remains visible and the normal server error path can be retried.
  }
}

function workRecoveryFor(agentId) {
  return swarmWorkRecoveries.get(swarmChatKey(agentId)) || null;
}

function rememberWorkRecoveryForKey(key, answered, objective, conversation = null) {
  const status = String(answered?.status || answered?.verification_status || "");
  const token = String(answered?.resume_token || "");
  if (!SWARM_RECOVERABLE_WORK_STATUSES.has(status) || !token) {
    if (answered?.goal_complete === true || answered?.status === "complete") {
      swarmWorkRecoveries.delete(key);
      saveSwarmWorkRecoveries();
    }
    return null;
  }
  const before = swarmWorkRecoveries.get(key);
  // The server normally returns the same roots on every pass. Once this token
  // has been saved, retain the first set even if a later response or renderer
  // accidentally supplies something different: resume authority never widens.
  const sameRun = before?.resumeToken === token;
  const project = answered?.project || {};
  const recovery = frozenWorkRecovery({
    status,
    resumeToken: token,
    allowedWriteRoots: sameRun
      ? before.allowedWriteRoots : (answered?.allowed_write_roots || []),
    writeScopeRestricted: sameRun
      ? before.writeScopeRestricted : Boolean(answered?.write_scope_restricted),
    contextToolBudget: answered?.context_tool_budget || before?.contextToolBudget || {},
    questions: answered?.questions || [],
    questionAnswers: sameRun ? before.questionAnswers : {},
    remaining: answered?.remaining || [],
    objective: sameRun ? before.objective : objective,
    answerDraft: sameRun ? before.answerDraft : "",
    projectId: sameRun ? before.projectId : (project.id || conversation?.project || ""),
    projectName: sameRun ? before.projectName : (project.name || "the selected project"),
    updatedAt: new Date().toISOString(),
  });
  swarmWorkRecoveries.set(key, recovery);
  saveSwarmWorkRecoveries();
  return recovery;
}

function updateWorkRecoveryAnswer(agentId, answer, source) {
  const key = swarmChatKey(agentId);
  const before = swarmWorkRecoveries.get(key);
  if (!before) return;
  swarmWorkRecoveries.set(key, frozenWorkRecovery({...before, answerDraft: answer}));
  saveSwarmWorkRecoveries();
  const card = theChatCardFor(agentId);
  const fields = [card?.querySelector(".work-recovery-answer")];
  if (theBigOne === agentId) fields.push($("theBigChatWorkRecovery")
    ?.querySelector(".work-recovery-answer"));
  for (const field of fields.filter(Boolean)) {
    if (field !== source && field.value !== answer) field.value = answer;
  }
  renderWorkRecoveryButtons(agentId);
}

function updateWorkRecoveryQuestionAnswer(agentId, questionId, answer) {
  const key = swarmChatKey(agentId);
  const before = swarmWorkRecoveries.get(key);
  if (!before) return;
  const questionAnswers = {...before.questionAnswers, [questionId]: answer};
  swarmWorkRecoveries.set(key, frozenWorkRecovery({...before, questionAnswers}));
  saveSwarmWorkRecoveries();
  renderWorkRecoveryButtons(agentId);
}

function workRecoveryTitle(status) {
  return status === "paused_for_user"
    ? "Project work paused for your answer"
    : status === "paused_provider"
      ? "Provider connection interrupted"
    : status === "paused_tool_budget"
      ? "Context-tool execution time needs your choice"
    : status === "needs_verification"
      ? "Applied changes need verification"
      : status === "incomplete"
        ? "Project work is not finished yet"
        : "Applied changes are not verified yet";
}

const DIRECT_LONG_GOAL_DESKTOP_OUTBOX_METHODS = Object.freeze([
  "saveDirectGoalOutbox", "listDirectGoalOutbox",
  "readDirectGoalOutbox", "deleteDirectGoalOutbox",
]);

function canUseDirectLongGoalDesktopOutbox() {
  if (typeof window.harnessDesktop === "undefined") return false;
  const missing = DIRECT_LONG_GOAL_DESKTOP_OUTBOX_METHODS.filter(
    (method) => typeof window.harnessDesktop?.[method] !== "function",
  );
  if (missing.length) {
    throw new Error(
      "This Nexus desktop build has an incomplete durable goal-request bridge "
      + `(${missing.join(", ")} missing). Restart or update Nexus before starting project work.`,
    );
  }
  return true;
}

async function saveDirectLongGoalOutbox(record) {
  if (!canUseDirectLongGoalDesktopOutbox()) {
    throw new Error(
      "This Nexus desktop build cannot durably stage an exact goal request before sending it.",
    );
  }
  return window.harnessDesktop.saveDirectGoalOutbox(record);
}

function exactDirectLongGoalOutboxInventoryValue(listed) {
  if (!Array.isArray(listed)) {
    throw new Error(
      "The desktop recovery journal did not return its exact recovery-record list.",
    );
  }
  return listed;
}

async function readDirectLongGoalOutbox() {
  if (!canUseDirectLongGoalDesktopOutbox()) return [];
  const listed = await window.harnessDesktop.listDirectGoalOutbox();
  // The desktop contract is an array. A fulfilled malformed IPC value is not
  // evidence that the durable outbox is empty: treating `{}` or `null` as []
  // would make an already-visible recovery disappear on the next full merge.
  return exactDirectLongGoalOutboxInventoryValue(listed);
}

async function loadDirectLongGoalOutboxPayload(recovery) {
  if (!canUseDirectLongGoalDesktopOutbox()) {
    throw new Error("The exact local goal request cannot be read by this desktop build.");
  }
  return window.harnessDesktop.readDirectGoalOutbox(
    recovery.chat_id, recovery.request_id, recovery.outbox_payload_sha256 || recovery.payload_sha256,
  );
}

function directLongGoalRecoveryPayloadViewKey(recovery) {
  const digest = String(
    recovery?.outbox_payload_sha256 || recovery?.intent || recovery?.intent_sha256
      || recovery?.payload_sha256 || "",
  );
  return JSON.stringify([
    String(recovery?.chat_id || ""), String(recovery?.request_id || ""), digest,
  ]);
}

function directLongGoalRecoveryPayloadView(recovery) {
  return directLongGoalRecoveryPayloadViews.get(
    directLongGoalRecoveryPayloadViewKey(recovery),
  ) || null;
}

function rememberDirectLongGoalRecoveryPayloadView(recovery, payload) {
  const attachments = Object.freeze((Array.isArray(payload?.attachments)
    ? payload.attachments : []).map((one) => Object.freeze({
      name: String(one?.name || "Saved attachment"),
      type: String(one?.type || "application/octet-stream"),
      size: Number(one?.size || 0),
    })));
  const visible = Object.freeze({
    text: String(payload?.text || ""), attachments,
  });
  directLongGoalRecoveryPayloadViews.set(
    directLongGoalRecoveryPayloadViewKey(recovery), visible,
  );
  return visible;
}

async function verifiedDirectLongGoalOutboxPayload(recovery) {
  const saved = await loadDirectLongGoalOutboxPayload(recovery);
  const payload = saved?.payload;
  const expectedDigest = String(
    recovery?.outbox_payload_sha256 || recovery?.intent || recovery?.payload_sha256 || "",
  );
  if (!saved || typeof saved !== "object" || Array.isArray(saved)
      || saved.schema_version !== 1 || !payload || typeof payload !== "object"
      || Array.isArray(payload)
      || String(saved.chat_id || "") !== String(recovery?.chat_id || "")
      || String(saved.request_id || "") !== String(recovery?.request_id || "")
      || String(saved.payload_sha256 || "") !== expectedDigest
      || String(saved.intent || "") !== expectedDigest
      || String(payload.chat_id || "") !== String(recovery?.chat_id || "")
      || typeof payload.project_id !== "string" || !payload.project_id
      || typeof payload.lead_id !== "string" || !payload.lead_id
      || String(payload.project_id) !== String(recovery?.project_id || "")
      || String(payload.lead_id) !== String(recovery?.lead_id || "")
      || typeof payload.text !== "string" || !payload.text.trim()
      || !Array.isArray(payload.attachments)) {
    throw new Error(
      "The exact local goal-request payload changed or returned an unsupported shape.",
    );
  }
  const computed = await directLongGoalIntent(
    {project: payload.project_id, id: payload.chat_id},
    payload.lead_id, payload.text, payload.attachments,
  );
  if (computed !== expectedDigest) {
    throw new Error(
      "The exact local goal-request payload no longer matches its saved digest.",
    );
  }
  return saved;
}

async function removeDirectLongGoalOutbox(chatId, requestId, payloadSha256 = "") {
  if (!canUseDirectLongGoalDesktopOutbox()) {
    throw new Error(
      "This Nexus desktop build cannot confirm deletion of the exact saved goal request.",
    );
  }
  const removed = await window.harnessDesktop.deleteDirectGoalOutbox(
    chatId, requestId, payloadSha256,
  );
  const acknowledgementKeys = removed && typeof removed === "object"
    && !Array.isArray(removed) ? Object.keys(removed).sort() : [];
  const exactAcknowledgement = acknowledgementKeys.length === 2
    && acknowledgementKeys[0] === "deleted"
    && acknowledgementKeys[1] === "reason";
  if (exactAcknowledgement
      && removed.deleted === true && removed.reason === "deleted") {
    return removed;
  }
  if (exactAcknowledgement
      && removed.deleted === false && removed.reason === "missing") {
    return removed;
  }
  if (exactAcknowledgement
      && removed.deleted === false && removed.reason === "mismatch") {
    throw new Error(
      "The durable desktop outbox changed before deletion. Its replacement was kept.",
    );
  }
  throw new Error(
    "The desktop did not return an exact deletion acknowledgement. The saved goal request was kept visible.",
  );
}

function clearDirectLongGoalRequestMarker(
  agentId, chatId, requestId, intentSha256,
) {
  if (!agentId) return false;
  const key = `nexus.long-horizon.direct-request.${swarmChatKeyFor(agentId, chatId)}`;
  let marker = null;
  try { marker = JSON.parse(localStorage.getItem(key) || "null"); }
  catch (_) { return false; }
  if (!marker || typeof marker !== "object" || Array.isArray(marker)
      || ![2, 3].includes(marker.schema_version)
      || typeof marker.id !== "string" || marker.id !== String(requestId || "")
      || typeof marker.intent !== "string"
      || marker.intent !== String(intentSha256 || "")) {
    return false;
  }
  localStorage.removeItem(key);
  return true;
}

function directLongGoalBrowserMarkerForCleanup(
  agentId, chatId, projectId,
) {
  if (!chatId || !projectId || !agentId) return null;
  const key = `nexus.long-horizon.direct-request.${swarmChatKeyFor(
    agentId, chatId,
  )}`;
  const rawMarker = localStorage.getItem(key);
  if (rawMarker === null) return null;
  let marker;
  try { marker = JSON.parse(rawMarker); }
  catch (_) {
    throw new Error("The saved browser goal-request marker is unreadable.");
  }
  if (!marker || typeof marker !== "object" || Array.isArray(marker)
      || ![2, 3].includes(marker.schema_version)
      || typeof marker.id !== "string" || !marker.id || marker.id.length > 160
      || !DIRECT_LONG_GOAL_RECOVERY_SHA256.test(String(marker.intent || ""))
      || ![undefined, false, true].includes(marker.prepared)
      || (marker.prepared === true
        && !DIRECT_LONG_GOAL_RECOVERY_SHA256.test(
          String(marker.payload_sha256 || ""),
        ))) {
    throw new Error("The saved browser goal-request marker has no exact identity.");
  }
  if (marker.schema_version === 3 && (
    marker.chat_id !== chatId
    || marker.project_id !== projectId
    || marker.lead_id !== agentId
  )) {
    throw new Error("The saved browser goal-request marker belongs to another exact chat binding.");
  }
  return Object.freeze({
    schema_version: 1,
    request_id: marker.id,
    chat_id: chatId,
    project_id: projectId,
    lead_id: agentId,
    intent_sha256: marker.intent,
    payload_sha256: String(marker.payload_sha256 || ""),
    prepared: marker.prepared === true,
    text_preview: "",
    text_characters: 0,
    attachment_count: 0,
    created_ms: 0,
    server_pending: false,
    desktop_outbox: false,
    browser_marker: true,
  });
}

function preparedDirectLongGoalBrowserMarkerFor(
  agentId, chatId, projectId,
) {
  const marker = directLongGoalBrowserMarkerForCleanup(
    agentId, chatId, projectId,
  );
  if (!marker) return null;
  if (!marker.prepared || !DIRECT_LONG_GOAL_RECOVERY_SHA256.test(
    String(marker.payload_sha256 || ""),
  )) {
    throw new Error(
      "The saved browser goal-request marker has not reached an exact prepared state.",
    );
  }
  return marker;
}

function preparedDirectLongGoalBrowserMarker(agentId) {
  const conversation = activeConversationFor(agentId);
  return preparedDirectLongGoalBrowserMarkerFor(
    agentId, conversation?.id, conversation?.project,
  );
}

function directLongGoalRecoveryFor(agentId) {
  const recovery = directLongGoalRecoveries.get(activeConversationIdFor(agentId)) || null;
  if (recovery && String(recovery.lead_id || "") === String(agentId || "")) {
    return recovery;
  }
  try {
    const marker = preparedDirectLongGoalBrowserMarker(agentId);
    if (marker) {
      directLongGoalRecoveries.set(marker.chat_id, marker);
      return marker;
    }
  } catch (_) {
    directLongGoalRecoveryInventoryReady = false;
    directLongGoalRecoveryError =
      "Nexus could not verify this chat's saved browser goal-request marker. No new project work was sent.";
  }
  return null;
}

const DIRECT_LONG_GOAL_RECEIPT_SCHEMA_VERSION = 1;
const DIRECT_LONG_GOAL_RECEIPT_ID_LIMITS = Object.freeze({
  request_id: 160, chat_id: 256, project_id: 512, lead_id: 256,
});

function verifiedDirectLongGoalReceiptIdentity(receipt, expected, label) {
  if (!receipt || typeof receipt !== "object" || Array.isArray(receipt)) {
    throw new Error(`${label} did not return an exact receipt object.`);
  }
  if (!expected || typeof expected !== "object" || Array.isArray(expected)) {
    throw new Error(`Nexus has no exact identity for ${label.toLowerCase()}.`);
  }
  const identity = {};
  for (const [field, maximum] of Object.entries(DIRECT_LONG_GOAL_RECEIPT_ID_LIMITS)) {
    const exact = expected[field];
    if (typeof exact !== "string" || !exact || [...exact].length > maximum) {
      throw new Error(
        `Nexus has no bounded exact ${field.replaceAll("_", " ")} for ${label.toLowerCase()}.`,
      );
    }
    if (typeof receipt[field] !== "string" || receipt[field] !== exact) {
      throw new Error(
        `${label} is bound to a different exact ${field.replaceAll("_", " ")}.`,
      );
    }
    identity[field] = exact;
  }
  const intent = expected.intent_sha256;
  if (typeof intent !== "string" || !DIRECT_LONG_GOAL_RECOVERY_SHA256.test(intent)) {
    throw new Error(`Nexus has no exact intent digest for ${label.toLowerCase()}.`);
  }
  if (typeof receipt.intent_sha256 !== "string" || receipt.intent_sha256 !== intent) {
    throw new Error(`${label} is bound to a different exact intent digest.`);
  }
  return {...identity, intent_sha256: intent};
}

function verifiedDirectLongGoalReceiptGoal(goal, expected, label) {
  if (!goal || typeof goal !== "object" || Array.isArray(goal)
      || !Number.isSafeInteger(goal.schema_version) || goal.schema_version < 1
      || typeof goal.goal_id !== "string" || !goal.goal_id
      || typeof goal.request_id !== "string" || goal.request_id !== expected.request_id
      || typeof goal.conversation_id !== "string"
      || goal.conversation_id !== expected.chat_id
      || !goal.project || typeof goal.project !== "object" || Array.isArray(goal.project)
      || typeof goal.project.id !== "string" || goal.project.id !== expected.project_id
      || typeof goal.lead_agent_id !== "string" || goal.lead_agent_id !== expected.lead_id) {
    throw new Error(`${label} did not return the matching canonical durable goal.`);
  }
  return goal;
}

function verifiedDirectLongGoalStartReceipt(receipt, expected) {
  const identity = verifiedDirectLongGoalReceiptIdentity(
    receipt, expected, "The durable goal start",
  );
  if (receipt.schema_version !== DIRECT_LONG_GOAL_RECEIPT_SCHEMA_VERSION
      || receipt.engine !== "long_horizon") {
    throw new Error("The durable goal start returned an unsupported receipt version or engine.");
  }
  verifiedDirectLongGoalReceiptGoal(receipt.goal, identity, "The durable goal start");
  return receipt;
}

function verifiedDirectLongGoalDiscardReceipt(receipt, expected) {
  const identity = verifiedDirectLongGoalReceiptIdentity(
    receipt, expected, "The saved goal discard",
  );
  if (receipt.schema_version !== DIRECT_LONG_GOAL_RECEIPT_SCHEMA_VERSION
      || typeof receipt.discarded !== "boolean"
      || typeof receipt.reconciled !== "boolean"
      || typeof receipt.safe_to_delete !== "boolean") {
    throw new Error("The saved goal discard returned an unsupported or ambiguous receipt.");
  }
  if (receipt.reconciled) {
    if (receipt.discarded || receipt.safe_to_delete) {
      throw new Error("The saved goal discard returned conflicting reconciliation flags.");
    }
    verifiedDirectLongGoalReceiptGoal(
      receipt.goal, identity, "The saved goal reconciliation",
    );
  } else {
    if (!receipt.discarded || !receipt.safe_to_delete
        || (receipt.goal !== undefined && receipt.goal !== null)) {
      throw new Error("The backend did not prove that this exact saved request is safe to delete.");
    }
  }
  return receipt;
}

function appendDirectLongGoalExactPayload(parent, recovery, {open = true} = {}) {
  const payload = directLongGoalRecoveryPayloadView(recovery);
  if (!payload) return false;
  const exact = make("details", "direct-long-goal-exact-payload");
  exact.open = open;
  exact.append(make("summary", "", "Exact saved request"));
  exact.append(make("pre", "direct-long-goal-exact-text", payload.text));
  if (payload.attachments.length) {
    const files = make("ul", "direct-long-goal-exact-attachments");
    for (const attachment of payload.attachments) {
      files.append(make(
        "li", "",
        `${attachment.name} · ${Number(attachment.size || 0).toLocaleString()} bytes`,
      ));
    }
    exact.append(files);
  }
  parent.append(exact);
  return true;
}

function fillDirectLongGoalRecoveryPanel(panel, agentId, recovery) {
  panel.hidden = false;
  panel.replaceChildren();
  panel.dataset.status = "pending_admission";
  panel.append(make("h4", "work-recovery-title", "Saved project goal needs reconciliation"));
  panel.append(make(
    "p", "work-recovery-status",
    recovery.outbox_conflict || recovery.outbox_digest_mismatch
      ? "The desktop outbox and backend journal disagree. Nexus has failed closed: it will not resend or delete either exact record."
      : "Nexus saved the exact bounded request and its attachment bytes before clearing the composer. "
        + "It has not automatically resent anything after the interrupted admission.",
  ));
  const exactPromptShown = appendDirectLongGoalExactPayload(panel, recovery);
  if (!exactPromptShown && recovery.text_preview) {
    panel.append(make("p", "hint", `Request: ${recovery.text_preview}`));
  }
  const attachmentWords = recovery.attachment_count
    ? ` · ${recovery.attachment_count} saved attachment${recovery.attachment_count === 1 ? "" : "s"}`
    : "";
  panel.append(make(
    "p", "hint",
    `${Number(recovery.text_characters || 0).toLocaleString()} saved characters${attachmentWords}.`,
  ));
  const row = make("div", "button-row work-recovery-actions");
  const recover = make("button", "primary direct-long-goal-recover", "Reconcile saved goal request");
  recover.type = "button";
  recover.addEventListener("click", () => recoverDirectLongGoalAdmission(agentId));
  row.append(recover);
  const discard = make("button", "danger direct-long-goal-discard", "Discard saved request");
  discard.type = "button";
  discard.addEventListener("click", () => discardDirectLongGoalAdmission(agentId));
  row.append(discard);
  panel.append(row);
}

async function openDirectLongGoalRecovery(recovery) {
  let exact = recovery;
  if (exact?.desktop_outbox) {
    const saved = await verifiedDirectLongGoalOutboxPayload(exact);
    rememberDirectLongGoalRecoveryPayloadView(exact, saved.payload);
    // Keep the revealed attachment bytes confined to this verified read. The
    // chat-selection path needs only immutable routing identity; neither the
    // compact nor maximized composer should ever receive a resubmittable copy.
    exact = {
      ...exact,
      project_id: String(saved.payload.project_id || ""),
      lead_id: String(saved.payload.lead_id || ""),
      chat_id: String(saved.payload.chat_id || ""),
    };
    // The exact payload is deliberately not inventoried on startup. Once the
    // person explicitly opens it, show the exact text before either Reconcile
    // or Discard can be chosen.
    renderDirectLongGoalBoardRecoveryNotice();
  }
  const agentId = String(exact?.lead_id || "");
  const chatId = String(exact?.chat_id || "");
  if (!agentId || !chatId || !theSwarmAgent(agentId)) {
    throw new Error("The saved goal request no longer has its exact agent and chat on this board.");
  }
  if (!swarmChats.some((one) => one.agent === agentId)) {
    await openTheChatFor(agentId);
  } else {
    await loadConversationsFor(agentId, false);
  }
  const held = swarmChats.find((one) => one.agent === agentId);
  const conversation = (held?.conversations || []).find((one) => one.id === chatId);
  if (!conversation) throw new Error("The exact saved chat could not be found on this board.");
  if (String(conversation.project || "") !== String(exact.project_id || "")) {
    throw new Error(
      "The exact saved chat is no longer bound to its saved project. The request remains journalled and was not resent.",
    );
  }
  if (conversation.archived_at) await restoreConversationFor(agentId, chatId);
  else if (held.conversation !== chatId) await activateConversationFor(agentId, chatId);
  if (activeConversationIdFor(agentId) !== chatId) {
    throw new Error(
      "Nexus could not activate the exact saved chat. The recovery remains on the board and was not resent.",
    );
  }
  renderWorkRecovery(agentId);
  const card = theChatCardFor(agentId);
  card?.scrollIntoView?.({block: "nearest"});
  card?.querySelector?.(".direct-long-goal-recover")?.focus?.({preventScroll: true});
}

function renderDirectLongGoalBoardRecoveryNotice() {
  const panel = $("directLongGoalRecoveryBoard");
  const list = $("directLongGoalRecoveryBoardList");
  if (!panel || !list) return;
  const recoveries = [...directLongGoalRecoveries.values()].flatMap(
    (one) => one.outbox_conflict ? [one, one.outbox_conflict] : [one],
  );
  panel.hidden = !recoveries.length && !directLongGoalRecoveryError;
  list.replaceChildren();
  if (directLongGoalRecoveryError) {
    list.append(make("li", "direct-long-goal-recovery-error", directLongGoalRecoveryError));
  }
  for (const [recoveryIndex, recovery] of recoveries.entries()) {
    const row = make("li", "direct-long-goal-recovery-row");
    const identity = String(recovery.request_id || "").slice(0, 12);
    row.append(make(
      "span", "direct-long-goal-recovery-identity",
      `${Number(recovery.text_characters || 0).toLocaleString()} characters · `
        + `${Number(recovery.attachment_count || 0)} attachments · request ${identity}`,
    ));
    appendDirectLongGoalExactPayload(row, recovery, {open: false});
    const actions = make("span", "button-row");
    const open = make("button", "direct-long-goal-open", "Open exact saved chat");
    open.type = "button";
    open.disabled = !swarmBoardHydrated;
    let loading = null;
    if (open.disabled) {
      loading = make(
        "span", "hint direct-long-goal-loading",
        "Loading the saved agent board before this exact chat can open.",
      );
      loading.id = `direct-long-goal-loading-${recoveryIndex}`;
      open.setAttribute("aria-describedby", loading.id);
    }
    open.title = open.disabled
      ? "Wait while Nexus loads the saved agent board; this request remains safely journalled"
      : "Open the exact saved chat without resending its request";
    open.addEventListener("click", async () => {
      try { await openDirectLongGoalRecovery(recovery); }
      catch (error) { showError(String(error.message || error)); }
    });
    actions.append(open);
    const discard = make("button", "danger direct-long-goal-board-discard", "Discard exact request");
    discard.type = "button";
    discard.addEventListener("click", () => discardDirectLongGoalAdmission(
      String(recovery.lead_id || ""), recovery,
    ));
    actions.append(discard);
    if (loading) row.append(loading);
    row.append(actions);
    list.append(row);
  }
}

function directLongGoalRecoveryInventoryState() {
  return {
    hasPending: directLongGoalRecoveries.size > 0,
    inventoryError: String(directLongGoalRecoveryError || ""),
  };
}

function directLongGoalLocalRecoveryMetadata(pending) {
  return {
    ...pending,
    lead_id: String(pending?.lead_id || pending?.payload?.lead_id || ""),
    project_id: String(pending?.project_id || pending?.payload?.project_id || ""),
    text_characters: Number(pending?.text_characters || 0),
    attachment_count: Number(pending?.attachment_count || 0),
    // Desktop metadata is intentionally not used as a display preview; only
    // the backend's credential-redacted preview may be rendered.
    text_preview: "",
    desktop_outbox: true,
    server_pending: false,
    outbox_payload_sha256: String(pending?.payload_sha256 || ""),
  };
}

const DIRECT_LONG_GOAL_RECOVERY_SHA256 = /^[a-f0-9]{64}$/;

function exactDirectLongGoalRecoveryText(pending, field, journal, index, maximum = 4096) {
  const value = pending?.[field];
  if (typeof value !== "string" || !value || value !== value.trim()
      || [...value].length > maximum) {
    throw new Error(
      `${journal} record ${index + 1} has no exact ${field.replaceAll("_", " ")}.`,
    );
  }
  return value;
}

function exactDirectLongGoalRecoveryCount(
  pending, field, journal, index, maximum = Number.MAX_SAFE_INTEGER,
) {
  const value = pending?.[field];
  if (!Number.isSafeInteger(value) || value < 0 || value > maximum) {
    throw new Error(
      `${journal} record ${index + 1} has an invalid ${field.replaceAll("_", " ")}.`,
    );
  }
  return value;
}

function verifiedDirectLongGoalRecoveryRows(value, journal) {
  if (!Array.isArray(value)) {
    throw new Error(`${journal} did not return an exact recovery-record list.`);
  }
  const chatIds = new Set();
  const requestIds = new Set();
  const digestFields = journal === "The backend recovery journal"
    ? ["intent_sha256", "payload_sha256"] : ["intent", "payload_sha256"];
  for (const [index, pending] of value.entries()) {
    if (!pending || typeof pending !== "object" || Array.isArray(pending)
        || pending.schema_version !== 1) {
      throw new Error(`${journal} record ${index + 1} has an unsupported shape or version.`);
    }
    const chatId = exactDirectLongGoalRecoveryText(
      pending, "chat_id", journal, index, 512,
    );
    const requestId = exactDirectLongGoalRecoveryText(
      pending, "request_id", journal, index, 160,
    );
    const projectId = exactDirectLongGoalRecoveryText(
      pending, "project_id", journal, index, 512,
    );
    const leadId = exactDirectLongGoalRecoveryText(
      pending, "lead_id", journal, index, 256,
    );
    if (chatIds.has(chatId) || requestIds.has(requestId)) {
      throw new Error(`${journal} returned a duplicate chat or request identity.`);
    }
    chatIds.add(chatId);
    requestIds.add(requestId);
    for (const field of digestFields) {
      if (typeof pending[field] !== "string"
          || !DIRECT_LONG_GOAL_RECOVERY_SHA256.test(pending[field])) {
        throw new Error(`${journal} record ${index + 1} has no exact ${field} digest.`);
      }
    }
    if (journal !== "The backend recovery journal"
        && pending.intent !== pending.payload_sha256) {
      throw new Error(`${journal} record ${index + 1} contains conflicting exact digests.`);
    }
    if (typeof pending.text_preview !== "string"
        || [...pending.text_preview].length > 500) {
      throw new Error(`${journal} record ${index + 1} has an invalid bounded text preview.`);
    }
    exactDirectLongGoalRecoveryCount(
      pending, "text_characters", journal, index, 12_000_000,
    );
    exactDirectLongGoalRecoveryCount(
      pending, "attachment_count", journal, index, 6,
    );
    if (journal === "The backend recovery journal") {
      exactDirectLongGoalRecoveryCount(
        pending, "created_ms", journal, index,
      );
      const terminalState = String(pending.terminal_state || "");
      if (terminalState) {
        if (!["discarded", "reconciled"].includes(terminalState)
            || pending.client_consumed !== false
            || typeof pending.goal_id !== "string"
            || (terminalState === "reconciled" && !pending.goal_id)
            || (terminalState === "discarded" && pending.goal_id)) {
          throw new Error(
            `${journal} record ${index + 1} has an invalid terminal outcome.`,
          );
        }
      }
      const contract = pending.execution_contract;
      if (!contract || typeof contract !== "object" || Array.isArray(contract)
          || contract.schema_version !== 1
          || ![
            "direct_long_horizon_admission",
            "desktop_direct_long_horizon_outbox",
          ].includes(contract.kind)
          || String(contract.project_id || "") !== projectId
          || String(contract.chat_id || "") !== chatId
          || String(contract.lead_id || "") !== leadId
          || typeof contract.chat_scope !== "string" || !contract.chat_scope
          || (contract.kind === "direct_long_horizon_admission"
            && !DIRECT_LONG_GOAL_RECOVERY_SHA256.test(
            String(contract.project_root_fingerprint_sha256 || ""),
            ))
          || !DIRECT_LONG_GOAL_RECOVERY_SHA256.test(
            String(contract.fingerprint_sha256 || ""),
          )) {
        throw new Error(
          `${journal} record ${index + 1} has an incomplete execution contract.`,
        );
      }
    } else {
      if (!DIRECT_LONG_GOAL_RECOVERY_SHA256.test(
        String(pending.project_fingerprint || ""),
      )) {
        throw new Error(
          `${journal} record ${index + 1} has no exact project fingerprint.`,
        );
      }
      exactDirectLongGoalRecoveryCount(
        pending, "attachment_bytes", journal, index, 8_000_000,
      );
      for (const field of ["created_at", "updated_at"]) {
        if (typeof pending[field] !== "string"
            || !Number.isFinite(Date.parse(pending[field]))) {
          throw new Error(
            `${journal} record ${index + 1} has an invalid ${field.replaceAll("_", " ")}.`,
          );
        }
      }
    }
  }
  return value;
}

function verifiedDirectLongGoalRemoteInventory(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)
      || !Object.hasOwn(value, "pending")
      || !Object.hasOwn(value, "terminal")
      || !Array.isArray(value.pending) || !Array.isArray(value.terminal)) {
    throw new Error("The backend recovery journal did not return its exact inventory envelope.");
  }
  return verifiedDirectLongGoalRecoveryRows(
    [...value.pending, ...value.terminal], "The backend recovery journal",
  );
}

function verifyDirectLongGoalCrossAuthorityRequests(rows) {
  const requests = new Map();
  for (const pending of rows) {
    const requestId = String(pending?.request_id || "");
    const chatId = String(pending?.chat_id || "");
    if (!requestId || !chatId) continue;
    const heldChat = requests.get(requestId);
    if (heldChat && heldChat !== chatId) {
      throw new Error(
        "The recovery journals bound one request identity to different exact chats.",
      );
    }
    requests.set(requestId, chatId);
  }
}

function sameDirectLongGoalCrossAuthorityBinding(one, other) {
  return ["project_id", "lead_id"].every(
    (field) => String(one?.[field] || "") === String(other?.[field] || ""),
  );
}

function mergeDirectLongGoalRecoveryInventories(remote, local, replace = false) {
  // Build the whole candidate inventory off to the side. A malformed but
  // fulfilled IPC/HTTP value must not clear or partially rewrite recovery
  // records which were already visible and safely frozen.
  const remoteRows = verifiedDirectLongGoalRecoveryRows(
    remote, "The backend recovery journal",
  );
  const localRows = verifiedDirectLongGoalRecoveryRows(
    local, "The desktop recovery journal",
  );
  verifyDirectLongGoalCrossAuthorityRequests([...remoteRows, ...localRows]);
  const merged = new Map(replace ? [] : directLongGoalRecoveries);
  for (const pending of remoteRows) {
    const chatId = String(pending?.chat_id || "");
    if (!chatId) continue;
    const terminalState = String(pending?.terminal_state || "");
    const remoteMetadata = {
      ...pending,
      server_pending: !terminalState,
      server_terminal: terminalState,
      desktop_outbox: false,
    };
    const existing = merged.get(chatId);
    if (!existing || !existing.desktop_outbox) {
      merged.set(chatId, {...existing, ...remoteMetadata});
    } else if (String(existing.request_id) === String(pending.request_id)
        && sameDirectLongGoalCrossAuthorityBinding(existing, pending)) {
      const digestMismatch = Boolean(
        pending.intent_sha256 && existing.outbox_payload_sha256
        && pending.intent_sha256 !== existing.outbox_payload_sha256
      );
      merged.set(chatId, {
        ...existing, ...remoteMetadata, desktop_outbox: true,
        outbox_payload_sha256: existing.outbox_payload_sha256,
        outbox_digest_mismatch: digestMismatch,
      });
    } else {
      merged.set(chatId, {
        ...remoteMetadata, outbox_conflict: Object.freeze({...existing}),
      });
    }
  }
  for (const pending of localRows) {
    const chatId = String(pending?.chat_id || "");
    if (!chatId) continue;
    const existing = merged.get(chatId);
    const localMetadata = directLongGoalLocalRecoveryMetadata(pending);
    if (!existing) {
      merged.set(chatId, localMetadata);
    } else if (String(existing.request_id) === String(pending.request_id)
        && sameDirectLongGoalCrossAuthorityBinding(existing, pending)) {
      const digestMismatch = Boolean(
        existing.intent_sha256 && localMetadata.outbox_payload_sha256
        && existing.intent_sha256 !== localMetadata.outbox_payload_sha256
      );
      merged.set(chatId, {
        ...localMetadata, ...existing, desktop_outbox: true,
        outbox_payload_sha256: localMetadata.outbox_payload_sha256,
        outbox_digest_mismatch: digestMismatch,
      });
    } else {
      merged.set(chatId, {
        ...existing, outbox_conflict: Object.freeze(localMetadata),
      });
    }
  }
  verifyDirectLongGoalCrossAuthorityRequests(
    [...merged.values()].flatMap(
      (pending) => pending.outbox_conflict
        ? [pending, pending.outbox_conflict] : [pending],
    ),
  );
  const committed = new Map();
  for (const [chatId, pending] of merged) {
    committed.set(chatId, Object.freeze({...pending}));
  }
  directLongGoalRecoveries.clear();
  for (const [chatId, pending] of committed) {
    directLongGoalRecoveries.set(chatId, pending);
  }
  const visiblePayloads = new Set();
  for (const pending of directLongGoalRecoveries.values()) {
    for (const one of pending.outbox_conflict
      ? [pending, pending.outbox_conflict] : [pending]) {
      if (one.desktop_outbox) {
        visiblePayloads.add(directLongGoalRecoveryPayloadViewKey(one));
      }
    }
  }
  for (const key of directLongGoalRecoveryPayloadViews.keys()) {
    if (!visiblePayloads.has(key)) directLongGoalRecoveryPayloadViews.delete(key);
  }
}

function sameDirectLongGoalRecoveryIdentity(recovery, chatId, requestId, digest = "") {
  if (!recovery || String(recovery.chat_id || "") !== String(chatId || "")
      || String(recovery.request_id || "") !== String(requestId || "")) return false;
  if (!digest) return true;
  return [
    recovery.outbox_payload_sha256, recovery.intent, recovery.intent_sha256,
    recovery.payload_sha256,
  ].some((one) => String(one || "") === String(digest));
}

function forgetDirectLongGoalRecovery(chatId, requestId, digest = "") {
  // Any refresh which sampled the two journals before successful admission is
  // now stale. Supersede it before changing the visible exact identity so its
  // delayed response cannot resurrect a phantom Reconcile card.
  directLongGoalRecoveryRefreshRevision += 1;
  const current = directLongGoalRecoveries.get(String(chatId || ""));
  if (sameDirectLongGoalRecoveryIdentity(current, chatId, requestId, digest)) {
    if (current.outbox_conflict) {
      directLongGoalRecoveries.set(
        String(chatId), Object.freeze({...current.outbox_conflict}),
      );
    } else {
      directLongGoalRecoveries.delete(String(chatId));
    }
  } else if (sameDirectLongGoalRecoveryIdentity(
    current?.outbox_conflict, chatId, requestId, digest,
  )) {
    const kept = {...current};
    delete kept.outbox_conflict;
    directLongGoalRecoveries.set(String(chatId), Object.freeze(kept));
  }
  for (const key of directLongGoalRecoveryPayloadViews.keys()) {
    let identity;
    try { identity = JSON.parse(key); } catch (_) { identity = []; }
    if (String(identity[0] || "") === String(chatId || "")
        && String(identity[1] || "") === String(requestId || "")) {
      directLongGoalRecoveryPayloadViews.delete(key);
    }
  }
  renderDirectLongGoalBoardRecoveryNotice();
  for (const held of swarmChats) renderWorkRecovery(held.agent);
  setWhatCanBePressedInSwarm();
}

async function boundedDirectLongGoalRecoveryRead(reader, journalName) {
  let timer = null;
  try {
    return await Promise.race([
      Promise.resolve().then(reader),
      new Promise((_, reject) => {
        timer = window.setTimeout(() => reject(new Error(
          `${journalName} did not answer within 8 seconds`,
        )), 8000);
      }),
    ]);
  } finally {
    if (timer !== null) window.clearTimeout(timer);
  }
}

async function refreshDirectLongGoalRecoveries() {
  const mine = ++directLongGoalRecoveryRefreshRevision;
  directLongGoalRecoveryInventoryReady = false;
  setWhatCanBePressedInSwarm();
  const [remoteResult, localResult] = await Promise.allSettled([
    boundedDirectLongGoalRecoveryRead(
      () => request("/api/long-horizon/pending-admissions"), "The backend recovery journal",
    ),
    boundedDirectLongGoalRecoveryRead(
      () => readDirectLongGoalOutbox(), "The desktop recovery journal",
    ),
  ]);
  if (mine !== directLongGoalRecoveryRefreshRevision) {
    return directLongGoalRecoveryInventoryState();
  }
  let remote = [];
  let local = [];
  let remoteProblem = remoteResult.status === "rejected" ? remoteResult.reason : null;
  let localProblem = localResult.status === "rejected" ? localResult.reason : null;
  if (!remoteProblem) {
    try { remote = verifiedDirectLongGoalRemoteInventory(remoteResult.value); }
    catch (error) { remoteProblem = error; }
  }
  if (!localProblem) {
    try {
      local = verifiedDirectLongGoalRecoveryRows(
        localResult.value, "The desktop recovery journal",
      );
    } catch (error) { localProblem = error; }
  }
  if (remoteProblem && localProblem) {
    directLongGoalRecoveryError = "Nexus could not verify either saved goal-request journal. Existing recovery records were kept; no request was assumed absent or resent.";
    renderDirectLongGoalBoardRecoveryNotice();
    setWhatCanBePressedInSwarm();
    return directLongGoalRecoveryInventoryState();
  }
  if (remoteProblem || localProblem) {
    // A partial inventory is not proof that the other authority deleted its
    // exact record. Preserve what was already displayed; a later complete
    // read will perform the authoritative merge/removal.
    directLongGoalRecoveryError = localProblem
      ? "The durable desktop goal-request outbox could not be verified. Existing recovery records were kept; repair or restart Nexus before discarding anything."
      : "The backend goal-request journal could not be verified. Existing recovery records were kept; Nexus will retry without resending anything.";
    try {
      // A successful authority is evidence that a record exists even though
      // the failed authority is not evidence that anything was deleted. Merge
      // additions only, preserving every previously displayed exact identity.
      mergeDirectLongGoalRecoveryInventories(
        remoteProblem ? [] : remote,
        localProblem ? [] : local,
        false,
      );
    } catch (_) {
      directLongGoalRecoveryError =
        "Nexus could not safely combine the available recovery journal with the records already shown. Everything was kept and no request was resent.";
    }
    renderDirectLongGoalBoardRecoveryNotice();
    for (const held of swarmChats) renderWorkRecovery(held.agent);
    // Rendering an open chat can discover a schema-v2/v3 browser marker from
    // a lost reconciliation response. Reflect that exact synthetic recovery
    // row in the board-wide notice in the same refresh.
    renderDirectLongGoalBoardRecoveryNotice();
    setWhatCanBePressedInSwarm();
    return directLongGoalRecoveryInventoryState();
  }
  try {
    directLongGoalRecoveryError = "";
    mergeDirectLongGoalRecoveryInventories(remote, local, true);
    directLongGoalRecoveryInventoryReady = true;
    renderDirectLongGoalBoardRecoveryNotice();
    for (const held of swarmChats) renderWorkRecovery(held.agent);
    // An open chat can surface a browser marker left by a lost reconciliation
    // response after the authoritative journals have returned empty.
    renderDirectLongGoalBoardRecoveryNotice();
    setWhatCanBePressedInSwarm();
  } catch (_) {
    // A recovery inventory read is not authority to discard a previously
    // displayed record. Refresh remains explicit and never resends a request.
    // Surface the failed verification as attention rather than silently
    // returning to Start and making a durable request appear to have vanished.
    directLongGoalRecoveryError =
      "Nexus could not safely interpret the saved goal-request journals. Existing records were kept and no request was resent.";
    renderDirectLongGoalBoardRecoveryNotice();
    setWhatCanBePressedInSwarm();
  }
  return directLongGoalRecoveryInventoryState();
}

async function recoverDirectLongGoalAdmission(agentId) {
  const recovery = directLongGoalRecoveryFor(agentId);
  if (!recovery || directLongGoalRecoveryBusy.has(recovery.request_id)) return;
  directLongGoalRecoveryBusy.add(recovery.request_id);
  renderWorkRecovery(agentId);
  try {
    if (recovery.outbox_conflict || recovery.outbox_digest_mismatch) {
      throw new Error(
        "The desktop outbox and backend journal do not describe the same exact request. "
        + "Nexus refused to resend or delete either record.",
      );
    }
    let backendIntentSha256 = String(
      recovery.outbox_payload_sha256 || recovery.intent_sha256 || "",
    );
    let backendPayloadSha256 = recovery.server_pending
      ? String(recovery.payload_sha256 || "") : "";
    let started = null;
    let terminalState = String(recovery.server_terminal || "");
    const expected = {
      request_id: recovery.request_id,
      chat_id: recovery.chat_id,
      project_id: recovery.project_id,
      lead_id: recovery.lead_id,
      intent_sha256: backendIntentSha256,
    };
    if (terminalState) {
      if (!["discarded", "reconciled"].includes(terminalState)) {
        throw new Error("The saved terminal goal request has an unsupported outcome.");
      }
      const existing = await exactExistingDirectLongGoal(expected);
      if (terminalState === "reconciled") {
        if (!existing || existing.goal_id !== String(recovery.goal_id || "")) {
          throw new Error(
            "The saved terminal reconciliation does not match its exact canonical goal.",
          );
        }
        started = {
          schema_version: DIRECT_LONG_GOAL_RECEIPT_SCHEMA_VERSION,
          engine: "long_horizon", ...expected, goal: existing,
          terminal_state: "reconciled",
        };
      } else if (existing) {
        throw new Error(
          "The saved discarded request unexpectedly resolves to a canonical goal.",
        );
      }
    } else if (recovery.desktop_outbox) {
      const saved = await verifiedDirectLongGoalOutboxPayload(recovery);
      const exactPayload = saved?.payload;
      if (!exactPayload || saved.request_id !== recovery.request_id
          || saved.chat_id !== recovery.chat_id) {
        throw new Error("The exact local goal-request outbox identity changed.");
      }
      if (!recovery.server_pending) {
        // A reconciliation response may have been lost after the backend
        // retired its journal. Prove an exact canonical goal first and retry
        // only the idempotent acknowledgement; never replay prepare/start.
        started = await reconcileExistingDirectLongGoalAdmission(expected);
      }
      if (!started) {
        const prepared = await request("/api/long-horizon/prepare-admission", {
          method: "POST", body: JSON.stringify({
            ...exactPayload, request_id: recovery.request_id,
          }),
        });
        const preparedPending = verifiedDirectLongGoalRecoveryRows(
          [prepared?.pending], "The backend recovery journal",
        )[0];
        for (const [field, exact] of Object.entries({
          request_id: recovery.request_id,
          chat_id: recovery.chat_id,
          project_id: recovery.project_id,
          lead_id: recovery.lead_id,
        })) {
          if (String(preparedPending[field] || "") !== String(exact || "")) {
            throw new Error(
              `The backend prepare replay changed the exact ${field.replaceAll("_", " ")}.`,
            );
          }
        }
        if (recovery.server_pending
            && String(preparedPending.payload_sha256 || "")
            !== String(recovery.payload_sha256 || "")) {
          throw new Error(
            "The backend prepare replay found a different full goal payload. Nexus did not start or delete it.",
          );
        }
        backendIntentSha256 = String(preparedPending.intent_sha256 || "");
        backendPayloadSha256 = String(preparedPending.payload_sha256 || "");
      }
    }
    if (!terminalState && recovery.browser_marker && !recovery.server_pending) {
      started = await reconcileExistingDirectLongGoalAdmission({
        request_id: recovery.request_id,
        chat_id: recovery.chat_id,
        project_id: recovery.project_id,
        lead_id: recovery.lead_id,
        intent_sha256: backendIntentSha256,
      });
      if (!started) {
        throw new Error(
          "The saved browser request has no matching canonical goal. Nexus kept its marker and did not start new work.",
        );
      }
    }
    if (recovery.desktop_outbox && recovery.intent_sha256
        && String(recovery.outbox_payload_sha256 || recovery.payload_sha256)
        !== String(recovery.intent_sha256)) {
      throw new Error(
        "The backend saved a different exact goal-request digest. Nexus did not start or delete it.",
      );
    }
    if (!terminalState) {
      started = started || await startAndReconcileDirectLongGoalAdmission(
        expected, backendPayloadSha256,
      );
      terminalState = String(started.terminal_state || "reconciled");
    }
    let browserMarker = null;
    try {
      browserMarker = directLongGoalBrowserMarkerForCleanup(
        agentId, recovery.chat_id, recovery.project_id,
      );
    }
    catch (error) { throw error; }
    if (browserMarker && (
      browserMarker.request_id !== recovery.request_id
      || browserMarker.chat_id !== recovery.chat_id
      || browserMarker.project_id !== recovery.project_id
      || browserMarker.lead_id !== recovery.lead_id
      || browserMarker.intent_sha256 !== backendIntentSha256
    )) {
      throw new Error(
        "The browser marker changed before exact terminal reconciliation.",
      );
    }
    if (recovery.desktop_outbox) {
      await removeDirectLongGoalOutbox(
        recovery.chat_id, recovery.request_id,
        recovery.outbox_payload_sha256 || recovery.payload_sha256,
      );
    }
    if (browserMarker && !clearDirectLongGoalRequestMarker(
      agentId, recovery.chat_id, recovery.request_id, backendIntentSha256,
    )) {
      throw new Error(
        "The exact browser marker could not be cleared. The server terminal receipt was not acknowledged.",
      );
    }
    forgetDirectLongGoalRecovery(
      recovery.chat_id, recovery.request_id,
      recovery.outbox_payload_sha256 || recovery.intent_sha256
        || recovery.payload_sha256,
    );
    await bestEffortAcknowledgeDirectLongGoalTerminal(
      expected, terminalState,
      terminalState === "reconciled" ? String(started?.goal?.goal_id || "") : "",
    );
    // An older release could leave several authenticated terminal rows for
    // one chat because it had no client-acknowledgement fence. The backend
    // exposes only the newest row so it can match the one surviving desktop
    // outbox/browser marker. Refresh after every acknowledgement—even a
    // reconciled goal—before Start can be enabled, so any older row becomes
    // visible and must be acknowledged next.
    await refreshDirectLongGoalRecoveries();
    if (!started) {
      sayInTheChatFor(
        agentId,
        "The exact discarded goal request was acknowledged. No provider work was started.",
      );
      return;
    }
    selectLongGoalSnapshot(started.goal);
    if (activeConversationIdFor(agentId) === recovery.chat_id) {
      await refreshTheChatFor(agentId);
    }
    await refreshLongGoals(true);
    const words = longHorizonAdmissionWords(longGoal);
    sayInTheChatFor(agentId, words.detail);
  } catch (error) {
    // A lost response may follow goal creation or provider dispatch. Read-only
    // refreshes are safe; never automatically repeat the admission POST.
    try {
      await Promise.all([
        refreshDirectLongGoalRecoveries(), refreshLongGoals(true),
      ]);
      if (activeConversationIdFor(agentId) === recovery.chat_id) {
        await refreshTheChatFor(agentId);
      }
    } catch (_) { /* Preserve the original admission outcome. */ }
    showError(error.message || String(error));
  } finally {
    directLongGoalRecoveryBusy.delete(recovery.request_id);
    renderWorkRecovery(agentId);
  }
}

async function discardDirectLongGoalAdmission(agentId, exactRecovery = null) {
  const recovery = exactRecovery || directLongGoalRecoveryFor(agentId);
  if (!recovery || directLongGoalRecoveryBusy.has(recovery.request_id)) return;
  if (!window.confirm(
    "Discard this exact saved goal request? Nexus first proves that no goal exists for its request identity. The saved chat transcript is kept, and this action starts no provider work.",
  )) return;
  directLongGoalRecoveryBusy.add(recovery.request_id);
  if (agentId) renderWorkRecovery(agentId);
  try {
    const exactIntent = recovery.outbox_payload_sha256
      || recovery.intent_sha256 || "";
    const exactLead = recovery.lead_id || agentId || "";
    // Validate any present origin-local marker before the server journal or
    // desktop outbox changes. This reader deliberately accepts the exact
    // schema-v3 pre-prepare marker written immediately after durable desktop
    // save, as well as a fully prepared marker.
    const browserMarker = agentId
      ? directLongGoalBrowserMarkerForCleanup(
        exactLead, recovery.chat_id, recovery.project_id,
      ) : null;
    if (browserMarker && (
      browserMarker.request_id !== recovery.request_id
      || browserMarker.intent_sha256 !== exactIntent
    )) {
      throw new Error(
        "The browser marker changed before the exact request could be discarded.",
      );
    }
    const expected = {
      request_id: recovery.request_id,
      chat_id: recovery.chat_id,
      project_id: recovery.project_id || "",
      lead_id: exactLead,
      intent_sha256: exactIntent,
    };
    const outcome = verifiedDirectLongGoalDiscardReceipt(
      await request("/api/long-horizon/discard-admission", {
        method: "POST", body: JSON.stringify({
          request_id: recovery.request_id,
          chat_id: recovery.chat_id,
          project_id: recovery.project_id || "",
          lead_id: exactLead,
          payload_sha256: recovery.server_pending ? recovery.payload_sha256 : "",
          intent_sha256: exactIntent,
        }),
      }),
      expected,
    );
    const sameDesktopRecord = recovery.desktop_outbox
      && !recovery.outbox_conflict && !recovery.outbox_digest_mismatch;
    let desktopOutboxRemoved = false;
    if (sameDesktopRecord) {
      const removed = await removeDirectLongGoalOutbox(
        recovery.chat_id, recovery.request_id,
        recovery.outbox_payload_sha256 || recovery.payload_sha256,
      );
      desktopOutboxRemoved = removed.deleted === true || removed.reason === "missing";
    }
    const retainedDesktopOutbox = (
      (recovery.desktop_outbox && !desktopOutboxRemoved)
      || recovery.outbox_conflict?.desktop_outbox === true
    );
    if (retainedDesktopOutbox) {
      throw new Error(
        "The exact desktop outbox was kept, so the server terminal receipt was not acknowledged.",
      );
    }
    if (browserMarker && !clearDirectLongGoalRequestMarker(
        agentId, recovery.chat_id, recovery.request_id, exactIntent,
        )) {
      throw new Error(
        "The exact browser marker could not be cleared, so the server terminal receipt was not acknowledged.",
      );
    }
    forgetDirectLongGoalRecovery(
      recovery.chat_id, recovery.request_id, exactIntent,
    );
    await bestEffortAcknowledgeDirectLongGoalTerminal(
      expected,
      outcome.reconciled ? "reconciled" : "discarded",
      outcome.reconciled ? String(outcome.goal?.goal_id || "") : "",
    );
    if (outcome.reconciled && outcome.goal) {
      selectLongGoalSnapshot(outcome.goal);
      if (agentId) sayInTheChatFor(agentId,
        "A goal already existed for that exact request, so Nexus reconciled it instead of discarding it.");
    } else {
      if (agentId) sayInTheChatFor(agentId,
        "The exact unadmitted goal request was discarded. No provider work was started by this action.");
    }
    await Promise.all([
      refreshDirectLongGoalRecoveries(), refreshLongGoals(true),
    ]);
    if (agentId && activeConversationIdFor(agentId) === recovery.chat_id) {
      await refreshTheChatFor(agentId);
    }
  } catch (error) {
    showError(String(error.message || error));
  } finally {
    directLongGoalRecoveryBusy.delete(recovery.request_id);
    if (agentId) renderWorkRecovery(agentId);
  }
}

function fillWorkRecoveryPanel(panel, agentId) {
  if (!panel) return;
  const directRecovery = directLongGoalRecoveryFor(agentId);
  if (directRecovery) {
    fillDirectLongGoalRecoveryPanel(panel, agentId, directRecovery);
    return;
  }
  const recovery = workRecoveryFor(agentId);
  panel.hidden = !recovery;
  panel.replaceChildren();
  if (!recovery) return;
  panel.dataset.status = recovery.status;
  panel.append(make("h4", "work-recovery-title", workRecoveryTitle(recovery.status)));
  const recoveryWords = recovery.status === "paused_for_user"
    ? "Nexus changed no files. Answer below to continue this exact saved run."
    : recovery.status === "paused_provider"
      ? "Nexus saved the exact run after a provider failed to answer. Reconnect that provider, then resume; no user answer is required."
      : recovery.status === "paused_tool_budget"
        ? "Nexus charged only time spent inside context tools, saved the exact run, and did not call this a provider outage. Reset the consumed tool time explicitly below, or change the displayed setting before resuming."
      : recovery.status === "incomplete"
        ? "Nexus saved the unfinished run and has not claimed completion. Resume the same run so the team can continue."
        : "Nexus has not claimed completion. Resume the same run so the team can verify or revise what was applied.";
  panel.append(make("p", "work-recovery-status", recoveryWords));
  const needs = recovery.questions.length ? recovery.questions : recovery.remaining;
  if (needs.length) {
    const heading = make("strong", "", recovery.questions.length
      ? "Questions from the team" : "What remains");
    panel.append(heading);
    if (recovery.questions.length) {
      const questions = make("div", "agent-question-list");
      for (const question of recovery.questions) {
        questions.append(userQuestionFields(
          question, recovery.questionAnswers[question.id],
          (answer) => updateWorkRecoveryQuestionAnswer(agentId, question.id, answer),
          `recovery-${agentId}-${recovery.resumeToken}`,
        ));
      }
      panel.append(questions);
    } else {
      const list = make("ul", "work-recovery-items");
      for (const one of needs) list.append(make("li", "", one));
      panel.append(list);
    }
  }
  const scope = make("details", "work-recovery-scope");
  scope.append(make("summary", "", "Locked write destinations"));
  scope.append(make("p", "hint",
    `Project: ${recovery.projectName}. These destinations are read-only and cannot be widened while resuming.`));
  const roots = make("ul", "work-recovery-roots");
  for (const root of recovery.allowedWriteRoots) roots.append(make("li", "", root));
  if (recovery.writeScopeRestricted && !recovery.allowedWriteRoots.length) {
    roots.append(make("li", "", "No project path is writable until you start a new run with valid destinations."));
  } else if (!recovery.allowedWriteRoots.length) {
    roots.append(make("li", "", "No narrower subfolder was recorded; the original project scope remains in force."));
  }
  scope.append(roots);
  if (recovery.contextToolBudget?.summary) {
    scope.append(make("p", "hint", recovery.contextToolBudget.summary));
  }
  panel.append(scope);
  if (recovery.status !== "paused_for_user" || !recovery.questions.length) {
    const label = make("label", "work-recovery-answer-label",
      recovery.status === "paused_for_user" ? "Your answer" : "Optional resume note");
    const answer = make("textarea", "work-recovery-answer");
    answer.rows = recovery.status === "paused_for_user" ? 3 : 2;
    answer.value = recovery.answerDraft;
    answer.placeholder = recovery.status === "paused_for_user"
      ? "Answer the team's questions to resume"
      : "Add useful context, or resume without a note";
    answer.addEventListener("input", () => updateWorkRecoveryAnswer(agentId, answer.value, answer));
    label.append(answer);
    panel.append(label);
  }
  const row = make("div", "button-row work-recovery-actions");
  const resume = make("button", "primary work-recovery-resume",
    recovery.status === "paused_for_user" ? "Answer and resume"
      : recovery.status === "paused_provider" ? "Retry provider and resume"
        : recovery.status === "paused_tool_budget" ? "Reset tool time and resume"
        : recovery.status === "incomplete" ? "Resume project work" : "Resume verification");
  resume.type = "button";
  resume.addEventListener("click", () => resumeSwarmWork(
    agentId, recovery.status === "paused_tool_budget",
  ));
  row.append(resume);
  panel.append(row);
}

function renderWorkRecoveryButtons(agentId) {
  const directRecovery = directLongGoalRecoveryFor(agentId);
  if (directRecovery) {
    const busy = swarmChatIsBusy(agentId)
      || directLongGoalRecoveryBusy.has(directRecovery.request_id);
    const card = theChatCardFor(agentId);
    const recoverButtons = [card?.querySelector(".direct-long-goal-recover")];
    const discardButtons = [card?.querySelector(".direct-long-goal-discard")];
    if (theBigOne === agentId) {
      recoverButtons.push(
      $("theBigChatWorkRecovery")?.querySelector(".direct-long-goal-recover"),
      );
      discardButtons.push(
      $("theBigChatWorkRecovery")?.querySelector(".direct-long-goal-discard"),
      );
    }
    for (const button of recoverButtons.filter(Boolean)) {
      button.disabled = busy || directRecovery.outbox_conflict
        || directRecovery.outbox_digest_mismatch;
      button.title = button.disabled
        ? "Wait for this exact chat operation to finish"
        : "Reconcile this exact saved request without changing or automatically resending it";
    }
    for (const button of discardButtons.filter(Boolean)) {
      button.disabled = busy;
      button.title = busy
        ? "Wait for this exact chat operation to finish"
        : "Prove no goal exists, then discard only this exact pending record";
    }
    return;
  }
  const recovery = workRecoveryFor(agentId);
  const unavailable = swarmChatIsBusy(agentId) || swarmConversationSwitching.has(agentId)
    || !theSwarmAgent(agentId)?.ready;
  const structuredAnswers = compiledQuestionAnswers(
    recovery?.questions || [], recovery?.questionAnswers || {},
  );
  const answerMissing = recovery?.status === "paused_for_user"
    && (recovery.questions.length
      ? structuredAnswers.missing.length > 0
      : !String(recovery.answerDraft || "").trim());
  const card = theChatCardFor(agentId);
  const buttons = [card?.querySelector(".work-recovery-resume")];
  if (theBigOne === agentId) buttons.push($("theBigChatWorkRecovery")
    ?.querySelector(".work-recovery-resume"));
  for (const button of buttons.filter(Boolean)) {
    setSwarmProjectWorkControl(button, unavailable || answerMissing,
      answerMissing ? "Answer the paused questions before resuming" : "Resume this saved project-work run",
      agentId);
  }
}

function renderWorkRecovery(agentId) {
  const card = theChatCardFor(agentId);
  fillWorkRecoveryPanel(card?.querySelector(".swarm-chat-work-recovery"), agentId);
  if (theBigOne === agentId) fillWorkRecoveryPanel($("theBigChatWorkRecovery"), agentId);
  renderWorkRecoveryButtons(agentId);
}

function chatRoundPolicyFor(agentId) {
  const key = swarmChatKey(agentId);
  if (!swarmChatRoundPolicies.has(key)) {
    swarmChatRoundPolicies.set(key, {unlimited: false, maximum: DEFAULT_FINITE_TEAM_ROUNDS});
  }
  return swarmChatRoundPolicies.get(key);
}

function selectedChatRoundLimit(agentId) {
  const policy = chatRoundPolicyFor(agentId);
  return policy.unlimited ? null : policy.maximum;
}

function updateChatRoundPolicy(agentId, unlimited, maximum) {
  const policy = chatRoundPolicyFor(agentId);
  policy.unlimited = Boolean(unlimited);
  const parsed = Math.round(Number(maximum));
  if (Number.isFinite(parsed)) policy.maximum = Math.max(1, Math.min(10000, parsed));
  syncChatRoundPolicy(agentId);
}

function syncChatRoundPolicy(agentId) {
  const policy = chatRoundPolicyFor(agentId);
  const card = theChatCardFor(agentId);
  const compactMaximum = card?.querySelector(".swarm-chat-round-limit");
  const compactUnlimited = card?.querySelector(".swarm-chat-round-unlimited");
  if (compactMaximum) {
    compactMaximum.value = String(policy.maximum);
    compactMaximum.disabled = policy.unlimited || swarmChatIsBusy(agentId);
  }
  if (compactUnlimited) {
    compactUnlimited.checked = policy.unlimited;
    compactUnlimited.disabled = swarmChatIsBusy(agentId);
  }
  if (theBigOne === agentId && $("theBigChatRoundLimit")) {
    $("theBigChatRoundLimit").value = String(policy.maximum);
    $("theBigChatRoundLimit").disabled = policy.unlimited || swarmChatIsBusy(agentId);
    $("theBigChatUnlimited").checked = policy.unlimited;
    $("theBigChatUnlimited").disabled = swarmChatIsBusy(agentId);
  }
}

function aChatRoundPolicy(agentId) {
  const policy = chatRoundPolicyFor(agentId);
  const panel = make("div", "chat-round-policy");
  panel.append(make("strong", "", "Connected-agent collaboration only"));
  const maximumLabel = make("label", "chat-round-maximum", "Maximum relay rounds");
  const maximum = document.createElement("input");
  maximum.type = "number";
  maximum.min = "1";
  maximum.max = "10000";
  maximum.value = String(policy.maximum);
  maximum.className = "swarm-chat-round-limit";
  maximum.setAttribute("aria-label", "Maximum relay rounds for connected-agent collaboration");
  maximum.disabled = policy.unlimited;
  maximum.addEventListener("change", () => (
    updateChatRoundPolicy(agentId, false, maximum.value)
  ));
  maximumLabel.append(maximum);
  const unlimitedLabel = make("label", "chat-round-unlimited");
  const unlimited = document.createElement("input");
  unlimited.type = "checkbox";
  unlimited.className = "swarm-chat-round-unlimited";
  unlimited.checked = policy.unlimited;
  unlimited.addEventListener("change", () => (
    updateChatRoundPolicy(agentId, unlimited.checked, maximum.value)
  ));
  unlimitedLabel.append(unlimited, document.createTextNode(
    " Unlimited while progress continues (advanced opt-in)"));
  panel.append(maximumLabel, unlimitedLabel);
  panel.append(make("span", "hint chat-round-help",
    "Ask connected agents stops after 3 relay rounds by default. Unlimited is an explicit opt-in. Project-file Work uses the separate goal engine."));
  return panel;
}

function activeConversationIdFor(agentId) {
  return activeConversationFor(agentId)?.id || "";
}

function transcriptIdentityFor(agentId) {
  return activeConversationIdFor(agentId) || "legacy";
}

function keptTranscriptFor(agentId) {
  const held = swarmChats.find((one) => one.agent === agentId);
  return held?.saidFor === transcriptIdentityFor(agentId) && Array.isArray(held.said)
    ? held.said : [];
}

function keptChatNoticeFor(agentId) {
  const held = swarmChats.find((one) => one.agent === agentId);
  return held?.noticeFor === transcriptIdentityFor(agentId)
    ? String(held.notice || "") : "";
}

function nextConversationListRevision(agentId) {
  const revision = (swarmConversationListRevisions.get(agentId) || 0) + 1;
  swarmConversationListRevisions.set(agentId, revision);
  return revision;
}

function nextSwarmChatRevision(agentId) {
  const key = swarmChatKey(agentId);
  const revision = Math.max(
    swarmChatRevisions.get(key) || 0,
    swarmChatRevisions.get(agentId) || 0,
  ) + 1;
  swarmChatRevisions.set(key, revision);
  // The aggregate key invalidates a read when the active conversation changes;
  // the pair-chat key keeps revisions independent once it is selected again.
  swarmChatRevisions.set(agentId, revision);
  return revision;
}

function beginConversationRead(controllers, agentId) {
  const controller = new AbortController();
  let lane = controllers.get(agentId);
  if (!lane) {
    lane = new Set();
    controllers.set(agentId, lane);
  }
  lane.add(controller);
  return controller;
}

function finishConversationRead(controllers, agentId, controller) {
  const lane = controllers.get(agentId);
  if (!lane) return;
  lane.delete(controller);
  if (!lane.size) controllers.delete(agentId);
}

function cancelConversationReadLane(controllers, agentId) {
  for (const controller of controllers.get(agentId) || []) controller.abort();
  controllers.delete(agentId);
}

// Closing/removing a chat discards its read intent. A surviving chat on a new
// board revision is different: its old transcript request is stale, but the
// next metadata read must inherit that intent and fetch the durable answer.
function cancelConversationReadsFor(agentId, preserveTranscriptIntent = false) {
  const transcriptWasRequested = swarmConversationTranscriptRefreshes.has(agentId) || Boolean(
    swarmConversationTranscriptControllers.get(agentId)?.size
  );
  cancelConversationReadLane(swarmConversationListControllers, agentId);
  cancelConversationReadLane(swarmConversationTranscriptControllers, agentId);
  if (preserveTranscriptIntent && transcriptWasRequested) {
    swarmConversationTranscriptRefreshes.add(agentId);
  } else {
    swarmConversationTranscriptRefreshes.delete(agentId);
  }
  nextConversationListRevision(agentId);
  nextSwarmChatRevision(agentId);
}

function conversationReadWasCancelled(error) {
  return error?.name === "AbortError";
}

function aChatActivityPanel(extraClass = "") {
  const panel = make("section", `chat-activity ${extraClass}`.trim());
  panel.hidden = true;
  panel.setAttribute("role", "status");
  panel.setAttribute("aria-live", "polite");
  panel.setAttribute("aria-atomic", "true");
  const heading = make("div", "chat-activity-heading");
  const spinner = make("span", "chat-activity-spinner");
  spinner.setAttribute("aria-hidden", "true");
  heading.append(spinner);
  heading.append(make("strong", "chat-activity-stage", "Starting the request"));
  heading.append(make("span", "chat-activity-elapsed", "Working now"));
  panel.append(heading);
  panel.append(make("p", "chat-activity-detail", "Nexus is preparing the agent connection."));
  const track = make("div", "chat-activity-track");
  track.setAttribute("aria-hidden", "true");
  track.append(make("span"));
  panel.append(track);
  return panel;
}

function activityWords(activity) {
  const seconds = Math.max(0, Math.floor((Date.now() - activity.startedAt) / 1000));
  if (activity.settled && activity.state === "attention") return `Needs attention after ${seconds}s`;
  if (activity.terminalState === "admitted") return `Accepted in ${seconds}s`;
  if (activity.state === "complete") return `Completed in ${seconds}s`;
  if (activity.state === "error") return `Stopped after ${seconds}s`;
  return seconds ? `Working for ${seconds}s` : "Working now";
}

function showActivityInPanel(panel, activity) {
  panel.hidden = !activity;
  if (!activity) return;
  panel.dataset.state = activity.state;
  panel.querySelector(".chat-activity-stage").textContent = activity.stage;
  panel.querySelector(".chat-activity-detail").textContent = activity.detail;
  panel.querySelector(".chat-activity-elapsed").textContent = activityWords(activity);
}

function renderSwarmChatActivity(agentId, chatKey = swarmChatRuntimeKey(agentId)) {
  const activity = visibleSwarmChatActivity(swarmChatActivity.get(chatKey));
  const panels = [];
  for (const held of swarmChats) {
    if (swarmChatRuntimeKey(held.agent) !== chatKey) continue;
    const card = theChatCardFor(held.agent);
    if (card) panels.push(card.querySelector(".chat-activity"));
    if (theBigOne === held.agent) panels.push($("theBigChatActivity"));
  }
  for (const panel of panels.filter(Boolean)) {
    showActivityInPanel(panel, activity);
  }
}

async function pollSwarmChatActivity(agentId, chatKey, activityId) {
  const current = swarmChatActivity.get(chatKey);
  if (!current || current.id !== activityId || !swarmActivityCanSettle(current)) return;
  try {
    const update = await request(
      `/api/swarm/activity?activity=${encodeURIComponent(activityId)}`
    );
    const still = swarmChatActivity.get(chatKey);
    if (!still || still.id !== activityId || !swarmActivityCanSettle(still)) return;
    if (still.state === "working") {
      still.stage = update.stage || still.stage;
      still.detail = update.detail || still.detail;
    }
    const turns = Array.isArray(update.turns) ? update.turns : [];
    if (turns.length !== still.remoteTurns.length) {
      still.remoteTurns = turns;
      renderTurnsThatArrived(agentId, chatKey);
    }
    if (["complete", "error", "stopped"].includes(String(update.state || ""))) {
      settleSwarmChatActivityFromFeed(agentId, still, update);
      return;
    }
    if (selectedChatIs(still)) renderSwarmChatActivity(agentId, chatKey);
  } catch (_) {
    // The main request owns errors. A missed progress poll must never turn a
    // healthy provider answer into a failed chat.
  }
  const still = swarmChatActivity.get(chatKey);
  if (still && still.id === activityId && swarmActivityCanSettle(still)) {
    still.pollTimer = window.setTimeout(
      () => pollSwarmChatActivity(agentId, chatKey, activityId), 450
    );
  }
}

function beginSwarmChatActivity(agentId, mode, agent, words = "", attachments = []) {
  const conversation = activeConversationFor(agentId);
  const chatKey = swarmChatRuntimeKey(agentId);
  const stateKey = swarmChatKey(agentId);
  const previous = swarmChatActivity.get(chatKey);
  if (previous) {
    window.clearInterval(previous.elapsedTimer);
    window.clearTimeout(previous.pollTimer);
    window.clearTimeout(previous.finishTimer);
  }
  const id = globalThis.crypto?.randomUUID
    ? globalThis.crypto.randomUUID()
    : `nexus-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
  const first = {
    auto: ["Reading your request", "Nexus will decide whether connected agents should help."],
    chat: [`Contacting ${agent?.name || "the selected agent"}`, "Nexus is sending a direct chat request."],
    collaborate: ["Preparing the connected-agent relay", "Nexus will contact ready agents on green communication lines."],
    work: ["Preparing confirmed project work", "Nexus will collect plans before validating any file changes."],
  }[mode] || ["Starting the request", "Nexus is preparing the agent connection."];
  const activity = {
    agentId, chatKey, stateKey, chatId: conversation?.id || "",
    filedAs: conversation?.filed_as || "", route: String(agent?.who || ""),
    id, state: "working", settled: false, settledBy: "", terminalState: "",
    responseFinished: false, collapsed: false, attachmentsCleared: false,
    stage: first[0], detail: first[1], startedAt: Date.now(),
    elapsedTimer: 0, pollTimer: 0, finishTimer: 0, remoteTurns: [],
    localTurns: words ? [{
      who: "you", speaker_name: "You", text: words,
      phase: mode === "work" ? "long_horizon_prompt" : "user_prompt",
      attachments: attachments.map((one) => ({
        name: one.name, type: one.type, image: String(one.type || "").startsWith("image/"),
      })),
    }] : [],
  };
  swarmChatActivity.set(chatKey, activity);
  activity.elapsedTimer = window.setInterval(() => {
    if (selectedChatIs(activity)) renderSwarmChatActivity(agentId, chatKey);
  }, 1000);
  renderSwarmChatActivity(agentId, chatKey);
  renderTurnsThatArrived(agentId, chatKey);
  activity.pollTimer = window.setTimeout(
    () => pollSwarmChatActivity(agentId, chatKey, id), 150,
  );
  return activity;
}

function scheduleSwarmChatActivityCollapse(agentId, activity, delay) {
  window.clearTimeout(activity.finishTimer);
  activity.finishTimer = window.setTimeout(() => {
    if (!swarmActivityIsCurrent(activity)) return;
    activity.collapsed = true;
    if (activity.responseFinished) swarmChatActivity.delete(activity.chatKey);
    renderSwarmChatActivity(agentId, activity.chatKey);
    if (theBigOne === agentId) renderTheBigChat();
  }, delay);
}

function finishSwarmActivityResponse(agentId, activity) {
  activity.responseFinished = true;
  if (!activity.collapsed || !swarmActivityIsCurrent(activity)) return;
  swarmChatActivity.delete(activity.chatKey);
  renderSwarmChatActivity(agentId, activity.chatKey);
  if (theBigOne === agentId) renderTheBigChat();
}

function settleSwarmChatActivityFromFeed(agentId, activity, update) {
  if (!swarmActivityCanSettle(activity)) return;
  activity.settled = true;
  activity.settledBy = "feed";
  activity.terminalState = String(update.state || "error");
  window.clearInterval(activity.elapsedTimer);
  window.clearTimeout(activity.pollTimer);
  const succeeded = update.state === "complete";
  const participantOutcome = normalizedParticipantOutcome({
    participant_outcome: update?.result?.participant_outcome,
  });
  const degraded = participantOutcome && participantOutcome.outcome !== "complete";
  activity.state = degraded ? "attention" : succeeded ? "complete" : "error";
  activity.stage = degraded
    ? `${participantOutcome.answered} of ${participantOutcome.expected} agents answered`
    : update.stage || (succeeded ? "Answer received" : "Request stopped");
  activity.detail = degraded
    ? (participantOutcome.outcome === "none"
      ? "No AI answer was saved. The transcript has exact Repair actions."
      : "Available replies were saved. The transcript has exact Repair actions for missing participants.")
    : update.detail || (succeeded
      ? "Nexus saved the answer and updated the conversation."
      : "The request ended before an answer was saved.");
  if (succeeded) {
    activity.localTurns = [];
    activity.remoteTurns = [];
    clearSwarmActivityAttachments(activity);
  } else {
    // A lost HTTP rejection must not strand the accepted prompt. Restore it
    // from the immutable activity snapshot now; a later transport catch sees
    // the terminal feed and performs no duplicate UI mutation.
    restoreSwarmActivityDraft(activity);
  }
  if (selectedChatIs(activity)) {
    renderSwarmChatActivity(agentId, activity.chatKey);
    renderTurnsThatArrived(agentId, activity.chatKey);
  }
  // The activity feed is authoritative even if the original HTTP response was
  // lost. A terminal server run must release this renderer's composer lease.
  // Late responses are identity-guarded and cannot finish a newer request.
  swarmBusy.delete(activity.chatKey);
  swarmStopping.delete(activity.chatKey);
  setWhatCanBePressedInSwarm();
  for (const held of swarmChats) {
    if (swarmChatRuntimeKey(held.agent) === activity.chatKey) {
      void refreshTheChatFor(held.agent);
    }
  }
  if (theBigOne === agentId) renderTheBigChat();
  scheduleSwarmChatActivityCollapse(agentId, activity,
    succeeded && !degraded ? 1600 : 4200);
}

function finishSwarmChatActivity(
  agentId, succeeded, detail = "", expectedActivity = null, rawParticipantOutcome = null,
) {
  const activity = expectedActivity || swarmChatActivityFor(agentId);
  if (!activity || (expectedActivity && activity !== expectedActivity)) return;
  if (!swarmActivityCanSettle(activity)) return;
  activity.settled = true;
  activity.settledBy = "response";
  activity.terminalState = succeeded ? "complete" : "error";
  window.clearInterval(activity.elapsedTimer);
  window.clearTimeout(activity.pollTimer);
  const participantOutcome = normalizedParticipantOutcome({
    participant_outcome: rawParticipantOutcome,
  });
  const degraded = participantOutcome && participantOutcome.outcome !== "complete";
  activity.state = degraded ? "attention" : succeeded ? "complete" : "error";
  if (succeeded) {
    activity.localTurns = [];
    activity.remoteTurns = [];
  }
  activity.stage = degraded
    ? `${participantOutcome.answered} of ${participantOutcome.expected} agents answered`
    : succeeded ? "Answer received" : "Request stopped";
  activity.detail = degraded
    ? (participantOutcome.outcome === "none"
      ? "No AI answer was saved. The transcript has exact Repair actions."
      : "Available replies were saved. The transcript has exact Repair actions for missing participants.")
    : detail || (succeeded
      ? "Nexus saved the answer and updated the conversation."
      : "The request ended before an answer was saved.");
  if (selectedChatIs(activity)) {
    renderSwarmChatActivity(agentId, activity.chatKey);
    renderTurnsThatArrived(agentId, activity.chatKey);
  }
  if (theBigOne === agentId) renderTheBigChat();
  // Failures are now durable Nexus transcript turns. Keep the terminal status
  // briefly for orientation, then collapse it so it never looks like a live
  // request or strands the composer below a permanent progress panel.
  scheduleSwarmChatActivityCollapse(agentId, activity,
    succeeded && !degraded ? 1600 : 4200);
}

function longHorizonAdmissionWords(goal) {
  const goalId = String(goal?.goal_id || "").slice(0, 8) || "unknown";
  const status = String(goal?.status || "unknown");
  if (status === "waiting_for_project") {
    return {
      stage: "Goal waiting for project",
      detail: `Durable goal ${goalId} was accepted and will start after the current project owner releases it.`,
    };
  }
  if (status === "queued") {
    return {stage: "Goal accepted and queued", detail: `Durable goal ${goalId} is queued in Mission control.`};
  }
  if (status === "running") {
    return {stage: "Goal accepted and running", detail: `Durable goal ${goalId} is running in Mission control.`};
  }
  if (status === "paused") {
    return {stage: "Goal accepted but paused", detail: `Durable goal ${goalId} is paused and is not complete.`};
  }
  if (status === "waiting_for_user") {
    return {stage: "Goal needs your input", detail: `Durable goal ${goalId} is waiting for you in Mission control.`};
  }
  if (status === "complete") {
    return {stage: "Goal verified complete", detail: `Durable goal ${goalId} already has verified completion evidence.`};
  }
  if (status === "failed") {
    return {stage: "Goal failed", detail: `Durable goal ${goalId} failed; Mission control has the recorded reason.`};
  }
  if (status === "cancelling") {
    return {
      stage: "Goal cancellation in progress",
      detail: `Durable goal ${goalId} is draining active work and has not released the project yet.`,
    };
  }
  if (status === "cancelled") {
    return {stage: "Goal cancelled", detail: `Durable goal ${goalId} was cancelled and is not complete.`};
  }
  return {stage: "Goal status received", detail: `Durable goal ${goalId} has status “${status}” in Mission control.`};
}

function finishLongHorizonAdmissionActivity(agentId, goal, expectedActivity) {
  const activity = expectedActivity || swarmChatActivityFor(agentId);
  if (!activity || (expectedActivity && activity !== expectedActivity)
      || !swarmActivityCanSettle(activity)) return;
  const words = longHorizonAdmissionWords(goal);
  const status = String(goal?.status || "unknown");
  activity.settled = true;
  activity.settledBy = "response";
  activity.terminalState = "admitted";
  activity.state = ["waiting_for_project", "paused", "waiting_for_user", "failed", "cancelling", "cancelled"]
    .includes(status) ? "attention" : "complete";
  activity.stage = words.stage;
  activity.detail = `${words.detail} The submitted prompt and canonical goal status are saved in this chat.`;
  activity.localTurns = [];
  activity.remoteTurns = [];
  window.clearInterval(activity.elapsedTimer);
  window.clearTimeout(activity.pollTimer);
  if (selectedChatIs(activity)) {
    renderSwarmChatActivity(agentId, activity.chatKey);
    renderTurnsThatArrived(agentId, activity.chatKey);
  }
  if (theBigOne === agentId) renderTheBigChat();
  scheduleSwarmChatActivityCollapse(agentId, activity,
    status === "queued" || status === "running" ? 2400 : 4200);
}

function markSwarmChatActivityStopping(agentId, activity = swarmChatActivityFor(agentId)) {
  if (!activity || !swarmActivityCanSettle(activity)) return;
  activity.state = "stopping";
  activity.stage = "Stopping";
  activity.detail = "Nexus is interrupting only this chat's current request.";
  if (selectedChatIs(activity)) renderSwarmChatActivity(agentId, activity.chatKey);
  if (theBigOne === agentId) renderTheBigChat();
}

function sayInSwarm(words) { $("swarmSaid").textContent = words; }

function theSwarmBoard() { return swarmSaid.board; }

function theSwarmAgent(id) {
  return theSwarmBoard().agents.find((one) => one.id === id) || null;
}

function theSwarmProject(id) {
  return theSwarmBoard().projects.find((one) => one.id === id) || null;
}

function thePickedAgent() {
  return swarmPicked && swarmPicked.kind === "agent" ? theSwarmAgent(swarmPicked.id) : null;
}

function thePickedProject() {
  return swarmPicked && swarmPicked.kind === "project" ? theSwarmProject(swarmPicked.id) : null;
}

function thePickedLine() {
  return swarmPicked && (swarmPicked.kind === "works" || swarmPicked.kind === "talks")
    ? swarmPicked : null;
}

// ---- the small pictures --------------------------------------------------
//
// Drawn here rather than fetched, so the panel needs nothing from the network,
// and drawn as shapes rather than letters so they are the same on every
// machine. No emoji: they are a different picture in every font.

const SWARM_DRAWINGS = {
  gear: [
    "M12 8.5a3.5 3.5 0 1 0 0 7 3.5 3.5 0 0 0 0-7z",
    "M12 2.5l1.6 2.2 2.6-.7.6 2.7 2.4 1.2-1.3 2.4 1.3 2.4-2.4 1.2-.6 2.7-2.6-.7L12 21.5l-1.6-2.2-2.6.7-.6-2.7-2.4-1.2 1.3-2.4-1.3-2.4 2.4-1.2.6-2.7 2.6.7z",
  ],
  chat: [
    "M4 5.5h16v10H9l-5 4z",
  ],
  robot: [
    "M8 3.5h8v3h-8z",
    "M5 7.5h14v11H5z",
    "M8.5 11.5h1.5v2.5h-1.5z",
    "M14 11.5h1.5v2.5h-1.5z",
    "M12 1.5v2",
  ],
  folder: [
    "M3.5 6.5h6l2 2.5h9v10h-17z",
  ],
  // A person, for the turns in a chat that are yours. Without one, your own
  // words and an assistant's carry the same little robot and the two read as
  // the same voice.
  person: [
    "M12 3.5a4 4 0 1 0 0 8 4 4 0 0 0 0-8z",
    "M4.5 20.5c0-4 3.4-6.5 7.5-6.5s7.5 2.5 7.5 6.5",
  ],
  cross: [
    "M5 5l14 14",
    "M19 5L5 19",
  ],
  code: [
    "M9 7l-5 5 5 5",
    "M15 7l5 5-5 5",
    "M13.5 4l-3 16",
  ],
  star: [
    "M12 2.8l2.8 5.7 6.3.9-4.6 4.5 1.1 6.3-5.6-3-5.6 3 1.1-6.3-4.6-4.5 6.3-.9z",
  ],
  brain: [
    "M9.5 5.5a3 3 0 0 0-5 2.2 3 3 0 0 0 .2 5.7A3.2 3.2 0 0 0 8 18.5h1.5z",
    "M14.5 5.5a3 3 0 0 1 5 2.2 3 3 0 0 1-.2 5.7 3.2 3.2 0 0 1-3.3 5.1h-1.5z",
    "M9.5 7.5v11M14.5 7.5v11M7 11h2.5M14.5 11H17",
  ],
  minus: [
    "M5 12h14",
  ],
};

function aSwarmDrawing(which, size) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("width", String(size || 16));
  svg.setAttribute("height", String(size || 16));
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  for (const drawn of SWARM_DRAWINGS[which] || []) {
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", drawn);
    path.setAttribute("fill", "none");
    path.setAttribute("stroke", "currentColor");
    path.setAttribute("stroke-width", "1.6");
    path.setAttribute("stroke-linejoin", "round");
    path.setAttribute("stroke-linecap", "round");
    svg.append(path);
  }
  return svg;
}

// A button with a picture on it and words for anybody who cannot see the
// picture. The words are on the button itself as well, in small text, because
// a picture nobody can name is a button nobody presses.
//
// Two sets of words, on purpose. `words` is what is written on it, kept short
// because it sits on a small button. `about` is what it is read out as, and
// says which box or which line it belongs to - going through a page by its
// buttons, twelve of them all saying "settings" is twelve buttons nobody can
// tell apart. What a check presses it by is `does`, which does not change when
// somebody is renamed.
function aSwarmButton(className, which, words, whenPressed, about, does) {
  const button = make("button", className);
  button.type = "button";
  button.title = about || words;
  button.setAttribute("aria-label", about || words);
  if (does) button.dataset.does = does;
  button.append(aSwarmDrawing(which, 15));
  button.append(make("span", "swarm-button-words", words));
  button.addEventListener("click", (event) => {
    event.preventDefault();
    event.stopPropagation();
    whenPressed();
  });
  // A press on a button is not a press on the box underneath it, and it is
  // certainly not the start of a drag.
  button.addEventListener("pointerdown", (event) => event.stopPropagation());
  return button;
}

// A read must not undo a change that landed while it was in flight.
//
// Reading the board and saving it are two different requests, and the answers
// can arrive in either order however carefully the server does them one at a
// time. Drag a box and press Look again in the same second, and the read - sent
// off before the drag landed - could come back last and put the box back where
// it was, while the new place was already written down. Nothing said a word.
//
// So a read counts the changes that had landed when it was sent, and throws its
// answer away if any landed while it was waiting. Counting rather than comparing
// version numbers: a board whose file somebody has deleted starts again at
// version nothing, and a panel that refused to believe anything older than what
// it holds would sit there showing a board that is not on the disk any more.
let howManyChangesLanded = 0;

async function refreshSwarm(quietly, {recoveryOnly = false} = {}) {
  const mine = ++swarmNewestRefresh;
  const changesThen = howManyChangesLanded;
  try {
    // The board and saved-board list are local files; provider discovery runs
    // several installed CLIs and is orders of magnitude slower. On the first
    // read, ask for the local state only so there is something useful on
    // screen immediately. Once it is drawn, refresh provider status in the
    // background. Later presses of Look again still perform a full refresh.
    const firstHydration = !swarmBoardHydrated;
    const said = await request(
      firstHydration ? "/api/swarm?refresh_providers=false" : "/api/swarm"
    );
    if (mine !== swarmNewestRefresh) return;
    if (changesThen !== howManyChangesLanded) return;
    swarmSaid = said;
    showProjectAuthorityPause(said.authority, said.cannot_run);
    swarmBoardHydrated = true;
    acceptKeptInventory(said, true);
    keepTheSwarmPick();
    renderSwarmBoard();
    renderSwarmNotReady();
    renderSwarmPanel();
    renderTheChatsOnThisBoard();
    renderTheKeptBoards();
    renderTheChatTray();
    if (theBigOne) renderTheBigChat();
    // The recovery notice may have been drawn before this first local board
    // read completed. Enable its exact-chat action immediately; a second
    // journal read is useful reconciliation, never the hydration authority.
    renderDirectLongGoalBoardRecoveryNotice();
    // Route/profile and project-path changes are authority changes for an
    // existing chat even when its short agent ids stayed the same. Re-read the
    // small registry snapshots so the protection card and one-click fresh-chat
    // action appear immediately after a repair, external edit, or board open.
    void Promise.all(swarmChats.map(
      (held) => loadConversationsFor(held.agent, false)
    )).then(() => refreshDirectLongGoalRecoveries()).catch(() => {
      // Each read owns its normal visible error path. Recovery hydration stays
      // read-only and will retry on the next board refresh.
    });
    if (!recoveryOnly) {
      // The signed run journal can contain substantial history and its first
      // open performs integrity verification. Draw the saved board immediately;
      // recovery cards hydrate independently a moment later.
      void refreshDurableSwarmWorkRecoveries();
      // The goal cursor is server-owned. Rehydrate it independently so closing
      // or reloading this renderer cannot restart at goal one or strand a queued
      // successor after the preceding goal verified. Recovery-only startup
      // deliberately skips these continuation paths: opening a saved chat is
      // never authority to dispatch unrelated provider work.
      void refreshBoardGoalQueue(false);
      void refreshLongGoals(true);
      // What the agents passed to each other, so the list down the side holds
      // those conversations too rather than only the ones you have had. It is
      // small, and without it the list is half a list until somebody opens the
      // fold at the bottom of the board.
      await refreshWhatTheySaidToEachOther();
      const doing = await readSwarmBoardRun(swarmBoardRunId, swarmBoardCursor);
      swarmBoardCursor = Number(
        (doing || {}).next_cursor ?? (doing || {}).cursor ?? swarmBoardCursor,
      );
      if (doing && !doing.going) {
        swarmBoardRequestId = "";
        localStorage.removeItem("nexus.swarm.board-request");
      }
      renderWhatTheyAreDoing(doing);
      if (doing && doing.going) watchWhatTheyAreDoing();
    }
    if (!quietly) sayInSwarm(whatTheBoardSays());
    if (!recoveryOnly && said.provider_status_stale && mine === swarmNewestRefresh) {
      // Do not await this: the durable board is already interactive. This
      // second pass only decorates it with newly discovered provider status.
      void refreshSwarm(true);
    }
  } catch (error) {
    if (mine !== swarmNewestRefresh) return;
    showError(error.message);
    sayInSwarm(error.message);
  }
}

// What was picked, or a chat that was open, may have been removed in another
// window. Rather than a panel showing an agent that is not there, it falls away.
function keepTheSwarmPick() {
  const boardVersion = String(theSwarmBoard()?.version ?? "");
  if (swarmConversationBoardVersion !== null
      && boardVersion !== swarmConversationBoardVersion) {
    for (const held of swarmChats) {
      cancelConversationReadsFor(held.agent, Boolean(theSwarmAgent(held.agent)));
    }
  }
  swarmConversationBoardVersion = boardVersion;
  for (const held of swarmChats) {
    if (theSwarmAgent(held.agent)) continue;
    cancelConversationReadsFor(held.agent);
    swarmConversationHydrating.delete(held.agent);
  }
  swarmChats = swarmChats.filter((one) => theSwarmAgent(one.agent));
  for (const agentId of swarmAgentSettingDrafts.keys()) {
    if (!theSwarmAgent(agentId)) discardSwarmAgentSettings(agentId);
  }
  if (!swarmPicked) return;
  if (swarmPicked.kind === "agent" && !theSwarmAgent(swarmPicked.id)) swarmPicked = null;
  if (swarmPicked.kind === "project" && !theSwarmProject(swarmPicked.id)) swarmPicked = null;
  if (swarmPicked.kind === "works"
    && !(theSwarmAgent(swarmPicked.agent) && theSwarmProject(swarmPicked.project))) {
    swarmPicked = null;
  }
  if (swarmPicked && swarmPicked.kind === "talks"
    && !(theSwarmAgent(swarmPicked.one) && theSwarmAgent(swarmPicked.other))) {
    swarmPicked = null;
  }
}

function whatTheBoardSays() {
  const board = theSwarmBoard();
  const active = swarmKept.find((one) => one.active);
  const returned = active ? `Opened ${active.name}, the saved board you used last. ` : "";
  if (!board.agents.length && !board.projects.length) {
    return `${returned}Nothing on the board yet. Press Add another agent to get started.`;
  }
  const agents = `${board.agents.length} agent${board.agents.length === 1 ? "" : "s"}`;
  const projects = `${board.projects.length} project${board.projects.length === 1 ? "" : "s"}`;
  return `${returned}${agents} and ${projects} on the board. Every box has a gear and a chat button.`;
}

// ---- drawing it ----------------------------------------------------------

const SWARM_ZOOM_MIN = 0.35;
const SWARM_ZOOM_MAX = 1.8;
let swarmZoom = 1;

// The scroll surface is the scaled size; the canvas inside it keeps the
// board's own coordinates. That lets lines, boxes, chats, dragging, and saved
// positions all continue to speak the same language at every zoom level.
function sizeTheSwarmCanvas() {
  const board = $("swarmBoard");
  const surface = $("swarmSurface");
  const canvas = $("swarmCanvas");
  let width = Math.max(600, board.clientWidth / swarmZoom);
  let height = Math.max(500, board.clientHeight / swarmZoom);
  for (const one of canvas.querySelectorAll(".swarm-box, .swarm-chat-card")) {
    width = Math.max(width, one.offsetLeft + one.offsetWidth + 80);
    height = Math.max(height, one.offsetTop + one.offsetHeight + 80);
  }
  canvas.style.width = `${Math.ceil(width)}px`;
  canvas.style.height = `${Math.ceil(height)}px`;
  canvas.style.transform = `scale(${swarmZoom})`;
  surface.style.width = `${Math.ceil(width * swarmZoom)}px`;
  surface.style.height = `${Math.ceil(height * swarmZoom)}px`;
  for (const sheet of [$("swarmLines"), $("swarmPointer")]) {
    sheet.setAttribute("width", String(Math.ceil(width)));
    sheet.setAttribute("height", String(Math.ceil(height)));
    sheet.style.width = `${Math.ceil(width)}px`;
    sheet.style.height = `${Math.ceil(height)}px`;
  }
}

function sayTheSwarmZoom() {
  $("swarmZoomValue").textContent = `${Math.round(swarmZoom * 100)}%`;
  $("swarmZoomOut").disabled = swarmZoom <= SWARM_ZOOM_MIN;
  $("swarmZoomIn").disabled = swarmZoom >= SWARM_ZOOM_MAX;
}

function setTheSwarmZoom(wanted, keepTheMiddle = true) {
  const board = $("swarmBoard");
  const middle = {
    x: (board.scrollLeft + board.clientWidth / 2) / swarmZoom,
    y: (board.scrollTop + board.clientHeight / 2) / swarmZoom,
  };
  swarmZoom = Math.max(SWARM_ZOOM_MIN, Math.min(SWARM_ZOOM_MAX, wanted));
  sizeTheSwarmCanvas();
  drawSwarmLines();
  if (keepTheMiddle) {
    board.scrollLeft = middle.x * swarmZoom - board.clientWidth / 2;
    board.scrollTop = middle.y * swarmZoom - board.clientHeight / 2;
  }
  sayTheSwarmZoom();
}

function fitTheWholeSwarm() {
  const board = $("swarmBoard");
  const things = [...$("swarmCanvas").querySelectorAll(".swarm-box, .swarm-chat-card")];
  if (!things.length) { setTheSwarmZoom(1, false); return; }
  const left = Math.min(...things.map((one) => one.offsetLeft));
  const top = Math.min(...things.map((one) => one.offsetTop));
  const right = Math.max(...things.map((one) => one.offsetLeft + one.offsetWidth));
  const bottom = Math.max(...things.map((one) => one.offsetTop + one.offsetHeight));
  const wanted = Math.min((board.clientWidth - 40) / Math.max(1, right - left),
    (board.clientHeight - 40) / Math.max(1, bottom - top));
  setTheSwarmZoom(wanted, false);
  board.scrollLeft = Math.max(0, left * swarmZoom - 20);
  board.scrollTop = Math.max(0, top * swarmZoom - 20);
}

function renderSwarmBoard() {
  // Board refreshes replace chat cards. Preserve an in-progress composer while
  // doing so; otherwise a refresh that happens after paste/copy destroys the
  // focused textarea and leaves the user typing into a detached element.
  const composerState = new Map();
  const focusedBoardBox = document.activeElement?.closest?.(".swarm-box");
  const focusedBoardControl = focusedBoardBox ? {
    kind: focusedBoardBox.dataset.kind,
    id: focusedBoardBox.dataset.id,
    does: document.activeElement?.dataset.does || "pick",
  } : null;
  const focusedLineKey = document.activeElement?.closest?.(".swarm-line-tools")?.dataset.focusKey || "";
  let focusedComposer = null;
  for (const box of document.querySelectorAll(".swarm-chat-card .swarm-chat-box")) {
    const agentId = box.closest(".swarm-chat-card")?.dataset.agent;
    if (!agentId) continue;
    const state = {
      value: box.value,
      start: box.selectionStart,
      end: box.selectionEnd,
      direction: box.selectionDirection,
    };
    composerState.set(agentId, state);
    const chatKey = swarmChatKey(agentId);
    swarmChatComposerDrafts.set(chatKey, state);
    swarmChatComposerKeys.set(agentId, chatKey);
    if (document.activeElement === box) focusedComposer = agentId;
  }
  // Anything pointed at is about to be a different element. Left behind, the
  // line stays on screen pointing at a box that no longer exists.
  stopPointing();
  const board = $("swarmBoard");
  const canvas = $("swarmCanvas");
  for (const old of [...board.querySelectorAll(
    ".swarm-box, .swarm-empty, .swarm-line-tools, .swarm-chat-card")]) {
    old.remove();
  }
  const said = theSwarmBoard();
  if (!swarmBoardHydrated) {
    canvas.append(make("div", "swarm-empty", "Loading your saved board…"));
  } else if (!said.agents.length && !said.projects.length) {
    canvas.append(make("div", "swarm-empty",
      "Nothing on the board yet. Press Add another agent under the board, then Add "
      + "another project folder, then press the gear on the line between them."));
  }
  for (const one of said.agents) canvas.append(oneSwarmBox("agent", one));
  for (const one of said.projects) canvas.append(oneSwarmBox("project", one));
  for (const one of swarmChats) {
    if (!one.minimised) canvas.append(oneSwarmChatCard(one));
  }
  for (const [agentId, state] of composerState) {
    const box = canvas.querySelector(
      `.swarm-chat-card[data-agent="${CSS.escape(agentId)}"] .swarm-chat-box`,
    );
    if (!box) continue;
    box.value = state.value;
    if (Number.isInteger(state.start) && Number.isInteger(state.end)) {
      try { box.setSelectionRange(state.start, state.end, state.direction || "none"); } catch (_) {}
    }
    countWhatIsTypedTo(agentId);
    if (focusedComposer === agentId) box.focus({preventScroll: true});
  }
  drawSwarmLines();
  renderSwarmStructure();
  if (focusedBoardControl) {
    const restoredBox = canvas.querySelector(
      `.swarm-box[data-kind="${CSS.escape(focusedBoardControl.kind)}"]`
      + `[data-id="${CSS.escape(focusedBoardControl.id)}"]`);
    const restored = restoredBox?.querySelector(
      `[data-does="${CSS.escape(focusedBoardControl.does)}"]`);
    if (restored) restored.focus({preventScroll: true});
  } else if (focusedLineKey) {
    canvas.querySelector(
      `.swarm-line-tools[data-focus-key="${CSS.escape(focusedLineKey)}"] button`,
    )?.focus({preventScroll: true});
  }
}

function oneSwarmBox(kind, one) {
  const picked = swarmPicked && swarmPicked.kind === kind && swarmPicked.id === one.id;
  const wrong = kind === "agent" ? !one.ready : !one.is_there;
  const box = make("div", [
    "swarm-box", kind,
    wrong ? (kind === "agent" ? "not-ready" : "gone") : "",
    picked ? "picked" : "",
  ].filter(Boolean).join(" "));
  box.dataset.kind = kind;
  box.dataset.id = one.id;
  box.style.left = `${one.at.x}px`;
  box.style.top = `${one.at.y}px`;
  const appearance = kind === "agent" ? styleForAgent(box, one) : null;

  // The box itself is what you press to pick it, drag it, or move it with the
  // arrows. It is a button so a keyboard can reach it; the gear and the chat
  // button sit beside it rather than inside it, because a button inside a
  // button is not a thing a browser will honour.
  const pick = make("button", "swarm-box-pick");
  pick.type = "button";
  pick.dataset.kind = kind;
  pick.dataset.id = one.id;
  pick.dataset.does = "pick";
  pick.setAttribute("aria-pressed", String(Boolean(picked)));
  pick.setAttribute("aria-label", kind === "agent"
    ? `${one.name}, an agent on the board. Pick it, or move it with the arrow keys`
    : `${one.name}, a project on the board. Pick it, or move it with the arrow keys`);
  if (kind === "agent") {
    pick.append(anAgentFace(one, "swarm-box-face", 28));
  } else {
    pick.append(aSwarmDrawing("folder", 26));
  }
  const words = make("span", "swarm-box-words");
  words.append(make("span", "swarm-box-name", one.name));
  if (kind === "agent") {
    words.append(make("span", "swarm-box-who",
      one.who ? (one.ready ? one.who : `${one.who} - not ready`) : "no assistant chosen"));
    if (one.job) words.append(make("span", "swarm-box-job", one.job));
    // What went wrong the last time this one's assistant was asked anything.
    // It is still ready and it will still be tried; this is here so somebody
    // reads it before typing a message rather than after sending one.
    if (one.trouble_last_time) {
      words.append(make("span", "swarm-box-trouble", one.trouble_last_time));
    }
  } else {
    words.append(make("span", "swarm-box-who",
      one.is_there ? one.path : `${one.path} - no such folder`));
    words.append(make("span", "swarm-box-job",
      one.tasks.length
        ? `${one.tasks.length} job${one.tasks.length === 1 ? "" : "s"}`
        : "no jobs yet"));
  }
  pick.append(words);
  box.append(pick);

  const tools = make("div", "swarm-box-tools");
  tools.append(aSwarmButton("swarm-icon-button", "gear", "settings",
    () => pickSwarmBox(kind, one.id), `settings for ${one.name}`, "settings"));
  if (kind === "agent") {
    tools.append(aSwarmButton("swarm-icon-button", "chat", "chat",
      () => openTheChatFor(one.id), `chat with ${one.name}`, "chat"));
  }
  box.append(tools);
  // Put the one provider-neutral repair entry point beside the problem itself.
  // The engine diagnoses this exact route before offering any provider action;
  // the board card must not guess that a failure is a login or Cloud-project
  // problem from the provider kind alone.
  if (kind === "agent" && (!one.ready || one.trouble_last_time)) {
    const connect = make("button", "swarm-connect swarm-box-connect", "Repair connection");
    connect.type = "button";
    // The whole card is draggable and captures a pointer that starts anywhere
    // inside it.  Keep this action out of that drag gesture, just like the gear
    // and chat buttons above, or a real mouse press is retargeted to the card
    // before its click can reach the connection flow.
    connect.addEventListener("pointerdown", (event) => event.stopPropagation());
    connect.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      void openAgentRepairFlow(one.id, connect);
    });
    box.append(connect);
  }
  makeSwarmBoxDraggable(box, pick);
  return box;
}

// Where the middle of one box is, in the board's own coordinates.
function theMiddleOf(box) {
  return {
    x: box.offsetLeft + box.offsetWidth / 2,
    y: box.offsetTop + box.offsetHeight / 2,
  };
}

function drawSwarmLines() {
  const sheet = $("swarmLines");
  sheet.replaceChildren();
  const board = $("swarmBoard");
  const canvas = $("swarmCanvas");
  for (const old of [...board.querySelectorAll(".swarm-line-tools")]) old.remove();
  const found = new Map();
  for (const box of board.querySelectorAll(".swarm-box")) {
    found.set(`${box.dataset.kind}:${box.dataset.id}`, box);
  }
  for (const card of board.querySelectorAll(".swarm-chat-card")) {
    found.set(`chat:${card.dataset.agent}`, card);
  }
  // The sheet is stretched to whatever the board scrolls to, so a line to a box
  // that is off the bottom is still drawn instead of being cut off at the edge.
  sizeTheSwarmCanvas();
  sheet.setAttribute("width", String(canvas.offsetWidth));
  sheet.setAttribute("height", String(canvas.offsetHeight));
  sheet.style.width = `${canvas.offsetWidth}px`;
  sheet.style.height = `${canvas.offsetHeight}px`;

  const said = theSwarmBoard();
  for (const line of said.works_on) {
    const drawn = drawOneSwarmLine(sheet, found.get(`agent:${line.agent}`),
      found.get(`project:${line.project}`), "works-on");
    if (!drawn) continue;
    const agent = theSwarmAgent(line.agent);
    const project = theSwarmProject(line.project);
    canvas.append(theToolsOnALine(drawn, "works on", true,
      () => pickSwarmLine({kind: "works", agent: line.agent, project: line.project}),
      `${agent ? agent.name : "this agent"} works on ${project ? project.name : "it"}`));
  }
  // A pair of agents on the same project gets a line whether or not they may
  // talk, so the gear that turns it on is somewhere to press. A pair with no
  // project in common gets nothing: letting those two talk would change what
  // no run ever does.
  for (const pair of whoCouldTalk()) {
    const on = mayTheyTalk(pair.one, pair.other);
    const drawn = drawOneSwarmLine(sheet, found.get(`agent:${pair.one}`),
      found.get(`agent:${pair.other}`), on ? "talks-to" : "talks-not");
    if (!drawn) continue;
    const first = theSwarmAgent(pair.one);
    const other = theSwarmAgent(pair.other);
    canvas.append(theToolsOnALine(drawn, on ? "communicates? YES" : "communicates? NO", on,
      () => pickSwarmLine({kind: "talks", one: pair.one, other: pair.other}),
      `whether ${first ? first.name : "one"} and ${other ? other.name : "the other"} may talk`));
  }
  // The thin line from a chat box to the agent it belongs to, so an open chat
  // is never a box floating on its own.
  for (const held of swarmChats.filter((one) => !one.minimised)) {
    drawOneSwarmLine(sheet, found.get(`chat:${held.agent}`),
      found.get(`agent:${held.agent}`), "to-its-chat");
  }
}

// Which pairs of agents get a line drawn between them.
//
// Two kinds. A pair that shares a project gets one whether or not they may
// talk, so the gear that turns it on is somewhere to press. And a pair that
// already may talk always gets one, even with no project in common - a line
// that exists and is drawn nowhere is a line nobody can turn off, and the only
// way back would have been to find it in a list on the far side of the page.
function whoCouldTalk() {
  const board = theSwarmBoard();
  const here = new Set(board.agents.map((one) => one.id));
  const on = new Map();
  for (const line of board.works_on) {
    if (!on.has(line.agent)) on.set(line.agent, new Set());
    on.get(line.agent).add(line.project);
  }
  const pairs = new Map();
  for (const first of board.agents) {
    for (const other of board.agents) {
      if (first.id >= other.id) continue;
      const mine = on.get(first.id);
      const theirs = on.get(other.id);
      if (!mine || !theirs) continue;
      if (![...mine].some((one) => theirs.has(one))) continue;
      pairs.set(`${first.id}|${other.id}`, {one: first.id, other: other.id});
    }
  }
  for (const line of board.talks_to) {
    if (!here.has(line.one) || !here.has(line.other)) continue;
    pairs.set(`${line.one}|${line.other}`, {one: line.one, other: line.other});
  }
  return [...pairs.values()];
}

function drawOneSwarmLine(sheet, from, to, kind) {
  if (!from || !to) return null;
  const start = theMiddleOf(from);
  const end = theMiddleOf(to);
  const drawn = document.createElementNS("http://www.w3.org/2000/svg", "line");
  drawn.setAttribute("class", `swarm-line ${kind}`);
  drawn.setAttribute("x1", String(start.x));
  drawn.setAttribute("y1", String(start.y));
  drawn.setAttribute("x2", String(end.x));
  drawn.setAttribute("y2", String(end.y));
  sheet.append(drawn);
  if (kind === "talks-not") {
    // A short stroke across the middle, which is how the drawing says no.
    const across = document.createElementNS("http://www.w3.org/2000/svg", "line");
    const middle = {x: (start.x + end.x) / 2, y: (start.y + end.y) / 2};
    across.setAttribute("class", "swarm-line crossed-out");
    across.setAttribute("x1", String(middle.x - 9));
    across.setAttribute("y1", String(middle.y + 9));
    across.setAttribute("x2", String(middle.x + 9));
    across.setAttribute("y2", String(middle.y - 9));
    sheet.append(across);
  }
  return {start, end};
}

// The gear and the words that sit on a line, halfway along it.
function theToolsOnALine(where, words, on, whenPressed, what) {
  const middle = {
    x: (where.start.x + where.end.x) / 2,
    y: (where.start.y + where.end.y) / 2,
  };
  const held = make("div", `swarm-line-tools ${on ? "on" : "off"}`);
  held.dataset.focusKey = what;
  held.style.left = `${Math.round(middle.x)}px`;
  held.style.top = `${Math.round(middle.y)}px`;
  held.append(aSwarmButton("swarm-icon-button", "gear", "settings", whenPressed,
    `settings for ${what}`, "settings"));
  held.append(make("span", "swarm-line-words", words));
  return held;
}

function renderSwarmNotReady() {
  const list = $("swarmNotReady");
  list.replaceChildren();
  const said = swarmSaid.what_is_not_ready || [];
  const board = theSwarmBoard();
  const runnableProjects = new Set((board.projects || [])
    .filter((project) => project.is_there && (project.tasks || []).length)
    .map((project) => project.id));
  const includedIds = new Set((board.works_on || [])
    .filter((line) => runnableProjects.has(line.project)
      && board.agents.some((agent) => agent.id === line.agent && agent.ready))
    .map((line) => line.agent));
  const excluded = Math.max(0, (board.agents || []).length - includedIds.size);
  $("swarmScopePreview").textContent = includedIds.size
    ? `${includedIds.size} agent${includedIds.size === 1 ? "" : "s"} will be asked across ${runnableProjects.size} ready project${runnableProjects.size === 1 ? "" : "s"}; ${excluded} agent${excluded === 1 ? " is" : "s are"} excluded. ${said.length} readiness issue${said.length === 1 ? "" : "s"} ${said.length === 1 ? "is" : "are"} listed below.`
    : `No agent will be asked: there is no ready agent assigned to a project folder that exists and has jobs. ${said.length} readiness issue${said.length === 1 ? "" : "s"} ${said.length === 1 ? "is" : "are"} listed below.`;
  if (!said.length) {
    list.append(make("li", "all-well",
      "Everything on the board is ready: every agent has an assistant, and every "
      + "project has somebody on it and jobs to do."));
    return;
  }
  for (const one of said) {
    const row = make("li", "", one);
    // Every agent problem is also a direct route into the same provider-neutral
    // diagnosis shown in that agent's settings. Users should never have to
    // decode this sentence, find a distant box, and guess which login button
    // might apply.
    const stuck = (theSwarmBoard().agents || []).find(
      (agent) => one.startsWith(`${agent.name}:`));
    if (stuck) {
      const connect = make("button", "swarm-connect", "Fix this agent");
      connect.type = "button";
      connect.setAttribute("aria-label",
        `Open connection repair for ${stuck.name}`);
      connect.addEventListener("click", () => void openAgentRepairFlow(stuck.id, connect));
      row.append(connect);
    }
    list.append(row);
  }
  // And one line for anything on this machine that nothing points at yet, even
  // when no agent is asking for it - so it can be connected before somebody
  // spends ten minutes wondering why the dropdown is short.
  const routesAnAgentWants = new Set(
    (theSwarmBoard().agents || []).map((agent) => agent.who).filter(Boolean));
  for (const one of swarmSaid.who_can_be_used || []) {
    if (one.ready || !one.can_be_connected || routesAnAgentWants.has(one.route)) continue;
    const row = make("li", "", `${one.label || one.route}: on this machine and not connected yet.`);
    const connect = make("button", "swarm-connect", "Connect it");
    connect.type = "button";
    connect.setAttribute("aria-label", `Connect ${one.label || one.route}`);
    connect.addEventListener("click", () => connectThisAssistant(one.can_be_connected, connect));
    row.append(connect);
    list.append(row);
  }
}

// ---- dragging ------------------------------------------------------------
//
// Letting go of a box where it started means you meant to pick it, not move it,
// so a press that never moved four pixels picks it instead of writing a new
// position down. Moving one does both: it lands where you dropped it, and it is
// the box the panel on the right is now showing.

function makeSwarmBoxDraggable(box, pick) {
  let dragging = null;
  box.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    // Letting go writes a new place down, which is a change to the board, so
    // while a run is going a box can be picked but not moved.
    if (whyTheBoardIsHeld()) return;
    dragging = {
      x: event.clientX, y: event.clientY,
      left: box.offsetLeft, top: box.offsetTop, moved: false,
    };
    box.setPointerCapture(event.pointerId);
  });
  box.addEventListener("pointermove", (event) => {
    if (!dragging) return;
    const across = (event.clientX - dragging.x) / swarmZoom;
    const down = (event.clientY - dragging.y) / swarmZoom;
    if (!dragging.moved && Math.abs(across) + Math.abs(down) < 4) return;
    dragging.moved = true;
    box.style.left = `${Math.max(0, Math.min(4000, Math.round(dragging.left + across)))}px`;
    box.style.top = `${Math.max(0, Math.min(4000, Math.round(dragging.top + down)))}px`;
    drawSwarmLines();
  });
  const letGo = async (event) => {
    if (!dragging) return;
    const wasMoved = dragging.moved;
    dragging = null;
    if (box.hasPointerCapture(event.pointerId)) box.releasePointerCapture(event.pointerId);
    if (!wasMoved) return;
    await putThisBoxDown(box);
  };
  box.addEventListener("pointerup", letGo);
  box.addEventListener("pointercancel", letGo);
  pick.addEventListener("click", (event) => {
    event.preventDefault();
    pickSwarmBox(box.dataset.kind, box.dataset.id);
  });
  // Moving one without a mouse. Dragging is a pointer at a target, which is no
  // use to somebody on a keyboard, and "Tidy the board" only ever puts things
  // back in rows - it is not a way to arrange them. The arrows move the box a
  // step at a time; holding shift moves it a small step, for lining two up.
  let moved = false;
  pick.addEventListener("keydown", (event) => {
    const which = {
      ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1],
    }[event.key];
    if (!which) return;
    event.preventDefault();
    const held = whyTheBoardIsHeld();
    if (held) { sayInSwarm(held); return; }
    const step = event.shiftKey ? 4 : 20;
    box.style.left = `${Math.max(0, Math.min(4000, box.offsetLeft + which[0] * step))}px`;
    box.style.top = `${Math.max(0, Math.min(4000, box.offsetTop + which[1] * step))}px`;
    drawSwarmLines();
    moved = true;
  });
  // Written down when the key comes back up, not on every step of holding it,
  // so moving a box across the board is one save and not forty.
  pick.addEventListener("keyup", async (event) => {
    if (!moved || !event.key.startsWith("Arrow")) return;
    moved = false;
    await putThisBoxDown(box);
  });
}

// Where a box was let go of, written down. The place is read off the screen now
// rather than when the change comes to be saved, because by then the board may
// have been drawn again somewhere else.
function putThisBoxDown(box) {
  const kind = box.dataset.kind;
  const id = box.dataset.id;
  const at = {x: box.offsetLeft, y: box.offsetTop};
  const one = kind === "agent" ? theSwarmAgent(id) : theSwarmProject(id);
  if (!one) return Promise.resolve(false);
  return changeTheSwarmBoard((board) => {
    const held = (kind === "agent" ? board.agents : board.projects)
      .find((each) => each.id === id);
    if (!held) return false;
    held.at = at;
  }, `${one.name} was moved.`);
}

function pickSwarmBox(kind, id) {
  const previous = thePickedAgent();
  if (previous && (kind !== "agent" || previous.id !== id)) {
    // Start the last pending save before the form is replaced. The captured
    // per-agent draft remains valid while the next panel opens.
    void flushSwarmAgentSettings(previous.id);
  }
  swarmPicked = {kind, id};
  showTheSwarmPanel();
  renderSwarmBoard();
  renderSwarmPanel();
}

async function openAgentRepairFlow(agentId, button = null) {
  // A maximised chat is modal, so expose the selected agent's board panel
  // before moving focus into it. The chat stays open in the tray.
  if (!$("theBigChat").hidden) minimiseTheBigChat(false);
  pickSwarmBox("agent", agentId);
  const selected = theSwarmAgent(agentId);
  if (!selected) {
    sayInSwarm("That agent is no longer on this board. The board was refreshed.");
    return;
  }
  if (selected.who) await loadAgentRepairPlan(agentId, selected.who, button);
  else renderSwarmAgentPanel(selected);
  const panel = $("swarmAgentRepair");
  panel?.scrollIntoView({block: "nearest", behavior: "smooth"});
  const first = panel?.querySelector("button:not([hidden]):not(:disabled)");
  first?.focus({preventScroll: true});
}

function renderSwarmStructure() {
  const held = $("swarmStructure");
  held.replaceChildren();
  const board = theSwarmBoard();
  const heading = make("p", "scope-preview",
    `${board.agents.length} agent${board.agents.length === 1 ? "" : "s"}, `
    + `${board.projects.length} project${board.projects.length === 1 ? "" : "s"}, `
    + `${board.works_on.length} work assignment${board.works_on.length === 1 ? "" : "s"}, `
    + `${board.talks_to.length} talk permission${board.talks_to.length === 1 ? "" : "s"}.`);
  held.append(heading);
  const agents = make("ul", "semantic-list");
  for (const agent of board.agents) {
    const row = make("li", "semantic-item");
    row.append(make("strong", "", agent.name), make("span", "",
      agent.ready ? ` — ready via ${agent.who || "the selected assistant"}` : " — excluded: assistant not ready"));
    const edit = make("button", "compact", `Open ${agent.name} settings`);
    edit.type = "button";
    edit.addEventListener("click", () => pickSwarmBox("agent", agent.id));
    row.append(edit);
    const assignments = board.works_on.filter((line) => line.agent === agent.id);
    const talks = board.talks_to.filter((line) => line.one === agent.id || line.other === agent.id);
    const facts = make("ul", "semantic-edge-list");
    for (const line of assignments) {
      const project = board.projects.find((one) => one.id === line.project);
      const item = make("li", "semantic-edge", `Works on ${project?.name || line.project}. `);
      const settings = make("button", "compact", "Change assignment");
      settings.type = "button";
      settings.setAttribute("aria-label", `Change whether ${agent.name} works on ${project?.name || line.project}`);
      settings.addEventListener("click", () => pickSwarmLine(
        {kind: "works", agent: line.agent, project: line.project}));
      item.append(settings);
      facts.append(item);
    }
    for (const line of talks) {
      const otherId = line.one === agent.id ? line.other : line.one;
      const other = board.agents.find((one) => one.id === otherId);
      const item = make("li", "semantic-edge", `May talk with ${other?.name || otherId}. `);
      const settings = make("button", "compact", "Change permission");
      settings.type = "button";
      settings.setAttribute("aria-label", `Change talk permission between ${agent.name} and ${other?.name || otherId}`);
      settings.addEventListener("click", () => pickSwarmLine(
        {kind: "talks", one: line.one, other: line.other}));
      item.append(settings);
      facts.append(item);
    }
    if (facts.childElementCount) row.append(facts);
    agents.append(row);
  }
  if (agents.childElementCount) held.append(agents);
  else held.append(make("p", "hint", "No agents are on the board yet."));
}

function pickSwarmLine(which) {
  const previous = thePickedAgent();
  if (previous) void flushSwarmAgentSettings(previous.id);
  swarmPicked = which;
  showTheSwarmPanel();
  renderSwarmBoard();
  renderSwarmPanel();
}

// ---- what you picked -----------------------------------------------------

function agentSettingsFromAgent(agent) {
  const appearance = agentAppearance(agent);
  return {
    name: agent.name || "",
    who: agent.who || "",
    job: agent.job || "",
    icon: appearance.icon,
    colour: appearance.colour,
    bubbleColour: appearance.bubble,
    profilePicture: appearance.picture,
    pictureZoom: appearance.pictureZoom,
    pictureHue: appearance.pictureHue,
  };
}

function agentSettingsFromForm() {
  return {
    name: $("swarmAgentName").value,
    who: $("swarmAgentWho").value,
    job: $("swarmAgentJob").value,
    icon: $("swarmAgentIcon").value,
    colour: $("swarmAgentColour").value,
    bubbleColour: $("swarmAgentBubbleColour").value,
    profilePicture: swarmAgentPictureDraft,
    pictureZoom: Number($("swarmAgentPictureZoom").value),
    pictureHue: Number($("swarmAgentPictureHue").value),
  };
}

function renderSwarmAgentSaveState(agentId = (thePickedAgent() || {}).id) {
  const state = $("swarmAgentSaveState");
  const save = $("swarmAgentSave");
  if (!state || !save) return;
  const draft = agentId ? swarmAgentSettingDrafts.get(agentId) : null;
  const held = Boolean(whyTheBoardIsHeld());
  let kind = "saved";
  let words = "All changes are saved automatically.";
  if (held && draft) {
    kind = "saving";
    words = "Changes are waiting. They will save when the board is available.";
  } else if (draft?.error) {
    kind = "error";
    words = draft.error;
  } else if (draft?.inFlight) {
    kind = "saving";
    words = "Saving changes…";
  } else if (draft) {
    kind = "saving";
    words = "Changes waiting to save…";
  }
  state.dataset.state = kind;
  state.textContent = words;
  save.textContent = draft?.inFlight ? "Saving…" : "Save now";
  save.disabled = held || !agentId || !draft || Boolean(draft.inFlight);
}

function rememberSwarmAgentSettings(delay = SWARM_AGENT_AUTOSAVE_DELAY) {
  const agent = thePickedAgent();
  if (!agent) return null;
  let draft = swarmAgentSettingDrafts.get(agent.id);
  if (!draft) {
    draft = {
      agentId: agent.id, values: agentSettingsFromForm(), revision: 0,
      savedRevision: 0, timer: 0, inFlight: null, error: "", waitingForBoard: false,
    };
    swarmAgentSettingDrafts.set(agent.id, draft);
  }
  draft.values = agentSettingsFromForm();
  draft.revision += 1;
  draft.error = "";
  draft.waitingForBoard = false;
  if (draft.timer) window.clearTimeout(draft.timer);
  draft.timer = 0;
  if (delay !== null) {
    draft.timer = window.setTimeout(() => {
      draft.timer = 0;
      void flushSwarmAgentSettings(agent.id);
    }, Math.max(0, delay));
  }
  renderSwarmAgentSaveState(agent.id);
  return draft;
}

async function flushSwarmAgentSettings(agentId, announce = false) {
  const draft = swarmAgentSettingDrafts.get(agentId);
  if (!draft) {
    if (announce) sayInSwarm("All agent settings are already saved.");
    return true;
  }
  if (draft.timer) window.clearTimeout(draft.timer);
  draft.timer = 0;
  if (draft.inFlight) {
    await draft.inFlight;
    return flushSwarmAgentSettings(agentId, announce);
  }
  if (whyTheBoardIsHeld()) {
    draft.waitingForBoard = true;
    renderSwarmAgentSaveState(agentId);
    return false;
  }

  const values = {...draft.values};
  const revision = draft.revision;
  const name = values.name.trim().replace(/\s+/g, " ");
  if (!name) {
    draft.error = "Not saved yet — give this agent a name.";
    renderSwarmAgentSaveState(agentId);
    if (thePickedAgent()?.id === agentId) $("swarmAgentName").focus();
    return false;
  }
  const jobLength = systemPromptCharacters(values.job);
  if (jobLength > AGENT_JOB_CHARACTER_LIMIT) {
    const over = jobLength - AGENT_JOB_CHARACTER_LIMIT;
    draft.error = `Not saved yet — this role description is ${jobLength.toLocaleString()} characters; the disclosed limit is ${AGENT_JOB_CHARACTER_LIMIT.toLocaleString()}. Nexus did not truncate it. Shorten it by ${over.toLocaleString()} characters.`;
    renderSwarmAgentSaveState(agentId);
    if (thePickedAgent()?.id === agentId) {
      renderDisclosedTextCount(
        "swarmAgentJob", "swarmAgentJobCount",
        AGENT_JOB_CHARACTER_LIMIT, "the role description");
      $("swarmAgentJob").focus();
    }
    return false;
  }
  const before = theSwarmAgent(agentId);
  if (!before) {
    if (draft.timer) window.clearTimeout(draft.timer);
    swarmAgentSettingDrafts.delete(agentId);
    return false;
  }
  const wasCalled = before.name;
  renderSwarmAgentSaveState(agentId);
  draft.inFlight = (async () => {
    const who = values.who;
    const job = values.job;
    const icon = values.icon;
    const colour = values.colour;
    const bubbleColour = values.bubbleColour;
    const profilePicture = values.profilePicture;
    const pictureZoom = Number(values.pictureZoom);
    const pictureHue = Number(values.pictureHue);
    const worked = await changeTheSwarmBoard((board) => {
      const held = board.agents.find((one) => one.id === agentId);
      if (!held) return false;
      held.name = name;
      held.who = who;
      held.job = job;
      held.icon = icon;
      held.colour = colour;
      held.bubble_colour = bubbleColour;
      held.profile_picture = profilePicture;
      held.picture_zoom = pictureZoom;
      held.picture_hue = pictureHue;
    });
    if (!worked) {
      draft.error = "Could not save these settings. Your edits are still here — press Save now to retry.";
      return false;
    }
    draft.savedRevision = Math.max(draft.savedRevision, revision);
    draft.error = "";
    draft.waitingForBoard = false;
    if (name !== wasCalled) refreshTheChatFor(agentId);
    if (draft.revision === revision) {
      swarmAgentSettingDrafts.delete(agentId);
      if (announce) sayInSwarm(`${name} is saved.`);
    }
    return true;
  })();
  renderSwarmAgentSaveState(agentId);
  let worked = false;
  try {
    worked = await draft.inFlight;
  } finally {
    if (swarmAgentSettingDrafts.get(agentId) === draft) {
      draft.inFlight = null;
      if (worked && draft.revision > draft.savedRevision && !draft.timer) {
        draft.timer = window.setTimeout(() => {
          draft.timer = 0;
          void flushSwarmAgentSettings(agentId);
        }, SWARM_AGENT_AUTOSAVE_DELAY);
      }
    }
    if (thePickedAgent()?.id === agentId) renderSwarmPanel();
  }
  return worked;
}

function discardSwarmAgentSettings(agentId) {
  const draft = swarmAgentSettingDrafts.get(agentId);
  if (draft?.timer) window.clearTimeout(draft.timer);
  swarmAgentSettingDrafts.delete(agentId);
}

function renderSwarmPanel() {
  const agent = thePickedAgent();
  const project = thePickedProject();
  const line = thePickedLine();
  $("swarmAgentPanel").hidden = !agent;
  $("swarmProjectPanel").hidden = !project;
  $("swarmLinePanel").hidden = !line;
  if (agent) {
    $("swarmPanelTitle").textContent = agent.name;
    $("swarmPanelHint").textContent = agent.ready
      ? "Change what it is and who it works with. Its chat opens on the board."
      : (agent.why_not || "");
    renderSwarmAgentPanel(agent);
  } else if (project) {
    $("swarmPanelTitle").textContent = project.name;
    $("swarmPanelHint").textContent = project.is_there
      ? project.path : `There is no folder at ${project.path} any more.`;
    renderSwarmProjectPanel(project);
  } else if (line) {
    renderSwarmLinePanel(line);
  } else {
    $("swarmPanelTitle").textContent = "Nothing picked";
    $("swarmPanelHint").textContent = "Press the gear on a box or a line to change it.";
  }
  setWhatCanBePressedInSwarm();
}

function renderSwarmAgentPanel(agent) {
  // A board redraw must never replace an edit that is waiting for autosave.
  // Render from that agent's durable page draft until the server accepts it.
  const draft = swarmAgentSettingDrafts.get(agent.id);
  const values = draft?.values || agentSettingsFromAgent(agent);
  $("swarmAgentName").value = values.name;
  $("swarmAgentJob").value = values.job;
  renderDisclosedTextCount(
    "swarmAgentJob", "swarmAgentJobCount",
    AGENT_JOB_CHARACTER_LIMIT, "the role description");
  $("swarmAgentIcon").value = values.icon;
  $("swarmAgentColour").value = values.colour;
  $("swarmAgentBubbleColour").value = values.bubbleColour;
  swarmAgentPictureDraft = values.profilePicture;
  $("swarmAgentPictureZoom").value = String(values.pictureZoom);
  $("swarmAgentPictureHue").value = String(values.pictureHue);
  $("swarmAgentPictureFile").value = "";
  $("swarmAgentPictureSaid").textContent = values.profilePicture
    ? "Custom picture selected. Browse again to replace it."
    : "PNG, JPEG, or WebP. Nexus resizes the picture before keeping it.";
  previewSwarmAgentAppearance();
  const who = $("swarmAgentWho");
  who.replaceChildren();
  const none = make("option", "", "Not chosen yet");
  none.value = "";
  who.append(none);
  for (const one of swarmSaid.who_can_be_used || []) {
    const choice = make("option", "", one.ready ? one.label : `${one.label} - not set up`);
    choice.value = one.route;
    who.append(choice);
  }
  if (canUseWebChats() && webChatProviderChoices.length) {
    const group = document.createElement("optgroup");
    group.label = "Connect a web AI chat";
    for (const provider of webChatProviderChoices) {
      const choice = make("option", "", `${provider.label} web chat — sign in or choose a chat…`);
      choice.value = `__connect_web__:${provider.id}`;
      group.append(choice);
    }
    who.append(group);
  }
  // One that is written down but no longer on this machine still has to be
  // shown, or opening its settings would quietly change it to something else.
  if (values.who && !(swarmSaid.who_can_be_used || []).some((one) => one.route === values.who)) {
    const gone = make("option", "", `${values.who} - not on this machine`);
    gone.value = values.who;
    who.append(gone);
  }
  who.value = values.who;
  const route = String(values.who || "");
  const routeIdentity = $("swarmAgentRouteIdentity");
  const routeSetup = (swarmSaid.who_can_be_used || []).find((one) => one.route === route);
  if (!route) {
    routeIdentity.textContent = "No provider route is assigned to this agent.";
    routeIdentity.dataset.tone = "missing";
  } else if (!routeSetup) {
    routeIdentity.textContent = `Actual route: ${route}. It is not available on this computer.`;
    routeIdentity.dataset.tone = "missing";
  } else {
    const model = String(routeSetup.model || agent.chat_destination?.model || "").trim();
    routeIdentity.textContent = `Actual provider: ${routeSetup.label || routeSetup.kind || route}. Route: ${route}${model ? `. Model: ${model}` : ""}.`;
    routeIdentity.dataset.tone = routeSetup.ready ? "ready" : "missing";
  }
  const cachedRepair = swarmAgentRepairPlans.get(agent.id);
  renderAgentRepairPanel(
    agent,
    route,
    cachedRepair?.route === route ? cachedRepair.plan : null,
  );

  const worksOn = $("swarmWorksOn");
  worksOn.replaceChildren();
  if (!theSwarmBoard().projects.length) {
    worksOn.append(make("p", "hint", "No projects on the board yet."));
  }
  for (const one of theSwarmBoard().projects) {
    worksOn.append(oneSwarmTick(
      `swarmWorks-${one.id}`, one.name,
      theSwarmBoard().works_on.some(
        (line) => line.agent === agent.id && line.project === one.id),
      (on) => setWhoWorksOnWhat(agent.id, one.id, on),
    ));
  }

  const talksTo = $("swarmTalksTo");
  talksTo.replaceChildren();
  const others = theSwarmBoard().agents.filter((one) => one.id !== agent.id);
  if (!others.length) {
    talksTo.append(make("p", "hint", "There is nobody else on the board to talk to."));
  }
  const mine = new Set(theSwarmBoard().works_on
    .filter((line) => line.agent === agent.id).map((line) => line.project));
  for (const one of others) {
    const shares = theSwarmBoard().works_on
      .some((line) => line.agent === one.id && mine.has(line.project));
    talksTo.append(oneSwarmTick(
      `swarmTalks-${one.id}`, shares ? one.name : `${one.name} - no project in common`,
      mayTheyTalk(agent.id, one.id),
      (on) => setWhetherTheyTalk(agent.id, one.id, on),
    ));
  }
  renderSwarmAgentSaveState(agent.id);
}

function oneSwarmTick(id, words, ticked, whenChanged) {
  const row = make("label", "swarm-tick");
  const tick = make("input");
  tick.type = "checkbox";
  tick.id = id;
  tick.checked = ticked;
  tick.addEventListener("change", () => whenChanged(tick.checked));
  row.append(tick, make("span", "", words));
  row.htmlFor = id;
  return row;
}

function mayTheyTalk(one, other) {
  const first = one < other ? one : other;
  const second = one < other ? other : one;
  return theSwarmBoard().talks_to.some(
    (line) => line.one === first && line.other === second);
}

function setWhoWorksOnWhat(agentId, projectId, on) {
  return changeTheSwarmBoard((board) => {
    board.works_on = board.works_on.filter(
      (line) => !(line.agent === agentId && line.project === projectId));
    if (on) board.works_on.push({agent: agentId, project: projectId});
  }, () => {
    const agent = theSwarmAgent(agentId) || {name: "That agent"};
    const project = theSwarmProject(projectId) || {name: "that project"};
    return on
      ? `${agent.name} works on ${project.name}.`
      : `${agent.name} is off ${project.name}.`;
  });
}

function setWhetherTheyTalk(one, other, on) {
  const first = one < other ? one : other;
  const second = one < other ? other : one;
  return changeTheSwarmBoard((board) => {
    board.talks_to = board.talks_to.filter(
      (line) => !(line.one === first && line.other === second));
    if (on) board.talks_to.push({one: first, other: second});
  }, () => {
    const them = `${(theSwarmAgent(one) || {}).name} and ${(theSwarmAgent(other) || {}).name}`;
    return on
      ? `${them} may talk to each other.`
      : `${them} will not hear from each other.`;
  });
}

function renderSwarmProjectPanel(project) {
  $("swarmProjectPath").value = project.path;
  const on_it = theSwarmBoard().works_on
    .filter((line) => line.project === project.id)
    .map((line) => (theSwarmAgent(line.agent) || {}).name)
    .filter(Boolean);
  $("swarmProjectWho").textContent = on_it.length
    ? `Worked on by ${on_it.join(", ")}.`
    : "Nobody works on this yet. Press the gear on an agent and tick this project.";
  const approval = (swarmSaid.verification_command_approvals || [])
    .find((one) => one.project_id === project.id) || {};
  const commands = Array.isArray(approval.commands) ? approval.commands : [];
  $("swarmProjectVerificationCommands").textContent = commands.length
    ? commands.map((command) => JSON.stringify(command)).join("\n")
    : "No deterministic test command is currently discoverable.";
  if (approval.approved) {
    $("swarmProjectVerificationStatus").textContent =
      "Approved. Nexus may run only these exact discovered commands for this exact project path.";
  } else if (approval.stale_approval) {
    $("swarmProjectVerificationStatus").textContent =
      "Approval expired because the project path or discovered commands changed. Review and approve the current commands again.";
  } else {
    $("swarmProjectVerificationStatus").textContent = approval.reason
      || "Refresh the command list to see whether this project needs approval.";
  }
  $("swarmProjectVerificationDigest").textContent = approval.approval_digest
    ? `Exact approval fingerprint: ${approval.approval_digest}`
    : "No approval fingerprint is available.";
  const list = $("swarmTasks");
  list.replaceChildren();
  if (!project.tasks.length) {
    list.append(make("li", "hint", "No jobs written down yet."));
  }
  project.tasks.forEach((task, at) => {
    const row = make("li", "swarm-task");
    row.append(make("span", "", task));
    const drop = make("button", "", "Remove");
    drop.type = "button";
    drop.addEventListener("click", () => removeOneSwarmTask(project.id, at));
    row.append(drop);
    list.append(row);
  });
  renderDisclosedTextCount(
    "swarmTaskText", "swarmTaskTextCount",
    BOARD_TASK_CHARACTER_LIMIT, "the project job");
}

function renderSwarmLinePanel(line) {
  const on = line.kind === "works"
    ? theSwarmBoard().works_on.some(
      (one) => one.agent === line.agent && one.project === line.project)
    : mayTheyTalk(line.one, line.other);
  if (line.kind === "works") {
    const agent = theSwarmAgent(line.agent);
    const project = theSwarmProject(line.project);
    $("swarmPanelTitle").textContent = "Works on";
    $("swarmPanelHint").textContent = "The line between an agent and a project.";
    $("swarmLineWhat").textContent =
      `${agent ? agent.name : "This agent"} and ${project ? project.name : "this project"}.`;
    $("swarmLineOnWords").textContent = "It works on this project";
    $("swarmLineWhy").textContent =
      "Off means it is not asked about this project when the board is set going.";
    $("swarmLineRemove").textContent = "Turn this line off";
  } else {
    const first = theSwarmAgent(line.one);
    const other = theSwarmAgent(line.other);
    $("swarmPanelTitle").textContent = "Communicates?";
    $("swarmPanelHint").textContent = "The line between two agents.";
    $("swarmLineWhat").textContent =
      `${first ? first.name : "One"} and ${other ? other.name : "the other"}.`;
    $("swarmLineOnWords").textContent = "They may talk to each other";
    $("swarmLineWhy").textContent =
      "Off means they never hear from each other. On means each is shown what the "
      + "other said, after both have answered on their own.";
    $("swarmLineRemove").textContent = "Turn this line off";
  }
  $("swarmLineOn").checked = on;
}

// One place decides what can be pressed, so no path can leave a button dead.
function whyTheBoardIsHeld() {
  if (swarmSaid.cannot_be_changed) return swarmSaid.cannot_be_changed;
  if (swarmGoing) {
    return "The board is going, so it cannot be changed until it finishes.";
  }
  return "";
}

function boardGoalAuthorityPause() {
  const authorities = swarmSaid.project_authorities || {};
  const selected = (theSwarmBoard().projects || []).filter(
    (project) => (project.tasks || []).some((task) => String(task || "").trim()),
  );
  const blocked = selected.filter(
    (project) => authorities[project.id]?.can_run === false,
  );
  return blocked.length
    ? blocked.map((project) => (
      `${project.name}: ${authorities[project.id]?.reason || "Project execution is paused."}`
    )).join(" ")
    : "";
}

function setWhatCanBePressedInSwarm() {
  const agent = thePickedAgent();
  const project = thePickedProject();
  const line = thePickedLine();
  const board = theSwarmBoard();
  const most = swarmSaid.most || {};
  // While a run is going the board cannot be changed at all. A turn already
  // asked for writes what it says under the name that agent had when it was
  // asked, so an agent renamed halfway through would have its answer land in a
  // chat nothing points at any more. The server turns those saves down as
  // well; this is so nobody is offered a button that will be refused.
  const held = Boolean(whyTheBoardIsHeld());
  // Saving a named snapshot does not mutate the live topology being executed.
  $("swarmKeep").disabled = false;
  $("swarmAddAgent").disabled = held || board.agents.length >= (most.agents || 24);
  $("swarmAddProject").disabled = held || board.projects.length >= (most.projects || 12);
  $("swarmRemoveAgent").disabled = held || !agent;
  $("swarmRemoveProject").disabled = held || !project;
  $("swarmTidy").disabled = held || (!board.agents.length && !board.projects.length);
  $("swarmAgentRemove").disabled = held || !agent;
  $("swarmOpenChat").disabled = !agent;
  for (const id of [
    "swarmAgentName", "swarmAgentWho", "swarmAgentJob", "swarmAgentIcon",
    "swarmAgentColour", "swarmAgentBubbleColour", "swarmAgentPictureBrowse",
  ]) {
    $(id).disabled = held || !agent;
  }
  const hasAgentPicture = Boolean(agent && safeAgentPicture(
    swarmAgentSettingDrafts.get(agent.id)?.values?.profilePicture ?? agent.profile_picture));
  $("swarmAgentPictureClear").disabled = held || !agent || !hasAgentPicture;
  $("swarmAgentPictureZoom").disabled = held || !agent || !hasAgentPicture;
  $("swarmAgentPictureHue").disabled = held || !agent || !hasAgentPicture;
  const settingsDraft = agent ? swarmAgentSettingDrafts.get(agent.id) : null;
  if (!held && settingsDraft?.waitingForBoard && !settingsDraft.timer && !settingsDraft.inFlight) {
    settingsDraft.waitingForBoard = false;
    settingsDraft.timer = window.setTimeout(() => {
      settingsDraft.timer = 0;
      void flushSwarmAgentSettings(agent.id);
    }, 0);
  }
  renderSwarmAgentSaveState(agent?.id);
  $("swarmAddTask").disabled = held
    || !project || project.tasks.length >= (most.tasks || 40);
  $("swarmProjectRemove").disabled = held || !project;
  $("swarmProjectRebind").disabled = held || !project;
  const verificationApproval = (swarmSaid.verification_command_approvals || [])
    .find((one) => one.project_id === project?.id) || {};
  $("swarmProjectVerificationApprove").disabled = held || !project
    || !verificationApproval.can_approve || verificationApproval.approved;
  $("swarmProjectVerificationRevoke").disabled = held || !project
    || !verificationApproval.approved_digest;
  $("swarmProjectVerificationRefresh").disabled = !project;
  $("swarmLineOn").disabled = held || !line;
  $("swarmLineRemove").disabled = held || !line || !$("swarmLineOn").checked;
  for (const tick of $("swarmWorksOn").querySelectorAll("input")) tick.disabled = held;
  for (const tick of $("swarmTalksTo").querySelectorAll("input")) tick.disabled = held;
  $("swarmStart").disabled = held || Boolean(swarmSaid.cannot_run) || swarmGoing;
  $("swarmStart").title = String(swarmSaid.cannot_run || held || "");
  const boardGoalPause = boardGoalAuthorityPause();
  $("swarmWorkGoals").disabled = Boolean(
    held || boardGoalPause || swarmGoing || swarmGoalWorkRunning);
  $("swarmWorkGoals").title = String(
    boardGoalPause || held
    || (swarmGoalWorkRunning ? "Goal work is starting." : ""));
  $("swarmCancelGoals").disabled = !longGoal
    || ["complete", "cancelled"].includes(longGoal.status);
  const longProjectWorkActive = longGoals.some((goal) => (
    ["waiting_for_project", "queued", "running", "paused", "waiting_for_user"]
      .includes(goal.status)
    || (goal.status === "cancelling" && goal.project_queue?.state !== "released")
    || (goal.status === "failed" && goal.project_queue?.state === "owner")
  ));
  $("swarmLegacyGoals").disabled = Boolean(
    held || boardGoalPause || swarmGoing || swarmGoalWorkRunning
    || ["queued", "running"].includes(swarmGoalQueue?.status)
    || longProjectWorkActive);
  $("swarmLegacyGoals").title = longProjectWorkActive
    ? "Cancel or finish active long-horizon project work before using the legacy paired workflow."
    : "Use the older paired plan/review/execute workflow.";
  $("swarmStop").disabled = !swarmGoing;
  for (const card of $("swarmBoard").querySelectorAll(".swarm-chat-card")) {
    setWhatCanBePressedInAChat(card);
  }
  if (theBigOne) {
    // The maximised chat owns its own agent identity. Board settings selection
    // is unrelated and may legitimately be empty (or point at a project or a
    // different agent), especially after restart and repair inspection.
    const chatAgent = theSwarmAgent(theBigOne);
    const busy = swarmChatIsBusy(theBigOne);
    const identityChanging = swarmChatIsResetting(theBigOne)
      || swarmConversationSwitching.has(theBigOne) || swarmChatIsHydrating(theBigOne);
    const waiting = busy || identityChanging;
    const stopping = swarmChatIsStopping(theBigOne);
    const recovery = workRecoveryFor(theBigOne);
    const lone = isLoneAgentChat(theBigOne);
    const conversation = activeConversationFor(theBigOne);
    const bindingProblem = conversation?.binding_problem;
    const bindingWords = String(bindingProblem?.message || "");
    const recipientWords = syncChatRecipientWords(theBigOne);
    const unavailablePeers = syncChatTeamReadiness(theBigOne);
    // Do not accept text under one saved-chat identity while its replacement
    // is still being selected. The composer draft is keyed by chat id, so a
    // keystroke in this interval would otherwise be saved under the old id and
    // appear to vanish as soon as the authoritative selection arrives.
    $("theBigChatBox").disabled = !chatAgent || identityChanging;
    $("theBigChatBox").title = identityChanging
      ? "Wait while Nexus opens the selected saved chat."
      : "";
    $("theBigChatAttach").disabled = waiting || !chatAgent || !chatAgent.ready;
    if (bindingProblem) $("theBigChatAttach").disabled = true;
    $("theBigChatSend").disabled = waiting || !chatAgent || !chatAgent.ready
      || Boolean(bindingProblem);
    $("theBigChatSend").title = bindingWords
      || `${recipientWords.direct}. No connected peer receives this request.`;
    $("theBigChatCollaborate").disabled = waiting || lone || Boolean(bindingProblem)
      || unavailablePeers.length > 0;
    $("theBigChatCollaborate").title = bindingWords || (lone
      ? loneAgentActionMessage("collaborate")
      : unavailablePeers.length
      ? `Repair ${unavailablePeers.map((one) => one.name || one.id).join(", ")} before asking the team.`
      : `${recipientWords.team}. Expected initial replies: ${recipientWords.expected}.`);
    if ($("theBigChatWork")) {
      const recoveryAuthorityWords = !directLongGoalRecoveryInventoryReady
        ? (directLongGoalRecoveryError
          || "Checking the saved goal-request journals before enabling project work")
        : "";
      const workTitle = bindingWords || recoveryAuthorityWords || (lone
        ? loneAgentActionMessage("work")
        : recovery
        ? "Resume the saved project-work run before starting another"
        : conversation && !conversation.project
        ? "Choose this chat's active project first"
        : "Start durable project work with a required contribution from every ready agent in this chat");
      setSwarmProjectWorkControl($("theBigChatWork"), waiting || lone || Boolean(recovery)
        || !directLongGoalRecoveryInventoryReady
        || Boolean(bindingProblem)
        || (Boolean(conversation) && !conversation.project), workTitle, theBigOne);
    }
    if ($("theBigChatProject")) {
      $("theBigChatProject").disabled = waiting || !conversation || Boolean(bindingProblem);
    }
    syncChatRoundPolicy(theBigOne);
    for (const button of $("theBigChatConversationList")?.querySelectorAll("button") || []) {
      const action = button.dataset.conversationAction || "";
      const chatId = button.dataset.chatId || "";
      const targetBusy = Boolean(chatId) && swarmChatIsBusy(theBigOne, chatId);
      const targetResetting = Boolean(chatId) && swarmChatIsResetting(theBigOne, chatId);
      const switching = swarmConversationSwitching.has(theBigOne);
      const hydrating = swarmChatIsHydrating(theBigOne);
      // Sidebar rows are navigation for independent saved chats. A turn in
      // the selected chat must not disable a sibling row or its New-chat
      // action; only destructive controls for the exact live chat are held.
      if (action === "pick") {
        button.disabled = hydrating || switching || button.dataset.archived === "true";
      } else if (action === "archive") {
        button.disabled = hydrating || switching || targetBusy || targetResetting;
      } else {
        button.disabled = hydrating || switching;
      }
    }
    const stop = $("theBigChatStop");
    if (stop) {
      stop.disabled = !busy || stopping;
      stop.textContent = stopping ? "Stopping…" : "Stop";
    }
    renderWorkRecoveryButtons(theBigOne);
  }
}

// ---- changing it ---------------------------------------------------------

// Every change goes through here, so the board on screen and the board on disk
// cannot drift apart. What comes back is what was really written, which is why
// it is read back into the panel rather than trusted from here.
//
// One at a time, in the order they were made, and what is queued is the change
// rather than the board it would leave behind. Two reasons, and the second one
// took a while to find.
//
// A board carries which version it was built from. Two changes made in the same
// second - a tick and then a drag, or somebody quick with a mouse - both went
// off carrying the version from before either had landed, and the second came
// back "somebody changed the board in another window", which was not true and
// was no help at all.
//
// Then waiting for the one before it was still not enough. Each change edited
// the board held on the page and queued a save of that; when the save before it
// came back, the whole board was replaced by what the server said, and the
// change waiting behind it was gone - while the message on screen said it had
// happened. Queueing the change means it runs against the board the one before
// it really wrote.
let theChangeBeforeThis = Promise.resolve();
let swarmAddingAgent = false;
let swarmAddingProject = false;

function changeTheSwarmBoard(change, note) {
  const mine = theChangeBeforeThis.then(() => applyOneChangeToTheBoard(change, note));
  // Caught here rather than left on the chain: one change that went wrong must
  // not stop every change after it.
  theChangeBeforeThis = mine.catch(() => {});
  return mine;
}

async function applyOneChangeToTheBoard(change, note) {
  try {
    const words = change(theSwarmBoard());
    if (words === false) return false;
    const said = await request("/api/swarm/save", {
      method: "POST", body: JSON.stringify({board: theSwarmBoard()}),
    });
    // Changes go one at a time and each is built on the one before, so a save
    // that came back is always the newest word on the board. Counted, so a read
    // that was already in flight knows to throw its own answer away.
    howManyChangesLanded += 1;
    swarmSaid = said;
    acceptKeptInventory(said, true);
    keepTheSwarmPick();
    renderSwarmBoard();
    renderSwarmNotReady();
    renderSwarmPanel();
    renderTheChatsOnThisBoard();
    void Promise.all(swarmChats.map(
      (held) => loadConversationsFor(held.agent, false)
    ));
    if (note) sayInSwarm(typeof note === "function" ? note() : note);
    return true;
  } catch (error) {
    // Whatever was half changed here is thrown away and the written-down board
    // is read again, so the screen never shows a change that was refused. The
    // reason comes after that reading, or the reading would wipe it.
    await refreshSwarm(true);
    showError(error.message);
    sayInSwarm(error.message);
    return false;
  }
}

// Somewhere free to put a new box: along a row, then down to the next.
function aFreeSpotOnTheBoard(kind) {
  const held = kind === "agent" ? theSwarmBoard().agents : theSwarmBoard().projects;
  const down = kind === "agent" ? 40 : 420;
  return {x: 40 + (held.length % 4) * 230, y: down + Math.floor(held.length / 4) * 130};
}

async function addAnAgentToTheBoard() {
  if (swarmAddingAgent) return;
  swarmAddingAgent = true;
  try {
    await addOneAgentToTheBoard();
  } finally {
    swarmAddingAgent = false;
  }
}

async function addOneAgentToTheBoard() {
  const taken = new Set(theSwarmBoard().agents.map((one) => one.name.toLowerCase()));
  let name = "New agent";
  for (let number = 2; taken.has(name.toLowerCase()); number += 1) name = `New agent ${number}`;
  const said = await askForOneLine(
    "Add another agent", "What do you want to call it?", name);
  if (said === null) { sayInSwarm("Nothing was added."); return; }
  // Tidied the way the server tidies it. It collapses runs of spaces, so a
  // name typed with two of them came back different from what was sent and the
  // agent just added was never the one that got picked.
  const wanted = said.trim().replace(/\s+/g, " ");
  if (!wanted) { sayInSwarm("An agent needs a name."); return; }
  const ready = (swarmSaid.who_can_be_used || []).find((one) => one.ready);
  const worked = await changeTheSwarmBoard((board) => {
    board.agents.push({
      id: "", name: wanted, who: ready ? ready.route : "", job: "",
      at: aFreeSpotOnTheBoard("agent"),
    });
  }, `${wanted} is on the board.`);
  if (!worked) return;
  const added = theSwarmBoard().agents.find((one) => one.name === wanted);
  if (added) pickSwarmBox("agent", added.id);
}

async function addAProjectToTheBoard() {
  if (swarmAddingProject) return;
  swarmAddingProject = true;
  try {
    await addOneProjectToTheBoard();
  } finally {
    swarmAddingProject = false;
  }
}

async function addOneProjectToTheBoard() {
  const already = new Set(theSwarmBoard().projects.map((one) => one.path));
  const known = (swarmSaid.projects_on_this_machine || [])
    .find((one) => !already.has(one.path));
  const said = await askForOneLine(
    "Add another project folder", "Which folder do you want worked on?",
    known ? known.path : "", null, true);
  if (said === null) { sayInSwarm("Nothing was added."); return; }
  const path = said.trim();
  if (!path) { sayInSwarm("A project needs a folder."); return; }
  const worked = await changeTheSwarmBoard((board) => {
    board.projects.push({
      id: "", path, tasks: [], at: aFreeSpotOnTheBoard("project"),
    });
  }, `${path} is on the board.`);
  if (!worked) return;
  const added = theSwarmBoard().projects.find((one) => one.path === path);
  if (added) pickSwarmBox("project", added.id);
}

async function saveTheSwarmAgent() {
  const agent = thePickedAgent();
  if (!agent) { sayInSwarm("Press the gear on an agent first."); return; }
  rememberSwarmAgentSettings(null);
  await flushSwarmAgentSettings(agent.id, true);
}

function restoreSwarmRemovalFocus(invoker) {
  const current = invoker?.id ? document.getElementById(invoker.id) : invoker;
  current?.focus?.({preventScroll: true});
}

async function removeTheSwarmAgent(event) {
  const agent = thePickedAgent();
  if (!agent) { sayInSwarm("Press the gear on an agent first."); return; }
  const invoker = event?.currentTarget || document.activeElement;
  const projects = theSwarmBoard().works_on.filter((line) => line.agent === agent.id).length;
  const connections = theSwarmBoard().talks_to.filter(
    (line) => line.one === agent.id || line.other === agent.id).length;
  if (!window.confirm(`Remove ${agent.name} from this board? This also removes ${projects} project assignment${projects === 1 ? "" : "s"} and ${connections} agent connection${connections === 1 ? "" : "s"}. Saved chat transcripts and project files are kept.`)) {
    restoreSwarmRemovalFocus(invoker);
    return;
  }
  // Removal is an explicit decision to discard the form as well as the agent.
  discardSwarmAgentSettings(agent.id);
  const changed = await changeTheSwarmBoard((board) => {
    board.agents = board.agents.filter((one) => one.id !== agent.id);
    board.works_on = board.works_on.filter((line) => line.agent !== agent.id);
    board.talks_to = board.talks_to.filter(
      (line) => line.one !== agent.id && line.other !== agent.id);
    swarmChats = swarmChats.filter((one) => one.agent !== agent.id);
    swarmPicked = null;
  }, `${agent.name} is off the board. What it said is kept.`);
  if (changed) $("swarmAddAgent")?.focus?.({preventScroll: true});
  else restoreSwarmRemovalFocus(invoker);
}

async function removeTheSwarmProject(event) {
  const project = thePickedProject();
  if (!project) { sayInSwarm("Press the gear on a project folder first."); return; }
  const invoker = event?.currentTarget || document.activeElement;
  const assignments = theSwarmBoard().works_on.filter((line) => line.project === project.id).length;
  const tasks = project.tasks?.length || 0;
  if (!window.confirm(`Remove ${project.name} from this board? This removes ${assignments} agent assignment${assignments === 1 ? "" : "s"} and ${tasks} board task${tasks === 1 ? "" : "s"}. Nothing in the project folder is changed.`)) {
    restoreSwarmRemovalFocus(invoker);
    return;
  }
  const changed = await changeTheSwarmBoard((board) => {
    board.projects = board.projects.filter((one) => one.id !== project.id);
    board.works_on = board.works_on.filter((line) => line.project !== project.id);
    swarmPicked = null;
  }, `${project.name} is off the board. Nothing in the folder was changed.`);
  if (changed) $("swarmAddProject")?.focus?.({preventScroll: true});
  else restoreSwarmRemovalFocus(invoker);
}

async function rebindTheSwarmProject() {
  const project = thePickedProject();
  if (!project) { sayInSwarm("Press the gear on a project folder first."); return; }
  const said = await askForOneLine(
    "Use this board project on this computer",
    "Which local folder contains this same project? Its tasks, agents, links, and history identity stay on the board. Test-command approval is cleared.",
    project.is_there ? project.path : "", null, true,
  );
  if (said === null) { sayInSwarm("The project folder was not changed."); return; }
  const wanted = said.trim();
  if (!wanted) { sayInSwarm("Choose a project folder."); return; }
  if (wanted === project.path) {
    sayInSwarm("That project already uses this folder.");
    return;
  }
  const changed = await changeTheSwarmBoard((board) => {
    const held = board.projects.find((one) => one.id === project.id);
    if (!held) return false;
    held.path = wanted;
    held.approved_test_command_digest = "";
  }, () => {
    const rebound = theSwarmProject(project.id);
    return `Rebound ${rebound?.name || project.name} to ${wanted}. Tasks, agents, links, and its board identity were kept; local command approval was cleared.`;
  });
  if (changed) pickSwarmBox("project", project.id);
}

async function addOneSwarmTask() {
  const project = thePickedProject();
  if (!project) return;
  const problem = disclosedTextProblem(
    "swarmTaskText", "swarmTaskTextCount",
    BOARD_TASK_CHARACTER_LIMIT, "the project job");
  if (problem) { sayInSwarm(problem); $("swarmTaskText").focus(); return; }
  const words = $("swarmTaskText").value;
  if (!words.trim()) { sayInSwarm("Type the job first."); return; }
  const worked = await changeTheSwarmBoard((board) => {
    const held = board.projects.find((one) => one.id === project.id);
    if (!held) return false;
    held.tasks.push(words);
  }, `Added to ${project.name}: ${words}`);
  if (worked) {
    $("swarmTaskText").value = "";
    renderDisclosedTextCount(
      "swarmTaskText", "swarmTaskTextCount",
      BOARD_TASK_CHARACTER_LIMIT, "the project job");
  }
}

function removeOneSwarmTask(projectId, at) {
  const project = theSwarmProject(projectId);
  if (!project) return Promise.resolve(false);
  const gone = project.tasks[at];
  return changeTheSwarmBoard((board) => {
    const held = board.projects.find((one) => one.id === projectId);
    if (!held || held.tasks[at] !== gone) return false;
    held.tasks.splice(at, 1);
  }, `Off ${project.name}: ${gone}`);
}

async function setThePickedLine(on) {
  const line = thePickedLine();
  if (!line) return;
  if (line.kind === "works") {
    await setWhoWorksOnWhat(line.agent, line.project, on);
  } else {
    await setWhetherTheyTalk(line.one, line.other, on);
  }
}

function tidyTheSwarmBoard() {
  return changeTheSwarmBoard((board) => {
    board.agents.forEach((one, at) => {
      one.at = {x: 40 + (at % 4) * 230, y: 40 + Math.floor(at / 4) * 130};
    });
    board.projects.forEach((one, at) => {
      one.at = {x: 40 + (at % 4) * 230, y: 420 + Math.floor(at / 4) * 130};
    });
  }, "The board was tidied. Agents on top, projects below.");
}

// ---- the chat boxes on the board -----------------------------------------
//
// One box per agent, on the board, beside the agent it belongs to, with a line
// back to it. Big, because a chat in a strip at the edge of the page is a chat
// nobody uses, and because the answer is the part you came to read.

async function openTheChatFor(agentId) {
  const agent = theSwarmAgent(agentId);
  if (!agent) return;
  const already = swarmChats.find((one) => one.agent === agentId);
  if (!already) {
    swarmConversationHydrating.add(agentId);
    swarmChats.push({
      agent: agentId,
      at: {x: Math.max(0, agent.at.x - 20), y: agent.at.y + 190},
      minimised: false,
      said: [],
      saidFor: "",
      notice: "",
      noticeFor: "",
      conversations: [],
      conversation: "",
    });
  } else {
    // The chat button on the agent means show its board card again. A chat in
    // the tray is still open; it was only put out of the way.
    already.minimised = false;
  }
  renderSwarmBoard();
  renderTheChatsOnThisBoard();
  await loadConversationsFor(agentId);
  renderTheChatTray();
  const card = theChatCardFor(agentId);
  if (card) {
    card.querySelector(".swarm-chat-box").focus();
    card.scrollIntoView({block: "nearest"});
  }
}

function closeTheChatFor(agentId) {
  cancelConversationReadsFor(agentId);
  swarmConversationHydrating.delete(agentId);
  swarmConversationTranscriptRefreshes.delete(agentId);
  swarmChats = swarmChats.filter((one) => one.agent !== agentId);
  renderSwarmBoard();
  renderTheChatsOnThisBoard();
  renderTheChatTray();
}

function setProjectVerificationApproval(approved) {
  const mine = theChangeBeforeThis.then(
    () => applyProjectVerificationApproval(Boolean(approved))
  );
  theChangeBeforeThis = mine.catch(() => {});
  return mine;
}

async function applyProjectVerificationApproval(approved) {
  const project = thePickedProject();
  if (!project) {
    sayInSwarm("Press the gear on a project folder first.");
    return false;
  }
  const proposal = (swarmSaid.verification_command_approvals || [])
    .find((one) => one.project_id === project.id) || {};
  if (approved && (!proposal.can_approve || !proposal.approval_digest)) {
    sayInSwarm(proposal.reason || "There are no discovered test commands to approve.");
    return false;
  }
  if (approved) {
    const exact = (proposal.commands || [])
      .map((command) => JSON.stringify(command)).join("\n");
    if (!window.confirm(
      `Allow Nexus to run only these discovered test commands in ${project.path}?\n\n${exact}\n\n`
      + `Approval fingerprint: ${proposal.approval_digest}\n\n`
      + "Changing the path or test configuration makes Nexus ask again."
    )) {
      sayInSwarm("Nothing was approved.");
      return false;
    }
  } else if (!window.confirm(
    `Revoke test-command approval for ${project.path}? Nexus will stop before running discovered project tests.`
  )) {
    sayInSwarm("The approval was left as it was.");
    return false;
  }
  try {
    const said = await request("/api/swarm/verification-approval", {
      method: "POST",
      body: JSON.stringify({
        project_id: project.id,
        project_path: project.path,
        board_version: theSwarmBoard().version,
        approved,
        approval_digest: approved ? proposal.approval_digest : "",
      }),
    });
    howManyChangesLanded += 1;
    swarmSaid = said;
    acceptKeptInventory(said, true);
    keepTheSwarmPick();
    renderSwarmBoard();
    renderSwarmNotReady();
    renderSwarmPanel();
    renderTheChatsOnThisBoard();
    sayInSwarm(said.verification_command_approval_note || (
      approved ? "Those exact test commands are approved." : "Test-command approval was revoked."
    ));
    return true;
  } catch (error) {
    await refreshSwarm(true);
    showError(error.message);
    sayInSwarm(error.message);
    return false;
  }
}

function pairKey(pair) {
  return [...pair].sort().join("|");
}

function connectedPairsFor(agentId) {
  const board = theSwarmBoard();
  const peers = (board.agents || []).filter((one) => (
    one.id !== agentId && (board.talks_to || []).some((line) => (
      [line.one, line.other].sort().join("|") === pairKey([agentId, one.id])
    ))
  ));
  return peers.map((peer) => [agentId, peer.id].sort());
}

function conversationPairsFor(agentId) {
  return [...connectedPairsFor(agentId), [agentId]];
}

function applyConversationList(agentId, said) {
  const held = swarmChats.find((one) => one.agent === agentId);
  if (!held) return false;
  swarmConversationHydrating.delete(agentId);
  const before = held.conversation;
  rememberSwarmChatComposer(agentId);
  held.conversations = Array.isArray(said?.chats) ? said.chats : [];
  const ids = new Set(held.conversations.map((one) => one.id));
  held.conversation = ids.has(said?.active) ? said.active
    : ids.has(held.conversation) ? held.conversation
    : held.conversations.find((one) => !one.archived_at)?.id || "";
  // Selection and transcript become one atomic piece of visible state. A
  // transcript is never reusable merely because it belongs to the same lead
  // agent; it must belong to this exact pair-chat id.
  const identityChanged = before !== held.conversation
    || held.saidFor !== transcriptIdentityFor(agentId);
  if (identityChanged) {
    held.said = [];
    held.saidFor = transcriptIdentityFor(agentId);
    nextSwarmChatRevision(agentId);
    renderTheChatThreadFor(agentId, []);
  }
  syncSwarmChatComposer(agentId);
  const card = theChatCardFor(agentId);
  const oldDestination = card?.querySelector(".chat-destination");
  if (oldDestination) {
    oldDestination.replaceWith(aChatDestination(theSwarmAgent(agentId), {
      offerFullChat: true, conversation: activeConversationFor(agentId),
    }));
    setWhatCanBePressedInAChat(card);
  }
  if (theBigOne === agentId) renderTheBigChat();
  return identityChanged;
}

async function loadConversationsFor(agentId, refresh = true) {
  const held = swarmChats.find((one) => one.agent === agentId);
  if (!held) return false;
  if (refresh || swarmChatIsHydrating(agentId)) {
    swarmConversationTranscriptRefreshes.add(agentId);
  }
  if (swarmConversationSwitching.has(agentId)) return false;
  const controller = beginConversationRead(swarmConversationListControllers, agentId);
  const revision = nextConversationListRevision(agentId);
  try {
    const said = await request(
      `/api/swarm/chats?agent=${encodeURIComponent(agentId)}`,
      {signal: controller.signal},
    );
    if (controller.signal.aborted || !swarmChats.includes(held)
        || swarmConversationListRevisions.get(agentId) !== revision) return false;
    const identityChanged = applyConversationList(agentId, said);
    // A metadata-only request may have superseded a full request while both
    // were in flight. The winning request inherits the pending transcript
    // intent instead of leaving a correctly selected chat visibly empty.
    const refreshTranscript = swarmConversationTranscriptRefreshes.delete(agentId)
      || identityChanged;
    if (refreshTranscript && swarmConversationListRevisions.get(agentId) === revision) {
      await refreshTheChatFor(agentId);
    }
    return !controller.signal.aborted;
  } catch (error) {
    if (conversationReadWasCancelled(error)) return false;
    if (swarmConversationListRevisions.get(agentId) !== revision) return false;
    // A failed first read must not leave this chat in a permanent loading
    // state. The virtual direct group remains usable, and a later metadata
    // read will request its transcript again when it has a saved identity.
    swarmConversationHydrating.delete(agentId);
    swarmConversationTranscriptRefreshes.delete(agentId);
    sayInTheChatFor(agentId, error.message);
    sayInBigChatConversationFor(agentId, error.message);
    return false;
  } finally {
    finishConversationRead(swarmConversationListControllers, agentId, controller);
    setWhatCanBePressedInSwarm();
  }
}

function finishConversationSwitch(agentId) {
  swarmConversationSwitching.delete(agentId);
  setWhatCanBePressedInSwarm();
  // A card can be closed and reopened while an older lifecycle request is in
  // flight. Its first metadata read correctly waits for that mutation; retry
  // now so the reopened card cannot remain forever under legacy:<agent>.
  if ((swarmChatIsHydrating(agentId)
      || swarmConversationTranscriptRefreshes.has(agentId))
      && swarmChats.some((one) => one.agent === agentId)) {
    void loadConversationsFor(agentId, false);
  }
}

async function createConversationFor(agentId, peerId, scope = "") {
  if (swarmConversationSwitching.has(agentId) || swarmChatIsHydrating(agentId)) return;
  swarmConversationSwitching.add(agentId);
  nextConversationListRevision(agentId);
  setWhatCanBePressedInSwarm();
  try {
    const said = await request("/api/swarm/chats/create", {
      method: "POST", body: JSON.stringify({agent: agentId, peer: peerId, scope}),
    });
    applyConversationList(agentId, said);
    keepWhatWasSaidTo(agentId, []);
    await refreshTheChatFor(agentId);
    sayInBigChatConversationFor(agentId,
      scope === "single" ? "New direct chat created." : "New pair chat created.");
    if (bigChatShows(agentId)) $("theBigChatBox").focus();
  } catch (error) {
    sayInBigChatConversationFor(agentId, error.message);
  } finally {
    finishConversationSwitch(agentId);
  }
}

async function activateConversationFor(agentId, chatId) {
  const held = swarmChats.find((one) => one.agent === agentId);
  const conversation = (held?.conversations || []).find((one) => one.id === chatId);
  if (!held || held.conversation === chatId
      || swarmConversationSwitching.has(agentId)
      || swarmChatIsHydrating(agentId)
      || !conversation || conversation.archived_at) return;
  rememberSwarmChatComposer(agentId);
  swarmConversationSwitching.add(agentId);
  nextConversationListRevision(agentId);
  // Change the highlighted row, header, destination, project, and transcript
  // together before waiting for disk. This makes it impossible to show the
  // previous chat under the newly selected title even for one frame.
  held.conversation = chatId;
  held.said = [];
  held.saidFor = transcriptIdentityFor(agentId);
  syncSwarmChatComposer(agentId);
  nextSwarmChatRevision(agentId);
  renderTheChatThreadFor(agentId, []);
  if (theBigOne === agentId) renderTheBigChat();
  setWhatCanBePressedInSwarm();
  let failed = null;
  try {
    const said = await request("/api/swarm/chats/activate", {
      method: "POST", body: JSON.stringify({agent: agentId, chat: chatId}),
    });
    if (!swarmChats.includes(held) || held.conversation !== chatId) return;
    applyConversationList(agentId, said);
    if (bigChatShows(agentId, chatId)) $("theBigChatSaidBack").textContent = "Switched chat.";
    await refreshTheChatFor(agentId);
  } catch (error) {
    failed = error;
  } finally {
    finishConversationSwitch(agentId);
  }
  if (failed) {
    // The optimistic selection was not committed. Re-read the authoritative
    // selection rather than leaving the interface on a chat the server did
    // not accept.
    await loadConversationsFor(agentId);
    sayInBigChatConversationFor(agentId, failed.message);
  }
}

async function archiveConversationFor(agentId, chatId) {
  const conversation = (swarmChats.find((one) => one.agent === agentId)?.conversations || [])
    .find((one) => one.id === chatId);
  if (!conversation || swarmChatIsBusy(agentId, chatId)
      || swarmChatIsResetting(agentId, chatId) || swarmChatIsHydrating(agentId)
      || swarmConversationSwitching.has(agentId)) return;
  const pair = (conversation.pair_agents || []).map((one) => one.name).join(" and ");
  if (!window.confirm(
    `Archive ${conversation.name} between ${pair || "these agents"}?\n\n`
    + "Its saved transcript and attachments will be kept and can be restored here."
  )) return;
  swarmConversationSwitching.add(agentId);
  nextConversationListRevision(agentId);
  setWhatCanBePressedInSwarm();
  try {
    const said = await request("/api/swarm/chats/delete", {
      method: "POST", body: JSON.stringify({agent: agentId, chat: chatId}),
    });
    applyConversationList(agentId, said);
    keepWhatWasSaidTo(agentId, []);
    if (activeConversationFor(agentId)) await refreshTheChatFor(agentId);
    sayInBigChatConversationFor(agentId, "Chat archived. Its history is still saved.");
  } catch (error) {
    sayInBigChatConversationFor(agentId, error.message);
  } finally {
    finishConversationSwitch(agentId);
  }
}

async function restoreConversationFor(agentId, chatId) {
  const conversation = (swarmChats.find((one) => one.agent === agentId)?.conversations || [])
    .find((one) => one.id === chatId);
  if (!conversation?.archived_at || swarmConversationSwitching.has(agentId)
      || swarmChatIsHydrating(agentId)) return;
  swarmConversationSwitching.add(agentId);
  nextConversationListRevision(agentId);
  setWhatCanBePressedInSwarm();
  try {
    const said = await request("/api/swarm/chats/restore", {
      method: "POST", body: JSON.stringify({agent: agentId, chat: chatId}),
    });
    applyConversationList(agentId, said);
    await refreshTheChatFor(agentId);
    sayInBigChatConversationFor(agentId, "Chat restored with its saved history.");
    if (bigChatShows(agentId)) $("theBigChatBox").focus();
  } catch (error) {
    sayInBigChatConversationFor(agentId, error.message);
  } finally {
    finishConversationSwitch(agentId);
  }
}

async function selectConversationProject(agentId, projectId) {
  const conversation = activeConversationFor(agentId);
  if (!conversation || swarmChatIsBusy(agentId) || swarmChatIsResetting(agentId)
      || swarmChatIsHydrating(agentId)
      || swarmConversationSwitching.has(agentId)) return;
  swarmConversationSwitching.add(agentId);
  nextConversationListRevision(agentId);
  setWhatCanBePressedInSwarm();
  try {
    const said = await request("/api/swarm/chats/project", {
      method: "POST", body: JSON.stringify({
        agent: agentId, chat: conversation.id, project: projectId,
      }),
    });
    applyConversationList(agentId, said);
    const selected = activeConversationFor(agentId);
    if (bigChatShows(agentId, conversation.id)) {
      $("theBigChatProjectHelp").textContent = selected?.project
        ? "This chat's file work is confined to the selected folder."
        : "Choose a shared project before asking the pair to change files.";
    }
  } catch (error) {
    sayInBigChatConversationFor(agentId, error.message, conversation.id);
    if (bigChatShows(agentId, conversation.id)) renderTheBigChat();
  } finally {
    finishConversationSwitch(agentId);
  }
}

// The cards move when they are dragged; the empty drawing paper moves the
// view. This makes a wide board usable without having to chase two small
// scrollbars, and deliberately starts only on the paper so dragging a card or
// pressing one of its controls keeps doing exactly what it did before.
function makeSwarmBoardPannable() {
  const board = $("swarmBoard");
  let panning = null;
  board.addEventListener("pointerdown", (event) => {
    const paper = ["swarmBoard", "swarmSurface", "swarmCanvas"];
    if (event.button !== 0 || !paper.includes(event.target.id)) return;
    panning = {
      x: event.clientX,
      y: event.clientY,
      left: board.scrollLeft,
      top: board.scrollTop,
      pointer: event.pointerId,
    };
    board.classList.add("panning");
    board.setPointerCapture(event.pointerId);
    event.preventDefault();
  });
  board.addEventListener("pointermove", (event) => {
    if (!panning || event.pointerId !== panning.pointer) return;
    board.scrollLeft = panning.left - (event.clientX - panning.x);
    board.scrollTop = panning.top - (event.clientY - panning.y);
  });
  const stop = (event) => {
    if (!panning || event.pointerId !== panning.pointer) return;
    panning = null;
    board.classList.remove("panning");
    if (board.hasPointerCapture(event.pointerId)) board.releasePointerCapture(event.pointerId);
  };
  board.addEventListener("pointerup", stop);
  board.addEventListener("pointercancel", stop);
  board.addEventListener("wheel", (event) => {
    if (!event.ctrlKey) return;
    event.preventDefault();
    setTheSwarmZoom(swarmZoom + (event.deltaY < 0 ? 0.1 : -0.1));
  }, {passive: false});
}

let swarmFullScreenHomes = [];
let swarmIsFullScreen = false;

function showTheSwarmPanel() {
  $("swarmPanel").hidden = false;
  drawSwarmLines();
}

async function closeTheSwarmPanel() {
  if (!swarmIsFullScreen) return;
  const agent = thePickedAgent();
  if (agent && !(await flushSwarmAgentSettings(agent.id))) return;
  renderSwarmBoard();
  $("swarmPanel").hidden = true;
  drawSwarmLines();
  $("swarmFullScreen").focus({preventScroll: true});
}

function putTheChatsInTheSwarmFullScreen() {
  keepTheBigChatComposerInHand(() => {
    for (const home of swarmFullScreenHomes) $("swarmStage").append(home.element);
  });
}

function putTheChatsBackWhereTheyLive() {
  keepTheBigChatComposerInHand(() => {
    for (const home of swarmFullScreenHomes) {
      home.parent.insertBefore(home.element, home.next);
    }
  });
}

function appearanceFromAgentForm(agent) {
  return {
    ...agent,
    name: $("swarmAgentName").value.trim() || agent.name,
    icon: $("swarmAgentIcon").value,
    colour: $("swarmAgentColour").value,
    bubble_colour: $("swarmAgentBubbleColour").value,
    profile_picture: swarmAgentPictureDraft,
    picture_zoom: Number($("swarmAgentPictureZoom").value),
    picture_hue: Number($("swarmAgentPictureHue").value),
  };
}

function previewSwarmAgentAppearance() {
  const agent = thePickedAgent();
  if (!agent) return;
  const draft = appearanceFromAgentForm(agent);
  const appearance = agentAppearance(draft);
  const preview = $("swarmAgentAppearancePreview");
  styleForAgent(preview, draft);
  putAgentFaceIn($("swarmAgentPreviewFace"), appearance, 36);
  $("swarmAgentPreviewName").textContent = draft.name;
  $("swarmPanelTitle").textContent = draft.name || agent.name;
  $("swarmAgentPictureZoomValue").textContent = `${Math.round(appearance.pictureZoom)}%`;
  $("swarmAgentPictureHueValue").textContent = `${Math.round(appearance.pictureHue)}°`;
  $("swarmAgentPictureClear").hidden = !appearance.picture;
  $("swarmAgentPictureZoom").disabled = !appearance.picture;
  $("swarmAgentPictureHue").disabled = !appearance.picture;

  // Show the draft on the actual board card too while its debounced autosave
  // is waiting. The form draft survives any board redraw until that save lands.
  const card = [...document.querySelectorAll('.swarm-box[data-kind="agent"]')]
    .find((one) => one.dataset.id === agent.id);
  if (card) {
    styleForAgent(card, draft);
    const face = card.querySelector(".swarm-box-face");
    if (face) {
      styleForAgent(face, draft);
      putAgentFaceIn(face, appearance, 28);
    }
    const name = card.querySelector(".swarm-box-name");
    if (name) name.textContent = draft.name;
  }
  return card;
}

async function resizedAgentPicture(file) {
  if (!file || !["image/png", "image/jpeg", "image/webp"].includes(file.type)) {
    throw new Error("Choose a PNG, JPEG, or WebP picture.");
  }
  if (file.size > 15_000_000) {
    throw new Error("That picture is larger than 15 MB. Choose a smaller copy.");
  }
  const url = URL.createObjectURL(file);
  try {
    const image = new Image();
    image.src = url;
    await new Promise((resolve, reject) => {
      image.onload = resolve;
      image.onerror = () => reject(new Error("That picture could not be read."));
    });
    if (!image.naturalWidth || !image.naturalHeight) {
      throw new Error("That picture has no readable pixels.");
    }
    for (const longest of [512, 420, 340, 280, 220]) {
      const ratio = Math.min(1, longest / Math.max(image.naturalWidth, image.naturalHeight));
      const canvas = document.createElement("canvas");
      canvas.width = Math.max(1, Math.round(image.naturalWidth * ratio));
      canvas.height = Math.max(1, Math.round(image.naturalHeight * ratio));
      canvas.getContext("2d").drawImage(image, 0, 0, canvas.width, canvas.height);
      for (const quality of [0.9, 0.78, 0.64]) {
        const data = canvas.toDataURL("image/webp", quality);
        if (data.length <= MAX_AGENT_PICTURE_LENGTH) return data;
      }
    }
    throw new Error("That picture could not be made small enough to keep safely.");
  } finally {
    URL.revokeObjectURL(url);
  }
}

async function useAgentPictureFile() {
  const file = $("swarmAgentPictureFile").files?.[0];
  if (!file) return;
  $("swarmAgentPictureSaid").textContent = "Preparing the picture…";
  try {
    swarmAgentPictureDraft = await resizedAgentPicture(file);
    $("swarmAgentPictureSaid").textContent =
      `${file.name} is ready. You can adjust zoom and hue while it saves.`;
    previewSwarmAgentAppearance();
    rememberSwarmAgentSettings(0);
  } catch (error) {
    $("swarmAgentPictureSaid").textContent = error.message;
    sayInSwarm(error.message);
  } finally {
    $("swarmAgentPictureFile").value = "";
  }
}

function showHowTheSwarmFillsTheScreen(full = document.fullscreenElement === $("swarmStage")) {
  const stage = $("swarmStage");
  swarmIsFullScreen = Boolean(full);
  stage.classList.toggle("is-fullscreen", swarmIsFullScreen);
  document.body.classList.toggle("workspace-is-fullscreen", swarmIsFullScreen);
  const button = $("swarmFullScreen");
  button.textContent = swarmIsFullScreen ? "Exit full screen" : "Full screen";
  button.setAttribute("aria-pressed", String(swarmIsFullScreen));
  button.title = swarmIsFullScreen
    ? "Return the board to the page"
    : "Fill the screen with the board and these controls";
  if (!swarmIsFullScreen) {
    putTheChatsBackWhereTheyLive();
    showTheSwarmPanel();
  }
  drawSwarmLines();
}

async function toggleTheSwarmFullScreen() {
  const wanted = !swarmIsFullScreen;
  const focusedBox = $("theBigChatBox");
  const restoreComposer = Boolean(
    focusedBox && document.activeElement === focusedBox && !$("theBigChat").hidden,
  );
  const focusedKey = theBigChatComposerKey;
  const focusedState = restoreComposer ? rememberTheBigChatComposer() : null;
  try {
    if (wanted) putTheChatsInTheSwarmFullScreen();
    if (window.harnessDesktop?.setFullScreen) {
      showHowTheSwarmFillsTheScreen(wanted);
      const changed = await window.harnessDesktop.setFullScreen(wanted);
      if (changed !== wanted) showHowTheSwarmFillsTheScreen(changed);
    } else if (document.fullscreenElement === $("swarmStage")) {
      await document.exitFullscreen();
    } else {
      await $("swarmStage").requestFullscreen();
    }
  } catch (error) {
    putTheChatsBackWhereTheyLive();
    sayInSwarm(`Full screen could not be opened: ${error.message || error}`);
  } finally {
    // Native full-screen transitions can take focus after the DOM has already
    // been reparented. The desktop bridge now waits for that transition; put
    // the exact saved chat's caret back only after it has really completed.
    const box = $("theBigChatBox");
    if (restoreComposer && focusedState && box?.isConnected
        && !$("theBigChat").hidden && theBigChatComposerKey === focusedKey) {
      box.focus({preventScroll: true});
      box.setSelectionRange(
        focusedState.start, focusedState.end, focusedState.direction || "none",
      );
    }
  }
}

function minimiseTheChatFor(agentId) {
  const held = swarmChats.find((one) => one.agent === agentId);
  if (!held) return;
  held.minimised = true;
  renderSwarmBoard();
  renderTheChatsOnThisBoard();
  renderTheChatTray();
}

function theChatCardFor(agentId) {
  return $("swarmBoard").querySelector(`.swarm-chat-card[data-agent="${agentId}"]`);
}

function chatDestinationFor(agent, conversation = null) {
  return conversation?.destination || (agent && agent.chat_destination) || {
    owner_label: "Nexus Harness",
    connected: Boolean(agent && agent.who),
    provider_label: (agent && (agent.who || agent.assistant_kind)) || "No assistant chosen",
    provider_app_linked: false,
    route: (agent && agent.who) || "",
    model: "",
    transcript_path: "",
    transcript_exists: false,
    collaboration_path: "",
    collaboration_exists: false,
    explanation: agent && agent.who
      ? "This conversation is kept by Nexus; no provider-app chat link is available."
      : "Choose an assistant before this Nexus chat can send a message.",
  };
}

async function showTheSavedChat(destination, button) {
  if (!window.harnessDesktop?.showProjectFile || !destination.transcript_path) return;
  const was = button.textContent;
  button.disabled = true;
  button.textContent = "Opening...";
  try {
    const opened = await window.harnessDesktop.showProjectFile(destination.transcript_path);
    if (!opened) sayInSwarm("The saved transcript file is not there yet.");
  } finally {
    button.disabled = !destination.transcript_exists;
    button.textContent = was;
  }
}

async function showTheSharedLedger(destination, button) {
  if (!window.harnessDesktop?.showProjectFile || !destination.collaboration_path) return;
  const was = button.textContent;
  button.disabled = true;
  button.textContent = "Opening...";
  try {
    const opened = await window.harnessDesktop.showProjectFile(destination.collaboration_path);
    if (!opened) sayInSwarm("The shared agent ledger is not there yet.");
  } finally {
    button.disabled = !destination.collaboration_exists;
    button.textContent = was;
  }
}

async function resetCollaborationRecord(agent, conversation, button) {
  if (!agent?.id || !conversation?.id || !conversation?.collaboration_problem) return;
  if (!window.confirm(
    "Reset only this chat's damaged collaboration record?\n\n"
    + "The saved transcript, attachments, and provider conversations are preserved. "
    + "No prompt is sent and no AI is contacted."
  )) return;
  const was = button.textContent;
  button.disabled = true;
  button.textContent = "Resetting record…";
  try {
    const said = await request("/api/swarm/collaboration/reset", {
      method: "POST", body: JSON.stringify({
        agent: agent.id, chat: conversation.id,
      }),
    });
    sayInRuntimeChat(swarmChatRuntimeKeyFor(agent.id, conversation.id),
      said.note || "The collaboration record was reset. The transcript and provider conversations were preserved.");
    await loadConversationsFor(agent.id, false);
    await refreshTheChatFor(agent.id);
  } catch (error) {
    const words = String(error?.message || error);
    showError(words);
    sayInRuntimeChat(swarmChatRuntimeKeyFor(agent.id, conversation.id), words);
  } finally {
    if (button.isConnected) {
      button.disabled = false;
      button.textContent = was;
    }
  }
}

function aChatDestination(agent, {offerFullChat = false, conversation = null} = {}) {
  const destination = chatDestinationFor(agent, conversation);
  const box = make("section", "chat-destination");
  box.append(make("strong", "", "Where this chat happens"));
  box.append(make("p", "",
    `${destination.owner_label || "Nexus Harness"} owns and saves this conversation.`));
  const route = [];
  if (destination.provider_label) route.push(`Answers come through ${destination.provider_label}`);
  if (destination.route) route.push(`route “${destination.route}”`);
  if (destination.model) route.push(`model ${destination.model}`);
  box.append(make("p", "chat-destination-route", route.join(" • ")));
  if (destination.transcript_path) {
    box.append(make("p", "chat-destination-route",
      `Saved transcript: ${destination.transcript_path}`));
  }
  if (destination.collaboration_path) {
    box.append(make("p", "chat-destination-route",
      `Live shared agent ledger: ${destination.collaboration_path}`));
  }
  box.append(make("p", "chat-destination-warning", destination.explanation));
  const collaborationProblem = conversation?.collaboration_problem;
  if (collaborationProblem) {
    box.append(make("p", "chat-destination-warning collaboration-problem",
      String(collaborationProblem.message || collaborationProblem)));
    if (collaborationProblem.action_note) {
      box.append(make("p", "hint collaboration-problem-note",
        String(collaborationProblem.action_note)));
    }
  }
  const actions = make("div", "chat-destination-actions");
  if (offerFullChat && agent) {
    const open = make("button", "", "Open full Nexus chat");
    open.type = "button";
    open.dataset.bigChatInvoker = agent.id;
    open.addEventListener("click", () => openTheBigChat(agent.id));
    actions.append(open);
  }
  if (destination.web_chat_id && window.harnessDesktop?.showWebChat) {
    const fullWeb = make("button", "primary", "View full web AI chat");
    fullWeb.type = "button";
    fullWeb.title = "Show the complete provider website conversation inside Nexus";
    fullWeb.addEventListener("click", () => (
      showFullWebChatInsideNexus(
        destination.web_chat_id,
        destination.web_conversation_key || "",
        Boolean(destination.web_prefer_existing_conversation),
      )
    ));
    actions.append(fullWeb);
  }
  if (destination.web_chat_id && window.harnessDesktop?.openWebChatWindow) {
    const provider = make("button", "", "Open web AI in a window");
    provider.type = "button";
    provider.addEventListener("click", () => (
      window.harnessDesktop.openWebChatWindow(
        destination.web_chat_id,
        destination.web_conversation_key || "",
        Boolean(destination.web_prefer_existing_conversation),
      )
    ));
    actions.append(provider);
  }
  if (window.harnessDesktop?.showProjectFile && destination.transcript_path) {
    const saved = make("button", "", "Show saved transcript file");
    saved.type = "button";
    saved.disabled = !destination.transcript_exists;
    saved.title = destination.transcript_exists
      ? destination.transcript_path
      : "The transcript file appears after the first message is saved.";
    saved.addEventListener("click", () => showTheSavedChat(destination, saved));
    actions.append(saved);
  }
  if (window.harnessDesktop?.showProjectFile && destination.collaboration_path) {
    const shared = make("button", "", "Show shared agent ledger");
    shared.type = "button";
    shared.disabled = !destination.collaboration_exists;
    shared.title = destination.collaboration_exists
      ? destination.collaboration_path
      : "The shared ledger appears when connected agents begin a collaboration.";
    shared.addEventListener("click", () => showTheSharedLedger(destination, shared));
    actions.append(shared);
  }
  if (collaborationProblem && agent && conversation) {
    const reset = make("button", "danger collaboration-record-reset",
      String(collaborationProblem.action_label || "Reset collaboration record"));
    reset.type = "button";
    reset.disabled = swarmChatIsBusy(agent.id, conversation.id);
    reset.title = reset.disabled
      ? "Stop this exact chat's current request before resetting its collaboration record."
      : "Reset only the damaged collaboration ledger. Preserve the transcript and provider conversations; send nothing.";
    reset.addEventListener("click", () => (
      resetCollaborationRecord(agent, conversation, reset)
    ));
    actions.append(reset);
  }
  if (actions.childElementCount) box.append(actions);
  return box;
}

function readChatAttachment(file) {
  return new Promise((resolve, reject) => {
    if (file.size > 4000000) {
      reject(new Error(`${file.name} is larger than 4 MB.`));
      return;
    }
    const reader = new FileReader();
    reader.addEventListener("load", () => resolve({
      name: file.name, type: file.type || "application/octet-stream",
      size: file.size, data: String(reader.result || ""),
    }));
    reader.addEventListener("error", () => reject(new Error(`${file.name} could not be read.`)));
    reader.readAsDataURL(file);
  });
}

async function addChatAttachments(agentId, files) {
  const key = swarmChatKey(agentId);
  const existing = swarmChatAttachments.get(key) || [];
  const selected = [...files];
  if (existing.length + selected.length > 6) {
    showError("Attach at most 6 files at once.");
    return;
  }
  try {
    const added = await Promise.all(selected.map(readChatAttachment));
    if ([...existing, ...added].reduce((sum, one) => sum + one.size, 0) > 8000000) {
      throw new Error("The attachments together are larger than 8 MB.");
    }
    swarmChatAttachments.set(key, [...existing, ...added]);
    renderChatAttachments(agentId);
  } catch (error) {
    showError(String(error.message || error));
  }
}

function removeChatAttachment(agentId, index) {
  const key = swarmChatKey(agentId);
  const kept = [...(swarmChatAttachments.get(key) || [])];
  kept.splice(index, 1);
  if (kept.length) swarmChatAttachments.set(key, kept);
  else swarmChatAttachments.delete(key);
  renderChatAttachments(agentId);
}

function attachmentChip(agentId, one, index) {
  const chip = make("span", "chat-attachment");
  if (String(one.type || "").startsWith("image/") && one.data) {
    const preview = document.createElement("img");
    preview.src = one.data;
    preview.alt = "";
    chip.append(preview);
  }
  chip.append(make("span", "chat-attachment-name", one.name));
  const remove = make("button", "chat-attachment-remove", "×");
  remove.type = "button";
  remove.title = `Remove ${one.name}`;
  remove.addEventListener("click", () => removeChatAttachment(agentId, index));
  chip.append(remove);
  return chip;
}

function renderChatAttachments(agentId) {
  const attachments = swarmChatAttachments.get(swarmChatKey(agentId)) || [];
  const card = theChatCardFor(agentId);
  const compact = card && card.querySelector(".chat-attachments");
  if (compact) compact.replaceChildren(...attachments.map(
    (one, index) => attachmentChip(agentId, one, index)));
  const big = $("theBigChatAttachments");
  if (big && theBigOne === agentId) big.replaceChildren(...attachments.map(
    (one, index) => attachmentChip(agentId, one, index)));
}

function oneSwarmChatCard(held) {
  const agent = theSwarmAgent(held.agent);
  const card = make("div", "swarm-chat-card");
  card.dataset.agent = held.agent;
  card.style.left = `${held.at.x}px`;
  card.style.top = `${held.at.y}px`;
  styleForAgent(card, agent);

  const bar = make("div", "swarm-chat-bar");
  const grip = make("button", "swarm-chat-grip");
  grip.type = "button";
  grip.append(make("strong", "", `Nexus chat with ${agent.name}`));
  grip.title = "Drag to move this chat, or use the arrow keys";
  bar.append(grip);
  bar.append(aSwarmButton("swarm-icon-button", "minus", "minimise",
    () => minimiseTheChatFor(held.agent), `minimise the chat with ${agent.name}`, "minimise"));
  bar.append(aSwarmButton("swarm-icon-button", "cross", "close",
    () => closeTheChatFor(held.agent), `close the chat with ${agent.name}`, "close"));
  card.append(bar);

  const keptNotice = keptChatNoticeFor(held.agent);
  card.append(make("p", "swarm-chat-said hint", keptNotice || (agent.ready
    ? (agent.trouble_last_time || "Nobody else reads this.")
    : (agent.why_not || "This one is not set up yet."))));
  if (agent.how_to_fix_it) {
    card.append(make("p", "swarm-chat-repair hint", agent.how_to_fix_it));
  }
  if (agent.trouble_last_time || !agent.ready) {
    const repair = make("button", "swarm-repair", "Repair connection");
    repair.type = "button";
    repair.addEventListener("click", () => void openAgentRepairFlow(agent.id, repair));
    card.append(repair);
  }
  card.append(aChatDestination(agent, {
    offerFullChat: true, conversation: activeConversationFor(held.agent),
  }));
  const transcript = make("div", "swarm-chat-transcript");
  const thread = make("ol", "swarm-chat-thread talk-thread");
  transcript.append(thread);
  putTheChatTurnsIn(
    thread, agent, chatTurnsWhileWorking(held.agent, keptTranscriptFor(held.agent)), false
  );

  const activityPanel = aChatActivityPanel("swarm-chat-activity");
  showActivityInPanel(
    activityPanel, visibleSwarmChatActivity(swarmChatActivityFor(held.agent)),
  );
  transcript.append(activityPanel);
  card.append(transcript);

  const recoveryPanel = make("section", "work-recovery swarm-chat-work-recovery");
  fillWorkRecoveryPanel(recoveryPanel, held.agent);
  card.append(recoveryPanel);

  const form = make("form", "swarm-chat-form");
  const box = make("textarea", "swarm-chat-box");
  box.rows = 6;
  box.placeholder = "What did you change and why?";
  box.setAttribute("aria-label", `What to say to ${agent.name}`);
  const composerKey = swarmChatKey(held.agent);
  const composer = swarmChatComposerDrafts.get(composerKey);
  box.value = composer?.value || "";
  if (composer) box.setSelectionRange(
    composer.start, composer.end, composer.direction || "none",
  );
  swarmChatComposerKeys.set(held.agent, composerKey);
  form.append(box);
  const attachments = make("div", "chat-attachments");
  form.append(attachments);
  const files = document.createElement("input");
  files.type = "file";
  files.multiple = true;
  files.className = "sr-only swarm-chat-files";
  files.setAttribute("aria-label", `Attach files or screenshots to ${agent.name}'s chat`);
  files.accept = "image/*,.txt,.md,.json,.yaml,.yml,.toml,.ini,.csv,.py,.js,.ts,.tsx,.jsx,.css,.html,.xml";
  files.addEventListener("change", async () => {
    await addChatAttachments(held.agent, files.files || []);
    files.value = "";
  });
  form.append(files);
  form.append(aChatRoundPolicy(held.agent));
  const recipientWords = chatRecipientWords(held.agent);
  const scope = make("p", "hint swarm-chat-scope", recipientWords.help);
  scope.setAttribute("role", "status");
  scope.setAttribute("aria-live", "polite");
  form.append(scope);
  const readiness = make("section", "chat-team-readiness swarm-chat-team-readiness");
  readiness.setAttribute("aria-live", "polite");
  readiness.setAttribute("aria-label", "Team connection readiness");
  form.append(readiness);
  fillChatTeamReadiness(readiness, held.agent);
  const row = make("div", "button-row");
  const send = make("button", "primary swarm-chat-send", recipientWords.direct);
  send.type = "submit";
  send.title = "Send only to this agent";
  send.setAttribute("aria-label", recipientWords.direct);
  row.append(send);
  const stop = make("button", "danger swarm-chat-stop", "Stop");
  stop.type = "button";
  stop.title = "Stop only this chat's current request";
  stop.disabled = true;
  stop.addEventListener("click", () => stopChatFor(held.agent));
  row.append(stop);
  const attach = make("button", "swarm-chat-attach", "Attach");
  attach.type = "button";
  attach.addEventListener("click", () => files.click());
  row.append(attach);
  const collaborate = make("button", "swarm-chat-collaborate", recipientWords.team);
  collaborate.type = "button";
  collaborate.title = "Immediately relay this prompt to every ready connected agent";
  collaborate.setAttribute("aria-label", recipientWords.team);
  collaborate.addEventListener("click", () => sendWhatIsTypedTo(held.agent, "collaborate"));
  row.append(collaborate);
  const work = make("button", "swarm-chat-work", "Work on project files");
  work.type = "button";
  work.title = "Ask connected project agents to plan, then apply validated file changes";
  work.addEventListener("click", () => sendWhatIsTypedTo(held.agent, "work"));
  row.append(work);
  const again = make("button", "swarm-chat-again", "Start again");
  again.type = "button";
  again.addEventListener("click", () => startTheChatAgainFor(held.agent));
  row.append(again);
  row.append(make("span", "swarm-chat-count hint"));
  form.append(row);
  const workStatus = make("p", "hint swarm-chat-work-status");
  workStatus.setAttribute("role", "status");
  workStatus.setAttribute("aria-live", "polite");
  workStatus.hidden = true;
  form.append(workStatus);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    sendWhatIsTypedTo(held.agent);
  });
  box.addEventListener("input", () => {
    rememberSwarmChatComposer(held.agent);
    countWhatIsTypedTo(held.agent);
  });
  box.addEventListener("select", () => rememberSwarmChatComposer(held.agent));
  box.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      sendWhatIsTypedTo(held.agent);
    }
  });
  attachments.replaceChildren(...(swarmChatAttachments.get(swarmChatKey(held.agent)) || []).map(
    (one, index) => attachmentChip(held.agent, one, index)));
  card.append(form);
  makeTheChatCardDraggable(card, grip, held);
  // The first card render happens before its saved conversation identity is
  // hydrated. Apply the same lifecycle locks immediately; waiting for the
  // request's finally block leaves Start again briefly enabled under legacy ID.
  setWhatCanBePressedInAChat(card);
  return card;
}

// The chat boxes move like the other boxes, and where they sit is remembered
// for as long as the window is open. Not written down with the board: which
// chats you have open is about this window and this minute.
function makeTheChatCardDraggable(card, grip, held) {
  let dragging = null;
  grip.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    dragging = {
      x: event.clientX, y: event.clientY,
      left: card.offsetLeft, top: card.offsetTop,
    };
    grip.setPointerCapture(event.pointerId);
  });
  grip.addEventListener("pointermove", (event) => {
    if (!dragging) return;
    held.at = {
      x: Math.max(0, Math.min(4000, Math.round(
        dragging.left + (event.clientX - dragging.x) / swarmZoom))),
      y: Math.max(0, Math.min(4000, Math.round(
        dragging.top + (event.clientY - dragging.y) / swarmZoom))),
    };
    card.style.left = `${held.at.x}px`;
    card.style.top = `${held.at.y}px`;
    drawSwarmLines();
  });
  const letGo = (event) => {
    if (!dragging) return;
    dragging = null;
    if (grip.hasPointerCapture(event.pointerId)) grip.releasePointerCapture(event.pointerId);
  };
  grip.addEventListener("pointerup", letGo);
  grip.addEventListener("pointercancel", letGo);
  grip.addEventListener("keydown", (event) => {
    const which = {
      ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1],
    }[event.key];
    if (!which) return;
    event.preventDefault();
    const step = event.shiftKey ? 4 : 20;
    held.at = {
      x: Math.max(0, Math.min(4000, card.offsetLeft + which[0] * step)),
      y: Math.max(0, Math.min(4000, card.offsetTop + which[1] * step)),
    };
    card.style.left = `${held.at.x}px`;
    card.style.top = `${held.at.y}px`;
    drawSwarmLines();
  });
}

function sayInTheChatFor(agentId, words) {
  const held = swarmChats.find((one) => one.agent === agentId);
  if (held) {
    held.notice = String(words || "");
    held.noticeFor = transcriptIdentityFor(agentId);
  }
  const card = theChatCardFor(agentId);
  if (card) card.querySelector(".swarm-chat-said").textContent = String(words || "");
}

function bigChatShows(agentId, conversationId = undefined) {
  return theBigOne === agentId && (
    conversationId === undefined || activeConversationIdFor(agentId) === conversationId
  );
}

function sayInBigChatConversationFor(agentId, words, conversationId = undefined) {
  if (!bigChatShows(agentId, conversationId)) return false;
  $("theBigChatConversationSaid").textContent = words;
  return true;
}

function sayInRuntimeChat(chatKey, words) {
  let shown = false;
  for (const held of swarmChats) {
    if (swarmChatRuntimeKey(held.agent) !== chatKey) continue;
    sayInTheChatFor(held.agent, words);
    if (theBigOne === held.agent) $("theBigChatSaidBack").textContent = words;
    shown = true;
  }
  return shown;
}

function setWhatCanBePressedInAChat(card) {
  const agent = theSwarmAgent(card.dataset.agent);
  const busy = swarmChatIsBusy(card.dataset.agent);
  const identityChanging = swarmChatIsResetting(card.dataset.agent)
    || swarmConversationSwitching.has(card.dataset.agent)
    || swarmChatIsHydrating(card.dataset.agent);
  const waiting = busy || identityChanging;
  const lone = isLoneAgentChat(card.dataset.agent);
  const conversation = activeConversationFor(card.dataset.agent);
  const bindingProblem = conversation?.binding_problem;
  const bindingWords = String(bindingProblem?.message || "");
  const recipientWords = syncChatRecipientWords(card.dataset.agent, card);
  const unavailablePeers = syncChatTeamReadiness(card.dataset.agent, card);
  // A route can become ready after the chat is opened (for example after the
  // user signs in or chooses an assistant). Keep the draft editable while it
  // is not ready; only actions which contact the provider need to be held.
  // Disabling the textarea threw away a useful distinction between "cannot
  // send yet" and "cannot compose", and made restored draft state unusable.
  const box = card.querySelector(".swarm-chat-box");
  box.disabled = !agent || identityChanging;
  box.title = identityChanging ? "Wait while Nexus opens the selected saved chat." : "";
  card.querySelector(".swarm-chat-send").disabled =
    waiting || !agent || !agent.ready || Boolean(bindingProblem);
  card.querySelector(".swarm-chat-send").title = bindingWords
    || `${recipientWords.direct}. No connected peer receives this request.`;
  const stop = card.querySelector(".swarm-chat-stop");
  stop.disabled = !busy || swarmChatIsStopping(card.dataset.agent);
  stop.textContent = swarmChatIsStopping(card.dataset.agent) ? "Stopping…" : "Stop";
  card.querySelector(".swarm-chat-attach").disabled =
    waiting || !agent || !agent.ready || Boolean(bindingProblem);
  card.querySelector(".swarm-chat-collaborate").disabled =
    waiting || lone || !agent || !agent.ready || Boolean(bindingProblem)
    || unavailablePeers.length > 0;
  card.querySelector(".swarm-chat-collaborate").title =
    bindingWords || (lone
      ? loneAgentActionMessage("collaborate")
      : unavailablePeers.length
      ? `Repair ${unavailablePeers.map((one) => one.name || one.id).join(", ")} before asking the team.`
      : `${recipientWords.team}. Expected initial replies: ${recipientWords.expected}.`);
  syncChatRoundPolicy(card.dataset.agent);
  const recoveryAuthorityWords = !directLongGoalRecoveryInventoryReady
    ? (directLongGoalRecoveryError
      || "Checking the saved goal-request journals before enabling project work")
    : "";
  const workDisabled = waiting || lone || !agent || !agent.ready
    || !directLongGoalRecoveryInventoryReady
    || Boolean(bindingProblem)
    || Boolean(workRecoveryFor(card.dataset.agent))
    || (Boolean(conversation) && !conversation.project);
  const workTitle = bindingWords || recoveryAuthorityWords || (lone
    ? loneAgentActionMessage("work")
    : workRecoveryFor(card.dataset.agent)
    ? "Resume the saved project-work run before starting another"
    : "Start durable project work with a required contribution from every ready agent in this chat");
  setSwarmProjectWorkControl(
    card.querySelector(".swarm-chat-work"), workDisabled, workTitle, card.dataset.agent,
  );
  const startAgain = card.querySelector(".swarm-chat-again");
  startAgain.disabled = waiting || !agent || Boolean(bindingProblem);
  startAgain.title = identityChanging
    ? "Wait while Nexus opens the selected saved chat."
    : bindingWords;
  renderWorkRecoveryButtons(card.dataset.agent);
}

function stoppedChatError(error) {
  return String(error?.message || error || "").includes("Stopped by you");
}

async function stopChatFor(agentId) {
  const chatKey = swarmChatRuntimeKey(agentId);
  if (!swarmBusy.has(chatKey) || swarmStopping.has(chatKey)) return;
  const activity = swarmChatActivity.get(chatKey);
  if (!activity) return;
  swarmStopping.add(chatKey);
  markSwarmChatActivityStopping(agentId, activity);
  sayInRuntimeChat(chatKey, "Stopping this chat...");
  setWhatCanBePressedInSwarm();
  try {
    const jobs = [request("/api/swarm/stop-chat", {
      method: "POST", body: JSON.stringify({
        agent: agentId, activity: activity.id, chat: activity.chatId,
      }),
    })];
    if (activity.route.startsWith("web:") && window.harnessDesktop?.stopWebChat) {
      jobs.push(window.harnessDesktop.stopWebChat(
        activity.route, activity.filedAs));
    }
    const [serverResult] = await Promise.allSettled(jobs);
    if (serverResult.status === "rejected") throw serverResult.reason;
    if (!serverResult.value?.stopped) {
      swarmStopping.delete(chatKey);
      setWhatCanBePressedInSwarm();
      sayInRuntimeChat(
        chatKey, serverResult.value?.note || "This chat is not waiting for an answer.",
      );
    }
  } catch (error) {
    swarmStopping.delete(chatKey);
    setWhatCanBePressedInSwarm();
    showError(String(error?.message || error));
  }
}

async function refreshTheChatFor(agentId) {
  const agent = theSwarmAgent(agentId);
  if (!agent) return;
  const controller = beginConversationRead(swarmConversationTranscriptControllers, agentId);
  const conversation = activeConversationFor(agentId);
  const conversationId = conversation?.id || "";
  const revisionKey = swarmChatKey(agentId);
  const revision = nextSwarmChatRevision(agentId);
  try {
    const said = await request(
      `/api/swarm/said?agent=${encodeURIComponent(agentId)}`
      + (conversation ? `&chat=${encodeURIComponent(conversation.id)}` : ""),
      {signal: controller.signal},
    );
    if (controller.signal.aborted || activeConversationIdFor(agentId) !== conversationId
        || swarmChatRevisions.get(agentId) !== revision
        || swarmChatRevisions.get(revisionKey) !== revision) return;
    if (said.limits && typeof said.limits === "object") {
      swarmChatLimits.set(String(agentId), said.limits);
    }
    keepWhatWasSaidTo(agentId, said.said || [], conversationId);
    countWhatIsTypedTo(agentId);
  } catch (error) {
    if (conversationReadWasCancelled(error)) return;
    if (activeConversationIdFor(agentId) !== conversationId
        || swarmChatRevisions.get(agentId) !== revision
        || swarmChatRevisions.get(revisionKey) !== revision) return;
    sayInTheChatFor(agentId, error.message);
  } finally {
    finishConversationRead(swarmConversationTranscriptControllers, agentId, controller);
  }
}

async function copyChatCode(button, code) {
  const before = button.textContent;
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(code);
    } else {
      const held = make("textarea", "sr-only");
      held.value = code;
      held.setAttribute("readonly", "");
      document.body.append(held);
      held.select();
      if (!document.execCommand("copy")) throw new Error("Copy was refused");
      held.remove();
    }
    button.textContent = "Copied";
  } catch (_) {
    button.textContent = "Select code";
    const selection = window.getSelection();
    const codeNode = button.closest(".chat-code-block")?.querySelector("code");
    if (selection && codeNode) {
      const range = document.createRange();
      range.selectNodeContents(codeNode);
      selection.removeAllRanges();
      selection.addRange(range);
    }
  }
  window.setTimeout(() => { button.textContent = before; }, 1800);
}

function appendChatText(container, text) {
  const value = String(text || "");
  const fences = /```([^\r\n`]*)\r?\n([\s\S]*?)```/g;
  let after = 0;
  let found;
  const plain = (words) => {
    if (words) container.append(make("p", "chat-prose", words));
  };
  while ((found = fences.exec(value)) !== null) {
    plain(value.slice(after, found.index));
    const language = found[1].trim() || "Plain text";
    const code = found[2];
    const block = make("section", "chat-code-block");
    const bar = make("div", "chat-code-bar");
    bar.append(make("span", "chat-code-language", language));
    const copy = make("button", "chat-code-copy", "Copy code");
    copy.type = "button";
    copy.title = `Copy this ${language} code block`;
    copy.addEventListener("click", () => copyChatCode(copy, code));
    bar.append(copy);
    const pre = make("pre", "chat-code");
    pre.append(make("code", "", code));
    block.append(bar, pre);
    container.append(block);
    after = fences.lastIndex;
  }
  plain(value.slice(after));
}

const chatPhaseNames = {
  user_prompt: "Sent to the team",
  long_horizon_prompt: "Saved project goal request",
  agent_reply: "Connected-agent reply",
  lead_draft: "Lead agent's first reply",
  agent_plan: "Connected-agent plan",
  lead_plan: "Lead agent's plan",
  agent_discussion: "Team discussion",
  agent_plan_review: "Plan review",
  lead_execution: "Provisional execution pass",
  agent_execution: "Connected-agent provisional execution",
  agent_verification: "Work verification",
  final_answer: "Final answer",
  nexus_error: "Nexus failure",
  participant_outcome: "Team response status",
  long_horizon_status: "Project goal status",
};

function chatPhaseName(phase) { return chatPhaseNames[phase] || ""; }

function normalizedLongHorizonCorrelation(one) {
  const raw = one?.correlation;
  if (!raw || typeof raw !== "object" || Number(raw.schema_version) !== 1
      || String(raw.kind || "") !== "long_horizon_status"
      || !String(raw.goal_id || "")) return null;
  return {
    goalId: String(raw.goal_id),
    status: String(raw.goal_status || "unknown"),
  };
}

function appendLongHorizonGoalLink(container, correlation) {
  if (!correlation?.goalId) return;
  const open = make("button", "compact long-horizon-chat-link",
    correlation.status === "complete" ? "Open verified goal" : "Open goal in Mission control");
  open.type = "button";
  open.dataset.goalId = correlation.goalId;
  open.dataset.goalStatus = correlation.status;
  open.addEventListener("click", async () => {
    localStorage.setItem(LONG_GOAL_SELECTED_KEY, correlation.goalId);
    await refreshLongGoals(true);
    $("missionControl")?.scrollIntoView({behavior: "smooth", block: "start"});
  });
  container.append(open);
}

const PARTICIPANT_OUTCOME_STATUSES = new Set([
  "answered", "failed", "outcome_unknown",
  "answered_then_failed", "answered_then_outcome_unknown",
]);

function normalizedParticipantOutcome(one) {
  let raw = one?.participant_outcome;
  if ((!raw || typeof raw !== "object") && one?.phase === "participant_outcome") raw = one;
  if (!raw || typeof raw !== "object" || Number(raw.schema_version) !== 1) return null;
  const participants = (Array.isArray(raw.participants) ? raw.participants : [])
    .slice(0, 32).map((participant) => {
      const status = String(participant?.status || "failed");
      return {
        agentId: String(participant?.agent_id || ""),
        name: String(participant?.name || "An agent"),
        route: String(participant?.route || ""),
        status: PARTICIPANT_OUTCOME_STATUSES.has(status) ? status : "failed",
        answerSaved: participant?.answer_saved === true,
        providerReason: String(participant?.provider_reason || ""),
        outcomeUnknown: participant?.outcome_unknown === true
          || status.includes("outcome_unknown"),
      };
    });
  const inferredAnswered = participants.filter((participant) => participant.answerSaved).length;
  const expected = Math.max(participants.length, Math.round(Number(raw.expected_count) || 0));
  const answered = Math.max(0, Math.min(expected,
    Math.round(Number(raw.answered_count ?? inferredAnswered) || 0)));
  const outcome = ["complete", "partial", "none"].includes(String(raw.outcome))
    ? String(raw.outcome) : answered >= expected && expected ? "complete"
      : answered ? "partial" : "none";
  const actions = (Array.isArray(raw.actions) ? raw.actions : []).slice(0, 32)
    .filter((action) => action && ["repair-provider", "inspect-provider-turn"]
      .includes(String(action.id || "")))
    .map((action) => ({
      id: String(action.id || ""),
      agentId: String(action.agent_id || ""),
      route: String(action.route || ""),
      label: String(action.label || ""),
    }));
  return {
    schemaVersion: 1,
    outcome,
    requestedMode: String(raw.requested_mode || "collaborate"),
    expected,
    answered,
    participants,
    actions,
  };
}

function participantOutcomeStatusWords(participant) {
  if (participant.status === "answered") return "Answer saved";
  if (participant.status === "answered_then_failed") {
    return "Answer saved; a later provider turn failed";
  }
  if (participant.status === "answered_then_outcome_unknown") {
    return "Answer saved; a later delivery is unknown — Nexus will not resend";
  }
  if (participant.outcomeUnknown) {
    return "Delivery unknown — Nexus will not resend";
  }
  return "Did not answer";
}

async function openParticipantRepairFlow(action, participant, button) {
  const agentId = String(action?.agentId || participant?.agentId || "");
  const route = String(action?.route || participant?.route || "");
  const current = theSwarmAgent(agentId);
  if (!current) {
    sayInSwarm(`${participant?.name || "That agent"} is no longer on this board. Nexus did not change another agent's connection.`);
    return;
  }
  if (route && String(current.who || "") !== route) {
    const message = (
      `${current.name}'s failed turn used route “${route}”, but the agent now uses “${current.who || "no route"}”. `
      + "Nexus did not diagnose or change the replacement route. Open this agent's current Settings to inspect it."
    );
    sayInSwarm(message);
    const item = button?.closest(".participant-outcome-participant");
    if (item) {
      const inline = make("p", "warning-one participant-outcome-route-changed", message);
      item.querySelector(".participant-outcome-route-changed")?.remove();
      item.append(inline);
      inline.scrollIntoView({behavior: "smooth", block: "nearest"});
    }
    return;
  }
  await openAgentRepairFlow(agentId, button);
}

function restoreParticipantOutcomePrompt(agentId, prompt, row, note) {
  const words = String(prompt || "").trim();
  if (!words) {
    note.textContent = "The original prompt is not available in this transcript view. Nothing was sent.";
    return;
  }
  const inBigChat = Boolean(row.closest("#theBigChat"));
  const box = inBigChat ? $("theBigChatBox")
    : row.closest(".swarm-chat-card")?.querySelector(".swarm-chat-box");
  if (!box) {
    note.textContent = "Open this saved chat before restoring its prompt. Nothing was sent.";
    return;
  }
  if (box.value.trim() && box.value !== words && !window.confirm(
    "Replace the draft already in this composer with the original team prompt?\n\nNothing will be sent automatically."
  )) {
    note.textContent = "The existing draft was kept. Nothing was sent.";
    return;
  }
  box.value = words;
  box.dispatchEvent(new Event("input", {bubbles: true}));
  box.focus();
  box.setSelectionRange(words.length, words.length);
  note.textContent = `Prompt restored. Review it, then press ${chatRecipientWords(agentId).team}. Nothing was sent.`;
}

function appendParticipantOutcome(container, outcome, agent, originalPrompt, row) {
  if (!outcome) return;
  row.classList.add("participant-outcome-turn");
  const card = make("section", "participant-outcome-card");
  card.dataset.outcome = outcome.outcome;
  card.setAttribute("aria-label", "Team response status");
  const summary = outcome.outcome === "complete"
    ? `${outcome.answered} of ${outcome.expected} agents answered.`
    : outcome.outcome === "partial"
      ? `${outcome.answered} of ${outcome.expected} agents answered. Available replies are saved.`
      : `0 of ${outcome.expected} agents answered. No AI answer was saved.`;
  card.append(make("h4", "participant-outcome-title", summary));
  const list = make("ul", "participant-outcome-list");
  for (const participant of outcome.participants) {
    const item = make("li", "participant-outcome-participant");
    item.dataset.status = participant.status;
    const head = make("div", "participant-outcome-participant-head");
    head.append(make("strong", "", participant.name));
    head.append(make("span", "participant-outcome-status",
      participantOutcomeStatusWords(participant)));
    item.append(head);
    if (participant.providerReason) {
      item.append(make("p", "hint participant-outcome-reason", participant.providerReason));
    }
    const offered = outcome.actions.filter((action) => action.agentId === participant.agentId);
    for (const action of offered) {
      const repair = make("button", "swarm-repair participant-outcome-repair",
        `${action.id === "inspect-provider-turn" ? "Inspect" : "Repair"} ${participant.name}`);
      repair.type = "button";
      repair.title = action.id === "inspect-provider-turn"
        ? `Inspect ${participant.name}'s exact provider conversation before any retry`
        : `Diagnose ${participant.name}'s exact provider route${action.route ? `: ${action.route}` : ""}`;
      repair.addEventListener("click", () => (
        openParticipantRepairFlow(action, participant, repair)
      ));
      item.append(repair);
    }
    list.append(item);
  }
  card.append(list);
  const hasUnknown = outcome.participants.some((participant) => participant.outcomeUnknown);
  const hasKnownFailure = outcome.participants.some((participant) => (
    participant.status === "failed" || participant.status === "answered_then_failed"
  ));
  if (hasUnknown) {
    card.append(make("p", "participant-outcome-safety",
      "At least one delivery is unknown. Nexus will not resend this team request; use that agent's Repair action to inspect the provider conversation first."));
  }
  const note = make("p", "hint participant-outcome-action-note");
  if (hasKnownFailure && !hasUnknown) {
    const again = make("button", "participant-outcome-ask-again", "Ask all agents again");
    again.type = "button";
    again.title = "Restore the original prompt for review. This button does not send it.";
    again.addEventListener("click", () => (
      restoreParticipantOutcomePrompt(agent.id, originalPrompt, row, note)
    ));
    card.append(again);
    note.textContent = "Restores the original prompt into this exact composer for review; it never sends automatically.";
  }
  if (note.textContent) card.append(note);
  container.append(card);
}

function chatTurnSpeaker(one, agent) {
  const from = one.speaker_name
    || (one.who === "you" ? "You" : ((agent && agent.name) || "The assistant"));
  return one.recipient_name ? `${from} → ${one.recipient_name}` : from;
}

let userQuestionRenderId = 0;

function appendInlineUserQuestions(row, agent, rawQuestions, alreadyAnswered) {
  const questions = normalizedUserQuestions(rawQuestions);
  if (!questions.length) return;
  const answers = {};
  const card = make("section", "agent-question-card");
  card.append(make("h4", "", alreadyAnswered ? "Answered" : "Waiting for your answer"));
  const list = make("div", "agent-question-list");
  const namespace = `inline-${++userQuestionRenderId}`;
  for (const question of questions) {
    list.append(userQuestionFields(
      question, {}, (answer) => { answers[question.id] = answer; }, namespace,
    ));
  }
  card.append(list);
  const status = make("p", "hint agent-question-status",
    alreadyAnswered ? "Your next message answered this request." : "The agent will continue in this exact conversation.");
  card.append(status);
  const submit = make("button", "primary agent-question-submit", "Submit answers");
  submit.type = "button";
  submit.disabled = alreadyAnswered;
  submit.addEventListener("click", () => {
    const compiled = compiledQuestionAnswers(questions, answers);
    if (compiled.missing.length) {
      status.textContent = `Answer ${compiled.missing.length === 1 ? "the question" : "all questions"} first.`;
      return;
    }
    const inBigChat = Boolean(row.closest("#theBigChat"));
    const box = inBigChat ? $("theBigChatBox")
      : row.closest(".swarm-chat-card")?.querySelector(".swarm-chat-box");
    if (!box || !agent?.id) return;
    box.value = compiled.text;
    box.dispatchEvent(new Event("input", {bubbles: true}));
    submit.disabled = true;
    status.textContent = "Sending your answers…";
    if (inBigChat) void sendFromTheBigChat("chat");
    else void sendWhatIsTypedTo(agent.id, "chat");
  });
  card.append(submit);
  if (alreadyAnswered) {
    for (const control of card.querySelectorAll("input, textarea")) control.disabled = true;
  }
  row.append(card);
}

function putTheChatTurnsIn(list, agent, said, scroll = true) {
  list.replaceChildren();
  if (!said.length) {
    list.append(make("li", "hint",
      "Nothing said yet. Whatever you type stays on this machine, and goes only to "
      + "this agent's assistant."));
    return;
  }
  let latestUserPrompt = "";
  for (const [turnIndex, one] of said.entries()) {
    if (one.who === "you" && String(one.text || "").trim()) latestUserPrompt = one.text;
    const participantOutcome = normalizedParticipantOutcome(one);
    const collaboration = ["agent_reply", "lead_draft", "agent_plan", "lead_plan",
      "agent_discussion", "agent_plan_review", "lead_execution", "agent_execution", "agent_verification"]
      .includes(one.phase);
    const row = make("li", `talk-turn ${one.who} ${collaboration ? "between" : ""}`);
    row.classList.toggle("nexus-turn", isNexusChatTurn(one));
    const speaker = agentForChatTurn(one, agent);
    if (one.who !== "you") styleForAgent(row, speaker);
    const heading = make("div", "talk-turn-heading");
    if (one.who !== "you") heading.append(
      aChatTurnFace(one, speaker, "talk-turn-face", 16));
    heading.append(make("strong", "talk-turn-who", chatTurnSpeaker(one, agent)));
    const phase = chatPhaseName(one.phase);
    if (phase) heading.append(make("span", `chat-turn-phase phase-${one.phase}`, phase));
    row.append(heading);
    const text = make("div", "talk-turn-text");
    if (participantOutcome) {
      appendParticipantOutcome(text, participantOutcome, agent, latestUserPrompt, row);
    } else {
      appendChatText(text, one.text);
    }
    row.append(text);
    appendLongHorizonGoalLink(row, normalizedLongHorizonCorrelation(one));
    if (one.structured_state_unavailable) {
      row.append(make("p", "hint chat-turn-protocol-warning",
        "Reply kept exactly as delivered; completion and progress could not be verified."));
    }
    appendInlineUserQuestions(
      row, agent, one.questions,
      said.slice(turnIndex + 1).some((later) => later.who === "you"),
    );
    if (Array.isArray(one.attachments) && one.attachments.length) {
      const files = make("div", "talk-attachments");
      for (const attached of one.attachments) {
        files.append(make("span", "talk-attachment",
          `${attached.image ? "Screenshot" : "File"}: ${attached.name}`));
      }
      row.append(files);
    }
    const under = [];
    if (one.at) under.push(one.at);
    if (one.milliseconds) under.push(prettyTime(one.milliseconds));
    if (one.speaker_route) under.push(`route ${one.speaker_route}`);
    if (one.model) under.push(one.model);
    if (under.length) row.append(make("p", "hint", under.join(" | ")));
    list.append(row);
  }
  if (scroll) list.lastElementChild.scrollIntoView({block: "nearest"});
}

function renderTheChatThreadFor(agentId, said) {
  const card = theChatCardFor(agentId);
  if (!card) return;
  putTheChatTurnsIn(card.querySelector(".swarm-chat-thread"), theSwarmAgent(agentId),
    chatTurnsWhileWorking(agentId, said));
}

function keepWhatWasSaidTo(agentId, said, conversationId = activeConversationIdFor(agentId)) {
  const held = swarmChats.find((one) => one.agent === agentId);
  // Network answers and reads carry the conversation they were requested for.
  // If the user has moved elsewhere, save/display belongs to that old request,
  // never to the conversation now named by the header.
  if (!held || activeConversationIdFor(agentId) !== conversationId) return false;
  // This write may be an answer landing after a read began. Move the revision
  // on again so that read cannot arrive afterwards and put the old turns back.
  nextSwarmChatRevision(agentId);
  held.said = Array.isArray(said) ? said : [];
  held.saidFor = conversationId || "legacy";
  renderTheChatThreadFor(agentId, held.said);
  if (theBigOne === agentId) renderTheBigChat();
  return true;
}

function keepWhatWasSaidToRuntime(
  chatKey, said, conversationId,
) {
  let kept = false;
  for (const held of swarmChats) {
    if (swarmChatRuntimeKey(held.agent) !== chatKey) continue;
    kept = keepWhatWasSaidTo(held.agent, said, conversationId) || kept;
  }
  return kept;
}

function countWhatIsTypedTo(agentId) {
  const card = theChatCardFor(agentId);
  if (!card) return;
  const typed = card.querySelector(".swarm-chat-box").value.length;
  const limits = limitsForSwarmChat(agentId);
  const limit = Number(limits.input_characters || 200000);
  card.querySelector(".swarm-chat-count").textContent = typed > limit
    ? `${(typed - limit).toLocaleString()} over ${limit.toLocaleString()} — not truncated`
    : `${typed.toLocaleString()} / ${limit.toLocaleString()} characters`;
  if (theBigOne === agentId) countWhatIsTypedInBigChat();
}

function countWhatIsTypedInBigChat() {
  const box = $("theBigChatBox");
  const counter = $("theBigChatCount");
  if (!box || !counter) return;
  const typed = box.value.length;
  const limits = limitsForSwarmChat(theBigOne);
  const limit = Number(limits.input_characters || 200000);
  const outputFact = outputBudgetFact(limits);
  counter.textContent = typed > limit
    ? `${(typed - limit).toLocaleString()} characters over ${limit.toLocaleString()} — Nexus will not truncate or send it`
    : `${typed.toLocaleString()} / ${limit.toLocaleString()} characters · answer hard cap ${Number(limits.answer_characters || 8000000).toLocaleString()} characters · ${outputFact} · ${captureBudgetFact(limits)}`
      + (longHorizonContextFact(limits) ? ` · ${longHorizonContextFact(limits)}` : "");
}

function looksLikeProjectWork(words) {
  const action = "(?:add|build|change|create|delete|edit|fix|implement|make|modify|move|refactor|remove|rename|replace|update|write)";
  const target = "(?:code|files?|folders?|projects?|repositor(?:y|ies)|repos?|scripts?|tests?)";
  return new RegExp(`\\b${action}\\b[\\s\\S]{0,100}\\b${target}\\b|\\b${target}\\b[\\s\\S]{0,100}\\b${action}\\b`, "i")
    .test(String(words || ""));
}

function automaticRoundStopWords(answered) {
  if (answered?.stopped_because === "stalled"
      || answered?.stopped_because === "plan_stalled") {
    return "Nexus stopped a repeated no-progress cycle. The saved transcript names what remains.";
  }
  if (answered?.stopped_because === "round_limit"
      || answered?.stopped_because === "plan_round_limit") {
    return `Stopped at your ${answered.round_limit}-round limit. The saved transcript names what remains.`;
  }
  return "";
}

function selectedProjectWorkPause(agentId) {
  const conversation = activeConversationFor(agentId);
  const unavailable = (conversation?.pair_agents || []).filter((one) => !one.ready);
  if (unavailable.length) {
    return `Connected agent${unavailable.length === 1 ? "" : "s"} `
      + `${unavailable.map((one) => one.name || one.id).join(", ")} `
      + `${unavailable.length === 1 ? "is" : "are"} not ready for this project. `
      + "Repair the named provider connection before starting paired file work.";
  }
  const authority = conversation?.work_authority;
  return authority && authority.can_run === false ? String(authority.reason || "") : "";
}

function swarmProjectWorkPauseMessage(agentId = theBigOne) {
  return executionPauseWords("Project work", selectedProjectWorkPause(agentId));
}

function setSwarmProjectWorkControl(
  button, ordinarilyDisabled, ordinaryTitle = "", agentId = theBigOne,
) {
  const reason = selectedProjectWorkPause(agentId);
  const status = button?.id === "theBigChatWork"
    ? $("theBigChatWorkStatus")
    : button?.closest(".swarm-chat-card")?.querySelector(".swarm-chat-work-status");
  let describedBy = "";
  if (status) {
    if (!status.id) {
      status.id = `swarm-work-status-${String(agentId || "agent").replace(/[^a-zA-Z0-9_-]/g, "-")}`;
    }
    status.textContent = reason ? executionPauseWords("Project work", reason) : "";
    status.hidden = !reason;
    describedBy = reason ? status.id : "";
  }
  setExecutionControl(button, ordinarilyDisabled, reason, ordinaryTitle,
    "Project work", describedBy);
}

function projectWorkPauseForMessage(mode, words, agentId = theBigOne) {
  if (!(mode === "work" || (mode === "auto" && looksLikeProjectWork(words)))) {
    return "";
  }
  if (!directLongGoalRecoveryInventoryReady || directLongGoalRecoveryError) {
    return directLongGoalRecoveryError
      ? "Nexus cannot verify the saved goal-request journals yet. Repair or restart Nexus before starting any new project work; no request was sent."
      : "Nexus is still checking the saved goal-request journals. Wait for that exact recovery check before starting project work; no request was sent.";
  }
  return swarmProjectWorkPauseMessage(agentId);
}

function directLongGoalCanonicalValue(value) {
  if (Array.isArray(value)) return value.map(directLongGoalCanonicalValue);
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.keys(value).sort().map(
      (key) => [key, directLongGoalCanonicalValue(value[key])],
    ));
  }
  return value;
}

async function directLongGoalIntent(conversation, agentId, text, attachments) {
  const canonical = JSON.stringify(directLongGoalCanonicalValue({
    schema_version: 1,
    project_id: String(conversation?.project || ""),
    chat_id: String(conversation?.id || ""),
    lead_id: String(agentId || ""),
    text: String(text || ""),
    attachments: attachments || [],
  }));
  if (globalThis.crypto?.subtle && globalThis.TextEncoder) {
    const digest = await globalThis.crypto.subtle.digest(
      "SHA-256", new TextEncoder().encode(canonical),
    );
    return [...new Uint8Array(digest)]
      .map((one) => one.toString(16).padStart(2, "0")).join("");
  }
  throw new Error(
    "This renderer cannot compute the required SHA-256 goal-request identity. Restart or update Nexus before starting project work.",
  );
}

async function prepareDirectLongGoalAdmission(
  exactPayload, directRequestId, directIntent, directRequestKey,
) {
  const desktopOutbox = canUseDirectLongGoalDesktopOutbox();
  let outbox = null;
  if (desktopOutbox) {
    outbox = await saveDirectLongGoalOutbox({
      schema_version: 1,
      chat_id: exactPayload.chat_id,
      request_id: directRequestId,
      intent: directIntent,
      payload: exactPayload,
    });
    if (String(outbox?.payload_sha256 || "") !== directIntent) {
      throw new Error("The durable desktop outbox saved a different exact request digest.");
    }
    localStorage.setItem(directRequestKey, JSON.stringify({
      schema_version: 3, id: directRequestId, intent: directIntent,
      chat_id: exactPayload.chat_id, project_id: exactPayload.project_id,
      lead_id: exactPayload.lead_id,
      prepared: false,
    }));
  }
  const expected = {
    request_id: directRequestId,
    chat_id: exactPayload.chat_id,
    project_id: exactPayload.project_id,
    lead_id: exactPayload.lead_id,
    intent_sha256: directIntent,
  };
  let prepared;
  try {
    prepared = await request("/api/long-horizon/prepare-admission", {
      method: "POST", body: JSON.stringify({
        ...exactPayload, request_id: directRequestId,
      }),
    });
  } catch (prepareError) {
    // A prior reconciliation may have committed while its response was lost.
    // Only an authenticated, exact canonical goal may turn that retired
    // request into success; otherwise preserve the original prepare failure.
    const reconciled = await reconcileExistingDirectLongGoalAdmission(expected);
    if (!reconciled) throw prepareError;
    return {desktopOutbox, outbox, pending: null, started: reconciled};
  }
  const pending = verifiedDirectLongGoalRecoveryRows(
    [prepared?.pending], "The backend recovery journal",
  )[0];
  for (const [field, expected] of Object.entries({
    request_id: directRequestId,
    chat_id: exactPayload.chat_id,
    project_id: exactPayload.project_id,
    lead_id: exactPayload.lead_id,
  })) {
    if (String(pending?.[field] || "") !== String(expected || "")) {
      throw new Error(
        `The backend admission journal saved a different exact ${field.replaceAll("_", " ")}. Nexus did not start it.`,
      );
    }
  }
  if (String(pending?.intent_sha256 || "") !== directIntent) {
    throw new Error(
      "The backend admission journal saved a different exact request digest. Nexus did not start it.",
    );
  }
  // In a normal browser window this authenticated backend record is the first
  // durable boundary. It cannot dispatch a provider. Only after its exact
  // receipt is verified may origin-local state or the visible draft change.
  localStorage.setItem(directRequestKey, JSON.stringify({
    schema_version: 3, id: directRequestId, intent: directIntent,
    chat_id: exactPayload.chat_id, project_id: exactPayload.project_id,
    lead_id: exactPayload.lead_id,
    prepared: true, payload_sha256: pending.payload_sha256,
  }));
  return {desktopOutbox, outbox, pending, started: null};
}

function directLongGoalRequestId(durableRequest, directIntent, exactBinding) {
  if (durableRequest === null) {
    return globalThis.crypto?.randomUUID
      ? globalThis.crypto.randomUUID()
      : `long-goal-${Date.now()}-${Math.random().toString(36).slice(2)}`;
  }
  const exactMarker = durableRequest
    && typeof durableRequest === "object" && !Array.isArray(durableRequest)
    && [2, 3].includes(durableRequest.schema_version)
    && typeof durableRequest.id === "string" && durableRequest.id
    && durableRequest.id.length <= 160
    && durableRequest.intent === directIntent
    && DIRECT_LONG_GOAL_RECOVERY_SHA256.test(String(durableRequest.intent || ""))
    && typeof durableRequest.prepared === "boolean"
    && (!durableRequest.prepared
      || DIRECT_LONG_GOAL_RECOVERY_SHA256.test(
        String(durableRequest.payload_sha256 || ""),
      ))
    && (durableRequest.schema_version !== 3 || (
      durableRequest.chat_id === exactBinding?.chat_id
      && durableRequest.project_id === exactBinding?.project_id
      && durableRequest.lead_id === exactBinding?.lead_id
    ));
  // A prepared marker can remain after a successful reconciliation response
  // was lost or another origin settled it. Reuse its id once: prepare either
  // finds the still-pending journal or the exact-goal fallback above retries
  // reconciliation. Only verified success clears the marker, so it can never
  // silently turn into a duplicate provider dispatch.
  if (exactMarker) return durableRequest.id;
  throw new Error(
    "The saved browser goal-request marker could not be verified. No new project work was sent.",
  );
}

async function exactExistingDirectLongGoal(expected) {
  const lookedUp = await request("/api/long-horizon/admission-goal", {
    method: "POST", body: JSON.stringify({
      request_id: expected.request_id,
      chat_id: expected.chat_id,
      project_id: expected.project_id,
      lead_id: expected.lead_id,
      intent_sha256: expected.intent_sha256,
    }),
  });
  const identity = verifiedDirectLongGoalReceiptIdentity(
    lookedUp, expected, "The exact durable-goal lookup",
  );
  if (lookedUp.schema_version !== DIRECT_LONG_GOAL_RECEIPT_SCHEMA_VERSION
      || typeof lookedUp.found !== "boolean") {
    throw new Error("The exact durable-goal lookup returned an unsupported receipt.");
  }
  if (!lookedUp.found) {
    if (lookedUp.goal !== undefined && lookedUp.goal !== null) {
      throw new Error("The exact durable-goal lookup returned a goal while claiming absence.");
    }
    return null;
  }
  return verifiedDirectLongGoalReceiptGoal(
    lookedUp.goal, identity, "The exact durable-goal lookup",
  );
}

async function reconcileDirectLongGoalAdmission(
  expected, payloadSha256 = "", exactGoalId = "",
) {
  if (payloadSha256
      && !DIRECT_LONG_GOAL_RECOVERY_SHA256.test(String(payloadSha256))) {
    throw new Error("Nexus has no exact backend payload digest for goal reconciliation.");
  }
  const reconciled = verifiedDirectLongGoalDiscardReceipt(
    await request("/api/long-horizon/discard-admission", {
      method: "POST", body: JSON.stringify({
        request_id: expected.request_id,
        chat_id: expected.chat_id,
        project_id: expected.project_id,
        lead_id: expected.lead_id,
        payload_sha256: payloadSha256,
        intent_sha256: expected.intent_sha256,
      }),
    }),
    expected,
  );
  if (!reconciled.reconciled || reconciled.discarded
      || reconciled.safe_to_delete
      || (exactGoalId && reconciled.goal.goal_id !== exactGoalId)) {
    throw new Error(
      "The backend did not reconcile the exact canonical durable goal. The saved request was kept visible.",
    );
  }
  return reconciled;
}

async function acknowledgeDirectLongGoalTerminal(
  expected, terminalState, exactGoalId = "",
) {
  const acknowledged = await request(
    "/api/long-horizon/acknowledge-admission", {
      method: "POST", body: JSON.stringify({
        request_id: expected.request_id,
        chat_id: expected.chat_id,
        project_id: expected.project_id,
        lead_id: expected.lead_id,
        intent_sha256: expected.intent_sha256,
        terminal_state: terminalState,
        goal_id: exactGoalId,
      }),
    },
  );
  const identity = verifiedDirectLongGoalReceiptIdentity(
    acknowledged, expected, "The terminal goal acknowledgement",
  );
  if (acknowledged.schema_version !== DIRECT_LONG_GOAL_RECEIPT_SCHEMA_VERSION
      || acknowledged.client_consumed !== true
      || acknowledged.terminal_state !== terminalState
      || String(acknowledged.goal_id || "") !== String(exactGoalId || "")) {
    throw new Error(
      "The backend did not acknowledge the exact terminal goal receipt.",
    );
  }
  if (terminalState === "reconciled") {
    const goal = verifiedDirectLongGoalReceiptGoal(
      acknowledged.goal, identity, "The terminal goal acknowledgement",
    );
    if (goal.goal_id !== exactGoalId) {
      throw new Error(
        "The terminal goal acknowledgement returned a different canonical goal.",
      );
    }
  } else if (terminalState !== "discarded"
      || (acknowledged.goal !== undefined && acknowledged.goal !== null)) {
    throw new Error("The terminal goal acknowledgement returned an invalid outcome.");
  }
  return acknowledged;
}

async function bestEffortAcknowledgeDirectLongGoalTerminal(
  expected, terminalState, exactGoalId = "",
) {
  try {
    await acknowledgeDirectLongGoalTerminal(
      expected, terminalState, exactGoalId,
    );
    return true;
  } catch (_) {
    // Local browser/desktop authorities have already been cleared after an
    // exact start/reconcile receipt. A lost final acknowledgement can only
    // leave the authenticated terminal fence visible on the next inventory;
    // it must never turn verified provider work back into a failed send.
    try { await refreshDirectLongGoalRecoveries(); } catch (_) { /* Retry is explicit. */ }
    return false;
  }
}

async function reconcileExistingDirectLongGoalAdmission(expected) {
  const existing = await exactExistingDirectLongGoal(expected);
  if (!existing) return null;
  const reconciled = await reconcileDirectLongGoalAdmission(
    expected, "", existing.goal_id,
  );
  return {
    schema_version: DIRECT_LONG_GOAL_RECEIPT_SCHEMA_VERSION,
    engine: "long_horizon",
    request_id: expected.request_id,
    chat_id: expected.chat_id,
    project_id: expected.project_id,
    lead_id: expected.lead_id,
    intent_sha256: expected.intent_sha256,
    goal: reconciled.goal,
    terminal_state: "reconciled",
  };
}

async function startAndReconcileDirectLongGoalAdmission(expected, payloadSha256) {
  if (!DIRECT_LONG_GOAL_RECOVERY_SHA256.test(String(payloadSha256 || ""))) {
    throw new Error(
      "Nexus has no exact backend payload digest for durable goal reconciliation.",
    );
  }
  // Never acknowledge a journal until the start response proves the exact
  // request/chat/project/lead/intent and its matching canonical goal.
  const started = verifiedDirectLongGoalStartReceipt(
    await request("/api/long-horizon/start", {
      method: "POST", body: JSON.stringify({
        request_id: expected.request_id,
        chat_id: expected.chat_id,
        from_pending: true,
      }),
    }),
    expected,
  );
  const reconciled = await reconcileDirectLongGoalAdmission(
    expected, payloadSha256, started.goal.goal_id,
  );
  return {...started, goal: reconciled.goal, terminal_state: "reconciled"};
}

function confirmProjectWork(agent, words, mode) {
  const needsConfirmation = mode === "work" || (mode === "auto" && looksLikeProjectWork(words));
  if (!needsConfirmation) return {allowed: true, confirmed: false};
  if (!directLongGoalRecoveryInventoryReady || directLongGoalRecoveryError) {
    const message = projectWorkPauseForMessage(mode, words, agent?.id);
    sayInTheChatFor(agent?.id, message);
    if (theBigOne === agent?.id) $("theBigChatSaidBack").textContent = message;
    return {allowed: false, confirmed: false};
  }
  if (workRecoveryFor(agent?.id)) {
    const message = "Resume the saved project-work run before starting another file task in this chat.";
    sayInTheChatFor(agent?.id, message);
    if (theBigOne === agent?.id) $("theBigChatSaidBack").textContent = message;
    renderWorkRecovery(agent?.id);
    return {allowed: false, confirmed: false};
  }
  if (directLongGoalRecoveryFor(agent?.id)) {
    const message = (
      "Reconcile the exact saved project-goal request in this chat before starting another one. "
      + "Nexus will not resend it automatically."
    );
    sayInTheChatFor(agent?.id, message);
    if (theBigOne === agent?.id) $("theBigChatSaidBack").textContent = message;
    renderWorkRecovery(agent?.id);
    return {allowed: false, confirmed: false};
  }
  // Marker discovery is deliberately repeated at confirmation time. If an
  // origin-local marker became unreadable after the last inventory refresh,
  // directLongGoalRecoveryFor fails the authority state closed; never let the
  // same click cross into a fresh admission.
  if (!directLongGoalRecoveryInventoryReady || directLongGoalRecoveryError) {
    const message = projectWorkPauseForMessage(mode, words, agent?.id);
    sayInTheChatFor(agent?.id, message);
    if (theBigOne === agent?.id) $("theBigChatSaidBack").textContent = message;
    return {allowed: false, confirmed: false};
  }
  const conversation = activeConversationFor(agent?.id);
  const project = (conversation?.projects || []).find(
    (one) => one.id === conversation?.project
  );
  const allowed = window.confirm(
    `Allow Nexus to apply file changes proposed by ${agent?.name || "this agent"}`
    + ` to ${project?.name || "its connected project"}?\n\n`
    + `Exact folder: ${project?.path || "No folder selected"}\n\n`
    + "Nexus will give every selected connected agent a concrete contribution task, apply validated changes, inspect evidence, and continue only as needed."
  );
  if (!allowed) {
    const message = "Project work was not started, and no files were changed.";
    sayInTheChatFor(agent?.id, message);
    if (theBigOne === agent?.id) $("theBigChatSaidBack").textContent = message;
  }
  return {allowed, confirmed: allowed};
}

function workResponseWords(answered, agentName = "The team", ordinaryWords = "") {
  const status = String(answered?.status || answered?.verification_status || "");
  const budget = answered?.context_tool_budget?.summary
    ? ` ${answered.context_tool_budget.summary}` : "";
  const participantOutcome = normalizedParticipantOutcome({
    participant_outcome: answered?.participant_outcome,
  });
  if (participantOutcome && participantOutcome.requestedMode !== "work") {
    if (participantOutcome.outcome === "complete") {
      return `${participantOutcome.answered} of ${participantOutcome.expected} agents answered.`;
    }
    if (participantOutcome.outcome === "partial") {
      return `${participantOutcome.answered} of ${participantOutcome.expected} agents answered. The available replies are saved; use the response-status card to repair the missing connection.`;
    }
    return `0 of ${participantOutcome.expected} agents answered. No AI answer was saved; use the response-status card to repair the connections.`;
  }
  if (status === "waiting_for_user") {
    return `${agentName} is waiting for your answer. Use the question card in the conversation.`;
  }
  if (status === "paused_for_user") {
    return "Project work paused safely. Answer the questions in the saved run card to continue." + budget;
  }
  if (status === "paused_provider") {
    return "A provider did not answer. Nexus saved the exact run; reconnect it and use Retry provider and resume. No user answer is required." + budget;
  }
  if (status === "paused_tool_budget") {
    return "The configured context-tool execution budget was used. Nexus saved the exact run; use Reset tool time and resume, or change Context tool execution seconds in Settings. Provider thinking and waiting did not spend this budget." + budget;
  }
  if (status === "incomplete") {
    return "The long-horizon goal is still incomplete. Nexus saved the exact run so the team can continue instead of starting over." + budget;
  }
  if (status === "needs_verification") {
    return "Changes were applied, but deterministic verification failed. Resume the saved run to verify or revise them." + budget;
  }
  if (status === "applied_unverified") {
    return "Changes were applied but are not deterministically verified. Resume the saved run before treating the work as complete." + budget;
  }
  if (ordinaryWords) return ordinaryWords;
  return answered.partial_provider_failure || automaticRoundStopWords(answered) || (answered?.changed?.length
    ? `${agentName} answered. Nexus applied ${answered.changed.length} file change(s).`
    : answered.routing?.requested === "auto" && answered.routing?.selected === "collaborate"
      ? `${agentName} answered after Nexus automatically involved ${answered.collaborated_with?.length || 0} connected agent(s).`
      : answered?.collaborated_with?.length
        ? `${agentName} answered after hearing ${answered.collaborated_with.length} connected agent(s).`
        : answered?.routing?.reason || `${agentName} answered.`);
}

async function resumeSwarmWork(agentId, resetToolExecutionBudget = false) {
  const key = swarmChatKey(agentId);
  const runtimeKey = swarmChatRuntimeKey(agentId);
  const recovery = swarmWorkRecoveries.get(key);
  const agent = theSwarmAgent(agentId);
  const conversation = activeConversationFor(agentId);
  if (!recovery || !agent || !conversation) return;
  if (conversation.binding_problem) {
    const message = String(conversation.binding_problem.message ||
      "This chat's setup changed. Start a fresh chat with the current setup.");
    sayInTheChatFor(agentId, message);
    if (theBigOne === agentId) $("theBigChatSaidBack").textContent = message;
    return;
  }
  const executionPause = swarmProjectWorkPauseMessage(agentId);
  if (executionPause) {
    sayInTheChatFor(agentId, executionPause);
    if (theBigOne === agentId) $("theBigChatSaidBack").textContent = executionPause;
    renderWorkRecoveryButtons(agentId);
    return;
  }
  if (!agent.ready) {
    sayInTheChatFor(agentId, agent.why_not || "This agent is not ready yet.");
    return;
  }
  if (swarmBusy.has(runtimeKey) || swarmChatResetting.has(runtimeKey)
      || swarmConversationSwitching.has(agentId) || swarmChatIsHydrating(agentId)) return;
  const structuredAnswers = compiledQuestionAnswers(
    recovery.questions, recovery.questionAnswers,
  );
  const answers = recovery.questions.length
    ? structuredAnswers.text : String(recovery.answerDraft || "").trim();
  if (recovery.status === "paused_for_user" && !answers) {
    const message = "Answer the paused questions before resuming this run.";
    sayInTheChatFor(agentId, message);
    if (theBigOne === agentId) $("theBigChatSaidBack").textContent = message;
    renderWorkRecoveryButtons(agentId);
    return;
  }
  if (recovery.status === "paused_for_user" && structuredAnswers.missing.length) {
    const message = "Answer every paused question before resuming this run.";
    sayInTheChatFor(agentId, message);
    if (theBigOne === agentId) $("theBigChatSaidBack").textContent = message;
    renderWorkRecoveryButtons(agentId);
    return;
  }
  if (recovery.projectId && conversation.project !== recovery.projectId) {
    const message = `Select ${recovery.projectName} for this chat before resuming its saved run.`;
    sayInTheChatFor(agentId, message);
    if (theBigOne === agentId) $("theBigChatSaidBack").textContent = message;
    return;
  }
  swarmBusy.add(runtimeKey);
  swarmStopping.delete(runtimeKey);
  nextSwarmChatRevision(agentId);
  const activity = beginSwarmChatActivity(agentId, "work", agent);
  sayInTheChatFor(agentId, recovery.status === "paused_for_user"
    ? "Resuming the saved run with your answer..."
    : recovery.status === "paused_provider"
      ? "Retrying the provider and resuming the exact saved run..."
      : recovery.status === "paused_tool_budget"
        ? "Resetting consumed context-tool time and resuming the exact saved run..."
      : recovery.status === "incomplete"
        ? "Resuming the unfinished long-horizon run..."
        : "Resuming deterministic verification for the saved run...");
  if (theBigOne === agentId) $("theBigChatSaidBack").textContent =
    "Resuming the exact saved project-work session...";
  setWhatCanBePressedInSwarm();
  renderWorkRecovery(agentId);
  try {
    const goalItem = (
      swarmGoalQueue?.current
      && ["paused", "running"].includes(swarmGoalQueue.status)
      && swarmGoalQueue.current.lead_id === agentId
      && swarmGoalQueue.current.project_id === conversation.project
      && swarmGoalQueue.current.objective === recovery.objective
    ) ? swarmGoalQueue.current : null;
    const answered = await request("/api/swarm/say", {
      method: "POST", body: JSON.stringify({
        agent: agentId,
        text: recovery.objective,
        mode: "work",
        attachments: [],
        activity: activity.id,
        chat: conversation.id,
        allow_project_changes: true,
        round_limit: selectedChatRoundLimit(agentId),
        resume_session_id: recovery.resumeToken,
        ...(goalItem ? {
          board_goal: true,
          goal_queue_id: swarmGoalQueue.queue_id,
          goal_item_id: goalItem.id,
        } : {}),
        ...(resetToolExecutionBudget
          ? {reset_context_tool_execution_budget: true} : {}),
        ...(answers ? {user_answers: answers} : {}),
        // Clone the frozen authority for JSON serialization. It came from the
        // original server response and has no editable UI path.
        ...(recovery.writeScopeRestricted
          ? {allowed_write_roots: [...recovery.allowedWriteRoots]} : {}),
      }),
    });
    if (!swarmActivityCanReconcileSuccess(activity)) {
      if (swarmChatKey(agentId) === key) void refreshTheChatFor(agentId);
      return answered;
    }
    finishSwarmChatActivity(agentId, true, "", activity, answered.participant_outcome);
    keepWhatWasSaidToRuntime(
      runtimeKey, answered.said || [], conversation.id,
    );
    const next = rememberWorkRecoveryForKey(key, answered, recovery.objective, conversation);
    if (next) {
      swarmWorkRecoveries.set(key, frozenWorkRecovery({...next, answerDraft: ""}));
      saveSwarmWorkRecoveries();
    }
    renderWorkRecovery(agentId);
    const message = workResponseWords(answered, agent.name);
    sayInRuntimeChat(runtimeKey, message);
    refreshSwarm(true);
    if (goalItem) {
      await refreshBoardGoalQueue(false);
      void continueBoardGoalQueue();
    }
    return answered;
  } catch (error) {
    if (!swarmActivityCanSettle(activity)) return null;
    if (swarmChatKey(agentId) === key) void refreshTheChatFor(agentId);
    const message = String(error?.message || error);
    sayInRuntimeChat(runtimeKey, message);
    if (!stoppedChatError(error)) showError(message);
    finishSwarmChatActivity(agentId, false, message, activity);
    return null;
  } finally {
    finishSwarmActivityResponse(agentId, activity);
    if (swarmActivityIsCurrent(activity)) {
      swarmBusy.delete(runtimeKey);
      swarmStopping.delete(runtimeKey);
    }
    setWhatCanBePressedInSwarm();
    renderWorkRecovery(agentId);
  }
}

async function sendWhatIsTypedTo(agentId) {
  const mode = arguments[1] || "chat";
  const confirmedPermission = arguments[2] || null;
  const goalQueueItem = arguments[3] || null;
  const card = theChatCardFor(agentId);
  const agent = theSwarmAgent(agentId);
  if (!card || !agent) return;
  const box = card.querySelector(".swarm-chat-box");
  const words = box.value.trim();
  const executionPause = projectWorkPauseForMessage(mode, words, agentId);
  if (executionPause) {
    sayInTheChatFor(agentId, executionPause);
    box.focus();
    return;
  }
  if (!words) { sayInTheChatFor(agentId, "Type something first."); return; }
  if (words.length > Number(limitsForSwarmChat(agentId).input_characters || 200000)) {
    sayInTheChatFor(agentId,
      "This message is over the displayed limit. Nexus kept the complete draft; split it or attach a file.");
    return;
  }
  if (!agent.ready) {
    sayInTheChatFor(agentId, agent.why_not || "This one is not set up yet.");
    return;
  }
  const conversation = activeConversationFor(agentId);
  if (conversation?.binding_problem) {
    sayInTheChatFor(agentId, String(conversation.binding_problem.message ||
      "This chat's setup changed. Start a fresh chat with the current setup."));
    return;
  }
  const requestChatKey = swarmChatKey(agentId);
  const runtimeKey = swarmChatRuntimeKey(agentId);
  if (swarmBusy.has(runtimeKey)) {
    sayInTheChatFor(agentId, "Still waiting for the last answer.");
    return;
  }
  if (swarmChatResetting.has(runtimeKey)) {
    sayInTheChatFor(agentId, "This exact chat is still starting again.");
    return;
  }
  if (swarmChatIsHydrating(agentId)) {
    sayInTheChatFor(agentId, "Loading this chat's saved identity first.");
    return;
  }
  if (swarmConversationSwitching.has(agentId)) {
    sayInTheChatFor(agentId, "Finishing the chat switch first.");
    return;
  }
  if (["collaborate", "work"].includes(mode) && isLoneAgentChat(agentId)) {
    sayInTheChatFor(agentId, loneAgentActionMessage(mode));
    box.focus();
    return;
  }
  if (mode === "collaborate") {
    const unavailable = syncChatTeamReadiness(agentId, card);
    if (unavailable.length) {
      sayInTheChatFor(agentId,
        `The team request was not sent. Repair ${unavailable.map((one) => one.name || one.id).join(", ")} first.`);
      box.focus();
      return;
    }
  }
  const projectPermission = confirmedPermission || confirmProjectWork(agent, words, mode);
  if (!projectPermission.allowed) return;
  const recoveryKey = requestChatKey;
  // The lease belongs to this immutable saved-chat identity. The selected
  // chat may change as soon as this request has started, and a sibling chat
  // under the same agent can acquire its own independent lease.
  swarmBusy.add(runtimeKey);
  swarmStopping.delete(runtimeKey);
  nextSwarmChatRevision(agentId);
  setWhatCanBePressedInSwarm();
  const attachmentKey = requestChatKey;
  const attachments = swarmChatAttachments.get(attachmentKey) || [];
  const durableDirectAdmission = mode === "work" && !goalQueueItem;
  // A sent prompt is already represented as the activity's local transcript
  // turn. Direct goal work keeps its draft until the backend confirms that the
  // exact payload (including attachment bytes) is durable. Other chat modes
  // retain their established immediate-clear behaviour.
  if (!durableDirectAdmission) {
    box.value = "";
    swarmChatComposerDrafts.delete(requestChatKey);
    rememberSwarmChatComposer(agentId);
  }
  const activity = beginSwarmChatActivity(agentId, mode, agent, words, attachments);
  sayInTheChatFor(agentId, mode === "auto" ? "Deciding whether connected agents should help..."
    : mode === "chat" ? `Asking ${agent.name}...`
    : mode === "collaborate" ? "Relaying to connected agents..."
    : "Starting durable goal work with the next useful task...");
  try {
    if (mode === "work" && !goalQueueItem) {
      if (!conversation?.project) throw new Error("Choose this chat's project before starting file work.");
      const directRequestKey = `nexus.long-horizon.direct-request.${recoveryKey}`;
      let durableRequest = null;
      const rawDurableRequest = localStorage.getItem(directRequestKey);
      if (rawDurableRequest !== null) {
        try { durableRequest = JSON.parse(rawDurableRequest); }
        catch (_) {
          throw new Error(
            "The saved browser goal-request marker is unreadable. No new project work was sent.",
          );
        }
        if (durableRequest === null) {
          throw new Error(
            "The saved browser goal-request marker has no exact identity. No new project work was sent.",
          );
        }
      }
      const directIntent = await directLongGoalIntent(
        conversation, agentId, words, attachments,
      );
      const directRequestId = directLongGoalRequestId(
        durableRequest, directIntent, {
          chat_id: conversation.id,
          project_id: conversation.project,
          lead_id: agentId,
        },
      );
      const exactPayload = {
        project_id: conversation.project,
        lead_id: agentId,
        chat_id: conversation.id,
        text: words,
        attachments,
      };
      const preparedAdmission = await prepareDirectLongGoalAdmission(
        exactPayload, directRequestId, directIntent, directRequestKey,
      );
      const {desktopOutbox, outbox, pending} = preparedAdmission;
      const heldDraft = swarmChatComposerDrafts.get(requestChatKey);
      if (!heldDraft || heldDraft.value === words) {
        swarmChatComposerDrafts.delete(requestChatKey);
      }
      if (swarmChatKey(agentId) === requestChatKey && box.value === words) {
        box.value = "";
        rememberSwarmChatComposer(agentId);
      }
      const exactAdmission = {
          request_id: directRequestId,
          chat_id: conversation.id,
          project_id: conversation.project,
          lead_id: agentId,
          intent_sha256: directIntent,
      };
      const started = preparedAdmission.started
        || await startAndReconcileDirectLongGoalAdmission(
          exactAdmission, pending.payload_sha256,
        );
      if (desktopOutbox) {
        await removeDirectLongGoalOutbox(
          conversation.id, directRequestId, outbox.payload_sha256,
        );
      }
      if (!clearDirectLongGoalRequestMarker(
        agentId, conversation.id, directRequestId, directIntent,
      )) {
        throw new Error(
          "The exact browser goal-request marker could not be cleared. The server receipt was left recoverable.",
        );
      }
      forgetDirectLongGoalRecovery(
        conversation.id, directRequestId, directIntent,
      );
      await bestEffortAcknowledgeDirectLongGoalTerminal(
        exactAdmission, "reconciled", started.goal.goal_id,
      );
      if (!swarmActivityCanReconcileSuccess(activity)) return started;
      selectLongGoalSnapshot(started.goal);
      if (swarmChatKey(agentId) === requestChatKey) {
        await refreshTheChatFor(agentId);
      }
      finishLongHorizonAdmissionActivity(agentId, longGoal, activity);
      clearSwarmActivityAttachments(activity);
      const admission = longHorizonAdmissionWords(longGoal);
      sayInRuntimeChat(runtimeKey,
        admission.detail);
      await refreshLongGoals(true);
      return started;
    }
    const said = await request("/api/swarm/say", {
      method: "POST", body: JSON.stringify({
        agent: agentId, text: words, mode, attachments, activity: activity.id,
        chat: conversation?.id || "",
        allow_project_changes: projectPermission.confirmed,
        ...(projectPermission.boardGoal ? {board_goal: true} : {}),
        ...(goalQueueItem ? {
          goal_queue_id: goalQueueItem.queueId,
          goal_item_id: goalQueueItem.itemId,
        } : {}),
        round_limit: selectedChatRoundLimit(agentId),
      }),
    });
    if (!swarmActivityCanReconcileSuccess(activity)) {
      if (swarmChatKey(agentId) === requestChatKey) void refreshTheChatFor(agentId);
      return said;
    }
    finishSwarmChatActivity(agentId, true, "", activity, said.participant_outcome);
    rememberWorkRecoveryForKey(recoveryKey, said, words, conversation);
    clearSwarmActivityAttachments(activity);
    if (!theChatCardFor(agentId)) {
      // The chat was closed while the answer was on its way. It is kept, and is
      // there when it is opened again.
      return;
    }
    keepWhatWasSaidToRuntime(
      runtimeKey, said.said || [], conversation?.id || "",
    );
    renderWorkRecovery(agentId);
    const participantWords = normalizedParticipantOutcome({
      participant_outcome: said.participant_outcome,
    });
    const ordinaryWords = participantWords
      ? workResponseWords(said, agent.name)
      : said.partial_provider_failure || automaticRoundStopWords(said) || (said.changed?.length
      ? `${agent.name} answered. Nexus applied ${said.changed.length} file change(s).`
      : said.routing?.requested === "auto" && said.routing?.selected === "collaborate"
        ? `${agent.name} answered after Nexus automatically involved ${said.collaborated_with?.length || 0} connected agent(s).`
        : said.collaborated_with?.length
          ? `${agent.name} answered after hearing ${said.collaborated_with.length} connected agent(s).`
          : `${agent.name} answered.`);
    sayInRuntimeChat(
      runtimeKey, workResponseWords(said, agent.name, ordinaryWords),
    );
    // The list down the side carries the last thing said under each name, and
    // something was just said.
    refreshSwarm(true);
    return said;
  } catch (error) {
    // Read back what was really kept, so a message that did not get through
    // stops looking like one that did. Do not keep the composer leased while a
    // secondary transcript read waits; the activity feed also reconciles a
    // terminal server run when the original HTTP response is lost.
    if (!swarmActivityCanSettle(activity)) return null;
    restoreSwarmChatDraft(requestChatKey, words);
    if (swarmChatKey(agentId) === requestChatKey) await refreshTheChatFor(agentId);
    if (durableDirectAdmission) await refreshDirectLongGoalRecoveries();
    if (!stoppedChatError(error)) showError(error.message);
    sayInRuntimeChat(runtimeKey, error.message);
    finishSwarmChatActivity(agentId, false, String(error.message || error), activity);
    return null;
  } finally {
    finishSwarmActivityResponse(agentId, activity);
    if (swarmActivityIsCurrent(activity)) {
      swarmBusy.delete(runtimeKey);
      swarmStopping.delete(runtimeKey);
    }
    setWhatCanBePressedInSwarm();
  }
}

async function startTheChatAgainFor(agentId) {
  const agent = theSwarmAgent(agentId);
  if (!agent) return;
  if (swarmConversationSwitching.has(agentId)) {
    sayInTheChatFor(agentId, "Finishing the selected saved-chat switch first. Start again was not sent.");
    return;
  }
  if (swarmChatIsHydrating(agentId)) {
    sayInTheChatFor(agentId, "Loading this chat's saved identity before starting it again...");
    const ready = await loadConversationsFor(agentId);
    if (!ready || swarmChatIsHydrating(agentId)
        || swarmConversationSwitching.has(agentId)) {
      sayInTheChatFor(agentId, "This chat's saved identity could not be loaded. Start again was not sent.");
      return;
    }
  }
  const conversation = activeConversationFor(agentId);
  const chatId = conversation?.id || "";
  const runtimeKey = swarmChatRuntimeKeyFor(agentId, chatId);
  if (swarmBusy.has(runtimeKey)) {
    sayInRuntimeChat(runtimeKey, "Stop this exact chat's current request before starting it again.");
    return;
  }
  if (swarmChatResetting.has(runtimeKey)) return;
  const route = String(agent.who || "");
  const agentName = String(agent.name || "This agent");
  swarmChatResetting.add(runtimeKey);
  nextSwarmChatRevision(agentId);
  sayInRuntimeChat(runtimeKey, "Starting this exact chat again...");
  setWhatCanBePressedInSwarm();
  try {
    const said = await request("/api/swarm/start-again", {
      method: "POST", body: JSON.stringify({
        agent: agentId, chat: chatId,
      }),
    });
    keepWhatWasSaidToRuntime(runtimeKey, [], chatId);
    let cleanupWarning = "";
    if (window.harnessDesktop?.resetWebChat) {
      const resets = Array.isArray(said.web_chat_resets)
        ? said.web_chat_resets.slice(0, 2)
        : (said.web_chat_id && said.web_conversation_key ? [{
            route: route.startsWith("web:") ? route : `web:${said.web_chat_id}`,
            previous_web_conversation_key: (
              said.previous_web_conversation_key || said.web_conversation_key
            ),
          }] : []);
      let resetFailure = null;
      for (const reset of resets) {
        const resetRoute = String(reset?.route || "");
        const previousKey = String(reset?.previous_web_conversation_key || "");
        if (resetRoute.startsWith("web:") && previousKey) {
          try {
            await window.harnessDesktop.resetWebChat(resetRoute, previousKey);
          } catch (error) {
            resetFailure ||= error;
          }
        }
      }
      if (resetFailure) {
        const detail = String(resetFailure?.message || resetFailure).slice(0, 240);
        cleanupWarning = (
          ` The chat did restart, but Nexus could not remove one old provider-window mapping. ${detail}`
        );
      }
    }
    if (chatId) await loadConversationsFor(agentId, false);
    sayInRuntimeChat(
      runtimeKey,
      `${said.note || `${agentName} starts again.`}${cleanupWarning}`,
    );
  } catch (error) {
    showError(error.message);
    sayInRuntimeChat(runtimeKey, error.message);
  } finally {
    swarmChatResetting.delete(runtimeKey);
    setWhatCanBePressedInSwarm();
  }
}

// ---- what they said to each other ----------------------------------------
//
// The run already showed one agent what another said - that is what the second
// round is - but it was shown only to the agent. This is the same thing where
// somebody watching can read it, which is what you want the moment two
// assistants disagree.

async function refreshWhatTheySaidToEachOther() {
  try {
    const said = await request("/api/swarm/what-they-said");
    swarmWhatTheySaid = said;
    renderWhatTheySaidToEachOther(said);
    renderTheChatsOnThisBoard();
  } catch (error) {
    $("swarmExchangeSaid").textContent = error.message;
  }
}

function renderWhatTheySaidToEachOther(said) {
  const list = $("swarmExchangeList");
  list.replaceChildren();
  const every = said.notes || [];
  // Showing one pair only, when somebody picked that pair down the side. The
  // whole exchange is still there; this is which part of it is on screen.
  const notes = swarmOnlyThisPair
    ? every.filter((one) => (
      (one.said_by === swarmOnlyThisPair.one && one.shown_to === swarmOnlyThisPair.other)
      || (one.said_by === swarmOnlyThisPair.other && one.shown_to === swarmOnlyThisPair.one)
    ))
    : every;
  if (swarmOnlyThisPair) {
    const back = make("button", "", "Show every pair again");
    back.type = "button";
    back.addEventListener("click", showEveryPairAgain);
    list.append(make("li", "hint", `Showing ${swarmOnlyThisPair.names.join(" and ")} only.`));
    const row = make("li");
    row.append(back);
    list.append(row);
  }
  const dropped = said.dropped || 0;
  const delivery = said.delivery || {};
  const countsKnown = delivery.counts_known !== false;
  const waiting = countsKnown ? Number(delivery.queued || 0) : 0;
  const retrying = countsKnown ? Number(delivery.retrying || 0) : 0;
  const deliveryTrouble = String(said.delivery_trouble || delivery.trouble || "").trim();
  const ordinaryStatus = notes.length
    ? `${notes.length} answer${notes.length === 1 ? "" : "s"} passed`
      + (dropped ? `, and ${dropped} older ones dropped to keep the list readable` : "")
      + (waiting ? `; ${waiting} safely queued${retrying ? ` (${retrying} awaiting retry)` : ""}` : "")
    : (waiting
      ? `${waiting} message${waiting === 1 ? " is" : "s are"} safely queued for the next successful turn`
      : "nothing passed yet");
  $("swarmExchangeSaid").textContent = deliveryTrouble
    ? `${ordinaryStatus}. Delivery status needs attention: ${deliveryTrouble}`
    : ordinaryStatus;
  if (deliveryTrouble) {
    list.append(make("li", "warning-one", deliveryTrouble));
  }
  if (!notes.length) {
    list.append(make("li", "hint",
      "Nothing has been passed between agents yet. It happens on the second round "
      + "of a run, and only between a pair with a green line."));
    return;
  }
  for (const one of notes) {
    const row = make("li", "swarm-exchange-one");
    row.append(make("strong", "",
      `${one.said_by_name} to ${one.shown_to_name}`));
    const under = [one.where];
    if (one.at) under.push(one.at);
    if (one.status === "queued") {
      under.push(one.attempts ? "delivery failed; kept for retry" : "queued for delivery");
    } else if (one.message_id) {
      under.push("received and acknowledged");
    }
    if (Number(one.original_characters || 0) > systemPromptCharacters(one.text || "")) {
      under.push(`display projection of ${Number(one.original_characters).toLocaleString()} characters`);
    }
    if (one.projection_source) under.push(`full source: ${one.projection_source}`);
    row.append(make("p", "hint", under.join(" | ")));
    row.append(make("p", "swarm-exchange-text", one.text));
    list.append(row);
  }
}

// ---- setting them going --------------------------------------------------
//
// The board says who works on what. This is the part that acts on it, and the
// part that has to keep saying what it is doing: every turn is a real
// assistant being asked a real question, which can take a minute, and a page
// that says nothing for a minute is a page somebody presses again.

let swarmGoing = false;      // a run is on
let swarmGoalWorkRunning = false; // this renderer is dispatching the server's exact cursor
let swarmGoalQueue = null;   // durable server-owned board-wide goal cursor
let swarmGoalQueueWatching = 0;
let swarmGoalQueueContinuing = false;
const SWARM_GOAL_QUEUE_REQUEST_KEY = "nexus.swarm.goal-queue-request.v1";
const LONG_GOAL_SELECTED_KEY = "nexus.long-horizon.selected.v1";
const LONG_GOAL_REQUEST_KEY = "nexus.long-horizon.board-request.v1";
const LONG_GOAL_COMPOSER_KEY = "nexus.long-horizon.composer.v1";
const LONG_GOAL_FORK_REQUEST_KEY = "nexus.long-horizon.fork-request.v1";
const LONG_GOAL_MAX_CUSTOM_CRITERIA = 29;
let longGoalDialogInvoker = null;
let longGoals = [];
let longGoal = null;
let longGoalEvents = [];
let longGoalCursor = 0;
let longGoalWatching = 0;
let longGoalLoadRevision = 0;
let selectedMissionTaskId = "";
let longGoalEventNotice = "";
let swarmWatching = 0;       // the timer that keeps asking how it is going
let swarmBoardRunId = localStorage.getItem("nexus.swarm.board-run") || "";
let swarmBoardRequestId = localStorage.getItem("nexus.swarm.board-request") || "";
let swarmBoardCursor = 0;

function beginLongGoalLoad() {
  // A previous goal's timer or slow response must never restore that goal
  // after the user admits or selects a newer one. Invalidate both before the
  // next network boundary; the load path checks this revision after every
  // awaited read.
  window.clearTimeout(longGoalWatching);
  longGoalWatching = 0;
  longGoalLoadRevision += 1;
  return longGoalLoadRevision;
}

function selectLongGoalSnapshot(goal) {
  beginLongGoalLoad();
  longGoal = goal;
  if (goal?.goal_id) localStorage.setItem(LONG_GOAL_SELECTED_KEY, goal.goal_id);
}

async function readSwarmBoardRun(runId, after = 0) {
  let identity = String(runId || "");
  let cursor = Math.max(0, Number(after) || 0);
  let doing = null;
  // Drain bounded server pages immediately after reload/reconnect. The cap
  // keeps one refresh fair; a later poll continues from the durable cursor.
  for (let page = 0; page < 100; page += 1) {
    const query = identity
      ? `?run_id=${encodeURIComponent(identity)}&after=${cursor}`
      : "";
    doing = (await request(`/api/swarm/how-it-is-going${query}`)).doing;
    if (!doing) return null;
    identity = String(doing.run_id || identity);
    const next = Math.max(cursor, Number(doing.next_cursor ?? doing.cursor) || 0);
    if (!doing.has_more || next === cursor) return doing;
    cursor = next;
  }
  return doing;
}

async function setThemGoing() {
  try {
    // Reuse an ambiguous request. A disconnected renderer cannot know whether
    // Start reached the server, so changing this identity on retry could ask
    // every agent twice.
    const requestId = swarmBoardRequestId || crypto.randomUUID();
    swarmBoardRequestId = requestId;
    localStorage.setItem("nexus.swarm.board-request", requestId);
    const said = await request("/api/swarm/start", {
      method: "POST", body: JSON.stringify({request_id: requestId}),
    });
    swarmBoardRunId = said.run_id || requestId;
    swarmBoardCursor = Number((said.doing || {}).cursor || 0);
    localStorage.setItem("nexus.swarm.board-run", swarmBoardRunId);
    swarmDoing = said.doing || null;
    renderWhatTheyAreDoing(said.doing);
    watchWhatTheyAreDoing();
    sayInSwarm("They are going. What each one says lands in its own chat.");
  } catch (error) {
    showError(error.message);
    sayInSwarm(error.message);
    $("swarmDoingSaid").textContent = error.message;
  }
}

function workingPairForProject(project) {
  const board = theSwarmBoard();
  const assigned = new Set((board.works_on || [])
    .filter((line) => line.project === project.id).map((line) => line.agent));
  const ready = (board.agents || []).filter(
    (agent) => assigned.has(agent.id) && agent.ready && agent.who);
  for (const lead of ready) {
    const peer = ready.find(
      (other) => other.id !== lead.id && mayTheyTalk(lead.id, other.id));
    if (peer) return {lead, peer};
  }
  return null;
}

async function prepareGoalConversation(project, pair) {
  await openTheChatFor(pair.lead.id);
  const held = swarmChats.find((one) => one.agent === pair.lead.id);
  if (!held) throw new Error(`Nexus could not open ${pair.lead.name}'s durable chat.`);
  let conversation = (held.conversations || []).find((one) => (
    one.peer === pair.peer.id && !one.archived_at && one.project === project.id));
  if (!conversation) {
    conversation = (held.conversations || []).find(
      (one) => one.peer === pair.peer.id && !one.archived_at);
  }
  if (conversation && held.conversation !== conversation.id) {
    await activateConversationFor(pair.lead.id, conversation.id);
  } else if (!conversation) {
    await createConversationFor(pair.lead.id, pair.peer.id);
  }
  conversation = activeConversationFor(pair.lead.id);
  if (!conversation || conversation.peer !== pair.peer.id) {
    throw new Error(
      `Nexus could not prepare a durable ${pair.lead.name}/${pair.peer.name} project chat.`);
  }
  if (conversation.project !== project.id) {
    await selectConversationProject(pair.lead.id, project.id);
    conversation = activeConversationFor(pair.lead.id);
  }
  if (!conversation || conversation.project !== project.id) {
    throw new Error(`Nexus could not bind the durable chat to ${project.name}.`);
  }
  return conversation;
}

function showBoardGoalQueue(queue) {
  swarmGoalQueue = queue || null;
  const button = $("swarmLegacyGoals");
  if (!queue) {
    button.textContent = "Use legacy paired workflow";
    return;
  }
  const current = queue.current;
  if (queue.status === "complete") {
    button.textContent = "Use legacy paired workflow";
    $("swarmGoalWorkSaid").textContent =
      `All ${queue.total} board goal(s) reached verified completion.`;
    localStorage.removeItem(SWARM_GOAL_QUEUE_REQUEST_KEY);
  } else if (queue.status === "cancelled") {
    button.textContent = "Restart legacy paired workflow";
    $("swarmGoalWorkSaid").textContent = queue.note || "The remaining goals were cancelled.";
    localStorage.removeItem(SWARM_GOAL_QUEUE_REQUEST_KEY);
  } else {
    button.textContent = queue.status === "paused"
      ? "Open the saved goal's Resume controls" : "Show active goal work";
    const number = Number(queue.cursor || 0) + 1;
    $("swarmGoalWorkSaid").textContent = queue.note
      || `Goal ${number} of ${queue.total}: ${current?.lead_name || "the team"} is working in ${current?.project_name || "the project"}.`;
  }
  setWhatCanBePressedInSwarm();
}

async function refreshBoardGoalQueue(autoContinue = false) {
  try {
    const said = await request("/api/swarm/goal-queue");
    showBoardGoalQueue(said.queue);
    if (said.queue?.status === "running") watchBoardGoalQueue();
    if (autoContinue && said.queue?.status === "queued") {
      void continueBoardGoalQueue();
    }
    return said.queue;
  } catch (error) {
    $("swarmGoalWorkSaid").textContent = String(error.message || error);
    return null;
  }
}

function watchBoardGoalQueue() {
  if (swarmGoalQueueWatching) return;
  const poll = async () => {
    swarmGoalQueueWatching = 0;
    const queue = await refreshBoardGoalQueue(false);
    if (queue?.status === "running") {
      swarmGoalQueueWatching = window.setTimeout(poll, 1200);
    } else if (queue?.status === "queued") {
      void continueBoardGoalQueue();
    }
  };
  swarmGoalQueueWatching = window.setTimeout(poll, 1200);
}

async function continueBoardGoalQueue({retryPaused = false} = {}) {
  if (swarmGoalQueueContinuing) return;
  swarmGoalQueueContinuing = true;
  swarmGoalWorkRunning = true;
  setWhatCanBePressedInSwarm();
  try {
    while (true) {
      const queue = await refreshBoardGoalQueue(false);
      if (!queue || ["complete", "cancelled"].includes(queue.status)) return;
      const item = queue.current;
      if (!item) return;
      if (queue.status === "running") {
        watchBoardGoalQueue();
        return;
      }
      const project = theSwarmProject(item.project_id);
      const lead = theSwarmAgent(item.lead_id);
      const peer = theSwarmAgent(item.peer_id);
      if (!project || !lead || !peer) {
        throw new Error(
          `The saved queue still points to ${item.project_path}, led by ${item.lead_name} with ${item.peer_name}, but that exact board topology is no longer open. Restore it; Nexus did not substitute another goal or team.`);
      }
      const conversation = await prepareGoalConversation(project, {lead, peer});
      if (queue.status === "paused" && item.resume_token) {
        $("swarmGoalWorkSaid").textContent =
          `Goal ${Number(queue.cursor) + 1} of ${queue.total} is saved in ${lead.name}'s chat. Use its Resume action; after verification Nexus will continue the remaining goals automatically.`;
        await refreshDurableSwarmWorkRecoveries();
        if (!workRecoveryFor(lead.id) && item.recovery?.resume_token) {
          rememberWorkRecoveryForKey(
            swarmChatKey(lead.id), item.recovery, item.objective,
            {project: item.project_id});
        }
        renderWorkRecovery(lead.id);
        return;
      }
      if (queue.status === "paused" && !retryPaused) return;
      $("swarmGoalWorkSaid").textContent =
        `Goal ${Number(queue.cursor) + 1} of ${queue.total}: ${lead.name} and ${peer.name} are working in ${project.name}.`;
      const card = theChatCardFor(lead.id);
      const box = card?.querySelector(".swarm-chat-box");
      if (!box) throw new Error(`Nexus could not open the composer for ${lead.name}.`);
      box.value = item.objective;
      countWhatIsTypedTo(lead.id);
      const answered = await sendWhatIsTypedTo(
        lead.id, "work", {allowed: true, confirmed: true, boardGoal: true},
        {queueId: queue.queue_id, itemId: item.id});
      if (!answered) {
        await refreshBoardGoalQueue(false);
        return;
      }
      // The server records the verified result and moves the cursor before
      // answering. Read that authority rather than incrementing browser state.
      retryPaused = false;
    }
  } finally {
    swarmGoalQueueContinuing = false;
    swarmGoalWorkRunning = false;
    setWhatCanBePressedInSwarm();
  }
}

async function workOnEveryBoardGoalLegacy() {
  if (swarmGoalWorkRunning) return;
  const existing = await refreshBoardGoalQueue(false);
  if (existing && ["queued", "running", "paused"].includes(existing.status)) {
    await continueBoardGoalQueue({retryPaused: existing.status === "paused"});
    return;
  }
  const projects = theSwarmBoard().projects.filter(
    (project) => project.is_there && Array.isArray(project.tasks) && project.tasks.length);
  if (!projects.length) {
    $("swarmGoalWorkSaid").textContent = "Write at least one job on a project first.";
    return;
  }
  const prepared = projects.map((project) => ({
    project, pair: workingPairForProject(project),
  }));
  const blocked = prepared.filter((one) => !one.pair);
  if (blocked.length) {
    $("swarmGoalWorkSaid").textContent =
      `Connect at least two ready agents who both work on ${blocked.map((one) => one.project.name).join(", ")}, with a green line between them.`;
    return;
  }
  const total = prepared.reduce((sum, one) => sum + one.project.tasks.length, 0);
  const scope = prepared.map((one) =>
    `${one.project.path} — ${one.project.tasks.length} goal(s), led by ${one.pair.lead.name} with ${one.pair.peer.name}`
  ).join("\n");
  if (!window.confirm(
    `Start durable project work for ${total} goal(s)?\n\n${scope}\n\n`
    + "Connected agents may change files inside these project folders. Nexus will plan, "
    + "review, test, repair, and keep incomplete work resumable. It will claim completion "
    + "only after deterministic verification."
  )) return;

  try {
    const requestId = localStorage.getItem(SWARM_GOAL_QUEUE_REQUEST_KEY)
      || (globalThis.crypto?.randomUUID ? globalThis.crypto.randomUUID()
        : `board-goals-${Date.now()}-${Math.random().toString(36).slice(2)}`);
    localStorage.setItem(SWARM_GOAL_QUEUE_REQUEST_KEY, requestId);
    const started = await request("/api/swarm/goal-queue/start", {
      method: "POST", body: JSON.stringify({request_id: requestId}),
    });
    showBoardGoalQueue(started.queue);
    await continueBoardGoalQueue({retryPaused: true});
  } catch (error) {
    const words = String(error.message || error);
    $("swarmGoalWorkSaid").textContent = words;
    showError(words);
  }
}

function missionSelectedGoalId() {
  // Admission and the select's own change handler persist the new identity
  // before any awaited refresh. The rendered select may still show the prior
  // goal during that gap, so renderer state is only a fallback.
  return String(localStorage.getItem(LONG_GOAL_SELECTED_KEY)
    || $("missionGoalSelect")?.value || "");
}

function missionStatusWords(goal) {
  if (!goal) return "No long-horizon goal selected.";
  const progress = goal.progress || {complete: 0, total: 0};
  return `${goal.status} · ${progress.complete}/${progress.total} tasks · `
    + `${goal.budget?.provider_calls || 0}/${goal.budget?.max_provider_calls || 0} provider calls`;
}

function missionProviderSetupChanged(goal = longGoal) {
  return Boolean(
    goal?.provider_setup_changed
    && goal?.provider_setup_status?.code === "provider_setup_changed"
  );
}

function focusCurrentBoardGoalSetup() {
  const status = longGoal?.provider_setup_status || {};
  const firstChanged = (status.agents || []).find((one) => one?.agent_id);
  const currentAgent = firstChanged ? theSwarmAgent(firstChanged.agent_id) : null;
  if (currentAgent) pickSwarmBox("agent", currentAgent.id);
  const start = $("swarmWorkGoals");
  start.scrollIntoView({behavior: "smooth", block: "center"});
  start.focus({preventScroll: true});
  const protectedOldGoal = longGoal
    && !["complete", "cancelled"].includes(longGoal.status);
  $("swarmGoalWorkSaid").textContent = protectedOldGoal
    ? "Review the current board routes. The old goal remains protected; cancel it in Mission control when ready, then press Work until the goals are achieved to create a new goal."
    : "Review the current board routes, then press Work until the goals are achieved to create a new goal with a fresh history binding.";
}

async function prepareNewGoalWithCurrentProviderSetup() {
  if (!missionProviderSetupChanged()) return;
  const protectedGoal = longGoal;
  const needsCancellation = !["complete", "cancelled"].includes(
    protectedGoal.status
  );
  if (needsCancellation && !window.confirm(
    "Cancel this protected old goal? Its tasks, events, artifacts, and evidence remain available for inspection. Nothing is rebound. After cancellation Nexus will focus the current board’s normal Start control so you can create a separate goal."
  )) return;
  try {
    let cancellationStatus = String(protectedGoal.status || "");
    if (needsCancellation) {
      const cancelled = await request("/api/long-horizon/control", {
        method: "POST", body: JSON.stringify({
          goal_id: protectedGoal.goal_id, action: "cancel", payload: {},
        }),
      });
      cancellationStatus = String(cancelled.goal?.status || cancellationStatus);
      await refreshLongGoals(true);
      const refreshed = longGoals.find(
        (goal) => goal.goal_id === protectedGoal.goal_id,
      ) || (longGoal?.goal_id === protectedGoal.goal_id ? longGoal : null);
      cancellationStatus = String(refreshed?.status || cancellationStatus);
      if (cancellationStatus !== "cancelled") {
        $("swarmGoalWorkSaid").textContent = cancellationStatus === "cancelling"
          ? "Cancellation is draining active work. Nexus has not released the project yet. The saved request and composer recovery state were retained; refresh and wait for cancelled before starting a new goal."
          : `The old goal is ${cancellationStatus || "not yet cancelled"}. Nexus retained the saved request and composer recovery state and did not prepare a new goal.`;
        return;
      }
    }
    // A new provider binding requires a new request identity. Never let an
    // interrupted earlier Start attempt turn this explicit migration into an
    // idempotent replay of old intent.
    localStorage.removeItem(LONG_GOAL_REQUEST_KEY);
    localStorage.removeItem(LONG_GOAL_COMPOSER_KEY);
    focusCurrentBoardGoalSetup();
    $("swarmGoalWorkSaid").textContent = needsCancellation
      ? "The old goal was cancelled and its history was kept. Press Work until the goals are achieved to start a separate goal from the current board setup."
      : "Press Work until the goals are achieved to start a separate goal from the current board setup.";
  } catch (error) {
    showError(error.message || String(error));
  }
}

function renderMissionControl() {
  const select = $("missionGoalSelect");
  const selected = longGoal?.goal_id || missionSelectedGoalId();
  select.replaceChildren();
  if (!longGoals.length) {
    const option = make("option", "", "No goals yet");
    option.value = "";
    select.append(option);
  }
  for (const goal of longGoals) {
    const setupLabel = missionProviderSetupChanged(goal) ? " · provider setup changed" : "";
    const option = make("option", "",
      `${goal.project?.name || "Project"}: ${String(goal.objective || "").split("\n")[0].slice(0, 70)} [${goal.status}${setupLabel}]`);
    option.value = goal.goal_id;
    option.selected = goal.goal_id === selected;
    select.append(option);
  }
  $("missionGoalSummary").textContent = longGoal
    ? `${longGoal.objective} · Success: ${(longGoal.success_criteria || []).join("; ")}`
    : "Start a project goal to see durable work here.";
  const lastActivity = longGoalEvents.length
    ? ` Last activity ${new Date(longGoalEvents[longGoalEvents.length - 1].at_ms).toLocaleString()}.`
    : "";
  $("missionProgress").textContent = longGoal
    ? `${missionStatusWords(longGoal)}. ${longGoal.note || ""}${lastActivity}` : "";
  const providerSetupChanged = missionProviderSetupChanged();
  const setupStatus = longGoal?.provider_setup_status || {};
  const setupWarning = $("missionProviderSetupChanged");
  setupWarning.hidden = !providerSetupChanged;
  $("missionProviderSetupChangedMessage").textContent = providerSetupChanged
    ? setupStatus.message || "The saved provider setup changed." : "";
  const setupAgents = $("missionProviderSetupChangedAgents");
  setupAgents.replaceChildren();
  for (const agent of providerSetupChanged ? setupStatus.agents || [] : []) {
    setupAgents.append(make(
      "li", "",
      `${agent.name || agent.agent_id || "Agent"}${agent.route ? ` (${agent.route})` : ""}: ${agent.reason || "Provider setup changed."}`,
    ));
  }
  if (longGoal && document.activeElement !== $("missionCriteria")) {
    $("missionCriteria").value = (longGoal.success_criteria || []).join("\n");
  }
  const terminal = !longGoal
    || ["complete", "cancelled", "failed", "cancelling"].includes(longGoal.status);
  const immutable = !longGoal
    || ["complete", "cancelled", "cancelling"].includes(longGoal.status);
  const hasPendingDecision = Boolean(longGoal?.pending_interrupts?.length);
  $("missionPause").disabled = !longGoal || terminal || longGoal.status === "paused";
  $("missionResume").disabled = !longGoal || providerSetupChanged || hasPendingDecision
    || !["paused", "failed"].includes(longGoal.status);
  // A failed goal still owns the exclusive project lease until cancellation.
  // Keep cancellation available so failure/provider drift cannot strand it.
  $("missionCancel").disabled = !longGoal
    || ["complete", "cancelled"].includes(longGoal.status);
  $("missionFork").disabled = immutable || providerSetupChanged || hasPendingDecision;
  $("missionCriteria").disabled = immutable || hasPendingDecision;
  $("missionCriteriaSave").disabled = immutable || hasPendingDecision;
  $("missionSteer").disabled = immutable || providerSetupChanged || hasPendingDecision;
  $("missionSteerSend").disabled = immutable || providerSetupChanged || hasPendingDecision;
  $("missionMessageAgent").disabled = immutable || providerSetupChanged || hasPendingDecision;
  $("missionProviderSetupPrepare").disabled = !longGoal || longGoal.status === "cancelling";

  const tasks = $("missionTasks");
  tasks.replaceChildren();
  for (const state of ["running", "pending_apply", "ready", "waiting", "waiting_review", "blocked", "failed", "complete", "cancelled"]) {
    const matching = (longGoal?.tasks || []).filter((task) => task.state === state);
    if (!matching.length) continue;
    const column = make("section", "mission-task-column");
    column.append(make("h3", "", `${state.replaceAll("_", " ")} (${matching.length})`));
    for (const task of matching) {
      const card = make("button", `mission-task-card${selectedMissionTaskId === task.id ? " selected" : ""}`);
      card.type = "button";
      card.dataset.taskId = task.id;
      card.setAttribute("aria-pressed", String(selectedMissionTaskId === task.id));
      card.append(make("strong", "", task.title));
      card.append(make("span", "", `Owner: ${task.assigned_agent_id || "unassigned"} · attempt ${task.attempts || 0}`));
      if ((task.depends_on || []).length) card.append(make("span", "", `Needs: ${task.depends_on.join(", ")}`));
      if (task.last_error) card.append(make("span", "warning-one", task.last_error));
      if ((task.evidence || []).length) card.append(make("span", "", `Evidence: ${task.evidence.slice(-2).join("; ")}`));
      card.addEventListener("click", () => { selectedMissionTaskId = task.id; renderMissionControl(); });
      column.append(card);
    }
    tasks.append(column);
  }

  const agents = $("missionAgents");
  agents.replaceChildren();
  const changedAgentIds = new Set((setupStatus.agents || []).map((one) => one.agent_id));
  for (const agent of longGoal?.agents || []) {
    const owned = (longGoal.tasks || []).filter((task) => task.assigned_agent_id === agent.id);
    const row = make("article", "mission-agent");
    row.classList.toggle("provider-setup-changed", changedAgentIds.has(agent.id));
    row.append(make("strong", "", agent.name));
    row.append(make("span", "", `${agent.who} · ${owned.filter((task) => task.state === "running").length ? "working" : "available"}`));
    row.append(make("span", "", `${owned.length} assigned task(s)`));
    const changedSetup = (setupStatus.agents || []).find((one) => one.agent_id === agent.id);
    if (changedSetup) row.append(make("span", "warning-one", changedSetup.reason));
    agents.append(row);
  }
  const selectedTask = longGoal?.tasks?.find((one) => one.id === selectedMissionTaskId);
  const reviewedParent = selectedTask?.kind === "review"
    ? longGoal?.tasks?.find((one) => one.id === selectedTask.review_of) : null;
  const reviewedOwner = reviewedParent
    ? longGoal?.agents?.find((one) => one.id === reviewedParent.assigned_agent_id) : null;
  const reassign = $("missionReassignAgent");
  const previousReassign = reassign.value;
  reassign.replaceChildren();
  for (const agent of longGoal?.agents || []) {
    const reviewerIdentity = String(agent.provider_identity_sha256 || "");
    const ownerIdentity = String(reviewedOwner?.provider_identity_sha256 || "");
    if (reviewedParent && (
      agent.id === reviewedParent.assigned_agent_id
      || !reviewerIdentity || !ownerIdentity || reviewerIdentity === ownerIdentity
    )) continue;
    const option = make("option", "", `${agent.name} (${agent.who})`);
    option.value = agent.id;
    option.selected = agent.id === previousReassign;
    reassign.append(option);
  }
  $("missionReassign").disabled = immutable || providerSetupChanged || hasPendingDecision || !selectedTask
    || !["ready", "blocked", "failed", "waiting"].includes(selectedTask.state);
  $("missionRetry").disabled = immutable || providerSetupChanged || hasPendingDecision || !selectedTask
    || !["blocked", "failed"].includes(selectedTask.state);
  const selectedOwner = longGoal?.agents?.find(
    (one) => one.id === selectedTask?.assigned_agent_id);
  const independentReviewerAvailable = (longGoal?.agents || []).some(
    (one) => one.id !== selectedTask?.assigned_agent_id
      && one.provider_identity_sha256
      && selectedOwner?.provider_identity_sha256
      && one.provider_identity_sha256 !== selectedOwner.provider_identity_sha256);
  $("missionRequestReview").disabled = immutable || providerSetupChanged || hasPendingDecision
    || !selectedTask || !independentReviewerAvailable;
  $("missionRequestReview").title = !selectedTask || independentReviewerAvailable
    ? ""
    : "Independent review needs an authorized agent on a different provider backend; another route alias is not enough.";

  const pending = longGoal?.pending_interrupts || [];
  $("missionInboxCount").textContent = pending.length ? `(${pending.length})` : "";
  const inbox = $("missionInbox");
  inbox.replaceChildren();
  if (!pending.length) inbox.append(make("p", "hint", "No agent is waiting for you."));
  if (pending.length) {
    const form = make("form", "mission-question");
    if (immutable || providerSetupChanged) form.setAttribute("aria-disabled", "true");
    form.append(make("p", "hint",
      "Answer the complete decision set shown here. Nexus rejects this submission if the goal or pending questions change before you send it."));
    for (const item of pending) {
      const fieldset = make("fieldset", "mission-question-set");
      fieldset.append(make("legend", "", `${item.reason} · task ${item.task_id}`));
      fieldset.append(make("p", "hint", `Waiting since ${new Date(item.created_ms).toLocaleString()}`));
    for (const question of item.questions || []) {
        fieldset.append(make("strong", "", question.prompt));
      const name = `${item.id}-${question.id}`;
      for (const option of question.options || []) {
        const label = make("label", "mission-option");
        const input = make("input");
        input.type = question.multiple ? "checkbox" : "radio";
        input.name = name;
        input.value = option.label;
        label.append(input, document.createTextNode(` ${option.label}${option.recommended ? " (recommended)" : ""}`));
        if (option.description) label.append(make("small", "", option.description));
          fieldset.append(label);
      }
      if (question.allow_other || !(question.options || []).length) {
        const other = make("textarea", "mission-other");
        other.rows = 2;
        other.placeholder = "Or type your own answer";
        other.dataset.questionName = name;
          fieldset.append(other);
        }
      }
      form.append(fieldset);
    }
    const submit = make("button", "primary", "Send decisions and resume exact tasks");
    submit.type = "submit";
    submit.disabled = immutable || providerSetupChanged;
    form.append(submit);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const answers = {};
      for (const item of pending) {
        const parts = [];
        for (const question of item.questions || []) {
          const name = `${item.id}-${question.id}`;
          const chosen = [...form.querySelectorAll(`input[name="${CSS.escape(name)}"]:checked`)].map((one) => one.value);
          const other = form.querySelector(`[data-question-name="${CSS.escape(name)}"]`)?.value.trim();
          const answer = other || chosen.join(", ");
          if (!answer) { showError("Answer every question before resuming."); return; }
          parts.push(`${question.prompt}: ${answer}`);
        }
        answers[item.id] = parts.join("\n");
      }
      await request("/api/long-horizon/answer", {method: "POST", body: JSON.stringify({
        goal_id: longGoal.goal_id,
        expected_revision: longGoal.revision,
        pending_ids: pending.map((one) => one.id),
        answers,
      })});
      await refreshLongGoals(true);
    });
    inbox.append(form);
  }

  const evidence = $("missionEvidence");
  evidence.replaceChildren();
  if (longGoal?.verification) {
    const pre = make("pre");
    pre.textContent = JSON.stringify(longGoal.verification, null, 2);
    evidence.append(make("h3", "", "Deterministic verification"), pre);
  }
  for (const artifact of longGoal?.artifacts || []) {
    const details = make("details");
    details.append(make("summary", "", `${artifact.kind || "artifact"} · ${artifact.patch_sha256 || "recorded evidence"}`));
    const pre = make("pre");
    pre.textContent = artifact.patch || JSON.stringify(artifact, null, 2);
    details.append(pre);
    evidence.append(details);
  }
  renderMissionEvents();
}

function renderMissionEvents() {
  const list = $("missionEvents");
  list.replaceChildren();
  const filter = String($("missionEventFilter")?.value || "");
  const shown = filter ? longGoalEvents.filter((event) => event.type.includes(filter)) : longGoalEvents;
  if (longGoalEventNotice) list.append(make("li", "warning-one", longGoalEventNotice));
  for (const event of shown.slice(-250).reverse()) {
    const row = make("li", "mission-event");
    row.append(make("strong", "", event.type.replaceAll("_", " ")));
    row.append(make("span", "", `${new Date(event.at_ms).toLocaleTimeString()} · r${event.revision} · #${event.seq}`));
    const payload = make("code", "", JSON.stringify(event.payload || {}));
    row.append(payload);
    list.append(row);
  }
}

async function refreshLongGoalOriginChats(goals) {
  const chatIds = new Set((Array.isArray(goals) ? goals : [goals])
    .map((goal) => String(goal?.conversation_id || "")).filter(Boolean));
  if (!chatIds.size) return;
  const agents = [...new Set(swarmChats
    .filter((held) => chatIds.has(String(activeConversationFor(held.agent)?.id || "")))
    .map((held) => held.agent))];
  await Promise.all(agents.map((agentId) => refreshTheChatFor(agentId)));
}

function longGoalNeedsWatching(goal) {
  return ["waiting_for_project", "queued", "running", "cancelling"]
    .includes(String(goal?.status || ""));
}

function anyLongGoalNeedsWatching() {
  return longGoalNeedsWatching(longGoal) || longGoals.some(longGoalNeedsWatching);
}

async function loadLongGoal(goalId, resetEvents = false, inheritedRevision = 0) {
  const loadRevision = inheritedRevision || beginLongGoalLoad();
  const isCurrent = () => loadRevision === longGoalLoadRevision;
  if (!goalId) {
    if (!isCurrent()) return;
    longGoal = null;
    longGoalEvents = [];
    longGoalCursor = 0;
    longGoalEventNotice = "";
    renderMissionControl();
    return;
  }
  const resetsHistory = resetEvents || longGoal?.goal_id !== goalId;
  let nextEvents = resetsHistory ? [] : [...longGoalEvents];
  let nextCursor = resetsHistory ? 0 : longGoalCursor;
  let nextNotice = resetsHistory ? "" : longGoalEventNotice;
  const goalAnswer = await request(`/api/long-horizon/goal?id=${encodeURIComponent(goalId)}`);
  if (!isCurrent()) return;
  const nextGoal = goalAnswer.goal;
  await refreshLongGoalOriginChats(nextGoal);
  if (!isCurrent()) return;
  for (let page = 0; page < 20; page += 1) {
    const eventAnswer = await request(
      `/api/long-horizon/events?id=${encodeURIComponent(goalId)}&after=${nextCursor}`);
    if (!isCurrent()) return;
    if (eventAnswer.truncated) {
      nextNotice = `Older event deltas were compacted; this view starts at event ${eventAnswer.oldest_available}. The authenticated goal snapshot remains current.`;
    }
    nextEvents.push(...(eventAnswer.events || []).filter(
      (event) => !nextEvents.some((held) => held.event_id === event.event_id)));
    const next = Number(eventAnswer.next || nextCursor);
    if (!eventAnswer.has_more || next === nextCursor) { nextCursor = next; break; }
    nextCursor = next;
  }
  if (!isCurrent()) return;
  longGoal = nextGoal;
  longGoalEvents = nextEvents;
  longGoalCursor = nextCursor;
  longGoalEventNotice = nextNotice;
  localStorage.setItem(LONG_GOAL_SELECTED_KEY, goalId);
  renderMissionControl();
  if (anyLongGoalNeedsWatching()) watchLongGoal();
}

async function refreshLongGoals(loadSelected = true) {
  const loadRevision = beginLongGoalLoad();
  const isCurrent = () => loadRevision === longGoalLoadRevision;
  try {
    const nextGoals = (await request("/api/long-horizon/goals")).goals || [];
    if (!isCurrent()) return;
    await refreshLongGoalOriginChats(nextGoals);
    if (!isCurrent()) return;
    longGoals = nextGoals;
    const wanted = missionSelectedGoalId();
    const selected = longGoals.find((goal) => goal.goal_id === wanted) || longGoals[0] || null;
    if (loadSelected && selected) {
      await loadLongGoal(
        selected.goal_id, longGoal?.goal_id !== selected.goal_id, loadRevision,
      );
    } else if (isCurrent()) {
      longGoal = selected;
      renderMissionControl();
    }
  } catch (error) {
    if (!isCurrent()) return;
    const words = String(error.message || error);
    $("missionProgress").textContent = words;
    if (anyLongGoalNeedsWatching()) {
      longGoalEventNotice = `Live refresh was interrupted: ${words} Nexus will reconnect automatically.`;
      renderMissionControl();
      watchLongGoal();
    }
  }
}

function watchLongGoal() {
  if (longGoalWatching) return;
  longGoalWatching = window.setTimeout(async () => {
    longGoalWatching = 0;
    // Poll the full durable set, not only the selected detail. The list read
    // projects lifecycle transitions into every origin chat, so concurrent
    // goals remain truthful when the user later reopens an inactive chat.
    await refreshLongGoals(true);
  }, 1200);
}

function savedLongGoalComposer() {
  try {
    const held = JSON.parse(localStorage.getItem(LONG_GOAL_COMPOSER_KEY) || "null");
    return held && held.schema_version === 1 && held.draft && typeof held.draft === "object"
      ? held : null;
  } catch (_) {
    return null;
  }
}

function availableLongGoalProjects() {
  return (theSwarmBoard().projects || []).filter((project) => project.is_there === true);
}

function selectedLongGoalAgentIds() {
  return [...$("longGoalAgents").querySelectorAll('input[type="checkbox"]:checked')]
    .map((input) => String(input.value || "")).filter(Boolean).sort();
}

function longGoalComposerDraft() {
  return {
    project_id: String($("longGoalProject").value || ""),
    objective: String($("longGoalText").value || ""),
    success_criteria: String($("longGoalCriteria").value || "")
      .split(/\r?\n/).map((one) => one.trim()).filter(Boolean),
    agent_ids: selectedLongGoalAgentIds(),
    lead_id: String($("longGoalLead").value || ""),
    collaboration_mode: $("longGoalParticipation").value === "adaptive"
      ? "adaptive" : "every",
  };
}

function longGoalIntent(draft) {
  return JSON.stringify({
    schema_version: 1,
    project_id: draft.project_id,
    objectives: [draft.objective.trim()],
    success_criteria: [...draft.success_criteria],
    agent_ids: [...draft.agent_ids].sort(),
    lead_id: draft.lead_id,
    collaboration_mode: draft.collaboration_mode,
  });
}

function saveLongGoalComposerDraft() {
  if (!$("longGoalDialog").open) return;
  const draft = longGoalComposerDraft();
  const intent = longGoalIntent(draft);
  const previous = savedLongGoalComposer();
  localStorage.setItem(LONG_GOAL_COMPOSER_KEY, JSON.stringify({
    schema_version: 1,
    draft,
    intent,
    request_id: previous?.intent === intent ? String(previous.request_id || "") : "",
  }));
}

function showLongGoalComposerError(message = "") {
  const error = $("longGoalError");
  error.textContent = message;
  error.hidden = !message;
  if (message) error.focus({preventScroll: true});
}

function renderLongGoalComposerReadiness() {
  const project = theSwarmProject($("longGoalProject").value);
  const authority = project
    ? (swarmSaid.project_authorities || {})[project.id] || null : null;
  const selected = selectedLongGoalAgentIds();
  const readyById = new Map((theSwarmBoard().agents || [])
    .filter((agent) => agent.ready === true && String(agent.who || ""))
    .map((agent) => [agent.id, agent]));
  const readySelected = selected.map((id) => readyById.get(id)).filter(Boolean);
  const participation = $("longGoalParticipation").value;
  const status = $("longGoalReadiness");
  status.replaceChildren();
  status.classList.remove("good", "problem");
  if (!project) {
    status.classList.add("problem");
    status.append(make("p", "", "Add or reconnect a local project folder before starting work."));
  } else if (authority?.can_run === false) {
    status.classList.add("problem");
    status.append(make("p", "",
      `${project.name}: ${authority.reason || "Project execution is paused."}`));
    status.append(make("p", "",
      "Use the host-folder recovery notice, or choose another project. Nexus will not start work through a paused authority."));
  } else if (!readySelected.length) {
    status.classList.add("problem");
    status.append(make("p", "", "Select at least one ready agent. Disabled agents have a connection problem to repair first."));
  } else {
    const routes = new Set(readySelected.map((agent) => agent.who));
    status.classList.add(readySelected.length > 1 ? "good" : "problem");
    status.append(make("p", "", `${readySelected.length} ready agent${readySelected.length === 1 ? "" : "s"} selected across ${routes.size} route${routes.size === 1 ? "" : "s"}.`));
    if (participation === "every") {
      status.append(make("p", "", readySelected.length > 1
        ? "Nexus will track a distinct required contribution from every selected agent; a missing reply cannot be reported as complete."
        : "Only one agent is selected, so this goal can produce only one required AI contribution."));
    } else {
      status.append(make("p", "", "The lead starts the work and may delegate or request an independent review when the saved task state calls for it."));
    }
  }
  $("longGoalParticipationWhy").textContent = participation === "every"
    ? "Best when you explicitly need evidence from every vendor or specialist. Each selected agent receives its own durable required task."
    : "Best when cost and speed matter more than hearing from everyone. All selected agents remain available for delegation and review.";
  const criteria = $("longGoalCriteria").value.split(/\r?\n/)
    .map((one) => one.trim()).filter(Boolean);
  const objective = $("longGoalText").value.trim();
  $("longGoalTextCount").textContent = `${$("longGoalText").value.length.toLocaleString()} of ${BOARD_TASK_CHARACTER_LIMIT.toLocaleString()} characters.`;
  $("longGoalStart").disabled = !project || authority?.can_run === false
    || !readySelected.length || !objective
    || criteria.length > LONG_GOAL_MAX_CUSTOM_CRITERIA;
  if (criteria.length > LONG_GOAL_MAX_CUSTOM_CRITERIA) {
    status.classList.remove("good");
    status.classList.add("problem");
    status.append(make("p", "", `Use at most ${LONG_GOAL_MAX_CUSTOM_CRITERIA} custom success checks.`));
  }
  saveLongGoalComposerDraft();
}

function renderLongGoalTeam(preferredIds = null, preferredLead = "") {
  const projectId = String($("longGoalProject").value || "");
  const assigned = new Set((theSwarmBoard().works_on || [])
    .filter((line) => line.project === projectId).map((line) => line.agent));
  const ready = (theSwarmBoard().agents || []).filter(
    (agent) => agent.ready === true && String(agent.who || ""));
  const selected = preferredIds instanceof Set
    ? preferredIds
    : assigned.size ? assigned : new Set(ready.map((agent) => agent.id));
  const list = $("longGoalAgents");
  list.replaceChildren();
  for (const agent of theSwarmBoard().agents || []) {
    const usable = agent.ready === true && String(agent.who || "");
    const label = make("label", `long-goal-agent${usable ? "" : " not-ready"}`);
    const tick = document.createElement("input");
    tick.type = "checkbox";
    tick.value = agent.id;
    tick.checked = usable && selected.has(agent.id);
    tick.disabled = !usable;
    tick.addEventListener("change", () => {
      renderLongGoalLead();
      renderLongGoalComposerReadiness();
    });
    label.append(tick, make("strong", "", agent.name || "Agent"));
    label.append(make("span", "", usable
      ? `${agent.who}${assigned.has(agent.id) ? " · assigned now" : " · will be assigned"}`
      : `${agent.who || "No provider selected"} · repair connection first`));
    list.append(label);
  }
  if (!theSwarmBoard().agents.length) {
    list.append(make("p", "hint", "No agents are on this board yet."));
  }
  renderLongGoalLead(preferredLead);
  renderLongGoalComposerReadiness();
}

function renderLongGoalLead(preferred = "") {
  const selected = new Set(selectedLongGoalAgentIds());
  const lead = $("longGoalLead");
  const wanted = selected.has(preferred) ? preferred
    : selected.has(lead.value) ? lead.value : "";
  lead.replaceChildren();
  for (const agent of theSwarmBoard().agents || []) {
    if (!selected.has(agent.id)) continue;
    const option = make("option", "", `${agent.name} (${agent.who})`);
    option.value = agent.id;
    option.selected = agent.id === wanted;
    lead.append(option);
  }
  lead.disabled = !lead.options.length;
}

function closeLongGoalComposer() {
  saveLongGoalComposerDraft();
  if ($("longGoalDialog").open) $("longGoalDialog").close();
  longGoalDialogInvoker?.focus?.({preventScroll: true});
}

function openLongGoalComposer() {
  const dialog = $("longGoalDialog");
  longGoalDialogInvoker = document.activeElement;
  const saved = savedLongGoalComposer()?.draft || {};
  const projects = availableLongGoalProjects();
  const projectSelect = $("longGoalProject");
  projectSelect.replaceChildren();
  if (!projects.length) {
    const option = make("option", "", "No local project is available");
    option.value = "";
    projectSelect.append(option);
  }
  const pickedProject = thePickedProject();
  const preferredProject = projects.find((one) => one.id === saved.project_id)?.id
    || projects.find((one) => one.id === pickedProject?.id)?.id || projects[0]?.id || "";
  for (const project of projects) {
    const option = make("option", "", `${project.name} — ${project.path}`);
    option.value = project.id;
    option.selected = project.id === preferredProject;
    projectSelect.append(option);
  }
  const project = projects.find((one) => one.id === preferredProject);
  $("longGoalText").value = typeof saved.objective === "string" && saved.objective.trim()
    ? saved.objective : (project?.tasks || []).join("\n\n");
  $("longGoalCriteria").value = Array.isArray(saved.success_criteria)
    ? saved.success_criteria.join("\n") : "";
  $("longGoalParticipation").value = saved.collaboration_mode === "adaptive"
    ? "adaptive" : "every";
  showLongGoalComposerError();
  if (!dialog.open) dialog.showModal();
  const preferredIds = saved.project_id === preferredProject && Array.isArray(saved.agent_ids)
    ? new Set(saved.agent_ids) : null;
  renderLongGoalTeam(preferredIds, String(saved.lead_id || ""));
  $("longGoalText").focus();
}

function editLongGoalProjectOnBoard() {
  const projectId = String($("longGoalProject").value || "");
  closeLongGoalComposer();
  if (projectId && theSwarmProject(projectId)) {
    pickSwarmBox("project", projectId);
    $("swarmPanel").scrollIntoView({behavior: "smooth", block: "nearest"});
    $("swarmTaskText").focus({preventScroll: true});
  } else {
    $("swarmAddProject").focus({preventScroll: true});
  }
}

async function checkLongGoalBoard() {
  closeLongGoalComposer();
  await refreshSwarm(false);
  $("swarmNotReadyTitle").scrollIntoView({behavior: "smooth", block: "center"});
  $("swarmRefresh").focus({preventScroll: true});
}

async function startLongGoalFromComposer(event) {
  event?.preventDefault?.();
  showLongGoalComposerError();
  const draft = longGoalComposerDraft();
  if (!draft.project_id) return showLongGoalComposerError("Choose an available project folder.");
  if (!draft.objective.trim()) return showLongGoalComposerError("Write the outcome that must be achieved.");
  if (!draft.agent_ids.length) return showLongGoalComposerError("Select at least one ready agent.");
  if (draft.success_criteria.length > LONG_GOAL_MAX_CUSTOM_CRITERIA) {
    return showLongGoalComposerError(`Use at most ${LONG_GOAL_MAX_CUSTOM_CRITERIA} custom success checks.`);
  }
  if (!draft.agent_ids.includes(draft.lead_id)) draft.lead_id = draft.agent_ids[0];
  const intent = longGoalIntent(draft);
  const previous = savedLongGoalComposer();
  const requestId = previous?.intent === intent && previous.request_id
    ? String(previous.request_id) : crypto.randomUUID();
  localStorage.setItem(LONG_GOAL_COMPOSER_KEY, JSON.stringify({
    schema_version: 1, draft, intent, request_id: requestId,
  }));
  const start = $("longGoalStart");
  start.disabled = true;
  start.textContent = "Saving team and starting…";
  try {
    const chosen = new Set(draft.agent_ids);
    const changed = await changeTheSwarmBoard((board) => {
      board.works_on = (board.works_on || []).filter(
        (line) => line.project !== draft.project_id || chosen.has(line.agent));
      const assigned = new Set(board.works_on
        .filter((line) => line.project === draft.project_id).map((line) => line.agent));
      for (const agentId of draft.agent_ids) {
        if (!assigned.has(agentId)) board.works_on.push({agent: agentId, project: draft.project_id});
      }
    }, "The selected goal team is assigned to this project.");
    if (!changed) throw new Error("Nexus could not save the selected team. Review the board and try again.");
    const goalSpec = {
      schema_version: 1,
      project_id: draft.project_id,
      objectives: [draft.objective.trim()],
      success_criteria: draft.success_criteria,
      lead_id: draft.lead_id,
      collaboration_mode: draft.collaboration_mode,
      participant_ids: draft.agent_ids,
    };
    const said = await request("/api/long-horizon/start-board", {
      method: "POST", body: JSON.stringify({request_id: requestId, goal: goalSpec}),
    });
    localStorage.removeItem(LONG_GOAL_COMPOSER_KEY);
    localStorage.removeItem(LONG_GOAL_REQUEST_KEY);
    longGoals = said.goals || [];
    if (longGoals[0]) {
      localStorage.setItem(LONG_GOAL_SELECTED_KEY, longGoals[0].goal_id);
      await refreshLongGoals(true);
    }
    $("longGoalDialog").close();
    $("swarmGoalWorkSaid").textContent =
      `Started a durable goal with ${draft.agent_ids.length} selected agent${draft.agent_ids.length === 1 ? "" : "s"}. Mission control shows each required contribution and saved checkpoint.`;
    $("missionControl")?.scrollIntoView({behavior: "smooth", block: "start"});
  } catch (error) {
    showLongGoalComposerError(String(error.message || error));
    $("swarmGoalWorkSaid").textContent = String(error.message || error);
  } finally {
    start.textContent = "Start durable goal";
    renderLongGoalComposerReadiness();
  }
}

function workOnEveryBoardGoal() {
  openLongGoalComposer();
}

async function missionControl(action, payload = {}) {
  if (!longGoal) return;
  const dispatchingActions = new Set([
    "resume", "retry", "reassign", "steer", "message", "request_review", "fork",
  ]);
  if (missionProviderSetupChanged() && dispatchingActions.has(action)) {
    const message = longGoal.provider_setup_status?.message
      || "The saved provider setup changed. Start a new goal with the current board setup.";
    showError(message);
    $("missionProviderSetupChanged").scrollIntoView({behavior: "smooth", block: "center"});
    $("missionProviderSetupReview").focus({preventScroll: true});
    return;
  }
  try {
    let forkRequestId = "";
    if (action === "fork") {
      try {
        const saved = JSON.parse(localStorage.getItem(LONG_GOAL_FORK_REQUEST_KEY) || "null");
        if (saved?.schema_version === 1 && saved.goal_id === longGoal.goal_id
            && typeof saved.request_id === "string" && saved.request_id) {
          forkRequestId = saved.request_id;
        }
      } catch (_) { /* A corrupt renderer draft is replaced below. */ }
      if (!forkRequestId) forkRequestId = crypto.randomUUID();
      localStorage.setItem(LONG_GOAL_FORK_REQUEST_KEY, JSON.stringify({
        schema_version: 1, goal_id: longGoal.goal_id, request_id: forkRequestId,
      }));
    }
    const said = await request("/api/long-horizon/control", {method: "POST", body: JSON.stringify({
      goal_id: longGoal.goal_id, action, payload,
      ...(action === "fork" ? {request_id: forkRequestId} : {}),
    })});
    if (action === "fork") {
      localStorage.removeItem(LONG_GOAL_FORK_REQUEST_KEY);
      localStorage.setItem(LONG_GOAL_SELECTED_KEY, said.goal.goal_id);
    }
    await refreshLongGoals(true);
  } catch (error) {
    // A fail-closed control (especially cancellation reconciliation) may have
    // committed a safer paused state while rejecting the requested action.
    // Refresh even when the HTTP result is an error so that recovery guidance
    // is visible without making the user reload the app.
    try { await refreshLongGoals(true); } catch (_) { /* Preserve the original control error. */ }
    showError(error.message);
  }
}

async function cancelLongGoal() {
  if (!longGoal || !window.confirm("Cancel this goal? Completed evidence remains recorded.")) return;
  await missionControl("cancel");
}

async function cancelRemainingBoardGoals() {
  const queue = swarmGoalQueue || await refreshBoardGoalQueue(false);
  if (!queue || !["queued", "paused"].includes(queue.status)) return;
  if (!window.confirm(
    `Cancel ${queue.total - queue.completed} remaining board goal(s)? Verified completed goals stay recorded.`
  )) return;
  try {
    const said = await request("/api/swarm/goal-queue/cancel", {
      method: "POST", body: JSON.stringify({queue_id: queue.queue_id}),
    });
    showBoardGoalQueue(said.queue);
  } catch (error) {
    showError(error.message);
  }
}

async function stopThemGoing() {
  try {
    if (!swarmBoardRunId) {
      $("swarmDoingSaid").textContent = "There is no exact active board run to stop.";
      return;
    }
    const said = await request("/api/swarm/stop", {
      method: "POST", body: JSON.stringify({run_id: swarmBoardRunId}),
    });
    swarmDoing = said.doing || null;
    renderWhatTheyAreDoing(said.doing);
    $("swarmDoingSaid").textContent = said.note;
  } catch (error) {
    showError(error.message);
    $("swarmDoingSaid").textContent = error.message;
  }
}

// One timer, however many times this is called. Two would ask twice as often
// and fight over the same list.
function watchWhatTheyAreDoing() {
  if (swarmWatching) return;
  swarmWatching = window.setInterval(async () => {
    try {
      const doing = await readSwarmBoardRun(swarmBoardRunId, swarmBoardCursor);
      swarmBoardCursor = Number((doing || {}).next_cursor ?? (doing || {}).cursor ?? swarmBoardCursor);
      swarmDoing = doing || null;
      renderWhatTheyAreDoing(doing);
      if (!doing || !doing.going) {
        swarmBoardRequestId = "";
        localStorage.removeItem("nexus.swarm.board-request");
        window.clearInterval(swarmWatching);
        swarmWatching = 0;
        // What they said is in their chats now, and a chat that was open while
        // its agent was asked is showing what it held before.
        for (const held of swarmChats) refreshTheChatFor(held.agent);
        refreshWhatTheySaidToEachOther();
        // And the board can be changed again, which the server decides.
        refreshSwarm(true);
      }
    } catch (error) {
      window.clearInterval(swarmWatching);
      swarmWatching = 0;
      $("swarmDoingSaid").textContent = error.message;
    }
  }, 1500);
}

// What the run last said it was doing, kept so the big chat can show what one
// agent has going on without asking for it again.
let swarmDoing = null;

function renderWhatTheyAreDoing(doing) {
  const list = $("swarmDoing");
  list.replaceChildren();
  swarmGoing = Boolean(doing && doing.going);
  setWhatCanBePressedInSwarm();
  if (!doing) {
    $("swarmDoingSaid").textContent = "";
    return;
  }
  $("swarmDoingSaid").textContent = doing.going
    ? `${doing.note} (${doing.done} of ${doing.of} done)`
    : doing.note;
  // Said out loud, not only shown as grey buttons. And taken back when it
  // finishes: left there, the line still said the board could not be changed
  // long after it could.
  if (doing.going) {
    sayInSwarm(whyTheBoardIsHeld());
  } else if ($("swarmSaid").textContent.startsWith("The board is going")) {
    sayInSwarm(whatTheBoardSays());
  }
  for (const turn of doing.turns) {
    const row = make("li", turn.state.replace(/ /g, "-"));
    row.append(make("strong", "", `${turn.name} on ${turn.where}`));
    const under = [turn.round, turn.state];
    // How long the answer was, which is the one thing about it worth showing
    // here. The answer itself is in that agent's own chat.
    if (turn.letters) under.push(`${turn.letters} letters`);
    if (turn.milliseconds) under.push(prettyTime(turn.milliseconds));
    row.append(make("p", "hint", under.join(" - ")));
    if ((turn.shown || []).length) {
      row.append(make("p", "hint", `shown what ${turn.shown.join(" and ")} said`));
    }
    if (turn.why_not) row.append(make("p", "hint", turn.why_not));
    list.append(row);
  }
}

// ---- every conversation, down the side ------------------------------------
//
// Two kinds, and they are different enough to be told apart at a glance: yours
// with one agent, which you can carry on, and what one agent passed to another
// during a run, which you can only read. A chat you can reach only by finding
// the box it belongs to is a chat nobody goes back to.

// What the agents passed to each other, kept here so the list down the side can
// be drawn without asking for the whole exchange every time the board is drawn.
let swarmWhatTheySaid = {notes: []};
// When the list is showing one pair only, and which.
let swarmOnlyThisPair = null;

function theChatsOnThisBoard() {
  const held = [];
  for (const agent of theSwarmBoard().agents) {
    held.push({
      kind: "yours",
      key: `you:${agent.id}`,
      who: `You and ${agent.name}`,
      last: agent.last_said || "nothing said yet",
      at: agent.last_said_at || "",
      howMany: agent.said || 0,
      open: () => openTheChatFor(agent.id),
    });
  }
  // One row per pair that really passed something, newest last, so a run that
  // has just happened reads down the page in the order it happened.
  const pairs = new Map();
  for (const note of swarmWhatTheySaid.notes || []) {
    const key = [note.said_by, note.shown_to].sort().join("|");
    const held = pairs.get(key) || {
      one: note.said_by, other: note.shown_to,
      names: [note.said_by_name, note.shown_to_name].sort(), howMany: 0,
      last: "", at: "",
    };
    held.howMany += 1;
    held.last = `${note.said_by_name}: ${note.text}`;
    held.at = note.at || held.at;
    pairs.set(key, held);
  }
  for (const [key, one] of pairs) {
    held.push({
      kind: "between",
      key: `between:${key}`,
      who: `${one.names[0]} and ${one.names[1]}`,
      last: one.last,
      at: one.at,
      howMany: one.howMany,
      open: () => showOnlyThisPair(one),
    });
  }
  return held;
}

function renderTheChatsOnThisBoard() {
  const list = $("swarmChats");
  list.replaceChildren();
  const held = theChatsOnThisBoard();
  if (!held.length) {
    list.append(make("li", "hint", "No agents on the board yet."));
    return;
  }
  for (const one of held) {
    const row = make("li");
    const pick = make("button", `swarm-chat-pick ${one.kind}`);
    pick.type = "button";
    pick.dataset.chat = one.key;
    if (one.kind === "yours" && theChatCardFor(one.key.slice(4))) {
      pick.classList.add("open");
    }
    pick.append(make("span", "swarm-chat-who",
      one.howMany ? `${one.who} (${one.howMany})` : one.who));
    pick.append(make("span", "swarm-chat-last", one.last));
    // Said out loud as one sentence. Going through the page by its buttons,
    // what is read out of a row of boxes is not something to leave to chance.
    pick.setAttribute("aria-label", one.kind === "yours"
      ? `Open your chat with ${one.who.replace("You and ", "")}, ${one.howMany} said`
      : `Read what ${one.who} passed to each other, ${one.howMany} answers`);
    pick.addEventListener("click", one.open);
    row.append(pick);
    list.append(row);
  }
}

function showOnlyThisPair(pair) {
  swarmOnlyThisPair = pair;
  $("swarmExchange").open = true;
  renderWhatTheySaidToEachOther(swarmWhatTheySaid);
  $("swarmExchange").scrollIntoView({block: "nearest"});
}

function showEveryPairAgain() {
  swarmOnlyThisPair = null;
  renderWhatTheySaidToEachOther(swarmWhatTheySaid);
}

// ---- signing in to Microsoft ----------------------------------------------
//
// Microsoft 365 Copilot has no command line, so there is nothing to hand the
// signing in off to, and Microsoft allows no key. What is left is a short code
// somebody pastes into a browser. The panel asks Microsoft for one, shows it,
// and then asks every few seconds whether it has been used yet. Nothing secret
// passes through this screen at any point.

let microsoftWaiting = null;
// Which attempt this window is watching, and how long it waits between asks.
// Two windows on the same panel can both press Sign in, and only the newer one
// is the one Microsoft is waiting on.
let microsoftAttempt = "";
let microsoftEvery = 5000;

function sayAboutMicrosoft(words) {
  const where = $("microsoftSays");
  if (where) where.textContent = words;
}

async function signInToMicrosoft() {
  const app = ($("microsoftApp").value || "").trim();
  if (!app) {
    sayAboutMicrosoft("Put in the Application (client) ID of the registered app first.");
    $("microsoftApp").focus();
    return;
  }
  stopWaitingOnMicrosoft();
  sayAboutMicrosoft("Asking Microsoft for a code...");
  try {
    const said = await request("/api/microsoft/sign-in", {
      method: "POST",
      body: JSON.stringify({app, organisation: ($("microsoftOrganisation").value || "").trim()}),
    });
    showTheMicrosoftCode(said);
  } catch (trouble) {
    sayAboutMicrosoft(String(trouble.message || trouble));
  }
}

function showTheMicrosoftCode(said) {
  $("microsoftCode").textContent = said.code;
  $("microsoftWhere").textContent = said.where;
  $("microsoftWhere").href = said.where;
  $("microsoftCodeBox").hidden = false;
  microsoftAttempt = said.attempt || "";
  sayAboutMicrosoft(
    "Open the address below, paste the code, and sign in with your work account. "
    + "This window will notice when you are done.");
  // Asked no faster than Microsoft said to ask. Faster and they start refusing.
  microsoftEvery = Math.max(2, Number(said.ask_again_after) || 5) * 1000;
  microsoftWaiting = setTimeout(askIfMicrosoftIsDoneYet, microsoftEvery);
}

function stopWaitingOnMicrosoft() {
  if (microsoftWaiting) clearTimeout(microsoftWaiting);
  microsoftWaiting = null;
}

async function askIfMicrosoftIsDoneYet() {
  try {
    // A body even though there is nothing to say in it: the panel will not
    // read a request that has none.
    const said = await request("/api/microsoft/sign-in/how-it-is-going", {
      method: "POST", body: JSON.stringify({attempt: microsoftAttempt}),
    });
    if (said.waiting) {
      // Microsoft asking to slow down and this window slowing down are two
      // different things. Kept at the old pace it gets stopped altogether, and
      // then somebody watches a code they already pasted never be noticed.
      if (said.wait_longer_by) microsoftEvery += Number(said.wait_longer_by) * 1000;
      microsoftWaiting = setTimeout(askIfMicrosoftIsDoneYet, microsoftEvery);
      return;
    }
    stopWaitingOnMicrosoft();
    $("microsoftCodeBox").hidden = true;
    if (said.done) {
      sayAboutMicrosoft("Signed in to Microsoft. This machine will stay signed in.");
      refreshTeam();
    } else {
      sayAboutMicrosoft(said.why);
    }
  } catch (trouble) {
    stopWaitingOnMicrosoft();
    $("microsoftCodeBox").hidden = true;
    sayAboutMicrosoft(String(trouble.message || trouble));
  }
}

async function signOutOfMicrosoft() {
  stopWaitingOnMicrosoft();
  $("microsoftCodeBox").hidden = true;
  try {
    await request("/api/microsoft/sign-out", {method: "POST", body: "{}"});
    sayAboutMicrosoft("The Microsoft sign-in on this machine has been forgotten.");
  } catch (trouble) {
    sayAboutMicrosoft(String(trouble.message || trouble));
  }
}

function wireUpMicrosoft() {
  const inIt = $("microsoftSignIn");
  if (!inIt) return;
  inIt.addEventListener("click", signInToMicrosoft);
  $("microsoftSignOut").addEventListener("click", signOutOfMicrosoft);
}


// ---- boards kept under a name ---------------------------------------------
//
// The board you are working on was always written down and always came back -
// one board, the same one, whatever you were working on. That is fine until you
// want two, and then the second one means taking the first apart and building
// it again from memory on Monday.

const MAX_SAVED_BOARD_IMPORT_BYTES = 768_000_000;
let swarmKept = [];
let swarmKeptProblems = [];

function acceptKeptInventory(said, keepOld = false) {
  swarmKept = said.kept || (keepOld ? swarmKept : []);
  swarmKeptProblems = said.kept_problems || [];
}

function renderTheKeptBoards() {
  const list = $("swarmKept");
  if (!list) return;
  list.replaceChildren();
  const problems = $("swarmKeptProblems");
  problems.hidden = !swarmKeptProblems.length;
  problems.textContent = swarmKeptProblems.length
    ? `${swarmKeptProblems.length} saved board file${swarmKeptProblems.length === 1 ? "" : "s"} `
      + `could not be read. The file${swarmKeptProblems.length === 1 ? " is" : "s are"} still on disk:\n`
      + swarmKeptProblems.join("\n")
    : "";
  if (!swarmKept.length) {
    list.append(make("li", "hint", "None saved yet."));
    return;
  }
  for (const one of swarmKept) {
    const row = make("li");
    const open = make("button", "swarm-kept-pick");
    open.type = "button";
    open.setAttribute("aria-label", `Open the saved board called ${one.name}`);
    if (one.active) {
      open.setAttribute("aria-current", "true");
      open.setAttribute("aria-label", `${one.name}, the board currently open`);
    }
    const held = Boolean(whyTheBoardIsHeld());
    open.disabled = held;
    if (held) open.title = `This board is safe, but cannot be opened yet. ${whyTheBoardIsHeld()}`;
    open.append(make("span", "", one.name));
    if (one.active) open.append(make("span", "swarm-kept-active", "Open now · returns next time"));
    open.append(make("span", "swarm-kept-when",
      `${one.agents} agent${one.agents === 1 ? "" : "s"}, `
      + `${one.projects} project${one.projects === 1 ? "" : "s"}`));
    open.addEventListener("click", () => openTheKeptBoard(one.name));
    row.append(open);
    const save = make("button", "swarm-icon-button", "Export");
    save.type = "button";
    save.setAttribute("aria-label", `Export the saved board called ${one.name} as JSON`);
    save.addEventListener("click", () => exportKeptBoard(one.name));
    row.append(save);
    const drop = make("button", "swarm-icon-button", "Delete");
    drop.type = "button";
    drop.setAttribute("aria-label", `Delete the saved board called ${one.name}`);
    drop.disabled = false;
    drop.addEventListener("click", () => forgetTheKeptBoard(one.name));
    row.append(drop);
    list.append(row);
  }
}

async function keepThisBoard() {
  const name = await askForOneLine(
    "Save this board", "What should this arrangement be called?", "");
  if (name === null) return;
  try {
    const said = await request("/api/swarm/keep", {
      method: "POST", body: JSON.stringify({name}),
    });
    acceptKeptInventory(said);
    renderTheKeptBoards();
    sayInSwarm(`Saved this board as ${said.name}.`);
  } catch (trouble) {
    sayInSwarm(String(trouble.message || trouble));
  }
}

async function openTheKeptBoard(name) {
  // Asked first, because what is on the board now goes. Somebody who has spent
  // ten minutes arranging it should not lose it to one press.
  if (!window.confirm(
      `Open the saved board "${name}"? What is on the board now is replaced. `
      + "Save it first if you want it back.")) {
    return;
  }
  try {
    const said = await request("/api/swarm/open-kept", {
      method: "POST", body: JSON.stringify({name}),
    });
    swarmSaid = said;
    acceptKeptInventory(said);
    keepTheSwarmPick();
    renderSwarmBoard();
    renderSwarmNotReady();
    renderSwarmPanel();
    renderTheKeptBoards();
    renderTheChatsOnThisBoard();
    void Promise.all(swarmChats.map(
      (held) => loadConversationsFor(held.agent, false)
    ));
    sayInSwarm(`Opened the board saved as ${name}.`);
    if (said.provider_status_stale) {
      // The board is already open and interactive. Discovering installed
      // provider tools may take many seconds, so decorate readiness in a
      // separate request instead of making the Open button appear stuck.
      void refreshSwarm(true);
    }
  } catch (trouble) {
    sayInSwarm(String(trouble.message || trouble));
  }
}

async function forgetTheKeptBoard(name) {
  if (!window.confirm(`Delete the saved board "${name}"? This cannot be undone.`)) return;
  try {
    const said = await request("/api/swarm/forget-kept", {
      method: "POST", body: JSON.stringify({name}),
    });
    acceptKeptInventory(said);
    renderTheKeptBoards();
    sayInSwarm(`Deleted the saved board ${name}.`);
  } catch (trouble) {
    sayInSwarm(String(trouble.message || trouble));
  }
}

// ---- models running on this machine ---------------------------------------
//
// The settings have taken an Ollama address for as long as there have been
// settings. What was missing was finding one: somebody with Ollama running and
// a model pulled still had to know the port and the model's name and write both
// into a file by hand, which is a strange thing to ask for the one route that
// needs nobody's permission.

let localModels = [];

function renderLocalModels() {
  const list = $("localModels");
  if (!list) return;
  list.replaceChildren();
  if (!localModels.length) {
    list.append(make("li", "hint", "Nothing looked for yet. Press Look again."));
    return;
  }
  for (const one of localModels) {
    const row = make("li", `local-model-one ${one.running ? "running" : "not-running"}`);
    row.append(make("strong", "", one.label));
    row.append(make("p", "hint", one.running
      ? `Running at ${one.endpoint}.`
      : (one.why_not || "Not running.")));
    if (!one.running || !one.models.length) {
      row.append(make("p", "hint", one.how_to_get_it));
    }
    if (one.models.length) {
      const names = make("div", "local-model-names");
      for (const model of one.models) {
        const use = make("button", "", model);
        use.type = "button";
        use.setAttribute("aria-label", `Use ${model} from ${one.label}`);
        use.addEventListener("click", () => useThisLocalModel(one, model, use));
        names.append(use);
      }
      row.append(names);
    }
    list.append(row);
  }
}

async function useThisLocalModel(server, model, button) {
  button.disabled = true;
  try {
    const said = await request("/api/local-models/use", {
      method: "POST", body: JSON.stringify({server: server.id, model}),
    });
    say(`${model} is set up, as the route called ${said.route}.`);
    await refreshTeam();
  } catch (trouble) {
    say(String(trouble.message || trouble));
  } finally {
    button.disabled = false;
  }
}


// ---- the page they share --------------------------------------------------
//
// Two agents talking to each other in a chat is two agents taking turns at a
// place where speaking is exclusive - one of them is always being cut off. On a
// page there is no such thing as an interruption: you read it, you add to the
// bottom, and your words sit under somebody else's without touching them.

let thePage = null;
let thePageFolder = "";
const THE_PAGE_WINDOW = 20;

function sayAboutThePage(words) {
  const where = $("thePageSaid");
  if (where) where.textContent = words;
}

function whichProjectsHavePages() {
  return (theSwarmBoard().projects || []).filter((one) => one.path);
}

function renderWhichPage() {
  const pick = $("thePageWhich");
  if (!pick) return;
  const projects = whichProjectsHavePages();
  const was = thePageFolder || pick.value;
  pick.replaceChildren();
  for (const one of projects) {
    const choice = make("option", "", one.name);
    choice.value = one.path;
    pick.append(choice);
  }
  if (!projects.length) {
    const none = make("option", "", "no project folders on the board yet");
    none.value = "";
    pick.append(none);
  }
  if (was && projects.some((one) => one.path === was)) pick.value = was;
  thePageFolder = pick.value || "";
}

async function refreshThePage() {
  if (!$("thePage")) return;
  renderWhichPage();
  if (!thePageFolder) {
    thePage = null;
    renderThePage();
    return;
  }
  const named = whichProjectsHavePages().find((one) => one.path === thePageFolder);
  try {
    thePage = await request("/api/swarm/the-page", {
      method: "POST",
      body: JSON.stringify({
        folder: thePageFolder,
        name: named ? named.name : "",
        limit: THE_PAGE_WINDOW,
      }),
    });
    renderThePage();
  } catch (trouble) {
    sayAboutThePage(String(trouble.message || trouble));
  }
}

function renderThePage() {
  const list = $("thePageList");
  if (!list) return;
  list.replaceChildren();
  if (!thePage) {
    list.append(make("li", "hint", "Pick a project folder to see its page."));
    $("thePageStands").value = "";
    renderDisclosedTextCount(
      "thePageStands", "thePageStandsCount",
      SHARED_PAGE_CHARACTER_LIMIT, "where it stands");
    return;
  }
  $("thePageStands").value = thePage.where_it_stands || "";
  renderDisclosedTextCount(
    "thePageStands", "thePageStandsCount",
    SHARED_PAGE_CHARACTER_LIMIT, "where it stands");
  if (!thePage.parts.length) {
    list.append(make("li", "hint",
      "Nothing on this page yet. It fills in as the agents work, and you can add to it too."));
  }
  if (thePage.window?.has_older) {
    const olderRow = make("li", "hint");
    const older = make("button", "", "Load 20 older parts");
    older.type = "button";
    older.addEventListener("click", loadOlderPageParts);
    olderRow.append(older, document.createTextNode(
      ` Showing ${thePage.parts.length.toLocaleString()} of ${Number(thePage.how_many || 0).toLocaleString()} parts.`
    ));
    list.append(olderRow);
  }
  for (const one of thePage.parts) {
    const row = make("li", "swarm-exchange-one");
    row.append(make("strong", "", `${one.number}. ${one.who}`));
    const about = [one.what_they_were_doing, one.at].filter(Boolean).join(" | ");
    row.append(make("p", "hint", about));
    const text = make("p", "swarm-exchange-text", one.text);
    row.append(text);
    if (Number(one.text_characters || 0) > String(one.text_preview || "").length) {
      const full = make("button", "");
      full.type = "button";
      full.textContent = one.text_complete
        ? "Collapse to the 20,000-character preview"
        : `Show complete ${Number(one.text_characters).toLocaleString()}-character part`;
      full.addEventListener("click", () => toggleCompletePagePart(one, full));
      row.append(full);
    }
    list.append(row);
  }
  // What a person wants to know first: how much is on it and who wrote last.
  const bits = [`${thePage.how_many} part${thePage.how_many === 1 ? "" : "s"}`];
  if (thePage.last_was) bits.push(`the last was ${thePage.last_was}, ${thePage.last_at}`);
  if (thePage.put_away_before) {
    bits.push(`${thePage.put_away_before} older page${
      thePage.put_away_before === 1 ? " was" : "s were"} put away`);
  }
  sayAboutThePage(thePage.trouble ? `${bits.join(". ")}. ${thePage.trouble}` : `${bits.join(". ")}.`);
}

async function toggleCompletePagePart(one, button) {
  if (one.text_complete) {
    one.text = one.text_preview || "";
    one.text_complete = false;
    renderThePage();
    return;
  }
  if (!thePageFolder) return;
  button.disabled = true;
  const named = whichProjectsHavePages().find((project) => project.path === thePageFolder);
  try {
    const complete = await request("/api/swarm/page-part", {
      method: "POST",
      body: JSON.stringify({
        folder: thePageFolder,
        name: named ? named.name : "",
        number: one.number,
      }),
    });
    Object.assign(one, complete);
    renderThePage();
  } catch (trouble) {
    sayAboutThePage(String(trouble.message || trouble));
    button.disabled = false;
  }
}

async function loadOlderPageParts(event) {
  if (!thePageFolder || !thePage?.window?.has_older) return;
  const button = event?.currentTarget;
  if (button) button.disabled = true;
  const named = whichProjectsHavePages().find((one) => one.path === thePageFolder);
  try {
    const older = await request("/api/swarm/the-page", {
      method: "POST",
      body: JSON.stringify({
        folder: thePageFolder,
        name: named ? named.name : "",
        before: thePage.window.next_before,
        limit: THE_PAGE_WINDOW,
      }),
    });
    const already = new Set((thePage.parts || []).map((one) => Number(one.number)));
    const additions = (older.parts || []).filter((one) => !already.has(Number(one.number)));
    thePage.parts = [...additions, ...(thePage.parts || [])];
    thePage.window.first = older.window?.first || thePage.window.first;
    thePage.window.next_before = older.window?.next_before || 0;
    thePage.window.has_older = Boolean(older.window?.has_older);
    renderThePage();
  } catch (trouble) {
    sayAboutThePage(String(trouble.message || trouble));
    if (button) button.disabled = false;
  }
}

async function saveWhereItStands() {
  if (!thePageFolder) return;
  const problem = disclosedTextProblem(
    "thePageStands", "thePageStandsCount",
    SHARED_PAGE_CHARACTER_LIMIT, "where it stands");
  if (problem) { sayAboutThePage(problem); $("thePageStands").focus(); return; }
  try {
    await request("/api/swarm/where-it-stands", {
      method: "POST",
      body: JSON.stringify({
        folder: thePageFolder,
        text: $("thePageStands").value,
        // What it was when this window read it, so two windows cannot write
        // over each other without one of them being told.
        instead_of: thePage ? thePage.where_it_stands_now : "",
      }),
    });
    await refreshThePage();
    sayAboutThePage("Saved. Every agent working on this project reads that.");
  } catch (trouble) {
    sayAboutThePage(String(trouble.message || trouble));
  }
}

async function addSomethingOfMyOwn() {
  if (!thePageFolder) return;
  const said = await askForLongPageText(
    "Add to the page", "This goes on the page under your name, and every agent reads it.", "");
  if (said === null || !said.trim()) return;
  try {
    await request("/api/swarm/add-to-the-page", {
      method: "POST",
      body: JSON.stringify({
        folder: thePageFolder, who: "You", text: said,
        after: thePage ? thePage.up_to : 0,
      }),
    });
    await refreshThePage();
  } catch (trouble) {
    sayAboutThePage(String(trouble.message || trouble));
  }
}

async function putThePageAway() {
  if (!thePageFolder) return;
  if (!window.confirm(
      "Start a fresh page? The one you have now is kept, in a folder called before.")) {
    return;
  }
  try {
    const said = await request("/api/swarm/put-the-page-away", {
      method: "POST", body: JSON.stringify({folder: thePageFolder}),
    });
    await refreshThePage();
    sayAboutThePage(said.put_away
      ? "Put away. This page starts empty, and the old one is kept."
      : String(said.why || ""));
  } catch (trouble) {
    sayAboutThePage(String(trouble.message || trouble));
  }
}

// ---- connecting an assistant ----------------------------------------------
//
// Somebody had Claude installed and signed in, an agent set to use it, and the
// board still said not ready - because nothing in the settings pointed at it by
// name, and the only way to say so was a settings file or a terminal. They had
// to ask somebody else for help with their own machine.

const GEMINI_CLI_CLOUD_PROJECT_HELP =
  "https://geminicli.com/docs/get-started/authentication/#set-your-google-cloud-project";

async function connectThisAssistant(kind, button) {
  // Google will not answer a work account until it knows which Cloud project to
  // bill the work to. Asked once here, rather than letting somebody connect it,
  // watch it refuse, and go hunting for a setting.
  let googleProject = "";
  if (kind === "gemini-cli") {
    const said = await askForOneLine(
      "Connect Gemini",
      "If yours is a work Google account, Google needs the Cloud project id to "
      + "bill the work to. A personal account needs none - leave this empty.",
      "",
      {
        label: "WHAT IS THIS? (external link)",
        href: GEMINI_CLI_CLOUD_PROJECT_HELP,
      });
    if (said === null) return;
    googleProject = said.trim();
  }
  const was = button.textContent;
  button.disabled = true;
  button.textContent = "Connecting...";
  try {
    const said = await request("/api/team/connect", {
      method: "POST", body: JSON.stringify({kind, google_project: googleProject}),
    });
    if (said.needs_your_say) {
      sayInSwarm(`${said.route} was written down. ${said.note}`);
    } else if (said.authentication === "signed-out" && said.can_login) {
      sayInSwarm(`${said.route} is installed and connected to the board, but its command line needs sign-in.`);
      if (window.confirm(
        `${said.route} is installed, but its command line is signed out. Open its own sign-in window now?`)) {
        await signInThisAssistant(said.route || kind);
      }
    } else if (said.authentication === "signed-in") {
      sayInSwarm(`${said.route} is connected and its command-line sign-in is ready.`);
    } else {
      sayInSwarm(`${said.route} is connected. Its command line will verify the subscription on the first message.`);
    }
    await refreshSwarm(true);
    return true;
  } catch (trouble) {
    sayInSwarm(String(trouble.message || trouble));
    return false;
  } finally {
    button.disabled = false;
    button.textContent = was;
  }
}


// ---- the tray of chats, and one chat opened big ---------------------------
//
// A board with five agents on it is five conversations. They used to be small
// cards floating on the board itself, so two open at once covered the board
// they were about, and a fifth was somewhere off the side.
//
// Now every open chat is a button along the bottom, the way a taskbar works.
// One is opened big over the board, the rest wait in the tray. Hovering the
// face on a tray button draws a line to that agent's box, because five chats
// called "chat with an agent" are five chats nobody can tell apart.

// Which chat is open big, by agent id. Empty means none, and the board is
// showing.
let theBigOne = "";
let theBigChatInvoker = null;
let theBigChatInvokerAgent = "";
let theBigChatRenderIdentity = "";
const theBigChatRenderSignatures = new Map();
const BIG_CHAT_LAYOUT_KEY = "nexus-big-chat-layout-v1";
const BIG_CHAT_LAYOUT_DEFAULTS = Object.freeze({
  width: null,
  height: null,
  sidebar: 270,
  activity: 290,
  destination: 180,
  composer: 320,
});
let theBigChatLayout = readTheBigChatLayout();
let theBigChatResize = null;

function aSavedBigChatSize(value, otherwise = null) {
  if (value === null || value === undefined || value === "") return otherwise;
  const number = Number(value);
  return Number.isFinite(number) && number >= 0 && number <= 10000 ? number : otherwise;
}

function unusedKeptBoardName(wanted) {
  let candidate = `${wanted} copy`;
  let number = 2;
  const used = new Set(swarmKept.map((one) => one.name.toLowerCase()));
  while (used.has(candidate.toLowerCase())) {
    candidate = `${wanted} copy ${number}`;
    number += 1;
  }
  return candidate;
}

async function importKeptBoard(file) {
  if (!file) return;
  try {
    if (file.size > MAX_SAVED_BOARD_IMPORT_BYTES) {
      throw new Error("That saved-board JSON file is larger than 768 MB. Nothing was imported.");
    }
    let written;
    try {
      written = new TextDecoder("utf-8", {fatal: true}).decode(
        await file.arrayBuffer()
      );
    } catch (_) {
      throw new Error("That saved-board file is not valid UTF-8. Nothing was imported.");
    }
    let document;
    try { document = JSON.parse(written); }
    catch (_) { throw new Error("That file is not valid JSON. Nothing was imported."); }
    const original = String(document?.name || file.name.replace(/\.json$/i, "") || "Imported board");
    let name = original;
    if (swarmKept.some((one) => one.name.toLowerCase() === original.toLowerCase())) {
      name = await askForOneLine(
        "Import as a new saved board",
        `“${original}” is already saved. Choose a name for the imported copy.`,
        unusedKeptBoardName(original),
      );
      if (!name) return;
    }
    const said = await request("/api/swarm/import-kept", {
      method: "POST", body: JSON.stringify({document, name}),
    });
    acceptKeptInventory(said);
    renderTheKeptBoards();
    sayInSwarm(`Imported and saved ${said.name}. It has not replaced the board on screen.`);
  } catch (trouble) {
    sayInSwarm(String(trouble.message || trouble));
  } finally {
    $("swarmImportFile").value = "";
  }
}

async function exportKeptBoard(name) {
  try {
    const said = await request(
      `/api/swarm/export-kept?name=${encodeURIComponent(name)}`
    );
    const written = JSON.stringify(said.document, null, 2) + "\n";
    if (window.harnessDesktop?.saveLargeJsonFile) {
      const saved = await window.harnessDesktop.saveLargeJsonFile(
        said.filename || "nexus-saved-board.json", written
      );
      sayInSwarm(saved?.saved
        ? `Exported ${name} as ${saved.filename || "JSON"}.`
        : "Export cancelled; nothing was written.");
      return;
    }
    const blob = new Blob([written], {
      type: "application/json",
    });
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = said.filename || "nexus-saved-board.json";
    link.click();
    URL.revokeObjectURL(link.href);
    sayInSwarm(`Exported ${name} as JSON.`);
  } catch (trouble) {
    sayInSwarm(String(trouble.message || trouble));
  }
}

function readTheBigChatLayout() {
  try {
    const saved = JSON.parse(window.localStorage.getItem(BIG_CHAT_LAYOUT_KEY) || "null");
    if (!saved || typeof saved !== "object") return {...BIG_CHAT_LAYOUT_DEFAULTS};
    return {
      width: aSavedBigChatSize(saved.width),
      height: aSavedBigChatSize(saved.height),
      sidebar: aSavedBigChatSize(saved.sidebar, BIG_CHAT_LAYOUT_DEFAULTS.sidebar),
      activity: aSavedBigChatSize(saved.activity, BIG_CHAT_LAYOUT_DEFAULTS.activity),
      destination: aSavedBigChatSize(saved.destination, BIG_CHAT_LAYOUT_DEFAULTS.destination),
      composer: aSavedBigChatSize(saved.composer, BIG_CHAT_LAYOUT_DEFAULTS.composer),
    };
  } catch (_) {
    return {...BIG_CHAT_LAYOUT_DEFAULTS};
  }
}

function saveTheBigChatLayout() {
  try { window.localStorage.setItem(BIG_CHAT_LAYOUT_KEY, JSON.stringify(theBigChatLayout)); }
  catch (_) { /* The layout still works for this window when storage is unavailable. */ }
}

function boundedBigChatSize(value, least, most) {
  return Math.max(Math.min(least, most), Math.min(most, Number(value) || least));
}

function theBigChatSheetBounds() {
  const overlay = $("theBigChat");
  const style = window.getComputedStyle(overlay);
  const horizontal = parseFloat(style.paddingLeft) + parseFloat(style.paddingRight);
  const vertical = parseFloat(style.paddingTop) + parseFloat(style.paddingBottom);
  const mostWidth = Math.max(320, overlay.clientWidth - horizontal);
  const mostHeight = Math.max(320, overlay.clientHeight - vertical);
  return {
    minWidth: Math.min(640, mostWidth),
    maxWidth: mostWidth,
    minHeight: Math.min(460, mostHeight),
    maxHeight: mostHeight,
  };
}

function setBigChatSeparatorValue(id, value, least, most) {
  const separator = $(id);
  if (!separator) return;
  separator.setAttribute("aria-valuemin", String(Math.round(least)));
  separator.setAttribute("aria-valuemax", String(Math.round(most)));
  separator.setAttribute("aria-valuenow", String(Math.round(value)));
}

function applyTheBigChatLayout() {
  const sheet = document.querySelector(".the-big-chat-sheet");
  if (!sheet) return;
  const bounds = theBigChatSheetBounds();
  sheet.style.width = theBigChatLayout.width == null ? ""
    : `${boundedBigChatSize(theBigChatLayout.width, bounds.minWidth, bounds.maxWidth)}px`;
  sheet.style.height = theBigChatLayout.height == null ? ""
    : `${boundedBigChatSize(theBigChatLayout.height, bounds.minHeight, bounds.maxHeight)}px`;

  const sheetWidth = sheet.getBoundingClientRect().width;
  const sidebarMax = Math.max(160, sheetWidth - 430);
  const sidebar = boundedBigChatSize(theBigChatLayout.sidebar, 160, sidebarMax);
  sheet.style.setProperty("--big-chat-sidebar-width", `${sidebar}px`);
  setBigChatSeparatorValue("theBigChatSidebarResize", sidebar, 160, sidebarMax);

  const main = document.querySelector(".the-big-chat-main");
  const mainWidth = main?.getBoundingClientRect().width || 620;
  const activityMax = Math.max(160, mainWidth - 280);
  const activity = boundedBigChatSize(theBigChatLayout.activity, 160, activityMax);
  sheet.style.setProperty("--big-chat-activity-width", `${activity}px`);
  setBigChatSeparatorValue("theBigChatActivityResize", activity, 160, activityMax);

  const mainHeight = main?.getBoundingClientRect().height || 560;
  const shared = Math.max(222, mainHeight - 24 - 96);
  const destinationMax = Math.max(72, shared - 150);
  const destination = boundedBigChatSize(theBigChatLayout.destination, 72, destinationMax);
  const composerMax = Math.max(150, shared - destination);
  const composer = boundedBigChatSize(theBigChatLayout.composer, 150, composerMax);
  sheet.style.setProperty("--big-chat-destination-height", `${destination}px`);
  sheet.style.setProperty("--big-chat-composer-height", `${composer}px`);
  setBigChatSeparatorValue("theBigChatDestinationResize", destination, 72, destinationMax);
  setBigChatSeparatorValue("theBigChatComposerResize", composer, 150, composerMax);
}

function resetTheBigChatLayout(part = "all") {
  if (part === "all" || part === "window") {
    theBigChatLayout.width = null;
    theBigChatLayout.height = null;
  }
  for (const name of ["sidebar", "activity", "destination", "composer"]) {
    if (part === "all" || part === name) theBigChatLayout[name] = BIG_CHAT_LAYOUT_DEFAULTS[name];
  }
  applyTheBigChatLayout();
  saveTheBigChatLayout();
  if (theBigOne) {
    $("theBigChatSaidBack").textContent = part === "all"
      ? "Default chat sizes restored." : "Default pane size restored.";
  }
}

function beginTheBigChatResize(kind, event) {
  if (event.button !== 0) return;
  const sheet = document.querySelector(".the-big-chat-sheet");
  const destination = $("theBigChatDestination");
  const bottom = document.querySelector(".the-big-chat-bottom");
  const sidebar = document.querySelector(".the-big-chat-conversations");
  const activity = document.querySelector(".the-big-chat-doing");
  if (!sheet || !destination || !bottom || !sidebar || !activity) return;
  event.preventDefault();
  event.currentTarget.setPointerCapture?.(event.pointerId);
  theBigChatResize = {
    kind,
    x: event.clientX,
    y: event.clientY,
    width: sheet.getBoundingClientRect().width,
    height: sheet.getBoundingClientRect().height,
    sidebar: sidebar.getBoundingClientRect().width,
    activity: activity.getBoundingClientRect().width,
    destination: destination.getBoundingClientRect().height,
    composer: bottom.getBoundingClientRect().height,
  };
  const axis = kind === "window" ? "both"
    : ["sidebar", "activity"].includes(kind) ? "vertical" : "horizontal";
  document.body.classList.add("big-chat-resizing");
  document.body.dataset.bigChatResizeAxis = axis;
}

function moveTheBigChatResize(event) {
  if (!theBigChatResize) return;
  const dx = event.clientX - theBigChatResize.x;
  const dy = event.clientY - theBigChatResize.y;
  if (theBigChatResize.kind === "window") {
    // The sheet is centred, so both edges move by half of a width change.
    theBigChatLayout.width = theBigChatResize.width + (dx * 2);
    theBigChatLayout.height = theBigChatResize.height + (dy * 2);
  } else if (theBigChatResize.kind === "sidebar") {
    theBigChatLayout.sidebar = theBigChatResize.sidebar + dx;
  } else if (theBigChatResize.kind === "activity") {
    theBigChatLayout.activity = theBigChatResize.activity - dx;
  } else if (theBigChatResize.kind === "destination") {
    theBigChatLayout.destination = theBigChatResize.destination + dy;
  } else if (theBigChatResize.kind === "composer") {
    theBigChatLayout.composer = theBigChatResize.composer - dy;
  }
  applyTheBigChatLayout();
}

function finishTheBigChatResize() {
  if (!theBigChatResize) return;
  theBigChatResize = null;
  document.body.classList.remove("big-chat-resizing");
  delete document.body.dataset.bigChatResizeAxis;
  saveTheBigChatLayout();
}

function resizeTheBigChatWithKeys(kind, event) {
  if (event.key === "Home") {
    event.preventDefault();
    resetTheBigChatLayout(kind);
    return;
  }
  const step = event.shiftKey ? 48 : 16;
  let changed = false;
  if (kind === "window" && ["ArrowLeft", "ArrowRight"].includes(event.key)) {
    const current = document.querySelector(".the-big-chat-sheet").getBoundingClientRect().width;
    theBigChatLayout.width = current + (event.key === "ArrowRight" ? step : -step);
    changed = true;
  } else if (kind === "window" && ["ArrowUp", "ArrowDown"].includes(event.key)) {
    const current = document.querySelector(".the-big-chat-sheet").getBoundingClientRect().height;
    theBigChatLayout.height = current + (event.key === "ArrowDown" ? step : -step);
    changed = true;
  } else if (["sidebar", "activity"].includes(kind)
      && ["ArrowLeft", "ArrowRight"].includes(event.key)) {
    const direction = event.key === "ArrowRight" ? step : -step;
    theBigChatLayout[kind] += kind === "activity" ? -direction : direction;
    changed = true;
  } else if (["destination", "composer"].includes(kind)
      && ["ArrowUp", "ArrowDown"].includes(event.key)) {
    const direction = event.key === "ArrowDown" ? step : -step;
    theBigChatLayout[kind] += kind === "composer" ? -direction : direction;
    changed = true;
  }
  if (!changed) return;
  event.preventDefault();
  applyTheBigChatLayout();
  saveTheBigChatLayout();
}

function wireTheBigChatResizer(id, kind) {
  const control = $(id);
  control.addEventListener("pointerdown", (event) => beginTheBigChatResize(kind, event));
  control.addEventListener("keydown", (event) => resizeTheBigChatWithKeys(kind, event));
  control.addEventListener("dblclick", () => resetTheBigChatLayout(kind));
}

function rememberTheBigChatComposer() {
  const box = $("theBigChatBox");
  if (!box || !theBigChatComposerKey) return null;
  const state = {
    value: box.value,
    start: box.selectionStart,
    end: box.selectionEnd,
    direction: box.selectionDirection || "none",
  };
  theBigChatComposerDrafts.set(theBigChatComposerKey, state);
  countWhatIsTypedInBigChat();
  return state;
}

function syncTheBigChatComposer() {
  const box = $("theBigChatBox");
  if (!box || !theBigOne) return;
  const nextKey = swarmChatKey(theBigOne);
  if (nextKey === theBigChatComposerKey) return;
  const wasFocused = document.activeElement === box;
  const previousKey = theBigChatComposerKey;
  rememberTheBigChatComposer();
  theBigChatComposerKey = nextKey;
  // First open begins under a temporary "legacy" identity while the saved
  // conversation list is loading. That metadata arrival is not a user switch:
  // carry the just-started draft into the real chat instead of blanking it.
  const legacyKey = `${theBigOne}:legacy`;
  const state = theBigChatComposerDrafts.get(nextKey)
    || (previousKey === legacyKey ? theBigChatComposerDrafts.get(previousKey) : null);
  if (state && previousKey === legacyKey && !theBigChatComposerDrafts.has(nextKey)) {
    theBigChatComposerDrafts.set(nextKey, state);
    theBigChatComposerDrafts.delete(previousKey);
  }
  box.value = state?.value || "";
  if (state) {
    box.setSelectionRange(state.start, state.end, state.direction);
  } else {
    box.setSelectionRange(box.value.length, box.value.length);
  }
  if (wasFocused) box.focus({preventScroll: true});
}

function keepTheBigChatComposerInHand(changeParents) {
  const box = $("theBigChatBox");
  const wasFocused = Boolean(box && document.activeElement === box);
  const state = rememberTheBigChatComposer();
  changeParents();
  if (!box || !wasFocused || $("theBigChat").hidden) return;
  box.focus({preventScroll: true});
  if (state) box.setSelectionRange(state.start, state.end, state.direction);
}

function bigChatPartChanged(part, value) {
  const signature = JSON.stringify(value);
  if (theBigChatRenderSignatures.get(part) === signature) return false;
  theBigChatRenderSignatures.set(part, signature);
  return true;
}

function resetTheBigChatRenderCache(identity) {
  if (theBigChatRenderIdentity === identity) return;
  theBigChatRenderIdentity = identity;
  theBigChatRenderSignatures.clear();
}

function everyOpenChat() {
  const board = theSwarmBoard();
  return swarmChats
    .map((one) => (board.agents || []).find((agent) => agent.id === one.agent))
    .filter(Boolean);
}

function renderTheChatTray() {
  const tray = $("theChatTray");
  if (!tray) return;
  const open = everyOpenChat();
  tray.hidden = !open.length;
  document.body.classList.toggle("has-a-tray", open.length > 0);
  const list = $("theChatTrayList");
  list.replaceChildren();
  for (const one of open) {
    const row = make("li");
    const pick = make("button", `the-chat-tray-one ${theBigOne === one.id ? "open" : ""}`);
    pick.type = "button";
    pick.dataset.chatTray = one.id;
    pick.setAttribute("aria-label",
      `Open the chat with ${one.name} big. It uses ${one.who || "no assistant yet"}.`);
    const face = anAgentFace(one, "the-chat-tray-face", 18);
    // The face is what points at the board. Hovering it says which of five
    // agents this is without reading anything.
    face.addEventListener("pointerenter", () => pointAtTheBox(one.id));
    face.addEventListener("pointerleave", stopPointing);
    pick.append(face);
    const destination = chatDestinationFor(one);
    pick.append(make("span", "",
      `${one.name} — Nexus chat via ${destination.provider_label || one.who || "nobody yet"}`));
    pick.addEventListener("focus", () => pointAtTheBox(one.id));
    pick.addEventListener("blur", stopPointing);
    pick.dataset.bigChatInvoker = one.id;
    pick.addEventListener("click", () => openTheBigChat(one.id));
    row.append(pick);
    list.append(row);
  }
}

function scrollTheTray(byHowMuch) {
  const list = $("theChatTrayList");
  if (list) list.scrollBy({left: byHowMuch, behavior: "smooth"});
}

// ---- the line from the tray to the box ------------------------------------

function theSheetToDrawOn() {
  // Declared in the page rather than made up here. An id invented in code is an
  // id nothing checks the spelling of, and a typo in one is a line that never
  // appears with nothing at all to say why.
  return $("swarmPointer");
}

function pointAtTheBox(agentId) {
  stopPointing();
  const box = document.querySelector(`.swarm-box[data-kind="agent"][data-id="${agentId}"]`);
  const sheet = theSheetToDrawOn();
  if (!box || !sheet) return;
  box.classList.add("pointed-at");
  const middle = theMiddleOf(box);
  const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
  // From the bottom of the board up to the box, because the tray is along the
  // bottom and that is where the eye is coming from.
  line.setAttribute("x1", String(middle.x));
  line.setAttribute("y1", String($("swarmCanvas").offsetHeight));
  line.setAttribute("x2", String(middle.x));
  line.setAttribute("y2", String(middle.y));
  sheet.append(line);
  box.scrollIntoView({block: "nearest", inline: "nearest"});
}

function stopPointing() {
  const sheet = $("swarmPointer");
  if (sheet) sheet.replaceChildren();
  for (const one of document.querySelectorAll(".swarm-box.pointed-at")) {
    one.classList.remove("pointed-at");
  }
}

// ---- one chat, opened big -------------------------------------------------

function theBigChatLayoutEvidence() {
  const sheet = $("theBigChat")?.querySelector(".the-big-chat-sheet");
  const main = $("theBigChat")?.querySelector(".the-big-chat-main");
  const header = $("theBigChat")?.querySelector(".the-big-chat-top");
  const title = $("theBigChatTitle");
  const close = $("theBigChatShut");
  const textbox = $("theBigChatBox");
  const send = $("theBigChatSend");
  if (!sheet || !main || !header || !title || !close || !textbox || !send
      || $("theBigChat").hidden) return {open: false};
  const sheetRect = sheet.getBoundingClientRect();
  const mainRect = main.getBoundingClientRect();
  const headerRect = header.getBoundingClientRect();
  const titleRect = title.getBoundingClientRect();
  const closeRect = close.getBoundingClientRect();
  const textboxRect = textbox.getBoundingClientRect();
  const sendRect = send.getBoundingClientRect();
  const inside = (rect, outer) => rect.width > 0 && rect.height > 0
    && rect.left >= outer.left - 1 && rect.top >= outer.top - 1
    && rect.right <= outer.right + 1 && rect.bottom <= outer.bottom + 1;
  const viewport = {left: 0, top: 0, right: innerWidth, bottom: innerHeight};
  const insideSheetAndViewport = (rect) => inside(rect, sheetRect) && inside(rect, viewport);
  const bottom = $("theBigChat")?.querySelector(".the-big-chat-bottom");
  const said = $("theBigChatSaid");
  const conversations = $("theBigChat")?.querySelector(".the-big-chat-conversations");
  return {
    open: true,
    viewportWidth: document.documentElement.clientWidth,
    documentHasHorizontalOverflow:
      document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
    mainPaneClippedHorizontally:
      mainRect.left < sheetRect.left - 1 || mainRect.right > sheetRect.right + 1,
    mainInsideSheetAndViewport: insideSheetAndViewport(mainRect),
    headerInsideSheetAndViewport: insideSheetAndViewport(headerRect),
    titleInsideSheetAndViewport: insideSheetAndViewport(titleRect),
    closeInsideSheetAndViewport: insideSheetAndViewport(closeRect),
    textboxInsideSheetAndViewport: insideSheetAndViewport(textboxRect),
    sendInsideSheetAndViewport: insideSheetAndViewport(sendRect),
    headerReachable: headerRect.top >= 0 && closeRect.top >= headerRect.top - 1,
    boundedInternalScrolling: [bottom, said, conversations].every((one) => {
      const overflow = getComputedStyle(one).overflowY;
      return one.clientHeight <= sheet.clientHeight && ["auto", "scroll"].includes(overflow);
    }),
    sendVisible: insideSheetAndViewport(sendRect),
    stacked: getComputedStyle($("theBigChat").querySelector(".the-big-chat-workspace"))
      .gridTemplateColumns.split(" ").length === 1,
  };
}
window.harnessUiLayoutEvidence = () => ({bigChat: theBigChatLayoutEvidence()});

function revealTheBigChatComposer() {
  const bottom = $("theBigChat")?.querySelector(".the-big-chat-bottom");
  if (!bottom) return;
  bottom.scrollTop = bottom.scrollHeight;
}

function openTheBigChat(agentId) {
  const agent = theSwarmAgent(agentId);
  if (!agent) return;
  if ($("theBigChat").hidden) {
    theBigChatInvoker = document.activeElement;
    theBigChatInvokerAgent = agentId;
  }
  const alreadyOpen = swarmChats.some((one) => one.agent === agentId);
  if (!alreadyOpen) openTheChatFor(agentId);
  theBigOne = agentId;
  $("theBigChatTitle").textContent = `${agent.name} — Nexus chat`;
  $("theBigChat").hidden = false;
  applyTheBigChatLayout();
  renderTheChatTray();
  renderTheBigChat();
  // Opening the compact chat already started this read. Starting it twice made
  // two independent active-chat snapshots race each other on first open.
  if (alreadyOpen) loadConversationsFor(agentId);
  revealTheBigChatComposer();
  $("theBigChatBox").focus({preventScroll: true});
}

function keepTabInsideTheBigChat(event) {
  // It says it is modal, so it has to behave like one. Without this, Tab walked
  // straight out of it and landed on something completely hidden underneath -
  // a control somebody is now typing into and cannot see.
  if (event.key !== "Tab" || $("theBigChat").hidden) return;
  const inside = [...$("theBigChat").querySelectorAll(
    "button, textarea, input, select, a[href]")].filter((one) => !one.disabled);
  if (!inside.length) return;
  const first = inside[0];
  const last = inside[inside.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  } else if (!$("theBigChat").contains(document.activeElement)) {
    event.preventDefault();
    first.focus();
  }
}

function restoreTheBigChatFocus() {
  const target = theBigChatInvoker?.isConnected
    ? theBigChatInvoker
    : document.querySelector(`[data-big-chat-invoker="${CSS.escape(theBigChatInvokerAgent)}"]`)
      || $("theChatTrayOn") || $("swarmTitle");
  theBigChatInvoker = null;
  theBigChatInvokerAgent = "";
  target?.focus?.({preventScroll: true});
}

function minimiseTheBigChat(restoreFocus = true) {
  // Back to the tray, not closed. The conversation is still open; it is just
  // not the one on screen.
  rememberTheBigChatComposer();
  theBigOne = "";
  $("theBigChat").hidden = true;
  renderTheChatTray();
  if (restoreFocus !== false) restoreTheBigChatFocus();
}

function shutTheBigChat() {
  const was = theBigOne;
  minimiseTheBigChat(false);
  if (was) closeTheChatFor(was);
  renderTheChatTray();
  restoreTheBigChatFocus();
}

function aFaceFor(kind, agent = null, nexus = false) {
  if (kind !== "you") return aChatTurnFace(
    {speaker_id: nexus ? "nexus" : ""}, agent, "the-big-chat-face", 24,
  );
  const face = make("span", "swarm-agent-face the-big-chat-face");
  face.append(aSwarmDrawing("person", 24));
  return face;
}

function renderTheConversationSidebar(agentId) {
  const list = $("theBigChatConversationList");
  if (!list) return;
  const held = swarmChats.find((one) => one.agent === agentId);
  const conversations = held?.conversations || [];
  const groups = new Map();
  for (const pair of conversationPairsFor(agentId)) groups.set(pairKey(pair), pair);
  for (const one of conversations) groups.set(pairKey(one.pair || []), one.pair || []);
  list.replaceChildren();
  for (const pair of groups.values()) {
    const agents = pair.map(theSwarmAgent).filter(Boolean);
    if (!agents.length) continue;
    const peer = agents.find((one) => one.id !== agentId);
    const singleAgentGroup = pair.length === 1 && pair[0] === agentId;
    const group = make("section", "the-big-chat-pair");
    const heading = make("div", "the-big-chat-pair-head");
    const faces = make("span", "the-big-chat-pair-faces");
    agents.forEach((one) => faces.append(anAgentFace(one, "", 16)));
    heading.append(faces);
    heading.append(make("strong", "the-big-chat-pair-name",
      agents.map((one) => one.name).join(" ↔ ") || "Direct agent chat"));
    group.append(heading);
    const items = make("div", "the-big-chat-conversation-items");
    const chats = conversations.filter((one) => pairKey(one.pair || []) === pairKey(pair));
    if (!chats.length) items.append(make(
      "p", "hint", singleAgentGroup
        ? "No saved chats for this agent." : "No saved chats for this pair."
    ));
    for (const conversation of chats) {
      const row = make("div", "the-big-chat-conversation-item");
      const archived = Boolean(conversation.archived_at);
      const running = swarmChatIsBusy(agentId, conversation.id);
      const resetting = swarmChatIsResetting(agentId, conversation.id);
      const bindingProblem = conversation.binding_problem;
      row.classList.toggle("archived", archived);
      row.classList.toggle("running", running || resetting);
      row.classList.toggle("binding-broken", Boolean(bindingProblem));
      const pick = make("button", "the-big-chat-conversation-pick");
      pick.type = "button";
      pick.classList.toggle("active", held.conversation === conversation.id);
      pick.dataset.archived = String(archived);
      pick.dataset.conversationAction = "pick";
      pick.dataset.chatId = conversation.id;
      // Selection is navigation, not mutation. Keep it available while a
      // sibling chat works so the user can watch or start another exact chat.
      pick.disabled = archived || swarmConversationSwitching.has(agentId)
        || swarmChatIsHydrating(agentId);
      const project = (conversation.projects || []).find(
        (one) => one.id === conversation.project
      );
      const subtitle = archived ? "Archived — history kept"
        : bindingProblem ? "Setup changed — transcript protected"
        : resetting ? "Starting again — sibling chats remain available"
          : running ? "Working now — you can safely open another chat"
          : conversation.legacy_source ? "Recovered from an older Nexus chat"
            : project?.name || ((conversation.projects || []).length
              ? "No project selected" : "No project shared by this pair");
      pick.append(make("strong", "", conversation.name));
      pick.append(make("span", "", subtitle));
      pick.addEventListener("click", () => activateConversationFor(agentId, conversation.id));
      row.append(pick);
      const remove = make("button", archived ? "the-big-chat-conversation-delete"
        : "danger the-big-chat-conversation-delete", archived ? "Restore" : "Archive");
      remove.type = "button";
      remove.title = `${archived ? "Restore" : "Archive"} ${conversation.name}`;
      remove.setAttribute("aria-label", remove.title);
      remove.dataset.conversationAction = archived ? "restore" : "archive";
      remove.dataset.chatId = conversation.id;
      remove.disabled = running || resetting || swarmConversationSwitching.has(agentId)
        || swarmChatIsHydrating(agentId);
      remove.addEventListener("click", () => archived
        ? restoreConversationFor(agentId, conversation.id)
        : archiveConversationFor(agentId, conversation.id));
      row.append(remove);
      if (bindingProblem && !archived) {
        const repair = make("div", "the-big-chat-binding-repair");
        repair.append(make("p", "", String(bindingProblem.message ||
          "This chat's provider or project setup changed.")));
        const fresh = make("button", "primary", String(
          bindingProblem.action_label || "Start fresh with current setup"
        ));
        fresh.type = "button";
        fresh.dataset.conversationAction = "create";
        fresh.disabled = swarmConversationSwitching.has(agentId)
          || swarmChatIsHydrating(agentId);
        const currentPeer = peer?.id || "";
        fresh.addEventListener("click", () =>
          createConversationFor(
            agentId, currentPeer, singleAgentGroup ? "single" : ""
          ));
        repair.append(fresh);
        row.append(repair);
      }
      items.append(row);
    }
    group.append(items);
    if (singleAgentGroup || connectedPairsFor(agentId).some(
      (one) => pairKey(one) === pairKey(pair)
    )) {
      const add = make("button", "the-big-chat-pair-new", singleAgentGroup
        ? "+ New chat for this agent" : "+ New chat for this pair");
      add.type = "button";
      add.dataset.conversationAction = "create";
      add.disabled = swarmConversationSwitching.has(agentId)
        || swarmChatIsHydrating(agentId);
      add.addEventListener("click", () => createConversationFor(
        agentId, peer?.id || "", singleAgentGroup ? "single" : ""
      ));
      group.append(add);
    }
    list.append(group);
  }
  if (!list.childElementCount) {
    list.append(make("p", "hint", "Connect this agent to another AI with a green line."));
  }
}

function renderTheConversationProject(agentId) {
  const select = $("theBigChatProject");
  if (!select) return;
  const conversation = activeConversationFor(agentId);
  select.replaceChildren();
  const projects = conversation?.projects || [];
  const none = make("option", "", projects.length
    ? "Choose a shared project" : "No project shared by this pair");
  none.value = "";
  select.append(none);
  for (const project of projects) {
    const option = make("option", "", project.name);
    option.value = project.id;
    option.title = project.path;
    select.append(option);
  }
  select.value = conversation?.project || "";
  select.disabled = !conversation || swarmChatIsBusy(agentId)
    || swarmChatIsResetting(agentId) || swarmConversationSwitching.has(agentId)
    || swarmChatIsHydrating(agentId) || Boolean(conversation?.binding_problem);
  $("theBigChatProjectHelp").textContent = !conversation
    ? "Create or select a pair chat first."
    : conversation.binding_problem
      ? conversation.binding_problem.message
    : conversation.project
      ? "This chat's file work is confined to the selected folder."
      : projects.length
        ? "Choose which shared project this chat may write to."
        : "This selected pair has no project in common. Choose another pair chat or connect both agents to the same project.";
}

function renderTheBigChat() {
  const list = $("theBigChatSaid");
  if (!list || !theBigOne) return;
  const agent = theSwarmAgent(theBigOne);
  const held = swarmChats.find((one) => one.agent === theBigOne);
  // A board refresh can remove the selected agent while a transcript or
  // activity callback is still queued. Close the now-ownerless modal instead
  // of dereferencing stale UI identity and throwing from the render loop.
  if (!agent || !held) {
    minimiseTheBigChat(false);
    return;
  }
  const conversation = activeConversationFor(theBigOne);
  const renderIdentity = swarmChatKey(theBigOne);
  syncTheBigChatComposer();
  resetTheBigChatRenderCache(renderIdentity);
  const pairNames = (conversation?.pair_agents || []).map((one) => one.name);
  $("theBigChatTitle").textContent = conversation
    ? `${pairNames.join(" ↔ ")} — ${conversation.name}`
    : `${agent.name} — Nexus chat`;
  if (bigChatPartChanged("sidebar", {
    conversations: held?.conversations || [],
    agents: (theSwarmBoard().agents || []).map((one) => ({
      id: one.id, name: one.name, icon: one.icon, colour: one.colour,
      bubble_colour: one.bubble_colour,
      profile_picture: one.profile_picture ? [
        one.profile_picture.length,
        one.profile_picture.slice(0, 48),
        one.profile_picture.slice(-48),
      ] : null,
      picture_zoom: one.picture_zoom, picture_hue: one.picture_hue,
    })),
    talks: theSwarmBoard().talks_to || [],
    busy: (held?.conversations || []).map((one) => [
      one.id, swarmChatIsBusy(theBigOne, one.id), swarmChatIsResetting(theBigOne, one.id),
    ]),
    switching: swarmConversationSwitching.has(theBigOne),
    hydrating: swarmChatIsHydrating(theBigOne),
  })) renderTheConversationSidebar(theBigOne);
  if (bigChatPartChanged("project", {
    id: conversation?.id || "", project: conversation?.project || "",
    projects: conversation?.projects || [], busy: swarmChatIsBusy(theBigOne),
    resetting: swarmChatIsResetting(theBigOne), hydrating: swarmChatIsHydrating(theBigOne),
    switching: swarmConversationSwitching.has(theBigOne),
  })) renderTheConversationProject(theBigOne);
  syncChatRoundPolicy(theBigOne);
  syncChatRecipientWords(theBigOne);
  syncChatTeamReadiness(theBigOne);
  const destination = $("theBigChatDestination");
  if (destination && bigChatPartChanged("destination", {
    agent: {id: agent?.id, name: agent?.name, who: agent?.who, ready: agent?.ready,
      why_not: agent?.why_not, chat_destination: agent?.chat_destination},
    conversation: conversation ? {
      id: conversation.id,
      destination: conversation.destination,
      collaboration_problem: conversation.collaboration_problem,
    } : null,
  })) destination.replaceChildren(aChatDestination(agent, {conversation}));

  const turns = [];
  let latestUserPrompt = "";
  for (const one of chatTurnsWhileWorking(theBigOne, keptTranscriptFor(theBigOne))) {
    if (one.who === "you" && String(one.text || "").trim()) latestUserPrompt = one.text;
    const collaboration = ["agent_reply", "lead_draft", "agent_plan", "lead_plan",
      "agent_discussion", "agent_plan_review", "lead_execution", "agent_execution", "agent_verification"]
      .includes(one.phase);
    turns.push({
      kind: one.who === "you" ? "you" : (collaboration ? "between" : "them"),
      who: chatTurnSpeaker(one, agent),
      text: one.text,
      at: one.at,
      attachments: one.attachments || [],
      phase: one.phase || "",
      route: one.speaker_route || "",
      model: one.model || "",
      milliseconds: Number(one.milliseconds || 0),
      speakerId: one.speaker_id || "",
      nexus: isNexusChatTurn(one),
      structuredStateUnavailable: Boolean(one.structured_state_unavailable),
      participantOutcome: normalizedParticipantOutcome(one),
      longHorizonCorrelation: normalizedLongHorizonCorrelation(one),
      originalPrompt: latestUserPrompt,
    });
  }
  // What this agent said to another agent, and what another said to it. Shown
  // here because a conversation between two of them is a conversation, and
  // reading it in a different place from your own is how you lose the thread.
  for (const one of (conversation ? [] : (swarmWhatTheySaid.notes || []))) {
    if (one.said_by !== theBigOne && one.shown_to !== theBigOne) continue;
    turns.push({
      kind: "between",
      who: `${one.said_by_name} to ${one.shown_to_name}`,
      text: one.text,
      at: one.at,
      phase: "agent_reply",
      route: "",
      model: "",
      milliseconds: Number(one.milliseconds || 0),
      speakerId: one.said_by || "",
    });
  }
  // A turn with no time on it goes where it was put, at the end, rather than
  // sorting to the very top. Empty sorts before everything, so the newest thing
  // said - the one somebody is waiting to read - jumped to the top of the list
  // and then got scrolled out of sight.
  turns.forEach((one, place) => { one.place = place; });
  turns.sort((a, b) => {
    if (!a.at || !b.at) return a.place - b.place;
    const said = String(a.at).localeCompare(String(b.at));
    return said || a.place - b.place;
  });

  if (bigChatPartChanged("turns", turns)) {
    list.replaceChildren();
    if (!turns.length) {
      list.append(make("li", "hint",
        "Nothing said yet. Type below, and anything this one says to another agent "
        + "turns up here too."));
    }
    for (const one of turns) {
      const row = make("li", `the-big-chat-turn from-${one.kind === "you" ? "you" : "them"} `
        + (one.kind === "between" ? "between" : ""));
      row.classList.toggle("nexus-turn", one.nexus);
      const speaker = one.nexus ? null : (theSwarmAgent(one.speakerId) || agent);
      if (one.kind !== "you") styleForAgent(row, speaker);
      row.append(aFaceFor(one.kind, speaker, one.nexus));
      const what = make("div", "the-big-chat-what");
      const heading = make("div", "the-big-chat-turn-head");
      heading.append(make("span", "the-big-chat-who", `${one.who}${one.at ? ` | ${one.at}` : ""}`));
      const phase = chatPhaseName(one.phase);
      if (phase) heading.append(make("span", `chat-turn-phase phase-${one.phase}`, phase));
      what.append(heading);
      if (one.participantOutcome) {
        appendParticipantOutcome(what, one.participantOutcome, agent, one.originalPrompt, row);
      } else {
        appendChatText(what, one.text);
      }
      appendLongHorizonGoalLink(what, one.longHorizonCorrelation);
      if (one.structuredStateUnavailable) {
        what.append(make("p", "hint chat-turn-protocol-warning",
          "Reply kept exactly as delivered; completion and progress could not be verified."));
      }
      if (Array.isArray(one.attachments) && one.attachments.length) {
        const files = make("div", "talk-attachments");
        for (const attached of one.attachments) {
          files.append(make("span", "talk-attachment",
            `${attached.image ? "Screenshot" : "File"}: ${attached.name}`));
        }
        what.append(files);
      }
      const under = [];
      if (one.milliseconds) under.push(prettyTime(one.milliseconds));
      if (one.route) under.push(`route ${one.route}`);
      if (one.model) under.push(one.model);
      if (under.length) what.append(make("p", "hint chat-turn-under", under.join(" | ")));
      row.append(what);
      list.append(row);
    }
    list.scrollTop = list.scrollHeight;
  }
  const attachments = swarmChatAttachments.get(renderIdentity) || [];
  if (bigChatPartChanged("attachments", attachments.map((one) => ({
    name: one.name, type: one.type, size: one.size, dataLength: one.data?.length || 0,
  })))) renderChatAttachments(theBigOne);
  renderSwarmChatActivity(theBigOne);
  renderWorkRecovery(theBigOne);
  if (bigChatPartChanged("doing", {
    doing: (swarmDoing?.turns || []).filter((one) => one.agent === theBigOne),
    trouble: agent?.trouble_last_time || "", fix: agent?.how_to_fix_it || "",
  })) renderWhatItHasGoingOn(agent);
}

function renderWhatItHasGoingOn(agent) {
  const list = $("theBigChatDoing");
  if (!list) return;
  list.replaceChildren();
  const doing = swarmDoing || {};
  const mine = (doing.turns || []).filter((one) => one.agent === theBigOne);
  if (!mine.length) {
    list.append(make("li", "hint", "Nothing running for this one right now."));
  }
  for (const one of mine) {
    const row = make("li", "the-big-chat-doing-one");
    row.append(make("strong", "", one.where || "a project"));
    const bits = [one.round, one.state];
    if (one.part) bits.push(`part ${one.part} of the page`);
    if (one.milliseconds) bits.push(`${Math.round(one.milliseconds / 1000)}s`);
    row.append(make("p", "hint", bits.filter(Boolean).join(" | ")));
    if (one.why_not) row.append(make("p", "hint", one.why_not));
    list.append(row);
  }
  if (agent && agent.trouble_last_time) {
    list.append(make("li", "the-big-chat-doing-one", agent.trouble_last_time));
  }
  if (agent && agent.how_to_fix_it) {
    list.append(make("li", "the-big-chat-doing-one", agent.how_to_fix_it));
  }
  if (agent && (agent.trouble_last_time || !agent.ready)) {
    const row = make("li", "the-big-chat-doing-one");
    const repair = make("button", "swarm-repair", "Repair connection");
    repair.type = "button";
    repair.addEventListener("click", () => void openAgentRepairFlow(agent.id, repair));
    row.append(repair);
    list.append(row);
  }
}

const GOOGLE_CLOUD_PROJECT_WELCOME = "https://console.cloud.google.com/welcome";

function showGeminiProjectHelp() {
  const openIt = window.confirm(
    "Open Google Cloud Console in your browser?\n\n"
    + "1. Use the project picker at the top to select your project.\n"
    + "2. On the Welcome page, copy Project ID — not Project name or Project number.\n"
    + "3. Paste that ID into Nexus when connecting Gemini.\n\n"
    + "If no project is listed, use Manage resources to create one or ask your "
    + "Google Workspace administrator which project your Gemini Code Assist seat uses."
  );
  if (!openIt) return;
  window.open(GOOGLE_CLOUD_PROJECT_WELCOME, "_blank", "noopener,noreferrer");
  sayInSwarm("Google Cloud Console opened. Choose a project and copy the Project ID from its Welcome page.");
}

async function repairClaudeAccess(route, diagnosisFingerprint, button = null) {
  if (!window.confirm(
      "Repair Claude command-line access?\n\n"
      + "This opens a visible terminal, updates Claude, signs the Claude command line out, "
      + "and starts a fresh Claude sign-in. Your open Claude app is a separate session. "
      + "Nexus will not see your account or credentials.")) {
    return;
  }
  const was = button ? button.textContent : "";
  if (button) {
    button.disabled = true;
    button.textContent = "Opening repair...";
  }
  try {
    const said = await request("/api/team/repair-claude", {
      method: "POST", body: JSON.stringify({
        route, diagnosis_fingerprint: diagnosisFingerprint,
      }),
    });
    sayInSwarm(said.note || "Claude's repair opened in its own terminal.");
  } catch (trouble) {
    const words = String(trouble.message || trouble);
    showError(words);
    sayInSwarm(words);
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = was;
    }
  }
}

async function signInThisAssistant(routeOrKind, button = null) {
  const was = button ? button.textContent : "";
  if (button) {
    button.disabled = true;
    button.textContent = "Opening...";
  }
  try {
    const said = await request("/api/team/login", {
      method: "POST", body: JSON.stringify({route: routeOrKind}),
    });
    sayInSwarm(said.note || "The provider sign-in opened in its own terminal.");
    return said;
  } catch (trouble) {
    const words = String(trouble.message || trouble);
    showError(words);
    sayInSwarm(words);
    return null;
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = was;
    }
  }
}

function agentStillUsesRoute(agentId, route) {
  const agent = theSwarmAgent(agentId);
  const draft = swarmAgentSettingDrafts.get(agentId);
  return Boolean(agent) && String(draft?.values?.who ?? agent.who ?? "") === String(route || "");
}

function renderAgentRepairPanel(agent, route, plan = null) {
  const panel = $("swarmAgentRepair");
  const badge = $("swarmAgentRepairBadge");
  const title = $("swarmAgentRepairTitle");
  const status = $("swarmAgentSessionStatus");
  const steps = $("swarmAgentRepairSteps");
  const start = $("swarmAgentRepairStart");
  const action = $("swarmAgentRepairAction");
  const test = $("swarmAgentLiveTest");
  const check = $("swarmAgentCheckLogin");
  const login = $("swarmAgentManualLogin");
  const stop = $("swarmAgentStopTest");
  const choices = $("swarmAgentRepairChoices");
  const note = $("swarmAgentRepairActionNote");
  const testing = swarmAgentRepairTests.has(agent.id);

  for (const button of [start, action, test, check, login, stop]) {
    button.hidden = true;
    button.disabled = false;
    button.onclick = null;
  }
  action.removeAttribute("data-action");
  choices.replaceChildren();
  choices.hidden = true;
  steps.replaceChildren();
  steps.hidden = true;
  note.textContent = "";

  if (!route) {
    panel.dataset.tone = "idle";
    title.textContent = "Connection";
    badge.textContent = "No assistant";
    status.textContent = "Choose an assistant first.";
    start.hidden = false;
    start.disabled = true;
    return;
  }

  if (testing) {
    panel.dataset.tone = "attention";
    title.textContent = "Testing the exact route";
    badge.textContent = "Working";
    status.textContent = "Waiting for one real model answer in an empty temporary folder…";
    note.textContent = "This uses one model request. No project files were supplied to the provider.";
    stop.hidden = false;
    stop.onclick = () => stopAgentRouteTest(agent.id, route);
    return;
  }

  if (!plan?.repair) {
    panel.dataset.tone = agent.trouble_last_time || !agent.ready ? "blocked" : "idle";
    title.textContent = "Connection";
    badge.textContent = agent.trouble_last_time || !agent.ready ? "Needs attention" : "Not checked";
    status.textContent = agent.trouble_last_time
      ? `${agent.trouble_last_time} Repair connection will diagnose this exact route.`
      : !agent.ready
        ? (agent.why_not || "This assistant is not ready yet.")
        : "Check this exact route and get the right recovery steps without sending a model prompt.";
    start.hidden = false;
    start.onclick = () => loadAgentRepairPlan(agent.id, route, start);
    return;
  }

  const repair = plan.repair;
  panel.dataset.tone = String(repair.tone || "attention");
  title.textContent = String(repair.title || "Connection repair");
  badge.textContent = String(repair.state || "checked").replaceAll("-", " ");
  status.textContent = String(repair.summary || plan.note || "The route was checked.");
  for (const words of repair.steps || []) steps.append(make("li", "", String(words)));
  steps.hidden = !steps.children.length;

  const descriptions = [];
  for (const offered of repair.actions || []) {
    const id = String(offered.id || "");
    if (offered.note) descriptions.push(`${offered.label}: ${offered.note}`);
    if (id === "check") {
      check.hidden = false;
      check.textContent = String(offered.label || "Check again");
      check.onclick = () => checkAgentLogin(agent.id, route, check);
    } else if (id === "live-test") {
      test.hidden = false;
      test.textContent = String(offered.label || "Run live test");
      test.classList.toggle("primary", Boolean(offered.primary));
      test.onclick = () => runAgentRouteTest(agent.id, route);
    } else if (id === "login") {
      login.hidden = false;
      login.textContent = String(offered.label || "Open sign-in");
      login.classList.toggle("primary", Boolean(offered.primary));
      login.onclick = () => manuallyLogInAgent(agent.id, route);
    } else {
      const choice = make("button", Boolean(offered.primary) ? "primary" : "",
        String(offered.label || "Continue repair"));
      choice.type = "button";
      choice.dataset.action = id;
      choice.onclick = () => performAgentRepairAction(agent.id, route, offered, choice);
      choices.append(choice);
    }
  }
  choices.hidden = !choices.children.length;
  note.textContent = descriptions.join(" ");
}

async function loadAgentRepairPlan(agentId, route, button = null) {
  if (!route) return null;
  const was = button?.textContent || "";
  if (button) {
    button.disabled = true;
    button.textContent = "Diagnosing…";
  }
  $("swarmAgentRepair").dataset.tone = "attention";
  $("swarmAgentRepairBadge").textContent = "Checking";
  $("swarmAgentSessionStatus").textContent = "Checking the exact route without sending a model prompt…";
  try {
    const plan = await request("/api/team/repair-plan", {
      method: "POST", body: JSON.stringify({route}),
    });
    swarmAgentRepairPlans.set(agentId, {route, plan});
    if (agentStillUsesRoute(agentId, route)) {
      renderAgentRepairPanel(theSwarmAgent(agentId), route, plan);
    }
    return plan;
  } catch (error) {
    if (agentStillUsesRoute(agentId, route)) {
      $("swarmAgentRepair").dataset.tone = "error";
      $("swarmAgentRepairBadge").textContent = "Check failed";
      $("swarmAgentSessionStatus").textContent = String(error.message || error);
    }
    return null;
  } finally {
    if (button?.isConnected) {
      button.disabled = false;
      button.textContent = was;
    }
  }
}

async function checkAgentLogin(agentId, route, button = null) {
  return loadAgentRepairPlan(agentId, route, button);
}

async function manuallyLogInAgent(agentId, route) {
  const status = $("swarmAgentSessionStatus");
  if (!route) return;
  if (route.startsWith("web:")) {
    status.textContent = "Opening Web AI chats. Reconnect this provider page, then press Check again.";
    await openWebChatManager();
    return;
  }
  status.textContent = "Opening the exact provider route's sign-in…";
  const said = await signInThisAssistant(route, $("swarmAgentManualLogin"));
  status.textContent = said?.note || "The sign-in did not open. Use Check again for the current diagnosis.";
}

async function repairGeminiRoute(agentId, route, button) {
  const project = await askForOneLine(
    "Repair Gemini connection",
    "Enter the Google Cloud Project ID used by this Workspace account. This is the Project ID, not its display name or number.",
    "",
    {label: "HOW TO FIND IT (external link)", href: GEMINI_CLI_CLOUD_PROJECT_HELP},
  );
  if (project === null) return;
  if (!project.trim()) {
    $("swarmAgentSessionStatus").textContent = "No Project ID was entered, so nothing changed.";
    return;
  }
  const was = button.textContent;
  button.disabled = true;
  button.textContent = "Saving…";
  try {
    const said = await request("/api/team/set-google-project", {
      method: "POST", body: JSON.stringify({route, google_project: project.trim()}),
    });
    sayInSwarm(said.note || `Saved the Cloud project for ${route}.`);
    await refreshSwarm(true);
    await loadAgentRepairPlan(agentId, route);
  } catch (error) {
    $("swarmAgentSessionStatus").textContent = String(error.message || error);
  } finally {
    if (button.isConnected) {
      button.disabled = false;
      button.textContent = was;
    }
  }
}

async function performAgentRepairAction(agentId, route, offered, button) {
  const actionId = String(offered?.id || offered || "");
  if (actionId === "connect-assistant") {
    const connected = await connectThisAssistant(String(offered?.kind || ""), button);
    if (connected && agentStillUsesRoute(agentId, route)) {
      await loadAgentRepairPlan(agentId, route);
    }
  } else if (actionId === "google-project") {
    await repairGeminiRoute(agentId, route, button);
  } else if (actionId === "repair-claude") {
    await repairClaudeAccess(route, offered?.diagnosis_fingerprint || "", button);
  } else if (actionId === "inspect-provider-turn") {
    const conversation = activeConversationFor(agentId);
    const destination = conversation?.destination || {};
    const webChatId = String(destination.web_chat_id || (route.startsWith("web:") ? route.slice(4) : ""));
    if (!webChatId || !window.harnessDesktop?.showWebChat) {
      $("swarmAgentSessionStatus").textContent =
        "This provider conversation cannot be opened here. Use its provider app before retrying the uncertain turn.";
      return;
    }
    $("swarmAgentSessionStatus").textContent =
      "Opening the exact provider conversation. Confirm whether the uncertain turn arrived before retrying it.";
    await showFullWebChatInsideNexus(
      webChatId,
      String(destination.web_conversation_key || ""),
      Boolean(destination.web_prefer_existing_conversation),
    );
  } else if (actionId === "web-chat") {
    const connectionId = String(offered?.connection_id || (route.startsWith("web:") ? route.slice(4) : ""));
    const providerId = String(offered?.provider || connectionId.split("-", 1)[0] || "")
      .toLowerCase();
    const conversation = activeConversationFor(agentId);
    const destination = conversation?.destination || {};
    const conversationKey = String(destination.web_conversation_key || conversation?.filed_as || "");
    const preferExisting = Boolean(destination.web_prefer_existing_conversation);
    pickSwarmBox("agent", agentId);
    $("swarmAgentSessionStatus").textContent =
      "Reconnect the provider chat saved for this exact agent. Nexus will preserve its route and conversation binding.";
    let providerChoices = webChatProviderChoices;
    if (providerId) {
      try {
        providerChoices = await refreshWebChatProviderChoices();
      } catch (error) {
        $("swarmAgentSessionStatus").textContent =
          `Nexus could not read the local web-chat providers: ${error.message || error}`;
        return;
      }
    }
    if (providerId && providerChoices.some((one) => one.id === providerId)) {
      await connectPickedAgentToWebProvider(providerId, {
        connectionId,
        conversationKey,
        preferExisting,
      });
    } else {
      await openWebChatManager();
    }
  } else if (actionId === "choose-route" || actionId === "map-route") {
    pickSwarmBox("agent", agentId);
    $("swarmAgentSessionStatus").textContent =
      "Choose an available assistant above. Nexus will keep this agent box and replace only its missing route.";
    $("swarmAgentWho").focus();
  } else if (actionId === "settings") {
    switchView("settings");
    $("settingsFilter").value = route;
    await refreshSettings();
    $("settingsFilter").focus();
    announce(`Settings are filtered to ${route}. Return to the agent and press Check again after saving.`);
  }
}

async function runAgentRouteTest(agentId, route) {
  if (!route || swarmAgentRepairTests.has(agentId)) return;
  const controller = new AbortController();
  swarmAgentRepairTests.set(agentId, {route, controller});
  renderAgentRepairPanel(theSwarmAgent(agentId), route, swarmAgentRepairPlans.get(agentId)?.plan);
  try {
    const said = await request("/api/team/test-route", {
      method: "POST",
      body: JSON.stringify({route}),
      signal: controller.signal,
    });
    if (said.plan) swarmAgentRepairPlans.set(agentId, {route, plan: said.plan});
    sayInSwarm(`${(theSwarmAgent(agentId) || {}).name || agentId}: connection verified.`);
    await refreshSwarm(true);
  } catch (error) {
    if (error.name !== "AbortError") {
      sayInSwarm(`Live connection test failed: ${error.message || error}`);
      await loadAgentRepairPlan(agentId, route);
    }
  } finally {
    const active = swarmAgentRepairTests.get(agentId);
    if (active?.controller === controller) swarmAgentRepairTests.delete(agentId);
    if (agentStillUsesRoute(agentId, route)) {
      renderAgentRepairPanel(
        theSwarmAgent(agentId), route,
        swarmAgentRepairPlans.get(agentId)?.route === route
          ? swarmAgentRepairPlans.get(agentId).plan : null,
      );
    }
  }
}

async function stopAgentRouteTest(agentId, route) {
  const active = swarmAgentRepairTests.get(agentId);
  if (!active || active.route !== route) return;
  const button = $("swarmAgentStopTest");
  button.disabled = true;
  button.textContent = "Stopping…";
  try {
    await request("/api/team/stop-route-test", {
      method: "POST", body: JSON.stringify({route}),
    });
    active.controller.abort();
  } catch (error) {
    $("swarmAgentSessionStatus").textContent = String(error.message || error);
    button.disabled = false;
    button.textContent = "Stop test";
  }
}

async function sendFromTheBigChat(mode = "chat") {
  const box = $("theBigChatBox");
  const typed = box.value;
  const said = typed.trim();
  if (!theBigOne) return;
  const agentId = theBigOne;
  const agent = theSwarmAgent(agentId);
  const executionPause = projectWorkPauseForMessage(mode, said, agentId);
  if (executionPause) {
    $("theBigChatSaidBack").textContent = executionPause;
    box.focus();
    return;
  }
  if (!said) {
    $("theBigChatSaidBack").textContent = mode === "work"
      ? "Describe the project-file change first."
      : mode === "collaborate"
        ? "Type the question or task the agents should discuss first."
        : "Type a message first.";
    box.focus();
    return;
  }
  if (said.length > Number(limitsForSwarmChat(agentId).input_characters || 200000)) {
    $("theBigChatSaidBack").textContent =
      "This message is over the displayed limit. Nexus kept the complete draft; split it or attach a file.";
    countWhatIsTypedInBigChat();
    box.focus();
    return;
  }
  if (!agent?.ready) {
    $("theBigChatSaidBack").textContent = agent?.why_not || "This agent is not ready yet.";
    return;
  }
  const conversation = activeConversationFor(agentId);
  if (conversation?.binding_problem) {
    $("theBigChatSaidBack").textContent = String(conversation.binding_problem.message ||
      "This chat's setup changed. Start a fresh chat with the current setup.");
    box.focus();
    return;
  }
  const recoveryKey = swarmChatKey(agentId);
  const runtimeKey = swarmChatRuntimeKey(agentId);
  if (swarmBusy.has(runtimeKey)) {
    $("theBigChatSaidBack").textContent = "Still waiting for the previous answer.";
    return;
  }
  if (swarmChatResetting.has(runtimeKey)) {
    $("theBigChatSaidBack").textContent = "This exact chat is still starting again.";
    return;
  }
  if (swarmChatIsHydrating(agentId)) {
    $("theBigChatSaidBack").textContent = "Loading this chat's saved identity first.";
    return;
  }
  if (swarmConversationSwitching.has(agentId)) {
    $("theBigChatSaidBack").textContent = "Finishing the chat switch first.";
    return;
  }
  if (["collaborate", "work"].includes(mode) && isLoneAgentChat(agentId)) {
    $("theBigChatSaidBack").textContent = loneAgentActionMessage(mode);
    box.focus();
    return;
  }
  if (mode === "collaborate") {
    const unavailable = syncChatTeamReadiness(agentId);
    if (unavailable.length) {
      $("theBigChatSaidBack").textContent =
        `The team request was not sent. Repair ${unavailable.map((one) => one.name || one.id).join(", ")} first.`;
      box.focus();
      return;
    }
  }
  const projectPermission = confirmProjectWork(agent, said, mode);
  if (!projectPermission.allowed) return;
  swarmBusy.add(runtimeKey);
  swarmStopping.delete(runtimeKey);
  nextSwarmChatRevision(agentId);
  setWhatCanBePressedInSwarm();
  const buttons = [
    "theBigChatSend", "theBigChatAttach",
    "theBigChatCollaborate", "theBigChatWork",
  ]
    .map($).filter(Boolean);
  buttons.forEach((button) => { button.disabled = true; });
  const attachmentKey = recoveryKey;
  const attachments = swarmChatAttachments.get(attachmentKey) || [];
  const durableDirectAdmission = mode === "work";
  // The sent text becomes a transcript turn immediately. Clear only that
  // exact draft after direct goal payload persistence; ordinary chat retains
  // immediate clearing so a user can compose its next turn while it runs.
  if (!durableDirectAdmission) {
    box.value = "";
    theBigChatComposerDrafts.delete(attachmentKey);
    rememberTheBigChatComposer();
  }
  const activity = beginSwarmChatActivity(agentId, mode, agent, said, attachments);
  $("theBigChatSaidBack").textContent = mode === "auto"
    ? "Deciding whether connected agents should help..."
    : mode === "chat" ? "Asking..."
    : mode === "collaborate" ? "Relaying to connected agents..."
    : "Starting durable goal work with the next useful task...";
  try {
    if (mode === "work") {
      if (!conversation?.project) throw new Error("Choose this chat's project before starting file work.");
      const directRequestKey = `nexus.long-horizon.direct-request.${recoveryKey}`;
      let durableRequest = null;
      const rawDurableRequest = localStorage.getItem(directRequestKey);
      if (rawDurableRequest !== null) {
        try { durableRequest = JSON.parse(rawDurableRequest); }
        catch (_) {
          throw new Error(
            "The saved browser goal-request marker is unreadable. No new project work was sent.",
          );
        }
        if (durableRequest === null) {
          throw new Error(
            "The saved browser goal-request marker has no exact identity. No new project work was sent.",
          );
        }
      }
      const directIntent = await directLongGoalIntent(
        conversation, agentId, said, attachments,
      );
      const directRequestId = directLongGoalRequestId(
        durableRequest, directIntent, {
          chat_id: conversation.id,
          project_id: conversation.project,
          lead_id: agentId,
        },
      );
      const exactPayload = {
        project_id: conversation.project,
        lead_id: agentId,
        text: said,
        chat_id: conversation.id,
        attachments,
      };
      const preparedAdmission = await prepareDirectLongGoalAdmission(
        exactPayload, directRequestId, directIntent, directRequestKey,
      );
      const {desktopOutbox, outbox, pending} = preparedAdmission;
      const heldDraft = theBigChatComposerDrafts.get(attachmentKey);
      if (!heldDraft || heldDraft.value === said) {
        theBigChatComposerDrafts.delete(attachmentKey);
      }
      if (theBigOne === agentId && swarmChatKey(agentId) === recoveryKey
          && box.value === said) {
        box.value = "";
        rememberTheBigChatComposer();
      }
      const exactAdmission = {
          request_id: directRequestId,
          chat_id: conversation.id,
          project_id: conversation.project,
          lead_id: agentId,
          intent_sha256: directIntent,
      };
      const started = preparedAdmission.started
        || await startAndReconcileDirectLongGoalAdmission(
          exactAdmission, pending.payload_sha256,
        );
      if (desktopOutbox) {
        await removeDirectLongGoalOutbox(
          conversation.id, directRequestId, outbox.payload_sha256,
        );
      }
      if (!clearDirectLongGoalRequestMarker(
        agentId, conversation.id, directRequestId, directIntent,
      )) {
        throw new Error(
          "The exact browser goal-request marker could not be cleared. The server receipt was left recoverable.",
        );
      }
      forgetDirectLongGoalRecovery(
        conversation.id, directRequestId, directIntent,
      );
      await bestEffortAcknowledgeDirectLongGoalTerminal(
        exactAdmission, "reconciled", started.goal.goal_id,
      );
      if (!swarmActivityCanReconcileSuccess(activity)) return;
      selectLongGoalSnapshot(started.goal);
      if (theBigOne === agentId && swarmChatKey(agentId) === recoveryKey) {
        await refreshTheChatFor(agentId);
      }
      finishLongHorizonAdmissionActivity(agentId, longGoal, activity);
      clearSwarmActivityAttachments(activity);
      const admission = longHorizonAdmissionWords(longGoal);
      sayInRuntimeChat(
        runtimeKey,
        admission.detail,
      );
      await refreshLongGoals(true);
      return;
    }
    const answered = await request("/api/swarm/say", {
      method: "POST", body: JSON.stringify({
        agent: agentId, text: said, mode, attachments, activity: activity.id,
        chat: conversation?.id || "",
        allow_project_changes: projectPermission.confirmed,
        round_limit: selectedChatRoundLimit(agentId),
      }),
    });
    if (!swarmActivityCanReconcileSuccess(activity)) {
      if (swarmChatKey(agentId) === recoveryKey) void refreshTheChatFor(agentId);
      return;
    }
    finishSwarmChatActivity(agentId, true, "", activity, answered.participant_outcome);
    rememberWorkRecoveryForKey(recoveryKey, answered, said, conversation);
    clearSwarmActivityAttachments(activity);
    keepWhatWasSaidToRuntime(
      runtimeKey, answered.said || [], conversation?.id || "",
    );
    renderWorkRecovery(agentId);
    sayInRuntimeChat(runtimeKey, workResponseWords(answered, agent.name));
    refreshSwarm(true);
  } catch (trouble) {
    if (!swarmActivityCanSettle(activity)) return;
    const priorDraft = theBigChatComposerDrafts.get(attachmentKey);
    if (!priorDraft?.value) {
      theBigChatComposerDrafts.set(attachmentKey, {
        value: typed, start: typed.length, end: typed.length, direction: "none",
      });
    }
    if (theBigOne === agentId && swarmChatKey(agentId) === attachmentKey && !box.value) {
      box.value = typed;
      box.setSelectionRange(typed.length, typed.length);
      rememberTheBigChatComposer();
    }
    if (swarmChatKey(agentId) === recoveryKey) await refreshTheChatFor(agentId);
    if (durableDirectAdmission) await refreshDirectLongGoalRecoveries();
    const words = String(trouble.message || trouble);
    sayInRuntimeChat(runtimeKey, words);
    if (!stoppedChatError(trouble)) showError(words);
    finishSwarmChatActivity(agentId, false, words, activity);
  } finally {
    finishSwarmActivityResponse(agentId, activity);
    if (swarmActivityIsCurrent(activity)) {
      swarmBusy.delete(runtimeKey);
      swarmStopping.delete(runtimeKey);
    }
    setWhatCanBePressedInSwarm();
  }
}

function wireUpTheTray() {
  if (!$("theChatTray")) return;
  $("theChatTrayBack").addEventListener("click", () => scrollTheTray(-320));
  $("theChatTrayOn").addEventListener("click", () => scrollTheTray(320));
  $("theBigChatSmall").addEventListener("click", minimiseTheBigChat);
  $("theBigChatShut").addEventListener("click", shutTheBigChat);
  $("theBigChatResetLayout").addEventListener("click", () => resetTheBigChatLayout());
  wireTheBigChatResizer("theBigChatWindowResize", "window");
  wireTheBigChatResizer("theBigChatSidebarResize", "sidebar");
  wireTheBigChatResizer("theBigChatActivityResize", "activity");
  wireTheBigChatResizer("theBigChatDestinationResize", "destination");
  wireTheBigChatResizer("theBigChatComposerResize", "composer");
  window.addEventListener("pointermove", moveTheBigChatResize);
  window.addEventListener("pointerup", finishTheBigChatResize);
  window.addEventListener("pointercancel", finishTheBigChatResize);
  window.addEventListener("resize", applyTheBigChatLayout);
  $("theBigChatSend").addEventListener("click", () => sendFromTheBigChat("chat"));
  $("theBigChatStop").addEventListener("click", () => stopChatFor(theBigOne));
  $("theBigChatAttach").addEventListener("click", () => $("theBigChatFiles").click());
  $("theBigChatFiles").addEventListener("change", async () => {
    await addChatAttachments(theBigOne, $("theBigChatFiles").files || []);
    $("theBigChatFiles").value = "";
  });
  $("theBigChatProject").addEventListener("change", () => (
    selectConversationProject(theBigOne, $("theBigChatProject").value)
  ));
  $("theBigChatRoundLimit").addEventListener("change", () => (
    updateChatRoundPolicy(theBigOne, false, $("theBigChatRoundLimit").value)
  ));
  $("theBigChatUnlimited").addEventListener("change", () => (
    updateChatRoundPolicy(
      theBigOne, $("theBigChatUnlimited").checked, $("theBigChatRoundLimit").value
    )
  ));
  $("theBigChatCollaborate").addEventListener("click", () => sendFromTheBigChat("collaborate"));
  $("theBigChatWork").addEventListener("click", () => sendFromTheBigChat("work"));
  // A provider WebContentsView can retain Electron's native keyboard target
  // after its visible view is moved back to the hidden relay host. In that
  // state this textarea has a DOM caret but typed keys still go to the hidden
  // provider page. Clicking the composer explicitly returns native keyboard
  // ownership to the board; the pointer's normal behavior still chooses the
  // caret position.
  $("theBigChatBox").addEventListener("pointerdown", () => {
    Promise.resolve(window.harnessDesktop?.focusHarness?.()).catch(() => {});
  });
  $("theBigChatBox").addEventListener("input", rememberTheBigChatComposer);
  $("theBigChatBox").addEventListener("select", rememberTheBigChatComposer);
  // Escape puts it back in the tray rather than closing it, because closing
  // would throw away which chats somebody had open.
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && theBigOne) minimiseTheBigChat();
  });
  document.addEventListener("keydown", keepTabInsideTheBigChat);
}

// ---- ordinary provider web chats ----------------------------------------
//
// Electron owns the authenticated browser session.  The Python agent loop
// owns collaboration.  This page is the small, token-protected courier
// between them: heartbeat the routes, claim a queued turn, hand it to the
// fixed Electron adapter, and return the visible reply.
let webChatConnections = [];
let webChatShown = null;
let webChatBridgeBusy = false;
let webChatBridgeTimer = 0;
let webChatHeartbeatTimer = 0;
let webChatHeartbeatBusy = false;
let webChatRouteSignature = "";
let webChatProviderChoices = [];
let webChatAssignTarget = null;
let webChatBackgroundMode = false;
let webChatSettingsRecoveryStatus = null;
let webChatSettingsRecoveryBusy = false;
let webChatSettingsRecoveryReadError = "";
const webChatRelayStatuses = new Map();
const WEB_CHAT_RELAY_STATUS_LIMIT = 24;

function canUseWebChats() {
  return Boolean(window.harnessDesktop?.webChats && window.harnessDesktop?.answerWebChat);
}

function webChatBounds() {
  const rect = $("webChatViewport").getBoundingClientRect();
  return {x: rect.left, y: rect.top, width: rect.width, height: rect.height};
}

async function refreshWebChatProviderChoices() {
  if (!canUseWebChats()) return [];
  webChatProviderChoices = await window.harnessDesktop.webChatProviders();
  if (thePickedAgent()) renderSwarmAgentPanel(thePickedAgent());
  return webChatProviderChoices;
}

async function refreshWebChatPreferences() {
  if (!window.harnessDesktop?.webChatPreferences) return {backgroundMode: false};
  const preferences = await window.harnessDesktop.webChatPreferences();
  webChatBackgroundMode = Boolean(preferences?.backgroundMode);
  const control = $("webChatBackgroundMode");
  if (control) control.checked = webChatBackgroundMode;
  return {backgroundMode: webChatBackgroundMode};
}

async function connectPickedAgentToWebProvider(providerId, options = {}) {
  const agent = thePickedAgent();
  const provider = webChatProviderChoices.find((one) => one.id === providerId);
  if (!agent || !provider) return false;
  const connectionId = String(options.connectionId || "");
  const conversationKey = String(options.conversationKey || "");
  const preferExisting = Boolean(options.preferExisting);
  webChatAssignTarget = {
    agentId: agent.id, providerId, connectionId, conversationKey,
  };
  renderSwarmAgentPanel(agent);
  sayInSwarm(`Sign in to ${provider.label} in the Nexus window, open or start a chat, then press Use this chat in Nexus.`);
  try {
    await window.harnessDesktop.connectWebChat(
      providerId,
      connectionId,
      conversationKey,
      preferExisting,
    );
    return true;
  } catch (error) {
    webChatAssignTarget = null;
    sayInSwarm(error.message || String(error));
    return false;
  }
}

async function assignSelectedWebChatToPendingAgent(chat) {
  const target = webChatAssignTarget;
  if (!target || target.providerId !== chat.provider) {
    return {matched: false, worked: false};
  }
  // Signing in or solving a provider challenge can legitimately take longer
  // than an arbitrary UI timeout. Keep this renderer-local intent until the
  // user makes an explicit provider selection, cancels it by choosing another
  // target, or restarts the app. Expiring it used to turn a late-but-valid
  // selection into a surprising new board agent.
  // Keep the exact selection until the board save is confirmed. If that save
  // is refused or the server is temporarily unavailable, the manager can show
  // a one-click retry instead of making the user reopen the provider and find
  // the conversation again.
  target.selectedChat = {...chat};
  target.lastError = "";
  if (target.connectionId && target.connectionId !== chat.id) {
    target.lastError = (
      `Nexus expected the saved provider connection ${target.connectionId}, but received ${chat.id}. `
      + "The agent route was not changed. Reopen the saved connection and try again."
    );
    sayInSwarm(target.lastError);
    renderWebChatConnections();
    return {matched: true, worked: false};
  }
  const route = `web:${chat.id}`;
  const current = theSwarmAgent(target.agentId);
  if (target.connectionId && current?.who === route) {
    webChatAssignTarget = null;
    pickSwarmBox("agent", current.id);
    sayInSwarm(`${chat.title || chat.provider} is reconnected to this agent's saved route.`);
    renderWebChatConnections();
    return {matched: true, worked: true};
  }
  const worked = await changeTheSwarmBoard((board) => {
    const agent = board.agents.find((one) => one.id === target.agentId);
    if (!agent) return false;
    agent.who = route;
  }, () => `${chat.title || chat.provider} is now the web AI chat used by this agent.`);
  const assigned = theSwarmAgent(target.agentId);
  if (worked && assigned) {
    webChatAssignTarget = null;
    pickSwarmBox("agent", assigned.id);
  } else {
    target.lastError = (
      `Nexus could not save this chat as ${assigned?.name || "the selected agent"}'s route. `
      + "The selected provider chat is retained below; retry the assignment or add it as a new agent."
    );
    sayInSwarm(target.lastError);
  }
  renderWebChatConnections();
  return {matched: true, worked: Boolean(worked && assigned)};
}

function acceptLocalWebChatConnections(chats) {
  webChatConnections = Array.isArray(chats) ? chats : [];
  if ($("webChatDialog")?.open) renderWebChatConnections();
  return webChatConnections;
}

async function refreshLocalWebChatConnections() {
  if (!canUseWebChats()) return [];
  return acceptLocalWebChatConnections(await window.harnessDesktop.webChats());
}

function webChatRecoveryNeedsChoice(status = webChatSettingsRecoveryStatus) {
  return Boolean(status && (
    status.resolution_required || status.requires_web_chat_resolution
  ));
}

function webChatRecoveryNeedsUpdate(status = webChatSettingsRecoveryStatus) {
  return Boolean(status && (
    status.update_required || status.write_blocked || status.state === "update_required"
  ));
}

function webChatRecoveryNeedsAttention(status = webChatSettingsRecoveryStatus) {
  return webChatRecoveryNeedsChoice(status) || webChatRecoveryNeedsUpdate(status);
}

function renderWebChatSettingsRecovery() {
  const card = $("webChatSettingsRecovery");
  const needsChoice = webChatRecoveryNeedsChoice();
  const needsUpdate = webChatRecoveryNeedsUpdate();
  const needsAttention = needsChoice || needsUpdate;
  const status = webChatSettingsRecoveryStatus || {};
  const count = Math.max(0, Number(status.recovered_web_chat_count || 0));
  const copies = status.copies_disagree || status.copies_currently_disagree
    ? "disagreed" : "could not both be read safely";
  const reasonCode = String(status.reason || "").trim();
  const reason = ({
    primary_missing: "The main settings copy was missing, so Nexus used the backup.",
    primary_invalid: "The main settings copy was corrupted or unreadable, so Nexus used the backup.",
    backup_newer: "The backup settings copy had the newer complete revision.",
    backup_selected: "The backup was the only settings copy Nexus could select safely.",
    backup_missing: "The backup settings copy was missing.",
    backup_invalid: "The backup settings copy was corrupted or unreadable.",
    primary_newer: "The main settings copy had the newer complete revision.",
    same_revision_disagreement: "Both settings copies claimed the same revision but contained different data.",
    copies_disagree: "The two saved settings copies contained different data.",
    both_copies_invalid: "Neither saved settings copy could be read safely.",
    primary_invalid_backup_missing: "The main settings copy was unreadable and its backup was missing.",
    primary_missing_backup_invalid: "The main settings copy was missing and its backup was unreadable.",
    no_valid_copy: "Nexus could not find a readable saved settings copy.",
  })[reasonCode] || reasonCode.replaceAll("_", " ");
  const noReadableCopy = status.selected_source === "none" || [
    "both_copies_invalid", "primary_invalid_backup_missing",
    "primary_missing_backup_invalid", "no_valid_copy",
  ].includes(reasonCode);
  const foundVersions = Array.isArray(status.found_format_versions)
    ? status.found_format_versions.map(Number).filter(Number.isFinite) : [];
  const versions = foundVersions.length ? foundVersions.join(", ") : "newer";
  const supportedVersion = Number(status.supported_format_version || status.format_version || 1);
  const message = needsUpdate
    ? `These desktop settings were written in newer format version ${versions}, but this Nexus `
      + `app supports version ${supportedVersion}. Nexus left both saved copies and their web-chat `
      + `routes untouched. Install or reopen the newer Nexus release; this older app keeps desktop `
      + `settings read-only.`
    : noReadableCopy
    ? `Nexus could not read a usable desktop settings copy. Web AI chats remain disconnected `
      + `instead of Nexus guessing. ${reason} Repair settings copies creates a clean matching `
      + `pair; no usable web chats were found to restore.`
    : (
      `Nexus quarantined ${count} last-known web AI chat${count === 1 ? "" : "s"} because `
      + `its saved desktop settings copies ${copies}. This prevents an uncertain or corrupted `
      + `copy from silently restoring the wrong provider conversations.${reason ? ` ${reason}` : ""}`
    );
  if (card) {
    card.hidden = !needsAttention;
    if (needsAttention) $("webChatSettingsRecoveryMessage").textContent = message;
    const title = $("webChatSettingsRecoveryTitle");
    if (title) title.textContent = needsUpdate
      ? "A newer Nexus version is required" : "Choose how to recover your web AI chats";
    const hint = $("webChatSettingsRecoveryHint");
    if (hint) hint.textContent = needsUpdate
      ? "This older app cannot safely change or convert these files. Close it and install or reopen the newer Nexus release."
      : "Nexus has not restored or deleted either copy automatically. Your other desktop settings are preserved whichever option you choose.";
  }

  const banner = $("webChatRecoveryBanner");
  if (banner) {
    banner.hidden = !needsAttention;
    if (needsAttention) $("webChatRecoveryBannerMessage").textContent = message;
    const title = $("webChatRecoveryBannerTitle");
    if (title) title.textContent = needsUpdate
      ? "A newer Nexus version is required" : "Web AI chats need a recovery choice";
  }

  const settingsCard = $("desktopSettingsRecoveryCard");
  if (settingsCard) {
    settingsCard.dataset.state = needsAttention || webChatSettingsRecoveryReadError
      ? "attention" : "ok";
    $("desktopSettingsRecoveryMessage").textContent = needsUpdate
      ? `${message} No recovery choice in this older app can safely convert that data.`
      : needsChoice
      ? `${message} Choose here, or open Web AI chats from the board.`
      : webChatSettingsRecoveryReadError
        ? `Nexus could not read the desktop recovery status. Nothing was changed: ${webChatSettingsRecoveryReadError}`
      : webChatSettingsRecoveryStatus
        ? "The saved desktop settings copies agree. No recovery action is needed."
        : "Desktop settings recovery is available in the Nexus desktop app.";
  }

  const restoreLabel = count > 0 ? "Restore recovered web chats" : "Repair settings copies";
  const restoreButtons = [
    $("webChatSettingsRecoveryRestore"), $("desktopSettingsRecoveryRestore"),
  ].filter(Boolean);
  const discardButtons = [
    $("webChatSettingsRecoveryDiscard"), $("desktopSettingsRecoveryDiscard"),
  ].filter(Boolean);
  for (const button of restoreButtons) {
    button.hidden = !needsChoice;
    button.disabled = webChatSettingsRecoveryBusy;
    button.textContent = webChatSettingsRecoveryBusy ? "Applying recovery choice…" : (
      button.id === "webChatSettingsRecoveryRestore" && count > 0
        ? "Restore these chats" : restoreLabel
    );
  }
  for (const button of discardButtons) {
    button.hidden = !needsChoice || count === 0;
    button.disabled = webChatSettingsRecoveryBusy;
    button.textContent = webChatSettingsRecoveryBusy
      ? "Applying recovery choice…"
      : button.id === "webChatSettingsRecoveryDiscard"
        ? "Start with no recovered web chats" : "Discard recovered web chats";
  }
  const bannerAction = $("webChatRecoveryBannerAction");
  if (bannerAction) {
    bannerAction.hidden = !needsAttention;
    bannerAction.disabled = webChatSettingsRecoveryBusy;
    bannerAction.textContent = needsUpdate ? "See update steps" : webChatSettingsRecoveryBusy
      ? "Applying recovery choice…" : count > 0 ? "Review recovery" : "Repair settings copies";
  }
  const backgroundMode = $("webChatBackgroundMode");
  if (backgroundMode) {
    backgroundMode.disabled = needsUpdate;
    backgroundMode.title = needsUpdate
      ? "Open these settings with the newer Nexus version before changing this preference." : "";
  }
}

function setWebChatSettingsRecoveryResult(message) {
  for (const id of ["webChatSettingsRecoveryResult", "desktopSettingsRecoveryResult"]) {
    const output = $(id);
    if (output) output.textContent = String(message || "");
  }
}

async function refreshWebChatSettingsRecoveryStatus() {
  if (!window.harnessDesktop?.desktopSettingsRecoveryStatus) {
    webChatSettingsRecoveryStatus = null;
    webChatSettingsRecoveryReadError = "";
    renderWebChatSettingsRecovery();
    return null;
  }
  try {
    const wasUnreadable = Boolean(webChatSettingsRecoveryReadError);
    webChatSettingsRecoveryStatus = (
      await window.harnessDesktop.desktopSettingsRecoveryStatus()
    ) || null;
    webChatSettingsRecoveryReadError = "";
    if (wasUnreadable) setWebChatSettingsRecoveryResult("");
    renderWebChatSettingsRecovery();
    return webChatSettingsRecoveryStatus;
  } catch (error) {
    webChatSettingsRecoveryReadError = error.message || String(error);
    renderWebChatSettingsRecovery();
    // Retain an already-visible unresolved card if this refresh fails. Hiding
    // it would imply that the quarantined chats had somehow been resolved.
    if (webChatRecoveryNeedsAttention()) {
      setWebChatSettingsRecoveryResult(
        `Nexus could not refresh the recovery status. Nothing was changed: ${error.message || error}`
      );
    } else {
      setWebChatSettingsRecoveryResult(
        `Nexus could not read the desktop recovery status. Nothing was changed: ${error.message || error}`
      );
    }
    throw error;
  }
}

async function resolveWebChatSettingsRecovery(action) {
  if (webChatSettingsRecoveryBusy || !webChatRecoveryNeedsChoice()) return false;
  if (!["restore", "discard_web_chats"].includes(action)) return false;
  if (!window.harnessDesktop?.resolveDesktopSettingsRecovery) {
    setWebChatSettingsRecoveryResult(
      "This Nexus window cannot apply the recovery choice. Restart the updated desktop app; nothing was changed."
    );
    return false;
  }
  const recoveredCount = Math.max(0, Number(
    webChatSettingsRecoveryStatus?.recovered_web_chat_count || 0
  ));
  webChatSettingsRecoveryBusy = true;
  setWebChatSettingsRecoveryResult(action === "restore"
    ? "Restoring the quarantined chat list…"
    : "Removing only the quarantined web-chat list…");
  renderWebChatSettingsRecovery();
  try {
    const result = await window.harnessDesktop.resolveDesktopSettingsRecovery(action);
    if (!result || !result.status) {
      throw new Error("The desktop recovery service did not return its final status.");
    }
    webChatSettingsRecoveryStatus = result.status;
    if (Array.isArray(result.connections)) acceptLocalWebChatConnections(result.connections);
    let refreshProblem = String(result.reload_error || "");
    try {
      await refreshLocalWebChatConnections();
    } catch (error) {
      const localRefreshError = error.message || String(error);
      refreshProblem = refreshProblem
        ? `${refreshProblem}; ${localRefreshError}` : localRefreshError;
    }
    if (webChatRecoveryNeedsChoice()) {
      throw new Error(
        result.status.reason || "The settings copies still need a recovery choice."
      );
    }
    renderWebChatConnections();
    renderWebChatSettingsRecovery();
    const confirmedRestoredCount = typeof result.restored_connection_count === "number"
      && Number.isFinite(result.restored_connection_count)
      ? Math.max(0, Math.min(recoveredCount, result.restored_connection_count)) : null;
    let message = "";
    if (result.changed === false) {
      message = "This recovery was already resolved by another Nexus window or an earlier attempt. Nexus reloaded the current saved chat list; no additional settings were changed.";
    } else if (action !== "restore") {
      message = "Started with no recovered web chats. Your other desktop settings were preserved.";
    } else if (recoveredCount === 0) {
      message = "Repaired the saved desktop settings copies. No web chats needed restoration.";
    } else if (refreshProblem && confirmedRestoredCount === null) {
      message = `Saved your choice to restore ${recoveredCount} recovered web AI chat entr${recoveredCount === 1 ? "y" : "ies"}. Nexus has not yet confirmed how many routes are reachable.`;
    } else if (confirmedRestoredCount === 0) {
      message = `No usable web AI chat routes were restored. Nexus safely ignored ${recoveredCount} saved entr${recoveredCount === 1 ? "y" : "ies"} that the current provider engine could not use.`;
    } else {
      const restoredCount = confirmedRestoredCount ?? recoveredCount;
      const unavailableCount = confirmedRestoredCount === null
        ? 0 : recoveredCount - confirmedRestoredCount;
      message = `Restored ${restoredCount} usable web AI chat${restoredCount === 1 ? "" : "s"} from the quarantined settings copy.`;
      if (unavailableCount) {
        message += ` ${unavailableCount} unavailable saved entr${unavailableCount === 1 ? "y was" : "ies were"} kept disconnected.`;
      }
    }
    if (refreshProblem) {
      message += ` The recovery choice was saved, but the local chat list could not refresh yet: ${refreshProblem}`;
    }
    setWebChatSettingsRecoveryResult(message);
    $("webChatSaid").textContent = message;
    void heartbeatWebChats(true, false).catch((error) => {
      const warning = `${message} Board availability sync will retry automatically: ${error.message || error}`;
      setWebChatSettingsRecoveryResult(warning);
      $("webChatSaid").textContent = warning;
    });
    return true;
  } catch (error) {
    const message = `Nothing was changed. Nexus could not apply that recovery choice: ${error.message || error}`;
    setWebChatSettingsRecoveryResult(message);
    $("webChatSaid").textContent = message;
    renderWebChatSettingsRecovery();
    return false;
  } finally {
    webChatSettingsRecoveryBusy = false;
    renderWebChatSettingsRecovery();
  }
}

async function heartbeatWebChats(refreshBoard = false, refreshLocal = true) {
  if (!canUseWebChats() || !token) return;
  const before = JSON.stringify(webChatConnections);
  if (refreshLocal) await refreshLocalWebChatConnections();
  const heartbeat = await request("/api/web-chats/heartbeat", {
    method: "POST", body: JSON.stringify({connections: webChatConnections}),
  });
  // A heartbeat is an availability signal only. Connected browser chats are
  // global Electron resources, while board membership is project-owned user
  // intent. Never materialize every global connection on whichever board is
  // currently open: that made removals reappear, contaminated other boards,
  // and let a full board block the courier before it could claim pending work.
  // Explicit "Use this chat" and "Add to board" actions own assignment.
  const routeSignature = JSON.stringify((heartbeat.routes || []).map((one) => ({
    route: one.route, provider: one.provider, title: one.title, url: one.url,
  })));
  const changed = before !== JSON.stringify(webChatConnections)
    || routeSignature !== webChatRouteSignature;
  webChatRouteSignature = routeSignature;
  if (!$("webChatDialog").hidden && $("webChatDialog").open) renderWebChatConnections();
  if ((refreshBoard || changed) && !$("swarmView").hidden) {
    await refreshSwarm(true);
    // Conversation destinations are server-computed snapshots. An already
    // open pair chat must be re-read too, or its header and controls can keep
    // claiming the web agent is missing after the heartbeat made it live.
    await Promise.all(swarmChats.map((held) => loadConversationsFor(held.agent, false)));
  }
}

function scheduleWebChatHeartbeat(refreshBoard = false, refreshLocal = true) {
  if (webChatHeartbeatBusy) return;
  webChatHeartbeatBusy = true;
  void heartbeatWebChats(refreshBoard, refreshLocal).catch((heartbeatError) => {
    if ($("webChatDialog").open) {
      $("webChatSaid").textContent = heartbeatError.message || String(heartbeatError);
    }
  }).finally(() => { webChatHeartbeatBusy = false; });
}

function webChatRelayKey(one) {
  return `${String(one?.request_id || "unknown-request")}\u0000${String(one?.route || "unknown-route")}`;
}

function renderWebChatRelayStatuses() {
  const section = $("webChatRelayActivity");
  const list = $("webChatRelayStatuses");
  if (!section || !list) return;
  section.hidden = webChatRelayStatuses.size === 0;
  list.replaceChildren();
  for (const status of [...webChatRelayStatuses.values()].reverse()) {
    const card = make("article", "web-chat-relay-status");
    card.dataset.state = status.state;
    card.dataset.terminal = status.terminal ? "true" : "false";
    card.append(make("strong", "", status.route));
    card.append(make("p", "", status.detail));
    const requestId = status.requestId === "unknown-request"
      ? "unknown request" : `request …${status.requestId.slice(-8)}`;
    card.append(make(
      "p", "hint",
      `${requestId} · ${status.terminal ? "finished" : "working"}`,
    ));
    list.append(card);
  }
}

function setWebChatRelayStatus(one, state, detail, terminal = false) {
  const key = webChatRelayKey(one);
  const record = {
    requestId: String(one?.request_id || "unknown-request"),
    route: String(one?.route || "unknown route"),
    state: String(state || "working"),
    detail: String(detail || ""),
    terminal: Boolean(terminal),
    updatedAt: Date.now(),
  };
  // Delete/reinsert moves an updated request to the top without allowing a
  // concurrent route's terminal result to replace it.
  webChatRelayStatuses.delete(key);
  webChatRelayStatuses.set(key, record);
  while (webChatRelayStatuses.size > WEB_CHAT_RELAY_STATUS_LIMIT) {
    webChatRelayStatuses.delete(webChatRelayStatuses.keys().next().value);
  }
  renderWebChatRelayStatuses();
  return record;
}

function webChatReceiptIsRetryable(error) {
  if (!error?.responseReceived) return true;
  const status = Number(error.status || 0);
  return status === 408 || status === 425 || status === 429 || status >= 500;
}

function waitForWebChatReceiptRetry(milliseconds) {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}

async function completeWebChatReceipt(one, receipt) {
  // This immutable body is retried only to reconcile the already-finished
  // provider attempt. The provider prompt is never submitted again here.
  const body = JSON.stringify({
    ...receipt, request_id: String(one?.request_id || ""),
  });
  const retryDelays = [0, 250, 900];
  let lastError = null;
  let attempts = 0;
  for (let attempt = 0; attempt < retryDelays.length; attempt += 1) {
    if (retryDelays[attempt]) await waitForWebChatReceiptRetry(retryDelays[attempt]);
    attempts = attempt + 1;
    try {
      const response = await request("/api/web-chats/complete", {
        method: "POST", body,
      });
      if (response?.accepted === true) {
        return {accepted: true, attempts: attempt + 1, reason: ""};
      }
      return {
        accepted: false,
        attempts: attempt + 1,
        reason: String(
          response?.reason || response?.error
          || "Nexus no longer recognizes this pending relay request.",
        ),
      };
    } catch (error) {
      lastError = error;
      if (!webChatReceiptIsRetryable(error) || attempt === retryDelays.length - 1) break;
    }
  }
  return {
    accepted: null,
    attempts,
    reason: String(lastError?.message || lastError || "Nexus receipt confirmation failed."),
  };
}

async function relayOneWebChatRequest(one) {
  const started = performance.now();
  let answer = "";
  let error = "";
  let model = "";
  let deliveryState = "";
  let failureCode = "";
  let diagnostics = {};
  setWebChatRelayStatus(
    one, "relaying", `Relaying this exact request through ${one.route}…`, false,
  );
  try {
    const said = await window.harnessDesktop.answerWebChat(
      one.route, one.prompt, one.attachments || [],
      one.conversation_key || "", Boolean(one.prefer_existing_conversation));
    answer = said.answer || "";
    model = said.model || "";
    error = said.error || "";
    deliveryState = said.delivery_state || "";
    failureCode = said.failure_code || "";
    diagnostics = said.diagnostics && typeof said.diagnostics === "object"
      ? said.diagnostics : {};
  } catch (trouble) {
    error = trouble.message || String(trouble);
    deliveryState = "unknown";
    failureCode = "electron_bridge_failure";
  }

  setWebChatRelayStatus(
    one,
    "confirming",
    error
      ? "The provider attempt ended locally. Confirming that exact failure with Nexus…"
      : "The visible provider reply was captured locally. Confirming receipt with Nexus…",
    false,
  );
  const confirmation = await completeWebChatReceipt(one, {
    answer, error, model,
    delivery_state: deliveryState, failure_code: failureCode, diagnostics,
    milliseconds: Math.round(performance.now() - started),
  });
  if (confirmation.accepted === true) {
    setWebChatRelayStatus(
      one,
      error ? "failed" : "confirmed",
      error
        ? `Nexus confirmed the stopped relay for this exact request: ${error}`
        : "Nexus confirmed receipt of the visible reply for this exact request.",
      true,
    );
    return confirmation;
  }
  if (confirmation.accepted === false) {
    setWebChatRelayStatus(
      one,
      "unreconciled",
      `The provider prompt was not resent. Nexus did not accept this receipt: ${confirmation.reason} `
        + "Inspect the affected chat before starting a new turn.",
      true,
    );
    return confirmation;
  }
  const failure = new Error(
    `Nexus could not confirm this receipt after ${confirmation.attempts} attempts: ${confirmation.reason}. `
    + "The provider prompt was not resent. Inspect the affected chat before starting a new turn."
  );
  failure.webChatTerminalDetail = failure.message;
  throw failure;
}

async function serviceWebChatBridge() {
  if (!canUseWebChats() || !token || webChatBridgeBusy) return;
  webChatBridgeBusy = true;
  let claimed = [];
  try {
    // Route availability refresh is useful but is not a prerequisite for
    // servicing work that the engine already queued. In particular, a stale
    // or temporarily failed heartbeat must not strand an otherwise reachable
    // browser conversation until the server-side provider wait expires.
    scheduleWebChatHeartbeat();
    const pending = await request("/api/web-chats/pending");
    claimed = pending.requests || [];
  } catch (error) {
    // The ordinary app still works in a browser, where there is deliberately
    // no Electron bridge. A transient server restart is retried at the next
    // heartbeat and should not cover the whole app with an unrelated error.
    if ($("webChatDialog").open) $("webChatSaid").textContent = error.message;
  } finally {
    // Only serialize claiming pending work. Provider turns can take minutes,
    // and holding this lock while one runs would leave every other parallel
    // web-chat turn unclaimed until its server-side timeout.
    webChatBridgeBusy = false;
  }
  const settled = await Promise.allSettled(claimed.map(async (one) => (
    relayOneWebChatRequest(one)
  )));
  settled.forEach((result, index) => {
    if (result.status !== "rejected") return;
    const one = claimed[index] || {};
    setWebChatRelayStatus(
      one,
      "unreconciled",
      String(
        result.reason?.webChatTerminalDetail || result.reason?.message || result.reason
        || "The relay ended before Nexus could confirm its receipt. The provider prompt was not resent.",
      ),
      true,
    );
  });
}

function startWebChatBridge() {
  if (!canUseWebChats() || !token) return;
  if (!webChatBridgeTimer) {
    webChatBridgeTimer = window.setInterval(serviceWebChatBridge, 1200);
  }
  if (!webChatHeartbeatTimer) {
    webChatHeartbeatTimer = window.setInterval(
      () => scheduleWebChatHeartbeat(), 4000);
  }
  scheduleWebChatHeartbeat();
  void serviceWebChatBridge();
}

async function addWebChatAgent(chat) {
  const route = `web:${chat.id}`;
  const already = theSwarmBoard().agents.find((one) => one.who === route);
  if (already) {
    pickSwarmBox("agent", already.id);
    sayInSwarm(`${already.name} is already on the board and is selected now.`);
    if ($("webChatDialog").open) $("webChatDialog").close();
    return true;
  }
  const base = String(chat.title || `${chat.provider} web`).replace(/[^A-Za-z0-9 _.-]/g, " ")
    .trim().replace(/\s+/g, " ").slice(0, 54) || `${chat.provider} web`;
  let name = base;
  const worked = await changeTheSwarmBoard((board) => {
    // The Electron event and a quick press on the fallback button may queue
    // together.  Check again when this change actually reaches the board.
    if (board.agents.some((one) => one.who === route)) return false;
    const taken = new Set(board.agents.map((one) => one.name.toLowerCase()));
    name = base;
    for (let number = 2; taken.has(name.toLowerCase()); number += 1) {
      name = `${base.slice(0, 54 - String(number).length)} ${number}`;
    }
    board.agents.push({id: "", name, who: route, job: "",
      at: aFreeSpotOnTheBoard("agent")});
  }, () => `${name} was added to the board with the connected ${chat.provider} web chat.`);
  const added = theSwarmBoard().agents.find((one) => one.who === route);
  if (added) pickSwarmBox("agent", added.id);
  if (worked && $("webChatDialog").open) $("webChatDialog").close();
  return Boolean(added);
}

function renderWebChatConnections() {
  const list = $("webChatConnections");
  list.replaceChildren();
  const pendingAssignment = webChatAssignTarget;
  if (pendingAssignment?.selectedChat && pendingAssignment.lastError) {
    const repair = make("article", "web-chat-assignment-repair");
    repair.append(make("strong", "", "Selected chat is safe — board assignment needs attention"));
    repair.append(make("p", "", pendingAssignment.lastError));
    const actions = make("div", "button-row");
    const retry = make("button", "primary", "Retry assignment");
    const addInstead = make("button", "", "Add as a new agent instead");
    retry.type = "button";
    addInstead.type = "button";
    retry.addEventListener("click", async () => {
      retry.disabled = true;
      const result = await assignSelectedWebChatToPendingAgent(
        pendingAssignment.selectedChat
      );
      if (!result.worked) renderWebChatConnections();
    });
    addInstead.addEventListener("click", async () => {
      addInstead.disabled = true;
      const added = await addWebChatAgent(pendingAssignment.selectedChat);
      if (added && webChatAssignTarget === pendingAssignment) webChatAssignTarget = null;
      renderWebChatConnections();
    });
    actions.append(retry, addInstead);
    repair.append(actions);
    list.append(repair);
  }
  const persistenceError = String(
    webChatConnections.find((one) => one?.persistence_error)?.persistence_error || ""
  );
  if (persistenceError) {
    list.append(make(
      "p", "problem web-chat-persistence-error",
      `${persistenceError} The connected chats remain open in this session. Fix disk access or space, then press Use this chat in Nexus again to save them for restart.`,
    ));
  }
  if (!webChatConnections.length) {
    list.append(make("p", "hint", "No web chats are connected yet. Open a provider above, log in, choose or start a chat, then press Use this chat in Nexus."));
    return;
  }
  for (const chat of webChatConnections) {
    const card = make("article", "web-chat-connection");
    card.append(make("strong", "", chat.title || chat.provider));
    card.append(make("p", "hint", `${chat.provider} web chat`));
    const actions = make("div", "button-row");
    const show = make("button", "", chat.external ? "Show secure browser" : "Show here");
    const open = make("button", "", "Open window");
    const onBoard = theSwarmBoard().agents.some((one) => one.who === `web:${chat.id}`);
    const agent = make("button", "primary", onBoard ? "On board" : "Add to board");
    const remove = make("button", "", "Disconnect");
    for (const button of [show, open, agent, remove]) button.type = "button";
    show.addEventListener("click", () => showFullWebChatInsideNexus(chat.id));
    open.addEventListener("click", () => window.harnessDesktop.openWebChatWindow(chat.id));
    agent.disabled = onBoard;
    agent.addEventListener("click", () => addWebChatAgent(chat));
    remove.addEventListener("click", async () => {
      if (!window.confirm(`Disconnect ${chat.title}? Its provider login remains in the isolated browser session.`)) return;
      await window.harnessDesktop.removeWebChat(chat.id);
      await refreshLocalWebChatConnections();
      void heartbeatWebChats(true, false).catch((error) => {
        $("webChatSaid").textContent = (
          `The chat was disconnected locally. Board availability sync will retry: ${error.message || error}`
        );
      });
    });
    actions.append(show, open, agent, remove);
    card.append(actions);
    list.append(card);
  }
}

async function openWebChatManager() {
  if (!canUseWebChats()) {
    sayInSwarm("Web AI chats are available in the Electron app. The browser version keeps desktop and command-line agents only.");
    return;
  }
  // Open first and render the Electron-owned snapshot already in memory. The
  // manager must never wait on Python to show a browser chat that Electron
  // already owns; heartbeat is a best-effort availability sync after this.
  $("webChatDialog").classList.remove("is-chat-viewing");
  $("webChatDialogTitle").textContent = "Web AI chats";
  renderWebChatConnections();
  renderWebChatRelayStatuses();
  renderWebChatSettingsRecovery();
  $("webChatSaid").textContent = "Reading connected chats from this computer…";
  if (!$("webChatDialog").open) $("webChatDialog").showModal();

  const [providersResult, preferencesResult, connectionsResult, recoveryResult] = await Promise.allSettled([
    refreshWebChatProviderChoices(), refreshWebChatPreferences(),
    refreshLocalWebChatConnections(),
    refreshWebChatSettingsRecoveryStatus(),
  ]);
  const providers = providersResult.status === "fulfilled" ? providersResult.value : [];
  const list = $("webChatProviders");
  list.replaceChildren();
  for (const provider of providers) {
    const card = make("div", "web-chat-provider");
    card.append(make("strong", "", provider.label));
    if (provider.external) card.append(make(
      "span", "hint", "Uses a Nexus-owned Chrome or Edge profile for secure sign-in."));
    const connect = make("button", "primary", "Open sign-in or choose a chat");
    connect.type = "button";
    connect.disabled = webChatRecoveryNeedsUpdate();
    if (connect.disabled) connect.title = (
      "Open these settings with the newer Nexus version before connecting a web AI chat."
    );
    connect.addEventListener("click", () => window.harnessDesktop.connectWebChat(provider.id));
    card.append(connect);
    list.append(card);
  }
  renderWebChatConnections();
  renderWebChatRelayStatuses();
  const persistenceError = String(
    webChatConnections.find((one) => one?.persistence_error)?.persistence_error || ""
  );
  const localError = connectionsResult.status === "rejected"
    ? connectionsResult.reason?.message || String(connectionsResult.reason) : "";
  const preferenceError = preferencesResult.status === "rejected"
    ? preferencesResult.reason?.message || String(preferencesResult.reason) : "";
  const providerError = providersResult.status === "rejected"
    ? providersResult.reason?.message || String(providersResult.reason) : "";
  const recoveryError = recoveryResult.status === "rejected"
    ? recoveryResult.reason?.message || String(recoveryResult.reason) : "";
  $("webChatSaid").textContent = webChatRecoveryNeedsUpdate()
    ? "A newer Nexus version is required. Desktop settings and saved web-chat routes remain untouched and read-only."
    : webChatRecoveryNeedsChoice()
    ? "Your saved web-chat list needs one recovery choice. Nothing has been restored or deleted automatically."
    : persistenceError
    ? `${persistenceError} Fix disk access or space, then press Use this chat in Nexus again.`
    : localError
      ? `Connected-chat state could not be read from Electron: ${localError}`
      : webChatConnections.length
        ? `${webChatConnections.length} web chat${webChatConnections.length === 1 ? " is" : "s are"} connected on this computer.`
        : providerError || preferenceError || recoveryError
          ? `Local provider controls need attention: ${providerError || preferenceError || recoveryError}`
          : "Connect a provider chat above.";
  void heartbeatWebChats(true, connectionsResult.status !== "fulfilled").catch((error) => {
    $("webChatSaid").textContent = (
      `Connected chats are shown from this computer. Board availability sync will retry automatically: ${error.message || error}`
    );
  });
}

async function showFullWebChatInsideNexus(
  id, conversationKey = "", preferExisting = false
) {
  if (!canUseWebChats()) {
    sayInSwarm("Full web AI chats are available inside the Electron app.");
    return false;
  }
  if (!$("webChatDialog").open) await openWebChatManager();
  else {
    try {
      await refreshLocalWebChatConnections();
    } catch (error) {
      $("webChatSaid").textContent = error.message || String(error);
    }
    void heartbeatWebChats(false, false).catch(() => {});
  }
  const chat = webChatConnections.find((one) => one.id === String(id));
  if (!chat) {
    const said = "That web AI chat is no longer connected. Choose it again below to view it.";
    $("webChatSaid").textContent = said;
    sayInSwarm(said);
    return false;
  }
  if (chat.external) {
    await window.harnessDesktop.openWebChatWindow(
      chat.id, String(conversationKey || ""), Boolean(preferExisting));
    const said = `${chat.title || chat.provider} opened in its secure Nexus browser window.`;
    $("webChatSaid").textContent = said;
    sayInSwarm(said);
    return true;
  }
  webChatShown = {
    id: chat.id,
    conversationKey: String(conversationKey || ""),
    preferExisting: Boolean(preferExisting),
  };
  $("webChatDialog").classList.add("is-chat-viewing");
  $("webChatDialogTitle").textContent = "Full web AI chat";
  $("webChatViewerTitle").textContent = chat.title || `${chat.provider} web chat`;
  $("webChatViewer").hidden = false;
  await new Promise((resolve) => requestAnimationFrame(resolve));
  try {
    const shown = await window.harnessDesktop.showWebChat(
      chat.id, webChatShown.conversationKey, webChatShown.preferExisting,
      webChatBounds());
    if (!shown) throw new Error("The provider page could not be shown.");
    return true;
  } catch (error) {
    await hideEmbeddedWebChat();
    const said = error.message || String(error);
    $("webChatSaid").textContent = said;
    sayInSwarm(said);
    return false;
  }
}

async function hideEmbeddedWebChat() {
  webChatShown = null;
  $("webChatDialog").classList.remove("is-chat-viewing");
  $("webChatDialogTitle").textContent = "Web AI chats";
  $("webChatViewer").hidden = true;
  if (canUseWebChats()) await window.harnessDesktop.hideWebChat();
}

async function closeWebChatManager() {
  await hideEmbeddedWebChat();
  $("webChatDialog").close();
  $("swarmWebChats").focus({preventScroll: true});
}

function wireUpWebChats() {
  $("swarmWebChats").addEventListener("click", openWebChatManager);
  $("webChatDialogClose").addEventListener("click", closeWebChatManager);
  $("webChatViewerClose").addEventListener("click", hideEmbeddedWebChat);
  $("webChatSettingsRecoveryRestore").addEventListener(
    "click", () => resolveWebChatSettingsRecovery("restore")
  );
  $("webChatSettingsRecoveryDiscard").addEventListener(
    "click", () => resolveWebChatSettingsRecovery("discard_web_chats")
  );
  $("desktopSettingsRecoveryRestore").addEventListener(
    "click", () => resolveWebChatSettingsRecovery("restore")
  );
  $("desktopSettingsRecoveryDiscard").addEventListener(
    "click", () => resolveWebChatSettingsRecovery("discard_web_chats")
  );
  $("webChatRecoveryBannerAction").addEventListener("click", () => {
    if (webChatRecoveryNeedsUpdate()) {
      switchView("settings");
      $("desktopSettingsRecoveryCard")?.scrollIntoView({block: "center"});
      return;
    }
    const count = Math.max(0, Number(
      webChatSettingsRecoveryStatus?.recovered_web_chat_count || 0
    ));
    if (count === 0) void resolveWebChatSettingsRecovery("restore");
    else void openWebChatManager();
  });
  $("webChatBackgroundMode").addEventListener("change", async (event) => {
    const enabled = Boolean(event.currentTarget.checked);
    try {
      const preferences = await window.harnessDesktop.setWebChatBackgroundMode(enabled);
      webChatBackgroundMode = Boolean(preferences?.backgroundMode);
      event.currentTarget.checked = webChatBackgroundMode;
      $("webChatSaid").textContent = webChatBackgroundMode
        ? "Relayed web AI turns will keep provider browser windows minimized."
        : "Relayed web AI turns may show their provider browser window.";
    } catch (error) {
      event.currentTarget.checked = webChatBackgroundMode;
      $("webChatSaid").textContent = error.message || String(error);
    }
  });
  $("webChatOpenWindow").addEventListener("click", async () => {
    if (!webChatShown) return;
    await window.harnessDesktop.openWebChatWindow(
      webChatShown.id, webChatShown.conversationKey, webChatShown.preferExisting);
    await hideEmbeddedWebChat();
  });
  $("webChatDialog").addEventListener("cancel", (event) => {
    event.preventDefault();
    closeWebChatManager();
  });
  window.addEventListener("resize", () => {
    if (webChatShown) window.harnessDesktop.resizeWebChat(
      webChatShown.id, webChatShown.conversationKey, webChatBounds());
  });
  if (window.harnessDesktop?.onWebChatsChanged) {
    window.harnessDesktop.onWebChatsChanged(async (chats, selected) => {
      acceptLocalWebChatConnections(chats);
      renderWebChatConnections();
      void refreshWebChatSettingsRecoveryStatus().catch(() => {});
      // "Use this chat in Nexus" is the user's choice to put this web AI on
      // the board.  Do not make them discover and press a second button in a
      // different window before anything visible happens. Apply that explicit
      // choice before heartbeat: a failed Python availability sync must not
      // prevent or erase the assignment the user just made in Electron.
      try {
        if (selected?.id) {
          const assignment = await assignSelectedWebChatToPendingAgent(selected);
          if (!assignment.matched) await addWebChatAgent(selected);
          if (assignment.matched && !assignment.worked) {
            $("webChatSaid").textContent = webChatAssignTarget?.lastError
              || "The selected chat is retained, but its board assignment needs attention.";
          }
        }
      } catch (error) {
        if (selected?.id && webChatAssignTarget?.providerId === selected.provider) {
          webChatAssignTarget.selectedChat = {...selected};
          webChatAssignTarget.lastError = (
            `The selected provider chat is retained, but Nexus could not apply it to the board: ${error.message || error}`
          );
        }
        $("webChatSaid").textContent = webChatAssignTarget?.lastError
          || `The selected chat is retained locally, but its board change failed: ${error.message || error}`;
        renderWebChatConnections();
      }
      void heartbeatWebChats(true, false).catch((error) => {
        if (webChatAssignTarget?.lastError) return;
        $("webChatSaid").textContent = (
          `The Electron chat selection is shown. Board availability sync will retry: ${error.message || error}`
        );
      });
    });
  }
  if (canUseWebChats()) {
    refreshWebChatProviderChoices().catch(() => {});
    void refreshWebChatSettingsRecoveryStatus().catch(() => {});
    startWebChatBridge();
    window.addEventListener("focus", () => void serviceWebChatBridge());
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") void serviceWebChatBridge();
    });
  }
}


function wireUpTheSwarmBoard() {
  swarmFullScreenHomes = [$("swarmPanel"), $("theBigChat"), $("theChatTray")].map((element) => ({
    element, parent: element.parentNode, next: element.nextSibling,
  }));
  makeSwarmBoardPannable();
  $("swarmFullScreen").addEventListener("click", toggleTheSwarmFullScreen);
  $("swarmPanelClose").addEventListener("click", closeTheSwarmPanel);
  document.addEventListener("fullscreenchange", () => {
    if (swarmIsFullScreen || document.fullscreenElement === $("swarmStage")) {
      showHowTheSwarmFillsTheScreen(document.fullscreenElement === $("swarmStage"));
    }
  });
  $("swarmZoomOut").addEventListener("click", () => setTheSwarmZoom(swarmZoom - 0.1));
  $("swarmZoomReset").addEventListener("click", () => setTheSwarmZoom(1));
  $("swarmZoomIn").addEventListener("click", () => setTheSwarmZoom(swarmZoom + 0.1));
  $("swarmFit").addEventListener("click", fitTheWholeSwarm);
  sayTheSwarmZoom();
  $("swarmAddAgent").addEventListener("click", addAnAgentToTheBoard);
  $("swarmAddProject").addEventListener("click", addAProjectToTheBoard);
  $("swarmRemoveAgent").addEventListener("click", removeTheSwarmAgent);
  $("swarmRemoveProject").addEventListener("click", removeTheSwarmProject);
  $("swarmTidy").addEventListener("click", tidyTheSwarmBoard);
  $("swarmRefresh").addEventListener("click", () => refreshSwarm());
  wireUpWebChats();
  $("swarmStart").addEventListener("click", setThemGoing);
  $("swarmWorkGoals").addEventListener("click", workOnEveryBoardGoal);
  $("longGoalForm").addEventListener("submit", startLongGoalFromComposer);
  $("longGoalClose").addEventListener("click", closeLongGoalComposer);
  $("longGoalCancel").addEventListener("click", closeLongGoalComposer);
  $("longGoalEditBoard").addEventListener("click", editLongGoalProjectOnBoard);
  $("longGoalCheckBoard").addEventListener("click", checkLongGoalBoard);
  $("longGoalProject").addEventListener("change", () => renderLongGoalTeam());
  $("longGoalLead").addEventListener("change", renderLongGoalComposerReadiness);
  $("longGoalParticipation").addEventListener("change", renderLongGoalComposerReadiness);
  $("longGoalText").addEventListener("input", renderLongGoalComposerReadiness);
  $("longGoalCriteria").addEventListener("input", renderLongGoalComposerReadiness);
  $("longGoalDialog").addEventListener("cancel", (event) => {
    event.preventDefault();
    closeLongGoalComposer();
  });
  $("longGoalDialog").addEventListener("close", () => {
    longGoalDialogInvoker?.focus?.({preventScroll: true});
  });
  $("swarmCancelGoals").addEventListener("click", cancelLongGoal);
  $("swarmLegacyGoals").addEventListener("click", workOnEveryBoardGoalLegacy);
  $("missionGoalSelect").addEventListener("change", (event) => {
    const goalId = String(event.target.value || "");
    localStorage.setItem(LONG_GOAL_SELECTED_KEY, goalId);
    void loadLongGoal(goalId, true);
  });
  $("missionRefresh").addEventListener("click", () => refreshLongGoals(true));
  $("missionProviderSetupReview").addEventListener("click", focusCurrentBoardGoalSetup);
  $("missionProviderSetupPrepare").addEventListener(
    "click", prepareNewGoalWithCurrentProviderSetup);
  $("missionPause").addEventListener("click", () => missionControl("pause"));
  $("missionResume").addEventListener("click", () => missionControl("resume"));
  $("missionCancel").addEventListener("click", cancelLongGoal);
  $("missionFork").addEventListener("click", () => missionControl("fork"));
  $("missionEventFilter").addEventListener("change", renderMissionEvents);
  $("missionSteerSend").addEventListener("click", async () => {
    const text = $("missionSteer").value.trim();
    if (!text) return showError("Write the steering instruction first.");
    await missionControl("steer", {text, task_id: selectedMissionTaskId});
    $("missionSteer").value = "";
  });
  $("missionMessageAgent").addEventListener("click", async () => {
    const task = longGoal?.tasks?.find((one) => one.id === selectedMissionTaskId);
    const text = $("missionSteer").value.trim();
    if (!task || !text) return showError("Select a task and write a message first.");
    await missionControl("message", {text, task_id: task.id, agent_id: task.assigned_agent_id});
    $("missionSteer").value = "";
  });
  $("missionRequestReview").addEventListener("click", async () => {
    const task = longGoal?.tasks?.find((one) => one.id === selectedMissionTaskId);
    const owner = longGoal?.agents?.find((one) => one.id === task?.assigned_agent_id);
    const reviewer = longGoal?.agents?.find(
      (one) => one.id !== task?.assigned_agent_id
        && one.provider_identity_sha256
        && owner?.provider_identity_sha256
        && one.provider_identity_sha256 !== owner.provider_identity_sha256);
    if (!task || !reviewer) return showError(
      "Select a task with an authorized agent on a different provider backend. Route aliases for the same backend are not independent reviewers."
    );
    await missionControl("request_review", {task_id: task.id, agent_id: reviewer.id});
  });
  $("missionCriteriaSave").addEventListener("click", async () => {
    const successCriteria = $("missionCriteria").value.split(/\r?\n/).map((one) => one.trim()).filter(Boolean);
    await missionControl("criteria", {success_criteria: successCriteria});
  });
  $("missionReassign").addEventListener("click", async () => {
    if (!selectedMissionTaskId) return showError("Select a task first.");
    await missionControl("reassign", {
      task_id: selectedMissionTaskId, agent_id: $("missionReassignAgent").value,
    });
  });
  $("missionRetry").addEventListener("click", async () => {
    const task = longGoal?.tasks?.find((one) => one.id === selectedMissionTaskId);
    if (!task) return showError("Select a blocked or failed task first.");
    const needsReconciliation = task.outcome_unknown === true
      || task.reconciliation_required === true;
    const reconciled = needsReconciliation && window.confirm(
      "This task has a prior provider or file effect that Nexus will not repeat automatically. Retry only after inspecting the saved provider conversation and project state. Confirm that you reconciled the original effect and want one fresh attempt?"
    );
    if (needsReconciliation && !reconciled) return;
    await missionControl("retry", {task_id: task.id, reconciled});
  });
  $("swarmStop").addEventListener("click", stopThemGoing);
  $("swarmAgentSave").addEventListener("click", saveTheSwarmAgent);
  $("swarmAgentWho").addEventListener("change", async (event) => {
    const value = String(event.target.value || "");
    if (value.startsWith("__connect_web__:")) {
      const agent = thePickedAgent();
      if (agent) await flushSwarmAgentSettings(agent.id);
      await connectPickedAgentToWebProvider(value.slice("__connect_web__:".length));
    } else {
      rememberSwarmAgentSettings(0);
    }
  });
  for (const id of ["swarmAgentName", "swarmAgentJob"]) {
    $(id).addEventListener("input", () => {
      if (id === "swarmAgentName") previewSwarmAgentAppearance();
      if (id === "swarmAgentJob") renderDisclosedTextCount(
        "swarmAgentJob", "swarmAgentJobCount",
        AGENT_JOB_CHARACTER_LIMIT, "the role description");
      rememberSwarmAgentSettings(550);
    });
    // Leaving a text field is a natural save boundary; do not make a quick
    // panel switch race the debounce timer.
    $(id).addEventListener("change", () => rememberSwarmAgentSettings(0));
  }
  $("swarmAgentIcon").addEventListener("change", () => {
    previewSwarmAgentAppearance();
    rememberSwarmAgentSettings(0);
  });
  for (const id of [
    "swarmAgentColour", "swarmAgentBubbleColour",
    "swarmAgentPictureZoom", "swarmAgentPictureHue",
  ]) {
    $(id).addEventListener("input", () => {
      previewSwarmAgentAppearance();
      rememberSwarmAgentSettings(250);
    });
    $(id).addEventListener("change", () => rememberSwarmAgentSettings(0));
  }
  $("swarmAgentPictureBrowse").addEventListener(
    "click", () => $("swarmAgentPictureFile").click());
  $("swarmAgentPictureFile").addEventListener("change", useAgentPictureFile);
  $("swarmAgentPictureClear").addEventListener("click", () => {
    swarmAgentPictureDraft = "";
    $("swarmAgentPictureSaid").textContent =
      "Using the fallback icon. This change is saving automatically.";
    previewSwarmAgentAppearance();
    rememberSwarmAgentSettings(0);
  });
  $("swarmAgentRemove").addEventListener("click", removeTheSwarmAgent);
  $("swarmOpenChat").addEventListener("click", () => {
    const agent = thePickedAgent();
    if (agent) openTheChatFor(agent.id);
  });
  $("swarmProjectRemove").addEventListener("click", removeTheSwarmProject);
  $("swarmProjectRebind").addEventListener("click", rebindTheSwarmProject);
  $("swarmProjectVerificationApprove").addEventListener(
    "click", () => setProjectVerificationApproval(true));
  $("swarmProjectVerificationRevoke").addEventListener(
    "click", () => setProjectVerificationApproval(false));
  $("swarmProjectVerificationRefresh").addEventListener(
    "click", () => refreshSwarm());
  $("swarmAddTask").addEventListener("click", addOneSwarmTask);
  $("swarmTaskText").addEventListener("input", () => renderDisclosedTextCount(
    "swarmTaskText", "swarmTaskTextCount",
    BOARD_TASK_CHARACTER_LIMIT, "the project job"));
  $("swarmTaskText").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
      event.preventDefault(); addOneSwarmTask();
    }
  });
  $("swarmLineOn").addEventListener("change", () => setThePickedLine($("swarmLineOn").checked));
  $("swarmLineRemove").addEventListener("click", () => setThePickedLine(false));
  $("swarmExchangeRefresh").addEventListener("click", refreshWhatTheySaidToEachOther);
  $("swarmExchange").addEventListener("toggle", () => {
    if ($("swarmExchange").open) refreshWhatTheySaidToEachOther();
  });
  // The lines are drawn where the boxes really are, so a board that changes
  // shape has to draw them again.
  window.addEventListener("resize", () => {
    if (!$("swarmView").hidden) drawSwarmLines();
  });
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState !== "hidden") return;
    const agent = thePickedAgent();
    if (agent) void flushSwarmAgentSettings(agent.id);
  });
  window.addEventListener("pagehide", () => {
    const agent = thePickedAgent();
    if (agent) void flushSwarmAgentSettings(agent.id);
  });
}

boot();
