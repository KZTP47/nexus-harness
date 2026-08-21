"use strict";

const $ = (id) => document.getElementById(id);
const svgNS = "http://www.w3.org/2000/svg";
const agentTypes = new Set(["planner", "coder", "evaluator", "merge"]);
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
  try { value = await response.json(); } catch (_) { throw new Error(`HTTP ${response.status}`); }
  if (!response.ok) throw new Error(value.error || `HTTP ${response.status}`);
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
function askForOneLine(title, question, value = "") {
  return new Promise((finish) => {
    const box = $("askDialog");
    $("askDialogTitle").textContent = title;
    $("askDialogWhy").textContent = question || "";
    $("askDialogWhy").hidden = !question;
    $("askDialogInput").value = value == null ? "" : String(value);
    const done = () => {
      box.removeEventListener("close", done);
      finish(box.returnValue === "ok" ? $("askDialogInput").value : null);
    };
    box.addEventListener("close", done);
    box.showModal();
    $("askDialogInput").focus();
    $("askDialogInput").select();
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
  fillAgentSelect($("agentRef")); fillProviderSelect($("agentProvider")); updateModelSuggestions($("agentProvider").value, $("agentModel")); renderCapabilityChecks($("agentCapabilities"), type === "coder" ? ["workspace.read", "workspace.write"] : ["workspace.read"]);
  $("agentMergeFields").hidden = type !== "merge"; $("agentFormError").hidden = true; $("agentFormError").replaceChildren();
  for (const field of $("agentForm").querySelectorAll("[aria-invalid]")) { field.removeAttribute("aria-invalid"); field.removeAttribute("aria-describedby"); }
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
  const invalid = !label ? ["agentLabel", "Enter a label."] : !provider ? ["agentProvider", "Choose a provider route."] : providerById(provider)?.graph_routing_allowed === false ? ["agentProvider", "This route does not allow submitted workflow graphs."] : !model ? ["agentModel", "Enter a model."] : !role ? ["agentRoleName", "Enter a role."] : null;
  if (invalid) {
    const [fieldId, message] = invalid; const field = $(fieldId); const link = make("a", "", message); link.href = `#${fieldId}`; link.addEventListener("click", (click) => { click.preventDefault(); field.focus(); });
    field.setAttribute("aria-invalid", "true"); field.setAttribute("aria-describedby", "agentFormError"); $("agentFormError").replaceChildren(link); $("agentFormError").hidden = false; $("agentFormError").focus(); return;
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
    if (isAgent) { const agentRef = node.config?.agent_ref || ""; const assignment = agentById(agentRef); fillAgentSelect($("nodeAgentRef"), agentRef); fillProviderSelect($("nodeProvider"), assignment?.provider_route || node.config?.provider_route || ""); $("nodeProvider").disabled = Boolean(assignment); $("nodeModel").value = assignment?.model || node.config?.model || ""; $("nodeModel").disabled = Boolean(assignment); $("nodeRoleName").value = node.config?.role_name || assignment?.role || ""; $("nodePrompt").value = node.config?.system_prompt || ""; renderCapabilityChecks($("nodeCapabilities"), node.config?.capabilities || [], assignment?.capabilities || null); $("mergeFields").hidden = node.type !== "merge"; $("mergeSlots").value = (node.config?.required_slots || []).join(", "); $("mergeOutput").value = node.config?.output_field || "merged_output"; }
  }
  if (selected?.kind === "edge") { const edge = graph.edges.find((item) => item.id === selected.id); if (!edge) return; $("edgeMode").value = edge.mode || "state"; $("edgeCondition").value = edge.condition || ""; $("edgeVariables").value = (edge.variables || []).join(", "); $("edgeTargetSlot").value = edge.target_slot || ""; $("edgeReturnFields").value = (edge.return_fields || []).join(", "); $("maxIterations").value = edge.loop?.max_iterations || ""; $("temperatureDecay").value = edge.loop?.temperature_decay || ""; $("loopTimeout").value = edge.loop?.timeout_seconds || ""; }
}
function updateSelectedNode() {
  if (selected?.kind !== "node") return; const node = nodeById(selected.id); pushHistory(); node.label = $("nodeLabel").value.trim() || node.id; node.config = {...(node.config || {})};
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
async function startRun() { const task = $("taskInput").value.trim(); if (!task) { showError("Enter a task before starting a run."); $("taskInput").focus(); return; } const checked = await validate(); if (!checked.valid) return; try { await request("/api/run", {method: "POST", body: JSON.stringify({task, dry_run: $("dryRunInput").checked, graph})}); announce("Run accepted. Events will appear in the run log."); appendEvent("run", "Accepted"); } catch (error) { showError(error.message); } }

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

function switchView(name) { document.querySelectorAll("[data-view]").forEach((button) => button.setAttribute("aria-pressed", String(button.dataset.view === name))); document.querySelectorAll("[data-view-panel]").forEach((panel) => { panel.hidden = panel.dataset.viewPanel !== name; }); $("workflowActions").hidden = name !== "workflow"; if (name === "memory") refreshMemory(); if (name === "prompts") refreshPrompts(); if (name === "start") { refreshCheckup(); refreshHowItWorks(); } if (name === "checks") { refreshChecks(); $("starterUrl").placeholder = window.location.origin + "/"; } if (name === "workflow") { fitGraph(); refreshTeamNotes(); renderWhatItIsDoing(); refreshWorkflows(); } if (name === "history") refreshHistory(); if (name === "pipelines") refreshPipelines(); if (name === "settings") refreshSettings(); if (name === "vault") refreshVault(vaultOpen); if (name === "team") refreshTeam(teamOpen); if (name === "lookup") refreshLookup(); if (name === "talk") refreshTalk(); if (name === "swarm") refreshSwarm(); }

/* ---- Start here: one plain-language answer to "is this ready?" ---- */

let checkup = null;
let qaSuite = {present: false, cases: [], tags: []};
let qaResult = null;

async function refreshCheckup(fresh = false) {
  try {
    checkup = await request(`/api/checkup${fresh ? "?refresh=1" : ""}`);
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
    head.append(
      make("span", "step-mark", option.state === "ready" ? "Ready" : "To do"),
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
      const button = make("button", "do-it-button", "I don't care, just do it for me");
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
  const task = $("quickTask").value.trim();
  if (!task) { showError("Say what the harness should do first."); $("quickTask").focus(); return; }
  try {
    await request("/api/run", {method: "POST", body: JSON.stringify({task, dry_run: $("quickDryRun").checked})});
    announce("Started. Open Workflow to watch the run log.");
    appendEvent("run", "Accepted");
    switchView("workflow");
  } catch (error) { showError(error.message); }
}

/* ---- Checks ---- */

async function refreshChecks() {
  try {
    qaSuite = await request("/api/qa/suite");
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
    row.append(make("span", "seat-state", seat.ready ? "Ready" : "Not here"));
    const detail = make("div", "");
    detail.append(make("strong", "", seat.label));
    detail.append(make("p", "", seat.ready
      ? `${seat.version} — found at ${seat.found_at}`
      : seat.why_not));
    if (!seat.ready && seat.install_hint) detail.append(make("p", "field-help", seat.install_hint));
    if (seat.ready && seat.already_set_up) detail.append(make("p", "field-help", "A route for this is already in your settings."));
    row.append(detail);
    list.append(row);
  }
  const ready = (found.seats || []).filter((seat) => seat.ready);
  $("seatFindSaid").textContent = ready.length
    ? `${ready.length} of ${(found.seats || []).length} are ready to use.`
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
  } catch (error) { showError(error.message); $("vaultSaid").textContent = error.message; }
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

async function refreshSettings() {
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
    return `${one.key} ${one.label} ${one.means}`.toLowerCase().includes(looking);
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
let pipelineStarters = [];
let pipelineWhens = [];   // when a step runs: always, only on failure, either way
let pipelineWaits = [];   // how long it waits before trying again
// Which saved pipeline is on the board, if it came from one. Older versions
// belong to a saved name, so without this there is nothing to look back at.
let pipelineSavedName = "";
let pipelineJoining = "";      // the node an arrow is being drawn from
let pipelineDragging = null;
let pipelineEditing = "";
let pipelineStates = new Map();

async function refreshPipelines(name) {
  try {
    const said = await request(`/api/pipelines${name ? `?name=${encodeURIComponent(name)}` : ""}`);
    pipelineKinds = said.kinds || [];
    pipelineSaved = said.saved || [];
    pipelineStarters = said.starters || [];
    pipelineWhens = said.when_it_runs || [];
    pipelineWaits = said.waits || [];
    pipeline = said.pipeline;
    pipelineStates = new Map();
    pipelineSavedName = name || "";
    pipelineOlderOnes = said.older_ones || [];
    $("pipelineName").value = pipeline.name || "";
    $("pipelineStop").disabled = !said.running;
    renderPipelinePalette();
    renderPipelineStarters();
    renderPipelineSaved();
    renderPipeline();
    if (said.last_run) showPipelineRun(said.last_run);
  } catch (error) { showError(error.message); }
}

function renderPipelineSaved() {
  const list = $("pipelineList");
  list.replaceChildren();
  if (!pipelineSaved.length) {
    list.append(make("li", "hint", "None saved yet. Draw one and press Save."));
    return;
  }
  for (const name of pipelineSaved) {
    const item = make("li", "");
    const button = make("button", `pipeline-saved-one${name === pipeline.name ? " chosen" : ""}`, name);
    button.type = "button";
    button.addEventListener("click", () => refreshPipelines(name));
    item.append(button);
    list.append(item);
  }
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
  if (pipeline.nodes.length
      && !window.confirm(`Replace what is on the board with "${starter.title}"?`)) return;
  try {
    const said = await request("/api/pipelines/starter", {
      method: "POST", body: JSON.stringify({key: starter.key}),
    });
    pipeline = said.pipeline;
    pipelineStates = new Map();
    $("pipelineName").value = pipeline.name;
    $("pipelineLog").replaceChildren();
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
  const box = $("pipelineNodes");
  const wires = $("pipelineWires");
  box.replaceChildren(wires);
  for (const node of pipeline.nodes) {
    const kind = kindOf(node.kind);
    const card = make("div", `pipeline-node colour-${kind.colour}`);
    card.dataset.node = node.id;
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
    join.title = "Draw an arrow from this step to another";
    join.addEventListener("click", (event) => { event.stopPropagation(); joinPipelineNodes(node.id); });
    const settings = make("button", "pipeline-node-button", "Settings");
    settings.type = "button";
    settings.addEventListener("click", (event) => { event.stopPropagation(); openPipelineNode(node.id); });
    const alone = make("button", "pipeline-node-button", "Run only this");
    alone.type = "button";
    alone.title = "Run this one step and nothing else, while you are building it";
    alone.addEventListener("click", (event) => {
      event.stopPropagation();
      runPipeline({only: node.id});
    });
    const onward = make("button", "pipeline-node-button", "Carry on from here");
    onward.type = "button";
    onward.title = "Run this step and everything after it, leaving the earlier ones alone";
    onward.addEventListener("click", (event) => {
      event.stopPropagation();
      runPipeline({from_here: node.id});
    });
    const remove = make("button", "pipeline-node-button", "Remove");
    remove.type = "button";
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
  drawPipelineWires();
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
    cut.addEventListener("click", () => {
      pipeline.edges = pipeline.edges.filter((item) => !(item.from === edge.from && item.to === edge.to));
      renderPipeline();
      say(`Took the arrow from ${edge.from} to ${edge.to} out.`);
    });
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
  const box = $("pipelineCanvas").getBoundingClientRect();
  pipelineDragging = {
    node,
    grabX: event.clientX - box.left - (node.at?.x || 0),
    grabY: event.clientY - box.top - (node.at?.y || 0),
  };
  card.setPointerCapture(event.pointerId);
  card.classList.add("moving");
}

function movePipelineDrag(event) {
  if (!pipelineDragging) return;
  const box = $("pipelineCanvas").getBoundingClientRect();
  const node = pipelineDragging.node;
  node.at = {
    x: Math.max(0, event.clientX - box.left - pipelineDragging.grabX),
    y: Math.max(0, event.clientY - box.top - pipelineDragging.grabY),
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

/* ---- one step's settings ---- */

const PIPELINE_FIELDS = {
  suite: {label: "Suite file, if not the usual one", placeholder: ".harness/qa/suite.json"},
  tag: {label: "Only checks with this tag", placeholder: "fast"},
  case: {label: "Only this one check", placeholder: "readme"},
  paths: {label: "Only these files or folders, separated by commas", placeholder: "src, tests"},
  needs: {label: "How much has to pass", choices: ["all", "any"]},
  command_kind: {label: "Which command", choices: ["test", "lint", "build"]},
  instructions: {label: "What the model should write", long: true,
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
    say(`Saved ${pipeline.name}.`);
    const list = await request(`/api/pipelines?name=${encodeURIComponent(pipeline.name)}`);
    pipelineSaved = list.saved || [];
    pipelineOlderOnes = list.older_ones || [];
    renderPipelineSaved();
    if (pipelineLooking === "before") listHowItLookedBefore();
  } catch (error) { say(error.message); showError(error.message); }
}

async function savePipelineAs() {
  const name = await askForOneLine(
       "Save a copy", "What should the copy be called?",
       `${$("pipelineName").value} copy`);
  if (!name) return;
  $("pipelineName").value = name;
  await savePipeline();
}

async function deletePipeline() {
  const name = $("pipelineName").value.trim();
  if (!name) return;
  if (!window.confirm(`Remove the saved pipeline "${name}"? The drawing on screen stays.`)) return;
  try {
    const said = await request("/api/pipelines/delete", {method: "POST", body: JSON.stringify({name})});
    pipelineSaved = said.saved || [];
    renderPipelineSaved();
    say(said.note);
  } catch (error) { say(error.message); showError(error.message); }
}

function newPipeline() {
  pipeline = {name: "New pipeline", nodes: [], edges: []};
  pipelineStates = new Map();
  $("pipelineName").value = pipeline.name;
  $("pipelineLog").replaceChildren();
  renderPipeline();
  renderPipelineSaved();
  say("A fresh one. Add a step from the left.");
}

async function runPipeline(options = {}) {
  try {
    pipelineStates = new Map();
    showWhatIsBeingAsked("");
    $("pipelineLog").replaceChildren();
    renderPipeline();
    await request("/api/pipelines/run", {
      method: "POST",
      body: JSON.stringify({
        pipeline: pipelineOnScreen(),
        // Three ways to run less than the whole thing. Left out when not
        // asked for, so an ordinary Run is exactly what it always was.
        ...(options.from_here ? {from_here: options.from_here} : {}),
        ...(options.only ? {only: options.only} : {}),
        ...(options.answers ? {answers: options.answers} : {}),
      }),
    });
    $("pipelineStop").disabled = false;
    say(options.only
      ? "Running that one step on its own."
      : options.from_here
        ? "Carrying on from that step. The ones before it are left as they were."
        : "Running. Each step lights up as it goes.");
  } catch (error) { say(error.message); showError(error.message); }
}

async function stopPipeline() {
  try {
    const said = await request("/api/pipelines/stop", {method: "POST", body: "{}"});
    say(said.note);
  } catch (error) { showError(error.message); }
}

// News from a run, arriving while it happens.
function applyPipelineEvent(event) {
  if (event.kind === "pipeline_started") {
    $("pipelineStop").disabled = false;
    say(`Running ${event.payload?.name || "the pipeline"}.`);
    return;
  }
  if (event.kind === "pipeline_node") {
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
    $("pipelineStop").disabled = true;
    showWhatIsBeingAsked("");
    showPipelineRun(event.payload || {});
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
  document.querySelectorAll("[data-node-type]").forEach((button) => { button.addEventListener("click", () => addNode(button.dataset.nodeType, 360, 300, button)); button.addEventListener("dragstart", (event) => event.dataTransfer.setData("application/x-harness-node", button.dataset.nodeType)); });
  document.querySelectorAll("[data-view]").forEach((button) => button.addEventListener("click", () => switchView(button.dataset.view)));
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
  $("agentDialog").addEventListener("close", () => dialogInvoker?.focus?.());
  ["nodeLabel", "nodeProvider", "nodeModel", "nodeRoleName", "nodePrompt", "nodeRole", "mergeSlots", "mergeOutput"].forEach((id) => $(id).addEventListener("change", updateSelectedNode)); $("nodeCapabilities").addEventListener("change", updateSelectedNode); $("nodeAgentRef").addEventListener("change", () => { applyAgentAssignment($("nodeAgentRef"), $("nodeProvider"), $("nodeModel"), $("nodeRoleName"), $("nodeCapabilities")); updateSelectedNode(); });
  ["edgeMode", "edgeCondition", "edgeVariables", "edgeTargetSlot", "edgeReturnFields", "maxIterations", "temperatureDecay", "loopTimeout"].forEach((id) => $(id).addEventListener("change", updateSelectedEdge)); $("deleteNode").addEventListener("click", () => selected?.kind === "node" && removeNode(selected.id)); $("deleteEdge").addEventListener("click", () => selected?.kind === "edge" && removeEdge(selected.id));
  $("newWorkflow").addEventListener("click", newWorkflow); $("saveWorkflow").addEventListener("click", saveWorkflow); $("renameWorkflow").addEventListener("click", renameWorkflow); $("deleteWorkflow").addEventListener("click", deleteWorkflow);
  $("refreshHistory").addEventListener("click", refreshHistory); $("refreshCheckup").addEventListener("click", () => refreshCheckup(true)); $("quickRun").addEventListener("click", quickRun); $("quickChecks").addEventListener("click", () => { switchView("checks"); runChecks(); });
  document.querySelectorAll("[data-example]").forEach((button) => button.addEventListener("click", () => { $("quickTask").value = button.dataset.example; $("quickTask").focus(); }));
  window.addEventListener("resize", () => { if (howStages.length) hideArrowsAtTheEndOfARow(); }); $("showMeAround").addEventListener("click", showMeAround); $("vaultNew").addEventListener("click", newVaultNote); $("vaultLearn").addEventListener("click", vaultLearnFromRuns); $("vaultRedraw").addEventListener("click", () => { vaultPlaces = new Map(); settleTheVault(); }); $("vaultEdit").addEventListener("click", editVaultNote); $("vaultRemove").addEventListener("click", removeVaultNote); $("vaultUsedWell").addEventListener("click", () => vaultNoteWasUsed(true)); $("vaultUsedBadly").addEventListener("click", () => vaultNoteWasUsed(false)); $("vaultClose").addEventListener("click", () => { $("vaultNote").hidden = true; vaultOpen = ""; renderVaultList(); drawTheVault(); }); $("vaultFormSave").addEventListener("click", saveVaultNote); $("vaultFormCancel").addEventListener("click", () => $("vaultDialog").close()); $("vaultSearch").addEventListener("input", (event) => { vaultLooking = event.target.value; renderVaultList(); settleTheVaultSoon(); if (vaultNotes.length >= MOST_TO_DRAW || vaultAskingFor) { vaultAskingFor = event.target.value.trim(); refreshVault(vaultOpen); } }); $("vaultOnlyNear").addEventListener("change", () => { renderVaultList(); settleTheVault(); }); $("vaultGraph").addEventListener("keydown", vaultGraphKey); $("refreshSettings").addEventListener("click", refreshSettings); $("settingsFilter").addEventListener("input", renderSettings); $("settingsChangedOnly").addEventListener("change", renderSettings); $("pipelineSave").addEventListener("click", savePipeline); $("pipelineSaveAs").addEventListener("click", savePipelineAs); $("pipelineRun").addEventListener("click", () => runPipelineAsking()); $("pipelineStop").addEventListener("click", stopPipeline); $("pipelineDelete").addEventListener("click", deletePipeline); $("pipelineNew").addEventListener("click", newPipeline); $("pipelineCheck").addEventListener("click", checkPipeline); $("pipelineNodeSave").addEventListener("click", savePipelineNode); $("pipelineNodeCancel").addEventListener("click", () => $("pipelineNodeDialog").close()); document.addEventListener("pointermove", movePipelineDrag); document.addEventListener("pointerup", endPipelineDrag); $("howDemo").addEventListener("click", demoHowItWorks); $("howRefresh").addEventListener("click", refreshHowItWorks); $("findSeats").addEventListener("click", findSeats); $("setUpSeats").addEventListener("click", setUpSeats); $("shareTheWork").addEventListener("click", shareTheWork); $("undoSeats").addEventListener("click", undoSeats); $("createSuite").addEventListener("click", createSuite); $("runChecks").addEventListener("click", runChecks); $("saveBaselines").addEventListener("click", saveBaselines); $("pickElement").addEventListener("click", pickElement); $("findGaps").addEventListener("click", findGaps); $("makeSharePage").addEventListener("click", makeSharePage); $("addMissingChecks").addEventListener("click", addMissingChecks);$("recordSteps").addEventListener("click", recordSteps); $("makeBundle").addEventListener("click", makeBundle); $("starterBox").addEventListener("toggle", () => $("starterBox").open && refreshStarters()); $("refreshUnstable").addEventListener("click", () => { refreshUnstable(); refreshChanged(); }); $("checkTag").addEventListener("change", renderChecks);
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
  wireUpTheSwarmBoard();
  wireUpMicrosoft();
  $("swarmKeep").addEventListener("click", keepThisBoard);
  $("talkStartAgain").addEventListener("click", startTalkingAgain);
  $("talkAskEveryone").addEventListener("click", askEveryone);
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
    row.append(make("p", "team-who-state", one.ready
      ? (one.already_set_up ? "Ready, and already set up." : "Ready. It is not set up yet.")
      : (one.why_not || "Not on this machine.")));
    if (one.version) row.append(make("p", "hint", one.version));
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
      system_prompt: one.prompt.trim(),
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
  $("teamModelEndpoint").value = "";
  $("teamModelKeyName").value = "";
  $("teamModelSaid").textContent = "";
  sayWhatTheWayInMeans();
  $("teamModelDialog").showModal();
}

function sayWhatTheWayInMeans() {
  const way = teamWaysIn.find((one) => one.way_in === $("teamModelWayIn").value);
  $("teamModelWayMeans").textContent = way?.means || "";
  // Only one of the two needs a key, and asking for one where it cannot be
  // used is how somebody ends up pasting a key that nothing reads.
  const needsAKey = $("teamModelWayIn").value === "with-a-key";
  $("teamModelKeyName").closest("form").querySelectorAll("label").forEach((label) => {
    if (label.getAttribute("for") === "teamModelKeyName") label.hidden = !needsAKey;
  });
  $("teamModelKeyName").hidden = !needsAKey;
}

async function saveTheModel() {
  try {
    const said = await request("/api/who-is-on-it/add-a-model", {
      method: "POST",
      body: JSON.stringify({
        model: {
          route: $("teamModelRoute").value.trim(),
          way_in: $("teamModelWayIn").value,
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
    tab.setAttribute("aria-selected", String(tab.dataset.pipelineTab === which));
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
  if (!window.confirm(
    "Put this older version back? What is on the board now is kept too, so you can "
    + "swap back again.")) return;
  try {
    const said = await request("/api/pipelines/put-one-back", {
      method: "POST",
      body: JSON.stringify({name: pipelineSavedName, which}),
    });
    pipeline = said.pipeline;
    pipelineOlderOnes = said.older_ones || [];
    $("pipelineName").value = pipeline.name;
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
    const said = await request("/api/pipelines/answer", {
      method: "POST",
      body: JSON.stringify({step, carry_on: carryOn}),
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

async function refreshTimers() {
  try {
    const said = await request("/api/timers");
    timers = said.timers || [];
    timerHowOften = said.how_often || [];
    timerMachine = said.how_to_ask_this_machine || {};
    fillOneChoice("timerAutomation", (said.automations || []).map((one) => ({name: one, label: one})), "name",
                  $("timerAutomation").value || (said.automations || [])[0] || "");
    fillOneChoice("timerHowOften", timerHowOften, "how_often",
                  $("timerHowOften").value || "every-day");
    fillOneChoice("timerOnDay", (said.days || []).map((one) => ({day: one, label: one[0].toUpperCase() + one.slice(1)})),
                  "day", $("timerOnDay").value || "monday");
    sayWhatTheTimerMeans();
    renderTimers();
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
    turn.addEventListener("click", () => turnTheTimer(one, !one.turned_on));
    const now = make("button", "", "Run it now");
    now.type = "button";
    now.title = "Do what the timer would do, without waiting for it";
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
  $("talkAskEveryone").disabled = talkBusy || !talkWho.some((one) => one.ready);
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
  const left = Number(box.maxLength || 6000) - box.value.length;
  $("talkCount").textContent = left < 400 ? `${left} letters left` : "";
}

async function sendWhatIsTyped() {
  const box = $("talkBox");
  const words = box.value.trim();
  const one = talkTheOpenOne();
  if (!words) { sayInTalk("Type something first."); return; }
  if (!one) { sayInTalk("Nobody is set up to talk to yet."); return; }
  if (talkBusy) { sayInTalk("Still waiting for the last answer."); return; }
  talkBusy = true;
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
    showError(error.message);
  } finally {
    talkBusy = false;
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
    showError(error.message);
  } finally {
    talkBusy = false;
    setWhatCanBePressed();
    renderTalkWho();
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
  try { const value = await request("/api/bootstrap"); token = value.token; startedId = value.started_id || ""; template = migrateGraph(value.template); graph = structuredClone(template); catalog = await request("/api/catalog"); nextId = graph.nodes.length + graph.edges.length + 1; focusedNodeId = graph.nodes[0]?.id || ""; render(); renderTeamNotes(); renderWhatItIsDoing(); await refreshProjects(); await validate(); await refreshUsage(); await loadWhatCanBeDoneForYou(); await refreshCheckup(); await refreshHowItWorks(); await refreshChecks(); await refreshTeamNotes(); await refreshWorkflows(); pollEvents(); } catch (error) { showError(error.message); }
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
let swarmBusy = new Set();   // the agents waiting on an answer right now
let swarmNewestRefresh = 0;  // so a slow look cannot overwrite a newer one
// The chats open on the board, in the order they were opened. Kept here rather
// than written down with the board: which boxes you have open is about this
// window and this minute, and two windows should not fight over it.
let swarmChats = [];

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
  cross: [
    "M5 5l14 14",
    "M19 5L5 19",
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

async function refreshSwarm(quietly) {
  const mine = ++swarmNewestRefresh;
  const changesThen = howManyChangesLanded;
  try {
    const said = await request("/api/swarm");
    if (mine !== swarmNewestRefresh) return;
    if (changesThen !== howManyChangesLanded) return;
    swarmSaid = said;
    swarmKept = said.kept || swarmKept;
    keepTheSwarmPick();
    renderSwarmBoard();
    renderSwarmNotReady();
    renderSwarmPanel();
    renderTheChatsOnThisBoard();
    renderTheKeptBoards();
    // What the agents passed to each other, so the list down the side holds
    // those conversations too rather than only the ones you have had. It is
    // small, and without it the list is half a list until somebody opens the
    // fold at the bottom of the board.
    await refreshWhatTheySaidToEachOther();
    const doing = (await request("/api/swarm/how-it-is-going")).doing;
    renderWhatTheyAreDoing(doing);
    if (doing && doing.going) watchWhatTheyAreDoing();
    if (!quietly) sayInSwarm(whatTheBoardSays());
  } catch (error) {
    if (mine !== swarmNewestRefresh) return;
    showError(error.message);
    sayInSwarm(error.message);
  }
}

// What was picked, or a chat that was open, may have been removed in another
// window. Rather than a panel showing an agent that is not there, it falls away.
function keepTheSwarmPick() {
  swarmChats = swarmChats.filter((one) => theSwarmAgent(one.agent));
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
  if (!board.agents.length && !board.projects.length) {
    return "Nothing on the board yet. Press Add another agent to get started.";
  }
  const agents = `${board.agents.length} agent${board.agents.length === 1 ? "" : "s"}`;
  const projects = `${board.projects.length} project${board.projects.length === 1 ? "" : "s"}`;
  return `${agents} and ${projects} on the board. Every box has a gear and a chat button.`;
}

// ---- drawing it ----------------------------------------------------------

function renderSwarmBoard() {
  const board = $("swarmBoard");
  for (const old of [...board.querySelectorAll(
    ".swarm-box, .swarm-empty, .swarm-line-tools, .swarm-chat-card")]) {
    old.remove();
  }
  const said = theSwarmBoard();
  if (!said.agents.length && !said.projects.length) {
    board.append(make("div", "swarm-empty",
      "Nothing on the board yet. Press Add another agent under the board, then Add "
      + "another project folder, then press the gear on the line between them."));
  }
  for (const one of said.agents) board.append(oneSwarmBox("agent", one));
  for (const one of said.projects) board.append(oneSwarmBox("project", one));
  for (const one of swarmChats) board.append(oneSwarmChatCard(one));
  drawSwarmLines();
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

  // The box itself is what you press to pick it, drag it, or move it with the
  // arrows. It is a button so a keyboard can reach it; the gear and the chat
  // button sit beside it rather than inside it, because a button inside a
  // button is not a thing a browser will honour.
  const pick = make("button", "swarm-box-pick");
  pick.type = "button";
  pick.dataset.kind = kind;
  pick.dataset.id = one.id;
  pick.setAttribute("aria-pressed", String(Boolean(picked)));
  pick.setAttribute("aria-label", kind === "agent"
    ? `${one.name}, an agent on the board. Pick it, or move it with the arrow keys`
    : `${one.name}, a project on the board. Pick it, or move it with the arrow keys`);
  pick.append(aSwarmDrawing(kind === "agent" ? "robot" : "folder", 26));
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
  sheet.setAttribute("width", String(board.scrollWidth));
  sheet.setAttribute("height", String(board.scrollHeight));
  sheet.style.width = `${board.scrollWidth}px`;
  sheet.style.height = `${board.scrollHeight}px`;

  const said = theSwarmBoard();
  for (const line of said.works_on) {
    const drawn = drawOneSwarmLine(sheet, found.get(`agent:${line.agent}`),
      found.get(`project:${line.project}`), "works-on");
    if (!drawn) continue;
    const agent = theSwarmAgent(line.agent);
    const project = theSwarmProject(line.project);
    board.append(theToolsOnALine(drawn, "works on", true,
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
    board.append(theToolsOnALine(drawn, on ? "communicates? YES" : "communicates? NO", on,
      () => pickSwarmLine({kind: "talks", one: pair.one, other: pair.other}),
      `whether ${first ? first.name : "one"} and ${other ? other.name : "the other"} may talk`));
  }
  // The thin line from a chat box to the agent it belongs to, so an open chat
  // is never a box floating on its own.
  for (const held of swarmChats) {
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
  if (!said.length) {
    list.append(make("li", "all-well",
      "Everything on the board is ready: every agent has an assistant, and every "
      + "project has somebody on it and jobs to do."));
    return;
  }
  for (const one of said) list.append(make("li", "", one));
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
    const across = event.clientX - dragging.x;
    const down = event.clientY - dragging.y;
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
  swarmPicked = {kind, id};
  renderSwarmBoard();
  renderSwarmPanel();
}

function pickSwarmLine(which) {
  swarmPicked = which;
  renderSwarmBoard();
  renderSwarmPanel();
}

// ---- what you picked -----------------------------------------------------

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
  $("swarmAgentName").value = agent.name;
  $("swarmAgentJob").value = agent.job || "";
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
  // One that is written down but no longer on this machine still has to be
  // shown, or opening its settings would quietly change it to something else.
  if (agent.who && !(swarmSaid.who_can_be_used || []).some((one) => one.route === agent.who)) {
    const gone = make("option", "", `${agent.who} - not on this machine`);
    gone.value = agent.who;
    who.append(gone);
  }
  who.value = agent.who || "";

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
  $("swarmAddAgent").disabled = held || board.agents.length >= (most.agents || 24);
  $("swarmAddProject").disabled = held || board.projects.length >= (most.projects || 12);
  $("swarmRemoveAgent").disabled = held || !agent;
  $("swarmRemoveProject").disabled = held || !project;
  $("swarmTidy").disabled = held || (!board.agents.length && !board.projects.length);
  $("swarmAgentSave").disabled = held || !agent;
  $("swarmAgentRemove").disabled = held || !agent;
  $("swarmOpenChat").disabled = !agent;
  $("swarmAddTask").disabled = held
    || !project || project.tasks.length >= (most.tasks || 40);
  $("swarmProjectRemove").disabled = held || !project;
  $("swarmLineOn").disabled = held || !line;
  $("swarmLineRemove").disabled = held || !line || !$("swarmLineOn").checked;
  for (const tick of $("swarmWorksOn").querySelectorAll("input")) tick.disabled = held;
  for (const tick of $("swarmTalksTo").querySelectorAll("input")) tick.disabled = held;
  $("swarmStart").disabled = swarmGoing;
  $("swarmStop").disabled = !swarmGoing;
  for (const card of $("swarmBoard").querySelectorAll(".swarm-chat-card")) {
    setWhatCanBePressedInAChat(card);
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
    swarmKept = said.kept || swarmKept;
    keepTheSwarmPick();
    renderSwarmBoard();
    renderSwarmNotReady();
    renderSwarmPanel();
    renderTheChatsOnThisBoard();
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
  const already = new Set(theSwarmBoard().projects.map((one) => one.path));
  const known = (swarmSaid.projects_on_this_machine || [])
    .find((one) => !already.has(one.path));
  const said = await askForOneLine(
    "Add another project folder", "Which folder do you want worked on?",
    known ? known.path : "");
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
  const name = $("swarmAgentName").value.trim().replace(/\s+/g, " ");
  if (!name) { sayInSwarm("An agent needs a name."); return; }
  const wasCalled = agent.name;
  const who = $("swarmAgentWho").value;
  const job = $("swarmAgentJob").value.trim();
  const worked = await changeTheSwarmBoard((board) => {
    const held = board.agents.find((one) => one.id === agent.id);
    if (!held) return false;
    held.name = name;
    held.who = who;
    held.job = job;
  }, `${name} was saved.`);
  // Its chat is kept under its name, so a rename opens a different one. Read
  // it again rather than leaving somebody else's words on screen.
  if (worked && name !== wasCalled) refreshTheChatFor(agent.id);
}

async function removeTheSwarmAgent() {
  const agent = thePickedAgent();
  if (!agent) { sayInSwarm("Press the gear on an agent first."); return; }
  await changeTheSwarmBoard((board) => {
    board.agents = board.agents.filter((one) => one.id !== agent.id);
    board.works_on = board.works_on.filter((line) => line.agent !== agent.id);
    board.talks_to = board.talks_to.filter(
      (line) => line.one !== agent.id && line.other !== agent.id);
    swarmChats = swarmChats.filter((one) => one.agent !== agent.id);
    swarmPicked = null;
  }, `${agent.name} is off the board. What it said is kept.`);
}

async function removeTheSwarmProject() {
  const project = thePickedProject();
  if (!project) { sayInSwarm("Press the gear on a project folder first."); return; }
  await changeTheSwarmBoard((board) => {
    board.projects = board.projects.filter((one) => one.id !== project.id);
    board.works_on = board.works_on.filter((line) => line.project !== project.id);
    swarmPicked = null;
  }, `${project.name} is off the board. Nothing in the folder was changed.`);
}

async function addOneSwarmTask() {
  const project = thePickedProject();
  if (!project) return;
  const words = $("swarmTaskText").value.trim();
  if (!words) { sayInSwarm("Type the job first."); return; }
  const worked = await changeTheSwarmBoard((board) => {
    const held = board.projects.find((one) => one.id === project.id);
    if (!held) return false;
    held.tasks.push(words);
  }, `Added to ${project.name}: ${words}`);
  if (worked) $("swarmTaskText").value = "";
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

function openTheChatFor(agentId) {
  const agent = theSwarmAgent(agentId);
  if (!agent) return;
  if (!swarmChats.some((one) => one.agent === agentId)) {
    swarmChats.push({
      agent: agentId,
      at: {x: Math.max(0, agent.at.x - 20), y: agent.at.y + 190},
    });
  }
  renderSwarmBoard();
  renderTheChatsOnThisBoard();
  refreshTheChatFor(agentId);
  const card = theChatCardFor(agentId);
  if (card) {
    card.querySelector(".swarm-chat-box").focus();
    card.scrollIntoView({block: "nearest"});
  }
}

function closeTheChatFor(agentId) {
  swarmChats = swarmChats.filter((one) => one.agent !== agentId);
  renderSwarmBoard();
  renderTheChatsOnThisBoard();
}

function theChatCardFor(agentId) {
  return $("swarmBoard").querySelector(`.swarm-chat-card[data-agent="${agentId}"]`);
}

function oneSwarmChatCard(held) {
  const agent = theSwarmAgent(held.agent);
  const card = make("div", "swarm-chat-card");
  card.dataset.agent = held.agent;
  card.style.left = `${held.at.x}px`;
  card.style.top = `${held.at.y}px`;

  const bar = make("div", "swarm-chat-bar");
  const grip = make("button", "swarm-chat-grip");
  grip.type = "button";
  grip.append(make("strong", "", `Chat with ${agent.name}`));
  grip.title = "Drag to move this chat, or use the arrow keys";
  bar.append(grip);
  bar.append(aSwarmButton("swarm-icon-button", "cross", "close",
    () => closeTheChatFor(held.agent), `close the chat with ${agent.name}`, "close"));
  card.append(bar);

  card.append(make("p", "swarm-chat-said hint", agent.ready
    ? "Nobody else reads this."
    : (agent.why_not || "This one is not set up yet.")));
  card.append(make("ol", "swarm-chat-thread talk-thread"));

  const form = make("form", "swarm-chat-form");
  const box = make("textarea", "swarm-chat-box");
  box.rows = 6;
  box.maxLength = 6000;
  box.placeholder = "What did you change and why?";
  box.setAttribute("aria-label", `What to say to ${agent.name}`);
  form.append(box);
  const row = make("div", "button-row");
  const send = make("button", "primary swarm-chat-send", "Send");
  send.type = "submit";
  row.append(send);
  const again = make("button", "swarm-chat-again", "Start again");
  again.type = "button";
  again.addEventListener("click", () => startTheChatAgainFor(held.agent));
  row.append(again);
  row.append(make("span", "swarm-chat-count hint"));
  form.append(row);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    sendWhatIsTypedTo(held.agent);
  });
  box.addEventListener("input", () => countWhatIsTypedTo(held.agent));
  box.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      sendWhatIsTypedTo(held.agent);
    }
  });
  card.append(form);
  makeTheChatCardDraggable(card, grip, held);
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
      x: Math.max(0, Math.min(4000, Math.round(dragging.left + event.clientX - dragging.x))),
      y: Math.max(0, Math.min(4000, Math.round(dragging.top + event.clientY - dragging.y))),
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
  const card = theChatCardFor(agentId);
  if (card) card.querySelector(".swarm-chat-said").textContent = words;
}

function setWhatCanBePressedInAChat(card) {
  const agent = theSwarmAgent(card.dataset.agent);
  const waiting = swarmBusy.has(card.dataset.agent);
  card.querySelector(".swarm-chat-box").disabled = !agent || !agent.ready;
  card.querySelector(".swarm-chat-send").disabled = waiting || !agent || !agent.ready;
  card.querySelector(".swarm-chat-again").disabled = waiting || !agent;
}

async function refreshTheChatFor(agentId) {
  const agent = theSwarmAgent(agentId);
  if (!agent) return;
  try {
    const said = await request(
      `/api/swarm/said?agent=${encodeURIComponent(agentId)}`);
    renderTheChatThreadFor(agentId, said.said || []);
    countWhatIsTypedTo(agentId);
  } catch (error) {
    sayInTheChatFor(agentId, error.message);
  }
}

function renderTheChatThreadFor(agentId, said) {
  const card = theChatCardFor(agentId);
  if (!card) return;
  const agent = theSwarmAgent(agentId);
  const list = card.querySelector(".swarm-chat-thread");
  list.replaceChildren();
  if (!said.length) {
    list.append(make("li", "hint",
      "Nothing said yet. Whatever you type stays on this machine, and goes only to "
      + "this agent's assistant."));
    return;
  }
  for (const one of said) {
    const row = make("li", `talk-turn ${one.who}`);
    row.append(make("strong", "talk-turn-who",
      one.who === "you" ? "You" : ((agent && agent.name) || "Them")));
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

function countWhatIsTypedTo(agentId) {
  const card = theChatCardFor(agentId);
  if (!card) return;
  const typed = card.querySelector(".swarm-chat-box").value.length;
  card.querySelector(".swarm-chat-count").textContent = typed ? `${typed} letters` : "";
}

async function sendWhatIsTypedTo(agentId) {
  const card = theChatCardFor(agentId);
  const agent = theSwarmAgent(agentId);
  if (!card || !agent) return;
  const box = card.querySelector(".swarm-chat-box");
  const words = box.value.trim();
  if (!words) { sayInTheChatFor(agentId, "Type something first."); return; }
  if (!agent.ready) {
    sayInTheChatFor(agentId, agent.why_not || "This one is not set up yet.");
    return;
  }
  if (swarmBusy.has(agentId)) {
    sayInTheChatFor(agentId, "Still waiting for the last answer.");
    return;
  }
  // One agent at a time, not one chat at a time: two chats open on two
  // different agents can both be waiting, which is the whole point of having
  // several of them on the board.
  swarmBusy.add(agentId);
  setWhatCanBePressedInSwarm();
  sayInTheChatFor(agentId, `Asking ${agent.name}...`);
  try {
    const said = await request("/api/swarm/say", {
      method: "POST", body: JSON.stringify({agent: agentId, text: words}),
    });
    if (!theChatCardFor(agentId)) {
      // The chat was closed while the answer was on its way. It is kept, and is
      // there when it is opened again.
      return;
    }
    box.value = "";
    countWhatIsTypedTo(agentId);
    renderTheChatThreadFor(agentId, said.said || []);
    sayInTheChatFor(agentId, `${agent.name} answered.`);
    // The list down the side carries the last thing said under each name, and
    // something was just said.
    refreshSwarm(true);
  } catch (error) {
    // Read back what was really kept, so a message that did not get through
    // stops looking like one that did. The words stay in the box.
    await refreshTheChatFor(agentId);
    showError(error.message);
    sayInTheChatFor(agentId, error.message);
  } finally {
    swarmBusy.delete(agentId);
    setWhatCanBePressedInSwarm();
  }
}

async function startTheChatAgainFor(agentId) {
  const agent = theSwarmAgent(agentId);
  if (!agent) return;
  try {
    const said = await request("/api/swarm/start-again", {
      method: "POST", body: JSON.stringify({agent: agentId}),
    });
    renderTheChatThreadFor(agentId, []);
    sayInTheChatFor(agentId, said.note || `${agent.name} starts again.`);
  } catch (error) {
    showError(error.message);
    sayInTheChatFor(agentId, error.message);
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
  $("swarmExchangeSaid").textContent = notes.length
    ? `${notes.length} answer${notes.length === 1 ? "" : "s"} passed`
      + (dropped ? `, and ${dropped} older ones dropped to keep the list readable` : "")
    : "nothing passed yet";
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
let swarmWatching = 0;       // the timer that keeps asking how it is going

async function setThemGoing() {
  try {
    const said = await request("/api/swarm/start", {
      method: "POST", body: JSON.stringify({}),
    });
    renderWhatTheyAreDoing(said.doing);
    watchWhatTheyAreDoing();
    sayInSwarm("They are going. What each one says lands in its own chat.");
  } catch (error) {
    showError(error.message);
    sayInSwarm(error.message);
    $("swarmDoingSaid").textContent = error.message;
  }
}

async function stopThemGoing() {
  try {
    const said = await request("/api/swarm/stop", {
      method: "POST", body: JSON.stringify({}),
    });
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
      const said = await request("/api/swarm/how-it-is-going");
      renderWhatTheyAreDoing(said.doing);
      if (!said.doing || !said.doing.going) {
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

let swarmKept = [];

function renderTheKeptBoards() {
  const list = $("swarmKept");
  if (!list) return;
  list.replaceChildren();
  if (!swarmKept.length) {
    list.append(make("li", "hint", "None saved yet."));
    return;
  }
  for (const one of swarmKept) {
    const row = make("li");
    const open = make("button", "swarm-kept-pick");
    open.type = "button";
    open.setAttribute("aria-label", `Open the saved board called ${one.name}`);
    open.append(make("span", "", one.name));
    open.append(make("span", "swarm-kept-when",
      `${one.agents} agent${one.agents === 1 ? "" : "s"}, `
      + `${one.projects} project${one.projects === 1 ? "" : "s"}`));
    open.addEventListener("click", () => openTheKeptBoard(one.name));
    row.append(open);
    const drop = make("button", "swarm-icon-button", "Delete");
    drop.type = "button";
    drop.setAttribute("aria-label", `Delete the saved board called ${one.name}`);
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
    swarmKept = said.kept || [];
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
    swarmKept = said.kept || [];
    keepTheSwarmPick();
    renderSwarmBoard();
    renderSwarmNotReady();
    renderSwarmPanel();
    renderTheKeptBoards();
    renderTheChatsOnThisBoard();
    sayInSwarm(`Opened the board saved as ${name}.`);
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
    swarmKept = said.kept || [];
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


function wireUpTheSwarmBoard() {
  $("swarmAddAgent").addEventListener("click", addAnAgentToTheBoard);
  $("swarmAddProject").addEventListener("click", addAProjectToTheBoard);
  $("swarmRemoveAgent").addEventListener("click", removeTheSwarmAgent);
  $("swarmRemoveProject").addEventListener("click", removeTheSwarmProject);
  $("swarmTidy").addEventListener("click", tidyTheSwarmBoard);
  $("swarmRefresh").addEventListener("click", () => refreshSwarm());
  $("swarmStart").addEventListener("click", setThemGoing);
  $("swarmStop").addEventListener("click", stopThemGoing);
  $("swarmAgentSave").addEventListener("click", saveTheSwarmAgent);
  $("swarmAgentRemove").addEventListener("click", removeTheSwarmAgent);
  $("swarmOpenChat").addEventListener("click", () => {
    const agent = thePickedAgent();
    if (agent) openTheChatFor(agent.id);
  });
  $("swarmProjectRemove").addEventListener("click", removeTheSwarmProject);
  $("swarmAddTask").addEventListener("click", addOneSwarmTask);
  $("swarmTaskText").addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); addOneSwarmTask(); }
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
}

boot();
