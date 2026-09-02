"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const source = fs.readFileSync(path.join(__dirname, "multi-vendor.e2e.js"), "utf8");

test("packaged acceptance drives the exact Electron artifact as a black box", () => {
  assert.match(source, /require\("playwright-core"\)/);
  assert.match(source, /path\.join\(OUTPUT, "win-unpacked", "Nexus Harness\.exe"\)/);
  assert.doesNotMatch(source, /readdirSync\(OUTPUT\)/);
  assert.doesNotMatch(source, /page\.evaluate\s*\(/);
  assert.doesNotMatch(source, /locator\.evaluate\s*\(/);
  assert.doesNotMatch(source, /addInitScript\s*\(/);
  assert.doesNotMatch(source, /exposeFunction\s*\(/);
  assert.doesNotMatch(source, /harness:webChatsChanged/);
  assert.doesNotMatch(source, /window\.request\s*=/);
  assert.doesNotMatch(source, /routeFromHAR|page\.route\s*\(/);
  assert.doesNotMatch(source, /["'`]\/api\//);

  const launch = source.slice(
    source.indexOf("async function launchPackaged"),
    source.indexOf("async function main"),
  );
  assert.ok(launch.indexOf("attachBrowserFailureCollection") < launch.indexOf("firstWindow"));
  assert.ok(launch.indexOf("attachPageFailureCollection") < launch.indexOf("reachPanel"));
  for (const event of ["pageerror", "crash", "console", "response", "requestfailed"]) {
    assert.match(source, new RegExp(`page\\.on\\(\"${event}\"`));
  }

  assert.match(source, /function filteredEnvironment\(/);
  assert.doesNotMatch(source, /env:\s*\{\s*\.\.\.process\.env/);
  for (const isolated of ["APPDATA", "LOCALAPPDATA", "USERPROFILE", "HOME", "TEMP", "TMP"]) {
    assert.match(source, new RegExp(`${isolated}:`), `${isolated} is not isolated`);
  }
});

test("fixture validates each production adapter and replies to the requested schema", () => {
  for (const route of ["/openai/responses", "/anthropic/messages", "/gemini/interactions"]) {
    assert.ok(source.includes(`pathname: "${route}"`), `${route} is not exact`);
  }
  for (const header of ["authorization", "x-api-key", "anthropic-version", "x-goog-api-key"]) {
    assert.match(source, new RegExp(header));
  }
  assert.match(source, /adapterContractProblems/);
  assert.match(source, /schemaContractProblems/);
  assert.match(source, /contractFailures/);
  assert.match(source, /OpenAI Responses input was not an array/);
  assert.match(source, /Anthropic messages was not an array/);
  assert.match(source, /Gemini interactions input was not an array/);
  assert.match(source, /type: "output_text"/);
  assert.match(source, /stop_reason: "end_turn"/);
  assert.match(source, /type: "model_output"/);

  for (const schema of [
    "nexus_long_horizon_action_v1",
    "nexus_board_goal_discussion_v1",
    "nexus_board_plan_review_v1",
    "nexus_board_work_verification_v1",
  ]) {
    assert.match(source, new RegExp(schema));
  }
  assert.match(source, /review-packet:\[a-f0-9\]\{64\}/);
  assert.match(source, /review_verdict: "approve"/);
  assert.match(source, /review_findings:/);
  assert.match(source, /message: `\$\{provider\} deterministic/);

  assert.match(source, /allTasks === 3/);
  assert.match(source, /completeTasks === 3/);
  assert.match(source, /actionRequests\.length, 3/);
  assert.match(source, /\["anthropic", "gemini", "openai"\]/);
});

test("every chat scenario proves dispatch, visible truth, recovery, and restart durability", () => {
  for (const control of [
    "longGoalClose", "longGoalCancel", "longGoalCheckBoard", "longGoalEditBoard",
    "longGoalStart", "longGoalProject", "longGoalLead", "longGoalParticipation",
  ]) {
    assert.match(source, new RegExp(`#${control}\\b`), `${control} is not exercised`);
  }
  assert.match(source, /participant-outcome-card\[data-outcome/);
  assert.match(source, /phase === "first_round"/);
  assert.match(source, /plainReply\(provider, scenario, "first_round"\)/);
  assert.match(source, /#theBigChatSaid"\)\.innerText/);
  assert.match(source, /"complete", "complete", "2 of 2 agents answered", PAIR_PROVIDERS/);
  assert.match(source, /"partial", "partial", "1 of 2 agents answered", \["openai"\]/);
  assert.match(source, /"none", "none", "0 of 2 agents answered", \[\]/);
  const pairOpening = source.slice(
    source.indexOf("async function openPairChat"),
    source.indexOf("async function runPairScenario"),
  );
  assert.match(pairOpening, /previousChatId/);
  assert.match(pairOpening, /currentChatId !== previousChatId/);
  assert.match(pairOpening, /New pair chat created/);
  assert.match(pairOpening, /#theBigChatBox"\)\.isEnabled\(\)/);

  const directOpening = source.slice(
    source.indexOf("async function assertOneDirectChatGroup"),
    source.indexOf("async function openPairChat"),
  );
  assert.match(directOpening, /\+ New chat for this agent/);
  assert.match(directOpening, /count === 1/);
  assert.match(directOpening, /currentChatId !== previousChatId/);
  assert.match(directOpening, /New direct chat created/);
  assert.match(source, /await createDirectChat\(page\)/);
  assert.ok(
    source.lastIndexOf("await assertOneDirectChatGroup(page)")
      > source.indexOf("the restarted board to hydrate"),
    "the permanent lone-agent group is not rechecked after restart",
  );

  const recovery = source.slice(
    source.indexOf("async function exercisePartialRecovery"),
    source.indexOf("function isLoopback"),
  );
  assert.match(recovery, /Ask all agents again/);
  assert.match(recovery, /Repair Fixture Anthropic/);
  assert.match(recovery, /#theBigChatBox"\)\.inputValue\(\)/);
  assert.match(recovery, /Nothing was sent/);
  assert.match(recovery, /fixtureState\.requests\.length/);
  assert.match(recovery, /#swarmAgentWho/);
  assert.doesNotMatch(recovery, /#theBigChatCollaborate"\)\.click/);

  assert.match(source, /await app\.close\(\)/);
  assert.match(source, /survive a full restart/);
  assert.match(source, /Repair Fixture Anthropic[\s\S]*Ask all agents again/);
  assert.match(source, /async function removeAcceptedFixture\(root, timeoutMs = 120_000\)/);
  assert.match(source, /\["EBUSY", "EPERM", "ENOTEMPTY"\]/);
  assert.match(source, /Date\.now\(\) - started >= timeoutMs/,
    "external Windows handles get a bounded window, never an endless cleanup mask");
  assert.match(source, /if \(passed\) await removeAcceptedFixture\(root\)/);
});

test("unpacked and installed acceptance lanes are release gates", () => {
  const manifest = require("./package.json");
  assert.equal(manifest.dependencies["playwright-core"], "1.62.1");
  assert.equal(manifest.scripts["e2e:multi-vendor"], "node multi-vendor.e2e.js");
  assert.ok(manifest.build.files.includes("!multi-vendor.e2e.js"));

  const checks = fs.readFileSync(
    path.join(__dirname, "..", ".github", "workflows", "checks.yml"), "utf8",
  );
  const release = fs.readFileSync(
    path.join(__dirname, "..", ".github", "workflows", "windows-release.yml"), "utf8",
  );
  assert.match(checks, /npm run build -- --win dir/);
  assert.match(checks, /npm run e2e:multi-vendor/);
  assert.ok(
    release.indexOf("npm run e2e:multi-vendor") > release.indexOf("npm run build -- --win nsis"),
  );
  assert.match(release, /npm run e2e:multi-vendor -- "\$installed"/);
  assert.ok((release.match(/npm run e2e:multi-vendor/g) || []).length >= 2);
});
