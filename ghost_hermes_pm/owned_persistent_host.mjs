// A managed outer work owns this carrier; supervisor connections can come and go.
import fs from 'node:fs';
import net from 'node:net';
import path from 'node:path';
import crypto from 'node:crypto';
import {bootOwnedRuntime} from './owned_runtime.mjs';

const configPath = process.argv[2];
const config = JSON.parse(fs.readFileSync(configPath, 'utf8'));
let runtime, gateway, ending = false;
const channels = new Set();
const empty = async function* () {};
const methods = new Set(['session/list', 'session/projections', 'session/page', 'session/create', 'session/prompt', 'session/cancel']);
const endpoints = new Set(['$events', 'session/control', 'session/follow', 'job/list', 'job/follow']);
const bound = 16 * 1024 * 1024;
const send = (channel, value) => {
  const bytes = Buffer.from(JSON.stringify(value) + '\n');
  if (bytes.length > bound || channel.writableLength + bytes.length > bound) throw new Error('Owned frame bound exceeded.');
  channel.write(bytes);
};
const reply = (channel, id, value) => send(channel, {type: 'reply', id, ok: true, value});
const inputPath = path.join(config.dsh_home, '.hermes-first-input.json');
const saveInput = receipt => {
  const temporary = inputPath + '.tmp';
  const descriptor = fs.openSync(temporary, 'w', 0o600);
  try {fs.writeFileSync(descriptor, JSON.stringify(receipt)); fs.fsyncSync(descriptor);}
  finally {fs.closeSync(descriptor);}
  fs.renameSync(temporary, inputPath);
};

async function shutdown() {
  if (ending) return;
  ending = true;
  for (const channel of channels) channel.destroy();
  server.close();
  if (runtime) await runtime.shutdown.shutdown(0);
}

const server = net.createServer(channel => {
  channels.add(channel);
  const streams = new Map();
  let buffered = Buffer.alloc(0);
  async function dispatch(message) {
    try {
      if (message.op === 'attach') return reply(channel, message.id, identity);
      if (message.op === 'shutdown') {reply(channel, message.id, {accepted: true}); return setImmediate(() => void shutdown());}
      if (ending || !gateway) throw new Error('Owned runtime unavailable.');
      if (message.op === 'call') {
        if (!methods.has(message.method)) throw new Error('Original method not admitted.');
        const [namespace, method] = message.method.split('/');
        const request = message.args?.request;
        if (namespace === 'session' && ['create', 'prompt', 'cancel'].includes(method)
            && request?.sessionId !== config.session_id) throw new Error('Owned Session target differs.');
        if (method === 'create' && (request.cwd !== config.workspace || request.agentPreset !== 'hermes-owned')) throw new Error('Owned repository or preset differs.');
        let input;
        if (method === 'prompt') {
          if (!request.requestId || request.mode !== 'queue') throw new Error('First work input requires a fixed request identity.');
          const digest = crypto.createHash('sha256').update(JSON.stringify(request)).digest('hex');
          if (fs.existsSync(inputPath)) {
            input = JSON.parse(fs.readFileSync(inputPath, 'utf8'));
            if (input.generation !== config.generation || input.request_id !== request.requestId || input.sha256 !== digest
                || input.status !== 'accepted') throw new Error('Original first input remains unknown or differs; do not resend.');
            return reply(channel, message.id, {type: 'server-response', rpcId: message.rpcId, result: input.result});
          }
          input = {generation: config.generation, session_id: config.session_id, request_id: request.requestId, sha256: digest, status: 'intent'};
          saveInput(input);
        }
        let result;
        try { result = {ok: true, value: await gateway.invoke({namespace, method, args: message.args, signal: new AbortController().signal})}; }
        catch (error) {result = {ok: false, error: gateway.wireStream.failure(error)};}
        if (input && result.ok && result.value?.accepted === true) {input.status = 'accepted'; input.result = result; saveInput(input);}
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
  runtime = await bootOwnedRuntime(config);
  gateway = runtime.ctx.get('typertGateway');
  await new Promise((resolve, reject) => {server.once('error', reject); server.listen(config.socket_path, resolve);});
  fs.chmodSync(config.socket_path, 0o600);
  fs.writeFileSync(config.identity_path, JSON.stringify(identity), {mode: 0o600});
} catch {
  process.exitCode = 1;
  await shutdown();
}
