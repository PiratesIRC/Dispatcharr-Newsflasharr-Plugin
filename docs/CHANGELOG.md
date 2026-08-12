# Changelog

## 1.26.2241159 (August 12, 2026)

**Documentation corrections for the on-screen ticker.** No code changed in this
release. The user guide printed a reference copy of the Output Profile
parameters that was two releases out of date: it still showed the fixed 5 Mbit/s
encoder and the stream examination limits that were both removed in
1.26.2221153, so anyone pasting it reproduced the picture problems that release
fixed. It also stopped short of the final line, without which the profile fails
at stream start. The block now matches what the Ticker filter button prints, and
a test keeps the two in step.

The guide also now documents the **Banner picture quality** setting, which was
added in 1.26.2221153 and explained nowhere, and records that an Output Profile
you pasted earlier keeps the banner position it was pasted with. If your banner
draws somewhere you did not choose, that is the first thing to check.

## 1.26.2231926 (August 11, 2026)

**Validate settings now names which setting turned each Dispatcharr event on.**
Newsflasharr can notify you about Dispatcharr's own system events, and you
choose them with four checkboxes plus an "Extra events (advanced)" box for
anything the checkboxes do not cover. Validate settings used to report only the
combined result, for example "4 subscribed". That is enough to tell you how many
events are active and nothing about where they came from, so if the contents of
the advanced box were lost you would not notice: the total still looked
reasonable because a checkbox was supplying events of its own.

Validate settings now prints a second line naming each control and what it
contributed, like this:

    'Notify on channel errors/failovers' -> 2 (channel_error, channel_failover); 'Extra events (advanced)' -> 0

A checkbox appears only when it is switched on, because you can already see the
checkboxes in the settings form. The advanced box is always shown, including
when it is empty, because it is the one control whose contents can disappear
without leaving anything to see. A zero next to it means it is empty.

Nothing to configure, and nothing changes about which events you receive. If you
do not use Dispatcharr system events at all, validate settings stays silent about
them exactly as before.

## 1.26.2221153 (August 10, 2026)

**The on-screen ticker was degrading the picture it draws on. This fixes it, and
adds a setting to control the trade.** If you use the banner overlay, every
channel that passed through it was re-encoded at a fixed 5 Mbit/s with no quality
target. Sources commonly arrive at about that rate already, so the overlay was
producing a second-generation encode at the first generation's bitrate. The
result was visible: blocky picture, stuttering, and in the worst cases a channel
that played correctly with the overlay switched off and badly with it on.

The encoder now aims at a picture quality target with a ceiling, rather than at
a fixed bitrate. A new setting, **Banner picture quality**, chooses the trade
between picture and bandwidth. See the user guide for what the three levels mean
and how to pick one.

A second, separate fault was fixed at the same time. The overlay told ffmpeg not
to spend any time examining the incoming stream before decoding it. A decoder
given no time to work out what it is receiving has nothing to decode against,
and it logged decode errors on streams that were arriving perfectly normally.
Those limits are now simply absent, which measured faster to first picture than
either the old setting or a larger one.

**If you already have an output profile for the banner, re-paste it.** Click the
plugin's Ticker filter button, copy what it prints, and paste it over your
profile's parameters, then restart the stream. Nothing updates a profile you
created earlier on its own.

## 1.26.2191208 (August 7, 2026)

**Action output files now work on a stock Dispatcharr installation.** Every
action writes its complete output to a file, because the pop-up toast is
clipped at roughly 280 characters from the middle and cannot show a long
result. That file was always written to `/config/newsflasharr/`, which only
works if your installation mounts a host directory at `/config`. In the stock
image `/config` is owned by root, so the plugin could not write there, and
every action reported `[readout file NOT written: PermissionError/EACCES]`
and produced no file at all. Validate settings was the worst affected, because
its output is far longer than a toast can hold, so there was no way to read it.

The plugin now falls back to `/data/newsflasharr/readouts/<action>.txt`, on the
volume it already uses for its queue, and the card names whichever path it
actually used. Nothing to configure. An installation that does mount `/config`
keeps writing there, so a file you could already open from the host stays where
it was.

Reported by a user. Delivery was never affected by this: notifications were
sent normally throughout, and only the output files were missing.

## 1.26.2171427 (August 5, 2026)

**Show status now lists which plugins send you reports.** One `report sources:`
line names every plugin that has delivered a report through Newsflasharr, with
how long ago its most recent one arrived. The list comes from Newsflasharr's own
record of what it has delivered, so a plugin appears as soon as it sends its
first report and there is nothing to add to any setting.

Where a calling plugin publishes a count of the reports it has written, that
count is shown beside it. The file is
`/data/<source>/report_count.json`, containing `{"reports_built": N}`, and
`docs/API.md` section 8 documents it for plugin authors. A plugin that publishes
no such count is still listed, just without a number, because printing zero for
it would say it has published nothing when the truth is that it publishes no
counter.

Treat that number as a floor rather than an exact total, which is why the
readout repeats the caveat underneath it. It counts reports written rather than
emails delivered, so it still rises while notifications are switched off. Its
writer does not lock the file, so two reports finishing in the same instant can
lose one. It is not stored in a database, so it restarts if that plugin's data
directory is cleared, and it counts only from the day that plugin started
publishing it rather than over its whole history.

**The line is informational and never turns the plugin card red.** Deciding that
a report is overdue remains the job of the report absence check and Validate
settings, judged against the cadence you configured. Two separate places making
that call would be two places that can disagree.

## 1.26.2142011 (August 2, 2026)

**The action output files are no longer readable by other users on the host.**
Files such as `show_redaction.txt` and `validate_settings.txt` print the list
of provider hostnames derived from your own electronic programme guide sources
and accounts, and each says so in its own header. They were being written with
mode 0644, which let any other user on the machine read them.

They are now left at 0600, the mode they are created with. The wider mode had
been set only for cosmetic consistency with another plugin's files in the same
directory, and the note that set it recorded that the narrower mode had already
been measured not to break reading the file from Windows. So it bought nothing.

Found by CodeQL when this plugin was submitted to the Dispatcharr Plugin Hub,
and confirmed by measuring the mode inside a running container before and after.

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
