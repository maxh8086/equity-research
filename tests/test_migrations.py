"""Static guard on the Alembic revision graph (no database needed).

Parallel worktrees each claim "the next migration number" and collide, giving
two migrations with the same id or two heads. That used to surface only at
deploy time; these tests make it fail in CI / at merge instead.
"""

from collections import defaultdict
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

REPO_ROOT = Path(__file__).resolve().parent.parent


def _script_dir() -> ScriptDirectory:
    return ScriptDirectory.from_config(Config(str(REPO_ROOT / "alembic.ini")))


def _revisions():
    return list(_script_dir().walk_revisions())


def _down_revisions(rev) -> tuple[str, ...]:
    down = rev.down_revision
    if down is None:
        return ()
    return (down,) if isinstance(down, str) else tuple(down)


def test_single_head():
    heads = _script_dir().get_heads()
    assert len(heads) == 1, (
        f"Migration history has {len(heads)} heads: {sorted(heads)}. Two "
        "sessions probably added migrations off the same parent. Rebase one "
        "onto the other: set its down_revision to the other's revision id "
        "(fetch origin/main first) so the chain has a single head."
    )


def test_revision_ids_unique_and_match_filenames():
    script_dir = _script_dir()
    files = [
        f for f in sorted(Path(script_dir.versions).glob("*.py")) if f.name != "__init__.py"
    ]

    seen: dict[str, list[str]] = defaultdict(list)
    bad_names = []
    for rev in _revisions():
        path = Path(rev.path)
        seen[rev.revision].append(path.name)
        if not path.stem.startswith(f"{rev.revision}_") and path.stem != rev.revision:
            bad_names.append((path.name, rev.revision))

    dupes = {r: names for r, names in seen.items() if len(names) > 1}
    assert not dupes, (
        f"Duplicate revision ids: {dupes}. Two sessions claimed the same "
        "number. Give the newer migration a new id (e.g. NNNN_short_slug with "
        "the next free number), rename the file to match, and rebase its "
        "down_revision onto the current head."
    )
    assert not bad_names, (
        f"Migration file names do not start with their revision id: "
        f"{bad_names}. Rename each file to '<revision>_<slug>.py' (or "
        "'<revision>.py'), or fix the revision string inside it."
    )
    assert len(files) == len(seen), (
        f"{len(files)} migration files but {len(seen)} revisions were loaded. "
        "Two files probably declare the same revision id; give one a new id."
    )


def test_every_down_revision_exists():
    revs = _revisions()
    known = {r.revision for r in revs}
    missing = [
        (r.revision, d) for r in revs for d in _down_revisions(r) if d not in known
    ]
    assert not missing, (
        f"down_revision points at a revision that does not exist: {missing} "
        "as (revision, missing parent). Fix the down_revision string, or "
        "restore the missing migration file."
    )


def test_chain_is_linear_and_fully_reachable():
    revs = _revisions()
    children: dict[str, list[str]] = defaultdict(list)
    for r in revs:
        for d in _down_revisions(r):
            children[d].append(r.revision)

    branches = {p: sorted(c) for p, c in children.items() if len(c) > 1}
    assert not branches, (
        f"Migration chain branches: {branches} (parent -> children). Two "
        "migrations both descend from the same parent. Rebase one onto the "
        "other's revision (set its down_revision to the other's id) and "
        "rename it to the next free number."
    )

    merges = [r.revision for r in revs if len(_down_revisions(r)) > 1]
    assert not merges, (
        f"Merge migrations found: {merges}. The chain must stay linear; give "
        "each migration exactly one down_revision."
    )

    bases = [r.revision for r in revs if not _down_revisions(r)]
    assert len(bases) == 1, (
        f"Expected exactly one base migration (down_revision = None), found "
        f"{bases}. Only the first migration may have down_revision None."
    )

    by_id = {r.revision: r for r in revs}
    heads = _script_dir().get_heads()
    visited: list[str] = []
    current = heads[0] if len(heads) == 1 else None
    while current is not None:
        visited.append(current)
        parents = _down_revisions(by_id[current])
        current = parents[0] if parents else None
    unreachable = sorted(set(by_id) - set(visited))
    assert not unreachable and len(visited) == len(revs), (
        f"Walking from head reached {len(visited)} of {len(revs)} migrations; "
        f"unreachable: {unreachable}. Some migration is not on the single "
        "chain; rebase it onto the current head."
    )
