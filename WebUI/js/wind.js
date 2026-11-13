// Live wind from the event's wind.json (written by a station script, e.g.
// scripts/parkdale_wind.py), shown under the map's zoom buttons: an arrow pointing
// downwind, speed and gust, and the direction the wind comes from as a true bearing
// (the tooltip adds magnetic via wmmDeclination() from wmm.js when loaded).
// Hidden if the event has no wind.json.
//
// WindDisplay.init(map, { url, storageKey, menuItem, menuCheck })
//   menuItem:  id of a menu entry that toggles the display (shown once wind data exists)
//   menuCheck: optional id of an element that shows ✓ when on
const WindDisplay = (() => {
    const POLL_MS = 60000;
    const STALE_S = 300, OLD_S = 900;
    let control = null, el = null, opts = {}, data = null, shown = true;

    const css = `
        .wind-box { background: rgba(255,255,255,0.92); border-radius: 6px; box-shadow: 0 1px 5px rgba(0,0,0,0.4);
                    padding: 4px 6px 5px; text-align: center; font: 600 12px/1.25 system-ui, sans-serif; color: #1f2937;
                    min-width: 64px; }
        .wind-box svg { display: block; margin: 0 auto 2px; }
        .wind-box .wind-speed { font-size: 14px; }
        .wind-box .wind-sub { font-weight: 500; font-size: 11px; color: #374151; }
        .wind-box.wind-old { opacity: 0.55; }
        .wind-box .wind-age { font-weight: 500; font-size: 10px; color: #b45309; }`;

    function addStyle() {
        if (document.getElementById('wind-display-css')) return;
        const st = document.createElement('style');
        st.id = 'wind-display-css';
        st.textContent = css;
        document.head.appendChild(st);
    }

    const fmt3 = v => String(Math.round(((v % 360) + 360) % 360) % 360).padStart(3, '0');
    const num = v => typeof v === 'number' && Number.isFinite(v);

    function render() {
        if (!el) return;
        const d = data;
        const ok = d && num(d.dir_deg) && num(d.speed_kn);
        if (control) control.getContainer().style.display = ok && shown ? '' : 'none';
        if (!ok) return;
        const st = d.station || {};
        const decl = typeof wmmDeclination === 'function' && num(st.lat) && num(st.lon) ? wmmDeclination(st.lat, st.lon) : null;
        const bearing = `${fmt3(d.dir_deg)}°T`;
        const age = num(d.obs_ts) ? Math.max(0, Date.now() / 1000 - d.obs_ts) : 0;
        const compass = typeof d.dir_compass === 'string' ? d.dir_compass.replace(/[^A-Z]/g, '').slice(0, 3) : '';
        // dir_deg is where the wind comes from; the arrow (head down at 0°) points downwind
        el.innerHTML =
            `<svg width="40" height="40" viewBox="-20 -20 40 40" style="transform:rotate(${d.dir_deg % 360}deg)">` +
            `<circle r="18" fill="none" stroke="#9ca3af" stroke-width="1"/>` +
            `<path d="M0,15 L7,-1 L2,-1 L2,-15 L-2,-15 L-2,-1 L-7,-1 Z" fill="#2563eb" stroke="#1e3a8a" stroke-width="1"/></svg>` +
            `<div class="wind-speed">${d.speed_kn.toFixed(1)} kn</div>` +
            (num(d.gust_kn) ? `<div class="wind-sub">gust ${d.gust_kn.toFixed(1)}</div>` : '') +
            `<div class="wind-sub">${compass} ${bearing}</div>` +
            (age > STALE_S ? `<div class="wind-age">${Math.round(age / 60)} min old</div>` : '');
        el.classList.toggle('wind-old', age > OLD_S);
        const lines = [];
        if (st.name) lines.push(String(st.name));
        lines.push(`From ${fmt3(d.dir_deg)}°T` + (decl === null ? '' : ` / ${fmt3(d.dir_deg - decl)}°M`));
        if (num(d.dir_avg10m_deg)) lines.push(`10 min average ${fmt3(d.dir_avg10m_deg)}°T` + (decl === null ? '' : ` / ${fmt3(d.dir_avg10m_deg - decl)}°M`));
        if (num(d.max_daily_speed_kn)) lines.push(`Today's max ${d.max_daily_speed_kn.toFixed(1)} kn, gust ${num(d.max_daily_gust_kn) ? d.max_daily_gust_kn.toFixed(1) : '-'}`);
        if (typeof d.obs_local === 'string') lines.push(`Observed ${d.obs_local.replace('T', ' ').slice(0, 16)}`);
        el.title = lines.join('\n');
    }

    function updateMenu() {
        const item = opts.menuItem && document.getElementById(opts.menuItem);
        if (item) {
            item.style.display = data ? '' : 'none';
            item.classList.toggle('active', shown);
        }
        const chk = opts.menuCheck && document.getElementById(opts.menuCheck);
        if (chk) chk.textContent = shown ? '✓' : '';
    }

    async function poll() {
        try {
            const r = await fetch(opts.url, { cache: 'no-cache' });
            data = r.ok ? await r.json() : null;
            if (data && (typeof data !== 'object' || !num(data.dir_deg))) data = null;
        } catch (e) {
            // keep the last reading on a transient error; its age shows it's getting old
        }
        render();
        updateMenu();
    }

    function init(map, o) {
        opts = o || {};
        addStyle();
        shown = localStorage.getItem(opts.storageKey) !== 'false';
        const Ctl = L.Control.extend({
            onAdd() {
                el = L.DomUtil.create('div', 'wind-box leaflet-control');
                L.DomEvent.disableClickPropagation(el);
                return el;
            }
        });
        control = new Ctl({ position: 'topleft' }).addTo(map);
        control.getContainer().style.display = 'none';
        poll();
        setInterval(poll, POLL_MS);
        setInterval(render, 30000);  // keep the age current between polls
    }

    function toggle() {
        shown = !shown;
        if (opts.storageKey) localStorage.setItem(opts.storageKey, shown);
        render();
        updateMenu();
    }

    return { init, toggle, isShown: () => shown };
})();
