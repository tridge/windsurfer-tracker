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
