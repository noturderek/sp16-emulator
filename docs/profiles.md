# Profiles, product descriptor and manifest

Everything that differs between firmware versions is data. Three JSON documents carry it. Profiles and the product
descriptor are in `emulator/profiles/`; manifests are written into your firmware library at import. None of them
holds firmware content: they hold identities (hashes, version strings) and measurements of the emulated hardware.

Loaders and validators: `emulator/profiles.py` (`validate`, `load`, `product`, `runtime_fields`) and
`emulator/firmware.py` (`validate_manifest`, `candidate_profile`, `check_report`).

## Product descriptor (`profiles/products/sp16.json`)

What every update of the product shares; used by the importer before a version has a profile.

| Field | Meaning |
|---|---|
| `schema`, `product`, `name` | `1`, `sp16`, display name |
| `reference_profile` | measurements used for an unprofiled version booted without a verified profile (marked unverified), and the start of `./sp16 firmware profile` |
| `update.stock_marker` | trailer marker of an official update (repacked updates use `SP16MODLAB-`) |
| `update.images_dir`, `update.tar_layers[]`, `update.squashfs_layers[]` | where the firmware layers are inside the update and where each is unpacked |
| `update.revision_files[]` | small version files copied into the manifest |
| `update.mount_points[]` | directories created in the assembled tree |
| `update.max_boot_read_bytes` | hardware read-length limit for each layer archive (`rootfs1`..`rootfs4`, `pdj`) |
| `update.enforce_boot_read` | layer archives whose size limits are enforced at unpack and repack (`rootfs2`, `pdj`) |
| `app_candidates[]`, `loader_candidates[]` | where the application and the ARM loader may be |

## Compatibility profile (`profiles/sp16-<version>.json`)

### Runtime data (the only part copied into the app and the guest)

| Field | Meaning |
|---|---|
| `schema`, `id`, `product` | `1`, `sp16-<version>`, `sp16` |
| `status` | `verified` or `candidate`; only `verified` makes a run verified, a candidate (or no profile, then the product's reference profile) runs unverified |
| `firmware` | `version`, `app_path`, `app_sha256` (the identity that matches a profile to an import), `upd_sha256` (repository copy only), `app_revision`, `panel_revisions`, `loader_path` |
| `runtime` | application working directory, emulated CPU, library path, data, storage and USB mount points, writable directories, allowed application arguments |
| `panel` | `selector_window_frames`, `pad_curve`, `button_roles`, `buttons_inert`, `counters`, `analog_words`, `lamp_roles` (SHIFT has no physical lamp), `lamp_groups`, `mono_dim_flag` |
| `audio` | `audio.jacks` mapping jack names to stream channel pairs (MAIN, PHONES, OUT 3-4, OUT 5-6, OUT 7-8), retained in shipped profiles for the Output monitor menu |
| `calibration` | pad calibration file path, format, template, range and default; the guest writes a synthetic file in this format at boot |

### Hardware suite data (repository copy only)

`identity` (version, image hash, frame sizes, boot timeout), `home`, `touch_probe`, `touch_dead`, `screen_titles`,
`nav_probe`, `selector_probe`, `knob_columns`, `knob_probe`, `knob_bounded_param`, `rgb_known`, `shift_probe`,
`recipes` (how to load a sample, save a project, set the tempo on this version), `audio`, `strip`, `usb`,
`device_nodes`, `device_nodes_unserved`, `ioctl_set`, `hardware.absent`. `null` means "not yet measured"; the
checks that need it are `BLOCKED` until it is.

Lamp and button data the suite checks every button and lamp against: `button_effects` (the visible effect of each
button role, or of a raw bit, which HW-09 looks for after a press), `lamp_unassigned` (channels and extra flags known
never to be driven, with a note), `lamp_required` (roles that must be seen in two states), `lamp_extra_driven` (the
extra flags the firmware does drive) and `ioctl_set_note` / `lamp_required_note` (why the lists are what they are);
HW-14 fails on a driven channel the profile does not account for. `derived_from` is written by `./sp16 firmware
profile` and names the profile whose measurements a candidate started from.

### Verification record

`verification` = `{method, result, date, evidence}`. A candidate carries `result: pending`. `./sp16 hwsuite --tier
full --mark-verified` writes the block after a full-tier pass and is the only writer of `status: verified`.

## Import manifest (`<library>/<id>/manifest.json`)

| Field | Meaning |
|---|---|
| `schema`, `product`, `id`, `kind` | `id` is `sp16-<version>-<update hash prefix>` (mods: `sp16-<version>-mod-<mod id>-<hash prefix>`); `kind` is `stock` or `mod` |
| `update` | trailer version, kind, CRC, size, SHA-256, file name, revisions |
| `app_path`, `loader_path`, `app_sha256` | found in the assembled tree |
| `image` | `firmware.sqfs` size and SHA-256, re-verified by the app and the guest before every boot |
| `files` | every entry of the tree: path -> type, mode, size, SHA-256 (or symlink target) |
| `compat` | device nodes, ALSA names and ioctls found, unknown candidates, calibration markers |
| `key_scan` | `absent`: no imported file contains the key |
| `importer` | importer version, base image hash, tool versions |
| `mod` | for repacked updates: base, recipes and rebuild hash when verified; `{id, verified: false, reason}` otherwise |
