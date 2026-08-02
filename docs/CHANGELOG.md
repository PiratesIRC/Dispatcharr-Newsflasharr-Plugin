# Changelog

## 1.26.2141913 (August 2, 2026)

First release.

Newsflasharr is a central notification service for Dispatcharr plugins. A
calling plugin makes one call, and this plugin owns everything after it:
routing, deduplication, storm throttling, quiet hours, retry, and delivery to
Discord, a generic webhook, ntfy, Apprise, email, or a banner drawn over live
video.

What it does, in the terms you will notice:

- **A caller makes one call and is finished.** It writes a small file and
  returns. The call never blocks and never raises, so a calling plugin is
  unaffected even when this one is not running.
- **Repeats collapse instead of flooding you.** The same alert arriving over
  and over becomes one message, then a summary when the window closes. An
  alert that gets worse breaks the window and is sent at once, so a warning
  can never swallow the critical that follows it.
- **Quiet hours, an hourly cap and per-channel retry** are configured once
  here rather than separately in every plugin.
- **Your provider hostnames are removed from outgoing messages**, using the
  electronic programme guide sources and accounts Dispatcharr already holds.
- **It is read-only on Dispatcharr.** It writes nothing outside its own
  directory, and it never creates or edits an output profile, a channel or a
  stream.
- **Every action writes its full output to a file**, because the pop-up in
  Dispatcharr holds only about 280 characters and trims from the middle.

Two things to know before relying on it:

- **Delivery is at-least-once per channel.** A duplicate is possible after a
  crash. That is a deliberate tradeoff, described in the caller API.
- **Email is the one channel where a successful send can still be a lie.** A
  mail server accepting a message says nothing about spam filtering. Check the
  inbox and the spam folder before trusting that channel.

Documentation is the [user guide](USER-GUIDE.md) for running it, the
[caller API](API.md) for sending events to it from your own plugin, and the
[developer guide](DEVELOPER-GUIDE.md) for working on it.

Versions before this one were builds that ran on a single installation and
were never published, so there is nothing anyone could have upgraded from.
