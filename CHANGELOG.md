# Changelog

## 0.23.1
- Fixed: a video tile could show a picture from a different video (a late frame from the video
  made just before it). Album previews are made fresh once after updating.
- Videos with more than one sound track (newer iPhones add a "spatial audio" track that common
  players can't decode) now play the track that works, instead of playing silence. The log
  lists each video's sound tracks.

## 0.23.0
- Adding from a folder synced with iCloud for Windows (or a similar service): LPT Pro says how
  many files are fully on the PC and how many are previews still in iCloud, downloads those
  one by one as it copies them, and can "Free up space" on them afterwards (they stay in
  iCloud; files you keep on the PC aren't touched). The folder itself is only read.
- When the folder is synced, the "Done!" card warns not to delete files there in File
  Explorer (that can delete them from iCloud and the iPhone too) and explains Free up space.
- iCloud-only items are recognised in the backup even when the downloaded file has a
  different name (matched by exact size, file type and date taken), also after saving as JPEG.

## 0.22.1
- **Clear favorites** (Album mode, between Refresh and Export): removes all the stars after
  asking first. No photos are deleted, and exported copies stay.

## 0.22.0
- **Favorites** in Album mode: press F, click the ★ on a tile, or right-click → Add to
  favorites (works on a whole selection). Show → Favorites lists them. The list is kept in
  the backup folder; nothing is copied until you export.
- **Export favorites…**: copies the favorites into a separate folder (default "LPT Pro
  Favorites" in Pictures), organized by date or all in one folder, HEIC saved as JPEG by
  default. Shows the space needed first; exporting again only adds new favorites.
- The grid follows the viewer: a "you are here" outline and scrolling; the selection follows
  only when one item is selected, so a multi-selection is never lost.
- Click a blank area of the grid to deselect. Shift multi-select now works in Album mode too.
- No more "Connecting…" flicker: background checks for an iPhone don't change the screen.
- Viewer: F = favorite (Album mode); full screen is Tab (Esc to leave).
- Album right-click menu: Open, Add to / Remove from favorites, Show in folder.

## 0.21.0
- **Album mode** (iPhone | Album, top bar): browse the backup folder without the iPhone,
  by year and month, with the same viewer and video player. Right-click a photo for
  "Show in folder", which opens the file manager with that file selected. Previews are
  kept so later visits are instant.
- The connection to the iPhone is kept alive while nothing is happening, so it doesn't
  drop after sitting idle. The log notes when and why a connection ended.

## 0.20.1
- Library photos whose originals are stored on the iPhone outside the camera roll are now
  backed up (under their real names) instead of being counted as "only in iCloud".
- "Shared with You" photos from Messages are no longer counted as part of the library.
- Removed the "Include edit files (.AAE)" option (those files only mean something to Apple's
  Photos app).
- A plain explanation when the iPhone refuses to hand over a photo ("permission denied").

## 0.20.0
- **Add downloaded photos…** (next to Recheck): pick a folder or zip files, for example a
  download from iCloud, and LPT Pro copies them into the backup folder sorted by date,
  verified, skipping any already there. The source files are never changed. iCloud-only
  items that were added are marked ON PC.
- Photos saved as JPEG are still recognised by their original name.
- Fixed: the "Done!" card could fail to appear after an earlier one was closed.

## 0.19.1
- Privacy: with "Hide Recently Deleted" ticked, nothing is shown until LPT Pro knows which
  photos are deleted, so they never flash up on screen. The deleted list is read first.
- One "Show" choice replaces two checkboxes: Everything, On this iPhone, Only in iCloud,
  Not backed up yet (each with a count).
- iCloud previews are found in more places; items without a "date taken" use the date they
  were added to the library.

## 0.19.0
- Photos and videos that are only in iCloud now appear in the grid with the iPhone's own
  preview and their real name and date, tagged iCLOUD ONLY. "Hide iCloud only" hides them.
- Opening one shows the preview, clearly labelled as a preview.
- "Back up what's missing" first says how many are iCloud-only and will be skipped, with a
  "What can I do?" guide.
- Originals downloaded from icloud.com into the backup folder are recognised (same name and
  size) and marked ON PC.
- The show / hide choices moved to their own row, so the window fits smaller screens.

## 0.18.0
- Counts photos and videos that are in the iPhone's Photos library but only stored in
  iCloud ("Optimize iPhone Storage"), so the backup check never says "everything is backed
  up" while they're missing. An "Only in iCloud?" button explains how to get them.
- The log and `--check` now note where those items live on the iPhone (read-only).

## 0.17.1
- "Hide Recently Deleted" is always shown (ticked by default), also before an iPhone is connected.
- LPT Pro's own bookkeeping folders no longer appear in the folder list, and are hidden in
  File Explorer on Windows.

## 0.17.0
- Photos and videos in the iPhone's Recently Deleted album are recognised (read from the
  iPhone's Photos library, read-only).
- "Hide Recently Deleted" (on by default) keeps them out of the grid; a red DELETED tag
  marks them when shown.
- "Skip Recently Deleted" (on by default) leaves them out of "Back up what's missing" and
  the counts. Items you select yourself are always copied.

## 0.16.1
- New app icon.

## 0.16.0 – first public test release
- Copy photos and videos from an iPhone to a PC, verified, sorted by year and month.
- Backup check by file content; "Back up what's missing" with one button.
- HEIC → JPEG option, original dates kept, Live Photo pairs kept together.
- Fast previews from the iPhone's own thumbnails; built-in photo viewer and video player.
- Honest handling of photos that are only in iCloud.
- Plain-language Problems list; "Done!" card when a backup finishes.
- First-time setup help for Windows (Apple Devices) and Linux (usbmuxd).
- Windows installer for testers: checks for Apple's USB service and links to Apple Devices.
