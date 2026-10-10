// The socket's kernel peer, never message fields or ancestry, grants access.
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import {spawnSync} from 'node:child_process';

export function createControllerGuard(config, writableRoots) {
  const helper = config.controller_helper_path;
  const binding = config.source_bindings.find(row => row.path === helper);
  const overlaps = (left, right) => left === right || left.startsWith(right + path.sep) || right.startsWith(left + path.sep);
  if (!binding?.sha256 || writableRoots.some(root => overlaps(fs.realpathSync(root), path.dirname(helper)))) {
    throw new Error('Kernel identity reader overlaps native writable roots.');
  }
  const verifyHelper = () => {
    const info = fs.lstatSync(helper, {bigint: true}), directory = fs.lstatSync(path.dirname(helper), {bigint: true});
    if (!info.isFile() || !directory.isDirectory() || info.uid !== BigInt(process.getuid())
        || directory.uid !== BigInt(process.getuid()) || (info.mode & 0o077n) || (directory.mode & 0o077n)
        || fs.realpathSync(helper) !== binding.resolved || String(info.dev) !== binding.device || String(info.ino) !== binding.inode
        || crypto.createHash('sha256').update(fs.readFileSync(helper)).digest('hex') !== binding.sha256) {
      throw new Error('Kernel identity reader changed.');
    }
  };
  const key = identity => {
    if (identity === null || typeof identity !== 'object' || Object.keys(identity).length !== 4
        || !Number.isSafeInteger(identity.pid) || identity.pid <= 0 || !/^\d+$/.test(identity.start_sec)
        || !/^\d+$/.test(identity.start_usec) || typeof identity.executable !== 'string' || !path.isAbsolute(identity.executable)) {
      throw new Error('Exact kernel process identity required.');
    }
    return JSON.stringify([identity.pid, identity.start_sec, identity.start_usec, identity.executable]);
  };
  const query = (args, descriptor) => {
    verifyHelper();
    const result = spawnSync(helper, args, {stdio: descriptor === undefined ? ['ignore', 'pipe', 'pipe']
      : ['ignore', 'pipe', 'pipe', descriptor], timeout: 2000, maxBuffer: 4096});
    if (result.status !== 0 || result.error) throw new Error('Kernel process identity unavailable.');
    const value = JSON.parse(result.stdout.toString()); key(value); return value;
  };
  const pin = identity => {
    if (key(query(['process', String(identity.pid)])) !== key(identity)) throw new Error('Controller kernel identity changed.');
    const copied = Object.freeze({...identity}); pinned.add(key(copied)); return copied;
  };
  const pinned = new Set();
  if (config.launcher_controller?.pid !== process.ppid) throw new Error('Initial native launcher differs.');
  pin(config.launcher_controller);
  if (!Array.isArray(config.trusted_controllers) || config.trusted_controllers.length > 1) throw new Error('One initial gateway authority required.');
  const gateway = config.trusted_controllers.length ? pin(config.trusted_controllers[0]) : null;
  return {
    authorize(channel) {
      const descriptor = channel._handle?.fd;
      if (!Number.isInteger(descriptor) || descriptor < 0) throw new Error('Original socket descriptor unavailable.');
      const peer = query(['peer'], descriptor);
      if (!pinned.has(key(peer))) throw new Error('Original controller peer is not admitted.');
      return peer;
    },
    admit(peer, identity) {
      if (!gateway || key(peer) !== key(gateway)) throw new Error('Only the initial gateway may admit a claimed worker.');
      return pin(identity);
    },
  };
}
