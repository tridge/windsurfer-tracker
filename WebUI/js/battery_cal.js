/**
 * GT06 battery calibration (display-time) for Windsurfer Tracker WebUI.
 *
 * GT06 ONLY. Converts a device terminal voltage to remaining % via the parametric
 * OCV fit in /gt06_calibration.json (soc_fit, 33-unit full-discharge). It is
 * LOAD-AWARE: a terminal voltage is reconstructed to cell open-circuit voltage,
 *   OCV = raw_bat_v + divider_offset + I_load * R_class
 * (I_load = tracking ~115 mA when active, idle ~5 mA when idle/stopped/sleeping),
 * then SoC = c1*(1 - 1/(1 + (OCV/c2)^c4)^c3). So an idle reading and a tracking
 * reading of the same charge agree (no load-sag step). Correction is display-only,
 * never written to logs. Gated on GT06 ids (G######) / ver==='gt06'.
 *
 * Replaces the old single-cell (G226122) discharge_curve lookup.
 */

// True iff this record is a GT06 tracker (corrections apply only to those).
function isGt06Record(rec) {
    if (!rec) return false;
    if (rec.ver === 'gt06') return true;
    return /^G\d{6}$/.test(String(rec.id || ''));   // GT06 sailor_id = G + 6 digits, exactly
}

const BatteryCal = {
    doc: null,          // full calibration document
    loaded: false,
    _loading: null,

    load() {
        if (this._loading) return this._loading;
        this._loading = fetch('/gt06_calibration.json', { cache: 'no-cache' })
            .then(r => (r.ok ? r.json() : null))
            .then(d => { if (d) this.doc = d; })
            .catch(() => {})
            .finally(() => { this.loaded = true; });
        return this._loading;
    },

    // Per-unit calibration; falls back to the file's defaults (`_default:true`).
    unitCal(id) {
        const u = this.doc && this.doc.units && this.doc.units[id];
        if (u) return u;
        const d = (this.doc && this.doc.defaults) || {};
        return { offset_mv: d.offset_mv || 0, resistance_ohm: d.resistance_ohm || null,
                 capacity_mah: d.capacity_mah || null, cap_class: d.cap_class || '6Ah',
                 _default: true };
    },

    // Fitted divider offset (volts to ADD to raw before the curve), gauge-fixed.
    offsetFor(id) {
        const o = (this.doc && this.doc.soc_fit && this.doc.soc_fit.offsets_mv) || {};
        return (o[id] || 0) / 1000;
    },

    // Cell internal resistance for this unit's capacity class (ohms).
    classR(id) {
        const r = (this.doc && this.doc.soc_fit && this.doc.soc_fit.class_r_ohm) || {};
        return r[this.unitCal(id).cap_class] || 0;
    },

    // Load current (A) for the IR add-back: idle ~ mode_power_w.idle/nominal_V, else track.
    _loadCurrentA(idle) {
        const d = this.doc || {};
        if (idle) {
            const v = d.nominal_voltage || 3.7;
            return v ? ((d.mode_power_w && d.mode_power_w.idle) || 0) / v : 0;
        }
        return (d.track_current_ma || 0) / 1000;
    },

    // SoC% from a cell OCV via the parametric fit; null if no fit / bad OCV.
    _socFromOcv(ocv) {
        const c = this.doc && this.doc.soc_fit && this.doc.soc_fit.coeffs;
        if (!c || !(ocv > 0)) return null;   // (ocv/c2)^c4 needs a positive base
        const s = c.c1 * (1 - 1 / Math.pow(1 + Math.pow(ocv / c.c2, c.c4), c.c3));
        return Number.isFinite(s) ? Math.max(0, Math.min(100, s)) : null;
    },

    // Remaining % from a raw device terminal voltage for one unit, given its load
    // state (idle vs tracking): reconstruct OCV then evaluate the fit. null if no fit.
    socFor(id, rawv, idle) {
        if (rawv == null) return null;
        const ocv = rawv + this.offsetFor(id) + this._loadCurrentA(idle) * this.classR(id);
        const s = this._socFromOcv(ocv);
        return s == null ? null : Math.round(s);
    },

    // Back-compat alias; idle defaults to tracking load when the caller can't tell.
    percentForUnit(id, rawv, idle = false) {
        return this.socFor(id, rawv, idle);
    },

    // Correct one record in place (GT06 only): set bat_v to the divider-corrected
    // terminal voltage and bat to the load-aware SoC%. Load state comes from the
    // record's idle/stopped/sleep flags (live views set them; log entries are
    // tracking-load). No-op for non-GT06 records or when bat_v is absent.
    correct(rec) {
        if (!isGt06Record(rec)) return rec;
        if (rec.bat_v === undefined || rec.bat_v === null) return rec;
        const idle = !!(rec.idle || rec.stopped || rec.sleep);
        const raw = rec.bat_v;
        const s = this.socFor(rec.id, raw, idle);
        const off = this.offsetFor(rec.id);
        if (off) rec.bat_v = Math.round((raw + off) * 1000) / 1000;
        if (s != null) rec.bat = s;
        return rec;
    },
};
