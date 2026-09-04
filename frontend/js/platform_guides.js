/* Platform "How to get started" guides — 2.65.0; per-step screenshots 4.6.4.
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
 *   steps       [{t, b, link?, img?}] ordered walk-through (b may contain simple HTML;
 *               img = {src, alt} shows a screenshot under the step, click opens it full size)
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
      need: ['A Threads account (public)', 'A free Meta developer app — the same one Instagram uses, if you have that too', 'A long-lived access token'],
      steps: [
        { t: 'Create (or open) a Meta developer app', b: 'On <b>My Apps</b> press <b>Create app</b>. Name it anything, and on the <b>Use cases</b> step tick <b>Access the Threads API</b> (tick <i>Manage messaging &amp; content on Instagram</i> at the same time if you want Instagram). No business portfolio is needed; finish the wizard. Already have the app? Open it and use <b>Add use cases</b>.',
          link: { label: 'developers.facebook.com/apps', url: 'https://developers.facebook.com/apps' },
          img: { src: '/img/guides/meta/create-app.png', alt: 'Create an app: the Use cases step, with Access the Threads API and Manage messaging &amp; content on Instagram' } },
        { t: 'Open the Threads use case', b: 'Dashboard &rarr; <b>Use cases</b> &rarr; <b>Customize</b> next to <i>Access the Threads API</i>. <code>threads_basic</code> is already in the permissions table. Adding <code>threads_manage_insights</code> is optional: on the app this guide was written from it was never added and the stats still arrive — that table is about App Review, not your own tester token.',
          img: { src: '/img/guides/meta/threads-permissions.png', alt: 'The Threads use case permissions table: threads_basic Ready for testing, the rest with an Add button' } },
        { t: 'Add yourself as a Threads Tester', b: 'On the use case\'s <b>Settings</b> tab press <b>Add or Remove Threads Testers</b> (it is the same page as <b>App roles &rarr; Roles</b>). <b>Add People</b> &rarr; pick <b>Threads Tester</b> under <i>Additional roles</i> &rarr; type your Threads username.',
          img: { src: '/img/guides/meta/roles-add-people.png', alt: 'Add people to your app: Instagram Tester and Threads Tester under Additional roles' } },
        { t: 'Accept the invite inside Threads', b: 'In the Threads app or on threads.net: <b>Settings &rarr; Account &rarr; Website permissions</b> &rarr; accept the tester invite. Until you do, the token generator will not list you.' },
        { t: 'Generate the long-lived token', b: 'Back on the use case\'s <b>Settings</b> tab, under <b>User Token Generator</b> your username now has a <b>Generate Access Token</b> button. Approve what it asks for, then copy the whole token — it is several hundred characters.',
          img: { src: '/img/guides/meta/threads-settings.png', alt: 'Threads use case Settings tab: display name, empty callback URLs, and the User Token Generator with a Generate Access Token button' } },
        { t: 'Connect in PawPoller', b: 'Settings &rarr; Platforms &rarr; Threads: paste the token, leave the user ID blank, press <b>Connect</b>. PawPoller reads your username back from the token — check it is the account you meant.',
          img: { src: '/img/guides/meta/pawpoller-threads-card.png', alt: 'PawPoller Settings, Threads card: access token, optional user id, Connect' } },
      ],
      paste: 'Settings → Threads → Access token (user id optional — leave blank)',
      renew: { when: 'Long-lived tokens last ~60 days', how: 'PawPoller auto-refreshes the token while it\'s polling. If it goes unused past ~60 days and lapses, generate a fresh one on the same Settings tab and paste it in — the app, tester role and use case all stay valid.' },
      notes: [
        'Do the whole setup on a <b>desktop</b> (not mobile) in <b>Microsoft Edge — or any browser that is NOT Chrome</b>. Chrome reliably breaks Meta\'s developer dashboard and token generator: the flow silently fails or the token never appears. Edge/Firefox work.',
        'Leave the app <b>Unpublished</b>. Development mode with your own account needs no App Review, and the Publish button stays greyed out until you add a privacy policy — you do not need one.',
        'Meta adds <b>Facebook Login for Business</b> and a <b>Manage everything on your Page</b> use case by itself. PawPoller never uses either; ignore them.',
        'Meta gates the API behind App Review for public use and removes adult content — for your own account in Development mode it works without review.',
      ],
    },

    // ── Instagram ────────────────────────────────────────────
    ig: {
      kind: 'Analytics + posting', difficulty: 'Involved',
      summary: 'Track views/reach/likes/comments/saves and post photos to Instagram.',
      need: ['A Business or Creator Instagram account', 'A free Meta developer app — the same one Threads uses, if you have that too', 'A long-lived access token'],
      steps: [
        { t: 'Switch to a professional account', b: 'In the Instagram app: <b>Settings &rarr; Account type and tools &rarr; Switch to professional account</b> (Business or Creator). Personal accounts can\'t use the API. You can skip connecting a Facebook Page.' },
        { t: 'Create (or open) a Meta developer app', b: 'On <b>My Apps</b> press <b>Create app</b>. Name it anything, and on the <b>Use cases</b> step tick <b>Manage messaging &amp; content on Instagram</b> (tick <i>Access the Threads API</i> too if you want Threads). No business portfolio is needed; finish the wizard. Already have the app? Open it and use <b>Add use cases</b>.',
          link: { label: 'developers.facebook.com/apps', url: 'https://developers.facebook.com/apps' },
          img: { src: '/img/guides/meta/create-app.png', alt: 'Create an app: the Use cases step, with Access the Threads API and Manage messaging &amp; content on Instagram' } },
        { t: 'Open the Instagram use case &rarr; API setup with Instagram login', b: 'Dashboard &rarr; <b>Use cases</b> &rarr; <b>Customize</b> next to <i>Manage messaging &amp; content on Instagram</i> &rarr; left tab <b>API setup with Instagram login</b>. Not <i>with Facebook login</i> — that one needs a Facebook Page. Meta\'s welcome box says to switch for insights; ignore it, PawPoller\'s stats work here.',
          img: { src: '/img/guides/meta/instagram-api-setup.png', alt: 'API setup with Instagram login: app name and id, section 1 messaging permissions, section 2 Generate access tokens with one tester account and a Generate token link' } },
        { t: 'Add yourself as an Instagram Tester', b: '<b>App roles &rarr; Roles &rarr; Add People</b> &rarr; pick <b>Instagram Tester</b> under <i>Additional roles</i> &rarr; your Instagram username. Then accept the invite in Instagram: <b>Settings &rarr; Apps and websites &rarr; Tester invites</b>. Until you accept, the token step will not list your account.',
          img: { src: '/img/guides/meta/roles-testers.png', alt: 'App roles: one Administrator, one Instagram Tester and one Threads Tester, with the Add People button' } },
        { t: 'Generate the token', b: 'Back on <b>API setup with Instagram login</b>, section <b>2. Generate access tokens</b>: press <b>Add account</b> if yours is not listed, then <b>Generate token</b>. Approve everything it asks for — the app asks for the basic, insights, publishing, comments and messages scopes in one go — and copy the whole token. Skip sections 1, 3 and 5: messaging permissions, webhooks and App Review are not needed.' },
        { t: 'Connect in PawPoller', b: 'Settings &rarr; Platforms &rarr; Instagram: paste the token, leave the user ID blank, press <b>Connect</b>. PawPoller reads your username back from the token — check it is the account you meant.',
          img: { src: '/img/guides/meta/pawpoller-instagram-card.png', alt: 'PawPoller Settings, Instagram card: access token, optional user id, Connect' } },
      ],
      paste: 'Settings → Instagram → Access token (user id optional — leave blank)',
      renew: { when: 'Long-lived tokens last ~60 days', how: 'PawPoller auto-refreshes it while polling. If it lapses, generate a fresh token in section 2 of <i>API setup with Instagram login</i> and paste it back in — the app, tester role and use case all stay valid.' },
      notes: [
        'Do the whole setup on a <b>desktop</b> (not mobile) in <b>Microsoft Edge — or any browser that is NOT Chrome</b>. Chrome reliably breaks Meta\'s developer dashboard and token generator: the flow silently fails or the token never appears. Edge/Firefox work.',
        'The <b>Permissions and features</b> table is about App Review. On the app this guide was written from, <code>instagram_business_manage_insights</code> and <code>instagram_business_content_publish</code> were never "added" there and stats arrive anyway: the token generator asks for all five Instagram scopes regardless. Add them if you like; it costs nothing.',
        'Every Instagram post <b>requires a photo</b> — there are no text-only posts. Instagram fetches the image from a public address, and PawPoller finds it one: your own server if you have one, otherwise the PawPoller relay (a public PawPoller server hosts the picture for 15 minutes), otherwise a temporary tunnel from your PC. Nothing to set up; see <b>Settings → Posting → Instagram image host</b> to switch either off or to download the tunnel helper.',
        'Leave the app <b>Unpublished</b>. Development mode with your own account needs no App Review, and the Publish button stays greyed out until you add a privacy policy — you do not need one. A public app for other users would need review and would likely be rejected for adult content.',
      ],
    },

    // ── Telegram ─────────────────────────────────────────────
    tg: {
      kind: 'Analytics + posting', difficulty: 'Easy',
      summary: 'Broadcast art and story announcements to a Telegram channel you run, and track the reactions and subscribers they earn.',
      need: ['A Telegram channel you own', 'A bot, made in 30 seconds', 'The bot added to the channel as an admin'],
      steps: [
        { t: 'Create a bot', b: 'Message <b>@BotFather</b> on Telegram and send <code>/newbot</code>. Give it a name and a username ending in <code>bot</code>. He replies with a <b>token</b> — a long string like <code>123456789:AAHk…</code>. That whole string is the token.',
          link: { label: '@BotFather', url: 'https://t.me/BotFather' } },
        { t: 'Create your channel', b: 'In Telegram: <b>New Channel</b>. Public or private both work — a public channel gets a <code>@username</code>, a private one does not.' },
        { t: 'Add the bot as an admin', b: 'Channel &rarr; <b>Administrators</b> &rarr; <b>Add Admin</b> &rarr; your bot. ⚠ <b>Tick "Post Messages"</b> — admin rights are individual toggles, and a bot can be an admin and still not be allowed to post. This is the step people miss.' },
        { t: 'Post one message in the channel', b: 'Anything at all. An admin bot receives channel posts, which is how PawPoller can find the channel’s id for you.' },
        { t: 'Connect in PawPoller', b: 'Settings &rarr; Telegram &rarr; paste the bot token, then press <b>🔍 Find my channel</b>. It fills the channel in. Then <b>Save &amp; send test</b> — a real message lands in the channel, and PawPoller tells you <i>which</i> channel it reached.' },
      ],
      paste: 'Settings → Telegram → Channel posting',
      renew: { when: 'Bot tokens don’t expire', how: 'Only if you revoke one with /revoke in BotFather — then paste the new token back in.' },
      notes: [
        '⚠ <b>A private channel has no <code>@username</code>.</b> Its title is not a handle. It can only be reached by a numeric <code>-100…</code> id, which Telegram never shows you — that is what <b>Find my channel</b> fetches. A <code>t.me/+…</code> invite link is not a handle either, and a bot cannot join by invite link at all.',
        '⚠ <b>Typing a bare name is risky.</b> A bare word is treated as a public <code>@username</code>, and someone else may already own it. PawPoller now tells you which channel it actually reached, so check that name is yours.',
        '<b>Reactions are counted from the day you switch tracking on.</b> Telegram <i>pushes</i> reactions and offers no way to ask for them, so anything posted before then shows as <i>not counted</i> rather than as zero — a real absence of measurement, not an absence of interest.',
        '<b>No view counts, ever.</b> The eye-count you see on a channel post is not in the bot API at all. It is not a gap PawPoller can close later.',
        '<b>Posts are never edited.</b> Telegram’s API refuses to edit anything older than 48 hours, so PawPoller leaves a Telegram post alone and skips it when syncing corrections elsewhere.',
        '<b>Stories are announced, not posted.</b> A Telegram message caps at ~700 words, so a story goes out as a cover, a blurb, and links to where it is actually published.',
        '<b>Full control per piece.</b> Blur, hashtags, caption, forwarding, full quality, silent and pin can be set channel-wide in Settings and overridden on any single artwork or story.',
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
      const img = s.img && s.img.src
        ? `<a class="guide-fig" href="${s.img.src}" target="_blank" rel="noopener" title="Open full size"><img src="${s.img.src}" alt="${s.img.alt || ''}" loading="lazy"></a>`
        : '';
      return `<li class="guide-step">
          <span class="guide-step-n">${i + 1}</span>
          <div class="guide-step-body"><b>${s.t}</b><div class="guide-step-b">${s.b}${link}</div>${img}</div>
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
