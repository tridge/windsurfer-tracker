/**
 * Event modal functions for Windsurfer Tracker WebUI
 * Requires: utils.js (for linkifyText)
 * Expects: global currentEventInfo variable to be set by the page
 */

/**
 * Show event description modal
 * Uses global currentEventInfo for event name and description
 */
function showEventModal() {
    if (!currentEventInfo) return;
    document.getElementById('eventModalTitle').textContent = currentEventInfo.name;
    const description = currentEventInfo.description || 'No description available.';
    document.getElementById('eventModalDescription').innerHTML = linkifyText(description);
    document.getElementById('eventModalOverlay').classList.add('visible');
}

/**
 * Hide event description modal
 * @param {Event} e - Optional click event (for checking target)
 */
function hideEventModal(e) {
    if (e && e.target !== e.currentTarget) return;
    document.getElementById('eventModalOverlay').classList.remove('visible');
}
