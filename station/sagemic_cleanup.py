"""
Free up space on the Pi by deleting detection clips the hub already has.

Runs from a systemd timer (hourly). Only clips listed in delivered_clips.log -
i.e. confirmed SAVED by the hub - are ever deleted. Clips that
were sent but not confirmed, or not sent at all, are never touched.
(CLEANUP: REQUIRE_DELIVERY: false falls back to "sent to the hub".)

1. Deletes sent clips older than KEEP_SENT_DAYS.
2. If free disk space is still below MIN_FREE_GB, deletes the oldest sent
   clips (any age) until there is enough room again.
3. Removes leftover *.flac.tmp files from interrupted saves, and empty
   date folders.

Settings (optional) in config.yaml:

    CLEANUP:
      KEEP_SENT_DAYS: 7
      MIN_FREE_GB: 2

4. Shortens sent_clips.log and delivered_clips.log to the clips still on the
   Pi, so the logs (read by the sender every minute) do not grow forever. The
   lines taken out of delivered_clips.log are added to
   delivered_clips_archive.log first, which keeps every clip the hub confirmed
   (never shortened; nothing reads it every minute).

Usage:
    python sagemic_cleanup.py --config config/config.yaml [--dry-run]
"""

import argparse
import os
import shutil
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import yaml

try:
    import fcntl
except ImportError:   # not on Windows (tests only)
    fcntl = None


def get_config(path):
    """config.yaml as a dict (read directly: sagemic.helpers would also load
    the audio libraries)."""
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def log_lock(base_path):
    """Exclusive lock shared by everything that writes sent_clips.log or
    delivered_clips.log (sender, agent, cleanup), so cleanup can rewrite a
    log without losing a line appended at the same moment."""
    f = open(os.path.join(base_path, ".clip_logs.lock"), "a", encoding="utf-8")
    if fcntl:
        fcntl.flock(f, fcntl.LOCK_EX)
    return f   # closing it releases the lock


def free_gb(path):
    """Free disk space (GB) on the filesystem holding path."""
    return shutil.disk_usage(path).free / 1e9


def read_sent(log_file):
    """Set of clip paths that sagemic_scansend.py confirmed as sent."""
    if not os.path.exists(log_file):
        return set()
    with open(log_file, "r", encoding="utf-8") as f:
        return {line.split("\t")[0].strip() for line in f if line.strip()}


def list_clips(base_path):
    """All .flac clips under base_path/<date>/ as (mtime, path), oldest first."""
    clips = []
    for folder in sorted(os.listdir(base_path)):
        folder_path = os.path.join(base_path, folder)
        if not os.path.isdir(folder_path):
            continue
        for name in os.listdir(folder_path):
            if name.endswith(".flac"):
                path = os.path.join(folder_path, name)
                try:
                    clips.append((os.path.getmtime(path), path))
                except OSError:
                    pass  # vanished while scanning
    clips.sort()
    return clips


def compact_log(base_path, name, keep, archive=None):
    """Rewrite a clip log with only the lines whose clip is in keep (still on
    the Pi). Keeps each clip's last line. With archive (a file name in
    base_path), the lines taken out are added to it first, so nothing is lost.
    Returns the number of lines dropped."""
    path = os.path.join(base_path, name)
    if not os.path.exists(path):
        return 0
    with log_lock(base_path):
        with open(path, "r", encoding="utf-8") as f:
            lines = [l.rstrip("\n") for l in f if l.strip()]
        last = {}
        for line in lines:
            last[line.split("\t")[0].strip()] = line
        kept = [line for clip, line in last.items() if clip in keep]
        if len(kept) == len(lines):
            return 0
        if archive:
            gone = [line for clip, line in last.items() if clip not in keep]
            if gone:
                with open(os.path.join(base_path, archive), "a", encoding="utf-8") as f:
                    f.write("".join(line + "\n" for line in gone))
                    f.flush()
                    os.fsync(f.fileno())
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("".join(line + "\n" for line in kept))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    return len(lines) - len(kept)


def delete(path, dry_run):
    """Delete one file; returns bytes freed."""
    try:
        size = os.path.getsize(path)
        if not dry_run:
            os.remove(path)
        return size
    except OSError as e:
        print(f"Could not delete {path}: {e}")
        return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, help="Path to config file")
    parser.add_argument("--dry-run", action="store_true",
                        help="Only report what would be deleted")
    args = parser.parse_args()

    config = get_config(args.config)
    base_path = config["PATHS"]["BASE_PATH"]
    cleanup = config.get("CLEANUP") or {}
    keep_days = float(cleanup.get("KEEP_SENT_DAYS", 7))
    min_free = float(cleanup.get("MIN_FREE_GB", 2))
    require_delivery = cleanup.get("REQUIRE_DELIVERY", True) is not False
    dry = args.dry_run
    tag = "[dry run] " if dry else ""

    if not os.path.isdir(base_path):
        print(f"{base_path} does not exist yet - nothing to clean")
        return

    sent = read_sent(os.path.join(base_path, "sent_clips.log"))
    delivered = read_sent(os.path.join(base_path, "delivered_clips.log"))
    if require_delivery:
        sent = sent & delivered   # only clips the hub confirmed saving
    clips = list_clips(base_path)
    now = time.time()
    cutoff = now - keep_days * 86400

    sent_clips = [(t, p) for t, p in clips if p in sent]
    unsent_count = len(clips) - len(sent_clips)

    # 1. sent clips older than KEEP_SENT_DAYS
    freed = 0
    removed = 0
    remaining_sent = []
    for t, p in sent_clips:
        if t < cutoff:
            freed += delete(p, dry)
            removed += 1
        else:
            remaining_sent.append((t, p))

    # 2. still short of space -> oldest sent clips first, regardless of age
    emergency = 0
    if not dry:
        while remaining_sent and free_gb(base_path) < min_free:
            _, p = remaining_sent.pop(0)
            freed += delete(p, dry)
            emergency += 1

    # 3. leftovers from interrupted saves (older than an hour) + empty folders
    try:
        tz = ZoneInfo((config.get("SETTINGS") or {}).get("LOCAL_TZ", "UTC"))
    except Exception:  # pylint: disable=broad-except
        tz = None
    today = datetime.now(tz).strftime("%Y-%m-%d")   # same zone as the detector
    for folder in os.listdir(base_path):
        folder_path = os.path.join(base_path, folder)
        if not os.path.isdir(folder_path):
            continue
        for name in os.listdir(folder_path):
            path = os.path.join(folder_path, name)
            try:
                if name.endswith(".tmp") and os.path.getmtime(path) < now - 3600:
                    freed += delete(path, dry)
            except OSError:
                pass   # vanished meanwhile
        if folder != today and not os.listdir(folder_path) and not dry:
            try:
                os.rmdir(folder_path)
            except OSError:
                pass

    # 4. logs: only the clips still on the Pi
    dropped = 0
    if not dry:
        on_pi = {p for _, p in list_clips(base_path)}
        dropped += compact_log(base_path, "sent_clips.log", on_pi)
        # every confirmed clip stays on record in the archive (see 4. above)
        dropped += compact_log(base_path, "delivered_clips.log", on_pi,
                               archive="delivered_clips_archive.log")

    free = free_gb(base_path)
    kind = "delivered" if require_delivery else "sent"
    print(f"{tag}Deleted {removed} {kind} clip(s) older than {keep_days:g} day(s)"
          + (f" and {emergency} more to free space" if emergency else "")
          + f"; freed {freed / 1e6:.1f} MB. Free space: {free:.1f} GB. "
          f"Clips kept (not yet {kind}): {unsent_count}."
          + (f" Log lines removed: {dropped}." if dropped else ""))
    if free < min_free:
        print(f"WARNING: free space {free:.1f} GB is below MIN_FREE_GB "
              f"({min_free:g} GB) and only clips not yet {kind} are left. "
              "Check the link to the hub, or raise CONFIDENCE_THRESHOLD.")


if __name__ == "__main__":
    main()
