-- TEST FIXTURE: the two pitdb tables and the price_asof macro that rank_pit_signals
-- reads, copied verbatim from implementation/pit_warehouse/pitdb/schema.sql.

CREATE TABLE IF NOT EXISTS dim_security (
    sec_id           BIGINT PRIMARY KEY,
    figi             VARCHAR,               -- OpenFIGI (free) permanent identifier
    isin             VARCHAR,
    primary_ticker   VARCHAR,
    exchange_mic     VARCHAR,
    country          VARCHAR,
    currency         VARCHAR,
    asset_class      VARCHAR,               -- equity, etf, index, future, fx, rate, commodity, option
    name             VARCHAR,
    sector           VARCHAR,
    is_active        BOOLEAN DEFAULT TRUE,
    listed_from      DATE,
    delisted_on      DATE,                  -- kept: the warehouse is survivorship-safe
    created_at       TIMESTAMP
);

CREATE TABLE IF NOT EXISTS fact_price_eod (
    sec_id           BIGINT      NOT NULL,
    event_date       DATE        NOT NULL,
    knowledge_time   TIMESTAMP   NOT NULL,
    revision_seq     INTEGER     NOT NULL DEFAULT 0,
    open             DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE,
    volume           DOUBLE,
    vwap             DOUBLE,
    trade_count      BIGINT,
    currency         VARCHAR,
    is_settled       BOOLEAN DEFAULT TRUE,   -- FALSE for intraday/provisional prints
    source_id        VARCHAR,
    ingest_run_id    BIGINT
);

CREATE OR REPLACE MACRO price_asof(kt) AS TABLE
SELECT sec_id, event_date, open, high, low, close, volume, vwap, currency,
       knowledge_time, revision_seq, source_id
FROM (
    SELECT *, ROW_NUMBER() OVER (
               PARTITION BY sec_id, event_date
               ORDER BY knowledge_time DESC, revision_seq DESC) AS rn
    FROM fact_price_eod
    WHERE knowledge_time <= kt
) WHERE rn = 1;
