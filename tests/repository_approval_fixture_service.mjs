// Only test preparation and an external native UI protocol client are substituted.
export const inject = ['permissionPresets', 'typertGateway'];
export function apply(ctx, config) {
  ctx.on('session/created', session => ctx.permissionPresets.set(session, 'read-only'));
  const control = new AbortController();
  ctx.effect(() => () => control.abort());
  const empty = async function* () {};
  void (async () => {
    const gateway = ctx.typertGateway;
    const stream = await gateway.wireStream.open('$events', {args: {}}, empty(), undefined, control.signal);
    let clientId;
    for await (const frame of stream) {
      if (frame.type === 'ready') {clientId = frame.clientId; continue;}
      if (frame.type !== 'waterfall') continue;
      if (frame.event !== 'approval/request') {
        await gateway.dispatchRpc('$events/result', {args: {clientId, eventId: frame.eventId,
          outcome: {kind: 'next'}}}, control.signal);
        continue;
      }
      let decision;
      while (!control.signal.aborted && !decision) {
        const result = await fetch(config.uiEndpoint, {signal: control.signal});
        decision = (await result.json()).decision;
        if (!decision) await new Promise(resolve => setTimeout(resolve, 20));
      }
      await gateway.dispatchRpc('$events/result', {args: {clientId, eventId: frame.eventId,
        outcome: {kind: 'result', value: decision}}}, control.signal);
    }
  })().catch(error => {if (!control.signal.aborted) throw error;});
}
