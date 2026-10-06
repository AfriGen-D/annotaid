"""Typed feature definitions — one place where every default is applied.

Before this, `Config.features` was a list of raw dicts and four separate modules
each re-derived the same defaults (`f.get("nullable", True)` appeared in
config_loader, importer, schema_builder and validation). Changing a default meant
finding all four. Now `parse_features` normalises once and hands back frozen
objects, and `isinstance(f, Group)` replaces stringly-typed `f["type"] == "group"`
checks at every call site.

Two levels, deliberately. A Group holds Leafs and nothing else — a group inside a
group is a config error. That cap lives in ONE place (`parse_features`), so
lifting it later is a local change rather than a hunt. The cap exists because
depth is what makes the prompt, the editor and the curation pane hard — storage
would need only a parent-row column to go deeper.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Names address a feature, and a field inside a group is addressed by a DOTTED
# path ("variants.p_value") in CSV column sources and in find(). A name carrying
# a dot would make that ambiguous, so the character is refused outright rather
# than silently mis-resolving later.
NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")

LEAF_TYPES = (
    "string",
    "boolean",
    "number",
    "enum",
    "array<string>",
    "array<number>",
)
GROUP_TYPE = "group"
FEATURE_TYPES = list(LEAF_TYPES) + [GROUP_TYPE]

# A group with no cap could be asked to return a 200-row supplementary table in
# one response. Extraction refuses beyond this; the curator can still add rows.
DEFAULT_MAX_ITEMS = 50


class FeatureError(Exception):
    pass


@dataclass(frozen=True)
class Leaf:
    """A single curated value."""

    name: str
    type: str
    description: str = ""
    nullable: bool = True
    label: str = ""
    enum_values: tuple = ()
    # Only meaningful inside a Group: marks a field that names a row.
    identifier: bool = False

    @property
    def is_group(self) -> bool:
        return False

    @property
    def display(self) -> str:
        return self.label or self.name

    def type_hint(self) -> str:
        """Human-readable type, for the prompt."""
        if self.type == "enum":
            return "one of: " + ", ".join(self.enum_values)
        return self.type

    def empty_value(self):
        if self.type.startswith("array"):
            return []
        if self.type == "boolean":
            return None if self.nullable else False
        return None

    def to_dict(self) -> dict:
        out = {
            "name": self.name,
            "type": self.type,
            "nullable": self.nullable,
            "description": self.description,
        }
        if self.label:
            out["label"] = self.label
        if self.type == "enum":
            out["enumValues"] = list(self.enum_values)
        if self.identifier:
            out["identifier"] = True
        return out


@dataclass(frozen=True)
class Group:
    """A repeating set of values hanging off one paper — e.g. one entry per
    genetic variant, each with its own rsid / odds ratio / p-value."""

    name: str
    description: str = ""
    label: str = ""
    features: tuple = ()          # tuple[Leaf, ...]
    min_items: int = 0
    max_items: int = DEFAULT_MAX_ITEMS
    type: str = GROUP_TYPE

    @property
    def is_group(self) -> bool:
        return True

    @property
    def display(self) -> str:
        return self.label or self.name

    def identifiers(self) -> tuple:
        return tuple(f for f in self.features if f.identifier)

    def feature(self, name: str):
        for f in self.features:
            if f.name == name:
                return f
        return None

    def row_label(self, values: dict) -> str:
        """Name a row from its identifier values: the first one with a value.

        `values` maps a feature name to its curated value. Falls back to a
        placeholder rather than an empty string — a row must always be
        identifiable in a list, even before the curator has filled it in.
        """
        for f in self.identifiers():
            v = values.get(f.name)
            if v not in (None, "", []):
                return str(v)
        return "(unidentified)"

    def to_dict(self) -> dict:
        out = {
            "name": self.name,
            "type": GROUP_TYPE,
            "description": self.description,
            "features": [f.to_dict() for f in self.features],
        }
        if self.label:
            out["label"] = self.label
        if self.min_items:
            out["min"] = self.min_items
        if self.max_items != DEFAULT_MAX_ITEMS:
            out["maxItems"] = self.max_items
        return out


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def _check_name(name: str, where: str) -> None:
    if not NAME_RE.match(name):
        raise FeatureError(
            f"{where}: {name!r} is not a usable feature name — start with a "
            "letter or underscore, then letters, digits, _ or - (no dots or "
            "spaces, because a dot separates a group from its field)"
        )


def _parse_leaf(raw: dict, where: str) -> Leaf:
    name = str(raw.get("name", "")).strip()
    ftype = raw.get("type")
    if ftype not in LEAF_TYPES:
        raise FeatureError(f"{where}: unknown feature type {ftype!r}")

    enum_values = tuple(raw.get("enumValues") or ())
    if ftype == "enum":
        if not enum_values:
            raise FeatureError(f"{where}: feature {name!r} is enum but has no enumValues")
        if len(set(enum_values)) != len(enum_values):
            raise FeatureError(f"{where}: feature {name!r} has duplicate enumValues")
    elif enum_values:
        raise FeatureError(f"{where}: feature {name!r} is {ftype}, so enumValues is meaningless")

    return Leaf(
        name=name,
        type=ftype,
        description=str(raw.get("description") or "").strip(),
        nullable=bool(raw.get("nullable", True)),
        label=str(raw.get("label") or "").strip(),
        enum_values=enum_values,
        identifier=bool(raw.get("identifier", False)),
    )


def _parse_group(raw: dict, where: str) -> Group:
    name = str(raw.get("name", "")).strip()
    children_raw = raw.get("features")
    if not isinstance(children_raw, list) or not children_raw:
        raise FeatureError(f"{where}: group {name!r} must declare at least one feature")

    children = []
    seen = set()
    for i, child in enumerate(children_raw):
        if not isinstance(child, dict):
            raise FeatureError(f"{where}: group {name!r} feature #{i + 1} is not an object")
        if child.get("type") == GROUP_TYPE:
            # The two-level cap. Storage would cope; the prompt, the editor and
            # the curation pane are what depth actually costs.
            raise FeatureError(
                f"{where}: group {name!r} contains another group "
                f"({child.get('name')!r}) — nesting is limited to two levels"
            )
        leaf = _parse_leaf(child, f"{where} > {name}")
        if not leaf.name:
            raise FeatureError(f"{where}: group {name!r} feature #{i + 1} has no name")
        _check_name(leaf.name, f"{where} > {name}")
        if leaf.name in seen:
            raise FeatureError(f"{where}: group {name!r} has a duplicate feature: {leaf.name}")
        seen.add(leaf.name)
        children.append(leaf)

    group = Group(
        name=name,
        description=str(raw.get("description") or "").strip(),
        label=str(raw.get("label") or "").strip(),
        features=tuple(children),
        min_items=max(0, int(raw.get("min") or 0)),
        max_items=max(1, int(raw.get("maxItems") or DEFAULT_MAX_ITEMS)),
    )

    # No "must have an identifier" check: pass 1 discovers row identity and the
    # curator reviews it before pass 2. `identifier: true` remains accepted for
    # compatibility with older project files, but nothing requires it.
    if group.min_items > group.max_items:
        raise FeatureError(
            f"{where}: group {name!r} has min ({group.min_items}) above "
            f"maxItems ({group.max_items})"
        )
    return group


def parse_features(raw_list, where: str = "config") -> list:
    """Normalise a raw features array into Leaf / Group objects."""
    if not isinstance(raw_list, list) or not raw_list:
        raise FeatureError(f"{where}: features must be a non-empty array")

    # At most one repeating group per project. Multiple independent groups
    # (each with its own discovery/review pass) multiply the "which rows exist"
    # question by however many groups there are — one group keeps that
    # question singular, which is the whole point of the simplification.
    group_count = sum(
        1 for raw in raw_list if isinstance(raw, dict) and raw.get("type") == GROUP_TYPE
    )
    if group_count > 1:
        raise FeatureError(
            f"{where}: a project may have at most one repeating group "
            f"({group_count} declared)"
        )

    out, seen = [], set()
    for i, raw in enumerate(raw_list):
        if not isinstance(raw, dict):
            raise FeatureError(f"{where}: feature #{i + 1} is not an object")
        name = str(raw.get("name", "")).strip()
        if not name:
            raise FeatureError(f"{where}: feature #{i + 1} has no name")
        if name in seen:
            raise FeatureError(f"{where}: duplicate feature name: {name}")
        _check_name(name, where)
        seen.add(name)

        if raw.get("type") == GROUP_TYPE:
            out.append(_parse_group(raw, where))
        else:
            leaf = _parse_leaf(raw, where)
            if leaf.identifier:
                # Outside a group there are no rows to identify, so the flag
                # would be silently inert — say so instead.
                raise FeatureError(
                    f"{where}: feature {name!r} is marked as an identifier but is "
                    "not inside a group"
                )
            out.append(leaf)
    return out


# --------------------------------------------------------------------------- #
# Views over a parsed feature list
# --------------------------------------------------------------------------- #
def leaves(features) -> list:
    """Paper-level leaf features only (groups excluded)."""
    return [f for f in features if not f.is_group]


def groups(features) -> list:
    return [f for f in features if f.is_group]


def find(features, path: str):
    """Resolve a feature by name or dotted path ('variants.p_value').

    Dotted paths are why nesting beats a flat namespace: a study-level p_value
    and a per-variant p_value are different things and may share a name.
    """
    head, _, rest = str(path).partition(".")
    for f in features:
        if f.name != head:
            continue
        if not rest:
            return f
        return f.feature(rest) if f.is_group else None
    return None


def group_cell_templates(features) -> dict:
    """{groupName: {featureName: {"type", "empty"}}}.

    Everything project_store needs in order to create a CURATOR-added row
    without importing the config: a new row's cells need a declared type, and
    their aiValue must be the type's empty value (the AI proposed nothing, so
    "AI said nothing, human supplied this" is the truthful audit pair).
    """
    return {
        g.name: {
            c.name: {"type": c.type, "empty": c.empty_value()} for c in g.features
        }
        for g in groups(features)
    }


def paths(features) -> list:
    """Every curatable path, in declaration order."""
    out = []
    for f in features:
        if f.is_group:
            out.extend(f"{f.name}.{c.name}" for c in f.features)
        else:
            out.append(f.name)
    return out
