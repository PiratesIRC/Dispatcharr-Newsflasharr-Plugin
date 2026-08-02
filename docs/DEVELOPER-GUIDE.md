# Newsflasharr developer guide

How the plugin is put together, how to run its tests, and the conventions a
change has to satisfy. If your goal is to **send** notifications from your own
plugin rather than to work on this one, you do not need this page: read the
[caller API](API.md) instead, which is a complete contract on its own.

| Also available | |
|---|---|
| [Documentation index](README.md) | every page in one list |
| [Project front page](../README.md) | what the plugin is |
| [User guide](USER-GUIDE.md) | configuring and operating it |
| [Caller API](API.md) | the contract for plugins that send events |

---

## How it runs, and what that rules out

Newsflasharr is a Dispatcharr plugin. It runs **inside Dispatcharr's Django
backend**. There is no build step, no standalone process, and no way to run it
on its own. That shapes everything else on this page:

- The safety net is the unit test suite, because there is no staging
  environment to catch a mistake before an operator does.
- Imports of `apps.*`, `django.*` and `core.utils` resolve only inside
  Dispatcharr. They are deliberately not importable locally, and the tests
  substitute them.
- The deployable code is the inner `newsflasharr/` package. Everything outside it
  is tooling, tests and documentation.

## The pipeline, end to end

A calling plugin never talks to Newsflasharr directly. It writes a file.

```
caller plugin
  notify_client.notify(...)          redacts, writes one JSON file atomically
        |
        v
  /data/newsflasharr/spool/          a directory of pending events
        |
        v
  collector (leader only)            one worker holds an flock and claims work
        |
        v
  pending/  ->  routing  ->  gates  ->  delivery  ->  notifications.jsonl
                   |           |            |
            which channels   dedup,     per channel,
            should get it    storm      with its own
                             throttle,  retry ladder
                             quiet
                             hours
```

Two properties fall out of that shape and are worth stating plainly:

- **`notify()` returning `True` means the event was durably queued, not that
  anyone received it.** Delivery happens later, in another process.
- **Delivery is at-least-once per channel.** A crash or a change of leader can
  produce a duplicate. This is a deliberate tradeoff, documented in the caller
  API, and not a defect to be fixed.

## The modules

Every module is loadable on its own, with no relative imports at the top level,
so a test can load one without dragging in the rest. `plugin.py` is the single
exception, because it is the entry point Dispatcharr itself imports.

| Module | What it does |
|---|---|
| `notify_client.py` | The entire caller surface. Copied byte for byte into each calling plugin. Standard library only, never raises. |
| `spool.py` | Scans the spool, claims an event by renaming it, parses it, and decides what to do with a version it does not recognise. |
| `collector.py` | Elects one leader through a file lock, then ticks: read settings, claim, deliver. The lock is an `flock` rather than anything in Redis, so notifications keep flowing through a Redis outage. |
| `routing.py` | Pure. Decides which channels an event belongs to. No input or output of any kind. |
| `gates.py` | Deduplication, the hourly cap, quiet hours, and the digests that summarise what was collapsed or held. |
| `channels.py` | One sender per destination behind a single `send()` call. |
| `deliver.py` | Runs one tick of the pipeline and owns the per-channel retry ladder. |
| `store.py` | The state file, the outcome ledger, and the failed directory. |
| `ticker.py` | The on-screen banner text file and the ffmpeg filter string. |
| `email_render.py` | Builds the email. Pure rendering, no network. |
| `attachment.py` | Reads a caller-supplied file safely and with a size limit. |
| `dispatcharr.py` | Reads Dispatcharr's own configuration: timezone, and the hostnames to redact. |
| `dispatcharr_events.py` | Subscribes to Dispatcharr's system events. |
| `report_expect.py` | Notices when a scheduled report stops arriving. |
| `readout.py` | Writes each action's full output to a file. |
| `plugin.py` | The Dispatcharr entry point: settings fields and action buttons. |

## Rules that are not obvious from reading the code

These exist because breaking them has caused real faults.

**Only three things an action returns are ever displayed.** `message` becomes a
pop-up that disappears, `error` becomes a persistent red message, and `file`
becomes a path shown to the operator. Anything else you return, including
`status`, is discarded silently. An action that returns a failure status with
no `error` key looks exactly like success.

**The pop-up holds about 280 characters and is trimmed from the middle**, with
nothing to indicate that text was removed. Any output longer than a couple of
lines belongs in a file, surfaced through `file`.

**A settings field with an unrecognised key is dropped silently.** Dispatcharr
validates each field and discards the ones that fail, logging a warning nobody
reads, and the plugin still loads. A dropped field also loses its default, so
the setting sticks at whatever it was. `tests/test_plugin_contract.py` guards
this by checking that every field declared is a field served.

**The plugin card takes its name, version and description from the `Plugin`
class attributes, not from `plugin.json`.** The two can disagree indefinitely
with nothing failing. `scripts/check_version_sync.py` compares them.

**Anything in `__init__`, `fields` or `run` executes on Dispatcharr's
request path.** Keep it cheap, repeatable and free of locks.

**Workers are cooperatively scheduled.** A loop that does not yield blocks
every other request that worker is handling, not just its own.

**Resolve settings from the dictionary passed to `run()`.** Not from a cached
copy, and not from an attribute set on an earlier call, which fails silently on
any path that never set it.

## Running the tests

```sh
pip install -r requirements-dev.txt
python -m pytest -q
ruff check .
python scripts/check_version_sync.py
```

The suite is over 1,300 tests and runs in about 20 seconds. It needs no
database, no network and no Dispatcharr installation. `tests/conftest.py`
substitutes Django and provides a controllable clock, a fake HTTP opener, a
fake mail server, and helpers that load a module by path.

Continuous integration runs exactly those four commands, so a green run locally
means a green run there.

### Writing a test that is worth having

- **Test the wiring, not only the helper.** Several real gaps have hidden
  behind tests that fed state directly to the consumer while nothing exercised
  the producer.
- **A guard that has only ever passed proves nothing.** Break it deliberately,
  watch it fail, then restore it. Every fix in this repository is expected to
  have done that.
- **Assert on the artifact, not the return value.** Actions are written not to
  raise, so a successful return says only that the code finished.

## Making a change

1. Work on a branch.
2. Write the failing test first, and run it to confirm it fails for the reason
   you expect.
3. Make the change and run the whole suite, not only your own test.
4. Bump the version with `python bump_version.py`. Hot reloading keys on the
   manifest, so a change shipped without a version bump leaves the old code
   running in memory.
5. Add a changelog entry describing the behaviour an operator will notice.

### Conventions in committed text

- **No em dashes** anywhere an operator will read the text.
- **No contractions in code or comments.** Write "does not" and "cannot".
  Possessive forms are fine.
- A comment should say why the code is the way it is. What it does is already
  visible.

## Deploying to a running installation

The package directory is copied into `/data/plugins/newsflasharr/` inside the
Dispatcharr container, owned by the same user the workers run as.

```sh
git archive HEAD:notifier | docker exec -i <container> \
  sh -c 'tar -x -C /data/plugins/newsflasharr'
```

Building from the git index rather than copying the working directory matters
for two reasons: it fixes line endings, and it cannot ship files that are
ignored by git and were never meant to leave your machine.

Three things to know afterwards:

- **A code-only change needs no restart.** Bumping the version updates the
  manifest, which is what hot reloading watches. A restart interrupts every
  stream in progress and is only needed when a scheduled task changed.
- **Opening the plugins page is not always enough to load a new build.** Use
  the refresh control on that page and confirm the card shows the new version.
- **Verify by comparing file contents, not by comparing version numbers.** A
  matching version string is not evidence of matching code.

## A starting prompt, if you use an AI assistant

Plenty of Dispatcharr plugins are written with help from a coding assistant.
The prompt below gives one enough context to add Newsflasharr support without
guessing, because the mistakes it lists are the ones that fail quietly rather
than raising.

Paste it into a session opened **in your own plugin's directory**, replacing
the two bracketed values. Treat what comes back as a draft to review, not as
finished work.

```text
Add Newsflasharr notification support to [YOUR PLUGIN NAME].

Read https://github.com/PiratesIRC/Dispatcharr-Newsflasharr-Plugin/blob/main/docs/API.md
first. It is the complete contract and it is short. Follow it rather than
inferring an interface from this prompt.

What to build:

1. Vendor notify_client.py from that repository into my plugin, UNCHANGED. It
   is standard library only and never raises. Do not edit it, do not
   reimplement it, and do not import anything from Newsflasharr itself.

2. Add ONE boolean setting that turns notifications on, defaulting to off.
   Resolve it from the settings dictionary passed to run(), not from a cached
   attribute and not from a snapshot. A setting read from an attribute that
   some entry path never set fails silently: no error, no notification.

3. Call notify() where the interesting thing happens. Use source
   "[MY SOURCE NAME]" for every call.

Rules that are easy to get wrong, all of which fail quietly:

- The severity vocabulary is exactly critical, warning, info. Any other
  string is silently converted to warning, so "error" or "high" will not
  fail, it will just be wrong.
- source is what every routing rule the operator writes matches on, and the
  match is case-sensitive. Choose it once and never change it. Renaming it
  later silently orphans their rules.
- notify() returning True means the event was written to a queue, NOT that
  anyone received it. Never report delivery to a user based on that value.
- Never call notify() from a plugin's __init__. That runs on Dispatcharr's
  request path, in every worker.
- Never put a credential, a provider hostname, or a full stream URL in a
  title or body. They are sent to an external service.
- If the same condition can fire repeatedly, pass a stable dedup_key so
  repeats collapse into one message instead of flooding the operator.

When you are done, tell me which routing rule the operator has to add in
Newsflasharr for these events to go anywhere, and do not try to change
Newsflasharr's own settings yourself.
```

## Reporting a problem

Open an issue with the plugin version from the card, what you expected, what
happened, and the output file from the relevant action. **Read that file
first.** Some of them list hostnames taken from your own configuration, and
every one of them says so at the top.
