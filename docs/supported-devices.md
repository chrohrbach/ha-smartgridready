# Supported devices

This add-on can drive **any device with a SmartGridready EID** — the
generic code path does not need to be modified per manufacturer. The
protocol details (Modbus register layout, REST endpoints, MQTT topics)
all live inside the EID XML file published by the SGr association.

The authoritative source is the public catalogue:

- Browser, filterable: <https://library.smartgridready.ch/Device>
- Raw XML, versioned:
  [github.com/SmartGridready/SGrSpecifications](https://github.com/SmartGridready/SGrSpecifications)
  → `XMLInstances/ExtInterfaces/`

The list below is **a snapshot of EIDs actually present in the
catalogue at the time of this release**. Manufacturer support evolves
quickly — always cross-check against the upstream library before
relying on a specific identifier.

## EID filename convention

Real filenames follow the pattern:

```
SGr_<level>_<manufacturer-code>_<device-code>_<vendor>_<model>[_<transport>]_V<version>.xml
```

Examples taken straight from the catalogue:

| Device                          | EID                                                                 |
|---------------------------------|---------------------------------------------------------------------|
| Stiebel Eltron heat pump        | `SGr_04_0015_xxxx_StiebelEltron_HeatPump_V1.0.0`                    |
| Hoval heat pump                 | `SGr_04_0017_xxxx_HOVAL_HeatPump_V1.0.0`                            |
| CTA heat pump                   | `SGr_02_0033_0000_CTA_HeatPump_V1.0.0`                              |
| Heliotherm heat pump            | `SGr_04_0020_xxxx_Heliotherm_HeatPumpV0.2.1`                        |
| KEBA KeContact P30 wallbox      | `SGr_04_mmmm_dddd_KEBA_KeContact_P30_V0.1`                          |
| GARO wallbox                    | `SGr_04_0005_xxxx_GARO_WallboxV0.2.1`                               |
| Eaton charging station          | `SGr_04_mmmm_dddd_Eaton_ChargingStation_V0.5.3`                     |
| Webasto Next wallbox            | `SGr_04_mmmm_dddd_Webasto_Next_V0.1`                                |
| Fronius Symo PV inverter        | `SGr_04_0021_xxxx_FroniusSymoV0.2.1`                                |
| Fronius Smart Meter IP          | `SGr_00_mmmm_dddd_Fronius_SmartMeterIP_ModbusTCP_V0.1`              |
| Shelly Pro 3EM (REST local)     | `SGr_00_mmmm_dddd_Shelly_Pro3EM_RestAPILocalBasicAuth_V1.0`         |
| Shelly Pro 3EM (REST cloud)     | `SGr_00_mmmm_dddd_Shelly_Pro3EM_RestAPICloud_V1.0`                  |
| Shelly Pro 3EM (MQTT)           | `SGr_00_mmmm_dddd_Shelly_Pro3EM_MQTT_V1.0`                          |
| Siemens PAC2200 meter           | `SGr_00_0016_dddd_Siemens_PAC2200_ModbusTCP_V0.1`                   |
| ABB B23 meter                   | `SGr_00_0016_dddd_ABB_B23_ModbusTCP_V0.4`                           |
| WAGO smart meter                | `SGr_04_0014_0000_WAGO_SmartMeterV0.2.3`                            |
| Smart-me sub-meter              | `SGr_02_mmmm_8288089799_Smart-me_SubMeterElectricity_ApiKey_V1.1.0` |
| CLEMAP energy monitor           | `SGr_00_0018_CLEMAP_EnergyMonitor_RestAPICloud_V1.1`                |
| Gantrisch gPlug (MQTT)          | `SGr_00_mmmm_dddd_Gantrisch_gPlug_MQTT_VSEFull_V0.2`                |
| Gantrisch gPlug (Modbus TCP)    | `SGr_00_mmmm_dddd_Gantrisch_gPlug_ModbusTCP_VSEFull_V0.1`           |
| Ensor Blue2Box (Modbus TCP)     | `SGr_00_mmmm_dddd_Ensor_Blue2Box_ModbusTCP_Opt_V1.0`                |

A few notes about the format:

- The first two digits after `SGr_` (e.g. `04`) encode the
  *information level* — the role the device plays in the SGr
  architecture. `04` is the most common for end devices (M2M);
  `00`/`02` appear on meters and tariff sources.
- `xxxx`, `mmmm`, `dddd` are wildcard segments — the device or
  manufacturer code can be unknown / generic, in which case the
  placeholder is kept.
- When the same model is exposed through multiple transports
  (Shelly, Gantrisch, ABB), each transport is a **separate EID** and
  you pick the one matching how the device is wired.

## Coverage by category

The catalogue groups devices by **functional profile category**.
The categories declared on the official documentation are:

- Actuator
- Battery
- EVSE (Electric Vehicle Supply Equipment)
- Heat pump appliance
- Inverter
- Metering
- Sensor
- Tariff (Dynamic Tariff data sources)
- Communication (gateways)

## Dynamic-tariff sources

A particular strength of the catalogue is tariff-broadcasting EIDs —
they expose the day-ahead spot price grid for many Swiss utilities:

| Utility    | EID                                                             |
|------------|-----------------------------------------------------------------|
| AEM        | `SGr_00_mmmm_dddd_DynamicTariff_AEM_V0.2`                       |
| CKW        | `SGr_00_mmmm_dddd_DynamicTariff_CKW_V0.2`                       |
| EGA        | `SGr_00_mmmm_dddd_DynamicTariff_EGA_V0.1`                       |
| EKZ        | `SGr_00_mmmm_dddd_DynamicTariff_EKZ_V0.2`                       |
| Groupe E   | `SGr_00_mmmm_dddd_DynamicTariff_GroupeE_V2.0`                   |
| Primeo     | `SGr_00_mmmm_dddd_DynamicTariff_Primeo_V0.1`                    |
| Swisspower | `SGr_00_mmmm_dddd_DynamicTariff_Swisspower_V0.2`                |

These let you feed a real tariff into `sensors.spot_price` without
maintaining a custom integration per utility.

## V2H / V2G hardware support

The add-on's V2H safety layer is in place, but **writing a negative
current target depends on the wallbox EID actually exposing a
bidirectional current limit**. As of this release, very few catalogue
EIDs declare such a data point. Reports about V2H-capable hardware are
welcome — please open an
[issue with the device-report template](.github/ISSUE_TEMPLATE/device_report.md).

## Contributing a new device

You do not need to change this add-on to use a new SGr device. If the
EID is in the library and your hardware is properly addressed, the
generic code path covers it.

It is still helpful to the community if you let us know what worked:

1. Open an issue titled `Confirmed: <Manufacturer> <Model>`.
2. Paste the exact `eid:` value and the `properties:` block.
3. Mention firmware version and transport (Modbus TCP, REST, MQTT).
4. Indicate which `profile / data_point` pairs you actually drove.

That information feeds back into this document over time.
