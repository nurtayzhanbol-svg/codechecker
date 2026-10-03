# Symbol index and jump-to-definition: decision log

Final decision log of the implemented feature. See
[architecture.md](architecture.md) for the resulting design.

Status vocabulary:

- **ACCEPTED** – agreed design decision.
- **IMPLEMENTED** – present in the feature code.
- **EXPERIMENTALLY VERIFIED** – supported by a recorded experiment or test,
  not only by reasoning.
- **DEFERRED** – deliberately left for future work.

| ID | Decision | Status |
|---|---|---|
| D1 | Analyzer-side indexing | ACCEPTED, IMPLEMENTED |
| D2 | Compiler dependency discovery | ACCEPTED, IMPLEMENTED, EXPERIMENTALLY VERIFIED |
| D3 | `-M` + Policy C | ACCEPTED, IMPLEMENTED, EXPERIMENTALLY VERIFIED |
| D4 | `SymbolIndex` identity `(content_hash, language)` | ACCEPTED, IMPLEMENTED, EXPERIMENTALLY VERIFIED |
| D5 | No compiler flags forwarded to Ctags | ACCEPTED, IMPLEMENTED |
| D6 | `symbols.json` format and normalization | ACCEPTED, IMPLEMENTED |
| D7 | New tables and `RunSymbolFile` | ACCEPTED, IMPLEMENTED |
| D8 | Stage A / Stage B storage split | ACCEPTED, IMPLEMENTED, EXPERIMENTALLY VERIFIED |
| D9 | Startup GC with Option SR | ACCEPTED, IMPLEMENTED, EXPERIMENTALLY VERIFIED |
| D10 | Current-run definition lookup | ACCEPTED, IMPLEMENTED, EXPERIMENTALLY VERIFIED |
| D11 | API returns candidates, not a resolved answer | ACCEPTED, IMPLEMENTED |
| D12 | No source `fileId` filter | ACCEPTED, IMPLEMENTED |
| D13 | Deterministic total order before `LIMIT` | ACCEPTED, IMPLEMENTED, EXPERIMENTALLY VERIFIED |
| D14 | Lezer syntax-tree identifier extraction | ACCEPTED, IMPLEMENTED, EXPERIMENTALLY VERIFIED |
| D15 | Ctrl/Cmd-click trigger | ACCEPTED, IMPLEMENTED |
| D16 | Frontend collapse of identical C/C++ targets | ACCEPTED, IMPLEMENTED, EXPERIMENTALLY VERIFIED |
| D17 | One-level *Back to report* | ACCEPTED, IMPLEMENTED; full history DEFERRED |

---

## D1 – Analyzer-side indexing

- **Problem:** where to run Ctags.
- **Alternatives:** (a) during `CodeChecker analyze`; (b) on the server after
  `store`, from the uploaded sources.
- **Choice:** (a), opt-in with `--symbol-index`.
- **Evidence:** only the `BuildAction` knows the language each file was
  compiled as and which headers a translation unit uses; the stored
  `File`/`FileContent` rows carry no language. A server-side indexer would
  need a new language transport and a server Ctags dependency.
- **Trade-off:** the analyzing machine needs Universal Ctags with JSON
  support; the store ZIP grows by `symbols.json` and the indexed headers.

## D2 – Compiler dependency discovery

- **Problem:** which files to index. Definitions usually live in headers,
  and a `BuildAction` only names its source file.
- **Alternatives:** sources only; every file in the repository; sources plus
  the headers the compiler actually includes.
- **Choice:** the source plus `tu_collector.get_dependent_headers()` on
  `action.original_command` (compiler `-M`).
- **Evidence:** reuses an existing CodeChecker mechanism that asks the real
  compiler; upstream's later relative `-include` normalization of
  `analyzer_options` did not affect it (merge E2E with `-include forced.h`).
- **Trade-off:** C/C++ specific (other languages need another input
  provider); one extra preprocessor run per build action; headers not
  included by any analyzed TU are not indexed.

## D3 – `-M` + Policy C

- **Problem:** `-M` reports all system headers (e.g. 35 707 raw references
  on the ctags corpus).
- **Alternatives:** `-MM`; path prefix denylist (`/usr/**`); Policy C.
- **Choice:** keep `-M`, then drop files under the compiler's implicit
  include directories unless an explicit `-I/-isystem/-iquote/-idirafter`
  directory is an equally or more specific match.
- **Evidence:** `-MM` is include-chain based, not path based: it dropped
  project headers given via `-isystem` and headers reached through a system
  header chain (GCC 11 and clang 15). On the corpus Policy C kept 436
  inputs, including 21 `/usr/include/libxml2` headers requested with `-I`.
- **Trade-off:** depends on the compiler probe (`ImplicitCompilerInfo`); the
  tu_collector behaviour for other features is unchanged.

## D4 – `SymbolIndex` identity = `(content_hash, language)`

- **Problem:** what makes two Ctags results reusable.
- **Alternatives:** file path; content hash; content hash + language.
- **Choice:** `(content_hash, language)`.
- **Evidence:** the same header tagged as C and as C++ can yield different
  tags; byte-identical copies at different paths yield identical tags. The
  shared header of the acceptance fixture is stored as two indexes
  (`shared.h` C and C++).
- **Trade-off:** a shared header has two backend candidates per name
  (handled by D16); one per-path index would duplicate identical content.

## D5 – No compiler flags forwarded to Ctags

- **Problem:** should `-D`, `-I`, `-std` reach Ctags.
- **Choice:** no; Ctags receives the file list and `--language-force`.
- **Evidence:** Ctags does not preprocess and gives these options other
  meanings (`-I` is an identifier ignore list).
- **Trade-off:** all `#if/#else` branches are indexed; lookup is
  candidate-based, not configuration-accurate.

## D6 – `symbols.json` format and normalization

- **Problem:** the contract between analyze and store.
- **Choice:** application-owned, versioned (`version: 1`) JSON:
  indexes `{content_hash, language, paths[], definitions[]}`, definitions
  `{name, kind, line, end_line?, scope?, scope_kind?, signature?, typeref?}`.
  Reference tags are dropped, no column is stored, indexes and definitions
  are totally ordered (None-safe), so identical input gives identical bytes.
- **Evidence:** analyzer unit tests for determinism and None-safe ordering;
  shared validator `codechecker_common/symbols_json.py` used by store and
  server.
- **Trade-off:** pretty-printed JSON is large (5.6 MiB for 20 k definitions,
  0.36 MiB gzip); fields Ctags offers beyond the schema are lost.

## D7 – New tables and `RunSymbolFile`

- **Problem:** persist indexes and say which ones a run uses.
- **Alternatives:** columns on `File`; per-run copies of definitions;
  global indexes plus a membership table.
- **Choice:** `symbol_indexes` (UNIQUE `content_hash, language`),
  `symbol_definitions`, `run_symbol_files (run_id, file_id,
  symbol_index_id)`; cascading deferrable FKs; migration `a7c2d5e9f1b3`.
- **Evidence:** `File` cannot represent "indexed once as C and once as C++
  in this run"; `RunHistory` is not the owner of current run state.
  Migration round-trip and ORM/schema equality tested on SQLite; upgrade on
  PostgreSQL 16 and 18.
- **Trade-off:** one more table to keep consistent; the invariant
  `File.content_hash == SymbolIndex.content_hash` is enforced by Stage B,
  not by the database.

## D8 – Stage A / Stage B storage split

- **Problem:** global deduplicated data vs. run-scoped data in one store.
- **Choice:** Stage A inserts missing indexes in batched transactions before
  the run lock/transaction (retry on conflicts); Stage B replaces the run's
  memberships as a set inside the run transaction, validating every row
  against persisted data.
- **Evidence:** concurrency experiment: two parallel stores of the same
  content produced exactly one index and one definition set; an index whose
  `FileContent` was missing was skipped while the rest committed; ~20 k
  definitions stored in 0.87 s on SQLite.
- **Trade-off:** a crash between the stages may leave unused indexes until
  GC (harmless).

## D9 – Startup GC with Option SR

- **Problem:** new tables must not leak or keep data forever, and must not
  delete content a resolved report still displays.
- **Choice:** at server start: files live via report paths or memberships;
  indexes live via memberships or a live `File` with the same content
  (Option SR); contents live via files, analysis info or indexes.
- **Evidence:** GC scenario harness (delete run, re-store, unchanged store,
  restart) on real databases; without the GC change memberships' files
  were collected while the run existed.
- **Trade-off:** retained old-version indexes occupy space until their file
  is gone; GC only runs at server start (existing CodeChecker behaviour).

## D10 – Current-run definition lookup

- **Problem:** which definitions to show when the displayed source is an old
  version (resolved report).
- **Choice:** always query `RunSymbolFile` of the report's run: the current
  definitions of the run.
- **Evidence:** functional API test and final history check: after
  re-storing a run with a changed `capi.c`, `c_divide` (v1 only) returns
  `[]`, `c_api` points to its new line.
- **Trade-off:** a jump from an old source may land in a different version
  of the file.

## D11 – API returns candidates, not a resolved answer

- **Problem:** the server cannot know which overload or `#if` branch the
  user means.
- **Choice:** `getDefinitionCandidates` returns a list; the client chooses.
- **Evidence:** overloads (`geo::scale(int)` / `(double)`), conditional
  definitions (`mode_value` twice) and C/C++ headers produce several
  legitimate rows.
- **Trade-off:** the UI must offer a picker.

## D12 – No source `fileId` filter

- **Problem:** should the request say which file the click came from.
- **Choice:** no; inputs are `runId`, `symbolName`, optional `limit`.
- **Reason:** a name-based index cannot use the file to resolve semantics
  correctly, and the displayed file may not be in the run's current set.
- **Trade-off:** no "same file first" preference on the server.

## D13 – Deterministic total order before `LIMIT`

- **Problem:** `filepath, language, line` alone can tie (several
  definitions on one line, the same path as two `File` rows).
- **Choice:** `ORDER BY filepath, language, line, kind, File.id,
  SymbolDefinition.id`, `LIMIT` after ordering.
- **Evidence:** tests on same-line ties and limit; repeated queries return
  identical lists on SQLite and PostgreSQL 18.
- **Trade-off:** it is transport stability, not relevance ranking.

## D14 – Lezer syntax-tree identifier extraction

- **Problem:** which word did the user click.
- **Alternatives:** CodeMirror `wordAt`; regex; the Lezer C++ syntax tree.
- **Choice:** the node at the click, accepted only for `Identifier`,
  `FieldIdentifier`, `TypeIdentifier`, `NamespaceIdentifier`,
  `DestructorName`; bounded parse (100 ms); both sides of a boundary.
- **Evidence:** `wordAt` returns words inside comments and strings; 71 unit
  tests; browser runs sent no request for comments/strings/includes.
  Ctags 5.9 and 6.2 emit destructors as `~S`, matching `DestructorName`.
- **Trade-off:** names inside `#define` bodies (`PreprocArg`) are not
  clickable.

## D15 – Ctrl/Cmd-click trigger

- **Problem:** how to start a lookup without breaking selection.
- **Alternatives:** F12 (opens DevTools in browsers), context menu (none
  exists in the viewer), Ctrl/Cmd-click.
- **Choice:** primary-button Ctrl-click (Cmd on macOS) in C/C++ files; the
  Ctrl/Cmd-click multi-cursor is disabled only in those files.
- **Trade-off:** no keyboard trigger yet.

## D16 – Frontend collapse of identical C/C++ targets

- **Problem:** a shared header returns one candidate per language that
  points to the same place.
- **Choice:** the frontend merges candidates equal in
  `fileId, line, endLine, kind, scope, scopeKind, signature`, keeps server
  order and lists all languages; the API stays unchanged.
- **Evidence:** `shared_s` (C + C++ rows) navigates directly; candidates that
  differ in any other field stay separate (unit tests).
- **Trade-off:** collapsing is a presentation rule, other clients see both
  rows.

## D17 – One-level *Back to report*

- **Problem:** returning from a definition.
- **Choice:** an explicit flag shows *Back to report* after a successful
  jump; it re-runs the report initialization. Destination is not in the URL.
- **Evidence:** browser E2E: same-file and cross-file return, refresh
  returns to the report.
- **Trade-off:** no multi-step history (DEFERRED). Because it reuses the
  report initialization, it shares the pre-existing report-tree behaviour
  where bug-path arrows may be missing until *Show arrows* is toggled; an A/B
  check showed the same behaviour on the Phase 3 frontend, so it is not
  addressed here.
