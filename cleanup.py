#!/usr/bin/env python3
import os
import sys
import time
from pathlib import Path

# Add the project root directory to the Python path
# to allow importing the config module
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

try:
    from config import STORAGE_PATH, FILE_LIFETIME_SECONDS, LOGS_PATH
except ImportError:
    print("Error: Could not import config. Make sure the script is in the project root.")
    sys.exit(1)

def cleanup_files():
    """Deletes files from STORAGE_PATH that are older than FILE_LIFETIME_SECONDS."""
    now = time.time()
    log_file = LOGS_PATH / "cleanup.log"
    deleted_count = 0
    
    try:
        for f in Path(STORAGE_PATH).glob('*'):
            if f.is_file():
                try:
                    # st_mtime - time of last modification
                    if (now - f.stat().st_mtime) > FILE_LIFETIME_SECONDS:
                        f.unlink()
                        deleted_count += 1
                except Exception as e:
                    with open(log_file, "a") as log:
                        log.write(f"[{time.ctime(now)}] Error deleting {f.name}: {e}\n")
        
        if deleted_count > 0:
            with open(log_file, "a") as log:
                log.write(f"[{time.ctime(now)}] Cleanup finished. Deleted {deleted_count} old file(s).\n")

    except Exception as e:
        with open(log_file, "a") as log:
            log.write(f"[{time.ctime(now)}] Critical error during cleanup: {e}\n")

if __name__ == "__main__":
    cleanup_files()