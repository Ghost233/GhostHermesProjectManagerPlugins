'use strict';
// Drive the registered component and its public form/buttons with controlled SDK
// and React hook boundaries. Independent QA also runs the actual React browser.
const fs = require('fs');
const vm = require('vm');
const assert = require('node:assert/strict');
const states = [], effects = [], requests = [];
let cursor = 0, initialized = false, component;
const snapshot = {status: 'completed', version: 1, runtime: 'synthetic', projects: [], profiles: [], requests: [],
  global_validations: [{id: 'fixture-round', request_id: 'fixture-task', status: 'ready', mono_commit: 'a'.repeat(40),
    occupancy: {released: true}, whole_project_complete: false, children: [], inputs: [], tests: [], rework: [],
    unassigned: [{path: 'components/fixture-unassigned', commit: 'b'.repeat(40)}],
    preparation: {status: 'ended', receipt: {action_id: 'fixture-preparation-receipt', status: 'ended'}}}]};
if (process.argv[3] === 'notifications') snapshot.notifications = {
  health: {supervision: 'unavailable', delivery: 'unverified', last_checked_at: 10000,
    sources: {'task-fixture': {status: 'unverified', service_id: 'original-service', last_confirmed_execution: 'running'}}},
  events: [{id: 'notification-fixture', kind: 'human_request', project_id: 'mono', delivery: 'unknown', text: 'Which colour? 原请求 fixture-q',
    mention_owner: true, segments: [{uuid: 'notification-uuid', attempts: [{status: 'delivered', chat_id: 'oc_entry', message_id: 'om_actual_local'}, {status: 'unknown'}]}]}
  ]};
const React = {Fragment: 'fragment',
  createElement(type, props, ...children) {return {type, props: props || {}, children};},
  useState(initial) {const i = cursor++; if (!(i in states)) states[i] = initial; return [states[i], value => {states[i] = typeof value === 'function' ? value(states[i]) : value;}];},
  useEffect(effect) {if (!initialized) effects.push(effect);}};
const window = {__HERMES_PLUGIN_SDK__: {React, async fetchJSON(url, options) {
  requests.push({url, options});
  if (options && options.method === 'POST') throw new Error(process.argv[3] + ': original fixture operation rejected');
  return JSON.parse(JSON.stringify(snapshot));
}}, __HERMES_PLUGINS__: {register(id, value) {assert.equal(id, 'ghost-hermes-pm'); component = value;}}};
vm.runInNewContext(fs.readFileSync(process.argv[2], 'utf8'), {window, console});
function render() {cursor = 0; const tree = component(); initialized = true; return tree;}
function nodes(tree) {return Array.isArray(tree) ? tree.flatMap(nodes) : tree && typeof tree === 'object' ? [tree, ...tree.children.flatMap(nodes)] : [];}
function text(tree) {return Array.isArray(tree) ? tree.map(text).join('') : tree && typeof tree === 'object' ? tree.children.map(text).join('') : tree == null || tree === false ? '' : String(tree);}
function find(tree, predicate) {const node = nodes(tree).find(predicate); assert.ok(node, 'Public rendered control not found'); return node;}
function section(tree, title) {return find(tree, node => node.type === 'section' && nodes(node).some(child => child.type === 'h2' && text(child) === title));}
(async () => {
  render(); await Promise.all(effects.map(effect => effect())); await new Promise(resolve => setImmediate(resolve)); let tree = render();
  if (process.argv[3].startsWith('migration_')) {
    let migration = section(tree, '选择性迁移到新 Profile');
    const stale = process.argv[3] === 'migration_stale';
    find(migration, node => node.type === 'select').props.onChange({target: {value: stale ? 'plan' : 'prepare'}}); tree = render();
    migration = section(tree, '选择性迁移到新 Profile');
    const details = {plan_id: 'fixture-migration', digest: 'a'.repeat(64)};
    if (stale) details.expected_profile_ids = ['old', 'new'];
    find(migration, node => node.type === 'textarea').props.onChange({target: {value: JSON.stringify(details)}}); tree = render();
    find(section(tree, '选择性迁移到新 Profile'), node => node.type === 'form').props.onSubmit({preventDefault() {}}); tree = render();
    if (stale) {
      assert.ok(text(section(tree, '选择性迁移到新 Profile')).includes('"expected_version": 1'));
      snapshot.version = 2;
      await find(tree, node => node.type === 'button' && text(node) === '刷新目录').props.onClick(); tree = render();
      assert.ok(!nodes(section(tree, '选择性迁移到新 Profile')).some(node => node.type === 'button' && text(node) === '本人确认迁移操作'));
      assert.ok(!requests.some(request => request.options && request.options.method === 'POST'), 'Stale Owner preview must not send any action');
    } else {
      await find(section(tree, '选择性迁移到新 Profile'), node => node.type === 'button' && text(node) === '本人确认迁移操作').props.onClick(); tree = render();
      const alert = find(tree, node => node.props.role === 'alert');
      assert.ok(text(alert).includes('migration_error') && text(alert).includes('fixture-migration'));
      const post = requests.find(request => request.options && request.options.method === 'POST');
      assert.ok(post.url.endsWith('/migration'));
      assert.deepEqual(JSON.parse(post.options.body), {action: 'prepare', details});
      assert.ok(requests.at(-1).url.endsWith('/snapshot'));
    }
  } else if (process.argv[3] === 'notifications') {
    const section = find(tree, node => node.type === 'section' && text(node).includes('通知与监督健康'));
    assert.ok(text(section).includes('unavailable') && text(section).includes('unverified'));
    assert.ok(text(section).includes('Which colour? 原请求 fixture-q') && text(section).includes('unknown'));
    assert.ok(text(section).includes('oc_entry') && text(section).includes('om_actual_local'));
    assert.ok(text(section).includes('original-service') && text(section).includes('running'));
    assert.ok(!nodes(section).some(node => node.type === 'button'), 'Notifications must remain factual read-only views');
  } else if (process.argv[3] === 'evidence') {
    const evidence = find(tree, node => node.type === 'details' && text(node).includes('固定子交付、实际输入、Git 元数据、测试与返工证据'));
    assert.ok(text(evidence).includes('components/fixture-unassigned'), 'Unassigned path is missing from public evidence');
    assert.ok(text(evidence).includes('b'.repeat(40)), 'Unassigned fixed commit is missing from public evidence');
    assert.ok(text(evidence).includes('fixture-preparation-receipt'), 'Original preparation receipt is missing from public evidence');
  } else {
    find(section(tree, 'mono 全局验证'), node => node.type === 'select' && node.props.value === 'plan').props.onChange({target: {value: 'start'}}); tree = render();
    find(tree, node => node.type === 'textarea' && node.props['aria-label'] === '本轮验证操作 JSON').props.onChange({target: {value: '{"validation_id":"fixture-round"}'}}); tree = render();
    find(tree, node => node.type === 'button' && text(node) === '核对本轮操作').props.onClick(); tree = render();
    await find(tree, node => node.type === 'button' && text(node) === '执行这条已核对操作').props.onClick(); tree = render();
    const alert = nodes(tree).find(node => node.props.role === 'alert');
    assert.ok(alert, 'Original operation error disappeared after successful refresh');
    assert.ok(text(alert).includes(process.argv[3]), 'Original operation error disappeared after successful refresh');
    assert.ok(requests.at(-1).url.endsWith('/snapshot') && !requests.at(-1).options, 'Expected actual post-error snapshot refresh');
    const post = requests.find(request => request.options && request.options.method === 'POST');
    assert.deepEqual(JSON.parse(post.options.body), {action: 'start', details: {validation_id: 'fixture-round'}});
  }
  console.log('public dashboard validation controls: OK');
})().catch(error => {console.error(error); process.exitCode = 1;});
