# Tests for the pure SQL helpers in utils/postgres. No db.

import pytest

from src.utils import postgres


def test_to_positional_binds_a_reused_name_once():
    sql, args = postgres.to_positional("SELECT * FROM t WHERE a = $a OR b = $a AND c = $c", {"a": 1, "c": 2})
    assert sql == "SELECT * FROM t WHERE a = $1 OR b = $1 AND c = $2"
    assert args == [1, 2]


def test_to_positional_leaves_positional_and_dollar_quotes_alone():
    sql, args = postgres.to_positional("SELECT $1, $$x$$, $name", {"name": "n"})
    assert sql == "SELECT $1, $$x$$, $1"
    assert args == ["n"]


def test_to_positional_fails_loud_on_a_missing_param():
    with pytest.raises(KeyError):
        postgres.to_positional("SELECT $missing", {})


def test_dynamic_clauses_render_named_placeholders():
    assert postgres.to_set_clause({"a": 1, "b": 2}) == "a = $a, b = $b"
    assert postgres.to_where_clause({"a": 1, "b": 2}) == "a = $a AND b = $b"
    assert postgres.to_where_clause({}) == "TRUE"
    assert postgres.to_insert_clause({"a": 1, "b": 2}) == "(a, b) VALUES ($a, $b)"


def test_dynamic_clauses_refuse_non_identifier_keys():
    with pytest.raises(ValueError):
        postgres.to_set_clause({"a = 1; DROP TABLE users; --": 1})
