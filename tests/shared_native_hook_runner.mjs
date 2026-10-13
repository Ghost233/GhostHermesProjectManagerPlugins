// Native in-process components; only the external model is a fixture.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import {pathToFileURL} from 'node:url';
import {execFileSync} from 'node:child_process';

const root = process.env.DSH_TEST_SDK_ROOT;
const scratch = fs.realpathSync(process.argv[2]);
const sdk = async name => import(pathToFileURL(path.join(root, `@deepseek-ai/${name}/lib/index.js`)));
const {Context} = await sdk('cordis');
const {Loader} = await sdk('cordis-plugin-loader');
const {LlmAdapter, createUserMessage} = await sdk('dsh-llm');
const {setSandboxMode} = await sdk('dsh-sandbox-policy');
const ctx = new Context();
await ctx.plugin(Loader, {baseUrl: pathToFileURL(root + '/').href});
const loader = ctx.loader;
const ledgerDir = path.join(scratch, 'ledger');
const workspace = path.join(scratch, 'repository');
fs.mkdirSync(ledgerDir, {mode: 0o700});
fs.mkdirSync(workspace, {mode: 0o700});
const config = {ledger_dir: ledgerDir, service_ref: 'fixture:shared-host',
  host_lease: 'fixture-native-lease', agent_preset: 'fixture-managed'};
const moduleURL = pathToFileURL(path.resolve('ghost_hermes_pm/shared_native_hook.mjs')).href;
const calls = [];
const toolRuns = [];
let observerAgent;
const diagnostics = [];
ctx.logger.exporter({levels: {default: 3}, export: message => diagnostics.push(message.args.map(String).join(' '))});
const scenario = process.argv[3] ?? 'basic';
const currentHostLease = scenario === 'host_restart' ? 'fixture-restarted-lease' : config.host_lease;
if (scenario !== 'late_host') ctx.provide('workflowHostInstance', {instanceId: currentHostLease});
class Model extends LlmAdapter {
  resolveModel(provider, model) {
    return Promise.resolve({provider, id: model, name: model, defaultMaxTokens: 12288, context: {contextWindow: 16384}});
  }
  providerRetryPolicy() { return {mode: 'normal', maxRetries: 0, retryableCodes: [],
    initialDelayMs: 0, maxDelayMs: 0, jitterRatio: 0}; }
  async *stream(options) {
    calls.push(options);
    if (options.model === 'fixture-wait' || options.model === 'fixture-window-interrupted'
        && calls.filter(call => call.model === 'fixture-window-interrupted').length === 1 || options.model === 'fixture-window-wait'
        && calls.filter(call => call.model === 'fixture-window-wait').length > 1) {
      if (options.model === 'fixture-window-interrupted') {
        yield {type: 'block-start', index: 0, blockType: 'text'};
        yield {type: 'text-delta', index: 0, text: 'Interrupted native response.'};
      }
      await new Promise((resolve, reject) => {
        const timer = setTimeout(resolve, 5000);
        options.signal.addEventListener('abort', () => {clearTimeout(timer); reject(options.signal.reason);}, {once: true});
      });
    }
    yield {type: 'block-start', index: 0, blockType: 'text'};
    yield {type: 'text-delta', index: 0, text: 'Native result.'};
    yield {type: 'block-end', index: 0, block: {type: 'text', text: 'Native result.'}};
    if (options.model === 'fixture-observer-write') {
      yield {type: 'block-start', index: 1, blockType: 'tool-call'};
      yield {type: 'block-end', index: 1, block: {type: 'tool-call', id: 'fixture-should-not-run',
        name: 'bash', arguments: '{}'}};
      observerAgent.followup(createUserMessage({content: [{type: 'text', text: 'Queued native followup.'}],
        source: {kind: 'user'}}));
      fs.chmodSync(ledgerDir, 0o500);
    }
    if (options.model === 'fixture-recovery-unknown') fs.chmodSync(ledgerDir, 0o500);
    if (!['fixture-unreported', 'fixture-observer-write', 'fixture-recovery-unknown'].includes(options.model)) {
      yield {type: 'usage', usage: {inputTokens: 12, outputTokens: options.model === 'fixture-large-usage' ? 300008 : 8}};
    }
    yield {type: 'finish', reason: {kind: 'completed'}};
  }
}
const names = ['ask_user_question', 'bash', 'job_kill', 'job_list', 'job_output', 'skill'];
const teamNames = ['interrupt_agent', 'list_agents', 'send_message', 'spawn_teammate',
  'team_task_create', 'team_task_get', 'team_task_list', 'team_task_update', 'wait_agent'];
let teamEvidence;
let settlementEvidence;
try {
  for (const name of ['dsh-session', 'dsh-session-projection', 'dsh-token-meter',
    'dsh-system-prompt', 'dsh-sandbox-policy', 'dsh-tools', 'dsh-jobs-local', 'dsh-llm', 'dsh-agent', 'dsh-agent-loop']) {
    await loader.create({id: name, name: pathToFileURL(path.join(root, `@deepseek-ai/${name}/lib/index.js`)).href});
  }
  await loader.create({id: 'presets', name: pathToFileURL(path.join(root,
    '@deepseek-ai/dsh-agent-preset-registry/lib/index.js')).href, config: {default: 'fixture-manual'}});
  await loader.await();
  assert.ok(ctx.get('agentLoop'), 'The original AgentLoop must load.');
  ctx.llm.registerAdapter(['fixture-model'], new Model());
  if (scenario !== 'team') for (const name of names) ctx.tools.register({name, description: 'Fixture external capability boundary.',
    parameters: {type: 'object', properties: {}}, execute: async () => {toolRuns.push(name); return 'unused';},
    output: {schema: {type: 'string'}, render: value => [{type: 'text', text: value}]}});
  const nativeTools = [];
  if (scenario === 'team') {
    const skills = path.join(scratch, 'skills');
    fs.mkdirSync(path.join(skills, 'fixture-workflow'), {recursive: true});
    fs.writeFileSync(path.join(skills, 'fixture-workflow', 'SKILL.md'),
      '---\nname: fixture-workflow\ndescription: Synthetic native workflow.\n---\nInspect original results.\n');
    for (const name of ['dsh-fs-local', 'dsh-working-directory', 'dsh-shell-env', 'dsh-sandbox-local',
      'dsh-subprocess-local', 'dsh-bash-sandbox', 'dsh-user-questions', 'dsh-user-approval', 'dsh-skill',
      'dsh-session-persistence-jsonl', 'dsh-subagent', 'dsh-experimental-agent-team', 'dsh-experimental-tool-agent-team']) {
      const configuration = name === 'dsh-working-directory' ? {defaultDirectory: workspace}
        : name === 'dsh-user-approval' ? {policy: 'never'}
        : name === 'dsh-session-persistence-jsonl' ? {root: path.join(scratch, 'session-store')} : {};
      await loader.create({id: name, name: pathToFileURL(path.join(root, `@deepseek-ai/${name}/lib/index.js`)).href,
        config: configuration});
    }
    for (const name of ['dsh-tool-bash', 'dsh-tool-jobs', 'dsh-tool-ask-user', 'dsh-skill-filesystem', 'dsh-tool-skill']) {
      const configuration = name === 'dsh-tool-bash' ? {enableRunInBackground: true, promoteOnTimeout: true}
        : name === 'dsh-tool-jobs' ? {completionDelivery: 'quiet'}
        : name === 'dsh-tool-ask-user' ? {mode: 'timed', timeout: -1}
        : name === 'dsh-skill-filesystem' ? {customSkillDirs: [skills], includeDefaultRoots: false, watch: false} : {};
      nativeTools.push({id: name, name: pathToFileURL(path.join(root, `@deepseek-ai/${name}/lib/index.js`)).href,
        config: configuration});
    }
    await loader.await();
    assert.ok(ctx.get('agentTeams'), 'The original Team service must be mounted.');
    ctx.systemPrompt.section({name: 'fixture:safety', order: 0, text: 'Fixture safety and responsibility stay in effect.'});
    ctx.systemPrompt.section({name: 'team:policy-fixture', order: 1, text: 'An unrelated similarly named section stays visible.'});
  }
  const declaration = {id: 'fixture-managed', plugins: [{id: 'shared-hook', name: moduleURL, config},
    {id: 'presentation', name: pathToFileURL(path.join(root,
      '@deepseek-ai/dsh-agent-tool-presentation/lib/index.js')).href, config: {mode: 'native'}}, ...nativeTools]};
  if (scenario === 'guard_deadline') {
    execFileSync('git', ['init', '-q', workspace]);
    const python = fs.realpathSync(process.env.HERMES_TEST_SESSION_PYTHON);
    const reader = path.join(scratch, 'slow-source-reader.py');
    fs.writeFileSync(reader, 'import runpy,time\ntime.sleep(1.2)\nrunpy.run_path('
      + JSON.stringify(path.resolve('ghost_hermes_pm/shared_source_reader.py')) + ',run_name="__main__")\n');
    config.source_snapshot = {python, reader, python_sha256: crypto.createHash('sha256').update(fs.readFileSync(python)).digest('hex'),
      reader_sha256: crypto.createHash('sha256').update(fs.readFileSync(reader)).digest('hex')};
  }
  if (scenario.startsWith('runtime_')) {
    execFileSync('git', ['init', '-q', workspace]);
    let python = fs.realpathSync(process.env.HERMES_TEST_SESSION_PYTHON);
    const reader = fs.realpathSync('ghost_hermes_pm/shared_source_reader.py');
    if (scenario === 'runtime_noncanonical') {
      const alias = path.join(scratch, 'python-alias');
      fs.symlinkSync(python, alias);
      python = alias;
    } else if (['runtime_workspace', 'runtime_writable', 'runtime_notexec',
                'runtime_old_version', 'runtime_entry_failed'].includes(scenario)) {
      python = path.join(scenario === 'runtime_workspace' ? workspace : scratch, 'fixture-python');
      const version = scenario === 'runtime_old_version' ? '3.10.0' : '3.11.0';
      fs.writeFileSync(python, '#!/bin/sh\nif [ "$1" = "-I" ] && [ "$2" = "--version" ]; then\n'
        + '  echo "Python ' + version + '"\n  exit 0\nfi\nexit 23\n', {mode: 0o700});
      if (scenario === 'runtime_writable') fs.chmodSync(python, 0o722);
      if (scenario === 'runtime_notexec') fs.chmodSync(python, 0o600);
    }
    config.source_snapshot = {python, reader, python_sha256: '0'.repeat(64),
      reader_sha256: scenario === 'runtime_reader_changed' ? '0'.repeat(64)
        : crypto.createHash('sha256').update(fs.readFileSync(reader)).digest('hex')};
  }
  const removePreset = await ctx.agentPresets.register(declaration);
  if (scenario === 'late_host' || scenario === 'renew_window' || scenario === 'renew_interrupted') {
    const configs = {'dsh-storage-json': {root: path.join(scratch, 'domain')},
      'dsh-storage-domain': {backend: 'json'},
      'dsh-session-persistence-jsonl': {root: path.join(scratch, 'sessions')},
      'dsh-working-directory': {defaultDirectory: workspace},
      'dsh-attachment-local': {dshHome: path.join(scratch, 'attachments')},
      'dsh-agent-default-model': {provider: 'fixture-model', model: 'fixture-default'},
      'dsh-credentials-local': {path: path.join(scratch, 'fixture-credentials.yaml'), watch: false}};
    for (const name of ['dsh-typert-registry', 'dsh-api-gateway', 'dsh-storage', 'dsh-storage-json',
      'dsh-storage-domain', 'dsh-session-persistence-jsonl', 'dsh-session-query', 'dsh-agent-default-model',
      'dsh-fs-local', 'dsh-working-directory', 'dsh-shell-env', 'dsh-user-approval', 'dsh-commands',
      'dsh-permission-presets', 'dsh-attachment-local', 'dsh-credentials-local', 'dsh-client-connection',
      'dsh-client-file-upload', 'dsh-workspace', 'dsh-api-session-controller']) {
      await loader.create({id: name, name: pathToFileURL(path.join(root, `@deepseek-ai/${name}/lib/index.js`)).href,
        config: configs[name]});
    }
    if (scenario === 'late_host') {
      const provider = path.join(scratch, 'late-host-provider.mjs');
      fs.writeFileSync(provider, 'export default function(ctx,config){ctx.provide("workflowHostInstance",{instanceId:config.instanceId});}\n');
      await loader.create({id: 'late-host-provider', name: pathToFileURL(provider).href,
        config: {instanceId: currentHostLease}});
    }
    await loader.await();
    const {TYPERT} = await import(pathToFileURL(path.join(root, '@deepseek-ai/dsh-api-session-controller/lib/typert.host.js')).href);
    ctx.typert.register(TYPERT);
    if (scenario === 'late_host') {
    const response = await ctx.typertGateway.dispatchRpc('session/create', {args: {request: {
      sessionId: 'fixture-late-host-session', cwd: workspace, agentPreset: config.agent_preset}}}, new AbortController().signal);
    assert.equal(response.ok, true, 'Late original Host service must admit the preset through public session/create: ' + JSON.stringify(response));
    assert.deepEqual(response.value, {sessionId: 'fixture-late-host-session', agentPreset: config.agent_preset});
    assert.equal(calls.length, 0, 'Adoption must not send model input.');
    assert.equal(ctx.agents.get(response.value.sessionId)?.session, ctx.sessions.get(response.value.sessionId));
    await removePreset();
    process.stdout.write(JSON.stringify({native_shared_hook: 'passed', requests: 0, late_host: response.value}));
    process.exitCode = 0;
    }
  }
  if (scenario !== 'late_host') {
  const removeManual = scenario === 'team' ? await ctx.agentPresets.register({id: 'fixture-manual',
    plugins: declaration.plugins.filter(plugin => plugin.id !== 'shared-hook')}) : undefined;
  const roster = await ctx.agentPresets.list();
  assert.equal(roster.find(row => row.id === declaration.id)?.broken, undefined,
    'The original Loader must activate the main-plugin Hook.');
  const create = async (id, preset = 'fixture-managed', restored) => {
    const handle = await ctx.agents.create({sessionId: id,
      meta: restored?.meta ?? {cwd: workspace, agentPreset: preset},
      ...(restored ? {seed: restored.events} : {}),
      agentOptions: {provider: 'fixture-model', model: 'fixture-default',
        ...(id === 'fixture-native-control' ? {maxTokens: 10000} : {})},
      setup: async agentCtx => {
        if (preset === 'fixture-managed' || scenario === 'team') await ctx.agentPresets.mount(agentCtx, preset);
      }});
    setSandboxMode(handle.agent.session, 'workspace-write');
    return handle;
  };
  const ledgerPath = id => path.join(ledgerDir, crypto.createHash('sha256').update(id).digest('hex') + '.json');
  const register = (agent, overrides = {}) => {
    const row = {schema: 1, ...config, session_id: agent.session.id,
      session_created_at: agent.session.header.createdAt, generation: 'fixture-work-' + agent.session.id,
      workspace, provider: 'fixture-model', model: 'fixture-route',
      budget: {max_wall_seconds: 1800, max_model_requests: 2},
      state: {started_at_ms: null, requests: 0, reported_tokens: 0, admissions: [], stop_reason: null},
      ...overrides};
    fs.writeFileSync(ledgerPath(agent.session.id), JSON.stringify(row), {mode: 0o600});
    return row;
  };
  const prompt = async agent => {
    agent.followup(createUserMessage({content: [{type: 'text', text: 'One external model response.'}],
      source: {kind: 'user'}}));
    await agent.whenIdle();
  };
  if (scenario === 'host_restart') {
    const managed = await create('fixture-restarted');
    register(managed.agent, {host_lease: currentHostLease});
    await prompt(managed.agent);
    assert.equal(calls.length, 1, 'The original public Host lease admits its re-bound Session after restart.');
    const stale = await create('fixture-stale-lease');
    register(stale.agent);
    await prompt(stale.agent);
    assert.equal(calls.length, 1, 'The preset configuration lease cannot admit a stale ledger on the new Host.');
    const manual = await create('fixture-manual-session', 'fixture-manual');
    await prompt(manual.agent);
    assert.equal(calls.length, 2, 'A sibling manual Session remains usable after the Host restart.');
    assert.equal(calls.at(-1).model, 'fixture-default');
    await managed.dispose();
    await stale.dispose();
    await manual.dispose();
  } else if (scenario === 'team') {
    const managed = await create('fixture-managed-with-native-team');
    register(managed.agent);
    const rawBefore = ctx.tools.schemas(managed.agent).map(tool => tool.name).sort();
    assert.deepEqual(rawBefore, [...names, ...teamNames].sort(), 'Original Team tools must really inject into the managed Agent.');
    await prompt(managed.agent);
    assert.equal(calls.length, 1, 'A native Team-enabled Agent must admit its original six-tool model request.');
    assert.deepEqual(calls[0].tools.map(tool => tool.name).sort(), names, 'The actual first native model request must advertise exactly six tools.');
    const modelText = call => call.messages.flatMap(message => message.content)
      .filter(part => part.type === 'text').map(part => part.text).join('\n');
    assert.equal(modelText(calls[0]).includes('Agent Teams is available in this session'), false,
      'The original model messages must not advertise unavailable Team operations to managed work.');
    assert.ok(modelText(calls[0]).includes('Fixture safety and responsibility stay in effect.'));
    assert.ok(modelText(calls[0]).includes('An unrelated similarly named section stays visible.'));
    const denied = [];
    const argumentsByName = {spawn_teammate: {name: 'fixture-peer', description: 'Synthetic peer.', prompt: 'Never execute.'},
      send_message: {target: 'fixture-peer', message: 'Never deliver.'}, interrupt_agent: {target: 'fixture-peer'},
      team_task_create: {subject: 'Never create.', description: 'Synthetic denial.'},
      team_task_get: {task_id: 'fixture-task'}, team_task_update: {task_id: 'fixture-task', expected_revision: 0, action: 'claim'},
      wait_agent: {timeout_ms: 10000}};
    for (const name of teamNames) {
      const result = await ctx.tools.execute({agent: managed.agent, name, callId: 'fixture-team-denied-' + name,
        arguments: argumentsByName[name] ?? {}, signal: new AbortController().signal});
      assert.equal(result.isError, true, 'Every native Team tool must be refused for managed work.');
      assert.match(JSON.stringify(result), /Shared work tool is outside its verified catalog/, 'Denial must come from the original monotonic guard, not invalid Team arguments.');
      denied.push(name);
    }
    assert.equal(ctx.agents.list().length, 1, 'Rejected Team calls must not create any teammate.');
    assert.equal(ctx.agentTeams.listTasks(managed.agent).length, 0);
    const manual = await create('fixture-manual-with-native-team', 'fixture-manual');
    const manualBefore = ctx.tools.schemas(manual.agent).map(tool => tool.name).sort();
    assert.deepEqual(manualBefore, [...names, ...teamNames].sort());
    await prompt(manual.agent);
    assert.deepEqual(calls[1].tools.map(tool => tool.name).sort(), manualBefore, 'Manual native Team capabilities remain model-visible.');
    assert.ok(modelText(calls[1]).includes('Agent Teams is available in this session'),
      'The manual Agent keeps the original Team policy.');
    const allowed = await ctx.tools.execute({agent: manual.agent, name: 'list_agents', callId: 'fixture-manual-team-list',
      arguments: {}, signal: new AbortController().signal});
    assert.equal(allowed.isError, false, 'The original manual Team tool must execute normally.');
    assert.deepEqual(ctx.tools.schemas(managed.agent).map(tool => tool.name).sort(), rawBefore,
      'Filtering the advertised surface does not unregister SDK-owned Team tools.');
    const unregistered = await create('fixture-native-team-without-ledger');
    const unadmitted = await ctx.tools.execute({agent: unregistered.agent, name: 'list_agents',
      callId: 'fixture-team-without-owner', arguments: {}, signal: new AbortController().signal});
    assert.equal(unadmitted.isError, true);
    assert.match(JSON.stringify(unadmitted), /Shared work tool has no admitted owner/);
    teamEvidence = {raw_managed_catalog: rawBefore, first_model_catalog: calls[0].tools.map(tool => tool.name).sort(),
      denied_team_tools: denied, manual_catalog: manualBefore, manual_list_agents_is_error: allowed.isError,
      unregistered_denial: 'no-admitted-owner', managed_team_policy_present: false, manual_team_policy_present: true,
      unrelated_safety_sections_preserved: true};
    await managed.dispose();
    await unregistered.dispose();
    await removePreset();
    const missingPreset = await ctx.agentPresets.register({...declaration,
      plugins: declaration.plugins.filter(plugin => plugin.id !== 'dsh-tool-bash')});
    const missing = await create('fixture-native-team-missing-required-tool');
    register(missing.agent);
    const beforeMissing = calls.length;
    await prompt(missing.agent);
    assert.equal(calls.length, beforeMissing, 'Missing an original required tool still rejects before the model request.');
    const missingState = JSON.parse(fs.readFileSync(ledgerPath(missing.agent.session.id))).state;
    assert.equal(missingState.requests, 0);
    assert.equal(missingState.started_at_ms, null);
    teamEvidence.required_tool_missing_model_requests = calls.length - beforeMissing;
    await missing.dispose();
    await missingPreset();
    await manual.dispose();
  } else if (scenario === 'integrity') {
    const held = await create('fixture-state-binding');
    const initial = register(held.agent);
    await prompt(held.agent);
    assert.equal(calls.length, 1);
    fs.writeFileSync(ledgerPath(held.agent.session.id), JSON.stringify(initial), {mode: 0o600});
    await prompt(held.agent);
    assert.equal(calls.length, 1, 'An in-process count rollback must not admit another model request.');
    await held.dispose();
    const unsafe = await create('fixture-unsafe-policy');
    register(unsafe.agent);
    setSandboxMode(unsafe.agent.session, 'danger-full-access');
    await prompt(unsafe.agent);
    assert.equal(calls.length, 1, 'Ledger cwd does not replace the original sandbox policy.');
    await unsafe.dispose();
    const future = await create('fixture-future-clock');
    register(future.agent, {state: {started_at_ms: Date.now() + 60000, requests: 0,
      reported_tokens: 0, admissions: [], stop_reason: null}});
    await prompt(future.agent);
    assert.equal(calls.length, 1, 'A future start cannot extend a wall-clock budget.');
    await future.dispose();
    const invalidFile = await create('fixture-public-ledger');
    register(invalidFile.agent);
    fs.chmodSync(ledgerPath(invalidFile.agent.session.id), 0o644);
    await prompt(invalidFile.agent);
    assert.equal(calls.length, 1, 'A public ledger is not an ownership grant.');
    await invalidFile.dispose();
  } else if (scenario.startsWith('runtime_')) {
    const owned = await create('fixture-runtime-source');
    register(owned.agent);
    await prompt(owned.agent);
    const result = await ctx.tools.execute({agent: owned.agent, name: 'bash', callId: 'fixture-runtime-source',
      arguments: {command: 'synthetic source receipt'}, signal: new AbortController().signal});
    const receipts = fs.readFileSync(ledgerPath(owned.agent.session.id).replace('.json', '.tools.jsonl'), 'utf8')
      .trim().split('\n').map(line => JSON.parse(line));
    if (scenario === 'runtime_stale_hash') {
      assert.equal(result.isError, false, 'A trusted compatible Python upgrade must accept stale binary metadata.');
      assert.equal(toolRuns.length, 1);
      assert.equal(receipts[0].execution_admission, 'admitted');
      assert.equal(receipts[0].source_before.head, null);
      assert.equal(receipts[0].source_before.workspace_status, '');
      assert.match(receipts[0].source_before.source_digest, /^[a-f0-9]{64}$/);
      assert.deepEqual(receipts[0].source_after, receipts[0].source_before,
        'The actual reader must record the same unmodified repository before and after tool dispatch.');
    } else {
      assert.equal(result.isError, true, 'An unsafe runtime or changed source reader must refuse tool execution: ' + scenario);
      assert.equal(toolRuns.length, 0);
      assert.equal(receipts[0].execution_admission, 'rejected');
      assert.equal(receipts[0].source_before, undefined);
    }
    await owned.dispose();
  } else if (scenario === 'guard_deadline') {
    const owned = await create('fixture-guard-deadline');
    const row = register(owned.agent);
    row.budget.max_wall_seconds = 1;
    fs.writeFileSync(ledgerPath(owned.agent.session.id), JSON.stringify(row));
    await prompt(owned.agent);
    const result = await ctx.tools.execute({agent: owned.agent, name: 'bash', callId: 'fixture-after-source-read',
      arguments: {command: 'synthetic nonexecuted tool'}, signal: new AbortController().signal});
    assert.equal(result.isError, true, 'A synchronous source read cannot grant a tool after the wall deadline.');
    assert.equal(toolRuns.length, 0);
    await owned.dispose();
  } else if (scenario === 'inherited') {
    ctx.tools.register({name: 'fixture-global-extra', description: 'Unrelated global capability.',
      parameters: {type: 'object', properties: {}}, execute: async () => 'unused',
      output: {schema: {type: 'string'}, render: value => [{type: 'text', text: value}]}});
    const managed = await create('fixture-filtered-inheritance');
    register(managed.agent);
    await prompt(managed.agent);
    assert.equal(calls.length, 1, 'Only inherited extra tools must be restricted for the admitted Agent.');
    assert.deepEqual(ctx.tools.schemas(managed.agent).map(tool => tool.name).sort(), names);
    assert.deepEqual(calls[0].tools.map(tool => tool.name).sort(), names,
      'The first original Model call must receive only the restricted tool catalog.');
    const manual = await create('fixture-inheritance-manual', 'fixture-manual');
    assert.ok(ctx.tools.schemas(manual.agent).some(tool => tool.name === 'fixture-global-extra'));
    await prompt(manual.agent);
    assert.equal(calls.length, 2);
    await managed.dispose();
    await manual.dispose();
  } else if (scenario === 'tool_guard') {
    const owned = await create('fixture-guarded-tools');
    register(owned.agent);
    await prompt(owned.agent);
    const result = await ctx.tools.execute({agent: owned.agent, name: 'bash', callId: 'fixture-danger',
      arguments: {sandbox_permissions: 'danger-full-access'}, signal: new AbortController().signal});
    assert.equal(result.isError, true, 'The native guard must reject danger privilege for the managed Agent.');
    assert.equal(toolRuns.length, 0);
    const file = path.join(ledgerDir, crypto.createHash('sha256').update(owned.agent.session.id).digest('hex') + '.tools.jsonl');
    const receipts = fs.readFileSync(file, 'utf8').trim().split('\n').map(line => JSON.parse(line));
    assert.equal(receipts[0].call_id, 'fixture-danger');
    assert.equal(receipts[0].execution_admission, 'rejected');
    assert.equal(receipts[0].is_error, true);
    assert.equal(fs.statSync(file).mode & 0o077, 0);
    const manual = await create('fixture-manual-tools', 'fixture-manual');
    const allowed = await ctx.tools.execute({agent: manual.agent, name: 'bash', callId: 'fixture-manual-call',
      arguments: {sandbox_permissions: 'danger-full-access'}, signal: new AbortController().signal});
    assert.equal(allowed.isError, false, 'The managed preset guard must not change a manual Agent.');
    assert.equal(toolRuns.length, 1);
    await owned.dispose();
    await manual.dispose();
  } else if (scenario === 'lifecycle') {
    const weak = await (async () => {
      let owned = await create('fixture-disposed-object');
      register(owned.agent);
      await prompt(owned.agent);
      const reference = new WeakRef(owned.agent);
      await owned.dispose();
      owned = null;
      return reference;
    })();
    for (let index = 0; index < 40; index++) {
      await new Promise(resolve => setTimeout(resolve, 10));
      global.gc();
      if (weak.deref() === undefined) break;
    }
    assert.ok(weak.deref() === undefined, 'The native disposal edge must release the exact admitted Agent.');
    assert.equal(JSON.parse(fs.readFileSync(ledgerPath('fixture-disposed-object'))).state.requests, 1,
      'Releasing an Agent does not erase its durable budget.');
    const manual = await create('fixture-disposal-manual', 'fixture-manual');
    await prompt(manual.agent);
    assert.equal(calls.length, 2);
    await manual.dispose();
  } else if (scenario === 'recovery') {
    const original = await create('fixture-recovered-unknown');
    register(original.agent, {model: 'fixture-recovery-unknown'});
    try {await prompt(original.agent);} finally {fs.chmodSync(ledgerDir, 0o700);}
    assert.equal(calls.length, 1);
    assert.equal(JSON.parse(fs.readFileSync(ledgerPath(original.agent.session.id))).state.stop_reason, null,
      'The actual permission failure left no durable unknown-usage stop marker.');
    const restore = {meta: {...original.agent.session.header},
      events: Array.from({length: original.agent.session.seq}, (_, index) => original.agent.session.eventAt(index))};
    try {await prompt(original.agent);} finally {fs.chmodSync(ledgerDir, 0o700);}
    assert.equal(calls.length, 1, 'An old unreported settlement cannot masquerade as a zero-usage completed request.');
    await original.dispose();
    const reattached = await create('fixture-recovered-unknown', 'fixture-managed', restore);
    try {await prompt(reattached.agent);} finally {fs.chmodSync(ledgerDir, 0o700);}
    assert.equal(calls.length, 1, 'Original native journal recovery must reject the missing usage proof.');
    await reattached.dispose();
  } else if (scenario === 'observer') {
    const failing = await create('fixture-observer-write');
    observerAgent = failing.agent;
    register(failing.agent, {model: 'fixture-observer-write'});
    try {await prompt(failing.agent);} finally {fs.chmodSync(ledgerDir, 0o700);}
    assert.equal(calls.length, 1);
    assert.equal(toolRuns.length, 0, 'A ledger write failure at settlement must cancel before tool dispatch.');
    assert.ok(diagnostics.some(message => message.includes('EACCES')), 'Original filesystem failure remains in native diagnostics.');
    await failing.dispose();
    observerAgent = undefined;
  } else if (scenario === 'unknown') {
    const missing = await create('fixture-unknown-usage');
    register(missing.agent, {model: 'fixture-unreported'});
    await prompt(missing.agent);
    await prompt(missing.agent);
    assert.equal(calls.length, 1, 'A completed provider response without usage cannot be silently free.');
    assert.equal(JSON.parse(fs.readFileSync(ledgerPath(missing.agent.session.id))).state.stop_reason,
      'shared-work-usage-unknown');
    const blocked = await create('fixture-wall-wait');
    const registration = register(blocked.agent, {model: 'fixture-wait'});
    registration.budget.max_wall_seconds = 1;
    fs.writeFileSync(ledgerPath(blocked.agent.session.id), JSON.stringify(registration));
    await prompt(blocked.agent);
    assert.equal(calls.length, 2);
    assert.equal(JSON.parse(fs.readFileSync(ledgerPath(blocked.agent.session.id))).state.stop_reason,
      'shared-work-wall-budget');
    const manual = await create('fixture-after-budget-stop', 'fixture-manual');
    await prompt(manual.agent);
    assert.equal(calls.length, 3, 'A work deadline does not stop its sibling manual Agent.');
    await missing.dispose();
    await blocked.dispose();
    await manual.dispose();
  } else if (scenario === 'settlement_history') {
    const original = await create('fixture-history-source');
    const registration = register(original.agent, {model: 'fixture-window-interrupted'});
    registration.budget.max_wall_seconds = 1;
    fs.writeFileSync(ledgerPath(original.agent.session.id), JSON.stringify(registration));
    await prompt(original.agent);
    const history = {meta: {...original.agent.session.header},
      events: Array.from({length: original.agent.session.seq}, (_, index) => structuredClone(original.agent.session.eventAt(index)))};
    assert.deepEqual(history.events.slice(-3).map(event => event.type), ['assistant/message', 'step/end', 'turn/end']);
    await original.dispose();
    const variants = [
      ['exact', () => {}],
      ['not-interrupted', events => {events.at(-3).data.interrupted = false;}],
      ['attempt', events => {events.at(-3).type = 'assistant/attempt'; delete events.at(-3).surfaceOp;}],
      ['missing-step-end', events => {events.splice(-2, 1);}],
      ['wrong-step', events => {events.at(-2).data.step++;}],
      ['wrong-step-turn', events => {events.at(-2).data.turn++;}],
      ['wrong-turn', events => {events.at(-1).data.turn++;}],
      ['missing-turn-end', events => {events.pop();}],
      ['wrong-reason', events => {events.at(-1).data.reason.reason.reason = 'shared-work-usage-unknown';}],
      ['wrong-reason-kind', events => {events.at(-1).data.reason.kind = 'completed';}],
      ['non-hook', events => {events.at(-1).data.reason.reason.kind = 'user';}],
      ['extra-reason', events => {events.at(-1).data.reason.extra = true;}],
      ['extra-hook-reason', events => {events.at(-1).data.reason.reason.extra = true;}],
      ['two-unknown', events => {events.push(...structuredClone(events.slice(-3)));}],
      ['unsealed-window', () => {}],
      ['current-window', () => {}],
    ];
    for (const [name, mutate] of variants) {
      const restored = structuredClone(history);
      mutate(restored.events);
      restored.events.forEach((event, index) => {event.seq = index;});
      const owned = await create('fixture-history-' + name, 'fixture-managed', restored);
      assert.deepEqual(Array.from({length: restored.events.length}, (_, index) => owned.agent.session.eventAt(index)), restored.events,
        'The original Session must retain the fixture history before executing its real Hook.');
      const requests = name === 'two-unknown' ? 2 : 1;
      const row = register(owned.agent, {state: {started_at_ms: Date.now() - 2000, requests, reported_tokens: 0,
        admissions: Array.from({length: requests}, (_, index) => 'fixture-old-' + index), stop_reason: 'shared-work-wall-budget'}});
      if (name !== 'current-window') row.renewWindow = {id: 'fixture-window-' + name, message_id: 'fixture-owner-' + name,
        host_lease: currentHostLease, request_base: requests, original_state: structuredClone(row.state),
        original_host_lease: 'fixture-pre-maintenance-lease', started_at_ms: null, stop_reason: null, hook_marker: null};
      if (name === 'unsealed-window') {
        row.state.stop_reason = null;
        row.renewWindow.original_state.stop_reason = null;
      }
      row.state.binding_sha256 = crypto.createHash('sha256').update(JSON.stringify({
        ...Object.fromEntries(['schema', 'service_ref', 'host_lease', 'agent_preset', 'session_id', 'session_created_at',
          'generation', 'workspace', 'provider', 'model', 'budget'].map(key => [key, row[key]])),
        ...(row.renewWindow ? {renewWindow: Object.fromEntries(Object.entries(row.renewWindow)
          .filter(([key]) => !['started_at_ms', 'stop_reason', 'hook_marker'].includes(key)))} : {})})).digest('hex');
      fs.writeFileSync(ledgerPath(owned.agent.session.id), JSON.stringify(row));
      const before = calls.length;
      await prompt(owned.agent);
      const after = JSON.parse(fs.readFileSync(ledgerPath(owned.agent.session.id)));
      if (name === 'exact') {
        assert.equal(calls.length, before + 1);
        assert.equal(after.state.requests, 2);
        assert.equal(after.state.reported_tokens, 20);
        assert.equal(after.renewWindow.hook_marker, 'same-sid-window-v1');
      } else {
        assert.equal(calls.length, before, name + ' must fail before model dispatch.');
        assert.deepEqual(after.state, row.state, name + ' must preserve the old reservation and tokens.');
        const turnEnd = owned.agent.session.eventAt(owned.agent.session.seq - 1);
        assert.equal(turnEnd.type, 'turn/end');
        assert.equal(turnEnd.data.reason.kind, 'error');
        assert.ok(JSON.stringify(turnEnd.data.reason).includes('Shared work native settlement usage is unknown.'), name);
      }
      await owned.dispose();
    }
    settlementEvidence = {accepted: ['exact'], rejected: variants.slice(1).map(([name]) => name)};
  } else if (scenario === 'renew_window' || scenario === 'renew_interrupted') {
    const renew = async (id, model = 'fixture-route') => {
      const original = await create(id);
      const registration = register(original.agent, {model});
      registration.budget.max_wall_seconds = 1;
      fs.writeFileSync(ledgerPath(id), JSON.stringify(registration));
      await prompt(original.agent);
      await new Promise(resolve => setTimeout(resolve, 1200));
      const raw = fs.readFileSync(ledgerPath(id));
      const row = JSON.parse(raw);
      assert.equal(row.state.stop_reason, 'shared-work-wall-budget');
      const oldEvents = Array.from({length: original.agent.session.seq}, (_, index) => structuredClone(original.agent.session.eventAt(index)));
      if (scenario === 'renew_interrupted') {
        assert.deepEqual(oldEvents.slice(-3).map(event => event.type), ['assistant/message', 'step/end', 'turn/end']);
        const [message, stepEnd, turnEnd] = oldEvents.slice(-3);
        assert.equal(message.data.interrupted, true);
        assert.equal(message.data.usage, undefined);
        assert.ok(!message.data.stream?.some(record => record.type === 'chunk' && record.chunk?.type === 'usage'));
        assert.equal(stepEnd.data.turn, message.data.turn);
        assert.equal(stepEnd.data.step, message.data.step);
        assert.equal(turnEnd.data.turn, message.data.turn);
        assert.deepEqual(turnEnd.data.reason, {kind: 'aborted', reason: {kind: 'hook', reason: 'shared-work-wall-budget'}});
        assert.equal(row.state.requests, 1);
        assert.equal(row.state.reported_tokens, 0, 'The interrupted request has no reported token proof.');
      }
      const createdAt = original.agent.session.header.createdAt;
      await original.dispose();
      fs.writeFileSync(ledgerPath(id) + '.old-window', raw, {mode: 0o600});
      row.renewWindow = {id: 'window-' + id, message_id: 'fixture-owner-' + id, host_lease: currentHostLease,
        request_base: row.state.requests, original_state: structuredClone(row.state),
        original_host_lease: 'fixture-pre-maintenance-lease', started_at_ms: null, stop_reason: null, hook_marker: null};
      row.state.binding_sha256 = crypto.createHash('sha256').update(JSON.stringify({
        ...Object.fromEntries(['schema', 'service_ref', 'host_lease', 'agent_preset', 'session_id', 'session_created_at',
          'generation', 'workspace', 'provider', 'model', 'budget'].map(key => [key, row[key]])),
        renewWindow: Object.fromEntries(Object.entries(row.renewWindow).filter(([key]) => !['started_at_ms', 'stop_reason', 'hook_marker'].includes(key)))})).digest('hex');
      fs.writeFileSync(ledgerPath(id), JSON.stringify(row));
      assert.equal(JSON.parse(fs.readFileSync(ledgerPath(id))).renewWindow.started_at_ms, null);
      const adoption = await ctx.typertGateway.dispatchRpc('session/create', {args: {request: {
        sessionId: id, cwd: workspace, agentPreset: config.agent_preset}}}, new AbortController().signal);
      assert.equal(adoption.ok, true, JSON.stringify(adoption));
      assert.deepEqual(adoption.value, {sessionId: id, agentPreset: config.agent_preset});
      const current = ctx.agents.get(id);
      assert.equal(current.session.header.createdAt, createdAt);
      assert.deepEqual(oldEvents.map((_, index) => current.session.eventAt(index)), oldEvents);
      await prompt(current);
      const after = JSON.parse(fs.readFileSync(ledgerPath(id)));
      assert.equal(after.renewWindow.hook_marker, 'same-sid-window-v1', 'Only the actual original AgentLoop beforeRequest writes its Hook marker.');
      assert.ok(after.renewWindow.started_at_ms > row.state.started_at_ms);
      assert.equal(after.state.started_at_ms, row.state.started_at_ms);
      assert.equal(after.state.stop_reason, row.state.stop_reason);
      assert.equal(after.state.requests, row.state.requests + 1);
      if (scenario === 'renew_interrupted') {
        assert.equal(after.renewWindow.request_base, 1, 'The uncertain old request remains reserved.');
        assert.equal(after.state.reported_tokens, 20, 'Only the new completed request reports tokens.');
        assert.deepEqual(after.state.admissions.slice(0, 1), row.state.admissions);
        assert.equal(calls.length, 2, 'The first new beforeRequest reserves exactly one additional request.');
        settlementEvidence = {history: oldEvents.slice(-3), old_state: row.state,
          resumed_state: after.state, window: after.renewWindow};
      }
      assert.deepEqual(after.renewWindow.original_state, row.renewWindow.original_state);
      assert.deepEqual(fs.readFileSync(ledgerPath(id) + '.old-window'), raw);
      return current;
    };
    if (scenario === 'renew_interrupted') {
      await renew('fixture-window-interrupted', 'fixture-window-interrupted');
    } else {
    const counted = await renew('fixture-window-count');
    await prompt(counted);
    await prompt(counted);
    const countedRow = JSON.parse(fs.readFileSync(ledgerPath(counted.session.id)));
    assert.equal(countedRow.state.requests - countedRow.renewWindow.request_base, 2);
    assert.equal(countedRow.state.requests, 3, 'Cumulative reservations are never reset.');
    assert.equal(countedRow.state.reported_tokens, 60);
    assert.equal(countedRow.renewWindow.stop_reason, 'shared-work-observed-budget');
    assert.equal(calls.length, 3, 'Exactly two new requests fit the explicit window.');
    const timed = await renew('fixture-window-wall', 'fixture-window-wait');
    const timedRow = JSON.parse(fs.readFileSync(ledgerPath(timed.session.id)));
    assert.equal(timedRow.renewWindow.stop_reason, 'shared-work-wall-budget');
    assert.equal(timedRow.state.stop_reason, 'shared-work-wall-budget');
    assert.equal(calls.length, 5, 'The new wall stop cancels the fifth external request.');
    }
  } else if (scenario === 'limits') {
    const tokens = await create('fixture-token-limit');
    const row = register(tokens.agent, {model: 'fixture-large-usage'});
    row.budget.max_reported_tokens = 300000;
    row.budget.max_output_tokens_per_request = 8192;
    row.state.effective_max_output_tokens = 8192;
    row.state.binding_sha256 = crypto.createHash('sha256').update(JSON.stringify(Object.fromEntries(
      ['schema', 'service_ref', 'host_lease', 'agent_preset', 'session_id', 'session_created_at',
        'generation', 'workspace', 'provider', 'model', 'budget'].map(key => [key, row[key]])))).digest('hex');
    fs.writeFileSync(ledgerPath(tokens.agent.session.id), JSON.stringify(row));
    await prompt(tokens.agent);
    assert.equal(calls[0].maxTokens, 12288, 'The original adapter default must not be capped at 8192.');
    const first = JSON.parse(fs.readFileSync(ledgerPath(tokens.agent.session.id)));
    assert.equal(first.state.reported_tokens, 300020);
    assert.equal(first.state.stop_reason, null, 'Reported usage above the historical token cap is accounting only.');
    assert.deepEqual(first.budget, row.budget, 'Historical token cap fields remain unmodified metadata.');
    assert.equal(first.state.binding_sha256, row.state.binding_sha256, 'The original four-field ledger binding is preserved.');
    assert.equal(first.state.effective_max_output_tokens, 8192, 'The historical output cap is neither enforced nor rewritten.');
    const tool = await ctx.tools.execute({agent: tokens.agent, name: 'job_list', callId: 'fixture-above-token-cap',
      arguments: {}, signal: new AbortController().signal});
    assert.equal(tool.isError, false, 'Accounted tokens cannot deny an owned allowed tool.');
    await prompt(tokens.agent);
    const second = JSON.parse(fs.readFileSync(ledgerPath(tokens.agent.session.id)));
    assert.equal(calls.length, 2, 'Token usage cannot deny an otherwise admitted second request.');
    assert.equal(second.state.reported_tokens, 600040);
    assert.equal(second.state.started_at_ms, first.state.started_at_ms);
    await prompt(tokens.agent);
    assert.equal(calls.length, 2, 'The request count still stops a third request.');
    assert.equal(JSON.parse(fs.readFileSync(ledgerPath(tokens.agent.session.id))).state.stop_reason,
      'shared-work-observed-budget');
    const upstream = await create('fixture-native-control');
    register(upstream.agent);
    await prompt(upstream.agent);
    assert.equal(calls.length, 3);
    assert.equal(calls.at(-1).maxTokens, 10000, 'An upstream native request control remains unchanged.');
    const expired = await create('fixture-expired');
    register(expired.agent, {state: {started_at_ms: Date.now() - 1800000, requests: 0,
      reported_tokens: 0, admissions: [], stop_reason: null}});
    await prompt(expired.agent);
    assert.equal(calls.length, 3, 'Expired work cannot emit its first model call.');
    await tokens.dispose();
    await upstream.dispose();
    await expired.dispose();
  } else {
  const managed = await create('fixture-owned');
  register(managed.agent);
  await prompt(managed.agent);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].model, 'fixture-route');
  assert.equal(calls[0].maxTokens, 12288);
  assert.equal(JSON.parse(fs.readFileSync(ledgerPath('fixture-owned'))).state.requests, 1);
  assert.equal(JSON.parse(fs.readFileSync(ledgerPath('fixture-owned'))).state.model_context_window, 16384,
    'The admitted model route must record original prepared context metadata.');
  await prompt(managed.agent);
  await prompt(managed.agent);
  assert.equal(calls.length, 2, 'The third model request must not reach the external model.');
  assert.equal(JSON.parse(fs.readFileSync(ledgerPath('fixture-owned'))).state.reported_tokens, 40);
  const manual = await create('fixture-manual-session', 'fixture-manual');
  await prompt(manual.agent);
  assert.equal(calls.length, 3, 'A sibling manual Session remains usable.');
  assert.equal(calls.at(-1).model, 'fixture-default');
  const absent = await create('fixture-no-ledger');
  await prompt(absent.agent);
  assert.equal(calls.length, 3, 'The managed preset cannot run unregistered Sessions.');
  const wrong = await create('fixture-wrong-owner');
  register(wrong.agent, {host_lease: 'foreign-lease'});
  await prompt(wrong.agent);
  assert.equal(calls.length, 3, 'A foreign host lease must fail before model dispatch.');
  await managed.dispose();
  await absent.dispose();
  await wrong.dispose();
  await removePreset();
  const reload = await ctx.agentPresets.register(declaration);
  const restored = await create('fixture-restored');
  register(restored.agent, {state: {started_at_ms: Date.now(), requests: 2,
    reported_tokens: 40, admissions: ['old-a', 'old-b'], stop_reason: null}});
  await prompt(restored.agent);
  assert.equal(calls.length, 3, 'Reload must honor durable admission count.');
  await restored.dispose();
  await reload();
  await prompt(manual.agent);
  assert.equal(calls.length, 4, 'Managed unloading does not disable the manual Agent.');
  await manual.dispose();
  }
  await removePreset();
  if (removeManual) await removeManual();
  process.stdout.write(JSON.stringify({native_shared_hook: 'passed', requests: calls.length,
    ...(teamEvidence ? {team: teamEvidence} : {}), ...(settlementEvidence ? {settlements: settlementEvidence} : {})}));
  }
} finally {
  await ctx.fiber.dispose();
}
