# What's new in PawPoller

Generated from the release notes: each version's plain-language summary.

## [4.92.4] - 2026-11-21 - A password reset you can find, and staying signed in longer

> - New in Settings → Security → Staying signed in: choose how long Remember me keeps you signed in, 30 days, 90 days or a year.
> - Copies that update themselves now get "Reset PawPoller password" in the Start menu. Copies installed before late October were missing it.
> - If it still isn't there, the sign-in page now gives a line to paste into the Windows Run box instead.
> - The Windows password reset no longer shows a wall of error text when it turns two-step sign-in off. It always worked; it just looked broken.

## [4.92.3] - 2026-11-21 - A way back in after losing your two-step app

> - Lost your authenticator app and your backup codes? The password reset on your computer now offers to turn two-step sign-in off.
> - The sign-in page now says what to do if you've lost your authenticator app.

## [4.92.2] - 2026-11-20 - Two-step sign-in works with more authenticator apps

> - Two-step sign-in codes now work when they're pasted with a space in the middle, the way many authenticator apps show them ("123 456").
> - Backup codes now fit in the sign-in box. Before, the box cut them short, so a backup code could never sign you in.
> - Turning two-step sign-in on or off now tells you when a code is refused because a clock is wrong, and by roughly how much.
> - Authenticator apps set to show 7- or 8-digit codes, which Authy offers, now work too. Before, the box kept the wrong six digits.

## [4.92.1] - 2026-11-18 - The Publish to list says what it means

> - Scheduling or posting artwork from Start: the piece picker has an Upload new artwork button for when the piece isn't in your Library yet.
> - When a site can't take a piece, the reason is now written under that site in the Publish to list. Before, it only showed on hover, so never on a phone.
> - The Publish to list now starts with a line saying what the ticks, the faded sites and Other sites mean.
> - FurAffinity checks now ask FurAffinity for about a third as many pages, so its bot check should turn PawPoller away much less often.
> - New pieces and your newest 48 are still read at every check. Older FurAffinity pieces now update about once a day.
> - Totals and charts stay steady between checks: a piece that wasn't read keeps its last numbers.
> - Full resync on the FurAffinity page still reads every piece.

## [4.92.0] - 2026-11-17 - Announcements, and alerts you can close

> - PawPoller can now show you a short announcement from us, as one line under the top bar. Press Details to read it and ✕ to close it.
> - Announcements are for things like a site breaking for everyone, a security fix, or an outage. They are plain words only, never a link.
> - PawPoller never asks you to download anything from an address in an announcement.
> - They are fetched from GitHub, which sees your internet address as it does for updates. Nothing else about your copy is sent.
> - You can switch announcements off in Settings, under Announcements.
> - Each alert under Needs attention on the Platforms page now has a ✕. Closed ones fold into one line with Show.
> - A closed alert comes back by itself if the problem changes or happens again. The site's tile still says what is wrong.
> - Fixed Schedule on an artwork's page seeming to do nothing on a phone. The date and time now open right under the button.
> - Messages such as "Tick at least one site" now show next to Publish now and Schedule, not at the bottom of the list.

## [4.91.0] - 2026-11-15 - Bring in the art already on your Patreon page

> - Art already on your Patreon page can now come into your Library. Open the Library, choose Discovered, and press Import.
> - Each Patreon post in that list says who it is for: everyone, all paying members, or the tier's name.
> - Imported Patreon art says "Needs rating" until you rate it. It can't be posted or merged before that.
> - Rate one piece on its page, or tick several in the Library and use Set rating.
> - A piece from a paid post remembers who it was for, and you are told it was a paid post before you send it elsewhere.
> - If a paid post's tier no longer exists, pick who sees the piece before posting it to Patreon again.
> - Patreon text posts, polls and video posts are not imported.
> - On a copy with no password, importing from any site now needs the computer itself or a paired device.
> - If you use a paired desktop, update it too, so it knows which pieces still need a rating.
> - When FurAffinity turns a post down, you now see FurAffinity's own reason instead of a web address.
> - PawPoller no longer re-sends a post FurAffinity turned down, since it would fail the same way.
> - Posting a chaptered story from Telegram no longer puts a copy of the whole story on SquidgeWorld or SoFurry for each chapter.
> - A large drive with plenty of room left is no longer reported as nearly full, which could show a hosted seat as down.

## [4.90.0] - 2026-11-13 - Patreon

> - Patreon is now a posting site: send a picture or a story chapter to Patreon and choose which tiers can see it.
> - PawPoller posts through your own Patreon sign-in, because Patreon has no way for apps to post. Connect it in Accounts.
> - Mature work only goes to paid tiers on Patreon, and Adult work isn't sent to Patreon at all.
> - A locked post keeps its text and pictures for members. Its title, tags and cover are public, so the cover must be General.
> - Bold, italics, links, lists and quotes come through, and General pictures can go in the text with a caption and alt text.
> - A General picture post can be handed to Patreon to publish at a set time, or go to members first and turn public later.
> - Analytics and the Patreon page show your patrons, the money coming in and each tier's numbers.
> - How many patrons left shows only if you switch it on. New patrons always show.
> - Comments on your Patreon posts arrive in the Inbox, and each piece shows its Patreon likes and comments.
> - Patreon posts go out one at a time, at least 90 seconds apart.
> - Not yet: making a new collection for a story, Mature pictures in a post's text, and Patreon's own schedule for Mature work.
> - Patreon posting is new, so check your first few posts on Patreon itself.
> - Hosted seats will be asked to accept their updated terms once: they now mention Patreon figures.
> - On a copy with no password, someone else on your network can no longer delete anything.

## [4.89.0] - 2026-11-09 - Asked at a better moment

> - PawPoller now asks if it can count this copy just after it first checks your sites successfully, not only during sign-up.
> - It's still your choice: nothing is sent unless you say yes, and you can change your mind in Settings → Diagnostics.
> - If you left the box unticked at sign-up you'll be asked once more. A clear yes or No thanks is never asked again.
> - The privacy policy now lists everything a check-in sends, so you'll be asked to accept the new version once.
> - Your choices for check-ins, error reports and the Instagram picture relay now stay on each copy instead of syncing to a paired one.
> - Updating a story on AO3 or SoFurry can no longer add every chapter again when the site's chapter list fails to load.
> - If part of a story update fails on AO3, SquidgeWorld or SoFurry, PawPoller now says so and names the chapter.
> - Choosing "Post and track" in setup now switches posting on. If your scheduled posts never go out, turn on Posting in Settings under Publishing defaults.

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
