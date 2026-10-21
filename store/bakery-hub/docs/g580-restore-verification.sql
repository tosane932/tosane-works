-- G580の隔離DBだけで実行する。個人情報や画像本体は出力しない。
-- psql -X -v ON_ERROR_STOP=1 -d 'service=approved_restore_target' -f docs/g580-restore-verification.sql
\pset pager off
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;
-- 名前だけでは独立性を証明しない。ネットワークとguard.preflightも別途確認。
SELECT 1 / CASE WHEN current_database() = 'isolated_restore'
                 AND current_setting('server_version_num')::integer / 10000 = 18
                THEN 1 ELSE 0 END AS expected_isolated_pg18;
SELECT current_database() AS restored_db, current_setting('server_version') AS pg_version;

-- 表名・実件数。pg_statの推計値を使わない。27表の確認にも使う。
SELECT format('SELECT %L AS table_name, count(*) AS rows FROM %I.%I;',
              n.nspname || '.' || c.relname, n.nspname, c.relname)
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
ORDER BY c.relname
\gexec

SELECT kind, count(*) AS datasets FROM public.datasets GROUP BY kind ORDER BY kind;
-- dataset_idを持つ全表について所有Datasetの分類と孤児件数を表示。
SELECT format('SELECT %L AS table_name, d.kind, count(*) AS rows FROM %I.%I t '
              || 'JOIN public.datasets d ON d.id=t.dataset_id GROUP BY d.kind ORDER BY d.kind;',
              table_name, table_schema, table_name)
FROM information_schema.columns
WHERE table_schema='public' AND column_name='dataset_id' AND table_name <> 'datasets'
ORDER BY table_name
\gexec
SELECT format('SELECT %L AS table_name, count(*) AS missing_dataset FROM %I.%I t '
              || 'LEFT JOIN public.datasets d ON d.id=t.dataset_id WHERE d.id IS NULL;',
              table_name, table_schema, table_name)
FROM information_schema.columns
WHERE table_schema='public' AND column_name='dataset_id' AND table_name <> 'datasets'
ORDER BY table_name
\gexec

-- 日別の金額・個人の行は出さず、日付順の集計全体をSHA-256にする。
WITH per_day AS (
  SELECT s.date, count(*) AS rows, sum(s.quantity) AS quantity,
         sum(s.quantity::bigint * p.price) AS sales_yen
  FROM public.daily_sales s JOIN public.products p ON p.id=s.product_id
  GROUP BY s.date
)
SELECT count(*) AS sales_days, coalesce(sum(rows), 0) AS sales_rows,
       encode(sha256(convert_to(coalesce(string_agg(
         format('%s:%s:%s:%s', date, rows, quantity, sales_yen), E'\n' ORDER BY date), ''), 'UTF8')), 'hex')
         AS dated_sales_digest
FROM per_day;
WITH per_day AS (
  SELECT business_date, kind, count(*) AS rows, sum(total_yen) AS total_yen
  FROM public.register_transactions GROUP BY business_date, kind
)
SELECT count(*) AS register_day_kinds, coalesce(sum(rows), 0) AS transactions,
       encode(sha256(convert_to(coalesce(string_agg(
         format('%s:%s:%s:%s', business_date, kind, rows, total_yen), E'\n'
         ORDER BY business_date, kind), ''), 'UTF8')), 'hex') AS register_totals_digest
FROM per_day;
SELECT kind, count(*) AS transactions FROM public.register_transactions GROUP BY kind ORDER BY kind;

-- 画像本体・商品ID・個別ハッシュは表示しない。旧productsには画像バイナリ列はない。
SELECT count(photo_webp) AS photos, coalesce(sum(octet_length(photo_webp)), 0) AS total_bytes,
       encode(sha256(convert_to(coalesce(string_agg(
         encode(sha256(photo_webp), 'hex'), ',' ORDER BY id)
         FILTER (WHERE photo_webp IS NOT NULL), ''), 'UTF8')), 'hex') AS photo_digest
FROM public.register_products;
-- 複合FKに加え、重要なリンクの孤児数を明示。
SELECT (SELECT count(*) FROM public.register_transaction_lines l
        LEFT JOIN public.register_transactions t ON t.id=l.transaction_id AND t.dataset_id=l.dataset_id
        WHERE t.id IS NULL) AS orphan_transaction_lines,
       (SELECT count(*) FROM public.register_prep_links l
        LEFT JOIN public.prep_recipes r ON r.id=l.recipe_id AND r.dataset_id=l.dataset_id
        LEFT JOIN public.register_products p ON p.id=l.register_product_id AND p.dataset_id=l.dataset_id
        WHERE r.id IS NULL OR p.id IS NULL) AS orphan_recipe_links,
       (SELECT count(*) FROM public.shop_shifts s
        LEFT JOIN public.shop_staff t ON t.id=s.staff_id AND t.dataset_id=s.dataset_id
        WHERE t.id IS NULL) AS orphan_shifts;
SELECT count(*) AS alembic_rows, min(version_num) AS revision FROM public.alembic_version;

SELECT contype, count(*) AS constraints, count(*) FILTER (WHERE NOT convalidated) AS unvalidated
FROM pg_constraint c JOIN pg_namespace n ON n.oid=c.connamespace
WHERE n.nspname='public' GROUP BY contype ORDER BY contype;
SELECT count(*) AS indexes, count(*) FILTER (WHERE NOT i.indisvalid) AS invalid,
       count(*) FILTER (WHERE NOT i.indisready) AS not_ready
FROM pg_index i JOIN pg_class t ON t.oid=i.indrelid
JOIN pg_namespace n ON n.oid=t.relnamespace WHERE n.nspname='public';
SELECT t.tgisinternal AS internal, t.tgenabled AS enabled, count(*) AS triggers
FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
JOIN pg_namespace n ON n.oid=c.relnamespace
WHERE n.nspname='public' GROUP BY t.tgisinternal, t.tgenabled ORDER BY 1,2;
SELECT schemaname, sequencename, last_value FROM pg_sequences
WHERE schemaname='public' ORDER BY sequencename;
COMMIT;
