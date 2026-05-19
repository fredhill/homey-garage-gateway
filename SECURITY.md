# Security model

Garage Gateway controls a **physical security boundary** of your home. The
following describes how the app handles credentials, what it trusts, and
what it deliberately does not protect against.

## Trust boundary

| Component                    | Trust level | Notes |
| ---------------------------- | ----------- | ----- |
| The iSmartGate / GogoGate2   | Trusted     | User-owned, on user's LAN |
| Homey hub                    | Trusted     | Hosts this app and stores credentials |
| Homey user accounts          | Trusted     | Anyone with Homey access can run flows that open doors |
| Local LAN                    | Trusted     | App communicates over the user's home network |
| `*.isgaccess.com` (UDI host) | Trusted     | iSmartGate cloud relay — uses TLS |
| Public internet              | Untrusted   | No inbound exposure |

If your Homey account is compromised, an attacker can open your garage.
This is the same trust model as every other Homey lock/door integration —
treat the Homey login as a physical key.

## Credential handling

- Credentials are **typed into the app settings page**, which is served
  only to authenticated Homey users.
- During pairing the password is validated, written to the **encrypted
  device store**, and then **immediately cleared from plain-text app
  settings**. The plain-text copy only exists for the brief window
  between save-and-pair.
- Re-typing a password on the settings page replaces the stored one;
  blank input leaves the stored value alone.
- The settings page **never reads the password back into the DOM**.
- If pairing fails before clearing the settings, the password may sit
  in the plain-text settings until the next successful pair. This is
  marked clearly in the UI hint and is the intended trade-off.

## Network

- Local connections to the iSmartGate use HTTP — this is iSmartGate
  firmware behavior, not a choice of this app. Acceptable on a trusted
  home LAN.
- UDI remote access (`*.isgaccess.com`) uses HTTPS with system trust
  store validation via `httpx` defaults — no custom CA bypass.
- Requests use the `ismartgate` library's default 20 s request timeout
  to prevent slow-loris hangs.
- The app makes **no inbound connections**.

## Lockout protection

- On `CredentialsIncorrectException`, the polling loop enters a
  **10-minute backoff** rather than the usual 60 s. iSmartGate firmware
  can lock the admin account after repeated failed logins; this stops
  the app from triggering that lockout if the stored password goes
  stale (e.g. user changed it from the iSmartGate web UI).
- Commands (`open_door`, `close_door`) refuse to send while the hub is
  in the credentials-rejected state — no chance of accidentally lock-
  ing the account through a flow action.

## Input validation

Settings page enforces, before saving:

- Device type must be `ismartgate` or `gogogate2` (anything else
  rejected).
- Host must match a strict pattern allowing only IPv4, bracketed IPv6,
  or DNS hostnames. URL schemes, paths, and whitespace are rejected.
- Host max 253 chars (DNS limit). Username max 64. Password max 256.

The Python side additionally:

- Strips leading/trailing whitespace on host and username.
- Falls back to safe defaults if device type, username, or polling
  intervals contain unexpected values.
- Validates door voltage before mapping to a battery percentage
  (rejects NaN / inf).

## Action surface

- The app exposes **open / close / toggle** actions. There is no
  intentional way to bypass the Homey command path — every actuation
  goes through the encrypted-store credentials in `GatewayDevice`.
- Two commands within 1 second to the same door surface a user-facing
  error rather than silently dropping the second. This is by design,
  so flow logs and the device tile reflect what actually happened.

## Information disclosure

- Logs include the iSmartGate hostname (which is the user's own LAN
  device) but **never** the password.
- Exception logs use `type(exc).__name__: str(exc)` rather than
  `repr(exc)`. This avoids HTTP-client `repr()` output, which can
  embed the full request URL with query string in app logs.
- Pair-flow error messages mention the host (to help the user debug),
  never credentials.

## Out of scope

- **Local-network MITM** against the iSmartGate firmware's HTTP API.
  Mitigated only by trusting your LAN.
- **Homey account compromise** — same impact as any other physical-
  access integration on Homey.
- **iSmartGate firmware vulnerabilities** — outside the app's reach.

## Reporting a vulnerability

Open a private security advisory at
<https://github.com/fredhill/homey-garage-gateway/security/advisories/new>.
