"""Print the LF sha256 of AFC Capture's reader files, for afc_tournament_and_scrims/capture_reader SOURCE_SHA256.

    python -I tools/capture_reader_sha.py [path to afc-capture/afc_capture]   (default: the vendored copy)
"""
import hashlib
import os
import sys

FILES = ("tailer.py", "replay.py", "events.py", "weapons.py", "weapons_table.py")
HERE = os.path.dirname(os.path.abspath(__file__))
folder = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "..", "afc_tournament_and_scrims", "capture_reader")
for name in FILES:
    with open(os.path.join(folder, name), "rb") as fh:
        data = fh.read().replace(b"\r\n", b"\n")
    print('    "%s": "%s",' % (name, hashlib.sha256(data).hexdigest()))
