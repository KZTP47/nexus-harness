"use strict";

// Deterministic, packaged, user-visible collaboration acceptance. These are
// scripted providers, not a claim about a live model's game-building quality.
// The real app/adapters/scheduler/files/transcript are used. The providers must
// consume each other's prior messages and alternate across useful file changes.
// Task actions use visible chat controls; APIs only arrange the isolated board
// or inspect durable state. Generated unit, HTTP API and Playwright tests are
// executed independently, including deliberately broken negative controls.

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const childProcess = require("node:child_process");
const {_electron: electron} = require("playwright-core");

const TIMEOUT = 180_000;
const AGENT_A = "portable-team-a";
const AGENT_B = "portable-team-b";
const PROJECT_ID = "portable-team-project";
const TEST_PROJECT_ID = "portable-team-test-project";
const STEER = "AMBER-STEER: make the arena amber and keep the win target at three coins.";
const GAME_GOAL = "TEAM-GAME-GOAL: Build a playable small 3D arena browser game with collect/reset controls, three coins to win, and an HTTP score API. Work together in the chat until the files and checks are complete.";
const TEST_GOAL = "TEAM-TESTS-GOAL: Create executable unit tests, HTTP API tests, and Playwright end-to-end tests for this arena repository and website. The tests must verify collecting three coins wins and Reset starts a fresh game.";

const GAME_ENGINE = String.raw`"use strict";
class Arena {
  constructor(target = 3) { this.target = target; this.reset(); }
  collect() { this.score = Math.min(this.target, this.score + 1); return this.won; }
  get won() { return this.score >= this.target; }
  reset() { this.score = 0; }
}
if (typeof module !== "undefined") module.exports = {Arena};
if (typeof window !== "undefined") window.Arena = Arena;
`;

const GAME_HTML = String.raw`<!doctype html><html lang="en"><meta charset="utf-8">
<title>Amber arena</title><style>body{font:20px system-ui;background:#17120a;color:#ffd27a;text-align:center}canvas{background:#261d0d;max-width:90vw}button{font:inherit;margin:12px;padding:10px}</style>
<h1>Amber arena</h1><canvas width="600" height="360" aria-label="3D arena"></canvas>
<p id="score" role="status"></p><button id="collect">Collect coin</button><button id="reset">Reset</button>
<script src="/arena.js"></script><script>
fetch('/arena-config.json').then(r=>r.json()).then(config=>{
const game=new Arena(config.target); const canvas=document.querySelector('canvas'),ctx=canvas.getContext('2d');
function cube(x,y,size,color){ctx.fillStyle=color;ctx.beginPath();ctx.moveTo(x,y-size);ctx.lineTo(x+size,y-size/2);ctx.lineTo(x,y);ctx.lineTo(x-size,y-size/2);ctx.closePath();ctx.fill();ctx.fillStyle='#996815';ctx.fillRect(x-size,y-size/2,size, size);ctx.fillStyle='#cb9424';ctx.fillRect(x,y-size/2,size,size);}
function draw(){ctx.clearRect(0,0,600,360);cube(300,210,85,config.color);for(let i=0;i<game.score;i++)cube(100+i*60,290,15,'#ffe5a3');document.querySelector('#score').textContent=game.won?'You won!':game.score+' / '+game.target;document.body.dataset.color=config.color;}
document.querySelector('#collect').onclick=()=>{game.collect();draw();};
document.querySelector('#reset').onclick=()=>{game.reset();draw();};draw();
});</script></html>`;

const SERVER = String.raw`"use strict";
const http=require('node:http'),fs=require('node:fs'),path=require('node:path');
const {Arena}=require('./arena.js');
function createServer(){return http.createServer((request,response)=>{
 const url=new URL(request.url,'http://localhost');
 if(url.pathname==='/api/score'){
  const game=new Arena();const coins=Number(url.searchParams.get('coins'));
  if(!Number.isInteger(coins)||coins<0||coins>100){response.writeHead(400);response.end('invalid coins');return;}
  for(let i=0;i<coins;i++)game.collect();
  response.setHeader('content-type','application/json');response.end(JSON.stringify({score:game.score,won:game.won}));return;
 }
 const files={'/':'arena.html','/arena.js':'arena.js','/arena-config.json':'arena-config.json'};
 const file=files[url.pathname];if(!file){response.writeHead(404);response.end('missing');return;}
 response.setHeader('content-type',file.endsWith('.js')?'application/javascript':file.endsWith('.json')?'application/json':'text/html');
 response.end(fs.readFileSync(path.join(__dirname,file)));
});}
module.exports={createServer};
if(require.main===module)createServer().listen(Number(process.env.PORT||0),'127.0.0.1',function(){console.log(this.address().port);});
`;

const UNIT_TESTS = String.raw`"use strict";
const test=require('node:test'),assert=require('node:assert/strict');
const {Arena}=require('./arena.js');
test('three coins win, score is capped, reset starts fresh',()=>{
 const game=new Arena();assert.equal(game.won,false);
 game.collect();game.collect();assert.equal(game.won,false);
 game.collect();assert.equal(game.won,true);assert.equal(game.score,3);
 game.collect();assert.equal(game.score,3);game.reset();assert.equal(game.score,0);assert.equal(game.won,false);
});
`;

const API_TESTS = String.raw`"use strict";
const test=require('node:test'),assert=require('node:assert/strict');
const {createServer}=require('./server.cjs');
test('HTTP API reports progress, completion, and invalid input',async()=>{
 const server=createServer();await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
 const base='http://127.0.0.1:'+server.address().port;
 try{
  assert.deepEqual(await(await fetch(base+'/api/score?coins=2')).json(),{score:2,won:false});
  assert.deepEqual(await(await fetch(base+'/api/score?coins=3')).json(),{score:3,won:true});
  assert.equal((await fetch(base+'/api/score?coins=-1')).status,400);
 }finally{await new Promise(resolve=>server.close(resolve));}
});
`;

const E2E_TESTS = String.raw`"use strict";
const assert=require('node:assert/strict'),path=require('node:path');
const test=require('node:test');
const {chromium}=require(path.join(process.env.NEXUS_TEST_PLAYWRIGHT_ROOT,'node_modules','playwright-core'));
const {createServer}=require('./server.cjs');
test('Playwright end-to-end collection and reset in the actual browser',async()=>{
 const server=createServer();await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
 let browser;
 try{
  browser=await chromium.launch({executablePath:process.env.NEXUS_TEST_CHROMIUM,headless:true});
  const page=await browser.newPage();await page.goto('http://127.0.0.1:'+server.address().port);
  await page.waitForFunction(()=>document.querySelector('#score').textContent==='0 / 3');
  assert.equal(await page.locator('body').getAttribute('data-color'),'#ffbf00');
  for(let i=0;i<3;i++)await page.locator('#collect').click();
  assert.equal(await page.locator('#score').textContent(),'You won!');
  await page.locator('#reset').click();assert.equal(await page.locator('#score').textContent(),'0 / 3');
  console.log('GENERATED_PLAYWRIGHT_PASS');
 }finally{if(browser)await browser.close();await new Promise(resolve=>server.close(resolve));}
});
`;

const PROVIDER = String.raw`import json, sys, time
from pathlib import Path
coordination = Path(sys.argv[1])
route = sys.argv[2]
payload = json.loads(sys.stdin.read())
context = str(payload.get('dynamic_context') or '')
project_tree = context.split('\n\nPROJECT TREE\n', 1)[1].split('\n\nREQUESTED FILE CONTENTS\n', 1)[0]
def project_has(name):
    # Provider processes belong to the configured app route. The selected
    # work repository is supplied by Nexus in context and may be elsewhere.
    return name in project_tree.splitlines()
templates = json.loads((coordination / 'templates.json').read_text(encoding='utf-8'))
scenario = 'tests' if 'TEAM-TESTS-GOAL' in context else 'game'
def record(event):
    with (coordination / 'dispatch.jsonl').open('a', encoding='utf-8') as stream:
        stream.write(json.dumps({'route': route, 'scenario': scenario, 'event': event}) + '\n')
def hold(name):
    (coordination / ('entered-' + name)).write_text(route, encoding='utf-8')
    deadline = time.monotonic() + 120
    while not (coordination / ('release-' + name)).is_file():
        if time.monotonic() >= deadline: raise RuntimeError('Timed out at controlled boundary ' + name)
        time.sleep(.05)
def change(name, content):
    return {'path':name, 'content':content, 'delete':False, 'reason':'fulfil the saved team objective'}
record('entered')
if scenario == 'game' and route == 'team-a' and not project_has('arena.js'):
    def ask_folder(identity, prompt):
        action = {'action':'ask_user','summary':'TEAM-A-QUESTION: Please clarify the destination.',
                  'interrupt_reason':'requirement_ambiguity','evidence':[],'risk':'low','changes':[],
                  'needs_files':[],'tool_calls':[],'tasks':[],'handoff_agent_id':'','criteria_evidence':[],
                  'questions':[{'id':identity,'prompt':prompt,'multiple':False,'allow_other':True,'options':[]}]}
        print(json.dumps({'text':json.dumps(action),'finish_reason':'stop'}))
        sys.exit(0)
    if not (coordination / 'asked-folder').exists():
        (coordination / 'asked-folder').write_text('first actual provider question', encoding='utf-8')
        ask_folder('folder-original', 'Use the misspelled PLOQGZ folder from the screenshot?')
    marker = '\n\nRESOLVED USER DECISIONS (user answers, not agent assumptions)\n'
    assert marker in context, 'The resumed requester lost its saved decision history'
    decisions = json.JSONDecoder().raw_decode(context.split(marker, 1)[1])[0]
    expected = (coordination / 'expected-answer.txt').read_text(encoding='utf-8')
    assert len(decisions) == 1 and decisions[0]['answers'][0]['text'] == expected, 'Exact typed correction was changed or duplicated'
    assert decisions[0]['audience'] == 'requesting_agent', 'Private answer lost its recipient scope'
    assert 'PLOQGZ' not in decisions[0]['answer_text'], 'The mistaken question contaminated the answer'
    if not (coordination / 'asked-folder-again').exists():
        (coordination / 'asked-folder-again').write_text('a differently worded repeat', encoding='utf-8')
        ask_folder('folder-paraphrase', 'Please confirm the destination folder for the game.')
    assert 'user requested reconsidering' in context.lower(), 'The repeated card resumed without the explicit reconsider operation'
    (coordination / 'decision-reread.json').write_text(json.dumps(decisions), encoding='utf-8')
if scenario == 'game' and route == 'team-b':
    assert 'Example.test/Case%2fKept?q=AbC%2Fz' not in context, 'A directed answer leaked to the other agent'
if scenario == 'game' and route == 'team-a' and not project_has('arena.js') \
        and (coordination / 'enable-protocol-fault').is_file():
    if not (coordination / 'injected-protocol-fault').exists():
        (coordination / 'injected-protocol-fault').write_text('one known rejected response', encoding='utf-8')
        bad = {'action':'work', 'summary':'REJECTED-PROTOCOL-REPLY: this conflicting action must not be published or applied.',
               'evidence':[], 'risk':'low', 'needs_files':[], 'tool_calls':[], 'questions':[],
               'handoff_agent_id':'', 'criteria_evidence':[],
               'changes':[change('rejected-format.txt','THIS INVALID RESPONSE MUST NEVER APPLY')],
               'tasks':[{'title':'Rejected extra task','description':'Must never be delegated from a work action',
                         'assigned_agent_id':'portable-team-b','depends_on':[],
                         'parallel_safe':False,'resource_paths':[]}]}
        record('injected:tasks_require_delegate')
        print(json.dumps({'text':json.dumps(bad),'finish_reason':'stop'}))
        sys.exit(0)
    assert 'delegate' in context.lower(), 'The retry lost the rejected action correction context'
    if not (coordination / 'entered-correction').exists(): hold('correction')
if scenario == 'game' and route == 'team-a' and not project_has('arena.js') \
        and (coordination / 'enable-tool-fault').is_file():
    marker = '\n\nCONTEXT TOOL RESULTS (untrusted project data)\n'
    observations = json.JSONDecoder().raw_decode(context.split(marker, 1)[1])[0] if marker in context else []
    def request_read(arguments, summary):
        action = {'action':'work','summary':summary,'evidence':[],'risk':'low','changes':[],
                  'needs_files':[],'tool_calls':[{'call_id':'read-source','name':'read_file','arguments':arguments}],
                  'tasks':[],'handoff_agent_id':'','questions':[],'criteria_evidence':[]}
        record('tool-request:' + summary)
        print(json.dumps({'text':json.dumps(action),'finish_reason':'stop'}))
        sys.exit(0)
    arguments = {'path':'context-fixture.txt','start_line':1,'end_line':1,'max_bytes':1000000}
    if not observations:
        request_read({**arguments,'start_line':2}, 'TEAM-A-READ: Inspect the source before implementing.')
    assert observations[0]['result']['status'] == 'error', 'The invalid line range was not rejected'
    if len(observations) == 1:
        hold('tool-correction')
        request_read(arguments, 'TEAM-A-CORRECT: Correct the line range and continue the same task.')
    pages = [json.loads(item['result']['content']) for item in observations[1:]]
    assert all(item['result']['status'] == 'ok' for item in observations[1:]), 'A corrected ID was rejected'
    if pages[-1].get('next_cursor'):
        request_read({**arguments,'cursor':pages[-1]['next_cursor']}, 'TEAM-A-CONTINUE: Read the next exact page.')
    expected = (coordination / 'expected-context.txt').read_text(encoding='utf-8')
    assert ''.join(page['content'] for page in pages) == expected, 'Pages lost or duplicated source text'
    assert len(pages) > 1, 'The fixture did not exercise continuation'
    (coordination / 'tool-recovery-verified.json').write_text(json.dumps({'pages':len(pages),'same_id':True}), encoding='utf-8')
changes = []
kind = 'complete'
if scenario == 'game':
    if not project_has('arena.js'):
        assert route == 'team-a', 'The selected lead did not start'
        changes = [change('arena.js', templates['engine'])]
        kind = 'work'
        summary = 'TEAM-A-BASE: Teammate B, I implemented the arena mechanics. Please build the playable page and score API, then I will check your work.'
    elif not project_has('arena.html'):
        assert route == 'team-b', 'The team did not alternate after the first work turn'
        assert 'TEAM-A-BASE' in context, 'B did not receive A\'s real preceding message'
        if 'AMBER-STEER' not in context:
            hold('stale')
            changes = [change('obsolete-blue.txt', 'THIS STALE RESULT MUST NEVER APPLY')]
            summary = 'OBSOLETE-BLUE-REPLY: A superseded proposal that must never appear as accepted work.'
        else:
            changes = [change('arena.html', templates['html']), change('server.cjs', templates['server']),
                       change('arena-config.json', json.dumps({'target':3,'color':'#ffbf00'}))]
            kind = 'work'
            summary = 'TEAM-B-STEERED: A, I used your mechanics and the user\'s amber steering. The playable page and HTTP API are ready for your review.'
    else:
        assert 'AMBER-STEER' in context, 'Active steering disappeared on a later turn'
        assert 'TEAM-B-STEERED' in context, 'A did not receive B\'s implementation message'
        if route == 'team-a' and not (coordination / 'entered-pause').exists(): hold('pause')
        summary = ('TEAM-A-REVIEW: B, I checked your amber page, three-coin win, reset, and API against the goal.'
                   if route == 'team-a' else 'TEAM-B-FINAL: A, our latest files satisfy the steered goal and are ready for deterministic checks.')
elif not project_has('test_unit.cjs'):
    assert route == 'team-a'
    changes = [change('test_unit.cjs', templates['unit'])]
    kind = 'work'
    summary = 'TEST-A-UNIT: B, the unit test now checks winning, score bounds, and reset. Please add real HTTP API and browser tests.'
elif not project_has('test_api.cjs'):
    assert route == 'team-b'
    assert 'TEST-A-UNIT' in context, 'B did not consume A\'s test handoff'
    changes = [change('test_api.cjs', templates['api']), change('test_e2e.cjs', templates['e2e'])]
    kind = 'work'
    summary = 'TEST-B-INTEGRATION: A, I added tests against a real local HTTP server and real Playwright button clicks. Please review all three layers.'
else:
    assert 'TEST-B-INTEGRATION' in context, 'The generated tests were not reviewed with B\'s contribution'
    summary = ('TEST-A-REVIEW: B, all three executable test layers cover the requested behavior.'
               if route == 'team-a' else 'TEST-B-FINAL: A, the unit, API, and Playwright tests are ready to execute.')
refs = ['file:' + item['path'] for item in changes] or ['verified-no-change']
criteria = [{'criterion':c,'evidence_refs':refs} for c in [
 'Original objective is satisfied','Every required task is complete','Configured deterministic verification passes']]
action = {'action':kind,'summary':summary,'evidence':refs,'risk':'low','changes':changes,
          'needs_files':[],'tool_calls':[],'tasks':[],'handoff_agent_id':'','questions':[], 'criteria_evidence':criteria}
record('returned:' + summary.split(':')[0])
print(json.dumps({'text':json.dumps(action),'finish_reason':'stop'}))
`;

function isolatedEnvironment(profile) {
  const environment = {};
  const permitted = new Set(["SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "COMSPEC", "SYSTEMDRIVE",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "PROCESSOR_IDENTIFIER", "OS", "CI", "GITHUB_ACTIONS"]);
  for (const [key, value] of Object.entries(process.env)) {
    if (permitted.has(key.toUpperCase())) environment[key] = value;
  }
  for (const [key, relative] of Object.entries({APPDATA:"Roaming", LOCALAPPDATA:"Local",
    USERPROFILE:"Home", HOME:"Home", TEMP:"Temp", TMP:"Temp"})) {
    environment[key] = path.join(profile, relative);
    fs.mkdirSync(environment[key], {recursive:true});
  }
  return environment;
}

function fixture(exe, project, coordination, environment) {
  const runtime = path.join(path.dirname(exe), "resources", "runtime");
  const python = path.join(runtime, "python.exe");
  const manifest = JSON.parse(fs.readFileSync(path.join(runtime, "NEXUS_RUNTIME.json"), "utf8"));
  const playwrightRoot = path.join(runtime, "playwright");
  const node = path.join(playwrightRoot, manifest.playwright.node);
  const browser = path.join(playwrightRoot, manifest.playwright.chromium_executable);
  for (const file of [python, node, browser]) assert.ok(fs.existsSync(file), `Missing bundled tool: ${file}`);
  fs.mkdirSync(path.join(project, ".harness"), {recursive:true});
  fs.writeFileSync(path.join(coordination, "provider.py"), PROVIDER, "utf8");
  fs.writeFileSync(path.join(coordination, "templates.json"), JSON.stringify({engine:GAME_ENGINE,
    html:GAME_HTML, server:SERVER, unit:UNIT_TESTS, api:API_TESTS, e2e:E2E_TESTS}), "utf8");
  // This pre-existing independent check must execute the newly generated
  // mechanics before the product can complete either goal. File existence and
  // the providers' own completion claims are insufficient runtime evidence.
  fs.writeFileSync(path.join(project, "test_runtime.cjs"), String.raw`"use strict";
const test = require('node:test'), assert = require('node:assert/strict');
const {Arena} = require('./arena.js');
test('the created arena wins at three coins, caps scoring, and resets', () => {
  const game = new Arena();
  assert.equal(game.score, 0);
  assert.deepEqual([game.collect(), game.collect(), game.collect()], [false, false, true]);
  game.collect(); assert.equal(game.score, 3);
  game.reset(); assert.equal(game.score, 0); assert.equal(game.won, false);
});
`, "utf8");
  fs.writeFileSync(path.join(project, "test_acceptance.py"), String.raw`import json, unittest
from pathlib import Path
class ArenaAcceptance(unittest.TestCase):
    def test_goal_files_and_steered_contract(self):
        for name in ['arena.js','arena.html','server.cjs','arena-config.json']:
            self.assertTrue(Path(name).is_file(), 'Missing requested game artifact ' + name)
        self.assertEqual(json.loads(Path('arena-config.json').read_text()), {'target':3,'color':'#ffbf00'})
        self.assertFalse(Path('obsolete-blue.txt').exists(), 'A superseded provider reply modified the project')
        if Path('test_unit.cjs').exists():
            for name in ['test_unit.cjs','test_api.cjs','test_e2e.cjs']:
                self.assertTrue(Path(name).is_file(), 'The requested test layers are incomplete')
if __name__ == '__main__': unittest.main()
`, "utf8");
  const provider = (route) => ({kind:"local", model:`scripted-${route}`,
    command:[python, path.join(coordination, "provider.py"), coordination, route],
    endpoint:"http://127.0.0.1:1", max_concurrency:1, timeout_seconds:150});
  fs.writeFileSync(path.join(project, ".harness", "config.local.json"), JSON.stringify({schema_version:1,
    providers:{"team-a":provider("team-a"), "team-b":provider("team-b")},
    project:{test_commands:[["python", "-m", "unittest", "test_acceptance"],
      // Direct invocation keeps Node's native test runner in this process;
      // the OS containment profile intentionally disallows arbitrary children.
      ["node", "test_runtime.cjs"]]},
    workflow:{require_review:false, reviewers:1, review_parallelism:1}}, null, 2), "utf8");
  const source = path.join(path.dirname(exe), "resources", "harness", "src");
  const trust = childProcess.spawnSync(python, ["-c", ["import sys", "from pathlib import Path",
    `sys.path.insert(0, ${JSON.stringify(source)})`,
    "from our_harness.config import trust_project_local_config",
    "from our_harness.pipeline_runs import project_identity", "root=Path(sys.argv[1]).resolve()",
    "trust_project_local_config(root)", "project_identity(root)"].join("; "), project],
  {cwd:project, env:environment, encoding:"utf8", timeout:60_000});
  assert.equal(trust.status, 0, trust.stderr || trust.stdout);
  return {node, browser, playwrightRoot};
}

async function until(read, description, timeout = TIMEOUT) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    const result = await read();
    if (result) return result;
    await new Promise(resolve => setTimeout(resolve, 150));
  }
  throw new Error(`Timed out waiting for ${description}`);
}

async function waitForPanelRuntime(page) {
  await page.waitForFunction(() => location.protocol === "http:"
    && document.readyState !== "loading"
    && typeof request === "function"
    && typeof activeConversationFor === "function"
    && typeof swarmChatIsHydrating === "function", null, {timeout: TIMEOUT});
}

async function launch(exe, profile, project, environment) {
  const app = await electron.launch({executablePath:exe,
    args:[`--user-data-dir=${path.join(profile, "Electron")}`, "--project", project],
    env:environment, timeout:TIMEOUT});
  try {
    const page = await app.firstWindow({timeout:TIMEOUT});
    page.on("pageerror", error => console.error("info  renderer error: " + String(error)));
    const first = await Promise.race([
      page.waitForFunction(()=>location.protocol === "http:", null, {timeout:TIMEOUT}).then(()=>"panel"),
      page.locator("#repair").waitFor({state:"visible", timeout:TIMEOUT}).then(()=>"repair")]);
    if (first === "repair") await page.locator("#repair").click();
    await waitForPanelRuntime(page);
    return {app, page};
  } catch (error) { await app.close(); throw error; }
}

async function setupBoard(page, project, testProject) {
  return page.evaluate(async ({a,b,projectId,projectPath,testProjectId,testProjectPath})=>{
    const {board} = await request("/api/swarm?refresh_providers=true");
    board.agents = [
      {id:a,name:"Team agent A",who:"team-a",job:"Build and respond to your teammate",at:{x:50,y:60},colour:"#4f46e5",icon:"robot"},
      {id:b,name:"Team agent B",who:"team-b",job:"Improve and respond to your teammate",at:{x:340,y:60},colour:"#0f766e",icon:"robot"}];
    board.projects = [
      {id:projectId,path:projectPath,name:"Portable arena",is_there:true,tasks:[],at:{x:80,y:370},approved_test_command_digest:""},
      {id:testProjectId,path:testProjectPath,name:"Arena test repository",is_there:true,tasks:[],at:{x:380,y:370},approved_test_command_digest:""}];
    board.works_on = [{agent:a,project:projectId},{agent:b,project:projectId},
      {agent:a,project:testProjectId},{agent:b,project:testProjectId}];
    board.talks_to = [{one:a,other:b}];board.made_agents=2;board.made_projects=2;
    await request("/api/swarm/save",{method:"POST",body:JSON.stringify({board})});
    const ids=[];
    for(let index=0;index<2;index++) {
      const chat=await request("/api/swarm/chats/create",{method:"POST",body:JSON.stringify({agent:a,peer:b})});
      await request("/api/swarm/chats/project",{method:"POST",body:JSON.stringify({agent:a,chat:chat.active,project:index===0?projectId:testProjectId})});
      ids.push(chat.active);
    }
    return ids;
  },{a:AGENT_A,b:AGENT_B,projectId:PROJECT_ID,projectPath:project,
    testProjectId:TEST_PROJECT_ID,testProjectPath:testProject});
}

async function openChat(page, chatId) {
  if (!await page.locator("#theBigChat").isVisible()) {
    await page.locator('[data-view="swarm"]').click();
    await page.locator(`.swarm-box[data-id="${AGENT_A}"] [data-does="chat"]`).click();
    await page.getByRole("button", {name:"Open full Nexus chat",exact:true}).click();
  }
  const history=page.locator("#theBigChatHistoryToggle");
  if(await history.count() && await history.getAttribute("aria-expanded")!=="true")await history.click();
  await page.locator(`#theBigChatConversationList [data-conversation-action="pick"][data-chat-id="${chatId}"]`).click();
  await page.waitForFunction(([id,agent])=>activeConversationFor(agent)?.id===id && !swarmChatIsHydrating(agent),
    [chatId,AGENT_A], {timeout:30_000});
  if(await history.count() && await history.getAttribute("aria-expanded")==="true")await history.click();
}

async function goals(page) {
  return page.evaluate(async ()=>(await request("/api/long-horizon/goals")).goals || []);
}

async function goalFor(page, chatId, predicate = ()=>true) {
  return until(async ()=>(await goals(page)).find(goal=>goal.conversation_id===chatId && predicate(goal)),
    `saved chat ${chatId} goal state`);
}

async function transcriptContains(page, words) {
  return until(async ()=>{
    const transcript=await page.locator("#theBigChatSaid").textContent();
    return words.every(word=>transcript.includes(word)) ? transcript : null;
  }, `visible transcript messages: ${words.join(", ")}`);
}

async function delayAdmissionRefreshUntilCollapse(page, chatId) {
  const observed={chatId,activityId:"",interceptedRequests:0,collapsedBeforeResponse:false};
  let gate=null;
  const pattern="**/api/long-horizon/goals";
  const handler=async route=>{
    const pending=await page.evaluate(([agent,id])=>{
      const activity=swarmChatActivityFor(agent,id);
      return activity?.chatId===id && activity.terminalState==="admitted"
        && activity.settledBy==="response" && !activity.responseFinished
        ? {id:activity.id} : null;
    },[AGENT_A,chatId]);
    if(pending) {
      observed.interceptedRequests++;
      if(!gate) {
        observed.activityId=pending.id;
        // Delay real HTTP reads, including the awaited post-admission refresh.
        // The product timer must collapse the still-unfinished activity itself.
        gate=(async ()=>{
          await page.waitForFunction(([agent,id,activityId])=>{
            const activity=swarmChatActivityFor(agent,id);
            return activity?.id===activityId && activity.collapsed && !activity.responseFinished;
          },[AGENT_A,chatId,pending.id],{timeout:15_000});
          observed.collapsedBeforeResponse=true;
        })();
      }
      await gate;
    }
    await route.continue();
  };
  await page.route(pattern,handler);
  return {observed,remove:()=>page.unroute(pattern,handler)};
}

async function captureReadableConversation(page, goalId, markers, destination) {
  // API completion alone is not UI evidence. Wait for the actual persisted
  // complete status to reach this chat, then show both agents' latest messages.
  await page.locator(`#theBigChatSaid .chat-goal-completion[data-goal-id="${goalId}"][data-goal-status="complete"]`).waitFor({timeout:30_000});
  await transcriptContains(page,markers);
  // Transcript projection and the controller's goal inventory are independent
  // reads. Wait for the visible controls to catch up with this exact completion
  // so the evidence cannot show a stale queued/running header above the result.
  await page.waitForFunction(id=>{
    const saved=longGoals.find(one=>one.goal_id===id);
    const panel=document.querySelector("#theBigChatTeamGoal");
    const work=document.querySelector("#theBigChatWork");
    return saved?.status==="complete" && panel?.hidden===true && work && !work.hidden
      && !work.disabled && work.getBoundingClientRect().height>0;
  },goalId,{timeout:30_000});
  // A Windows runner may clamp Electron's native window to its smaller virtual
  // display. Use the same real page viewport as the local desktop layout proof;
  // screenshot acceptance must not depend on the build worker's display setup.
  const initialViewport=await page.evaluate(()=>({width:innerWidth,height:innerHeight}));
  await page.setViewportSize({width:1264,height:775});
  await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
  // The last reply may already be visible above trailing status cards while
  // the preceding agent sits just outside the viewport. Align the first
  // requested agent row so this is deliberate conversation framing, not a
  // no-op scroll of the already visible last reply.
  await page.locator("#theBigChatSaid .chat-prose").filter({hasText:markers[0]}).last().evaluate(paragraph=>{
    const transcript=paragraph.closest("#theBigChatSaid");
    const row=paragraph.closest(".the-big-chat-turn") || paragraph;
    transcript.scrollTop+=row.getBoundingClientRect().top-transcript.getBoundingClientRect().top-12;
  });
  let latestLayout=null;
  let layout;
  try {
    layout=await until(async ()=>{
      latestLayout=await page.evaluate((wanted)=>{
        const transcript=document.querySelector('#theBigChatSaid');
        const bounds=transcript.getBoundingClientRect();
        const visible=wanted.map(marker=>{
          const matches=[...transcript.querySelectorAll('.chat-prose')].filter(one=>one.textContent.includes(marker));
          const paragraph=matches.at(-1);
          if(!paragraph)return {marker,visible:false};
          const rect=paragraph.getBoundingClientRect();
          const visibleHeight=Math.min(rect.bottom,bounds.bottom,innerHeight)-Math.max(rect.top,bounds.top,0);
          return {marker,visible:visibleHeight>=Math.min(30,rect.height)&&rect.left>=bounds.left&&rect.right<=bounds.right,
            visibleHeight,height:rect.height,top:rect.top,bottom:rect.bottom,left:rect.left,right:rect.right};
        });
        return {viewportWidth:innerWidth,viewportHeight:innerHeight,transcriptHeight:bounds.height,
          transcriptTop:bounds.top,transcriptBottom:bounds.bottom,transcriptLeft:bounds.left,transcriptRight:bounds.right,
          transcriptScrollTop:transcript.scrollTop,transcriptScrollHeight:transcript.scrollHeight,
          teamControllerHidden:document.querySelector("#theBigChatTeamGoal").hidden,visible};
      },markers);
      return latestLayout.transcriptHeight>=Math.min(240,latestLayout.viewportHeight*.35)
        &&latestLayout.visible.every(one=>one.visible)?latestLayout:null;
    },"a readable chat viewport showing both actual agents' latest messages",30_000);
  } catch(error) {
    const diagnostic={initialViewport,...latestLayout};
    fs.writeFileSync(destination.replace(/\.png$/,"-layout.json"),JSON.stringify(diagnostic,null,2));
    console.error("info  unreadable conversation geometry: "+JSON.stringify(diagnostic));
    throw error;
  }
  await page.screenshot({path:destination});
  fs.writeFileSync(destination.replace(/\.png$/,"-layout.json"),JSON.stringify({initialViewport,...layout},null,2));
}

async function approveDiscoveredChecks(page, projectId) {
  await page.getByRole("button",{name:"Minimise",exact:true}).click();
  const compact=page.locator(`.swarm-chat-card[data-agent="${AGENT_A}"]`);
  if(await compact.isVisible()) await compact.locator('[data-does="minimise"]').click();
  const settings=page.locator(`.swarm-box[data-id="${projectId}"] [data-does="settings"]`);
  await settings.evaluate(button=>button.scrollIntoView({block:"center"}));
  await settings.click();
  await page.locator("#swarmProjectVerificationRefresh").click();
  await until(async ()=>!(await page.locator("#swarmProjectVerificationApprove").isDisabled()),
    "the exact discovered project checks available for user approval",30_000);
  const commands=await page.locator("#swarmProjectVerificationCommands").textContent();
  assert.ok(commands.includes("unittest")&&commands.includes("discover"),`Wrong discovered command: ${commands}`);
  page.once("dialog",dialog=>dialog.accept());
  await page.locator("#swarmProjectVerificationApprove").click();
  await until(async ()=>(await page.locator("#swarmProjectVerificationStatus").textContent()).startsWith("Approved."),
    "the user's exact command approval persisted on the project",30_000);
}

async function approveChatChecks(page, goal) {
  await page.locator("#theBigChat").getByRole("button",{name:"Advanced goal details",exact:true}).click();
  await until(async ()=>(await page.locator("#missionGoalSelect").inputValue())===goal.goal_id,
    "the selected chat's exact goal details",30_000);
  if(!await page.locator("#missionEvidence").isVisible()) {
    await page.getByText("Artifacts, diffs, tests, and reviews",{exact:true}).click();
  }
  const endpoint="/api/long-horizon/verification-approval";
  const previewResponse=page.waitForResponse(response=>{
    const url=new URL(response.url());
    return url.pathname===endpoint && response.request().method()==="GET"
      && url.searchParams.get("goal_id")===goal.goal_id;
  },{timeout:30_000});
  const approvalResponse=page.waitForResponse(response=>new URL(response.url()).pathname===endpoint
    && response.request().method()==="POST",{timeout:30_000});
  let confirmation="";
  page.once("dialog",async dialog=>{confirmation=dialog.message();await dialog.accept();});
  await page.getByRole("button",{name:"Review this chat's test commands",exact:true}).click();
  const preview=await (await previewResponse).json();
  const response=await approvalResponse;
  const approved=await response.json();
  assert.equal(response.status(),200,JSON.stringify(approved));
  assert.equal(preview.goal_id,goal.goal_id);
  assert.equal(preview.approved,false,"The goal's commands were approved before the visible confirmation");
  assert.ok(preview.commands.some(command=>command.includes("unittest")&&command.includes("discover")),
    "The selected working copy's discovered Python check was not displayed");
  assert.ok(confirmation.includes(goal.goal_id)&&confirmation.includes(preview.approval_digest));
  for(const command of preview.commands)assert.ok(confirmation.includes(JSON.stringify(command)));
  assert.ok(confirmation.includes("only to this chat's goal")&&confirmation.includes("Resume team"));
  const sent=response.request().postDataJSON();
  assert.equal(sent.goal_id,goal.goal_id);
  assert.equal(sent.expected_revision,preview.revision);
  assert.equal(sent.command_digest,preview.approval_digest);
  assert.equal(sent.approved,true);
  assert.equal(approved.goal.goal_id,goal.goal_id);
  assert.equal(approved.goal.status,"paused","Approving commands silently resumed the goal");
  assert.equal(approved.goal.verification.status,"not_run","Commands executed before Resume");
  assert.equal(approved.approval.approved,true);
  return approved.goal;
}

async function startGoal(page, words) {
  await page.locator("#theBigChatBox").fill(words);
  page.once("dialog", dialog=>dialog.accept());
  await page.locator("#theBigChatWork").click();
  await transcriptContains(page,[words]);
}

async function answerAndReconsiderFolder(page, chatId, exactAnswer, coordination) {
  const first = await goalFor(page, chatId, goal => goal.status === "waiting_for_user" && goal.pending_interrupts?.length === 1);
  const panel = page.locator("#theBigChatTeamGoal");
  await panel.locator("textarea").fill(exactAnswer);
  await panel.getByRole("combobox", {name:"Answer audience",exact:true}).selectOption("requesting_agent");
  const answerResponse = page.waitForResponse(response => new URL(response.url()).pathname === "/api/long-horizon/answer"
    && response.request().method() === "POST", {timeout:30_000});
  await panel.getByRole("button", {name:"Send answers to the asking agents",exact:true}).click();
  const response = await answerResponse;
  const answered = await response.json();
  assert.equal(response.status(), 200, JSON.stringify(answered));
  const sent = response.request().postDataJSON();
  assert.equal(sent.goal_id, first.goal_id);
  assert.deepEqual(sent.pending_ids, first.pending_interrupts.map(item => item.id));
  assert.match(sent.request_id, /^[0-9a-f-]{36}$/i);
  assert.deepEqual(sent.answers[first.pending_interrupts[0].id], {schema_version:1,audience:"requesting_agent",
    questions:[{question_id:"folder-original",selected_options:[],text:exactAnswer}]});
  const original = answered.goal.interrupts.find(item => item.id === first.pending_interrupts[0].id);
  assert.equal(original.state, "resolved");
  assert.equal(original.answer_record.answers[0].text, exactAnswer);
  const repeated = await goalFor(page, chatId, goal => goal.decision_reconsideration?.available === true);
  assert.equal(repeated.goal_id, first.goal_id);
  assert.equal(repeated.pending_interrupts[0].questions[0].id, "folder-paraphrase");
  const preflightResponses = [];
  const observePreflight = reply => {
    const url = new URL(reply.url());
    if (url.pathname === "/api/long-horizon/goal" && url.searchParams.get("id") === first.goal_id
      && reply.request().method() === "GET") preflightResponses.push(reply.json());
  };
  page.on("response", observePreflight);
  const reconsiderResponse = page.waitForResponse(reply => new URL(reply.url()).pathname === "/api/long-horizon/reconsider"
    && reply.request().method() === "POST", {timeout:30_000});
  await panel.getByRole("button", {name:"Reconsider using saved answers",exact:true}).click();
  const reconsideredResponse = await reconsiderResponse;
  page.off("response", observePreflight);
  const reconsidered = await reconsideredResponse.json();
  assert.equal(reconsideredResponse.status(), 200, JSON.stringify(reconsidered));
  const reconsiderBody = reconsideredResponse.request().postDataJSON();
  const fresh = (await Promise.all(preflightResponses)).find(read => read.goal?.revision === reconsiderBody.expected_revision);
  assert.ok(fresh, "The actual reconsider click did not read the exact revision it submitted");
  assert.equal(reconsiderBody.goal_id, first.goal_id);
  assert.deepEqual(reconsiderBody.pending_ids, repeated.pending_interrupts.map(item => item.id));
  assert.equal(reconsiderBody.expected_revision, fresh.goal.revision);
  assert.equal(fresh.goal.scheduler_live, false, "Reconsideration raced the scheduler's final release");
  assert.deepEqual(fresh.goal.pending_interrupts, repeated.pending_interrupts,
    "The preflight replaced the displayed question set");
  assert.equal(Object.hasOwn(reconsiderBody, "answers"), false);
  assert.equal(Object.hasOwn(reconsiderBody, "approved"), false);
  assert.equal(reconsidered.goal.pending_interrupts.length, 0);
  assert.equal(reconsidered.goal.interrupts.filter(item => item.state === "resolved").length, 1);
  assert.deepEqual(reconsidered.goal.interrupts.find(item => item.id === original.id).answer_record, original.answer_record);
  assert.equal(reconsidered.goal.interrupts.find(item => item.id === repeated.pending_interrupts[0].id).state, "superseded");
  await until(() => fs.existsSync(path.join(coordination, "decision-reread.json")), "the actual requester rereading the unchanged directed answer");
  fs.writeFileSync(path.join(coordination, "answer-reconsideration.json"), JSON.stringify({
    goal_id:first.goal_id,answer:sent,reconsider:reconsiderBody,
    resolved_decision_id:original.id,superseded_interrupt_id:repeated.pending_interrupts[0].id,
    saved_answer_unchanged:true,provider_reread:true}, null, 2));
  console.log("pass  exact typed paths and URLs reach the requester without the mistaken prompt; explicit reconsider preserves that answer and resumes the same goal");
}

function execute(node, arguments_, project, environment, expectedSuccess = true) {
  const result = childProcess.spawnSync(node, arguments_, {cwd:project, env:environment,
    encoding:"utf8", timeout:60_000, windowsHide:true});
  if (expectedSuccess) assert.equal(result.status, 0, `${arguments_.join(" ")}\n${result.stderr}\n${result.stdout}`);
  else assert.ok(result.status !== 0 && result.status !== null,
    `A deliberately broken implementation escaped its generated tests: ${result.stdout} ${result.stderr}`);
  return result.stdout;
}

async function verifyArtifacts(project, coordination, bundled, environment) {
  const testEnvironment={...environment,NEXUS_TEST_PLAYWRIGHT_ROOT:bundled.playwrightRoot,
    NEXUS_TEST_CHROMIUM:bundled.browser};
  const positive={
    unit:execute(bundled.node,["--test","test_unit.cjs"],project,testEnvironment),
    api:execute(bundled.node,["--test","test_api.cjs"],project,testEnvironment),
    e2e:execute(bundled.node,["test_e2e.cjs"],project,testEnvironment)};
  assert.ok(positive.e2e.includes("GENERATED_PLAYWRIGHT_PASS"));
  const broken=path.join(coordination,"negative control");fs.mkdirSync(broken);
  for(const file of ["arena.js","arena.html","arena-config.json","server.cjs","test_unit.cjs","test_api.cjs","test_e2e.cjs"])
    fs.copyFileSync(path.join(project,file),path.join(broken,file));
  fs.writeFileSync(path.join(broken,"arena.js"),GAME_ENGINE.replace("this.score + 1","this.score + 0"),"utf8");
  execute(bundled.node,["--test","test_unit.cjs"],broken,testEnvironment,false);
  execute(bundled.node,["--test","test_api.cjs"],broken,testEnvironment,false);
  execute(bundled.node,["test_e2e.cjs"],broken,testEnvironment,false);
  fs.writeFileSync(path.join(coordination,"artifact-verification.json"),JSON.stringify({
    schema_version:1,positive:{unit:true,api:true,playwright:true},
    negative:{unit_detected_broken_scoring:true,api_detected_broken_scoring:true,playwright_detected_broken_scoring:true}},null,2));
  console.log("pass  generated unit, HTTP API, and real Playwright tests pass and each rejects broken game scoring");
}

async function main() {
  assert.equal(process.platform,"win32","This packaged acceptance requires Windows");
  const exe=path.resolve(process.argv[2] || path.join(__dirname,"build-output","win-unpacked","Nexus Harness.exe"));
  assert.ok(fs.existsSync(exe),`Build the app first: ${exe}`);
  const root=fs.mkdtempSync(path.join(os.tmpdir(),"nexus-team-chat-"));
  const project=path.join(root,"PLOQQIZ – Åsa & O'Brien (portable)!");
  const testProject=path.join(root,"Separate arena test repository (no configured checks)");
  const coordination=path.join(root,"scripted provider control");
  const profile=path.join(root,"fresh profile");
  for(const directory of [project,testProject,coordination,profile])fs.mkdirSync(directory);
  const environment=isolatedEnvironment(profile);
  const bundled=fixture(exe,project,coordination,environment);
  const exactAnswer=`  ${project}\nhttps://Example.test/Case%2fKept?q=AbC%2Fz&mode=Play#Chapter-2  `;
  fs.writeFileSync(path.join(coordination,"expected-answer.txt"),exactAnswer,"utf8");
  const protocolFault=process.env.NEXUS_TEAM_PROTOCOL_FAULT === "1";
  if(protocolFault)fs.writeFileSync(path.join(coordination,"enable-protocol-fault"),"one malformed action");
  const toolFault=process.env.NEXUS_TEAM_TOOL_FAULT === "1";
  if(toolFault) {
    fs.writeFileSync(path.join(coordination,"enable-tool-fault"),"corrected call identity and bounded pages");
    const source='Source "quoted" \\ path Å😀 '.repeat(550);
    fs.writeFileSync(path.join(project,"context-fixture.txt"),source);
    fs.writeFileSync(path.join(coordination,"expected-context.txt"),source);
  }
  console.log(`info  deterministic scripted-provider fixture: ${root}`);
  let running=null,passed=false;
  try {
    running=await launch(exe,profile,project,environment);
    let page=running.page;
    const [gameChat,testChat]=await setupBoard(page,project,testProject);
    await page.reload({waitUntil:"domcontentloaded"});
    await waitForPanelRuntime(page);
    await openChat(page,gameChat);
    await startGoal(page,GAME_GOAL);
    await answerAndReconsiderFolder(page,gameChat,exactAnswer,coordination);
    if(protocolFault) {
      await until(()=>fs.existsSync(path.join(coordination,"entered-correction")),"automatic correction after the malformed lead reply");
      const correcting=await goalFor(page,gameChat);
      assert.equal(correcting.status,"running","A known rejected reply abandoned the team");
      assert.equal(correcting.tasks.length,2,"Invalid delegated tasks were applied");
      assert.ok(!fs.existsSync(path.join(project,"rejected-format.txt")),"Invalid file changes were applied");
      await page.waitForFunction(()=>document.querySelector('#theBigChatActivity')?.textContent.includes('Correcting'),null,{timeout:30_000});
      await page.screenshot({path:path.join(coordination,"automatic-correction.png")});
      fs.writeFileSync(path.join(coordination,"release-correction"),"continue corrected useful work");
      console.log("pass  malformed lead action is rejected without effects, visible automatic correction keeps the same team running");
    }
    if(toolFault) {
      await until(()=>fs.existsSync(path.join(coordination,"entered-tool-correction")),"automatic tool argument correction");
      const correcting=await goalFor(page,gameChat);
      assert.equal(correcting.status,"running","A rejected tool argument abandoned the swarm");
      assert.ok(!fs.existsSync(path.join(project,"arena.js")),"Implementation preceded source inspection");
      await transcriptContains(page,["TEAM-A-READ"]);
      const failedTool=page.locator('#theBigChatSaid .chat-tool-activity-row[data-tool-name="read_file"][data-tool-status="failed"]').first();
      await failedTool.waitFor({state:"visible",timeout:30_000});
      await failedTool.locator('summary').click();
      const failedDetails=await failedTool.locator('.chat-tool-body').innerText();
      assert.match(failedDetails,/Input.*"end_line": 1.*"start_line": 2.*Error\s+read_file end_line must be at least start_line/s,
        "Actual rejected tool arguments and their diagnostic must be inspectable in the conversation");
      assert.equal(await page.locator('#theBigChatSaid .phase-agent_progress').count()>0,true,
        "Provider-shared progress must be visible in the chat");
      await page.screenshot({path:path.join(coordination,"tool-correction.png")});
      fs.writeFileSync(path.join(coordination,"release-tool-correction"),"continue corrected read");
      await until(()=>fs.existsSync(path.join(coordination,"tool-recovery-verified.json")),"same-ID correction and exact paginated reads");
      await page.locator('#theBigChatSaid .chat-tool-activity-row[data-tool-name="read_file"][data-tool-status="finished"]').first()
        .waitFor({state:"visible",timeout:30_000});
      console.log("pass  invalid tool arguments correct automatically; repeated call IDs and bounded UTF-8 pages preserve the complete source");
    }
    await until(()=>fs.existsSync(path.join(coordination,"entered-stale")),"B consuming A's first turn");
    const initial=await goalFor(page,gameChat);
    await transcriptContains(page,["TEAM-A-BASE"]);
    await page.locator("#theBigChatBox").fill(STEER);
    await page.locator("#theBigChatSend").click();
    await transcriptContains(page,[STEER]);
    const steered=await goalFor(page,gameChat,goal=>Number(goal.objective_epoch)>Number(initial.objective_epoch));
    assert.equal(steered.goal_id,initial.goal_id,"Ordinary Send created another goal instead of steering the existing one");
    fs.writeFileSync(path.join(coordination,"release-stale"),"release");
    await until(()=>fs.existsSync(path.join(coordination,"entered-pause")),"A reviewing B's steered implementation");
    const middle=await transcriptContains(page,["TEAM-A-BASE","TEAM-B-STEERED",STEER]);
    assert.ok(!middle.includes("OBSOLETE-BLUE-REPLY"),"A superseded reply was published as accepted agent work");
    assert.ok(!middle.includes("REJECTED-PROTOCOL-REPLY"),"Invalid protocol content was published as agent speech");
    assert.ok(!fs.existsSync(path.join(project,"obsolete-blue.txt")),"A superseded reply changed project files");
    console.log("pass  A's work reaches B; ordinary Send steers the same goal and invalidates B's obsolete in-flight changes");

    await page.locator("#theBigChatStop").click();
    await goalFor(page,gameChat,goal=>goal.status==="paused");
    fs.writeFileSync(path.join(coordination,"release-pause"),"release");
    await goalFor(page,gameChat,goal=>goal.status==="paused" && goal.tasks.every(task=>task.state!=="running"));
    await running.app.close();running=null;
    running=await launch(exe,profile,project,environment);page=running.page;
    await openChat(page,gameChat);
    await transcriptContains(page,[GAME_GOAL,STEER,"TEAM-A-BASE","TEAM-B-STEERED"]);
    assert.equal((await goalFor(page,gameChat)).status,"paused","Restart silently resumed the paused team");
    await until(async ()=>(await page.locator("#theBigChatStop").textContent()).includes("Resume"),"restored inline Resume team control");
    await page.locator("#theBigChatStop").click();
    const completed=await goalFor(page,gameChat,goal=>goal.status==="complete");
    const dialogue=await transcriptContains(page,["TEAM-A-BASE","TEAM-B-STEERED","TEAM-A-REVIEW"]);
    assert.ok(dialogue.indexOf("TEAM-A-BASE")<dialogue.indexOf("TEAM-B-STEERED"));
    assert.ok(dialogue.indexOf("TEAM-B-STEERED")<dialogue.indexOf("TEAM-A-REVIEW"));
    assert.equal(completed.goal_id,initial.goal_id);
    assert.equal((await goals(page)).filter(goal=>goal.conversation_id===gameChat).length,1);
    assert.equal(completed.verification.status,"passed");
    await captureReadableConversation(page,completed.goal_id,["TEAM-A-REVIEW","TEAM-B-FINAL"],
      path.join(coordination,"game-conversation.png"));
    console.log("pass  A → B → A conversation and steering survive pause, app restart, resume, and verified game completion");

    // The second selected repository has real game inputs but no configured
    // verifier and no inherited authority from the first project's settings.
    // Its Python check is discoverable only, so approval must be explicit.
    for(const name of ["arena.js","arena.html","arena-config.json","server.cjs","test_acceptance.py","test_runtime.cjs"])
      fs.copyFileSync(path.join(project,name),path.join(testProject,name));
    await openChat(page,testChat);
    const admissionDelay=await delayAdmissionRefreshUntilCollapse(page,testChat);
    await startGoal(page,TEST_GOAL);
    const waitingForChecks=await goalFor(page,testChat,goal=>goal.status==="paused"
      &&goal.verification?.basis==="discovered_command_approval_required");
    await admissionDelay.remove();
    assert.ok(admissionDelay.observed.interceptedRequests>0 && admissionDelay.observed.collapsedBeforeResponse,
      "The packaged admission did not exercise its real collapse-before-response boundary");
    fs.writeFileSync(path.join(coordination,"admission-collapse.json"),JSON.stringify(admissionDelay.observed,null,2));
    await page.waitForFunction(()=>{
      const send=document.querySelector("#theBigChatSend"),resume=document.querySelector("#theBigChatStop");
      return send?.textContent==="Send to team" && !send.disabled
        && resume?.textContent==="Resume team" && !resume.disabled;
    },null,{timeout:30_000});
    console.log("pass  the real activity collapses before the admission response finishes and still releases Send and Resume");
    assert.equal(waitingForChecks.verification.status,"unavailable");
    assert.equal(waitingForChecks.verification.commands.length,0,"Unapproved discovered checks executed");
    assert.equal(waitingForChecks.verification_contract.approved_test_command_digest,"");
    assert.deepEqual(waitingForChecks.verification_contract.test_commands,[]);
    const callsBeforeApproval=waitingForChecks.budget.provider_calls;
    const approved=await approveChatChecks(page,waitingForChecks);
    assert.equal(approved.budget.provider_calls,callsBeforeApproval,"Approval dispatched another provider turn");
    const selectedProject=await page.evaluate(async projectId=>(await request("/api/swarm")).board.projects
      .find(project=>project.id===projectId),TEST_PROJECT_ID);
    assert.equal(selectedProject.approved_test_command_digest,"","A chat approval silently approved the project board");
    assert.deepEqual((await goalFor(page,gameChat)).verification_contract,completed.verification_contract,
      "Approving the selected chat changed another chat's command authority");
    await openChat(page,testChat);
    await until(async ()=>(await page.locator("#theBigChatStop").textContent()).includes("Resume"),
      "Resume team after this chat's command approval",30_000);
    const beforeResume=await goalFor(page,testChat);
    assert.equal(beforeResume.status,"paused");
    assert.equal(beforeResume.verification.status,"not_run");
    await page.locator("#theBigChatStop").click();
    const testsComplete=await goalFor(page,testChat,goal=>goal.status==="complete");
    assert.equal(testsComplete.goal_id,waitingForChecks.goal_id,"Check approval replaced the goal");
    assert.equal(testsComplete.budget.provider_calls,callsBeforeApproval,"Verification recovery repeated settled provider work");
    assert.ok(testsComplete.verification_contract.approved_test_command_digest,"Resume did not adopt the explicit command approval");
    await transcriptContains(page,["TEST-A-UNIT","TEST-B-INTEGRATION","TEST-A-REVIEW"]);
    assert.equal(testsComplete.verification.status,"passed");
    await captureReadableConversation(page,testsComplete.goal_id,["TEST-A-REVIEW","TEST-B-FINAL"],
      path.join(coordination,"tests-conversation.png"));
    console.log("pass  the real chat-specific command button previews and approves only its exact paused goal; Resume verifies without repeating agent work");
    // Keep the separate board-level approval control covered after publication;
    // the completed chat above must succeed using only its own scoped approval.
    await approveDiscoveredChecks(page,TEST_PROJECT_ID);
    await openChat(page,testChat);
    console.log("pass  the project board's separate explicit command approval still works after publication");
    await verifyArtifacts(testProject,coordination,bundled,environment);
    await running.app.close();running=null;
    running=await launch(exe,profile,project,environment);page=running.page;
    await openChat(page,testChat);
    await transcriptContains(page,[TEST_GOAL,"TEST-A-UNIT","TEST-B-INTEGRATION","TEST-A-REVIEW"]);
    assert.equal((await goalFor(page,testChat)).status,"complete");
    await openChat(page,gameChat);
    await transcriptContains(page,[GAME_GOAL,STEER,"TEAM-A-BASE","TEAM-B-STEERED","TEAM-A-REVIEW"]);
    fs.writeFileSync(path.join(coordination,"final-goals.json"),JSON.stringify(await goals(page),null,2));
    console.log("pass  both saved chats retain their own goals, agent dialogue, steering, and verified outcomes after restart");
    console.log("TEAM_CHAT_PACKAGED_ACCEPTANCE_PASS");passed=true;
  } catch(error) {
    if(running?.page) {
      await running.page.screenshot({path:path.join(coordination,"failure.png")}).catch(()=>{});
      await running.page.evaluate(() => ({
        goal: longGoal, goals: longGoals, watching: longGoalWatching,
        selectedAgent: theBigOne, chats: swarmChats.map(held => ({agent:held.agent,conversation:held.conversation,
          saidFor:held.saidFor,turns:held.said})),
        visibleTurns: [...document.querySelectorAll("#theBigChatSaid > li")].map(one => ({
          classes:one.className,goalId:one.dataset.goalId,goalStatus:one.dataset.goalStatus,text:one.textContent})),
      })).then(value => fs.writeFileSync(path.join(coordination,"failure-renderer.json"),JSON.stringify(value,null,2))).catch(()=>{});
      try {
        const observed=await goals(running.page);
        fs.writeFileSync(path.join(coordination,"failure-goals.json"),JSON.stringify(observed,null,2));
        console.error("info  durable failure state: "+JSON.stringify(observed.map(goal=>({
          id:goal.goal_id,status:goal.status,note:goal.note,
          tasks:goal.tasks?.map(task=>({agent:task.assigned_agent_id,state:task.state,error:task.last_error}))}))));
      } catch(_diagnosticError) {}
    }
    throw error;
  } finally {
    for(const name of ["stale","pause","correction","tool-correction"])fs.writeFileSync(path.join(coordination,`release-${name}`),"cleanup release");
    if(running?.app)await running.app.close().catch(()=>{});
    if(passed && process.env.NEXUS_KEEP_TEAM_SMOKE!=="1") {
      assert.ok(path.resolve(root).startsWith(path.resolve(os.tmpdir())+path.sep));
      fs.rmSync(root,{recursive:true,force:true,maxRetries:10,retryDelay:200});
    } else console.log(`info  fixture and evidence retained at ${root}`);
  }
}

if(require.main===module)main().catch(error=>{console.error(error.stack || error);process.exitCode=1;});

module.exports={GAME_ENGINE,GAME_HTML,SERVER,UNIT_TESTS,API_TESTS,E2E_TESTS,PROVIDER,waitForPanelRuntime};
