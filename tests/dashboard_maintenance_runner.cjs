'use strict';
// Public registered component and controls; external SDK and React hooks only.
const fs = require('fs'), vm = require('vm'), assert = require('node:assert/strict');
const states = [], effects = [], requests = [];
let cursor = 0, initialized = false, component;
const snapshot = {status: 'completed', version: 4, runtime: 'directory_available', projects: [],
  profiles: [{id: 'steward', role: 'steward', capability: 'non_development', project_id: null, native_profile: 'steward', connection_refs: {}}],
  requests: [], execution: 'not_enabled', manual_capabilities: [{kind: 'desktop', status: 'unknown'}],
  notifications: {health: {supervision: 'unavailable', delivery: 'unverified', sources: {}}, events: []},
  maintenance: {mode: 'maintenance', runtime: {status: 'verified', loaded: true, plugin_version: '0.1.0', sdk_version: '0.21.5',
    source_digest: 'a'.repeat(64), sdk_source_digest: 'b'.repeat(64), service_id: 'original-native', generation: 'generation-1',
    verified_at: '2026-10-07T10:00:00+00:00', release_verified: false, evidence: 'original-loaded-proof'},
    plans: [{id: 'original-upgrade', intent: 'maintenance', status: 'switch_failed', approved_scope: {expected_version: 1, expected_profile_ids: ['steward']},
      checks: {inflight_requests: ['original-inflight'], manual: [{session_id: 'manual-id', status: 'blocked', control: 'observe_only'}]},
      switch_request: {status: 'accepted'}, checkpoint: {directory: {status: 'verified', sha256: 'c'.repeat(64)}, native: {artifacts: {archive: {sha256: 'd'.repeat(64)}}}},
      restore: {old_tasks_started: false, manager_authority: 'preserved_current'}, needs_human: ['Handle original manual execution.']}],
    events: [{id: 'forced-loss', status: 'pending_verification', reason: 'forced-native-unload', execution_stopped: false}]}};
const React = {Fragment: 'fragment', createElement(type, props, ...children) {return {type, props: props || {}, children};},
  useState(initial) {const i = cursor++; if (!(i in states)) states[i] = initial; return [states[i], value => {states[i] = typeof value === 'function' ? value(states[i]) : value;}];},
  useEffect(effect) {if (!initialized) effects.push(effect);}};
const window = {__HERMES_PLUGIN_SDK__: {React, async fetchJSON(url, options) {
  requests.push({url, options});
  if (options && options.method === 'POST') {
    const body = JSON.parse(options.body);
    if (process.argv[3] === 'unknown' && body.action !== 'check') throw new Error('outcome_unknown: original response lost');
    return {id: body.details.operation_id, status: body.action === 'check' ? 'blocked' : 'maintenance', needs_human: ['Verify original execution.']};
  }
  return JSON.parse(JSON.stringify(snapshot));
}}, __HERMES_PLUGINS__: {register(id, value) {assert.equal(id, 'ghost-hermes-pm'); component = value;}}};
vm.runInNewContext(fs.readFileSync(process.argv[2], 'utf8'), {window, console});
function render() {cursor = 0; const tree = component(); initialized = true; return tree;}
function nodes(tree) {return Array.isArray(tree) ? tree.flatMap(nodes) : tree && typeof tree === 'object' ? [tree, ...tree.children.flatMap(nodes)] : [];}
function text(tree) {return Array.isArray(tree) ? tree.map(text).join('') : tree && typeof tree === 'object' ? tree.children.map(text).join('') : tree == null || tree === false ? '' : String(tree);}
function find(tree, predicate) {const node = nodes(tree).find(predicate); assert.ok(node, 'Public rendered control not found'); return node;}
function section(tree) {return find(tree, node => node.type === 'section' && nodes(node).some(child => child.type === 'h2' && text(child) === '维护、停用与恢复'));}
(async () => {
  render(); await Promise.all(effects.map(effect => effect())); await new Promise(resolve => setImmediate(resolve)); let tree = render();
  const content = text(section(tree));
  for (const value of ['0.1.0', '0.21.5', 'original-loaded-proof', '2026-10-07T10:00:00+00:00', '尚未通过全部真实验收',
    'switch_failed', 'accepted', 'original-inflight', 'manual-id', 'observe_only', 'Handle original manual execution.',
    'pending_verification', 'forced-native-unload', 'preserved_current', 'd'.repeat(64)]) assert.ok(content.includes(value), 'Missing original evidence: ' + value);
  assert.ok(!requests.some(r => r.options), 'Reading evidence must not execute any action');
  if (process.argv[3] === 'enter') {
    find(section(tree), n => n.type === 'input' && n.props['aria-label'] === '维护操作 ID').props.onChange({target: {value: 'owner-reviewed-31'}}); tree = render();
    find(section(tree), n => n.type === 'textarea').props.onChange({target: {value: JSON.stringify({id: 'release-v2', plugin_version: '0.2.0', source_digest: 'e'.repeat(64)})}}); tree = render();
    find(section(tree), n => n.type === 'form').props.onSubmit({preventDefault() {}}); tree = render();
    assert.ok(text(section(tree)).includes('"expected_version": 4'));
    await find(section(tree), n => n.type === 'button' && text(n) === '本人确认维护操作').props.onClick(); tree = render();
    const post = requests.find(r => r.options);
    assert.ok(post.url.endsWith('/maintenance'));
    assert.deepEqual(JSON.parse(post.options.body), {action: 'enter', details: {operation_id: 'owner-reviewed-31',
      expected_version: 4, expected_profile_ids: ['steward'], expected_release: {plugin_version: '0.1.0', source_digest: 'a'.repeat(64), sdk_version: '0.21.5', sdk_source_digest: 'b'.repeat(64)},
      target_release: {id: 'release-v2', plugin_version: '0.2.0', source_digest: 'e'.repeat(64)}}});
    assert.ok(text(section(tree)).includes('owner-reviewed-31') && text(section(tree)).includes('maintenance'));
    assert.ok(!nodes(section(tree)).some(n => n.type === 'button' && text(n) === '本人确认维护操作'));
    assert.ok(requests.at(-1).url.endsWith('/snapshot') && !requests.at(-1).options);
  }

  if (process.argv[3] === 'unknown') {
    find(section(tree), n => n.type === 'select').props.onChange({target: {value: 'switch'}}); tree = render();
    find(section(tree), n => n.props['aria-label'] === '维护操作 ID').props.onChange({target: {value: 'original-upgrade'}}); tree = render();
    find(section(tree), n => n.type === 'form').props.onSubmit({preventDefault() {}}); tree = render();
    await find(section(tree), n => n.type === 'button' && text(n) === '本人确认维护操作').props.onClick(); tree = render();
    assert.ok(text(find(tree, n => n.props.role === 'alert')).includes('outcome_unknown'));
    assert.ok(text(section(tree)).includes('原维护操作 ID：original-upgrade') && text(section(tree)).includes('结果待核实'));
    assert.ok(!nodes(section(tree)).some(n => n.type === 'button' && text(n) === '本人确认维护操作'), 'Lost response must remove control approval');
    await find(tree, n => n.type === 'button' && text(n) === '刷新目录').props.onClick(); tree = render();
    assert.equal(requests.filter(r => r.options).length, 1, 'Refresh must never replay control');
    find(section(tree), n => n.type === 'select').props.onChange({target: {value: 'check'}}); tree = render();
    find(section(tree), n => n.type === 'form').props.onSubmit({preventDefault() {}}); tree = render();
    await find(section(tree), n => n.type === 'button' && text(n) === '本人确认维护操作').props.onClick(); tree = render();
    assert.deepEqual(JSON.parse(requests.filter(r => r.options).at(-1).options.body), {action: 'check', details: {operation_id: 'original-upgrade'}});
    assert.ok(text(section(tree)).includes('blocked'));
  }
  if (process.argv[3].startsWith('stale_')) {
    find(section(tree), n => n.props['aria-label'] === '维护操作 ID').props.onChange({target: {value: 'old-owner-preview'}}); tree = render();
    find(section(tree), n => n.type === 'form').props.onSubmit({preventDefault() {}}); tree = render();
    assert.ok(text(section(tree)).includes('"expected_version": 4'));
    if (process.argv[3] === 'stale_version') snapshot.version = 5;
    if (process.argv[3] === 'stale_scope') snapshot.profiles.push({id: 'new-profile', native_profile: 'new-profile', role: 'independent', connection_refs: {}});
    if (process.argv[3] === 'stale_release') snapshot.maintenance.runtime.source_digest = 'e'.repeat(64);
    if (process.argv[3] === 'stale_offline') {snapshot.status = 'unverified'; snapshot.maintenance.runtime.status = 'unverified'; snapshot.maintenance.runtime.loaded = false;}
    await find(tree, n => n.type === 'button' && text(n) === '刷新目录').props.onClick(); tree = render();
    assert.ok(!nodes(section(tree)).some(n => n.type === 'button' && text(n) === '本人确认维护操作'), 'Changed snapshot must invalidate original scope approval');
    assert.equal(requests.filter(r => r.options).length, 0);
    if (process.argv[3] === 'stale_offline') assert.equal(find(section(tree), n => n.type === 'button' && text(n) === '预览维护操作').props.disabled, true);
  }
  if (process.argv[3].startsWith('action_')) {
    const action = process.argv[3].slice(7);
    find(section(tree), n => n.type === 'select').props.onChange({target: {value: action}}); tree = render();
    find(section(tree), n => n.props['aria-label'] === '维护操作 ID').props.onChange({target: {value: 'original-upgrade'}}); tree = render();
    const expected = {operation_id: 'original-upgrade'};
    if (action === 'check' || action === 'checkpoint') {
      find(section(tree), n => n.type === 'input' && n.props['aria-label'].startsWith('本人已处理')).props.onChange({target: {value: 'manual-id'}}); tree = render();
      expected.handled_manual_session_ids = ['manual-id'];
    } else {expected.expected_version = 4; expected.expected_profile_ids = ['steward'];}
    if (action === 'deactivate') expected.expected_release = {plugin_version: '0.1.0', source_digest: 'a'.repeat(64), sdk_version: '0.21.5', sdk_source_digest: 'b'.repeat(64)};
    find(section(tree), n => n.type === 'form').props.onSubmit({preventDefault() {}}); tree = render();
    await find(section(tree), n => n.type === 'button' && text(n) === '本人确认维护操作').props.onClick(); tree = render();
    assert.deepEqual(JSON.parse(requests.find(r => r.options).options.body), {action, details: expected});
    assert.ok(requests.at(-1).url.endsWith('/snapshot') && !requests.at(-1).options);
  }
  console.log('public dashboard maintenance controls: OK');
})().catch(error => {console.error(error); process.exitCode = 1;});
