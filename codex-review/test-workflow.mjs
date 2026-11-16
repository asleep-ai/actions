import {readFileSync} from 'node:fs';
import {test} from 'node:test';
import assert from 'node:assert/strict';
const workflow = readFileSync(new URL('../.github/workflows/code-review.yml', import.meta.url), 'utf8');
const AsyncFunction = Object.getPrototypeOf(async function(){}).constructor;
function script(name) {
  const text = workflow.split(`// BEGIN ${name}\n`)[1].split(`// END ${name}`)[0];
  return new AsyncFunction('context','github','core','process',text);
}
const sha='a'.repeat(40), base='b'.repeat(40);
function fixture() {
  const pr={number:1,state:'open',draft:false,head:{sha,repo:{full_name:'owner/repo'}},base:{sha:base}};
  const outputs={},calls=[];
  const context={eventName:'pull_request',actor:'writer',repo:{owner:'owner',repo:'repo'},payload:{action:'opened',pull_request:pr}};
  const github={rest:{repos:{getCollaboratorPermissionLevel:async()=>({data:{permission:'write'}})},pulls:{get:async()=>({data:pr})},issues:{listComments:()=>{},createComment:async args=>calls.push(args),updateComment:async args=>calls.push(args)}},paginate:async()=>[]};
  const core={setOutput:(k,v)=>outputs[k]=v};
  const process={env:{REVIEW_MODEL:'test-model',REVIEW_HEAD:sha,REVIEW_BASE:base,REVIEW_NUMBER:'1',REVIEW_MESSAGE:'review'}};
  return {context,github,core,process,outputs,calls,pr};
}
async function run(name,f){await script(name)(f.context,f.github,f.core,f.process);}
test('allows internal writable actor PR with immutable revisions',async()=>{const f=fixture();await run('GATE',f);assert.equal(f.outputs.allowed,'true');assert.equal(f.outputs.head,sha);});
test('rejects forks, drafts, bots and privileged event types',async()=>{
  for(const change of [f=>f.context.eventName='pull_request_target',f=>f.pr.head.repo.full_name='fork/repo',f=>f.pr.draft=true,f=>f.context.actor='dependabot[bot]',f=>f.context.payload.action='labeled']){const f=fixture();change(f);await run('GATE',f);assert.equal(f.outputs.allowed,'false');}
});
test('requires both original and rerun actor write access',async()=>{const f=fixture();f.process.env.GITHUB_TRIGGERING_ACTOR='outsider';f.github.rest.repos.getCollaboratorPermissionLevel=async({username})=>({data:{permission:username==='writer'?'write':'read'}});await run('GATE',f);assert.equal(f.outputs.allowed,'false');});
test('stale revisions never receive API key job',async()=>{const f=fixture();f.github.rest.pulls.get=async()=>({data:{...f.pr,head:{...f.pr.head,sha:'c'.repeat(40)}}});await run('GATE',f);assert.equal(f.outputs.allowed,'false');});
test('invalid model fails before spending',async()=>{const f=fixture();f.process.env.REVIEW_MODEL='';await assert.rejects(run('GATE',f));});
test('publishing treats output as data and neutralizes mentions',async()=>{const f=fixture();f.process.env.REVIEW_MESSAGE='@everyone ${evil} ::error::';await run('PUBLISH',f);assert.equal(f.calls.length,1);assert.ok(f.calls[0].body.includes('@\u200beveryone'));assert.ok(f.calls[0].body.includes('${evil}'));});
test('stale reviews are never posted',async()=>{const f=fixture();f.process.env.REVIEW_HEAD='c'.repeat(40);await run('PUBLISH',f);assert.equal(f.calls.length,0);});
test('only updates own marker comments and caps size',async()=>{const f=fixture();f.process.env.REVIEW_MESSAGE='x'.repeat(70000);f.github.paginate=async()=>[{id:99,user:{login:'github-actions[bot]'},body:'<!-- asleep-codex-agent-review -->old'}];await run('PUBLISH',f);assert.equal(f.calls[0].comment_id,99);assert.ok(f.calls[0].body.length<60000);});
test('workflow trust boundaries and pinned dependencies',()=>{
  for(const [,ref] of workflow.matchAll(/uses: ([^\s]+)/g)) assert.match(ref,/@[a-f0-9]{40}$/);
  assert.ok(workflow.includes("permission-profile: ':read-only'"));assert.ok(workflow.includes('safety-strategy: drop-sudo'));
  assert.ok(workflow.includes('persist-credentials: false'));assert.ok(workflow.includes('project_doc_max_bytes = 0'));assert.ok(workflow.includes('trust_level = \"untrusted\"'));assert.ok(workflow.includes('codex-home: ${{ runner.temp }}'));
  assert.ok(!workflow.includes('self-hosted'));assert.ok(!workflow.includes('allow-users:'));assert.ok(!workflow.includes('secrets: inherit'));
  const review=workflow.split('\n  review:')[1].split('\n  publish:')[0];assert.ok(!review.includes(': write'));assert.equal((review.match(/- uses:/g)||[]).length,2);assert.ok(review.includes("steps.bound.outputs.message"));assert.ok(review.includes("python3 -I -"));assert.ok(review.includes("working-directory: ${{ runner.temp }}"));assert.ok(!review.includes("steps.codex.outputs.final-message"));
  const publish=workflow.split('\n  publish:')[1];assert.ok(!publish.includes('checkout@'));assert.ok(!publish.includes('secrets.'));assert.ok(!publish.includes('run:'));
});
