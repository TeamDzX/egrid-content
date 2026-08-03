#!/bin/bash
# Keeps the app and this content folder in step. Run from anywhere.
#
# JSON travels app -> content: EGrid/Resources holds the offline fallback and
# is where you edit, and this folder is what gets committed and served.
#
# Images travel content -> app: they are mirrored here first (see
# mirror-images.py), and the app ships copies so a race page is complete on
# first launch and offline. Both directions run here so the two cannot drift.
set -e
cd "$(dirname "$0")/.."

cp EGrid/Resources/channels.json EGrid/Resources/circuits.json \
   EGrid/Resources/drivers.json EGrid/Resources/machines.json content/
echo "Synced channels.json, circuits.json, drivers.json and machines.json into content/"

mkdir -p EGrid/Resources/CircuitPhotos
# --delete so an image removed from the content repo also leaves the app,
# rather than lingering in the bundle as dead weight.
rsync -a --delete --include='*.jpg' --exclude='*' \
      content/images/circuits/ EGrid/Resources/CircuitPhotos/
count=$(ls -1 EGrid/Resources/CircuitPhotos/*.jpg 2>/dev/null | wc -l | tr -d ' ')
size=$(du -sh EGrid/Resources/CircuitPhotos | cut -f1)
echo "Synced $count circuit photos ($size) into EGrid/Resources/CircuitPhotos"
