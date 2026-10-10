// A managed outer work owns this carrier; supervisor connections can come and go.
import fs from 'node:fs';
import net from 'node:net';
import path from 'node:path';
import crypto from 'node:crypto';
import {pathToFileURL} from 'node:url';
import {bootOwnedRuntime} from './owned_runtime.mjs';
import {createControllerGuard} from './trusted_controller.mjs';

const configPath = process.argv[2];
const config = JSON.parse(fs.readFileSync(configPath, 'utf8'));
let runtime, gateway, controllerGuard, ending = false;
const channels = new Set();
const empty = async function* () {};
const methods = new Set(['session/list', 'session/projections', 'session/page', 'session/create', 'session/prompt', 'session/cancel', 'session/rename', '$events/result', 'userQuestions/answer']);
const endpoints = new Set(['$events', 'session/control', 'session/follow', 'job/list', 'job/follow']);
const bound = 16 * 1024 * 1024;
const eventFrames = [], deliveries = new Map();
const approvalIntents = new Set();
const eventControl = new AbortController();
let eventClientId, eventFailure = false;
const quote = value => "'" + String(value).replaceAll("'", "'\\''") + "'";
const verificationDiagnostic = value => {
  let text = JSON.stringify(value);
  const key = config.runtime_configuration?.model?.configuration?.apiKeyEnv;
  if (key && process.env[key]) text = text.replaceAll(process.env[key], '[REDACTED_CREDENTIAL]');
  text = text.replace(/(?:github_pat_[A-Za-z0-9_]+|gh[pousr]_[A-Za-z0-9]+|sk-[A-Za-z0-9_-]{16,})/g, '[REDACTED_CREDENTIAL]');
  fs.writeFileSync(path.join(config.dsh_home, '.hermes-verification-diagnostic.json'), text, {mode: 0o600});
};
async function verifyTests(testFiles) {
  if (!config.test_python || !config.test_runner_path || !Array.isArray(testFiles) || !testFiles.length
      || testFiles.some(value => typeof value !== 'string' || value.startsWith('-') || path.isAbsolute(value)
        || !fs.realpathSync(path.join(config.workspace, value)).startsWith(config.workspace + path.sep)
        || !fs.statSync(path.join(config.workspace, value)).isFile())) throw new Error('Explicit bound repository tests required.');
  for (const source of [config.test_python, config.test_runner_path]) {
    const binding = config.source_bindings.find(item => item.path === source);
    if (!binding || crypto.createHash('sha256').update(fs.readFileSync(fs.realpathSync(source))).digest('hex') !== binding.sha256) throw new Error('Trusted test runner source changed.');
  }
  const agent = runtime.ctx.get('agents').get(config.session_id);
  if (!agent || agent.status !== 'idle' || agent.session.header.cwd !== config.workspace
      || agent.inbox.nextTurn.length || agent.inbox.nextStep.length) throw new Error('Original repository is not idle for verification.');
  const shell = runtime.ctx.get('shell');
  const readOnly = runtime.ctx.get('sandboxPolicy').resolve({session: agent.session, mode: 'read-only'});
  const receiptPath = path.join(config.dsh_home, '.hermes-test-verification.json');
  const previous = fs.existsSync(receiptPath) ? JSON.parse(fs.readFileSync(receiptPath, 'utf8')) : null;
  if (previous && previous.status !== 'completed') throw new Error('Original verification outcome is unknown; do not replay.');
  const execute = async (command, policy, signal) => {
    const spec = shell.resolve({command, workdir: config.workspace, sandboxPolicy: policy, signal,
      timeoutMs: 60000, onExpiry: 'kill', env: {PYTHONDONTWRITEBYTECODE: '1'}});
    let result;
    try {result = await (await shell.execute(spec)).result();}
    catch (error) {verificationDiagnostic({type: error.name, message: error.message}); throw error;}
    if (result.sandbox?.mode !== policy.mode || result.sandbox.denied || result.aborted || result.timedOut
        || result.stdout.truncated || result.stderr.truncated) {verificationDiagnostic(result); throw new Error('Original verification confinement or result is unconfirmed.');}
    return result;
  };
  return agent.runMaintenance(async signal => {
  const stateCommand = [config.test_python, '-I', '-B', config.test_runner_path, config.workspace, '--state'].map(quote).join(' ');
  const snapshot = await execute(stateCommand, readOnly, signal);
  if (snapshot.exitCode !== 0) {verificationDiagnostic(snapshot); throw new Error('Original verification source state is unavailable.');}
  const source = JSON.parse(snapshot.stdout.text);
  if (previous && previous.generation === config.generation && previous.session_id === config.session_id
      && JSON.stringify(previous.test_files) === JSON.stringify(testFiles)
      && previous.before_source_digest === source.source_digest && previous.source_commit === source.head
      && previous.before_workspace_status === source.workspace_status) return previous;
  const git = await execute('git -C ' + quote(config.workspace) + ' rev-parse --path-format=absolute --git-dir --git-common-dir', readOnly, signal);
  if (git.exitCode !== 0) throw new Error('Original Git metadata is unavailable.');
  const protectedRoots = [config.workspace, ...git.stdout.text.trim().split('\n').map(value => fs.realpathSync(value))];
  const artifactBase = path.join(config.dsh_home, '.hermes-verification-artifacts');
  fs.mkdirSync(artifactBase, {mode: 0o700, recursive: true});
  const artifacts = path.join(artifactBase, crypto.randomUUID());
  fs.mkdirSync(artifacts, {mode: 0o700});
  const policy = {...readOnly, mode: 'workspace-write', workspaceRoot: artifacts};
  const {writableRoots} = await import(pathToFileURL(path.join(config.runtime_package_root, '@deepseek-ai/dsh-sandbox/lib/index.js')));
  const overlaps = (left, right) => left === right || left.startsWith(right + path.sep) || right.startsWith(left + path.sep);
  if (writableRoots(policy).some(root => protectedRoots.some(protectedRoot => overlaps(root, protectedRoot)))) throw new Error('Original test writable roots overlap repository source or Git.');
  const command = [config.test_python, '-I', '-B', config.test_runner_path, config.workspace, artifacts, ...testFiles].map(quote).join(' ');
  fs.writeFileSync(receiptPath, JSON.stringify({generation: config.generation, session_id: config.session_id, status: 'intent'}), {mode: 0o600});
  const result = await execute(command, policy, signal);
  let test;
  try {test = JSON.parse(result.stdout.text);} catch {verificationDiagnostic(result); throw new Error('Trusted test runner result is unavailable.');}
  if (typeof test.executed_tests !== 'number' || JSON.stringify(test.artifact_roots) !== JSON.stringify([artifacts])) throw new Error('Trusted test runner result is malformed.');
  delete test.output;
  const receipt = {...test, generation: config.generation, session_id: config.session_id, native_identity: identity,
    test_files: testFiles,
    command, command_sha256: crypto.createHash('sha256').update(command).digest('hex'), cwd: config.workspace,
    actual_exit_code: result.exitCode, exit_code: result.exitCode, timed_out: result.timedOut, aborted: result.aborted,
    sandbox: result.sandbox, source_access: 'read-only', git_access: 'read-only'};
  verificationDiagnostic({...receipt, stderr: result.stderr});
  fs.writeFileSync(receiptPath, JSON.stringify({status: 'completed', ...receipt}), {mode: 0o600});
  return receipt;
  });
}
async function watchEvents() {
  const source = await gateway.wireStream.open('$events', {args: {}}, empty(), undefined, eventControl.signal);
  for await (const frame of source) {
    if (frame.type === 'ready') {eventClientId = frame.clientId; continue;}
    if (frame.type === 'waterfall') {
      if (frame.event === 'approval/request') {
        deliveries.set(frame.eventId, frame);
        if (frame.agentId === config.session_id) eventFrames.push(frame);
        continue;
      }
      if (frame.agentId !== config.session_id || frame.event !== 'user-questions/request') {
        await gateway.dispatchRpc('$events/result', {args: {clientId: eventClientId, eventId: frame.eventId, outcome: {kind: 'next'}}}, new AbortController().signal);
        continue;
      }
      deliveries.set(frame.eventId, frame);
      eventFrames.push(frame);
    } else if (frame.type === 'resolved') {
      deliveries.delete(frame.eventId);
      eventFrames.push(frame);
    }
    if (eventFrames.length > 10000) throw new Error('Owned event observation bound exceeded.');
  }
  if (!ending) eventFailure = true;
}
const send = (channel, value) => {
  const bytes = Buffer.from(JSON.stringify(value) + '\n');
  if (bytes.length > bound || channel.writableLength + bytes.length > bound) throw new Error('Owned frame bound exceeded.');
  channel.write(bytes);
};
const reply = (channel, id, value) => send(channel, {type: 'reply', id, ok: true, value});
const inputPath = path.join(config.dsh_home, '.hermes-first-input.json');
const saveInput = (receipt, receiptPath = inputPath) => {
  const temporary = receiptPath + '.tmp';
  const descriptor = fs.openSync(temporary, 'w', 0o600);
  try {fs.writeFileSync(descriptor, JSON.stringify(receipt)); fs.fsyncSync(descriptor);}
  finally {fs.closeSync(descriptor);}
  fs.renameSync(temporary, receiptPath);
};

async function shutdown() {
  if (ending) return;
  ending = true;
  eventControl.abort();
  for (const channel of channels) channel.destroy();
  server.close();
  if (runtime) await runtime.shutdown.shutdown(0);
}

const server = net.createServer(channel => {
  channels.add(channel);
  const streams = new Map();
  let buffered = Buffer.alloc(0);
  async function dispatch(message) {
    let peer;
    try {peer = controllerGuard.authorize(channel);}
    catch {
      if (!channel.destroyed) send(channel, {type: 'reply', id: message.id, ok: false, reason: 'controller_not_admitted'});
      return;
    }
    try {
      if (message.op === 'admit_controller') return reply(channel, message.id, controllerGuard.admit(peer, message.controller));
      if (message.op === 'attach') return reply(channel, message.id, identity);
      if (message.op === 'shutdown') {reply(channel, message.id, {accepted: true}); return setImmediate(() => void shutdown());}
      if (ending || !gateway) throw new Error('Owned runtime unavailable.');
      if (message.op === 'events') {
        if (!eventClientId || eventFailure) throw new Error('Original event observation unavailable.');
        return reply(channel, message.id, {client_id: eventClientId, generation: config.generation,
          session_id: config.session_id, frames: eventFrames});
      }
      if (message.op === 'verify_tests') return reply(channel, message.id, await verifyTests(message.test_files));
      if (message.op === 'call') {
        if (!methods.has(message.method)) throw new Error('Original method not admitted.');
        const [namespace, method] = message.method.split('/');
        const request = message.args?.request;
        if (namespace === 'session' && ['create', 'prompt', 'cancel', 'rename'].includes(method)
            && request?.sessionId !== config.session_id) throw new Error('Owned Session target differs.');
        if (method === 'create' && (request.cwd !== config.workspace || request.agentPreset !== 'hermes-owned')) throw new Error('Owned repository or preset differs.');
        if (namespace === '$events') {
          const args = message.args?.request ?? message.args, delivery = deliveries.get(args?.eventId);
          if (args?.clientId !== eventClientId || delivery?.agentId !== config.session_id
              || args.outcome?.kind !== 'result') throw new Error('Only the original pending delivery may receive a result.');
          const agent = runtime.ctx.get('agents').get(config.session_id);
          if (!agent || agent.session.header.cwd !== config.workspace) throw new Error('Original result Session is unavailable or differs.');
          if (delivery.event === 'approval/request') {
            const binding = message.args?._hermes_approval;
            const keys = ['approval_id', 'call_id', 'command_sha256', 'generation', 'session_id'];
            if (!binding || typeof binding !== 'object' || Array.isArray(binding)
                || Object.keys(binding).length !== keys.length || keys.some(key => typeof binding[key] !== 'string' || !binding[key])
                || binding.generation !== config.generation || binding.session_id !== config.session_id
                || !/^[a-f0-9]{64}$/.test(binding.command_sha256)
                || delivery.request.toolName !== 'bash' || delivery.request.callId !== binding.call_id
                || !['allowed-once', 'rejected'].includes(args.outcome.value) || approvalIntents.has(args.eventId)) {
              throw new Error('Original single-operation approval binding differs.');
            }
            const events = agent.session.snapshotEvents();
            const asks = events.filter(event => event.type === 'approval/asked' && event.data.id === binding.approval_id);
            const pendingAsks = events.filter(event => event.type === 'approval/asked' && event.data.callId === binding.call_id
              && event.data.toolName === 'bash' && !events.some(decision => decision.type === 'approval/decided' && decision.data.id === event.data.id));
            const calls = events.filter(event => event.type === 'tool/call' && event.data.callId === binding.call_id);
            if (asks.length !== 1 || pendingAsks.length !== 1 || pendingAsks[0].data.id !== binding.approval_id
                || calls.length !== 1 || asks[0].data.callId !== binding.call_id
                || asks[0].data.toolName !== 'bash' || calls[0].data.name !== 'bash'
                || calls[0].seq >= asks[0].seq || events.some(event => event.type === 'approval/decided' && event.data.id === binding.approval_id)
                || events.some(event => event.type === 'tool/result' && event.data.message.toolCallId === binding.call_id)) {
              throw new Error('Original approval is absent, ambiguous, decided or already settled.');
            }
            const turn = events.findLast(event => event.type === 'turn/start' && event.seq < asks[0].seq);
            if (!turn || calls[0].seq <= turn.seq || calls[0].data.turn !== turn.data.turn
                || events.some(event => event.seq > turn.seq && (event.type === 'turn/end' && event.data.turn === turn.data.turn
                    || event.type === 'turn/start'))) throw new Error('Original approval turn is no longer current.');
            const operation = typeof calls[0].data.arguments === 'string' ? JSON.parse(calls[0].data.arguments) : null;
            if (typeof operation?.command !== 'string' || operation.sandbox_permissions === 'danger-full-access'
                || crypto.createHash('sha256').update(operation.command).digest('hex') !== binding.command_sha256) {
              throw new Error('Original approved command differs or requests an unsupported capability.');
            }
            approvalIntents.add(args.eventId);
          } else if (delivery.event === 'user-questions/request') {
            const active = runtime.ctx.get('sessionProjections').stateOf(agent.session, 'userQuestions')?.questions.active;
            if (!active?.some(row => row.callId === delivery.request.wait?.callId && row.state === 'open')) throw new Error('Original question is no longer open.');
          } else throw new Error('Original delivery result is not admitted.');
          const nativeArgs = {clientId: args.clientId, eventId: args.eventId, outcome: args.outcome};
          const result = await gateway.dispatchRpc('$events/result', {args: nativeArgs}, new AbortController().signal);
          if (result.ok && result.value === undefined) result.value = null;
          return reply(channel, message.id, {type: 'server-response', rpcId: message.rpcId, result});
        }
        if (namespace === 'userQuestions' && (message.args?.request ?? message.args)?.agentId !== config.session_id) throw new Error('Owned answer Session differs.');
        let input, receiptPath = inputPath;
        if (method === 'prompt') {
          if (message.args?._hermes_question_source) {
            const source = message.args._hermes_question_source;
            const agent = runtime.ctx.get('agents').get(config.session_id);
            const events = agent?.session.snapshotEvents() ?? [];
            const original = events.find(event => event.seq === source.seq && event.type === 'assistant/message');
            const text = original?.data.message.content.filter(block => block.type === 'text').map(block => block.text).join('\n');
            if (!agent || agent.inbox.nextTurn.length || agent.inbox.nextStep.length || typeof text !== 'string'
                || crypto.createHash('sha256').update(text).digest('hex') !== source.sha256
                || events.some(event => event.seq > source.seq && ['assistant/message', 'assistant/attempt', 'user/message', 'tool/call'].includes(event.type))) {
              throw new Error('Original ordinary question changed or was already handled.');
            }
          }
          if (!request.requestId || !['queue', 'steer'].includes(request.mode)) throw new Error('Work input requires a fixed request identity.');
          const digest = crypto.createHash('sha256').update(JSON.stringify(request)).digest('hex');
          if (fs.existsSync(inputPath)) {
            const first = JSON.parse(fs.readFileSync(inputPath, 'utf8'));
            if (first.generation !== config.generation || first.status !== 'accepted') throw new Error('Original first input remains unknown; do not resend.');
            if (first.request_id !== request.requestId) {
              const directory = path.join(config.dsh_home, '.hermes-inputs');
              fs.mkdirSync(directory, {mode: 0o700, recursive: true});
              receiptPath = path.join(directory, crypto.createHash('sha256').update(request.requestId).digest('hex') + '.json');
            }
          } else if (request.mode !== 'queue') throw new Error('Original first input must be queued.');
          if (fs.existsSync(receiptPath)) {
            input = JSON.parse(fs.readFileSync(receiptPath, 'utf8'));
            if (input.generation !== config.generation || input.request_id !== request.requestId || input.sha256 !== digest
                || input.status !== 'accepted') throw new Error('Original input remains unknown or differs; do not resend.');
            return reply(channel, message.id, {type: 'server-response', rpcId: message.rpcId, result: input.result});
          }
          input = {generation: config.generation, session_id: config.session_id, request_id: request.requestId, sha256: digest, status: 'intent'};
          saveInput(input, receiptPath);
        }
        let result;
        const callArgs = namespace === 'userQuestions' ? (message.args?.request ?? message.args)
          : method === 'prompt' ? {request} : message.args;
        try { result = {ok: true, value: await gateway.invoke({namespace, method, args: callArgs, signal: new AbortController().signal})}; }
        catch (error) {result = {ok: false, error: gateway.wireStream.failure(error)};}
        if (input && result.ok && result.value?.accepted === true) {input.status = 'accepted'; input.result = result; saveInput(input, receiptPath);}
        return reply(channel, message.id, {type: 'server-response', rpcId: message.rpcId, result});
      }
      if (message.op === 'open') {
        if (!endpoints.has(message.endpoint) || streams.has(message.id)) throw new Error('Stream not admitted.');
        const control = new AbortController();
        streams.set(message.id, control);
        const source = await gateway.wireStream.open(message.endpoint, {args: message.args}, empty(), undefined, control.signal);
        reply(channel, message.id, null);
        try { for await (const value of source) send(channel, {type: 'item', id: message.id, value});
          send(channel, {type: 'end', id: message.id}); }
        catch {if (!channel.destroyed) send(channel, {type: 'end', id: message.id, failed: true});}
        finally {streams.delete(message.id);}
        return;
      }
      if (message.op === 'cancel') {streams.get(message.streamId)?.abort(); return reply(channel, message.id, null);}
      throw new Error('Owned operation not admitted.');
    } catch {if (!channel.destroyed) send(channel, {type: 'reply', id: message.id, ok: false});}
  }
  channel.on('data', chunk => {
    buffered = Buffer.concat([buffered, chunk]);
    while (buffered.includes(10)) {
      const index = buffered.indexOf(10);
      if (index > bound) return channel.destroy();
      const line = buffered.subarray(0, index); buffered = buffered.subarray(index + 1);
      try {void dispatch(JSON.parse(line));} catch {channel.destroy();}
    }
    if (buffered.length > bound) channel.destroy();
  });
  channel.on('close', () => {channels.delete(channel); for (const control of streams.values()) control.abort();});
  channel.on('error', () => channel.destroy());
});
const identity = {pid: process.pid, created_at_ms: Date.now(), generation: config.generation, instance_id: config.instance_id,
  configuration_sha256: config.configuration_sha256,
  version: JSON.parse(fs.readFileSync(path.join(config.runtime_package_root, '@deepseek-ai/dsh/package.json'), 'utf8')).version};
process.once('exit', code => {
  fs.writeFileSync(config.exit_path, JSON.stringify({...identity, exit_code: code, shutdown_requested: ending}), {mode: 0o600});
});
try {
  const {writableRoots} = await import(pathToFileURL(path.join(config.runtime_package_root, '@deepseek-ai/dsh-sandbox/lib/index.js')));
  fs.mkdirSync(path.join(config.dsh_home, 'tmp'), {recursive: true, mode: 0o700});
  controllerGuard = createControllerGuard(config, writableRoots({mode: 'workspace-write', workspaceRoot: config.workspace,
    sessionId: config.session_id}));
  runtime = await bootOwnedRuntime(config);
  gateway = runtime.ctx.get('typertGateway');
  void watchEvents().catch(() => {eventFailure = true;});
  while (!eventClientId && !eventFailure) await new Promise(resolve => setTimeout(resolve, 5));
  if (eventFailure) throw new Error('Original event observation unavailable.');
  await new Promise((resolve, reject) => {server.once('error', reject); server.listen(config.socket_path, resolve);});
  fs.chmodSync(config.socket_path, 0o600);
  fs.writeFileSync(config.identity_path, JSON.stringify(identity), {mode: 0o600});
} catch {
  process.exitCode = 1;
  await shutdown();
}
