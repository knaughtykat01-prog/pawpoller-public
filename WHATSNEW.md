# What's new in PawPoller

Generated from the release notes: each version's plain-language summary.

## [4.88.11] - 2026-11-08 - Security fixes

> - Art and posts found on an account marked "Never post" are no longer offered for import into your Library or Posts.
> - Deleting a piece or a story can no longer reach the whole archive folder by mistake.
> - Saving the Turnstile login protection with the secret box left empty keeps your saved key instead of turning protection off.
> - Turnstile protection now has its own Turn off button.
> - Connecting or adding a Mastodon account only accepts a public server address starting with https.
> - On a hosted PawPoller, the artwork, story and backup folders stay inside its own storage.
> - FurAffinity's journal page and sign-in pages in the desktop app can no longer pop up a Windows message box.

## [4.88.10] - 2026-11-06 - A tighter desktop window

> - The desktop app's window gives its pages only the buttons they use, and only on PawPoller's own pages.
> - Changing your story, artwork or backup folder in the desktop app now asks you in a desktop box first.
> - Restoring a backup keeps this computer's own folders and gallery-dl program instead of taking them from the backup.
> - The desktop app no longer lets a page use your camera, microphone or screen.
> - Adding art from a file in the desktop app takes only a file you picked from your computer.
> - A link in the desktop app's window can no longer start another program on your computer.

## [4.88.9] - 2026-11-05 - Pen names kept apart, small safety fixes

> - A piece that belongs to one pen name can no longer be posted or scheduled on another pen name's account.
> - Your server key is never sent over plain http: pairing, Settings → Publishing and the archive uploads all refuse such an address.
> - On a hosted seat, a mistyped size limit no longer stops moving files from uploading; the normal 20 GB applies.
> - The Publish button on a post's board now works, and says how many sites failed if any did.
> - Publishing a story's draft now uses the account the chapter was posted on, and a chapter number past the end is refused.
> - The setup guide is split: one short guide for each way of installing, and one for after you install.
> - A failed post that is tried again keeps your draft choice and pen name.
> - DeviantArt literature no longer shows the hidden marker lines from your story file.
> - Saving a story in the editor on Windows no longer changes the line endings of the whole file.
> - The Instagram setup guide now goes one click at a time, and every setup guide has tick boxes that remember your place.

## [4.88.8] - 2026-11-04 - Safer versions, the right chapter, small safety fixes

> - In safe mode, an adult version of a piece now stays blurred, in the version strip and after you switch to it.
> - A cleaner version of a picture no longer goes out with the adult picture's thumbnail.
> - Posting chapter 1 of a long story no longer picks up chapter 10's file.
> - Toyhouse sign-ins keep the account's own name, and a refused sign-in no longer leaves an empty account behind.
> - The Inbox no longer fills with every Toyhouse comment ever: ones over a year old count as handled, as on other sites.
> - Moving to another PawPoller: if saving stops part-way, everything is put back; if copying pictures stops, it says why and how to finish.
> - Moving files made from now on are much harder to unlock without the phrase. Older moving files still open.
> - On a hosted seat, a move bigger than the seat's 20 GB is refused before it starts uploading.
> - Pressing Post again (or in a second tab) while the same thing is still posting no longer sends it twice.
> - Scheduling something that's already scheduled for that site now asks before scheduling it a second time.
> - Pictures now go out without their hidden details, like where a photo was taken, the phone or camera, and your art app's name.
> - Your stored pictures are never changed: PawPoller sends a cleaned copy. Turn it off in Settings → Posting.
> - If a picture's details can't be removed, the result, the bell and the posting log say so, and the check before posting warns you first.
> - The desktop app can now move to a different server: choose Change server… in its tray menu or on its "can't reach your server" page.
> - It checks the new address and key before saving anything, and shows which server you're moving to; if something's wrong, nothing changes.
> - Fixed a gap that let pages on your server read the desktop app's own key for it.
> - Links a page opens in a new window now only go to your web browser or email app, never to other programs.
> - Saving Settings → Publishing no longer disconnects a paired desktop.
> - A paired desktop's Never post list and server connection now stay on that device instead of syncing across.

## [4.88.7] - 2026-11-04 - Toyhouse finds all your characters

> - The Toyhouse character list now shows every character, not just the first 18.
> - Posting to a Toyhouse character past the first 18 no longer fails with "not your character".

## [4.88.6] - 2026-11-03 - Toyhouse character picker, and small safety fixes

> - The Toyhouse character list in the post form now has a search box and scrolls, with your picked characters on top.
> - Signing in to Toyhouse through the browser can no longer switch an existing account to a different Toyhouse user.
> - If a Toyhouse sign-in is refused, PawPoller now says why instead of "Sign-in cancelled" or a generic error.
> - A sign-in your server refuses is no longer reported as "server unreachable" and retried.
> - An AO3 or SquidgeWorld update that can't read the work's rating is no longer retried over and over.

## [4.88.5] - 2026-11-02 - SquidgeWorld outages, AO3 ratings, and small safety fixes

> - When SquidgeWorld is down, PawPoller now says the site isn't responding instead of asking you to check your password.
> - A SquidgeWorld sign-in with the wrong password no longer shows as signed in.
> - The same for AO3: while AO3 is down, PawPoller says it isn't responding instead of asking you to check your password.
> - Links in the Inbox can no longer run code when clicked.
> - Signing in to Toyhouse through the browser only saves the sign-in if it belongs to the username you typed.
> - Inkbunny no longer writes any part of your sign-in session to the log.
> - AO3 and SquidgeWorld now post stories at the right rating: Teen and Not Rated stories no longer go up as General.
> - Updating a story on AO3 or SquidgeWorld never lowers the rating of the live work.
> - A post that went up without the site sending back a link is no longer retried, so it can't be posted twice.

## [4.88.4] - 2026-11-01 - Toyhouse, and Inkbunny posts first time

> - New: Toyhouse. Post pictures to your characters, and see each character's favourites, comments and image count.
> - Add Toyhouse from the Accounts page; it isn't offered in the setup steps yet. Toyhouse literature isn't supported yet.
> - Posting a piece now asks which version to post, with the one you're looking at already ticked.
> - Scheduling a piece posts the version you chose, and the scheduled list says which one.
> - A piece's scheduled list names the site each post goes to (it showed only the time).
> - Posting to Inkbunny no longer fails once with a "session" error before going through on the retry.
> - Posting to X now says in the log whether it worked.
> - Scheduling a story's chapters turns away a malformed request straight away.
> - Sending your PawPoller straight to a hosted seat no longer fills the seat's log with an error when the line drops for a moment.
> - Newgrounds milestone alerts now count your Newgrounds work, and Newgrounds activity alerts follow the Newgrounds switch, not the SoFurry one.

## [4.88.3] - 2026-10-29 - Smoother posting flows

> - Scheduling a piece of art for later works again (it stopped with a server error).
> - Posting several pieces at once works out the plan as soon as it opens, so Queue no longer waits for Preview.
> - Posting several pieces at once unticks a default site every piece is already on, and says which.
> - When one chapter can't go to a site, scheduling a story can leave just that chapter off and keep the rest.
> - A failed post's badge now says whether PawPoller will try again by itself, and the failure scrolls into view.
> - Syncing an edit to your sites no longer pops a "new version available" message.
> - On a phone, a box you scroll to no longer hides behind the bottom bar.
> - The time-zone offer no longer covers a dialog's buttons.

## [4.88.2] - 2026-10-28 - Story imports keep your writing

> - Creating a story from a Word .rtf file now brings in the whole text, italics and accents included (it used to come in empty).
> - Creating a story from an .html file keeps italics and bold, and a messy file no longer stalls.
> - Scene breaks written *** or * * * now show as scene breaks on every site, not only ---.
> - Itaku no longer shows as ready to post on the story page when you haven't connected Itaku.
> - Signing in to Itaku from the desktop app now opens Itaku's sign-in page, not a "Page not found".
> - The Accounts page's Itaku link goes to Itaku's sign-in page too.
> - A journal rated Teen now posts as Mature, not General, the same as stories.
> - The tag preview's "sent" and "left out" now add up when a tag is listed twice.
> - When AO3 is busy and asks you to try later, PawPoller now retries the post instead of giving up.

## [4.88.1] - 2026-10-27 - Sites in A–Z order

> - Every list of sites is now in A–Z order, so you can find yours at a glance: setup, Accounts, posting, pickers and Settings.
> - Inkbunny no longer jumps to the top of Settings → Polling; it sits in its place with the rest.
> - Importing a story from SquidgeWorld while a second SquidgeWorld account is being checked no longer mixes the two up.
> - Plain-text story summaries brought in from PostyBirb show symbols like & properly again.
> - Small security tidy-ups in the story editor's error messages and in cancelling a move upload.
> - Each account's sign-in button is now called Sign in (it was Credentials), and on FurAffinity, X, Newgrounds and Itaku it stands out.
> - On a server or hosted seat, the Accounts page now says how to sign in to those four sites with the desktop app.
> - If the desktop app is still connected to an old server address that now only runs the Instagram relay, it says so instead of showing a blank "Not Found" page.

## [4.88.0] - 2026-10-25 - Sign in without copying, and bring in PostyBirb

> - On the desktop app, press Sign in for FurAffinity, X, Newgrounds or Itaku and sign in on the site's own page. Nothing to copy.
> - On a server or hosted seat, press Sign in with the PawPoller app: type the code it shows into the desktop app and sign in there.
> - PawPoller checks the sign-in belongs to the right account before saving it, so a mix-up can't connect the wrong one.
> - Copying cookies is still there under Other ways to connect, and the guides now have the Firefox steps too.
> - Bring your PostyBirb submissions into your Library any time: Settings → Data → Bring in from PostyBirb.
> - Each one keeps its picture, title, description, tags and rating. Anything already in your Library is skipped.
> - On a server or hosted seat, upload a zip of your PostyBirb folder. It is deleted as soon as you finish.
> - Logins from PostyBirb are never ticked for you: tick only the accounts that are yours.
> - New: saved descriptions and tag groups. Drop one into any piece from the description or tags box; manage them in Settings → Saved text.
> - PostyBirb's description templates and tag groups come in too, and PostyBirb descriptions now arrive as plain text, not web code.
> - When you run PawPoller yourself, your account no longer needs an email. Leave it blank and nothing is sent anywhere.
> - Forgot your password with no email? The sign-in page now shows the exact steps to set a new one on the machine PawPoller runs on.
> - On Windows, a new Start menu item, Reset PawPoller password, does it without typing a command.
> - You can add or remove your account email in Settings → Security. Hosted seats still need one.
> - Instagram posts from the desktop app work again for people who use the PawPoller relay. Nothing to change or update.
> - When the relay is down, the error now says so in one plain sentence instead of showing a web page's code.
> - The Overview's charts load faster on a hosted seat: each one now draws about 300 points instead of thousands.
> - When X sends back nothing on two checks in a row, PawPoller now says so plainly and tells you to sign in to X again.
> - Taking your email off a self-hosted PawPoller sends a clear "email removed" message, with how to get back in.
> - The password reset in a terminal refuses to run if it can't hide what you type, and now notices PawPoller open on any port.
> - Small security tidy-ups: plainer error messages, and the Instagram relay counts IPv6 callers fairly.
> - Fixed importing a story from SquidgeWorld, which failed every time.
> - Story imports keep their chapter headings, and a stray "<" in the text no longer swallows the words after it.
> - Podcast alerts in Telegram now show a name and an icon.
> - With two YouTube accounts, the second one no longer picks up the first one's videos and subscriber count.
> - "Publish draft" on a site without drafts now says so instead of showing an error page.
> - Uploading or posting big images and videos no longer pauses the rest of the app while the file loads.

## [4.87.1] - 2026-10-24 - Setup stays open

> - Leaving setup part-way no longer looks like finishing it: a strip at the top says setup isn't done, with a button back.
> - Connecting a site from setup brings you back to setup by itself once the site is connected.
> - Setup now remembers where you got to, even in another tab or after signing in to a site in a new window.
> - "Finish later" on the later setup steps lists what stays as it is; Settings → General asks just those questions later.
> - Every site's setup guide is one tap away: on the setup cards, at the top of the site's settings and in Add account.
> - Post forms list the sites you can post to first. The rest sit under "Other sites", each with a Connect link.
> - Sites you've switched off in Settings no longer appear on post forms or Quick publish.
> - When Inkbunny says "slow down" while PawPoller reads comments, it stops for that check instead of signing in again and again.

## [4.87.0] - 2026-10-24 - Every site reports back

> - Every site you post to now says how it went, in the results and in the bell, even if you closed the page.
> - If a site takes the upload but sends back no link, PawPoller says so and asks you to check the site before posting again. It won't post it twice.
> - When PawPoller gives up retrying a site, the bell says so, with the last reason.
> - A post cut off by PawPoller restarting shows as "not sent" in the bell. Nothing is re-posted on its own.
> - Tags sent to e621 are now written the way e621 wants them: lower case, underscores for spaces.
> - Tags a site won't accept (an accent on e621, a very long tag on Itaku) are named on the result, with the reason.
> - Before you press Publish, the confirmation lists each site that would refuse the piece, and why.

## [4.86.0] - 2026-10-23 - Start here

> - PawPoller now opens on a Start screen that asks what you came to do: post, schedule, write, see your numbers or set up a site.
> - Scheduling a story's chapters is one button on the story's page, in three steps: where, when, then a calendar to check before anything is booked.
> - Chapters can go out every few minutes, hours or days, up to every 28 days. Drag a chapter on the calendar to move just that one.
> - If a site would refuse a chapter (too few tags, say), the first step says so and links to the fix.
> - Each chapter on a story's page has its own Schedule button and shows when it's lined up, with a Cancel.
> - Schedule something works for every kind of posting: a story, its chapters, artwork, a short post or a journal.
> - The menu is regrouped: Post & schedule, Make, Organise and Insights. "Queue & Schedule" is now called Scheduled.
> - Scheduled story posts now keep your "Save as draft" choice. Before, AO3 always saved a draft and Inkbunny and SoFurry always posted publicly.
> - Prefer the old first page? Settings → Appearance → Home screen switches back to the Overview.
> - Links everywhere now use your theme's colour instead of the browser's blue. Start has Library and Settings cards, and its extra links are buttons.
> - Security: PawPoller now requires the certificate update that stops a compromised certificate authority being trusted.

## [4.85.1] - 2026-10-23 - Quick fixes

> - The Promo Maker now has a megaphone icon instead of sparkles, which many people read as "AI".
> - The DeviantArt guide now walks you through copying the cookie for description changes, one step at a time.
> - A link on another website can no longer wipe your saved app and editor drafts through the "Refresh PawPoller" page.
> - Oversized or broken uploads are now turned away straight away, with a clear message instead of an error.
> - Servers that host Instagram pictures for other installs now host pictures only, with tighter limits.
> - Playing a sound no longer leaves an error in the browser console.

## [4.85.0] - 2026-10-22 - Progress everywhere

> - Levels, medals and rewards are now your choice, and off unless you turn them on. PawPoller asks once, when you set up or on your next visit.
> - Turn them on or off any time from the menu under your picture, or in Settings → Appearance. Off hides all of it, Laurels included; nothing you earn is lost.
> - New: every piece in your Library has its own level, shown on its cover. More views, faves and comments raise it.
> - Each story and artwork page has a Laurels card: its level, what the next level needs, and how it compares with your usual pieces.
> - Pieces earn medals of their own, like First comment, On 3 sites and 100 views, and a level-up for a piece earns you XP.
> - Pieces you posted before this update are paid their XP once, in one catch-up note.
> - Each account on the Accounts page shows its level too.
> - Medals are now worked out from every site, not just Inkbunny, and each shows when you earned it.
> - The Overview has a Progress card, and the overnight summary has a Your progress section.
> - Laurels now sits second in the menu and on the phone bar; Analytics moved into More on phones.

## [4.84.0] - 2026-10-21 - Levels

> - New: experience points and levels. Posting, writing, replying to comments and tidying your work all earn points.
> - A bar beside the clock shows your level, and a post's result note shows the points it earned.
> - Levelling up takes over the screen for a moment: your new level slams in with a burst of colour, and every fifth level is gold.
> - The account menu shows your level and your XP toward the next one under your name.
> - Settings → Logs & diagnostics has a Replay level-up button, so you can see the moment again any time.
> - Phones now pick up a fixed app straight away, even when the version number hasn't changed.
> - Points count what you do, never views or faves, so every artist levels up at the same pace.
> - Posting the same piece again earns nothing extra, and each site has a daily limit, so spamming never pays.
> - If you've used PawPoller for a while, you start at the level your history has already earned.
> - Laurels shows your level, the points still needed for the next level, and this week's points. Turn the bar off in Settings → Appearance → Levels.
> - Levels come with rewards: a new title every five levels, and a theme, an avatar ring or a level-up style on every fifth level.
> - New rewards flip in during the level-up, with a Use it now button. A few hidden surprise rewards turn up for things like a four-week streak.
> - Laurels has a Rewards shelf with everything you've earned and what's still to come, plus a history of when you reached each level.
> - You can use a picture as your avatar: pick a piece from your Library or upload one, then crop it to the circle.
> - Rewards are only for show. They never unlock or limit anything PawPoller does.
> - Import from PostyBirb now has a button on the Accounts page and in Settings → Data, so you can run it again any time.
> - Fixed several pop-up boxes (signing out, the age question, PostyBirb import, uninstall) showing with no background behind their text.

## [4.83.1] - 2026-10-21 - Faster Overview and Laurels on hosted seats

> - The Overview loads several times faster on a hosted seat: PawPoller now compresses what it sends.
> - Laurels loads faster, and no longer downloads all its numbers twice after you sign in.
> - Story covers in the Library now load as small card-sized pictures, like artwork already did.
> - Other pages full of numbers and lists load faster from a seat too.

## [4.83.0] - 2026-10-21 - Texture

> - New: optional sounds, off until you turn them on in Settings → Appearance → Sounds. Each device keeps its own setting and volume.
> - A short, quiet sound plays when a post goes up or fails, new faves or comments come in, or you earn a medal.
> - Sounds also play when an account connects, a post is scheduled, an import finishes, or the connection drops.
> - A post that goes up draws a check in its result note.
> - Numbers on the dashboards count up to their new value after a check.
> - A newly earned medal gets a shine, result notes slide in, and buttons press down when clicked.
> - If your computer is set to reduce motion, all of these change at once with no animation.
> - You now get a note when PawPoller loses its connection to the server, and another when it's back.
