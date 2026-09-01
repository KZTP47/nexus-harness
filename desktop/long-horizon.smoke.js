"use strict";

// User-perspective acceptance for durable Work together project goals.
//
// This launches the packaged Electron app with a clean Windows profile and an
// arbitrary temporary project. Two deterministic local provider processes are
// connected as a real pair. First, the backend admission request is cut after
// Electron has durably saved an exact prompt and binary attachment. The app is
// restarted and that request is reconciled through the visible recovery UI.
// Goal A then deliberately waits inside its provider so Goal B can be submitted
// from another saved chat against the same project. The check releases A,
// observes B promote and finish, restarts again, and reopens Chat 2 to prove the
// prompts, attachment, outcomes, and verified lifecycle are durable.

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const crypto = require("node:crypto");
const childProcess = require("node:child_process");
const { _electron: electron } = require("playwright-core");

const TIMEOUT_MS = 240000;
const OUTPUT = path.join(__dirname, "build-output");
const GIVEN_APP = process.argv[2] || "";
const AGENT_A = "nexus-smoke-agent-a";
const AGENT_B = "nexus-smoke-agent-b";
const PROJECT_ID = "nexus-smoke-project";
const RECOVERY_ATTACHMENT = Buffer.from(
  Array.from({length: 4097}, (_value, index) => (index * 73 + 19) % 256),
);
const RECOVERY_ATTACHMENT_SHA256 = crypto
  .createHash("sha256").update(RECOVERY_ATTACHMENT).digest("hex");

function builtApp() {
  if (GIVEN_APP) {
    const given = path.resolve(GIVEN_APP);
    if (!fs.existsSync(given)) throw new Error(`The app does not exist: ${given}`);
    return given;
  }
  for (const name of fs.readdirSync(OUTPUT)) {
    const folder = path.join(OUTPUT, name);
    if (!fs.statSync(folder).isDirectory() || !name.includes("unpacked")) continue;
    const found = fs.readdirSync(folder)
      .find((one) => one.endsWith(".exe") && !one.startsWith("Uninstall"));
    if (found) return path.join(folder, found);
  }
  throw new Error(`No built app in ${OUTPUT}. Build it first: npm run build`);
}

function writeFixture(project, coordination) {
  fs.mkdirSync(path.join(project, ".harness"), {recursive: true});
  fs.mkdirSync(coordination, {recursive: true});
  const provider = path.join(project, "scripted-provider.ps1");
  const verifier = path.join(project, "test_verify_result.py");
  fs.writeFileSync(provider, String.raw`param(
  [Parameter(Mandatory=$true)][string]$Coordination,
  [Parameter(Mandatory=$true)][ValidateSet('smoke-a', 'smoke-b')][string]$RouteLabel
)
$ErrorActionPreference = 'Stop'
$payload = [Console]::In.ReadToEnd() | ConvertFrom-Json
$context = [string]$payload.dynamic_context
$isA = $context.Contains('NEXUS-SMOKE-GOAL-A')
$isB = $context.Contains('NEXUS-SMOKE-GOAL-B')
$isR = $context.Contains('NEXUS-SMOKE-GOAL-RECOVERY')
$isD = $context.Contains('NEXUS-SMOKE-GOAL-DISCARD')
if (-not $isA -and -not $isB -and -not $isR -and -not $isD) { throw 'The scripted provider received an unknown goal.' }
if ($isB -and -not $context.Contains('NEXUS-SMOKE-TAIL-B-REQUIRED')) {
  throw 'The exact Goal B tail instruction was clipped before provider dispatch.'
}
if ($isR) {
  $files = @($payload.attachments)
  if ($files.Count -ne 1) { throw 'The recovered goal did not deliver its one exact attachment.' }
  $bytes = [Convert]::FromBase64String([string]$files[0].data)
  $hasher = [Security.Cryptography.SHA256]::Create()
  try { $actualHash = [BitConverter]::ToString($hasher.ComputeHash($bytes)).Replace('-', '').ToLowerInvariant() }
  finally { $hasher.Dispose() }
  if ($actualHash -ne '${RECOVERY_ATTACHMENT_SHA256}') {
    throw "The recovered attachment bytes changed: $actualHash"
  }
}
$suffix = if ($isA) { 'a' } elseif ($isB) { 'b' } elseif ($isR) { 'r' } else { 'd' }
$target = "goal-$suffix.txt"
$wanted = if ($isA) { 'alpha complete' } elseif ($isB) { 'beta complete' } elseif ($isR) { 'recovery complete' } else { 'discard request dispatched' }
if ($isR) {
  [IO.File]::AppendAllText((Join-Path $Coordination 'entered-r.marker'), ($RouteLabel + [Environment]::NewLine))
} else {
  [IO.File]::WriteAllText((Join-Path $Coordination "entered-$suffix.marker"), 'entered')
}
if ($isA) {
  $deadline = [DateTime]::UtcNow.AddSeconds(90)
  while (-not (Test-Path -LiteralPath (Join-Path $Coordination 'release-a.marker'))) {
    if ([DateTime]::UtcNow -ge $deadline) { throw 'Timed out waiting for the E2E release marker.' }
    [Threading.Thread]::Sleep(50)
  }
}
$changes = @()
if (-not (Test-Path -LiteralPath $target)) {
  $changes = @(@{path=$target; content=($wanted + [Environment]::NewLine); delete=$false; reason='fulfil the exact saved goal'})
}
$refs = @("file:$target")
$criteria = @(
  @{criterion='Original objective is satisfied'; evidence_refs=$refs},
  @{criterion='Every required task is complete'; evidence_refs=$refs},
  @{criterion='Configured deterministic verification passes'; evidence_refs=$refs}
)
$action = @{
  action='complete'; summary="Created or confirmed $target"; evidence=$refs; risk='low'
  changes=$changes; needs_files=@(); tool_calls=@(); tasks=@(); handoff_agent_id=''
  questions=@(); criteria_evidence=$criteria
}
@{text=($action | ConvertTo-Json -Depth 12 -Compress); finish_reason='stop'} |
  ConvertTo-Json -Depth 15 -Compress
`, "utf8");
  fs.writeFileSync(verifier, `import unittest
from pathlib import Path


class ExactGoalResultTests(unittest.TestCase):
    def test_existing_goal_results_match_their_exact_requested_contents(self):
        expected = {
            "goal-a.txt": "alpha complete",
            "goal-b.txt": "beta complete",
            "goal-r.txt": "recovery complete",
        }
        found = sorted(Path(".").glob("goal-?.txt"))
        self.assertTrue(found, "No requested goal result exists.")
        for item in found:
            with self.subTest(path=item.name):
                self.assertIn(item.name, expected, f"Unexpected goal result {item.name}")
                self.assertEqual(
                    item.read_text(encoding="utf-8").strip(),
                    expected[item.name],
                    f"Wrong exact content in {item.name}",
                )


if __name__ == "__main__":
    unittest.main()
`, "utf8");
  const command = (route) => [
    "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
    "-File", provider, coordination, route,
  ];
  const config = {
    schema_version: 1,
    providers: {
      "smoke-a": {
        kind: "local", model: "deterministic-smoke-a", command: command("smoke-a"),
        endpoint: "http://127.0.0.1:1", max_concurrency: 1,
      },
      "smoke-b": {
        kind: "local", model: "deterministic-smoke-b", command: command("smoke-b"),
        endpoint: "http://127.0.0.1:1", max_concurrency: 1,
      },
    },
    project: {
      // The verifier deliberately uses the packaged private Python, which has
      // a supported Windows write-containment profile. PowerShell remains the
      // provider transport above, but is not an accepted verification sandbox.
      test_commands: [[
        "python", "-m", "unittest", "discover", "-s", ".", "-p", "test_verify_result.py",
      ]],
    },
    workflow: {require_review: false, reviewers: 1, review_parallelism: 1},
  };
  fs.writeFileSync(
    path.join(project, ".harness", "config.local.json"),
    `${JSON.stringify(config, null, 2)}\n`, "utf8",
  );
}

function isolatedEnvironment(profile) {
  const roaming = path.join(profile, "AppData", "Roaming");
  const local = path.join(profile, "AppData", "Local");
  const temporary = path.join(profile, "Temp");
  fs.mkdirSync(roaming, {recursive: true});
  fs.mkdirSync(local, {recursive: true});
  fs.mkdirSync(temporary, {recursive: true});
  const parsed = path.parse(profile);
  const safeNames = [
    "SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "COMSPEC", "SYSTEMDRIVE",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "PROCESSOR_IDENTIFIER",
    "OS", "LANG", "LC_ALL", "CI", "GITHUB_ACTIONS",
  ];
  const source = new Map(
    Object.entries(process.env).map(([key, value]) => [key.toUpperCase(), value]),
  );
  const filtered = {};
  for (const name of safeNames) {
    if (source.get(name) != null) filtered[name] = source.get(name);
  }
  return {
    ...filtered,
    APPDATA: roaming,
    LOCALAPPDATA: local,
    USERPROFILE: profile,
    HOME: profile,
    HOMEDRIVE: parsed.root.slice(0, 2),
    HOMEPATH: profile.slice(parsed.root.length - 1),
    TEMP: temporary,
    TMP: temporary,
  };
}

function trustFixtureSettings(exe, project, environment) {
  const installedRoot = path.dirname(exe);
  const python = path.join(installedRoot, "resources", "runtime", "python.exe");
  const harnessSource = path.join(installedRoot, "resources", "harness", "src");
  for (const target of [python, harnessSource]) {
    if (!fs.existsSync(target)) {
      throw new Error(`The packaged fixture runtime is missing ${target}`);
    }
  }
  const code = [
    "import sys",
    "from pathlib import Path",
    `sys.path.insert(0, ${JSON.stringify(harnessSource)})`,
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
      `Could not trust the isolated packaged fixture: ${result.stderr || result.stdout}`,
    );
  }
}

async function launch(exe, profile, project, environment) {
  const app = await electron.launch({
    executablePath: exe,
    args: [`--user-data-dir=${path.join(profile, "Electron profile")}`, "--project", project],
    env: environment,
    timeout: TIMEOUT_MS,
  });
  try {
    const page = await app.firstWindow({timeout: TIMEOUT_MS});
    const first = await Promise.race([
      page.waitForFunction(() => location.protocol === "http:", null, {timeout: 120000})
        .then(() => "panel"),
      page.waitForSelector("#repair", {state: "visible", timeout: 120000})
        .then(() => "repair"),
    ]);
    if (first === "repair") {
      await page.click("#repair");
      await page.waitForFunction(() => location.protocol === "http:", null, {timeout: 120000});
    }
    return {app, page};
  } catch (error) {
    await app.close().catch(() => {});
    throw error;
  }
}

async function configureBoard(page, project) {
  return page.evaluate(async ({agentA, agentB, projectId, projectPath}) => {
    const standing = await request("/api/swarm?refresh_providers=true");
    const board = standing.board;
    board.agents = [
      {id: agentA, name: "Smoke agent A", who: "smoke-a", job: "Lead deterministic work",
        at: {x: 60, y: 60}, colour: "#4f46e5", icon: "robot", bubble_colour: "#eef2ff",
        profile_picture: "", picture_zoom: 100, picture_hue: 0, filed_as: "Smoke agent A"},
      {id: agentB, name: "Smoke agent B", who: "smoke-b", job: "Review deterministic work",
        at: {x: 330, y: 60}, colour: "#0f766e", icon: "robot", bubble_colour: "#ecfdf5",
        profile_picture: "", picture_zoom: 100, picture_hue: 0, filed_as: "Smoke agent B"},
    ];
    board.projects = [{
      id: projectId, path: projectPath, name: "Arbitrary portable project", is_there: true,
      tasks: [], at: {x: 60, y: 390}, approved_test_command_digest: "",
    }];
    board.works_on = [
      {agent: agentA, project: projectId}, {agent: agentB, project: projectId},
    ];
    board.talks_to = [{one: agentA, other: agentB}];
    board.made_agents = Math.max(Number(board.made_agents || 0), 2);
    board.made_projects = Math.max(Number(board.made_projects || 0), 1);
    await request("/api/swarm/save", {
      method: "POST", body: JSON.stringify({board}),
    });
    const recovery = await request("/api/swarm/chats/create", {
      method: "POST", body: JSON.stringify({agent: agentA, peer: agentB}),
    });
    const chatR = recovery.active;
    await request("/api/swarm/chats/project", {
      method: "POST", body: JSON.stringify({agent: agentA, chat: chatR, project: projectId}),
    });
    const first = await request("/api/swarm/chats/create", {
      method: "POST", body: JSON.stringify({agent: agentA, peer: agentB}),
    });
    const chatA = first.active;
    await request("/api/swarm/chats/project", {
      method: "POST", body: JSON.stringify({agent: agentA, chat: chatA, project: projectId}),
    });
    const second = await request("/api/swarm/chats/create", {
      method: "POST", body: JSON.stringify({agent: agentA, peer: agentB}),
    });
    const chatB = second.active;
    await request("/api/swarm/chats/project", {
      method: "POST", body: JSON.stringify({agent: agentA, chat: chatB, project: projectId}),
    });
    return {chatR, chatA, chatB};
  }, {agentA: AGENT_A, agentB: AGENT_B, projectId: PROJECT_ID, projectPath: project});
}

async function openBigChat(page, chatId) {
  await page.click('[data-view="swarm"]', {timeout: 30000});
  await page.waitForFunction(
    () => !document.getElementById("swarmView").hidden,
    null, {timeout: 30000},
  );
  const card = `.swarm-box[data-id="${AGENT_A}"] .swarm-icon-button[data-does="chat"]`;
  await page.click(card, {timeout: 30000});
  await page.getByRole("button", {name: "Open full Nexus chat"}).click();
  await page.waitForSelector("#theBigChat:not([hidden])", {timeout: 30000});
  await page.click(
    `#theBigChatConversationList [data-conversation-action="pick"][data-chat-id="${chatId}"]`,
    {timeout: 30000},
  );
  await page.waitForFunction(
    ([wanted, agentId]) => activeConversationFor(agentId)?.id === wanted
      && !swarmChatIsHydrating(agentId),
    [chatId, AGENT_A], {timeout: 30000},
  );
}

async function submitGoal(page, chatId, words) {
  await page.click(
    `#theBigChatConversationList [data-conversation-action="pick"][data-chat-id="${chatId}"]`,
    {timeout: 30000},
  );
  await page.waitForFunction(
    ([wanted, agentId]) => activeConversationFor(agentId)?.id === wanted
      && !swarmChatIsHydrating(agentId),
    [chatId, AGENT_A], {timeout: 30000},
  );
  await page.fill("#theBigChatBox", words);
  await page.waitForFunction(
    () => !document.getElementById("theBigChatWork").disabled,
    null, {timeout: 30000},
  );
  page.once("dialog", (dialog) => dialog.accept());
  await page.click("#theBigChatWork");
  await page.waitForFunction(
    (wanted) => document.getElementById("theBigChatSaid").textContent.includes(wanted),
    words, {timeout: 30000},
  );
}

async function waitForGoals(page, predicate, timeout = TIMEOUT_MS) {
  const deadline = Date.now() + timeout;
  let goals = [];
  while (Date.now() < deadline) {
    goals = await page.evaluate(
      async () => (await request("/api/long-horizon/goals")).goals || [],
    );
    if (predicate(goals)) return goals;
    await new Promise((resolve) => setTimeout(resolve, 300));
  }
  throw new Error(`Timed out waiting for long-horizon goals: ${JSON.stringify(goals)}`);
}

async function main() {
  if (process.platform !== "win32") {
    console.log("skip  the packaged long-horizon desktop acceptance is Windows-specific");
    return;
  }
  const exe = builtApp();
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "nexus-long-horizon-ui-"));
  const profile = path.join(root, "portable user profile");
  const project = path.join(root, "arbitrary project with spaces");
  const coordination = path.join(root, "provider coordination");
  console.log(`info  isolated fixture: ${root}`);
  fs.mkdirSync(profile, {recursive: true});
  fs.mkdirSync(project, {recursive: true});
  writeFixture(project, coordination);
  const environment = isolatedEnvironment(profile);
  trustFixtureSettings(exe, project, environment);
  let running = null;
  let passed = false;
  try {
    running = await launch(exe, profile, project, environment);
    let {app, page} = running;
    const chats = await configureBoard(page, project);
    // Reload exactly as a user reopening the panel would. The product must
    // hydrate the saved board and chat list through its visible UI path.
    await page.reload({waitUntil: "domcontentloaded", timeout: 120000});
    await page.waitForFunction(() => location.protocol === "http:", null, {timeout: 120000});
    await openBigChat(page, chats.chatR);
    console.log("pass  a clean packaged profile opens three real saved pair chats");

    // Keep the selected-project objective an exact file-state operation. The
    // browser/provider assertions below, not project-authored tests, prove the
    // crash recovery, byte-exact attachment delivery, and one dispatch for
    // each selected participant.
    const recoveryGoal = "Create goal-r.txt containing exactly recovery complete after restoring this exact request and attachment. NEXUS-SMOKE-GOAL-RECOVERY.";
    await page.setInputFiles("#theBigChatFiles", {
      name: "exact-recovery-input.bin",
      mimeType: "application/octet-stream",
      buffer: RECOVERY_ATTACHMENT,
    });
    await page.waitForSelector(
      "#theBigChatAttachments .chat-attachment-name",
      {state: "visible", timeout: 30000},
    );
    let cutAdmission = false;
    const admissionUrl = "**/api/long-horizon/prepare-admission";
    await page.route(admissionUrl, async (route) => {
      if (!cutAdmission) {
        cutAdmission = true;
        await route.abort("failed");
      } else {
        await route.continue();
      }
    });
    await page.fill("#theBigChatBox", recoveryGoal);
    page.once("dialog", (dialog) => dialog.accept());
    await page.click("#theBigChatWork");
    await page.waitForSelector(
      "#directLongGoalRecoveryBoard:not([hidden]) .direct-long-goal-open",
      {timeout: 30000},
    );
    if (!cutAdmission) throw new Error("The admission cut never reached the backend boundary.");
    if (await page.inputValue("#theBigChatBox") !== recoveryGoal) {
      throw new Error("The exact draft was cleared before backend durability was acknowledged.");
    }
    await page.unroute(admissionUrl);
    console.log("pass  a cut backend admission leaves the exact desktop outbox visibly recoverable");

    await app.close();
    running = await launch(exe, profile, project, environment);
    ({app, page} = running);
    await page.waitForSelector("#swarmView:not([hidden])", {timeout: 30000});
    await page.waitForSelector(
      "#directLongGoalRecoveryBoard:not([hidden]) .direct-long-goal-open",
      {timeout: 30000},
    );
    let preReconcileAdmissionRequests = 0;
    const observePreReconcileAdmission = (requestEvent) => {
      const requestPath = new URL(requestEvent.url()).pathname;
      if (requestEvent.method() === "POST" && [
        "/api/long-horizon/prepare-admission", "/api/long-horizon/start",
      ].includes(requestPath)) {
        preReconcileAdmissionRequests += 1;
      }
    };
    page.on("request", observePreReconcileAdmission);
    await page.locator("#directLongGoalRecoveryBoard").getByRole(
      "button", {name: "Open exact saved chat", exact: true},
    ).click();
    await page.waitForFunction(
      ([wanted, agentId]) => activeConversationFor(agentId)?.id === wanted
        && !swarmChatIsHydrating(agentId),
      [chats.chatR, AGENT_A], {timeout: 30000},
    );
    const recoveryCard = page.locator(`.swarm-chat-card[data-agent="${AGENT_A}"]`);
    await recoveryCard.locator(".direct-long-goal-exact-payload").waitFor(
      {state: "visible", timeout: 30000},
    );
    const exactRecoveryText = await recoveryCard.locator(
      ".direct-long-goal-exact-text",
    ).textContent();
    if (exactRecoveryText !== recoveryGoal) {
      throw new Error(
        `The pre-reconcile chat did not show the exact full saved prompt: ${exactRecoveryText}`,
      );
    }
    const exactRecoveryFiles = await recoveryCard.locator(
      ".direct-long-goal-exact-attachments",
    ).textContent();
    if (!exactRecoveryFiles.includes("exact-recovery-input.bin")
        || exactRecoveryFiles.replace(/\D/g, "") !== String(RECOVERY_ATTACHMENT.length)) {
      throw new Error(
        `The pre-reconcile chat did not show the saved attachment name and size: ${exactRecoveryFiles}`,
      );
    }
    if (await recoveryCard.locator(".chat-attachments .chat-attachment").count() !== 0) {
      throw new Error(
        "Opening recovery repopulated the resubmittable attachment composer.",
      );
    }
    await page.waitForTimeout(300);
    if (preReconcileAdmissionRequests !== 0) {
      throw new Error(
        `Opening exact recovery automatically sent ${preReconcileAdmissionRequests} admission request(s).`,
      );
    }
    page.off("request", observePreReconcileAdmission);
    const compactRecovery = recoveryCard.getByRole(
      "button", {name: "Reconcile saved goal request", exact: true},
    );
    await compactRecovery.waitFor({state: "visible", timeout: 30000});
    await compactRecovery.click();
    const recoveryGoals = await waitForGoals(page, (items) => (
      items.some((one) => one.conversation_id && one.status === "complete")
    ));
    const recoveredGoal = recoveryGoals.find((one) => one.conversation_id === chats.chatR);
    if (!recoveredGoal || recoveredGoal.status !== "complete") {
      throw new Error(`The exact recovered goal did not complete: ${JSON.stringify(recoveryGoals)}`);
    }
    const recoveryResult = path.join(project, "goal-r.txt");
    if (!fs.existsSync(recoveryResult)
        || fs.readFileSync(recoveryResult, "utf8").trim() !== "recovery complete") {
      throw new Error("The reconciled recovery goal did not achieve its exact file outcome.");
    }
    const recoveryDispatchRoutes = fs.readFileSync(
      path.join(coordination, "entered-r.marker"), "utf8",
    ).trim().split(/\r?\n/).filter(Boolean).sort();
    const expectedRecoveryAgents = [AGENT_A, AGENT_B].sort();
    const expectedRecoveryRoutes = ["smoke-a", "smoke-b"];
    const recoveryTasks = Array.isArray(recoveredGoal.tasks) ? recoveredGoal.tasks : [];
    const recoveredTaskAgents = recoveryTasks
      .map((task) => task.assigned_agent_id)
      .filter((agentId) => expectedRecoveryAgents.includes(agentId))
      .sort();
    const acknowledgedEffects = recoveryTasks
      .filter((task) => expectedRecoveryAgents.includes(task.assigned_agent_id))
      .map((task) => ({
        agent: task.assigned_agent_id,
        attempts: task.attempts,
        effect: task.provider_effect_id,
        state: task.provider_effect_state,
      }));
    const exactParticipantDispatch = recoveredGoal.require_all_participants === true
      && Array.isArray(recoveredGoal.requested_agent_ids)
      && [...recoveredGoal.requested_agent_ids].sort().every(
        (agentId, index) => agentId === expectedRecoveryAgents[index],
      )
      && recoveredGoal.requested_agent_ids.length === expectedRecoveryAgents.length
      && recoveryTasks.length === expectedRecoveryAgents.length
      && recoveredTaskAgents.length === expectedRecoveryAgents.length
      && recoveredTaskAgents.every((agentId, index) => agentId === expectedRecoveryAgents[index])
      && acknowledgedEffects.every((effect) => (
        effect.attempts === 1
        && typeof effect.effect === "string" && effect.effect.length > 0
        && effect.state === "acknowledged"
      ))
      && new Set(acknowledgedEffects.map((effect) => effect.effect)).size
        === expectedRecoveryAgents.length;
    if (recoveryDispatchRoutes.length !== expectedRecoveryRoutes.length
        || !recoveryDispatchRoutes.every(
          (route, index) => route === expectedRecoveryRoutes[index],
        )
        || Number(recoveredGoal.budget?.provider_calls) !== expectedRecoveryAgents.length
        || !exactParticipantDispatch) {
      throw new Error(
        "The recovered request did not dispatch each selected participant exactly once: "
        + JSON.stringify({
          recoveryDispatchRoutes,
          providerCalls: recoveredGoal.budget?.provider_calls,
          expectedRecoveryAgents,
          acknowledgedEffects,
        }),
      );
    }
    await page.waitForSelector("#directLongGoalRecoveryBoard", {state: "hidden", timeout: 30000});
    await openBigChat(page, chats.chatR);
    const recoveryTranscript = await page.textContent("#theBigChatSaid");
    if (!recoveryTranscript.includes(recoveryGoal) || /Answer received/i.test(recoveryTranscript)) {
      throw new Error(`The recovered exact prompt was not shown truthfully: ${recoveryTranscript}`);
    }
    console.log("pass  restart recovery admits once and dispatches each required participant once with byte-exact attachment and verified result");

    const discardGoal = "NEXUS-SMOKE-GOAL-DISCARD — Keep this second exact request unadmitted, then discard it without provider dispatch.";
    const unexpectedStarts = [];
    const observeUnexpectedStart = (requestEvent) => {
      if (requestEvent.method() === "POST"
          && new URL(requestEvent.url()).pathname === "/api/long-horizon/start") {
        unexpectedStarts.push(requestEvent.url());
      }
    };
    page.on("request", observeUnexpectedStart);
    let cutDiscardAdmission = false;
    await page.route(admissionUrl, async (route) => {
      if (!cutDiscardAdmission) {
        cutDiscardAdmission = true;
        await route.abort("failed");
      } else {
        await route.continue();
      }
    });
    try {
      await page.fill("#theBigChatBox", discardGoal);
      page.once("dialog", (dialog) => dialog.accept());
      await page.click("#theBigChatWork");
      await page.waitForSelector(
        "#directLongGoalRecoveryBoard:not([hidden]) .direct-long-goal-board-discard",
        {timeout: 30000},
      );
      if (!cutDiscardAdmission) {
        throw new Error("The second admission cut never reached the backend boundary.");
      }
      await page.getByRole("button", {name: "Minimise", exact: true}).click();
      const compactDiscard = recoveryCard.getByRole(
        "button", {name: "Discard saved request", exact: true},
      );
      await compactDiscard.waitFor({state: "visible", timeout: 30000});
      page.once("dialog", (dialog) => dialog.dismiss());
      await compactDiscard.click();
      await page.locator("#directLongGoalRecoveryBoard").getByRole(
        "button", {name: "Discard exact request", exact: true},
      ).waitFor({state: "visible", timeout: 30000});
      page.once("dialog", (dialog) => dialog.accept());
      await page.locator("#directLongGoalRecoveryBoard").getByRole(
        "button", {name: "Discard exact request", exact: true},
      ).click();
      await page.waitForSelector(
        "#directLongGoalRecoveryBoard", {state: "hidden", timeout: 30000},
      );
      await new Promise((resolve) => setTimeout(resolve, 1000));
      const discardMarker = path.join(coordination, "entered-d.marker");
      if (unexpectedStarts.length || fs.existsSync(discardMarker)
          || fs.existsSync(path.join(project, "goal-d.txt"))) {
        throw new Error(
          `Discarding the unadmitted request started provider work: ${JSON.stringify({
            starts: unexpectedStarts, marker: fs.existsSync(discardMarker),
          })}`,
        );
      }
    } finally {
      page.off("request", observeUnexpectedStart);
      await page.unroute(admissionUrl);
    }
    console.log("pass  both exact discard labels preserve zero admission and zero provider dispatch");

    await openBigChat(page, chats.chatA);

    const goalA = "Create goal-a.txt containing exactly alpha complete. NEXUS-SMOKE-GOAL-A.";
    const goalB = "Create goal-b.txt containing exactly beta complete. "
      + "NEXUS-SMOKE-GOAL-B. Preserve this bounded objective exactly. "
      + "portable context ".repeat(1400)
      + "NEXUS-SMOKE-TAIL-B-REQUIRED.";
    if (goalB.length <= 20000) throw new Error("Goal B no longer exercises the former clipping boundary.");
    await submitGoal(page, chats.chatA, goalA);
    const enteredA = path.join(coordination, "entered-a.marker");
    for (let attempt = 0; attempt < 600 && !fs.existsSync(enteredA); attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
    if (!fs.existsSync(enteredA)) throw new Error("Goal A never reached its real provider process.");
    console.log("pass  Chat 1 shows its durable prompt while Goal A is still running");

    await submitGoal(page, chats.chatB, goalB);
    let goals = await waitForGoals(page, (items) => (
      items.some((one) => one.conversation_id && one.status === "waiting_for_project")
      && items.some((one) => one.status === "running" || one.status === "queued")
    ));
    const second = goals.find((one) => one.conversation_id === chats.chatB);
    if (!second || second.status !== "waiting_for_project") {
      throw new Error(`Chat 2 did not keep a waiting goal: ${JSON.stringify(goals)}`);
    }
    await page.waitForSelector(
      `#theBigChatSaid [data-goal-id="${second.goal_id}"][data-goal-status="waiting_for_project"]`,
      {timeout: 30000},
    );
    const waitingWords = await page.textContent("#theBigChatSaid");
    if (!waitingWords.includes(goalB)
        || !/waiting|queued behind|project work ahead/i.test(waitingWords)
        || /Answer received/i.test(waitingWords)) {
      throw new Error(`Chat 2 did not show a truthful durable wait: ${waitingWords}`);
    }
    if (fs.existsSync(path.join(coordination, "entered-b.marker"))) {
      throw new Error("The waiting goal dispatched its provider before promotion.");
    }
    console.log("pass  Chat 2 persists the exact prompt and visibly waits without dispatching");

    fs.writeFileSync(path.join(coordination, "release-a.marker"), "release", "utf8");
    goals = await waitForGoals(page, (items) => (
      items.filter((one) => one.conversation_id).length >= 2
      && items.filter((one) => one.conversation_id).every((one) => one.status === "complete")
    ));
    if (!fs.existsSync(path.join(coordination, "entered-b.marker"))) {
      throw new Error("Goal B was never promoted into its provider process.");
    }
    if (fs.readFileSync(path.join(project, "goal-a.txt"), "utf8").trim() !== "alpha complete"
        || fs.readFileSync(path.join(project, "goal-b.txt"), "utf8").trim() !== "beta complete") {
      throw new Error("The promoted goals did not achieve their exact file objectives.");
    }
    await page.waitForSelector(
      `#theBigChatSaid [data-goal-id="${second.goal_id}"][data-goal-status="complete"]`,
      {timeout: 30000},
    );
    console.log("pass  terminal ownership promotes once and both exact goals verify complete");

    await app.close();
    running = await launch(exe, profile, project, environment);
    await openBigChat(running.page, chats.chatB);
    await running.page.waitForSelector(
      `#theBigChatSaid [data-goal-id="${second.goal_id}"][data-goal-status="complete"]`,
      {timeout: 30000},
    );
    const restored = await running.page.textContent("#theBigChatSaid");
    if (!restored.includes(goalB) || /Answer received/i.test(restored)) {
      throw new Error(`Restart did not restore Chat 2 truthfully: ${restored}`);
    }
    const restoredGoals = await running.page.evaluate(
      async () => (await request("/api/long-horizon/goals")).goals || [],
    );
    if (restoredGoals.filter((one) => one.conversation_id).length < 2
        || restoredGoals.filter((one) => one.conversation_id)
          .some((one) => one.status !== "complete")) {
      throw new Error(`Restart lost durable goal state: ${JSON.stringify(restoredGoals)}`);
    }
    console.log("pass  closing and reopening the packaged app restores Chat 2 and both goals");
    console.log("\nPackaged Work together acceptance passed from a clean user's perspective.");
    passed = true;
  } finally {
    if (running?.app) await running.app.close().catch(() => {});
    if (passed) {
      try {
        fs.rmSync(root, {recursive: true, force: true, maxRetries: 10, retryDelay: 200});
      } catch (cleanupError) {
        console.warn(`warn  passed, but the temporary fixture is still at ${root}: ${cleanupError}`);
      }
    } else {
      console.error(`info  failed fixture preserved for inspection: ${root}`);
    }
  }
}

main().catch((error) => {
  console.error(`\n${error && error.stack ? error.stack : error}`);
  process.exit(1);
});
