# esp_wireguard (vendored)

- Source: https://github.com/droscy/esp_wireguard tag `v0.4.8`, commit `9b7660fed27151abdcbf32b43861044cf4a32497`
- License: BSD 3-Clause (see `LICENSE`, kept as required)
- Layout change only: `include/esp_wireguard.h` sits next to the sources so the sketch can build it from `src/`.
  No source file is modified.
- Why vendored: the library is not in the Arduino Library Manager, and the one that is (`WireGuard-ESP32` 0.1.5)
  does not build on arduino-esp32 3.x (#6). Upgrading = replace this folder with a newer tag and rebuild.
