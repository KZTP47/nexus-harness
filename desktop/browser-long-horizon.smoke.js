"use strict";

// Source-browser acceptance for durable Work together admission.
//
// This deliberately does not launch Electron. It starts the real source UI
// with the packaged private Python, drives the packaged Chromium as an
// ordinary browser, and therefore exercises the exact environment in which
// `window.harnessDesktop` is absent. The first admission response is cut only
// after the server has persisted it. A different loopback origin then proves
// that backend inventory, rather than origin-local state, owns recovery.

const childProcess = require("node:child_process");
const fs = require("node:fs");
const net = require("node:net");
const os = require("node:os");
const path = require("node:path");
const {chromium} = require("playwright-core");

const TIMEOUT_MS = 180_000;
const ROOT = path.resolve(__dirname, "..");
const PACKAGED_RUNTIME = path.join(
  __dirname, "build-output", "win-unpacked", "resources", "runtime",
);
const AGENT_A = "browser-source-agent-a";
const AGENT_B = "browser-source-agent-b";
const PROJECT_ID = "browser-source-project";
const RECOVERY_MARKER = "BROWSER-SOURCE-RECOVERY";
const START_LOSS_MARKER = "BROWSER-SOURCE-START-LOSS";
const ACK_LOSS_MARKER = "BROWSER-SOURCE-ACK-LOSS";
const ACK_BEFORE_MARKER = "BROWSER-SOURCE-ACK-BEFORE";
const ACK_AFTER_MARKER = "BROWSER-SOURCE-ACK-AFTER";
const NORMAL_MARKER = "BROWSER-SOURCE-NORMAL";

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

function sleep(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function until(read, description, timeout = TIMEOUT_MS) {
  const deadline = Date.now() + timeout;
  let latest;
  while (Date.now() < deadline) {
    latest = await read();
    if (latest) return latest;
    await sleep(100);
  }
  throw new Error(`Timed out waiting for ${description}; latest=${JSON.stringify(latest)}`);
}

function packagedTools() {
  const python = path.join(PACKAGED_RUNTIME, "python.exe");
  const manifestPath = path.join(PACKAGED_RUNTIME, "NEXUS_RUNTIME.json");
  if (!fs.existsSync(python) || !fs.existsSync(manifestPath)) {
    throw new Error(
      `The packaged private runtime is missing under ${PACKAGED_RUNTIME}. Build the Windows app first.`,
    );
  }
  const manifest = JSON.parse(fs.readFileSync(manifestPath, "utf8"));
  const relativeBrowser = String(manifest?.playwright?.chromium_executable || "");
  if (!relativeBrowser || path.isAbsolute(relativeBrowser)) {
    throw new Error("The packaged runtime manifest has no portable Chromium executable path.");
  }
  const browser = path.resolve(
    PACKAGED_RUNTIME, "playwright",
    ...relativeBrowser.replaceAll("\\", "/").split("/"),
  );
  if (!fs.existsSync(browser)) {
    throw new Error(`The packaged Chromium executable is missing: ${browser}`);
  }
  return {python, browser};
}

function isolatedEnvironment(profile, runtime) {
  const roaming = path.join(profile, "AppData", "Roaming");
  const local = path.join(profile, "AppData", "Local");
  const temporary = path.join(profile, "Temporary files");
  for (const folder of [roaming, local, temporary]) {
    fs.mkdirSync(folder, {recursive: true});
  }
  const safeNames = [
    "SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "COMSPEC", "SYSTEMDRIVE",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "PROCESSOR_IDENTIFIER",
    "OS", "LANG", "LC_ALL", "CI", "GITHUB_ACTIONS",
  ];
  const source = new Map(
    Object.entries(process.env).map(([key, value]) => [key.toUpperCase(), value]),
  );
  const environment = {};
  for (const name of safeNames) {
    if (source.get(name) != null) environment[name] = source.get(name);
  }
  environment.APPDATA = roaming;
  environment.LOCALAPPDATA = local;
  environment.USERPROFILE = profile;
  environment.HOME = profile;
  environment.TEMP = temporary;
  environment.TMP = temporary;
  environment.PYTHONUTF8 = "1";
  environment.PATH = `${runtime}${path.delimiter}${environment.PATH || ""}`;
  return environment;
}

function writeFixture(project, coordination, python) {
  fs.mkdirSync(path.join(project, ".harness"), {recursive: true});
  fs.mkdirSync(coordination, {recursive: true});
  const provider = path.join(project, "deterministic browser provider.py");
  const verifier = path.join(project, "test_browser_goal_results.py");
  fs.writeFileSync(provider, `from __future__ import annotations

import json
import sys
from pathlib import Path


coordination = Path(sys.argv[1])
route = sys.argv[2]
payload = json.loads(sys.stdin.read())
context = str(payload.get("dynamic_context") or "")
if "${RECOVERY_MARKER}" in context:
    stem = "recovered"
    wanted = "browser recovery complete"
elif "${START_LOSS_MARKER}" in context:
    stem = "start-loss"
    wanted = "browser start loss complete"
elif "${ACK_LOSS_MARKER}" in context:
    stem = "ack-loss"
    wanted = "browser acknowledgement loss complete"
elif "${ACK_BEFORE_MARKER}" in context:
    stem = "ack-before"
    wanted = "browser precommit acknowledgement complete"
elif "${ACK_AFTER_MARKER}" in context:
    stem = "ack-after"
    wanted = "browser committed acknowledgement complete"
elif "${NORMAL_MARKER}" in context:
    stem = "normal"
    wanted = "browser normal complete"
else:
    raise RuntimeError("The deterministic provider received an unknown objective")

# The second selected participant must consume the durable outcome from the
# first participant. A second independent call with no team fan-in is not
# cooperation.
if route == "browser-b" and "REQUIRED CONTRIBUTION FAN-IN" not in context:
    raise RuntimeError("The second required participant received no bounded team fan-in")

coordination.mkdir(parents=True, exist_ok=True)
with (coordination / f"{stem}-dispatch.log").open("a", encoding="utf-8") as stream:
    stream.write(route + "\\n")

target = f"browser-{stem}.txt"
changes = []
if not Path(target).is_file():
    changes.append({
        "path": target,
        "content": wanted + "\\n",
        "delete": False,
        "reason": "fulfil the exact browser-source acceptance objective",
    })
refs = [f"file:{target}"]
criteria = [
    {"criterion": "Original objective is satisfied", "evidence_refs": refs},
    {"criterion": "Every required task is complete", "evidence_refs": refs},
    {"criterion": "Configured deterministic verification passes", "evidence_refs": refs},
]
action = {
    "action": "complete",
    "summary": f"Created or confirmed {target}",
    "evidence": refs,
    "risk": "low",
    "changes": changes,
    "needs_files": [],
    "tool_calls": [],
    "tasks": [],
    "handoff_agent_id": "",
    "questions": [],
    "criteria_evidence": criteria,
}
print(json.dumps({
    "text": json.dumps(action, separators=(",", ":")),
    "finish_reason": "stop",
}, separators=(",", ":")))
`, "utf8");
  fs.writeFileSync(verifier, `import unittest
from pathlib import Path


class BrowserGoalResultTests(unittest.TestCase):
    def test_every_created_browser_goal_has_exact_contents(self):
        expected = {
            "browser-recovered.txt": "browser recovery complete",
            "browser-start-loss.txt": "browser start loss complete",
            "browser-ack-loss.txt": "browser acknowledgement loss complete",
            "browser-ack-before.txt": "browser precommit acknowledgement complete",
            "browser-ack-after.txt": "browser committed acknowledgement complete",
            "browser-normal.txt": "browser normal complete",
        }
        found = [name for name in expected if Path(name).is_file()]
        self.assertTrue(found, "No browser acceptance goal result exists")
        for name in found:
            with self.subTest(path=name):
                self.assertEqual(Path(name).read_text(encoding="utf-8").strip(), expected[name])


if __name__ == "__main__":
    unittest.main()
`, "utf8");
  const command = (route) => [python, provider, coordination, route];
  const config = {
    schema_version: 1,
    providers: {
      "browser-a": {
        kind: "local", model: "deterministic-browser-a",
        command: command("browser-a"), endpoint: "http://127.0.0.1:1",
        max_concurrency: 1,
      },
      "browser-b": {
        kind: "local", model: "deterministic-browser-b",
        command: command("browser-b"), endpoint: "http://127.0.0.1:1",
        max_concurrency: 1,
      },
    },
    project: {
      test_commands: [[
        "python", "-m", "unittest", "discover", "-s", ".",
        "-p", "test_browser_goal_results.py",
      ]],
    },
    workflow: {require_review: false, reviewers: 1, review_parallelism: 1},
  };
  fs.writeFileSync(
    path.join(project, ".harness", "config.local.json"),
    `${JSON.stringify(config, null, 2)}\n`, "utf8",
  );
}

function trustFixture(python, project, environment) {
  const source = path.join(ROOT, "src");
  const code = [
    "import sys",
    "from pathlib import Path",
    `sys.path.insert(0, ${JSON.stringify(source)})`,
    "from our_harness.config import trust_project_local_config",
    "from our_harness.pipeline_runs import project_identity",
    "root = Path(sys.argv[1]).resolve()",
    "trust_project_local_config(root)",
    "project_identity(root)",
  ].join("; ");
  const result = childProcess.spawnSync(python, ["-c", code, project], {
    cwd: project,
    env: environment,
    encoding: "utf8",
    timeout: 60_000,
  });
  if (result.status !== 0) {
    throw new Error(
      `Could not trust the isolated source-browser fixture: ${result.stderr || result.stdout}`,
    );
  }
}

function freePort(except = -1) {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.unref();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const port = Number(server.address().port);
      server.close((error) => {
        if (error) reject(error);
        else if (port === except) resolve(freePort(except));
        else resolve(port);
      });
    });
  });
}

async function startSourceServer(python, project, environment, port) {
  const child = childProcess.spawn(
    python,
    [
      path.join(ROOT, "scripts", "harness.py"),
      "--project", project,
      "ui", "--host", "127.0.0.1", "--port", String(port),
      "--no-open-browser",
    ],
    {
      cwd: project,
      env: environment,
      windowsHide: true,
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  const lines = [];
  let buffered = "";
  let settled = false;
  let readyResolve;
  let readyReject;
  const ready = new Promise((resolve, reject) => {
    readyResolve = resolve;
    readyReject = reject;
  });
  const ingest = (chunk) => {
    buffered += String(chunk);
    const parts = buffered.split(/\r?\n/);
    buffered = parts.pop() || "";
    for (const line of parts) {
      lines.push(line);
      if (lines.length > 200) lines.shift();
      if (!settled && line.startsWith("harness-ui-ready ")) {
        settled = true;
        try { readyResolve(JSON.parse(line.slice("harness-ui-ready ".length)).url); }
        catch (error) { readyReject(error); }
      }
    }
  };
  child.stdout.on("data", ingest);
  child.stderr.on("data", ingest);
  child.once("error", (error) => {
    if (!settled) {
      settled = true;
      readyReject(error);
    }
  });
  child.once("exit", (code, signal) => {
    if (!settled) {
      settled = true;
      readyReject(new Error(
        `The source UI exited before ready (code=${code}, signal=${signal}): ${lines.slice(-20).join("\n")}`,
      ));
    }
  });
  const timer = setTimeout(() => {
    if (!settled) {
      settled = true;
      readyReject(new Error(
        `The source UI did not become ready: ${lines.slice(-20).join("\n")}`,
      ));
    }
  }, 60_000);
  let url;
  try {
    url = await ready;
  } finally {
    clearTimeout(timer);
  }
  assert(new URL(url).port === String(port), `The source server did not bind requested port ${port}: ${url}`);
  return {
    child, url, port, lines,
    async stop() {
      if (child.exitCode !== null || child.signalCode !== null) return;
      const exited = new Promise((resolve) => child.once("exit", resolve));
      child.kill();
      await Promise.race([exited, sleep(10_000)]);
      if (child.exitCode === null && child.signalCode === null) {
        child.kill("SIGKILL");
        await Promise.race([exited, sleep(5_000)]);
      }
      assert(
        child.exitCode !== null || child.signalCode !== null,
        `Could not stop source UI on port ${port}`,
      );
    },
  };
}

async function showSwarm(page) {
  await page.click('[data-view="swarm"]', {timeout: 30_000});
  await page.waitForSelector("#swarmView", {state: "visible", timeout: 30_000});
  await page.waitForFunction(() => typeof swarmBoardHydrated !== "undefined" && swarmBoardHydrated, null, {
    timeout: 30_000,
  });
}

async function configureBoard(page, project) {
  return page.evaluate(async ({agentA, agentB, projectId, projectPath}) => {
    const standing = await request("/api/swarm?refresh_providers=true");
    const board = standing.board;
    board.agents = [
      {
        id: agentA, name: "Browser provider A", who: "browser-a",
        job: "Lead deterministic browser work", at: {x: 60, y: 60},
        colour: "#4f46e5", icon: "robot", bubble_colour: "#eef2ff",
        profile_picture: "", picture_zoom: 100, picture_hue: 0,
        filed_as: "Browser provider A",
      },
      {
        id: agentB, name: "Browser provider B", who: "browser-b",
        job: "Contribute deterministic browser review", at: {x: 330, y: 60},
        colour: "#0f766e", icon: "robot", bubble_colour: "#ecfdf5",
        profile_picture: "", picture_zoom: 100, picture_hue: 0,
        filed_as: "Browser provider B",
      },
    ];
    board.projects = [{
      id: projectId, path: projectPath, name: "Portable browser project",
      is_there: true, tasks: [], at: {x: 60, y: 390},
      approved_test_command_digest: "",
    }];
    board.works_on = [
      {agent: agentA, project: projectId},
      {agent: agentB, project: projectId},
    ];
    board.talks_to = [{one: agentA, other: agentB}];
    board.made_agents = Math.max(Number(board.made_agents || 0), 2);
    board.made_projects = Math.max(Number(board.made_projects || 0), 1);
    await request("/api/swarm/save", {
      method: "POST", body: JSON.stringify({board}),
    });
    const chats = {};
    for (const name of [
      "normalChat", "ackAfterChat", "ackBeforeChat",
      "ackLossChat", "startLossChat", "recoveryChat",
    ]) {
      const created = await request("/api/swarm/chats/create", {
        method: "POST", body: JSON.stringify({agent: agentA, peer: agentB}),
      });
      chats[name] = created.active;
      await request("/api/swarm/chats/project", {
        method: "POST",
        body: JSON.stringify({
          agent: agentA, chat: created.active, project: projectId,
        }),
      });
    }
    return chats;
  }, {agentA: AGENT_A, agentB: AGENT_B, projectId: PROJECT_ID, projectPath: project});
}

async function openCompactChat(page, chatId) {
  await showSwarm(page);
  const open = `.swarm-box[data-id="${AGENT_A}"] .swarm-icon-button[data-does="chat"]`;
  await page.click(open, {timeout: 30_000});
  const card = page.locator(`.swarm-chat-card[data-agent="${AGENT_A}"]`);
  await card.waitFor({state: "visible", timeout: 30_000});
  await page.evaluate(async ([agentId, wanted]) => {
    if (activeConversationFor(agentId)?.id !== wanted) {
      await activateConversationFor(agentId, wanted);
    }
  }, [AGENT_A, chatId]);
  await page.waitForFunction(
    ([agentId, wanted]) => activeConversationFor(agentId)?.id === wanted
      && !swarmChatIsHydrating(agentId),
    [AGENT_A, chatId], {timeout: 30_000},
  );
  return card;
}

async function openBigChat(page, chatId) {
  const card = await openCompactChat(page, chatId);
  await card.getByRole("button", {name: "Open full Nexus chat"}).click();
  await page.waitForSelector("#theBigChat", {state: "visible", timeout: 30_000});
  await page.click(
    `#theBigChatConversationList [data-conversation-action="pick"][data-chat-id="${chatId}"]`,
    {timeout: 30_000},
  );
  await page.waitForFunction(
    ([agentId, wanted]) => activeConversationFor(agentId)?.id === wanted
      && !swarmChatIsHydrating(agentId),
    [AGENT_A, chatId], {timeout: 30_000},
  );
}

async function waitForGoal(page, conversationId, status = "complete") {
  return until(async () => {
    const goals = await page.evaluate(
      async () => (await request("/api/long-horizon/goals")).goals || [],
    );
    return goals.find((goal) => (
      goal.conversation_id === conversationId && goal.status === status
    )) || false;
  }, `${status} goal for ${conversationId}`);
}

function assertExactTeamGoal(goal, expectedRequestId = "") {
  const expectedAgents = [AGENT_A, AGENT_B].sort();
  assert(goal.status === "complete", `Goal was not complete: ${JSON.stringify(goal)}`);
  if (expectedRequestId) {
    assert(goal.request_id === expectedRequestId, "Recovered goal changed request identity.");
  }
  assert(goal.require_all_participants === true, "Goal did not require every pair participant.");
  const requested = [...(goal.requested_agent_ids || [])].sort();
  assert(
    JSON.stringify(requested) === JSON.stringify(expectedAgents),
    `Goal changed its required participant set: ${JSON.stringify(requested)}`,
  );
  const participantTasks = (goal.tasks || []).filter(
    (task) => expectedAgents.includes(task.assigned_agent_id),
  );
  assert(participantTasks.length === 2, `Expected exactly two participant tasks: ${JSON.stringify(goal.tasks)}`);
  assert(
    participantTasks.every((task) => (
      task.state === "complete" && task.attempts === 1
      && task.provider_effect_state === "acknowledged"
      && typeof task.provider_effect_id === "string" && task.provider_effect_id.length > 0
    )),
    `A required provider was not acknowledged exactly once: ${JSON.stringify(participantTasks)}`,
  );
  assert(
    Number(goal.budget?.provider_calls) === 2,
    `Expected exactly two provider calls: ${JSON.stringify(goal.budget)}`,
  );
  assert(goal.verification?.status === "passed", `Verification did not pass: ${JSON.stringify(goal.verification)}`);
  assert(
    (goal.verification?.criteria_results || []).length > 0
      && goal.verification.criteria_results.every((criterion) => criterion.status === "passed"),
    `Acceptance criteria did not all pass: ${JSON.stringify(goal.verification)}`,
  );
}

function dispatches(coordination, stem) {
  const log = path.join(coordination, `${stem}-dispatch.log`);
  if (!fs.existsSync(log)) return [];
  return fs.readFileSync(log, "utf8").split(/\r?\n/).filter(Boolean).sort();
}

function assertExactDispatches(coordination, stem) {
  const actual = dispatches(coordination, stem);
  const expected = ["browser-a", "browser-b"];
  assert(
    JSON.stringify(actual) === JSON.stringify(expected),
    `${stem} provider dispatches were not exactly once each: ${JSON.stringify(actual)}`,
  );
}

function removeFixtureSafely(fixture) {
  const temporaryRoot = path.resolve(os.tmpdir());
  const resolved = path.resolve(fixture);
  const relative = path.relative(temporaryRoot, resolved);
  assert(
    relative && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative)
      && path.basename(resolved).startsWith("nexus browser source acceptance - "),
    `Refusing to recursively remove a path outside the owned temporary fixture: ${resolved}`,
  );
  fs.rmSync(resolved, {recursive: true, force: true, maxRetries: 10, retryDelay: 200});
}

async function main() {
  if (process.platform !== "win32") {
    console.log("skip  the source-browser long-horizon acceptance is Windows-specific");
    return;
  }
  const tools = packagedTools();
  const fixture = fs.mkdtempSync(path.join(os.tmpdir(), "nexus browser source acceptance - "));
  const profile = path.join(fixture, "arbitrary profile with spaces");
  const project = path.join(fixture, "arbitrary project with spaces");
  const coordination = path.join(fixture, "deterministic provider records");
  for (const folder of [profile, project, coordination]) fs.mkdirSync(folder, {recursive: true});
  const environment = isolatedEnvironment(profile, PACKAGED_RUNTIME);
  writeFixture(project, coordination, tools.python);
  trustFixture(tools.python, project, environment);

  let browser = null;
  let context = null;
  let server = null;
  let passed = false;
  console.log(`info  isolated source-browser fixture: ${fixture}`);
  try {
    const firstPort = await freePort();
    server = await startSourceServer(tools.python, project, environment, firstPort);
    browser = await chromium.launch({
      executablePath: tools.browser,
      headless: true,
    });
    context = await browser.newContext({viewport: {width: 1500, height: 1000}});
    let page = await context.newPage();
    page.on("dialog", (dialog) => dialog.accept().catch(() => {}));
    await page.goto(server.url, {waitUntil: "domcontentloaded", timeout: 60_000});
    assert(
      await page.evaluate(() => typeof window.harnessDesktop === "undefined"),
      "The source acceptance unexpectedly received an Electron desktop bridge.",
    );
    await showSwarm(page);
    const chats = await configureBoard(page, project);
    await page.reload({waitUntil: "domcontentloaded", timeout: 60_000});
    const compact = await openCompactChat(page, chats.recoveryChat);
    console.log("pass  an ordinary source browser has no desktop bridge and opens a real paired chat");

    const recoveryGoal = (
      "Create browser-recovered.txt containing exactly browser recovery complete. "
      + `${RECOVERY_MARKER}.`
    );
    let persistedReceipt = null;
    let firstPreparePosts = 0;
    let firstStartPosts = 0;
    page.on("request", (request) => {
      const pathname = new URL(request.url()).pathname;
      if (request.method() === "POST" && pathname === "/api/long-horizon/prepare-admission") {
        firstPreparePosts += 1;
      }
      if (request.method() === "POST" && pathname === "/api/long-horizon/start") {
        firstStartPosts += 1;
      }
    });
    await page.route("**/api/long-horizon/prepare-admission", async (route) => {
      if (persistedReceipt) {
        await route.continue();
        return;
      }
      const response = await route.fetch();
      assert(response.ok(), `Backend prepare failed before the simulated response loss: ${response.status()}`);
      persistedReceipt = await response.json();
      assert(persistedReceipt?.pending?.request_id, "Backend prepare returned no durable pending identity.");
      // The response is lost only after route.fetch() has completed, so a
      // recovery record exists on the server while the browser sees failure.
      await route.abort("failed");
    });
    await compact.locator(".swarm-chat-box").fill(recoveryGoal);
    await compact.locator(".swarm-chat-work").click();
    await until(() => Promise.resolve(persistedReceipt), "persisted prepare receipt", 30_000);
    await page.waitForSelector(
      "#directLongGoalRecoveryBoard .direct-long-goal-recovery-row",
      {state: "visible", timeout: 30_000},
    );
    assert(firstPreparePosts === 1, `The compact Work entry prepared ${firstPreparePosts} times.`);
    assert(firstStartPosts === 0, "The compact Work entry started after its prepare response was lost.");
    assert(
      await compact.locator(".swarm-chat-box").inputValue() === recoveryGoal,
      "The compact composer cleared before the browser received durable prepare proof.",
    );
    const directMarkers = await page.evaluate(() => Object.keys(localStorage).filter(
      (key) => key.startsWith("nexus.long-horizon.direct-request."),
    ));
    assert(directMarkers.length === 0, `The browser recovery improperly depended on localStorage: ${directMarkers}`);
    assert(dispatches(coordination, "recovered").length === 0, "A provider ran before explicit reconciliation.");
    console.log("pass  compact Work reaches backend prepare and loses only the persisted response without dispatch");

    const firstOrigin = new URL(server.url).origin;
    await server.stop();
    server = null;
    await page.close();
    const secondPort = await freePort(firstPort);
    server = await startSourceServer(tools.python, project, environment, secondPort);
    const secondOrigin = new URL(server.url).origin;
    assert(secondOrigin !== firstOrigin, `Restart reused the same origin: ${secondOrigin}`);

    page = await context.newPage();
    page.on("dialog", (dialog) => dialog.accept().catch(() => {}));
    let recoveredInventory = null;
    const restartPosts = [];
    page.on("request", (request) => {
      const pathname = new URL(request.url()).pathname;
      if (request.method() === "POST" && [
        "/api/long-horizon/prepare-admission", "/api/long-horizon/start",
        "/api/long-horizon/admission-goal", "/api/long-horizon/discard-admission",
        "/api/long-horizon/acknowledge-admission",
      ].includes(pathname)) restartPosts.push(pathname);
    });
    page.on("response", async (response) => {
      if (new URL(response.url()).pathname === "/api/long-horizon/pending-admissions"
          && response.ok()) {
        try { recoveredInventory = await response.json(); }
        catch (_) { /* The visible recovery assertion remains authoritative. */ }
      }
    });
    await page.goto(server.url, {waitUntil: "domcontentloaded", timeout: 60_000});
    assert(
      await page.evaluate(() => typeof window.harnessDesktop === "undefined"),
      "The restarted ordinary browser unexpectedly received a desktop bridge.",
    );
    await showSwarm(page);
    await page.waitForSelector(
      "#directLongGoalRecoveryBoard .direct-long-goal-recovery-row",
      {state: "visible", timeout: 30_000},
    );
    await until(
      () => Promise.resolve(recoveredInventory?.pending?.some(
        (one) => one.request_id === persistedReceipt.pending.request_id,
      )),
      "the restarted pending-admissions inventory",
      30_000,
    );
    const restartedMarkers = await page.evaluate(() => Object.keys(localStorage).filter(
      (key) => key.startsWith("nexus.long-horizon.direct-request."),
    ));
    assert(restartedMarkers.length === 0, `The new origin acquired an unexpected retry marker: ${restartedMarkers}`);
    await sleep(750);
    assert(restartPosts.length === 0, `Restart automatically resent admission: ${JSON.stringify(restartPosts)}`);
    assert(dispatches(coordination, "recovered").length === 0, "Restart automatically dispatched a provider.");
    console.log("pass  a different origin visibly restores pending-admissions with no localStorage retry state or auto-resend");

    await page.getByRole("button", {name: "Open exact saved chat", exact: true}).click();
    const recoveredCard = page.locator(`.swarm-chat-card[data-agent="${AGENT_A}"]`);
    await recoveredCard.locator(".direct-long-goal-recover").waitFor({
      state: "visible", timeout: 30_000,
    });
    await recoveredCard.locator(".direct-long-goal-recover").click();
    const recoveredGoal = await waitForGoal(page, chats.recoveryChat);
    assertExactTeamGoal(recoveredGoal, persistedReceipt.pending.request_id);
    assertExactDispatches(coordination, "recovered");
    assert(
      fs.readFileSync(path.join(project, "browser-recovered.txt"), "utf8").trim()
        === "browser recovery complete",
      "Recovered goal did not achieve its exact file outcome.",
    );
    await page.waitForSelector("#directLongGoalRecoveryBoard", {state: "hidden", timeout: 30_000});
    await sleep(750);
    assert(
      JSON.stringify(restartPosts) === JSON.stringify([
        "/api/long-horizon/start", "/api/long-horizon/discard-admission",
        "/api/long-horizon/acknowledge-admission",
      ]),
      `Explicit reconcile did not perform exactly one start and exact acknowledgement: ${JSON.stringify(restartPosts)}`,
    );
    assertExactDispatches(coordination, "recovered");
    console.log("pass  explicit reconciliation starts once, dispatches both required providers once, and verifies complete");

    // Lose the /start response only after the real server has committed the
    // canonical goal. The pending journal must remain authoritative until a
    // later exact acknowledgement, even though the provider work has already
    // run to completion.
    const startLossCard = await openCompactChat(page, chats.startLossChat);
    const startLossText = (
      "Create browser-start-loss.txt containing exactly browser start loss complete. "
      + `${START_LOSS_MARKER}.`
    );
    let lostStartReceipt = null;
    const startLossRequests = [];
    page.on("request", (request) => {
      const pathname = new URL(request.url()).pathname;
      if (request.method() === "POST" && [
        "/api/long-horizon/prepare-admission", "/api/long-horizon/start",
        "/api/long-horizon/admission-goal", "/api/long-horizon/discard-admission",
        "/api/long-horizon/acknowledge-admission",
      ].includes(pathname)) startLossRequests.push(pathname);
    });
    await page.route("**/api/long-horizon/start", async (route) => {
      if (lostStartReceipt) {
        await route.continue();
        return;
      }
      const response = await route.fetch();
      assert(response.ok(), `Backend start failed before simulated response loss: ${response.status()}`);
      lostStartReceipt = await response.json();
      assert(lostStartReceipt?.goal?.goal_id, "Committed start returned no canonical goal identity.");
      await route.abort("failed");
    });
    await startLossCard.locator(".swarm-chat-box").fill(startLossText);
    await startLossCard.locator(".swarm-chat-work").click();
    await until(() => Promise.resolve(lostStartReceipt), "committed start receipt", 30_000);
    await page.unroute("**/api/long-horizon/start");
    await page.waitForSelector(
      "#directLongGoalRecoveryBoard .direct-long-goal-recovery-row",
      {state: "visible", timeout: 30_000},
    );
    const startedBeforeRestart = await waitForGoal(page, chats.startLossChat);
    assertExactTeamGoal(startedBeforeRestart, lostStartReceipt.request_id);
    assert(
      startedBeforeRestart.goal_id === lostStartReceipt.goal.goal_id,
      "The committed /start receipt and durable goal inventory disagree.",
    );
    assertExactDispatches(coordination, "start-loss");
    const retainedAfterLostStart = await page.evaluate(
      async () => (await request("/api/long-horizon/pending-admissions")).pending || [],
    );
    assert(
      retainedAfterLostStart.some((one) => one.request_id === lostStartReceipt.request_id),
      "A successful /start retired its pending journal before exact acknowledgement.",
    );
    assert(
      JSON.stringify(startLossRequests) === JSON.stringify([
        "/api/long-horizon/prepare-admission", "/api/long-horizon/start",
      ]),
      `Lost start response triggered an acknowledgement or retry: ${JSON.stringify(startLossRequests)}`,
    );
    assert(
      await startLossCard.locator(".swarm-chat-box").inputValue() === startLossText,
      "The exact draft was not restored after the committed start response was lost.",
    );
    console.log("pass  compact Work retains the pending journal after a committed /start response is lost");

    const secondOriginBeforeRestart = new URL(server.url).origin;
    const thirdPort = await freePort(secondPort);
    await server.stop();
    server = null;
    await page.close();
    server = await startSourceServer(tools.python, project, environment, thirdPort);
    const thirdOrigin = new URL(server.url).origin;
    assert(
      thirdOrigin !== secondOriginBeforeRestart,
      `Committed-start recovery did not move to a different origin: ${thirdOrigin}`,
    );
    page = await context.newPage();
    page.on("dialog", (dialog) => dialog.accept().catch(() => {}));
    const startRetryRequests = [];
    let startRetryInventory = null;
    page.on("request", (request) => {
      const pathname = new URL(request.url()).pathname;
      if (request.method() === "POST" && [
        "/api/long-horizon/prepare-admission", "/api/long-horizon/start",
        "/api/long-horizon/admission-goal", "/api/long-horizon/discard-admission",
        "/api/long-horizon/acknowledge-admission",
      ].includes(pathname)) startRetryRequests.push(pathname);
    });
    page.on("response", async (response) => {
      if (new URL(response.url()).pathname === "/api/long-horizon/pending-admissions"
          && response.ok()) {
        try { startRetryInventory = await response.json(); }
        catch (_) { /* Visible inventory is asserted below as well. */ }
      }
    });
    await page.goto(server.url, {waitUntil: "domcontentloaded", timeout: 60_000});
    assert(
      await page.evaluate(() => typeof window.harnessDesktop === "undefined"),
      "Committed-start recovery unexpectedly acquired a desktop bridge.",
    );
    await showSwarm(page);
    await page.waitForSelector(
      "#directLongGoalRecoveryBoard .direct-long-goal-recovery-row",
      {state: "visible", timeout: 30_000},
    );
    await until(
      () => Promise.resolve(startRetryInventory?.pending?.some(
        (one) => one.request_id === lostStartReceipt.request_id,
      )),
      "retained committed-start inventory on the new origin",
      30_000,
    );
    assert(
      (await page.evaluate(() => Object.keys(localStorage).filter(
        (key) => key.startsWith("nexus.long-horizon.direct-request."),
      ))).length === 0,
      "The new origin recovered committed start state from localStorage.",
    );
    await sleep(750);
    assert(
      startRetryRequests.length === 0,
      `New-origin inventory automatically resent committed work: ${JSON.stringify(startRetryRequests)}`,
    );
    assertExactDispatches(coordination, "start-loss");

    await page.getByRole("button", {name: "Open exact saved chat", exact: true}).click();
    const startRetryCard = page.locator(`.swarm-chat-card[data-agent="${AGENT_A}"]`);
    await startRetryCard.locator(".direct-long-goal-recover").waitFor({
      state: "visible", timeout: 30_000,
    });
    await startRetryCard.locator(".direct-long-goal-recover").click();
    const reconciledStartLoss = await waitForGoal(page, chats.startLossChat);
    assertExactTeamGoal(reconciledStartLoss, lostStartReceipt.request_id);
    assert(
      reconciledStartLoss.goal_id === lostStartReceipt.goal.goal_id,
      "Idempotent /start recovery created a second canonical goal.",
    );
    await page.waitForSelector("#directLongGoalRecoveryBoard", {
      state: "hidden", timeout: 30_000,
    });
    await sleep(750);
    assert(
      JSON.stringify(startRetryRequests) === JSON.stringify([
        "/api/long-horizon/start", "/api/long-horizon/discard-admission",
        "/api/long-horizon/acknowledge-admission",
      ]),
      `Explicit committed-start recovery was not one idempotent start plus acknowledgement: ${JSON.stringify(startRetryRequests)}`,
    );
    assertExactDispatches(coordination, "start-loss");
    console.log("pass  a new origin explicitly reconciles committed start once with zero provider redispatch");

    // Now cut the reconciliation response after the server has retired its
    // pending record. The unconsumed terminal journal must survive even when
    // the next browser origin has neither this marker nor an Electron outbox.
    const ackLossCard = await openCompactChat(page, chats.ackLossChat);
    const ackLossText = (
      "Create browser-ack-loss.txt containing exactly browser acknowledgement loss complete. "
      + `${ACK_LOSS_MARKER}.`
    );
    let lostAckReceipt = null;
    const ackLossRequests = [];
    page.on("request", (request) => {
      const pathname = new URL(request.url()).pathname;
      if (request.method() === "POST" && [
        "/api/long-horizon/prepare-admission", "/api/long-horizon/start",
        "/api/long-horizon/admission-goal", "/api/long-horizon/discard-admission",
        "/api/long-horizon/acknowledge-admission",
      ].includes(pathname)) ackLossRequests.push(pathname);
    });
    await page.route("**/api/long-horizon/discard-admission", async (route) => {
      if (lostAckReceipt) {
        await route.continue();
        return;
      }
      const response = await route.fetch();
      assert(response.ok(), `Backend acknowledgement failed before simulated response loss: ${response.status()}`);
      lostAckReceipt = await response.json();
      assert(
        lostAckReceipt?.reconciled === true && lostAckReceipt?.goal?.goal_id,
        "Committed acknowledgement returned no reconciled canonical goal.",
      );
      await route.abort("failed");
    });
    await ackLossCard.locator(".swarm-chat-box").fill(ackLossText);
    await ackLossCard.locator(".swarm-chat-work").click();
    await until(() => Promise.resolve(lostAckReceipt), "committed acknowledgement receipt", 30_000);
    await page.unroute("**/api/long-horizon/discard-admission");
    const acknowledgedBeforeRestart = await waitForGoal(page, chats.ackLossChat);
    assertExactTeamGoal(acknowledgedBeforeRestart, lostAckReceipt.request_id);
    assert(
      acknowledgedBeforeRestart.goal_id === lostAckReceipt.goal.goal_id,
      "Lost acknowledgement receipt did not name the existing durable goal.",
    );
    assertExactDispatches(coordination, "ack-loss");
    await ackLossCard.locator(".direct-long-goal-recover").waitFor({
      state: "visible", timeout: 30_000,
    });
    const ackMarkers = await page.evaluate(() => Object.keys(localStorage).filter(
      (key) => key.startsWith("nexus.long-horizon.direct-request."),
    ));
    assert(ackMarkers.length === 1, `Lost acknowledgement did not retain one exact browser marker: ${ackMarkers}`);
    const inventoryAfterAck = await page.evaluate(
      async () => await request("/api/long-horizon/pending-admissions"),
    );
    assert(
      !inventoryAfterAck.pending.some(
        (one) => one.request_id === lostAckReceipt.request_id,
      ),
      "Committed acknowledgement did not retire its backend pending record.",
    );
    assert(
      inventoryAfterAck.terminal.some((one) => (
        one.request_id === lostAckReceipt.request_id
        && one.terminal_state === "reconciled"
        && one.client_consumed === false
        && one.goal_id === lostAckReceipt.goal.goal_id
      )),
      "Committed reconciliation was not durably discoverable before its response.",
    );
    assert(
      JSON.stringify(ackLossRequests) === JSON.stringify([
        "/api/long-horizon/prepare-admission", "/api/long-horizon/start",
        "/api/long-horizon/discard-admission",
      ]),
      `Acknowledgement loss triggered an automatic retry: ${JSON.stringify(ackLossRequests)}`,
    );
    console.log("pass  browser marker remains visible when a committed reconciliation response is lost");

    const ackOrigin = new URL(server.url).origin;
    const fourthPort = await freePort(thirdPort);
    await server.stop();
    server = null;
    await page.close();
    server = await startSourceServer(tools.python, project, environment, fourthPort);
    assert(
      new URL(server.url).origin !== ackOrigin,
      "Acknowledgement recovery did not move to a fresh browser origin.",
    );
    page = await context.newPage();
    page.on("dialog", (dialog) => dialog.accept().catch(() => {}));
    const ackRetryRequests = [];
    page.on("request", (request) => {
      const pathname = new URL(request.url()).pathname;
      if (request.method() === "POST" && [
        "/api/long-horizon/prepare-admission", "/api/long-horizon/start",
        "/api/long-horizon/admission-goal", "/api/long-horizon/discard-admission",
        "/api/long-horizon/acknowledge-admission",
      ].includes(pathname)) ackRetryRequests.push(pathname);
    });
    await page.goto(server.url, {waitUntil: "domcontentloaded", timeout: 60_000});
    await showSwarm(page);
    await page.waitForSelector(
      "#directLongGoalRecoveryBoard .direct-long-goal-recovery-row",
      {state: "visible", timeout: 30_000},
    );
    assert(
      (await page.evaluate(() => Object.keys(localStorage).filter(
        (key) => key.startsWith("nexus.long-horizon.direct-request."),
      ))).length === 0,
      "The fresh acknowledgement-recovery origin unexpectedly had a local marker.",
    );
    const ackRetryCard = await openCompactChat(page, chats.ackLossChat);
    await ackRetryCard.locator(".direct-long-goal-recover").waitFor({
      state: "visible", timeout: 30_000,
    });
    await sleep(750);
    assert(
      ackRetryRequests.length === 0,
      `New-origin terminal recovery automatically posted: ${JSON.stringify(ackRetryRequests)}`,
    );
    await ackRetryCard.locator(".direct-long-goal-recover").click();
    await until(
      () => Promise.resolve(ackRetryRequests.length >= 2),
      "exact terminal lookup and acknowledgement requests",
      30_000,
    );
    await page.waitForSelector("#directLongGoalRecoveryBoard", {
      state: "hidden", timeout: 30_000,
    });
    await until(async () => {
      const inventory = await page.evaluate(
        async () => await request("/api/long-horizon/pending-admissions"),
      );
      return !inventory.terminal.some(
        (one) => one.request_id === lostAckReceipt.request_id,
      );
    }, "server-side terminal receipt consumption", 30_000);
    const acknowledgedAfterRestart = await waitForGoal(page, chats.ackLossChat);
    assertExactTeamGoal(acknowledgedAfterRestart, lostAckReceipt.request_id);
    assert(
      acknowledgedAfterRestart.goal_id === lostAckReceipt.goal.goal_id,
      "Exact acknowledgement retry selected a different canonical goal.",
    );
    assert(
      JSON.stringify(ackRetryRequests) === JSON.stringify([
        "/api/long-horizon/admission-goal",
        "/api/long-horizon/acknowledge-admission",
      ]),
      `Browser marker retry did anything beyond exact lookup/acknowledgement: ${JSON.stringify(ackRetryRequests)}`,
    );
    assertExactDispatches(coordination, "ack-loss");
    console.log("pass  new-origin restart consumes the terminal receipt without prepare, start, retyping, or redispatch");

    // Lose the final acknowledgement before it reaches the server. Start and
    // reconciliation are already verified, and the local marker is already
    // cleared, so the handler must still succeed while the unconsumed terminal
    // row becomes the only explicit recovery authority.
    const ackBeforeCard = await openCompactChat(page, chats.ackBeforeChat);
    const ackBeforeText = (
      "Create browser-ack-before.txt containing exactly browser precommit acknowledgement complete. "
      + `${ACK_BEFORE_MARKER}.`
    );
    let ackBeforeCut = false;
    const ackBeforeRequests = [];
    page.on("request", (request) => {
      const pathname = new URL(request.url()).pathname;
      if (request.method() === "POST" && [
        "/api/long-horizon/prepare-admission", "/api/long-horizon/start",
        "/api/long-horizon/admission-goal", "/api/long-horizon/discard-admission",
        "/api/long-horizon/acknowledge-admission",
      ].includes(pathname)) ackBeforeRequests.push(pathname);
    });
    await page.route("**/api/long-horizon/acknowledge-admission", async (route) => {
      if (ackBeforeCut) {
        await route.continue();
        return;
      }
      ackBeforeCut = true;
      await route.abort("failed");
    });
    await ackBeforeCard.locator(".swarm-chat-box").fill(ackBeforeText);
    await ackBeforeCard.locator(".swarm-chat-work").click();
    await until(() => Promise.resolve(ackBeforeCut), "precommit acknowledgement cut", 30_000);
    await page.unroute("**/api/long-horizon/acknowledge-admission");
    await ackBeforeCard.locator(".direct-long-goal-recover").waitFor({
      state: "visible", timeout: 30_000,
    });
    assert(
      await ackBeforeCard.locator(".swarm-chat-box").inputValue() === "",
      "A lost final acknowledgement restored a verified goal draft.",
    );
    const ackBeforeTerminal = await page.evaluate(
      async () => (await request("/api/long-horizon/pending-admissions")).terminal || [],
    );
    const ackBeforeRow = ackBeforeTerminal.find(
      (one) => one.chat_id === chats.ackBeforeChat,
    );
    assert(
      ackBeforeRow?.terminal_state === "reconciled"
        && ackBeforeRow.client_consumed === false,
      "A precommit acknowledgement loss did not leave a discoverable terminal receipt.",
    );
    assert(
      JSON.stringify(ackBeforeRequests) === JSON.stringify([
        "/api/long-horizon/prepare-admission", "/api/long-horizon/start",
        "/api/long-horizon/discard-admission",
        "/api/long-horizon/acknowledge-admission",
      ]),
      `Precommit acknowledgement loss changed admission order: ${JSON.stringify(ackBeforeRequests)}`,
    );
    const ackBeforeRetryAt = ackBeforeRequests.length;
    await ackBeforeCard.locator(".direct-long-goal-recover").click();
    await until(
      () => Promise.resolve(ackBeforeRequests.length >= ackBeforeRetryAt + 2),
      "precommit acknowledgement explicit recovery",
      30_000,
    );
    assert(
      JSON.stringify(ackBeforeRequests.slice(ackBeforeRetryAt))
        === JSON.stringify([
          "/api/long-horizon/admission-goal",
          "/api/long-horizon/acknowledge-admission",
        ]),
      `Precommit acknowledgement recovery resent work: ${JSON.stringify(ackBeforeRequests)}`,
    );
    const ackBeforeGoal = await waitForGoal(page, chats.ackBeforeChat);
    assertExactTeamGoal(ackBeforeGoal, ackBeforeRow.request_id);
    assertExactDispatches(coordination, "ack-before");
    console.log("pass  precommit acknowledgement loss stays successful and explicitly consumes without resend");

    // Lose the final acknowledgement response only after route.fetch has
    // committed client_consumed=true. This ambiguity is cleanup-only: the
    // verified Work send remains successful, no draft is restored, and no
    // terminal recovery record remains to replay.
    const ackAfterCard = await openCompactChat(page, chats.ackAfterChat);
    const ackAfterText = (
      "Create browser-ack-after.txt containing exactly browser committed acknowledgement complete. "
      + `${ACK_AFTER_MARKER}.`
    );
    let ackAfterReceipt = null;
    const ackAfterRequests = [];
    page.on("request", (request) => {
      const pathname = new URL(request.url()).pathname;
      if (request.method() === "POST" && [
        "/api/long-horizon/prepare-admission", "/api/long-horizon/start",
        "/api/long-horizon/admission-goal", "/api/long-horizon/discard-admission",
        "/api/long-horizon/acknowledge-admission",
      ].includes(pathname)) ackAfterRequests.push(pathname);
    });
    await page.route("**/api/long-horizon/acknowledge-admission", async (route) => {
      if (ackAfterReceipt) {
        await route.continue();
        return;
      }
      const response = await route.fetch();
      assert(response.ok(), `Terminal acknowledgement failed before response loss: ${response.status()}`);
      ackAfterReceipt = await response.json();
      assert(
        ackAfterReceipt?.client_consumed === true,
        "Terminal acknowledgement did not commit before its response was cut.",
      );
      await route.abort("failed");
    });
    await ackAfterCard.locator(".swarm-chat-box").fill(ackAfterText);
    await ackAfterCard.locator(".swarm-chat-work").click();
    await until(() => Promise.resolve(ackAfterReceipt), "committed acknowledgement", 30_000);
    await page.waitForFunction(
      ([agentId, chatId]) => {
        const activity = swarmChatActivityFor(agentId, chatId);
        return !activity || activity.responseFinished === true;
      },
      [AGENT_A, chats.ackAfterChat], {timeout: 30_000},
    );
    await page.unroute("**/api/long-horizon/acknowledge-admission");
    const ackAfterGoal = await waitForGoal(page, chats.ackAfterChat);
    assertExactTeamGoal(ackAfterGoal, ackAfterReceipt.request_id);
    assertExactDispatches(coordination, "ack-after");
    assert(
      await ackAfterCard.locator(".swarm-chat-box").inputValue() === "",
      "Lost committed acknowledgement response restored a verified goal draft.",
    );
    const ackAfterInventory = await page.evaluate(
      async () => await request("/api/long-horizon/pending-admissions"),
    );
    assert(
      !ackAfterInventory.terminal.some(
        (one) => one.request_id === ackAfterReceipt.request_id,
      ),
      "Consumed terminal receipt remained visible after acknowledgement commit.",
    );
    assert(
      JSON.stringify(ackAfterRequests) === JSON.stringify([
        "/api/long-horizon/prepare-admission", "/api/long-horizon/start",
        "/api/long-horizon/discard-admission",
        "/api/long-horizon/acknowledge-admission",
      ]),
      `Committed acknowledgement response loss triggered a resend: ${JSON.stringify(ackAfterRequests)}`,
    );
    assertExactDispatches(coordination, "ack-after");
    console.log("pass  committed acknowledgement response loss cannot fail or replay verified Work");

    await openBigChat(page, chats.normalChat);
    const normalGoalText = (
      "Create browser-normal.txt containing exactly browser normal complete. "
      + `${NORMAL_MARKER}.`
    );
    const normalRequests = [];
    page.on("request", (request) => {
      const pathname = new URL(request.url()).pathname;
      if (request.method() === "POST" && [
        "/api/long-horizon/prepare-admission", "/api/long-horizon/start",
        "/api/long-horizon/discard-admission",
        "/api/long-horizon/acknowledge-admission",
      ].includes(pathname)) normalRequests.push(pathname);
    });
    await page.fill("#theBigChatBox", normalGoalText);
    await page.click("#theBigChatWork");
    const normalGoal = await waitForGoal(page, chats.normalChat);
    assertExactTeamGoal(normalGoal);
    assertExactDispatches(coordination, "normal");
    assert(
      fs.readFileSync(path.join(project, "browser-normal.txt"), "utf8").trim()
        === "browser normal complete",
      "Maximized Work did not achieve its exact normal file outcome.",
    );
    assert(
      JSON.stringify(normalRequests) === JSON.stringify([
        "/api/long-horizon/prepare-admission", "/api/long-horizon/start",
        "/api/long-horizon/discard-admission",
        "/api/long-horizon/acknowledge-admission",
      ]),
      `Maximized Work did not prepare, start, and reconcile exactly once: ${JSON.stringify(normalRequests)}`,
    );
    console.log("pass  maximized Work follows the normal prepare/start/reconcile path and both participants verify complete");
    console.log("\nSource-browser Work together acceptance passed.");
    passed = true;
  } finally {
    if (context) await context.close().catch(() => {});
    if (browser) await browser.close().catch(() => {});
    if (server) await server.stop().catch((error) => {
      console.error(`warn  could not stop source UI: ${error}`);
    });
    if (passed) {
      try {
        removeFixtureSafely(fixture);
      } catch (error) {
        console.warn(`warn  acceptance passed but fixture remains at ${fixture}: ${error}`);
      }
    } else {
      console.error(`info  failed fixture preserved for inspection: ${fixture}`);
    }
  }
}

main().catch((error) => {
  console.error(`\n${error && error.stack ? error.stack : error}`);
  process.exit(1);
});
