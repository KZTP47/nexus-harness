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
      } catch (error) {
        appendEvent("update", `One update could not be read: ${error.message}`);
      }
    }
    if (result.events.length) {
      const now = Date.now();
      renderNodes(); await refreshUsage();
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

function switchView(name) { document.querySelectorAll("[data-view]").forEach((button) => button.setAttribute("aria-pressed", String(button.dataset.view === name))); document.querySelectorAll("[data-view-panel]").forEach((panel) => { panel.hidden = panel.dataset.viewPanel !== name; }); $("workflowActions").hidden = name !== "workflow"; if (name === "memory") refreshMemory(); if (name === "prompts") refreshPrompts(); if (name === "start") refreshCheckup(); if (name === "checks") { refreshChecks(); $("starterUrl").placeholder = window.location.origin + "/"; } if (name === "workflow") { fitGraph(); refreshTeamNotes(); refreshWorkflows(); } if (name === "history") refreshHistory(); }

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

function modelSetup() {
  const advice = checkup?.model_setup;
  const box = make("details", "model-setup");
  if (!advice) return box;
  box.open = !checkup.steps.find((step) => step.id === "provider")?.done;
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
    list.append(item);
  }
  box.append(list, make("p", "field-help", advice.note));
  return box;
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
  const name = prompt("Save this workflow as:", suggested);
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
  const name = prompt(`Rename ${currentWorkflow} to:`, currentWorkflow);
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
  const address = window.prompt(
    "Which page should open?\n\nDo the thing you want to check in the window that opens, "
      + "then press Done in the bar at the top.",
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
  const address = window.prompt(
    "Which page should open?\n\nClick the thing you want to check in the window that opens. Press Escape there to give up.",
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

let coverageFound = null;

async function findGaps() {
  const address = window.prompt(
    "Which address should the walk start from?\n\nIt opens your site, follows the links, and "
      + "tells you which pages have no check at all.",
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
  $("createSuite").addEventListener("click", createSuite); $("runChecks").addEventListener("click", runChecks); $("saveBaselines").addEventListener("click", saveBaselines); $("pickElement").addEventListener("click", pickElement); $("findGaps").addEventListener("click", findGaps); $("makeSharePage").addEventListener("click", makeSharePage); $("addMissingChecks").addEventListener("click", addMissingChecks);$("recordSteps").addEventListener("click", recordSteps); $("makeBundle").addEventListener("click", makeBundle); $("starterBox").addEventListener("toggle", () => $("starterBox").open && refreshStarters()); $("refreshUnstable").addEventListener("click", () => { refreshUnstable(); refreshChanged(); }); $("checkTag").addEventListener("change", renderChecks);
  $("refreshMemory").addEventListener("click", refreshMemory); $("memoryQuery").addEventListener("change", refreshMemory); $("memoryKind").addEventListener("change", refreshMemory); $("refreshPrompts").addEventListener("click", refreshPrompts); $("promptLeft").addEventListener("change", renderPromptCompare); $("promptRight").addEventListener("change", renderPromptCompare);
  window.addEventListener("keydown", (event) => { if (event.key === "Escape" && edgeDrag) { event.preventDefault(); finishEdgeDrag({pointerId: edgeDrag.pointerId}, true); } else if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "z" && !$("agentDialog").open) { event.preventDefault(); undo(); } });
}

async function boot() {
  bindEvents();
  try { const value = await request("/api/bootstrap"); token = value.token; startedId = value.started_id || ""; template = migrateGraph(value.template); graph = structuredClone(template); catalog = await request("/api/catalog"); nextId = graph.nodes.length + graph.edges.length + 1; focusedNodeId = graph.nodes[0]?.id || ""; render(); renderTeamNotes(); await validate(); await refreshUsage(); await refreshCheckup(); await refreshChecks(); await refreshTeamNotes(); await refreshWorkflows(); pollEvents(); } catch (error) { showError(error.message); }
}

boot();
