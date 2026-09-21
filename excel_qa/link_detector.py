"""Finds which columns connect one sheet to another (foreign key -> primary key), from the data alone."""

from __future__ import annotations

import re
from dataclasses import dataclass

from excel_qa.file_loader import DataStore, TableInfo
from excel_qa.logger import log

_KEY_LIKE_NAME = re.compile(r"(^|_)(id|key|code|no|num|number|ref)($|_)")
_INTEGER_TYPES = ("BIGINT", "INTEGER", "HUGEINT", "SMALLINT", "TINYINT")
MAX_PRIMARY_KEY_VALUES = 100_000
MAX_FOREIGN_KEY_VALUES = 1_000        # a sample is enough to test containment


@dataclass
class TableLink:
    """from_table.from_column points at to_table.to_column (which is unique there)."""
    from_table: str
    from_column: str
    to_table: str
    to_column: str
    match_share: float                 # 0..1, share of from_column values found in to_column


def _distinct_values(store: DataStore, table: str, column: str, limit: int) -> set[str]:
    rows = store.con.execute(
        f'SELECT DISTINCT "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL LIMIT {limit}'
    ).fetchall()
    values = set()
    for (value,) in rows:
        if isinstance(value, float) and value.is_integer():     # ids with blanks load as 12.0
            value = int(value)
        values.add(str(value).strip().lower())
    return values


def _find_key_columns(store: DataStore, table: TableInfo) -> tuple[list[dict], list[dict]]:
    """Splits a table's columns into possible primary keys and possible foreign keys."""
    primary_keys, foreign_keys = [], []
    for position, (column, column_type) in enumerate(zip(table.columns, table.types)):
        is_text = column_type == "VARCHAR"
        is_integer = column_type in _INTEGER_TYPES
        has_key_name = bool(_KEY_LIKE_NAME.search(column))
        # ids containing blanks become DOUBLE in pandas, so accept those only when named like a key
        if not (is_text or is_integer or (column_type == "DOUBLE" and has_key_name)):
            continue

        distinct, filled = store.con.execute(
            f'SELECT COUNT(DISTINCT "{column}"), COUNT("{column}") FROM "{table.name}"'
        ).fetchone()
        if distinct < 2:
            continue

        candidate = {"column": column, "is_number": not is_text, "position": position}
        if distinct == table.row_count == filled:                # unique with no blanks
            primary_keys.append(candidate)
        if has_key_name or is_text:
            foreign_keys.append(candidate)
    return primary_keys, foreign_keys


def _names_agree(from_column: str, to_column: str) -> bool:
    """owner_id or assigned_owner_id -> owner_id"""
    return from_column == to_column or from_column.endswith("_" + to_column)


def detect_links(store: DataStore, min_match_share: float = 0.9, max_links: int = 200) -> list[TableLink]:
    """Fills store.links with every foreign key -> primary key relationship found in the data."""
    keys = {name: _find_key_columns(store, table) for name, table in store.tables.items()}
    primary_key_values = {
        (name, pk["column"]): _distinct_values(store, name, pk["column"], MAX_PRIMARY_KEY_VALUES)
        for name, (primary_keys, _) in keys.items() for pk in primary_keys
    }

    links: list[TableLink] = []
    for from_table, (_, foreign_keys) in keys.items():
        for fk in foreign_keys:
            fk_values = None
            best = None                                          # (score, TableLink)
            for to_table, (primary_keys, _) in keys.items():
                if to_table == from_table:
                    continue
                for pk in primary_keys:
                    if fk["is_number"] != pk["is_number"]:
                        continue
                    # number ids are all 1, 2, 3... so values prove nothing: the names must agree too
                    if fk["is_number"] and not _names_agree(fk["column"], pk["column"]):
                        continue
                    if fk_values is None:
                        fk_values = _distinct_values(store, from_table, fk["column"], MAX_FOREIGN_KEY_VALUES)
                    if not fk_values:
                        continue
                    match_share = len(fk_values & primary_key_values[(to_table, pk["column"])]) / len(fk_values)
                    if match_share < min_match_share:
                        continue
                    # tie-break: the real parent usually has the key as its first column, with the same name
                    score = (match_share, pk["position"] == 0, fk["column"] == pk["column"])
                    if best is None or score > best[0]:
                        best = (score, TableLink(from_table, fk["column"], to_table, pk["column"], round(match_share, 2)))
            if best:
                links.append(best[1])

    links.sort(key=lambda link: -link.match_share)
    store.links = links[:max_links]
    log.info("LINKS     %d link(s) detected across %d table(s)", len(store.links), len(store.tables))
    return store.links
