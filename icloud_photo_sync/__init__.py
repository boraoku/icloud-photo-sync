"""icloud-photo-sync: a one-way iCloud Photos downloader for macOS.

Downloads the entire iCloud Photos library into ``./YYYY/MM/`` (by capture
date), supports stop/resume and byte-level interrupted-file recovery, and can
keep the folder up to date by pulling only newly added items. One-way only:
it never deletes or modifies anything, locally or in iCloud.
"""

__version__ = "0.1.0"
