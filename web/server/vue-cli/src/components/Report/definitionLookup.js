import { ensureSyntaxTree, syntaxTree } from "@codemirror/language";

// C/C++ source and header extensions, as listed by the analyzer
// (compilation_database.C_CPP_OBJC_OBJCPP_EXTS without Objective-C, plus
// analyze.header_file_extensions). Matching is case-sensitive like there.
const C_CPP_EXTENSIONS = new Set([
  ".c", ".i", ".ii", ".cc", ".cp", ".cxx", ".cpp", ".CPP", ".c++", ".C",
  ".h", ".hh", ".H", ".hp", ".hxx", ".hpp", ".HPP", ".h++", ".tcc"
]);

const SYMBOL_NODE_NAMES = new Set([
  "Identifier",
  "FieldIdentifier",
  "TypeIdentifier",
  "NamespaceIdentifier",
  "DestructorName"
]);

const PARSE_TIMEOUT_MS = 100;

function isDefinitionLookupSupported(filePath) {
  const fileName = (filePath || "").split("/").pop();
  const dot = fileName.lastIndexOf(".");
  return dot > 0 && C_CPP_EXTENSIONS.has(fileName.substring(dot));
}

function isMacPlatform(nav) {
  return /Mac|iPhone|iPad|iPod/.test(nav?.platform || nav?.userAgent || "");
}

function isDefinitionClick(event, isMac) {
  return event.button === 0 &&
    !event.altKey &&
    !event.shiftKey &&
    (isMac ? event.metaKey && !event.ctrlKey
      : event.ctrlKey && !event.metaKey);
}

/**
 * Returns the identifier token at the given document position as
 * { name, from, to }, or null if there is no symbol to look up there.
 * Parsing is bounded in time; if the tree is not available around the
 * position, null is returned.
 */
function symbolAt(state, pos, timeout = PARSE_TIMEOUT_MS) {
  if (pos < 0 || pos > state.doc.length)
    return null;

  const upto = state.doc.lineAt(pos).to;
  const tree = ensureSyntaxTree(state, upto, timeout) || syntaxTree(state);
  if (tree.length < upto)
    return null;

  for (const side of [ 1, -1 ]) {
    const node = tree.resolveInner(pos, side);
    if (SYMBOL_NODE_NAMES.has(node.name)) {
      return {
        name: state.sliceDoc(node.from, node.to),
        from: node.from,
        to: node.to
      };
    }
  }

  return null;
}

function toNumber(value) {
  if (value === null || value === undefined)
    return null;
  return typeof value === "number" ? value : value.toNumber();
}

function groupKey(candidate) {
  return JSON.stringify([
    String(toNumber(candidate.fileId)),
    toNumber(candidate.line),
    toNumber(candidate.endLine),
    candidate.kind,
    candidate.scope ?? null,
    candidate.scopeKind ?? null,
    candidate.signature ?? null
  ]);
}

/**
 * Merges candidates which only differ in their language (e.g. a header
 * indexed both as C and C++). The server order of the first occurrences is
 * kept; each group lists its distinct languages in sorted order.
 */
function collapseCandidates(candidates) {
  const groups = new Map();

  for (const candidate of candidates || []) {
    const key = groupKey(candidate);
    const group = groups.get(key);
    if (group) {
      if (!group.languages.includes(candidate.language))
        group.languages.push(candidate.language);
    } else {
      groups.set(key, {
        fileId: candidate.fileId,
        filePath: candidate.filePath,
        line: candidate.line,
        endLine: candidate.endLine ?? null,
        kind: candidate.kind,
        scope: candidate.scope ?? null,
        scopeKind: candidate.scopeKind ?? null,
        signature: candidate.signature ?? null,
        languages: [ candidate.language ]
      });
    }
  }

  return [ ...groups.values() ].map(group => {
    group.languages.sort();
    return group;
  });
}

function candidateLabel(symbolName, group) {
  const fileName = (group.filePath || "").split("/").pop();
  return {
    title: (group.scope ? group.scope + "::" : "") + symbolName +
      (group.signature || ""),
    subtitle: [
      group.kind,
      `${fileName}:${toNumber(group.line)}`,
      group.languages.join(", ")
    ].join(" · "),
    tooltip: group.filePath
  };
}

function definitionResultKind(groups) {
  if (!groups.length)
    return "none";
  return groups.length === 1 ? "navigate" : "pick";
}

function clampLine(line, numberOfLines) {
  return Math.min(Math.max(toNumber(line) || 1, 1), numberOfLines);
}

function createRequestSequence() {
  let latest = 0;
  return {
    next: () => ++latest,
    isLatest: id => id === latest
  };
}

export {
  candidateLabel,
  clampLine,
  collapseCandidates,
  createRequestSequence,
  definitionResultKind,
  isDefinitionClick,
  isDefinitionLookupSupported,
  isMacPlatform,
  symbolAt
};
