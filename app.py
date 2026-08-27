"""Orders — read a SQLite database off the first attached EFS volume.

Launchpad's storage example, from the reading side. Something else — a nightly
job, an export from a system of record, a person with the volume browser — puts
`orders.db` on an EFS volume. This app finds the volume from the platform's own
declaration, opens the database read-only, and shows the `orders` table as a
list you can sort and filter. It writes nothing, ever.

The interesting half is what happens when the file is not there, because on a
volume that is the normal state for a while: the page says which volume it
looked at, where it looked, and what would have to happen — and offers a button
that looks again. Nothing is cached between requests, so the button is a real
check rather than a reload of a decision made at boot.

This is the *generic Python* path: a `requirements.txt` naming no framework the
platform recognises, started as `python app.py`. Nothing supplies a server, so
the app supplies its own, and with that comes the whole contract — PORT, HOST,
BASE_PATH, SIGTERM. Standard library only: http.server, sqlite3, csv, html.
"""

import csv
import html
import io
import json
import os
import signal
import sqlite3
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import orders
import storage

BASE_PATH = os.getenv("BASE_PATH", "")
TITLE = os.getenv("VIEWER_TITLE", "Orders")
HERE = os.path.dirname(os.path.abspath(__file__))

# The house style, read once at startup rather than per request. Three files,
# byte-identical in every example in the Launchpad gallery.
STATIC = {}
for _name, _type in (("launchpad-kit.css", "text/css; charset=utf-8"),
                     ("launchpad-kit.js", "text/javascript; charset=utf-8"),
                     ("favicon.svg", "image/svg+xml")):
    try:
        with open(os.path.join(HERE, "static", _name), "rb") as _f:
            STATIC["/static/" + _name] = (_f.read(), _type)
    except OSError:
        pass
DB_NAME = os.getenv("ORDERS_DB", "orders.db")
TABLE = os.getenv("ORDERS_TABLE", "orders")
STARTED_AT = datetime.now(timezone.utc)


def page_size():
    try:
        size = int(os.getenv("PAGE_SIZE", "100"))
    except ValueError:
        return 100
    return max(10, min(size, 1000))


class Problem(Exception):
    """A situation the page explains and offers to re-check, not an error."""

    def __init__(self, problem, hint="", where=""):
        self.problem, self.hint, self.where = problem, hint, where
        super().__init__(problem)


def load(params):
    """Everything one request needs: the volume, the schema, and a page of rows.

    Opened and closed per request. A connection held open would keep this
    process's answer frozen at whatever was true when it started, and the whole
    point of the retry button is that it is not.
    """
    state = storage.resolve_db(DB_NAME, None)
    if not state["ready"]:
        raise Problem(state["problem"], state["hint"], _where_line(state))

    where = _where_line(state)
    try:
        conn = orders.connect(state["db_path"])
    except sqlite3.OperationalError as err:
        raise Problem(
            "%s could not be opened: %s." % (state["db_path"], err),
            "A read-only connection to a database in WAL mode needs to write a -shm file beside "
            "it; if this volume is mounted read-only, that is what failed. Mount it read-write, "
            "or checkpoint the database before copying it here.",
            where,
        )
    try:
        try:
            cols = orders.columns(conn, TABLE)
        except orders.TableMissing as err:
            raise Problem(
                "%s has no table named “%s”." % (os.path.basename(state["db_path"]), TABLE),
                ("What is in it: %s." % ", ".join(err.present)) if err.present
                else "The database is empty — it has no tables at all.",
                where,
            )
        if not cols:
            raise Problem("The table “%s” has no columns." % TABLE, "", where)

        view = orders.normalize(params, cols)
        total = orders.count(conn, TABLE, view, cols)
        size = page_size()
        try:
            offset = max(0, int(params.get("offset", "0")))
        except ValueError:
            offset = 0
        if offset >= total:
            offset = max(0, (max(total - 1, 0) // size) * size)
        rows = orders.page(conn, TABLE, view, cols, size, offset)
        return {
            "state": state, "cols": cols, "view": view, "rows": rows,
            "total": total, "offset": offset, "size": size, "where": where,
        }
    finally:
        conn.close()


def _where_line(state):
    mount = state["mount"]
    if mount is None:
        return ""
    return "%s · %s" % (mount.name, state["db_path"] or mount.path)


class Handler(BaseHTTPRequestHandler):
    server_version = "orders-viewer/1.0"

    def do_GET(self):  # noqa: N802 — http.server's spelling
        url = urlparse(self.path)
        path = url.path.rstrip("/") or "/"
        params = {k: v[0] for k, v in parse_qs(url.query).items()}
        # Static first, out of a dict built at startup: reading a file per
        # request would be a filesystem call on the hot path, and building the
        # path from the URL would be a directory traversal waiting to happen.
        asset = STATIC.get(path)
        if asset is not None:
            self.send_response(200)
            self.send_header("Content-Type", asset[1])
            self.send_header("Content-Length", str(len(asset[0])))
            self.send_header("Cache-Control", "public, max-age=300")
            self.end_headers()
            return self.wfile.write(asset[0])

        routes = {"/": self.page, "/orders.csv": self.csv, "/healthz": self.healthz}
        handler = routes.get(path)
        if handler is None:
            return self.send_json(
                404, {"error": "no such path", "paths": sorted(list(routes) + list(STATIC))})
        handler(params)

    # --- routes --------------------------------------------------------------

    def page(self, params):
        try:
            self.send_html(render_table(load(params)))
        except Problem as problem:
            self.send_html(render_problem(problem), status=200)

    def csv(self, params):
        """The current filter and sort, as a download. Whole result, not a page."""
        try:
            data = load(params)
        except Problem as problem:
            return self.send_json(409, {"error": problem.problem, "hint": problem.hint})
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        names = [c["name"] for c in data["cols"]]
        writer.writerow(names)
        conn = orders.connect(data["state"]["db_path"])
        try:
            for row in orders.page(conn, TABLE, data["view"], data["cols"], data["total"], 0):
                writer.writerow([orders.display(row[n]) for n in names])
        finally:
            conn.close()
        body = buffer.getvalue().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", 'attachment; filename="%s.csv"' % TABLE)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def healthz(self, _params):
        """Liveness is the process, not the data.

        A missing database is a state this app is designed to show, so it is
        200 with `database: false` — reporting it as unhealthy would have the
        platform restart a process that is working exactly as intended.
        """
        state = storage.resolve_db(DB_NAME, None)
        uptime = (datetime.now(timezone.utc) - STARTED_AT).total_seconds()
        self.send_json(200, {
            "status": "ok",
            "uptime_seconds": int(uptime),
            "volume": state["mount"].name if state["mount"] else None,
            "database": state["ready"],
            "path": state["db_path"],
            "problem": state["problem"] or None,
        })

    # --- plumbing ------------------------------------------------------------

    def send_json(self, status, payload):
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, markup, status=200):
        body = markup.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        sys.stdout.write("%s %s\n" % (self.address_string(), fmt % args))
        sys.stdout.flush()


# --- rendering ---------------------------------------------------------------

# The app's own layer on top of the house style. It adds nothing the kit
# already has: the table, the buttons, the pager and the empty state are all
# kit classes, and what is left is three rules for the sorted-column arrow and
# the null cell.
STYLE = """
.arrow { color: var(--lp-primary); }
th a { color: inherit; text-decoration: none; display: block; }
th a:hover { color: var(--lp-primary); }
td.null { color: var(--lp-ink-3); }
.tbl th, .tbl td { white-space: nowrap; }
.pager { display: flex; gap: .5rem; align-items: center; margin-top: .875rem;
         color: var(--lp-ink-2); font-size: .8125rem; }
.pager .spacer { flex: 1; }
.where { font-family: var(--lp-mono); font-size: .8125rem; color: var(--lp-ink-2);
         background: var(--lp-sunk); border: 1px solid var(--lp-rule);
         border-radius: var(--lp-r-sm); padding: .45rem .6rem; margin: 0 0 1rem;
         overflow-wrap: anywhere; }
"""


def link(view, path="/", **overrides):
    """A URL back to this page with some of the current state changed.

    BASE_PATH belongs here and nowhere else: the proxy strips the prefix before
    forwarding, so routes are matched at "/", but a href is resolved by the
    browser against the platform's origin and needs it back.
    """
    params = {"q": view["q"], "col": view["col"], "sort": view["sort"] or "", "dir": view["dir"]}
    params.update(overrides)
    query = urlencode({k: v for k, v in params.items() if v not in ("", None)})
    return html.escape(BASE_PATH + path + ("?" + query if query else ""))


def cap(label, on, note=""):
    return '<span class="cap {state}" title="{note}"><b>{label}</b></span>'.format(
        state="on" if on else "off", note=html.escape(note), label=html.escape(label))


def rail(state):
    """What this example demonstrates, and whether it is really here.

    Read from `LAUNCHPAD_STORAGE`, which is the platform's own declaration of
    what an administrator mounted. Nothing here guesses at a path.
    """
    mounts, declaration_error = storage.declared_mounts()
    writable = [m for m in mounts if m.access != storage.ACCESS_READ]
    return "".join([
        cap("Generic Python", True, "No framework the platform recognises; started as `python app.py`."),
        cap("App storage", bool(mounts) and not declaration_error,
            declaration_error or
            ("%d mount%s declared in LAUNCHPAD_STORAGE."
             % (len(mounts), "" if len(mounts) == 1 else "s") if mounts else
             "Nothing is mounted. An administrator attaches storage on this app's "
             "Storage tab; a grant is a person's and a mapping is an app's.")),
        cap("Read-only", True,
            "This app opens the database with mode=ro and writes nothing, ever — "
            "even where the mount would let it."),
        cap("The file", bool(state and state.get("ready")),
            (state or {}).get("db_path") or "Not there yet, which is the normal state on a fresh volume."),
        cap("Writable mount", bool(writable),
            "%s is writable — this app still does not write to it." % writable[0].name
            if writable else "Every mount is read-only, which is what this app wants."),
        cap("Launchpad workload", bool(os.getenv("LAUNCHPAD_APP_SLUG")),
            "Running as a Launchpad workload." if os.getenv("LAUNCHPAD_APP_SLUG")
            else "This is a local run."),
    ])


def shell(body, subtitle, state=None):
    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<meta name="description" content="A SQLite database on an attached volume, read.">
<link rel="icon" href="{base}/static/favicon.svg" type="image/svg+xml">
<link rel="stylesheet" href="{base}/static/launchpad-kit.css">
<style>{style}</style></head>
<body>
<a class="lp-skip" href="#main">Skip to content</a>

<header class="masthead"><div class="masthead-in">
<div>
<div class="wordmark"><span class="mark" aria-hidden="true"></span>
<span class="wordmark-text">Launchpad example</span></div>
<h1>{title}</h1>
<p class="standfirst">{subtitle}</p>
</div>
<div class="masthead-aside">
<span class="chip chip-lang">Python &middot; standard library</span>
<span class="chip">Read-only</span>
</div>
</div></header>

<div class="rail"><div class="rail-in">
<span class="rail-label">Launchpad</span>{rail}
</div></div>

<main class="shell" id="main">{body}</main>

<footer class="foot"><div class="foot-in">
<span>An example app from the <strong>Launchpad</strong> gallery.</span>
<span>Python &middot; http.server &middot; sqlite3 &middot; no dependencies</span>
</div></footer>

<script src="{base}/static/launchpad-kit.js"></script>
</body></html>""".format(
        title=html.escape(TITLE), style=STYLE, subtitle=subtitle, body=body,
        base=html.escape(BASE_PATH), rail=rail(state))


def render_problem(problem):
    """The state this app is really about: the database is not there yet.

    It is a page, not an error — 200, with where it looked and a button that
    looks again. Nothing is cached, so the button re-reads the declaration and
    re-stats the volume.
    """
    where = ('<p class="where">%s</p>' % html.escape(problem.where)) if problem.where else ""
    hint = ('<p class="muted">%s</p>' % html.escape(problem.hint)) if problem.hint else ""
    return shell(
        """<div class="card" style="max-width:46rem">
             <div class="card-hd"><h2>{problem}</h2>
               <span class="badge tone-att">not there yet</span></div>
             <div class="card-bd">
               {where}{hint}
               <div class="note">
                 <strong>This is a page, not an error.</strong> On a volume somebody else
                 fills, an absent file is the normal state for a while &mdash; so the app
                 says which volume it looked at, where it looked, and what would have to
                 happen. Nothing is cached between requests, so the button below is a real
                 check rather than a reload of a decision made at boot.
               </div>
               <div class="btns">
                 <a class="btn btn-primary" href="{retry}">Check again</a>
                 <a class="btn" href="{health}">What the health check says</a>
               </div>
             </div>
             <div class="card-ft">
               Liveness is the process, not the data: a missing database is
               <code>200</code> with <code>database: false</code>, because reporting it as
               unhealthy would have the platform restart something that is working exactly
               as intended.
             </div>
           </div>""".format(
            problem=html.escape(problem.problem),
            where=where,
            hint=hint,
            retry=html.escape(BASE_PATH + "/"),
            health=html.escape(BASE_PATH + "/healthz"),
        ),
        "Looking for <code>%s</code> on the first attached volume." % html.escape(DB_NAME),
        state=storage.resolve_db(DB_NAME, None),
    )


def render_table(data):
    cols, view, rows = data["cols"], data["view"], data["rows"]

    options = "".join(
        '<option value="%s"%s>%s</option>' % (
            html.escape(c["name"]), " selected" if view["col"] == c["name"] else "", html.escape(c["name"]))
        for c in cols
    )
    filter_form = """<div class="card" style="margin-bottom:1rem"><div class="card-bd">
        <form class="controls" method="get" action="{action}">
          <div class="field field-wide">
            <label for="q">Filter</label>
            <input class="input" type="search" id="q" name="q" value="{q}"
                   placeholder="Anything in {table}…">
          </div>
          <div class="field">
            <label for="col">Column</label>
            <select id="col" name="col"><option value="">All columns</option>{options}</select>
          </div>
          <input type="hidden" name="sort" value="{sort}"><input type="hidden" name="dir" value="{dir}">
          <div class="btns">
            <button class="btn btn-primary" type="submit">Filter</button>
            {clear}
          </div>
        </form>
      </div></div>""".format(
        action=html.escape(BASE_PATH + "/"),
        q=html.escape(view["q"]),
        table=html.escape(TABLE),
        options=options,
        sort=html.escape(view["sort"] or ""),
        dir=html.escape(view["dir"]),
        clear=('<a class="btn" href="%s">Clear</a>' % link(view, q="", col="", offset=""))
        if view["q"] else "",
    )

    headers = ""
    for c in cols:
        active = view["sort"] == c["name"]
        # Clicking the sorted column reverses it; clicking another starts it
        # ascending, and either way sorting returns to the first page — a sort
        # that kept the offset would land on row 400 of a different order.
        nxt = orders.DESC if active and view["dir"] == orders.ASC else orders.ASC
        arrow = ' <span class="arrow">%s</span>' % ("↑" if view["dir"] == orders.ASC else "↓") if active else ""
        headers += '<th class="{cls}"><a href="{href}">{name}{arrow}</a></th>'.format(
            cls="num" if c["numeric"] else "",
            href=link(view, sort=c["name"], **{"dir": nxt, "offset": ""}),
            name=html.escape(c["name"]),
            arrow=arrow,
        )

    body = ""
    for row in rows:
        cells = ""
        for c in cols:
            value = orders.display(row[c["name"]])
            if value is None:
                cells += '<td class="null">—</td>'
            else:
                cells += '<td class="%s">%s</td>' % ("num" if c["numeric"] else "", html.escape(value))
        body += "<tr>%s</tr>" % cells

    if rows:
        table = ('<div class="card"><div class="card-bd" style="padding:0">'
                 '<div class="tbl-wrap"><table class="tbl"><thead><tr>%s</tr></thead>'
                 "<tbody>%s</tbody></table></div></div></div>") % (headers, body)
    elif view["q"]:
        table = ('<div class="empty"><h3>No row matches &ldquo;%s&rdquo;</h3>'
                 "<p>Clear the filter, or try another column.</p></div>"
                 ) % html.escape(view["q"])
    else:
        table = ('<div class="empty"><h3>The table %s is empty</h3>'
                 "<p>The file is there and the table has no rows in it &mdash; which is a "
                 "different thing from the file not being there, and the app says which.</p>"
                 "</div>") % html.escape(TABLE)

    first = data["offset"] + 1 if data["total"] else 0
    last = min(data["offset"] + data["size"], data["total"])
    prev = ('<a class="btn btn-sm" href="%s">&larr; Previous</a>' % link(
        view, offset=max(0, data["offset"] - data["size"]))) if data["offset"] > 0 else ""
    nxt = ('<a class="btn btn-sm" href="%s">Next &rarr;</a>' % link(
        view, offset=data["offset"] + data["size"])) if last < data["total"] else ""
    pager = """<div class="pager">{prev}{next}<span class="spacer"></span>
        <span>Showing {first}–{last} of {total}{filtered}</span></div>""".format(
        prev=prev, next=nxt, first=first, last=last, total=data["total"],
        filtered=" matching rows" if view["q"] else " rows",
    )

    return shell(
        filter_form + table + pager + """<div class="card" style="margin-top:1.5rem">
          <div class="card-hd"><h2>Where this came from</h2></div>
          <div class="card-bd">
            <dl class="kv">
              <dt>Read from</dt><dd class="mono small">{where}</dd>
              <dt>Opened</dt><dd><code>mode=ro</code> &mdash; this app never writes, even
                where the mount would let it</dd>
              <dt>This view</dt><dd><a href="{csv}">Download as CSV</a> &mdash; the whole
                result, not the page you are looking at</dd>
              <dt>Liveness</dt><dd><a href="{base}/healthz"><code>{base}/healthz</code></a></dd>
            </dl>
            <div class="note tone-brand">
              <strong>Nothing here guesses at a path.</strong> The volume comes from
              <code>LAUNCHPAD_STORAGE</code>, which is the platform's own declaration of
              what an administrator mounted. A grant is a person's and a mapping is an
              app's, and neither implies the other &mdash; so an app that hardcoded
              <code>/mnt/data</code> would work on one install and fail on the next.
            </div>
          </div>
        </div>""".format(
            where=html.escape(data["where"]),
            csv=link(view, path="/orders.csv", offset=""),
            base=html.escape(BASE_PATH),
        ),
        "%s row%s in <code>%s</code>, read from a volume somebody else fills. "
        "This app opens the database read-only and writes nothing, ever." % (
            "{:,}".format(data["total"]), "" if data["total"] == 1 else "s", html.escape(TABLE)),
        state=data["state"],
    )


def main():
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8000"))

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True

    # PM2 stops a process with SIGINT and then SIGTERM; a pod gets SIGTERM.
    # Handling both means a deploy replaces this app cleanly instead of killing
    # it mid-response.
    #
    # shutdown() has to run on another thread. It blocks until serve_forever's
    # loop has stopped, and serve_forever is what the main thread — the one a
    # signal handler interrupts — is sitting in: calling it here waits for a
    # loop that cannot resume until the handler returns. The process then
    # ignores every stop signal and is killed at the end of the grace period,
    # which is exactly the mid-response kill the handler was added to avoid.
    def shutdown(_signum, _frame):
        print("shutting down", flush=True)
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    state = storage.resolve_db(DB_NAME, None)
    print("orders viewer listening on http://%s:%d (base path %r)" % (host, port, BASE_PATH), flush=True)
    print("database: %s" % (state["db_path"] if state["ready"] else "not present — " + state["problem"]),
          flush=True)
    server.serve_forever()
    server.server_close()


if __name__ == "__main__":
    main()
