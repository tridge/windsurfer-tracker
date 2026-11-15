// Reference implementation of the wind-based course time model (docs/CourseTiming.md).
// Plain browser/Node JS, no dependencies. Parameters come from params.json (course_time_model.py fit).
(function (root) {
    'use strict';
    const KN = 1852 / 3600, RAD = Math.PI / 180;

    function interp(x, xs, ys) {
        if (x <= xs[0]) return ys[0];
        for (let i = 1; i < xs.length; i++) {
            if (x <= xs[i]) return ys[i - 1] + (ys[i] - ys[i - 1]) * (x - xs[i - 1]) / (xs[i] - xs[i - 1]);
        }
        return ys[ys.length - 1];
    }
    const angDiff = (a, b) => ((a - b + 180) % 360 + 360) % 360 - 180;

    // speed along a straight leg, as a multiple of upwind VMG
    const straightRatio = (P, twa) => interp(twa, P.ratio_angles, P.ratios);

    // best downwind VMG (multiple of upwind VMG) and its angle
    function downVmg(P) {
        let best = 0, phi = 180;
        for (let f = 90; f <= P.gamma_deg; f++) {
            const v = straightRatio(P, f) * -Math.cos(f * RAD);
            if (v > best) { best = v; phi = f; }
        }
        return { vmg: best, phi };
    }

    // seconds for one leg of distM metres at true wind angle twa (0..180), wind twsKn knots
    function legTime(P, distM, twsKn, twa) {
        const vmg = P.vmg_slope * (twsKn + P.w0_kn) * KN;          // m/s
        if (!(vmg > 0)) return { s: Infinity, mode: 'x' };
        if (twa <= P.beta_deg) return { s: distM * Math.cos(twa * RAD) / vmg, mode: 'beat' };
        let s = twa <= P.gamma_deg ? distM / (vmg * straightRatio(P, twa)) : Infinity;
        let mode = twa < 150 ? 'reach' : 'run';
        if (twa > 90) {
            const d = downVmg(P);
            if (twa > d.phi) {
                const g = distM * -Math.cos(twa * RAD) / (vmg * d.vmg) + P.gybe_s;
                if (g < s) { s = g; mode = 'gybe'; }
            }
        }
        return { s, mode };
    }

    // legs: [{dist (m), bearing (deg true)}] in sailing order (start-line middle .. finish).
    // windFrom: true wind-from deg; default = bearing of the first leg (first leg is a beat).
    function courseTime(P, legs, twsKn, windFrom) {
        if (!legs.length) return null;
        if (windFrom == null) windFrom = legs[0].bearing;
        let total = 0, prev = null;
        const detail = legs.map(l => {
            const d = angDiff(l.bearing, windFrom), twa = Math.abs(d), stbd = d > 0;
            const r = legTime(P, l.dist, twsKn, twa);
            let s = r.s;
            if (prev) s += (prev.twa >= 90 && twa >= 90 && prev.stbd !== stbd) ? P.gybe_s : P.turn_s;
            prev = { twa, stbd };
            total += s;
            return { twa, mode: r.mode, s };
        });
        return { seconds: total, legs: detail, windFrom };
    }

    const api = { courseTime, legTime, downVmg };
    if (typeof module !== 'undefined' && module.exports) module.exports = api;
    else root.CourseTime = api;
})(this);
