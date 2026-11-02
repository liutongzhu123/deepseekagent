import os
import tempfile

# Keep the test run hermetic: no login, no production data directory, no
# outbound WeKnora calls.  The production systemd unit never sets these.
os.environ.setdefault("P0_AUTH_DISABLED", "1")
os.environ.setdefault("P0_COOKIE_SECURE", "0")
os.environ.setdefault("P0_WEKNORA_REQUIRED", "0")
os.environ.setdefault("P0_DATA_DIR", tempfile.mkdtemp(prefix="p0-test-data-"))
