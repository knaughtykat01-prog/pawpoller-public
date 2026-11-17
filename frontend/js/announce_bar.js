/* The announcement bar (spec 089): one line under the top bar, on every page.
 *
 * The server has already checked that the words came from the operator (announce.py). Here they
 * are still only words: every piece of text goes in with textContent, so nothing in an
 * announcement can become markup, a link or a script. Details always ends with a line that ships
 * in the app, so an announcement can never be the thing telling someone to download something.
 */
(function () {
    'use strict';

    const LEVELS = { 'info': 'Info', 'heads-up': 'Heads-up', 'urgent': 'Urgent' };
    const FIXED = 'PawPoller never asks you to download anything from an address in an announcement.';
    const EVERY_MS = 30 * 60 * 1000;
    let open = false;      // Details stays as the reader left it across refreshes of the same announcement
    let showing = '';
    let painted = '';      // what is on screen now, so an unchanged refresh leaves it alone

    function el(tag, cls, text) {
        const n = document.createElement(tag);
        if (cls) n.className = cls;
        if (text != null) n.textContent = text;
        return n;
    }

    function paint(items) {
        const bar = document.getElementById('pp-announce');
        if (!bar) return;
        const a = (items || [])[0];
        if (!a || !LEVELS[a.level]) { bar.hidden = true; bar.replaceChildren(); showing = ''; painted = ''; return; }
        // The half-hourly refresh usually brings the same announcement back. Redrawing it would make
        // a screen reader read it again and drop the keyboard focus from Details or ✕.
        const sig = JSON.stringify([a.id, a.level, a.title, a.body]);
        if (sig === painted && !bar.hidden) return;
        painted = sig;
        if (a.id !== showing) open = false;
        showing = a.id;

        const more = el('div', 'pp-announce-more');
        more.id = 'pp-announce-more';
        more.hidden = !open;
        more.append(el('p', 'pp-announce-body', a.body), el('p', 'pp-announce-fixed', FIXED));

        const details = el('button', 'pp-announce-details', 'Details');
        details.type = 'button';
        details.setAttribute('aria-expanded', String(open));
        details.setAttribute('aria-controls', 'pp-announce-more');
        details.addEventListener('click', () => {
            open = !open;
            more.hidden = !open;
            details.setAttribute('aria-expanded', String(open));
        });

        const close = el('button', 'pp-announce-x', '✕');
        close.type = 'button';
        close.setAttribute('aria-label', 'Close this announcement');
        close.addEventListener('click', async () => {
            close.disabled = true;
            try { paint((await API.closeAnnouncement(a.id)).items); }
            catch (e) { close.disabled = false; }     // still there, still closable: nothing was lost
        });

        bar.className = 'pp-announce pp-announce--' + a.level;
        // Shown first, filled a moment later: a status region is only read out when its content
        // changes while it is already on the page, not when it appears already full.
        const parts = [el('span', 'pp-announce-level', LEVELS[a.level]), el('span', 'pp-announce-title', a.title),
            details, close, more];
        bar.replaceChildren();
        bar.hidden = false;
        requestAnimationFrame(() => { if (painted === sig) bar.replaceChildren(...parts); });
    }

    async function refresh() {
        // On the full-screen pages (sign-in, setup) app.js route() hides the bar and never calls
        // ensure(), so nothing is asked before someone is in the app.
        try { paint((await API.getAnnouncements()).items); }
        catch (e) { /* signed out or offline: no bar, and no error either */ }
    }

    /* Called by app.js route() on every in-app page. Asks once, then every 30 minutes: never per
       navigation (a request cut off by a reload is logged as a network error and trips the self-heal). */
    let started = false;
    function ensure() {
        if (started) return;
        started = true;
        refresh();
        // The desktop window opens as the server starts, before its first check (20 s in), and a
        // new announcement should show within minutes of starting: look once more after a minute.
        setTimeout(refresh, 60 * 1000);
        setInterval(refresh, EVERY_MS);
    }

    window.AnnounceBar = { ensure, refresh, paint };
})();
