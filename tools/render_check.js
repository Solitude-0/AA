/* 渲染冒烟测试：happy-dom 里真跑一遍 Vue 应用（本机 Chromium 被沙箱拦住，退而求其次）
 *
 * 能拦住三类错误：
 *   ① in-DOM 模板编译错误（最容易出、且浏览器里只表现为一片空白）
 *   ② 运行时异常（computed 取空、字段拼错、undefined.toFixed）
 *   ③ 接口契约对不上（页面渲染出来了但没有数据行）
 *
 * 跑法：
 *   NODE_PATH="C:/Users/Administrator/.workbuddy/binaries/node/workspace/node_modules" \
 *   node tools/render_check.js [http://127.0.0.1:8768]
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
// 无 canvas 上下文：验证 drawFlow 的防御分支
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
  WebSocket: undefined,               // 强制走轮询兜底，顺带验证降级分支
  AudioContext: undefined,
  addEventListener: (...a) => window.addEventListener(...a),
  removeEventListener: (...a) => window.removeEventListener(...a),
  devicePixelRatio: 1,
};
sandbox.window = window;
sandbox.globalThis = sandbox;
// Vue 的 runtime-dom 在模块初始化时就会引用这些构造器，缺一个就整体挂载失败
for (const k of ['Element', 'HTMLElement', 'SVGElement', 'Node', 'Text', 'Comment',
  'DocumentFragment', 'DOMParser', 'MutationObserver', 'CustomEvent', 'Event',
  'KeyboardEvent', 'MouseEvent', 'Image', 'CSS', 'File', 'Blob', 'FormData']) {
  if (window[k]) sandbox[k] = window[k];
}
vm.createContext(sandbox);

try {
  vm.runInContext(fs.readFileSync(path.join(ROOT, 'web', 'vendor', 'vue.global.prod.js'), 'utf8'), sandbox);
  if (!sandbox.Vue) throw new Error('Vue 未挂到全局');
  vm.runInContext(fs.readFileSync(path.join(ROOT, 'web', 'app.js'), 'utf8'), sandbox);
} catch (e) {
  errors.push('加载/挂载失败: ' + e.message);
}

process.on('uncaughtException', (e) => errors.push('uncaught: ' + e.message));
process.on('unhandledRejection', (e) => errors.push('unhandled: ' + (e && e.message)));

setTimeout(() => {
  const doc = window.document;
  const q = (s) => doc.querySelectorAll(s).length;
  const txt = (s) => {
    const el = doc.querySelector(s);
    return el ? el.textContent.replace(/\s+/g, ' ').trim() : '';
  };
  const firstRow = txt('table.grid tbody tr');
  const out = {
    errors,
    indexCells: q('.idx'),
    breadthKpis: q('.kpi'),
    boardCells: q('.cell'),
    tableRows: q('table.grid tbody tr'),
    alertRows: q('.arow'),
    wsBadge: txt('.badges .badge:nth-child(3)'),
    firstRow: firstRow.slice(0, 90),
    topIndex: txt('.idx'),
    bodyLen: doc.body.innerText.length,
  };
  console.log(JSON.stringify(out, null, 2));

  // 资金流卡片单独打一行：这块最容易「渲染没报错但内容是错的」——
  // 换源（东财 ↔ 新浪兜底）之后，标题、图例、榜单都得跟着变，不能只看有没有报错。
  const flowbig = doc.querySelector('.flowbig');
  const legend = doc.querySelector('.flowlegend');
  const blCard = [...doc.querySelectorAll('.card')].find((c) => c.querySelector('.blist'));
  const blTitle = blCard ? blCard.querySelector('.card-h .t').textContent.trim() : '';
  const blRows = blCard ? blCard.querySelectorAll('.brow').length : 0;
  const blFirst = blCard && blCard.querySelector('.brow')
    ? blCard.querySelector('.brow').textContent.replace(/[ \t\n]+/g, ' ').trim().slice(0, 40) : '';
  const lgText = legend ? legend.textContent.replace(/[ \t\n]+/g, ' ').trim() : '';
  console.log('资金流: 主力=' + (flowbig ? flowbig.textContent.trim() : '—') +
    ' legend="' + lgText + '" 榜="' + blTitle + '" rows=' + blRows + ' first="' + blFirst + '"');


  // 第二阶段：点开「龙虎榜」抽屉，验证 T+1 那条链路的前端也通
  const btn = [...doc.querySelectorAll('button')].find((b) => b.textContent.includes('龙虎榜'));
  if (btn) {
    btn.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
    setTimeout(() => {
      const drawers = [...doc.querySelectorAll('.drawer')];
      const lhb = drawers.find((d) => d.textContent.includes('上榜原因'));
      const rows = lhb ? lhb.querySelectorAll('tbody tr').length : 0;
      const first = lhb && lhb.querySelector('tbody tr')
        ? lhb.querySelector('tbody tr').textContent.replace(/\s+/g, ' ').trim().slice(0, 80) : '';
      console.log(`龙虎榜抽屉: open=${!!lhb && lhb.className.includes('open')} rows=${rows} first="${first}"`);
      detailPhase();
    }, 3000);
    return;
  }
  detailPhase();

  // 第三阶段：点开个股详情 → K 线（用桩上下文，把 drawK 全路径跑一遍）
  function detailPhase() {
    const tr = doc.querySelector('table.grid tbody tr');
    if (!tr) { done(errors.length > 0 || out.tableRows === 0); return; }
    tr.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
    setTimeout(() => {
      // 换成可记录调用的桩，否则 getContext 返回 null，drawK 会在第一行就退出
      const calls = { bar: 0, line: 0, text: 0 };
      window.HTMLCanvasElement.prototype.getContext = () => ({
        setTransform() {}, clearRect() {}, beginPath() {},
        moveTo() {}, lineTo() { calls.line++; }, stroke() {}, fill() {},
        fillRect() { calls.bar++; }, strokeRect() { calls.bar++; },
        fillText() { calls.text++; }, setLineDash() {}, arc() {}, closePath() {},
        save() {}, restore() {}, measureText: () => ({ width: 10 }),
      });
      const perBtn = [...doc.querySelectorAll('.kper button')]
        .find((b) => b.textContent.trim() === '周');
      if (perBtn) perBtn.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
      setTimeout(() => {
        const dr = doc.querySelector('.drawer.open');
        const hasK = !!doc.querySelector('canvas.kchart');
        const meta = doc.querySelector('.kmeta');
        console.log(`个股详情+K线: drawer=${!!dr} canvas=${hasK} ` +
          `periodBtns=${doc.querySelectorAll('.kper button').length} ` +
          `meta="${meta ? meta.textContent.replace(/\s+/g, ' ').trim() : ''}" ` +
          `drawcalls=${JSON.stringify(calls)}`);
        // 选股不在这里验：它要等 B 层拉分时（10~20 s），会把整站冒烟拖到超时。
        // 专项脚本见 tools/pick_check.js。
        done(errors.length > 0 || out.tableRows === 0);
      }, 3200);
    }, 2000);
  }
}, 6500);

function done(fatal) {
  process.exit(fatal ? 1 : 0);
}
