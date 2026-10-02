/* PawPoller loading + toast UI
 *
 * A toast stack in the bottom-right for success / error / info
 *      messages. Exposed as window.toast.{success,error,info,warn}.
 *      Auto-dismiss after 4s (success/info) or 6s (error/warn); click
 *      to dismiss earlier. Stacks vertically with newest on top.
 *
 * Self-contained — no dependencies. Safe to load before app.js. The
 * fetch wrap is idempotent (won't double-wrap on hot reload).
 */
(function () {
    'use strict';

    if (window.__pawpollerLoadingInstalled) return;
    window.__pawpollerLoadingInstalled = true;

    // The fetch-count ring that used to sit here (it spun for ANY request, so it could
    // not say what was happening) was replaced in 4.51.0 (spec 017) by the activity pill
    // for real work (activity.js) and page skeletons for loads (Utils.skeleton).

    // ─────────────────────────────────────────────────────────────
    // Toasts
    // ─────────────────────────────────────────────────────────────

    let toastStackEl = null;

    function ensureToastStack() {
        if (toastStackEl) return toastStackEl;
        toastStackEl = document.createElement('div');
        toastStackEl.className = 'pp-toast-stack';
        // Screen readers hear every toast without moving focus (4.45.0, spec 012).
        toastStackEl.setAttribute('role', 'status');
        toastStackEl.setAttribute('aria-live', 'polite');
        document.body.appendChild(toastStackEl);
        return toastStackEl;
    }

    function makeToast(message, kind = 'info', timeoutMs = null) {
        ensureToastStack();
        const t = document.createElement('div');
        t.className = 'pp-toast pp-toast-' + kind;
        if (kind === 'error') t.setAttribute('role', 'alert');   // errors interrupt; the rest wait their turn
        const icon =
            kind === 'success' ? '✓' :
            kind === 'error'   ? '✕' :
            kind === 'warn'    ? '⚠' : '·';
        t.innerHTML =
            '<span class="pp-toast-icon" aria-hidden="true">' + icon + '</span>' +
            '<span class="pp-toast-msg"></span>' +
            '<button class="pp-toast-close" aria-label="Dismiss">&times;</button>';
        t.querySelector('.pp-toast-msg').textContent = String(message);
        const close = () => {
            t.classList.remove('is-visible');
            setTimeout(() => t.remove(), 200);
        };
        t.querySelector('.pp-toast-close').addEventListener('click', close);
        toastStackEl.insertBefore(t, toastStackEl.firstChild);
        // next-frame add visible class so the CSS transition runs
        requestAnimationFrame(() => t.classList.add('is-visible'));
        if (timeoutMs === null) {
            // Errors stick until dismissed (2.53.0) — an error toast that
            // auto-vanishes in a few seconds is easy to miss, and the user
            // asked for failures to be obvious. Warnings/info still auto-hide.
            // Pass an explicit timeoutMs (e.g. 4000) to override per call.
            timeoutMs = kind === 'error' ? 0 : (kind === 'warn' ? 6000 : 4000);
        }
        if (timeoutMs > 0) {
            setTimeout(close, timeoutMs);
        }
        return { close };
    }

    window.toast = {
        success: (m, ms) => makeToast(m, 'success', ms),
        error:   (m, ms) => makeToast(m, 'error', ms),
        warn:    (m, ms) => makeToast(m, 'warn', ms),
        info:    (m, ms) => makeToast(m, 'info', ms),
    };

    // ─────────────────────────────────────────────────────────────
    // Error modal — the "you must acknowledge this" layer. Reserved for a
    // critical failure of an action the user JUST triggered (e.g. a post
    // dying on an expired session), where a toast is too easy to miss. Reuses
    // the app's .modal-overlay.open convention. opts: {title, message,
    // actionLabel, actionHref}.
    // ─────────────────────────────────────────────────────────────
    window.errorModal = function errorModal(opts) {
        const o = opts || {};
        const existing = document.getElementById('pp-error-modal');
        if (existing) existing.remove();
        const overlay = document.createElement('div');
        overlay.id = 'pp-error-modal';
        overlay.className = 'modal-overlay open';
        const actionHTML = o.actionHref
            ? `<a class="btn btn-primary" id="pp-em-action" href="${o.actionHref}"></a>`
            : '';
        overlay.innerHTML =
            `<div class="modal pp-error-modal-box" role="alertdialog" aria-modal="true">
                <div class="pp-em-head"><span class="pp-em-ico" aria-hidden="true">⚠</span>
                    <span class="pp-em-title"></span></div>
                <div class="pp-em-body"></div>
                <div class="pp-em-actions">${actionHTML}<button class="btn" id="pp-em-ok">OK</button></div>
            </div>`;
        overlay.querySelector('.pp-em-title').textContent = o.title || 'Something went wrong';
        overlay.querySelector('.pp-em-body').textContent = o.message || '';
        const action = overlay.querySelector('#pp-em-action');
        if (action) action.textContent = o.actionLabel || 'Fix it';
        document.body.appendChild(overlay);
        const close = () => overlay.remove();
        overlay.querySelector('#pp-em-ok').addEventListener('click', close);
        if (action) action.addEventListener('click', close);
        overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });
        return { close };
    };

    // ─────────────────────────────────────────────────────────────
    // Button loading helper — opt-in per call site.
    // Usage:
    //     btn.addEventListener('click', () =>
    //         withLoading(btn, async () => { await fetch(...); })
    //     );
    // ─────────────────────────────────────────────────────────────

    window.withLoading = async function withLoading(btn, asyncFn) {
        if (!btn) return asyncFn();
        const originalHTML = btn.innerHTML;
        const originalDisabled = btn.disabled;
        btn.disabled = true;
        btn.classList.add('pp-btn-loading');
        // Preserve width so the layout doesn't jump when the label
        // shrinks to just the spinner glyph.
        const rect = btn.getBoundingClientRect();
        const stashedMinWidth = btn.style.minWidth;
        btn.style.minWidth = rect.width + 'px';
        btn.innerHTML = '<span class="pp-btn-spinner"></span>';
        try {
            return await asyncFn();
        } finally {
            btn.innerHTML = originalHTML;
            btn.disabled = originalDisabled;
            btn.style.minWidth = stashedMinWidth;
            btn.classList.remove('pp-btn-loading');
        }
    };

    // ─────────────────────────────────────────────────────────────
    // Generic [data-tooltip] hover tooltip.
    //
    // Any element with a non-empty data-tooltip attribute gets a
    // styled hover tooltip after a 1.2s delay (matches the anchor
    // toolbar from 2.13.7/8). One shared DOM node + event
    // delegation on document — works for elements added later in
    // the SPA lifecycle without any explicit init call. Used by
    // sidebar health dots, platform-page "last polled" subtitles,
    // and anywhere else inline help is wanted.
    //
    // Hidden on mouseleave / mousedown / scroll / Escape so it
    // never traps a user mid-action.
    // ─────────────────────────────────────────────────────────────

    const TOOLTIP_DELAY_MS = 1200;
    let tooltipEl = null;
    let tooltipTimer = null;
    let activeTarget = null;

    function ensureTooltipEl() {
        if (tooltipEl) return tooltipEl;
        tooltipEl = document.createElement('div');
        tooltipEl.className = 'pp-tooltip';
        tooltipEl.setAttribute('role', 'tooltip');
        document.body.appendChild(tooltipEl);
        return tooltipEl;
    }

    function showTooltip(target) {
        const text = target.getAttribute('data-tooltip');
        if (!text) return;
        const tip = ensureTooltipEl();
        tip.textContent = text;
        tip.classList.add('is-visible');
        const rect = target.getBoundingClientRect();
        const tipW = tip.offsetWidth;
        const tipH = tip.offsetHeight;
        // Default below; flip above if it would overflow the viewport.
        let left = Math.max(8, Math.min(rect.left, window.innerWidth - tipW - 8));
        let top = rect.bottom + 6;
        if (top + tipH > window.innerHeight - 8) {
            top = Math.max(8, rect.top - tipH - 6);
        }
        tip.style.left = `${left}px`;
        tip.style.top = `${top}px`;
    }

    function hideTooltip() {
        clearTimeout(tooltipTimer);
        tooltipTimer = null;
        activeTarget = null;
        if (tooltipEl) tooltipEl.classList.remove('is-visible');
    }

    function findTooltipTarget(node) {
        // Walk up from the actual hovered child to find the nearest
        // ancestor that owns the data-tooltip attribute. Handles e.g.
        // a tooltip on a button whose hover lands on the inner span.
        for (let n = node; n && n !== document; n = n.parentNode) {
            if (n.nodeType === 1 && n.hasAttribute && n.hasAttribute('data-tooltip')) {
                return n;
            }
        }
        return null;
    }

    document.addEventListener('mouseover', (e) => {
        const target = findTooltipTarget(e.target);
        if (!target || target === activeTarget) return;
        activeTarget = target;
        clearTimeout(tooltipTimer);
        tooltipTimer = setTimeout(() => showTooltip(target), TOOLTIP_DELAY_MS);
    }, true);

    document.addEventListener('mouseout', (e) => {
        const target = findTooltipTarget(e.target);
        if (!target) return;
        // Only hide if leaving the owning element entirely (not
        // moving between its children).
        if (e.relatedTarget && target.contains(e.relatedTarget)) return;
        hideTooltip();
    }, true);

    // Belt-and-braces: any click, scroll, or escape kills the tooltip
    // so it doesn't sit stale on top of an action-in-progress UI.
    document.addEventListener('mousedown', hideTooltip, true);
    document.addEventListener('scroll', hideTooltip, true);
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') hideTooltip();
    });

})();
