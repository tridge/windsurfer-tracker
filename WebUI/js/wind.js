// Live wind from the event's wind.json (written by a station script, e.g.
// scripts/parkdale_wind.py), shown under the map's zoom buttons: an arrow pointing
// downwind, speed and gust, and the direction the wind comes from as a true bearing
// (the tooltip adds magnetic via wmmDeclination() from wmm.js when loaded).
// Hidden if the event has no wind.json.
//
// WindDisplay.init(map, { url, storageKey, menuItem, menuCheck, position, aboveScale, onUpdate })
//   menuItem:   id of a menu entry that toggles the display (shown once wind data exists)
//   menuCheck:  optional id of an element that shows ✓ when on
//   position:   Leaflet control corner (default 'topleft', under the zoom buttons)
//   aboveScale: id of a scale element at the bottom left: keep the box just above it
//   avoid:      ids of other boxes to stay clear of (moves up above any visible one it would overlap)
//   onUpdate:   called after each poll/age refresh (e.g. to show controls that use current())
// WindDisplay.current(): { dir, speed_kn, age } while the reading is under 10 minutes old, else null.
//   dir is where the wind comes from, °T: the 10 minute average when the station gives one.
const WindDisplay = (() => {
    const POLL_MS = 60000;
    const OLD_S = 600;            // older than this: shown as "N minutes old", faded, not used
    const AHEAD_S = 120;          // an observation time further ahead than this is treated as unknown
    let control = null, el = null, opts = {}, data = null, shown = true, link = null, map = null;

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
        // age of the observation; without one, of the fetch (shown as such); else unknown.
        // A time in the future (station clock or units wrong) is as good as none.
        const sane = t => num(t) && t - Date.now() / 1000 <= AHEAD_S;
        const obsOk = sane(d.obs_ts);
        const when = obsOk ? d.obs_ts : sane(d.fetched) ? d.fetched : null;
        const age = when === null ? Infinity : Math.max(0, Date.now() / 1000 - when);
        const compass = typeof d.dir_compass === 'string' ? d.dir_compass.replace(/[^A-Z]/g, '').slice(0, 3) : '';
        // dir_deg is where the wind comes from; the arrow (head down at 0°) points downwind
        el.innerHTML =
            `<svg width="40" height="40" viewBox="-20 -20 40 40" style="transform:rotate(${d.dir_deg % 360}deg)">` +
            `<circle r="18" fill="none" stroke="#9ca3af" stroke-width="1"/>` +
            `<path d="M0,15 L7,-1 L2,-1 L2,-15 L-2,-15 L-2,-1 L-7,-1 Z" fill="#2563eb" stroke="#1e3a8a" stroke-width="1"/></svg>` +
            `<div class="wind-speed">${d.speed_kn.toFixed(1)} kn</div>` +
            (num(d.gust_kn) ? `<div class="wind-sub">gust ${d.gust_kn.toFixed(1)}</div>` : '') +
            `<div class="wind-sub">${compass} ${bearing}</div>` +
            (when === null ? `<div class="wind-age">time unknown</div>`
                : !obsOk ? `<div class="wind-age">obs time unknown</div>`
                : age > OLD_S ? `<div class="wind-age">${Math.round(age / 60)} minutes old</div>` : '');
        el.classList.toggle('wind-old', age > OLD_S);
        const lines = [];
        if (st.name) lines.push(String(st.name));
        lines.push(`From ${fmt3(d.dir_deg)}°T` + (decl === null ? '' : ` / ${fmt3(d.dir_deg - decl)}°M`));
        if (num(d.dir_avg10m_deg)) lines.push(`10 min average ${fmt3(d.dir_avg10m_deg)}°T` + (decl === null ? '' : ` / ${fmt3(d.dir_avg10m_deg - decl)}°M`));
        if (num(d.max_daily_speed_kn)) lines.push(`Today's max ${d.max_daily_speed_kn.toFixed(1)} kn, gust ${num(d.max_daily_gust_kn) ? d.max_daily_gust_kn.toFixed(1) : '-'}`);
        if (typeof d.obs_local === 'string') lines.push(`Observed ${d.obs_local.replace('T', ' ').slice(0, 16)}`);
        // url: the station's full weather page, opened by clicking (http(s) only)
        link = typeof d.url === 'string' && /^https?:\/\//i.test(d.url) ? d.url : null;
        if (link) lines.push('Click for the full weather page');
        el.style.cursor = link ? 'pointer' : '';
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

    function current() {
        const d = data;
        // only a reading whose observation time is known and recent
        if (!d || !num(d.dir_deg) || !num(d.speed_kn) || !num(d.obs_ts)) return null;
        const age = Date.now() / 1000 - d.obs_ts;
        if (!(age >= -AHEAD_S && age <= OLD_S)) return null;
        return { dir: num(d.dir_avg10m_deg) ? d.dir_avg10m_deg : d.dir_deg, avg: num(d.dir_avg10m_deg),
                 speed_kn: d.speed_kn, age: Math.max(0, age) };
    }

    // bottom left, just above the scale; moved right if it would reach up into the top-left controls
    function placeAboveScale() {
        const scale = document.getElementById(opts.aboveScale), wind = control && control.getContainer();
        if (!scale || !wind || !map) return;
        const mapRect = map.getContainer().getBoundingClientRect();
        wind.style.marginBottom = `${Math.max(10, Math.round(mapRect.bottom - scale.getBoundingClientRect().top + 6))}px`;
        wind.style.marginLeft = '';
        if (wind.offsetParent === null) return;
        // clear of other boxes at the bottom (e.g. the course info box on a phone): sit just above
        let w = wind.getBoundingClientRect();
        for (const id of opts.avoid || []) {
            const a = document.getElementById(id);
            if (!a || a.offsetParent === null) continue;
            const r = a.getBoundingClientRect();
            if (r.width && r.height && r.left < w.right && r.right > w.left && r.top < w.bottom && r.bottom > w.top) {
                wind.style.marginBottom = `${Math.round(mapRect.bottom - r.top + 6)}px`;
                w = wind.getBoundingClientRect();
            }
        }
        const topLeft = map.getContainer().querySelector('.leaflet-top.leaflet-left');
        if (!topLeft) return;
        const tl = topLeft.getBoundingClientRect();
        if (tl.height > 0 && w.top < tl.bottom + 4) wind.style.marginLeft = `${Math.round(tl.right - mapRect.left + 10)}px`;
    }

    function updated() {
        if (opts.aboveScale) placeAboveScale();
        if (typeof opts.onUpdate === 'function') opts.onUpdate();
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
        updated();
    }

    function init(m, o) {
        map = m;
        opts = o || {};
        addStyle();
        shown = localStorage.getItem(opts.storageKey) !== 'false';
        const Ctl = L.Control.extend({
            onAdd() {
                el = L.DomUtil.create('div', 'wind-box leaflet-control');
                L.DomEvent.disableClickPropagation(el);
                el.addEventListener('click', () => { if (link) window.open(link, '_blank', 'noopener'); });
                return el;
            }
        });
        control = new Ctl({ position: opts.position || 'topleft' }).addTo(map);
        control.getContainer().style.display = 'none';
        if (opts.aboveScale) {
            if (opts.position === undefined) control.setPosition('bottomleft');
            window.addEventListener('resize', placeAboveScale);
            if (window.ResizeObserver) {
                const ro = new ResizeObserver(() => placeAboveScale());
                [document.getElementById(opts.aboveScale), control.getContainer(),
                 map.getContainer().querySelector('.leaflet-top.leaflet-left'),
                 ...(opts.avoid || []).map(id => document.getElementById(id))].forEach(e => e && ro.observe(e));
            }
        }
        poll();
        setInterval(poll, POLL_MS);
        setInterval(() => { render(); updated(); }, 30000);  // keep the age current between polls
    }

    function toggle() {
        shown = !shown;
        if (opts.storageKey) localStorage.setItem(opts.storageKey, shown);
        render();
        updateMenu();
        updated();
    }

    return { init, toggle, current, place: placeAboveScale, isShown: () => shown, container: () => control && control.getContainer() };
})();
