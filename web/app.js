/* 分析看盘 · 前端逻辑（Vue 3 全局构建，无构建步骤）
 *
 * 数据流：REST 拿首屏 + WS 拿增量，WS 连续失败 3 次自动降级为 3 s 轮询。
 * 表格只持有「当前页」的行，全市场 5343 行留在服务端——前端持有全量是最常见的自伤。
 */
const { createApp } = Vue;

const SESSION_CN = {
  pre: '盘前', auction: '集合竞价', preopen: '开盘前', trading: '交易中',
  lunch: '午休', closing: '收盘竞价', closed: '已收盘',
};
const KIND_CN = { limit_open: '开板', vol_surge: '量比', speed: '涨速',
  speed_up: '拉升', speed_dn: '跳水' };

/* K 线最短视野。kVis / kWheel / kZoomStep 三处必须用同一个数——
 * 之前这里写 10、那两处写 20，直接赋值就能造出 10 根的窗口，
 * 属于典型的"两个数字做同一件事"。20 根是能看出形态的下限，再少没有意义。 */
const K_MIN_VIEW = 20;
const HIST_LABELS = ['<-9', '-9~-7', '-7~-5', '-5~-3', '-3~-1', '-1~0',
                     '0~1', '1~3', '3~5', '5~7', '7~9', '>9'];

/* 只影响显示、不影响判断的格式化。±0 必须归一，否则会出现绿色 "+0.00" */
function normZero(v) {
  const n = Number(v || 0);
  return Math.abs(n) < 0.005 ? 0 : n;
}

function Api() {
  const get = async (url) => {
    const r = await fetch(url, { cache: 'no-store' });
    if (!r.ok) throw new Error(`${r.status} ${url}`);
    return r.json();
  };
  const post = async (url, body) => {
    const r = await fetch(url, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
    if (!r.ok) throw new Error(`${r.status} ${url}`);
    return r.json();
  };
  return { get, post };
}
const api = Api();

// 模式参数的中文标签（键名与 /api/picker/meta 的 params 一一对应）
const P_LABEL = {
  open_pct: '竞价涨幅≥%', vr: '量比≥', oi: '外内比≥', pullback: '回调≤%',
  flat_min: '横盘分钟≥', amp_max: '平台振幅≤%', vol_x: '突破放量×',
  within: '金叉近N根', after: '14:30后涨幅≥%', near_high: '距日高%',
};

createApp({
  data() {
    return {
      idx: [],
      breadth: {},
      flow: { market: [], stocks: [], boards: [] },
      // 资金流来源元信息：东财是「全市场 + 真分钟曲线」，新浪兜底是「样本池代理」，
      // 两者标度不同，标签必须跟着变，否则数字会被当成大盘总量读。
      flowMeta: { src: '', pool: 0, label: '' },

      // 选股：模式元数据与默认参数都来自 /api/picker/meta，前端不硬编码阈值
      pick: {
        modes: [], markets: [], sel: [], rows: [], stat: null,
        loading: false, err: '', busy: false, jump: '',
        // general / adv 必须带上初始形状：模板直接读 general.boards.includes，
        // 留空对象的话 meta 还没回来那一帧就会抛 undefined，整个渲染函数炸掉。
        // 这组默认值与 app/compute/picker.py 的 GENERAL / MODES.params 对齐。
        general: {
          price_min: 3, price_max: 100, cap_min: 30, cap_max: 800,
          to_min: 2, to_max: 15, amt_min: 1,
          excl_st: 1, excl_halt: 1, excl_zt: 0, boards: ['sh', 'sz'],   // 默认沪深主板
        },
        adv: { open_pct: 1.5, vr: 1.5, oi: 1.5, flat_min: 20, amp_max: 1.2,
          vol_x: 1.5, pullback: 3, within: 3, after: 3, near_high: 95 },
      },

      boards: { i: [], c: [], r: [] },
      boardKind: 'i', boardSort: 'pct', boardView: 'matrix',
      boardStale: false, boardOpen: false, boardSel: null, boardMembers: [],
      kindName: { i: '行业', c: '概念', r: '地域' },

      order: [],            // 当前页的代码顺序（服务端给的排序）
      quotes: {},           // code -> 行数据（WS 增量就地合并）
      meta: { page: 1, size: 50, total: 0, sort: 'pct', order: 'desc' },
      // 市场板块多选：空数组 = 全部。口径与选股面板的「板块」一致（sh/sz/cyb/kcb/bj）
      boardChips: [
        { k: 'sh', n: '沪主板' }, { k: 'sz', n: '深主板' }, { k: 'cyb', n: '创业板' },
        { k: 'kcb', n: '科创板' }, { k: 'bj', n: '北证' },
      ],
      filters: { kw: '', pct_min: null, amt_min: null, excl_st: false, only_zt: false, boards: [] },
      // 板块信息面板 + 个股操作建议（详情抽屉）
      sboard: { open: false, loading: false, boards: [], idx: null, sel: '',
                panel: null, ploading: false },
      adv: null, advLoading: false,
      tab: 'market',
      // ── ETF Tab ──
      etf: { rows: [], meta: { total: 0, page: 1, size: 50, sort: 'pct', order: 'desc' },
             cats: [], cat: '', kw: '', amt_min: null, pct_min: null, jump: '', loading: false },
      // ── 波浪 Tab ──
      wave: { tags: [], sel: [], idx: [], err: null, rows: [], stat: null,
              busy: false, bloading: false, pct: null,
              min_conf: 55, probe: 60, amt_min: null },
      wavePcts: [1.5, 2, 3, 5],
      // ── 抽屉 K 线浪型叠加 ──
      kWave: { on: false, loading: false, data: null, seq: 0 },
      jumpErr: '',
      view: 'market', loadErr: '',

      watchGroups: { '默认': [] }, watchActive: '默认',
      selCode: '', selIndex: -1,

      book: null, bookCode: '', bookName: '',
      ticksMap: {}, alerts: [],
      detail: null, drawerOpen: false, histSeries: [],
      kPeriod: 'day', kBars: [], kMeta: {}, kTip: null, kSeq: 0,
      // kZoom = 视野内画多少根（0 = 自动：日/周/月 120 根，分钟线 160 根）
      // kOff  = 距今最后一根往左退了几根（拖拽平移），换周期/换标的时整体复位
      kZoom: 0, kOff: 0, kDrag: null, kBox: null,
      kPeriods: [
        { k: '1m', n: '1分' }, { k: '5m', n: '5分' }, { k: '15m', n: '15分' },
        { k: '30m', n: '30分' }, { k: '60m', n: '60分' },
        { k: 'day', n: '日' }, { k: 'week', n: '周' }, { k: 'month', n: '月' },
      ],
      lhbOpen: false, lhbDate: '', lhbRows: [], lhbMeta: {}, lhbMsg: '',

      lagSec: 0, lagAlarm: false, session: '', sessionNote: '',
      health: {}, wsState: 'connecting', wsTry: 0,
      sound: true, theme: 'dark', thrift: false, help: false,
      pollTimer: null, healthTimer: null, pollTick: 0,
      ws: null, lastFrame: 0, subscribed: new Set(),
      histLabels: HIST_LABELS, kindLabel: KIND_CN,
    };
  },

  computed: {
    pages() { return Math.max(1, Math.ceil((this.meta.total || 0) / this.meta.size)); },
    // ETF 分页数。之前误放在 methods 区，模板 {{ etfPages }} 不带括号就把函数体渲染成了字符串
    etfPages() { return Math.max(1, Math.ceil((this.etf.meta.total || 0) / (this.etf.meta.size || 50))); },
    // 市场分析页的大盘浪型卡：与「波浪」Tab 共用同一批 /api/wave_batch 结果，不重复算
    shWave() {
      return (this.wave.idx || []).find((r) => r.code === 'sh000001') || null;
    },
    rows() {
      const out = [];
      for (const c of this.order) {
        const r = this.quotes[c];
        if (r) out.push(r);
      }
      return out;
    },
    watchRows() {
      if (this.view !== 'watch') return [];
      const g = this.watchGroups[this.watchActive] || [];
      return g.map((c) => this.quotes[c]).filter(Boolean);
    },
    groupNames() { return Object.keys(this.watchGroups); },
    boardRows() {
      const rows = (this.boards[this.boardKind] || []).slice();
      const k = this.boardSort;
      // 新浪兜底源没有 net/up，按这两列排会变成原序，干脆退回涨幅
      const key = (!this.boardHasNet && (k === 'net' || k === 'up')) ? 'pct' : k;
      rows.sort((a, b) => (b[key] || 0) - (a[key] || 0));
      return rows;
    },
    boardHasNet() {
      const rows = this.boards[this.boardKind] || [];
      return rows.some((b) => b.src !== 'sina');
    },
    flowMain() {
      const m = this.flow.market;
      return m.length ? m[m.length - 1].main : 0;
    },
    // 东财给的是全天分钟序列；新浪兜底是本地按分钟累计的，只能从服务启动算起。
    // 两者点数含义不同，图例必须跟着换，否则 240 点会被误读成「今天只跑了 4 小时」。
    flowSpan() {
      const n = (this.flow.market || []).length;
      if (!n) return '开盘';
      return this.flowMeta.src === 'em' ? `全天 ${n} 点` : `自启动累计 ${n} 点`;
    },
    flowLabel() {
      if (this.flowMeta.label) return this.flowMeta.label;
      return this.flowMeta.src === 'em' ? '分钟级主力净额' : '资金流数据源不可用';
    },
    // 东财板块源回来时优先看板块榜；新浪兜底源没有板块净额，退回个股榜。
    flowTopIsBoard() { return !!(this.flow.boards && this.flow.boards.length); },
    flowTop() {
      const rows = this.flowTopIsBoard ? this.flow.boards : (this.flow.stocks || []);
      return rows.slice(0, 10);
    },
    flowTopTitle() {
      return this.flowTopIsBoard ? '板块主力净流入 Top10' : '个股主力净流入 Top10';
    },
    flowTopNote() {
      return this.flowTopIsBoard ? '亿元'
        : `亿元 · 成交额前 ${this.flowMeta.pool || 0} 名`;
    },
    curTicks() { return this.ticksMap[this.bookCode] || []; },
    flowRows() {
      const f = this.detail && this.detail.flow;
      if (!f) return [];
      const items = [
        { k: '超大单', v: f.super }, { k: '大单', v: f.large },
        { k: '中单', v: f.medium }, { k: '小单', v: f.small },
      ];
      const max = Math.max(0.01, ...items.map((x) => Math.abs(x.v || 0)));
      return items.map((x) => ({ ...x, w: `${Math.min(100, Math.abs(x.v || 0) / max * 100)}%` }));
    },
    // 当前选中模式的可调参数（键名 → 中文标签）。只列选中的，避免一次摆一大片。
    advList() {
      // P_LABEL 是模块顶层常量，不是组件属性 —— 写 this.PLABEL 会拿到 undefined，
      // 于是 lbl[k] 抛 "Cannot read properties of undefined"，整个渲染函数中断、
      // 页面直接白掉。这条踩过一次，别再踩。
      const lbl = P_LABEL;
      const out = [];
      const seen = new Set();
      for (const m of this.pick.modes) {
        if (!this.pick.sel.includes(m.k)) continue;
        for (const k of Object.keys(m.params || {})) {
          if (seen.has(k)) continue;
          seen.add(k);
          out.push({ k, n: lbl[k] || k });
        }
      }
      return out;
    },
    pickEmptyText() {
      if (this.view === 'watch') return '自选池为空，点表格里的 ★ 加入';
      if (this.view === 'pick') {
        if (this.pick.loading) return '筛选中…（会为候选拉分时序列）';
        if (this.pick.err) return this.pick.err;
        if (this.pick.stat) return '没有命中的标的：放宽通用筛选，或减少模式';
        return '点「执行」开始筛选';
      }
      return '数据采集中…';
    },
    lagText() {
      if (this.session === 'lunch' || this.session === 'closed') return '价格冻结';
      const s = Number(this.lagSec || 0);
      return `延迟 ${s.toFixed(1)}s`;
    },
    lagCls() {
      if (this.session === 'lunch' || this.session === 'closed' || this.session === 'pre') return '';
      if (this.lagAlarm) return 'bad';
      return Number(this.lagSec) > 5 ? 'warn' : 'ok';
    },
    lagTitle() {
      return '源端快照时间戳与本机的差。午休/盘后时间戳不推进，累积到几十秒属正常。';
    },
    sessionText() { return SESSION_CN[this.session] || '—'; },
    wsText() {
      return { live: '实时推送', polling: '轮询兜底', connecting: '连接中', down: '已断开' }[this.wsState] || '—';
    },
    wsCls() {
      return this.wsState === 'live' ? 'ok' : (this.wsState === 'polling' ? 'warn' : 'bad');
    },
  },

  methods: {
    /* ── 格式化 ───────────────────────────────── */
    fmt(v) {
      if (v === null || v === undefined || v === '') return '—';
      const n = Number(v);
      if (!isFinite(n)) return '—';
      return n.toFixed(2);
    },
    pct(v) {
      const n = normZero(v);
      return `${n > 0 ? '+' : ''}${n.toFixed(2)}%`;
    },
    fmtSigned(v, d) {
      const n = normZero(v);
      return `${n > 0 ? '+' : ''}${n.toFixed(d === undefined ? 2 : d)}`;
    },
    cls(v) {
      const n = normZero(v);
      return n > 0 ? 'up' : (n < 0 ? 'down' : 'flat');
    },
    share(n) {
      const t = this.breadth.total || 0;
      if (!t) return '—';
      return `${((n || 0) / t * 100).toFixed(1)}%`;
    },
    amtText(yi) {
      const v = Number(yi || 0);
      return v >= 10000 ? `${(v / 10000).toFixed(2)} 万亿` : `${v.toFixed(0)} 亿`;
    },
    histCls(i) { return i < 6 ? 'd' : 'u'; },
    histH(h) {
      const arr = this.breadth.hist || [];
      const max = Math.max(1, ...arr);
      return `${Math.max(1, (h || 0) / max * 34)}px`;
    },
    histTitle(i, h) { return `${HIST_LABELS[i]}%：${h} 只`; },
    cellStyle(b) {
      const p = normZero(b.pct);
      const a = Math.min(0.55, 0.08 + Math.abs(p) / 10 * 0.45);
      const c = p >= 0 ? 'var(--up)' : 'var(--down)';
      return { background: `color-mix(in srgb, ${c} ${(a * 100).toFixed(0)}%, transparent)` };
    },
    barW(net) {
      const items = this.flow.boards.slice(0, 10);
      const max = Math.max(0.01, ...items.map((x) => Math.abs(x.net || 0)));
      return `${Math.min(100, Math.abs(net || 0) / max * 100)}%`;
    },
    depthW(v) {
      const b = this.book;
      if (!b) return '0%';
      const all = [...(b.b || []), ...(b.a || [])].map((x) => x[1] || 0);
      const max = Math.max(1, ...all);
      return `${Math.min(100, (v || 0) / max * 100)}%`;
    },
    ar(k) { return this.meta.sort === k ? (this.meta.order === 'desc' ? '▼' : '▲') : ''; },
    etfAr(k) { return this.etf.meta.sort === k ? (this.etf.meta.order === 'desc' ? '▼' : '▲') : ''; },

    /* ── 数据加载 ─────────────────────────────── */
    async loadIndex() {
      try {
        const j = await api.get('/api/index');
        this.idx = j.rows || [];
        if (j.lag_sec !== null && j.lag_sec !== undefined) this.lagSec = j.lag_sec;
      } catch (e) { /* 顶栏失败不打断页面 */ }
    },
    async loadBreadth() {
      try {
        const j = await api.get('/api/breadth');
        this.breadth = j;
        this.session = j.session || this.session;
      } catch (e) { /* ignore */ }
    },
    async loadBoards(kind) {
      const k = kind || 'i';
      try {
        const j = await api.get(`/api/boards?kind=${k}&sort=${this.boardSort}&size=600`);
        this.boards[k] = j.rows || [];
      } catch (e) { this.loadErr = '板块接口失败'; }
    },
    async loadFlow() {
      try {
        const j = await api.get('/api/flow/market');
        this.flow.market = j.rows || [];
        this.flowMeta = { src: j.src || '', pool: j.pool || 0, label: j.label || '' };
        const s = await api.get('/api/flow/stocks?limit=100');
        this.flow.stocks = s.rows || [];
        const b = await api.get('/api/flow/boards?limit=30');
        this.flow.boards = b.rows || [];
        this.drawFlow();
      } catch (e) { /* ignore */ }
    },
    async loadAlerts() {
      try {
        const j = await api.get('/api/alert?n=80');
        this.alerts = j.rows || [];
      } catch (e) { /* ignore */ }
    },
    async loadHealth() {
      try {
        const j = await api.get('/api/health');
        this.health = j;
        this.session = j.scheduler.session;
        this.sessionNote = j.scheduler.session_note;
        this.lagAlarm = j.scheduler.lag_alarm;
        this.thrift = j.scheduler.thrift;
        if (j.scheduler.lag_sec !== null && j.scheduler.lag_sec !== undefined) {
          this.lagSec = j.scheduler.lag_sec;
        }
        const st = j.scheduler.stale || {};
        this.boardStale = (st['board_' + this.boardKind] || 0) > 20;
      } catch (e) { /* ignore */ }
    },

    async fetchRank() {
      if (this.view !== 'market') return;
      const f = this.filters;
      const q = new URLSearchParams({
        sort: this.meta.sort, order: this.meta.order,
        page: this.meta.page, size: this.meta.size,
      });
      if (f.kw) q.set('kw', f.kw);
      if (f.pct_min !== null && f.pct_min !== '') q.set('pct_min', f.pct_min);
      if (f.amt_min !== null && f.amt_min !== '') q.set('amt_min', f.amt_min);
      if (f.excl_st) q.set('excl_st', 'true');
      if (f.only_zt) q.set('only_zt', 'true');
      if (f.boards && f.boards.length) q.set('boards', f.boards.join(','));

      try {
        const j = await api.get('/api/rank?' + q.toString());
        this.meta.total = j.meta.total;
        this.order = (j.rows || []).map((r) => r.c);
        this.quotes = {};
        for (const r of (j.rows || [])) this.quotes[r.c] = { ...r };
        this.loadErr = '';
        this.resubQuotes();
      } catch (e) {
        this.loadErr = '排行接口失败：' + e.message;
      }
    },

    async loadWatch() {
      const codes = this.watchGroups[this.watchActive] || [];
      this.order = codes.slice();
      if (!codes.length) return;
      try {
        const j = await api.get('/api/quotes?codes=' + codes.join(','));
        for (const r of (j.rows || [])) this.quotes[r.c] = { ...(this.quotes[r.c] || {}), ...r };
        this.resubQuotes();
      } catch (e) { this.loadErr = '自选加载失败'; }
    },

    /* ── 选股 ─────────────────────────────────── */
    async loadPickMeta() {
      try {
        const j = await api.get('/api/picker/meta');
        this.pick.modes = j.modes || [];
        this.pick.markets = j.markets || [];
        this.pick.sel = (j.modes || []).map((m) => m.k);
        this.pick.general = { ...(j.general || {}), boards: [] };
        const adv = {};
        for (const m of this.pick.modes) {
          for (const [k, v] of Object.entries(m.params || {})) if (!(k in adv)) adv[k] = v;
        }
        this.pick.adv = adv;
      } catch (e) { this.pick.err = '选股模式加载失败'; }
    },

    pickQuery() {
      const p = new URLSearchParams();
      p.set('modes', this.pick.sel.join(','));
      for (const [k, v] of Object.entries(this.pick.general)) {
        if (v === '' || v === null || v === undefined) continue;
        if (Array.isArray(v)) { if (v.length) p.set(k, v.join(',')); continue; }
        p.set(k, v);
      }
      // 参数覆盖：只发和默认值不同的，避免把模式默认值重复带一遍
      for (const k of Object.keys(this.pick.adv)) {
        const v = this.pick.adv[k];
        if (v !== '' && v !== null && v !== undefined) p.set(k, v);
      }
      p.set('size', 50);
      p.set('probe', 40);
      return p.toString();
    },

    async loadPick() {
      if (!this.pick.sel.length) { this.pick.err = '至少选一个模式'; return; }
      if (this.pick.busy) return;
      this.pick.busy = true;
      this.pick.loading = true;
      this.pick.err = '';
      try {
        const j = await api.get('/api/picker?' + this.pickQuery());
        this.pick.stat = j;
        this.pick.rows = j.rows || [];
        for (const r of this.pick.rows) this.quotes[r.c] = { ...(this.quotes[r.c] || {}), ...r };
        this.order = this.pick.rows.map((r) => r.c);
        this.meta.total = this.pick.rows.length;
        this.view = 'pick';        // 筛选完直接看结果，否则改了条件还停在「全市场」会以为没生效
        this.resubQuotes();
        this.fillSpark();
      } catch (e) {
        this.pick.err = '选股接口失败：' + (e && e.message ? e.message : e);
      } finally {
        this.pick.loading = false;
        this.pick.busy = false;
      }
    },

    // A 层模式不需要分时就能判定，但表格要画缩略图 —— 单独批量补齐，不拖慢主接口
    async fillSpark() {
      const miss = this.pick.rows.filter((r) => !(r.spark && r.spark.length))
        .map((r) => r.c).slice(0, 60);
      if (!miss.length) return;
      try {
        const j = await api.get('/api/spark?codes=' + miss.join(','));
        const map = j.rows || {};
        for (const r of this.pick.rows) {
          const sp = map[r.c];
          if (sp && sp.length) {
            r.spark = sp;
            const q = this.quotes[r.c];
            if (q) q.spark = sp;
          }
        }
      } catch (e) { /* 缩略图拿不到就不画，不影响判定结论 */ }
    },

    toggleMode(k) {
      const i = this.pick.sel.indexOf(k);
      if (i >= 0) this.pick.sel.splice(i, 1); else this.pick.sel.push(k);
      this.loadPick();
    },
    toggleBoard(k) {
      const b = this.pick.general.boards || (this.pick.general.boards = []);
      const i = b.indexOf(k);
      if (i >= 0) b.splice(i, 1); else b.push(k);
      this.loadPick();
    },
    modeName(k) {
      const m = this.pick.modes.find((x) => x.k === k);
      return m ? m.icon + m.n : k;
    },
    sparkPts(arr) {
      if (!arr || arr.length < 2) return '';
      let lo = Math.min(...arr);
      let hi = Math.max(...arr);
      if (hi - lo < 1e-9) { hi = lo + 0.01; }
      const n = arr.length - 1;
      return arr.map((v, i) =>
        ((i / n) * 64).toFixed(1) + ',' + (16 - ((v - lo) / (hi - lo)) * 14).toFixed(1)).join(' ');
    },
    async watchAllPick() {
      // 走前端分组（watchGroups → persistWatch），和表格里的 ★ 是同一套逻辑，
      // 不然服务端自选池更新了、本地分组不动，两边会打架
      const g = this.watchGroups[this.watchActive] || (this.watchGroups[this.watchActive] = []);
      const add = this.pick.rows.map((r) => r.c).filter((c) => !this.isWatch(c));
      if (!add.length) { this.pick.err = '这些标的都已在自选池'; return; }
      g.push(...add);
      this.saveGroups();
      await this.persistWatch();
      if (this.view === 'watch') this.loadWatch();
      this.pick.err = '';
    },

    /* ── 交互 ─────────────────────────────────── */
    applyFilters() { this.meta.page = 1; this.fetchRank(); },
    toggleBoardF(k) {
      const i = this.filters.boards.indexOf(k);
      if (i >= 0) this.filters.boards.splice(i, 1); else this.filters.boards.push(k);
      this.applyFilters();
    },
    sortBy(k) {
      if (this.meta.sort === k) {
        this.meta.order = this.meta.order === 'desc' ? 'asc' : 'desc';
      } else { this.meta.sort = k; this.meta.order = 'desc'; }
      this.meta.page = 1;
      if (this.view === 'market') { this.fetchRank(); return; }
      // 自选/选股命中结果：数据已在前端，就地重排 order 即可。
      // 之前这里无脑走 fetchRank，而它对非 market 视图直接 return —— 点表头只换箭头不排数据
      this.sortCodes();
    },
    /* 按 quotes 最新值（即表格正在显示的值）重排当前 order，自选与命中结果共用 */
    sortCodes() {
      const k = this.meta.sort, desc = this.meta.order === 'desc';
      const arr = (this.order || []).slice().sort((a, b) => {
        const ra = this.quotes[a] || {}, rb = this.quotes[b] || {};
        if (k === 'code' || k === 'name') {
          const d = String(ra[k] || '').localeCompare(String(rb[k] || ''), 'zh');
          return desc ? -d : d;
        }
        const va = Number(ra[k]) || 0, vb = Number(rb[k]) || 0;
        return desc ? vb - va : va - vb;
      });
      this.order = arr;
    },
    goto(p) {
      const np = Math.max(1, Math.min(this.pages, p));
      if (np === this.meta.page) return;
      this.meta.page = np;
      this.fetchRank();
    },
    setView(v) {
      this.view = v;
      this.meta.page = 1;
      if (v === 'market') this.fetchRank();
      else if (v === 'watch') this.loadWatch();
      else if (!this.pick.stat) this.loadPick();   // 首次进入才自动筛一次，之后保持结果
    },
    setTab(t) {
      if (this.tab === t) return;
      this.tab = t;
      // v-show 隐藏的画布尺寸是 0，切回来必须重画，否则资金流曲线 / K 线是空白
      this.$nextTick(() => { this.drawFlow(); this.drawSpark(); this.drawK(); });
      if (t === 'pick' && this.view === 'pick' && !this.pick.stat) this.loadPick();
      if (t === 'etf' && !this.etf.cats.length) this.loadEtfMeta();
      if (t === 'etf' && !this.etf.rows.length && !this.etf.loading) this.loadEtf();
      if (t === 'wave' && !this.wave.tags.length) this.loadWaveMeta();
      if (t === 'wave' && !this.wave.idx.length && !this.wave.bloading) this.loadWaveBatch();
      if (t === 'market' && !this.wave.idx.length && !this.wave.bloading) this.loadWaveBatch();
    },
    switchBoard(k) {
      this.boardKind = k;
      if (!(this.boards[k] || []).length) this.loadBoards(k);
      this.loadHealth();
    },
    sortBoards() { /* 排序是 computed，改 boardSort 即可 */ },
    async openBoard(b) {
      this.boardSel = b;
      this.boardMembers = [];
      this.boardOpen = true;
      try {
        const j = await api.get(`/api/boards/${b.c}/stocks?page=1&size=50`);
        this.boardMembers = j.rows || [];
      } catch (e) { this.boardMembers = []; }
    },
    /* ── ETF ─────────────────────────────────────────── */
    async loadEtfMeta() {
      try {
        const j = await api.get('/api/etf/meta');
        this.etf.cats = j.cats || [];
        this.etf.na = j.na || [];
      } catch (e) { this.etf.cats = []; }
    },
    etfQuery() {
      const q = new URLSearchParams();
      const e = this.etf;
      if (e.cat) q.set('cats', e.cat);
      if (e.kw) q.set('kw', e.kw);
      if (e.amt_min !== null && e.amt_min !== '') q.set('amt_min', e.amt_min);
      if (e.pct_min !== null && e.pct_min !== '') q.set('pct_min', e.pct_min);
      q.set('sort', e.meta.sort); q.set('order', e.meta.order);
      q.set('page', e.meta.page); q.set('size', e.meta.size);
      return q.toString();
    },
    async loadEtf(force) {
      this.etf.loading = true;
      try {
        if (force) await api.get('/api/etf/meta?force=1').catch(() => {});
        const j = await api.get('/api/etf?' + this.etfQuery());
        this.etf.rows = j.rows || [];
        this.etf.meta = { ...this.etf.meta, ...(j.meta || {}) };
        if (!this.etf.cats.length) this.loadEtfMeta();
      } catch (e) {
        this.etf.rows = [];
      }
      this.etf.loading = false;
    },
    setEtfCat(k) { this.etf.cat = k; this.etf.meta.page = 1; this.loadEtf(); },
    sortEtf(k) {
      const m = this.etf.meta;
      if (m.sort === k) { m.order = m.order === 'desc' ? 'asc' : 'desc'; }
      else { m.sort = k; m.order = (k === 'code' || k === 'name') ? 'asc' : 'desc'; }
      this.etf.meta.page = 1;
      this.loadEtf();
    },
    gotoEtf(p) {
      const np = Math.max(1, Math.min(this.etfPages, p));
      if (np === this.etf.meta.page) return;
      this.etf.meta.page = np;
      this.loadEtf();
    },
    catName(k) {
      const c = (this.etf.cats || []).find((x) => x.k === k);
      return c ? c.n : (k || '—');
    },
    /* ETF 不在个股快照里（/api/quote 会 404），所以抽屉走 ETF 分支：
       快照字段来自 ETF 列表行，K 线照常拉，五档/资金流/逐笔在模板里按 isEtf 隐藏 */
    async openEtf(r) {
      if (!r || !r.c) return;
      this.selCode = r.c;
      this.sboard.panel = null; this.sboard.sel = ''; this.sboard.open = false;
      this.adv = null;
      this.detail = {
        c: r.c, n: r.n, p: r.p, pc: r.pc, isEtf: true,
        o: r.op, h: r.hi, l: r.lo, prev: r.pc0, to: r.to,
        amt: r.amt, cat: r.cat, sc: r.sc, ticks: [], a: [], b: [], flow: null,
      };
      this.drawerOpen = true;
      this.bookCode = r.c;
      this.bookName = r.n;
      this.histSeries = [];
      this.loadKline(r.c, 'day');
    },

    /* ── 波浪 ─────────────────────────────────────────── */
    async loadWaveMeta() {
      try {
        const j = await api.get('/api/wave/meta');
        this.wave.tags = j.tags || [];
        this.wave.note = j.note || '';
      } catch (e) { this.wave.tags = []; }
    },
    waveIdxCodes() {
      // 指数必须带市场前缀：'000001' 会被解析成平安银行，上证指数是 sh000001
      const syms = (this.idx || []).map((r) => r.sym || r.c).filter(Boolean);
      const out = syms.length ? syms
        : ['sh000001', 'sz399001', 'sz399006', 'sh000300', 'sh000905', 'sh000688', 'bj899050'];
      const watch = (this.watchGroups[this.watchActive] || []).filter(
        (c) => /^\d{6}$/.test(c)).slice(0, 8);
      return out.concat(watch);
    },
    async loadWaveBatch() {
      this.wave.bloading = true;
      const codes = this.waveIdxCodes();
      const q = new URLSearchParams({ codes: codes.join(',') });
      if (this.wave.pct !== null) q.set('pct', this.wave.pct);
      try {
        const j = await api.get('/api/wave_batch?' + q.toString());
        this.wave.idx = j.rows || [];
        this.wave.err = null;
      } catch (e) {
        // 不能静默吞：批量算失败时卡上写「无数据」，看不出是源坏了还是真没形态
        this.wave.idx = [];
        this.wave.err = String((e && e.message) || e);
      }
      this.wave.bloading = false;
    },
    setWavePct(p) {
      this.wave.pct = p;
      this.loadWaveBatch();
      if (this.kWave.on) this.loadKWave();
    },
    toggleWaveTag(k) {
      const i = this.wave.sel.indexOf(k);
      if (i >= 0) this.wave.sel.splice(i, 1); else this.wave.sel.push(k);
    },
    async loadWaveScan() {
      if (!this.wave.sel.length && this.wave.probe > 80) {
        // 不提示也能跑，只是慢——这里不拦，只把预算压回上限，避免一次拉 120 只日线
        this.wave.probe = 80;
      }
      this.wave.busy = true;
      const q = new URLSearchParams();
      if (this.wave.sel.length) q.set('tags', this.wave.sel.join(','));
      if (this.wave.pct !== null) q.set('pct', this.wave.pct);
      q.set('min_conf', this.wave.min_conf);
      q.set('probe', this.wave.probe);
      q.set('size', 50);
      if (this.wave.amt_min !== null && this.wave.amt_min !== '') q.set('amt_min', this.wave.amt_min);
      try {
        const j = await api.get('/api/wave/scan?' + q.toString());
        this.wave.rows = j.rows || [];
        this.wave.stat = j;
      } catch (e) {
        this.wave.rows = [];
        this.wave.stat = { pool: 0, probed: 0, probe_ok: 0, total: 0, ms: 0 };
      }
      this.wave.busy = false;
    },
    patName(p) {
      return ({ impulse_up: '上升推动浪', impulse_down: '下降推动浪',
                abc_down: 'A-B-C 调整（下）', abc_up: 'A-B-C 调整（上）' })[p] || p || '—';
    },
    /* 抽屉 K 线叠加：波浪按日线算，所以只在 day 周期启用 */
    async toggleKWave() {
      this.kWave.on = !this.kWave.on;
      if (this.kWave.on && !this.kWave.data) this.loadKWave();
      else this.$nextTick(() => this.drawK());
    },
    async loadKWave() {
      if (!this.selCode) return;
      const seq = ++this.kWave.seq;
      this.kWave.loading = true;
      const q = new URLSearchParams();
      if (this.wave.pct !== null) q.set('pct', this.wave.pct);
      // 摆动点索引是相对日线序列的，必须和 K 线当前根数对齐，否则标注会整体错位
      const n = (this.kBars || []).length || 260;
      q.set('n', String(Math.max(60, n)));
      try {
        const j = await api.get(`/api/wave/${this.selCode}?${q.toString()}`);
        if (seq !== this.kWave.seq) return;
        this.kWave.data = j;
      } catch (e) {
        if (seq !== this.kWave.seq) return;
        this.kWave.data = { ok: false, na: ['波浪计算失败'] };
      }
      this.kWave.loading = false;
      this.$nextTick(() => this.drawK());
    },

    // 从波浪扫描列表打开：自动叠加浪型标注（普通入口不受影响，仍是手动开关）
    async openDetailW(code) {
      if (!code) return;
      this.kPeriod = 'day';          // 波浪按日线算，切周期省得提示
      this.kWave.on = true;
      await this.openDetail(code);
    },

    isStockCode(c) { return /^\d{6}$/.test(c || ''); },

    // 单标的直达：选股 / ETF 面板输入 6 位代码回车即开抽屉
    jumpCode(c) {
      const s = (c || '').trim();
      if (!/^\d{6}$/.test(s)) {
        this.jumpErr = '请输入 6 位代码';
        clearTimeout(this._jumpT);
        this._jumpT = setTimeout(() => { this.jumpErr = ''; }, 2500);
        return;
      }
      this.jumpErr = '';
      this.openDetail(s);
    },

    // 振幅 =（最高-最低）/ 昨收；昨收缺失或为 0 时给 na，不用现价糊弄
    ampEtf(r) {
      if (!r || !r.pc0 || !r.hi || !r.lo) return 'na';
      return (((r.hi - r.lo) / r.pc0) * 100).toFixed(2);
    },

    async openDetail(code) {
      if (!code) return;
      this.selCode = code;
      // 换标的：板块面板复位（归属要按新票查）；建议在拿到详情后再算（指数没有建议）
      this.sboard.panel = null; this.sboard.sel = '';
      if (this.sboard.open) this.loadStockBoards();
      this.selIndex = this.rows.findIndex((r) => r.c === code);
      this.drawerOpen = true;
      this.bookCode = code;
      try {
        const j = await api.get('/api/quote/' + code);
        this.detail = j;
        this.bookName = j.n;
        this.book = j;
        // 指数只有快照：没有逐笔、资金流与操作建议，K 线/浪型照常出
        if (!j.index) {
          if (j.ticks && j.ticks.length) this.ticksMap[code] = j.ticks;
          this.wsSend({ op: 'sub', ch: 'tick', codes: [code] });
          this.loadAdvice();
          this.loadHistory(code);
        } else {
          this.adv = null;
          this.histSeries = [];
        }
        this.loadKline(code, this.kPeriod);
        this.kWave.data = null;
        if (this.kWave.on) this.loadKWave();
      } catch (e) {
        this.detail = null;
      }
    },
    async toggleSBoard() {
      this.sboard.open = !this.sboard.open;
      if (this.sboard.open) this.loadStockBoards();
    },
    async loadStockBoards() {
      if (!this.selCode) return;
      this.sboard.loading = true;
      try {
        const j = await api.get('/api/stock_boards/' + this.selCode);
        this.sboard.boards = j.boards || [];
        this.sboard.idx = j.index;
        if (!this.sboard.sel && this.sboard.boards.length) this.selBoard(this.sboard.boards[0].c);
      } catch (e) { /* 归属失败不打断抽屉 */ }
      this.sboard.loading = false;
    },
    async selBoard(bk) {
      this.sboard.sel = bk; this.sboard.panel = null; this.sboard.ploading = true;
      try {
        this.sboard.panel = await api.get('/api/board_panel/' + encodeURIComponent(bk));
      } catch (e) { this.sboard.panel = { ok: false, error: '面板加载失败' }; }
      this.sboard.ploading = false;
    },
    async loadAdvice() {
      if (!this.selCode) return;
      this.advLoading = true; this.adv = null;
      try { this.adv = await api.get('/api/advice/' + this.selCode); }
      catch (e) { this.adv = { ok: false, error: '建议生成失败' }; }
      this.advLoading = false;
    },
    async loadHistory(code) {
      this.histSeries = [];
      try {
        const j = await api.get(`/api/history/quote/${code}?minutes=240`);
        this.histSeries = j.rows || [];
      } catch (e) { /* 无库时静默 */ }
      this.$nextTick(() => this.drawSpark());
    },
    drawSpark() {
      const cv = this.$refs.sparkc;
      if (!cv || !cv.getContext) return;
      if (typeof cv.clientWidth === 'undefined') return;
      const dpr = window.devicePixelRatio || 1;
      const w = cv.clientWidth || 400;
      const h = cv.clientHeight || 88;
      if (cv.width !== w * dpr) { cv.width = w * dpr; cv.height = h * dpr; }
      const ctx = cv.getContext('2d');
      if (!ctx) return;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, w, h);

      const css = getComputedStyle(document.documentElement);
      const cUp = css.getPropertyValue('--up').trim() || '#ff4d4f';
      const cDn = css.getPropertyValue('--down').trim() || '#22b06b';
      const cLine = css.getPropertyValue('--line').trim() || '#242c38';
      const pts = this.histSeries || [];

      if (pts.length < 2) {
        ctx.strokeStyle = cLine; ctx.setLineDash([4, 4]);
        ctx.beginPath(); ctx.moveTo(0, h / 2); ctx.lineTo(w, h / 2); ctx.stroke();
        ctx.setLineDash([]);
        return;
      }
      const ys = pts.map((p) => p.p);
      const hi = Math.max(...ys);
      const lo = Math.min(...ys);
      const span = Math.max(0.01, hi - lo);
      const x = (i) => (i / (pts.length - 1)) * (w - 4) + 2;
      const y = (v) => h - 6 - ((v - lo) / span) * (h - 12);
      const color = ys[ys.length - 1] >= ys[0] ? cUp : cDn;

      ctx.beginPath();
      ctx.moveTo(x(0), y(ys[0]));
      for (let i = 1; i < ys.length; i++) ctx.lineTo(x(i), y(ys[i]));
      ctx.strokeStyle = color; ctx.lineWidth = 1.4; ctx.stroke();

      ctx.lineTo(x(ys.length - 1), h); ctx.lineTo(x(0), h); ctx.closePath();
      ctx.fillStyle = color; ctx.globalAlpha = 0.10; ctx.fill(); ctx.globalAlpha = 1;

      ctx.fillStyle = color;
      ctx.beginPath(); ctx.arc(x(ys.length - 1), y(ys[ys.length - 1]), 2.4, 0, Math.PI * 2); ctx.fill();
    },
    /* ── K 线 ─────────────────────────────────────── */
    async loadKline(code, period) {
      const seq = ++this.kSeq;
      this.kTip = null;
      try {
        const j = await api.get(`/api/kline/${code}?period=${period}`);
        if (seq !== this.kSeq) return;            // 快速切周期时丢掉旧响应
        this.kBars = (j.bars || []).map((b) => ({
          t: b.t, o: b.o, c: b.c, h: b.h, l: b.l, v: b.v, amt: b.amt,
        }));
        this.kZoom = 0; this.kOff = 0;        // 换周期/换标的：视角复位
        this.kMeta = { n: j.n, src: j.src, name: j.name, err: j.err };
      } catch (e) {
        if (seq !== this.kSeq) return;
        this.kBars = []; this.kMeta = { err: String(e && e.message || e) };
      }
      this.$nextTick(() => this.drawK());
    },
    setPeriod(p) {
      if (this.kPeriod === p) return;
      this.kPeriod = p;
      if (this.detail) this.loadKline(this.detail.c, p);
      if (this.kWave.on && p === 'day') this.loadKWave();
    },
    // K 线来源标签：降级链 东财 → 腾讯 → 新浪（新浪的周/月线由日线聚合，单独标注）
    srcName(s) {
      return ({ em: '东财', tx: '腾讯', sina: '新浪',
        'sina+resample': '新浪（日线聚合）' })[s] || '—';
    },
    kVolFmt(v) {
      const n = v || 0;
      if (n >= 1e6) return (n / 1e6).toFixed(1) + '万';
      if (n >= 1e4) return (n / 1e4).toFixed(1) + '万';
      return String(Math.round(n));
    },
    ma(arr, i, n) {
      if (i + 1 < n) return null;
      let s = 0;
      for (let k = i - n + 1; k <= i; k++) s += arr[k].c;
      return s / n;
    },
    /* 可见窗口：kZoom = 显示多少根（0=自动），kOff = 距最新一根往左退了几根 */
    kVis() {
      const n = (this.kBars || []).length;
      const auto = (this.kPeriod === 'day' || this.kPeriod === 'week'
        || this.kPeriod === 'month') ? 120 : 160;
      const view = Math.max(K_MIN_VIEW, Math.min(n, this.kZoom || Math.min(n, auto)));
      const maxOff = Math.max(0, n - view);
      const off = Math.max(0, Math.min(maxOff, this.kOff || 0));
      const i1 = n - 1 - off;
      const i0 = Math.max(0, i1 - view + 1);
      return { i0, i1, n: n ? i1 - i0 + 1 : 0 };
    },

    /* 压力/支撑 + 筹码密集。全部只用**当前可见区间**的 K 线算——
     * 能看到多少就按多少算，跟手动画线工具的习惯一致；缩小时自然会出现更多级别。
     *
     * 压力/支撑：摆动高低点（左右各 2 根都不超过它）按价格聚类（0.6% 内算同一价位），
     *            打分 = 触碰次数 × 10 + 最近性 × 6，取现价上方/下方各前 3 条。
     *            这只是"这些价位被反复试探过"的客观陈述，不是预测。
     * 筹码密集：成交量按价格分箱（30 箱），取占比最高的 1~2 箱（≥6% 才画）。
     */
    kLevels() {
      const v = this.kVis();
      const bars = (this.kBars || []).slice(v.i0, v.i1 + 1);
      const empty = { res: [], sup: [], dense: [] };
      if (bars.length < 12) return empty;
      const price = (this.detail && this.detail.p) || bars[bars.length - 1].c;
      if (!(price > 0)) return empty;

      const k = 2;
      const hiPts = [], loPts = [];
      for (let i = k; i < bars.length - k; i++) {
        let isH = true, isL = true;
        for (let j = i - k; j <= i + k; j++) {
          if (j === i) continue;
          if (bars[j].h >= bars[i].h) isH = false;
          if (bars[j].l <= bars[i].l) isL = false;
        }
        if (isH) hiPts.push({ p: bars[i].h, i });
        if (isL) loPts.push({ p: bars[i].l, i });
      }

      const tol = price * 0.006;
      const cluster = (pts) => {
        pts.sort((a, b) => a.p - b.p);
        const out = [];
        for (const pt of pts) {
          const last = out[out.length - 1];
          if (last && pt.p - last.p <= tol) {
            last.p = (last.p * last.n + pt.p) / (last.n + 1);
            last.n += 1;
            last.last = pt.i;
          } else {
            out.push({ p: pt.p, n: 1, last: pt.i });
          }
        }
        return out;
      };
      const score = (c) => c.n * 10 + (c.last / Math.max(1, bars.length)) * 6;

      const res = cluster(hiPts).filter((c) => c.p > price * 1.002)
        .sort((a, b) => score(b) - score(a)).slice(0, 3);
      const sup = cluster(loPts).filter((c) => c.p < price * 0.998)
        .sort((a, b) => score(b) - score(a)).slice(0, 3);

      // 筹码密集：成交量按价格分箱
      let lo = Infinity, hi = -Infinity;
      for (const b of bars) {
        if (b.l < lo) lo = b.l;
        if (b.h > hi) hi = b.h;
      }
      const BINS = 30;
      const bins = new Array(BINS).fill(0);
      const span = hi - lo;
      if (span > 0) {
        for (const b of bars) {
          const mid = (b.h + b.l + b.c) / 3;
          const idx = Math.min(BINS - 1, Math.max(0, Math.floor((mid - lo) / span * BINS)));
          bins[idx] += b.v || 0;
        }
      }
      const totalV = bins.reduce((a, b) => a + b, 0) || 1;
      const dense = bins.map((val, i) => ({
        v: val, p: lo + (i + 0.5) / BINS * span, share: val / totalV,
      })).sort((a, b) => b.v - a.v).slice(0, 2).filter((d) => d.share > 0.06);

      return { res, sup, dense };
    },

    drawK() {
      const cv = this.$refs.kc;
      if (!cv || !cv.getContext || typeof cv.clientWidth === 'undefined') return;
      const dpr = window.devicePixelRatio || 1;
      const w = cv.clientWidth || 400;
      const h = cv.clientHeight || 236;
      if (cv.width !== Math.round(w * dpr)) { cv.width = w * dpr; cv.height = h * dpr; }
      const ctx = cv.getContext('2d');
      if (!ctx) return;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, w, h);

      const css = getComputedStyle(document.documentElement);
      const cUp = css.getPropertyValue('--up').trim() || '#c62828';
      const cDn = css.getPropertyValue('--down').trim() || '#2e7d32';
      const cLine = css.getPropertyValue('--line-soft').trim() || '#e8edf2';
      const cTx = css.getPropertyValue('--text-2').trim() || '#666';
      const cWarn = css.getPropertyValue('--warn').trim() || '#b45309';
      const cAcc = css.getPropertyValue('--accent').trim() || '#1d4ed8';
      const all = this.kBars || [];
      const padR = 42, padL = 4, padT = 6, volH = 46, gap = 10;
      const ph = h - padT - volH - gap - 4;          // 主图高度

      if (all.length < 2) {
        ctx.strokeStyle = cLine; ctx.setLineDash([4, 4]);
        ctx.beginPath(); ctx.moveTo(0, h / 2); ctx.lineTo(w, h / 2); ctx.stroke();
        ctx.setLineDash([]);
        ctx.fillStyle = cTx; ctx.font = '11px system-ui,sans-serif';
        ctx.fillText('无 K 线数据', 8, h / 2 - 8);
        this.kBox = null;
        return;
      }

      const v = this.kVis();
      const iw = w - padL - padR;
      const step = iw / v.n;
      let hi = -Infinity, lo = Infinity, vmax = 0;
      for (let i = v.i0; i <= v.i1; i++) {
        const b = all[i];
        if (b.h > hi) hi = b.h;
        if (b.l < lo) lo = b.l;
        if (b.v > vmax) vmax = b.v;
      }
      const span = Math.max(1e-6, hi - lo);
      const top = hi + span * 0.04, bot = lo - span * 0.04;
      const y = (val) => padT + (top - val) / (top - bot) * ph;
      const bw = Math.max(1, Math.min(9, step * 0.62));
      const x = (i) => padL + step * (i - v.i0 + 0.5);
      const vy = (val) => h - 4 - (vmax > 0 ? (val / vmax) * volH : 0);

      // 供命中测试复用（kMove / 拖拽都要用同一套几何）
      this.kBox = { i0: v.i0, i1: v.i1, step, padL, padR, top, bot, iw };

      // 网格 + 右侧价位
      ctx.font = '10px ui-monospace,Menlo,monospace';
      ctx.textAlign = 'left';
      for (let i = 0; i <= 3; i++) {
        const pv = top - (top - bot) * (i / 3);
        const py = y(pv);
        ctx.strokeStyle = cLine; ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(padL, py); ctx.lineTo(w - padR, py); ctx.stroke();
        ctx.fillStyle = cTx; ctx.fillText(pv.toFixed(2), w - padR + 4, py + 3);
      }

      // 筹码密集区（画在蜡烛底下，避免盖住实体）
      const lv = this.kLevels();
      for (const d of lv.dense) {
        const y0 = y(d.p + span * 0.012), y1 = y(d.p - span * 0.012);
        ctx.fillStyle = cAcc; ctx.globalAlpha = 0.1;
        ctx.fillRect(padL, Math.min(y0, y1), iw, Math.abs(y1 - y0) + 1);
        ctx.globalAlpha = 1;
      }

      // 蜡烛
      for (let i = v.i0; i <= v.i1; i++) {
        const b = all[i];
        const up = b.c >= b.o;
        const col = up ? cUp : cDn;
        ctx.strokeStyle = col; ctx.fillStyle = col; ctx.lineWidth = 1;
        const cx = x(i);
        ctx.beginPath(); ctx.moveTo(cx, y(b.h)); ctx.lineTo(cx, y(b.l)); ctx.stroke();
        const yo = y(b.o), yc = y(b.c);
        const y1 = Math.min(yo, yc), hh = Math.max(1, Math.abs(yc - yo));
        // A 股习惯：阳线红空心、阴线绿实心
        if (up) {
          ctx.strokeRect(cx - bw / 2 + 0.5, y1 + 0.5, bw - 1, hh - 1);
        } else {
          ctx.fillRect(cx - bw / 2, y1, bw, hh);
        }

        // 量柱
        if (b.v > 0) {
          ctx.globalAlpha = 0.55;
          ctx.fillStyle = col;
          ctx.fillRect(cx - bw / 2, vy(b.v), bw, h - 4 - vy(b.v));
          ctx.globalAlpha = 1;
        }
      }

      // 均线（在完整序列上递推，否则可见段最左边几根没有均线）
      const lines = [[5, '#e8a33d'], [10, '#4d8dff'], [20, '#b46bd8']];
      for (const [n, col] of lines) {
        if (all.length < n) continue;
        ctx.beginPath();
        let started = false;
        for (let i = v.i0; i <= v.i1; i++) {
          const m = this.ma(all, i, n);
          if (m == null) continue;
          if (!started) { ctx.moveTo(x(i), y(m)); started = true; }
          else ctx.lineTo(x(i), y(m));
        }
        ctx.strokeStyle = col; ctx.lineWidth = 1.1; ctx.stroke();
      }

      // 压力/支撑：虚线 + 左侧「R1/S1 + 价格 + 触碰次数」
      ctx.setLineDash([4, 3]);
      ctx.lineWidth = 1;
      const drawLv = (list, prefix, col) => {
        list.forEach((c, idx) => {
          const py = y(c.p);
          if (py < padT - 2 || py > padT + ph + 2) return;
          ctx.strokeStyle = col;
          ctx.beginPath(); ctx.moveTo(padL, py); ctx.lineTo(w - padR, py); ctx.stroke();
          const label = prefix + (idx + 1) + ' ' + c.p.toFixed(2) + ' ·' + c.n + '次';
          ctx.font = '10px ui-monospace,Menlo,monospace';
          const tw = ctx.measureText(label).width;
          ctx.fillStyle = col; ctx.globalAlpha = 0.16;
          ctx.fillRect(padL + 1, py - 11, tw + 6, 12);
          ctx.globalAlpha = 1;
          ctx.fillStyle = col;
          ctx.fillText(label, padL + 4, py - 2);
        });
      };
      drawLv(lv.res, 'R', cWarn);      // 压力：橙
      drawLv(lv.sup, 'S', cAcc);       // 支撑：蓝
      ctx.setLineDash([]);

      // 波浪叠加：ZigZag 连线 + 浪型标注 + 斐波那契位 + 失效位
      const wv = this.kWave.on && this.kWave.data && this.kWave.data.ok ? this.kWave.data : null;
      if (wv && this.kPeriod === 'day') {
        const piv = (wv.pivots || []).filter((p) => p.i >= v.i0 - 1 && p.i <= v.i1 + 1);
        // ZigZag 折线（实线：不透明、加粗、圆角拐点，避免细线+半透明在屏上看起来像虚线）
        if (piv.length >= 2) {
          ctx.setLineDash([]); ctx.lineWidth = 1.8;
          ctx.lineJoin = 'round'; ctx.lineCap = 'round';
          ctx.strokeStyle = cTx; ctx.globalAlpha = 1;
          ctx.beginPath();
          piv.forEach((p, i) => { i ? ctx.lineTo(x(p.i), y(p.p)) : ctx.moveTo(x(p.i), y(p.p)); });
          ctx.stroke();
          ctx.globalAlpha = 1;
        }
        // 参与拟合的浪型点：圆点 + 标签（起点/1..5 或 起点/A/B/C）
        for (const w of (wv.waves || [])) {
          if (w.i < v.i0 - 1 || w.i > v.i1 + 1) continue;
          const px = x(w.i), py = y(w.p);
          ctx.beginPath(); ctx.arc(px, py, 3.2, 0, Math.PI * 2);
          ctx.fillStyle = cAcc; ctx.fill();
          ctx.font = '10px system-ui,sans-serif';
          const tw = ctx.measureText(w.label).width;
          ctx.fillStyle = cAcc; ctx.globalAlpha = 0.18;
          ctx.fillRect(px - tw / 2 - 2, py + (w.k === 'H' ? -14 : 4), tw + 4, 12);
          ctx.globalAlpha = 1;
          ctx.fillStyle = cAcc;
          ctx.fillText(w.label, px - tw / 2, py + (w.k === 'H' ? -5 : 13));
        }
        // 斐波那契回撤/反弹位（细虚线，标在右侧）
        ctx.setLineDash([2, 3]); ctx.lineWidth = 1;
        for (const f of (wv.fib.retr || [])) {
          const py = y(f.p);
          if (py < padT || py > padT + ph) continue;
          ctx.strokeStyle = cAcc; ctx.globalAlpha = 0.5;
          ctx.beginPath(); ctx.moveTo(padL, py); ctx.lineTo(w - padR, py); ctx.stroke();
          ctx.globalAlpha = 1;
          const lb = f.r.toFixed(3).replace(/0+$/, '').replace(/\.$/, '');
          ctx.fillStyle = cAcc;
          ctx.font = '9px ui-monospace,Menlo,monospace';
          ctx.fillText(lb, w - padR + 4, py + 3);
        }
        // 失效位：结构被破坏的价格，用告警色粗虚线
        const inv = wv.stage && wv.stage.invalid;
        if (inv) {
          const py = y(inv);
          if (py >= padT && py <= padT + ph) {
            ctx.setLineDash([6, 4]); ctx.lineWidth = 1.6;
            ctx.strokeStyle = cWarn;
            ctx.beginPath(); ctx.moveTo(padL, py); ctx.lineTo(w - padR, py); ctx.stroke();
            ctx.setLineDash([]); ctx.lineWidth = 1;
            ctx.fillStyle = cWarn;
            ctx.font = '10px system-ui,sans-serif';
            ctx.fillText('失效 ' + inv.toFixed(2), padL + 4, py - 3);
          }
        }
      }

      // 成交量基线
      ctx.strokeStyle = cLine;
      ctx.beginPath(); ctx.moveTo(padL, h - 4); ctx.lineTo(w - padR, h - 4); ctx.stroke();
      ctx.fillStyle = cTx;
      ctx.font = '10px ui-monospace,Menlo,monospace';
      ctx.fillText('量', padL + 1, h - volH - gap + 10);
      // 可见区间角标：缩放过之后必须让用户知道自己在看哪一段
      if (v.n < all.length) {
        ctx.fillStyle = cTx;
        const tag = v.i0 + 1 + '-' + (v.i1 + 1) + ' / ' + all.length;
        ctx.fillText(tag, w - padR - ctx.measureText(tag).width - 2, padT + 9);
      }
    },

    /* 滚轮缩放：以光标所在位置为锚点，缩放后那一根还停在原处 */
    kWheel(e) {
      const n = (this.kBars || []).length;
      if (n < K_MIN_VIEW) return;
      const cur = this.kVis();
      const view = cur.n;
      const next = Math.round(view * (e.deltaY > 0 ? 1.25 : 0.8));
      const nv = Math.max(K_MIN_VIEW, Math.min(n, next));
      if (nv === view) return;
      let ratio = 0.5;
      const box = this.kBox;
      const cv = this.$refs.kc;
      if (box && cv && cv.getBoundingClientRect) {
        const rect = cv.getBoundingClientRect();
        ratio = Math.max(0, Math.min(1, (e.clientX - rect.left - box.padL) / Math.max(1, box.iw)));
      }
      const anchor = cur.i0 + ratio * Math.max(0, view - 1);
      let i1 = Math.round(anchor + (1 - ratio) * (nv - 1));
      i1 = Math.max(nv - 1, Math.min(n - 1, i1));
      this.kZoom = nv;
      this.kOff = (n - 1) - i1;
      this.drawK();
    },
    kZoomStep(dir) {
      // dir>0 放大（看得更细、根数更少）
      const n = (this.kBars || []).length;
      if (n < K_MIN_VIEW) return;
      const view = this.kVis().n;
      const nv = Math.max(K_MIN_VIEW, Math.min(n, Math.round(view * (dir > 0 ? 0.75 : 1.33))));
      if (nv === view) return;
      this.kZoom = nv;
      this.kOff = 0;                 // 按钮缩放贴回最新一段，和滚轮的锚点缩放互补
      this.drawK();
    },
    kFull() {
      this.kZoom = (this.kBars || []).length || 0;
      this.kOff = 0;
      this.drawK();
    },
    kReset() {
      this.kZoom = 0;
      this.kOff = 0;
      this.drawK();
    },
    kDown(e) {
      if (!this.kBars.length || e.button !== 0) return;
      this.kDrag = { x: e.clientX, off: this.kOff || 0 };
      this.kTip = null;
      const cv = this.$refs.kc;
      if (cv && cv.style) cv.style.cursor = 'grabbing';
      if (cv && cv.setPointerCapture && e.pointerId != null) {
        try { cv.setPointerCapture(e.pointerId); } catch (err) { /* 部分环境不支持 */ }
      }
    },
    kDragMove(e) {
      if (!this.kDrag) return;
      const box = this.kBox;
      if (!box) return;
      const dx = e.clientX - this.kDrag.x;
      const shift = Math.round(dx / Math.max(1, box.step));
      if (!shift) return;
      const maxOff = Math.max(0, this.kBars.length - this.kVis().n);
      // 向右拖 = 看更早的一段，所以 kOff 增加
      this.kOff = Math.max(0, Math.min(maxOff, this.kDrag.off + shift));
      this.drawK();
    },
    kUp() {
      if (!this.kDrag) return;
      this.kDrag = null;
      const cv = this.$refs.kc;
      if (cv && cv.style) cv.style.cursor = 'crosshair';
    },
    kMove(e) {
      // 拖拽平移时不更新十字读数：拖动过程中左右读数会连续跳变，很难看
      if (this.kDrag) { this.kTip = null; return; }
      const box = this.kBox;
      if (!box || !this.kBars.length) return;
      const cv = this.$refs.kc;
      const rect = cv.getBoundingClientRect();
      const i = box.i0 + Math.floor((e.clientX - rect.left - box.padL) / Math.max(1, box.step));
      if (i < box.i0 || i > box.i1 || !this.kBars[i]) { this.kTip = null; return; }
      const b = this.kBars[i];
      this.kTip = { ...b, ma5: this.ma(this.kBars, i, 5), ma10: this.ma(this.kBars, i, 10) };
    },
    kLeave() { this.kTip = null; },

    addGroup() {
      const name = (window.prompt('新分组名称', '') || '').trim();
      if (!name) return;
      if (this.watchGroups[name]) { this.watchActive = name; return; }
      this.watchGroups[name] = [];
      this.watchActive = name;
      this.saveGroups();
      this.loadWatch();
    },
    delGroup() {
      const g = this.watchActive;
      if (Object.keys(this.watchGroups).length <= 1) return;
      if (!window.confirm(`删除分组「${g}」？组内 ${(this.watchGroups[g] || []).length} 只股票会移出自选。`)) return;
      delete this.watchGroups[g];
      this.watchActive = Object.keys(this.watchGroups)[0];
      this.saveGroups();
      this.persistWatch();
      this.loadWatch();
    },
    saveGroups() {
      localStorage.setItem('board.groups', JSON.stringify(this.watchGroups));
    },
    async persistWatch() {
      const all = [...new Set(Object.values(this.watchGroups).flat())];
      try { await api.post('/api/watchlist', { codes: all }); } catch (e) { /* ignore */ }
    },
    closeDrawer() { this.drawerOpen = false; this.selCode = ''; },

    /* ── 龙虎榜（T+1）───────────────────────────── */
    openLhb() {
      this.lhbOpen = true;
      if (!this.lhbRows.length) this.loadLhb();
    },
    async loadLhb() {
      this.lhbMsg = '加载中…';
      const q = new URLSearchParams({ limit: '200' });
      if (this.lhbDate) q.set('date', this.lhbDate);
      try {
        const j = await api.get('/api/lhb?' + q.toString());
        this.lhbRows = j.rows || [];
        this.lhbMeta = { source: j.source, total: j.total };
        this.lhbMsg = this.lhbRows.length ? '' : (j.error || '该日没有龙虎榜数据');
      } catch (e) {
        this.lhbRows = [];
        this.lhbMsg = '龙虎榜接口失败：' + e.message;
      }
    },
    isWatch(code) {
      if (!code) return false;
      return Object.values(this.watchGroups).some((g) => g.includes(code));
    },
    async toggleWatch(code) {
      if (!code) return;
      const g = this.watchGroups[this.watchActive] || (this.watchGroups[this.watchActive] = []);
      const i = g.indexOf(code);
      if (i >= 0) g.splice(i, 1); else g.push(code);
      this.saveGroups();
      await this.persistWatch();
      if (this.view === 'watch') this.loadWatch();
    },
    onGroupChange() { this.loadWatch(); },
    toggleTheme() {
      this.theme = this.theme === 'dark' ? 'light' : 'dark';
      document.documentElement.setAttribute('data-theme', this.theme);
      localStorage.setItem('board.theme', this.theme);
      this.drawFlow();
    },
    async toggleThrift() {
      this.thrift = !this.thrift;
      try { await api.post('/api/mode', { thrift: this.thrift }); } catch (e) { /* ignore */ }
    },

    /* ── 声音 ─────────────────────────────────── */
    beep(kind) {
      if (!this.sound) return;
      try {
        const Ctx = window.AudioContext || window.webkitAudioContext;
        if (!this._ctx) this._ctx = new Ctx();
        const ctx = this._ctx;
        const o = ctx.createOscillator();
        const gnode = ctx.createGain();
        o.type = 'sine';
        o.frequency.value = kind === 'limit_open' ? 980 : (kind === 'speed' ? 720 : 560);
        gnode.gain.setValueAtTime(0.0001, ctx.currentTime);
        gnode.gain.exponentialRampToValueAtTime(0.18, ctx.currentTime + 0.02);
        gnode.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.22);
        o.connect(gnode).connect(ctx.destination);
        o.start(); o.stop(ctx.currentTime + 0.24);
      } catch (e) { /* 浏览器未授权音频，忽略 */ }
    },

    /* ── Canvas：大盘资金流 ───────────────────── */
    drawFlow() {
      const cv = this.$refs.flowc;
      if (!cv) return;
      const dpr = window.devicePixelRatio || 1;
      const w = cv.clientWidth || 320;
      const h = cv.clientHeight || 112;
      if (cv.width !== w * dpr || cv.height !== h * dpr) {
        cv.width = w * dpr; cv.height = h * dpr;
      }
      const ctx = cv.getContext('2d');
      if (!ctx) return;                      // 无 canvas 上下文（老浏览器/测试环境）直接跳过
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, w, h);

      const pts = this.flow.market || [];
      const css = getComputedStyle(document.documentElement);
      const cUp = css.getPropertyValue('--up').trim() || '#ff4d4f';
      const cDn = css.getPropertyValue('--down').trim() || '#22b06b';
      const cLine = css.getPropertyValue('--line').trim() || '#242c38';

      if (pts.length < 2) {
        ctx.strokeStyle = cLine; ctx.setLineDash([4, 4]);
        ctx.beginPath(); ctx.moveTo(0, h / 2); ctx.lineTo(w, h / 2); ctx.stroke();
        ctx.setLineDash([]);
        return;
      }

      const vals = pts.map((p) => p.main);
      const mx = Math.max(1, ...vals.map(Math.abs));
      const y = (v) => h / 2 - (v / mx) * (h / 2 - 6);
      const x = (i) => (i / (pts.length - 1)) * (w - 2) + 1;

      // 零轴
      ctx.strokeStyle = cLine;
      ctx.beginPath(); ctx.moveTo(0, h / 2); ctx.lineTo(w, h / 2); ctx.stroke();

      // 分正负段着色：不画成一条变色线，而是按区间填色，更接近行情软件习惯
      for (const cut of [{ s: 0, c: cUp }, { s: 1, c: cDn }]) {
        ctx.beginPath();
        let started = false;
        for (let i = 0; i < pts.length; i++) {
          const v = vals[i];
          const inSeg = cut.s === 0 ? v >= 0 : v < 0;
          if (!inSeg) { started = false; continue; }
          if (!started) { ctx.moveTo(x(i), y(v)); started = true; }
          else ctx.lineTo(x(i), y(v));
        }
        ctx.strokeStyle = cut.c; ctx.lineWidth = 1.6; ctx.stroke();
      }

      // 末点
      const last = vals[vals.length - 1];
      ctx.fillStyle = last >= 0 ? cUp : cDn;
      ctx.beginPath(); ctx.arc(x(vals.length - 1), y(last), 2.6, 0, Math.PI * 2); ctx.fill();
    },

    /* ── WebSocket ────────────────────────────── */
    wsSend(obj) {
      if (this.ws && this.ws.readyState === 1) this.ws.send(JSON.stringify(obj));
    },
    connect() {
      const proto = location.protocol === 'https:' ? 'wss' : 'ws';
      this.wsState = 'connecting';
      let ws;
      try { ws = new WebSocket(`${proto}://${location.host}/ws`); }
      catch (e) { this.startPolling(); return; }
      this.ws = ws;

      ws.onopen = () => {
        this.wsState = 'live';
        this.wsTry = 0;
        this.stopPolling();
        this.subscribeAll();
      };
      ws.onmessage = (ev) => {
        this.lastFrame = Date.now();
        let m;
        try { m = JSON.parse(ev.data); } catch (e) { return; }
        this.onFrame(m);
      };
      ws.onclose = () => {
        this.wsState = 'down';
        this.wsTry += 1;
        if (this.wsTry <= 3) {
          setTimeout(() => this.connect(), 800 * this.wsTry);
        } else {
          // 三次都连不上：回落轮询，功能不中断（方案 §7.4 降级条款）
          this.startPolling();
        }
      };
      ws.onerror = () => { try { ws.close(); } catch (e) { /* ignore */ } };
    },
    subscribeAll() {
      const idxCodes = this.idx.map((x) => x.c);
      const pageCodes = this.order.slice();
      const tickCodes = [...new Set([this.bookCode, ...(this.watchGroups[this.watchActive] || [])])]
        .filter(Boolean).slice(0, 20);
      this.wsSend({
        op: 'batch',
        ops: [
          { op: 'sub', ch: 'index', codes: idxCodes },
          { op: 'sub', ch: 'quote', codes: pageCodes },
          { op: 'sub', ch: 'breadth' },
          { op: 'sub', ch: 'board' },
          { op: 'sub', ch: 'flow' },
          { op: 'sub', ch: 'alert' },
          { op: 'sub', ch: 'tick', codes: tickCodes },
        ],
      });
      this.subscribed = new Set(pageCodes);
    },
    resubQuotes() {
      if (!this.ws || this.ws.readyState !== 1) return;
      const next = new Set(this.order);
      const old = this.subscribed || new Set();
      const add = [...next].filter((c) => !old.has(c));
      const del = [...old].filter((c) => !next.has(c));
      // 翻页/换筛选时必须退订旧代码，否则订阅集无限膨胀（方案 §9.4）
      if (del.length) this.wsSend({ op: 'unsub', ch: 'quote', codes: del });
      if (add.length) this.wsSend({ op: 'sub', ch: 'quote', codes: add });
      this.subscribed = next;
      if (this.wsState === 'live' && add.length) {
        // 新进视口的行先补一次全量，避免等到下一轮 diff
        api.get('/api/quotes?codes=' + add.slice(0, 200).join(','))
          .then((j) => { for (const r of (j.rows || [])) this.quotes[r.c] = { ...(this.quotes[r.c] || {}), ...r }; })
          .catch(() => {});
      }
    },
    onFrame(m) {
      switch (m.t) {
        case 'hello':
          this.session = m.session;
          this.lagSec = m.lag_sec || 0;
          break;
        case 'hb':
          break;
        case 'index': {
          const list = Array.isArray(m.d) ? m.d : Object.values(m.d || {});
          if (m.full) this.idx = list;
          else {
            const map = new Map(this.idx.map((x) => [x.c, x]));
            for (const it of list) {
              const cur = map.get(it.c) || {};
              map.set(it.c, { ...cur, ...it });
            }
            const orderKeys = this.idx.map((x) => x.c);
            this.idx = orderKeys.map((c) => map.get(c)).filter(Boolean);
          }
          break;
        }
        case 'quote': {
          const list = Array.isArray(m.d) ? m.d : Object.values(m.d || {});
          for (const it of list) {
            if (!it || !it.c) continue;
            const cur = this.quotes[it.c];
            if (cur) Object.assign(cur, it);
            else this.quotes[it.c] = { ...it };
          }
          if (this.bookCode && this.quotes[this.bookCode]) {
            const q = this.quotes[this.bookCode];
            if (q.b && q.a) this.book = q;
          }
          break;
        }
        case 'breadth':
          this.breadth = { ...this.breadth, ...(m.d || {}) };
          break;
        case 'board': {
          const d = m.d || {};
          if (d.all) {
            const grouped = { i: [], c: [], r: [] };
            for (const b of d.all) (grouped[b.k] || grouped.i).push(b);
            this.boards = grouped;
          } else if (d.kind) {
            this.boards = { ...this.boards, [d.kind]: d.rows || [] };
          }
          break;
        }
        case 'flow': {
          const d = m.d || {};
          this.flow = {
            market: d.market || this.flow.market,
            stocks: d.stocks || this.flow.stocks,
            boards: d.boards || this.flow.boards,
          };
          if (d.src) this.flowMeta = { src: d.src, pool: d.pool || 0, label: d.label || '' };
          this.drawFlow();
          break;
        }
        case 'tick': {
          const d = m.d || {};
          for (const code of Object.keys(d)) {
            const rows = (d[code] && d[code].d) || [];
            const key = rows.length ? `${rows[0].t}|${rows[0].p}|${rows[0].v}` : '';
            const prevKey = this._tickKey && this._tickKey[code];
            if (key && key === prevKey) continue;      // 同一批不重复插
            this._tickKey = this._tickKey || {};
            this._tickKey[code] = key;
            const merged = [...rows, ...(this.ticksMap[code] || [])];
            const seen = new Set();
            const uniq = [];
            for (const t of merged) {
              const k = `${t.t}|${t.p}|${t.v}|${t.s}`;
              if (seen.has(k)) continue;
              seen.add(k);
              uniq.push(t);
            }
            uniq.sort((a, b) => (a.t < b.t ? -1 : a.t > b.t ? 1 : 0));
            this.ticksMap[code] = uniq.slice(-60);
          }
          break;
        }
        case 'alert': {
          const list = Array.isArray(m.d) ? m.d : [];
          if (m.full) this.alerts = list.slice(0, 200);
          else {
            this.alerts = [...list, ...this.alerts].slice(0, 200);
            for (const a of list) this.beep(a.kind);
          }
          break;
        }
        default:
          break;
      }
    },

    /* ── 轮询兜底 ─────────────────────────────── */
    startPolling() {
      if (this.pollTimer) return;
      this.wsState = 'polling';
      this.pollTick = 0;
      this.pollTimer = setInterval(() => {
        this.pollTick += 1;
        this.fetchRank();
        this.loadIndex();
        this.loadBreadth();
        if (this.pollTick % 3 === 0) {
          this.loadFlow();
          this.loadBoards(this.boardKind);
          this.loadAlerts();
          if (this.bookCode) this.openDetail(this.bookCode);
        }
      }, 3000);
    },
    stopPolling() {
      if (this.pollTimer) { clearInterval(this.pollTimer); this.pollTimer = null; }
      if (this.wsState === 'polling') this.wsState = 'live';
    },

    /* ── 快捷键 ───────────────────────────────── */
    onKey(e) {
      const tag = (e.target.tagName || '').toLowerCase();
      const typing = tag === 'input' || tag === 'select' || tag === 'textarea';
      if (e.key === 'Escape') { this.help = false; this.drawerOpen = false; this.boardOpen = false; this.lhbOpen = false; return; }
      if (typing) return;

      switch (e.key) {
        case '1': this.setTab('market'); break;
        case '2': this.setTab('pick'); break;
        case '3': this.setTab('etf'); break;
        case '4': this.setTab('wave'); break;
        case '/': e.preventDefault(); this.$refs.kw && this.$refs.kw.focus(); break;
        case '?': this.help = !this.help; break;
        case 'd': this.toggleTheme(); break;
        case 't': this.toggleThrift(); break;
        case 's': this.sound = !this.sound; break;
        case 'l': this.lhbOpen ? (this.lhbOpen = false) : this.openLhb(); break;
        case ']': this.goto(this.meta.page + 1); break;
        case '[': this.goto(this.meta.page - 1); break;
        case 'j': {
          const n = Math.min(this.rows.length - 1, (this.selIndex < 0 ? -1 : this.selIndex) + 1);
          if (this.rows[n]) { this.selIndex = n; this.selCode = this.rows[n].c; }
          break;
        }
        case 'k': {
          const n = Math.max(0, (this.selIndex < 0 ? 1 : this.selIndex) - 1);
          if (this.rows[n]) { this.selIndex = n; this.selCode = this.rows[n].c; }
          break;
        }
        case 'Enter':
          if (this.selCode) this.openDetail(this.selCode);
          break;
        default: break;
      }
    },
  },

  async mounted() {
    this.theme = localStorage.getItem('board.theme') || 'dark';
    document.documentElement.setAttribute('data-theme', this.theme);
    try {
      const saved = JSON.parse(localStorage.getItem('board.groups') || 'null');
      if (saved && typeof saved === 'object') this.watchGroups = saved;
    } catch (e) { /* ignore */ }
    if (!Object.keys(this.watchGroups).length) this.watchGroups = { '默认': [] };
    this.watchActive = Object.keys(this.watchGroups)[0];

    // 后端自选池作为初始值（与 /api/watchlist 一致，避免两边不一致）
    try {
      const w = await api.get('/api/watchlist');
      if (w.codes && w.codes.length && !(this.watchGroups['默认'] || []).length) {
        this.watchGroups['默认'] = w.codes;
      }
    } catch (e) { /* ignore */ }

    await Promise.all([
      this.loadIndex(), this.loadBreadth(), this.loadBoards('i'),
      this.loadFlow(), this.loadAlerts(), this.loadHealth(), this.loadPickMeta(),
    ]);
    await this.fetchRank();
    // 市场分析页常驻的大盘浪型卡：后台算，不阻塞首屏行情
    this.loadWaveBatch();
    this.connect();
    window.addEventListener('keydown', this.onKey);
    this.healthTimer = setInterval(() => this.loadHealth(), 10000);
    window.addEventListener('resize', () => { this.drawFlow(); this.drawSpark(); this.drawK(); });
    setTimeout(() => this.drawFlow(), 300);
  },

  beforeUnmount() {
    window.removeEventListener('keydown', this.onKey);
    clearInterval(this.healthTimer);
    this.stopPolling();
    if (this.ws) this.ws.close();
  },
}).mount('#app');
