from __future__ import annotations

import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "Node.js is required for the desktop bridge behavior tests")
class DesktopLargeJsonBridgeTests(unittest.TestCase):
    def run_node(self, script: str, *arguments: Path) -> str:
        completed = subprocess.run(
            [str(NODE), "-e", textwrap.dedent(script), *(str(one) for one in arguments)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        return completed.stdout.strip()

    def test_preload_chunks_below_cap_without_splitting_a_surrogate_pair(self) -> None:
        output = self.run_node(
            r"""
            const Module = require("node:module");
            const originalLoad = Module._load;
            let bridge = null;
            const calls = [];
            const ipcRenderer = {
              invoke: async (channel, ...args) => {
                calls.push({channel, args});
                if (channel === "harness:beginLargeJsonFile") {
                  return {saved: true, identity: "export-1"};
                }
                if (channel === "harness:appendLargeJsonFile") {
                  return {sequence: Number(args[1]) + 1};
                }
                if (channel === "harness:finishLargeJsonFile") {
                  return {saved: true, filename: "portable.json"};
                }
                throw new Error(`unexpected IPC ${channel}`);
              },
              on: () => {},
            };
            Module._load = function(request, parent, isMain) {
              if (request === "electron") {
                return {
                  contextBridge: {exposeInMainWorld: (_name, value) => { bridge = value; }},
                  ipcRenderer,
                };
              }
              return originalLoad.call(this, request, parent, isMain);
            };
            require(process.argv[1]);
            Module._load = originalLoad;

            (async () => {
              const written = "a".repeat(999999) + "😀" + "z";
              const result = await bridge.saveLargeJsonFile("portable.json", written);
              const appends = calls.filter((one) => one.channel === "harness:appendLargeJsonFile");
              const chunks = appends.map((one) => one.args[2]);
              if (!result.saved || chunks.join("") !== written) throw new Error("content changed");
              if (chunks.length !== 2 || chunks[0].length !== 999999) {
                throw new Error(`unexpected chunk boundaries ${chunks.map((one) => one.length)}`);
              }
              if (chunks.some((one) => Buffer.byteLength(one, "utf8") > 8000000)) {
                throw new Error("chunk exceeded main-process cap");
              }
              for (const chunk of chunks) {
                const first = chunk.charCodeAt(0);
                const last = chunk.charCodeAt(chunk.length - 1);
                if (first >= 0xDC00 && first <= 0xDFFF) throw new Error("chunk starts low");
                if (last >= 0xD800 && last <= 0xDBFF) throw new Error("chunk ends high");
              }
              if (appends[0].args[1] !== 0 || appends[1].args[1] !== 1) {
                throw new Error("sequences were not ordered");
              }
              const finish = calls.find((one) => one.channel === "harness:finishLargeJsonFile");
              if (finish.args[1] !== 2) throw new Error("finish sequence is wrong");
              process.stdout.write("preload bridge ok");
            })().catch((error) => { console.error(error); process.exitCode = 1; });
            """,
            ROOT / "desktop" / "preload.js",
        )
        self.assertEqual(output, "preload bridge ok")

    def test_main_chunk_protocol_orders_limits_fsyncs_publishes_and_cleans_up(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = self.run_node(
                r"""
                const fsReal = require("node:fs");
                const path = require("node:path");
                const crypto = require("node:crypto");
                const vm = require("node:vm");
                const source = fsReal.readFileSync(process.argv[1], "utf8");
                const start = source.indexOf("function closeLargeJsonExport");
                const end = source.indexOf('ipcMain.handle("harness:setFullScreen"');
                if (start < 0 || end < 0) throw new Error("large export handlers missing");
                const handlers = new Map();
                const ipcMain = {handle: (name, handler) => handlers.set(name, handler)};
                let fsyncs = 0;
                let writes = 0;
                const fs = Object.assign({}, fsReal, {
                  fsyncSync: (descriptor) => { fsyncs += 1; fsReal.fsyncSync(descriptor); },
                  writeSync: (descriptor, buffer, offset, length) => {
                    writes += 1;
                    const partial = Math.max(1, Math.floor(length / 2));
                    return fsReal.writeSync(descriptor, buffer, offset, partial);
                  },
                });
                const folder = process.argv[2];
                const chosen = path.join(folder, "portable.json");
                const context = {
                  fs, path, crypto, ipcMain, process, Buffer,
                  dialog: {showSaveDialogSync: () => chosen},
                  app: {getPath: () => folder},
                  window: null,
                  fromHarnessWindow: () => true,
                };
                vm.runInNewContext(
                  '"use strict"; const pendingJsonExports = new Map();\n'
                    + source.slice(start, end),
                  context,
                );
                const event = {sender: {id: 7}};
                const begin = handlers.get("harness:beginLargeJsonFile");
                const append = handlers.get("harness:appendLargeJsonFile");
                const finish = handlers.get("harness:finishLargeJsonFile");
                const abort = handlers.get("harness:abortLargeJsonFile");
                const parts = () => fsReal.readdirSync(folder).filter((one) => one.endsWith(".part"));

                (async () => {
                  let active = await begin(event, "portable.json");
                  let refused = false;
                  try { await append(event, active.identity, 1, "out of order"); }
                  catch (error) { refused = /out of order/.test(error.message); }
                  if (!refused || parts().length) throw new Error("out-of-order append not cleaned");

                  active = await begin(event, "portable.json");
                  refused = false;
                  try { await append(event, active.identity, 0, "x".repeat(8000001)); }
                  catch (error) { refused = /1 to 8000000/.test(error.message); }
                  if (!refused || parts().length) throw new Error("oversize chunk not cleaned");

                  active = await begin(event, "portable.json");
                  const first = await append(event, active.identity, 0, "hello ");
                  const second = await append(event, active.identity, 1, "😀");
                  if (first.sequence !== 1 || second.sequence !== 2 || second.bytes !== 10) {
                    throw new Error("ordered append accounting is wrong");
                  }
                  if (fsReal.existsSync(chosen)) throw new Error("destination published before finish");
                  const saved = await finish(event, active.identity, 2);
                  if (!saved.saved || fsyncs !== 1) throw new Error("finish was not fsynced");
                  if (writes <= 2) throw new Error("partial writes were not retried");
                  if (fsReal.readFileSync(chosen, "utf8") !== "hello 😀") {
                    throw new Error("finished bytes changed");
                  }
                  if (parts().length) throw new Error("finish left temporary files");

                  active = await begin(event, "portable.json");
                  if (!await abort(event, active.identity)) throw new Error("abort was refused");
                  if (parts().length) throw new Error("abort left temporary files");
                  process.stdout.write("main protocol ok");
                })().catch((error) => { console.error(error); process.exitCode = 1; });
                """,
                ROOT / "desktop" / "main.js",
                Path(temporary),
            )
        self.assertEqual(output, "main protocol ok")

    def test_main_contract_has_total_limit_and_atomic_fsync_finish_order(self) -> None:
        source = (ROOT / "desktop" / "main.js").read_text(encoding="utf-8")
        append = source[source.index('ipcMain.handle("harness:appendLargeJsonFile"'):
                        source.index('ipcMain.handle("harness:finishLargeJsonFile"')]
        finish = source[source.index('ipcMain.handle("harness:finishLargeJsonFile"'):
                        source.index('ipcMain.handle("harness:abortLargeJsonFile"')]
        abort = source[source.index('ipcMain.handle("harness:abortLargeJsonFile"'):
                       source.index('ipcMain.handle("harness:setFullScreen"')]
        self.assertIn("sequence !== held.sequence", append)
        self.assertIn("bytes.length > 8_000_000", append)
        self.assertIn("held.bytes + bytes.length > 768_000_000", append)
        self.assertIn("while (written < bytes.length)", append)
        self.assertIn("bytes.length - written", append)
        self.assertLess(finish.index("fs.fsyncSync"), finish.index("fs.closeSync"))
        self.assertLess(finish.index("fs.closeSync"), finish.index("fs.renameSync"))
        self.assertIn("closeLargeJsonExport(key)", finish)
        self.assertIn("closeLargeJsonExport(key)", abort)

    def test_renderer_fatal_decoder_stops_both_imports_before_request(self) -> None:
        output = self.run_node(
            r"""
            const fs = require("node:fs");
            const vm = require("node:vm");
            const source = fs.readFileSync(process.argv[1], "utf8");
            const between = (start, end) => source.slice(source.indexOf(start), source.indexOf(end));
            const invalid = {
              size: 2,
              name: "invalid.json",
              arrayBuffer: async () => Uint8Array.from([0xff, 0xfe]).buffer,
            };

            async function exercise(code, functionName, message, board) {
              const notices = [];
              let requests = 0;
              const input = {value: "selected"};
              const context = {
                TextDecoder, Uint8Array, JSON,
                request: async () => { requests += 1; return {}; },
                $: () => input,
                askForOneLine: async () => "copy",
                pipelineSaved: [],
                swarmKept: [],
                MAX_SAVED_BOARD_IMPORT_BYTES: 768000000,
                say: (text) => notices.push(String(text)),
                showError: (text) => notices.push(String(text)),
                sayInSwarm: (text) => notices.push(String(text)),
              };
              vm.runInNewContext(`${code}\nthis.importUnderTest = ${functionName};`, context);
              await context.importUnderTest(invalid);
              if (requests !== 0) throw new Error(`${functionName} contacted server`);
              if (!notices.includes(message)) throw new Error(`${functionName}: ${notices}`);
              if (input.value !== "") throw new Error(`${functionName} did not clear picker`);
            }

            (async () => {
              await exercise(
                between("async function importPipeline", "async function exportPipeline"),
                "importPipeline",
                "That automation file is not valid UTF-8. Nothing was imported.",
              );
              await exercise(
                between("async function importKeptBoard", "async function exportKeptBoard"),
                "importKeptBoard",
                "That saved-board file is not valid UTF-8. Nothing was imported.",
              );
              process.stdout.write("renderer decoder ok");
            })().catch((error) => { console.error(error); process.exitCode = 1; });
            """,
            ROOT / "src" / "our_harness" / "ui" / "app.js",
        )
        self.assertEqual(output, "renderer decoder ok")

    def test_project_rebind_behavior_keeps_identity_tasks_and_assignments(self) -> None:
        output = self.run_node(
            r"""
            const fs = require("node:fs");
            const vm = require("node:vm");
            const source = fs.readFileSync(process.argv[1], "utf8");
            const start = source.indexOf("async function rebindTheSwarmProject");
            const end = source.indexOf("async function addOneSwarmTask");
            const code = source.slice(start, end);
            const board = {
              projects: [{
                id: "project-7", name: "Portable", path: "C:/old",
                tasks: ["keep first", "keep second"],
                approved_test_command_digest: "a".repeat(64),
                history_identity: "history-7",
                is_there: false,
              }],
              works_on: [
                {agent: "agent-1", project: "project-7"},
                {agent: "agent-2", project: "project-7"},
              ],
            };
            const beforeTasks = JSON.stringify(board.projects[0].tasks);
            const beforeAssignments = JSON.stringify(board.works_on);
            let picked = null;
            const notices = [];
            const context = {
              thePickedProject: () => board.projects[0],
              askForOneLine: async () => "  D:/rebound/project  ",
              changeTheSwarmBoard: async (change, note) => {
                if (change(board) === false) return false;
                notices.push(typeof note === "function" ? note() : note);
                return true;
              },
              theSwarmProject: (id) => board.projects.find((one) => one.id === id),
              pickSwarmBox: (kind, id) => { picked = [kind, id]; },
              sayInSwarm: (text) => notices.push(String(text)),
            };
            vm.runInNewContext(`${code}\nthis.rebind = rebindTheSwarmProject;`, context);

            (async () => {
              await context.rebind();
              const project = board.projects[0];
              if (project.id !== "project-7" || project.history_identity !== "history-7") {
                throw new Error("project identity changed");
              }
              if (JSON.stringify(project.tasks) !== beforeTasks) throw new Error("tasks changed");
              if (JSON.stringify(board.works_on) !== beforeAssignments) {
                throw new Error("assignments changed");
              }
              if (project.path !== "D:/rebound/project") throw new Error("path did not rebind");
              if (project.approved_test_command_digest !== "") throw new Error("approval survived");
              if (JSON.stringify(picked) !== JSON.stringify(["project", "project-7"])) {
                throw new Error("same project was not reselected");
              }
              if (!notices.some((one) => /identity were kept/.test(one))) {
                throw new Error("preservation result was not disclosed");
              }
              process.stdout.write("project rebind ok");
            })().catch((error) => { console.error(error); process.exitCode = 1; });
            """,
            ROOT / "src" / "our_harness" / "ui" / "app.js",
        )
        self.assertEqual(output, "project rebind ok")

    def test_board_role_and_task_ui_preserve_exact_whitespace(self) -> None:
        output = self.run_node(
            r"""
            const fs = require("node:fs");
            const vm = require("node:vm");
            const source = fs.readFileSync(process.argv[1], "utf8");
            const agentCode = source.slice(
              source.indexOf("async function flushSwarmAgentSettings"),
              source.indexOf("function discardSwarmAgentSettings"),
            );
            const taskCode = source.slice(
              source.indexOf("async function addOneSwarmTask"),
              source.indexOf("function removeOneSwarmTask"),
            );
            const exactRole = " \r\nRole line\r\n ";
            const exactTask = "\t\r\nTask line\r\n\t";
            const agent = {id: "agent-1", name: "Planner", job: "old"};
            const project = {id: "project-1", name: "Project", tasks: []};
            const field = {value: exactTask, focus: () => {}};
            const drafts = new Map([["agent-1", {
              values: {
                name: "Planner", who: "route", job: exactRole, icon: "robot",
                colour: "#000000", bubbleColour: "#111111", profilePicture: "",
                pictureZoom: 100, pictureHue: 0,
              },
              revision: 1, savedRevision: 0, timer: 0, inFlight: null,
              error: "", waitingForBoard: false,
            }]]);
            const board = {agents: [agent], projects: [project]};
            const context = {
              Map, Number, Array, Promise,
              AGENT_JOB_CHARACTER_LIMIT: 100000,
              BOARD_TASK_CHARACTER_LIMIT: 200000,
              SWARM_AGENT_AUTOSAVE_DELAY: 100,
              swarmAgentSettingDrafts: drafts,
              window: {clearTimeout: () => {}, setTimeout: () => 1},
              systemPromptCharacters: (value) => Array.from(String(value || "")).length,
              whyTheBoardIsHeld: () => "",
              renderSwarmAgentSaveState: () => {},
              renderDisclosedTextCount: () => {},
              renderSwarmPanel: () => {},
              thePickedAgent: () => agent,
              thePickedProject: () => project,
              theSwarmAgent: () => agent,
              refreshTheChatFor: () => {},
              sayInSwarm: () => {},
              disclosedTextProblem: () => "",
              $: () => field,
              changeTheSwarmBoard: async (change) => { change(board); return true; },
            };
            vm.runInNewContext(
              `${agentCode}\n${taskCode}\nthis.flush = flushSwarmAgentSettings; this.addTask = addOneSwarmTask;`,
              context,
            );

            (async () => {
              if (!await context.flush("agent-1")) throw new Error("role save failed");
              if (agent.job !== exactRole) throw new Error("role whitespace changed");
              await context.addTask();
              if (project.tasks.length !== 1 || project.tasks[0] !== exactTask) {
                throw new Error("task whitespace changed");
              }
              process.stdout.write("board whitespace ok");
            })().catch((error) => { console.error(error); process.exitCode = 1; });
            """,
            ROOT / "src" / "our_harness" / "ui" / "app.js",
        )
        self.assertEqual(output, "board whitespace ok")
