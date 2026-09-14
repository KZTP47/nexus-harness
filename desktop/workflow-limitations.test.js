"use strict";
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const crypto = require('node:crypto');
const source = fs.readFileSync(path.join(__dirname, '../src/our_harness/ui/app.js'), 'utf8');
function section(first, last) {
  const a = source.indexOf(first), b = source.indexOf(last, a);
  assert.ok(a >= 0 && b > a); return source.slice(a, b);
}
function mission() {
  const nodes = new Map(), calls = [], errors = [];
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, {value:'  Exact message  ', listeners:{},
      addEventListener(event, callback) {this.listeners[event] = callback;}, scrollIntoView() {}, focus() {}});
    return nodes.get(id);
  };
  const state = {drift:false, hook:null, refreshHook:null, receiptHook:null};
  const context = vm.createContext({
    longGoal:{goal_id:'arbitrary-goal',status:'paused',tasks:[{id:'arbitrary-task',assigned_agent_id:'intended-agent'}]},
    selectedMissionTaskId:'arbitrary-task', $:node, console, crypto:crypto.webcrypto, TextEncoder,
    missionProviderSetupChanged:()=>state.drift, showError:message=>errors.push(message),
    refreshLongGoals:async()=>{if(state.refreshHook) await state.refreshHook();},
    request:async(url, options)=>{
      const body=JSON.parse(options.body); calls.push(body);
      if(state.hook) await state.hook(body);
      const receipt={schema_version:1,accepted:true,goal_id:body.goal_id,
        task_id:body.payload.task_id,agent_id:body.payload.agent_id,
        request_id:body.payload.request_id,submission_sha256:crypto.createHash('sha256').update(JSON.stringify({
          agent_id:body.payload.agent_id,goal_id:body.goal_id,task_id:body.payload.task_id,text:body.payload.text})).digest('hex')};
      if(state.receiptHook) state.receiptHook(receipt);
      return {goal:{goal_id:body.goal_id},directed_message_receipt:receipt};
    },
  });
  vm.runInContext(section('const missionMessageSubmissions =', 'async function cancelLongGoal'),context);
  vm.runInContext(section('  $("missionSteerSend").addEventListener', '  $("missionRequestReview").addEventListener'),context);
  return {state,context,calls,errors,box:node('missionSteer'),
    send:action=>node(action==='message'?'missionMessageAgent':'missionSteerSend').listeners.click()};
}
for(const action of ['steer','message']) {
  for(const failure of ['HTTP rejection','provider drift','wrong goal']) {
    test(`${action}: ${failure} keeps the exact draft`,async()=>{
      const f=mission();
      if(failure==='HTTP rejection') f.state.hook=async()=>{throw Error('Rejected');};
      if(failure==='provider drift') f.state.drift=true;
      if(failure==='wrong goal') f.context.request=async()=>({goal:{goal_id:'other-goal'}});
      await f.send(action);assert.equal(f.box.value,'  Exact message  ');
    });
  }
  for(const change of ['none','new text','goal switch','task switch','refresh failure']) {
    test(`${action}: acceptance clears only original goal/draft (${change})`,async()=>{
      const f=mission();
      f.state.hook=async()=>{
        if(change==='new text') f.box.value='Independent new draft';
        if(change==='goal switch') f.context.longGoal={goal_id:'different-goal'};
        if(change==='task switch') f.context.selectedMissionTaskId='different-task';
      };
      if(change==='refresh failure') f.state.refreshHook=async()=>{throw Error('Refresh failed');};
      await f.send(action);
      assert.equal(f.box.value,['none','refresh failure'].includes(change)?'':change==='new text'?'Independent new draft':'  Exact message  ');
      assert.equal(f.calls[0].goal_id,'arbitrary-goal');
      if(change==='refresh failure') assert.match(f.errors.at(-1),/action was saved/);
    });
  }
}
for(const field of ['schema_version','accepted','goal_id','task_id','agent_id','request_id','submission_sha256']) {
  test(`message: invalid ${field} receipt keeps draft`,async()=>{
    const f=mission();f.state.receiptHook=value=>{value[field]=null;};await f.send('message');
    assert.equal(f.box.value,'  Exact message  ');
  });
}
test('message: uncertain retry keeps original recipient/envelope, then a deliberate resend gets new identity',async()=>{
  const f=mission();f.state.hook=async()=>{throw Error('Response lost');};await f.send('message');
  f.context.longGoal.tasks[0].assigned_agent_id='new-agent';f.state.hook=null;
  await f.send('message');assert.deepEqual(f.calls[1],f.calls[0]);assert.equal(f.box.value,'');
  f.box.value='  Exact message  ';await f.send('message');
  assert.notEqual(f.calls[2].payload.request_id,f.calls[1].payload.request_id);
  assert.equal(f.calls[2].payload.agent_id,'new-agent');
});
test('message: duplicate click while acceptance is pending sends once',async()=>{
  const f=mission();let release;f.state.hook=()=>new Promise(resolve=>{release=resolve;});
  const first=f.send('message');await f.send('message');
  while(!release) await new Promise(setImmediate);
  assert.equal(f.calls.length,1);
  release();await first;
});
test('message: a different valid digest is rejected and edited/restored uncertainty reuses original identity',async()=>{
  const f=mission();f.state.receiptHook=value=>{value.submission_sha256='f'.repeat(64);};
  await f.send('message');assert.equal(f.box.value,'  Exact message  ');
  f.box.value='Different independent input';await f.send('message');
  f.box.value='  Exact message  ';f.state.receiptHook=null;await f.send('message');
  assert.deepEqual(f.calls[2],f.calls[0]);
  assert.notEqual(f.calls[1].payload.request_id,f.calls[0].payload.request_id);
  assert.equal(f.box.value,'');
});
for (const proof of ['exact', 'wrong digest', 'wrong request', 'none']) {
  test(`message: only exact definitive rejection retires a stale recipient (${proof})`,async()=>{
    const f=mission();
    f.state.hook=async body=>{
      const rejection={schema_version:1,accepted:false,goal_id:body.goal_id,
        task_id:body.payload.task_id,agent_id:body.payload.agent_id,request_id:body.payload.request_id,
        submission_sha256:crypto.createHash('sha256').update(JSON.stringify({agent_id:body.payload.agent_id,
          goal_id:body.goal_id,task_id:body.payload.task_id,text:body.payload.text})).digest('hex')};
      if(proof==='wrong digest') rejection.submission_sha256='f'.repeat(64);
      if(proof==='wrong request') rejection.request_id='other';
      const error=new Error('Recipient no longer owns task');
      if(proof!=='none') error.directedMessageRejection=rejection;
      throw error;
    };
    f.state.refreshHook=async()=>{f.context.longGoal.tasks[0].assigned_agent_id='current-agent';};
    await f.send('message');assert.equal(f.box.value,'  Exact message  ');
    f.state.hook=null;await f.send('message');
    if(proof==='exact') {
      assert.equal(f.calls[1].payload.agent_id,'current-agent');
      assert.notEqual(f.calls[1].payload.request_id,f.calls[0].payload.request_id);
    } else assert.deepEqual(f.calls[1],f.calls[0]);
  });
}
test('request exposes rejection proof only from the directed-control endpoint',async()=>{
  const proof={schema_version:1,accepted:false};
  const context=vm.createContext({token:'fixture',fetch:async()=>({ok:false,status:409,json:async()=>({error:'rejected',directed_message_rejection:proof})})});
  vm.runInContext(section('async function request(path','// Asking somebody for one line of text.'),context);
  for(const endpoint of ['/api/long-horizon/control','/api/other']) {
    await assert.rejects(context.request(endpoint),error=>{
      assert.equal(Boolean(error.directedMessageRejection),endpoint==='/api/long-horizon/control');
      return true;
    });
  }
});
test('Fork is available for settled sources while other terminal mutations remain blocked',()=>{
  const code=section('  const terminal = !longGoal','  const tasks = $("missionTasks")');
  for(const status of ['paused','failed','complete','cancelled','running','cancelling']) {
    for(const guard of ['none','provider','decision','scheduler']) {
      const nodes={};const context={longGoal:{status,pending_interrupts:guard==='decision'?[{}]:[],scheduler_live:guard==='scheduler'},
        providerSetupChanged:guard==='provider',$:id=>nodes[id]||(nodes[id]={})};
      vm.runInNewContext(code,context);
      assert.equal(nodes.missionFork.disabled,guard!=='none'||['running','cancelling'].includes(status),`${status}/${guard}`);
      if(['complete','cancelled'].includes(status)) assert.equal(nodes.missionCriteriaSave.disabled,true);
    }
  }
});

test('both Work controls permit ready singletons while retaining readiness and recovery gates',()=>{
  for(const view of ['compact','expanded']) {
    for(const blocked of ['none','busy','unready','recovery','binding','no project']) {
      let disabled;
      const context={lone:true,waiting:blocked==='busy',agent:{ready:blocked!=='unready'},chatAgent:{ready:blocked!=='unready'},
        theBigOne:'single-agent',conversation:{project:blocked==='no project'?'':'project'},
        bindingProblem:blocked==='binding',bindingWords:'',recoveryAuthorityWords:'',directLongGoalRecoveryInventoryReady:true,
        directLongGoalRecoveryError:'',recovery:blocked==='recovery'?{}:null,
        workRecoveryFor:()=>blocked==='recovery'?{}:null,$:()=>({}),card:{dataset:{agent:'single-agent'},querySelector:()=>({})},
        setSwarmProjectWorkControl:(node,value)=>{disabled=value;}};
      const code=view==='compact'
        ?section('  const workDisabled = waiting','  const startAgain =')
        :section('    if ($("theBigChatWork")) {','    if ($("theBigChatProject")) {');
      vm.runInNewContext(code,context);
      assert.equal(disabled,blocked!=='none',`${view}/${blocked}`);
    }
  }
});

test('attachment omission notice accepts only the supported contract with actual omissions',()=>{
  const context=vm.createContext({});
  vm.runInContext(section('function attachmentContextNotice','function appendChatDeliveryNotice'),context);
  const valid={schema_version:1,contract_fingerprint:'c0677eb7cd50f2cb1b1805643e96fd54990c4be8bcc47f439ea1d2f2b91605d4',
    omitted_files:2,notice:'Two earlier files were not included; reattach to prioritize.'};
  assert.equal(context.attachmentContextNotice(valid),valid.notice);
  for(const changes of [{schema_version:2},{contract_fingerprint:'unknown'},{omitted_files:0},{omitted_files:-1},{omitted_files:'2'},{notice:null}]) {
    assert.equal(context.attachmentContextNotice({...valid,...changes}),'');
  }
  assert.equal(context.withAttachmentContextNotice('Answered.',{attachment_context:valid}),'Answered. '+valid.notice);
});
