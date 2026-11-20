-- NCFTA layer 1: staging + RAW | 2026-11-20
-- Prepared locally; not executed or validated by a Snowflake server.
-- Run as a role authorized to create objects in the existing schemas.
-- Select the intended account/role before executing this script.
-- This script provisions objects only; it does not upload or load any data.
-- CREATE IF NOT EXISTS preserves existing objects but does not verify them.
-- The additive ALTER below also upgrades the earlier table with SOURCE_VALUE.
-- Run 01_verify_staging_and_raw.sql afterward and compare definitions.
-- Run separately from data-load transactions (DDL commits independently).
-- All timestamps ending _AT are ingestion times, not source modification times.
-- Source timestamps, objects, attributes and deletion flags remain in JSON.
-- Keys below are logical contracts: the loader must enforce deduplication.
-- Initial scope is one ingestion writer; no concurrent-load guarantee.
-- No existing ALERT_* objects, grants, schemas or data are changed.
-- References:
-- https://docs.snowflake.com/en/sql-reference/sql/create-file-format
-- https://docs.snowflake.com/en/sql-reference/sql/create-stage
-- https://docs.snowflake.com/en/sql-reference/sql/create-table

SET NCFTA_DATABASE = 'COMPLIANCE_SCREENING_SANDBOX';
USE DATABASE IDENTIFIER($NCFTA_DATABASE);

-- Existing schemas: INTERNAL_STAGE, RAW, REFINED, REPORTING, AUDIT_LAYER.
-- Proposed placement: curated -> REFINED; application -> REPORTING.
-- ALERT_STAGE and existing ALERT_CSV_* formats remain owned by the alert pipeline.

-- Both upload streams use generated UTF-8 JSONL, with one envelope per line.
-- Original files remain unchanged; source JSON is kept under payload properties.
CREATE FILE FORMAT IF NOT EXISTS INTERNAL_STAGE.NCFTA_JSONL_FF
  TYPE = JSON
  COMPRESSION = AUTO
  MULTI_LINE = FALSE
  STRIP_OUTER_ARRAY = FALSE
  STRIP_NULL_VALUES = FALSE
  ALLOW_DUPLICATE = FALSE
  REPLACE_INVALID_CHARACTERS = FALSE
  IGNORE_UTF8_ERRORS = FALSE
  COMMENT = 'NCFTA event and prediction JSONL envelopes; preserve source nulls';

CREATE STAGE IF NOT EXISTS INTERNAL_STAGE.NCFTA_STAGE
  FILE_FORMAT = (FORMAT_NAME = 'INTERNAL_STAGE.NCFTA_JSONL_FF')
  COMMENT = 'Paths: batch_uuid/events and batch_uuid/results; manifest selects exact files';

-- One stable package registration, reused by later load attempts.
-- Logical unique key: BATCH_FINGERPRINT. SOURCE_RUN_NAME is the existing local
-- run name (currently not a UUID); do not relabel it as a source UUID.
CREATE TABLE IF NOT EXISTS RAW.NCFTA_BATCHES (
  SOURCE_BATCH_ID VARCHAR(36) NOT NULL,
  BATCH_FINGERPRINT VARCHAR(64) NOT NULL,
  SOURCE_RUN_NAME VARCHAR NOT NULL,
  SOURCE_INSTANCE_ID VARCHAR NOT NULL,
  SOURCE_MANIFEST VARIANT NOT NULL,
  SOURCE_EVENTS_SHA256 VARCHAR(64) NOT NULL,
  SOURCE_RECORDS_SHA256 VARCHAR(64) NOT NULL,
  SOURCE_PREDICTIONS_SHA256 VARCHAR(64) NOT NULL,
  CATALOG_SHA256 VARCHAR(64) NOT NULL,
  MODEL_SHA256 VARCHAR(64) NOT NULL,
  ASSET_HASH VARCHAR(64) NOT NULL,
  PREDICTION_IDENTITY VARCHAR(64) NOT NULL,
  THRESHOLD FLOAT NOT NULL,
  SOURCE_GROUP_IDS ARRAY NOT NULL,
  SOURCE_DATE_FROM DATE,
  SOURCE_DATE_TO DATE,
  EXPECTED_EVENTS NUMBER(38,0) NOT NULL,
  EXPECTED_OBJECTS NUMBER(38,0) NOT NULL,
  EXPECTED_ATTRIBUTES NUMBER(38,0) NOT NULL,
  EXPECTED_PREDICTION_RECORDS NUMBER(38,0) NOT NULL,
  FIRST_LOAD_RUN_ID VARCHAR(36) NOT NULL,
  REGISTERED_AT TIMESTAMP_TZ NOT NULL DEFAULT CURRENT_TIMESTAMP()
) COMMENT = 'Completed local run registration and original manifest; no model loading';

-- One event snapshot per source batch, including direct attributes and objects.
-- Event UUID may be absent in an imported source; preserve NULL plus source ID.
-- EVENT_KEY is an explicitly labelled, loader-generated identity in that case.
-- Logical key: (SOURCE_BATCH_ID, EVENT_KEY). Payload hash differences under that
-- same key are conflicts; a later source batch is a new historical observation.
CREATE TABLE IF NOT EXISTS RAW.NCFTA_EVENTS (
  EVENT_SNAPSHOT_ID VARCHAR(36) NOT NULL,
  SOURCE_BATCH_ID VARCHAR(36) NOT NULL,
  FIRST_LOAD_RUN_ID VARCHAR(36) NOT NULL,
  EVENT_KEY VARCHAR NOT NULL,
  EVENT_UUID VARCHAR(36),
  SOURCE_EVENT_ID VARCHAR,
  EVENT_DATE DATE,
  EVENT_SHARING_GROUP_ID VARCHAR,
  EVENT_PAYLOAD VARIANT NOT NULL,
  PAYLOAD_SHA256 VARCHAR(64) NOT NULL,
  STAGED_FILENAME VARCHAR NOT NULL,
  FILE_RECORD_NUMBER NUMBER(38,0) NOT NULL,
  FILE_CONTENT_KEY VARCHAR,
  LOADED_AT TIMESTAMP_TZ NOT NULL DEFAULT CURRENT_TIMESTAMP()
) COMMENT = 'Complete event JSON snapshot; values never replaced by model predictions';

-- One original attribute record joined to its prediction outcome.
-- Logical key: (SOURCE_BATCH_ID, SOURCE_REVIEW_ID, PREDICTION_IDENTITY).
-- SOURCE_REVIEW_ID is the saved internal join key; not an NCFTA UUID.
CREATE TABLE IF NOT EXISTS RAW.NCFTA_MODEL_RESULTS (
  RESULT_ID VARCHAR(36) NOT NULL,
  SOURCE_BATCH_ID VARCHAR(36) NOT NULL,
  FIRST_LOAD_RUN_ID VARCHAR(36) NOT NULL,
  EVENT_SNAPSHOT_ID VARCHAR(36) NOT NULL,
  SOURCE_REVIEW_ID VARCHAR NOT NULL,
  EVENT_UUID VARCHAR(36),
  OBJECT_UUID VARCHAR(36),
  ATTRIBUTE_UUID VARCHAR(36),
  SOURCE_EVENT_ID VARCHAR,
  SOURCE_OBJECT_ID VARCHAR,
  SOURCE_ATTRIBUTE_ID VARCHAR,
  EVENT_SHARING_GROUP_ID VARCHAR,
  ATTRIBUTE_SHARING_GROUP_ID VARCHAR,
  ATTRIBUTE_DISTRIBUTION VARCHAR,
  SOURCE_TYPE VARCHAR,
  SOURCE_CATEGORY VARCHAR,
  SOURCE_VALUE VARCHAR COMMENT 'Original attribute text, unchanged; absent or null stays NULL; full source remains in JSON',
  SOURCE_ATTRIBUTE_SHA256 VARCHAR(64) NOT NULL,
  INPUT_SHA256 VARCHAR(64) NOT NULL,
  PREDICTION_IDENTITY VARCHAR(64) NOT NULL,
  MODEL_SHA256 VARCHAR(64) NOT NULL,
  OUTCOME_STATUS VARCHAR NOT NULL,
  SOURCE_ATTRIBUTE_DELETED BOOLEAN,
  SOURCE_OBJECT_DELETED BOOLEAN,
  IS_DELETED BOOLEAN NOT NULL,
  SOURCE_RECORD VARIANT NOT NULL,
  PREDICTION VARIANT NOT NULL,
  RESULT_PAYLOAD_SHA256 VARCHAR(64) NOT NULL,
  STAGED_FILENAME VARCHAR NOT NULL,
  FILE_RECORD_NUMBER NUMBER(38,0) NOT NULL,
  FILE_CONTENT_KEY VARCHAR,
  LOADED_AT TIMESTAMP_TZ NOT NULL DEFAULT CURRENT_TIMESTAMP()
) COMMENT = 'Exact saved source context, prediction spans, confidence and outcome';

-- Safe additive upgrade if the earlier RAW table already exists.
-- Existing rows remain NULL; no historical data is rewritten here.
ALTER TABLE RAW.NCFTA_MODEL_RESULTS ADD COLUMN IF NOT EXISTS
  SOURCE_VALUE VARCHAR COMMENT 'Original attribute text, unchanged; absent or null stays NULL; full source remains in JSON';


