/* 顶层 Tab 导航冒烟：市场分析 / 选股 两个 Tab 的切换、内容归属、子视图与抽屉联动。
 *
 * 跑法：
 *   NODE_PATH="C:/Users/Administrator/.workbuddy/binaries/node/workspace/node_modules" \
 *   node tools/tabnav_check.js [http://127.0.0.1:8768]
 *
 * 为什么单独验：v-show 隐藏的节点仍在 DOM 里，render_check 那种「查选择器」的写法
 * 查得到但用户看不见——必须显式断言 display，否则 Tab 切不动也能全绿。
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { Window } = require('happy-dom');

const BASE = process.argv[2] || 'http://127.0.0.1:8768';
const ROOT = path.resolve(__dirname, '..');
const errors = [];

const html = fs.readFileSync(path.join(ROOT, 'web', 'index.html'), 'utf8');
const body = html.slice(html.indexOf('<body>') + 6, html.indexOf('<script src="vendor'));

const window = new Window({ url: BASE + '/' });
window.document.body.innerHTML = body;
window.HTMLCanvasElement.prototype.getContext = () => null;

const sandbox = {
  console,
  setTimeout, setInterval, clearTimeout, clearInterval,
  Promise, JSON, Math, Date, Number, String, Boolean, Array, Object, Map, Set,
  RegExp, Error, TypeError, isFinite, parseInt, parseFloat, encodeURIComponent,
  document: window.document,
  location: window.location,
  navigator: window.navigator,
  localStorage: window.localStorage,
  getComputedStyle: (el) => window.getComputedStyle(el),
  requestAnimationFrame: (f) => setTimeout(() => f(Date.now()), 16),
  fetch: (url, opt) => fetch(new URL(url, BASE).toString(), opt),
  URL, URLSearchParams, TextEncoder, TextDecoder,
  WebSocket: undefined,
  AudioContext: undefined,
  addEventListener: (...a) => window.addEventListener(...a),
  removeEventListener: (...a) => window.removeEventListener(...a),
  devicePixelRatio: 1,
};
sandbox.window = window;
sandbox.globalThis = sandbox;
for (const k of ['Element', 'HTMLElement', 'SVGElement', 'Node', 'Text', 'Comment',
  'DocumentFragment', 'DOMParser', 'MutationObserver', 'CustomEvent', 'Event',
  'KeyboardEvent', 'MouseEvent', 'Image', 'CSS', 'File', 'Blob', 'FormData']) {
  if (window[k]) sandbox[k] = window[k];
}
vm.createContext(sandbox);

try {
  vm.runInContext(fs.readFileSync(path.join(ROOT, 'web', 'vendor', 'vue.global.prod.js'), 'utf8'), sandbox);
  var app = vm.runInContext(fs.readFileSync(path.join(ROOT, 'web', 'app.js'), 'utf8'), sandbox);
} catch (e) {
  console.log('挂载失败: ' + e.message);
  process.exit(1);
}

process.on('uncaughtException', (e) => { errors.push('uncaught: ' + e.message); });
process.on('unhandledRejection', (e) => { errors.push('unhandled: ' + (e && e.message)); });
setTimeout(() => { console.log('watchdog: 100 s 到点强制退出'); finish(); }, 100000);

const doc = window.document;
const clean = (s) => (s || '').replace(/[ \t\n]+/g, ' ').trim();
const shown = (sel) => {
  const el = doc.querySelector(sel);
  if (!el) return null;
  return el.style.display !== 'none';
};
const click = (el) => el.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
const btnByText = (sel, txt) =>
  [...doc.querySelectorAll(sel)].find((b) => b.textContent.trim() === txt);

(async () => {
  await new Promise((r) => setTimeout(r, 4500));   // 等首屏接口

  // 1) 初始态：停在市场分析
  console.log(`① 初始 tab=${app.tab} market可见=${shown('.pane-market')} pick可见=${shown('.pane-pick')}`);
  if (app.tab !== 'market') errors.push('初始 tab 应为 market，实际 ' + app.tab);
  if (shown('.pane-market') !== true) errors.push('市场分析面板应可见');
  if (shown('.pane-pick') !== false) errors.push('选股面板初始应隐藏');

  // 2) 市场分析该有的四块 / 不该有的
  const mktHas = {
    市场宽度: !!doc.querySelector('.pane-market .breadth'),
    板块: !!doc.querySelector('.pane-market .matrix'),
    资金流: !!doc.querySelector('.pane-market .flowlegend'),
    异动: !!doc.querySelector('.pane-market .alist'),
  };
  console.log('② 市场分析内容 ' + JSON.stringify(mktHas));
  for (const [k, v] of Object.entries(mktHas)) if (!v) errors.push('市场分析缺「' + k + '」');
  if (doc.querySelector('.pane-market .pickb')) errors.push('选股筛选面板不该出现在市场分析 Tab');
  if (doc.querySelector('.pane-market .tblwrap table')) errors.push('个股表不该出现在市场分析 Tab');

  // 2b) 市场分析页常驻「上证指数 · 波浪」卡（与波浪 Tab 共用同一批计算结果）
  for (let i = 0; i < 25 && !app.shWave; i++) await new Promise((r) => setTimeout(r, 800));
  const swCard = [...doc.querySelectorAll('.pane-market .card-h .t')]
    .find((e) => e.textContent.includes('上证指数'));
  const sw = app.shWave;
  const swHtml = swCard ? clean(swCard.parentElement.parentElement.querySelector('.card-b').textContent) : '';
  console.log(`②b 上证指数波浪卡 存在=${!!swCard} ok=${!!(sw && sw.ok)} `
    + `结构=${sw && sw.pattern} 阶段=${sw && sw.stage && sw.stage.n} na=${JSON.stringify(sw && sw.na)} 摘要=${swHtml.slice(0, 40)}`);
  if (!swCard) errors.push('市场分析页没有「上证指数 · 波浪」卡');
  if (!(sw && sw.ok)) errors.push('上证指数浪型没算出来');
  if (sw && sw.ok && !swHtml.includes('失效')) errors.push('浪型卡没渲染失效位');

  // 3) 点「选股」切过去
  const pickTab = btnByText('.topnav .nvtab', '选股');
  if (!pickTab) { errors.push('顶栏没有「选股」Tab 按钮'); return finish(); }
  click(pickTab);
  await new Promise((r) => setTimeout(r, 600));
  console.log(`③ 点选股后 tab=${app.tab} market可见=${shown('.pane-market')} pick可见=${shown('.pane-pick')}`);
  if (app.tab !== 'pick') errors.push('点击后 tab 应切到 pick');
  if (shown('.pane-pick') !== true) errors.push('选股面板应可见');
  if (shown('.pane-market') !== false) errors.push('市场分析面板应隐藏');

  // 4) 选股 Tab 该有的：模式筛选 + 全市场个股表
  const modeChips = doc.querySelectorAll('.pane-pick .pickb .mchip').length;
  const tblRows = doc.querySelectorAll('.pane-pick .tblwrap tbody tr').length;
  const total = app.meta.total;
  console.log(`④ 选股内容 模式chips=${modeChips} 表格行数=${tblRows} 全市场=${total} 只`);
  if (modeChips < 6) errors.push('选股面板模式 chip 不足 6 个，实际 ' + modeChips);
  if (!tblRows) errors.push('选股 Tab 个股表没数据');
  if (!total) errors.push('全市场总数没拉到');

  // 5) 子视图：自选 / 选股
  const watchBtn = btnByText('.pane-pick .card-h .tab', '自选');
  if (watchBtn) {
    click(watchBtn);
    await new Promise((r) => setTimeout(r, 800));
    console.log(`⑤ 切自选 view=${app.view} 行数=${doc.querySelectorAll('.pane-pick .tblwrap tbody tr').length}`);
    if (app.view !== 'watch') errors.push('子视图没切到 watch');
  } else errors.push('个股表缺「自选」子视图');

  const pickSubBtn = btnByText('.pane-pick .card-h .tab', '选股');
  if (pickSubBtn) {
    click(pickSubBtn);
    for (let i = 0; i < 25 && !app.pick.stat; i++) await new Promise((r) => setTimeout(r, 1000));
    console.log(`⑥ 切选股子视图 view=${app.view} 命中=${app.pick.stat ? app.pick.stat.total : '—'} 行数=${app.pick.rows.length}`);
    if (app.view !== 'pick') errors.push('子视图没切到 pick');
    if (!app.pick.stat) errors.push('选股子视图没跑出结果');
  } else errors.push('个股表缺「选股」子视图');

  // 6) 抽屉在选股 Tab 下能开
  await app.openDetail('600519');
  await new Promise((r) => setTimeout(r, 1200));
  console.log(`⑦ 选股 Tab 开抽屉 drawerOpen=${app.drawerOpen} 标的=${app.detail && app.detail.n}`);
  if (!app.drawerOpen) errors.push('选股 Tab 下抽屉打不开');

  // 7) 快捷键 1 / 2
  const press = (key) => window.dispatchEvent(new window.KeyboardEvent('keydown', { key, bubbles: true }));
  press('1');
  await new Promise((r) => setTimeout(r, 500));
  const backOk = app.tab === 'market' && shown('.pane-market') === true && shown('.pane-pick') === false;
  press('2');
  await new Promise((r) => setTimeout(r, 500));
  const forthOk = app.tab === 'pick' && shown('.pane-pick') === true;
  console.log(`⑧ 快捷键 1→market=${backOk} 2→pick=${forthOk}`);
  if (!backOk) errors.push('快捷键 1 没切回市场分析');
  if (!forthOk) errors.push('快捷键 2 没切到选股');

  finish();
})().catch((e) => { errors.push('流程异常: ' + e.message); finish(); });

let finished = false;
function finish() {
  if (finished) return;
  finished = true;
  console.log('errors=' + JSON.stringify(errors));
  process.exit(errors.length ? 1 : 0);
}
