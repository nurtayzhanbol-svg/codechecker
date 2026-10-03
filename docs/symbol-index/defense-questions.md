# Defense questions

Short answers; details in [decisions.md](decisions.md) (D-numbers) and
[how-it-works.md](how-it-works.md).

**1. Why Universal Ctags instead of a Clang AST?**
Ctags is fast (≈ 0.5 s for 436 files), robust on code that does not compile in
isolation, needs no build of an AST per TU, and is language-agnostic. The
feature is a name-based navigation aid; a Clang/USR source could be added
later behind the same API (D1, D11).

**2. Why index on the analyzer side?**
Only the analyzer knows the build actions: the effective language and the
compiler command that determines which headers are read. The server sees
neither (D1).

**3. Why compiler dependency discovery?**
`#include` resolution depends on `-I`, `-D`, `-include` and conditional
includes. Asking the compiler (`tu_collector`, `-M`) gives exactly the files
the build reads (D2).

**4. Why `-M` instead of `-MM`?**
`-MM` omits headers found via system-header semantics, which also drops
explicitly requested `-isystem` project/vendor headers. `-M` lists everything;
Policy C then removes only implicit compiler/system directories (D3).

**5. Why is `SymbolIndex` keyed by content hash + language?**
The same content gives the same tags for the same Ctags language, so it is
indexed once, across paths, runs and stores. Language is part of the key
because a header read as C and as C++ can produce different tags (D4).

**6. Why not one index per path?**
Paths are not stable identities: the same path changes content between runs,
and identical content appears under different paths (vendored copies, build
trees). Per-path indexes would duplicate work and break reuse (D4).

**7. Why `RunSymbolFile`?**
It records, per run, which stored `File` was indexed as which language.
`File` has no language, and `SymbolIndex` is shared by runs, so membership
must be a separate run-scoped table. It is what makes lookup current-run
only (D7, D10).

**8. Why does the API return a list?**
A name can have several valid definitions: overloads, `#if/#else` branches,
the same header as C and C++, unrelated files. The server reports them; the
client chooses (D11).

**9. Why no source file ID parameter?**
It would only matter for ranking, which the server does not do. Leaving it out
keeps the API a pure function of `(runId, name)` and cacheable (D12).

**10. How are overloads handled?**
Each overload is a separate candidate with its signature. The UI shows a
chooser; the user picks. No overload resolution (D11, D16).

**11. How are `#if/#else` definitions handled?**
Ctags does not evaluate the preprocessor with build macros, so both branches
are indexed and both are offered. For review this is correct: the stored code
can be read in either configuration (D5).

**12. What if a file changes between analyze and store?**
`CodeChecker store` hashes the file at store time. The `symbols.json` hash no
longer matches, so that index has no `FileContent`, Stage A skips it and
Stage B rejects the membership. Navigation simply lacks that file (D8).

**13. Old resolved source vs. current definitions?**
The viewer shows the report's stored (possibly old) source, but lookup uses
the run's current memberships, so definitions point at the latest stored
version. Old `SymbolIndex` rows retained by GC Option SR are not members of
the run and cannot leak (D9, D10).

**14. Why deterministic ordering?**
A `LIMIT` over an unstable order returns arbitrary rows; tests and clients
need repeatable results. The order (path, language, line, kind, file ID,
definition ID) is total; it is transport stability, not relevance (D13).

**15. Why Lezer instead of a regex?**
The syntax tree distinguishes identifiers from comments, strings, includes,
keywords and numbers, and handles `a::b`, `obj.f`, `p->f`. A regex/`wordAt`
returns words in comments and strings (D14).

**16. Why Ctrl/Cmd-click?**
It is the convention of IDEs and code browsers, does not conflict with
selection or plain clicks, and the viewer had no context menu. F12 opens
browser DevTools (D15).

**17. Shared C/C++ header?**
The backend returns two candidates (C and C++) with identical location and
metadata. The frontend collapses them into one target and shows both
languages, so the click navigates directly (D16).

**18. How does GC behave?**
At server start: a `File` is live if a report path or a run membership uses
it; a `SymbolIndex` is live if a membership uses it or a live `File` has its
content (Option SR); a `FileContent` is live if a `File`, analysis info or a
`SymbolIndex` refers to it (D9).

**19. What does it cost?**
On the Ctags corpus (255 actions): ≈ 2 s extra analysis time, `symbols.json`
5.5 MiB (361 KiB gzip), first store +≈ 7 s (mostly shipping the 436 indexed
source files), unchanged re-store ≈ baseline, lookup median 3–4 ms over HTTP.

**20. What would adding Python require?**
An analyzer-side input source (Python has no `BuildAction` language/compiler
dependency list — e.g. project files from the analyzer's input), a Ctags
language mapping, a frontend extension gate and a Lezer Python grammar with
its identifier node names, and a decision on module-qualified names. The DB
schema and API need no change: `language` is already a free string.

**21. Why is the Back button only one level?**
It covers the main need (return to the report after inspecting a definition)
with no new state model. Full history and URL persistence are future work
(D17).
