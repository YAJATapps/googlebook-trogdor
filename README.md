# googlebook-trogdor

Standalone **Bring-Your-Own-Files (BYOF)** converter script that adapts user-supplied Googlebook OS (`mica`, Android 17 / SDK 37) system partitions to boot on **Qualcomm Snapdragon 7c / SC7180 (`trogdor`) Chromebooks** from external USB/SD storage using a LineageOS 24 Trogdor hardware base.

> [!NOTE]
> **Experimental Status:** Boots to the Googlebook desktop UI (`NexusLauncher`, taskbar, quick settings, apps), though early `mica` desktop builds remain buggy on non-native Chromebook hardware. Released as-is for research and tinkering.

---

## Legal & Non-Affiliation Notice

- **Zero Proprietary Code / Strictly BYOF:** This repository contains **only** original build/interoperability automation scripts. No Google, Qualcomm, or OEM binaries, ROM images, firmware, cryptographic keys, or proprietary files are included or distributed here.
- **No Download Links:** Do **not** post links to unreleased OS images or proprietary dumps in Issues, Discussions, or Pull Requests. Any such links will be removed immediately.
- **Interoperability Research:** Provided strictly for hardware/OS interoperability research and educational study (`17 U.S.C. § 1201(f)`). Does not circumvent DRM, Widevine, or cryptographic access controls.
- **Trademark Disclaimer:** Independent community project. Not affiliated with, endorsed by, or sponsored by Google LLC, Qualcomm Incorporated, or any Chromebook OEM. Product and codename references (`Googlebook`, `mica`, `Android`, `Trogdor`) are used purely for descriptive compatibility identification.

---

## What `convert_trogdor_to_googlebook.py` Does

1. **Extracts Trogdor Hardware Base:** Parses the GPT + dynamic `super` metadata from a LineageOS 24 Trogdor raw disk image zip (`KERN-A`, `vendor_a`, `vendor_dlkm_a`, `system_dlkm_a`).
2. **Preserves Native Graphics Stack:** Keeps the stock `mica` SDK 37 `surfaceflinger` + `libgui` / `libui` userland intact so `LayerState` parcels match framework expectations.
3. **Patches Boot & Desktop Services (DEX `039`):**
   - Neutralizes `DesktopRepairModeService` blocking waits on absent GSC/VPD hardware (`desktop-services.jar`).
   - Fixes Headless System User Mode (**HSUM User 10**) boot/unlock state transitions and marks initial user setup/provisioning natively inside `UserController` (`services.jar`).
   - Immediately signals `onDrawn()` in `KeyguardServiceDelegate` when keyguard is disabled so WindowManager never holds the display blank.
   - Disables unsupported Google Security Chip / Trusty init units (`gscd`, `timberslide`, `pinweaver_storaged`, `udsattestation`).
   - Suppresses `SetupWizard` / `DesktopSetupWizard` installation via `SystemConfig` (`z-disable-setupwizard.xml`).
4. **Tunes External USB Flash Storage I/O:**
   - Configures `fstab.trogdor` (`ext4` without `casefold`, online `discard`, `noauto_da_alloc`, or journal barriers), SCSI `cache_type` (`temporary none`), VM writeback ratios, and async WAL SQLite properties to avoid USB flash I/O stalls.
5. **Repacks Dynamic `super` & Bootable GPT Image:** Generates combined SELinux `file_contexts` on the fly from the user's own images, packs `system_a` and `vendor_a` with `mkfs.erofs`, builds `super` with `lpmake`, and outputs a ready-to-flash `.img`.

---

## Host Requirements (Linux)

- Python 3.9+
- `erofs-utils` (`mkfs.erofs`, `fsck.erofs`, optionally `dump.erofs`)
- `lpmake` (from Android `lpunpack` / `android-sdk-libsparse-utils` or AOSP host tools)
- `smali` & `baksmali` (supporting `-a 29` / DEX `039`)
- `gdisk` (`sgdisk`), `zip`, `unzip`

---

## Usage

Supply your own:
1. **LineageOS 24 Trogdor** bootable image zip (`lineage-24-trogdor-*.zip`)
2. **Googlebook OS (`mica`)** partitions — either pre-extracted `system_a.img`, `product_a.img`, and `system_ext_a.img` in the workspace directory, **or** pass `--mica-image` pointing to a `mica` recovery `.zip`/`.bin` image.

```bash
python3 convert_trogdor_to_googlebook.py \
  --lineage-zip /path/to/lineage-24-trogdor.zip \
  --mica-image /path/to/mica-recovery.zip \
  --output googlebook-trogdor.img
```

*(If `system_a.img`, `product_a.img`, and `system_ext_a.img` are already placed alongside the script, `--mica-image` can be omitted.)*

### Flashing & Booting on Trogdor Chromebooks

1. Enable Developer Mode and USB booting (`crossystem dev_boot_usb=1`) on your SC7180 (`trogdor`) Chromebook.
2. Write the generated raw image to a fast USB 3.x drive or SD card:
   ```bash
   sudo dd if=googlebook-trogdor.img of=/dev/sdX bs=16M status=progress oflag=direct conv=fsync
   ```
3. At the Chromebook developer boot screen, press **`Ctrl + U`** to boot from external storage.

---

## License

Copyright 2026 Yajat Kumar. Licensed under the [Apache License, Version 2.0](LICENSE).
