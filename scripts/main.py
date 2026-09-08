"""Entry point: sync both stores and publish the app's dashboard data file.

Both providers run sequentially in ONE process so a single writer builds the
app's pending-list file on R2 (no race), then one Slack notification is sent
for the whole run.
"""

import logging
import os

from common.publish import publish
from providers.appstore import run_appstore_collect
from providers.playstore import run_playstore_collect


LOG = logging.getLogger(__name__)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    project_slug = os.environ.get("PROJECT_SLUG", "").strip()
    LOG.info(
        "Review sync starting: project_slug=%s",
        project_slug or "(PROJECT_SLUG unset -> legacy single-app state names)",
    )

    appstore_entries, appstore_state = run_appstore_collect()
    playstore_entries, playstore_state = run_playstore_collect()
    publish(
        project_slug,
        {"appstore": appstore_state, "playstore": playstore_state},
        appstore_entries + playstore_entries,
    )


if __name__ == "__main__":
    main()
