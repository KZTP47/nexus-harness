"use strict";

// Black-box acceptance for the failure that unit and renderer-injection tests
// cannot prove: a person clicks in the packaged Electron app, the real Python
// server dispatches through three real provider adapters, durable state is
// written, and the visible UI reports 2/2, 1/2, and 0/2 replies truthfully.
//
// Fixture setup may write a trusted temporary provider config and change the
// loopback provider's scripted fault mode. Every product action and assertion
// after launch uses Playwright locators; this file never injects renderer state,
// replaces request(), or calls an internal API in place of a user action.

const assert = require("node:assert/strict");
const childProcess = require("node:child_process");
const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");
const { _electron: electron } = require("playwright-core");

const TIMEOUT_MS = 180_000;
const OUTPUT = path.join(__dirname, "build-output");
const UNPACKED_APP = path.join(OUTPUT, "win-unpacked", "Nexus Harness.exe");
const CUSTOM_CRITERION = "Each of the three selected providers contributes";
const GIVEN_APP = process.argv[2] || "";
const PAIR_PROVIDERS = ["openai", "anthropic"];
const PROVIDER_CONTRACTS = {
  openai: {
    pathname: "/openai/responses", model: "gpt-fixture",
    keyHeader: "authorization", keyValue: "Bearer fixture-openai-key",
  },
  anthropic: {
    pathname: "/anthropic/messages", model: "claude-sonnet-5",
    keyHeader: "x-api-key", keyValue: "fixture-anthropic-key",
  },
  gemini: {
    pathname: "/gemini/interactions", model: "gemini-fixture",
    keyHeader: "x-goog-api-key", keyValue: "fixture-gemini-key",
  },
};

function builtApp() {
  if (GIVEN_APP) {
    const given = path.resolve(GIVEN_APP);
    if (!fs.existsSync(given) || !fs.statSync(given).isFile()) {
      throw new Error(`The app does not exist: ${given}`);
    }
    return given;
  }
  if (!fs.existsSync(UNPACKED_APP) || !fs.statSync(UNPACKED_APP).isFile()) {
    throw new Error(`The exact unpacked app is missing: ${UNPACKED_APP}. Build it first.`);
  }
  return UNPACKED_APP;
}

function responseFormat(requestBody) {
  const openai = requestBody?.text?.format;
  const anthropic = requestBody?.output_config?.format;
  const gemini = requestBody?.response_format;
  const format = openai || anthropic || gemini;
  if (!format) return null;
  const schema = format.schema;
  const properties = schema && typeof schema === "object" ? schema.properties || {} : {};
  let kind = "unsupported";
  let inferredName = "";
  if (properties.action && properties.criteria_evidence) {
    kind = "long_horizon_action";
    inferredName = "nexus_long_horizon_action_v1";
  } else if (properties.message && properties.goal_complete && properties.remaining) {
    kind = "discussion";
    inferredName = "nexus_board_goal_discussion_v1";
  } else if (properties.contribution && properties.ready_to_execute) {
    kind = "plan_review";
    inferredName = "nexus_board_plan_review_v1";
  } else if (properties.feedback && properties.goal_complete && properties.remaining) {
    kind = "work_verification";
    inferredName = "nexus_board_work_verification_v1";
  }
  return {
    kind,
    name: String(openai?.name || inferredName),
    schema,
    transport: openai ? "openai" : anthropic ? "anthropic" : "gemini",
    wrapper: format,
  };
}

function requestWords(requestBody) {
  return JSON.stringify(requestBody);
}

function reviewPacket(requestBody) {
  return requestWords(requestBody).match(/review-packet:[a-f0-9]{64}/i)?.[0] || "";
}

function providerAction(provider, requestBody) {
  const packet = reviewPacket(requestBody);
  const evidence = packet || "verified-no-change";
  return JSON.stringify({
    action: "complete",
    summary: packet
      ? `${provider} approved the deterministic review packet`
      : `${provider} completed its distinct read-only contribution`,
    evidence: [evidence],
    risk: "low",
    changes: [],
    needs_files: [],
    tasks: [],
    handoff_agent_id: "",
    questions: [],
    criteria_evidence: [
      {criterion: "Original objective is satisfied", evidence_refs: [evidence]},
      {criterion: "Every required task is complete", evidence_refs: [evidence]},
      {criterion: "Configured deterministic verification passes", evidence_refs: [evidence]},
      {criterion: CUSTOM_CRITERION, evidence_refs: [evidence]},
    ],
    ...(packet ? {
      review_verdict: "approve",
      review_findings: [`Inspected and approved ${packet} in the deterministic fixture.`],
    } : {}),
  });
}

function requestPhase(requestBody, format) {
  if (format?.kind === "long_horizon_action") {
    return reviewPacket(requestBody) ? "long_horizon_review" : "long_horizon_task";
  }
  if (format?.kind === "discussion") return "discussion";
  if (format?.kind === "plan_review") return "plan_review";
  if (format?.kind === "work_verification") return "work_verification";
  const words = requestWords(requestBody);
  if (words.includes("FINAL TEAM REPORT")) return "final_report";
  if (words.includes("COLLABORATION ROUND")) return "first_round";
  return "plain";
}

function plainReply(provider, scenario, phase) {
  return `${provider} deterministic ${scenario} ${phase} reply through the real adapter.`;
}

function providerReply(provider, requestBody, record) {
  const format = responseFormat(requestBody);
  if (!format) return plainReply(provider, record.scenario, record.phase);
  if (format.kind === "long_horizon_action") return providerAction(provider, requestBody);
  if (format.kind === "discussion") {
    return JSON.stringify({
      message: `${provider} deterministic ${record.scenario} discussion reply.`,
      goal_complete: true,
      remaining: [],
      progress: [{
        id: `${provider}-assessment`, state: "complete",
        evidence: `${provider} delivered a distinct deterministic assessment`,
      }],
    });
  }
  if (format.kind === "plan_review") {
    return JSON.stringify({
      contribution: `${provider} reviewed the proposed plan.`,
      message_to_lead: "The deterministic plan is ready to execute.",
      needs_files: [], effect_paths: [], ready_to_execute: true, remaining: [],
      questions: [],
      progress: [{id: `${provider}-plan`, state: "ready", evidence: "Plan contract validated"}],
    });
  }
  if (format.kind === "work_verification") {
    return JSON.stringify({
      goal_complete: true,
      feedback: `${provider} verified the deterministic work result.`,
      remaining: [],
    });
  }
  throw new Error(`Unsupported structured fixture schema: ${format.name || "unnamed"}`);
}

function schemaContractProblems(format) {
  if (!format) return [];
  const problems = [];
  const required = Array.isArray(format.schema?.required) ? format.schema.required : [];
  const expected = {
    long_horizon_action: ["action", "summary", "evidence", "criteria_evidence"],
    discussion: ["message", "goal_complete", "remaining"],
    plan_review: ["contribution", "message_to_lead", "ready_to_execute", "remaining"],
    work_verification: ["goal_complete", "feedback", "remaining"],
  }[format.kind] || [];
  if (format.kind === "unsupported") problems.push(`unsupported structured schema ${format.name || "unnamed"}`);
  if (format.schema?.type !== "object") problems.push("structured schema type is not object");
  if (format.schema?.additionalProperties !== false) {
    problems.push("structured schema must reject additional properties");
  }
  for (const field of expected) {
    if (!required.includes(field)) problems.push(`structured schema does not require ${field}`);
  }
  const expectedName = {
    long_horizon_action: "nexus_long_horizon_action_v1",
    discussion: "nexus_board_goal_discussion_v1",
    plan_review: "nexus_board_plan_review_v1",
    work_verification: "nexus_board_work_verification_v1",
  }[format.kind];
  if (format.transport === "openai" && expectedName && format.name !== expectedName) {
    problems.push(`OpenAI schema name was ${format.name || "missing"}, expected ${expectedName}`);
  }
  return problems;
}

function adapterContractProblems(provider, pathname, request, body, format, parseError) {
  const contract = PROVIDER_CONTRACTS[provider];
  if (!contract) return [`unknown provider route ${pathname}`];
  const problems = [];
  if (request.method !== "POST") problems.push(`expected POST, received ${request.method}`);
  if (pathname !== contract.pathname) {
    problems.push(`expected ${contract.pathname}, received ${pathname}`);
  }
  if (parseError) problems.push(`request was not valid JSON: ${parseError.message}`);
  if (!String(request.headers["content-type"] || "").toLowerCase().startsWith("application/json")) {
    problems.push("content-type was not application/json");
  }
  if (request.headers[contract.keyHeader] !== contract.keyValue) {
    problems.push(`${contract.keyHeader} did not contain the fixture credential`);
  }
  if (body.model !== contract.model) problems.push(`expected model ${contract.model}`);
  if (provider === "openai") {
    if (!Array.isArray(body.input)) problems.push("OpenAI Responses input was not an array");
    if (body.stream !== false) problems.push("OpenAI Responses request was not non-streaming");
    if (format && (format.transport !== "openai" || format.wrapper.type !== "json_schema"
        || format.wrapper.strict !== true || format.name === "")) {
      problems.push("OpenAI structured-output wrapper did not match the Responses contract");
    }
  } else if (provider === "anthropic") {
    if (!Array.isArray(body.messages)) problems.push("Anthropic messages was not an array");
    if (body.stream !== false) problems.push("Anthropic request was not non-streaming");
    for (const forbidden of ["temperature", "top_p", "top_k"]) {
      if (Object.hasOwn(body, forbidden)) {
        problems.push(`Anthropic ${body.model} request included forbidden ${forbidden}`);
      }
    }
    if (request.headers["anthropic-version"] !== "2023-06-01") {
      problems.push("Anthropic version header was not pinned");
    }
    if (format && (format.transport !== "anthropic" || format.wrapper.type !== "json_schema")) {
      problems.push("Anthropic structured-output wrapper did not match the Messages contract");
    }
  } else if (provider === "gemini") {
    if (!Array.isArray(body.input)) problems.push("Gemini interactions input was not an array");
    if (format && (format.transport !== "gemini" || format.wrapper.type !== "text"
        || format.wrapper.mime_type !== "application/json")) {
      problems.push("Gemini structured-output wrapper did not match the Interactions contract");
    }
  }
  return [...problems, ...schemaContractProblems(format)];
}

function startProviderFixture() {
  const state = {scenario: "complete", requests: [], contractFailures: []};
  const server = http.createServer((request, response) => {
    const chunks = [];
    request.on("data", (chunk) => chunks.push(chunk));
    request.on("end", () => {
      let body = {};
      let parseError = null;
      try { body = JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}"); }
      catch (error) { parseError = error; }
      const pathname = new URL(request.url, "http://127.0.0.1").pathname;
      const provider = Object.entries(PROVIDER_CONTRACTS)
        .find(([, contract]) => pathname === contract.pathname)?.[0] || "unknown";
      const format = responseFormat(body);
      const record = {
        provider, pathname, scenario: state.scenario,
        sequence: state.requests.length + 1,
        structured: Boolean(format), schemaName: format?.name || "",
        schemaKind: format?.kind || "", reviewPacket: reviewPacket(body),
      };
      record.phase = requestPhase(body, format);
      record.contractProblems = adapterContractProblems(
        provider, pathname, request, body, format, parseError,
      );
      state.requests.push(record);
      if (record.contractProblems.length) state.contractFailures.push(record);
      const knownFailure = state.scenario === "none"
        ? provider === "openai" || provider === "anthropic"
        : state.scenario === "partial" && provider === "anthropic";
      response.setHeader("Content-Type", "application/json");
      if (record.contractProblems.length) {
        response.statusCode = provider === "unknown" ? 404 : 400;
        response.end(JSON.stringify({
          error: {message: `fixture contract rejected request: ${record.contractProblems.join("; ")}`},
        }));
        return;
      }
      if (knownFailure) {
        response.statusCode = 503;
        response.end(JSON.stringify({
          error: {message: `${provider} scripted known failure for ${state.scenario}`},
        }));
        return;
      }
      const text = providerReply(provider, body, record);
      response.statusCode = 200;
      if (provider === "openai") {
        response.end(JSON.stringify({
          id: `resp-${record.sequence}`,
          status: "completed",
          output: [{
            type: "message", role: "assistant",
            content: [{type: "output_text", text}],
          }],
          usage: {input_tokens: 10, output_tokens: 10},
        }));
      } else if (provider === "anthropic") {
        response.end(JSON.stringify({
          id: `msg-${record.sequence}`, type: "message", role: "assistant",
          content: [{type: "text", text}], stop_reason: "end_turn",
          usage: {input_tokens: 10, output_tokens: 10},
        }));
      } else {
        response.end(JSON.stringify({
          id: `interaction-${record.sequence}`, status: "completed",
          steps: [{type: "model_output", content: [{type: "text", text}]}],
          usage: {total_input_tokens: 10, total_output_tokens: 10},
        }));
      }
    });
  });
  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      resolve({server, state, origin: `http://127.0.0.1:${address.port}`});
    });
  });
}

function packagedPaths(exe) {
  const root = path.dirname(exe);
  return {
    python: path.join(root, "resources", "runtime", "python.exe"),
    harnessSource: path.join(root, "resources", "harness", "src"),
  };
}

function prepareProject({exe, project, origin, env}) {
  const harness = path.join(project, ".harness");
  fs.mkdirSync(harness, {recursive: true});
  fs.writeFileSync(path.join(harness, "config.local.json"), `${JSON.stringify({
    providers: {
      "fixture-openai": {
        kind: "openai", model: "gpt-fixture", endpoint: `${origin}/openai`,
        api_key_env: "NEXUS_E2E_OPENAI_KEY", api_mode: "responses",
        timeout_seconds: 15, max_output_tokens: 4096,
      },
      "fixture-anthropic": {
        kind: "anthropic", model: "claude-sonnet-5", endpoint: `${origin}/anthropic`,
        api_key_env: "NEXUS_E2E_ANTHROPIC_KEY",
        timeout_seconds: 15, max_output_tokens: 4096,
      },
      "fixture-gemini": {
        kind: "gemini", model: "gemini-fixture", endpoint: `${origin}/gemini`,
        api_key_env: "NEXUS_E2E_GEMINI_KEY",
        timeout_seconds: 15, max_output_tokens: 4096,
      },
    },
  }, null, 2)}\n`, "utf8");
  fs.writeFileSync(path.join(project, "README.md"), "# Packaged multi-vendor acceptance fixture\n", "utf8");
  const packaged = packagedPaths(exe);
  for (const target of [packaged.python, packaged.harnessSource]) {
    if (!fs.existsSync(target)) throw new Error(`The packaged runtime is missing ${target}`);
  }
  const code = [
    "import sys",
    "from pathlib import Path",
    `sys.path.insert(0, ${JSON.stringify(packaged.harnessSource)})`,
    "from our_harness.config import trust_project_local_config",
    "from our_harness.pipeline_runs import project_identity",
    "root = Path(sys.argv[1]).resolve()",
    "trust_project_local_config(root)",
    "project_identity(root)",
  ].join("; ");
  const result = childProcess.spawnSync(packaged.python, ["-c", code, project], {
    cwd: project,
    env,
    encoding: "utf8",
    timeout: 60_000,
  });
  if (result.status !== 0) {
    throw new Error(`Could not prepare the isolated packaged fixture: ${result.stderr || result.stdout}`);
  }
}

function filteredEnvironment(overrides) {
  const safeNames = [
    "SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "TEMP", "TMP", "COMSPEC",
    "SYSTEMDRIVE", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
    "PROCESSOR_IDENTIFIER", "OS", "LANG", "LC_ALL", "CI", "GITHUB_ACTIONS",
  ];
  const source = new Map(Object.entries(process.env).map(([key, value]) => [key.toUpperCase(), value]));
  const filtered = {};
  for (const name of safeNames) {
    if (source.get(name) != null) filtered[name] = source.get(name);
  }
  return {...filtered, ...overrides};
}

async function waitUntil(description, probe, timeout = TIMEOUT_MS) {
  const deadline = Date.now() + timeout;
  let last = "";
  while (Date.now() < deadline) {
    try {
      const value = await probe();
      if (value === true) return;
      last = value == null ? last : String(value);
    } catch (error) {
      last = String(error?.message || error);
    }
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
  throw new Error(`${description} did not happen before timeout${last ? `; last observed: ${last}` : ""}`);
}

async function reachPanel(page) {
  await waitUntil("the packaged panel to open", async () => {
    if (page.url().startsWith("http://127.0.0.1:") || page.url().startsWith("http://localhost:")) {
      return true;
    }
    const repair = page.locator("#repair");
    if (await repair.count() && await repair.isVisible().catch(() => false)) {
      await repair.click();
    }
    return page.url();
  }, 90_000);
}

function agentCard(page, name) {
  return page.locator("#swarmCanvas .swarm-box.agent").filter({hasText: name});
}

async function addAgent(page, name, route) {
  await page.locator("#swarmAddAgent").click();
  await page.locator("#askDialog[open]").waitFor({state: "visible"});
  await page.locator("#askDialogInput").fill(name);
  await page.locator("#askDialogOk").click();
  const card = agentCard(page, name);
  await card.waitFor({state: "visible"});
  await page.locator(`#swarmAgentWho option[value="${route}"]`).waitFor({state: "attached"});
  await page.locator("#swarmAgentWho").selectOption(route);
  await waitUntil(`${name} to use ${route}`, async () => (
    (await card.textContent()).includes(route)
      && await page.locator('#swarmAgentSaveState[data-state="saved"]').isVisible()
      ? true : await page.locator("#swarmAgentSaveState").textContent()
  ));
}

async function addProject(page, project) {
  await page.locator("#swarmAddProject").click();
  await page.locator("#askDialog[open]").waitFor({state: "visible"});
  await page.locator("#askDialogInput").fill(project);
  await page.locator("#askDialogOk").click();
  const card = page.locator("#swarmCanvas .swarm-box.project").filter({hasText: project});
  await card.waitFor({state: "visible"});
  return card;
}

async function openGoalComposer(page) {
  await page.locator("#swarmWorkGoals").click();
  await page.locator("#longGoalDialog[open]").waitFor({state: "visible"});
}

async function exerciseGoalComposerControls(page) {
  await openGoalComposer(page);
  await page.locator("#longGoalProject").selectOption({index: 0});
  await page.locator("#longGoalParticipation").selectOption("adaptive");
  await page.locator("#longGoalParticipation").selectOption("every");
  await page.locator("#longGoalLead").selectOption({index: 0});
  await page.locator("#longGoalClose").click();
  await page.locator("#longGoalDialog").waitFor({state: "hidden"});

  await openGoalComposer(page);
  await page.locator("#longGoalCancel").click();
  await page.locator("#longGoalDialog").waitFor({state: "hidden"});

  await openGoalComposer(page);
  await page.locator("#longGoalCheckBoard").click();
  await page.locator("#longGoalDialog").waitFor({state: "hidden"});
  await waitUntil("connection checking to return to the hydrated board", async () => {
    const said = await page.locator("#swarmSaid").textContent();
    return said && !said.includes("Loading") ? true : said;
  });

  await openGoalComposer(page);
  await page.locator("#longGoalEditBoard").click();
  await page.locator("#longGoalDialog").waitFor({state: "hidden"});
  await page.locator("#swarmProjectPanel:not([hidden])").waitFor({state: "visible"});
  await page.locator("#swarmTaskText").waitFor({state: "visible"});
}

function assertAdapterContracts(records, description) {
  const failures = records.filter((record) => record.contractProblems.length);
  assert.equal(
    failures.length,
    0,
    `${description} violated a production adapter contract:\n${JSON.stringify(failures, null, 2)}`,
  );
}

async function startThreeVendorGoal(page, fixtureState) {
  const firstRequest = fixtureState.requests.length;
  await openGoalComposer(page);
  await page.locator("#longGoalText").fill(
    "Read-only: have every selected provider make a distinct assessment of this fixture. Do not change any files.",
  );
  await page.locator("#longGoalCriteria").fill(CUSTOM_CRITERION);
  const ticks = page.locator('#longGoalAgents input[type="checkbox"]:not(:disabled)');
  const count = await ticks.count();
  assert.equal(count, 3, "the goal composer should expose all three ready provider routes");
  for (let index = 0; index < count; index += 1) await ticks.nth(index).check();
  await page.locator("#longGoalParticipation").selectOption("every");
  await page.locator("#longGoalStart").click();
  await page.locator("#longGoalDialog").waitFor({state: "hidden"});
  await waitUntil("all three durable goal tasks to complete", async () => {
    const progress = await page.locator("#missionProgress").textContent();
    const allTasks = await page.locator("#missionTasks .mission-task-card").count();
    const completeColumns = await page.locator("#missionTasks .mission-task-column")
      .filter({hasText: "complete (3)"}).count();
    const completeTasks = await page.locator("#missionTasks .mission-task-column")
      .filter({hasText: "complete (3)"}).locator(".mission-task-card").count();
    return /complete/i.test(progress || "") && allTasks === 3
      && completeTasks === 3 && completeColumns === 1
      ? true
      : `${progress} / ${allTasks} total task cards / ${completeTasks} complete tasks`;
  });
  assert.equal(await page.locator("#missionTasks .mission-task-card").count(), 3);
  assert.deepEqual(
    await page.locator("#missionTasks .mission-task-column h3").allTextContents(),
    ["complete (3)"],
  );
  assert.equal(await page.locator("#missionAgents .mission-agent").count(), 3);
  const requests = fixtureState.requests.slice(firstRequest);
  assertAdapterContracts(requests, "the three-provider mission");
  const actionRequests = requests.filter((one) => one.schemaKind === "long_horizon_action");
  assert.equal(actionRequests.length, 3, "the mission must dispatch exactly one task per provider");
  assert.deepEqual(
    [...actionRequests.map((one) => one.provider)].sort(),
    ["anthropic", "gemini", "openai"],
    "the mission must physically dispatch through all three configured adapters",
  );
  return requests;
}

async function connectPair(page, one, other) {
  const card = agentCard(page, one);
  await card.getByRole("button", {name: `settings for ${one}`, exact: true}).click();
  const peer = page.locator("#swarmTalksTo label").filter({hasText: other}).locator("input");
  await peer.check();
  await waitUntil(`${one} and ${other} to remain connected`, async () => (
    await peer.isChecked() ? true : "connection checkbox is not checked"
  ));
}

async function assertOneDirectChatGroup(page) {
  const directNew = page.getByRole(
    "button", {name: "+ New chat for this agent", exact: true},
  );
  await waitUntil("one permanent lone-agent chat group to remain visible", async () => {
    const count = await directNew.count();
    return count === 1 ? true : `found ${count} lone-agent New controls`;
  });
  return directNew;
}

async function createDirectChat(page) {
  const directNew = await assertOneDirectChatGroup(page);
  await waitUntil("the lone-agent New control to become available", async () => (
    await directNew.isEnabled() ? true : "the lone-agent New control is disabled"
  ));
  const activePick = page.locator(
    "#theBigChatConversationList .the-big-chat-conversation-pick.active",
  );
  const previousChatId = String(await activePick.getAttribute("data-chat-id") || "");
  await directNew.click();
  await waitUntil("the new saved lone-agent chat identity to become active", async () => {
    const currentChatId = String(await activePick.getAttribute("data-chat-id") || "");
    const status = String(await page.locator("#theBigChatConversationSaid").textContent() || "");
    return currentChatId && currentChatId !== previousChatId
      && status.includes("New direct chat created")
      ? true
      : `old=${previousChatId || "none"} current=${currentChatId || "none"} status=${status}`;
  });
  await assertOneDirectChatGroup(page);
}

async function openPairChat(page, one, other, create = true) {
  if (!await page.locator("#theBigChat").isVisible()) {
    const card = agentCard(page, one);
    await card.getByRole("button", {name: `chat with ${one}`, exact: true}).click();
    await page.locator(".swarm-chat-card").filter({hasText: `Nexus chat with ${one}`})
      .waitFor({state: "visible"});
    await page.getByRole("button", {name: "Open full Nexus chat", exact: true}).click();
    await page.locator("#theBigChat:not([hidden])").waitFor({state: "visible"});
  }
  const pair = page.locator("#theBigChatConversationList .the-big-chat-pair")
    .filter({hasText: one}).filter({hasText: other});
  await pair.waitFor({state: "visible"});
  const activePick = page.locator(
    "#theBigChatConversationList .the-big-chat-conversation-pick.active",
  );
  if (create) {
    const previousChatId = String(await activePick.getAttribute("data-chat-id") || "");
    await pair.getByRole("button", {name: "+ New chat for this pair", exact: true}).click();
    await waitUntil("the new saved pair chat identity to become active", async () => {
      const currentChatId = String(await activePick.getAttribute("data-chat-id") || "");
      const status = String(await page.locator("#theBigChatConversationSaid").textContent() || "");
      return currentChatId && currentChatId !== previousChatId
        && status.includes("New pair chat created")
        ? true
        : `old=${previousChatId || "none"} current=${currentChatId || "none"} status=${status}`;
    });
  }
  await waitUntil("the exact pair chat to become active", async () => {
    const title = await page.locator("#theBigChatTitle").textContent();
    const button = await page.locator("#theBigChatCollaborate").textContent();
    const chatId = String(await activePick.getAttribute("data-chat-id") || "");
    const composerEnabled = await page.locator("#theBigChatBox").isEnabled();
    return title?.includes(one) && title?.includes(other) && button?.includes("both")
      && chatId && composerEnabled
      ? true : `${title} / ${button} / chat=${chatId || "none"} / composer=${composerEnabled}`;
  });
}

async function runPairScenario(
  page, fixtureState, scenario, expectedOutcome, summary, visibleProviders,
) {
  const prompt = `Scenario ${scenario}: each connected provider must answer this exact user request.`;
  const firstRequest = fixtureState.requests.length;
  fixtureState.scenario = scenario;
  await page.locator("#theBigChatBox").fill(prompt);
  await page.locator("#theBigChatCollaborate").click();
  const card = page.locator(
    `#theBigChatSaid .participant-outcome-card[data-outcome="${expectedOutcome}"]`,
  ).last();
  await card.waitFor({state: "visible", timeout: TIMEOUT_MS});
  await waitUntil(`the ${scenario} outcome summary`, async () => {
    const text = await card.textContent();
    return text?.includes(summary) ? true : text;
  });
  await waitUntil(`${scenario} to dispatch once to every selected provider`, () => {
    const firstRoundProviders = new Set(
      fixtureState.requests.slice(firstRequest)
        .filter((one) => one.scenario === scenario && one.phase === "first_round")
        .map((one) => one.provider),
    );
    return PAIR_PROVIDERS.every((provider) => firstRoundProviders.has(provider))
      ? true : [...firstRoundProviders].join(", ");
  });
  const requests = fixtureState.requests.slice(firstRequest);
  assertAdapterContracts(requests, `${scenario} pair chat`);
  for (const provider of PAIR_PROVIDERS) {
    assert.ok(
      requests.some((one) => one.provider === provider && one.phase === "first_round"),
      `${scenario} never dispatched the original request through ${provider}`,
    );
  }
  const transcript = await page.locator("#theBigChatSaid").innerText();
  for (const provider of PAIR_PROVIDERS) {
    const marker = plainReply(provider, scenario, "first_round");
    if (visibleProviders.includes(provider)) {
      assert.match(transcript, new RegExp(marker.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
    } else {
      assert.doesNotMatch(transcript, new RegExp(marker.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
    }
  }
  if (scenario === "complete") {
    for (const provider of PAIR_PROVIDERS) {
      assert.ok(
        requests.some((one) => one.provider === provider && one.schemaKind === "discussion"),
        `the complete chat never received ${provider}'s structured discussion turn`,
      );
      assert.match(transcript, new RegExp(`${provider} deterministic complete discussion reply\\.`));
    }
    assert.ok(
      requests.some((one) => one.provider === "openai" && one.phase === "final_report"),
      "the complete chat never dispatched the visible final report through its lead adapter",
    );
    assert.match(transcript, /openai deterministic complete final_report reply through the real adapter\./);
  }
  return {card, prompt, requests, transcript};
}

async function exercisePartialRecovery(page, fixtureState, partial) {
  const beforeRestore = fixtureState.requests.length;
  const askAgain = partial.card.getByRole("button", {name: "Ask all agents again", exact: true});
  await askAgain.waitFor({state: "visible"});
  await askAgain.click();
  assert.equal(await page.locator("#theBigChatBox").inputValue(), partial.prompt);
  assert.match(
    await partial.card.locator(".participant-outcome-action-note").textContent(),
    /Prompt restored[\s\S]*Nothing was sent\./,
  );
  await new Promise((resolve) => setTimeout(resolve, 750));
  assert.equal(
    fixtureState.requests.length,
    beforeRestore,
    "Ask all agents again must restore the draft without dispatching it",
  );
  await page.locator("#theBigChatBox").fill("");

  const beforeRepair = fixtureState.requests.length;
  const repair = partial.card.getByRole(
    "button", {name: "Repair Fixture Anthropic", exact: true},
  );
  await repair.waitFor({state: "visible"});
  await repair.click();
  await page.locator("#swarmAgentPanel:not([hidden])").waitFor({state: "visible"});
  await waitUntil("the failed participant's exact provider-neutral repair panel", async () => {
    const name = await page.locator("#swarmAgentName").inputValue();
    const route = await page.locator("#swarmAgentWho").inputValue();
    const badge = await page.locator("#swarmAgentRepairBadge").textContent();
    return name === "Fixture Anthropic" && route === "fixture-anthropic" && badge !== "Checking"
      ? true : `${name} / ${route} / ${badge}`;
  });
  assert.match(await page.locator("#swarmAgentRouteIdentity").textContent(), /fixture-anthropic/);
  assert.equal(
    fixtureState.requests.length,
    beforeRepair,
    "Repair must diagnose the exact route without sending another model request",
  );
}

function isLoopback(url) {
  try {
    return ["127.0.0.1", "localhost", "::1"].includes(new URL(url).hostname);
  } catch (_) {
    return false;
  }
}

function attachPageFailureCollection(page, monitor) {
  if (monitor.pages.has(page)) return;
  monitor.pages.add(page);
  page.on("pageerror", (error) => monitor.failures.push(`renderer exception: ${error.message}`));
  page.on("crash", () => monitor.failures.push(`renderer crashed: ${page.url()}`));
  page.on("console", (message) => {
    if (message.type() === "error") {
      monitor.failures.push(`console error at ${page.url()}: ${message.text()}`);
    }
  });
  page.on("response", (response) => {
    const url = response.url();
    if (response.status() >= 500 && isLoopback(url)) {
      monitor.failures.push(`local UI response ${response.status()}: ${url}`);
    }
  });
  page.on("requestfailed", (request) => {
    const failure = request.failure()?.errorText || "unknown request failure";
    if (isLoopback(request.url()) && !failure.includes("ERR_ABORTED")) {
      monitor.failures.push(`local UI request failed: ${request.url()} (${failure})`);
    }
  });
}

function attachBrowserFailureCollection(app, monitor) {
  for (const page of app.windows()) attachPageFailureCollection(page, monitor);
  app.on("window", (page) => attachPageFailureCollection(page, monitor));
}

async function assertNoBrowserFailures(page, monitor) {
  await page.waitForTimeout(500);
  assert.equal(monitor.failures.length, 0, monitor.failures.join("\n"));
}

async function launchPackaged(exe, args, env, monitor) {
  const app = await electron.launch({executablePath: exe, args, env, timeout: TIMEOUT_MS});
  attachBrowserFailureCollection(app, monitor);
  const page = await app.firstWindow({timeout: TIMEOUT_MS});
  attachPageFailureCollection(page, monitor);
  await reachPanel(page);
  return {app, page};
}

async function main() {
  const exe = builtApp();
  const fixture = await startProviderFixture();
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "nexus-multivendor-e2e-"));
  const project = path.join(root, "arbitrary-user-project");
  const appData = path.join(root, "roaming-state");
  const localAppData = path.join(root, "local-state");
  const profile = path.join(root, "electron-profile");
  const userHome = path.join(root, "user-home");
  const temporary = path.join(root, "temporary");
  for (const folder of [project, appData, localAppData, profile, userHome, temporary]) {
    fs.mkdirSync(folder, {recursive: true});
  }
  const homeDrive = path.parse(userHome).root.slice(0, 2);
  const env = filteredEnvironment({
    APPDATA: appData,
    LOCALAPPDATA: localAppData,
    USERPROFILE: userHome,
    HOME: userHome,
    HOMEDRIVE: homeDrive,
    HOMEPATH: userHome.slice(homeDrive.length),
    TEMP: temporary,
    TMP: temporary,
    NEXUS_E2E_OPENAI_KEY: "fixture-openai-key",
    NEXUS_E2E_ANTHROPIC_KEY: "fixture-anthropic-key",
    NEXUS_E2E_GEMINI_KEY: "fixture-gemini-key",
  });
  const args = [`--user-data-dir=${profile}`, "--project", project];
  let app = null;
  let page = null;
  let passed = false;
  const browserMonitor = {failures: [], pages: new WeakSet()};
  try {
    prepareProject({exe, project, origin: fixture.origin, env});
    ({app, page} = await launchPackaged(exe, args, env, browserMonitor));
    await page.locator('[data-view="swarm"]').click();
    await page.locator("#swarmView").waitFor({state: "visible"});
    await waitUntil("the durable board to finish loading", async () => {
      const said = await page.locator("#swarmSaid").textContent();
      return said && !said.includes("Loading") ? true : said;
    });
    console.log("pass  packaged Electron loaded the real saved-board surface");

    await addAgent(page, "Fixture OpenAI", "fixture-openai");
    await addAgent(page, "Fixture Anthropic", "fixture-anthropic");
    await addAgent(page, "Fixture Gemini", "fixture-gemini");
    await addProject(page, project);
    console.log("pass  a user added and configured three heterogeneous provider agents through the UI");

    await exerciseGoalComposerControls(page);
    console.log("pass  a user can close, cancel, inspect connections, and return to project editing");

    fixture.state.scenario = "complete";
    await startThreeVendorGoal(page, fixture.state);
    console.log("pass  the goal composer required and visibly completed all three provider contributions");

    await connectPair(page, "Fixture OpenAI", "Fixture Anthropic");
    await openPairChat(page, "Fixture OpenAI", "Fixture Anthropic", false);
    await createDirectChat(page);
    console.log("pass  a connected agent keeps one permanent lone chat and can create another direct chat");
    await openPairChat(page, "Fixture OpenAI", "Fixture Anthropic", true);
    const complete = await runPairScenario(
      page, fixture.state, "complete", "complete", "2 of 2 agents answered", PAIR_PROVIDERS,
    );
    assert.equal(await complete.card.locator('[data-status="answered"]').count(), 2);
    console.log("pass  complete chat dispatched both adapters and painted both real replies plus 2 of 2");

    await openPairChat(page, "Fixture OpenAI", "Fixture Anthropic", true);
    const partial = await runPairScenario(
      page, fixture.state, "partial", "partial", "1 of 2 agents answered", ["openai"],
    );
    assert.equal(await partial.card.locator('[data-status="answered"]').count(), 1);
    assert.equal(await partial.card.locator('[data-status="failed"]').count(), 1);
    await exercisePartialRecovery(page, fixture.state, partial);
    console.log("pass  partial chat preserves one reply and offers exact no-send recovery controls");

    await assertNoBrowserFailures(page, browserMonitor);
    await app.close();
    app = null;
    assert.equal(browserMonitor.failures.length, 0, browserMonitor.failures.join("\n"));
    ({app, page} = await launchPackaged(exe, args, env, browserMonitor));
    await page.locator('[data-view="swarm"]').click();
    await page.locator("#swarmView").waitFor({state: "visible"});
    await waitUntil("the restarted board to hydrate", async () => {
      const said = await page.locator("#swarmSaid").textContent();
      return said && !said.includes("Loading") ? true : said;
    });
    await openPairChat(page, "Fixture OpenAI", "Fixture Anthropic", false);
    await assertOneDirectChatGroup(page);
    const persisted = page.locator(
      '#theBigChatSaid .participant-outcome-card[data-outcome="partial"]',
    );
    await persisted.last().waitFor({state: "visible"});
    assert.match(await persisted.last().textContent(), /1 of 2 agents answered/);
    assert.match(
      await page.locator("#theBigChatSaid").innerText(),
      /openai deterministic partial first_round reply through the real adapter\./,
    );
    await persisted.last().getByRole(
      "button", {name: "Repair Fixture Anthropic", exact: true},
    ).waitFor({state: "visible"});
    await persisted.last().getByRole(
      "button", {name: "Ask all agents again", exact: true},
    ).waitFor({state: "visible"});
    console.log("pass  the partial reply, outcome, and exact recovery actions survive a full restart");

    await openPairChat(page, "Fixture OpenAI", "Fixture Anthropic", true);
    const none = await runPairScenario(
      page, fixture.state, "none", "none", "0 of 2 agents answered", [],
    );
    assert.equal(await none.card.locator('[data-status="failed"]').count(), 2);
    assert.match(await none.card.textContent(), /No AI answer was saved/i);
    console.log("pass  zero-response chat dispatched both adapters and painted no invented AI reply");

    assert.equal(
      fixture.state.contractFailures.length,
      0,
      JSON.stringify(fixture.state.contractFailures, null, 2),
    );
    await assertNoBrowserFailures(page, browserMonitor);
    await app.close();
    app = null;
    assert.equal(browserMonitor.failures.length, 0, browserMonitor.failures.join("\n"));
    passed = true;
    console.log("\nPackaged multi-vendor UI acceptance passed without renderer injection.");
  } catch (error) {
    if (page) {
      await page.screenshot({path: path.join(root, "failure.png"), fullPage: true}).catch(() => {});
      const html = await page.content().catch(() => "");
      if (html) fs.writeFileSync(path.join(root, "failure.html"), html, "utf8");
    }
    throw new Error(
      `${error?.stack || error}\nBrowser faults:\n${browserMonitor.failures.join("\n") || "(none)"}`
      + `\nFailure artifacts: ${root}`,
    );
  } finally {
    if (app) await app.close().catch(() => {});
    await new Promise((resolve) => fixture.server.close(resolve));
    if (passed) fs.rmSync(root, {
      recursive: true,
      force: true,
      // Windows can briefly retain a cwd/file handle after Electron and its
      // server have exited. Keep a successful release acceptance from turning
      // red solely because that temporary directory needs another moment.
      maxRetries: 12,
      retryDelay: 250,
    });
  }
}

main().catch((error) => {
  console.error(`\n${error?.message || error}`);
  process.exit(1);
});
