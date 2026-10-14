// Original Loader, AgentLoop, tools, sandbox and job providers; only the model is scripted.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import {pathToFileURL} from 'node:url';
import {execFileSync} from 'node:child_process';

const sdkRoot = process.env.DSH_TEST_SDK_ROOT;
const scratch = fs.realpathSync(process.argv[2]);
const protectedRoot = fs.realpathSync(process.argv[3]);
const scenario = process.argv[4] ?? 'tools';
const url = name => pathToFileURL(path.join(sdkRoot, `@deepseek-ai/${name}/lib/index.js`)).href;
const canonical = value => JSON.stringify(Array.isArray(value) ? value.map(item => JSON.parse(canonical(item)))
  : value && typeof value === 'object' ? Object.fromEntries(Object.keys(value).sort()
    .map(key => [key, JSON.parse(canonical(value[key]))])) : value);
const {Context} = await import(url('cordis'));
const {Loader} = await import(url('cordis-plugin-loader'));
const {LlmAdapter, createUserMessage} = await import(url('dsh-llm'));
const {setSandboxMode} = await import(url('dsh-sandbox-policy'));
const ctx = new Context();
const workspace = path.join(scratch, 'repository');
const ledgerDir = path.join(scratch, 'ledger');
const skills = path.join(scratch, 'skills');
for (const directory of [workspace, ledgerDir, skills]) fs.mkdirSync(directory, {mode: 0o700});
const git = args => execFileSync('git', ['-C', workspace, '-c', 'core.hooksPath=/dev/null',
  '-c', 'commit.gpgsign=false', ...args], {encoding: 'utf8'});
git(['init', '--quiet', '--initial-branch=main']);
fs.writeFileSync(path.join(workspace, 'README.md'), 'Synthetic native tool repository.\n');
fs.writeFileSync(path.join(workspace, '.gitignore'), '__pycache__/\n');
git(['add', 'README.md', '.gitignore']);
git(['-c', 'user.name=Fixture User', '-c', 'user.email=fixture@example.invalid', 'commit', '--quiet', '-m', 'Synthetic baseline']);
fs.mkdirSync(path.join(skills, 'fixture-matt'));
fs.writeFileSync(path.join(skills, 'fixture-matt', 'SKILL.md'),
  '---\nname: fixture-matt\ndescription: Synthetic public workflow.\n---\nCheck actual tool results.\n');
const config = {ledger_dir: ledgerDir, service_ref: 'fixture:shared-host',
  host_lease: 'fixture-native-lease', agent_preset: 'fixture-managed-tools'};
ctx.provide('workflowHostInstance', {instanceId: config.host_lease});
const reader = path.resolve('ghost_hermes_pm/shared_source_reader.py');
const python = fs.realpathSync(process.env.HERMES_TEST_SESSION_PYTHON);
config.source_snapshot = {python, reader, python_sha256: crypto.createHash('sha256').update(fs.readFileSync(python)).digest('hex'),
  reader_sha256: crypto.createHash('sha256').update(fs.readFileSync(reader)).digest('hex')};
const names = ['ask_user_question', 'bash', 'job_kill', 'job_list', 'job_output', 'skill'];
const results = [];
const calls = [];
const jobs = [];
const passed = [];
const systemJobs = [];
const questions = [];
let script;
let issued = false;
class Model extends LlmAdapter {
  resolveModel(provider, model) {return {provider, id: model, name: model, context: {contextWindow: 16384}};}
  providerRetryPolicy() {return {mode: 'normal', maxRetries: 0, retryableCodes: [],
    initialDelayMs: 0, maxDelayMs: 0, jitterRatio: 0};}
  async *stream(options) {
    calls.push(options);
    if (script && !issued) {
      issued = true;
      yield {type: 'block-start', index: 0, blockType: 'tool-call'};
      yield {type: 'block-end', index: 0, block: {type: 'tool-call', id: 'fixture-call-' + calls.length,
        name: script.name, arguments: JSON.stringify(script.arguments)}};
    } else {
      yield {type: 'block-start', index: 0, blockType: 'text'};
      yield {type: 'block-end', index: 0, block: {type: 'text', text: 'Native tool result inspected.'}};
    }
    yield {type: 'usage', usage: {inputTokens: 12, outputTokens: 8}};
    yield {type: 'finish', reason: {kind: 'completed'}};
  }
}
await ctx.plugin(Loader, {baseUrl: pathToFileURL(sdkRoot + '/').href});
const loader = ctx.loader;
const common = ['dsh-session', 'dsh-session-projection', 'dsh-token-meter', 'dsh-system-prompt',
  'dsh-sandbox-policy', 'dsh-tools', 'dsh-llm', 'dsh-agent', 'dsh-agent-loop', 'dsh-fs-local',
  'dsh-working-directory', 'dsh-shell-env', 'dsh-sandbox-local', 'dsh-subprocess-local',
  'dsh-bash-sandbox', 'dsh-jobs-local', 'dsh-user-questions', 'dsh-user-approval', 'dsh-skill'];
const configs = {'dsh-sandbox-policy': {mode: 'workspace-write', workspaceRoot: workspace},
  'dsh-working-directory': {defaultDirectory: workspace}, 'dsh-user-approval': {policy: 'never'}};
const plugins = [
  {id: 'presentation', name: url('dsh-agent-tool-presentation'), config: {mode: 'native'}},
  {id: 'tool-bash', name: url('dsh-tool-bash'), config: {enableRunInBackground: true, promoteOnTimeout: true}},
  {id: 'tool-jobs', name: url('dsh-tool-jobs'), config: {completionDelivery: 'quiet'}},
  {id: 'tool-ask-user', name: url('dsh-tool-ask-user'), config: {mode: 'timed', timeout: -1}},
  {id: 'skill-filesystem', name: url('dsh-skill-filesystem'),
    config: {customSkillDirs: [skills], includeDefaultRoots: false, watch: false}},
  {id: 'tool-skill', name: url('dsh-tool-skill')},
];
const declaration = {id: config.agent_preset, plugins: [...plugins,
  {id: 'shared-hook', name: pathToFileURL(path.resolve('ghost_hermes_pm/shared_native_hook.mjs')).href, config}]};
const handles = [];
let remove;
try {
  for (const name of common) await loader.create({id: name, name: url(name), config: configs[name]});
  await loader.create({id: 'presets', name: url('dsh-agent-preset-registry'), config: {default: 'fixture-manual'}});
  await loader.await();
  for (const name of ['agentLoop', 'shell', 'jobs', 'sandboxPolicy', 'userQuestions', 'skills']) {
    assert.ok(ctx.get(name), `Original ${name} must be available.`);
  }
  ctx.llm.registerAdapter(['fixture-model'], new Model());
  ctx.on('tools/result', (exec, result) => results.push({exec, result}));
  // The human/UI answerer is an external boundary; the service and tool remain original.
  ctx.on('user-questions/request', request => {
    questions.push({owner: request.agent.id, questions: request.questions});
    return {answers: request.questions.map(question => ({id: question.id, selected: [], custom: 'Fixture answer.'}))};
  });
  ctx.jobs.events.subscribe({owners: 'all'}, event => jobs.push(event));
  remove = await ctx.agentPresets.register(declaration);
  const manualRemove = await ctx.agentPresets.register({id: 'fixture-manual', plugins});
  if (scenario === 'inherited') await loader.create({id: 'inherited-files', name: url('dsh-tool-fs')});
  const create = async (id, preset = config.agent_preset) => {
    const handle = await ctx.agents.create({sessionId: id, meta: {cwd: workspace, agentPreset: preset},
      agentOptions: {provider: 'fixture-model', model: 'fixture-default'},
      setup: async agentCtx => {await ctx.agentPresets.mount(agentCtx, preset);}});
    setSandboxMode(handle.agent.session, 'workspace-write');
    if (preset === config.agent_preset) fs.writeFileSync(path.join(ledgerDir,
      crypto.createHash('sha256').update(id).digest('hex') + '.json'), JSON.stringify({schema: 1, ...config,
      session_id: id, session_created_at: handle.agent.session.header.createdAt, generation: 'fixture-work-' + id,
      workspace, provider: 'fixture-model', model: 'fixture-route',
      budget: {max_wall_seconds: 30, max_model_requests: 80, max_reported_tokens: 300000,
        max_output_tokens_per_request: 8192},
      state: {started_at_ms: null, requests: 0, reported_tokens: 0, admissions: [], stop_reason: null}}), {mode: 0o600});
    handles.push(handle);
    return handle.agent;
  };
  const owned = await create('fixture-owned-tools');
  if (scenario !== 'inherited') assert.deepEqual(ctx.tools.schemas(owned).map(row => row.name).sort(), names);
  const run = async (agent, name, args) => {
    const before = results.length;
    script = {name, arguments: args}; issued = false;
    agent.followup(createUserMessage({content: [{type: 'text', text: 'Run one synthetic bounded operation.'}], source: {kind: 'user'}}));
    await agent.whenIdle();
    script = undefined;
    assert.ok(results.length > before, 'The original model/AgentLoop must dispatch its tool.');
    const row = results.slice(before).find(row => row.exec.name === name);
    assert.ok(row, `Original ${name} result required.`);
    return row.result;
  };
  const bash = (agent, command, extra = {}) => run(agent, 'bash',
    {description: 'Synthetic native tool probe.', command, timeoutMs: 5000, workdir: workspace, ...extra});
  const waitFile = async file => {
    for (let index = 0; index < 100 && !fs.existsSync(file); index++) await new Promise(resolve => setTimeout(resolve, 20));
    assert.ok(fs.existsSync(file), 'The actual native process must report its identity.');
    return Number(fs.readFileSync(file, 'utf8'));
  };
  if (scenario === 'tools') {
    const result = await bash(owned, "printf 'native-code\\n' > delivered.txt");
    assert.equal(result.isError, false, JSON.stringify(result));
    assert.equal(result.value.exitCode, 0);
    assert.equal(result.value.sandbox.mode, 'workspace-write');
    assert.equal(fs.readFileSync(path.join(workspace, 'delivered.txt'), 'utf8'), 'native-code\n');
    passed.push('allowed_write');
    assert.ok(jobs.some(event => event.type === 'registered' && event.job?.owner === owned.id), 'Foreground runs are native owned jobs.');
    passed.push('foreground_job_registered');
    const target = path.join(protectedRoot, 'protected.txt');
    fs.writeFileSync(target, 'unchanged');
    fs.symlinkSync(target, path.join(workspace, 'linked.txt'));
    const linking = await bash(owned, `ln '${target}' dynamiclinked.txt && printf 'dynamic-hardlink' > dynamiclinked.txt`);
    assert.equal(fs.readFileSync(target, 'utf8'), 'unchanged', 'Creating and mutating an outside hardlink in one original bash call must fail.');
    assert.ok(linking.isError || linking.value.exitCode !== 0, JSON.stringify(linking));
    passed.push('dynamic_hardlink_write_denied');
    for (const [name, command] of [['outside_write_denied', `printf 'outside' > '${target}'`],
      ['symlink_write_denied', "printf 'symlink' > linked.txt"], ['hardlink_write_denied', "printf 'hardlink' > hardlinked.txt"]]) {
      if (name === 'hardlink_write_denied') fs.linkSync(target, path.join(workspace, 'hardlinked.txt'));
      const denied = await bash(owned, command);
      assert.equal(fs.readFileSync(target, 'utf8'), 'unchanged', `Native workspace boundary failed: ${command}`);
      assert.ok(denied.isError || denied.value.exitCode !== 0, JSON.stringify(denied));
      passed.push(name);
    }
    fs.unlinkSync(path.join(workspace, 'hardlinked.txt'));
    const danger = await bash(owned, "printf 'danger' > danger.txt", {sandbox_permissions: 'danger-full-access', justification: 'Synthetic denial.'});
    assert.equal(danger.isError, true);
    assert.equal(fs.existsSync(path.join(workspace, 'danger.txt')), false);
    passed.push('danger_denied');
    const skill = await run(owned, 'skill', {name: 'fixture-matt'});
    assert.equal(skill.isError, false, JSON.stringify(skill));
    assert.equal(skill.value.name, 'fixture-matt');
    assert.match(skill.value.content, /Check actual tool results/);
    passed.push('original_skill_loaded');
    const question = await run(owned, 'ask_user_question', {questions: [{id: 'fixture-q', question: 'Synthetic question?'}]});
    assert.equal(question.isError, false, JSON.stringify(question));
    assert.equal(questions.at(-1).owner, owned.id);
    assert.equal(question.value.answers[0].custom, 'Fixture answer.');
    passed.push('original_question_answered');
    const list = await run(owned, 'job_list', {});
    assert.equal(list.isError, false, JSON.stringify(list));
    const implementation = "printf '%s\\n' 'import unittest' 'from pathlib import Path' 'class NativeTest(unittest.TestCase):' '    def test_delivered_file(self):' '        self.assertEqual(Path(\"delivered.txt\").read_text(), \"native-code\\n\")' > test_native_fixture.py";
    const written = await bash(owned, implementation);
    assert.equal(written.isError, false, JSON.stringify(written));
    assert.equal(written.value.exitCode, 0);
    const tested = await bash(owned, `'${python}' -m unittest -q test_native_fixture.py`);
    assert.equal(tested.isError, false, JSON.stringify(tested));
    assert.equal(tested.value.exitCode, 0);
    assert.match(tested.value.stdout.text + tested.value.stderr.text, /Ran 1 test[\s\S]*OK/);
    passed.push('actual_test_executed_same_source');
  } else if (scenario === 'inherited') {
    const before = ctx.tools.schemas(owned).map(row => row.name);
    assert.ok(before.length > names.length, 'The original root filesystem tools must be inherited before managed admission.');
    await bash(owned, "printf 'native-code\\n' > delivered.txt");
    assert.deepEqual(ctx.tools.schemas(owned).map(row => row.name).sort(), names);
    const manual = await create('fixture-manual-inherited', 'fixture-manual');
    assert.ok(ctx.tools.schemas(manual).length > names.length, 'The manual sibling retains its inherited original tools.');
    passed.push('inherited_tools_limited_only_for_owned_agent');
  } else if (scenario === 'deadline') {
    const file = path.join(ledgerDir, crypto.createHash('sha256').update(owned.id).digest('hex') + '.json');
    const row = JSON.parse(fs.readFileSync(file));
    row.budget.max_wall_seconds = 1;
    fs.writeFileSync(file, JSON.stringify(row));
    const command = `exec '${process.execPath}' -e 'require("fs").writeFileSync("deadline.pid", String(process.pid));setInterval(()=>{},1000)'`;
    const original = await bash(owned, command, {run_in_background: true});
    assert.equal(original.isError, false, JSON.stringify(original));
    const pid = await waitFile(path.join(workspace, 'deadline.pid'));
    const manual = await create('fixture-manual-deadline', 'fixture-manual');
    const other = await bash(manual, command.replace('deadline.pid', 'manual-deadline.pid'), {run_in_background: true});
    assert.equal(other.isError, false, JSON.stringify(other));
    assert.equal(owned.status, 'idle', 'The original Agent has returned while its native background job runs.');
    const settled = await ctx.jobs.wait(original.value.jobId, 5000, owned.id);
    assert.equal(settled.status, 'killed', 'A work deadline must stop owned background jobs even when the Agent is idle.');
    assert.throws(() => process.kill(pid, 0), {code: 'ESRCH'});
    assert.equal(ctx.jobs.get(other.value.jobId, manual.id).status, 'running', 'A managed deadline cannot stop the manual sibling job.');
    assert.equal(JSON.parse(fs.readFileSync(file)).state.stop_reason, 'shared-work-wall-budget');
    passed.push('idle_background_deadline_stopped', 'manual_sibling_job_preserved');
  } else if (scenario === 'jobs') {
    const command = `exec '${process.execPath}' -e 'require("fs").writeFileSync("job.pid", String(process.pid));setInterval(()=>{},1000)'`;
    const started = await bash(owned, command, {run_in_background: true});
    assert.equal(started.isError, false, JSON.stringify(started));
    const id = started.value.jobId;
    assert.ok(id);
    const pid = await waitFile(path.join(workspace, 'job.pid'));
    const foreign = await create('fixture-manual-tools', 'fixture-manual');
    const other = await bash(foreign, command.replace('job.pid', 'foreign.pid'), {run_in_background: true});
    assert.equal(other.isError, false, JSON.stringify(other));
    const denied = await run(owned, 'job_kill', {job_id: other.value.jobId});
    assert.equal(denied.isError, true, 'The managed preset cannot kill another Agent job.');
    assert.equal(ctx.jobs.get(other.value.jobId, foreign.id).status, 'running');
    const outputDenied = await run(owned, 'job_output', {job_id: other.value.jobId});
    assert.equal(outputDenied.isError, true, 'A foreign Agent job output remains private.');
    passed.push('foreign_job_kill_denied', 'foreign_job_output_denied');
    ctx.jobs.attachController('fixture-system-producer');
    const unownedProcess = await ctx.shell.execute(ctx.shell.resolve({command: command.replace('job.pid', 'system.pid'),
      workdir: workspace, timeoutMs: 10000, onExpiry: 'none', sandboxPolicy: ctx.sandboxPolicy.resolve()}));
    const systemId = ctx.jobs.start({kind: 'fixture-system', label: 'Synthetic unowned process.', run: () => ({
      done: unownedProcess.done.then(() => ({status: unownedProcess.status === 'killed' ? 'killed' : 'completed'})),
      cancel: () => unownedProcess.kill(),
    })});
    systemJobs.push(systemId);
    const systemPid = await waitFile(path.join(workspace, 'system.pid'));
    for (const name of ['job_output', 'job_kill']) {
      const rejected = await run(owned, name, {job_id: systemId});
      assert.equal(rejected.isError, true, 'Unowned work is not owned by the managed Session.');
      assert.equal(ctx.jobs.get(systemId).status, 'running');
    }
    const exposed = await run(owned, 'job_list', {});
    assert.equal(exposed.isError, true, 'A native mixed list must not leak unowned records.');
    passed.push('unowned_job_kill_denied', 'unowned_job_output_denied', 'unowned_job_list_denied');
    ctx.jobs.kill(systemId);
    await ctx.jobs.wait(systemId, 10000);
    assert.throws(() => process.kill(systemPid, 0), {code: 'ESRCH'});
    const read = await run(owned, 'job_output', {job_id: id, wait: false});
    assert.equal(read.isError, false, JSON.stringify(read));
    const killed = await run(owned, 'job_kill', {job_id: id});
    assert.equal(killed.isError, false, JSON.stringify(killed));
    await ctx.jobs.wait(id, 10000, owned.id);
    assert.equal(ctx.jobs.get(id, owned.id).status, 'killed');
    assert.throws(() => process.kill(pid, 0), {code: 'ESRCH'}, 'A stopped native job process must actually exit.');
    passed.push('managed_background_stopped');
    const nativeJobs = jobs.filter(event => event.job?.owner === owned.id);
    assert.ok(nativeJobs.some(event => event.type === 'settled' && event.job.id === id));
    const target = path.join(protectedRoot, 'background.txt');
    fs.writeFileSync(target, 'unchanged');
    const boundary = await bash(owned, `printf 'outside' > '${target}'`, {run_in_background: true});
    assert.equal(boundary.isError, false, JSON.stringify(boundary));
    await ctx.jobs.wait(boundary.value.jobId, 10000, owned.id);
    const boundaryRead = await run(owned, 'job_output', {job_id: boundary.value.jobId});
    assert.equal(fs.readFileSync(target, 'utf8'), 'unchanged');
    assert.equal(boundaryRead.isError, false, JSON.stringify(boundaryRead));
    assert.match(boundaryRead.value.text, /Operation not permitted|sandbox.*denied/);
    passed.push('background_write_boundary');
    const before = results.length;
    script = {name: 'bash', arguments: {description: 'Synthetic cancellable foreground.',
      command: command.replace('job.pid', 'foreground.pid'), timeoutMs: 10000, workdir: workspace}};
    issued = false;
    owned.followup(createUserMessage({content: [{type: 'text', text: 'Run one cancellable foreground operation.'}], source: {kind: 'user'}}));
    const foregroundPid = await waitFile(path.join(workspace, 'foreground.pid'));
    owned.cancel({kind: 'hook', reason: 'fixture-native-cancel'}, {keepInbox: false});
    await owned.whenIdle();
    script = undefined;
    const cancelled = results.slice(before).find(row => row.exec.name === 'bash');
    assert.ok(cancelled?.result.isError, 'Native foreground cancellation must materialize a tool error.');
    assert.throws(() => process.kill(foregroundPid, 0), {code: 'ESRCH'});
    passed.push('foreground_cancelled');
    await handles[0].dispose();
    assert.equal(ctx.jobs.get(other.value.jobId, foreign.id).status, 'running', 'Owned teardown preserves the manual sibling job.');
    passed.push('manual_sibling_job_preserved');
  }
  const receiptPrefix = path.join(ledgerDir, crypto.createHash('sha256').update(owned.id).digest('hex'));
  const toolReceipts = fs.readFileSync(receiptPrefix + '.tools.jsonl', 'utf8').trim().split('\n').map(JSON.parse);
  const jobReceipts = fs.readFileSync(receiptPrefix + '.jobs.jsonl', 'utf8').trim().split('\n').map(JSON.parse);
  assert.ok(toolReceipts.length > 0 && jobReceipts.length > 0, 'Original public tool/job outcomes require persistent plugin receipts.');
  for (const row of [...toolReceipts, ...jobReceipts]) {
    assert.equal(row.schema, 1);
    assert.equal(row.session_id, owned.id);
    assert.equal(row.generation, 'fixture-work-' + owned.id);
  }
  for (const row of toolReceipts.filter(row => row.name === 'bash' && row.kind === 'foreground')) {
    assert.equal(row.command, undefined, 'Persistent diagnosis does not copy raw command text.');
    assert.match(row.command_sha256, /^[a-f0-9]{64}$/);
    assert.equal(row.sandbox.mode, 'workspace-write');
    for (const snapshot of [row.source_before, row.source_after]) {
      assert.equal(snapshot.head, git(['rev-parse', 'HEAD']).trim());
      assert.match(snapshot.source_digest, /^[a-f0-9]{64}$/);
      assert.equal(typeof snapshot.workspace_status, 'string');
    }
  }
  if (scenario === 'tools') {
    const final = toolReceipts.findLast(row => row.name === 'bash' && row.kind === 'foreground');
    const actual = JSON.parse(execFileSync(python, ['-I', reader, workspace], {encoding: 'utf8'}));
    assert.equal(final.exit_code, 0);
    assert.deepEqual(final.source_before, final.source_after, 'The original test must consume the same source it reports.');
    assert.deepEqual(final.source_after, actual, 'Successful test receipt equals the existing original source_state entry.');
  }
  passed.push('native_tool_and_job_receipts');
  for (const handle of handles) await handle.dispose();
  await manualRemove();
  await remove();
  const catalog = calls[0].tools.map(row => row.name).sort();
  assert.deepEqual(catalog, names, 'The actual model request must show exactly six original native tools.');
  const composition = plugins.filter(row => row.id !== 'skill-filesystem').map(({name, config}) =>
    ({name: '@deepseek-ai/' + new URL(name).pathname.split('/@deepseek-ai/')[1].split('/')[0], ...(config ? {config} : {})}));
  const sdkSources = Object.fromEntries([...common, 'cordis', 'cordis-plugin-loader', 'dsh-agent-preset-registry',
    ...plugins.map(row => new URL(row.name).pathname.split('/@deepseek-ai/')[1].split('/')[0])]
    .map(name => [`@deepseek-ai/${name}/lib/index.js`, crypto.createHash('sha256').update(fs.readFileSync(
      path.join(sdkRoot, `@deepseek-ai/${name}/lib/index.js`))).digest('hex')]));
  process.stdout.write(JSON.stringify({native_shared_tools: 'passed', scenario, requests: calls.length,
    tool_catalog: catalog, passed_cases: passed, preset: declaration,
    composition, composition_sha256: crypto.createHash('sha256').update(canonical({composition, sdk_sources: sdkSources})).digest('hex'),
    managed_jobs: {foreground: passed.includes('foreground_job_registered') || passed.includes('foreground_cancelled'),
      background: passed.includes('managed_background_stopped') || passed.includes('idle_background_deadline_stopped'),
      scope: 'darwin_process_group'},
    source_hook_sha256: crypto.createHash('sha256').update(fs.readFileSync(path.resolve('ghost_hermes_pm/shared_native_hook.mjs'))).digest('hex'),
    sdk_sources: sdkSources,
    managed_job_events: jobs.filter(event => event.job?.owner === owned.id).map(event => ({type: event.type, status: event.job.status}))}));
} catch (error) {
  process.stderr.write(JSON.stringify({native_shared_tools: 'failed', scenario, passed_cases: passed}) + '\n');
  throw error;
} finally {
  for (const id of systemJobs) {ctx.jobs.kill(id); await ctx.jobs.wait(id, 10000);}
  for (const handle of handles) await handle.dispose();
  await ctx.fiber.dispose();
}
