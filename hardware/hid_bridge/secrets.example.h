// Copy to secrets.h (git-ignored) and fill in.
#define WIFI_SSID "your-wifi"
#define WIFI_PASS "your-password"
#define SERVER_HOST "192.168.1.10"  // machine running `python -m hidagent`
#define SERVER_PORT 7300
// Shared with the server (DEVICE_KEY in server/.env); at least 16 characters, e.g. from:
//   python3 -c 'import secrets; print(secrets.token_hex(16))'
// The server refuses this example value.
#define DEVICE_KEY "change-me-to-a-long-random-string"

// --- Optional: use the board away from home (#5, #6) ---
// A second Wi-Fi, tried when the first one is not reachable for 15 s (e.g. the iPhone's Personal Hotspot with
// "Maximize Compatibility" on: the board only does 2.4 GHz, WPA2/WPA3-Personal, no captive portal):
// #define WIFI_SSID2 "iPhone"
// #define WIFI_PASS2 "hotspot-password"
// A WireGuard tunnel to the server's network, so the board reaches the server from any Wi-Fi and the link is
// encrypted. With it, SERVER_HOST is the server's address inside the tunnel. Make the board's key pair with
//   wg genkey | tee board.key | wg pubkey > board.pub     (board.key goes below; board.pub to the hub as a peer)
// #define WG_PRIVATE_KEY "board private key (base64)"
// #define WG_PEER_PUBLIC_KEY "the hub's public key (base64)"
// #define WG_ENDPOINT "203.0.113.5"        // the hub's public address (or host name)
// #define WG_ENDPOINT_PORT 51820
// #define WG_ADDRESS "10.9.0.2"            // the board's address inside the tunnel
// #define WG_NETMASK "255.255.255.0"
