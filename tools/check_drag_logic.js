/**
 * Unit-test the drag & drop route builder logic without a browser.
 *
 * The page is one big script, so this drives it with a deliberately tiny DOM stub
 * and a fake fetch: enough to exercise addToDraft / removeChip / addRoute's guards
 * and to prove the multi-speaker payload really carries every target.
 *
 *   node tools/check_drag_logic.js
 */

const fs = require('fs');
const path = require('path');

const html = fs.readFileSync(path.join(__dirname, '..', 'wavelink', 'ui', 'index.html'), 'utf8');
const js = html.split('<script>')[1].split('</script>')[0];

/* ---------- minimal DOM stub ---------- */
function makeEl(id) {
  const el = {
    id,
    innerHTML: '',
    textContent: '',
    className: '',
    style: {},
    value: '',
    dataset: {},
    disabled: false,
    classList: {
      _s: new Set(),
      add(...c) { c.forEach(x => x && this._s.add(x)); },
      remove(...c) { c.forEach(x => this._s.delete(x)); },
      toggle(c, on) { on === undefined ? (this._s.has(c) ? this._s.delete(c) : this._s.add(c)) : (on ? this._s.add(c) : this._s.delete(c)); },
      contains(c) { return this._s.has(c); },
    },
    addEventListener() {},
    querySelectorAll() { return []; },
    querySelector() { return null; },
    getBoundingClientRect() { return { left: 0, top: 0, right: 0, bottom: 0 }; },
  };
  return el;
}

const els = {};
const document = {
  getElementById(id) { return (els[id] = els[id] || makeEl(id)); },
  querySelectorAll() { return []; },
  querySelector() { return null; },
  createElement(tag) { return makeEl(tag); },
  body: { appendChild() {}, removeChild() {}, classList: makeEl('body').classList },
  execCommand() { return true; },
};

const toasts = [];
let lastRoutePayload = null;

const sandbox = {
  document,
  window: { addEventListener() {}, removeEventListener() {} },
  navigator: { clipboard: null },
  location: { hostname: '127.0.0.1', port: '17800' },
  console,
  setTimeout: () => 0,
  clearTimeout: () => {},
  setInterval: () => 0,
  fetch: async (url, opt) => {
    if (url.includes('/api/routes/add')) lastRoutePayload = JSON.parse(opt.body);
    return { ok: true, text: async () => JSON.stringify({ ok: true, routes: [] }) };
  },
  alert: () => {},
  prompt: () => null,
};

/* ---------- run the page script in the sandbox ---------- */
const vm = require('vm');
const context = vm.createContext(sandbox);
let failures = 0;
function check(name, ok, detail) {
  console.log(`[${ok ? 'PASS' : 'FAIL'}] ${name}${detail ? ' — ' + detail : ''}`);
  if (!ok) failures++;
}

// The page calls refreshAll() at the bottom, which hits the network; stub the state
// it needs first by defining it after evaluation.
try {
  vm.runInContext(js, context, { filename: 'page.js' });
  check('页面脚本可执行（无运行时错误）', true);
} catch (e) {
  check('页面脚本可执行（无运行时错误）', false, e.message);
  process.exit(1);
}

// Capture toasts by replacing the helper.
vm.runInContext('toast = (m, k) => { __toasts.push([m, k]); };', Object.assign(context, { __toasts: toasts }));

function call(expr) { return vm.runInContext(expr, context); }

/* ---------- device list ---------- */
call(`state.self = {name:'我的笔记本', host:'192.168.0.101', web_port:17800, sources:[], sinks:[]};
     state.peers = [
       {node_id:'node-b', value:'node-b', name:'伴侣的电脑', host:'192.168.0.102', online:true, age_s:1, sources:[{name:'扬声器',kind_class:'system'}], sinks:[{name:'耳机',kind_class:'output'}]},
       {node_id:'node-c', value:'node-c', name:'客厅音箱', host:'192.168.0.103', online:true, age_s:1, sources:[{name:'扬声器',kind_class:'system'}], sinks:[{name:'音箱',kind_class:'output'}]}
     ];`);

call('renderDragList()');
const listHtml = context.document.getElementById('dragList').innerHTML;
check('设备卡片已渲染', listHtml.includes('伴侣的电脑') && listHtml.includes('客厅音箱') && listHtml.includes('本机'),
      `${(listHtml.match(/class="drag/g) || []).length} 张卡片`);
check('本机卡片有标记', listHtml.includes('drag self'));

/* ---------- dragging into the boxes ---------- */
call('clearDraft()');
call(`addToDraft('from', 'node-b', '伴侣的电脑')`);
check('拖入「要听谁的」生效', call('draft.from.length') === 1, call('JSON.stringify(draft.from)'));

call(`addToDraft('from', 'node-b', '伴侣的电脑')`);
check('重复拖入不会叠加', call('draft.from.length') === 1);

const before = toasts.length;
call(`addToDraft('from', 'self', '我的笔记本')`);
// Putting this machine on the "要听谁的" side is the headline use case - it is how
// you send your own sound to the other machine's headphones - so it must be allowed.
check('本机可以当「要听谁的」', call('draft.from.length') === 2 &&
      call('JSON.stringify(draft.from)') === '["node-b","self"]', call('JSON.stringify(draft.from)'));
check('本机当来源不会报错', !toasts.slice(before).some(t => t[1] === 'err'));

call(`addToDraft('to', 'self', '我的笔记本')`);
call(`addToDraft('to', 'node-c', '客厅音箱')`);
check('「用谁的耳机听」可以放多台', call('draft.to.length') === 2, call('JSON.stringify(draft.to)'));

// Both sides local is a genuine no-op ("play my sound on my own machine"), so the
// builder still refuses to add that combination.
call('clearDraft()');
call(`addToDraft('from', 'self', '我的笔记本')`);
const beforeLocal = toasts.length;
call(`addToDraft('to', 'self', '我的笔记本')`);
check('两边都放本机会被拒绝', call('draft.to.length') === 0 &&
      toasts.slice(beforeLocal).some(t => t[1] === 'err'), 'toast=' + JSON.stringify(toasts[toasts.length - 1]));
call('clearDraft()');
call(`addToDraft('from', 'node-b', '伴侣的电脑')`);
call(`addToDraft('to', 'self', '我的笔记本')`);
call(`addToDraft('to', 'node-c', '客厅音箱')`);

call('renderDraft()');
const dropHtml = context.document.getElementById('dropTo').innerHTML;
check('多目标时显示同时播放提示', context.document.getElementById('multiHint').innerHTML.includes('同时'),
      context.document.getElementById('multiHint').innerHTML.trim().slice(0, 40));

/* ---------- removing a chip ---------- */
call(`removeChip('to', 'node-c')`);
check('移除芯片生效', call('draft.to.length') === 1, call('JSON.stringify(draft.to)'));

/* ---------- payload sent to the backend ---------- */
(async () => {
  call(`addToDraft('to', 'node-c', '客厅音箱')`);
  await call('addRoute()');
  const wait = () => new Promise(r => setTimeout(r, 30));
  await wait();
  check('addRoute 发出了请求', !!lastRoutePayload, JSON.stringify(lastRoutePayload));
  if (lastRoutePayload) {
    const r = lastRoutePayload.route;
    check('请求里包含全部目标（多音响）', r.to.length === 2, 'to=' + JSON.stringify(r.to));
    check('请求里保留拖进去的来源', r.from.length === 1 && r.from[0] === 'node-b', 'from=' + JSON.stringify(r.from));
  }
  check('添加成功后清空了草稿', call('draft.from.length') === 0 && call('draft.to.length') === 0);

  // The headline use case end to end: my own sound -> the other machine's headphones.
  call('clearDraft()');
  call(`addToDraft('from', 'self', '我的笔记本')`);
  call(`addToDraft('to', 'node-b', '伴侣的电脑')`);
  await call('addRoute()');
  await wait();
  const own = lastRoutePayload && lastRoutePayload.route;
  check('可以把自己的声音送去对方耳机',
        !!own && own.from.length === 1 && own.from[0] === 'self' &&
        own.to.length === 1 && own.to[0] === 'node-b',
        own ? JSON.stringify(own) : 'no payload');

  console.log();
  console.log(`共 ${failures} 项失败`);
  console.log('RESULT:', failures ? 'FAIL' : 'PASS');
  process.exit(failures ? 1 : 0);
})();
