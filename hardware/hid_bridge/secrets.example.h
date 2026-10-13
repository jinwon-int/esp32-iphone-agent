// Copy to secrets.h (git-ignored) and fill in.
#define WIFI_SSID "your-wifi"
#define WIFI_PASS "your-password"
#define SERVER_HOST "192.168.1.10"  // machine running `python -m hidagent`
#define SERVER_PORT 7300
// Shared with the server (DEVICE_KEY in server/.env); at least 16 characters, e.g. from:
//   python3 -c 'import secrets; print(secrets.token_hex(16))'
// The server refuses this example value.
#define DEVICE_KEY "change-me-to-a-long-random-string"
