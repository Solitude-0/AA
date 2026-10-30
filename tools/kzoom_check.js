/* K 线缩放 / 压力支撑 专项验证
 *
 * 和 render_check 的区别：render_check 只保证「页面没报错、有数据行」，
 * 而缩放这套东西的核心是**数学** —— 锚点对不对、边界夹得对不对、
 * 压力线取的是不是真摆动高点、个数有没有超。这些不出错但会画错，
 * 静态渲染检查看不见，必须喂一组确定的 K 线去算。
 *
 * 做法：happy-dom 里挂载 app.js（mount() 的返回值就是根实例 proxy），
 * 先 openDetail 让抽屉里的 <canvas class="kchart"> 真正渲染出来（否则 $refs.kc 为空、
 * drawK 会在第一行就 return），再喂构造好的 bars 直接调方法断言。
 *
 * 跑法：
 *   NODE_PATH="C:/Users/Administrator/.workbuddy/binaries/node/workspace/node_modules" \
 *   node tools/kzoom_check.js
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { Window } = require('happy-dom');

const ROOT = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(ROOT, 'web', 'index.html'), 'utf8');
const body = html.slice(html.indexOf('<body>') + 6, html.indexOf('<script src="vendor'));

const window = new Window({ url: 'http://127.0.0.1:8768/' });
window.document.body.innerHTML = body;

/* 假 2D 上下文：drawK 必须能完整跑一遍并留下 kBox，
 * 后面测拖拽 / 命中测试用的就是这套几何 */
let ctxN = 0;
const mkCtx = () => new Proxy({}, {
  get(t, k) {
    if (k === 'measureText') return () => ({ width: 24 });
    if (k in t) return t[k];
    return () => { ctxN++; };
  },
  set(t, k, v) { t[k] = v; return true; },
});
window.HTMLCanvasElement.prototype.getContext = () => mkCtx();

const sandbox = {
  console: { log() {}, warn() {}, error() {} },
  setTimeout, setInterval, clearTimeout, clearInterval,
  Promise, JSON, Math, Date, Number, String, Boolean, Array, Object, Map, Set,
  RegExp, Error, TypeError, isFinite, parseInt, parseFloat, encodeURIComponent,
  document: window.document, location: window.location, navigator: window.navigator,
  localStorage: window.localStorage,
  getComputedStyle: (el) => window.getComputedStyle(el),
  requestAnimationFrame: (f) => setTimeout(() => f(Date.now()), 16),
  // 完全不碰网络：接口一律返回空对象，只验证 K 线这一块的纯计算
  fetch: async () => ({ ok: true, status: 200, json: async () => ({}), text: async () => '' }),
  URL, URLSearchParams, TextEncoder, TextDecoder,
  WebSocket: undefined, AudioContext: undefined, devicePixelRatio: 1,
  addEventListener: (...a) => window.addEventListener(...a),
  removeEventListener: (...a) => window.removeEventListener(...a),
};
sandbox.window = window;
sandbox.globalThis = sandbox;
for (const k of ['Element', 'HTMLElement', 'SVGElement', 'Node', 'Text', 'Comment',
  'DocumentFragment', 'DOMParser', 'MutationObserver', 'CustomEvent', 'Event',
  'KeyboardEvent', 'MouseEvent', 'Image', 'CSS', 'File', 'Blob', 'FormData']) {
  if (window[k]) sandbox[k] = window[k];
}
vm.createContext(sandbox);

vm.runInContext(fs.readFileSync(path.join(ROOT, 'web', 'vendor', 'vue.global.prod.js'), 'utf8'), sandbox);
const app = vm.runInContext(fs.readFileSync(path.join(ROOT, 'web', 'app.js'), 'utf8'), sandbox);

/* 构造确定性数据：300 根三角波，每 20 根一个 22.12 的高点、一个 17.48 的低点。
 * 这样「哪些价位被反复试探过」是已知的，便于断言压力/支撑落在哪儿。 */
function mkBars(n) {
  const out = [];
  for (let i = 0; i < n; i++) {
    const ph = (i % 20) / 20;
    const base = ph < 0.5 ? 17.6 + 4.4 * (ph / 0.5) : 22 - 4.4 * ((ph - 0.5) / 0.5);
    out.push({
      t: '2026-01-' + String((i % 28) + 1).padStart(2, '0'),
      o: +(base - 0.05).toFixed(2), c: +(base + 0.05).toFixed(2),
      h: +(base + 0.12).toFixed(2), l: +(base - 0.12).toFixed(2),
      v: 1000 + (i % 7) * 300, amt: 1e7,
    });
  }
  return out;
}

const fails = [];
const ck = (name, cond, got) => { if (!cond) fails.push(name + ' → ' + JSON.stringify(got)); };
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

/* --real=600519 时拉真实 K 线来算。合成三角波太规整，
 * 只能证明算法自身自洽，证明不了在真实走势上算出来的价位是否合理。 */
const REAL = (process.argv.find((a) => a.startsWith('--real=')) || '').slice(7);

(async () => {
  await wait(500);                       // 等挂载 + 首屏数据
  await app.openDetail('600519');        // 抽屉打开后 <canvas.kchart> 才真的存在
  await wait(300);
  await app.$nextTick();

  const cv = window.document.querySelector('canvas.kchart');
  console.log('画布存在=' + !!cv + ' $refs.kc=' + !!app.$refs.kc
    + ' 图例=' + window.document.querySelectorAll('.klegend').length
    + ' 视角按钮=' + window.document.querySelectorAll('.kzoom button').length);
  ck('抽屉里能取到 K 线画布', !!cv, !!cv);
  ck('Vue ref 已注册', !!app.$refs.kc, !!app.$refs.kc);
  ck('视角按钮渲染出 4 个', window.document.querySelectorAll('.kzoom button').length === 4,
    window.document.querySelectorAll('.kzoom button').length);
  if (!cv) { console.log('FAIL: 无画布，后续跳过'); process.exit(1); }

  // happy-dom 给不出真实布局：手动给一个 400×236 的矩形，缩放锚点测试才有意义
  cv.getBoundingClientRect = () => ({ left: 0, top: 0, right: 400, bottom: 236, width: 400, height: 236 });

  let N = 300;
  app.kBars = mkBars(N);
  if (REAL) {
    try {
      const r = await fetch('http://127.0.0.1:8768/api/kline/' + REAL + '?period=day');
      const j = await r.json();
      if (!(j.bars || []).length) throw new Error('接口没给 bars');
      app.kBars = j.bars.map((b) => ({ t: b.t, o: b.o, c: b.c, h: b.h, l: b.l, v: b.v, amt: b.amt }));
      N = app.kBars.length;
      console.log('真实数据 ' + REAL + ' ' + (j.name || '') + ': ' + N + ' 根, '
        + app.kBars[0].t + ' ~ ' + app.kBars[N - 1].t
        + ', 现价 ' + app.kBars[N - 1].c);
    } catch (e) {
      console.log('拉取真实数据失败(' + e.message + ')，退回合成数据');
      N = 300; app.kBars = mkBars(N);
    }
  }
  app.kPeriod = 'day';
  app.kZoom = 0; app.kOff = 0;
  await app.$nextTick();
  ck('有数据时图例出现', window.document.querySelectorAll('.klegend').length === 1,
    window.document.querySelectorAll('.klegend').length);

  // ① 默认视野：日线自动 120 根，贴着最新一根
  let v = app.kVis();
  console.log('① 默认视野: n=' + v.n + ' i0=' + v.i0 + ' i1=' + v.i1);
  ck('默认 120 根', v.n === 120, v);
  ck('默认贴最新一根', v.i1 === N - 1, v);
  ck('默认起点', v.i0 === N - 120, v);

  // ② 平移与越界夹紧
  app.kOff = 50;
  ck('平移后右端', app.kVis().i1 === N - 1 - 50, app.kVis());
  app.kOff = 99999;
  ck('右越界夹紧到最左', app.kVis().i0 === 0, app.kVis());
  app.kOff = -5;
  ck('负偏移夹紧到 0', app.kVis().i1 === N - 1, app.kVis());
  app.kOff = 0;

  // ③ 缩放边界
  app.kZoom = 5;
  ck('最少 20 根', app.kVis().n === 20, app.kVis());
  app.kZoom = 99999;
  ck('最多不超过总量', app.kVis().n === N, app.kVis());
  app.kZoom = 0;

  // ④ drawK 能跑完整并留下 kBox（拖拽 / 命中测试的地基）
  ctxN = 0;
  let threw = null;
  try { app.drawK(); } catch (e) { threw = e.message; }
  console.log('④ drawK: ctx 调用 ' + ctxN + ' 次, 异常=' + (threw || '无'), 'kBox=' + JSON.stringify(app.kBox));
  ck('drawK 不抛异常', !threw, threw);
  ck('drawK 真的在画', ctxN > 50, ctxN);
  ck('drawK 留下 kBox', !!app.kBox, app.kBox);
  const box0 = app.kBox;

  // ⑤ 滚轮缩放的锚点：缩放前后光标下那一根的**相对位置**应保持不变
  app.kZoom = 0; app.kOff = 0; app.drawK();
  const b = app.kBox;
  const ai = b.i1 - 60;                                   // 挑一根位于视野中部的
  const clientX = b.padL + b.step * (ai - b.i0 + 0.5);    // 它的像素中心
  const r0 = (clientX - b.padL) / b.iw;
  const before = app.kVis();
  app.kWheel({ deltaY: -1, clientX });                    // 向上滚 = 放大
  const after = app.kVis();
  console.log('⑤ 锚点缩放: ' + before.n + ' 根 → ' + after.n + ' 根,'
    + ' 锚点根#' + ai + ' 偏移前 ' + (ai - before.i0) + ' 后 ' + (ai - after.i0));
  ck('向上滚 = 视野变小', after.n < before.n, [before.n, after.n]);
  ck('锚点仍在窗口内', ai >= after.i0 && ai <= after.i1, [ai, after.i0, after.i1]);
  ck('锚点相对位置保持（±1 根）',
    Math.abs((ai - after.i0) - r0 * (after.n - 1)) <= 1,
    { want: r0 * (after.n - 1), got: ai - after.i0 });

  // 反向滚回去不应跑飞：连续 5 次放大后仍不越界
  for (let i = 0; i < 5; i++) app.kWheel({ deltaY: -1, clientX });
  const vv = app.kVis();
  ck('连续放大后仍合法', vv.i0 >= 0 && vv.i1 <= N - 1 && vv.n >= 20, vv);
  app.kZoom = 0; app.kOff = 0; app.drawK();

  // ⑥ 压力 / 支撑
  const lv = app.kLevels();
  const last = app.kBars[N - 1].c;
  console.log('⑥ 压力: ' + lv.res.map((c) => 'R ' + c.p.toFixed(2) + '×' + c.n).join('  '));
  console.log('   支撑: ' + lv.sup.map((c) => 'S ' + c.p.toFixed(2) + '×' + c.n).join('  '));
  console.log('   筹码: ' + lv.dense.map((d) => d.p.toFixed(2) + '/' + (d.share * 100).toFixed(0) + '%').join('  '));
  ck('有压力线', lv.res.length > 0, lv.res.length);
  ck('有支撑线', lv.sup.length > 0, lv.sup.length);
  ck('压力最多 3 条', lv.res.length <= 3, lv.res.length);
  ck('支撑最多 3 条', lv.sup.length <= 3, lv.sup.length);
  ck('压力全在现价上方（>0.2%）', lv.res.every((c) => c.p > last * 1.002), lv.res.map((c) => c.p));
  ck('支撑全在现价下方（>0.2%）', lv.sup.every((c) => c.p < last * 0.998), lv.sup.map((c) => c.p));
  ck('压力按分值降序', lv.res.every((c, i) => i === 0 || lv.res[i - 1].n >= c.n), lv.res.map((c) => c.n));
  ck('筹码占比超过 6% 才画', lv.dense.every((d) => d.share > 0.06), lv.dense.map((d) => d.share));

  // ⑦ 缩小视野后重算不崩
  app.kZoom = 30; app.drawK();
  const lv2 = app.kLevels();
  console.log('⑦ 30 根视野: 压力 ' + lv2.res.length + ' 条 / 支撑 ' + lv2.sup.length + ' 条');
  ck('小视野线数合规', lv2.res.length <= 3 && lv2.sup.length <= 3, lv2);
  app.kZoom = 0;

  // ⑧ 拖拽平移：向右拖 = 看更早的一段
  app.kZoom = 60; app.kOff = 0; app.drawK();
  const st = app.kBox.step;
  app.kDrag = { x: 100, off: app.kOff };
  app.kDragMove({ clientX: 100 + st * 5 });
  console.log('⑧ 右拖 5 根(步长' + st.toFixed(2) + 'px) → kOff=' + app.kOff);
  ck('右拖让 kOff 增加', app.kOff === 5, app.kOff);
  app.kDragMove({ clientX: 100 - st * 3 });
  ck('往回拖被夹到 0', app.kOff === 0, app.kOff);
  app.kDragMove({ clientX: 100 + st * 9999 });
  ck('拖到底不超过上界', app.kOff === N - app.kVis().n, app.kOff);
  app.kDrag = null;

  // ⑨ 拖拽时不更新 tooltip（防闪烁）
  app.kDrag = { x: 0, off: 0 };
  app.kTip = null;
  app.kMove({ clientX: 200 });
  ck('拖拽中 tooltip 保持空', app.kTip === null, app.kTip);
  app.kDrag = null;
  app.kMove({ clientX: b.padL + b.step * 5 });
  ck('松手后 tooltip 正常出', !!app.kTip, app.kTip);

  // ⑩ 按钮行为
  app.kZoom = 0; app.kOff = 40;
  app.kZoomStep(1);
  console.log('⑩ 放大后 kZoom=' + app.kZoom + ' kOff=' + app.kOff);
  ck('按钮缩放复位 kOff', app.kOff === 0, app.kOff);
  ck('按钮缩放视野变小', app.kZoom < 120, app.kZoom);
  app.kFull();
  ck('全览 = 全部根数', app.kVis().n === N, app.kVis());
  app.kReset();
  v = app.kVis();
  ck('复位回默认 120 根且贴最新', v.n === 120 && v.i1 === N - 1, v);

  // ⑪ 数据不足时不能崩
  app.kBars = mkBars(8);
  app.kZoom = 0; app.kOff = 0;
  let threw2 = null;
  try { app.drawK(); const l = app.kLevels(); ck('8 根时线为空', l.res.length === 0 && l.sup.length === 0, l); }
  catch (e) { threw2 = e.message; }
  ck('数据不足不抛异常', !threw2, threw2);

  console.log(fails.length ? 'FAIL(' + fails.length + '):' + NL_I() + fails.map((f) => '  · ' + f).join(NL_I())
    : 'KZOOM_ALL_PASS');
  process.exit(fails.length ? 1 : 0);
})().catch((e) => { console.log('CRASH: ' + e.stack); process.exit(1); });

function NL_I() { return '\n'; }
