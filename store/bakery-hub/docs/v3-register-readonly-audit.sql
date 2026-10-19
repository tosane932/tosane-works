-- Phase 7: approved read-only connection only; no credentials in this file.
-- Run inside a READ ONLY transaction (Render query tool supplies this wrapper).
-- Dataset UUIDs and submission keys are deliberately not returned.
-- Matching counts do not prove that the Web service uses this database.
SELECT clock_timestamp() AS checked_at_utc, current_database() AS database_name,
       current_setting('server_version') AS server_version,
       current_setting('transaction_read_only') AS transaction_read_only,
       (SELECT string_agg(version_num, ',') FROM alembic_version) AS alembic_revision,
       pg_database_size(current_database()) AS database_bytes;

WITH a AS (SELECT id FROM datasets WHERE kind='admin'),
old AS (SELECT s.date,s.quantity,p.price FROM daily_sales s JOIN products p ON p.id=s.product_id WHERE p.dataset_id IN (SELECT id FROM a)),
tx AS (SELECT * FROM register_transactions WHERE dataset_id IN (SELECT id FROM a)),
lines AS (SELECT * FROM register_transaction_lines WHERE dataset_id IN (SELECT id FROM a)),
rp AS (SELECT * FROM register_products WHERE dataset_id IN (SELECT id FROM a)),
mismatch AS (SELECT t.id FROM tx t LEFT JOIN lines l ON l.transaction_id=t.id AND l.dataset_id=t.dataset_id GROUP BY t.id,t.total_yen HAVING count(l.id)=0 OR COALESCE(sum(l.line_total_yen),0)<>t.total_yen)
SELECT clock_timestamp() AS checked_at_utc,current_setting('transaction_read_only') AS transaction_read_only,
(SELECT count(*) FROM a) AS admin_datasets,
(SELECT count(*) FROM datasets WHERE kind='guest') AS guest_datasets,
(SELECT count(*) FROM products WHERE dataset_id IN (SELECT id FROM a)) AS old_products,
(SELECT count(*) FROM products WHERE dataset_id IN (SELECT id FROM a) AND is_active) AS old_active_products,
(SELECT count(*) FROM old) AS old_sales_rows,(SELECT min(date) FROM old) AS old_first_day,(SELECT max(date) FROM old) AS old_last_day,
(SELECT sum(quantity) FROM old) AS old_quantity,(SELECT sum(quantity::bigint*price) FROM old) AS old_current_price_estimate_yen,
(SELECT count(*) FROM rp) AS register_products,(SELECT count(*) FROM rp WHERE is_active AND price_yen>0) AS register_saleable_products,
(SELECT count(*) FROM rp WHERE photo_webp IS NOT NULL) AS uploaded_photos,(SELECT count(*) FROM rp WHERE image_key IS NOT NULL) AS catalog_images,
(SELECT min(price_yen) FROM rp WHERE is_active) AS active_min_price,(SELECT max(price_yen) FROM rp WHERE is_active) AS active_max_price,
(SELECT count(*) FROM register_price_history WHERE dataset_id IN (SELECT id FROM a)) AS price_history_rows,
(SELECT count(*) FROM register_cutovers WHERE dataset_id IN (SELECT id FROM a)) AS cutovers,
(SELECT count(*) FROM register_cutover_approvals WHERE dataset_id IN (SELECT id FROM a)) AS cutover_approvals,
(SELECT count(*) FROM tx) AS transactions,(SELECT count(*) FROM tx WHERE kind='sale') AS sales,(SELECT count(*) FROM tx WHERE kind='refund') AS refunds,
(SELECT count(*) FROM lines) AS transaction_lines,(SELECT count(*) FROM mismatch) AS header_line_mismatches,
(SELECT count(*) FROM (SELECT DISTINCT date FROM old INTERSECT SELECT DISTINCT business_date FROM tx) q) AS overlap_days,
(SELECT count(*) FROM register_recovery_cases WHERE dataset_id IN (SELECT id FROM a)) AS recovery_cases,
(SELECT count(*) FROM register_recovery_cases WHERE dataset_id IN (SELECT id FROM a) AND status NOT IN ('saved','cancelled')) AS unresolved_recovery_cases,
(SELECT count(*) FROM register_recovery_lines WHERE dataset_id IN (SELECT id FROM a)) AS recovery_lines,
(SELECT count(*) FROM register_recovery_events WHERE dataset_id IN (SELECT id FROM a)) AS recovery_events,
(SELECT count(*) FROM register_offline_permits WHERE dataset_id IN (SELECT id FROM a)) AS offline_permits,
(SELECT count(*) FROM register_prep_links WHERE dataset_id IN (SELECT id FROM a)) AS prep_links;
