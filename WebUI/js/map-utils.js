/**
 * Map utility functions for Windsurfer Tracker WebUI
 * Requires: Leaflet, global 'map' variable
 */

/**
 * Update the custom scale bar based on current map zoom and position
 * Requires elements: #scaleBar (width set), #scaleText (text content set)
 * @param {L.Map} map - Leaflet map instance
 */
function updateScale(map) {
    const center = map.getCenter();
    const zoom = map.getZoom();

    // Calculate meters per pixel at current zoom and latitude
    // At zoom 0, the world is 256 pixels wide = 40075km at equator
    const metersPerPixel = 40075016.686 * Math.cos(center.lat * Math.PI / 180) / Math.pow(2, zoom + 8);

    // Find a nice round distance that fits in ~80-200 pixels
    const distances = [10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000, 50000];
    let bestDist = distances[0];
    let bestWidth = bestDist / metersPerPixel;

    for (const dist of distances) {
        const width = dist / metersPerPixel;
        if (width >= 80 && width <= 200) {
            bestDist = dist;
            bestWidth = width;
            break;
        }
        if (width < 200) {
            bestDist = dist;
            bestWidth = width;
        }
    }

    // Update the scale bar
    document.getElementById('scaleBar').style.width = bestWidth + 'px';

    // Format the distance text
    let text;
    if (bestDist >= 1000) {
        text = (bestDist / 1000) + ' km';
    } else {
        text = bestDist + ' m';
    }
    document.getElementById('scaleText').textContent = text;
}
