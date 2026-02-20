#!/usr/bin/env python3
"""Extract symbols from source files using tree-sitter.

Used in two modes:
1. Batch: parse an entire project directory → JSON array of symbols
2. Single: parse one file for a specific symbol name → matching symbols

Supports: TypeScript, JavaScript, Python, Bash, Rust, Go
"""

import json
import os
import sys
from pathlib import Path

try:
    from tree_sitter_languages import get_language, get_parser
except ImportError:
    print("tree-sitter-languages not installed. Run: pip install tree-sitter-languages", file=sys.stderr)
    sys.exit(1)

# File extension → tree-sitter language name
LANG_MAP = {
    ".ts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".jsx": "javascript",
    ".py": "python",
    ".sh": "bash",
    ".rs": "rust",
    ".go": "go",
}

# Directories to skip
SKIP_DIRS = {
    "node_modules", ".git", "dist", "build", ".next", "__pycache__",
    ".venv", "venv", ".tox", ".mypy_cache", ".pytest_cache",
    "target", "vendor", ".turbo", "coverage", ".nyc_output",
}

# tree-sitter node types that represent symbols, per language
SYMBOL_QUERIES = {
    "typescript": {
        "function_declaration": "function",
        "arrow_function": "function",
        "method_definition": "function",
        "class_declaration": "class",
        "interface_declaration": "interface",
        "type_alias_declaration": "type",
        "enum_declaration": "enum",
        "lexical_declaration": "constant",  # const exports
        "export_statement": None,  # handled specially
    },
    "tsx": None,  # same as typescript
    "javascript": {
        "function_declaration": "function",
        "arrow_function": "function",
        "method_definition": "function",
        "class_declaration": "class",
        "lexical_declaration": "constant",
        "export_statement": None,
    },
    "python": {
        "function_definition": "function",
        "class_definition": "class",
        "decorated_definition": None,  # handled via child
    },
    "rust": {
        "function_item": "function",
        "struct_item": "class",
        "enum_item": "enum",
        "impl_item": "class",
        "trait_item": "interface",
        "type_item": "type",
    },
    "go": {
        "function_declaration": "function",
        "method_declaration": "function",
        "type_declaration": "type",
    },
    "bash": {
        "function_definition": "function",
    },
}

# tsx uses same queries as typescript
SYMBOL_QUERIES["tsx"] = SYMBOL_QUERIES["typescript"]


def get_name_from_node(node, source_bytes, lang):
    """Extract the name of a symbol from a tree-sitter node."""
    # Try common patterns for finding name child nodes
    for child in node.children:
        if child.type in ("identifier", "type_identifier", "property_identifier"):
            return source_bytes[child.start_byte:child.end_byte].decode("utf-8", errors="replace")
        if child.type == "name":
            return source_bytes[child.start_byte:child.end_byte].decode("utf-8", errors="replace")

    # For variable declarations (const x = ...), dig into declarator
    if node.type in ("lexical_declaration", "variable_declaration"):
        for child in node.children:
            if child.type == "variable_declarator":
                for gc in child.children:
                    if gc.type in ("identifier", "property_identifier"):
                        return source_bytes[gc.start_byte:gc.end_byte].decode("utf-8", errors="replace")

    # For export statements, look deeper
    if node.type == "export_statement":
        for child in node.children:
            name = get_name_from_node(child, source_bytes, lang)
            if name:
                return name

    # For decorated definitions (Python), get the inner function/class
    if node.type == "decorated_definition":
        for child in node.children:
            if child.type in ("function_definition", "class_definition"):
                return get_name_from_node(child, source_bytes, lang)

    return None


def get_signature_from_node(node, source_bytes, lang):
    """Extract function signature from a tree-sitter node."""
    # Look for formal_parameters or parameters node
    for child in node.children:
        if child.type in ("formal_parameters", "parameters", "parameter_list"):
            sig = source_bytes[child.start_byte:child.end_byte].decode("utf-8", errors="replace")
            # Also look for return type annotation
            for sib in node.children:
                if sib.type == "type_annotation":
                    sig += " " + source_bytes[sib.start_byte:sib.end_byte].decode("utf-8", errors="replace")
            return sig

        # For variable declarators with arrow functions
        if child.type == "variable_declarator":
            for gc in child.children:
                if gc.type == "arrow_function":
                    return get_signature_from_node(gc, source_bytes, lang)

    # For export statements, look at child declarations
    if node.type == "export_statement":
        for child in node.children:
            sig = get_signature_from_node(child, source_bytes, lang)
            if sig:
                return sig

    return None


def classify_symbol(name, node_type, file_path, source_bytes, node):
    """Refine symbol type based on naming conventions and context."""
    # React components: PascalCase functions in .tsx/.jsx
    if file_path.endswith((".tsx", ".jsx")) and name and name[0].isupper():
        return "component"

    # React hooks: useXxx
    if name and name.startswith("use") and name[2:3].isupper():
        return "hook"

    # Zustand stores: useXxxStore or createXxx
    if name and ("Store" in name or name.startswith("create")):
        if file_path.endswith((".ts", ".tsx", ".js", ".jsx")):
            return "store"

    return None  # Use default classification


def extract_symbols(file_path, source_bytes=None):
    """Extract all symbols from a single file."""
    ext = Path(file_path).suffix
    lang_name = LANG_MAP.get(ext)
    if not lang_name:
        return []

    if source_bytes is None:
        try:
            with open(file_path, "rb") as f:
                source_bytes = f.read()
        except (OSError, PermissionError):
            return []

    try:
        parser = get_parser(lang_name)
        tree = parser.parse(source_bytes)
    except Exception:
        return []

    queries = SYMBOL_QUERIES.get(lang_name, {})
    symbols = []

    def visit(node, depth=0):
        if depth > 20:  # Prevent infinite recursion
            return

        node_type_map = queries.get(node.type)

        if node.type in queries:
            name = get_name_from_node(node, source_bytes, lang_name)
            if name and len(name) > 1 and not name.startswith("_"):
                sym_type = node_type_map or "function"

                # Refine classification
                refined = classify_symbol(name, sym_type, file_path, source_bytes, node)
                if refined:
                    sym_type = refined

                signature = get_signature_from_node(node, source_bytes, lang_name)
                line = node.start_point[0] + 1  # tree-sitter is 0-indexed

                symbols.append({
                    "file": str(file_path),
                    "line": line,
                    "type": sym_type,
                    "name": name,
                    "signature": signature,
                })

        for child in node.children:
            visit(child, depth + 1)

    visit(tree.root_node)
    return symbols


def scan_directory(project_path, extensions=None):
    """Recursively scan a directory and extract symbols from all supported files."""
    project_path = Path(project_path)
    all_symbols = []

    if extensions is None:
        extensions = set(LANG_MAP.keys())

    for root, dirs, files in os.walk(project_path):
        # Skip excluded directories
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]

        for filename in files:
            ext = Path(filename).suffix
            if ext not in extensions:
                continue

            file_path = os.path.join(root, filename)
            # Make path relative to project root
            rel_path = os.path.relpath(file_path, project_path)
            # Normalize to forward slashes
            rel_path = rel_path.replace("\\", "/")

            symbols = extract_symbols(file_path)
            for sym in symbols:
                sym["file"] = rel_path
            all_symbols.extend(symbols)

    return all_symbols


def find_symbol(project_path, symbol_name, exact=False):
    """Search for a specific symbol by name across a project.

    Args:
        project_path: Root directory of the project
        symbol_name: Name to search for
        exact: If True, exact match only. If False, substring match.

    Returns:
        List of matching symbols with absolute file paths.
    """
    project_path = Path(project_path)
    matches = []

    for root, dirs, files in os.walk(project_path):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]

        for filename in files:
            ext = Path(filename).suffix
            if ext not in LANG_MAP:
                continue

            file_path = os.path.join(root, filename)
            symbols = extract_symbols(file_path)

            for sym in symbols:
                if exact and sym["name"] == symbol_name:
                    matches.append(sym)
                elif not exact and symbol_name.lower() in sym["name"].lower():
                    matches.append(sym)

    return matches


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage:")
        print("  parse_symbols.py <project_path>              # Scan entire project")
        print("  parse_symbols.py <project_path> <symbol>     # Find specific symbol")
        sys.exit(1)

    project_path = sys.argv[1]

    if len(sys.argv) >= 3:
        symbol_name = sys.argv[2]
        results = find_symbol(project_path, symbol_name)
    else:
        results = scan_directory(project_path)

    print(json.dumps(results, indent=2))
