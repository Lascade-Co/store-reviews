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

    app_code = os.environ.get("APP_CODE", "").strip()
    app_details = {
        "appname": os.environ.get("APP_NAME", "").strip() or app_code,
        "appcode": app_code,
        "infisical_slug": os.environ.get("APP_INFISICAL_SLUG", "").strip(),
    }
    LOG.info(
        "Review sync starting: app_code=%s app_name=%s",
        app_code or "(APP_CODE unset -> legacy single-app state names)",
        app_details["appname"],
    )

    appstore_entries, appstore_state = run_appstore_collect()
    playstore_entries, playstore_state = run_playstore_collect()
    publish(
        app_details,
        {"appstore": appstore_state, "playstore": playstore_state},
        appstore_entries + playstore_entries,
    )


if __name__ == "__main__":
    main()
