# Storage, snapshots, and cache semantics

SQLite is the product boundary: collection may contact a provider, while dashboard inspection,
export, and MCP reads consume the saved database. They do not revisit X.

The default database is `var/x_collection_workbench.db`. SQLite runs in WAL mode, enables foreign
keys, and uses an owner-private file on POSIX systems. Keep the database, `-wal`/`-shm` companions,
and migration backups together when making an offline copy.

## Snapshot model

A collection job becomes a snapshot when it reaches a terminal state. Stored state includes:

- the normalized request and exact approved execution plan;
- provider/parser versions, source and request fingerprints, timestamps, and stop reason;
- ordered Post observations with capture segment, scan ordinal, DOM position, and observed time;
- available text, author, timestamp, relationship, metric, and media fields;
- partial/truncated state, field coverage, warnings, and provider resource counts where applicable.

Post identity is unique within a snapshot. `snapshot_position` is stable insertion order;
`source_position` remains a compatibility name for the observed DOM position. Missing values stay
null. Capture batches and their checkpoint metadata commit atomically, so a later failure cannot
publish a half-written batch.

Successful and partial snapshots with at least one row and valid stored metadata are considered
usable. Failed, cancelled, and interrupted jobs may preserve observations for inspection but are
not automatically promoted to usable evidence.

## Freshness and reuse

The versioned storage model records a snapshot time and `stale_after_seconds` (default 86400, one
day), then reports `fresh`, `stale`, or `unknown` from age. Source fingerprints group the same
normalized source; request fingerprints additionally bind the exact request budget and filters.

This metadata is not an automatic cache hit. The application does not silently substitute an old
snapshot, skip a newly approved request, or refresh in the background. Browser Home snapshots are
marked ineligible for reuse because the source is inherently volatile. Other sources can be
identified as compatible, but a caller must deliberately choose a stored result. A stale label is
descriptive, not a statement that the underlying X content changed.

There is no scheduled retention or automatic purge. The explicit local purge API requires
confirmation and keeps the configured number of newest terminal snapshots per logical source
(default 10); it deletes matching observations, FTS rows, and jobs atomically. Nonterminal jobs are
never selected. Decide and document your own retention period for sensitive local content.

## Search and export

The database maintains a Unicode FTS5 text index with snapshot observations. Search treats the
normalized terms as a quoted literal AND query, supports bounded source/snapshot/time filters, and
orders by relevance then stable snapshot/position identity. Results carry evidence IDs, original
URLs where safe, snapshot/source provenance, freshness, and partial/truncated metadata. Search does
not perform a provider request.

The local export endpoint supports JSON and CSV. Exports include public provenance and partial
state; official-API resource/cost fields appear only where applicable. CSV values that could become
spreadsheet formulas are prefixed defensively. Opening an export in another program creates a new
trust boundary, so retain normal spreadsheet and untrusted-text precautions.

`Changes` compares two usable snapshots with the same source fingerprint and compatible parser
versions. It scans at most 500 observations per snapshot and reports newly observed, reobserved, and
not observed in the newer bounded sample, plus comparable engagement deltas and deterministic
author/hashtag/link-domain/term changes. Missing metrics are never treated as zero. “Not observed”
is explicitly not a deletion claim, and every category reports partial/truncated sample limits.

## Schema and migrations

The current development schema is family `x_collection_workbench`, version 4. Initialization is
idempotent. Compatible versions 1-3 are migrated inside a transaction only after a protected
SQLite backup passes `PRAGMA integrity_check`; the backup name records the source and destination
versions. An existing backup target, incompatible family, schema/index drift, symlink, or non-file
database fails closed rather than being guessed or overwritten.

The migration preserves legacy observations and rebuilds versioned metadata/indexes. If migration
fails, preserve the original and backup, record sanitized diagnostics, and do not manually edit
`schema_meta` to force acceptance.

For exact migration, rollback, corruption, and data-boundary evidence, see
[verification](verification.md). Configuration paths and permissions are in
[configuration](configuration.md).
