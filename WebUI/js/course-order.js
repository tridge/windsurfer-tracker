// Course mark rounding order (course.order, e.g. "S12313F") and per-mark
// port/starboard rounding (mark.rounding), shared by the course pages.
// routeLayer() needs Leaflet; uses wmmDeclination() from wmm.js for labels if loaded.
const CourseOrder = (() => {
    const EARTH_R = 6371000;
    const rad = Math.PI / 180;
    const OFFSET_PX = 18;   // route offset from a mark
    const GAP_PX = 14;      // extra offset each time a pair of points is reused

    const unwrapLon = (lon, ref) => lon + 360 * Math.round((ref - lon) / 360);

    const escHtml = s => String(s).replace(/[&<>"']/g, c =>
        ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

    // Mark comment as HTML (escaped, newlines as <br>, optionally non-breaking spaces); null if none
    function commentHtml(text, nbsp) {
        if (text === undefined || text === null || text === '') return null;
        let h = escHtml(String(text).replace(/\n+$/, ''));
        if (nbsp) h = h.replace(/ /g, '&nbsp;');
        return h.replace(/\n/g, '<br>');
    }

    const safeColor = (c, dflt = '#f97316') => typeof c === 'string' && /^#[0-9a-f]{6}$/i.test(c) ? c : dflt;

    // --- multiple courses (A, B, C...) in one course file ---
    // Course A is the file itself; B, C... are course.extraCourses[0], [1]...
    // each with its own start/marks/finish/order.
    const MAX_COURSES = 6;
    const COURSE_COLORS = ['#ff6600', '#2563eb', '#9333ea', '#059669', '#db2777', '#ca8a04'];
    const letterOf = i => String.fromCharCode(65 + i);

    // [{letter, index, course, color}] for every course in the file
    function subCourses(course) {
        if (!course) return [];
        const extra = Array.isArray(course.extraCourses) ? course.extraCourses : [];
        return [course, ...extra].map((c, i) => ({ letter: letterOf(i), index: i, course: c, color: COURSE_COLORS[i % COURSE_COLORS.length] }));
    }

    function subCourse(course, letter) {
        const e = subCourses(course).find(x => x.letter === letter);
        return e ? e.course : null;
    }

    const courseCount = course => subCourses(course).length;

    // Copy of one course without the file-level fields (other courses, server timestamps)
    function plainCourse(c) {
        const out = JSON.parse(JSON.stringify(c || {}));
        ['extraCourses', 'saved', 'updated', 'updated_iso'].forEach(k => delete out[k]);
        return out;
    }

    // File with course `letter` removed; later courses move up a letter
    function removeCourse(course, letter) {
        const subs = subCourses(course).filter(x => x.letter !== letter).map(x => plainCourse(x.course));
        const out = subs[0] || {};
        if (subs.length > 1) out.extraCourses = subs.slice(1);
        return out;
    }

    // Mark name: "3", or "B3" when the file has more than one course
    const markName = (letter, num, multi) => multi ? `${letter}${num}` : String(num);

    function metresNE(a, b) {
        return {
            north: (b.lat - a.lat) * rad * EARTH_R,
            east: (unwrapLon(b.lon, a.lon) - a.lon) * rad * EARTH_R * Math.cos(a.lat * rad)
        };
    }

    function distanceKm(a, b) {
        const dLat = (b.lat - a.lat) * rad, dLon = (b.lon - a.lon) * rad;
        const h = Math.sin(dLat / 2) ** 2 + Math.cos(a.lat * rad) * Math.cos(b.lat * rad) * Math.sin(dLon / 2) ** 2;
        return 6371 * 2 * Math.atan2(Math.sqrt(h), Math.sqrt(1 - h));
    }

    function bearingDeg(a, b) {
        const y = Math.sin((b.lon - a.lon) * rad) * Math.cos(b.lat * rad);
        const x = Math.cos(a.lat * rad) * Math.sin(b.lat * rad) -
                  Math.sin(a.lat * rad) * Math.cos(b.lat * rad) * Math.cos((b.lon - a.lon) * rad);
        return (Math.atan2(y, x) / rad + 360) % 360;
    }

    // Parse "S12313F" (or "S 1 2 10 F") into ['S', 1, 2, ..., 'F'].
    // S/F are added if missing. Returns {seq} (null for blank) or {error}.
    function parse(str, numMarks) {
        const s = (str || '').toUpperCase().trim();
        if (!s) return { seq: null };
        const tokens = /[\s,\-]/.test(s) ? s.split(/[\s,\-]+/).filter(t => t) : s.split('');
        const seq = [];
        for (const t of tokens) {
            if (t === 'S' || t === 'F') { seq.push(t); continue; }
            if (!/^\d+$/.test(t)) return { error: `"${t}" is not S, F or a mark number` };
            const n = parseInt(t, 10);
            if (n < 1 || n > numMarks) return { error: `Mark ${n} doesn't exist (course has ${numMarks})` };
            seq.push(n);
        }
        if (seq[0] !== 'S') seq.unshift('S');
        if (seq[seq.length - 1] !== 'F') seq.push('F');
        for (let i = 0; i < seq.length; i++) {
            if (seq[i] === 'S' && i !== 0) return { error: 'S must come first' };
            if (seq[i] === 'F' && i !== seq.length - 1) return { error: 'F must come last' };
            if (i > 0 && seq[i] === seq[i - 1]) return { error: `Mark ${seq[i]} repeated back to back` };
        }
        return { seq };
    }

    function format(seq) {
        return seq.some(k => typeof k === 'number' && k > 9) ? seq.join(' ') : seq.join('');
    }

    // Custom order of a course as keys, or null if none/invalid
    function keys(course) {
        if (!course || !course.order) return null;
        return parse(course.order, course.marks ? course.marks.length : 0).seq || null;
    }

    // Visiting sequence on the raw mark positions (no pin lines applied)
    function rawSequence(course) {
        const nm = course.marks ? course.marks.length : 0;
        let ks = keys(course);
        if (!ks) {
            ks = [];
            if (course.start) ks.push('S');
            for (let i = 1; i <= nm; i++) ks.push(i);
            if (course.finish) ks.push('F');
        }
        return ks.map(k => k === 'S' ? { key: k, pt: course.start, mark: null }
                         : k === 'F' ? { key: k, pt: course.finish, mark: null }
                         : { key: k, pt: course.marks[k - 1], mark: k - 1 })
                 .filter(e => e.pt);
    }

    // Start/finish line with a pin: course.start.pin / course.finish.pin = {dist (m), side}.
    // The pin lies square to the first leg (start) or last leg (finish); left/right is
    // looking from the start towards the next mark, or from the finish towards the
    // previous mark. Returns {mark, pin, mid} or null.
    function pinLine(course, which) {
        const pt = which === 'S' ? course.start : course.finish;
        if (!pt || !pt.pin || !(pt.pin.dist > 0)) return null;
        const seq = rawSequence(course);
        const i = seq.findIndex(e => e.key === which);
        if (i < 0) return null;
        // nearest point along the course that isn't on top of this one
        let nb = null;
        for (let j = which === 'S' ? i + 1 : i - 1; j >= 0 && j < seq.length; j += which === 'S' ? 1 : -1) {
            const d = metresNE(pt, seq[j].pt);
            if (Math.hypot(d.north, d.east) > 0.5) { nb = d; break; }
        }
        if (!nb) return null;
        const len = Math.hypot(nb.north, nb.east);
        const fn = nb.north / len, fe = nb.east / len;           // facing the neighbour
        const sgn = pt.pin.side === 'right' ? 1 : -1;            // right of facing (n,e) is (-e,n)
        const dn = sgn * -fe * pt.pin.dist, de = sgn * fn * pt.pin.dist;
        const pin = {
            lat: pt.lat + (dn / EARTH_R) / rad,
            lon: pt.lon + (de / (EARTH_R * Math.cos(pt.lat * rad))) / rad
        };
        return { mark: pt, pin, mid: { lat: (pt.lat + pin.lat) / 2, lon: (pt.lon + unwrapLon(pin.lon, pt.lon)) / 2 } };
    }

    // Visiting sequence: [{key, pt, mark}] where mark is the 0-based mark index or null.
    // A start/finish with a pin is represented by the middle of its line.
    function sequence(course) {
        const seq = rawSequence(course);
        for (const which of ['S', 'F']) {
            const pl = pinLine(course, which);
            if (!pl) continue;
            seq.forEach(e => { if (e.key === which) e.pt = pl.mid; });
        }
        return seq;
    }

    // Dotted start/finish lines and pin markers for a course (not yet added to a map).
    // opts.ref: longitude the rest of the course is drawn around (default each line's own mark)
    function pinLayers(course, opts = {}) {
        const layers = [];
        for (const which of ['S', 'F']) {
            const pl = pinLine(course, which);
            if (!pl) continue;
            const ref = opts.ref !== undefined ? opts.ref : pl.mark.lon;
            layers.push(L.polyline([[pl.mark.lat, unwrapLon(pl.mark.lon, ref)], [pl.pin.lat, unwrapLon(pl.pin.lon, ref)]], {
                color: which === 'S' ? '#16a34a' : '#111827', weight: 3, opacity: 0.9,
                dashArray: '2, 7', lineCap: 'round', interactive: false
            }));
            layers.push(L.circleMarker([pl.pin.lat, unwrapLon(pl.pin.lon, ref)], {
                radius: 6, color: '#7c2d12', weight: 2, fillColor: '#f97316', fillOpacity: 1,
                interactive: opts.interactive !== false
            }).bindPopup(`<strong>${which === 'S' ? 'Start' : 'Finish'} pin</strong><br>` +
                         `<small>${Math.round(pl.mark.pin.dist)} m ${pl.mark.pin.side === 'right' ? 'right' : 'left'}</small>`));
        }
        return layers;
    }

    // Course length in km following the order
    function lengthKm(course) {
        if (!course || !course.start || !course.finish) return 0;
        const pts = sequence(course).map(e => e.pt);
        let km = 0;
        for (let i = 0; i + 1 < pts.length; i++) km += distanceKm(pts[i], pts[i + 1]);
        return km;
    }

    // Auto rounding side from the net turn at the marks: left = port
    function autoSide(seq) {
        let turn = 0;
        for (let i = 1; i + 1 < seq.length; i++) {
            if (seq[i].mark === null) continue;
            const a = metresNE(seq[i - 1].pt, seq[i].pt), b = metresNE(seq[i].pt, seq[i + 1].pt);
            turn += Math.atan2(a.east * b.north - a.north * b.east, a.east * b.east + a.north * b.north);
        }
        return turn >= 0 ? 'port' : 'starboard';
    }

    function markSide(course, markIdx, auto) {
        const r = course.marks && course.marks[markIdx] && course.marks[markIdx].rounding;
        return r === 'port' || r === 'starboard' ? r : auto;
    }

    // Keep a custom order consistent after inserting a mark at 0-based index
    // insertIndex between prevKey and nextKey (old numbering)
    function insertMark(course, insertIndex, prevKey, nextKey) {
        const ks = keys(course);
        if (!ks) return;
        const num = insertIndex + 1;
        const shift = k => typeof k === 'number' && k >= num ? k + 1 : k;
        const shifted = ks.map(shift);
        const pk = shift(prevKey), nk = shift(nextKey);
        const out = [];
        shifted.forEach((k, i) => {
            out.push(k);
            if (k === pk && shifted[i + 1] === nk) out.push(num);
        });
        course.order = format(out);
    }

    // Split the leg sailed into a mark (0-based index, or 'finish') with a new mark at
    // its middle. With a custom order this is the leg into the mark's first visit; the
    // new mark goes into every occurrence of that leg. Returns false if nothing to split.
    function splitLeg(course, target) {
        const key = target === 'finish' ? 'F' : target + 1;
        if (key !== 'F' && !(course.marks && course.marks[target])) return false;
        const seq = sequence(course);
        const i = seq.findIndex(e => e.key === key);
        if (i < 1) return false;
        const a = seq[i - 1].pt, b = seq[i].pt;
        let lon = (a.lon + unwrapLon(b.lon, a.lon)) / 2;
        lon = ((lon + 540) % 360) - 180;
        const insertIndex = key === 'F' ? (course.marks ? course.marks.length : 0) : target;
        if (!course.marks) course.marks = [];
        insertMark(course, insertIndex, seq[i - 1].key, key);
        course.marks.splice(insertIndex, 0, { lat: (a.lat + b.lat) / 2, lon });
        return true;
    }

    // Keep a custom order consistent when deleting the 1-based mark num
    // (call before removing the mark)
    function deleteMark(course, num) {
        const ks = keys(course);
        if (!ks) { delete course.order; return; }
        const out = [];
        for (const k of ks) {
            if (k === num) continue;
            const k2 = typeof k === 'number' && k > num ? k - 1 : k;
            if (out.length && out[out.length - 1] === k2) continue;
            out.push(k2);
        }
        course.order = format(out);
    }

    function legLengthText(a, b) {
        const km = distanceKm(a, b);
        return `${km < 1 ? Math.round(km * 1000) + ' m' : km.toFixed(2) + ' km'} / ${(km / 1.852).toFixed(2)} nm`;
    }

    // Draw the ordered route: legs offset to the rounding side, arcs around each mark
    // in the rounding direction, direction arrows, and optional leg labels
    function draw(map, course, opts, add) {
        const seq = sequence(course);
        if (seq.length < 2) return;
        const keyName = k => typeof k === 'number' && opts.letter ? opts.letter + k : k;
        const auto = autoSide(seq);
        const ref = seq[0].pt.lon;
        const P = seq.map(e => map.latLngToLayerPoint([e.pt.lat, unwrapLon(e.pt.lon, ref)]));
        // +1 = offset to the right of travel (mark passed to port), -1 = left
        const sideSign = i => {
            const e = seq[i];
            if (e.mark !== null) return markSide(course, e.mark, auto) === 'port' ? 1 : -1;
            const nb = seq[i === 0 ? 1 : i - 1];
            return (nb.mark !== null ? markSide(course, nb.mark, auto) : auto) === 'port' ? 1 : -1;
        };
        const pairUse = {};
        const legs = [];
        for (let i = 0; i + 1 < seq.length; i++) {
            const a = P[i], b = P[i + 1];
            const len = a.distanceTo(b);
            const u = len > 0 ? b.subtract(a).divideBy(len) : L.point(1, 0);
            const n = L.point(-u.y, u.x);  // right of travel in screen coords
            const ka = `${seq[i].pt.lat},${seq[i].pt.lon}`, kb = `${seq[i + 1].pt.lat},${seq[i + 1].pt.lon}`;
            const pk = ka < kb ? ka + '|' + kb : kb + '|' + ka;
            const k = pairUse[pk] = (pairUse[pk] === undefined ? 0 : pairUse[pk] + 1);
            const r = OFFSET_PX + k * GAP_PX;
            const sa = sideSign(i), sb = sideSign(i + 1);
            // only roundings are offset; start/finish ends go to the point itself
            // (the mark, or the middle of its pin line)
            const ra = seq[i].mark === null ? 0 : r, rb = seq[i + 1].mark === null ? 0 : r;
            legs.push({ n, ra, rb, sa, sb, from: a.add(n.multiplyBy(sa * ra)), to: b.add(n.multiplyBy(sb * rb)) });
        }

        const route = [legs[0].from, legs[0].to];
        for (let i = 1; i < legs.length; i++) {
            const c = P[i], pin = legs[i - 1].to, pout = legs[i].from;
            const rin = legs[i - 1].rb, rout = legs[i].ra;
            const a0 = Math.atan2(pin.y - c.y, pin.x - c.x);
            let d = Math.atan2(pout.y - c.y, pout.x - c.x) - a0;
            // turn between the legs, screen coords (>0 = clockwise = to starboard)
            const ui = L.point(legs[i - 1].n.y, -legs[i - 1].n.x), uo = L.point(legs[i].n.y, -legs[i].n.x);
            const turn = Math.atan2(ui.x * uo.y - ui.y * uo.x, ui.x * uo.x + ui.y * uo.y);
            if (sideSign(i) * turn > 0 && Math.abs(turn) < Math.PI / 2) {
                // gentle turn away from the rounding side: the mark is passed on the
                // right side without going round it. Join the two offset lines where
                // they cross near the mark, else taper between them.
                const lin = legs[i - 1].from.distanceTo(pin), lout = pout.distanceTo(legs[i].to);
                const cr = ui.x * uo.y - ui.y * uo.x, w = pout.subtract(pin);
                const s = Math.abs(cr) > 1e-9 ? (w.x * uo.y - w.y * uo.x) / cr : NaN;
                const t = Math.abs(cr) > 1e-9 ? (w.x * ui.y - w.y * ui.x) / cr : NaN;
                if (s <= 0 && s >= -lin / 2 && t >= 0 && t <= lout / 2) {
                    route[route.length - 1] = pin.add(ui.multiplyBy(s));
                } else {
                    const taper = Math.min(40, lin / 3, lout / 3);
                    route[route.length - 1] = pin.subtract(ui.multiplyBy(taper));
                    route.push(pout.add(uo.multiplyBy(taper)));
                }
                route.push(legs[i].to);
                continue;
            } else if (sideSign(i) > 0) { while (d > 0) d -= 2 * Math.PI; }   // port: anticlockwise on screen
            else { while (d < 0) d += 2 * Math.PI; }
            const steps = Math.max(2, Math.ceil(Math.abs(d) / 0.2));
            for (let j = 1; j < steps; j++) {
                const f = j / steps, ang = a0 + d * f, rr = rin + (rout - rin) * f;
                route.push(L.point(c.x + rr * Math.cos(ang), c.y + rr * Math.sin(ang)));
            }
            route.push(pout, legs[i].to);
        }
        add(L.polyline(route.map(p => map.layerPointToLatLng(p)), {
            color: opts.color || '#ff6600', weight: 3, opacity: opts.opacity || 0.85, interactive: false
        }));

        const arrows = [];
        legs.forEach(g => {
            const mid = g.from.add(g.to).divideBy(2);
            const deg = Math.atan2(g.to.y - g.from.y, g.to.x - g.from.x) * 180 / Math.PI;
            arrows.push(mid);
            add(L.marker(map.layerPointToLatLng(mid), {
                icon: L.divIcon({
                    className: 'leg-arrow', iconSize: [18, 18], iconAnchor: [9, 9],
                    html: `<svg width="18" height="18" viewBox="-9 -9 18 18" style="transform:rotate(${deg}deg)">` +
                          `<path d="M-6,-6 L7,0 L-6,6 L-2,0 Z" fill="${opts.color || '#ff6600'}" stroke="#7c2d12" stroke-width="1"/></svg>`
                }),
                interactive: false, keyboard: false
            }));
        });
        if (!opts.labels) return;
        const decl = declinationAt(seq.map(e => e.pt));
        const items = legs.map((g, i) => ({
            a: g.from, b: g.to, n: g.n, pref: (g.sa + g.sb) >= 0 ? 1 : -1,
            html: legLabelHtml(seq[i].pt, seq[i + 1].pt, decl, `${keyName(seq[i].key)}→${keyName(seq[i + 1].key)}: `)
        }));
        const obstacles = P.map(p => box(p, 30, 30)).concat(arrows.map(p => box(p, 18, 18)));
        const shown = items.filter(it => it.html);
        placeLabels(map, shown, obstacles).forEach((c, i) => add(labelMarker(map, c, shown[i].html)));
    }

    // --- leg labels with overlap avoidance (screen space, so redrawn on zoom) ---

    const fmt3 = v => String(Math.round(v) % 360).padStart(3, '0');

    function declinationAt(pts) {
        if (typeof wmmDeclination !== 'function' || !pts.length) return null;
        const ref = pts[0].lon;
        return wmmDeclination(pts.reduce((a, p) => a + p.lat, 0) / pts.length,
                              pts.reduce((a, p) => a + unwrapLon(p.lon, ref), 0) / pts.length);
    }

    // what leg labels show; both off = no labels
    const labelOpts = { bearing: true, distance: true };

    function legLabelHtml(a, b, decl, prefix = '') {
        const lines = [];
        if (labelOpts.bearing) {
            const t = bearingDeg(a, b);
            lines.push(decl === null ? `${fmt3(t)}°T` : `${fmt3(t)}°T / ${fmt3((t - decl + 360) % 360)}°M`);
        }
        if (labelOpts.distance) lines.push(legLengthText(a, b));
        if (!lines.length) return null;
        lines[0] = prefix + lines[0];
        return `<div>${lines.join('<br>')}</div>`;
    }

    const box = (c, w, h) => ({ x0: c.x - w / 2, y0: c.y - h / 2, x1: c.x + w / 2, y1: c.y + h / 2 });
    const overlapArea = (p, q) => Math.max(0, Math.min(p.x1, q.x1) - Math.max(p.x0, q.x0)) *
                                  Math.max(0, Math.min(p.y1, q.y1) - Math.max(p.y0, q.y0));

    // rendered size of a label, measured once per distinct text
    const sizeCache = new Map();
    function labelSize(map, html) {
        if (sizeCache.has(html)) return sizeCache.get(html);
        const host = document.createElement('div');
        host.className = 'leg-label';
        host.style.cssText = 'position:absolute;left:-9999px;top:0;visibility:hidden';
        host.innerHTML = html;
        map.getContainer().appendChild(host);
        const el = host.firstElementChild;
        const sz = { w: el.offsetWidth || 120, h: el.offsetHeight || 36 };
        host.remove();
        sizeCache.set(html, sz);
        return sz;
    }

    // Boxes bucketed on a coarse screen grid so overlap tests only look nearby
    function boxGrid(cell = 80) {
        const cells = new Map();
        const each = (b, fn) => {
            for (let gx = Math.floor(b.x0 / cell); gx <= Math.floor(b.x1 / cell); gx++)
                for (let gy = Math.floor(b.y0 / cell); gy <= Math.floor(b.y1 / cell); gy++) fn(gx + ',' + gy);
        };
        return {
            add(b) { each(b, k => { const a = cells.get(k); if (a) a.push(b); else cells.set(k, [b]); }); },
            cost(r) {
                const seen = new Set();
                let c = 0;
                each(r, k => (cells.get(k) || []).forEach(q => { if (!seen.has(q)) { seen.add(q); c += overlapArea(r, q); } }));
                return c;
            }
        };
    }

    // Greedy placement: each label (shorter legs first) takes the first candidate
    // spot beside its leg that clears placed labels and obstacles; candidates slide
    // along the leg, then try the other side, then further out. Returns centres.
    function placeLabels(map, items, obstacles) {
        const PAD = 3;
        const placed = boxGrid();
        obstacles.forEach(b => placed.add(b));
        const out = new Array(items.length);
        const order = items.map((it, i) => i).sort((i, j) =>
            items[i].a.distanceTo(items[i].b) - items[j].a.distanceTo(items[j].b));
        const ts = [0.5, 0.4, 0.6, 0.3, 0.7, 0.2, 0.8];
        for (const i of order) {
            const it = items[i];
            const { w, h } = labelSize(map, it.html);
            const clear = 8 + Math.abs(it.n.x) * w / 2 + Math.abs(it.n.y) * h / 2;
            const step = Math.abs(it.n.x) * w + Math.abs(it.n.y) * h + 4;
            let best = null, bestCost = Infinity;
            search:
            for (const extra of [0, 1, 2]) {
                for (const side of [it.pref, -it.pref]) {
                    for (const t of ts) {
                        const c = it.a.add(it.b.subtract(it.a).multiplyBy(t))
                                      .add(it.n.multiplyBy(side * (clear + extra * step)));
                        const cost = placed.cost(box(c, w + 2 * PAD, h + 2 * PAD));
                        if (cost === 0) { best = c; break search; }
                        if (cost < bestCost) { bestCost = cost; best = c; }
                    }
                }
            }
            out[i] = best;
            placed.add(box(best, w, h));
        }
        return out;
    }

    function labelMarker(map, c, html) {
        return L.marker(map.layerPointToLatLng(c), {
            icon: L.divIcon({ className: 'leg-label', iconSize: [0, 0], html }),
            interactive: false, keyboard: false
        });
    }

    // Labels for straight legs between consecutive points (plain courses, drawing)
    function drawLegLabels(map, pts, opts, add) {
        if (pts.length < 2) return;
        const ref = pts[0].lon;
        const P = pts.map(p => map.latLngToLayerPoint([p.lat, unwrapLon(p.lon, ref)]));
        const decl = declinationAt(pts);
        const items = [];
        for (let i = 0; i + 1 < pts.length; i++) {
            const len = P[i].distanceTo(P[i + 1]);
            const u = len > 0 ? P[i + 1].subtract(P[i]).divideBy(len) : L.point(1, 0);
            items.push({ a: P[i], b: P[i + 1], n: L.point(-u.y, u.x), pref: 1,
                         html: legLabelHtml(pts[i], pts[i + 1], decl) });
        }
        const shown = items.filter(it => it.html);
        placeLabels(map, shown, P.map(p => box(p, 30, 30))).forEach((c, i) => add(labelMarker(map, c, shown[i].html)));
    }

    // --- checking a course read from a file; throws with the reason ---

    const isNum = v => typeof v === 'number' && Number.isFinite(v);
    const isObj = v => v !== null && typeof v === 'object' && !Array.isArray(v);

    // Check an imported start/finish ('S'/'F') or mark; throws with the reason
    function checkImportedPoint(p, what, kind) {
        if (!isObj(p) || !isNum(p.lat) || !isNum(p.lon) || Math.abs(p.lat) > 90 || Math.abs(p.lon) > 180)
            throw new Error(`missing or invalid ${what} position`);
        if (p.comment !== undefined && typeof p.comment !== 'string') throw new Error(`invalid ${what} comment`);
        if (kind === 'mark') {
            if (p.color !== undefined && p.color !== safeColor(p.color, null)) throw new Error(`invalid ${what} colour`);
            if (p.rounding !== undefined && p.rounding !== 'port' && p.rounding !== 'starboard')
                throw new Error(`invalid ${what} rounding`);
        } else if (p.pin !== undefined && (!isObj(p.pin) || !isNum(p.pin.dist) || !(p.pin.dist > 0 && p.pin.dist <= 10000) ||
                                           (p.pin.side !== 'left' && p.pin.side !== 'right'))) {
            throw new Error(`invalid ${what} pin`);
        }
    }

    // Check one course (A, or one of extraCourses) of a course file
    function checkImportedSub(j, prefix) {
        if (!isObj(j)) throw new Error(`${prefix}not a course`);
        checkImportedPoint(j.start, prefix + 'start', 'S');
        checkImportedPoint(j.finish, prefix + 'finish', 'F');
        if (j.marks !== undefined) {
            if (!Array.isArray(j.marks)) throw new Error(`invalid ${prefix}marks`);
            j.marks.forEach((m, i) => checkImportedPoint(m, `${prefix}mark ${i + 1}`, 'mark'));
        }
        if (j.order !== undefined && typeof j.order !== 'string') throw new Error(`invalid ${prefix}order`);
    }

    function checkCourse(j) {
        checkImportedSub(j, '');
        if (j.extraCourses !== undefined) {
            if (!Array.isArray(j.extraCourses) || j.extraCourses.length > MAX_COURSES - 1)
                throw new Error('invalid extra courses');
            j.extraCourses.forEach((x, i) => checkImportedSub(x, `course ${letterOf(i + 1)} `));
        }
    }

    // --- course marker icons (Leaflet divIcons) ---

    function darkenColor(hex) {
        const r = parseInt(hex.slice(1, 3), 16);
        const g = parseInt(hex.slice(3, 5), 16);
        const b = parseInt(hex.slice(5, 7), 16);
        const darken = (v) => Math.max(0, Math.floor(v * 0.7)).toString(16).padStart(2, '0');
        return `#${darken(r)}${darken(g)}${darken(b)}`;
    }

    // letter: course letter shown on the icon when the file has several courses
    function startIcon(label, letter) {
        const text = letter ? `S<tspan font-size="9" dy="1">${letter}</tspan>` : 'S';
        const svg = `
            <svg width="32" height="32" viewBox="-16 -16 32 32" xmlns="http://www.w3.org/2000/svg">
                <circle cx="0" cy="0" r="12" fill="#22c55e" stroke="#166534" stroke-width="2"/>
                <text x="0" y="5" text-anchor="middle" fill="white" font-size="12" font-weight="bold">${text}</text>
            </svg>
        `;
        const labelHtml = label ? `<div class="marker-label">${label}</div>` : '';
        return L.divIcon({
            html: `<div style="position:relative">${svg}${labelHtml}</div>`,
            className: 'course-marker',
            iconSize: [32, 32 + (label ? 20 : 0)],
            iconAnchor: [16, 16]
        });
    }

    function markIcon(number, color, label) {
        color = color || '#f97316';
        const strokeColor = darkenColor(color);
        const fs = String(number).length > 2 ? 9 : 11;
        const svg = `
            <svg width="32" height="32" viewBox="-16 -16 32 32" xmlns="http://www.w3.org/2000/svg">
                <circle cx="0" cy="0" r="12" fill="${color}" stroke="${strokeColor}" stroke-width="2"/>
                <text x="0" y="${fs === 11 ? 5 : 4}" text-anchor="middle" fill="white" font-size="${fs}" font-weight="bold">${number}</text>
            </svg>
        `;
        const labelHtml = label ? `<div class="marker-label">${label}</div>` : '';
        return L.divIcon({
            html: `<div style="position:relative">${svg}${labelHtml}</div>`,
            className: 'course-marker',
            iconSize: [32, 32 + (label ? 20 : 0)],
            iconAnchor: [16, 16]
        });
    }

    function finishIcon(label, letter, color) {
        const badge = letter ? `<circle cx="10" cy="-10" r="6" fill="${color}" stroke="white" stroke-width="1"/>` +
                               `<text x="10" y="-7" text-anchor="middle" fill="white" font-size="9" font-weight="bold">${letter}</text>` : '';
        const svg = `
            <svg width="32" height="32" viewBox="-16 -16 32 32" xmlns="http://www.w3.org/2000/svg">
                <rect x="-12" y="-12" width="24" height="24" fill="white" stroke="#333" stroke-width="2" rx="3"/>
                <rect x="-12" y="-12" width="6" height="6" fill="#333"/>
                <rect x="0" y="-12" width="6" height="6" fill="#333"/>
                <rect x="-6" y="-6" width="6" height="6" fill="#333"/>
                <rect x="6" y="-6" width="6" height="6" fill="#333"/>
                <rect x="-12" y="0" width="6" height="6" fill="#333"/>
                <rect x="0" y="0" width="6" height="6" fill="#333"/>
                <rect x="-6" y="6" width="6" height="6" fill="#333"/>
                <rect x="6" y="6" width="6" height="6" fill="#333"/>
                ${badge}
            </svg>
        `;
        const labelHtml = label ? `<div class="marker-label">${label}</div>` : '';
        return L.divIcon({
            html: `<div style="position:relative">${svg}${labelHtml}</div>`,
            className: 'course-marker',
            iconSize: [32, 32 + (label ? 20 : 0)],
            iconAnchor: [16, 16]
        });
    }

    // Layer that redraws itself on zoom (offsets and label placement are in pixels).
    // Built on first use so pages without Leaflet can still use the helpers.
    let ZoomLayer = null;
    const makeZoomLayer = () => L.LayerGroup.extend({
        initialize(drawFn) {
            L.LayerGroup.prototype.initialize.call(this);
            this._drawFn = drawFn;
        },
        onAdd(map) {
            L.LayerGroup.prototype.onAdd.call(this, map);
            map.on('zoomend', this._redraw, this);
            this._redraw();
        },
        onRemove(map) {
            map.off('zoomend', this._redraw, this);
            L.LayerGroup.prototype.onRemove.call(this, map);
        },
        _redraw() {
            if (!this._map) return;
            this.clearLayers();
            this._drawFn(this._map, l => this.addLayer(l));
        }
    });

    const zoomLayer = fn => new (ZoomLayer || (ZoomLayer = makeZoomLayer()))(fn);

    return {
        parse, format, keys, sequence, lengthKm, autoSide, markSide, insertMark, deleteMark,
        pinLine, pinLayers, splitLeg, escHtml, commentHtml, safeColor,
        darkenColor, startIcon, markIcon, finishIcon, checkCourse,
        MAX_COURSES, COURSE_COLORS, letterOf, subCourses, subCourse, courseCount, plainCourse, removeCourse, markName,
        setLabelOptions: o => Object.assign(labelOpts, o),
        labelOptions: () => ({ ...labelOpts }),
        routeLayer: (course, opts = {}) => zoomLayer((m, add) => draw(m, course, opts, add)),
        legLabelLayer: (pts, opts = {}) => zoomLayer((m, add) => drawLegLabels(m, pts, opts, add))
    };
})();
