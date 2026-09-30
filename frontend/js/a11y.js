/*
 * Accessibility helpers (4.45.0, spec 012 — WCAG core principles in the app).
 *
 * 1. Dialogs. The app builds its dialogs in many places (.modal-overlay.open, role="dialog",
 *    role="alertdialog", <dialog open>). Rather than patch each one, this watches the page and
 *    gives every dialog the same behaviour when it opens: focus moves into it, Tab and Shift+Tab
 *    stay inside it, Escape closes it if the dialog doesn't handle Escape itself, and when it
 *    closes focus goes back to whatever opened it.
 * 2. A11y.announce(text, {assertive}) — tell a screen reader about something that happened
 *    without a page change and without moving focus. (Toasts are announced by their own
 *    live region in loading_indicator.js.)
 *
 * No network, no storage, nothing visible. Contract: specs/012-accessibility-wcag/contracts/a11y_js.md
 */
(function () {
    'use strict';

    const DIALOG = '.modal-overlay.open, [role="dialog"], [role="alertdialog"], dialog[open]';
    const FOCUSABLE = 'a[href], area[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), '
        + 'select:not([disabled]), textarea:not([disabled]), iframe, [contenteditable="true"], '
        + '[tabindex]:not([tabindex="-1"])';
    const open = [];          // [{el, opener}] — the last is on top
    let uid = 0;

    const visible = (el) => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
    const focusables = (root) => Array.from(root.querySelectorAll(FOCUSABLE)).filter(visible);
    // Open = in the page, a dialog, AND visible. Several dialogs stay in the page after closing,
    // hidden by a parent's class (publish check, the Ctrl+K palette); counting those as open
    // swallowed Tab everywhere and let Escape click buttons nobody could see (4.45.2).
    const isOpen = (el) => el.isConnected && el.matches(DIALOG) && !el.hidden && visible(el);

    function adopt(el) {
        if (open.some(d => d.el === el)) return;
        // A .modal-overlay nested inside another tracked dialog is the same dialog.
        if (open.some(d => d.el.contains(el) || el.contains(d.el))) return;
        const opener = document.activeElement && document.activeElement !== document.body
            ? document.activeElement : null;
        if (!el.getAttribute('role') && el.tagName !== 'DIALOG') el.setAttribute('role', 'dialog');
        if (!el.hasAttribute('aria-modal')) el.setAttribute('aria-modal', 'true');
        if (!el.hasAttribute('aria-label') && !el.hasAttribute('aria-labelledby')) {
            const h = el.querySelector('h1, h2, h3, h4, [class*="title"]');
            if (h && h.textContent.trim()) {
                if (!h.id) h.id = 'a11y-dlg-title-' + (++uid);
                el.setAttribute('aria-labelledby', h.id);
            }
        }
        open.push({ el, opener });
        // Let the dialog's own code finish (it may focus a field itself) before choosing a target.
        setTimeout(() => {
            if (!isOpen(el) || el.contains(document.activeElement)) return;
            const f = focusables(el);
            const target = f.find(x => !x.matches('.modal-close, [data-close], [aria-label="Close"], [aria-label="Dismiss"]'))
                || f[0];
            if (target) target.focus();
            else {
                const panel = el.querySelector('.modal-panel, .modal') || el;
                if (!panel.hasAttribute('tabindex')) panel.setAttribute('tabindex', '-1');
                panel.focus();
            }
        }, 0);
    }

    function release(entry) {
        const i = open.indexOf(entry);
        if (i !== -1) open.splice(i, 1);
        const o = entry.opener;
        if (o && o.isConnected && visible(o) && !open.length) {
            try { o.focus(); } catch (e) { /* the opener went away */ }
        }
    }

    // Settings rows show their label as text beside a toggle, not in a <label> the toggle is
    // tied to — so a screen reader heard just "checkbox". Point each unnamed control at its
    // row's visible label (41 toggles, and any added later).
    function nameRowControls(root) {
        root.querySelectorAll('.settings-row input:not([type="hidden"]), .settings-row select, .settings-row textarea')
            .forEach(c => {
                if (c.hasAttribute('aria-label') || c.hasAttribute('aria-labelledby')) return;
                if (c.labels && Array.from(c.labels).some(l => l.textContent.trim())) return;
                const lbl = c.closest('.settings-row').querySelector('.settings-label');
                if (!lbl || !lbl.textContent.trim()) return;
                if (!lbl.id) lbl.id = 'a11y-row-label-' + (++uid);
                c.setAttribute('aria-labelledby', lbl.id);
            });
    }

    // Status dots say their state in words to a screen reader (their shape says it on screen).
    const DOT_STATES = [['connected', 'Working'], ['ok', 'Working'], ['warn', 'Needs attention'],
        ['expired', 'Needs attention'], ['disconnected', 'Not connected'], ['muted', 'Off']];
    function nameStatusDots(root) {
        root.querySelectorAll('.status-dot').forEach(d => {
            const hit = DOT_STATES.find(([c]) => d.classList.contains(c));
            const name = hit ? hit[1] : 'Status unknown';
            if (d.getAttribute('aria-label') !== name) {
                d.setAttribute('role', 'img');
                d.setAttribute('aria-label', name);
            }
        });
    }

    function sweep() {
        for (const d of open.slice()) if (!isOpen(d.el)) release(d);
        document.querySelectorAll(DIALOG).forEach(el => { if (isOpen(el)) adopt(el); });
        nameRowControls(document);
        nameStatusDots(document);
    }

    document.addEventListener('keydown', (e) => {
        const top = open[open.length - 1];
        if (!top || !isOpen(top.el)) return;
        if (e.key === 'Tab') {
            const f = focusables(top.el);
            if (!f.length) { e.preventDefault(); return; }
            const first = f[0], last = f[f.length - 1];
            if (!top.el.contains(document.activeElement)) { e.preventDefault(); first.focus(); }
            else if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
            else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
        } else if (e.key === 'Escape') {
            // Run after the dialog's own handlers: if one of them dealt with Escape, leave it be.
            setTimeout(() => {
                if (e.defaultPrevented || !isOpen(top.el)) return;
                // The dialog's OWN close control first; a button merely labelled Cancel/Close only as a
                // last resort — and never one that can't be seen (a hidden "Cancel" can be a queue
                // cancel in another panel of the same dialog).
                const shown = (sel) => Array.from(top.el.querySelectorAll(sel)).find(visible);
                const btn = shown('[data-pub-cancel], [data-close], .modal-close, [aria-label="Close"]')
                    || Array.from(top.el.querySelectorAll('button')).filter(visible)
                        .find(b => /^(cancel|close)$/i.test(b.textContent.trim()));
                if (btn) btn.click();
            }, 0);
        }
    });

    // ── announce ───────────────────────────────────────────────────────────
    const regions = {};
    function region(assertive) {
        const key = assertive ? 'alert' : 'status';
        if (regions[key] && regions[key].isConnected) return regions[key];
        const r = document.createElement('div');
        r.className = 'sr-only';
        r.setAttribute('role', key);
        r.setAttribute('aria-live', assertive ? 'assertive' : 'polite');
        document.body.appendChild(r);
        regions[key] = r;
        return r;
    }

    const A11y = {
        announce(text, opts) {
            try {
                const r = region(!!(opts && opts.assertive));
                r.textContent = '';                                  // the same text twice is still heard
                setTimeout(() => { r.textContent = String(text == null ? '' : text); }, 30);
            } catch (e) { /* never break the caller */ }
        },
        _openDialogs: () => open.map(d => d.el),                      // for tests
        _sweep: sweep,
    };

    // The app toggles classes constantly; look at most once per frame, not once per change.
    let queued = false;
    const schedule = () => {
        if (queued) return;
        queued = true;
        (window.requestAnimationFrame || setTimeout)(() => { queued = false; sweep(); });
    };

    function start() {
        sweep();
        new MutationObserver(schedule).observe(document.body, {
            childList: true, subtree: true, attributes: true, attributeFilter: ['class', 'open', 'hidden', 'role'],
        });
    }
    if (typeof document !== 'undefined') {
        if (document.body) start(); else document.addEventListener('DOMContentLoaded', start);
    }
    if (typeof window !== 'undefined') window.A11y = A11y;
})();
