/**
 * Common utility functions for Windsurfer Tracker WebUI
 */

/**
 * Escape HTML special characters to prevent XSS
 * @param {string} str - Input string
 * @returns {string} HTML-escaped string
 */
function escapeHtml(str) {
    if (!str) return '';
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

/**
 * Convert URLs in text to clickable links
 * @param {string} text - Input text
 * @returns {string} Text with URLs converted to anchor tags
 */
function linkifyText(text) {
    if (!text) return '';
    const urlPattern = /(https?:\/\/[^\s<]+)/g;
    return text.replace(urlPattern, '<a href="$1" target="_blank" rel="noopener noreferrer">$1</a>');
}
