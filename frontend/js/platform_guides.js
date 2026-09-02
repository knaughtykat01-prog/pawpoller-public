/* Platform "How to get started" guides — 2.65.0.
 *
 * One shared, static dataset describing, per platform, how to go from nothing to
 * a working + connected credential in PawPoller, plus how to keep it alive
 * (cookies expire, Meta tokens last ~60 days, etc.). Surfaced two ways: a "Setup
 * guide" button on each Settings connect card (modal) and the Getting Started
 * hub page (#/getting-started). Pure content + a renderer — no network, no state.
 *
 * Schema per platform code:
 *   kind        'Analytics' | 'Analytics + posting'
 *   difficulty  'Easy' | 'Medium' | 'Involved'
 *   summary     one-line what-it-does
 *   need        [str]           prerequisites ("What you'll need")
 *   steps       [{t, b, link?}] ordered walk-through (b may contain simple HTML)
 *   paste       str             where the credential goes in PawPoller
 *   renew       {when, how}     "Keeping it alive"
 *   notes       [str]           gotchas / good-to-know
 */
(function () {
  'use strict';

  // Reusable snippet: how to copy a cookie value from a browser (FA/DA/X).
  const COOKIE_HOWTO =
    'To read a cookie: log in on a desktop browser, press <b>F12</b> to open ' +
    'DevTools, go to <b>Application</b> (Chrome/Edge) or <b>Storage</b> (Firefox) ' +
    '&rarr; <b>Cookies</b> &rarr; the site, then copy the <b>Value</b> of each ' +
    'named cookie.';

  const GUIDES = {

    // ── Inkbunny ─────────────────────────────────────────────
    ib: {
      kind: 'Analytics + posting', difficulty: 'Easy',
      summary: 'Track your Inkbunny submission stats using your login.',
      need: ['An Inkbunny account', 'API access enabled on that account'],
      steps: [
        { t: 'Log into Inkbunny', b: 'Sign in at inkbunny.net.',
          link: { label: 'inkbunny.net', url: 'https://inkbunny.net' } },
        { t: 'Enable API access', b: 'Go to <b>Account &rarr; Settings</b> and turn on <b>Allow API Access</b> (accept the API terms). The API uses your normal username + password to log in.' },
        { t: 'Allow the ratings you post', b: 'If you post mature/adult work, set your content-rating preferences so the API can see it.' },
        { t: 'Connect in PawPoller', b: 'Enter your Inkbunny username and password in Settings.' },
      ],
      paste: 'Settings → Inkbunny → Username + Password',
      renew: { when: 'Only if you change your Inkbunny password', how: 'Re-enter the new password in Settings.' },
      notes: ['Nothing expires on a schedule — it stays connected until you change your password or revoke API access.'],
    },

    // ── FurAffinity ──────────────────────────────────────────
    fa: {
      kind: 'Analytics + posting', difficulty: 'Medium',
      summary: 'Track your FA gallery stats via your session cookies.',
      need: ['A FurAffinity account', 'Your logged-in cookies (a and b)'],
      steps: [
        { t: 'Log into FurAffinity', b: 'Sign in at furaffinity.net in a desktop browser.',
          link: { label: 'furaffinity.net', url: 'https://www.furaffinity.net' } },
        { t: 'Copy the a and b cookies', b: COOKIE_HOWTO + ' You need the two cookies named <code>a</code> and <code>b</code>.' },
        { t: 'Connect in PawPoller', b: 'Paste the <code>a</code> and <code>b</code> values into the FurAffinity fields in Settings.' },
      ],
      paste: 'Settings → FurAffinity → Cookie a + Cookie b',
      renew: { when: 'Cookies expire when you log out or after a while — polling starts failing with an auth error', how: 'Log back into FA, grab fresh <code>a</code> and <code>b</code> cookies, and paste them again.' },
      notes: [
        'FA blocks datacenter IPs, so posting/importing runs from the <b>desktop</b> app (residential IP), not the server.',
        'An official FA API is in closed beta and will replace cookies eventually.',
      ],
    },

    // ── Weasyl ───────────────────────────────────────────────
    ws: {
      kind: 'Analytics + posting', difficulty: 'Easy',
      summary: 'Track your Weasyl stats with an API key (no password stored).',
      need: ['A Weasyl account'],
      steps: [
        { t: 'Open your API keys', b: 'On weasyl.com go to <b>Settings &rarr; Manage API Keys</b>.',
          link: { label: 'Weasyl API keys', url: 'https://www.weasyl.com/control/apikeys' } },
        { t: 'Create a key', b: 'Generate a new API key and copy it.' },
        { t: 'Connect in PawPoller', b: 'Paste the API key (and your Weasyl username) in Settings.' },
      ],
      paste: 'Settings → Weasyl → API key (+ username)',
      renew: { when: 'Never, unless you delete the key', how: 'Generate a new key on Weasyl and paste it in.' },
      notes: ['An API key is safer than a password — it can be revoked without changing your login.'],
    },

    // ── SoFurry ──────────────────────────────────────────────
    sf: {
      kind: 'Analytics + posting', difficulty: 'Medium',
      summary: 'Track your SoFurry stats using your login.',
      need: ['A SoFurry account', '2FA turned OFF (the 2FA login path is unsupported)'],
      steps: [
        { t: 'Have your SoFurry login ready', b: 'Your normal SoFurry username and password.' },
        { t: 'Connect in PawPoller', b: 'Enter them in the SoFurry fields in Settings. On the desktop app the session is saved; on the server it logs in through the Cloudflare proxy.' },
      ],
      paste: 'Settings → SoFurry → Username + Password',
      renew: { when: 'Only if you change your password', how: 'Re-enter the new password.' },
      notes: [
        'If your SoFurry account has <b>two-factor auth</b>, the login can\'t complete — that path isn\'t handled yet.',
        'On the server SoFurry polls through the CF proxy (its datacenter IP is blocked otherwise).',
      ],
    },

    // ── SquidgeWorld ─────────────────────────────────────────
    sqw: {
      kind: 'Analytics + posting', difficulty: 'Easy',
      summary: 'Track your SquidgeWorld (AO3-style archive) stats.',
      need: ['A SquidgeWorld account'],
      steps: [
        { t: 'Have your login ready', b: 'Your SquidgeWorld username and password.',
          link: { label: 'squidgeworld.org', url: 'https://squidgeworld.org' } },
        { t: 'Connect in PawPoller', b: 'Enter them in the SquidgeWorld fields in Settings.' },
      ],
      paste: 'Settings → SquidgeWorld → Username + Password',
      renew: { when: 'Only if you change your password', how: 'Re-enter the new password.' },
      notes: ['SquidgeWorld runs the same software as AO3, so hits/kudos/comments work the same way.'],
    },

    // ── AO3 ──────────────────────────────────────────────────
    ao3: {
      kind: 'Analytics', difficulty: 'Medium',
      summary: 'Track hits, kudos and comments on your AO3 works.',
      need: ['An AO3 account (username + password, OR a session cookie)'],
      steps: [
        { t: 'Choose how to log in', b: 'Easiest is your AO3 <b>username + password</b>. Alternatively you can paste the <code>_otwarchive_session</code> cookie.' },
        { t: 'Connect in PawPoller', b: 'Enter your username + password (or the session cookie) in the AO3 fields in Settings.' },
      ],
      paste: 'Settings → AO3 → Username + Password (or session cookie)',
      renew: { when: 'A session cookie expires; username + password re-logs in automatically', how: 'If you used the cookie method and it lapses, grab a fresh <code>_otwarchive_session</code> cookie, or switch to username + password.' },
      notes: [
        'AO3 throttles datacenter IPs hard, so bulk <b>imports run from the desktop</b> app. Ongoing polling still runs server-side.',
      ],
    },

    // ── DeviantArt ───────────────────────────────────────────
    da: {
      kind: 'Analytics', difficulty: 'Medium',
      summary: 'Track views/faves/comments on a DeviantArt gallery.',
      need: ['A DeviantArt login cookie', 'The DA username to track'],
      steps: [
        { t: 'Log into DeviantArt', b: 'Sign in at deviantart.com in a desktop browser.',
          link: { label: 'deviantart.com', url: 'https://www.deviantart.com' } },
        { t: 'Copy your login cookie', b: COOKIE_HOWTO },
        { t: 'Connect in PawPoller', b: 'Paste the cookie and the target DA username in Settings.' },
      ],
      paste: 'Settings → DeviantArt → Cookie + Target user',
      renew: { when: 'The cookie expires periodically — stats stop updating', how: 'Log back into DeviantArt and paste a fresh cookie.' },
      notes: [
        'On the server DA polls through the CF proxy (datacenter IPs are blocked).',
        'DeviantArt now has an official OAuth API that returns public stats without a cookie — a future PawPoller update will switch to it and drop the cookie step.',
      ],
    },

    // ── Wattpad ──────────────────────────────────────────────
    wp: {
      kind: 'Analytics', difficulty: 'Easy',
      summary: 'Track reads/votes/comments on a Wattpad profile — no login.',
      need: ['A Wattpad username (public data only)'],
      steps: [
        { t: 'Find the username', b: 'The @handle of the Wattpad profile you want to track.',
          link: { label: 'wattpad.com', url: 'https://www.wattpad.com' } },
        { t: 'Connect in PawPoller', b: 'Enter the Wattpad username in Settings. No password needed — it reads public stats.' },
      ],
      paste: 'Settings → Wattpad → Target user',
      renew: { when: 'Never', how: 'Nothing to renew — it uses public data.' },
      notes: ['Because it\'s public data, there\'s no login and nothing to expire.'],
    },

    // ── Itaku ────────────────────────────────────────────────
    ik: {
      kind: 'Analytics + posting', difficulty: 'Easy',
      summary: 'Track an Itaku gallery; add an auth token to post to it.',
      need: ['An Itaku username (for tracking)', 'An auth token (only for posting) — from your logged-in session'],
      steps: [
        { t: 'Find the username', b: 'The Itaku account to track. Tracking needs nothing else — you can stop here.',
          link: { label: 'itaku.ee', url: 'https://itaku.ee' } },
        { t: 'Grab your auth token — only if you want to POST', b: 'It is an API token, NOT a cookie (so it is not in the Cookies list). Log in at itaku.ee, open DevTools (F12) → Network tab, scroll your feed so requests appear, click any request to itaku.ee, and under Request Headers find "Authorization: Token abc123…" — copy only the part AFTER "Token " (the abc123… itself). Alternatively: DevTools → Application → Local Storage → itaku.ee, and copy the saved token value.' },
        { t: 'Connect in PawPoller', b: 'Enter the username in Settings → Itaku. To also post, paste the token in the Auth token box and Save — the panel will then show "posting enabled".' },
      ],
      paste: 'Settings → Itaku → username (+ Auth token to post)',
      renew: { when: 'If posting fails with "auth token not configured" or stops working', how: 'Grab a fresh token the same way (Network tab → the value after "Authorization: Token ") and Save it again.' },
      notes: [
        'Tracking works with just the username. The auth token is needed ONLY to post (and for full-res / private imports).',
        'The token is NOT a browser cookie — it is the value after "Token " in the Authorization request header, or the token in Local Storage.',
      ],
    },

    // ── Bluesky ──────────────────────────────────────────────
    bsky: {
      kind: 'Analytics + posting', difficulty: 'Easy',
      summary: 'Track likes/reposts/replies and post to Bluesky.',
      need: ['A Bluesky account', 'An app password (not your main password)'],
      steps: [
        { t: 'Open App Passwords', b: 'In the Bluesky app or web: <b>Settings &rarr; Privacy and Security &rarr; App Passwords</b>.',
          link: { label: 'Bluesky app passwords', url: 'https://bsky.app/settings/app-passwords' } },
        { t: 'Create one', b: 'Add a new app password, name it "PawPoller", and copy it (it looks like <code>xxxx-xxxx-xxxx-xxxx</code>).' },
        { t: 'Connect in PawPoller', b: 'Enter your handle (e.g. <code>you.bsky.social</code>) and the app password in Settings.' },
      ],
      paste: 'Settings → Bluesky → Handle + App password',
      renew: { when: 'App passwords don\'t expire — only if you revoke one', how: 'Create a new app password and paste it in.' },
      notes: [
        'Always use an <b>app password</b>, never your real password — you can revoke it anytime.',
        'Posting works from any IP. Images are auto-downscaled to fit Bluesky\'s blob limit.',
      ],
    },

    // ── X / Twitter ──────────────────────────────────────────
    tw: {
      kind: 'Analytics + posting', difficulty: 'Involved',
      summary: 'Track views/likes/replies and post to X — via your session cookies.',
      need: ['An X account', 'Two logged-in cookies: auth_token and ct0'],
      steps: [
        { t: 'Log into X', b: 'Sign in at x.com in a desktop browser.',
          link: { label: 'x.com', url: 'https://x.com' } },
        { t: 'Copy auth_token and ct0', b: COOKIE_HOWTO + ' You need the cookies named <code>auth_token</code> and <code>ct0</code>.' },
        { t: 'Connect in PawPoller', b: 'Paste both cookies and the X username to track in Settings.' },
      ],
      paste: 'Settings → X/Twitter → auth_token + ct0 (+ target user)',
      renew: { when: 'X expires these cookies aggressively — expect to re-do this fairly often', how: 'Log into X again, grab fresh <code>auth_token</code> and <code>ct0</code> cookies, and paste them.' },
      notes: [
        'X actively fights automation, so this is the most fragile platform — posting can break when X rotates its internal endpoints.',
        'Posting reuses the same cookie session (no separate developer app needed).',
      ],
    },

    // ── Mastodon ─────────────────────────────────────────────
    mast: {
      kind: 'Analytics + posting', difficulty: 'Medium',
      summary: 'Track favourites/boosts/replies and post to Mastodon.',
      need: ['A Mastodon account on any instance', 'An access token with read + write scopes'],
      steps: [
        { t: 'Open your instance\'s Development page', b: 'On your instance go to <b>Preferences &rarr; Development &rarr; New application</b>.' },
        { t: 'Create an application', b: 'Name it "PawPoller". Tick the <b>read</b> scope (for polling) and <b>write</b> scope (for posting), then Submit.' },
        { t: 'Copy the access token', b: 'Open the app you just created and copy <b>Your access token</b>.' },
        { t: 'Connect in PawPoller', b: 'Enter your instance URL (e.g. <code>https://mastodon.social</code>) and the access token in Settings.' },
      ],
      paste: 'Settings → Mastodon → Instance URL + Access token',
      renew: { when: 'Tokens don\'t expire unless you delete the app', how: 'Re-create the application and paste the new token.' },
      notes: [
        'For <b>posting</b> the token must include the <b>write</b> scope — a read-only token polls fine but can\'t post.',
      ],
    },

    // ── Tumblr ───────────────────────────────────────────────
    tum: {
      kind: 'Analytics + posting', difficulty: 'Medium',
      summary: 'Track note counts and post to Tumblr.',
      need: ['A registered Tumblr app (OAuth Consumer Key)', 'Your blog name', 'For posting: the full OAuth1 token set'],
      steps: [
        { t: 'Register a Tumblr app', b: 'Go to the Tumblr apps page and <b>Register application</b>.',
          link: { label: 'Tumblr OAuth apps', url: 'https://www.tumblr.com/oauth/apps' } },
        { t: 'Copy the OAuth Consumer Key', b: 'That key is your <b>API key</b> — enough for polling notes.' },
        { t: '(For posting) get OAuth1 tokens', b: 'Posting also needs the <b>consumer secret</b> plus a user <b>OAuth token</b> + <b>token secret</b> (generated via the OAuth1 flow).' },
        { t: 'Connect in PawPoller', b: 'Enter the API key and your blog name (and the OAuth1 tokens if posting) in Settings.' },
      ],
      paste: 'Settings → Tumblr → API key + Blog name (+ OAuth1 tokens for posting)',
      renew: { when: 'Keys/tokens are long-lived — only if you delete the app', how: 'Re-register the app and paste the new key/tokens.' },
      notes: [
        'Polling needs only the API key + blog name. <b>Posting</b> needs the extra OAuth1 token set.',
        'Tumblr reports a single "notes" number (likes + reblogs + replies combined).',
      ],
    },

    // ── Pixiv ────────────────────────────────────────────────
    pix: {
      kind: 'Analytics', difficulty: 'Involved',
      summary: 'Track views/bookmarks/comments on your Pixiv works.',
      need: ['A Pixiv account', 'A refresh token from a browser login'],
      steps: [
        { t: 'Get a refresh token', b: 'Pixiv has no simple token page — use a helper like <code>gppt</code> (<code>pip install gppt</code>) or a browser-based pixiv-token tool. It walks you through a Pixiv login and captures a <b>refresh token</b>.' },
        { t: 'Copy the refresh token', b: 'The long string the tool prints after you log in.' },
        { t: 'Connect in PawPoller', b: 'Paste the refresh token (and optionally your user id) in Settings.' },
      ],
      paste: 'Settings → Pixiv → Refresh token (+ user id)',
      renew: { when: 'Refresh tokens are long-lived and rotate automatically — PawPoller stores the rotated one', how: 'Only if it\'s revoked: run the token helper again to get a fresh refresh token.' },
      notes: ['This uses Pixiv\'s app API, so it polls gently to respect rate limits.'],
    },

    // ── Threads ──────────────────────────────────────────────
    thr: {
      kind: 'Analytics', difficulty: 'Involved',
      summary: 'Track views/likes/reposts/replies on your Threads posts.',
      need: ['A Threads account (public)', 'A free Meta developer app', 'A long-lived access token'],
      steps: [
        { t: 'Open the Meta developer dashboard', b: 'Go to developers.facebook.com and create (or open) an app.',
          link: { label: 'developers.facebook.com', url: 'https://developers.facebook.com/apps' } },
        { t: 'Add the Threads use case', b: 'Add the <b>Threads</b> product / use case to the app.' },
        { t: 'Add the permissions', b: 'Add <code>threads_basic</code> and <code>threads_manage_insights</code>.' },
        { t: 'Add yourself as a tester', b: 'Under the app\'s roles, add your Threads account, then accept the tester invite from your Threads account.' },
        { t: 'Generate a long-lived token', b: 'Use the token generator to produce a long-lived access token for your account, and copy it.' },
        { t: 'Connect in PawPoller', b: 'Paste the access token (and your Threads user id, optional) in Settings.' },
      ],
      paste: 'Settings → Threads → Access token (+ user id)',
      renew: { when: 'Long-lived tokens last ~60 days', how: 'PawPoller auto-refreshes the token while it\'s polling. If it goes unused past ~60 days and lapses, generate a fresh one in the Meta dashboard and paste it in.' },
      notes: [
        'Do the whole setup on a <b>desktop</b> (not mobile) in <b>Microsoft Edge — or any browser that is NOT Chrome</b>. Chrome reliably breaks Meta\'s developer dashboard and token generator: the flow silently fails or the token never appears. Edge/Firefox work.',
        'Meta gates this behind app review for public use and removes adult content — for your own account in Development mode it works without review.',
      ],
    },

    // ── Instagram ────────────────────────────────────────────
    ig: {
      kind: 'Analytics + posting', difficulty: 'Involved',
      summary: 'Track views/reach/likes/comments/saves and post photos to Instagram.',
      need: ['A Business or Creator Instagram account', 'A free Meta developer app', 'A long-lived access token'],
      steps: [
        { t: 'Switch to a professional account', b: 'In the Instagram app: <b>Settings &rarr; Account type and tools &rarr; Switch to professional account</b> (Business or Creator). Personal accounts can\'t use the API.' },
        { t: 'Open the Meta developer dashboard', b: 'Create (or open) an app and add the <b>Instagram</b> product &rarr; <b>API setup with Instagram login</b>.',
          link: { label: 'developers.facebook.com', url: 'https://developers.facebook.com/apps' } },
        { t: 'Add the permissions', b: 'Add <code>instagram_business_basic</code> and <code>instagram_business_manage_insights</code> (for stats). For posting, also add <code>instagram_business_content_publish</code>.' },
        { t: 'Add yourself as a tester', b: 'Under <b>Roles &rarr; Instagram Tester</b> add your account, then accept the invite in Instagram (<b>Settings &rarr; Apps and websites &rarr; Tester invites</b>).' },
        { t: 'Generate the token', b: 'In "API setup with Instagram login", generate a long-lived access token for your account (approve the scopes) and copy it.' },
        { t: 'Connect in PawPoller', b: 'Paste the access token (and your Instagram user id, optional) in Settings.' },
      ],
      paste: 'Settings → Instagram → Access token (+ user id)',
      renew: { when: 'Long-lived tokens last ~60 days', how: 'PawPoller auto-refreshes it while polling. If it lapses, generate a fresh token in the Meta dashboard and paste it back in.' },
      notes: [
        'Do the whole setup on a <b>desktop</b> (not mobile) in <b>Microsoft Edge — or any browser that is NOT Chrome</b>. Chrome reliably breaks Meta\'s developer dashboard and token generator: the flow silently fails or the token never appears. Edge/Firefox work. (The one mobile step is switching your IG account to professional, done in the Instagram app.)',
        'Stats need <code>instagram_business_manage_insights</code>; <b>posting</b> needs <code>instagram_business_content_publish</code> — re-generate the token if you add a scope later.',
        'Every Instagram post <b>requires a photo</b> — there are no text-only posts. Instagram fetches the image from a public address: on the server that\'s <code>IG_PUBLIC_BASE_URL</code>; on the desktop app, pair it with your server (Settings → Posting) and it hands the image to the server automatically.',
        'Development mode + your own account needs no App Review; a public app for other users would need review + would likely be rejected for adult content.',
      ],
    },

    // ── e621 ─────────────────────────────────────────────────
    e621: {
      kind: 'Analytics', difficulty: 'Easy',
      summary: 'Track score, favorites and comments on your e621 uploads.',
      need: ['An e621 account', 'An API key (not your password)'],
      steps: [
        { t: 'Open your API access page', b: 'Log in to e621, then go to <b>Account &rarr; Manage API Access</b> (e621.net/users/home &rarr; "Manage API Access").',
          link: { label: 'e621.net', url: 'https://e621.net/users/home' } },
        { t: 'Copy your API key', b: 'The page shows your <b>API key</b> — a long string tied to your account. This is <b>not</b> your login password.' },
        { t: 'Connect in PawPoller', b: 'Enter your e621 <b>username</b> and paste the <b>API key</b> in Settings.' },
      ],
      paste: 'Settings → e621 → Username + API key',
      renew: { when: 'API keys don\'t expire', how: 'Only if you regenerate/revoke it on e621 — paste the new key back in.' },
      notes: [
        'Poll-only: PawPoller reads the engagement on posts you <b>uploaded</b> (tags <code>user:&lt;you&gt;</code>). It never posts.',
        'e621 exposes no view count, so <b>score</b> (up-votes minus down-votes, which can go negative) is the headline metric alongside favorites and comments.',
        'Polling is gentle by design — e621\'s API asks for about one request per second, which PawPoller respects.',
      ],
    },
  };

  function _plat(code) {
    try { return (window.platformByCode) ? window.platformByCode(code) : null; }
    catch (e) { return null; }
  }

  function has(code) { return !!GUIDES[code]; }
  function get(code) { return GUIDES[code] || null; }
  function codes() { return Object.keys(GUIDES); }

  function label(code) {
    const p = _plat(code);
    return (p && p.label) || code.toUpperCase();
  }
  function emoji(code) {
    const p = _plat(code);
    return (p && p.emoji) || '';
  }

  /* Render the full guide body (used by both the modal and the hub detail). */
  function renderBody(code) {
    const g = GUIDES[code];
    if (!g) return '<p class="muted">No guide for this platform yet.</p>';
    const steps = (g.steps || []).map((s, i) => {
      const link = s.link
        ? ` <a href="${s.link.url}" target="_blank" rel="noopener" class="guide-link">${s.link.label} &#8599;</a>`
        : '';
      return `<li class="guide-step">
          <span class="guide-step-n">${i + 1}</span>
          <div><b>${s.t}</b><div class="guide-step-b">${s.b}${link}</div></div>
        </li>`;
    }).join('');
    const need = (g.need || []).map(n => `<li>${n}</li>`).join('');
    const notes = (g.notes || []).map(n => `<li>${n}</li>`).join('');
    return `
      <p class="guide-summary">${g.summary}</p>
      <div class="guide-badges">
        <span class="guide-badge guide-badge--kind">${g.kind}</span>
        <span class="guide-badge guide-badge--diff">${g.difficulty} setup</span>
      </div>
      <h4 class="guide-h">What you'll need</h4>
      <ul class="guide-need">${need}</ul>
      <h4 class="guide-h">Step by step</h4>
      <ol class="guide-steps">${steps}</ol>
      <h4 class="guide-h">Where it goes in PawPoller</h4>
      <p class="guide-paste">${g.paste}</p>
      <h4 class="guide-h">Keeping it alive</h4>
      <p class="guide-renew"><b>${g.renew.when}.</b> ${g.renew.how}</p>
      ${notes ? `<h4 class="guide-h">Good to know</h4><ul class="guide-notes">${notes}</ul>` : ''}
    `;
  }

  window.PlatformGuides = { has, get, codes, label, emoji, renderBody };

  /* ── Controller: modal, hub page, connect-card triggers ──── */

  function _escClose(e) { if (e.key === 'Escape') closeModal(); }

  function closeModal() {
    const el = document.getElementById('guide-modal');
    if (el) el.remove();
    document.removeEventListener('keydown', _escClose);
  }

  function openModal(code) {
    if (!has(code)) return;
    closeModal();   // never stack two
    const el = document.createElement('div');
    el.className = 'guide-modal';
    el.id = 'guide-modal';
    el.innerHTML =
      '<div class="guide-modal-card" role="dialog" aria-modal="true" aria-label="How to get started: ' + label(code) + '">' +
        '<div class="guide-modal-head">' +
          '<span class="guide-modal-emoji">' + emoji(code) + '</span>' +
          '<h3 class="guide-modal-title">How to get started: ' + label(code) + '</h3>' +
          '<button class="guide-modal-close" type="button" aria-label="Close">&times;</button>' +
        '</div>' +
        '<div class="guide-modal-body guide-body">' + renderBody(code) + '</div>' +
      '</div>';
    el.addEventListener('click', e => { if (e.target === el) closeModal(); });
    el.querySelector('.guide-modal-close').addEventListener('click', closeModal);
    document.body.appendChild(el);
    document.addEventListener('keydown', _escClose);
  }

  /* Getting Started hub — one card per platform, click opens the guide. */
  function renderHub() {
    const cards = (window.PLATFORMS || []).map(p => {
      const g = GUIDES[p.code];
      if (!g) return '';
      return '<button class="guide-hub-card" type="button" data-guide="' + p.code + '">' +
        '<div class="guide-hub-card-top">' +
          '<span class="guide-hub-card-emoji">' + (p.emoji || '') + '</span>' +
          '<span class="guide-hub-card-name">' + p.label + '</span>' +
        '</div>' +
        '<div class="guide-hub-card-kind">' + g.kind + '</div>' +
        '<div class="guide-hub-card-sum">' + g.summary + '</div>' +
        '<div class="guide-hub-card-diff">' + g.difficulty + ' setup &rarr;</div>' +
      '</button>';
    }).join('');
    const html =
      '<div class="guide-hub-head"><h1 class="guide-hub-title">Getting Started</h1></div>' +
      '<p class="guide-hub-intro">Pick a platform to see exactly how to go from nothing to connected — ' +
      'including how to keep it alive when a cookie or token expires.</p>' +
      '<div class="guide-hub-grid">' + cards + '</div>';
    const app = document.getElementById('app');
    if (app) app.innerHTML = html;
  }

  /* Inject a "Setup guide" button next to each platform's connect/disconnect
     button in the Settings → Platforms pane. Idempotent; run after settings
     render. Covers both connected + disconnected states, plus Inkbunny's
     bespoke save button. */
  function injectSettingsButtons() {
    const pane = document.querySelector('.settings-tab-content[data-tab-content="platforms"]');
    if (!pane) return;
    const btns = pane.querySelectorAll('[id$="-connect-btn"], [id$="-disconnect-btn"], #save-creds-btn');
    btns.forEach(btn => {
      let code = btn.id === 'save-creds-btn' ? 'ib'
        : btn.id.replace(/-(connect|disconnect)-btn$/, '');
      if (code === 'telegram' || !has(code)) return;
      const host = btn.parentElement;
      if (!host || host.querySelector('.guide-trigger[data-guide="' + code + '"]')) return;
      const trigger = document.createElement('button');
      trigger.type = 'button';
      trigger.className = 'guide-trigger';
      trigger.dataset.guide = code;
      trigger.innerHTML = '📖 Setup guide';   // 📖
      btn.insertAdjacentElement('afterend', trigger);
    });
  }

  /* One delegated click handler powers every [data-guide] trigger (hub cards +
     injected connect-card buttons), so nothing needs re-binding after renders. */
  function _init() {
    if (window.__guidesInit) return;
    window.__guidesInit = true;
    document.addEventListener('click', e => {
      const t = e.target.closest && e.target.closest('[data-guide]');
      if (t && t.dataset.guide) { e.preventDefault(); openModal(t.dataset.guide); }
    });
    // Enter/Space on a role="button" span trigger (e.g. the Platforms-hub
    // "Setup guide" chip, which lives inside an <a> so it isn't a real button).
    document.addEventListener('keydown', e => {
      if (e.key !== 'Enter' && e.key !== ' ') return;
      const t = e.target.closest && e.target.closest('[data-guide][role="button"]');
      if (t && t.dataset.guide) { e.preventDefault(); openModal(t.dataset.guide); }
    });
  }
  _init();

  window.Guides = { openModal, closeModal, renderHub, injectSettingsButtons };
})();
