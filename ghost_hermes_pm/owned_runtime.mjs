// One instance-local composition over the unmodified original DSH public APIs.
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import {pathToFileURL} from 'node:url';

export async function bootOwnedRuntime(config) {
  for (const binding of config.source_bindings ?? []) {
    const resolved = fs.realpathSync(binding.path);
    const info = fs.statSync(resolved, {bigint: true});
    if (resolved !== binding.resolved || String(info.dev) !== binding.device || String(info.ino) !== binding.inode
        || binding.sha256 && crypto.createHash('sha256').update(fs.readFileSync(resolved)).digest('hex') !== binding.sha256) {
      throw new Error('Original owned source identity changed.');
    }
  }
  const home = fs.lstatSync(config.dsh_home, {bigint: true});
  if (!home.isDirectory() || home.uid !== BigInt(process.getuid()) || (home.mode & 0o077n) !== 0n
      || fs.realpathSync(config.dsh_home) !== config.dsh_home) throw new Error('Owned home is not private.');
  const {runProfile} = await import(pathToFileURL(path.join(config.runtime_package_root, '@deepseek-ai/dsh/lib/profile-boot.js')));
  const {createLaunchEnvironmentSnapshot} = await import(pathToFileURL(path.join(config.runtime_package_root, '@deepseek-ai/dsh-launch-environment/lib/index.js')));
  const disabled = ['preset-standard', 'preset-minimal', 'preset-ptc', 'preset-cordis', 'terminal-controller', 'ui-sidebar-terminal',
    'cordis-host-runner', 'cordis-client-runner', 'cordis-inspect-providers', 'ui-cordis', 'ui-plugin-manager', 'tool-plugin-manager',
    'schedule', 'ui-schedule', 'mcp-resources', 'session-title-llm', 'tool-working-directory', 'permission'];
  const plugins = [
    {id: 'persona', name: '@deepseek-ai/dsh-persona', config: {prefix: 'Work in the bound repository. Follow its instructions and the installed Matt workflow. Report real tool results and ask the user about unresolved decisions.', complete: true, includeRuntimeContext: false}},
    {id: 'tool-bash', name: '@deepseek-ai/dsh-tool-bash', config: {enableRunInBackground: false, promoteOnTimeout: false}},
    {id: 'tool-jobs', name: '@deepseek-ai/dsh-tool-jobs', config: {completionDelivery: 'quiet'}},
  ];
  const settings = config.runtime_configuration ?? {};
  if (settings.skill_directories?.length) plugins.push(
    {id: 'skill-filesystem', name: '@deepseek-ai/dsh-skill-filesystem', config: {customSkillDirs: settings.skill_directories, includeDefaultRoots: false, watch: false}},
    {id: 'tool-skill', name: '@deepseek-ai/dsh-tool-skill'},
  );
  const overlay = [
    ...disabled.map(id => ({id, disabled: true})),
    {id: 'web-runtime', config: {openBrowser: false, printUrl: false, surfaceContext: false, trustedHosts: []}},
    {id: 'webserver', config: {host: '127.0.0.1', port: 0}},
    {id: 'tools', config: {mode: 'native'}},
    {id: 'sandbox-policy', config: {mode: 'workspace-write', workspaceRoot: config.workspace}},
    {id: 'agent-preset-registry', config: {default: 'hermes-owned', selectedDefault: 'hermes-owned'}},
    {insert: [
      {id: 'owned-permission', name: '@deepseek-ai/dsh-permission-presets', config: {presets: {
        'read-only': {sandbox: 'read-only', approval: 'ask'}, 'workspace-write': {sandbox: 'workspace-write', approval: 'ask'}}}},
      {id: 'preset-hermes-owned', name: '@deepseek-ai/dsh-agent-preset', config: {id: 'hermes-owned', plugins}},
    ]},
  ];
  if (settings.model) overlay.push(
    {id: 'agent-default-model', config: {provider: settings.model.provider, model: settings.model.model}},
    {id: 'llm-pi-ai', config: {providers: {[settings.model.provider]: settings.model.configuration}}},
  );
  const patch = path.join(config.dsh_home, '.hermes-owned-overlay.json');
  fs.writeFileSync(patch, JSON.stringify(overlay), {mode: 0o600});
  const runtime = await runProfile({environment: createLaunchEnvironmentSnapshot([{source: 'process', values: {...process.env}}]),
    profile: 'hermes-owned', patchFiles: [patch], args: ['--host', '127.0.0.1', '--port', '0', '--no-open'],
    ...(!fs.existsSync(path.join(config.dsh_home, 'profiles/hermes-owned/package.json')) ? {fromDefaultProfile: 'web'} : {}),
  });
  const inventory = await runtime.ctx.get('agentPresets').compositionInventory();
  if (inventory.length !== 1 || inventory[0].id !== 'hermes-owned' || inventory[0].broken
      || runtime.ctx.get('shell').sandboxMode !== 'workspace-write'
      || ['terminalController', 'dynamicCordisRunner', 'cordisInspect'].some(name => runtime.ctx.get(name) !== undefined)) {
    await runtime.shutdown.shutdown(1);
    throw new Error('The original owned composition is unavailable.');
  }
  const release = runtime.ctx.get('tools').guard(exec => {
    if (!['bash', 'job_kill', 'job_list', 'job_output', ...(settings.skill_directories?.length ? ['skill'] : [])].includes(exec.name)) return 'Unverified owned capability rejected.';
    const session = exec.agent?.session;
    if (!session || session.id !== config.session_id || !session.header.cwd
        || fs.realpathSync(session.header.cwd) !== config.workspace) return 'Owned Session binding rejected.';
    const policy = runtime.ctx.get('sandboxPolicy').resolve({session});
    if (!['read-only', 'workspace-write'].includes(policy.mode)) return 'Unsupported sandbox rejected.';
    if (exec.arguments?.sandbox_permissions === 'danger-full-access') return 'Danger permission rejected.';
    if (exec.arguments?.run_in_background === true) return 'Unverified background start rejected.';
  });
  runtime.ctx.effect(() => release);
  runtime.ctx.on('tools/result', (exec, result) => {
    if (exec.agent?.session?.id !== config.session_id) return;
    const value = result.value;
    const output = part => ({sha256: crypto.createHash('sha256').update(part.text).digest('hex'),
      byte_length: Buffer.byteLength(part.text), truncated: part.truncated});
    const row = {generation: config.generation, session_id: config.session_id, call_id: exec.callId,
      name: exec.name, is_error: result.isError,
      ...(value?.kind === 'foreground' ? {kind: value.kind, exit_code: value.exitCode,
        sandbox: value.sandbox, aborted: value.aborted, timed_out: value.timedOut,
        stdout: output(value.stdout), stderr: output(value.stderr)} : {}),
      ...(exec.name === 'skill' && typeof value?.name === 'string' ? {skill_name: value.name,
        skill_content_sha256: typeof value.content === 'string' ? crypto.createHash('sha256').update(value.content).digest('hex') : null} : {})};
    fs.appendFileSync(path.join(config.dsh_home, '.hermes-tool-receipts.jsonl'), JSON.stringify(row) + '\n', {mode: 0o600});
  });
  if (settings.budget) {
    const budget = settings.budget;
    const statePath = path.join(config.dsh_home, '.hermes-budget.json');
    const state = fs.existsSync(statePath) ? JSON.parse(fs.readFileSync(statePath, 'utf8'))
      : {generation: config.generation, started_at_ms: null, requests: 0, stop_reason: null, usage: null};
    if (state.generation !== config.generation) {await runtime.shutdown.shutdown(1); throw new Error('Original budget generation changed.');}
    const save = () => fs.writeFileSync(statePath, JSON.stringify(state), {mode: 0o600});
    const readUsage = session => runtime.ctx.get('sessionProjections').stateOf(session, 'tokenUsage');
    const stopped = agent => {
      if (state.stop_reason) {agent.cancel({kind: 'hook', reason: state.stop_reason}, {keepInbox: false}); return true;}
      if (state.started_at_ms !== null && Date.now() - state.started_at_ms >= budget.max_wall_seconds * 1000) {
        state.stop_reason = 'owned-budget-deadline'; save();
        agent.cancel({kind: 'hook', reason: state.stop_reason}, {keepInbox: false}); return true;
      }
      return false;
    };
    runtime.ctx.on('agent/request', async (payload, next) => {
      const agent = payload.agent;
      if (agent.session.id !== config.session_id) throw new Error('Owned model Session differs.');
      if (state.started_at_ms === null) state.started_at_ms = Date.now();
      state.usage = readUsage(agent.session) ?? null;
      const knownTokens = state.usage ? Object.values(state.usage).reduce((sum, value) => sum + (typeof value === 'number' ? value : 0), 0) : null;
      if (state.requests >= budget.max_model_requests || knownTokens !== null && knownTokens >= budget.max_reported_tokens) {
        state.stop_reason = 'owned-budget-observed-limit'; save();
      }
      if (stopped(agent)) throw new Error('Owned budget stopped this work.');
      const request = await next();
      if (request.provider !== settings.model.provider || request.model !== settings.model.model) throw new Error('Owned model route changed.');
      state.requests++; save();
      return {...request, maxTokens: budget.max_output_tokens_per_request};
    });
    runtime.ctx.on('session/event', session => {
      if (session.id === config.session_id) {state.usage = readUsage(session) ?? null; save();}
    });
    const timer = setInterval(() => {
      const agent = runtime.ctx.get('agents').get(config.session_id);
      if (agent) stopped(agent);
    }, 100);
    runtime.ctx.effect(() => () => clearInterval(timer));
  }
  return runtime;
}
