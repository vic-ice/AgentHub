from __future__ import annotations

import re
from typing import Any


_ENTITY_SCHEMAS = frozenset(
    {
        "possession.entity",
        "preference.entity",
        "relationship.entity",
        "entity.name",
    }
)
_PET_SPECIES = {
    "猫": "cat",
    "猫咪": "cat",
    "小猫": "cat",
    "狗": "dog",
    "狗狗": "dog",
    "小狗": "dog",
}
_PET_SUFFIXES = tuple(sorted(_PET_SPECIES, key=len, reverse=True))
_LEADING_ENTITY_NOISE_RE = re.compile(
    r"^(?:我的|我家的|这只|那只|这个|那个|一只|一个|一条|只|个|条)\s*"
)
_TRAILING_ENTITY_NOISE_RE = re.compile(
    r"\s*(?:这个|那个|这只|那只)$"
)


def normalize_entity_fact(
    schema_key: str,
    value: dict[str, Any],
    qualifiers: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Normalize open-vocabulary entity slots before identity hashing.

    The evidence quote remains the authoritative raw wording. The structured
    fact keeps a stable entity spelling without guessing that every word
    containing 猫/狗 is a pet.  Entity suffix stripping is only allowed when
    upstream extraction explicitly marks the entity as a pet name.
    """

    if schema_key not in _ENTITY_SCHEMAS:
        return value, qualifiers
    entity = str(value.get("entity") or "").strip()
    if not entity:
        return value, qualifiers

    normalized = _normalize_entity_name(entity)
    explicit_species = _species_from_qualifier(qualifiers.get("species"))
    exact_species = _species_from_exact_entity(normalized)
    species = explicit_species or exact_species
    entity_type = str(qualifiers.get("entity_type") or "").strip().lower()
    if schema_key == "possession.entity" and exact_species and entity_type in {"", "animal"}:
        entity_type = "pet"
    if explicit_species and _explicitly_pet_like(qualifiers) and entity_type in {"", "animal"}:
        entity_type = "pet"

    canonical_name = normalized
    if _should_strip_species_suffix(
        canonical_name,
        explicit_species=explicit_species,
        qualifiers=qualifiers,
    ):
        canonical_name = _strip_species_suffix(canonical_name)
    canonical_name = canonical_name or normalized or entity

    next_value = dict(value)
    next_value["entity"] = canonical_name
    next_qualifiers = dict(qualifiers)
    if entity_type:
        next_qualifiers["entity_type"] = entity_type
    if species:
        next_qualifiers["species"] = species
    return next_value, next_qualifiers


def _normalize_entity_name(value: Any) -> str:
    text = " ".join(str(value or "").split()).strip()
    text = text.strip(" \t\r\n,.;:!?，。！？；：\"'“”‘’（）()[]{}")
    previous = ""
    while previous != text:
        previous = text
        text = _LEADING_ENTITY_NOISE_RE.sub("", text).strip()
        text = _TRAILING_ENTITY_NOISE_RE.sub("", text).strip()
    return text


def _strip_species_suffix(value: str) -> str:
    text = value.strip()
    for suffix in _PET_SUFFIXES:
        if text.endswith(suffix) and len(text) > len(suffix):
            return text[: -len(suffix)].strip()
    return text


def _species_from_qualifier(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"cat", "dog"}:
        return text
    return _PET_SPECIES.get(text, "")


def _species_from_exact_entity(value: Any) -> str:
    text = str(value or "").strip()
    return _PET_SPECIES.get(text, "")


def _explicitly_pet_like(qualifiers: dict[str, Any]) -> bool:
    entity_type = str(qualifiers.get("entity_type") or "").strip().lower()
    return entity_type in {"pet", "animal"}


def _should_strip_species_suffix(
    value: str,
    *,
    explicit_species: str,
    qualifiers: dict[str, Any],
) -> bool:
    if not explicit_species:
        return False
    role = str(
        qualifiers.get("entity_role")
        or qualifiers.get("name_kind")
        or ""
    ).strip().lower()
    return role in {"pet_name", "named_pet"}

