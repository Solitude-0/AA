/* sortBy / sortCodes 回归：选股与自选子视图点表头必须真的重排数据。
 * 背景：sortBy 原来无脑走 fetchRank，而 fetchRank 对 view!=='market' 直接 return，
 * 导致命中结果视图点「涨幅↓」只换箭头不排数据。跑法：node tools/sort_check.js */
const fs = require('fs');
const path = require('path');
const errors = [];
const ok = (cond, msg) => { if (!cond) errors.push(msg); };

const src = fs.readFileSync(path.join(__dirname, '..', 'web', 'app.js'), 'utf8');
// 抠出 sortBy + sortCodes 两个方法体，配一个假 this 直接调用
const m = src.match(/sortBy\(k\) \{[\s\S]*?\n    \},\n[\s\S]*?sortCodes\(\) \{[\s\S]*?\n    \},/);
ok(m, 'app.js 中应能定位 sortBy/sortCodes 方法体');
if (m) {
  const fake = {
    view: 'pick', rankCalled: false,
    meta: { sort: 'pct', order: 'desc', page: 1 },
    order: ['c1', 'c2', 'c3'],
    quotes: { c1: { pct: 5.9, code: '000001' }, c2: { pct: 10.0, code: '600519' },
      c3: { pct: -1.2, code: '301314' } },
    fetchRank() { this.rankCalled = true; },
  };
  // eslint-disable-next-line no-eval
  const { sortBy, sortCodes } = eval('({' + m[0] + '})');
  Object.assign(fake, { sortBy, sortCodes });     // 方法互调走 this，必须挂到同一个 this 上
  sortBy.call(fake, 'pct');                       // 已是 pct/desc → 翻转? 不，sort==='pct' 且 order='desc' → 翻成 asc
  ok(fake.meta.order === 'asc', '重复点同列应翻转方向');
  ok(JSON.stringify(fake.order) === '["c3","c1","c2"]', 'asc 应为 ' + JSON.stringify(fake.order) + '，实际 ' + JSON.stringify(fake.order));
  ok(!fake.rankCalled, 'pick 视图点排序不应触发 fetchRank');
  sortBy.call(fake, 'pct');                       // 再点 → desc
  ok(JSON.stringify(fake.order) === '["c2","c1","c3"]', 'desc 应为 c2,c1,c3');
  sortBy.call(fake, 'code');                      // 换列 → desc code（字符串倒序：600519>301314>000001）
  ok(fake.meta.sort === 'code' && fake.meta.order === 'desc', '换列应重置为 desc');
  ok(JSON.stringify(fake.order) === '["c2","c3","c1"]', 'code desc 应为 c2,c3,c1，实际 ' + JSON.stringify(fake.order));
  // 缺字段（quotes 里没有的票）按 0 处理，不应抛异常
  fake.order = ['c1', 'cx', 'c2'];
  sortBy.call(fake, 'amt');
  ok(fake.order.length === 3, '缺字段排序不应丢行/抛错');
  // market 视图仍走服务端
  fake.view = 'market';
  sortBy.call(fake, 'pct');
  ok(fake.rankCalled, 'market 视图应仍走 fetchRank（服务端排序分页）');
}

console.log(errors.length ? 'SORT_CHECK_FAIL\n' + errors.join('\n') : 'SORT_ALL_PASS');
process.exit(errors.length ? 1 : 0);
