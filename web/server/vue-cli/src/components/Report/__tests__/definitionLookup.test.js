import { EditorState } from "@codemirror/state";
import { cpp } from "@codemirror/lang-cpp";

import {
  candidateLabel,
  clampLine,
  collapseCandidates,
  createNavigationTracker,
  createRequestSequence,
  definitionResultKind,
  isDefinitionClick,
  isDefinitionLookupSupported,
  isMacPlatform,
  symbolAt
} from "../definitionLookup";

// Minimal stand-in for the Thrift i64 (node-int64) values.
class Int64 {
  constructor(value) { this.value = value; }
  toNumber() { return this.value; }
}

function symbolIn(doc, needle, offset = 0) {
  const state = EditorState.create({ doc, extensions: [ cpp() ] });
  const pos = doc.indexOf(needle);
  if (pos === -1) throw new Error(`"${needle}" not in "${doc}"`);
  const symbol = symbolAt(state, pos + offset);
  return symbol ? symbol.name : null;
}

describe("symbolAt", () => {
  test.each([
    [ "int x = foo();", "foo", "foo" ],
    [ "int x = a::foo();", "foo", "foo" ],
    [ "int x = a::foo();", "a::", "a" ],
    [ "int x = a::b::bar();", "bar", "bar" ],
    [ "void g() { obj.foo(); }", "foo", "foo" ],
    [ "void g() { ptr->foo(); }", "foo", "foo" ],
    [ "struct Vec { int x; };", "Vec", "Vec" ],
    [ "Vec w;", "Vec", "Vec" ],
    [ "#define FOO(x) x\n", "FOO", "FOO" ],
    [ "int y = FOO(1);", "FOO", "FOO" ],
    [ "S::~S() {}", "~S", "~S" ],
    [ "struct S {\n  ~S();\n};", "~S", "~S" ]
  ])("%j at %j -> %j", (doc, needle, expected) => {
    expect(symbolIn(doc, needle)).toBe(expected);
  });

  test("click inside the identifier", () => {
    expect(symbolIn("int x = foobar();", "foobar", 3)).toBe("foobar");
  });

  test("click at the end boundary of the identifier", () => {
    expect(symbolIn("int x = foo();", "foo", 3)).toBe("foo");
    expect(symbolIn("int foo", "foo", 3)).toBe("foo");
  });

  test.each([
    [ "// foo()\n", "foo" ],
    [ "/* foo */\n", "foo" ],
    [ "const char *s = \"foo\";", "foo" ],
    [ "char c = 'f';", "f" ],
    [ "#include \"foo.h\"\n", "foo" ],
    [ "#define FOO(x) x + foo\n", "foo" ],
    [ "struct Vec { int x; };", "struct" ],
    [ "int x = 42;", "42" ],
    [ "int x  =  1;", "  =", 1 ]
  ])("%j at %j -> null", (doc, needle, offset = 0) => {
    expect(symbolIn(doc, needle, offset)).toBeNull();
  });

  test("returns null when the tree is not available in time", () => {
    const doc = "int f0;\n".repeat(200000) + "int last;";
    const state = EditorState.create({ doc, extensions: [ cpp() ] });
    expect(symbolAt(state, doc.length - 2, 1)).toBeNull();
  });
});

describe("isDefinitionLookupSupported", () => {
  test.each([
    "a.c", "/src/a.cpp", "a.cc", "a.cxx", "a.C", "a.CPP", "a.c++",
    "inc/a.h", "a.hpp", "a.hh", "a.hxx", "a.H", "a.tcc"
  ])("%s is supported", path => {
    expect(isDefinitionLookupSupported(path)).toBe(true);
  });

  test.each([
    "a.py", "a.java", "a.js", "a.ts", "a.go", "a.m", "a.mm", "a.txt",
    "Makefile", "CMakeLists.txt", "a.Cpp", ".h", "", null, undefined
  ])("%s is not supported", path => {
    expect(isDefinitionLookupSupported(path)).toBe(false);
  });
});

describe("click detection", () => {
  const click = mods => ({
    button: 0, ctrlKey: false, metaKey: false, altKey: false,
    shiftKey: false, ...mods
  });

  test("Ctrl+click on non-Mac, Cmd+click on Mac", () => {
    expect(isDefinitionClick(click({ ctrlKey: true }), false)).toBe(true);
    expect(isDefinitionClick(click({ metaKey: true }), false)).toBe(false);
    expect(isDefinitionClick(click({ metaKey: true }), true)).toBe(true);
    expect(isDefinitionClick(click({ ctrlKey: true }), true)).toBe(false);
  });

  test("plain, right, Alt and Shift clicks are ignored", () => {
    expect(isDefinitionClick(click({}), false)).toBe(false);
    expect(isDefinitionClick(click({ button: 2, ctrlKey: true }), false))
      .toBe(false);
    expect(isDefinitionClick(click({ ctrlKey: true, altKey: true }), false))
      .toBe(false);
    expect(isDefinitionClick(click({ ctrlKey: true, shiftKey: true }),
      false)).toBe(false);
  });

  test("platform detection", () => {
    expect(isMacPlatform({ platform: "MacIntel" })).toBe(true);
    expect(isMacPlatform({ platform: "Linux x86_64" })).toBe(false);
    expect(isMacPlatform({ platform: "Win32" })).toBe(false);
  });
});

function candidate(fields) {
  return {
    fileId: new Int64(4),
    filePath: "/p/shared.h",
    line: new Int64(10),
    kind: "struct",
    language: "c",
    endLine: new Int64(10),
    scope: null,
    scopeKind: null,
    signature: null,
    ...fields
  };
}

describe("collapseCandidates", () => {
  test("C and C++ entries of the same target become one group", () => {
    const groups = collapseCandidates([
      candidate({ language: "c++" }),
      candidate({ language: "c" })
    ]);
    expect(groups).toHaveLength(1);
    expect(groups[0].languages).toEqual([ "c", "c++" ]);
    expect(groups[0].fileId.toNumber()).toBe(4);
  });

  test.each([
    [ "signature", { signature: "(double x)" } ],
    [ "scope", { scope: "a" } ],
    [ "scopeKind", { scope: null, scopeKind: "namespace" } ],
    [ "endLine", { endLine: new Int64(12) } ],
    [ "line", { line: new Int64(11) } ],
    [ "fileId", { fileId: new Int64(5) } ],
    [ "kind", { kind: "typedef" } ]
  ])("different %s stays separate", (_, fields) => {
    const groups = collapseCandidates([
      candidate({}),
      candidate({ language: "c++", ...fields })
    ]);
    expect(groups).toHaveLength(2);
  });

  test("keeps server order of first occurrences", () => {
    const groups = collapseCandidates([
      candidate({ line: new Int64(4), signature: "(int x)" }),
      candidate({ line: new Int64(5), signature: "(double x)" }),
      candidate({ line: new Int64(4), signature: "(int x)",
        language: "c++" })
    ]);
    expect(groups.map(g => g.line.toNumber())).toEqual([ 4, 5 ]);
  });

  test("empty input", () => {
    expect(collapseCandidates([])).toEqual([]);
  });
});

describe("candidateLabel", () => {
  test("scope and signature", () => {
    const [ group ] = collapseCandidates([ candidate({
      filePath: "/p/x.h", kind: "function", scope: "a",
      signature: "(int x)", language: "c++"
    }) ]);
    expect(candidateLabel("foo", group)).toEqual({
      title: "a::foo(int x)",
      subtitle: "function · x.h:10 · c++",
      tooltip: "/p/x.h"
    });
  });

  test("no scope, no signature, multiple languages", () => {
    const groups = collapseCandidates([
      candidate({}), candidate({ language: "c++" })
    ]);
    expect(candidateLabel("shared_s", groups[0])).toEqual({
      title: "shared_s",
      subtitle: "struct · shared.h:10 · c, c++",
      tooltip: "/p/shared.h"
    });
  });

  test("signature without scope", () => {
    const [ group ] = collapseCandidates([ candidate({
      kind: "function", signature: "(double x)", language: "c++"
    }) ]);
    expect(candidateLabel("over", group).title).toBe("over(double x)");
  });
});

describe("definitionResultKind", () => {
  test("0 / 1 / N", () => {
    expect(definitionResultKind([])).toBe("none");
    expect(definitionResultKind(collapseCandidates([
      candidate({}), candidate({ language: "c++" })
    ]))).toBe("navigate");
    expect(definitionResultKind(collapseCandidates([
      candidate({}), candidate({ line: new Int64(11) })
    ]))).toBe("pick");
  });
});

describe("clampLine", () => {
  test("clamps into the document", () => {
    expect(clampLine(new Int64(5), 10)).toBe(5);
    expect(clampLine(new Int64(0), 10)).toBe(1);
    expect(clampLine(new Int64(99), 10)).toBe(10);
    expect(clampLine(3, 10)).toBe(3);
  });
});

describe("createRequestSequence", () => {
  test("only the latest request is current", () => {
    const seq = createRequestSequence();
    const first = seq.next();
    const second = seq.next();
    expect(seq.isLatest(second)).toBe(true);
    expect(seq.isLatest(first)).toBe(false);
  });
});

describe("createNavigationTracker", () => {
  test("a newer definition navigation supersedes an older one", () => {
    const nav = createNavigationTracker();
    const a = nav.startDefinitionNavigation();
    const b = nav.startDefinitionNavigation();
    expect(a.isCurrent()).toBe(false);
    expect(a.ownsView()).toBe(false);
    expect(b.isCurrent()).toBe(true);
    expect(b.ownsView()).toBe(true);
  });

  test("a report switch supersedes a pending definition navigation", () => {
    const nav = createNavigationTracker();
    const jump = nav.startDefinitionNavigation();
    const report = nav.startReportLoad();
    expect(jump.isCurrent()).toBe(false);
    expect(jump.ownsView()).toBe(false);
    expect(report.isCurrent()).toBe(true);
  });

  test("Back supersedes a pending jump started from a destination", () => {
    const nav = createNavigationTracker();
    const first = nav.startDefinitionNavigation();
    expect(first.isCurrent()).toBe(true);
    const second = nav.startDefinitionNavigation();
    const back = nav.startReportLoad();
    expect(second.isCurrent()).toBe(false);
    expect(back.isCurrent()).toBe(true);
    expect(back.ownsView()).toBe(true);
  });

  test("a newer definition navigation supersedes a pending report load", () => {
    const nav = createNavigationTracker();
    const report = nav.startReportLoad();
    const jump = nav.startDefinitionNavigation();
    expect(report.isCurrent()).toBe(false);
    expect(jump.isCurrent()).toBe(true);
  });

  test("a newer lookup supersedes a navigation, not a report load", () => {
    const nav = createNavigationTracker();
    const report = nav.startReportLoad();
    const jump = nav.startDefinitionNavigation();
    const lookup = nav.startLookup();
    expect(jump.isCurrent()).toBe(false);
    // No newer source replacement started, so the jump clears loading.
    expect(jump.ownsView()).toBe(true);
    expect(lookup.isCurrent()).toBe(true);

    const nav2 = createNavigationTracker();
    const report2 = nav2.startReportLoad();
    nav2.startLookup();
    expect(report2.isCurrent()).toBe(true);
    expect(report.isCurrent()).toBe(false);
  });

  test("a report switch supersedes a pending lookup", () => {
    const nav = createNavigationTracker();
    const lookup = nav.startLookup();
    nav.startReportLoad();
    expect(lookup.isCurrent()).toBe(false);
  });

  test("invalidate() supersedes every pending intent", () => {
    const nav = createNavigationTracker();
    const lookup = nav.startLookup();
    const jump = nav.startDefinitionNavigation();
    const report = nav.startReportLoad();
    nav.invalidate();
    expect(lookup.isCurrent()).toBe(false);
    expect(jump.isCurrent()).toBe(false);
    expect(report.isCurrent()).toBe(false);
  });
});

// Mirrors the guarded flow of Report.vue: an async source load may only
// commit while its intent is current, and only the latest source
// replacement clears the loading state.
describe("guarded navigation with late responses", () => {
  function deferred() {
    let resolve;
    const promise = new Promise(r => { resolve = r; });
    return { promise, resolve };
  }

  function createView() {
    const nav = createNavigationTracker();
    const view = { shown: "report-1", back: false, loading: false };

    async function load(intent, source, pending, back) {
      view.loading = true;
      try {
        await pending;
        if (!intent.isCurrent()) return;
        view.shown = source;
        view.back = back;
      } finally {
        if (intent.ownsView()) view.loading = false;
      }
    }

    return {
      view,
      jump: (source, pending) =>
        load(nav.startDefinitionNavigation(), source, pending, true),
      report: (source, pending) => {
        view.back = false;
        return load(nav.startReportLoad(), source, pending, false);
      }
    };
  }

  test("report switch wins over a late definition response", async () => {
    const { view, jump, report } = createView();
    const a = deferred();
    const pendingA = jump("target_a", a.promise);
    await report("report-2", Promise.resolve());
    expect(view).toEqual({ shown: "report-2", back: false, loading: false });
    a.resolve();
    await pendingA;
    expect(view).toEqual({ shown: "report-2", back: false, loading: false });
  });

  test("newer jump wins over a late older jump", async () => {
    const { view, jump } = createView();
    const a = deferred();
    const pendingA = jump("target_a", a.promise);
    await jump("target_b", Promise.resolve());
    a.resolve();
    await pendingA;
    expect(view).toEqual({ shown: "target_b", back: true, loading: false });
  });

  test("Back wins over a late destination response", async () => {
    const { view, jump, report } = createView();
    await jump("target_b", Promise.resolve());
    const late = deferred();
    const pending = jump("util_h", late.promise);
    await report("report-1", Promise.resolve());
    expect(view).toEqual({ shown: "report-1", back: false, loading: false });
    late.resolve();
    await pending;
    expect(view).toEqual({ shown: "report-1", back: false, loading: false });
  });

  test("an old completion does not clear loading of a newer one", async () => {
    const { view, jump } = createView();
    const a = deferred();
    const b = deferred();
    const pendingA = jump("target_a", a.promise);
    const pendingB = jump("target_b", b.promise);
    a.resolve();
    await pendingA;
    expect(view.loading).toBe(true);
    b.resolve();
    await pendingB;
    expect(view).toEqual({ shown: "target_b", back: true, loading: false });
  });
});
