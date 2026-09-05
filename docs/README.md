# Newsflasharr documentation

Four pages, split by who is reading.

## If you are running Dispatcharr

**[User guide](USER-GUIDE.md)** is the one you want. It covers setting up each
notification channel, writing routing rules, sending a report as an
attachment, quiet hours, hostname redaction, the on-screen banner over live
video, and a troubleshooting ladder for a notification that did not arrive.

**[Changelog](CHANGELOG.md)** lists what changed in each version, described by
what you will notice rather than by which functions moved.

## If you are writing a plugin that sends notifications

**[Caller API](API.md)** is a complete contract on its own. It covers the
`notify()` function, the event schema, what the version guarantees mean, and
the security rules governing what may go into a title or body. You do not need
to read anything else to integrate.

## If you are working on Newsflasharr itself

**[Developer guide](DEVELOPER-GUIDE.md)** covers the delivery pipeline, what
each module owns, the rules that are not obvious from reading the code, how to
run the tests, and how to deploy to a running installation.

---

The **[project front page](../README.md)** describes what the plugin is and
what it does, with screenshots.
