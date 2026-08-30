"""The fixture redactor must stop a leak, not report one after writing.

`read_write_token` grants write access to a thread, so `redact` is a gate: it either
returns text with every listed key replaced, or it exits.
"""

import importlib.util
import pathlib

import pytest

SCRIPT = pathlib.Path(__file__).parent.parent / "spike" / "make_fixtures.py"
spec = importlib.util.spec_from_file_location("make_fixtures", SCRIPT)
make_fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(make_fixtures)


def test_redacts_every_listed_key():
    text = '{"author_id": "u1", "read_write_token": "secret", "answer": "keep me"}'
    out = make_fixtures.redact(text)
    assert "secret" not in out and "u1" not in out
    assert "keep me" in out


def test_redacts_a_value_containing_an_escaped_quote():
    out = make_fixtures.redact(
        r'{"read_write_token": "a\"secret", "answer": "keep me"}'
    )
    assert "secret" not in out
    assert "keep me" in out


def test_exits_when_a_value_survives():
    # A capture cut mid-value leaves an unterminated string the substitution misses.
    with pytest.raises(SystemExit) as e:
        make_fixtures.redact('{"read_write_token": "abc')
    assert "read_write_token" in str(e.value)
