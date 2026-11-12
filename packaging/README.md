# Building and publishing

Everything here runs automatically on GitHub (`.github/workflows/build.yml`) on every push.
The finished files appear under **Actions → the latest run → Artifacts**.

| Output | Built by | Goes to |
|---|---|---|
| `LPT-Pro-<version>-Windows-Test.zip` (installer `.exe` + read-me) | `packaging/windows/lptpro.iss` (Inno Setup) | GitHub Releases, friends and testers |
| `LPTPro_<version>_x64.msix` | `packaging/windows/build_msix.ps1` | Microsoft Store (upload in Partner Center) |
| `lpt-pro_<version>_amd64.deb` | `packaging/linux/build_deb.sh` | GitHub Releases |

Both start from the same PyInstaller build (`packaging/lptpro.spec` → `dist/LPTPro/`), which
bundles Python and every required package, so users don't install Python themselves.

## Making a release

1. Set the new version in `lptpro/__init__.py`, add a line to `CHANGELOG.md`, and add a
   `<release>` entry to `packaging/linux/io.github.lptpro.LPTPro.metainfo.xml`.
2. Push, then create a tag `v<version>` (GitHub Desktop: *History → right-click the commit
   → Create Tag*, then *Push origin*). The workflow builds everything and creates a GitHub
   Release with the `.deb` attached.
3. Microsoft Store: download the `windows-store-package` artifact from that run and upload
   the `.msix` as a new submission in Partner Center.

## Microsoft Store setup (once)

1. Register as an individual developer at <https://storedeveloper.microsoft.com> (free).
2. In Partner Center: *Apps and games → New product → MSIX or PWA app* and reserve the name
   **LPT Pro**.
3. Open *Product management → Product identity* and copy these three values into the
   repository on GitHub (*Settings → Secrets and variables → Actions → Variables*):
   - `MSIX_IDENTITY_NAME` = Package/Identity/Name
   - `MSIX_PUBLISHER` = Package/Identity/Publisher (starts with `CN=`)
   - `MSIX_PUBLISHER_DISPLAY_NAME` = Package/Properties/PublisherDisplayName
4. Run the workflow again (*Actions → Build → Run workflow*). Its `.msix` now matches the
   reservation and can be uploaded.
5. Submission details:
   - Privacy policy URL: the `PRIVACY.md` link on GitHub.
   - Category: Photo & video.
   - The **runFullTrust** capability needs a short explanation: "Desktop app that reads
     photos from a USB-connected iPhone through Apple's USB service and saves them to a
     folder the user chooses."
   - Say in the description that the free *Apple Devices* app is required and iTunes is not.

The Store signs the package itself, so no certificate needs to be bought.

## Linux: software centers (later)

The `.deb` already contains the menu entry and the AppStream description
(`*.metainfo.xml`) that software centers read. To appear in Software Manager / GNOME
Software / Discover, the next step is a Flathub submission (free). It needs:
- a Flatpak manifest built on the PySide6 base app;
- access to the host's `usbmuxd` socket.

## Building the Windows installer on your own PC

Double-click `packaging\windows\build_installer.bat`. It sets up what it needs (PyInstaller
in a private build environment, Inno Setup via winget) and leaves
`dist\LPT-Pro-<version>-Windows-Test.zip`, ready to send. The installer is unsigned, so on
a PC with Smart App Control turned on, Windows won't run it. Test it on another PC.

## Building locally

```
pip install -r requirements.txt pyinstaller
pyinstaller --noconfirm packaging/lptpro.spec          # -> dist/LPTPro/
packaging/linux/build_deb.sh dist/LPTPro out           # Linux
powershell -File packaging\windows\build_msix.ps1 -Dist dist\LPTPro -Out out   # Windows (Windows SDK needed)
```
