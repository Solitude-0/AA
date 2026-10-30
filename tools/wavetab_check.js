/* ETF / 波浪 两个新 Tab 的链路冒烟。
 *
 * 跑法：
 *   NODE_PATH="C:/Users/Administrator/.workbuddy/binaries/node/workspace/node_modules" \
 *   node tools/wavetab_check.js [http://127.0.0.1:8768]
 *
 * 重点验的是「用户看得见」的部分：pane 的 display、表格真的有行、浪型卡真的算出了
 * 结构、斐波那契位与失效位不是空的。波浪这种主观指标最怕的是"算了个寂寞还显示正常"。
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
setTimeout(() => { console.log('watchdog: 90 s 到点强制退出'); finish(); }, 90000);

const doc = window.document;
const shown = (sel) => {
  const el = doc.querySelector(sel);
  if (!el) return null;
  return el.style.display !== 'none';
};
const click = (el) => el.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
const btnByText = (sel, txt) =>
  [...doc.querySelectorAll(sel)].find((b) => b.textContent.trim() === txt);
const press = (key) => window.dispatchEvent(new window.KeyboardEvent('keydown', { key, bubbles: true }));

(async () => {
  await new Promise((r) => setTimeout(r, 4500));   // 等首屏接口

  // ① 快捷键 3 → ETF Tab
  press('3');
  for (let i = 0; i < 20 && !app.etf.rows.length; i++) await new Promise((r) => setTimeout(r, 1000));
  const etfRows = doc.querySelectorAll('.pane-etf tbody tr').length;
  console.log(`① 快捷键3 → tab=${app.tab} ETF可见=${shown('.pane-etf')} 分类=${app.etf.cats.length} 行数=${etfRows} 总数=${app.etf.meta.total}`);
  if (app.tab !== 'etf') errors.push('快捷键 3 没切到 ETF Tab');
  if (shown('.pane-etf') !== true) errors.push('ETF 面板应可见');
  if (!app.etf.cats.length) errors.push('ETF 分类没拉到');
  if (!etfRows) errors.push('ETF 表格没数据');
  if (app.etf.meta.total < 500) errors.push('ETF 总数异常：' + app.etf.meta.total);
  // 溢价率必须是 na，不能是 0（0 会被当成"没有溢价"，是错的）
  // 列位不写死：按表头文字找「溢价」列，加列/调序不会再把这断言弄假
  const ths = [...doc.querySelectorAll('.pane-etf thead th')];
  const premIdx = ths.findIndex((th) => th.textContent.includes('溢价'));
  if (premIdx < 0) errors.push('ETF 表头找不到溢价列');
  const premCells = premIdx >= 0 ? [...doc.querySelectorAll('.pane-etf tbody tr')]
    .map((tr) => (tr.children[premIdx] || {}).textContent || '').slice(0, 5) : [];
  console.log('   溢价列(idx=' + premIdx + '): ' + JSON.stringify(premCells));
  if (!premCells.every((s) => s.trim() === 'na')) errors.push('溢价率列应为 na，实际 ' + JSON.stringify(premCells));

  // ② ETF 分类筛选
  const catBtn = [...doc.querySelectorAll('.pane-etf .pickb .mchip')]
    .find((b) => b.textContent.indexOf('宽基') === 0);
  if (catBtn) {
    click(catBtn);
    for (let i = 0; i < 15 && app.etf.loading; i++) await new Promise((r) => setTimeout(r, 800));
    const cats = new Set((app.etf.rows || []).map((r) => r.cat));
    console.log(`② 点「宽基」 cat=${app.etf.cat} 总数=${app.etf.meta.total} 行内分类=${[...cats].join(',')}`);
    if (app.etf.cat !== 'broad') errors.push('分类没切到 broad');
    if (cats.size > 1 || (cats.size === 1 && !cats.has('broad'))) errors.push('分类筛选后仍混入其它类：' + [...cats]);
  } else errors.push('ETF 分类 chips 里没有「宽基」');

  // ③ ETF 行 → 抽屉（ETF 模式下五档/资金流/逐笔应隐藏）
  await app.openEtf(app.etf.rows[0]);
  await new Promise((r) => setTimeout(r, 1500));
  const bookShown = !!doc.querySelector('.drawer-b .book');
  console.log(`③ ETF 抽屉 标的=${app.detail && app.detail.n} isEtf=${app.detail && app.detail.isEtf} 五档显示=${bookShown}`);
  if (!app.drawerOpen) errors.push('ETF 行点不开抽屉');
  if (!app.detail || !app.detail.isEtf) errors.push('ETF 抽屉没走 isEtf 分支');
  if (bookShown) errors.push('ETF 不该显示五档盘口（ETF 拿不到）');

  // ④ 快捷键 4 → 波浪 Tab
  press('4');
  for (let i = 0; i < 25 && !app.wave.idx.length; i++) await new Promise((r) => setTimeout(r, 1000));
  const okCards = (app.wave.idx || []).filter((r) => r.ok);
  console.log(`④ 快捷键4 → tab=${app.tab} 波浪可见=${shown('.pane-wave')} 浪型卡=${app.wave.idx.length} 有结构=${okCards.length}`);
  if (app.tab !== 'wave') errors.push('快捷键 4 没切到波浪 Tab');
  if (shown('.pane-wave') !== true) errors.push('波浪面板应可见');
  if (!app.wave.tags.length) errors.push('波浪标签没拉到');
  if (!okCards.length) errors.push('指数/自选浪型一张都没算出来');
  const c0 = okCards[0];
  if (c0) {
    console.log(`   样例 ${c0.name}: ${c0.pattern} / ${c0.stage.n} / 目标 ${(c0.stage.targets || []).length} 个 / 失效 ${c0.stage.invalid}`);
    if (!c0.stage || !c0.stage.n) errors.push('浪型卡缺 stage');
    if (c0.stage.invalid == null) errors.push('浪型卡缺失效位');
  }

  // ⑤ 全市场扫描
  app.wave.probe = 30;
  await app.loadWaveScan();
  for (let i = 0; i < 30 && app.wave.busy; i++) await new Promise((r) => setTimeout(r, 1000));
  const st = app.wave.stat || {};
  console.log(`⑤ 扫描 pool=${st.pool} probed=${st.probed} probe_ok=${st.probe_ok} 命中=${st.total} ms=${st.ms}`);
  if (!st.pool) errors.push('扫描池为空');
  if (st.probe_ok === 0) errors.push('日线一条都没取到（probe_ok=0），此时不该有结论');
  if (!app.wave.rows.length) errors.push('扫描结果为空');
  const r0 = app.wave.rows[0];
  if (r0) {
    console.log(`   样例 ${r0.c} ${r0.n}: ${r0.tag_n} / ${r0.stage} / tgt ${r0.tgt} / inv ${r0.inv} / conf ${r0.conf}`);
    if (!r0.tag_n) errors.push('扫描结果缺浪型标签');
    if (r0.inv == null) errors.push('扫描结果缺失效位');
  }

  // ⑥ 抽屉 K 线波浪叠加
  await app.openDetail('600519');
  await new Promise((r) => setTimeout(r, 1500));
  await app.toggleKWave();
  for (let i = 0; i < 20 && app.kWave.loading; i++) await new Promise((r) => setTimeout(r, 800));
  const wv = app.kWave.data;
  console.log(`⑥ K线叠加 on=${app.kWave.on} ok=${wv && wv.ok} 浪型=${wv && wv.pattern} 标注=${wv && (wv.waves || []).length} 斐波=${wv && ((wv.fib.retr || []).length + (wv.fib.ext || []).length)}`);
  if (!app.kWave.on) errors.push('波浪开关没打开');
  if (!wv || !wv.ok) errors.push('抽屉波浪没算出结构');
  if (wv && !(wv.waves || []).length) errors.push('抽屉波浪缺浪型标注');
  if (wv && !(wv.fib.retr || []).length) errors.push('抽屉波浪缺斐波那契位');
  app.drawK();          // 叠加分支要能画完不抛错
  console.log('   drawK 带波浪叠加执行完成');

  // ⑦ 上证指数卡 → 抽屉：指数没有五档/资金流，但快照、K 线、浪型必须齐
  const hasIdx = (app.wave.idx || []).some((r) => r.code === 'sh000001');
  await app.openDetailW('sh000001');
  for (let i = 0; i < 25 && app.kWave.loading; i++) await new Promise((r) => setTimeout(r, 800));
  const idxBook = !!doc.querySelector('.drawer-b .book');
  const idxBars = (app.kBars || []).length;
  const idxWave = app.kWave.data;
  console.log(`⑦ 上证指数抽屉 卡存在=${hasIdx} drawer=${app.drawerOpen} index=${app.detail && app.detail.index} K线=${idxBars} 浪型ok=${idxWave && idxWave.ok} 五档显示=${idxBook}`);
  if (!hasIdx) errors.push('指数/自选卡里没有上证指数');
  if (!app.drawerOpen) errors.push('上证指数卡点不开抽屉');
  if (!app.detail || !app.detail.index) errors.push('指数抽屉没走 index 分支');
  if (!idxBars) errors.push('指数抽屉 K 线为空');
  if (!(idxWave && idxWave.ok)) errors.push('指数抽屉浪型没算出来');
  if (idxBook) errors.push('指数不该显示五档盘口');

  finish();
})().catch((e) => { errors.push('流程异常: ' + e.message); finish(); });

let finished = false;
function finish() {
  if (finished) return;
  finished = true;
  console.log('errors=' + JSON.stringify(errors));
  process.exit(errors.length ? 1 : 0);
}
