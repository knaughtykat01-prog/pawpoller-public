# Privacy policy — DRAFT for review

> **Draft, not yet published.** Text marked ⟦like this⟧ is waiting on a fact or a decision. Written in plain
> language on purpose. Not legal advice.

**Effective:** ⟦date of publishing⟧ · **Version:** 1

## The short version

- PawPoller is software you run on your own computer or server. **What you put into it stays there.** We never
  see your art, your stories, your logins or your accounts.
- Our website has no adverts, no trackers and no analytics scripts.
- PawPoller only sends us anything if **you say yes**: anonymous check-ins, error reports, or the Instagram
  picture relay. Each one is off until you turn it on, and you can turn it off at any time.
- Write to **privacy@pawpoller.com** to ask what we hold about you, or to have it deleted.

## Who we are

PawPoller is made by **the PawPoller Project**, run from New South Wales, Australia. For anything about
privacy, write to **privacy@pawpoller.com**.

## What we handle, and why

### When you visit pawpoller.com

The site is hosted by Cloudflare. Like any website host, Cloudflare receives your IP address and basic request
details (the page, your browser) to deliver the page and protect the site from attacks. We don't add anything on
top: no analytics, no advertising, no tracking scripts.

**Cookies:** ⟦if Web Analytics and Bot Fight Mode are both off: "pawpoller.com sets no cookies." · if Bot Fight
Mode is on: "Cloudflare may set one cookie, `__cf_bm`, to tell people from bots. It's needed for the site to work
safely, lasts 30 minutes, and isn't used to track you." · if Web Analytics is on: say so here — it is cookieless,
but it is still a measurement script⟧. Because there are no optional cookies, there's no cookie banner.

### When you download PawPoller or it checks for updates

Downloads and update checks come from GitHub, which receives your IP address to deliver the file. We don't get a
list of who downloaded.

### Only if you say yes

| What | What's sent | Why | Kept for |
|---|---|---|---|
| **Check-ins** ("count this copy") | A random ID for your copy of PawPoller, its version, operating system, how it was installed, which kinds of site you've connected (never which accounts), a size range for your library, and how long it's been running. We also note the country your connection comes from. ⟦confirm: the IP address itself is not stored⟧ | So we know how many people use PawPoller, and on what | 12 months, then only totals are kept |
| **Error reports** | The error and a short excerpt of the log, with logins, tokens, cookies, email addresses, @handles, other people's names and web-address paths removed before it leaves your computer. Your copy's random ID. | So we can fix what breaks | 90 days |
| **Instagram picture relay** | The picture or video you're posting to Instagram, re-encoded, and your IP address (to limit how often one address can use it) | Instagram has to fetch your picture from a public web address, which a home computer doesn't have | The picture: 15 minutes, then deleted. The IP address: ⟦the rate-limit window — 10 minutes⟧ |

You can see exactly what a check-in sends under **Settings → Diagnostics** before you agree. We record **when**
you said yes and **to which wording**, so we can show your consent was real; if the wording changes, we ask
again.

### When you email us

If you write to any @pawpoller.com address, we receive your name, address and message. Mail is forwarded by
Cloudflare to our inbox at Google (Gmail). We keep it for as long as the conversation needs, and delete it when
asked unless we have to keep it (for example, a security report we're still fixing).

## Who else is involved

| Who | What they do | Where |
|---|---|---|
| Cloudflare | Hosts the website and forwards email | Worldwide |
| GitHub | Hosts downloads and the source code | United States |
| Google Cloud | Runs our server (check-ins, error reports, the relay) | United States |
| Google (Gmail) | Our inbox | Worldwide |

We don't sell or rent anyone's information, and we don't share it for advertising.

## Your choices and rights

Wherever you live, you can:

- **see** what we hold about you;
- **have it corrected or deleted**;
- **withdraw your yes** at any time — switch check-ins, error reports or the relay off in Settings; anything
  sent before stays only as long as the table above says;
- **complain** to us, and to your privacy regulator (below).

Check-ins and error reports carry no name, so to find yours, include your copy's random ID, shown under
**Settings → Diagnostics**. We answer within **30 days**.

### If you're in the European Union or the United Kingdom (GDPR)

- **Legal basis.** Check-ins and error reports: your **consent**. The relay: your **consent**, given when you
  switch it on, and then what's needed to post the picture you asked to post. Email: our **legitimate interest**
  in answering you. The website: Cloudflare's and our **legitimate interest** in delivering and protecting it.
- **Your rights** also include data portability and objecting to processing.
- **Transfers.** Our server is in the United States and we're in Australia, neither of which has an EU adequacy
  decision covering this. Data you choose to send goes there on the strength of your consent.
- **Complaints:** your national data protection authority (in the UK, the ICO).

### If you're in Brazil (LGPD)

You have the rights in Article 18 of the LGPD, including confirmation that we process your data, access,
correction, anonymisation or deletion, information about who we share it with, and withdrawal of consent. Our
contact channel for these is **privacy@pawpoller.com**. You can also complain to the ANPD.

### If you're in Australia

We follow the Australian Privacy Principles. If you're not happy with our answer, you can complain to the Office
of the Australian Information Commissioner (OAIC).

### If you're in California

We don't sell or share personal information, and we don't use it for targeted advertising.

## Children

PawPoller can be used by people under 18 for safe-for-work art. Adult features are locked for anyone who tells
the app they're under 18. We don't knowingly collect anything from children beyond what's described above, and
check-ins, error reports and the relay stay off unless they're switched on.

## Security

Our server is kept up to date and scanned for known problems before each release; error reports are scrubbed
before they leave your computer; the relay's links can't be guessed and expire after 15 minutes. If something
goes wrong that affects you, we'll tell you.

## Running your own copy for other people?

If other people use a PawPoller server you run, **you** are responsible for their data, not us. There's a
template to start from in the source code: `docs/PRIVACY_TEMPLATE.md`.

## Changes

We'll date every change here, and big ones will also show in the app's "What's new".

| Version | Date | Change |
|---|---|---|
| 1 | ⟦date⟧ | First version |
