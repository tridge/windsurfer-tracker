/**
 * Export utilities for Windsurfer Tracker WebUI
 * Provides FIT file encoding, file download, and data conversion functions
 */

// ===== General Export Utilities =====

/**
 * Download content as a file
 * @param {string|Uint8Array} content - File content
 * @param {string} filename - Download filename
 * @param {string} mimeType - MIME type for the file
 */
function downloadFile(content, filename, mimeType) {
    const blob = new Blob([content], { type: mimeType });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
}

/**
 * Escape special XML characters
 * @param {string} str - Input string
 * @returns {string} XML-safe string
 */
function escapeXml(str) {
    return str.replace(/&/g, '&amp;')
              .replace(/</g, '&lt;')
              .replace(/>/g, '&gt;')
              .replace(/"/g, '&quot;')
              .replace(/'/g, '&apos;');
}

/**
 * Calculate distance between two points using haversine formula
 * @param {number} lat1 - Latitude of first point
 * @param {number} lon1 - Longitude of first point
 * @param {number} lat2 - Latitude of second point
 * @param {number} lon2 - Longitude of second point
 * @returns {number} Distance in meters
 */
function haversineDistance(lat1, lon1, lat2, lon2) {
    const R = 6371000; // Earth radius in meters
    const dLat = (lat2 - lat1) * Math.PI / 180;
    const dLon = (lon2 - lon1) * Math.PI / 180;
    const a = Math.sin(dLat/2) * Math.sin(dLat/2) +
              Math.cos(lat1 * Math.PI / 180) * Math.cos(lat2 * Math.PI / 180) *
              Math.sin(dLon/2) * Math.sin(dLon/2);
    const c = 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1-a));
    return R * c;
}

// ===== FIT File Encoder =====
// Minimal FIT encoder for activity files with GPS, speed, and heart rate

const FIT_EPOCH_OFFSET = 631065600; // Seconds from Unix epoch (1970) to FIT epoch (1989-12-31)

/**
 * Convert Unix timestamp to FIT timestamp
 * @param {number} unixSeconds - Unix timestamp in seconds
 * @returns {number} FIT timestamp
 */
function toFitTimestamp(unixSeconds) {
    return unixSeconds - FIT_EPOCH_OFFSET;
}

/**
 * Convert degrees to semicircles for FIT format
 * @param {number} degrees - Coordinate in degrees
 * @returns {number} Coordinate in semicircles (32-bit signed int)
 */
function toSemicircles(degrees) {
    return Math.round(degrees * (Math.pow(2, 31) / 180));
}

// CRC-16 lookup table for FIT files
const crcTable = new Uint16Array(256);
(function initCrcTable() {
    for (let i = 0; i < 256; i++) {
        let crc = i;
        for (let j = 0; j < 8; j++) {
            crc = (crc & 1) ? ((crc >> 1) ^ 0xA001) : (crc >> 1);
        }
        crcTable[i] = crc;
    }
})();

/**
 * Calculate CRC-16 for FIT file data
 * @param {Uint8Array} bytes - Data bytes
 * @param {number} start - Start index (default 0)
 * @param {number} end - End index (default bytes.length)
 * @returns {number} CRC-16 value
 */
function fitCrc(bytes, start = 0, end = bytes.length) {
    let crc = 0;
    for (let i = start; i < end; i++) {
        crc = ((crc >> 8) & 0xFF) ^ crcTable[(crc ^ bytes[i]) & 0xFF];
    }
    return crc;
}

// FIT Message Numbers
const FIT_MESG_FILE_ID = 0;
const FIT_MESG_SESSION = 18;
const FIT_MESG_LAP = 19;
const FIT_MESG_RECORD = 20;
const FIT_MESG_EVENT = 21;
const FIT_MESG_ACTIVITY = 34;

// FIT Base Types
const FIT_UINT8 = 0x02;
const FIT_UINT16 = 0x84;
const FIT_UINT32 = 0x86;
const FIT_SINT32 = 0x85;
const FIT_ENUM = 0x00;

/**
 * FIT file encoder class
 * Encodes GPS track data into Garmin FIT format
 */
class FitEncoder {
    constructor() {
        this.data = [];
        this.localMessageTypes = {};
        this.nextLocalType = 0;
    }

    writeByte(val) {
        this.data.push(val & 0xFF);
    }

    writeUint16(val) {
        this.data.push(val & 0xFF);
        this.data.push((val >> 8) & 0xFF);
    }

    writeUint32(val) {
        this.data.push(val & 0xFF);
        this.data.push((val >> 8) & 0xFF);
        this.data.push((val >> 16) & 0xFF);
        this.data.push((val >> 24) & 0xFF);
    }

    writeSint32(val) {
        // Handle signed 32-bit integers
        this.writeUint32(val >>> 0);
    }

    writeDefinition(localType, globalMesgNum, fields) {
        // Definition message header: bit 6 set for definition
        this.writeByte(0x40 | (localType & 0x0F));
        this.writeByte(0); // Reserved
        this.writeByte(0); // Architecture: 0 = little endian
        this.writeUint16(globalMesgNum);
        this.writeByte(fields.length);

        for (const field of fields) {
            this.writeByte(field.num);
            this.writeByte(field.size);
            this.writeByte(field.baseType);
        }

        this.localMessageTypes[globalMesgNum] = { localType, fields };
    }

    writeData(globalMesgNum, values) {
        const msgType = this.localMessageTypes[globalMesgNum];
        if (!msgType) throw new Error(`No definition for message ${globalMesgNum}`);

        // Data message header
        this.writeByte(msgType.localType & 0x0F);

        for (let i = 0; i < msgType.fields.length; i++) {
            const field = msgType.fields[i];
            const val = values[i];

            switch (field.baseType) {
                case 0x00: // enum (1 byte)
                case 0x02: // uint8
                    this.writeByte(val);
                    break;
                case 0x84: // uint16
                    this.writeUint16(val);
                    break;
                case 0x86: // uint32
                    this.writeUint32(val);
                    break;
                case 0x85: // sint32
                    this.writeSint32(val);
                    break;
                default:
                    // For unknown types, write bytes as-is
                    for (let j = 0; j < field.size; j++) {
                        this.writeByte((val >> (j * 8)) & 0xFF);
                    }
            }
        }
    }

    getFile() {
        const dataBytes = new Uint8Array(this.data);
        const dataSize = dataBytes.length;

        // Create header (14 bytes for FIT 2.0)
        const header = new Uint8Array(14);
        header[0] = 14; // Header size
        header[1] = 0x20; // Protocol version 2.0
        header[2] = 0x08; // Profile version LSB (2088 = 8.24)
        header[3] = 0x08; // Profile version MSB
        header[4] = dataSize & 0xFF;
        header[5] = (dataSize >> 8) & 0xFF;
        header[6] = (dataSize >> 16) & 0xFF;
        header[7] = (dataSize >> 24) & 0xFF;
        header[8] = 0x2E; // '.'
        header[9] = 0x46; // 'F'
        header[10] = 0x49; // 'I'
        header[11] = 0x54; // 'T'

        // Header CRC (bytes 0-11)
        const headerCrc = fitCrc(header, 0, 12);
        header[12] = headerCrc & 0xFF;
        header[13] = (headerCrc >> 8) & 0xFF;

        // Combine header + data
        const combined = new Uint8Array(header.length + dataBytes.length + 2);
        combined.set(header, 0);
        combined.set(dataBytes, header.length);

        // File CRC (over header + data)
        const fileCrc = fitCrc(combined, 0, header.length + dataBytes.length);
        combined[combined.length - 2] = fileCrc & 0xFF;
        combined[combined.length - 1] = (fileCrc >> 8) & 0xFF;

        return combined;
    }
}

// ===== Format Generation Functions =====
// These are pure functions that take track data and return formatted output

/**
 * Generate GPX format from track data
 * @param {Array} tracks - Array of {name, entries} where entries have {ts, lat, lon, spd, hdg}
 * @returns {string} GPX XML string
 */
function generateGPX(tracks) {
    const now = new Date().toISOString();
    let gpx = `<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="Windsurfer Tracker"
     xmlns="http://www.topografix.com/GPX/1/1"
     xmlns:gpxtpx="http://www.garmin.com/xmlschemas/TrackPointExtension/v2">
  <metadata>
    <name>Track Export</name>
    <time>${now}</time>
  </metadata>
`;

    for (const { name, entries } of tracks) {
        gpx += `  <trk>
    <name>${escapeXml(name)}</name>
    <trkseg>
`;
        for (const e of entries) {
            const time = new Date(e.ts * 1000).toISOString();
            const speedMs = (e.spd || 0) * 0.514444; // knots to m/s
            gpx += `      <trkpt lat="${e.lat.toFixed(7)}" lon="${e.lon.toFixed(7)}">
        <time>${time}</time>
        <extensions>
          <gpxtpx:TrackPointExtension>
            <gpxtpx:speed>${speedMs.toFixed(2)}</gpxtpx:speed>
            <gpxtpx:course>${e.hdg || 0}</gpxtpx:course>
          </gpxtpx:TrackPointExtension>
        </extensions>
      </trkpt>
`;
        }
        gpx += `    </trkseg>
  </trk>
`;
    }

    gpx += `</gpx>`;
    return gpx;
}

/**
 * Generate CSV format from track data
 * @param {Array} tracks - Array of {userId, name, entries} where entries have {ts, lat, lon, spd, hdg, bat, sig, hac, hr}
 * @returns {string} CSV string
 */
function generateCSV(tracks) {
    let csv = 'timestamp,user_id,user_name,latitude,longitude,speed_kn,speed_ms,heading,battery,signal,accuracy,heart_rate\n';

    for (const { userId, name, entries } of tracks) {
        for (const e of entries) {
            const time = new Date(e.ts * 1000).toISOString();
            const speedMs = (e.spd || 0) * 0.514444;
            csv += `${time},${userId},"${name}",${e.lat.toFixed(7)},${e.lon.toFixed(7)},`;
            csv += `${(e.spd || 0).toFixed(2)},${speedMs.toFixed(2)},${e.hdg || 0},`;
            csv += `${e.bat !== undefined ? e.bat : ''},${e.sig !== undefined ? e.sig : ''},`;
            csv += `${e.hac !== undefined ? e.hac.toFixed(1) : ''},${e.hr !== undefined ? e.hr : ''}\n`;
        }
    }

    return csv;
}

/**
 * Generate GeoJSON format from track data
 * @param {Array} tracks - Array of {userId, name, entries} where entries have {ts, lat, lon, spd}
 * @returns {object} GeoJSON FeatureCollection object
 */
function generateGeoJSON(tracks) {
    const features = [];

    for (const { userId, name, entries } of tracks) {
        const coordinates = entries.map(e => [e.lon, e.lat]); // GeoJSON uses [lon, lat]
        const timestamps = entries.map(e => new Date(e.ts * 1000).toISOString());
        const speeds = entries.map(e => e.spd || 0);

        features.push({
            type: 'Feature',
            properties: {
                user_id: userId,
                user_name: name,
                start_time: timestamps[0],
                end_time: timestamps[timestamps.length - 1],
                point_count: entries.length,
                avg_speed_kn: (speeds.reduce((a, b) => a + b, 0) / speeds.length).toFixed(2),
                max_speed_kn: Math.max(...speeds).toFixed(2)
            },
            geometry: {
                type: 'LineString',
                coordinates: coordinates
            }
        });
    }

    return {
        type: 'FeatureCollection',
        features: features
    };
}

/**
 * Generate FIT format from single track data
 * @param {Array} entries - Array of track points with {ts, lat, lon, spd, hr}
 * @returns {Uint8Array} FIT file binary data
 */
function generateFIT(entries) {
    if (!entries || entries.length === 0) return null;

    const encoder = new FitEncoder();

    // Get timestamps
    const startTime = entries[0].ts;
    const endTime = entries[entries.length - 1].ts;
    const startFitTime = toFitTimestamp(startTime);

    // Define file_id message
    encoder.writeDefinition(0, FIT_MESG_FILE_ID, [
        { num: 0, size: 1, baseType: FIT_ENUM },    // type
        { num: 1, size: 2, baseType: FIT_UINT16 },  // manufacturer
        { num: 2, size: 2, baseType: FIT_UINT16 },  // product
        { num: 3, size: 4, baseType: FIT_UINT32 },  // serial_number
        { num: 4, size: 4, baseType: FIT_UINT32 },  // time_created
    ]);

    // Write file_id: type=4 (activity), manufacturer=255 (development), product=1, serial=12345
    encoder.writeData(FIT_MESG_FILE_ID, [
        4,                  // type: activity
        255,                // manufacturer: development
        1,                  // product
        12345,              // serial_number
        startFitTime,       // time_created
    ]);

    // Define event message for start
    encoder.writeDefinition(1, FIT_MESG_EVENT, [
        { num: 0, size: 1, baseType: FIT_ENUM },     // event
        { num: 1, size: 1, baseType: FIT_ENUM },     // event_type
        { num: 253, size: 4, baseType: FIT_UINT32 }, // timestamp
    ]);

    // Write start event
    encoder.writeData(FIT_MESG_EVENT, [
        0,                  // event: timer
        0,                  // event_type: start
        startFitTime,       // timestamp
    ]);

    // Check if we have heart rate data
    const hasHeartRate = entries.some(e => e.hr !== undefined && e.hr > 0);

    // Pre-calculate cumulative distances
    const distances = [0];
    let totalDistance = 0;
    for (let i = 1; i < entries.length; i++) {
        const dist = haversineDistance(
            entries[i-1].lat, entries[i-1].lon,
            entries[i].lat, entries[i].lon
        );
        totalDistance += dist;
        distances.push(totalDistance);
    }

    // Define record message - fields MUST be in ascending order by field number
    const recordFields = [
        { num: 0, size: 4, baseType: FIT_SINT32 },   // position_lat (semicircles)
        { num: 1, size: 4, baseType: FIT_SINT32 },   // position_long (semicircles)
    ];

    if (hasHeartRate) {
        recordFields.push({ num: 3, size: 1, baseType: FIT_UINT8 }); // heart_rate
    }

    recordFields.push({ num: 5, size: 4, baseType: FIT_UINT32 });   // distance (m * 100)
    recordFields.push({ num: 6, size: 2, baseType: FIT_UINT16 });   // speed (m/s * 1000)
    recordFields.push({ num: 253, size: 4, baseType: FIT_UINT32 }); // timestamp

    encoder.writeDefinition(2, FIT_MESG_RECORD, recordFields);

    // Write record messages
    for (let i = 0; i < entries.length; i++) {
        const e = entries[i];
        const fitTs = toFitTimestamp(e.ts);
        const lat = toSemicircles(e.lat);
        const lon = toSemicircles(e.lon);
        const speedMs = (e.spd || 0) * 0.514444; // knots to m/s
        const speedScaled = Math.round(speedMs * 1000); // FIT uses mm/s
        const distScaled = Math.round(distances[i] * 100); // FIT uses centimeters

        const recordValues = [lat, lon];

        if (hasHeartRate) {
            recordValues.push(e.hr || 0xFF); // 0xFF = invalid
        }

        recordValues.push(distScaled);              // distance (cumulative, cm)
        recordValues.push(speedScaled & 0xFFFF);    // speed (16-bit, capped)
        recordValues.push(fitTs);                   // timestamp

        encoder.writeData(FIT_MESG_RECORD, recordValues);
    }

    // Total distance for session/lap (in centimeters for FIT)
    const totalDistanceScaled = Math.round(totalDistance * 100);

    // Write stop event
    const endFitTime = toFitTimestamp(endTime);
    encoder.writeData(FIT_MESG_EVENT, [
        0,                  // event: timer
        4,                  // event_type: stop_all
        endFitTime,         // timestamp
    ]);

    // Calculate summary stats
    const totalElapsedTime = (endTime - startTime) * 1000; // milliseconds
    const totalTimerTime = totalElapsedTime;

    // Define and write lap message
    encoder.writeDefinition(3, FIT_MESG_LAP, [
        { num: 0, size: 1, baseType: FIT_ENUM },     // event
        { num: 1, size: 1, baseType: FIT_ENUM },     // event_type
        { num: 2, size: 4, baseType: FIT_UINT32 },   // start_time
        { num: 7, size: 4, baseType: FIT_UINT32 },   // total_elapsed_time
        { num: 8, size: 4, baseType: FIT_UINT32 },   // total_timer_time
        { num: 9, size: 4, baseType: FIT_UINT32 },   // total_distance (m * 100)
        { num: 253, size: 4, baseType: FIT_UINT32 }, // timestamp
        { num: 254, size: 2, baseType: FIT_UINT16 }, // message_index
    ]);

    encoder.writeData(FIT_MESG_LAP, [
        9,                  // event: lap
        1,                  // event_type: stop
        startFitTime,       // start_time
        totalElapsedTime,   // total_elapsed_time
        totalTimerTime,     // total_timer_time
        totalDistanceScaled, // total_distance
        endFitTime,         // timestamp
        0,                  // message_index
    ]);

    // Define and write session message
    encoder.writeDefinition(4, FIT_MESG_SESSION, [
        { num: 0, size: 1, baseType: FIT_ENUM },     // event
        { num: 1, size: 1, baseType: FIT_ENUM },     // event_type
        { num: 2, size: 4, baseType: FIT_UINT32 },   // start_time
        { num: 5, size: 1, baseType: FIT_ENUM },     // sport
        { num: 6, size: 1, baseType: FIT_ENUM },     // sub_sport
        { num: 7, size: 4, baseType: FIT_UINT32 },   // total_elapsed_time
        { num: 8, size: 4, baseType: FIT_UINT32 },   // total_timer_time
        { num: 9, size: 4, baseType: FIT_UINT32 },   // total_distance (m * 100)
        { num: 25, size: 2, baseType: FIT_UINT16 },  // first_lap_index
        { num: 26, size: 2, baseType: FIT_UINT16 },  // num_laps
        { num: 253, size: 4, baseType: FIT_UINT32 }, // timestamp
        { num: 254, size: 2, baseType: FIT_UINT16 }, // message_index
    ]);

    // sport=43 (windsurfing), sub_sport=0 (generic)
    encoder.writeData(FIT_MESG_SESSION, [
        8,                  // event: session
        1,                  // event_type: stop
        startFitTime,       // start_time
        43,                 // sport: windsurfing
        0,                  // sub_sport: generic
        totalElapsedTime,   // total_elapsed_time
        totalTimerTime,     // total_timer_time
        totalDistanceScaled, // total_distance
        0,                  // first_lap_index
        1,                  // num_laps
        endFitTime,         // timestamp
        0,                  // message_index
    ]);

    // Define and write activity message
    encoder.writeDefinition(5, FIT_MESG_ACTIVITY, [
        { num: 0, size: 4, baseType: FIT_UINT32 },   // total_timer_time
        { num: 1, size: 2, baseType: FIT_UINT16 },   // num_sessions
        { num: 2, size: 1, baseType: FIT_ENUM },     // type
        { num: 3, size: 1, baseType: FIT_ENUM },     // event
        { num: 4, size: 1, baseType: FIT_ENUM },     // event_type
        { num: 5, size: 4, baseType: FIT_UINT32 },   // local_timestamp
        { num: 253, size: 4, baseType: FIT_UINT32 }, // timestamp
    ]);

    encoder.writeData(FIT_MESG_ACTIVITY, [
        totalTimerTime,     // total_timer_time
        1,                  // num_sessions
        0,                  // type: generic
        26,                 // event: activity
        1,                  // event_type: stop
        endFitTime,         // local_timestamp
        endFitTime,         // timestamp
    ]);

    return encoder.getFile();
}
