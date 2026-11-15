// Wind-based course time estimate for the median sailor (docs/CourseTiming.md).
// Upwind VMG scales with wind speed; other legs run at a multiple of it that depends
// on the true wind angle; beats are tacked and deep runs gybed; marks add turn time.
// PARAMS come from scripts/course_time/params.json (course_time_model.py fit) and are
// checked against it by scripts/course_time/test_js_parity.py.
// courseTime() is plain JS; estimate()/courseLegs() use CourseOrder (course-order.js) if loaded.
const CourseTime = (() => {
    const PARAMS = {
        vmg_slope: 0.165027,         // upwind VMG (kn) per kn of (TWS + w0)
        w0_kn: 2.0,
        beta_deg: 50.0,              // legs up to this angle off the wind are tacked
        gamma_deg: 165.0,            // legs deeper than this are gybed
        ratio_angles: [50.0, 90, 135, 165.0],
        ratios: [1.555724, 3.05671, 2.439731, 2.064004],   // leg speed / upwind VMG
        turn_s: 2.0,
        gybe_s: 6.0
    };
    const KN = 1852 / 3600, rad = Math.PI / 180;

    function interp(x, xs, ys) {
        if (x <= xs[0]) return ys[0];
        for (let i = 1; i < xs.length; i++) {
            if (x <= xs[i]) return ys[i - 1] + (ys[i] - ys[i - 1]) * (x - xs[i - 1]) / (xs[i] - xs[i - 1]);
        }
        return ys[ys.length - 1];
    }
    const angDiff = (a, b) => ((a - b + 180) % 360 + 360) % 360 - 180;
    const straightRatio = (P, twa) => interp(twa, P.ratio_angles, P.ratios);

    // best downwind VMG (multiple of upwind VMG) and the angle it's sailed at
    function downVmg(P) {
        let vmg = 0, phi = 180;
        for (let f = 90; f <= P.gamma_deg; f++) {
            const v = straightRatio(P, f) * -Math.cos(f * rad);
            if (v > vmg) { vmg = v; phi = f; }
        }
        return { vmg, phi };
    }

    // seconds for distM metres at true wind angle twa (0..180) in twsKn knots
    function legTime(P, distM, twsKn, twa) {
        const vmg = P.vmg_slope * (twsKn + P.w0_kn) * KN;   // m/s
        if (!(vmg > 0)) return { s: Infinity, mode: 'x' };
        if (twa <= P.beta_deg) return { s: distM * Math.cos(twa * rad) / vmg, mode: 'beat' };
        let s = twa <= P.gamma_deg ? distM / (vmg * straightRatio(P, twa)) : Infinity;
        let mode = twa < 150 ? 'reach' : 'run';
        if (twa > 90) {
            const d = downVmg(P);
            if (twa > d.phi) {
                const g = distM * -Math.cos(twa * rad) / (vmg * d.vmg) + P.gybe_s;
                if (g < s) { s = g; mode = 'gybe'; }
            }
        }
        return { s, mode };
    }

    // legs: [{dist (m), bearing (deg true)}] in sailing order. windFrom: true wind-from
    // bearing; default the first leg's bearing (first leg is a beat). P: params override.
    // Returns {seconds, windFrom, legs: [{twa, mode, s}]} or null.
    function courseTime(legs, twsKn, windFrom = null, P = PARAMS) {
        if (!legs || !legs.length || !(twsKn > 0)) return null;
        if (windFrom === null || windFrom === undefined) windFrom = legs[0].bearing;
        let total = 0, prev = null;
        const detail = legs.map(l => {
            const d = angDiff(l.bearing, windFrom), twa = Math.abs(d), port = d > 0;   // port tack
            const r = legTime(P, l.dist, twsKn, twa);
            let s = r.s;
            if (prev) s += (prev.twa >= 90 && twa >= 90 && prev.port !== port) ? P.gybe_s : P.turn_s;
            prev = { twa, port };
            total += s;
            return { twa, mode: r.mode, s };
        });
        return { seconds: total, windFrom, legs: detail };
    }

    function distM(a, b) {
        const dLat = (b.lat - a.lat) * rad, dLon = (b.lon - a.lon) * rad;
        const h = Math.sin(dLat / 2) ** 2 + Math.cos(a.lat * rad) * Math.cos(b.lat * rad) * Math.sin(dLon / 2) ** 2;
        return 6371000 * 2 * Math.atan2(Math.sqrt(h), Math.sqrt(1 - h));
    }
    function bearingDeg(a, b) {
        const y = Math.sin((b.lon - a.lon) * rad) * Math.cos(b.lat * rad);
        const x = Math.cos(a.lat * rad) * Math.sin(b.lat * rad) -
                  Math.sin(a.lat * rad) * Math.cos(b.lat * rad) * Math.cos((b.lon - a.lon) * rad);
        return (Math.atan2(y, x) / rad + 360) % 360;
    }

    // [{lat, lon}] in sailing order -> legs (zero-length legs dropped)
    function legsFromPoints(pts) {
        const legs = [];
        for (let i = 1; i < pts.length; i++) {
            const d = distM(pts[i - 1], pts[i]);
            if (d > 0.5) legs.push({ dist: d, bearing: bearingDeg(pts[i - 1], pts[i]) });
        }
        return legs;
    }

    // legs of one course along the same points the course length is measured on
    function courseLegs(course) {
        if (!course || !course.start || !course.finish || typeof CourseOrder === 'undefined') return [];
        return legsFromPoints(CourseOrder.sequence(course).map(e => e.pt));
    }

    // course time in seconds for twsKn, wind down the first leg unless windFrom given; null if none
    function estimate(course, twsKn, windFrom = null) {
        const r = courseTime(courseLegs(course), twsKn, windFrom);
        return r ? r.seconds : null;
    }

    const api = { PARAMS, courseTime, legTime, downVmg, legsFromPoints, courseLegs, estimate };
    if (typeof module !== 'undefined' && module.exports) module.exports = api;
    return api;
})();
