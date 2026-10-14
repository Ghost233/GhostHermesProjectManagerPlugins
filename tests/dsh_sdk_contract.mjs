// Read the unmodified original SDK; create no Host, Agent, or network service.
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
const root = process.env.DSH_TEST_SDK_ROOT;
const files = ['@deepseek-ai/dsh-api-gateway/lib/index.js', '@deepseek-ai/dsh-api-gateway/package.json',
  '@deepseek-ai/dsh-session/lib/index.js', '@deepseek-ai/dsh-session/package.json',
  '@deepseek-ai/cordis/lib/index.js', '@deepseek-ai/cordis/package.json',
  '@deepseek-ai/dsh-typert-protocol/lib/index.js', '@deepseek-ai/dsh-typert-protocol/package.json',
  '@deepseek-ai/dsh-user-approval/lib/index.js', '@deepseek-ai/dsh-user-approval/package.json'];
const digest = () => files.map(file => createHash('sha256').update(readFileSync(join(root, file))).digest('hex'));
const before = digest();
let input = '';
for await (const chunk of process.stdin) input += chunk;
const payload = JSON.parse(input);
let result;
if (payload.kind === 'approval-audit') {
  const { ApprovalService } = await import(pathToFileURL(join(root, files[8])).href);
  const { snapshotSessionEvent } = await import(pathToFileURL(join(root, files[2])).href);
  const events = payload.events.map(event => snapshotSessionEvent(event));
  const session = { get seq() { return events.length; }, eventAt(seq) { return events[seq]; },
    append(type, data) { const event = snapshotSessionEvent({ type, data, seq: events.length, time: 2 }); events.push(event); return event; } };
  const service = Object.create(ApprovalService.prototype);
  Object.defineProperty(service, 'config', { value: { policy: 'ask' } });
  Object.defineProperty(service, 'ctx', { value: { waterfall: async () => payload.outcome } });
  const outcome = await service.request({ ...payload.request, agent: { session } });
  result = { events, outcome };
} else if (payload.kind === 'waterfall') {
  const { TypertGatewayService } = await import(pathToFileURL(join(root, files[0])).href);
  const gateway = Object.create(TypertGatewayService.prototype);
  let settled = null;
  const pending = { id: payload.eventId, deliveries: new Set(), source: { resolve: value => { settled = value; } },
    releaseSignal() {}, releaseContext() {} };
  const desktop = { deliveries: new Map(), queue: { push() {} } };
  const observer = { deliveries: new Map(), queue: { push() {} } };
  for (const client of [desktop, observer]) { pending.deliveries.add(client); client.deliveries.set(pending.id, pending); }
  gateway.pendingRemoteEvents = new Map([[pending.id, pending]]);
  gateway.receiveRemoteEventResult(desktop, { eventId: pending.id, outcome: { kind: 'next' } });
  const afterDesktop = settled;
  for (const reply of payload.replies) gateway.receiveRemoteEventResult(observer, reply);
  result = { afterDesktop, settled, remainingDeliveries: pending.deliveries.size };
} else {
  const { snapshotSessionEvent, foldSurface } = await import(pathToFileURL(join(root, files[2])).href);
  try {
    const events = payload.events.map(event => snapshotSessionEvent(event));
    const surface = foldSurface(events);
    if (payload.expectRejected) throw new Error('Expected rejection did not occur.');
    result = { events, replacements: surface.replacements };
  } catch (error) {
    if (!payload.expectRejected || error.message === 'Expected rejection did not occur.') throw error;
    result = { rejected: true };
  }
}
if (JSON.stringify(before) !== JSON.stringify(digest())) throw new Error('Original SDK bytes changed.');
process.stdout.write(JSON.stringify({ ...result, sdkBytesUnchanged: true }));
