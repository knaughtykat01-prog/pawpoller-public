# Installer code-signing — status & decision

**Status:** PawPoller's desktop installers are **not code-signed.** This is a deliberate, documented decision, not an
oversight. This note explains what that means for you and what the options are.

## What "unsigned" means for you

- **Windows** (`PawPoller-Setup-x.y.z.exe`): SmartScreen will show a blue *"Windows protected your PC"* warning on
  first run. Click **More info → Run anyway**. The warning fades as more people run a given build (reputation), but a
  freshly-built unsigned exe always trips it.
- **Linux** (`.AppImage`): no signing ecosystem expectation; `chmod +x` and run. (No warning.)
- **macOS**: not currently built; if it were, Gatekeeper would block an unsigned/un-notarised app harder than Windows.

None of this affects what the app *does* — it's about the OS trusting the publisher. Verify a download by matching its
SHA-256 against the checksum on the GitHub release instead.

## Why it's unsigned today

- An **EV code-signing certificate** (the kind that clears SmartScreen immediately) costs ~US$300–600/year and
  requires a registered business identity + hardware token. PawPoller is a free, single-maintainer, MIT project.
- A **standard OV certificate** (~US$100/year) signs the binary but still needs to build SmartScreen reputation, so it
  only partly removes the warning.
- Signing cannot be done from this repo/CI without a certificate + private key held by the maintainer.

## Options, if signing becomes worth it

1. **Do nothing (current).** Document the SmartScreen step (done, above) + publish SHA-256 checksums. Best fit for a
   free hobby project.
2. **OV certificate** in CI (e.g. via an Azure Trusted Signing / a cert in a GitHub secret). Removes the "unknown
   publisher" text; reputation still accrues over time. Moderate cost + setup.
3. **EV certificate.** Immediate SmartScreen pass. Highest cost; needs a business entity + hardware/cloud HSM.
4. **Sigstore / cosign** for the AppImage + checksums — free, verifiable provenance, but not something end users' OSes
   check automatically.

## Recommendation

Stay on **option 1** while PawPoller is a free single-maintainer project: keep the SmartScreen step documented and
publish per-asset SHA-256 checksums with each release so security-conscious users can verify. Revisit **option 2**
only if a real distribution/reputation need emerges (e.g. many non-technical self-hosters hitting the warning).
