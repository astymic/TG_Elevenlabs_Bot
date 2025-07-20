#!/usr/bin/env python3
import os
import sys
import time
from pathlib import Path

# Add the project root directory to the Python path
# to allow importing the config module when run by cron.
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

try:
    from config import STORAGE_PATH, FILE_LIFETIME_SECONDS, LOGS_PATH
except ImportError:
    # This will be printed to the cron log if the config cannot be found.
    print("Error: Could not import config. Make sure the script is in the project root.")
    sys.exit(1)

def cleanup_files():
    """
    Deletes files from STORAGE_PATH that are older than FILE_LIFETIME_SECONDS.
    This script is intended to be run periodically by a scheduler like cron.
    """
    now = time.time()
    log_file = LOGS_PATH / "cleanup.log"
    deleted_count = 0
    
    try:
        for f in Path(STORAGE_PATH).glob('*'):
            if f.is_file():
                try:
                    # st_mtime is the time of last modification.
                    if (now - f.stat().st_mtime) > FILE_LIFETIME_SECONDS:
                        f.unlink()
                        deleted_count += 1
                except Exception as e:
                    # Log errors for individual file deletions.
                    with open(log_file, "a") as log:
                        log.write(f"[{time.ctime(now)}] Error deleting {f.name}: {e}\n")
        
        if deleted_count > 0:
            with open(log_file, "a") as log:
                log.write(f"[{time.ctime(now)}] Cleanup finished. Deleted {deleted_count} old file(s).\n")

    except Exception as e:
        # Log critical errors, e.g., if the storage path is not accessible.
        with open(log_file, "a") as log:
            log.write(f"[{time.ctime(now)}] Critical error during cleanup process: {e}\n")

if __name__ == "__main__":
    cleanup_files()