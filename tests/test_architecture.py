"""Architecture rules from CLAUDE.md and docs/temporal-model.md, checked with `ast`.

No database needed. Each rule is a function over parsed source files that
returns violations as "path:line: message". The repo must produce none, and
every rule is also run against synthetic violations, so a rule that silently
stops matching fails here instead of passing vacuously.
"""

from __future__ import annotations

import ast
import os
import re
import textwrap
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from functools import cache
from pathlib import Path, PurePosixPath

import pytest

ROOT = Path(__file__).resolve().parents[1]

# The LLM gateway wrapper: its own top-level package, outside core/.
GATEWAY = "gateway"
GATEWAY_PATH = "gateway"
GATEWAY_CALLERS = ("extract", "narrate")
PROVIDER_SDKS = (
    "anthropic",
    "openai",
    "google.genai",
    "google.generativeai",
    "vertexai",
    "mistralai",
    "cohere",
    "litellm",
    "ollama",
    "langchain",
    "langchain_core",
    "langchain_community",
    "langchain_anthropic",
    "langchain_openai",
    "llama_index",
)

# MCP client packages: only ingest/ adapters (plain code, no model) and the gateway.
MCP_PACKAGES = ("mcp", "fastmcp")
MCP_CALLERS = ("ingest", GATEWAY_PATH)

# Every ingest/ module or subpackage is a source adapter, except shared machinery.
# Mirrors ingest.registry.INFRA_MODULES (checked below).
INGEST_INFRA = frozenset(
    {"ingest/__init__.py", "ingest/__main__.py", "ingest/base.py", "ingest/http.py",
     "ingest/registry.py", "ingest/schema.py"}
)  # fmt: skip
ADAPTER_DECLARATIONS = frozenset({"name", "source_class", "target_stores"})

# R2 provenance columns.
PROVENANCE_COLUMNS = frozenset(
    {"as_of", "content_hash", "source_url", "extracted_by", "model_version"}
)

# Point-in-time read modules: the only code allowed to query store tables.
PIT_PATHS = ("core/db/pit.py", "core/db/pit")
READ_CALLS = frozenset({"select", "query", "get", "exists"})
RAW_SQL_CALLS = frozenset({"text", "execute", "exec_driver_sql"})

# Allowlist, not denylist: anything not here is assumed to do I/O or be
# non-deterministic. zoneinfo is excluded (reads tz files); pass tz objects in.
COMPUTE_ALLOWED_STDLIB = frozenset(
    {
        "__future__",
        "abc",
        "bisect",
        "collections",
        "dataclasses",
        "datetime",
        "decimal",
        "enum",
        "fractions",
        "functools",
        "hashlib",
        "heapq",
        "itertools",
        "math",
        "numbers",
        "operator",
        "re",
        "statistics",
        "string",
        "typing",
    }
)
COMPUTE_BANNED_CALLS = frozenset(
    {"open", "print", "input", "exec", "eval", "compile", "__import__", "breakpoint"}
)
# Clock reads: compute functions take `t` explicitly (docs/temporal-model.md).
COMPUTE_BANNED_METHODS = frozenset({"now", "today", "utcnow"})

FLOAT_COLUMN_TYPES = frozenset({"Float", "FLOAT", "Double", "DOUBLE", "DOUBLE_PRECISION", "REAL"})
MONEY_TOKENS = frozenset(
    {
        "amount", "price", "prices", "value", "open", "high", "low", "close",
        "revenue", "sales", "turnover", "income", "profit", "pat", "pbt", "ebit",
        "ebitda", "expense", "expenses", "cost", "costs", "capex", "cwip", "block",
        "debt", "borrowings", "cash", "equity", "assets", "liabilities", "networth",
        "capital", "dividend", "eps", "fee", "fees", "tax", "interest", "mcap",
        "nav", "inr", "rupees", "crore", "crores", "lakh", "lakhs",
    }
)  # fmt: skip


# --------------------------------------------------------------------------- #
# Source model
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SourceFile:
    rel: PurePosixPath
    tree: ast.Module

    @property
    def is_package(self) -> bool:
        return self.rel.name == "__init__.py"

    @property
    def module(self) -> str:
        parts = list(self.rel.with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        return ".".join(parts)

    def under(self, *prefixes: str) -> bool:
        return any(
            self.rel.parts[: len(PurePosixPath(p).parts)] == PurePosixPath(p).parts
            for p in prefixes
        )


@dataclass(frozen=True)
class Import:
    line: int
    module: str
    names: tuple[str, ...] = ()  # `from module import names`

    def targets(self) -> list[str]:
        """Every module this could bind: `from x import gateway` may import `x.gateway`."""
        return [self.module, *(f"{self.module}.{n}" for n in self.names if n != "*")]


_SKIP_DIRS = {"__pycache__", "build", "dist", "node_modules"}


@cache
def repo_files() -> tuple[SourceFile, ...]:
    files = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        here = Path(dirpath)
        dirnames[:] = sorted(
            d
            for d in dirnames
            if not d.startswith(".")
            and d not in _SKIP_DIRS
            and not d.endswith(".egg-info")
            and not (here / d / "pyvenv.cfg").exists()
        )
        for name in sorted(filenames):
            if name.endswith(".py"):
                path = here / name
                rel = PurePosixPath(path.relative_to(ROOT).as_posix())
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(rel))
                files.append(SourceFile(rel, tree))
    return tuple(files)


def _dotted(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


def _terminal(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _matches(module: str, prefix: str) -> bool:
    return module == prefix or module.startswith(prefix + ".")


def _resolve_from(sf: SourceFile, node: ast.ImportFrom) -> str:
    if node.level == 0:
        return node.module or ""
    package = sf.module.split(".") if sf.is_package else sf.module.split(".")[:-1]
    base = package[: len(package) - (node.level - 1)]
    return ".".join([*base, *([node.module] if node.module else [])])


def imports(sf: SourceFile) -> Iterator[Import]:
    for node in ast.walk(sf.tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield Import(node.lineno, alias.name)
        elif isinstance(node, ast.ImportFrom):
            yield Import(node.lineno, _resolve_from(sf, node), tuple(a.name for a in node.names))
        elif isinstance(node, ast.Call) and _dotted(node.func) in {
            "__import__",
            "import_module",
            "importlib.import_module",
        }:
            arg = node.args[0] if node.args else None
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                yield Import(node.lineno, arg.value)
            else:
                yield Import(node.lineno, "<dynamic>")


# --------------------------------------------------------------------------- #
# Rule 1: only extract/ and narrate/ import the LLM gateway
# --------------------------------------------------------------------------- #


def check_gateway_imports(files: tuple[SourceFile, ...]) -> list[str]:
    violations = []
    for sf in files:
        if sf.under("tests", *GATEWAY_CALLERS, GATEWAY_PATH):
            continue
        for imp in imports(sf):
            if any(_matches(t, GATEWAY) for t in imp.targets()):
                violations.append(
                    f"{sf.rel}:{imp.line}: imports LLM gateway ({imp.module}); "
                    f"only {'/, '.join(GATEWAY_CALLERS)}/ may"
                )
    return violations


def check_provider_sdk_imports(files: tuple[SourceFile, ...]) -> list[str]:
    violations = []
    for sf in files:
        if sf.under(GATEWAY_PATH):
            continue
        for imp in imports(sf):
            if any(_matches(imp.module, sdk) for sdk in PROVIDER_SDKS):
                violations.append(
                    f"{sf.rel}:{imp.line}: imports provider SDK {imp.module}; "
                    f"go through the gateway ({GATEWAY}/)"
                )
    return violations


# --------------------------------------------------------------------------- #
# Rule 2: no float on monetary fields
# --------------------------------------------------------------------------- #


def _mentions_float(annotation: ast.expr) -> bool:
    for node in ast.walk(annotation):
        if _terminal(node) == "float":
            return True
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            try:
                inner = ast.parse(node.value, mode="eval").body
            except SyntaxError:
                continue
            if _mentions_float(inner):
                return True
    return False


def _is_mapped(annotation: ast.expr) -> bool:
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        try:
            annotation = ast.parse(annotation.value, mode="eval").body
        except SyntaxError:
            return False
    return isinstance(annotation, ast.Subscript) and _terminal(annotation.value) == "Mapped"


def _is_money_name(name: str) -> bool:
    return any(token in MONEY_TOKENS for token in name.lower().split("_"))


def _annotations(tree: ast.AST) -> Iterator[tuple[str | None, int, ast.expr]]:
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign):
            yield _terminal(node.target), node.lineno, node.annotation
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            a = node.args
            for arg in [*a.posonlyargs, *a.args, *a.kwonlyargs, a.vararg, a.kwarg]:
                if arg is not None and arg.annotation is not None:
                    yield arg.arg, arg.lineno, arg.annotation
            if node.returns is not None:
                yield node.name, node.lineno, node.returns


def check_no_float_money(files: tuple[SourceFile, ...]) -> list[str]:
    violations = []
    for sf in files:
        if sf.under("tests"):
            continue
        in_compute = sf.under("core/compute")
        for name, line, annotation in _annotations(sf.tree):
            if not _mentions_float(annotation):
                continue
            if in_compute:
                violations.append(f"{sf.rel}:{line}: float annotation in core/compute; use Decimal")
            elif _is_mapped(annotation):
                violations.append(f"{sf.rel}:{line}: ORM column {name!r} mapped as float")
            elif name is not None and _is_money_name(name):
                violations.append(f"{sf.rel}:{line}: monetary field {name!r} annotated float")

        if not any(_matches(i.module, "sqlalchemy") for i in imports(sf)):
            continue
        for node in ast.walk(sf.tree):
            if isinstance(node, (ast.Name, ast.Attribute)) and _terminal(node) in FLOAT_COLUMN_TYPES:
                violations.append(f"{sf.rel}:{node.lineno}: float column type {_terminal(node)}")
            elif (
                isinstance(node, ast.Call)
                and (_terminal(node.func) or "").upper() == "NUMERIC"
                and any(
                    k.arg == "asdecimal"
                    and isinstance(k.value, ast.Constant)
                    and k.value.value is False
                    for k in node.keywords
                )
            ):
                violations.append(f"{sf.rel}:{node.lineno}: Numeric(asdecimal=False) returns float")
    return violations


# --------------------------------------------------------------------------- #
# Rule 3: every table carries the R2 provenance columns
# --------------------------------------------------------------------------- #

ClassKey = tuple[str, str]  # (module, class name)


def _class_index(files: tuple[SourceFile, ...]) -> dict[ClassKey, tuple[SourceFile, ast.ClassDef]]:
    return {
        (sf.module, node.name): (sf, node)
        for sf in files
        for node in sf.tree.body
        if isinstance(node, ast.ClassDef)
    }


def _body_assigns(cls: ast.ClassDef) -> dict[str, ast.expr | None]:
    out: dict[str, ast.expr | None] = {}
    for stmt in cls.body:
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            out[stmt.target.id] = stmt.value
        elif isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name):
                    out[target.id] = stmt.value
    return out


def _resolve_class(index, sf: SourceFile, base: ast.expr) -> ClassKey | None:
    name = _terminal(base)
    if (sf.module, name) in index:
        return (sf.module, name)
    for imp in imports(sf):
        if name in imp.names and (imp.module, name) in index:
            return (imp.module, name)
    candidates = [key for key in index if key[1] == name]
    return candidates[0] if len(candidates) == 1 else None


def _fields(index, key: ClassKey, seen: frozenset[ClassKey] = frozenset()) -> tuple[set[str], set[str]]:
    """Attribute names declared on a class and its resolvable bases, plus unresolved bases."""
    sf, cls = index[key]
    fields, unresolved = set(_body_assigns(cls)), set()
    for base in cls.bases:
        base_key = _resolve_class(index, sf, base)
        if base_key is None:
            unresolved.add(ast.unparse(base))
        elif base_key not in seen | {key}:
            more, still = _fields(index, base_key, seen | {key})
            fields |= more
            unresolved |= still
    return fields, unresolved


def table_models(files: tuple[SourceFile, ...]) -> Iterator[tuple[SourceFile, ast.ClassDef, set[str], set[str]]]:
    index = _class_index(files)
    for key, (sf, cls) in index.items():
        assigns = _body_assigns(cls)
        abstract = assigns.get("__abstract__")
        if isinstance(abstract, ast.Constant) and abstract.value is True:
            continue
        if "__tablename__" in assigns or "__table__" in assigns:
            yield (sf, cls, *_fields(index, key))


def check_provenance_columns(files: tuple[SourceFile, ...]) -> list[str]:
    violations = []
    files = tuple(sf for sf in files if not sf.under("tests"))

    for sf, cls, fields, unresolved in table_models(files):
        if missing := PROVENANCE_COLUMNS - fields:
            hint = f" (unresolved bases: {', '.join(sorted(unresolved))})" if unresolved else ""
            violations.append(
                f"{sf.rel}:{cls.lineno}: model {cls.name} missing {sorted(missing)}{hint}"
            )

    # Core `Table(...)` and migrations' `op.create_table(...)`: the real schema.
    for sf in files:
        for node in ast.walk(sf.tree):
            if not (isinstance(node, ast.Call) and _terminal(node.func) in {"Table", "create_table"}):
                continue
            columns = {
                arg.args[0].value
                for arg in node.args
                if isinstance(arg, ast.Call)
                and _terminal(arg.func) == "Column"
                and arg.args
                and isinstance(arg.args[0], ast.Constant)
            }
            if missing := PROVENANCE_COLUMNS - columns:
                table = ast.unparse(node.args[0]) if node.args else "?"
                violations.append(f"{sf.rel}:{node.lineno}: table {table} missing {sorted(missing)}")
    return violations


# --------------------------------------------------------------------------- #
# Rule 4: core/compute/ imports nothing with I/O
# --------------------------------------------------------------------------- #


def _compute_import_ok(imp: Import) -> bool:
    def ok(module: str) -> bool:
        return module.split(".")[0] in COMPUTE_ALLOWED_STDLIB or _matches(module, "core.compute")

    return ok(imp.module) or bool(imp.names) and all(ok(f"{imp.module}.{n}") for n in imp.names)


def check_compute_is_pure(files: tuple[SourceFile, ...]) -> list[str]:
    violations = []
    for sf in files:
        if not sf.under("core/compute"):
            continue
        for imp in imports(sf):
            if not _compute_import_ok(imp):
                violations.append(
                    f"{sf.rel}:{imp.line}: core/compute imports {imp.module}; "
                    "not on the pure allowlist"
                )
        for node in ast.walk(sf.tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name) and node.func.id in COMPUTE_BANNED_CALLS:
                violations.append(f"{sf.rel}:{node.lineno}: core/compute calls {node.func.id}()")
            elif isinstance(node.func, ast.Attribute) and node.func.attr in COMPUTE_BANNED_METHODS:
                violations.append(
                    f"{sf.rel}:{node.lineno}: core/compute reads the clock "
                    f"({ast.unparse(node.func)}); take t as an argument"
                )
    return violations


# --------------------------------------------------------------------------- #
# Rule 5: store tables are read only through point-in-time functions (R2)
# --------------------------------------------------------------------------- #


def check_reads_go_through_pit(files: tuple[SourceFile, ...]) -> list[str]:
    models, tables = set(), set()
    for _, cls, _, _ in table_models(tuple(sf for sf in files if not sf.under("tests"))):
        models.add(cls.name)
        tablename = _body_assigns(cls).get("__tablename__")
        if isinstance(tablename, ast.Constant) and isinstance(tablename.value, str):
            tables.add(tablename.value)
    table_sql = (
        re.compile(rf"\b(?:from|join)\s+(?:\w+\.)?({'|'.join(map(re.escape, sorted(tables)))})\b", re.I)
        if tables
        else None
    )

    violations = []
    for sf in files:
        if sf.under("tests", "migrations", *PIT_PATHS):
            continue
        for node in ast.walk(sf.tree):
            if not isinstance(node, ast.Call):
                continue
            name = _terminal(node.func)
            args = [*node.args, *(k.value for k in node.keywords)]
            if name in READ_CALLS:
                if mentioned := {_terminal(n) for a in args for n in ast.walk(a)} & models:
                    violations.append(
                        f"{sf.rel}:{node.lineno}: reads store {', '.join(sorted(mentioned))} "
                        "directly; use a function in core/db/pit.py"
                    )
            elif name in RAW_SQL_CALLS and table_sql and args:
                first = args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    if match := table_sql.search(first.value):
                        violations.append(
                            f"{sf.rel}:{node.lineno}: raw SQL reads store table {match.group(1)}; "
                            "use a function in core/db/pit.py"
                        )
    return violations


# --------------------------------------------------------------------------- #
# Rule 6: only ingest/ and gateway/ import the MCP client
# --------------------------------------------------------------------------- #


def check_mcp_imports(files: tuple[SourceFile, ...]) -> list[str]:
    violations = []
    for sf in files:
        if sf.under("tests", *MCP_CALLERS):
            continue
        for imp in imports(sf):
            if any(_matches(imp.module, pkg) for pkg in MCP_PACKAGES):
                violations.append(
                    f"{sf.rel}:{imp.line}: imports MCP client {imp.module}; "
                    f"only {'/, '.join(MCP_CALLERS)}/ may"
                )
    return violations


# --------------------------------------------------------------------------- #
# Rule 7: every ingest/ module declares its source class and target stores
# --------------------------------------------------------------------------- #


def _valued_fields(index, key: ClassKey, seen: frozenset[ClassKey] = frozenset()) -> set[str]:
    """Names assigned a value on a class or its resolvable bases; bare annotations don't count."""
    sf, cls = index[key]
    fields = {name for name, value in _body_assigns(cls).items() if value is not None}
    for base in cls.bases:
        base_key = _resolve_class(index, sf, base)
        if base_key is not None and base_key not in seen | {key}:
            fields |= _valued_fields(index, base_key, seen | {key})
    return fields


def check_ingest_modules_declare(files: tuple[SourceFile, ...]) -> list[str]:
    index = _class_index(files)
    units: dict[str, list[SourceFile]] = {}
    for sf in files:
        if sf.under("ingest") and str(sf.rel) not in INGEST_INFRA:
            units.setdefault("/".join(sf.rel.parts[:2]), []).append(sf)

    violations = []
    for unit, sfs in sorted(units.items()):
        declared = any(
            ADAPTER_DECLARATIONS <= _valued_fields(index, (sf.module, node.name))
            for sf in sfs
            for node in sf.tree.body
            if isinstance(node, ast.ClassDef)
        )
        if not declared:
            violations.append(
                f"{sfs[0].rel}:1: {unit} has no adapter assigning {sorted(ADAPTER_DECLARATIONS)}"
            )
    return violations


# --------------------------------------------------------------------------- #
# Tests against the repo
# --------------------------------------------------------------------------- #

RULES: dict[str, Callable[[tuple[SourceFile, ...]], list[str]]] = {
    "gateway": check_gateway_imports,
    "provider_sdk": check_provider_sdk_imports,
    "float_money": check_no_float_money,
    "provenance": check_provenance_columns,
    "compute_pure": check_compute_is_pure,
    "pit_reads": check_reads_go_through_pit,
    "mcp_client": check_mcp_imports,
    "adapter_declares": check_ingest_modules_declare,
}


@pytest.mark.parametrize("rule", RULES)
def test_repo_obeys_rule(rule):
    violations = RULES[rule](repo_files())
    assert not violations, "\n" + "\n".join(violations)


def test_scan_is_not_vacuous():
    rels = {str(sf.rel) for sf in repo_files()}
    assert {
        "core/db/models.py", "core/db/base.py", "core/db/pit.py", "core/compute/isin.py",
        "core/blob.py", "ingest/base.py", "ingest/registry.py",
    } <= rels  # fmt: skip
    assert not any(r.startswith((".venv/", "equity_knowledge.egg-info/")) for r in rels)

    stores = (
        "FinancialFact", "RawSourceFile", "Entity", "EntityIsin",
        "IndexSnapshot", "IndexSnapshotConstituent", "IndexSnapshotQuarantine",
    )  # fmt: skip
    models = {cls.name: fields for _, cls, fields, _ in table_models(repo_files())}
    assert set(stores) <= set(models)
    for name in stores:
        assert PROVENANCE_COLUMNS <= models[name]  # inherited via ProvenanceMixin


def test_ingest_infra_list_matches_registry():
    from ingest.registry import INFRA_MODULES

    assert {m.replace(".", "/") + ".py" for m in INFRA_MODULES} | {"ingest/__init__.py"} == INGEST_INFRA


def test_orm_metadata_has_provenance_columns():
    """Runtime cross-check of rule 3, for columns the AST pass cannot see (e.g. declared_attr)."""
    import core.db.models  # noqa: F401  registers tables on Base.metadata
    from core.db.base import Base

    assert Base.metadata.sorted_tables
    for table in Base.metadata.sorted_tables:
        assert PROVENANCE_COLUMNS <= set(table.c.keys()), table.name


# --------------------------------------------------------------------------- #
# Tests that each rule actually fires
# --------------------------------------------------------------------------- #


def _file(rel: str, source: str) -> SourceFile:
    return SourceFile(PurePosixPath(rel), ast.parse(textwrap.dedent(source)))


PROVENANCE_MIXIN = textwrap.dedent("""
    class ProvenanceMixin:
        as_of: Mapped[datetime]
        content_hash: Mapped[str]
        source_url: Mapped[str]
        extracted_by: Mapped[str]
        model_version: Mapped[str | None]
""")

STORE = "class Fact(Base):\n    __tablename__ = 'facts'\n"

CASES = [
    # rule, path, source, expected violation count
    ("gateway", "core/db/x.py", "from gateway.client import complete", 1),
    ("gateway", "ingest/bse.py", "import gateway", 1),
    ("gateway", "resolve/x.py", "from gateway import client", 1),
    ("gateway", "core/db/x.py", "from ...gateway import client", 1),
    ("gateway", "core/compute/x.py", "import importlib\nimportlib.import_module('gateway.client')", 1),
    ("gateway", "extract/guidance.py", "from gateway.client import complete", 0),
    ("gateway", "narrate/report.py", "import gateway", 0),
    ("gateway", "tests/test_extract.py", "from gateway import client", 0),
    ("gateway", "gateway/client.py", "from gateway.schemas import Claim", 0),
    ("gateway", "ingest/x.py", "from core.db import gateway_log", 0),
    ("provider_sdk", "extract/guidance.py", "import anthropic", 1),
    ("provider_sdk", "narrate/report.py", "from openai import OpenAI", 1),
    ("provider_sdk", "tests/test_x.py", "from langchain_anthropic import ChatAnthropic", 1),
    ("provider_sdk", "gateway/client.py", "import anthropic", 0),
    ("float_money", "core/compute/ratios.py", "def roce(ebit, capital) -> float: ...", 1),
    ("float_money", "core/db/models.py", "class M(Base):\n    ratio: Mapped[float | None]", 1),
    ("float_money", "core/db/models.py", "class M(Base):\n    ratio: 'Mapped[float]'", 1),
    ("float_money", "ingest/upstox.py", "class Candle:\n    close: 'float'", 1),
    ("float_money", "ingest/x.py", "def f(revenue_inr: Optional[float]): ...", 1),
    ("float_money", "migrations/versions/0002.py", "import sqlalchemy as sa\nsa.Column('v', sa.Float())", 1),
    ("float_money", "core/db/m.py", "from sqlalchemy import Numeric\nx = Numeric(28, 6, asdecimal=False)", 1),
    ("float_money", "ingest/x.py", "def fetch(timeout: float, price: Decimal) -> bytes: ...", 0),
    ("float_money", "tests/test_x.py", "def test_x(price: float): ...", 0),
    ("provenance", "core/db/models.py", "class Bad(Base):\n    __tablename__ = 'bad'\n    id: Mapped[int]", 1),
    ("provenance", "core/db/models.py", PROVENANCE_MIXIN + "class Ok(ProvenanceMixin, Base):\n    __tablename__ = 'ok'", 0),
    ("provenance", "core/db/models.py", "class A(Base):\n    __abstract__ = True", 0),
    ("provenance", "core/db/t.py", "t = Table('t', metadata, Column('id', Integer))", 1),
    ("provenance", "migrations/versions/0002_x.py", "op.create_table('t', sa.Column('as_of', sa.DateTime()))", 1),
    ("compute_pure", "core/compute/r.py", "import os", 1),
    ("compute_pure", "core/compute/r.py", "from pathlib import Path", 1),
    ("compute_pure", "core/compute/r.py", "from core.db.models import FinancialFact", 1),
    ("compute_pure", "core/compute/r.py", "from core import config", 1),
    ("compute_pure", "core/compute/r.py", "from sqlalchemy import select", 1),
    ("compute_pure", "core/compute/r.py", "import zoneinfo", 1),
    ("compute_pure", "core/compute/r.py", "def f():\n    return open('x').read()", 1),
    ("compute_pure", "core/compute/r.py", "from datetime import datetime\ndatetime.now()", 1),
    ("compute_pure", "core/compute/r.py", "from decimal import Decimal\nfrom collections.abc import Sequence", 0),
    ("compute_pure", "core/compute/r.py", "from core.compute.isin import is_valid_isin\nfrom . import hashing", 0),
    ("compute_pure", "core/db/x.py", "import os", 0),
    ("pit_reads", "ingest/x.py", STORE + "stmt = select(Fact).where(Fact.as_of <= t)", 1),
    ("pit_reads", "resolve/x.py", STORE + "session.get(Fact, 1)", 1),
    ("pit_reads", "narrate/x.py", STORE + "session.query(Fact.value)", 1),
    ("pit_reads", "resolve/x.py", STORE + "session.execute(text('SELECT * FROM facts WHERE isin = :i'))", 1),
    ("pit_reads", "ingest/x.py", STORE + "session.add(Fact(isin='x'))", 0),
    ("pit_reads", "ingest/x.py", STORE + '"""Loads rows from facts files."""', 0),
    ("pit_reads", "core/db/pit.py", STORE + "stmt = select(Fact)", 0),
    ("pit_reads", "migrations/versions/0002_x.py", STORE + "op.execute('SELECT 1 FROM facts')", 0),
    ("mcp_client", "extract/x.py", "from mcp import ClientSession", 1),
    ("mcp_client", "core/db/x.py", "import mcp.client.stdio", 1),
    ("mcp_client", "narrate/x.py", "from fastmcp import Client", 1),
    ("mcp_client", "core/x.py", "import importlib\nimportlib.import_module('mcp')", 1),
    ("mcp_client", "ingest/broker_holdings.py", "from mcp import ClientSession", 0),
    ("mcp_client", "gateway/tools.py", "from mcp.client.session import ClientSession", 0),
    ("mcp_client", "tests/test_x.py", "import mcp", 0),
    ("mcp_client", "core/x.py", "from core import mcp_notes\nimport mcpx", 0),
    ("adapter_declares", "ingest/nse.py", "class A(Adapter):\n    name = 'nse'\n    source_class = S.WEB_SCRAPE\n    target_stores = ('t',)", 0),
    ("adapter_declares", "ingest/nse.py", "class A(Adapter):\n    name = 'nse'\n    source_class = S.WEB_SCRAPE", 1),
    ("adapter_declares", "ingest/nse.py", "class A(Adapter):\n    name: ClassVar[str]\n    source_class: ClassVar[S]\n    target_stores: ClassVar[tuple]", 1),
    ("adapter_declares", "ingest/helpers.py", "def parse(): ...", 1),
    ("adapter_declares", "ingest/nse/parser.py", "def parse(): ...", 1),
    ("adapter_declares", "ingest/drop.py", "class D(Adapter):\n    source_class = S.MANUAL_DROP\nclass X(D):\n    name = 'x'\n    target_stores = ('t',)", 0),
    ("adapter_declares", "ingest/base.py", "class Adapter(ABC):\n    name: ClassVar[str]", 0),
    ("adapter_declares", "core/db/x.py", "def f(): ...", 0),
]  # fmt: skip


@pytest.mark.parametrize(
    ("rule", "rel", "source", "expected"),
    CASES,
    ids=[f"{rule}-{rel}-{i}" for i, (rule, rel, _, _) in enumerate(CASES)],
)
def test_rule_detects(rule, rel, source, expected):
    violations = RULES[rule]((_file(rel, source),))
    assert len(violations) == expected, violations
