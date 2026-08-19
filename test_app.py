"""What has to keep working. `python -m unittest -v`.

Two things are worth a test here and the rest is rendering: the rule that picks
the volume, and the promise that a query string cannot reach SQL as anything
but a bound value.
"""

import json
import os
import sqlite3
import tempfile
import unittest

import orders
import storage


def sample(path, rows=None):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, customer TEXT, total REAL, notes TEXT)")
    conn.executemany("INSERT INTO orders VALUES (?,?,?,?)", rows or [
        (1, "Acme Freight", 10.5, None),
        (2, "Bluebird Labs", 200.0, "priority"),
        (3, "acme freight", 3.25, "back-ordered"),
    ])
    conn.commit()
    conn.close()
    return path


class Discovery(unittest.TestCase):
    def declare(self, *entries):
        return json.dumps(list(entries))

    def test_no_storage_is_a_sentence_not_an_empty_page(self):
        state = storage.resolve_db("orders.db", "")
        self.assertFalse(state["ready"])
        self.assertIn("No storage is attached", state["problem"])

    def test_unparseable_declaration_is_an_error(self):
        _, err = storage.declared_mounts("{not json")
        self.assertIn("LAUNCHPAD_STORAGE", err)

    def test_stores_are_never_candidates(self):
        raw = self.declare({"name": "exports", "kind": "store", "path": "/mnt/exports", "access": "write"})
        state = storage.resolve_db("orders.db", raw)
        self.assertFalse(state["ready"])
        self.assertIn("none of it is an EFS volume", state["problem"])

    def test_first_declared_volume_wins_even_when_it_is_the_broken_one(self):
        # The rule is "the first volume", not "the first one that works". A
        # missing mount is the fact to report; silently reading the second
        # volume would show the wrong database with no sign of it.
        raw = self.declare(
            {"name": "primary", "kind": "volume", "path": "/nonexistent/primary", "access": "write"},
            {"name": "secondary", "kind": "volume", "path": "/tmp", "access": "write"},
        )
        state = storage.resolve_db("orders.db", raw)
        self.assertFalse(state["ready"])
        self.assertEqual("primary", state["mount"].name)
        self.assertIn("nothing is there", state["problem"])

    def test_a_store_before_a_volume_does_not_win(self):
        with tempfile.TemporaryDirectory() as tmp:
            sample(os.path.join(tmp, "orders.db"))
            raw = self.declare(
                {"name": "exports", "kind": "store", "path": "/mnt/exports", "access": "read"},
                {"name": "data", "kind": "volume", "path": tmp, "access": "read"},
            )
            state = storage.resolve_db("orders.db", raw)
            self.assertTrue(state["ready"], state["problem"])
            self.assertEqual("data", state["mount"].name)

    def test_missing_file_names_where_it_looked(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = self.declare({"name": "data", "kind": "volume", "path": tmp, "access": "write"})
            state = storage.resolve_db("orders.db", raw)
            self.assertFalse(state["ready"])
            self.assertEqual(os.path.join(tmp, "orders.db"), state["db_path"])

    def test_configured_name_cannot_leave_the_volume(self):
        path, why = storage.contained_path("/mnt/data", "../../etc/passwd")
        self.assertIsNone(path)
        self.assertIn("outside the volume", why)
        path, _ = storage.contained_path("/mnt/data", "archive/orders.db")
        self.assertEqual("/mnt/data/archive/orders.db", path)


class Reading(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = sample(os.path.join(self.dir.name, "orders.db"))
        self.conn = orders.connect(self.path)
        self.addCleanup(self.conn.close)
        self.cols = orders.columns(self.conn, "orders")

    def view(self, **kw):
        return orders.normalize(kw, self.cols)

    def test_connection_is_read_only(self):
        with self.assertRaises(sqlite3.OperationalError):
            self.conn.execute("DELETE FROM orders")

    def test_opening_a_missing_file_does_not_create_one(self):
        missing = os.path.join(self.dir.name, "absent.db")
        with self.assertRaises(sqlite3.OperationalError):
            orders.connect(missing)
        self.assertFalse(os.path.exists(missing))

    def test_missing_table_reports_what_is_there(self):
        with self.assertRaises(orders.TableMissing) as caught:
            orders.columns(self.conn, "invoices")
        self.assertEqual(["orders"], caught.exception.present)

    def test_numeric_columns_are_detected_from_the_schema(self):
        numeric = {c["name"] for c in self.cols if c["numeric"]}
        self.assertEqual({"id", "total"}, numeric)

    def test_sort_column_must_be_one_the_schema_named(self):
        self.assertIsNone(self.view(sort='total"; DROP TABLE orders; --')["sort"])
        self.assertEqual("total", self.view(sort="total")["sort"])

    def test_a_hostile_sort_leaves_the_table_alone(self):
        view = self.view(sort='total"; DROP TABLE orders; --', dir="desc")
        rows = orders.page(self.conn, "orders", view, self.cols, 10, 0)
        self.assertEqual(3, len(rows))
        self.assertEqual(["orders"], orders.table_names(self.conn))

    def test_filter_text_is_bound_not_interpolated(self):
        view = self.view(q="' OR 1=1 --")
        self.assertEqual(0, orders.count(self.conn, "orders", view, self.cols))

    def test_filter_matches_any_column_and_ignores_case(self):
        view = self.view(q="acme")
        self.assertEqual(2, orders.count(self.conn, "orders", view, self.cols))

    def test_filter_can_be_narrowed_to_one_column(self):
        self.assertEqual(1, orders.count(self.conn, "orders", self.view(q="priority"), self.cols))
        self.assertEqual(0, orders.count(self.conn, "orders",
                                         self.view(q="priority", col="customer"), self.cols))

    def test_filter_compares_numbers_as_text(self):
        # A search box that cannot find an order by its id is a search box
        # nobody uses twice.
        self.assertEqual(1, orders.count(self.conn, "orders", self.view(q="200"), self.cols))

    def test_like_wildcards_in_the_filter_are_literal(self):
        self.assertEqual(0, orders.count(self.conn, "orders", self.view(q="%"), self.cols))

    def test_nulls_sort_last_in_both_directions(self):
        for direction in ("asc", "desc"):
            rows = orders.page(self.conn, "orders", self.view(sort="notes", dir=direction),
                               self.cols, 10, 0)
            self.assertIsNone(rows[-1]["notes"], direction)

    def test_paging_walks_the_whole_table_once(self):
        seen = []
        for offset in (0, 2):
            seen += [r["id"] for r in orders.page(self.conn, "orders", self.view(sort="id"),
                                                  self.cols, 2, offset)]
        self.assertEqual([1, 2, 3], seen)

    def test_values_are_shown_as_stored(self):
        self.assertEqual("10.5", orders.display(10.5))
        self.assertEqual("200.0", orders.display(200.0))
        self.assertIsNone(orders.display(None))
        self.assertEqual("<3 bytes>", orders.display(b"abc"))


if __name__ == "__main__":
    unittest.main()
