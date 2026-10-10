"""Newsflasharr's usage counter: the Newsflashes Sent total to the plugin-stats Worker.

The total is the count of ledger lines whose outcome is delivered, the same count the
README badge script makes. Only deliver.Deliverer.tick writes those lines, and its
callers report after it returns (collector._tick and plugin._action_flush_held).
run() reports once after each user action. report() never raises.
"""
from .usage_client import UsageReporter, jsonl_sum, load_plugin_settings, with_usage_field

# Must equal plugin.DEFAULT_BASE_DIR + "/notifications.jsonl*"; a test pins it.
LEDGER_GLOB = "/data/newsflasharr/notifications.jsonl*"

USAGE = UsageReporter(
    plugin="newsflasharr",
    counter="newsflashes_sent",
    label="Newsflashes Sent",
    total_fn=lambda: jsonl_sum(LEDGER_GLOB, where={"outcome": "delivered"}),
    settings_fn=lambda: load_plugin_settings("newsflasharr"),
)


USAGE_DESCRIPTION = (
    "Sends the Newsflashes Sent total and a random install id to plugin-stats.dpas.workers.dev, "
    "hourly at most (10 min after a delivery). Your IP address is seen too. See the README. Untick to stop."
)


def report(settings=None, force=False, logger=None):
    """Send if due. settings None reads consent from the database."""
    try:
        USAGE.report(settings, logger, force=force)
    except Exception:
        pass


def fields_with_usage(fields):
    """fields plus the opt-out checkbox. The contract bans help_text (it shadows
    description), so the checkbox text goes in description."""
    out = with_usage_field(fields, USAGE)
    checkbox = out[-1]
    checkbox.pop("help_text", None)
    checkbox["description"] = USAGE_DESCRIPTION
    return out
