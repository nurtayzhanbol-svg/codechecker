# How jump-to-definition works

This is the thesis-oriented explanation of the symbol index and
jump-to-definition feature. The reference design is in
[architecture.md](architecture.md); the rationale for each choice is in
[decisions.md](decisions.md) (cited below as D1–D17).

## Table of Contents

1. [Problem](#problem)
2. [Why CodeChecker did not already have it](#why-not-already)
3. [Phase 1 — collection and indexing](#phase-1)
4. [Phase 2 — persistence](#phase-2)
5. [Phase 3 — API](#phase-3)
6. [Phase 4 — frontend](#phase-4)
7. [End-to-end example](#example)
8. [Ambiguity handling](#ambiguity)
9. [Failure handling](#failure)
10. [Limitations](#limitations)
11. [Performance and evaluation](#performance)
12. [Future work](#future-work)

## 1. Problem <a name="problem"></a>

A CodeChecker report shows a bug path through source files. To judge a report,
a reviewer usually needs to look at code that is *not* on the bug path: the
body of a called function, the layout of a struct, the value of a global. Before
this feature the report viewer could only show files that the report touched,
and the reviewer had to switch to a local checkout or IDE to follow a name.

The goal is: Ctrl-click (Cmd-click on macOS) an identifier in the report
viewer and land on its definition, using only what was stored for the run.

## 2. Why CodeChecker did not already have it <a name="why-not-already"></a>

- **The server stored only the files a report touched.** `CodeChecker store`
  shipped the source files referenced by reports. A definition in a clean
  header with no finding never reached the server.
- **The server knows nothing about languages or builds.** It receives plist
  results and files; it does not see the compilation database, compiler flags
  or the language each file was compiled as.
- **Analyzers do not export a symbol table.** Clang Static Analyzer and
  clang-tidy produce diagnostics, not a cross-reference database. The CTU
  machinery produces a USR→AST mapping only for function definitions and only
  for internal use during analysis.
- **The viewer had no symbol model.** The CodeMirror 6 editor was used as a
  read-only highlighted view; nothing mapped a click to a name.

So the feature needed new data (definitions and the files containing them),
new storage, a new API and new UI. The design keeps these as four separable
phases.

## 3. Phase 1 — collection and indexing <a name="phase-1"></a>

`CodeChecker analyze --symbol-index` (and `check --symbol-index`) writes
`symbols.json` into the report directory.

1. Each `BuildAction` from `compile_commands.json` already carries the source
   file, the effective language (`c` / `c++`) and the original compiler
   command (D1).
2. The compiler is asked which files the translation unit reads:
   `tu_collector.get_dependent_headers()` runs the original command with `-M`
   (D2, D3). This sees exactly the headers the build sees, including ones
   selected by `-D`, `-I`, `-include`.
3. **Policy C** drops headers below the compiler's implicit system include
   directories, but keeps everything under explicitly requested `-I`,
   `-isystem`, `-iquote`, `-idirafter` paths (D3).
4. Every `(path, language)` input is hashed (SHA-256 of the content, the same
   hash `FileContent` uses) and grouped by `(content_hash, language)` (D4). A
   header included from 100 C++ files is indexed once as C++.
5. Universal Ctags is run once per language over the unique files, with
   `--language-force` and no compiler flags (D5).
6. Tags are normalized (`name`, `kind`, `line`, `end_line`, `scope`,
   `scope_kind`, `signature`, `typeref`) and written deterministically as
   `symbols.json` version 1 (D6).

## 4. Phase 2 — persistence <a name="phase-2"></a>

- `CodeChecker store` adds `symbols.json` and every indexed file to the store
  ZIP, so clean headers reach the server.
- The server has three new tables (D7):

  | Table | Identity | Meaning |
  |---|---|---|
  | `symbol_indexes` (`SymbolIndex`) | `(content_hash, language)` | "this content, read as this language, was indexed" — product-global, shared by runs |
  | `symbol_definitions` (`SymbolDefinition`) | row of a `SymbolIndex` | one Ctags definition |
  | `run_symbol_files` (`RunSymbolFile`) | `(run_id, file_id, symbol_index_id)` | "in this run, this path was indexed as this language" |

- **Stage A** (`store_symbol_indexes`) runs after source files are stored and
  outside the run lock: insert-if-absent of indexes and definitions in
  batches, retrying unique/lock conflicts (D8). An unchanged re-store
  inserts nothing.
- **Stage B** (`replace_run_symbol_files`) runs inside the run's store
  transaction: replace the run's membership rows, after validating path,
  content hash and index existence.
- **GC** at server start keeps a `SymbolIndex` if any run membership uses it,
  or if any live `File` has its content hash (Option SR, D9). `FileContent`
  is kept while a `SymbolIndex` refers to it.

## 5. Phase 3 — API <a name="phase-3"></a>

`codeCheckerDBAccess.getDefinitionCandidates(runId, symbolName, optional limit)`
returns `list<DefinitionCandidate>` (API version 6.75, D11).

```
runId + symbolName
  → RunSymbolFile (run_id = runId)        current-run scoping (D10)
  → SymbolIndex                           language
  → SymbolDefinition (name = symbolName)  line, kind, scope, signature
  → File                                  fileId, filePath
ORDER BY filepath, language, line, kind, File.id, SymbolDefinition.id  (D13)
LIMIT limit (if given)
```

- There is no source `fileId` parameter (D12): the result does not depend on
  where the click happened.
- Empty name → `RequestFailed`; unknown name, unknown run, or a run stored
  without `symbols.json` → `[]`.

## 6. Phase 4 — frontend <a name="phase-4"></a>

In `Report.vue`, with helpers in `definitionLookup.js`:

1. Only files with a C/C++ extension take part.
2. A primary-button click with Ctrl (Cmd on macOS) is intercepted, and
   CodeMirror's "add cursor" behaviour is suppressed for that click (D15).
3. `symbolAt()` asks the Lezer C++ syntax tree for the leaf node at the click
   position and accepts only identifier nodes (D14). Comments, strings,
   includes, keywords and macro bodies yield nothing and send no request.
4. `getDefinitionCandidates(report.runId, name)` is called. A request
   sequence number discards responses that arrive after a newer click.
5. Candidates that differ only in language are collapsed (D16).
6. 0 groups → disabled "No definition found"; 1 group → navigate;
   N groups → anchored chooser.
7. Navigation: `setSourceFileData(fileId)`, `drawBugPath()`, then
   `jumpTo(clampLine(line))`. A *Back to report* button re-runs the normal
   report initialisation (D17).

## 7. End-to-end example <a name="example"></a>

The final acceptance fixture (C file `capi.c`, C++ files `main.cpp`,
`geometry.cpp`, headers `shared.h`, `config.h`, `geometry.hpp`) was analyzed
with `--symbol-index`: 3 build actions → 8 unique `(content_hash, language)`
indexes → 34 definitions. After `CodeChecker store` the run has 6 `File`
rows and 8 `RunSymbolFile` rows (`shared.h` and `config.h` each appear as C
and as C++).

The null-dereference report is on `main.cpp:22`. Line 21 reads:

```cpp
  int value = helper(c_api(1));
```

Ctrl-clicking `c_api`:

1. `symbolAt()` returns `c_api` (`Identifier` node).
2. `getDefinitionCandidates(1, "c_api")` → one candidate:
   `capi.c`, line 11, end line 15, `function`, `c`, signature `(int x)`.
3. One group → `setSourceFileData(<capi.c fileId>)`, bug-path redraw,
   `jumpTo(11)`. The header now shows `capi.c`; *Back to report* returns to
   `main.cpp:22`.

Note that `c_api` in the comment on line 8 and in the string on line 33 is
not a request at all.

## 8. Ambiguity handling <a name="ambiguity"></a>

Name-based lookup is ambiguous by nature; the system makes the ambiguity
explicit instead of guessing.

| Case | Backend rows | UI |
|---|---|---|
| Overload `geo::scale(int)` / `geo::scale(double)` | 2 | chooser with both signatures |
| `#if USE_FAST … #else … #endif` `mode_value` in `config.h`, included from C and C++ | 4 (lines 6 and 8, each C and C++) | chooser with 2 entries, each labelled `c, c++` |
| `struct shared_s` in `shared.h` included from C and C++ | 2 | 1 group → direct jump |
| Same name in unrelated files (`main` in four tools of the Ctags corpus) | 4 | chooser |

Ctags indexes both branches of a conditional, because it does not evaluate
the preprocessor with the build's macros (D5). Showing both is correct for a
review tool: the stored run may have been built in either configuration.

The server never ranks; ordering is a stable transport order (D13). Ranking
belongs to the client, and the current client only collapses exact duplicates.

## 9. Failure handling <a name="failure"></a>

| Failure | Behaviour |
|---|---|
| Ctags missing with `--symbol-index` | `analyze` stops before the analysis starts, with an error (fail early instead of after a long analysis) |
| Ctags failing during index generation | the analysis results are kept; an error is logged, no `symbols.json` is written and `analyze` exits with status 1 |
| Header discovery fails for one action | a warning is logged; that action contributes only its source file |
| `symbols.json` malformed / wrong version | `CodeChecker store` refuses to upload it (error, exit 1, "re-run analyze --symbol-index or remove the file"); the server also validates and aborts such a store |
| An indexed file changed between analyze and store | its hash no longer matches; the index for the old content has no `FileContent` and is skipped in Stage A; membership validation rejects it in Stage B |
| Concurrent stores of the same content | unique constraint + retry; exactly one `SymbolIndex` survives |
| Run stored without symbols | API returns `[]`; UI shows "No definition found" |
| Thrift error during lookup | standard error snackbar; menu and loading state are reset |
| Response arrives after a newer click | discarded by request sequence |
| Candidate line beyond the file end | clamped |

## 10. Limitations <a name="limitations"></a>

- Name-based, not semantic: no overload resolution, no template or ADL
  reasoning, no distinction between a local and a global with the same name.
- Only C and C++ (frontend extension gate and analyzer language mapping).
  Objective-C extensions are deliberately excluded.
- Identifiers inside `#define` bodies are not navigable (Lezer represents
  them as an unparsed argument).
- Definitions in system headers are not indexed (Policy C).
- One level of *Back to report*; no navigation history; the destination is
  not stored in the URL, so a page reload returns to the report.
- Only the run of the open report is searched.
- Pre-existing, not introduced by this feature: when selecting another report
  in the report tree (and therefore also after *Back to report*, which uses
  the same initialisation), bug-path arrows can be missing until *Show
  arrows* is toggled or the page is reloaded. Reproduced identically on the
  Phase 3 frontend (before this feature's UI) and on Phase 4.
- Also pre-existing and identical on the Phase 3 and Phase 4 frontends in the
  acceptance environment: the git blame gutter rendered blank although the
  server returned blame data. Jump-to-definition only guarantees that toggling
  blame after a jump does not scroll back to the report line.

## 11. Performance and evaluation <a name="performance"></a>

Measurements on one machine (8 cores, Linux, Python 3.11, SQLite unless
noted, Universal Ctags 6.2.0). Corpus: the Universal Ctags source tree,
255 C build actions. Store/analysis times from log timestamps have 1 s
resolution.

### Final measurements (2026-10-03, commit `317930657`, 8 jobs)

| Measurement | Result |
|---|---|
| A. analyze without `--symbol-index` | ≈ 116 s |
| B. analyze with `--symbol-index` | ≈ 118 s |
| C. symbol-index generation (log: "Generating…" → "written") | ≈ 2 s |
| D. store without `symbols.json` | ≈ 7 s |
| E. first store with `symbols.json` | ≈ 14 s (server: source files 5.20 s, Stage A 0.79 s) |
| F. unchanged re-store with `symbols.json` | ≈ 7 s (server: source files 1.61 s, Stage A 0.16 s, 0 definitions inserted) |
| G. `symbols.json` size | 5,724,517 B raw; 370,034 B gzip |
| H. counts | 255 build actions; 436 unique `(content_hash, language)` indexes; 20,501 definitions; 436 physical files |
| I. API latency, 300 requests each (Python Thrift client, HTTP) | `parseTagRegex` (1 candidate): median 3.4 ms, p95 6.0 ms, max 10.4 ms; `main` (4 candidates): median 4.1 ms, p95 5.9 ms, max 8.6 ms; `no_such_symbol_xyz` (0): median 3.1 ms, p95 4.2 ms, max 6.8 ms |

A − B is within run-to-run noise of the analyzers; the symbol index cost is
the ≈ 2 s generation step (C). The store overhead (E − D) is dominated by
shipping and storing the 436 indexed source files, not by the symbol tables.

### Historical measurements (kept for comparison, not re-run)

| Source | Setup | Result |
|---|---|---|
| Phase 1 report | same corpus, 4 jobs, Ctags 6.2, timing inside `symbol_index.generate` | dependency discovery 1.77 s, hash/group 0.01 s, Ctags 0.48 s, total 2.78 s; `symbols.json` 5,590 KiB, gzip 364 KiB |
| Phase 2 report | same corpus, SQLite | store baseline 7.28 s, first store with symbols 13.60 s, unchanged re-store 7.29 s; DB growth +0.52 MiB baseline vs +4.25 MiB with symbols |
| Phase 2 PostgreSQL validation | PostgreSQL 16, same corpus | Stage A/B 2.15 s first, 0.06 s reused; lookup round trip 10–13 ms; `EXPLAIN ANALYZE` ≈ 0.97 ms |

### Functional evaluation (2026-10-03)

- Clean-room fixture on SQLite and PostgreSQL 18.6: store succeeded; the
  queries run on both backends (`c_api`, `scale`, `shared_s`, `mode_value`,
  `undefined_function_xyz`) returned the same rows in the same order, and
  repeated queries returned the same order.
- Current-run isolation: after re-storing a changed project, a function that
  existed only in the old version returns `[]`; its replacement returns the
  new line.
- Browser acceptance A–K: see the final acceptance report.

## 12. Future work <a name="future-work"></a>

- Client-side ranking (same file first, matching language first) on top of
  the unchanged candidate list.
- Navigation history and URL-addressable destinations.
- Keyboard trigger (e.g. a dedicated shortcut) for accessibility.
- Identifiers in macro bodies via a word-based fallback restricted to
  `#define` lines.
- More languages: needs an analyzer-side language/dependency source, a Ctags
  language mapping, and a frontend tokenizer and extension gate (see the
  defense questions).
- Semantic resolution (e.g. Clang USRs) as an optional, more precise
  candidate source behind the same API.
