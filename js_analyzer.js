// js_analyzer.js
const fs = require("fs");
const parser = require("@typescript-eslint/typescript-estree");


// ----------------------------
// Small helpers
// ----------------------------
function getStaticString(node) {
  if (!node) return null;

  if (node.type === "Literal" && typeof node.value === "string") {
    return node.value;
  }

  // Handles import(`./foo`) when there are no dynamic expressions.
  if (node.type === "TemplateLiteral") {
    if ((node.expressions || []).length === 0) {
      const quasis = node.quasis || [];
      return quasis.map(q => (q.value && q.value.cooked) || "").join("");
    }
  }

  return null;
}


function walk(node, fn) {
  if (!node || typeof node !== "object") return;

  fn(node);

  for (const key of Object.keys(node)) {
    const value = node[key];

    if (Array.isArray(value)) {
      for (const child of value) {
        walk(child, fn);
      }
    } else {
      walk(value, fn);
    }
  }
}


function wordCount(text) {
  return (text || "").trim().split(/\s+/).filter(Boolean).length;
}


// ----------------------------
// Lexical metrics
// ----------------------------
function computeSlocAndComments(code) {
  const lines = code.split(/\r?\n/);

  const blankLines = new Set();
  const commentOnlyLines = new Set();

  for (let i = 0; i < lines.length; i++) {
    if (!lines[i].trim()) {
      blankLines.add(i + 1);
    }
  }

  function stripLineComments(line) {
    const idx = line.indexOf("//");
    return idx >= 0 ? line.slice(0, idx) : line;
  }

  function stripBlockComments(line) {
    return line.replace(/\/\*.*?\*\//g, "");
  }

  for (let i = 0; i < lines.length; i++) {
    const lineNumber = i + 1;
    const original = lines[i];
    const trimmed = original.trim();

    if (!trimmed) continue;

    if (
      trimmed.startsWith("//") ||
      trimmed.startsWith("/*") ||
      trimmed.startsWith("*") ||
      trimmed.startsWith("*/")
    ) {
      commentOnlyLines.add(lineNumber);
      continue;
    }

    const codePart = stripLineComments(stripBlockComments(original)).trim();

    if (!codePart) {
      commentOnlyLines.add(lineNumber);
    }
  }

  let sloc = 0;

  for (let i = 0; i < lines.length; i++) {
    const lineNumber = i + 1;

    if (blankLines.has(lineNumber)) continue;
    if (commentOnlyLines.has(lineNumber)) continue;

    sloc += 1;
  }

  let commentWords = 0;

  // Line comments.
  for (const line of lines) {
    const idx = line.indexOf("//");

    if (idx >= 0) {
      const commentText = line.slice(idx + 2);
      commentWords += wordCount(commentText);
    }
  }

  // Block comments.
  const blockMatches = code.match(/\/\*[\s\S]*?\*\//g) || [];

  for (const block of blockMatches) {
    const text = block.replace(/^\/\*/, "").replace(/\*\/$/, "");
    commentWords += wordCount(text);
  }

  return {
    sloc,
    commentWords,
    blankLines: Array.from(blankLines),
    commentOnlyLines: Array.from(commentOnlyLines),
  };
}


// ----------------------------
// Parser setup
// ----------------------------
function formatParseError(err) {
  if (!err) return "unknown_parse_error";

  const parts = [];
  const name = err.name || "TSError";
  const msg = err.message || String(err);

  parts.push(name);
  parts.push(msg);

  const line =
    typeof err.lineNumber === "number" ? err.lineNumber :
    typeof err.line === "number" ? err.line :
    null;

  const column =
    typeof err.column === "number" ? err.column :
    typeof err.col === "number" ? err.col :
    null;

  if (line != null && column != null) {
    parts.push(`line ${line}, col ${column}`);
  } else if (line != null) {
    parts.push(`line ${line}`);
  }

  return parts.join(": ");
}


function getParseCandidates(filePath) {
  const lower = String(filePath || "").toLowerCase();

  if (lower.endsWith(".tsx")) {
    return [
      { sourceType: "module", jsx: true, label: "tsx_module_jsx" },
      { sourceType: "script", jsx: true, label: "tsx_script_jsx" },
    ];
  }

  if (lower.endsWith(".ts")) {
    return [
      { sourceType: "module", jsx: false, label: "ts_module" },
      { sourceType: "module", jsx: true, label: "ts_module_jsx_fallback" },
      { sourceType: "script", jsx: false, label: "ts_script" },
      { sourceType: "script", jsx: true, label: "ts_script_jsx_fallback" },
    ];
  }

  if (lower.endsWith(".jsx")) {
    return [
      { sourceType: "module", jsx: true, label: "jsx_module" },
      { sourceType: "script", jsx: true, label: "jsx_script" },
      { sourceType: "module", jsx: false, label: "jsx_module_nojsx_fallback" },
    ];
  }

  return [
    { sourceType: "module", jsx: true, label: "js_module_jsx" },
    { sourceType: "script", jsx: true, label: "js_script_jsx" },
    { sourceType: "module", jsx: false, label: "js_module" },
    { sourceType: "script", jsx: false, label: "js_script" },
  ];
}


function tryParseWithCandidates(code, filePath) {
  const candidates = getParseCandidates(filePath);
  let lastError = null;

  for (const cfg of candidates) {
    try {
      const ast = parser.parse(code, {
        loc: true,
        jsx: cfg.jsx,
        sourceType: cfg.sourceType,
        filePath,
        errorOnUnknownASTType: false,
        comment: true,
        range: false,
      });

      return { ast, cfg, error: null };

    } catch (err) {
      lastError = err;
    }
  }

  return { ast: null, cfg: null, error: lastError };
}


// ----------------------------
// Identifier and import helpers
// ----------------------------
function isIdentifierUse(node, parent) {
  if (!node || node.type !== "Identifier") return false;
  if (!parent) return true;

  const parentType = parent.type;

  if (
    parentType === "ImportSpecifier" ||
    parentType === "ImportDefaultSpecifier" ||
    parentType === "ImportNamespaceSpecifier"
  ) {
    return false;
  }

  if (parentType === "FunctionDeclaration" && parent.id === node) return false;
  if (parentType === "VariableDeclarator" && parent.id === node) return false;
  if (parentType === "ClassDeclaration" && parent.id === node) return false;
  if (parentType === "MethodDefinition" && parent.key === node) return false;
  if (parentType === "Property" && parent.key === node && !parent.computed) return false;

  return true;
}


function addPatternLocals(pattern, outSet) {
  if (!pattern) return;

  if (pattern.type === "Identifier") {
    outSet.add(pattern.name);
    return;
  }

  if (pattern.type === "ObjectPattern") {
    for (const prop of pattern.properties || []) {
      if (!prop) continue;

      if (prop.type === "RestElement") {
        addPatternLocals(prop.argument, outSet);
        continue;
      }

      if (prop.value) {
        addPatternLocals(prop.value, outSet);
      } else if (prop.argument) {
        addPatternLocals(prop.argument, outSet);
      }
    }

    return;
  }

  if (pattern.type === "ArrayPattern") {
    for (const el of pattern.elements || []) {
      if (el) addPatternLocals(el, outSet);
    }

    return;
  }

  if (pattern.type === "AssignmentPattern") {
    addPatternLocals(pattern.left, outSet);
    return;
  }

  if (pattern.type === "Property") {
    if (pattern.value) addPatternLocals(pattern.value, outSet);
    return;
  }

  if (pattern.type === "RestElement") {
    addPatternLocals(pattern.argument, outSet);
  }
}


function collectJsxUsedName(nameNode, outSet) {
  if (!nameNode) return;

  // <Component />
  if (nameNode.type === "JSXIdentifier") {
    const name = nameNode.name || "";

    // Imported React components are usually capitalized.
    if (/^[A-Z]/.test(name)) {
      outSet.add(name);
    }

    return;
  }

  // <Icons.User /> or <motion.div />
  if (nameNode.type === "JSXMemberExpression") {
    let current = nameNode;

    while (current && current.type === "JSXMemberExpression") {
      current = current.object;
    }

    if (current && current.type === "JSXIdentifier") {
      const name = current.name || "";

      if (name) {
        outSet.add(name);
      }
    }

    return;
  }

  // Rare fallback for namespaced JSX.
  if (nameNode.type === "JSXNamespacedName") {
    const name = nameNode.namespace && nameNode.namespace.name;

    if (name) {
      outSet.add(name);
    }
  }
}


function buildParentMap(ast) {
  const parent = new Map();

  walk(ast, (node) => {
    for (const key of Object.keys(node)) {
      const value = node[key];

      if (Array.isArray(value)) {
        for (const child of value) {
          if (child && typeof child === "object") {
            parent.set(child, node);
          }
        }
      } else if (value && typeof value === "object") {
        parent.set(value, node);
      }
    }
  });

  return parent;
}


// ----------------------------
// AST metric extraction
// ----------------------------
function countLogicalLines(ast) {
  let lloc = 0;

  walk(ast, (node) => {
    if (!node || !node.type) return;

    if (
      node.type.endsWith("Statement") ||
      node.type === "FunctionDeclaration" ||
      node.type === "ClassDeclaration" ||
      node.type === "VariableDeclaration"
    ) {
      lloc += 1;
    }
  });

  return lloc;
}


function collectFunctions(ast) {
  const functions = [];
  const fnSpans = [];

  walk(ast, (node) => {
    let fnNode = null;

    if (
      node.type === "FunctionDeclaration" ||
      node.type === "FunctionExpression" ||
      node.type === "ArrowFunctionExpression"
    ) {
      fnNode = node;

    } else if (node.type === "MethodDefinition" && node.value) {
      fnNode = node.value;

    } else if (
      node.type === "Property" &&
      node.value &&
      (
        node.value.type === "FunctionExpression" ||
        node.value.type === "ArrowFunctionExpression"
      )
    ) {
      fnNode = node.value;

    } else if (
      (node.type === "PropertyDefinition" || node.type === "FieldDefinition") &&
      node.value &&
      (
        node.value.type === "FunctionExpression" ||
        node.value.type === "ArrowFunctionExpression"
      )
    ) {
      fnNode = node.value;
    }

    if (fnNode) {
      functions.push(fnNode);

      if (fnNode.loc) {
        fnSpans.push([fnNode.loc.start.line, fnNode.loc.end.line]);
      }
    }
  });

  return { functions, fnSpans };
}


function cyclomaticComplexityForFunction(fn) {
  let cc = 1;

  function flattenLogical(node, op) {
    let count = 0;

    function rec(x) {
      if (!x) return;

      if (x.type === "LogicalExpression" && x.operator === op) {
        rec(x.left);
        rec(x.right);
      } else {
        count += 1;
      }
    }

    rec(node);
    return count;
  }

  walk(fn.body || fn, (node) => {
    if (!node || !node.type) return;

    if (
      node.type === "IfStatement" ||
      node.type === "ForStatement" ||
      node.type === "ForInStatement" ||
      node.type === "ForOfStatement" ||
      node.type === "WhileStatement" ||
      node.type === "DoWhileStatement" ||
      node.type === "CatchClause" ||
      node.type === "ConditionalExpression"
    ) {
      cc += 1;
      return;
    }

    if (
      node.type === "LogicalExpression" &&
      (node.operator === "&&" || node.operator === "||")
    ) {
      const operands = flattenLogical(node, node.operator);
      cc += Math.max(0, operands - 1);
      return;
    }

    if (node.type === "SwitchStatement" && Array.isArray(node.cases)) {
      for (const switchCase of node.cases) {
        if (switchCase && switchCase.test != null) {
          cc += 1;
        }
      }
    }
  });

  return cc;
}


function collectUsedIdentifiers(ast, parent) {
  const usedIds = new Set();

  walk(ast, (node) => {
    if (!node || !node.type) return;

    if (node.type === "Identifier") {
      const parentNode = parent.get(node);

      if (isIdentifierUse(node, parentNode)) {
        usedIds.add(node.name);
      }

      return;
    }

    // JSX component use, e.g. <Button />.
    if (node.type === "JSXOpeningElement") {
      collectJsxUsedName(node.name, usedIds);
      return;
    }

    // Defensive fallback for standalone JSX identifiers.
    if (node.type === "JSXIdentifier") {
      const name = node.name || "";

      if (/^[A-Z]/.test(name)) {
        usedIds.add(name);
      }
    }
  });

  return usedIds;
}


function collectImportSources(ast, parent, usedIds) {
  const importsBySource = new Map();
  const sideEffectSources = new Set();

  for (const statement of ast.body || []) {
    if (statement.type === "ImportDeclaration") {
      const src = statement.source && statement.source.value;

      if (!src) continue;

      if (!statement.specifiers || statement.specifiers.length === 0) {
        sideEffectSources.add(src);
      } else {
        if (!importsBySource.has(src)) {
          importsBySource.set(src, new Set());
        }

        for (const specifier of statement.specifiers) {
          if (specifier.local && specifier.local.name) {
            importsBySource.get(src).add(specifier.local.name);
          }
        }
      }
    }

    if (
      statement.type === "ExportNamedDeclaration" ||
      statement.type === "ExportAllDeclaration"
    ) {
      const src = statement.source && statement.source.value;

      if (src) {
        sideEffectSources.add(src);
      }
    }
  }

  walk(ast, (node) => {
    if (node.type === "ImportExpression") {
      const src = getStaticString(node.source);

      if (src) {
        sideEffectSources.add(src);
      }

      return;
    }

    if (
      node.type === "CallExpression" &&
      node.callee &&
      node.callee.type === "Identifier" &&
      node.callee.name === "require"
    ) {
      collectRequireSource(node, parent, importsBySource, sideEffectSources);
    }
  });

  const usedSources = new Set();

  for (const src of sideEffectSources) {
    usedSources.add(src);
  }

  for (const [src, locals] of importsBySource.entries()) {
    for (const name of locals.values()) {
      if (usedIds.has(name)) {
        usedSources.add(src);
        break;
      }
    }
  }

  return Array.from(usedSources);
}


function collectRequireSource(node, parent, importsBySource, sideEffectSources) {
  const arg = node.arguments && node.arguments[0];
  const src = getStaticString(arg);

  if (!src) return;

  const parentNode = parent.get(node);

  if (parentNode && parentNode.type === "VariableDeclarator" && parentNode.id) {
    const locals = new Set();
    addPatternLocals(parentNode.id, locals);

    if (locals.size > 0) {
      if (!importsBySource.has(src)) {
        importsBySource.set(src, new Set());
      }

      for (const name of locals) {
        importsBySource.get(src).add(name);
      }
    } else {
      sideEffectSources.add(src);
    }

    return;
  }

  if (parentNode && parentNode.type === "MemberExpression") {
    const grandParent = parent.get(parentNode);

    if (grandParent && grandParent.type === "VariableDeclarator" && grandParent.id) {
      const locals = new Set();
      addPatternLocals(grandParent.id, locals);

      if (locals.size > 0) {
        if (!importsBySource.has(src)) {
          importsBySource.set(src, new Set());
        }

        for (const name of locals) {
          importsBySource.get(src).add(name);
        }
      } else {
        sideEffectSources.add(src);
      }

      return;
    }
  }

  sideEffectSources.add(src);
}


// ----------------------------
// Output helpers
// ----------------------------
function printJson(payload) {
  console.log(JSON.stringify(payload));
}


function baseResult(overrides = {}) {
  return {
    ok: true,
    parse_ok: true,
    parse_error: "",
    sourceTypeUsed: null,
    jsxModeUsed: null,
    parseModeUsed: null,
    sloc: 0,
    lloc: 0,
    fnSpans: [],
    ccVals: [],
    commentWords: 0,
    usedImportSources: [],
    blankLines: [],
    commentOnlyLines: [],
    functionsCount: 0,
    functionsWithLoc: 0,
    ...overrides,
  };
}


// ----------------------------
// Main analysis
// ----------------------------
function main(filePath) {
  let code = "";

  try {
    code = fs.readFileSync(filePath, "utf8");

  } catch (err) {
    printJson({
      ok: false,
      error: `read_fail: ${String(err)}`,
      filePath: String(filePath),
      argv: process.argv,
    });

    return;
  }

  const slocPack = computeSlocAndComments(code);

  const lexicalFields = {
    sloc: slocPack.sloc,
    commentWords: slocPack.commentWords,
    blankLines: slocPack.blankLines,
    commentOnlyLines: slocPack.commentOnlyLines,
  };

  // Keep lexical metrics even when parsing fails.
  if (code.includes("<<<<<<<") && code.includes("=======") && code.includes(">>>>>>>")) {
    printJson(baseResult({
      ...lexicalFields,
      parse_ok: false,
      parse_error: "TSError: Merge conflict marker encountered.",
    }));

    return;
  }

  if (code.indexOf("\u0000") >= 0) {
    printJson(baseResult({
      ...lexicalFields,
      parse_ok: false,
      parse_error: "TSError: File appears to be binary.",
    }));

    return;
  }

  // Convert shebang to a comment so parser can handle it more often.
  let normalizedCode = code;

  if (normalizedCode.startsWith("#!")) {
    const firstNewLine = normalizedCode.indexOf("\n");
    normalizedCode = firstNewLine >= 0
      ? `//${normalizedCode.slice(2)}`
      : "//";
  }

  const parsed = tryParseWithCandidates(normalizedCode, filePath);

  if (!parsed.ast) {
    printJson(baseResult({
      ...lexicalFields,
      parse_ok: false,
      parse_error: formatParseError(parsed.error),
    }));

    return;
  }

  const ast = parsed.ast;
  const parent = buildParentMap(ast);

  const lloc = countLogicalLines(ast);
  const { functions, fnSpans } = collectFunctions(ast);
  const ccVals = functions.map(cyclomaticComplexityForFunction);

  const usedIds = collectUsedIdentifiers(ast, parent);
  const usedImportSources = collectImportSources(ast, parent, usedIds);

  printJson(baseResult({
    ...lexicalFields,
    sourceTypeUsed: parsed.cfg.sourceType,
    jsxModeUsed: parsed.cfg.jsx,
    parseModeUsed: parsed.cfg.label,
    lloc,
    fnSpans,
    ccVals,
    usedImportSources,
    functionsCount: functions.length,
    functionsWithLoc: fnSpans.length,
  }));
}


// ----------------------------
// CLI entry point
// ----------------------------
const filePath = process.argv[2];

if (!filePath) {
  printJson({
    ok: false,
    error: "missing_file_path",
    argv: process.argv,
  });

  process.exit(0);
}

main(filePath);