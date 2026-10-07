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
const React = {Fragment: 'fragment',
  createElement(type, props, ...children) {return {type, props: props || {}, children};},
  useState(initial) {const i = cursor++; if (!(i in states)) states[i] = initial; return [states[i], value => {states[i] = value;}];},
  useEffect(effect) {if (!initialized) effects.push(effect);}};
const window = {__HERMES_PLUGIN_SDK__: {React, async fetchJSON(url, options) {
  requests.push({url, options});
  if (options && options.method === 'POST') throw new Error(process.argv[3] + ': original fixture operation rejected');
  return snapshot;
}}, __HERMES_PLUGINS__: {register(id, value) {assert.equal(id, 'ghost-hermes-pm'); component = value;}}};
vm.runInNewContext(fs.readFileSync(process.argv[2], 'utf8'), {window, console});
function render() {cursor = 0; const tree = component(); initialized = true; return tree;}
function nodes(tree) {return Array.isArray(tree) ? tree.flatMap(nodes) : tree && typeof tree === 'object' ? [tree, ...tree.children.flatMap(nodes)] : [];}
function text(tree) {return Array.isArray(tree) ? tree.map(text).join('') : tree && typeof tree === 'object' ? tree.children.map(text).join('') : tree == null || tree === false ? '' : String(tree);}
function find(tree, predicate) {const node = nodes(tree).find(predicate); assert.ok(node, 'Public rendered control not found'); return node;}
(async () => {
  render(); await Promise.all(effects.map(effect => effect())); await new Promise(resolve => setImmediate(resolve)); let tree = render();
  if (process.argv[3] === 'evidence') {
    const evidence = find(tree, node => node.type === 'details' && text(node).includes('固定子交付、实际输入、Git 元数据、测试与返工证据'));
    assert.ok(text(evidence).includes('components/fixture-unassigned'), 'Unassigned path is missing from public evidence');
    assert.ok(text(evidence).includes('b'.repeat(40)), 'Unassigned fixed commit is missing from public evidence');
    assert.ok(text(evidence).includes('fixture-preparation-receipt'), 'Original preparation receipt is missing from public evidence');
  } else {
    find(tree, node => node.type === 'select' && node.props.value === 'plan').props.onChange({target: {value: 'start'}}); tree = render();
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
