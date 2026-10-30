/* 选股面板专项冒烟：在 happy-dom 里点「选股」tab，看面板/模式 chip/命中行/分时缩略图是否都出来了。
 *
 * 跑法：
 *   NODE_PATH="C:/Users/Administrator/.workbuddy/binaries/node/workspace/node_modules" \
 *   node tools/pick_check.js [http://127.0.0.1:8768]
 *
 * 为什么要单独一个脚本：选股要等 B 层分时精筛（2~10 s），塞进 render_check 的阶段里
 * 会让那个脚本的总时长逼近工具超时；单独跑可以放心地轮询到 25 s。
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
  vm.runInContext(fs.readFileSync(path.join(ROOT, 'web', 'app.js'), 'utf8'), sandbox);
} catch (e) {
  console.log('挂载失败: ' + e.message);
  process.exit(1);
}

process.on('uncaughtException', (e) => { errors.push('uncaught: ' + e.message); });
process.on('unhandledRejection', (e) => { errors.push('unhandled: ' + (e && e.message)); });

// 兜底：绝对不允许挂住
setTimeout(() => { console.log('watchdog: 25 s 到点强制退出'); finish(); }, 25000);

const doc = window.document;
let tries = 0;
let started = 0;

setTimeout(() => {
  const tab = [...doc.querySelectorAll('.tabs .tab')].find((b) => b.textContent.trim() === '选股');
  if (!tab) { console.log('未找到「选股」tab'); return finish(); }
  started = Date.now();
  tab.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));

  const tick = setInterval(() => {
    tries++;
    const panel = doc.querySelector('.pickb');
    const rows = doc.querySelectorAll('table.grid tbody tr').length;
    const chips = doc.querySelectorAll('.mchip').length;
    const sps = doc.querySelectorAll('svg.sp').length;
    const acts = doc.querySelectorAll('.mact').length;
    console.log(`[${tries}s] panel=${!!panel} chips=${chips} rows=${rows} 标签=${acts} 缩略图=${sps}`);
    if (tries >= 22 || (rows > 0 && sps > 0)) {
      clearInterval(tick);
      const note = panel ? panel.querySelector('.mrow.note') : null;
      console.log('漏斗: ' + (note ? note.textContent.replace(/[ \t\n]+/g, ' ').trim() : ''));
      const firstRow = doc.querySelector('table.grid tbody tr');
      console.log('首行: ' + (firstRow ? firstRow.textContent.replace(/[ \t\n]+/g, ' ').trim().slice(0, 90) : ''));
      finish();
    }
  }, 1000);
}, 5000);

function finish() {
  console.log('errors=' + JSON.stringify(errors));
  process.exit(errors.length ? 1 : 0);
}
