# Orders

A read-only viewer for a SQLite database that somebody else put on an EFS
volume. It finds the volume from the platform's own declaration, opens
`orders.db` read-only, and shows the `orders` table as a list you can sort,
filter and download. It never writes — not to the database, not to the volume.

Python, standard library only: `http.server`, `sqlite3`, `csv`, `html`. Nothing
installed, nothing built, no framework.

## The example is really about the file not being there

On a volume that is the normal state for a while. The database arrives from a
nightly export, an operations job, or a person with the volume browser — and
until it does, this app has to say something better than a stack trace or an
empty table.

So the page names the volume it looked at, the exact path it expected, what
would have to happen, and offers a **Check again** button. Nothing is cached
between requests: the declaration is re-read and the volume re-stat'ed every
time, so the button is a real check and the file appearing needs no redeploy
and no restart.

The same page appears — with different words — when no storage is attached at
all, when the only mounts are object stores, when the volume is declared but
missing, and when the database is there but has no `orders` table (in which
case it lists the tables it does have). All of them are 200, because none of
them is a fault in this process.

## How the volume is found

Launchpad sets `LAUNCHPAD_STORAGE` on every isolated workload: a JSON array of
`{name, kind, path, access}`, written by the same code that built the pod's
mounts, so the description and the filesystem cannot disagree. This app reads
that and nothing else — it never scans `/mnt` and never guesses a path.

It takes the **first declared volume**, not the first one that works. If that
mount is missing, that is the fact to report; quietly reading the second volume
would show the wrong database with nothing on the page to say so.

**Object stores are never candidates.** A store is an S3 prefix through
Mountpoint, which provides no file locking — and SQLite's correctness depends
on real `flock`/`fcntl` locks. An embedded database belongs on a volume, and a
store is skipped with that reason on the page rather than tried and corrupted.

## Requirements

**Isolated mode.** Durable storage is attached below a workload's own boundary,
which a shared process does not have — Launchpad refuses a shared deploy of an
app with storage mapped.

**A storage mapping takes effect on the next deployment.** After attaching a
volume, redeploy or restart this app. Nothing arrives on a running pod. (The
*file* is a different matter: it is looked for per request, so a database
copied onto an already-mounted volume shows up immediately.)

## Configuration

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `VIEWER_TITLE` | no | `Orders` | Heading and `<title>`. |
| `ORDERS_DB` | no | `orders.db` | The database file, relative to the volume root. A path that would leave the volume is refused. |
| `ORDERS_TABLE` | no | `orders` | The table or view to show. |
| `PAGE_SIZE` | no | `100` | Rows per page, clamped to 10–1000. |

`PORT`, `HOST`, `BASE_PATH` and `LAUNCHPAD_STORAGE` come from the platform.

## Routes

| Path | What |
|---|---|
| `/` | The table: filter box, sortable columns, pager. |
| `/orders.csv` | The current filter and sort — every matching row, not just the page. |
| `/healthz` | Liveness, plus which volume and whether the database is present. |

An unknown path returns 404 **with the list of paths that do exist**.

`/healthz` is 200 with `"database": false` when the file is missing. A missing
database is a state this app is designed to show; reporting it as unhealthy
would have the platform restart a process that is working exactly as intended.

## Two things worth copying

**Read-only means read-only.** The connection is opened as
`file:/path/orders.db?mode=ro`, so a missing file is an error instead of an
empty database silently created on someone else's volume, and this viewer can
never be the process that damages the data it shows.

*One consequence to know about:* a read-only connection to a database in **WAL**
mode needs to create a `-shm` file beside it. On a read-only mount that fails,
and the page says so. Ship databases here in the rollback journal
(`PRAGMA journal_mode=DELETE`) or mount the volume read-write.

**The schema is discovered; the query string is not trusted.** Columns come
from `PRAGMA table_info`, so a column added upstream appears with no change
here. A sort column from the URL is accepted only if it is one of the names the
schema just returned — never interpolated — and the filter text is always a
bound parameter. `test_app.py` holds both to it, including a `sort=` that tries
to drop the table.

## Try it without a volume

`tools/make_orders_db.py` writes the file this app expects — 240 orders from a
fixed seed, so everyone gets the same data:

```bash
python tools/make_orders_db.py /tmp/vol/orders.db

LAUNCHPAD_STORAGE='[{"name":"orders-data","kind":"volume","path":"/tmp/vol","access":"read"}]' \
  PORT=8000 python app.py
```

Delete the file and reload to see the other half of the app.

On a real install, upload the same file to a volume with Launchpad's volume
browser, and this app picks it up on the next request.

## Tests

```bash
python -m unittest -v
```

Twenty-one, no dependencies: which mount is chosen and why, and the promise
that nothing from a query string reaches SQL as anything but a bound value.

## The house style

The look is the [Launchpad Example Kit](https://github.com/gopanair/launchpad-example-kit) —
one stylesheet, one script, no CDN — served from `/static` out of a dict this app
builds at startup. Reading a file per request would be a filesystem call on the
hot path, and building the path from the URL would be a directory traversal
waiting to happen.

`STYLE` in `app.py` is what is left of this app's own CSS after the kit arrived:
four rules for the sorted-column arrow, the null cell, the pager and the "where
it looked" block. Everything else — the table, the buttons, the empty state, the
cards — is kit vocabulary.

MIT.
