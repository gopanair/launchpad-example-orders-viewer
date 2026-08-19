"""Reading one table out of a SQLite file nobody here wrote.

Three rules run through this module.

**Open read-only.** `mode=ro` on the URI means a missing file is an error
instead of an empty database silently created on someone else's volume, and it
means this viewer can never be the process that damages the data it is showing.

**The schema is discovered, never assumed.** Column names come from
`PRAGMA table_info`, so the page shows whatever the table actually has and a
column added upstream appears without a change here.

**Values are bound; identifiers are whitelisted.** A sort column arrives from
a query string and can never be interpolated — it is accepted only if it is one
of the names the schema just returned. The filter text is always a parameter.
Between them that is the whole injection surface of this app.
"""

import os
import re
import sqlite3

# SQLite's declared types are free-form text; these are the substrings that
# mean "a number", which is all this app needs them for — right-aligning a
# column reads as a mistake when the values are words.
NUMERIC_HINTS = ("INT", "REAL", "FLOA", "DOUB", "NUM", "DEC")

ASC, DESC = "asc", "desc"


class TableMissing(Exception):
    """The database opened, but the table this app exists to show is not in it."""

    def __init__(self, table, present):
        self.table = table
        self.present = present
        super().__init__("no table named %r" % table)


def connect(path):
    """Open the database read-only.

    A read-only connection to a database in WAL mode needs to create a shared
    -shm file beside it, so a read-only *mount* can refuse this. That surfaces
    as an sqlite3.OperationalError, which the page reports as a sentence — it
    is a real condition an operator has to know about, not a bug to swallow.
    """
    uri = "file:" + _uri_path(os.path.abspath(path)) + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def _uri_path(path):
    """Percent-encode a filesystem path for a file: URI."""
    safe = "/-._~"
    out = []
    for byte in path.encode("utf-8"):
        char = chr(byte)
        out.append(char if char.isalnum() and byte < 128 or char in safe else "%%%02X" % byte)
    return "".join(out)


def table_names(conn):
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view') "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [r["name"] for r in rows]


def columns(conn, table):
    """The table's columns, or TableMissing with what is there instead.

    PRAGMA table_info takes an identifier, so the name is checked against
    sqlite_master first rather than quoted into the statement on trust.
    """
    present = table_names(conn)
    if table not in present:
        raise TableMissing(table, present)
    rows = conn.execute("PRAGMA table_info(%s)" % quote_ident(table)).fetchall()
    return [
        {"name": r["name"], "type": (r["type"] or "").upper(),
         "numeric": any(h in (r["type"] or "").upper() for h in NUMERIC_HINTS)}
        for r in rows
    ]


def quote_ident(name):
    """Quote an identifier that came from the schema, never from a request."""
    return '"' + name.replace('"', '""') + '"'


def normalize(params, cols):
    """Turn the query string into the four things a page needs.

    Anything unrecognised falls back to the default rather than erroring: a
    hand-edited URL or a stale bookmark should show the first page of the
    table, not a 400.
    """
    names = [c["name"] for c in cols]
    sort = params.get("sort")
    if sort not in names:
        sort = None
    direction = DESC if params.get("dir") == DESC else ASC
    column = params.get("col")
    if column not in names:
        column = ""
    return {
        "q": (params.get("q") or "").strip(),
        "col": column,
        "sort": sort,
        "dir": direction,
    }


LIKE_SPECIAL = re.compile(r"([\\%_])")


def _where(view, cols):
    """The WHERE clause and its parameters for the current filter.

    The text matches anywhere in a value, compared as text so a search for
    `1004` finds an integer id. With no column chosen it is every column ORed
    together, which is what people mean by a search box.
    """
    if not view["q"]:
        return "", []
    targets = [c for c in cols if not view["col"] or c["name"] == view["col"]]
    if not targets:
        return "", []
    pattern = "%" + LIKE_SPECIAL.sub(r"\\\1", view["q"]) + "%"
    parts = ["CAST(%s AS TEXT) LIKE ? ESCAPE '\\'" % quote_ident(c["name"]) for c in targets]
    return " WHERE (" + " OR ".join(parts) + ")", [pattern] * len(targets)


def count(conn, table, view, cols):
    clause, args = _where(view, cols)
    sql = "SELECT COUNT(*) FROM %s%s" % (quote_ident(table), clause)
    return conn.execute(sql, args).fetchone()[0]


def page(conn, table, view, cols, limit, offset):
    """One page of rows, filtered and sorted.

    NULLs sort last in both directions. SQLite puts them first ascending,
    which means every page of a table with a sparse column opens on a screen of
    blanks — true to the storage engine and useless to look at.
    """
    clause, args = _where(view, cols)
    order = ""
    if view["sort"]:
        ident = quote_ident(view["sort"])
        order = " ORDER BY (%s IS NULL), %s %s" % (ident, ident, "DESC" if view["dir"] == DESC else "ASC")
    sql = "SELECT * FROM %s%s%s LIMIT ? OFFSET ?" % (quote_ident(table), clause, order)
    return conn.execute(sql, args + [limit, offset]).fetchall()


def display(value):
    """One cell, as text. Bytes are described rather than printed."""
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "<%d bytes>" % len(bytes(value))
    # Everything else is shown as SQLite stores it — a REAL of 63 is "63.0",
    # not "63". A viewer that tidies values is a viewer you cannot trust to
    # tell you what is in the column.
    return str(value)
