# Study guide: symbol index and jump-to-definition

A reading order through the implementation, from the analyzer to the browser.
Read [architecture.md](architecture.md) first for the big picture. For each
step: what problem the code solves, the few functions worth understanding,
what you can safely skip, a likely defense question and one exercise.

## 1. `analyzer/codechecker_analyzer/symbol_index.py`

**Problem solved.** Turn the build actions of an analysis into a minimal,
deduplicated set of `(file, language)` inputs and run Universal Ctags on them.

**Understand.**
- `collect_inputs_for_action()` — source + compiler `-M` dependencies of one
  `BuildAction`.
- `is_indexable_dependency()` with `get_implicit_include_dirs()` /
  `get_explicit_include_dirs()` — Policy C.
- `group_by_identity()` — `(content_hash, language)` grouping.
- `build_indexes()` / `normalize_tag()` — one Ctags run per language and the
  tag normalization.

**Don't memorize.** The Ctags command-line field list, the multiprocessing
plumbing in `collect_inputs()`, the exact log messages.

**Defense question.** Why ask the compiler for dependencies instead of
scanning `#include` lines? (It applies `-I`, `-D`, `-include`, and conditional
includes exactly like the build.)

**Exercise.** Add `-isystem vendor/` to a lab compile command and predict
which headers Policy C keeps; check with `CodeChecker analyze --symbol-index`
and `symbols.json`.

## 2. `codechecker_common/symbols_json.py`

**Problem solved.** One validated, versioned format shared by the analyzer
(writer), `CodeChecker store` and the server (readers).

**Understand.**
- `SymbolIndex` and `Definition` dataclasses — the in-memory model.
- `parse()` / `_parse_definition()` — what is rejected (version, types,
  required fields).
- `load()`.

**Don't memorize.** Individual `_expect()` messages.

**Defense question.** Why is the schema versioned? (So a server can refuse a
document it does not understand instead of storing garbage.)

**Exercise.** Change `"version"` to 2 in a `symbols.json` and run
`CodeChecker store`; note where it fails.

## 3. `web/client/codechecker_client/cli/store.py` — symbol transport

**Problem solved.** Ship clean headers: the store ZIP must contain every file
in `symbols.json`, not only files referenced by reports.

**Understand.**
- `collect_symbol_indexed_files()` — reads `symbols.json`, returns its paths.
- The place where those paths are added to the files to compress and to
  `content_hashes.json`.

**Don't memorize.** ZIP layout details unrelated to symbols, skipped-file
statistics.

**Defense question.** What happens if a header changes between analyze and
store? (The stored content hash differs from the `symbols.json` hash; the old
index has no matching `FileContent` and is skipped.)

**Exercise.** Store a run whose `symbols.json` references a deleted file and
read the warning.

## 4. `web/server/codechecker_server/database/run_db_model.py` — symbol tables

**Problem solved.** Persist indexes once per content+language, and record
per run which file was indexed as which language.

**Understand.**
- `SymbolIndex` — unique `(content_hash, language)`, FK to `FileContent`.
- `SymbolDefinition` — definitions of one index; index on `name`.
- `RunSymbolFile` — `(run_id, file_id, symbol_index_id)`.

**Don't memorize.** Constraint and index names, column lengths.

**Defense question.** Why not store the language on `File`? (`File` is
path+content; the same file can be read as C and as C++ in one run.)

**Exercise.** Draw the four tables (`File`, `FileContent`, `SymbolIndex`,
`RunSymbolFile`) for `shared.h` included from one `.c` and one `.cpp` file.

## 5. Migration `a7c2d5e9f1b3_add_symbol_index_tables.py`

**Problem solved.** Create the three tables on existing product databases.

**Understand.** `upgrade()` and `downgrade()`; the foreign key to
`file_contents` and the cascade from `runs`.

**Don't memorize.** Alembic boilerplate.

**Defense question.** Is the migration safe for old runs? (Yes: tables are
new; old runs simply have no memberships and the API returns `[]`.)

**Exercise.** Run the migration down and up on a copy of a lab database and
confirm old reports still open.

## 6. `web/server/codechecker_server/api/symbol_index_store.py`

**Problem solved.** Stage A global persistence, Stage B run membership, and
the run-scoped query.

**Understand.**
- `load_symbol_indexes()` — merges `symbols.json` files of a store.
- `store_symbol_indexes()` / `_store_batch()` — insert-if-absent with retry.
- `replace_run_symbol_files()` — membership validation inside the run
  transaction.
- `find_definitions()` — the join chain and total `ORDER BY`.

(Wiring: `MassStoreRun` in `mass_store_run.py`; GC: `db_cleanup.py`.)

**Don't memorize.** Statistics dataclasses, batch size, retry log strings.

**Defense question.** Why is Stage A outside the run lock? (Large inserts would
otherwise block other stores; Stage A is idempotent, so a retry is safe.)

**Exercise.** Store the same project twice and compare the Stage A log lines
(`created` vs `reused`).

## 7. `codechecker_api/report_server.thrift` and the handler

**Problem solved.** A public, versioned way to ask for candidates.

**Understand.**
- `struct DefinitionCandidate` and `getDefinitionCandidates` in the Thrift
  file.
- `ThriftRequestHandler.getDefinitionCandidates()` in
  `web/server/codechecker_server/api/report_server.py`: permission check,
  validation, `find_definitions()`, conversion.

**Don't memorize.** Generated binding code, version negotiation internals.

**Defense question.** Why does an unknown run return `[]` instead of an
error? (Avoids an extra query; "nothing to navigate to" is the same UI either
way.)

**Exercise.** Call the method from a Python Thrift client with `limit=1` for
an overloaded name and check which candidate is returned.

## 8. `web/server/vue-cli/src/components/Report/definitionLookup.js`

**Problem solved.** All decisions that can be tested without a browser.

**Understand.**
- `symbolAt()` — Lezer node under the click; accepted node names.
- `isDefinitionClick()` / `isMacPlatform()`.
- `collapseCandidates()` and `definitionResultKind()` — 0/1/N.
- `createRequestSequence()`.

**Don't memorize.** The extension list and label formatting.

**Defense question.** Why not a regex/`wordAt`? (It returns words inside
comments and strings; the syntax tree does not.)

**Exercise.** Add a unit test for clicking a name inside a `#define` body and
explain the result.

## 9. `web/server/vue-cli/src/components/Report/Report.vue` integration

**Problem solved.** Connect the helpers to CodeMirror, the API and the
existing source/bug-path rendering.

**Understand.**
- `onEditorMouseDown()` — gate, extraction, multi-cursor suppression.
- `goToDefinition()` — request, stale-response check, 0/1/N.
- `navigateToDefinition()` — `setSourceFileData`, `drawBugPath`, clamped
  `jumpTo`.
- `backToReport()` / `resetDefinitionNavigation()`.

**Don't memorize.** Template/SCSS layout of the menu and button.

**Defense question.** Why redraw the bug path after replacing the file?
(Editor decorations are reset by the new document, but jsPlumb arrows are DOM
outside the editor and must be cleared/redrawn explicitly.)

**Exercise.** Jump to a definition, toggle git blame, and explain why the view
stays on the definition (the blame scroll guard).
