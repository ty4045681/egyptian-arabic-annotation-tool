-- Run with psql as postgres against the production database.
-- The matching OS account authenticates via the existing local peer rule.
\set ON_ERROR_STOP on
BEGIN;
SELECT 'CREATE ROLE annotation_backup LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS'
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'annotation_backup') \gexec
GRANT CONNECT ON DATABASE annotation_production_20260915 TO annotation_backup;
GRANT USAGE ON SCHEMA public TO annotation_backup;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO annotation_backup;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO annotation_backup;
ALTER DEFAULT PRIVILEGES FOR ROLE annotation_prod_owner IN SCHEMA public
    GRANT SELECT ON TABLES TO annotation_backup;
ALTER DEFAULT PRIVILEGES FOR ROLE annotation_prod_owner IN SCHEMA public
    GRANT SELECT ON SEQUENCES TO annotation_backup;
ALTER ROLE annotation_backup SET default_transaction_read_only = on;
COMMIT;
