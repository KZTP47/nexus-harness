"use strict";

// Real packaged Electron, native confirmation, authenticated backend, persisted
// chat/goal and two deterministic CLI providers. No live accounts or user data.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const childProcess = require("node:child_process");
const {_electron: electron} = require("playwright-core");
const root = fs.mkdtempSync(path.join(os.tmpdir(), "nexus-reconnect-desktop-"));
const executable = path.resolve(process.argv[2]);
const hold = process.argv[3] || "";
if (hold) assert.ok(!fs.existsSync(hold) && !fs.existsSync(hold+'.release'), 'Use a fresh hold marker for each deployment survival run');
const control = path.join(root, "Portable control project Å");
const work = path.join(root, "Saved team work");
const env = {};
for (const [key,value] of Object.entries(process.env)) {
  if (["SYSTEMROOT","WINDIR","PATH","PATHEXT","COMSPEC","SYSTEMDRIVE","OS"].includes(key.toUpperCase())) env[key]=value;
}
for (const [key,folder] of Object.entries({APPDATA:"Roaming",LOCALAPPDATA:"Local",HOME:"Home",USERPROFILE:"Home",TEMP:"Temp",TMP:"Temp"})) {
  env[key]=path.join(root,folder);fs.mkdirSync(env[key],{recursive:true});
}
fs.mkdirSync(control);fs.mkdirSync(work);
const python = path.join(path.dirname(executable), "resources/runtime/python.exe");
const source = path.join(path.dirname(executable), "resources/harness/src");
const provider = path.join(root,"provider.py");
fs.writeFileSync(provider, String.raw`import json,sys
from pathlib import Path
request=json.load(sys.stdin)
route=sys.argv[1]
context=str(request.get('dynamic_context',''))+'\n'+'\n'.join(str(m.get('content','')) for m in request.get('messages',[]))
if route=='second': assert 'FIRST-RESUMED' in context, 'peer did not receive the resumed answer'
summary='FIRST-RESUMED: I checked the saved work; teammate, please verify it.' if route=='first' else 'SECOND-RESUMED: I verified the preserved checkpoint and agree.'
action={'action':'complete','summary':summary,'evidence':['verified-no-change'],'risk':'low','changes':[],
        'needs_files':[],'tool_calls':[],'tasks':[],'handoff_agent_id':'','questions':[],
        'criteria_evidence':[{'criterion':c,'evidence_refs':['verified-no-change']} for c in [
          'Original objective is satisfied','Every required task is complete','Configured deterministic verification passes']]}
print(json.dumps({'text':json.dumps(action),'finish_reason':'stop'}))
`);
fs.writeFileSync(path.join(work,"checkpoint.txt"),"saved before reconnect");
fs.writeFileSync(path.join(work,"test_saved.py"),String.raw`import unittest
from pathlib import Path
class SavedWork(unittest.TestCase):
    def test_retained_checkpoint(self):
        self.assertEqual(Path('checkpoint.txt').read_text(), 'saved before reconnect')
`);
fs.mkdirSync(path.join(control,".harness"));
fs.writeFileSync(path.join(control,".harness/config.local.json"),JSON.stringify({schema_version:1,
  providers:{first:{kind:"local",model:"fixture-a",command:[python,provider,"first"],endpoint:"http://127.0.0.1:1"},
    second:{kind:"local",model:"fixture-b",command:[python,provider,"second"],endpoint:"http://127.0.0.1:2"}},
  project:{test_commands:[["python","-m","unittest","test_saved"]]},workflow:{require_review:false}}));
const seed = String.raw`
import json,sys
from pathlib import Path
from unittest import mock
sys.path.insert(0,sys.argv[1])
from our_harness import chat,swarm,swarm_chats,long_horizon
from our_harness.config import load_config,trust_project_local_config
from our_harness.providers.base import LocalProcessProvider
control,work=Path(sys.argv[2]),Path(sys.argv[3])
trust_project_local_config(control)
config=load_config(control)
board={'schema_version':1,'workspace_id':'workspace-'+'f'*32,'binding_schema_version':1,
 'agents':[{'id':a,'name':a.title(),'who':a,'ready':True,'at':{'x':x,'y':60}} for a,x in [('first',60),('second',350)]],
 'projects':[{'id':'saved-work','name':'Saved work','path':str(work),'is_there':True,'at':{'x':100,'y':300}}],
 'works_on':[{'agent':a,'project':'saved-work'} for a in ['first','second']],
 'talks_to':[{'one':'first','other':'second'}]}
board=swarm.save(board,config).to_dict()
for agent in board['agents']: agent['ready']=True
original=LocalProcessProvider.effective_dispatch_fingerprint
def previous(self):
    held=original(self)
    if self.settings.get('model')=='fixture-a': held['effective_dispatch_fingerprint_sha256']='a'*64
    return held
store=long_horizon.GoalStore(config)
with mock.patch.object(LocalProcessProvider,'effective_dispatch_fingerprint',previous):
    listing=swarm_chats.list_for_agent(config,board,'first')
    conversation=next(c for c in listing['chats'] if c['pair']==['first','second'])
    swarm_chats.activate(config,board,'first',conversation['id'])
    chat.keep_exchange(config,'first','SAVED-BEFORE-RECONNECT','RETAINED-ANSWER',filed_as=conversation['filed_as'])
    goal=store.create(board,'saved-work',['Verify the preserved checkpoint together'],'desktop-reconnect',
        lead_id='first',participant_ids=['first','second'],conversation_id=conversation['id'],policy={'agent_access_mode':'full'})
    store.control(goal['goal_id'],'pause')
print(json.dumps({'chat':conversation['id'],'goal':goal['goal_id']}))
`;
const seeded=childProcess.spawnSync(python,["-c",seed,source,control,work],{env,cwd:control,encoding:"utf8",timeout:60_000});
assert.equal(seeded.status,0,seeded.stderr||seeded.stdout);
const identity=JSON.parse(seeded.stdout.trim().split(/\r?\n/).at(-1));

async function until(read, description, timeout=45_000){
  const deadline=Date.now()+timeout;
  while(Date.now()<deadline){
    if(await read())return;
    await new Promise(resolve=>setTimeout(resolve,200));
  }
  throw Error('Timed out waiting for '+description);
}

async function main(){
  console.log(JSON.stringify({stage:"launch",root,executable,identity}));
  const app=await electron.launch({executablePath:executable,args:[`--user-data-dir=${path.join(root,"Electron")}`,"--project",control],env,timeout:120_000});
  const processHandle=app.process();
  let expectedClose=false;
  processHandle.on("exit",(code,signal)=>console.log(JSON.stringify({stage:"exit",code,signal,expectedClose})));
  try{
    const page=await app.firstWindow({timeout:120_000});
    await app.evaluate(({BrowserWindow})=>BrowserWindow.getAllWindows().forEach(window=>window.minimize()));
    page.on("pageerror",error=>console.error("renderer: "+String(error)));
    await page.waitForFunction(()=>location.protocol==='http:' && typeof activeConversationFor==='function',null,{timeout:120_000});
    await page.locator('[data-view="swarm"]').click();
    await page.locator('.swarm-box[data-id="first"] [data-does="chat"]').click();
    await page.getByRole('button',{name:'Open full Nexus chat',exact:true}).click();
    const history=page.locator('#theBigChatHistoryToggle');
    if(await history.getAttribute('aria-expanded')!=='true')await history.click();
    await page.locator(`#theBigChatConversationList [data-conversation-action="pick"][data-chat-id="${identity.chat}"]`).click();
    await page.waitForFunction(chat=>activeConversationFor('first')?.id===chat && !swarmChatIsHydrating('first'),identity.chat);
    await page.getByText('RETAINED-ANSWER',{exact:false}).first().waitFor();
    const goal=()=>page.evaluate(async id=>(await request('/api/long-horizon/goal?id='+id)).goal,identity.goal);
    const before=await goal();
    assert.equal(before.provider_setup_changed,true);
    let confirmed=0;
    page.on('dialog',async dialog=>{
      console.log(JSON.stringify({stage:'native-dialog',type:dialog.type(),message:dialog.message()}));
      if(confirmed){await dialog.dismiss();return;}
      confirmed++;await dialog.accept();
    });
    for(let attempt=0;attempt<3 && !confirmed;attempt++){
      await page.locator('#theBigChatTeamGoal').getByRole('button',{name:'Reconnect saved chat',exact:true}).click();
      await until(()=>page.evaluate(()=>!swarmConversationSwitching.has('first')),'reconnect request to finish');
      if(!confirmed)await new Promise(resolve=>setTimeout(resolve,500));
    }
    await until(async()=>!(await goal()).provider_setup_changed,'reconnected goal binding');
    assert.equal(confirmed,1,'Real Electron did not deliver its native confirmation');
    assert.equal(processHandle.exitCode,null,'Reconnect exited the application');
    assert.equal(await page.locator('#theBigChat').isVisible(),true);
    const connected=await goal();
    assert.equal(connected.goal_id,before.goal_id);
    assert.deepEqual(connected.budget,before.budget);
    assert.equal(connected.status,'paused');
    await page.locator('#theBigChat').getByRole('button', { name: 'Resume team', exact: true }).click();
    await until(async()=>(await goal()).status==='complete','resumed team to complete',120_000);
    await page.getByText('SECOND-RESUMED:',{exact:false}).first().waitFor({timeout:30_000});
    assert.equal(fs.readFileSync(path.join(work,'checkpoint.txt'),'utf8'),'saved before reconnect');
    assert.equal((await goal()).conversation_id,identity.chat);
    console.log('PASS: native reconnect keeps Electron open and same saved team resumes to completion');
    await page.screenshot({path:path.join(root,'resumed-desktop.png')});
    if(hold){
      fs.writeFileSync(hold,JSON.stringify({root,executable,pid:processHandle.pid,identity}));
      console.log('Holding the completed chat open through the deployment gate: '+hold);
      const deadline=Date.now()+600_000;
      while(!fs.existsSync(hold+'.release')){
        if(processHandle.exitCode!==null)throw Error('The deployment terminated the open desktop');
        if(Date.now()>deadline)throw Error('Deployment survival check timed out');
        await new Promise(resolve=>setTimeout(resolve,200));
      }
      assert.equal(processHandle.exitCode,null);
      assert.equal(await page.locator('#theBigChat').isVisible(),true);
      assert.equal((await goal()).status,'complete');
      await page.screenshot({path:path.join(root,'survived-rebuild.png')});
      console.log('PASS: actual desktop rebuild left the app and saved conversation alive');
    }
  }catch(error){
    const page=app.windows()[0];
    if(page){
      await page.screenshot({path:path.join(root,'failure.png')}).catch(()=>{});
      console.error(await page.locator('body').innerText().then(text=>text.slice(-5500)).catch(()=>''));
    }
    throw error;
  }finally{expectedClose=true;await app.close();}
}
main().catch(error=>{console.error(error.stack||error);process.exitCode=1;});
