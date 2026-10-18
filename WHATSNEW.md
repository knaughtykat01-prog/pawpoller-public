# What's new in PawPoller

Generated from the release notes: each version's plain-language summary.

## [4.81.1] - 2026-10-19 - Fresh copy

> - After an update, PawPoller now replaces the old copy saved on your phone or computer by itself, so you don't get stuck on an old version.
> - If the app can't reach the server even though the server is fine, it clears its saved copy and reloads once.
> - "Server unreachable" messages now offer "Refresh PawPoller on this device". It keeps you signed in.

## [4.81.0] - 2026-10-19 - Twitch

> - Twitch is a new site to watch: see when you're live, your past broadcasts and clips, and, once you connect Twitch, your followers, subscribers and Bits.
> - Go-live posts: when you go live on Twitch, PawPoller can post your own words to Bluesky, Mastodon, X, Threads, Tumblr, Instagram, Telegram, Facebook and your Discord channel.
> - "Show me first" is on to begin with. You get a Telegram message with Post and Skip buttons, plus a note in the bell, and nothing posts until you say so.
> - A dropped stream that restarts within the cool-down counts as the same stream, so nothing posts twice.
> - When the stream ends, PawPoller can change the Discord, Mastodon and Telegram posts to your "it's over" words.
> - Go-live posts are new and still being tested on real streams.

## [4.80.1] - 2026-10-18 - Tidy alerts

> - This is the release that brings you the 4.80.0 alert changes below.
> - Fixed a check that stopped the 4.80.0 downloads from being built.

## [4.80.0] - 2026-10-17 - Tidy alerts

> - Every alert opens with the site's name and the account, in the same order, with each name shown once.
> - "1 Deviation Updated" is gone: alerts now say what changed, like "+2 faves, +1 comment".
> - Alerts use the title from your Library, not a folder name or a line the site made up from your description.
> - One publish to several sites sends one message listing every site, with links.
> - Poll failure alerts say which account failed.
> - No two sites share an icon any more.
> - Digests leave out empty lines, and a new account's first digest no longer counts its whole history as one week's growth.
> - Polls on big galleries finish their milestone check faster.
> - Also includes the 4.79.2 fixes below: the What's-new popup, account names in the log, and Telegram failures now logged.

## [4.79.2] - 2026-10-17 - Small fixes

> - The "What's new" popup works again on hosted seats and downloaded installs.
> - The server log now names the account on every line a poll writes, so you can tell whose poll did what.
> - Clearer wording for the Cloudflare relay switches in Settings.
> - Telegram notifications that Telegram turns away are now noted in the log instead of vanishing. Over-long digests are trimmed to fit, and alerts with odd characters in a title arrive as plain text.

## [4.79.1] - 2026-10-16 - Small fixes

> - Changing or removing your DeviantArt cookie now takes effect straight away, without a restart.
> - A small security tidy-up to how AO3 error messages are read.
> - Hosted seats: FurAffinity polling can go through the Cloudflare relay (the FA relay switch in Settings), for when FA's bot check blocks the home connection.

## [4.79.0] - 2026-10-16 - Replies from X

> - Replies to your X posts now arrive in the Inbox, and you can answer them from there.
> - Fixed the Inbox site filter: after you filter to one site and answer everything there, the other sites stay in the list.
> - The DeviantArt Test no longer says your cookie has expired when DeviantArt simply didn't answer.
> - Small security tidy-ups to AO3 and SquidgeWorld page reading, moving to a hosted seat, and artwork syncing.

## [4.78.1] - 2026-10-16 - AO3 posting fixed

> - Fixed posting new works to AO3 (and SquidgeWorld), which failed with "auth_error" whenever the site was showing a banner.
> - When AO3 turns a work down (a missing title, a tag too long), PawPoller now says why and doesn't keep retrying it.

## [4.78.0] - 2026-10-16 - Back on AO3

> - AO3 works again: PawPoller now connects the way a current web browser does, so AO3's bot protection stops turning it away.
> - The AO3 Test button always answers within a minute, and says plainly when AO3 isn't responding instead of showing a Cloudflare error page.
> - Hosted seats: the server operator can now pause one site for every seat at once, for example while a site asks tools to stop. A paused site shows as skipped, "paused by the server operator".
> - Small security tidy-ups to Rule34 connecting, file names on exports and the Inbox.

## [4.77.6] - 2026-10-16 - FurAffinity bot checks named, and waited out

> - When FurAffinity's bot protection blocks PawPoller, it now says so, instead of wrongly saying your cookies expired.
> - The Test button shows "FurAffinity bot check" in that case: your login is fine, so there's no need to paste new cookies.
> - While FurAffinity is blocking, PawPoller pauses FA checks and tries again later, waiting longer each time (up to a day), as FurAffinity asks.
> - You get one message when FA starts blocking and one when it's reachable again.

## [4.77.5] - 2026-10-16 - X waits out its limit properly

> - X polls that hit X's rate limit now wait for it to reset instead of giving up halfway and saving only part of your posts.
> - If X still hasn't reset in time, the poll is marked as limited and tries again next time, instead of making things worse.
> - Fixed the weekly Telegram digest, which stopped sending once a Telegram channel or Facebook page had stats.

## [4.77.4] - 2026-10-15 - X polls say which way they got in

> - X poll history has a new Source column: it shows gallery-dl when the normal way worked, and a warning when PawPoller had to fall back (gallery-dl failed or isn't installed).
> - X settings show which source the last poll actually used.
> - X polls use fewer requests: the separate login check before each poll is gone; the poll itself now says when the X cookies need renewing.
> - X no longer keeps other people's reposts, even when they tag you. A repost of your own post counts as that post, and a quote keeps its own numbers.
> - The update check asks GitHub far less often, so seats sharing one internet connection no longer hit GitHub's hourly limit.

## [4.77.3] - 2026-10-15 - The moving guide is on Getting Started

> - Getting Started now has the "Moving to a hosted seat" guide at the top, so it's easy to find before you move.
> - The moving guide ends with a "Check your sites" step: test each account, and the two extra things DeviantArt needs on a new address.
> - After a move, the rate watch only counts checks made on the new PawPoller, so the old copy's history no longer sets off an alert.

## [4.77.2] - 2026-10-15 - Sync says when part of it didn't happen

> - A sync that only partly worked now says so. For example, DeviantArt can't change a description without your sign-in cookie, and the result now tells you that instead of just "Synced".
> - The same goes for Weasyl when it can't replace the picture file itself.
> - The Test button on a DeviantArt account now also checks whether descriptions can sync, and warns you if they can't.
> - DeviantArt description problems now come with a code, DA-COOKIE-MISSING or DA-COOKIE-EXPIRED, and the DeviantArt guide says what to do for each.

## [4.77.1] - 2026-10-15 - DeviantArt edits work again

> - Fixed editing artwork on DeviantArt: it always failed with "isn't in … gallery", whatever the username.
> - DeviantArt edits now go through DeviantArt's official connection, the same one polling already uses.

## [4.77.0] - 2026-10-15 - Sync one site at a time

> - Each site under an artwork's "Published to" list has its own Sync button, so you can update one site without touching the others.
> - Sites whose live title no longer matches your record are flagged "title differs".
> - When PawPoller can't find a deviation to edit, it now says which one and whose gallery it looked in, instead of DeviantArt's "Api endpoint not found".

## [4.76.1] - 2026-10-15 - Apostrophes on DeviantArt, and the account Test message

> - Fixed apostrophes and quotes showing as code (like isn&amp;#x27;t) in descriptions posted to DeviantArt, Newgrounds and journals.
> - A long Test message on the Accounts page now wraps, so it no longer pushes the Authorise posting button off the screen.
> - DeviantArt's "re-authorise" message now points at the right button.

## [4.76.0] - 2026-10-15 - Inbox clean-up

> - Fixed the Send to my hosted seat card showing its "Is this your seat?" and "Carry on sending" buttons when they didn't apply.
> - Fixed the account menu opening behind the Library's filter bar, and the filter bar sliding over the top bar when you scroll.
> - The Library's filter row is tidier: the sort and filter boxes stay together on the right, and the search box takes the spare room.
> - The Inbox has a Handle all button, for clearing a big pile of old comments in one go.
> - Comments more than a year old no longer count as "to answer" in the Inbox.
> - DeviantArt comments in the Inbox no longer show bits of web code like <br />.
> - PawPoller now says it's a beta next to its version number, for example "4.76.0 Beta".

## [4.75.0] - 2026-10-15 - Send straight to your hosted seat

> - Moving into a hosted seat no longer needs a file: your seat shows a one-time code, you type it into your old PawPoller, and it sends everything straight there.
> - Everything on the way is locked with that code, and the code itself never leaves either PawPoller.
> - If your internet drops, sending carries on by itself; if PawPoller was closed, press Carry on sending.
> - The moving file still works when you're moving somewhere that isn't a seat.
> - Also from 4.74.1: the first-run setup on a hosted seat no longer says "Every 1 hours", and a small privacy tidy-up in what a hosted seat reports to the Tech Centre.

## [4.74.1] - 2026-10-15 - Two small fixes from the 4.74.0 review

> - The first-run setup on a hosted seat no longer says "Every 1 hours".
> - A small privacy tidy-up in what a hosted seat reports to the Tech Centre.
