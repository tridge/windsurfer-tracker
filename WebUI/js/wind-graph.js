// Wind speed graph from an event's daily wind logs <base>logs/YYYY_MM_DD_wind.jsonl (dated in
// the event's time zone, appended about once a minute by e.g. scripts/parkdale_wind.py):
// {"t": unix s, "s": speed kn, "g": gust kn, "d": direction from °T, "a": 10 min average °T}.
// Shown in a floating panel.
//
// WindGraph.attach(itemId, base, { tz, date }): show the menu entry only while that day's file
//   exists; tz/date may be functions (date 'YYYY-MM-DD', default today), re-checked as they change
// WindGraph.open({ base, from, to, tz, live, cursor, title })
//   from/to: unix s; live: hours back from now (adds range buttons, refreshes each minute)
//   tz: IANA zone of the event: file dates and the time axis (default the browser's)
//   cursor: optional () => unix s or null, drawn as a marker
// WindGraph.close()
// WindGraph.dayRange('YYYY-MM-DD', tz): [start, end] unix s of that day in tz
const WindGraph = (() => {
    const NS = 'http://www.w3.org/2000/svg';
    const LIVE_HOURS = [1, 3, 6, 12, 24];
    const ARROW_STEPS = [300, 600, 900, 1800, 3600, 7200, 10800];
    const TICK_STEPS = [300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400];
    const C_SPEED = '#1d4ed8', C_GUST = '#93c5fd', C_BAND = 'rgba(96,165,250,0.25)', C_CURSOR = '#dc2626';

    let panel = null, opts = null, data = [], gapS = 300, from = 0, to = 0;
    let refreshTimer = null, cursorTimer = null, ro = null, loadSeq = 0;
    let svg = null, plot = null, tip = null, hoverEls = null, cursorEl = null;

    const num = v => typeof v === 'number' && Number.isFinite(v);
    const fmt3 = v => String(Math.round(((v % 360) + 360) % 360) % 360).padStart(3, '0');

    const css = `
        .wg-panel { position: fixed; top: 60px; left: 50%; transform: translateX(-50%); z-index: 10000;
                    width: min(920px, calc(100vw - 16px)); background: #fff; border-radius: 8px;
                    box-shadow: 0 4px 18px rgba(0,0,0,0.35); font: 12px/1.3 system-ui, sans-serif; color: #1f2937; }
        .wg-head { display: flex; align-items: center; gap: 8px; padding: 6px 8px 4px 12px; flex-wrap: wrap; }
        .wg-title { font-weight: 700; font-size: 14px; }
        .wg-stats { color: #4b5563; flex: 1; min-width: 120px; }
        .wg-ranges button { border: 1px solid #cbd5e1; background: #f8fafc; color: #1f2937; border-radius: 4px;
                            padding: 2px 7px; margin-left: 2px; font: inherit; cursor: pointer; }
        .wg-ranges button.on { background: #1d4ed8; border-color: #1d4ed8; color: #fff; }
        .wg-close { border: none; background: none; font-size: 22px; line-height: 1; cursor: pointer; color: #4b5563; padding: 0 4px; }
        .wg-body { position: relative; height: 280px; touch-action: none; user-select: none; -webkit-user-select: none; }
        .wg-body svg { display: block; width: 100%; height: 100%; }
        .wg-msg { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; color: #6b7280; }
        .wg-tip { position: absolute; pointer-events: none; background: rgba(17,24,39,0.92); color: #fff; border-radius: 5px;
                  padding: 4px 7px; white-space: pre; font-size: 12px; display: none; }
        .wg-foot { display: flex; gap: 12px; flex-wrap: wrap; padding: 2px 12px 7px; color: #4b5563; font-size: 11px; }
        .wg-foot svg { vertical-align: middle; margin-right: 3px; }
        @media (max-width: 600px) { .wg-panel { top: 50px; } .wg-body { height: 220px; } .wg-ranges { order: 5; flex-basis: 100%; } }`;

    function addStyle() {
        if (document.getElementById('wind-graph-css')) return;
        const st = document.createElement('style');
        st.id = 'wind-graph-css';
        st.textContent = css;
        document.head.appendChild(st);
    }

    // --- time zone helpers ---
    function tzOffset(t, tz) {
        const p = {};
        try {
            for (const x of new Intl.DateTimeFormat('en-US', { timeZone: tz, hourCycle: 'h23', year: 'numeric', month: 'numeric',
                    day: 'numeric', hour: 'numeric', minute: 'numeric', second: 'numeric' }).formatToParts(new Date(t * 1000)))
                p[x.type] = +x.value;
        } catch (e) {
            return -new Date(t * 1000).getTimezoneOffset() * 60;
        }
        return Date.UTC(p.year, p.month - 1, p.day, p.hour % 24, p.minute, p.second) / 1000 - Math.floor(t);
    }

    function dayRange(dateStr, tz) {
        const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(dateStr || '');
        if (!m) return null;
        const local = (y, mo, d) => {
            const g = Date.UTC(y, mo - 1, d) / 1000;
            return g - tzOffset(g - tzOffset(g, tz), tz);
        };
        return [local(+m[1], +m[2], +m[3]), local(+m[1], +m[2], +m[3] + 1) - 1];
    }

    // local 'YYYY-MM-DD' of unix time t in tz
    function localDate(t, tz) {
        try {
            return new Date(t * 1000).toLocaleDateString('en-CA', { timeZone: tz || undefined });
        } catch (e) {
            return new Date(t * 1000).toLocaleDateString('en-CA');
        }
    }

    const fileUrl = (base, date) => `${base}logs/${date.replace(/-/g, '_')}_wind.jsonl`;

    function parse(text, lo, hi) {
        const out = [];
        for (const l of text.split('\n')) {
            if (!l) continue;
            let o;
            try { o = JSON.parse(l); } catch (e) { continue; }
            if (!o || !num(o.t) || o.t < lo || o.t > hi) continue;
            const v = k => num(o[k]) ? o[k] : null;
            out.push({ t: o.t, s: num(o.s) && o.s >= 0 ? o.s : null, g: v('g'), d: v('d'), a: v('a') });
        }
        return out;
    }

    // finished days don't change: keep their text
    const doneDays = new Map();

    async function fetchDay(base, date, tz) {
        const url = fileUrl(base, date);
        if (doneDays.has(url)) return doneDays.get(url);
        const r = await fetch(url, { cache: 'no-cache' });
        if (r.status === 404) return '';
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const text = await r.text();
        const range = dayRange(date, tz);
        if (range && range[1] < Date.now() / 1000 - 600) doneDays.set(url, text);
        return text;
    }

    // the daily files covering lo..hi
    async function load(base, lo, hi, tz) {
        const dates = [];
        for (let d = localDate(lo, tz), last = localDate(hi, tz); dates.length < 32; ) {
            dates.push(d);
            if (d >= last) break;
            d = localDate(dayRange(d, tz)[1] + 1, tz);
        }
        const texts = await Promise.all(dates.map(d => fetchDay(base, d, tz)));
        const out = texts.flatMap(t => parse(t, lo, hi));
        out.sort((x, y) => x.t - y.t);
        return out.filter((p, i) => i === 0 || p.t !== out[i - 1].t);
    }

    // --- rendering ---
    function el(tag, attrs, parent) {
        const e = document.createElementNS(NS, tag);
        for (const k in attrs) e.setAttribute(k, attrs[k]);
        if (parent) parent.appendChild(e);
        return e;
    }

    const tz = () => (opts && opts.tz) || undefined;
    const multiDay = () => to - from > 20 * 3600;
    function fmtTime(t, secs) {
        const o = { timeZone: tz(), hour: '2-digit', minute: '2-digit', hourCycle: 'h23' };
        if (secs) o.second = '2-digit';
        if (multiDay()) { o.weekday = 'short'; o.day = 'numeric'; }
        try { return new Date(t * 1000).toLocaleString([], o); } catch (e) { delete o.timeZone; return new Date(t * 1000).toLocaleString([], o); }
    }

    function nearest(t) {
        let a = 0, b = data.length - 1;
        if (b < 0) return -1;
        while (b - a > 1) { const m = (a + b) >> 1; if (data[m].t < t) a = m; else b = m; }
        return Math.abs(data[a].t - t) <= Math.abs(data[b].t - t) ? a : b;
    }

    function geom() {
        const body = panel.querySelector('.wg-body');
        const W = body.clientWidth, H = body.clientHeight;
        const L = 34, R = 12, T = 14, B = 22;
        let ymax = 0;
        for (const p of data) ymax = Math.max(ymax, p.s ?? 0, p.g ?? 0);
        const step = ymax <= 12 ? 2 : ymax <= 30 ? 5 : ymax <= 60 ? 10 : 20;
        const top = Math.max(10, Math.ceil((ymax * 1.05 + 0.01) / step) * step);
        return {
            W, H, L, R, T, B, step, top,
            x: t => L + (t - from) / Math.max(1, to - from) * (W - L - R),
            y: v => T + (1 - v / top) * (H - T - B),
        };
    }

    function render() {
        if (!panel) return;
        const body = panel.querySelector('.wg-body');
        if (svg) svg.remove();
        svg = el('svg', {});
        body.insertBefore(svg, body.firstChild);
        const g = geom();
        svg.setAttribute('viewBox', `0 0 ${g.W} ${g.H}`);

        // grid + y axis
        for (let v = 0; v <= g.top + 1e-9; v += g.step) {
            el('line', { x1: g.L, x2: g.W - g.R, y1: g.y(v), y2: g.y(v), stroke: v ? '#e5e7eb' : '#9ca3af', 'stroke-width': 1 }, svg);
            el('text', { x: g.L - 5, y: g.y(v) + 4, 'text-anchor': 'end', 'font-size': 10, fill: '#6b7280' }, svg).textContent = String(v);
        }
        el('text', { x: 4, y: 10, 'font-size': 10, fill: '#6b7280' }, svg).textContent = 'kn';
        // x axis: ticks on whole local times
        const pxs = (g.W - g.L - g.R) / Math.max(1, to - from);
        const step = TICK_STEPS.find(s => s * pxs >= 70) || 86400;
        const off = tzOffset(from, tz());
        for (let t = Math.ceil((from + off) / step) * step - off; t <= to; t += step) {
            const x = g.x(t);
            el('line', { x1: x, x2: x, y1: g.T, y2: g.H - g.B, stroke: '#f1f5f9', 'stroke-width': 1 }, svg);
            el('line', { x1: x, x2: x, y1: g.H - g.B, y2: g.H - g.B + 4, stroke: '#9ca3af' }, svg);
            const o = { timeZone: tz(), hour: '2-digit', minute: '2-digit', hourCycle: 'h23' };
            if (step >= 86400) { delete o.hour; delete o.minute; o.weekday = 'short'; o.day = 'numeric'; }
            let label;
            try { label = new Date(t * 1000).toLocaleString([], o); } catch (e) { label = new Date(t * 1000).toLocaleTimeString(); }
            el('text', { x, y: g.H - 6, 'text-anchor': 'middle', 'font-size': 10, fill: '#6b7280' }, svg).textContent = label;
        }

        // segments split at gaps and missing speeds
        const segs = [];
        let cur = [];
        for (let i = 0; i < data.length; i++) {
            const p = data[i];
            if (p.s === null || (cur.length && p.t - cur[cur.length - 1].t > gapS)) { if (cur.length) segs.push(cur); cur = []; }
            if (p.s !== null) cur.push(p);
        }
        if (cur.length) segs.push(cur);
        const pt = (p, v) => `${g.x(p.t).toFixed(1)},${g.y(v).toFixed(1)}`;
        for (const s of segs) {
            const gust = s.map(p => pt(p, Math.max(p.s, p.g ?? p.s)));
            el('polygon', { points: gust.concat(s.slice().reverse().map(p => pt(p, p.s))).join(' '), fill: C_BAND }, svg);
            el('polyline', { points: gust.join(' '), fill: 'none', stroke: C_GUST, 'stroke-width': 1.2, 'stroke-linejoin': 'round' }, svg);
            if (s.length === 1) el('circle', { cx: g.x(s[0].t), cy: g.y(s[0].s), r: 2, fill: C_SPEED }, svg);
            else el('polyline', { points: s.map(p => pt(p, p.s)).join(' '), fill: 'none', stroke: C_SPEED, 'stroke-width': 2, 'stroke-linejoin': 'round' }, svg);
        }

        // direction arrows every 5 minutes (thinned when they would crowd), pointing downwind
        const astep = ARROW_STEPS.find(s => s * pxs >= 20) || 10800;
        const arrows = el('g', { class: 'wg-arrows' }, svg);
        for (let t = Math.ceil(from / astep) * astep; t <= to; t += astep) {
            const i = nearest(t);
            if (i < 0) break;
            const p = data[i];
            const dir = num(p.a) ? p.a : p.d;
            if (Math.abs(p.t - t) > 90 || p.s === null || !num(dir)) continue;
            const a = el('g', { transform: `translate(${g.x(p.t).toFixed(1)},${g.y(p.s).toFixed(1)}) rotate(${dir % 360}) scale(0.65)`,
                                'data-t': p.t, 'data-dir': dir }, arrows);
            el('path', { d: 'M0,15 L7,-1 L2,-1 L2,-15 L-2,-15 L-2,-1 L-7,-1 Z', fill: '#1e3a8a', stroke: '#fff', 'stroke-width': 1.5,
                         'paint-order': 'stroke' }, a);
        }

        cursorEl = el('line', { y1: g.T - 6, y2: g.H - g.B, stroke: C_CURSOR, 'stroke-width': 1.5, 'stroke-dasharray': '4 3', display: 'none',
                                class: 'wg-cursor' }, svg);
        const hl = el('line', { y1: g.T, y2: g.H - g.B, stroke: '#374151', 'stroke-width': 1, display: 'none' }, svg);
        const hd = el('circle', { r: 4, fill: C_SPEED, stroke: '#fff', 'stroke-width': 1.5, display: 'none' }, svg);
        hoverEls = { hl, hd };
        plot = el('rect', { x: g.L, y: 0, width: Math.max(0, g.W - g.L - g.R), height: g.H, fill: 'transparent' }, svg);
        plot.addEventListener('pointermove', hover);
        plot.addEventListener('pointerdown', hover);
        plot.addEventListener('pointerleave', e => { if (e.pointerType === 'mouse') hideHover(); });
        updateCursor();

        const msg = panel.querySelector('.wg-msg');
        msg.style.display = data.length ? 'none' : '';
        let n = 0, sum = 0, gmax = null;
        for (const p of data) {
            if (p.s !== null) { n++; sum += p.s; }
            if (p.g !== null) gmax = Math.max(gmax ?? 0, p.g);
        }
        panel.querySelector('.wg-stats').textContent = n
            ? `avg ${(sum / n).toFixed(1)} kn` + (gmax !== null ? `, max gust ${gmax.toFixed(1)} kn` : '') : '';
    }

    function hideHover() {
        if (!hoverEls) return;
        hoverEls.hl.setAttribute('display', 'none');
        hoverEls.hd.setAttribute('display', 'none');
        tip.style.display = 'none';
    }

    function hover(e) {
        const g = geom();
        const r = svg.getBoundingClientRect();
        const px = (e.clientX - r.left) * (g.W / Math.max(1, r.width));
        const t = from + (px - g.L) / Math.max(1, g.W - g.L - g.R) * (to - from);
        const i = nearest(t);
        const p = i >= 0 && Math.abs(data[i].t - t) <= gapS / 2 + 30 ? data[i] : null;
        const x = p ? g.x(p.t) : px;
        hoverEls.hl.setAttribute('x1', x);
        hoverEls.hl.setAttribute('x2', x);
        hoverEls.hl.setAttribute('display', '');
        const lines = [fmtTime(p ? p.t : t, !!p)];
        if (p && p.s !== null) {
            hoverEls.hd.setAttribute('cx', x);
            hoverEls.hd.setAttribute('cy', g.y(p.s));
            hoverEls.hd.setAttribute('display', '');
            lines.push(`Speed ${p.s.toFixed(1)} kn`);
        } else hoverEls.hd.setAttribute('display', 'none');
        if (!p) lines.push('No data');
        else {
            if (p.s === null) lines.push('Speed -');
            if (p.g !== null) lines.push(`Gust ${p.g.toFixed(1)} kn`);
            if (num(p.d)) lines.push(`From ${fmt3(p.d)}°T`);
            if (num(p.a)) lines.push(`10 min avg ${fmt3(p.a)}°T`);
        }
        tip.textContent = lines.join('\n');
        tip.style.display = 'block';
        const sx = x * r.width / g.W, tw = tip.offsetWidth;
        tip.style.left = `${sx + 12 + tw > r.width ? Math.max(0, sx - 12 - tw) : sx + 12}px`;
        tip.style.top = '8px';
    }

    function updateCursor() {
        if (!cursorEl || !opts) return;
        const t = typeof opts.cursor === 'function' ? opts.cursor() : null;
        if (!num(t) || t < from || t > to) { cursorEl.setAttribute('display', 'none'); return; }
        const x = geom().x(t);
        cursorEl.setAttribute('x1', x);
        cursorEl.setAttribute('x2', x);
        cursorEl.setAttribute('display', '');
    }

    async function reload() {
        if (!opts) return;
        if (opts.live) {
            to = Math.floor(Date.now() / 1000);
            from = to - opts.live * 3600;
        }
        const seq = ++loadSeq;
        const msg = panel.querySelector('.wg-msg');
        if (!data.length) { msg.textContent = 'Loading…'; msg.style.display = ''; }
        let d;
        try {
            d = await load(opts.base, from, to, tz());
        } catch (e) {
            if (seq !== loadSeq || !panel) return;
            msg.textContent = 'Wind history unavailable';
            msg.style.display = '';
            return;
        }
        if (seq !== loadSeq || !panel) return;
        data = d;
        const dts = [];
        for (let i = 1; i < data.length; i++) dts.push(data[i].t - data[i - 1].t);
        dts.sort((a, b) => a - b);
        gapS = Math.max(300, 3 * (dts.length ? dts[dts.length >> 1] : 60));
        msg.textContent = 'No wind data for this period';
        render();
    }

    function legend() {
        const sw = (inner) => `<svg width="22" height="12" viewBox="0 0 22 12">${inner}</svg>`;
        return `<span>${sw(`<line x1="1" y1="6" x2="21" y2="6" stroke="${C_SPEED}" stroke-width="2"/>`)}speed</span>` +
            `<span>${sw(`<rect x="1" y="2" width="20" height="8" fill="${C_BAND}" stroke="${C_GUST}"/>`)}gust</span>` +
            `<span>${sw(`<path transform="translate(11,6) rotate(-90) scale(0.38)" d="M0,15 L7,-1 L2,-1 L2,-15 L-2,-15 L-2,-1 L-7,-1 Z" fill="#1e3a8a"/>`)}downwind (10 min avg)</span>` +
            `<span class="wg-cursor-key" style="display:none">${sw(`<line x1="11" y1="0" x2="11" y2="12" stroke="${C_CURSOR}" stroke-width="1.5" stroke-dasharray="3 2"/>`)}playback</span>` +
            `<span class="wg-tz"></span>`;
    }

    function close() {
        clearInterval(refreshTimer);
        clearInterval(cursorTimer);
        if (ro) ro.disconnect();
        document.removeEventListener('keydown', onKey);
        if (panel) panel.remove();
        panel = svg = plot = tip = hoverEls = cursorEl = null;
        opts = null;
        data = [];
        loadSeq++;
    }

    function onKey(e) { if (e.key === 'Escape') close(); }

    function open(o) {
        close();
        addStyle();
        opts = Object.assign({}, o);
        if (opts.live) opts.live = +(localStorage.getItem('windGraphHours') || opts.live) || 3;
        else { from = Math.floor(opts.from); to = Math.ceil(opts.to); }
        panel = document.createElement('div');
        panel.className = 'wg-panel';
        panel.innerHTML = `<div class="wg-head"><span class="wg-title"></span><span class="wg-stats"></span>` +
            `<span class="wg-ranges"></span><button class="wg-close" title="Close" aria-label="Close">×</button></div>` +
            `<div class="wg-body"><div class="wg-msg"></div><div class="wg-tip"></div></div><div class="wg-foot">${legend()}</div>`;
        panel.querySelector('.wg-title').textContent = opts.title || 'Wind';
        let zone = tz();
        try { zone = zone || Intl.DateTimeFormat().resolvedOptions().timeZone; } catch (e) { /* */ }
        panel.querySelector('.wg-tz').textContent = `times ${zone || 'local'}`;
        if (typeof opts.cursor === 'function') panel.querySelector('.wg-cursor-key').style.display = '';
        panel.querySelector('.wg-close').addEventListener('click', close);
        tip = panel.querySelector('.wg-tip');
        if (opts.live) {
            const box = panel.querySelector('.wg-ranges');
            for (const h of LIVE_HOURS) {
                const b = document.createElement('button');
                b.textContent = `${h}h`;
                b.classList.toggle('on', h === opts.live);
                b.addEventListener('click', () => {
                    opts.live = h;
                    localStorage.setItem('windGraphHours', h);
                    box.querySelectorAll('button').forEach(x => x.classList.toggle('on', x === b));
                    reload();
                });
                box.appendChild(b);
            }
            refreshTimer = setInterval(reload, 60000);
        }
        document.body.appendChild(panel);
        document.addEventListener('keydown', onKey);
        if (window.ResizeObserver) {
            ro = new ResizeObserver(() => { if (svg) { hideHover(); render(); } });
            ro.observe(panel.querySelector('.wg-body'));
        }
        if (typeof opts.cursor === 'function') cursorTimer = setInterval(updateCursor, 250);
        reload();
    }

    function attach(itemId, base, o) {
        const item = document.getElementById(itemId);
        if (!item) return;
        o = o || {};
        const val = v => typeof v === 'function' ? v() : v;
        let key = null, seq = 0;
        const check = async force => {
            const tz = val(o.tz) || undefined;
            const date = val(o.date) || localDate(Date.now() / 1000, tz);
            if (!/^\d{4}-\d{2}-\d{2}$/.test(date) || (!force && date === key)) return;
            key = date;
            const my = ++seq;
            let ok = false;
            try {
                const r = await fetch(fileUrl(base, date), { headers: { Range: 'bytes=0-0' }, cache: 'no-store' });
                ok = r.ok;
            } catch (e) { /* hidden */ }
            if (my === seq) item.style.display = ok ? '' : 'none';
        };
        check(true);
        setInterval(check, 2000);                    // date / time zone changes
        setInterval(() => check(true), 300000);      // a file appearing during the day
    }

    return { open, close, attach, dayRange, isOpen: () => !!panel, _load: load, _localDate: localDate, _data: () => data, _range: () => [from, to] };
})();
