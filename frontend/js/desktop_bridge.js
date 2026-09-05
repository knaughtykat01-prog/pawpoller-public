/* Connected-desktop bridge (SYNCTRUTH, 4.13.0).
 *
 * When this page is shown inside the PawPoller desktop app in *connected* mode, the app
 * injects `window.pywebview.api` with the local agent (desktop_agent.AgentApi). The page
 * itself is served by the SERVER, which has no screen, so two things must be redirected to
 * the desktop:
 *
 *   • API.browserLogin(...)            → pywebview.api.agent_login(...)   (popup runs locally,
 *                                        cookies are handed to the server by the agent)
 *   • pywebview.api.open_image_dialog  → already the agent's own method: it uploads the picked
 *                                        file and returns the SERVER-side path, so artwork.js
 *                                        needs no change at all.
 *
 * In an ordinary browser, or in the standalone desktop build (whose bridge has no
 * agent_login), this file does nothing. pywebview injects its object after load, so the
 * decision is made at call time and again on `pywebviewready`. */
(() => {
    'use strict';
    const hasAgent = () => !!(window.pywebview && window.pywebview.api && window.pywebview.api.agent_login);
    const wrap = () => {
        if (typeof API === 'undefined' || !API || API.__desktopBridge || typeof API.browserLogin !== 'function') return;
        const original = API.browserLogin.bind(API);
        API.browserLogin = function (platform, extraFields = {}, accountId = null) {
            if (hasAgent()) return window.pywebview.api.agent_login(platform, extraFields || {}, accountId);
            return original(platform, extraFields, accountId);
        };
        API.__desktopBridge = true;
    };
    wrap();
    window.addEventListener('pywebviewready', wrap);
})();
