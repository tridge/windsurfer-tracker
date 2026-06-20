/**
 * GT06 battery calibration (display-time) for Windsurfer Tracker WebUI.
 *
 * GT06 ONLY. Each unit's voltage divider has a small fixed offset, so the raw
 * bat_v it reports is biased by up to ~50mV. This module loads a public
 * calibration table (/gt06_calibration.json) and corrects records as they are
 * displayed: corrected_v = raw_bat_v - offset, then battery % = remaining
 * capacity from the empirical discharge curve (corrected voltage -> remaining %).
 * Correction happens in the UI, never when recording to logs, so historical logs
 * stay raw and any display reads the same corrected value.
 *
 * NOT applied to phone/app trackers (they own their own %). Gated on GT06 ids
 * (G######) / ver==='gt06'.
 *
 * discharge_curve (in the JSON) is the offset-corrected tracking-voltage curve
 * built from the 2026-06 full discharge; voltageToPercent() below is the legacy
 * single-unit fallback used only if the JSON has no curve.
 */

// Legacy fallback curve (single V6.63 unit, 24h turntable) — only used if the
// calibration JSON provides no discharge_curve.
const _W07C_DISCHARGE = [
    [4.14, 100], [4.03, 95], [3.99, 90], [3.97, 85], [3.93, 80],
    [3.89, 75],  [3.86, 70], [3.82, 65], [3.77, 60], [3.72, 55],
    [3.67, 50],  [3.65, 45], [3.62, 40], [3.60, 35], [3.58, 30],
    [3.55, 25],  [3.52, 20], [3.47, 15], [3.44, 10], [3.37, 5],
];

function voltageToPercent(voltage) {
    const t = _W07C_DISCHARGE;
    if (voltage >= t[0][0]) return 100;
    if (voltage < t[t.length - 1][0]) return 0;
    for (let i = 0; i < t.length - 1; i++) {
        const [vHi, pHi] = t[i];
        const [vLo, pLo] = t[i + 1];
        if (voltage >= vLo) {
            const frac = (voltage - vLo) / (vHi - vLo);
            return Math.round(pLo + frac * (pHi - pLo));
        }
    }
    return 0;
}

// True iff this record is a GT06 tracker (corrections apply only to those).
function isGt06Record(rec) {
    if (!rec) return false;
    if (rec.ver === 'gt06') return true;
    return /^G\d{6}$/.test(String(rec.id || ''));   // GT06 sailor_id = G + 6 digits, exactly
}

const BatteryCal = {
    offsets: {},
    doc: null,          // full calibration document
    loaded: false,
    _loading: null,

    load() {
        if (this._loading) return this._loading;
        this._loading = fetch('/gt06_calibration.json', { cache: 'no-cache' })
            .then(r => (r.ok ? r.json() : null))
            .then(d => { if (d) { this.doc = d; if (d.offsets) this.offsets = d.offsets; } })
            .catch(() => {})
            .finally(() => { this.loaded = true; });
        return this._loading;
    },

    offsetFor(id) {
        return this.offsets[id] || 0;
    },

    // Remaining-capacity % from a CORRECTED voltage, via the empirical discharge
    // curve (curve[k] = corrected voltage at (100-k)% remaining, monotonic down).
    // Falls back to the legacy curve if the JSON has none.
    remainingPercent(v) {
        const c = this.doc && this.doc.discharge_curve;
        if (!c || !c.length) return voltageToPercent(v);
        const n = c.length;
        if (v >= c[0]) return 100;
        if (v <= c[n - 1]) return 0;                            // at/below cutoff floor = empty
        for (let k = 1; k < n; k++) {
            if (v > c[k]) {                       // v in (c[k], c[k-1]]
                const span = c[k - 1] - c[k];
                const frac = span > 0 ? (c[k - 1] - v) / span : 0;
                return Math.round((100 - (k - 1)) - frac);
            }
        }
        return 0;
    },

    // Volts to subtract before the curve lookup to map a cap-class onto the
    // 3Ah-derived discharge curve (6Ah cells sag ~50mV less under load, so they
    // read high vs the curve). Keyed by cap_class.
    classCurveOffset(id) {
        const cls = this.unitCal(id).cap_class;
        const m = (this.doc && this.doc.class_curve_offset_mv) || {};
        return (m[cls] || 0) / 1000;
    },

    // Remaining % from a raw device voltage for one unit: applies the per-unit
    // divider offset and the cap-class curve offset, then looks up the curve.
    percentForUnit(id, rawv) {
        if (rawv == null) return null;
        return this.remainingPercent(rawv + this.offsetFor(id) - this.classCurveOffset(id));
    },

    // Per-unit calibration; falls back to the file's defaults (`_default:true`).
    unitCal(id) {
        const u = this.doc && this.doc.units && this.doc.units[id];
        if (u) return u;
        const d = (this.doc && this.doc.defaults) || {};
        return { offset_mv: d.offset_mv || 0, resistance_ohm: d.resistance_ohm || null,
                 capacity_mah: d.capacity_mah || null, cap_class: d.cap_class || '?',
                 _default: true };
    },

    // Correct one record in place (GT06 only). Applies the divider offset and
    // recomputes bat % from the corrected voltage via the discharge curve.
    // No-op for non-GT06 records or when bat_v is absent.
    correct(rec) {
        if (!isGt06Record(rec)) return rec;
        if (rec.bat_v === undefined || rec.bat_v === null) return rec;
        const off = this.offsetFor(rec.id);
        if (off) rec.bat_v = Math.round((rec.bat_v + off) * 1000) / 1000;
        rec.bat = this.remainingPercent(rec.bat_v - this.classCurveOffset(rec.id));
        return rec;
    },
};
