# NCFTA Snowflake layers

Layers 1, 2, 3 and 4 are prepared locally. Nothing has been executed in Snowflake.

## 4. Application table

Run `04_app.sql`, then `04_verify_app.sql`. This creates
`REPORTING.NCFTA_APP`: 29 business columns plus 33 technical columns (62 total).
The final verification query returns mismatches only; zero rows means the checked
definitions match. Inspect DESCRIBE for defaults. No data or procedures are loaded.

The business fields follow the preserved v2 mapping in `app-fields-v2.json`.
`build_app_schema.py` generates both SQL files. The original workbook is unchanged.
Dollar Amount is named REPORTED_AMOUNT to avoid implying USD; Bank Address is
BANK_ADDRESS but retains bank_location semantics, possibly just a city.

Each row represents one curated attribute observation and app mapping version.
The publishing procedure uses `(CURATED_ROW_ID, APP_MAPPING_VERSION)`
as the logical key, rejects conflicting content for that key and makes identical retries no-ops.
New observations and mapping versions remain history. A repeated observation is
not automatically a source change. No active-only/latest view is created yet.
Source order must not be inferred from publication time.

All 29 business columns are arrays of literal strings. Repeated values stay in
source-span order, and positions across different columns do not imply ownership
or pairing. The publisher must validate string elements. Evaluated/no matching
value is []; deleted/unevaluated outcomes have SQL NULL business arrays.
Original nullable deletion flags and effective IS_DELETED remain separate.

FIELD_PROVENANCE is an object keyed by app column, containing evidence entries
with value_index, source_key, value_kind and offsets where available. For URL,
value_kind distinguishes domain from url; for MERCHANT_CATEGORY it distinguishes
raw category text from an explicitly supported MCC. Never add protocols, infer
currency, or convert account numbers to numbers. Comments come only from the raw
value of a comment-type / Other attribute, never its metadata comment. Merchant
category raw text applies only to the mapped category attribute, not arbitrary
comments. These conditional rules are implemented in the publisher described below.
Model-mapped fields use only accepted, validated predictions; no model reruns.
Original text/full JSON remain in RAW, accessible using RESULT_ID and EVENT_SNAPSHOT_ID.

IDs, source group IDs, extraction/transform/mapping versions, review status,
warnings and publication audit IDs remain attached. Groups 6, 7 and 8 share the
table. The publisher must carry existing review status without inventing approval.
History filtering, delta detection and concurrent writer protection remain work
for the procedures; table definitions alone do not provide those behaviors.

## 1. Staging and RAW

Database: `COMPLIANCE_SCREENING_SANDBOX`. Use the existing `INTERNAL_STAGE` and
`RAW` schemas. Run `01_staging_and_raw.sql` under an authorized role, then use
`01_verify_staging_and_raw.sql` to inspect the resulting definitions. Account,
role and privileges still need to be verified on the work PC. The script does
not choose a role, create schemas, grant access, upload files or load rows.

| Object | Purpose | Row meaning |
| --- | --- | --- |
| `INTERNAL_STAGE.NCFTA_JSONL_FF` | Strict UTF-8 JSON Lines file format; preserves JSON nulls | File format, not a table |
| `INTERNAL_STAGE.NCFTA_STAGE` | Dedicated upload location | Stage, not a table |
| `RAW.NCFTA_BATCHES` | Source run manifest, checksums, model identity, scope and expected counts | One immutable completed-run package |
| `RAW.NCFTA_EVENTS` | Complete event JSON in `EVENT_PAYLOAD VARIANT`, with source IDs and load lineage | One event snapshot per package |
| `RAW.NCFTA_MODEL_RESULTS` | Saved attribute context and prediction JSON, IDs, outcome and deletion flags | One source attribute's saved extraction outcome per package |

An event's objects and attributes stay nested in `EVENT_PAYLOAD`. Empty events
and objects are therefore preserved. No separate raw object table is necessary.
`SOURCE_RECORD` and `PREDICTION` preserve the saved extraction evidence; raw
source values are not replaced by predicted values. The 118 extracted fields
will become columns in REFINED, not in these RAW JSON tables.

`RAW.NCFTA_MODEL_RESULTS` now has 32 columns, including `SOURCE_VALUE VARCHAR`
for the original attribute text. Groups 6, 7 and 8 share this table and are
identified by the source group columns. The RAW loader copies the original
attribute's string `value` without trimming, truncation, normalization or model
substitution. Preserve empty text as an empty string, and absent/null as SQL NULL.
Non-string source values remain intact in the JSON and must not be silently
converted into text; leave this convenience column NULL and record a validation
warning. The original event JSON remains the authoritative source.

The layer-1 script includes an additive `ADD COLUMN IF NOT EXISTS` for tables
created using the earlier definition. Existing rows are not backfilled and remain
NULL in this column. Run the verification script afterward; its final check must
return PASS. The RAW-loading procedure populates the column for new rows.

The loader will read the completed parent run's `manifest.json`, `events.json`,
`attribute-records.jsonl` and `predictions.jsonl`. Excel remains the review output;
the loader must not reconstruct full source data from Excel preview cells or
load the parent and its duplicate group folders together. Reviewed Excel edits
need a separate import contract and are not implicitly present in these JSON files.

## Required loader behavior

- Package copies as JSONL envelopes, one event or result per line. Preserve
  original input files. Upload under `<source_batch_uuid>/events/` and
  `<source_batch_uuid>/results/`, using exact manifest-listed filenames.
- Give each load attempt a new audit run ID. Reuse the registered batch and row
  IDs when retrying the same package. `SOURCE_RUN_NAME` is the suite's run name,
  not a UUID; `SOURCE_REVIEW_ID` is an internal join key, not a source UUID.
- Validate hashes, counts and joins before committing RAW rows. Reject changed
  content under an already registered immutable key. Table creation alone does
  not implement duplicate prevention; the procedures enforce the logical keys.
- A newly extracted run is a new RAW observation, even if it contains unchanged
  events. REFINED/app version handling will distinguish repeated observations
  from actual content changes. Do not overwrite earlier RAW observations.
- Preserve `SOURCE_ATTRIBUTE_DELETED` and `SOURCE_OBJECT_DELETED` independently;
  absent source flags stay NULL. `IS_DELETED` is true if either source flag is
  explicitly true. Otherwise it is false, meaning no explicit deletion notice
  in that observation, not proof that the record is still active today.
  Reject malformed flags. Never infer deletion from a missing event/attribute.
  A proposal to delete is not a confirmed deletion.
- Preserve raw source timestamps in JSON. `LOADED_AT` is ingestion time and
  must not determine which source version is newest.
- Retain upload files until the load is confirmed; define cleanup/retention
  with the loader. Do not remove original run files or Excel workbooks.

The tables intentionally do not claim enforced unique/foreign-key constraints.
The first implementation supports one ingestion writer. `CREATE ... IF NOT EXISTS`
also does not migrate an older object: compare the inspection output before use.

## 2. Audit - one table

Run the updated `02_audit.sql`, then inspect it with `02_verify_audit.sql`.
It creates just **`AUDIT_LAYER.NCFTA_LOAD_AUDIT`**, replacing the earlier
three-table proposal. Existing alert audit tables are untouched. If the earlier
three NCFTA audit tables were already created, this script leaves them intact;
the new loader will use only the single table. It does not migrate old audit rows.

The audit table includes explicit operational metadata:

| Category | Columns |
| --- | --- |
| Source scope | `SOURCE_INSTANCE_ID`, `SOURCE_DATE_FROM`, `SOURCE_DATE_TO`, `SOURCE_GROUP_IDS` |
| File | `FILE_NAME`, `FILE_SHA256`, `FILE_SIZE_BYTES` |
| Processing versions | `LOADER_VERSION`, `MODEL_SHA256`, `TRANSFORM_VERSION`, `APP_MAPPING_VERSION` |
| Snowflake context | `SNOWFLAKE_ACCOUNT`, `SNOWFLAKE_USER`, `SNOWFLAKE_ROLE`, `SNOWFLAKE_WAREHOUSE` |
| Timing and target | `STARTED_AT`, `FINISHED_AT`, `ELAPSED_MS`, `LAST_HEARTBEAT_AT`, `REQUESTED_TARGET`, `TARGET_TABLE` |
| Trace and outcomes | Run/batch/retry IDs, step/attempt, row counts, `QUERY_ID`, error fields |

The same setup script adds the 16 new nullable fields if the earlier single-table
version was already created. It requires ALTER privileges for that upgrade and
preserves existing rows; historical metadata stays NULL rather than being invented.
The verification script inspects all columns automatically. This is still one table.

Populate source scope from the validated source manifest, not the current wall-clock
date. Imported runs with no declared date filter retain NULL bounds; group arrays
are the declared/validated scope, not proof of access. Source instance is a configured
identifier, not a URL containing credentials. Version/hash fields identify the actual
saved artifacts and executed code. Read Snowflake context from the active session,
not from guessed profile values. `FILE_SIZE_BYTES` uses the same uncompressed bytes
as `FILE_SHA256`; `ELAPSED_MS` is measured per operation, and heartbeat is optional
progress metadata, not a commit indicator. These operational fields may repeat on
step rows; the RUN summary supplies complete invocation context.

Each row describes one step for a run, optionally linked to a file. `LOAD_RUN_ID`
groups the entire invocation. `AUDIT_ID` uniquely identifies a row; the loader
must enforce this logical uniqueness. It inserts a RUNNING row before work and
updates that same row when the outcome is known. A retry gets a new row/attempt
number; a new invocation gets a new run ID and an optional retry-of link.

| LOAD_RUN_ID | STEP_NAME | FILE_NAME | TARGET_TABLE | STATUS | ROWS_INSERTED |
| --- | --- | --- | --- | --- | --- |
| example-run | RUN | NULL | NULL | SUCCESS | NULL |
| example-run | LOAD_RAW_EVENTS | events-001.jsonl.gz | RAW.NCFTA_EVENTS | SUCCESS | 10 |
| example-run | LOAD_RAW_RESULTS | results-001.jsonl.gz | RAW.NCFTA_MODEL_RESULTS | SUCCESS | 85 |
| example-run | CURATE | NULL | REFINED.NCFTA_FIELDS | SUCCESS | 85 |

These are illustrative rows, not loaded data. A single `RUN` summary row records
whether the requested target was reached, including failure before any file was
processed. Batch-wide steps have NULL filenames. For per-file work, use the
relative staged path so shards with identical basenames remain distinguishable.

The loader/procedures must implement these rules:

- Step names: `RUN`, `VALIDATE`, `UPLOAD`, `LOAD_RAW_EVENTS`, `LOAD_RAW_RESULTS`,
  `CURATE`, `PUBLISH_APP`, `CLEANUP`. Statuses: `RUNNING`, `SUCCESS`, `NO_OP`,
  `PARTIAL`, `FAILED`, `CANCELLED`, `INTERRUPTED`, `SKIPPED`, `VALIDATED`.
  Validate these values and positive attempt numbers in application logic.
- Keep row counts beside their target table. Never sum events, attributes and
  multiple processing layers into one total. The RUN summary's row-count
  columns stay NULL; layer/file summaries can be recorded in `DETAILS`.
- `ROWS_READ` counts parsed input rows; inserts/updates count committed target
  changes; unchanged means matched but not changed; rejected means records
  refused by validation. Record nonnegative counts only when known. For a
  confirmed rollback, committed inserts/updates are zero. For an ambiguous
  commit outcome they remain NULL until reconciled. No fabricated zeros.
- Log each independently committed operation separately. `QUERY_ID` records
  its main Snowflake query; additional query IDs may be included in `DETAILS`.
- `FILE_SHA256` hashes the named artifact's uncompressed bytes before upload.
  Original input hashes also remain in RAW batch metadata. If the audit row names
  the compressed staged file, keep its precompression artifact identity in
  `DETAILS`; never substitute Snowflake content keys for SHA-256 hashes.
- `DETAILS` is restricted to supplementary operational metadata: last completed
  step, file role, additional hashes/query IDs and counts by layer. Prefer the
  explicit columns above for their named metadata instead of duplicating values.
  Do not put source field values, event bodies, passwords, API keys or tokens in it.
- A RUN is SUCCESS only after its requested target commits. PARTIAL means an
  earlier layer committed but the requested target failed. Cleanup failures are
  separate CLEANUP rows and must not imply committed data was rolled back.
- Record sanitized error codes/messages after rolling back the failed data
  transaction, outside that transaction. Failed audit writes or failures before
  Snowflake login require a local sanitized attempt log for reconciliation.
  A process crash can leave RUNNING; verify commit state before retrying or
  marking it INTERRUPTED. A stale heartbeat alone does not establish rollback.

Table creation does not yet implement these audit writes. No audit data has
been loaded or migrated by these scripts.

## 3. REFINED - 118 catalog fields plus 33 technical columns

`03_refined.sql` creates **`REFINED.NCFTA_FIELDS`**, with 151 columns total.
`03_verify_refined.sql` inspects its definition and reports missing/unexpected
columns, wrong types/nullability, incorrect bounded VARCHAR lengths and numeric
precision/scale. Its last query should return zero mismatches; inspect DESCRIBE
for default expressions too. No source data is read by this verification.

The setup is CREATE IF NOT EXISTS, never CREATE OR REPLACE. If an older draft
table already exists, this script leaves it intact and the verification identifies
differences. It does not silently alter types, rebuild the table or load data.

The 118 business columns are generated in the exact order of
`assets/field-catalog.json`, catalog 2.0.0. The catalog SHA-256 is pinned in
`build_refined_schema.py`; a changed catalog must be reviewed before regeneration.
The original mapping workbook and the catalog are not edited.

| Column category | Count | Storage |
| --- | ---: | --- |
| Model extraction fields | 116 | ARRAY of literal text values |
| `OBSERVATION_FIRST_SEEN`, `OBSERVATION_LAST_SEEN` | 2 | VARCHAR containing original source metadata |
| Identity, lineage, versions, deletion, outcomes and tracking | 33 | Technical columns |

There is one row per saved RAW attribute outcome and transformation version.
The logical key is `(RESULT_ID, TRANSFORM_VERSION)`. Retrying that same key must
not create another row; different content under the same key must fail validation.
A new RAW observation or transformation version is retained as a separate row.
Unchanged observations from different extraction runs can therefore be present
in this evidence table; the app history/delta procedure must distinguish those
from actual source changes. This table does not itself choose the current record.

The 33 technical columns preserve the RAW result/batch/event links; source
namespace, UUIDs and original IDs; sharing/distribution/type/category metadata;
source/model/catalog hashes; saved prediction identity; transformation version;
source deletion flags; outcome/review status; accepted/rejected span counts;
multivalue fields/warnings; and the creating audit run/time.

Transformation rules to implement later:

- Join each RAW result to exactly one batch and event snapshot. Validate source
  namespace, model/catalog hashes and identity before writing. Use explicit
  column names, never positional INSERT SELECT *. Missing source UUIDs stay NULL;
  preserve original IDs and the saved review key without inventing NCFTA UUIDs.
- Populate the 116 arrays only from saved accepted spans after rechecking field
  names, literal text and offsets against the saved source value. Sort occurrences
  by source start/end with saved span order as the stable tie-breaker. Preserve
  repeated values, leading zeros, currency spelling, accents and original text.
  Do not infer account ownership or combine unrelated attributes into a person.
- `OUTCOME_STATUS = 'predicted'` with no accepted match for a field gives `[]`.
  Deleted, invalid-source, empty-value or input-error outcomes give SQL NULL
  model-field arrays; never copy older predictions onto a deletion notice.
  Arrays must contain strings only, validated by the procedure: untyped ARRAY
  storage alone does not enforce element types. Rejected spans remain in RAW.
- Copy observation first_seen/last_seen from source metadata, not model spans
  or load time. Preserve source text without assuming a timezone. Missing values
  remain NULL; malformed non-text metadata is a validation issue, not an invented date.
- Keep attribute and parent-object deletion signals independently, including
  NULL for absent flags. Their effective OR is IS_DELETED. Missing data and
  deletion proposals do not establish deletion. A latest-active app view must
  choose the latest supported source observation before excluding deleted rows;
  ingestion time alone must never resurrect an older active record.
- Keep non-predicted outcomes for reconciliation. Zero evaluated attributes is
  not automatically a failed import. Counts must reconcile to saved outcomes,
  not just to attributes with extracted values. Technical span counts are zero
  when the saved outcome contains no spans. WARNINGS and MULTIVALUE_FIELDS are
  arrays, including [] when empty, and never manufacture extracted values.
- Default REVIEW_STATUS is NOT_REVIEWED. The existence of an Excel workbook does
  not mean it was approved. Excel edits need the separately defined review import.
- Log writes through the single AUDIT_LAYER.NCFTA_LOAD_AUDIT table. Procedures
  implement transactions, duplicate checks and failure reporting as described below.

These are storage contracts, not implemented transformation procedures. No data
has been populated and no Snowflake server compilation has been performed.

## Remaining layers

1. History/delta view rules, including source ordering and review change workflow.
2. Optional stage retention/cleanup policy and production throughput improvements.
3. Snowflake integration validation before production data loads.

## 5. RAW loading procedure (prepared, not deployed)

`05_load_raw.sql` defines `RAW.SP_LOAD_NCFTA_RAW(BATCH VARIANT, FILES ARRAY,
LOAD_RUN_ID VARCHAR)`. `raw_loader.py` is the tested handler;
`build_raw_procedure.py` embeds it into the deployable SQL. After deploying,
`05_verify_load_raw.sql` inspects the definition without loading data.
The procedure uses Python 3.12 and Snowpark, runs as the caller and uses the
existing stage/file format. Snowflake package availability and privileges have
not been verified. It does not upload files, invoke models or touch Excel.

Call outside an existing transaction, with a fresh UUID for LOAD_RUN_ID, and
only one writer at a time. There is no concurrent-writer uniqueness guarantee.
Use menu option 8 or the local uploader described below; do not pass existing Excel/JSON files
directly to this procedure. It requires the following package contract:

- BATCH contains exactly the 18 metadata keys in `raw_loader.metadata`: source
  run/instance/manifest, original artifact/model/catalog hashes, prediction identity,
  threshold, source group IDs/date range and four expected counts. Dates are ISO
  dates or NULL. The completed manifest must report fetch_complete and
  inference_complete true and phase complete. Original file hashes are supplied
  by the local package builder; this procedure cannot re-hash original local files.
- Batch fingerprint is SHA-256 of canonical BATCH JSON (sorted keys, UTF-8,
  ensure_ascii=False, compact separators). Batch UUID is UUID5(NAMESPACE_URL,
  'ncfta:' + fingerprint). The complete immutable manifest participates in this
  identity; changing it means a different package. Retries reuse the same metadata.
- Event JSONL contains one unwrapped event object per line. Results JSONL contains
  exactly `{ "source_record": <saved record>, "prediction": <saved prediction> }`
  per line. No values are reconstructed from Excel. Original saved records stay
  intact even where their normalized empty values differ from raw null/missing.
- FILES is an array of `{path, kind, rows, canonical_sha256}` descriptors, where
  kind is events or results and path is `<batch_uuid>/<kind>/<safe_name>.jsonl`
  or `.jsonl.gz`. SHA-256 covers each parsed row's canonical JSON plus newline,
  in file row order; it is not the compressed file hash. Numeric serialization
  must survive the JSON stage parser or integrity verification rejects the file.
  Empty batches use no file descriptors, not empty files. All listed nonempty
  files must have positive expected row counts. Unlisted files are ignored.

The procedure reads only those exact filenames, validates canonical checksums,
counts, unique event/attribute identities, source values, saved input context,
prediction identity and source/deletion joins. SOURCE_VALUE is derived from the
original event attribute, preserving NULL versus empty string and leading zeros.
Non-string values remain in original JSON; SOURCE_VALUE stays NULL. The procedure
stores all saved outcomes; field-level span acceptance is the REFINED procedure's
responsibility. Missing records never become inferred deletion notices.

Only after validation does it atomically insert the batch, events and results.
A previously registered identical package returns NO_OP after comparing stored
evidence. Missing/conflicting rows or orphan rows cause failure, not silent repair.
It does not overwrite old RAW observations. Package paths/content keys may change
on retry; original load lineage remains. New extraction batches preserve history.

The same transaction finalizes success/no-op audit rows. On error, it rolls back
data and writes sanitized failure audit rows separately. One RUN summary row and
two batch-level LOAD_RAW_EVENTS/LOAD_RAW_RESULTS rows are used; per-file upload
auditing remains the uploader's job. RUN counts stay NULL; event/result counts
remain separate. Audit rows include session identity, batch/filter/model metadata,
timing, status and last successful data query ID. Exception messages/source values
are withheld. The return object must be checked: a SQL CALL completing does not
mean status SUCCESS. Audit write failure returns audit_status WRITE_FAILED.
Initial invalid/reused run IDs or an existing transaction raise before audit setup.

This first implementation is deliberately bounded to 100 files, 50,000 combined
event/result rows and 64 MiB canonical payload per call. It performs row inserts
inside one transaction and is intended for modest PoC batches, not bulk throughput.
Unexpected termination can leave RUNNING audit rows; reconcile them before retrying.
Local tests cover validation, retry conflicts, rollback and sanitized failures with
a transaction test double. They do not compile SQL or prove Snowflake permissions,
stage metadata behavior, runtime availability or performance. Live validation is
still required; no deployment or data load has been performed.

## 6. REFINED transformation procedure (prepared, not deployed)

`06_populate_refined.sql` defines
`REFINED.SP_POPULATE_NCFTA_REFINED(SOURCE_BATCH_ID VARCHAR, LOAD_RUN_ID VARCHAR)`.
Run the definition after provisioning layers 1, 2 and 3; inspect it with
`06_verify_populate_refined.sql`. No real data is transformed by those definition
scripts. The handler is `refined_loader.py`; `build_refined_procedure.py` embeds
the validated RAW helpers and pinned 151-column schema in the SQL. No local file
imports are needed on the Snowflake server. Regenerate the SQL if helpers change.

Call with the SOURCE_BATCH_ID returned by the RAW load. LOAD_RUN_ID may be the
same pipeline run ID if no CURATE step has used it yet; a retry needs a fresh ID.
The procedure adds one CURATE audit row, without rewriting the RAW RUN summary.
The suite uploader owns the overall multi-step run summary.
Caller privileges must include RAW SELECT, REFINED SELECT/INSERT and audit
SELECT/INSERT/UPDATE, plus normal database/schema/warehouse usage. Python 3.12
and the Snowpark package must be available. Call outside a transaction and run
only one writer at a time; this remains a bounded PoC, not a concurrent bulk loader.

Behavior:

- Read and revalidate the complete RAW batch, original event attributes and saved
  prediction joins/hashes. Require the pinned catalog checksum before mapping.
- Map 116 model fields from accepted predictions only. Validate exact Unicode
  source substrings, input/value offsets and known field keys. A corrupt accepted
  span fails the batch; rejected spans remain rejected and are counted, not promoted.
  No reclassification, confidence rescore, inference or model execution occurs.
- Preserve repeated occurrences in start/end/saved-order sequence. Arrays contain
  literal strings, including leading zeros. [] means evaluated with no match.
  Deleted and unevaluated outcomes keep lineage with NULL model arrays. Metadata
  first_seen/last_seen fills the remaining two fields as supplied text; non-string
  metadata yields NULL plus a warning, never an invented timezone or value.
- Preserve source IDs, nullable deletion flags, effective IS_DELETED and saved
  warnings. New rows start NOT_REVIEWED. Existing review status is never reset.
- Logical key is RESULT_ID plus the fixed code version `refined-v1`. Identical rows
  are skipped, conflicting values fail, and missing rows can be inserted. New RAW
  observations remain separate; unchanged observations are not automatically deltas.
  A future behavior change must bump TRANSFORM_VERSION to preserve history.
- Commit new rows and successful audit counts together. Roll back on failure and
  record a sanitized failure separately. Audit counts refer to attribute rows,
  not extracted spans. Read/expected counts are NULL when not yet known. Check the
  returned status even when CALL itself succeeds; FAILED is not success.

The procedure does not populate REPORTING or construct an active/latest view.
It uses in-memory validation with bounded RAW reads and row-wise inserts. Local
tests exercise spans, repeated values, catalog drift, source corruption, deletion,
retry/no-op, conflict detection, review preservation and rollback using a test
double. The embedded Python compiles locally. Snowflake SQL/runtime, privileges,
stage-to-RAW integration and performance remain unverified until a live smoke test.

## 7. App publishing procedure (prepared, not deployed)

`07_populate_app.sql` defines
`REPORTING.SP_POPULATE_NCFTA_APP(SOURCE_BATCH_ID VARCHAR, LOAD_RUN_ID VARCHAR)`.
It publishes the complete `refined-v1` batch into the 29-business-field/62-total-column
app history table using mapping version `app-v1`. Run the definition only after the
tables are provisioned; `07_verify_populate_app.sql` inspects it without publishing.
The local implementation is `app_loader.py`, with pinned mapping/schema constants
in `app_contract.py`. `build_app_procedure.py` embeds all required Python code and
contracts in the SQL. It refuses a changed mapping checksum. Mapping changes must
be explicitly reviewed and versioned; the preserved workbook is not edited.

The procedure revalidates the original RAW batch and accepted spans, then compares
each saved REFINED row with that evidence. Missing, duplicate or conflicting rows
fail publication; it does not silently publish a partial batch. It does not call
the model or join attributes into inferred people/accounts. Technical fields and
source roles carry through; repeated values retain their individual provenance.

Mapping details:

- Direct fields follow the v2 mapping: Bank Address uses bank_location, Submitted
  By uses reporter, Dollar Amount uses reported_amount, and company/merchant/target
  remain separate. No inferred ownership, arithmetic, currency, date conversion,
  protocol, country code or number conversion is introduced.
- IP publishes source_ip only for the sheet's ip-src source type.
- URL combines accepted url/domain spans in source order. FIELD_PROVENANCE records
  each value's kind, original field, offsets, prediction index and app array index;
  a domain remains a domain with no protocol added.
- Comments copies the complete raw value only for comment/Other in source groups
  6, 7 or 8. It never substitutes the attribute metadata comment.
- Merchant Category copies raw value for the sheet's group-8 comment/Targeting data
  row. Otherwise, an accepted merchant_category_code span is published only when
  it is four ASCII digits and explicitly labelled MCC/merchant category code in
  the source comment or immediately preceding value text. Unconfirmed candidates
  are withheld with a warning. This checks source evidence, not a live MCC registry.
  Raw category text takes precedence over its extracted MCC representation to
  avoid duplicating the same source as both raw text and a code.
- Deleted or unevaluated outcomes retain IDs/flags with NULL business arrays and
  empty provenance lists. Evaluated fields with no applicable match are [].

Rows use `(CURATED_ROW_ID, APP_MAPPING_VERSION)`. New observations and mapping
versions remain separate history; retries skip identical rows. Conflicts fail
without overwriting. The review status is copied at initial publication; if it
later differs on retry, APP_REVIEW_STATUS_CHANGED requires an explicit review
workflow rather than silently updating or misreporting the app status as current.

Use one writer, outside an existing transaction, with caller RAW/REFINED SELECT,
REPORTING SELECT/INSERT, audit SELECT/INSERT/UPDATE and required usage privileges.
Python 3.12/Snowpark availability and account permissions still need live checking.
The same pipeline LOAD_RUN_ID is accepted if it has no PUBLISH_APP step yet;
retries use a fresh ID. One PUBLISH_APP audit row records metadata, counts, version,
timing and query ID. The orchestrator remains responsible for the overall RUN row.
Success audit and data inserts commit together. Failure rolls back all inserts
and writes a sanitized failure audit separately; audit-write errors are explicit
in the returned object. Always inspect the returned status.

This remains a bounded PoC using in-memory validation and row-wise inserts, with
50,000 rows/64 MiB limits per input table read. It creates neither an active/latest
view nor a cross-run delta view. The local tests cover mapping exceptions, evidence
links, deletion, missing/conflicting input, retries, review changes, and rollback
after an earlier insert succeeds. The embedded Python compiles and is exercised
locally, but Snowflake SQL execution and integration have not been tested live.

Existing `ALERT_*` objects and the preserved mapping workbook are not modified.
The earlier combined design under `outputs/ncfta-snowflake-design-2026-11-19`
remains a reference draft; these numbered scripts are the layer-by-layer build.

## 8. Suite integration (local development build)

`snowflake_loader.py` implements menu option 8 and `suite.py snowflake`. It chooses
completed parent runs only; group subfolders are rejected to prevent loading the
same data twice. It verifies event/record checksums, prediction identity, joins,
counts and the pinned catalog, then writes canonical JSONL packages under
`<run>/snowflake-loads/packages/<batch_uuid>/`. Original JSON and Excel are unchanged.
`--package-only` performs this validation/packaging without network access.

The existing Snowflake CLI profile is used with externalbrowser authentication.
The CLI opens the browser and the loader waits for completion without a short
login timeout. The profile controls account/user/role and the default warehouse;
an optional warehouse can be configured. Preflight checks the active warehouse,
expected table column counts and procedure names visible to the role. It does not
prove every column type, procedure version, signature or grant; run the numbered
verification scripts and a live integration check before relying on the pipeline.
Each CLI command opens a connection; additional SSO prompts depend on the account's
connection caching policy. No password collection, certificate changes or token
caching policy changes are made by the suite.

Uploads use exact batch-scoped filenames with AUTO_COMPRESS=FALSE; canonical file
hashes therefore also match the uncompressed uploaded bytes. UPLOAD audit entries
precede the RAW call under the same LOAD_RUN_ID. The current RAW procedure permits
only those earlier upload entries, then creates the RUN/details rows. The suite
keeps RUN active across later steps and finalizes it after APP, or marks the overall
invocation failed/interrupted when a later step stops. Already committed earlier
layers remain intact; a later attempt with a fresh run ID safely retries them.
Rows/counts for events and predictions remain distinct in the detailed audit;
the terminal RAW count combines those two and excludes batch-registration rows.

Only successful/NO_OP procedure responses with matching batch and run IDs advance
to the next layer. CLI output and raw exception bodies are never copied into the
local report. Errors before login can only be recorded locally. A lost response or
interruption is not proof of server rollback; inspect the server audit before retry.
Staged files are retained, as are the immutable local packages. No original source
file is moved, truncated, deleted or reconstructed from Excel.

The implementation is not deployed or release-packaged and has not used a live
Snowflake account. Local tests mock the CLI and database; deployment/SQL/runtime,
work-PC SSO, CLI JSON formatting and actual privileges require an integration test.
