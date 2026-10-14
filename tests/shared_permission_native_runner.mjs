// The original command Gateway and permission writer; no Host or model input.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';

const root = process.env.DSH_TEST_SDK_ROOT;
const workspace = fs.realpathSync(process.argv[2]);
const [initialMode, initialApproval] = (process.argv[3] ?? 'danger-full-access/never').split('/');
const url = name => pathToFileURL(path.join(root, `@deepseek-ai/${name}/lib/index.js`)).href;
const {Context} = await import(url('cordis'));
const {Loader} = await import(url('cordis-plugin-loader'));
const ctx = new Context();
await ctx.plugin(Loader, {baseUrl: pathToFileURL(root + '/').href});
const configs = {'dsh-sandbox-policy': {mode: initialMode, workspaceRoot: workspace},
  'dsh-working-directory': {defaultDirectory: workspace}, 'dsh-user-approval': {policy: initialApproval},
  'dsh-permission-presets': {defaultPreset: initialMode, presets: {
    'read-only': {sandbox: 'read-only', approval: 'ask'},
    'workspace-write': {sandbox: 'workspace-write', approval: 'ask'},
    'danger-full-access': {sandbox: 'danger-full-access', approval: 'never'}}}};
try {
  for (const name of ['dsh-typert-registry', 'dsh-api-gateway', 'dsh-session', 'dsh-session-projection',
    'dsh-system-prompt', 'dsh-sandbox-policy', 'dsh-tools', 'dsh-llm', 'dsh-agent', 'dsh-agent-loop',
    'dsh-fs-local', 'dsh-working-directory', 'dsh-shell-env', 'dsh-sandbox-local',
    'dsh-subprocess-local', 'dsh-bash-sandbox', 'dsh-user-approval', 'dsh-commands', 'dsh-permission-presets']) {
    await ctx.loader.create({id: name, name: url(name), config: configs[name]});
  }
  await ctx.loader.await();
  assert.ok(ctx.get('commands') && ctx.get('permissionPresets') && ctx.get('typertGateway'));
  const {TYPERT} = await import(pathToFileURL(path.join(root, '@deepseek-ai/dsh-commands/lib/typert.host.js')).href);
  ctx.typert.register(TYPERT);
  const handle = await ctx.agents.create({sessionId: 'fixture-permission-session', meta: {cwd: workspace}});
  try {
    const response = await ctx.typertGateway.dispatchRpc('commands/execute', {args: {
      agentId: handle.agent.id, line: '/permission workspace-write', submittedAttachments: []}}, new AbortController().signal);
    assert.equal(response.ok, true);
    const events = Array.from({length: handle.agent.session.seq}, (_, index) => handle.agent.session.eventAt(index));
    const projections = ctx.sessionProjections.snapshot(handle.agent.session, ['permissions', 'inbox']).values;
    assert.equal(events.some(event => ['turn/start', 'user/message', 'assistant/message', 'tool/call'].includes(event.type)), false);
    process.stdout.write(JSON.stringify({response: response.value, events, projections}));
  } finally {await handle.dispose();}
} finally {await ctx.fiber.dispose();}
