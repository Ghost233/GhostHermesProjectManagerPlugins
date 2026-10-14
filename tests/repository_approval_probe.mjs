// Compose unmodified native services; the second client substitutes only the UI responder.
import fs from 'node:fs';
import {bootOwnedRuntime} from '../ghost_hermes_pm/owned_runtime.mjs';

const config = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const expected = process.argv[3];
const runtime = await bootOwnedRuntime(config);
const gateway = runtime.ctx.get('typertGateway');
const controls = [], clients = [];
const empty = async function* () {};
const waitFor = async check => {
  const deadline = Date.now() + 20000;
  while (Date.now() < deadline) {
    const value = check();
    if (value) return value;
    await new Promise(resolve => setTimeout(resolve, 10));
  }
  throw new Error('Original native approval did not reach its expected boundary.');
};
async function client() {
  const control = new AbortController(), frames = [];
  controls.push(control);
  const stream = await gateway.wireStream.open('$events', {args: {}}, empty(), undefined, control.signal);
  const reader = (async () => {try {for await (const frame of stream) frames.push(frame);} catch {}})();
  clients.push(reader);
  const ready = await waitFor(() => frames.find(frame => frame.type === 'ready'));
  return {id: ready.clientId, frames};
}
try {
  const observer = await client(), ui = await client();
  const sid = config.session_id;
  const invoke = (method, request) => gateway.invoke({namespace: 'session', method, args: {request}, signal: new AbortController().signal});
  await invoke('create', {sessionId: sid, cwd: config.workspace, agentPreset: 'hermes-owned'});
  await invoke('rename', {sessionId: sid, title: 'Hermes fixture approval'});
  const agent = runtime.ctx.get('agents').get(sid);
  runtime.ctx.get('permissionPresets').set(agent.session, 'read-only');
  await invoke('prompt', {sessionId: sid, mode: 'queue', requestId: 'input-approval',
    content: [{type: 'text', text: 'Run the exact synthetic repository operation, using its original approval.'}]});
  const frame = await waitFor(() => observer.frames.find(value => value.event === 'approval/request'));
  const uiFrame = await waitFor(() => ui.frames.find(value => value.eventId === frame.eventId));
  if (uiFrame.agentId !== sid || uiFrame.request.callId !== 'approval-fixture') throw new Error('Native UI target differs.');
  if (expected === 'cancelled') await invoke('cancel', {sessionId: sid});
  else {
    const decision = await gateway.dispatchRpc('$events/result', {args: {clientId: ui.id,
      eventId: frame.eventId, outcome: {kind: 'result', value: expected}}}, new AbortController().signal);
    if (!decision.ok) throw new Error('Native UI decision failed.');
  }
  const decided = await waitFor(() => {
    for (let seq = 0; seq < agent.session.seq; seq++) {
      const event = agent.session.eventAt(seq);
      if (event.type === 'approval/decided') return event;
    }
  });
  await waitFor(() => agent.status === 'idle');
  const events = Array.from({length: agent.session.seq}, (_, seq) => agent.session.eventAt(seq));
  const asked = events.find(event => event.type === 'approval/asked');
  const result = events.find(event => event.type === 'tool/result' && event.data.message.toolCallId === 'approval-fixture');
  if (decided.data.outcome !== expected || asked.data.id !== decided.data.id) throw new Error('Original approval audit differs.');
  process.stdout.write(JSON.stringify({outcome: decided.data.outcome, observer_supplied_decisions: 0,
    same_session: frame.agentId === sid, same_call: asked.data.callId === 'approval-fixture',
    original_exit_code: 0, tool_failed: result?.data.message.isError === true}) + '\n');
} finally {
  for (const control of controls) control.abort();
  await Promise.all(clients);
  await runtime.shutdown.shutdown(0);
}
