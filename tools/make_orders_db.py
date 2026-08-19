#!/usr/bin/env python3
"""Write a sample orders.db — the file this app expects to find on a volume.

    python tools/make_orders_db.py /tmp/orders.db

The viewer never creates the database; something else does, which is the point
of the example. This is that something else, small enough to read: 240 orders
over the last twelve weeks from a fixed seed, so two people running it get the
same data.

Upload the result to a volume with Launchpad's volume browser (or copy it into
the mount on the box), and the viewer picks it up on the next request — no
redeploy, because the file is looked for per request rather than at boot.
"""

import os
import random
import sqlite3
import sys
from datetime import date, timedelta

CUSTOMERS = [
    "Northwind Tools", "Acme Freight", "Bluebird Labs", "Corvid Analytics",
    "Delta Print", "Elmwood Health", "Fenwick Rail", "Granite Supply",
    "Harbour Foods", "Ivy Systems", "Juniper Media", "Kestrel Energy",
]
PRODUCTS = [
    ("Anchor bolt M12", 3.40), ("Bearing 6204", 8.15), ("Cable tray 2m", 22.00),
    ("Drive belt A48", 11.75), ("Enclosure IP66", 64.50), ("Filter cartridge", 17.90),
    ("Gasket set", 5.25), ("Hydraulic hose", 41.00), ("Inverter 3kW", 289.00),
    ("Junction box", 9.60),
]
REGIONS = ["North", "South", "East", "West"]
STATUSES = ["pending", "paid", "shipped", "delivered", "cancelled", "refunded"]
WEIGHTS = [12, 22, 20, 38, 5, 3]


def build(path, count=240, seed=20260819):
    rng = random.Random(seed)
    if os.path.exists(path):
        os.remove(path)
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE orders (
            id           INTEGER PRIMARY KEY,
            reference    TEXT    NOT NULL UNIQUE,
            ordered_on   TEXT    NOT NULL,
            customer     TEXT    NOT NULL,
            region       TEXT    NOT NULL,
            product      TEXT    NOT NULL,
            quantity     INTEGER NOT NULL,
            unit_price   REAL    NOT NULL,
            total        REAL    NOT NULL,
            status       TEXT    NOT NULL,
            shipped_on   TEXT,
            notes        TEXT
        )""")
    today = date.today()
    rows = []
    for n in range(1, count + 1):
        ordered = today - timedelta(days=rng.randint(0, 84))
        product, price = rng.choice(PRODUCTS)
        quantity = rng.choice([1, 2, 3, 4, 5, 6, 8, 10, 12, 24, 50])
        status = rng.choices(STATUSES, weights=WEIGHTS)[0]
        shipped = None
        if status in ("shipped", "delivered"):
            shipped = (ordered + timedelta(days=rng.randint(1, 6))).isoformat()
        rows.append((
            n,
            "SO-%05d" % (10000 + n),
            ordered.isoformat(),
            rng.choice(CUSTOMERS),
            rng.choice(REGIONS),
            product,
            quantity,
            price,
            round(quantity * price, 2),
            status,
            shipped,
            # Most rows have no note; a sparse column is what makes NULL
            # handling on the page worth looking at.
            rng.choice([None, None, None, None, "back-ordered", "customer collect",
                        "partial shipment", "priority"]),
        ))
    conn.executemany("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    # WAL is the default for an app writing continuously; a file that is going
    # to be copied to a volume and read by someone else is simplest in the
    # rollback journal, with nothing beside it to copy.
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.close()
    return len(rows)


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "orders.db"
    print("wrote %d orders to %s" % (build(target), target))
