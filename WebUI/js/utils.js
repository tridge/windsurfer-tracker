/**
 * Common utility functions for Windsurfer Tracker WebUI
 */

/**
 * Escape HTML special characters to prevent XSS
 * @param {string} str - Input string
 * @returns {string} HTML-escaped string
 */
function escapeHtml(str) {
    if (str === undefined || str === null || str === '') return '';
    // quotes too, so the result is safe inside attribute values as well as text
    return String(str).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

/**
 * Convert URLs in text to clickable links
 * @param {string} text - Input text
 * @returns {string} Text with URLs converted to anchor tags
 */
function linkifyText(text) {
    if (!text) return '';
    // the text is escaped first, so only the links themselves become HTML
    const urlPattern = /(https?:\/\/[^\s<>"']+)/g;
    return escapeHtml(text).replace(urlPattern, '<a href="$1" target="_blank" rel="noopener noreferrer">$1</a>');
}
