# Installation

## Prerequisites

- A working Home Assistant **Supervised** or **Home Assistant OS**
  installation (regular Container or Core installs cannot run add-ons).
- The **MQTT** integration enabled inside Home Assistant — required if
  you want SGr data points to show up automatically as HA entities via
  MQTT discovery. The default
  [Mosquitto broker add-on](https://github.com/home-assistant/addons/tree/master/mosquitto)
  works out of the box.
- Network access from the HA host to every SGr device (IP + port).
- The Python flavour of **`sgr-commhandler`** is bundled inside the
  add-on image; no manual install required.

## Adding the repository

1. Open Home Assistant.
2. Go to **Settings → Add-ons → ⋮ → Repositories**.
3. Add the URL:

   ```
   https://github.com/chrohrbach/ha-smartgridready
   ```

4. The store will refresh and the **SmartGridready** add-on will
   appear in the list.

## Installing the add-on

1. Click **SmartGridready** in the store.
2. Press **Install**. The image is pulled from GitHub Container
   Registry; this typically takes a couple of minutes on first run.
3. Once installed, switch to the **Configuration** tab and review the
   defaults. The most important option is `config_path` — by default
   the user-facing configuration lives at
   `/addon_config/config.yaml`.

## First start

1. Press **Start**. On the very first start, the add-on writes a
   commented starter configuration to `/addon_config/config.yaml` if
   none exists yet. Look at the **Log** tab to confirm the file was
   created.
2. Open the ingress UI from the left-hand sidebar (icon labelled
   *SmartGridready*).
3. The Overview page will show:
   - 0 devices (you have not configured any yet),
   - MQTT discovery `on` or `off`, depending on whether a broker was
     detected.

## Configuring devices

1. Open the Home Assistant **File Editor** (or VS Code) add-on and
   open `/addon_config/config.yaml`.
2. Replace the example block with your actual devices and rules. See
   [configuration.md](configuration.md) for the full schema and the
   [rules DSL](rules-dsl.md) for the condition language.
3. Save the file.
4. Restart the **SmartGridready** add-on.
5. Re-open the ingress UI. Each declared device should appear under
   **Devices** with the badge **connected** (green). The
   **Audit** tab fills up as soon as the first evaluation cycle
   completes.

## Updating

The add-on follows semantic versioning. To update:

1. Visit the SmartGridready add-on page.
2. Press **Update** when a new version is published.
3. Read the **Changelog** tab before pressing — breaking changes are
   highlighted at the top.

## Uninstalling

Stopping the add-on does not delete your configuration file or the
audit log. To remove everything cleanly:

1. Stop and uninstall the add-on from the HA UI.
2. Optionally delete `/addon_config/config.yaml` and the share folder
   `/share/smartgridready/` (which holds the audit log and EID cache).
3. If you used MQTT discovery, the previously published entities will
   stay visible in HA until you manually delete them — they become
   "unavailable" once the add-on stops publishing.
