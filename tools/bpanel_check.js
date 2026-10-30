/* 板块面板 + 操作建议 UI 冒烟：在 happy-dom 里开真实抽屉，
 * 验证「操作建议 → 板块按钮 → 归属 chips → 板块面板」这条用户点击链路。
 *
 * 跑法：
 *   NODE_PATH="C:/Users/Administrator/.workbuddy/binaries/node/workspace/node_modules" \
 *   node tools/bpanel_check.js [http://127.0.0.1:8768]
 *
 * 与接口测试的区别：接口 200 不代表模板绑对了（v-if 层级、字段名拼错都会白屏），
 * 这一步验的是「用户点下去真的能看到东西」。
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
setTimeout(() => { console.log('watchdog: 45 s 到点强制退出'); finish(); }, 45000);

const doc = window.document;
const clean = (s) => (s || '').replace(/[ \t\n]+/g, ' ').trim();
const VERDICTS = ['可参与', '观察', '回避'];

(async () => {
  await new Promise((r) => setTimeout(r, 4000));   // 等首屏
  await app.openDetail('600519');
  console.log('抽屉打开 drawerOpen=' + app.drawerOpen);

  // 1) 操作建议块（首次要拉分时，给 20 s）
  let adv = null;
  for (let i = 0; i < 20 && !adv; i++) {
    await new Promise((r) => setTimeout(r, 1000));
    adv = doc.querySelector('.drawer .advbox');
    if (i % 5 === 4) console.log(`[${i + 1}s] 等待建议块 advLoading=${app.advLoading}`);
  }
  const advHead = adv ? clean(adv.querySelector('.advhead') && adv.querySelector('.advhead').textContent) : '';
  console.log('建议块=' + !!adv + ' 头部=' + advHead.slice(0, 60));
  if (!adv) errors.push('建议块没渲染出来');
  else {
    if (!VERDICTS.some((v) => advHead.includes(v))) errors.push('结论档位异常: ' + advHead);
    if (!clean(adv.textContent).includes('纪律')) errors.push('建议块缺纪律位');
  }

  // 2) 点「板块」按钮 → 归属 chips
  const btn = [...doc.querySelectorAll('.drawer .drawer-h .tbtn')]
    .find((b) => b.textContent.trim() === '板块');
  if (!btn) { errors.push('头部没有「板块」按钮'); return finish(); }
  btn.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));

  let chips = [];
  for (let i = 0; i < 15 && !chips.length; i++) {
    await new Promise((r) => setTimeout(r, 1000));
    chips = [...doc.querySelectorAll('.drawer .mrow .mchip')]
      .filter((c) => !c.textContent.includes('沪主板'));   // 选股面板才有的市场 chip 排除
  }
  console.log('归属 chips=' + chips.length + ' 首个=' + (chips[0] ? clean(chips[0].textContent) : ''));
  if (!chips.length) errors.push('板块归属 chips 没渲染（索引未就绪或绑定错）');

  // 3) 面板（首 chip 自动选中）—— 梯队网格 + 前排表
  let panelGrid = null, topRows = 0;
  for (let i = 0; i < 20 && !topRows; i++) {
    await new Promise((r) => setTimeout(r, 1000));
    const grids = [...doc.querySelectorAll('.drawer .qgrid')];
    panelGrid = grids.find((g) => g.textContent.includes('梯队'));
    const tbls = [...doc.querySelectorAll('.drawer table.tbl')];
    const topTbl = tbls.find((t) => t.textContent.includes('前排'));
    topRows = topTbl ? topTbl.querySelectorAll('tbody tr').length : 0;
    if (i % 5 === 4) console.log(`[${i + 1}s] 等面板 ploading=${app.sboard.ploading}`);
  }
  const gridTxt = panelGrid ? clean(panelGrid.textContent).slice(0, 110) : '';
  console.log('梯队网格=' + !!panelGrid + ' → ' + gridTxt);
  console.log('前排表行数=' + topRows);
  if (!panelGrid) errors.push('板块面板梯队网格没渲染');
  else {
    for (const kw of ['涨停', '强度', '持续性']) {
      if (!gridTxt.includes(kw)) errors.push('面板缺「' + kw + '」块');
    }
  }
  if (!topRows) errors.push('前排成分股表没渲染');

  finish();
})().catch((e) => { errors.push('流程异常: ' + e.message); finish(); });

let finished = false;
function finish() {
  if (finished) return;
  finished = true;
  console.log('errors=' + JSON.stringify(errors));
  process.exit(errors.length ? 1 : 0);
}
