"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const test = require("node:test");
const source = fs.readFileSync(path.join(__dirname, "../src/our_harness/ui/app.js"), "utf8");
function section(start, end) {
  const from = source.indexOf(start), to = source.indexOf(end, from);
  assert.ok(from >= 0 && to > from, start);
  return source.slice(from, to);
}
function fixture() {
  const completed = {goal_id:"goal-current",request_id:"admission-current",conversation_id:"chat-current",
    project:{id:"project-current"},status:"complete",revision:8};
  const prompt = {who:"you",phase:"long_horizon_prompt",correlation:{schema_version:1,kind:"long_horizon_prompt",
    request_id:completed.request_id,chat_id:completed.conversation_id,project_id:completed.project.id}};
  const receipt = {who:"them",speaker_id:"nexus",phase:"long_horizon_status",text:"Persisted completion record",
    correlation:{schema_version:1,kind:"long_horizon_status",goal_id:completed.goal_id,
      request_id:completed.request_id,
      chat_id:completed.conversation_id,project_id:completed.project.id,goal_status:"complete",goal_revision:8}};
  const held = {agent:"builder",conversation:completed.conversation_id,saidFor:completed.conversation_id,
    conversations:[{id:completed.conversation_id,project:completed.project.id}],said:[prompt]};
  const f = {completed,prompt,receipt,held,reads:[],timers:new Map(),transcripts:[[prompt],[prompt],[prompt,receipt]],renders:[]};
  let timerId = 0;
  const context = vm.createContext({
    console,AbortController,swarmChats:[held],theBigOne:"builder",LONG_GOAL_SELECTED_KEY:"selected",
    swarmChatRevisions:new Map(),swarmConversationTranscriptControllers:new Map(),swarmChatLimits:new Map(),
    localStorage:{getItem:()=>"",setItem(){}},$:()=>({textContent:""}),
    window:{clearTimeout:id=>f.timers.delete(id),setTimeout:(callback,delay)=>{
      const id = ++timerId; f.timers.set(id,{callback,delay}); return id;
    }},
    activeConversationIdFor:()=>held.conversation,swarmChatKey:()=>`builder:${held.conversation}`,
    theSwarmAgent:id=>({id}),keptTranscriptFor:()=>held.saidFor === held.conversation ? held.said : [],
    missionSelectedGoalId:()=>completed.goal_id,renderMissionControl(){},
    renderTheChatThreadFor(){},renderTheBigChat:()=>f.renders.push([...held.said]),countWhatIsTypedTo(){},
    sayInTheChatFor(){},conversationReadWasCancelled:()=>false,
    async request(url) {
      f.reads.push(url);
      if (url === "/api/long-horizon/goals") return {goals:[completed]};
      if (url.startsWith("/api/long-horizon/goal?")) return {goal:completed};
      if (url.startsWith("/api/long-horizon/events?")) return {events:[],next:0};
      if (url.startsWith("/api/swarm/said?")) return {said:f.transcripts.shift() || [prompt,receipt]};
      throw new Error(url);
    },
  });
  vm.runInContext(section("let longGoals =", "async function readSwarmBoardRun")
    + section("function activeConversationFor", "function isLoneAgentChat")
    + section("const chatGoalRequests =", "async function openChatGoalDetails")
    + section("function nextSwarmChatRevision", "function cancelConversationReadLane")
    + section("async function refreshTheChatFor", "async function copyChatCode")
    + section("function keepWhatWasSaidTo", "function keepWhatWasSaidToRuntime")
    + section("function isNexusChatTurn", "function aChatTurnFace")
    + section("function normalizedLongHorizonCorrelation", "function uniqueChatCompletionTurns")
    + section("async function refreshLongGoalOriginChats", "function savedLongGoalComposer")
    + "\nfunction seedGoal(goal) { longGoals = [goal]; longGoal = goal; }", context);
  context.seedGoal({...completed,status:"running",revision:7});
  f.context = context;
  f.poll = async () => {
    const entry = [...f.timers.entries()][0];
    assert.ok(entry,"the completion delivery must schedule a retry");
    f.timers.delete(entry[0]);
    assert.equal(entry[1].delay,1200,"catch-up must retain the ordinary bounded polling cadence");
    await entry[1].callback();
  };
  return f;
}

test("a terminal snapshot keeps polling until the exact open chat receives its persisted completion", async () => {
  const f = fixture();
  await f.context.refreshLongGoals(true);
  assert.deepEqual(f.held.said,[f.prompt],"the renderer must not manufacture a completion from the goal snapshot");
  assert.equal(f.timers.size,1,"terminal state must not abandon a delayed transcript delivery");
  await f.poll();
  assert.equal(f.held.said.at(-1),f.receipt);
  assert.equal(f.timers.size,0,"a delivered persisted receipt must stop polling");
});

test("closing or switching the exact chat stops completion delivery watches", async () => {
  for (const operation of ["close","switch"]) {
    const f = fixture();
    await f.context.refreshLongGoals(true);
    if (operation === "close") f.context.swarmChats.length=0;
    else f.held.conversation="another-chat";
    const reads = f.reads.length;
    await f.poll();
    assert.equal(f.timers.size,0);
    assert.equal(f.reads.length,reads,"a no-longer-visible chat must not trigger another catch-up request");
  }
});

test("another goal's completion cannot settle this admission and a reset transcript does not restart old polling", () => {
  const f = fixture();
  f.context.seedGoal(f.completed);
  f.held.said.push({...f.receipt,correlation:{...f.receipt.correlation,goal_id:"older-goal"}});
  assert.equal(f.context.anyLongGoalNeedsWatching(),true);
  f.held.said=[{...f.prompt,correlation:{...f.prompt.correlation,request_id:"newer-admission"}}];
  assert.equal(f.context.anyLongGoalNeedsWatching(),false);
});

test("a waiting decision keeps polling through scheduler release then settles without depending on a stale PID", async () => {
  const f = fixture();
  Object.assign(f.completed, {status:"waiting_for_user",scheduler_live:true,worker:{pid:1234},
    decision_reconsideration:{available:false,pending_ids:["decision"]}});
  await f.context.refreshLongGoals(true);
  assert.equal(f.timers.size,1);
  Object.assign(f.completed, {revision:9,scheduler_live:false,
    decision_reconsideration:{available:true,pending_ids:["decision"]}});
  await f.poll();
  assert.equal(f.timers.size,0,"the engine's released lease must win over a stale PID value");
  assert.equal(f.completed.decision_reconsideration.available,true);
});
