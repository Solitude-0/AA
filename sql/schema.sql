-- 分析看盘 · 时序库结构（库名 realtime，与 dashboard 物理隔离，避免刷新链锁表影响盯盘）
-- 全部语句幂等，启动时执行一次即可。

-- ── 分区工具（TimescaleDB 的核心能力，PG 原生等价实现）──────────
CREATE OR REPLACE FUNCTION ensure_partition(p_parent text, p_day date)
RETURNS void AS $$
DECLARE
    v_child text := p_parent || '_' || to_char(p_day, 'YYYYMMDD');
    v_start text := to_char(p_day, 'YYYY-MM-DD');
    v_end   text := to_char(p_day + 1, 'YYYY-MM-DD');
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = v_child) THEN
        EXECUTE format(
            'CREATE TABLE %I PARTITION OF %I FOR VALUES FROM (%L) TO (%L)',
            v_child, p_parent, v_start, v_end);
    END IF;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION drop_partitions_before(p_parent text, p_day date)
RETURNS int AS $$
DECLARE
    r record;
    v_suffix text := to_char(p_day, 'YYYYMMDD');
    n int := 0;
BEGIN
    FOR r IN
        SELECT c.relname
          FROM pg_inherits i
          JOIN pg_class c ON c.oid = i.inhrelid
          JOIN pg_class p ON p.oid = i.inhparent
         WHERE p.relname = p_parent
    LOOP
        IF substring(r.relname from length(p_parent) + 2) < v_suffix THEN
            EXECUTE format('DROP TABLE IF EXISTS %I', r.relname);
            n := n + 1;
        END IF;
    END LOOP;
    RETURN n;
END;
$$ LANGUAGE plpgsql;

-- ── 分钟快照（全市场）───────────────────────────────
CREATE TABLE IF NOT EXISTS snap_min (
    ts          timestamptz NOT NULL,
    code        char(6)     NOT NULL,
    price       numeric(10,3),
    pct         numeric(7,3),
    vol_hand    bigint,
    amt_wan     numeric(14,1),
    turnover    numeric(7,3),
    vol_ratio   numeric(8,3),
    bid1        numeric(10,3),
    bid1_vol    int,
    ask1        numeric(10,3),
    ask1_vol    int,
    out_vol     bigint,
    in_vol      bigint,
    limit_up    numeric(10,3),
    limit_dn    numeric(10,3),
    PRIMARY KEY (ts, code)
) PARTITION BY RANGE (ts);

CREATE INDEX IF NOT EXISTS idx_snap_min_code_ts ON snap_min (code, ts DESC);
-- 时间列天然有序，BRIN 体积约为 B-tree 的 1/100
CREATE INDEX IF NOT EXISTS idx_snap_min_ts_brin ON snap_min USING brin (ts);

-- ── 板块分钟 ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS board_min (
    ts          timestamptz NOT NULL,
    bk_code     varchar(12) NOT NULL,
    name        text,
    kind        char(1),
    pct         numeric(7,3),
    amt_wan     numeric(14,1),
    net_in      numeric(16,1),
    up_cnt      int,
    dn_cnt      int,
    leader_code varchar(12),   -- 东财给 6 位代码，新浪给 sz000972（带市场前缀），char(6) 会直接让 COPY 失败
    leader_name text,
    PRIMARY KEY (ts, bk_code)
) PARTITION BY RANGE (ts);

CREATE INDEX IF NOT EXISTS idx_board_min_ts_brin ON board_min USING brin (ts);

-- ── 自选 tick（5 s 粒度）────────────────────────────
CREATE TABLE IF NOT EXISTS tick_sec (
    ts        timestamptz NOT NULL,
    code      char(6)     NOT NULL,
    price     numeric(10,3),
    vol_hand  bigint,
    amt_wan   numeric(14,1),
    PRIMARY KEY (ts, code)
) PARTITION BY RANGE (ts);

CREATE INDEX IF NOT EXISTS idx_tick_sec_code_ts ON tick_sec (code, ts DESC);

-- ── 龙虎榜（T+1，不分区）─────────────────────────────
CREATE TABLE IF NOT EXISTS lhb (
    trade_date  date        NOT NULL,
    code        char(6)     NOT NULL,
    name        text,
    reason      text,
    pct         numeric(9,3),
    buy_amt     numeric(16,1),
    sell_amt    numeric(16,1),
    net_amt     numeric(16,1),
    amt         numeric(16,1),
    seat        text,
    PRIMARY KEY (trade_date, code, reason)
);
CREATE INDEX IF NOT EXISTS idx_lhb_date ON lhb (trade_date DESC);

-- ── 异动事件（收盘后可回看）──────────────────────────
CREATE TABLE IF NOT EXISTS alert_log (
    ts     timestamptz NOT NULL,
    code   char(6),
    name   text,
    kind   text,
    text   text,
    price  numeric(10,3),
    pct    numeric(7,3)
);
CREATE INDEX IF NOT EXISTS idx_alert_ts ON alert_log (ts DESC);

-- ── 收盘作业日志 ────────────────────────────────────
CREATE TABLE IF NOT EXISTS job_log (
    day      date PRIMARY KEY,
    ran_at   timestamptz,
    snap_rows int,
    board_rows int,
    lhb_rows int,
    note     text
);
