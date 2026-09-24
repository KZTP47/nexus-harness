const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const assert = require("node:assert/strict");
const test = require("node:test");

const source = fs.readFileSync(path.join(__dirname, "../src/our_harness/ui/app.js"), "utf8");
const start = source.indexOf("// One short, plain line per live provider event");
const end = source.indexOf("function showLiveActivityFeed");
const context = vm.createContext({JSON});
vm.runInContext(source.slice(start, end), context);
const line = (row) => vm.runInContext("liveActivityLine", context)(row);

test("live provider rows become short plain lines of what the agent is doing", () => {
  assert.deepEqual({...line({kind: "reasoning_summary", text: "Check the API first."})},
    {icon: "💭", text: "Thinking: Check the API first."});
  assert.equal(line({kind: "message", text: "I will add a route."}).text, "I will add a route.");
  assert.equal(line({kind: "tool", status: "requested", arguments: JSON.stringify({command: "npm test"})}).text, "Running npm test");
  assert.equal(line({kind: "tool", status: "failed", arguments: JSON.stringify({command: "npm test"}),
    result: JSON.stringify({exit_code: 2})}).text, "Failed npm test (exit 2)");
  assert.equal(line({kind: "tool", status: "finished", arguments: JSON.stringify({changes: [
    {path: "C:\\work\\unicorn.html"}, {path: "src/app.js"}]})}).text, "Edited unicorn.html, app.js");
  assert.equal(line({kind: "tool", status: "finished", name: "web_search", arguments: JSON.stringify({query: "three.js"})}).text,
    "Searching the web: three.js");
  assert.equal(line({kind: "tool", status: "requested", name: "Read", arguments: JSON.stringify({file_path: "a.py"})}).text,
    "Using Read: a.py");
  assert.equal(line({kind: "unknown"}), null);
  assert.ok(line({kind: "message", text: "x".repeat(900)}).text.length <= 300);
});

test("malformed arguments never break the feed", () => {
  assert.equal(line({kind: "tool", status: "finished", name: "custom_tool", arguments: "{not json"}).text, "Used custom tool");
});
