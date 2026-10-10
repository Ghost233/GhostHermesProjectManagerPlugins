// Real Unix sockets and the production kernel guard; no native runtime is booted.
import fs from 'node:fs';
import net from 'node:net';
import {createControllerGuard} from '../ghost_hermes_pm/trusted_controller.mjs';
const config = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const guard = createControllerGuard(config, config.writable_roots);
const server = net.createServer(channel => {
  let buffered = '';
  channel.on('data', chunk => {
    buffered += chunk;
    while (buffered.includes('\n')) {
      const index = buffered.indexOf('\n');
      const message = JSON.parse(buffered.slice(0, index)); buffered = buffered.slice(index + 1);
      try {
        const peer = guard.authorize(channel);
        if (message.op === 'admit_controller') guard.admit(peer, message.controller);
        channel.write(JSON.stringify({ok: true, peer}) + '\n');
      } catch {channel.write('{"ok":false}\n');}
    }
  });
  channel.on('error', () => channel.destroy());
});
server.listen(config.socket_path, () => process.stdout.write('ready\n'));
process.stdin.once('data', () => server.close(() => process.exit(0)));
