/**
 * GT06 battery voltage calibration (display-time) for Windsurfer Tracker WebUI.
 *
 * Each unit's voltage divider has a small fixed offset, so the raw bat_v it
 * reports (and the % derived from it) is biased by up to ~50mV. This module
 * loads a public per-unit offset table (/gt06_calibration.json) and corrects
 * records as they are ingested: bat_v += offset, and bat is recomputed from the
 * corrected voltage. Correcting in the UI (not at capture) means historical logs
 * are corrected too, and the raw logged data stays untouched.
 *
 * voltageToPercent() is a direct port of voltage_to_percent() in
 * server/protocol_GT06.py (the W07C 24h-turntable discharge curve) so an
 * offset of 0 reproduces the server's stored %.
 */

// (voltage, percent), descending voltage, evenly spaced in time.
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

const BatteryCal = {
    offsets: {},
    doc: null,          // full calibration document (v2: units, defaults, ...)
    loaded: false,
    _loading: null,

    // Fetch the global offset table once. Tolerates 404 (offsets stay empty,
    // so every record passes through unchanged). Safe to await repeatedly.
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

    // Per-unit 3-param calibration (v2). Falls back to the file's defaults
    // (6Ah medians) for an unseen device; `_default:true` flags that case.
    unitCal(id) {
        const u = this.doc && this.doc.units && this.doc.units[id];
        if (u) return u;
        const d = (this.doc && this.doc.defaults) || {};
        return { offset_mv: d.offset_mv || 0, resistance_ohm: d.resistance_ohm || null,
                 capacity_mah: d.capacity_mah || null, cap_class: d.cap_class || '?',
                 _default: true };
    },

    // Correct one record in place. No-op when bat_v is absent (old records keep
    // their stored bat). Returns the same record for chaining.
    correct(rec) {
        if (!rec || rec.bat_v === undefined || rec.bat_v === null) return rec;
        const off = this.offsetFor(rec.id);
        if (off) rec.bat_v = Math.round((rec.bat_v + off) * 1000) / 1000;
        rec.bat = voltageToPercent(rec.bat_v);
        return rec;
    },
};
