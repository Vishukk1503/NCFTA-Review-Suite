-- NCFTA layer 4: application history. Prepared locally; not server-validated.
-- 29 business columns + 33 technical columns = 62 total.
-- Logical key: (CURATED_ROW_ID, APP_MAPPING_VERSION), enforced by future procedure.
-- One attribute observation/version per row. Never combine unrelated attributes.
-- Repeated observations remain traceable; no latest/current view is created here.
-- Arrays contain literal strings, not inferred person/account relationships.
-- FIELD_PROVENANCE records field + array index + source key/kind/offsets.
-- Deleted or unevaluated outcomes have NULL business arrays; retain their IDs/flags.
-- Evaluated fields without matching values use []; do not flatten arrays into joins.
-- Original source text and full JSON remain in RAW, linked by RESULT_ID.
-- No uploads, inserts, procedures, grants, destructive changes or model execution.
-- CREATE IF NOT EXISTS does not migrate existing definitions; run verification.
USE DATABASE COMPLIANCE_SCREENING_SANDBOX;

CREATE TABLE IF NOT EXISTS REPORTING.NCFTA_APP (
  CURATED_ROW_ID VARCHAR(36) NOT NULL COMMENT 'Stable loader UUID for RESULT_ID and TRANSFORM_VERSION',
  RESULT_ID VARCHAR(36) NOT NULL COMMENT 'Links to RAW.NCFTA_MODEL_RESULTS',
  SOURCE_BATCH_ID VARCHAR(36) NOT NULL COMMENT 'Links to RAW.NCFTA_BATCHES',
  EVENT_SNAPSHOT_ID VARCHAR(36) NOT NULL COMMENT 'Links to RAW.NCFTA_EVENTS',
  SOURCE_INSTANCE_ID VARCHAR NOT NULL COMMENT 'Source namespace from the batch; no credentials',
  EVENT_UUID VARCHAR(36),
  OBJECT_UUID VARCHAR(36),
  ATTRIBUTE_UUID VARCHAR(36),
  SOURCE_EVENT_ID VARCHAR,
  SOURCE_OBJECT_ID VARCHAR,
  SOURCE_ATTRIBUTE_ID VARCHAR,
  SOURCE_REVIEW_ID VARCHAR NOT NULL COMMENT 'Saved internal join key, not a source UUID',
  EVENT_SHARING_GROUP_ID VARCHAR,
  ATTRIBUTE_SHARING_GROUP_ID VARCHAR,
  ATTRIBUTE_DISTRIBUTION VARCHAR,
  SOURCE_TYPE VARCHAR,
  SOURCE_CATEGORY VARCHAR,
  SOURCE_ATTRIBUTE_SHA256 VARCHAR(64) NOT NULL COMMENT 'Source attribute hash copied from RAW',
  MODEL_SHA256 VARCHAR(64) NOT NULL,
  CATALOG_SHA256 VARCHAR(64) NOT NULL,
  PREDICTION_IDENTITY VARCHAR(64) NOT NULL COMMENT 'Saved inference identity; not a source version timestamp',
  TRANSFORM_VERSION VARCHAR NOT NULL,
  OUTCOME_STATUS VARCHAR NOT NULL COMMENT 'predicted, deleted, invalid_source, empty_value or input_error',
  SOURCE_ATTRIBUTE_DELETED BOOLEAN COMMENT 'Original attribute deletion signal; absent stays NULL',
  SOURCE_OBJECT_DELETED BOOLEAN COMMENT 'Original parent object deletion signal; absent stays NULL',
  IS_DELETED BOOLEAN NOT NULL COMMENT 'True when either source signal is true; absence is not a deletion',
  REVIEW_STATUS VARCHAR NOT NULL DEFAULT 'NOT_REVIEWED' COMMENT 'No automatic analyst approval',
  WARNINGS ARRAY NOT NULL COMMENT 'Saved extraction and validation warnings',
  APP_ROW_ID VARCHAR(36) NOT NULL COMMENT 'Stable publication identity for curated row and mapping version',
  APP_MAPPING_VERSION VARCHAR NOT NULL,
  FIELD_PROVENANCE VARIANT NOT NULL COMMENT 'Per field and array index: source key, value kind and evidence offsets; preserves URL versus domain and category text versus MCC',
  PUBLISHED_BY_LOAD_RUN_ID VARCHAR(36) NOT NULL,
  PUBLISHED_AT TIMESTAMP_TZ NOT NULL DEFAULT CURRENT_TIMESTAMP() COMMENT 'Publication time, not source version order',
  ADDRESS ARRAY COMMENT 'Address. Source: address. Preserve the reported address. Do not infer ownership.',
  REPORTED_AMOUNT ARRAY COMMENT 'Dollar Amount. Source: reported_amount. Use reported_currency when supplied. Do not assume USD.',
  BANK_NAME ARRAY COMMENT 'Bank Name. Source: bank_name. Preserve the source bank name.',
  BANK_ADDRESS ARRAY COMMENT 'Bank Address. Source: bank_location. A bank location may be a city, not a full postal address.',
  CRIME_DATETIME ARRAY COMMENT 'Date of Crime. Source: crime_datetime. Keep separate from event date. Do not invent a timezone.',
  SUBMITTED_BY ARRAY COMMENT 'Submitted By. Source: reporter. Source Reported By value. Not the threat actor or victim.',
  SWIFT_CODE ARRAY COMMENT 'SWIFT. Source: swift_code. Preserve the reported SWIFT value as text.',
  COMMENTS ARRAY COMMENT 'Comments. Source: Raw attribute.value (saved source_value). Use the comment-type / Other attribute value, not its metadata comment.',
  BANK_ACCOUNT_NUMBER ARRAY COMMENT 'Bank Account #. Source: bank_account_number. Store as text to preserve leading zeros and masking.',
  ROUTING_NUMBER ARRAY COMMENT 'Bank Routing #. Source: routing_number. Store as text to preserve leading zeros.',
  REPORTED_ACTOR_NAME ARRAY COMMENT 'Threat Actor Name. Source: reported_actor_name. Preserve the reported role. Do not infer account ownership.',
  PHONE_NUMBER ARRAY COMMENT 'Phone. Source: phone_number. Do not infer mobile status or country code.',
  SOURCE_IP ARRAY COMMENT 'IP. Source: source_ip. Applies to the ip-src rows shown in the mapping sheet.',
  EMAIL_ADDRESS ARRAY COMMENT 'Email. Source: email_address. Preserve the source email value and its attribute link.',
  DATE_OF_BIRTH ARRAY COMMENT 'DoB. Source: date_of_birth. Do not associate a date with a person without source evidence.',
  URL ARRAY COMMENT 'URL. Source: url domain. Keep domain and full URL distinguishable. Do not invent a protocol.',
  MERCHANT_CATEGORY ARRAY COMMENT 'Merchant Category. Source: Raw attribute.value merchant_category_code (if MCC). Use raw category text. Use merchant_category_code only for an actual MCC.',
  COMPANY_NAME ARRAY COMMENT 'Company Name. Source: company_name. Preserve the reported company. Keep it separate from merchant and target.',
  ALIAS_NAME ARRAY COMMENT 'Alias Name. Source: alias_name. Preserve the reported alias. Link it to a person only with evidence.',
  AGE ARRAY COMMENT 'Age. Source: age. Preserve reported age and units. Do not derive it from date of birth.',
  PLACE_OF_BIRTH ARRAY COMMENT 'Place of Birth. Source: place_of_birth. Keep birthplace separate from current or previous address.',
  OCCUPATION ARRAY COMMENT 'Occupation. Source: occupation. Use the occupation or job title explicitly reported by the source.',
  EMPLOYER_NAME ARRAY COMMENT 'Employer Name. Source: employer_name. Use an organisation explicitly identified as an employer.',
  TARGET_ORGANIZATION ARRAY COMMENT 'Target Organisation. Source: target_organization. Preserve the target role. Keep it separate from company and merchant.',
  MERCHANT_NAME ARRAY COMMENT 'Merchant Name. Source: merchant_name. Use explicit merchant evidence. Keep company and target separate.',
  PREVIOUS_ADDRESS ARRAY COMMENT 'Previous Address. Source: previous_address. Use an address explicitly described as previous or former.',
  ACCOUNT_TYPE ARRAY COMMENT 'Account Type. Source: account_type. Use the reported account type. Link it only to the supported account.',
  TOTAL_AMOUNT ARRAY COMMENT 'Total Amount. Source: total_amount. Preserve the reported total and its scope. Do not calculate a new total.',
  REPORTED_CURRENCY ARRAY COMMENT 'Reported Currency. Source: reported_currency. Use an explicit currency code. Do not infer from an ambiguous symbol.'
) COMMENT = 'NCFTA app field history with attribute lineage and deletion evidence';
