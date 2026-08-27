/* ═══════════════════════════════════════════════════════════════════════════
   Launchpad Example Kit — the small amount of JavaScript the house style needs.
   ───────────────────────────────────────────────────────────────────────────
   No build step, no framework, no dependencies, no network. A plain script tag
   and everything below is wired by `data-` attributes, so a server-rendered
   page in Go, Python or R gets the same behaviour without shipping a bundle.

     data-tabs / data-tab / data-panel   the one navigation device
     data-sort                           a sortable table
     data-filter + data-filter-target    a search box over a table
     data-spark="1,4,2,9"                a sparkline
     data-cols="…"                       a column chart
     data-donut="0.62"                   a ring

   Everything is idempotent: call `LP.enhance(root)` again after you replace a
   fragment and only the new nodes are touched.
   ═══════════════════════════════════════════════════════════════════════════ */

(function (global) {
  "use strict";

  var LP = {};

  /* ── formatting ─────────────────────────────────────────────────────── */

  LP.num = function (n, digits) {
    if (n === null || n === undefined || isNaN(n)) return "—";
    return Number(n).toLocaleString(undefined, {
      minimumFractionDigits: digits || 0,
      maximumFractionDigits: digits === undefined ? 0 : digits,
    });
  };

  LP.bytes = function (n) {
    if (!n && n !== 0) return "—";
    var u = ["B", "KiB", "MiB", "GiB", "TiB"], i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return (i === 0 ? n : n.toFixed(1)) + " " + u[i];
  };

  LP.pct = function (n, digits) {
    if (n === null || n === undefined || isNaN(n)) return "—";
    return (n * 100).toFixed(digits === undefined ? 1 : digits) + "%";
  };

  /* Relative time, and it says "just now" rather than "0 seconds ago". */
  LP.ago = function (when) {
    var t = when instanceof Date ? when : new Date(when);
    if (isNaN(t)) return "—";
    var s = Math.round((Date.now() - t.getTime()) / 1000);
    if (s < 45) return "just now";
    var steps = [[60, "minute"], [60, "hour"], [24, "day"], [7, "week"]];
    var v = s / 60, unit = "minute";
    for (var i = 1; i < steps.length; i++) {
      if (v < steps[i][0]) break;
      v /= steps[i][0]; unit = steps[i][1];
    }
    v = Math.round(v);
    return v + " " + unit + (v === 1 ? "" : "s") + " ago";
  };

  /* ── toasts ─────────────────────────────────────────────────────────────
     Every mutation says something. A silent success is indistinguishable from
     a click that did not land. */

  LP.toast = function (text, tone, ms) {
    var host = document.querySelector(".toasts");
    if (!host) {
      host = document.createElement("div");
      host.className = "toasts";
      host.setAttribute("role", "status");
      host.setAttribute("aria-live", "polite");
      document.body.appendChild(host);
    }
    var el = document.createElement("div");
    el.className = "toast" + (tone ? " tone-" + tone : "");
    el.textContent = text;
    host.appendChild(el);
    setTimeout(function () {
      el.style.transition = "opacity .2s ease";
      el.style.opacity = "0";
      setTimeout(function () { el.remove(); }, 220);
    }, ms || 3600);
    return el;
  };

  /* ── tabs ───────────────────────────────────────────────────────────────
     In-page tabs for a single-document app. A server-rendered app that gives
     each tab its own URL uses <a class="tab"> instead and needs none of this. */

  function wireTabs(root) {
    each(root, "[data-tabs]", function (bar) {
      if (bar.__lpTabs) return;
      bar.__lpTabs = true;
      var buttons = Array.prototype.slice.call(bar.querySelectorAll("[data-tab]"));
      function show(name, push) {
        buttons.forEach(function (b) {
          var on = b.getAttribute("data-tab") === name;
          b.classList.toggle("is-on", on);
          if (on) b.setAttribute("aria-current", "page");
          else b.removeAttribute("aria-current");
        });
        document.querySelectorAll("[data-panel]").forEach(function (p) {
          p.hidden = p.getAttribute("data-panel") !== name;
        });
        if (push && global.history && history.replaceState) {
          history.replaceState(null, "", "#" + name);
        }
      }
      buttons.forEach(function (b) {
        b.addEventListener("click", function (e) {
          e.preventDefault();
          show(b.getAttribute("data-tab"), true);
        });
      });
      var initial = (location.hash || "").slice(1);
      if (!initial || !bar.querySelector('[data-tab="' + cssEscape(initial) + '"]')) {
        initial = buttons.length ? buttons[0].getAttribute("data-tab") : null;
      }
      if (initial) show(initial, false);
    });
  }

  /* ── sortable tables ────────────────────────────────────────────────────
     `data-sort="num"` on a <th> sorts by the cell's `data-v` when it has one
     and by its text otherwise, so a formatted "1,204" and a date both sort
     correctly without the page shipping a parser. */

  function wireSort(root) {
    each(root, "table[data-sortable]", function (table) {
      if (table.__lpSort) return;
      table.__lpSort = true;
      var heads = Array.prototype.slice.call(table.querySelectorAll("thead th[data-sort]"));
      heads.forEach(function (th, idx) {
        var col = Array.prototype.indexOf.call(th.parentNode.children, th);
        var btn = document.createElement("button");
        btn.className = "th-sort";
        btn.type = "button";
        btn.innerHTML = th.innerHTML;
        th.textContent = "";
        th.appendChild(btn);
        btn.addEventListener("click", function () {
          var dir = btn.getAttribute("data-dir") === "asc" ? "desc" : "asc";
          heads.forEach(function (o) {
            var b = o.querySelector(".th-sort");
            if (b && b !== btn) b.removeAttribute("data-dir");
          });
          btn.setAttribute("data-dir", dir);
          var kind = th.getAttribute("data-sort");
          var body = table.tBodies[0];
          var rows = Array.prototype.slice.call(body.rows);
          rows.sort(function (a, b2) {
            var x = cellValue(a.cells[col], kind), y = cellValue(b2.cells[col], kind);
            if (x < y) return dir === "asc" ? -1 : 1;
            if (x > y) return dir === "asc" ? 1 : -1;
            return 0;
          });
          rows.forEach(function (r) { body.appendChild(r); });
        });
      });
    });
  }

  function cellValue(cell, kind) {
    if (!cell) return kind === "num" ? -Infinity : "";
    var raw = cell.hasAttribute("data-v") ? cell.getAttribute("data-v") : cell.textContent.trim();
    if (kind === "num") {
      var n = parseFloat(String(raw).replace(/[^0-9.eE+-]/g, ""));
      return isNaN(n) ? -Infinity : n;
    }
    if (kind === "date") {
      var t = Date.parse(raw);
      return isNaN(t) ? -Infinity : t;
    }
    return String(raw).toLowerCase();
  }

  /* ── filter box ─────────────────────────────────────────────────────── */

  function wireFilter(root) {
    each(root, "[data-filter]", function (input) {
      if (input.__lpFilter) return;
      input.__lpFilter = true;
      var target = document.querySelector(input.getAttribute("data-filter"));
      if (!target) return;
      var countEl = input.getAttribute("data-filter-count")
        ? document.querySelector(input.getAttribute("data-filter-count"))
        : null;
      var apply = function () {
        var q = input.value.trim().toLowerCase();
        var shown = 0, rows = target.tBodies ? target.tBodies[0].rows : target.children;
        Array.prototype.forEach.call(rows, function (r) {
          var hit = !q || r.textContent.toLowerCase().indexOf(q) !== -1;
          r.hidden = !hit;
          if (hit) shown++;
        });
        if (countEl) countEl.textContent = shown;
        var empty = target.parentNode.querySelector("[data-filter-empty]");
        if (empty) empty.hidden = shown !== 0;
      };
      input.addEventListener("input", apply);
      apply();
    });
  }

  /* ── charts ─────────────────────────────────────────────────────────────
     Three shapes, all drawn from a `data-` attribute so a server-rendered page
     needs no inline script of its own. */

  function wireSparks(root) {
    each(root, "[data-spark]", function (el) {
      if (el.__lpDrawn) return;
      el.__lpDrawn = true;
      var vals = parseNums(el.getAttribute("data-spark"));
      if (vals.length < 2) return;
      el.innerHTML = LP.sparkSVG(vals, { fill: el.getAttribute("data-spark-fill") !== "0" });
    });
    each(root, "[data-cols]", function (el) {
      if (el.__lpDrawn) return;
      el.__lpDrawn = true;
      var vals = parseNums(el.getAttribute("data-cols"));
      var max = Math.max.apply(null, vals.concat([1]));
      var wrap = document.createElement("div");
      wrap.className = "cols";
      vals.forEach(function (v, i) {
        var d = document.createElement("div");
        d.style.height = Math.max(2, Math.round((v / max) * 100)) + "%";
        d.title = LP.num(v);
        var tones = (el.getAttribute("data-col-tones") || "").split(",");
        if (tones[i]) d.setAttribute("data-tone", tones[i].trim());
        wrap.appendChild(d);
      });
      el.appendChild(wrap);
      var labels = (el.getAttribute("data-col-labels") || "").split(",").filter(Boolean);
      if (labels.length) {
        var ax = document.createElement("div");
        ax.className = "cols-axis";
        labels.forEach(function (l) {
          var s = document.createElement("span");
          s.textContent = l.trim();
          ax.appendChild(s);
        });
        el.appendChild(ax);
      }
    });
    each(root, "[data-donut]", function (el) {
      if (el.__lpDrawn) return;
      el.__lpDrawn = true;
      var f = Math.max(0, Math.min(1, parseFloat(el.getAttribute("data-donut")) || 0));
      var colour = el.getAttribute("data-donut-colour") || "var(--lp-primary)";
      el.style.background = "conic-gradient(" + colour + " 0 " + (f * 360).toFixed(1) +
        "deg, var(--lp-sunk) " + (f * 360).toFixed(1) + "deg 360deg)";
      if (!el.querySelector("span")) {
        var s = document.createElement("span");
        s.textContent = el.getAttribute("data-donut-label") || LP.pct(f, 0);
        el.appendChild(s);
      }
    });
  }

  /* A sparkline as an SVG string: 100×32 viewBox, preserveAspectRatio none, so
     it stretches to whatever box the caller put it in. */
  LP.sparkSVG = function (vals, opts) {
    opts = opts || {};
    var w = 100, h = 32, pad = 2;
    var min = Math.min.apply(null, vals), max = Math.max.apply(null, vals);
    var span = max - min || 1;
    var pts = vals.map(function (v, i) {
      var x = pad + (i / (vals.length - 1)) * (w - pad * 2);
      var y = h - pad - ((v - min) / span) * (h - pad * 2);
      return [x, y];
    });
    var line = pts.map(function (p, i) {
      return (i ? "L" : "M") + p[0].toFixed(2) + " " + p[1].toFixed(2);
    }).join(" ");
    var area = line + " L" + pts[pts.length - 1][0].toFixed(2) + " " + h + " L" + pts[0][0].toFixed(2) + " " + h + " Z";
    var last = pts[pts.length - 1];
    return '<svg class="spark" viewBox="0 0 ' + w + " " + h + '" preserveAspectRatio="none" aria-hidden="true">' +
      (opts.fill === false ? "" : '<path class="area" d="' + area + '"/>') +
      '<path class="line" d="' + line + '" vector-effect="non-scaling-stroke"/>' +
      '<circle class="dot" cx="' + last[0].toFixed(2) + '" cy="' + last[1].toFixed(2) + '" r="1.6"/>' +
      "</svg>";
  };

  /* A multi-series line chart with a grid, as an SVG string. The examples that
     want one build it server-side or call this; either way no library. */
  LP.lineChartSVG = function (series, opts) {
    opts = opts || {};
    var w = opts.width || 720, h = opts.height || 240;
    var m = { t: 12, r: 12, b: 26, l: 44 };
    var iw = w - m.l - m.r, ih = h - m.t - m.b;
    var all = series.reduce(function (a, s) { return a.concat(s.values); }, []);
    var max = opts.max !== undefined ? opts.max : Math.max.apply(null, all.concat([1]));
    var min = opts.min !== undefined ? opts.min : Math.min(0, Math.min.apply(null, all));
    var span = max - min || 1;
    var n = series.length ? series[0].values.length : 0;
    var x = function (i) { return m.l + (n < 2 ? iw / 2 : (i / (n - 1)) * iw); };
    var y = function (v) { return m.t + ih - ((v - min) / span) * ih; };
    var out = ['<svg class="chart" viewBox="0 0 ' + w + " " + h + '" role="img">'];
    for (var g = 0; g <= 4; g++) {
      var gy = m.t + (g / 4) * ih;
      out.push('<line class="grid" x1="' + m.l + '" y1="' + gy.toFixed(1) + '" x2="' + (w - m.r) + '" y2="' + gy.toFixed(1) + '"/>');
      out.push('<text class="lbl" x="' + (m.l - 8) + '" y="' + (gy + 3.5).toFixed(1) + '" text-anchor="end">' +
        LP.num(max - (g / 4) * span, opts.digits || 0) + "</text>");
    }
    series.forEach(function (s, si) {
      var d = s.values.map(function (v, i) { return (i ? "L" : "M") + x(i).toFixed(1) + " " + y(v).toFixed(1); }).join(" ");
      out.push('<path class="s' + ((si % 3) + 1) + '" d="' + d + '"/>');
    });
    (opts.labels || []).forEach(function (l, i) {
      if (!l) return;
      out.push('<text class="lbl" x="' + x(i).toFixed(1) + '" y="' + (h - 8) + '" text-anchor="middle">' + esc(l) + "</text>");
    });
    out.push("</svg>");
    return out.join("");
  };

  /* ── the JSON helper every example's fetch goes through ─────────────────
     One place that turns a non-2xx into an Error carrying the platform's own
     message, because "an error occurred" is the least useful sentence in
     software and Launchpad's refusals always name the remedy. */

  LP.json = function (url, opts) {
    opts = opts || {};
    var init = {
      method: opts.method || "GET",
      headers: Object.assign({ Accept: "application/json" }, opts.headers || {}),
      credentials: "same-origin",
    };
    if (opts.body !== undefined) {
      if (opts.body instanceof FormData) {
        init.body = opts.body;
      } else {
        init.headers["Content-Type"] = "application/json";
        init.body = JSON.stringify(opts.body);
      }
    }
    return fetch(url, init).then(function (res) {
      var ct = res.headers.get("content-type") || "";
      var body = ct.indexOf("json") !== -1 ? res.json() : res.text();
      return body.then(function (b) {
        if (res.ok) return b;
        var msg = (b && (b.message || b.error)) || res.status + " " + res.statusText;
        var err = new Error(msg);
        err.status = res.status;
        err.code = b && b.code;
        err.body = b;
        throw err;
      });
    });
  };

  /* A form that posts JSON and reports the outcome, which is the shape most of
     these examples need and none of them should write twice. */
  LP.submit = function (form, opts) {
    opts = opts || {};
    var data = {};
    new FormData(form).forEach(function (v, k) { data[k] = v; });
    var btn = form.querySelector('[type="submit"]');
    if (btn) btn.disabled = true;
    return LP.json(opts.url || form.getAttribute("action"), {
      method: opts.method || form.getAttribute("method") || "POST",
      body: opts.body || data,
    }).then(function (b) {
      LP.toast(opts.ok || "Saved", "ok");
      return b;
    }).catch(function (e) {
      LP.toast(e.message, "dan", 6000);
      throw e;
    }).finally(function () {
      if (btn) btn.disabled = false;
    });
  };

  /* ── plumbing ───────────────────────────────────────────────────────── */

  function each(root, sel, fn) {
    (root || document).querySelectorAll(sel).forEach(fn);
  }
  function parseNums(s) {
    return String(s || "").split(",").map(function (v) { return parseFloat(v); })
      .filter(function (v) { return !isNaN(v); });
  }
  function esc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  LP.esc = esc;
  function cssEscape(s) { return String(s).replace(/["\\]/g, "\\$&"); }

  LP.enhance = function (root) {
    wireTabs(root);
    wireSort(root);
    wireFilter(root);
    wireSparks(root);
    each(root, "[data-ago]", function (el) {
      el.textContent = LP.ago(el.getAttribute("data-ago"));
      if (!el.title) el.title = new Date(el.getAttribute("data-ago")).toLocaleString();
    });
    return root;
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () { LP.enhance(document); });
  } else {
    LP.enhance(document);
  }

  global.LP = LP;
})(typeof window !== "undefined" ? window : this);
