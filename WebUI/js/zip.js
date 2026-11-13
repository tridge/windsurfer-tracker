/**
 * Zip archive helpers for Windsurfer Tracker WebUI: createZip (stored entries)
 * and readZip (stored or deflated entries)
 */


const CRC32_TABLE = (() => {
    const t = new Uint32Array(256);
    for (let n = 0; n < 256; n++) {
        let c = n;
        for (let k = 0; k < 8; k++) c = (c & 1) ? (0xEDB88320 ^ (c >>> 1)) : (c >>> 1);
        t[n] = c >>> 0;
    }
    return t;
})();

function crc32(bytes) {
    let c = 0xFFFFFFFF;
    for (let i = 0; i < bytes.length; i++) {
        c = CRC32_TABLE[(c ^ bytes[i]) & 0xFF] ^ (c >>> 8);
    }
    return (c ^ 0xFFFFFFFF) >>> 0;
}

// DOS date/time fields for zip headers
function zipDosDateTime(d) {
    const time = (d.getHours() << 11) | (d.getMinutes() << 5) | (d.getSeconds() >> 1);
    const date = ((d.getFullYear() - 1980) << 9) | ((d.getMonth() + 1) << 5) | d.getDate();
    return { time, date };
}

/**
 * Build a zip archive with stored (uncompressed) entries
 * @param {Array<{name: string, data: Uint8Array|string}>} files
 * @returns {Uint8Array} zip file bytes
 */
function createZip(files) {
    const enc = new TextEncoder();
    const { time, date } = zipDosDateTime(new Date());
    const locals = [];
    const centrals = [];
    let offset = 0;

    for (const f of files) {
        const data = (typeof f.data === 'string') ? enc.encode(f.data) : f.data;
        const name = enc.encode(f.name);
        const crc = crc32(data);

        // local file header (30 bytes) + name + data
        const lh = new DataView(new ArrayBuffer(30));
        lh.setUint32(0, 0x04034b50, true);
        lh.setUint16(4, 20, true);          // version needed
        lh.setUint16(6, 0x0800, true);      // flags: UTF-8 names
        lh.setUint16(8, 0, true);           // method: stored
        lh.setUint16(10, time, true);
        lh.setUint16(12, date, true);
        lh.setUint32(14, crc, true);
        lh.setUint32(18, data.length, true);
        lh.setUint32(22, data.length, true);
        lh.setUint16(26, name.length, true);
        lh.setUint16(28, 0, true);          // extra length
        locals.push(new Uint8Array(lh.buffer), name, data);

        // central directory header (46 bytes) + name
        const ch = new DataView(new ArrayBuffer(46));
        ch.setUint32(0, 0x02014b50, true);
        ch.setUint16(4, 20, true);          // version made by
        ch.setUint16(6, 20, true);          // version needed
        ch.setUint16(8, 0x0800, true);
        ch.setUint16(10, 0, true);
        ch.setUint16(12, time, true);
        ch.setUint16(14, date, true);
        ch.setUint32(16, crc, true);
        ch.setUint32(20, data.length, true);
        ch.setUint32(24, data.length, true);
        ch.setUint16(28, name.length, true);
        ch.setUint16(30, 0, true);          // extra length
        ch.setUint16(32, 0, true);          // comment length
        ch.setUint16(34, 0, true);          // disk number
        ch.setUint16(36, 0, true);          // internal attrs
        ch.setUint32(38, 0, true);          // external attrs
        ch.setUint32(42, offset, true);     // local header offset
        centrals.push(new Uint8Array(ch.buffer), name);

        offset += 30 + name.length + data.length;
    }

    const cdSize = centrals.reduce((n, a) => n + a.length, 0);
    const eocd = new DataView(new ArrayBuffer(22));
    eocd.setUint32(0, 0x06054b50, true);
    eocd.setUint16(4, 0, true);
    eocd.setUint16(6, 0, true);
    eocd.setUint16(8, files.length, true);
    eocd.setUint16(10, files.length, true);
    eocd.setUint32(12, cdSize, true);
    eocd.setUint32(16, offset, true);
    eocd.setUint16(20, 0, true);

    const parts = [...locals, ...centrals, new Uint8Array(eocd.buffer)];
    const out = new Uint8Array(parts.reduce((n, a) => n + a.length, 0));
    let pos = 0;
    for (const a of parts) { out.set(a, pos); pos += a.length; }
    return out;
}

/**
 * Read a zip archive (stored or deflated entries; deflate needs DecompressionStream)
 * @param {ArrayBuffer|Uint8Array} buf - zip file bytes
 * @returns {Promise<Array<{name: string, data: Uint8Array}>>} file entries (directories skipped)
 */
async function readZip(buf) {
    const bytes = buf instanceof Uint8Array ? buf : new Uint8Array(buf);
    const dv = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    let eocd = -1;
    for (let i = bytes.length - 22; i >= Math.max(0, bytes.length - 22 - 65535); i--) {
        if (dv.getUint32(i, true) === 0x06054b50) { eocd = i; break; }
    }
    if (eocd < 0) throw new Error('not a zip file');
    const count = dv.getUint16(eocd + 10, true);
    let p = dv.getUint32(eocd + 16, true);
    const dec = new TextDecoder();
    const out = [];
    for (let k = 0; k < count; k++) {
        if (dv.getUint32(p, true) !== 0x02014b50) throw new Error('bad zip central directory');
        const method = dv.getUint16(p + 10, true);
        const csize = dv.getUint32(p + 20, true);
        const nlen = dv.getUint16(p + 28, true), xlen = dv.getUint16(p + 30, true), clen = dv.getUint16(p + 32, true);
        const lho = dv.getUint32(p + 42, true);
        const name = dec.decode(bytes.subarray(p + 46, p + 46 + nlen));
        p += 46 + nlen + xlen + clen;
        if (name.endsWith('/')) continue;
        const start = lho + 30 + dv.getUint16(lho + 26, true) + dv.getUint16(lho + 28, true);
        const raw = bytes.subarray(start, start + csize);
        let data;
        if (method === 0) {
            data = raw.slice();
        } else if (method === 8) {
            const stream = new Blob([raw]).stream().pipeThrough(new DecompressionStream('deflate-raw'));
            data = new Uint8Array(await new Response(stream).arrayBuffer());
        } else {
            throw new Error(`${name}: unsupported zip compression (${method})`);
        }
        out.push({ name, data });
    }
    return out;
}
