// Private child carrier for the original public DSH profile and Gateway APIs.
import fs from 'node:fs';
import net from 'node:net';
import path from 'node:path';
import {pathToFileURL} from 'node:url';

const bound = 16 * 1024 * 1024;
const channel = new net.Socket({fd: Number(process.env.HERMES_PM_IPC_FD), readable: true, writable: true});
let runtime, gateway, buffered = Buffer.alloc(0), ending = false, shutdown;
const streams = new Map();
const send = value => {
  const bytes = Buffer.from(JSON.stringify(value) + '\n');
  if (bytes.length > bound || channel.writableLength + bytes.length > bound) {
    channel.destroy();
    throw new Error('Carrier frame bound exceeded.');
  }
  channel.write(bytes);
};
const reply = (id, value) => send({type: 'reply', id, ok: true, value});
const reject = id => send({type: 'reply', id, ok: false});
const empty = async function* () {};
const allowed = new Set(['session/list', 'session/projections', 'session/page', 'session/create',
  'session/prompt', 'session/cancel']);
const streamAllowed = new Set(['$events', 'session/control', 'session/follow', 'job/list', 'job/follow']);

const endOwned = () => {
  ending = true;
  for (const entry of streams.values()) entry.control.abort();
  if (runtime) return shutdown ??= runtime.shutdown.shutdown(0);
  process.exitCode = 0;
};

function verifyPaths(config) {
  const home = fs.lstatSync(config.dsh_home, {bigint: true});
  if (!home.isDirectory() || home.uid !== BigInt(process.getuid()) || (home.mode & 0o077n) !== 0n
      || fs.realpathSync(config.dsh_home) !== config.dsh_home
      || String(home.dev) !== config.home_identity?.device || String(home.ino) !== config.home_identity?.inode) {
    throw new Error('The original owned home identity changed.');
  }
  if (!Array.isArray(config.source_bindings) || config.source_bindings.length < 6) {
    throw new Error('Original source path bindings are absent.');
  }
  for (const binding of config.source_bindings) {
    const resolved = fs.realpathSync(binding.path);
    const info = fs.statSync(resolved, {bigint: true});
    if (resolved !== binding.resolved || String(info.dev) !== binding.device || String(info.ino) !== binding.inode) {
      throw new Error('An original source path identity changed.');
    }
  }
}

async function boot(config) {
  verifyPaths(config);
  const {runProfile} = await import(pathToFileURL(path.join(config.runtime_package_root, '@deepseek-ai/dsh/lib/profile-boot.js')));
  const {createLaunchEnvironmentSnapshot} = await import(pathToFileURL(path.join(config.runtime_package_root, '@deepseek-ai/dsh-launch-environment/lib/index.js')));
  if (ending || channel.destroyed) return;
  verifyPaths(config);
  const scratch = fs.mkdtempSync(path.join(config.dsh_home, '.hermes-carrier-'));
  const patch = path.join(scratch, 'owned.patch.yml');
  fs.writeFileSync(patch, '- id: web-runtime\n  config:\n    openBrowser: false\n    printUrl: false\n    surfaceContext: false\n    trustedHosts: []\n- id: webserver\n  config:\n    host: 127.0.0.1\n    port: 0\n', {mode: 0o600});
  try {
    runtime = await runProfile({
      environment: createLaunchEnvironmentSnapshot([{source: 'process', values: {...process.env}}]),
      profile: 'hermes-owned', patchFiles: [patch], args: ['--host', '127.0.0.1', '--port', '0', '--no-open'],
      ...(!fs.existsSync(path.join(config.dsh_home, 'profiles/hermes-owned/package.json')) ? {fromDefaultProfile: 'web'} : {}),
    });
  } finally { fs.rmSync(scratch, {recursive: true, force: true}); }
  if (ending || channel.destroyed) {await endOwned(); return;}
  verifyPaths(config);
  gateway = runtime.ctx.get('typertGateway');
  const web = runtime.ctx.get('webServer');
  let ready = false;
  const release = runtime.ctx.get('appReady').onReady(() => {ready = true;});
  release();
  if (!ready || !gateway || web?.host !== '127.0.0.1' || !Number.isInteger(web.port) || web.port <= 0) {
    throw new Error('Original owned profile did not become ready.');
  }
  return {pid: process.pid, version: JSON.parse(fs.readFileSync(path.join(config.runtime_package_root, '@deepseek-ai/dsh/package.json'), 'utf8')).version};
}

async function dispatch(message) {
  try {
    if (message.op === 'boot' && !runtime) {
      const value = await boot(message.config);
      if (!ending && !channel.destroyed) reply(message.id, value);
      return;
    }
    if (message.op === 'shutdown') {
      await endOwned();
      return;
    }
    if (!gateway || ending) throw new Error('Original runtime is unavailable.');
    if (message.op === 'call') {
      if (message.method === '$events/result') {
        // An in-memory Request selects the original public Host interceptor.
        // It creates no network request and obtains no authentication material.
        const handler = runtime.ctx.get('connection').createSharedFetchHandler('/api');
        const response = await handler.fetch(new Request('http://carrier.invalid/api/$events/result', {
          method: 'POST', headers: {'content-type': 'application/json'},
          body: JSON.stringify({type: 'client-request', rpcId: message.rpcId, method: message.method,
            payload: {args: message.args}}),
        }));
        if (response.status !== 200) throw new Error('Original special endpoint did not confirm its RPC.');
        return reply(message.id, await response.json());
      }
      if (!allowed.has(message.method)) throw new Error('Native method is not admitted.');
      const [namespace, method] = message.method.split('/');
      let result;
      try {
        const value = await gateway.invoke({namespace, method, args: message.args, signal: new AbortController().signal});
        result = {ok: true, value};
      } catch (error) {result = {ok: false, error: gateway.wireStream.failure(error)};}
      return reply(message.id, {type: 'server-response', rpcId: message.rpcId, result});
    }
    if (message.op === 'open') {
      if (!streamAllowed.has(message.endpoint) || streams.has(message.id)) throw new Error('Native stream is not admitted.');
      const control = new AbortController();
      streams.set(message.id, {control});
      const source = await gateway.wireStream.open(message.endpoint, {args: message.args}, empty(), undefined, control.signal);
      const iterator = source[Symbol.asyncIterator]();
      streams.set(message.id, {control, iterator});
      reply(message.id, null);
      try {
        for await (const value of {[Symbol.asyncIterator]: () => iterator}) send({type: 'item', id: message.id, value});
        send({type: 'end', id: message.id});
      } catch { send({type: 'end', id: message.id, failed: true}); }
      finally { streams.delete(message.id); }
      return;
    }
    if (message.op === 'cancel') {
      const entry = streams.get(message.streamId);
      entry?.control.abort();
      return reply(message.id, null);
    }
    throw new Error('Unknown private carrier operation.');
  } catch { streams.delete(message.id); if (!ending && !channel.destroyed) reject(message.id); }
}

channel.on('data', chunk => {
  buffered = Buffer.concat([buffered, chunk]);
  while (buffered.includes(10)) {
    const index = buffered.indexOf(10);
    if (index > bound) {channel.destroy(); return;}
    const line = buffered.subarray(0, index);
    buffered = buffered.subarray(index + 1);
    try { void dispatch(JSON.parse(line)); } catch {channel.destroy(); return;}
  }
  if (buffered.length > bound) channel.destroy();
});
channel.on('end', () => {void endOwned();});
channel.on('close', () => {void endOwned();});
channel.on('error', () => channel.destroy());
