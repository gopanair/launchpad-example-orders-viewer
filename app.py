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
        routes = {"/": self.page, "/orders.csv": self.csv, "/healthz": self.healthz}
        handler = routes.get(path)
        if handler is None:
            return self.send_json(404, {"error": "no such path", "paths": sorted(routes)})
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

STYLE = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
body { margin:0; padding:2.5rem 1.5rem; background:#f7f8fa; color:#1c2024;
       font:15px/1.5 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width:72rem; margin:0 auto; }
h1 { font-size:1.35rem; letter-spacing:-0.015em; margin:0 0 .25rem; }
p.sub { color:#6b7280; font-size:.82rem; margin:0 0 1.5rem; }
p.sub code { font-family:ui-monospace, Menlo, monospace; font-size:.78rem; }
form.filter { display:flex; gap:.5rem; flex-wrap:wrap; align-items:center; margin:0 0 .9rem; }
input[type=search], select { font:inherit; font-size:.85rem; padding:.4rem .55rem; background:#fff;
       border:1px solid #d5d7db; border-radius:7px; color:inherit; }
input[type=search] { min-width:16rem; }
button, a.btn { font:inherit; font-size:.85rem; padding:.4rem .8rem; border-radius:7px;
       border:1px solid #d5d7db; background:#fff; color:#1c2024; cursor:pointer;
       text-decoration:none; display:inline-block; }
button.primary, a.btn.primary { background:#3538cd; border-color:#3538cd; color:#fff; }
table { width:100%; border-collapse:collapse; background:#fff; border:1px solid #e5e7eb;
        border-radius:10px; overflow:hidden; }
th, td { text-align:left; padding:.5rem .8rem; border-bottom:1px solid #f0f1f3; font-size:.85rem;
        white-space:nowrap; }
th { font-size:.7rem; text-transform:uppercase; letter-spacing:.06em; color:#6b7280; background:#fbfbfc; }
th a { color:inherit; text-decoration:none; display:block; }
th a:hover { color:#3538cd; }
th .arrow { color:#3538cd; }
tbody tr:last-child td { border-bottom:none; }
tbody tr:hover td { background:#fbfbfd; }
td.num, th.num { text-align:right; font-variant-numeric:tabular-nums; }
td.null { color:#c0c4cc; }
.scroll { overflow-x:auto; }
.pager { display:flex; gap:.5rem; align-items:center; margin:.9rem 0 0; color:#6b7280; font-size:.8rem; }
.pager .spacer { flex:1; }
.empty { background:#fff; border:1px solid #e5e7eb; border-radius:10px; padding:2rem 1.5rem;
         text-align:center; color:#6b7280; font-size:.9rem; }
.notice { background:#fff; border:1px solid #e5e7eb; border-radius:10px; padding:1.75rem; max-width:44rem; }
.notice h2 { font-size:1rem; margin:0 0 .5rem; color:#1c2024; }
.notice p { margin:0 0 .75rem; color:#4b5563; font-size:.88rem; }
.notice .where { font-family:ui-monospace, Menlo, monospace; font-size:.78rem; color:#6b7280;
         background:#f7f8fa; border:1px solid #eceef1; border-radius:6px; padding:.45rem .6rem;
         margin:0 0 1rem; overflow-wrap:anywhere; }
footer { margin-top:1.25rem; color:#98a2b3; font-size:.78rem; }
footer a, a { color:#3538cd; }
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


def shell(body, subtitle):
    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title><style>{style}</style></head>
<body><main>
  <h1>{title}</h1>
  <p class="sub">{subtitle}</p>
  {body}
</main></body></html>""".format(title=html.escape(TITLE), style=STYLE, subtitle=subtitle, body=body)


def render_problem(problem):
    """The state this app is really about: the database is not there yet.

    It is a page, not an error — 200, with where it looked and a button that
    looks again. Nothing is cached, so the button re-reads the declaration and
    re-stats the volume.
    """
    where = ('<p class="where">%s</p>' % html.escape(problem.where)) if problem.where else ""
    hint = ("<p>%s</p>" % html.escape(problem.hint)) if problem.hint else ""
    return shell(
        """<div class="notice">
             <h2>{problem}</h2>
             {where}{hint}
             <p><a class="btn primary" href="{retry}">Check again</a></p>
           </div>""".format(
            problem=html.escape(problem.problem),
            where=where,
            hint=hint,
            retry=html.escape(BASE_PATH + "/"),
        ),
        "Looking for <code>%s</code> on the first attached volume." % html.escape(DB_NAME),
    )


def render_table(data):
    cols, view, rows = data["cols"], data["view"], data["rows"]

    options = "".join(
        '<option value="%s"%s>%s</option>' % (
            html.escape(c["name"]), " selected" if view["col"] == c["name"] else "", html.escape(c["name"]))
        for c in cols
    )
    filter_form = """<form class="filter" method="get" action="{action}">
        <input type="search" name="q" value="{q}" placeholder="Filter {table}…" aria-label="Filter">
        <select name="col" aria-label="Column"><option value="">All columns</option>{options}</select>
        <input type="hidden" name="sort" value="{sort}"><input type="hidden" name="dir" value="{dir}">
        <button class="primary" type="submit">Filter</button>
        {clear}
      </form>""".format(
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
        table = '<div class="scroll"><table><thead><tr>%s</tr></thead><tbody>%s</tbody></table></div>' % (
            headers, body)
    elif view["q"]:
        table = '<div class="empty">No row matches “%s”.</div>' % html.escape(view["q"])
    else:
        table = '<div class="empty">The table %s is empty.</div>' % html.escape(TABLE)

    first = data["offset"] + 1 if data["total"] else 0
    last = min(data["offset"] + data["size"], data["total"])
    prev = ('<a class="btn" href="%s">Previous</a>' % link(view, offset=max(0, data["offset"] - data["size"]))
            ) if data["offset"] > 0 else ""
    nxt = ('<a class="btn" href="%s">Next</a>' % link(view, offset=data["offset"] + data["size"])
           ) if last < data["total"] else ""
    pager = """<div class="pager">{prev}{next}<span class="spacer"></span>
        <span>Showing {first}–{last} of {total}{filtered}</span></div>""".format(
        prev=prev, next=nxt, first=first, last=last, total=data["total"],
        filtered=" matching rows" if view["q"] else " rows",
    )

    return shell(
        filter_form + table + pager + """<footer>Read-only from <code>{where}</code> ·
          <a href="{csv}">Download this view as CSV</a> · liveness at <code>{base}/healthz</code></footer>""".format(
            where=html.escape(data["where"]),
            csv=link(view, path="/orders.csv", offset=""),
            base=html.escape(BASE_PATH),
        ),
        "%s row%s in <code>%s</code>, read from the volume — this app never writes." % (
            data["total"], "" if data["total"] == 1 else "s", html.escape(TABLE)),
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
