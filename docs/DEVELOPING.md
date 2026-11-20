# Developing LPT Pro

## Ground rules

These come from real data loss. Every change must keep them true.

1. **Copy only, never sync.** Nothing on the phone is ever changed or deleted. Nothing on
   the PC is ever deleted or overwritten (new names get " (1)", " (2)", …).
2. **Every copy is verified.**
   - The SHA-1 of the bytes as received must match a fresh re-read of the saved file.
   - The size must match exactly.
   - The first and last 64 KB are read from the phone again and must match.
   - Otherwise the file is discarded and reported.
3. **Never ask the phone to hash files** (AFC `GET_FILE_HASH` / `GET_FILE_HASH_RANGE`).
   Thousands of these requests exhaust the phone's file service: every request then fails
   with `NO_RESOURCES` until the iPhone is restarted. Read the bytes and hash them on the PC.
4. **Be honest about iCloud-only items.** Never show something as backed up unless a
   verified copy exists.
5. **The UI never blocks.** Phone and disk work runs on worker threads at low priority.
6. **Plain language** in everything the user sees. Technical details go to the log.

## Running

```
python -m venv .venv
.venv/bin/pip install -r requirements.txt        # Windows: .venv\Scripts\pip
python -m lptpro                                 # real iPhone
python -m lptpro --check                         # connection test in the console
python -m lptpro --demo /path/to/folder          # a folder with DCIM/100APPLE/... as a pretend phone
```

`--demo` makes it possible to work on almost everything without a phone.

## Code map

| File | What's in it |
|---|---|
| `lptpro/__init__.py` | version, names, settings, migration from test builds |
| `lptpro/__main__.py` | start-up, logging, video engine and GPU options, `--check` |
| `lptpro/device.py` | `AfcBackend` (real iPhone through pymobiledevice3, own asyncio loop thread, sync API) and `FolderBackend` (demo). Read-only API: `listdir`, `stat`, `stat_many`, `open`, `read`, `pread`, `close_file`, `read_all`, `head_hash` |
| `lptpro/photosdb.py` | read-only reader for the iPhone's Photos library (SQLite + its WAL, fetched in a few large reads) → which items are in Recently Deleted (`ZASSET.ZTRASHEDSTATE`) |
| `lptpro/setup_check.py` | first-time checks: Apple Mobile Device Service (Windows), usbmuxd (Linux) |
| `lptpro/media.py` | `MediaItem`, phone scan, capture dates (EXIF, QuickTime), Live Photo pairing, folder templates, file times |
| `lptpro/backup_index.py` | "is it backed up?" by size + first-64-KB hash; fingerprint cache; record of JPEG-only copies (`<backup>/.lptpro/converted.json`) |
| `lptpro/jobs.py` | worker threads: connect + scan, backup check, copy (temp `.part` → verify → move), tiles, dates taken, backup-folder watcher |
| `lptpro/heic.py` | HEIF container reader (rotation, colour profile, EXIF, embedded thumbnail); decoding through WIC on Windows, pillow-heif on Linux; HEIC → JPEG |
| `lptpro/videothumbs.py` | video tiles for videos without an iPhone thumbnail: a seekable `QIODevice` over the phone + `QMediaPlayer` slots |
| `lptpro/album.py` | Album mode: the backup folder as a browsable grid (FolderBackend + the same tiles and viewer), "Show in folder" |
| `lptpro/importer.py` | "Add downloaded photos": a folder or zip files fed to the copy engine (ZipSource) |
| `lptpro/preview.py` | viewer window: photo zoom/pan, video player (Qt Quick display, `video.qml`) |
| `lptpro/ui.py` | main window, grid, selection, folder tree, problems list, setup card, Done card, style sheet |

## Things that are easy to get wrong

- **Windows Smart App Control** blocks unsigned native code (DLL / .pyd) that has no
  reputation. Only add native dependencies whose Windows builds are signed or long
  established. That's why HEIC on Windows goes through WIC (part of Windows) via ctypes,
  and why chardet is kept below 7.
- **Qt signals from worker threads**: never use `Qt.DirectConnection` to a Python slot. It
  can deadlock against the GIL. Use queued connections.
- **Files shared between threads** (preview cache) are written atomically
  (`jobs.write_atomic`). An empty file means "not cached".
- **Fast tiles**: iOS keeps ready-made thumbnails readable over AFC:
  - `/PhotoData/Thumbnails/V2/DCIM/<NNNAPPLE>/<NAME>/5005.JPG` for photos and videos.
  - `/PhotoData/Thumbnails/VideoKeyFrames/DCIM/<NNNAPPLE>/<NAME>/LocalVideoKeyFrame.jpg`
    for some videos.
  - Live Photo videos use their photo's thumbnail.
  - Video length comes from the `mvhd` header, so no decoding is needed.
  - Only videos without any of these are decoded.
- **iCloud-only originals**: the item reports no storage blocks (`st_blocks == 0`) and
  reading it fails with `NO_RESOURCES`. Trust this only when an ordinary file reads fine
  at the same time. Otherwise the phone's file service is unwell, not offloaded.
- **Dates**: the phone's file date is when the item arrived on *this* phone (it can be a
  restore date). The grid and the folders use the date taken from inside the file.
- **Smooth video on variable-refresh screens**: while playing, `video.qml` redraws on
  every refresh (`steady`), so frames are presented evenly. Windows decodes on the
  processor by default (`QT_FFMPEG_DECODING_HW_DEVICE_TYPES=none`). The player's ⋯ menu
  can switch to the graphics card.

## Building releases

See [`packaging/README.md`](../packaging/README.md).
