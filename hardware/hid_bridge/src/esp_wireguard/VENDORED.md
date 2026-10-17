# esp_wireguard (vendored)

- Source: https://github.com/droscy/esp_wireguard tag `v0.4.8`, commit `9b7660fed27151abdcbf32b43861044cf4a32497`
- License: BSD 3-Clause (see `LICENSE`, kept as required)
- Layout change: `include/esp_wireguard.h` sits next to the sources so the sketch can build it from `src/`.
- One source change, marked `LOCAL CHANGE` in `esp_wireguard.c` (`esp_wireguard_peer_init`): the peer's allowed IP
  is the tunnel network (`address` / `netmask`) instead of the board's own address as a /32. Upstream leaves other
  allowed IPs to `esp_wireguard_add_allowed_ip()`, but outside ESP-IDF menuconfig `WIREGUARD_MAX_SRC_IPS` is 1, so
  that call has no free slot and every packet to the hub was dropped (`ERR_RTE`): the handshake and keepalives went
  out, the TCP link to the server never did. Found on the first device run through a phone hotspot (2026-10-17).
  Re-apply it when upgrading.
- Why vendored: the library is not in the Arduino Library Manager, and the one that is (`WireGuard-ESP32` 0.1.5)
  does not build on arduino-esp32 3.x (#6). Upgrading = replace this folder with a newer tag and rebuild.
