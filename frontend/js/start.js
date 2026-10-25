// @ts-check
/* Start — "What would you like to do?" (spec 053, TASKHOME).
 *
 * The first screen used to be the Overview for everyone: stats, charts, levels. A writer who
 * came to schedule their chapters had to hunt through tabs for the place to do it. Start is
 * the five jobs as plain choices, each one click from here:
 *
 *   #/start            the five choices
 *   #/start/post       "Post something now"  → which kind of thing
 *   #/start/schedule   "Schedule something"  → which kind of thing
 *
 * Every kind of posting has a door (Q2 of the spec): a story or chapters, artwork / sound /
 * video, a quick picture or sound, a short post, a journal. Picking a story or a piece opens
 * the work picker and lands on that work's page with its Schedule form already open.
 *
 * `#/` shows Start or the Overview per Settings → Appearance → Home screen (home_screen).
 */
(function () {
    const esc = (s) => Utils.escapeHtml(String(s == null ? '' : s));
    const enc = encodeURIComponent;

    const JOBS = [
        { href: '#/start/post', icon: '📤', title: 'Post something now', note: 'A story, a chapter, artwork, a short post or a journal.' },
        { href: '#/start/schedule', icon: '📅', title: 'Schedule something', note: 'Pick a day and time, or line up every chapter a week apart.' },
        { href: '#/editor', icon: '✍️', title: 'Write', note: 'Open a story in the editor, or start a new one.' },
        { href: '#/library', icon: '📚', title: 'My library', note: 'Every story and piece of art, and where each one is posted.' },
        { href: '#/overview', icon: '📊', title: 'See how things are doing', note: 'Views, favourites and comments across your sites.' },
        { href: '#/accounts', icon: '🔌', title: 'Set up my sites', note: 'Connect a site, or fix one that stopped working.' },
        { href: '#/settings', icon: '⚙️', title: 'Settings', note: 'Time zone, notifications, look and feel, backups.' },
    ];

    /* One list for both "post now" and "schedule": every type of posting. `pick` opens the
     * work picker; `href` goes straight to its page. */
    const KINDS = [
        { key: 'story', icon: '📖', title: 'A story or chapters', pick: 'story' },
        { key: 'art', icon: '🖼', title: 'Artwork, sound or video', pick: 'artwork', note: 'Something already in your Library.' },
        { key: 'quick', icon: '⚡', title: 'A picture or sound, quickly', href: '#/artwork/quick', note: 'Drop a file and go.' },
        { key: 'upload', icon: '⬆', title: 'Upload new artwork', href: '#/artwork/new', note: 'The full form, with per-site details.' },
        { key: 'post', icon: '💬', title: 'A short post', href: '#/posts/new', note: 'Text and pictures for Bluesky, X, Mastodon and friends.' },
        { key: 'journal', icon: '📓', title: 'A journal', href: '#/posts/journal', note: 'FurAffinity, Weasyl and DeviantArt journals.' },
    ];

    const Start = {
        render(mode) {
            const app = document.getElementById('app');
            if (mode === 'post' || mode === 'schedule') { app.innerHTML = this._kindsHtml(mode); return; }
            app.innerHTML = `
                <div class="start-wrap">
                    <h1 class="start-title">What would you like to do?</h1>
                    <ul class="start-grid" role="list">
                        ${JOBS.map(j => `<li><a class="start-card" href="${j.href}">
                            <span class="start-ico" aria-hidden="true">${j.icon}</span>
                            <span class="start-text"><b>${esc(j.title)}</b><small>${esc(j.note)}</small></span></a></li>`).join('')}
                    </ul>
                    <nav class="start-foot" aria-label="More">
                        <a class="btn btn-secondary start-foot-btn" href="#/posting/queue" title="See everything that's scheduled"><span aria-hidden="true">🗓️</span> Scheduled</a>
                        <a class="btn btn-secondary start-foot-btn" href="#/settings/appearance" title="Choose which screen PawPoller opens on"><span aria-hidden="true">🏠</span> Home screen</a>
                    </nav>
                </div>`;
        },

        _kindsHtml(mode) {
            const schedule = mode === 'schedule';
            const q = schedule ? '?schedule=1' : '';
            return `
                <div class="start-wrap">
                    <a class="btn btn-secondary btn-sm start-back" href="#/start"><span aria-hidden="true">←</span> Back to Start</a>
                    <h1 class="start-title">${schedule ? 'What do you want to schedule?' : 'What do you want to post?'}</h1>
                    <ul class="start-grid start-grid--kinds" role="list">
                        ${KINDS.map(k => `<li>${k.pick
                            ? `<button type="button" class="start-card" data-start-pick="${k.pick}" data-start-mode="${mode}">`
                            : `<a class="start-card" href="${k.href}${k.key === 'post' ? q : ''}">`}
                            <span class="start-ico" aria-hidden="true">${k.icon}</span>
                            <span class="start-text"><b>${esc(k.title)}</b>${k.note ? `<small>${esc(k.note)}</small>` : ''}</span>
                            ${k.pick ? '</button>' : '</a>'}</li>`).join('')}
                    </ul>
                </div>`;
        },

        /* Story → its page with the Schedule sheet (or the chapter grid, to post now) open.
         * Artwork → its page with its schedule form open. */
        pick(kind, mode) {
            if (!window.WorkPicker) return;
            const schedule = mode === 'schedule';
            window.WorkPicker.open({
                title: kind === 'story' ? 'Which story?' : 'Which piece?',
                filters: [kind], multi: false, pickOnClick: true,
                confirmLabel: schedule ? 'Schedule this' : 'Post this',
                onConfirm: (items) => {
                    const ref = (items[0] || {}).member_ref || '';
                    const name = ref.slice(ref.indexOf(':') + 1);
                    if (!name) return;
                    location.hash = kind === 'story'
                        ? `#/library/work/${enc(name)}?${schedule ? 'schedule' : 'post'}=1`
                        : `#/artwork/image/${enc(name)}${schedule ? '?schedule=1' : ''}`;
                },
            });
        },
    };

    document.addEventListener('click', (e) => {
        const b = e.target && e.target.closest && e.target.closest('[data-start-pick]');
        if (b) { e.preventDefault(); Start.pick(b.dataset.startPick, b.dataset.startMode); }
    });

    window.Start = Start;
})();
