// Course mark rounding order (course.order, e.g. "S12313F") and per-mark
// port/starboard rounding (mark.rounding), shared by the course pages.
// routeLayer() needs Leaflet; uses wmmDeclination() from wmm.js for labels if loaded.
const CourseOrder = (() => {
    const EARTH_R = 6371000;
    const rad = Math.PI / 180;
    const OFFSET_PX = 18;   // route offset from a mark
    const GAP_PX = 14;      // extra offset each time a pair of points is reused

    const unwrapLon = (lon, ref) => lon + 360 * Math.round((ref - lon) / 360);

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

    // Dotted start/finish lines and pin markers for a course (not yet added to a map)
    function pinLayers(course, opts = {}) {
        const layers = [];
        for (const which of ['S', 'F']) {
            const pl = pinLine(course, which);
            if (!pl) continue;
            const ref = pl.mark.lon;
            layers.push(L.polyline([[pl.mark.lat, pl.mark.lon], [pl.pin.lat, unwrapLon(pl.pin.lon, ref)]], {
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
        const shifted = ks.map(k => typeof k === 'number' && k >= num ? k + 1 : k);
        const nk = typeof nextKey === 'number' && nextKey >= num ? nextKey + 1 : nextKey;
        const out = [];
        shifted.forEach((k, i) => {
            out.push(k);
            if (k === prevKey && shifted[i + 1] === nk) out.push(num);
        });
        course.order = format(out);
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
            if (sideSign(i) > 0) { while (d > 0) d -= 2 * Math.PI; }   // port: anticlockwise on screen
            else { while (d < 0) d += 2 * Math.PI; }
            const steps = Math.max(2, Math.ceil(Math.abs(d) / 0.2));
            for (let j = 1; j < steps; j++) {
                const f = j / steps, ang = a0 + d * f, rr = rin + (rout - rin) * f;
                route.push(L.point(c.x + rr * Math.cos(ang), c.y + rr * Math.sin(ang)));
            }
            route.push(pout, legs[i].to);
        }
        add(L.polyline(route.map(p => map.layerPointToLatLng(p)), {
            color: opts.color || '#ff6600', weight: 3, opacity: 0.85, interactive: false
        }));

        let decl = null;
        if (opts.labels && typeof wmmDeclination === 'function') {
            const c = seq.reduce((acc, e) => ({ lat: acc.lat + e.pt.lat / seq.length,
                                               lon: acc.lon + unwrapLon(e.pt.lon, ref) / seq.length }), { lat: 0, lon: 0 });
            decl = wmmDeclination(c.lat, c.lon);
        }
        const fmt = v => String(Math.round(v) % 360).padStart(3, '0');
        const LW = 116, LH = 36;
        legs.forEach((g, i) => {
            const mid = g.from.add(g.to).divideBy(2);
            const deg = Math.atan2(g.to.y - g.from.y, g.to.x - g.from.x) * 180 / Math.PI;
            add(L.marker(map.layerPointToLatLng(mid), {
                icon: L.divIcon({
                    className: 'leg-arrow', iconSize: [18, 18], iconAnchor: [9, 9],
                    html: `<svg width="18" height="18" viewBox="-9 -9 18 18" style="transform:rotate(${deg}deg)">` +
                          `<path d="M-6,-6 L7,0 L-6,6 L-2,0 Z" fill="${opts.color || '#ff6600'}" stroke="#7c2d12" stroke-width="1"/></svg>`
                }),
                interactive: false, keyboard: false
            }));
            if (!opts.labels) return;
            const side = (g.sa + g.sb) >= 0 ? 1 : -1;
            const away = 12 + Math.abs(g.n.x) * LW / 2 + Math.abs(g.n.y) * LH / 2;
            const t = bearingDeg(seq[i].pt, seq[i + 1].pt);
            const hdg = decl === null ? `${fmt(t)}°T` : `${fmt(t)}°T / ${fmt((t - decl + 360) % 360)}°M`;
            add(L.marker(map.layerPointToLatLng(mid.add(g.n.multiplyBy(side * away))), {
                icon: L.divIcon({
                    className: 'leg-label', iconSize: [0, 0],
                    html: `<div>${seq[i].key}→${seq[i + 1].key}: ${hdg}<br>${legLengthText(seq[i].pt, seq[i + 1].pt)}</div>`
                }),
                interactive: false, keyboard: false
            }));
        });
    }

    // Layer drawing the ordered route; redraws itself on zoom (offsets are in pixels).
    // Built on first use so pages without Leaflet can still use the helpers.
    let RouteLayer = null;
    const makeRouteLayer = () => L.LayerGroup.extend({
        initialize(course, opts) {
            L.LayerGroup.prototype.initialize.call(this);
            this._course = course;
            this._opts = opts || {};
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
            draw(this._map, this._course, this._opts, l => this.addLayer(l));
        }
    });

    return {
        parse, format, keys, sequence, lengthKm, autoSide, markSide, insertMark, deleteMark,
        pinLine, pinLayers,
        routeLayer: (course, opts) => new (RouteLayer || (RouteLayer = makeRouteLayer()))(course, opts)
    };
})();
