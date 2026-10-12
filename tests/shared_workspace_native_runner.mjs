// Original Workspace and Session Gateway contracts, without a Host or model input.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';

const root = process.env.DSH_TEST_SDK_ROOT;
const scratch = fs.realpathSync(process.argv[2]);
const workspace = path.join(scratch, 'repository');
fs.mkdirSync(workspace, {recursive: true});
const url = name => pathToFileURL(path.join(root, `@deepseek-ai/${name}/lib/index.js`)).href;
const {Context} = await import(url('cordis'));
const {Loader} = await import(url('cordis-plugin-loader'));
const sid = 'fixture-workspace-session';
let modelRequests = 0;
const boot = async () => {
  const ctx = new Context();
  ctx.on('agent/request', () => {modelRequests++;});
  await ctx.plugin(Loader, {baseUrl: pathToFileURL(root + '/').href});
  const configs = {
    'dsh-storage-json': {root: path.join(scratch, 'domain')},
    'dsh-storage-domain': {backend: 'json'},
    'dsh-session-persistence-jsonl': {root: path.join(scratch, 'sessions')},
    'dsh-working-directory': {defaultDirectory: workspace},
    'dsh-sandbox-policy': {mode: 'workspace-write', workspaceRoot: workspace},
    'dsh-permission-presets': {defaultPreset: 'workspace-write'},
    'dsh-attachment-local': {dshHome: path.join(scratch, 'attachments')},
    'dsh-agent-default-model': {provider: 'fixture-uninvoked', model: 'fixture-uninvoked'},
    'dsh-credentials-local': {path: path.join(scratch, 'fixture-credentials.yaml'), watch: false},
  };
  for (const name of ['dsh-typert-registry', 'dsh-api-gateway', 'dsh-storage', 'dsh-storage-json',
    'dsh-storage-domain', 'dsh-session', 'dsh-session-persistence-jsonl', 'dsh-session-query',
    'dsh-session-projection', 'dsh-system-prompt', 'dsh-sandbox-policy', 'dsh-tools', 'dsh-llm',
    'dsh-agent', 'dsh-agent-loop', 'dsh-agent-default-model', 'dsh-fs-local', 'dsh-working-directory',
    'dsh-shell-env', 'dsh-sandbox-local', 'dsh-subprocess-local', 'dsh-bash-sandbox', 'dsh-user-approval',
    'dsh-commands', 'dsh-permission-presets', 'dsh-attachment-local', 'dsh-credentials-local', 'dsh-client-connection', 'dsh-client-file-upload',
    'dsh-workspace', 'dsh-api-workspace-controller', 'dsh-api-session-controller']) {
    await ctx.loader.create({id: name, name: url(name), config: configs[name]});
  }
  await ctx.loader.await();
  for (const name of ['dsh-api-workspace-controller', 'dsh-api-session-controller']) {
    const {TYPERT} = await import(pathToFileURL(path.join(root, `@deepseek-ai/${name}/lib/typert.host.js`)).href);
    ctx.typert.register(TYPERT);
  }
  return ctx;
};
const call = async (ctx, method, request) => {
  const response = await ctx.typertGateway.dispatchRpc(method, {args: {request}}, new AbortController().signal);
  assert.equal(response.ok, true, JSON.stringify(response));
  return response.value;
};
let ctx = await boot();
try {
  const original = await call(ctx, 'session/create', {sessionId: sid, cwd: workspace});
  const before = ctx.sessions.get(sid);
  const header = {...before.header};
  const events = Array.from({length: before.seq}, (_, index) => before.eventAt(index));
  const created = await call(ctx, 'workspace/create', {path: workspace});
  assert.equal(created.created, true);
  const repeated = await call(ctx, 'workspace/create', {path: workspace});
  assert.equal(repeated.created, false);
  assert.equal(repeated.workspace.workspaceId, created.workspace.workspaceId);
  assert.deepEqual(await call(ctx, 'session/create', {sessionId: sid, workspaceId: created.workspace.workspaceId}), original);
  assert.deepEqual(ctx.sessions.get(sid).header, header);
  assert.deepEqual(Array.from({length: before.seq}, (_, index) => before.eventAt(index)), events);
  assert.deepEqual(ctx.workspaceRegistry.get(created.workspace.workspaceId).sessionIds, [sid]);
  const both = await ctx.typertGateway.dispatchRpc('session/create', {args: {request: {
    sessionId: sid, workspaceId: created.workspace.workspaceId, cwd: workspace}}}, new AbortController().signal);
  assert.equal(both.ok, false);
  await ctx.fiber.dispose();
  ctx = await boot();
  const restored = await call(ctx, 'workspace/create', {path: workspace});
  assert.equal(restored.created, false);
  assert.equal(restored.workspace.workspaceId, created.workspace.workspaceId);
  assert.deepEqual(restored.workspace.sessionIds, [sid]);
  assert.equal(modelRequests, 0);
  process.stdout.write(JSON.stringify({created, repeated, restored, original, header, events,
    cwd_xor_refused: true, model_requests: modelRequests}));
} finally {await ctx.fiber.dispose();}
