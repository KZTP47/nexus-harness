"use strict";

const facts = document.getElementById("facts");
const status = document.getElementById("copyStatus");
let diagnostics = null;

function draw(value) {
  facts.replaceChildren();
  const labels = {
    version: "Nexus version",
    commit: "Harness commit",
    buildKind: "Build identity",
    installation: "Running as",
    project: "Project root",
    serverUrl: "Local address",
    port: "Local port",
    processId: "App process",
    executable: "App executable",
    electron: "Electron version",
  };
  for (const [key, label] of Object.entries(labels)) {
    const term = document.createElement("dt");
    const detail = document.createElement("dd");
    term.textContent = label;
    detail.textContent = String(value[key] ?? "Unknown");
    facts.append(term, detail);
  }
}

window.harnessDesktop.diagnostics().then((value) => {
  diagnostics = value;
  draw(value || {});
}).catch((error) => { status.textContent = error.message; });

document.getElementById("copy").addEventListener("click", async () => {
  if (!diagnostics) return;
  try {
    await navigator.clipboard.writeText(JSON.stringify(diagnostics, null, 2));
    status.textContent = "Diagnostics copied.";
  } catch (_error) {
    status.textContent = "Windows would not let this page copy. The details are still shown above.";
  }
});
document.getElementById("back").addEventListener("click", () => window.harnessDesktop.retry());
