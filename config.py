"""Settings shared by the feed reader and all posting targets."""
import os

USER_AGENT = os.environ.get("USER_AGENT", "mastofeed/1.0")
