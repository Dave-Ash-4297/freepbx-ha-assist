# FreePBX Assist

A Home Assistant custom integration that lets you **dial a number on any FreePBX
extension and talk to Home Assistant Assist** — with commands automatically
scoped to the room the call came from.

```
Room phone (ext 201, "Kitchen")
        │  dial 400
        ▼
FreePBX / Asterisk ── SIP INVITE (From: 201) ──► Home Assistant (this integration)
                                                        │
                                     caller ID 201 → device "FreePBX extension 201"
                                     device is assigned to area "Kitchen"
                                                        │
        beep ◄── answers call                           ▼
   "turn off the lights"  ──► RTP audio ──► Assist pipeline (STT → intent → TTS)
                                                        │  scoped to Kitchen
        "Turned off the lights" ◄── TTS audio ◄─────────┘
   (speak again for another command, or hang up)
```

## How the room scoping works

- Every calling extension gets its own **device** in Home Assistant
  (`Settings → Devices & Services → FreePBX Assist`), keyed by the caller ID
  number in the SIP `From` header.
- These room phones are pre-configured and created with their area already
  assigned when the integration is set up:

  | Extension | Room    |
  |-----------|---------|
  | 1000      | Office  |
  | 2000      | Lounge  |
  | 3000      | Bedroom |
  | 4000      | Attic   |

  (The mapping lives in `EXTENSION_AREAS` in
  [`const.py`](custom_components/freepbx_assist/const.py) — edit it there if
  extensions or rooms change. Any other caller still gets a device on first
  call; assign its area in the UI.)
- Home Assistant's Assist automatically prefers entities in the device's
  area, so "turn off the lights" from the lounge phone turns off the lounge
  lights.
- The reply from the intent (e.g. "Turned on the light") is converted to
  speech by the pipeline's TTS engine and played back **into the same call**.
- After each reply you hear a short beep and can speak another command
  (conversation context is kept for the whole call). Hang up when done; the
  call also ends by itself after ~30 s of silence.

## Requirements

- Home Assistant **2024.12 or newer** (uses `voip-utils` and the Assist
  pipeline API, the same building blocks as the core VoIP integration).
- A working **Assist pipeline with STT and TTS** (e.g. Whisper + Piper, or
  Home Assistant Cloud) — test it in the browser first.
- FreePBX/Asterisk with the **OPUS codec** available (`codec_opus` ships with
  the FreePBX distro; check with `asterisk -rx "core show codecs" | grep -i opus`).
- Network path from the PBX to Home Assistant on the SIP port (UDP 5060 by
  default) and ephemeral UDP RTP ports. On plain Home Assistant OS on a LAN
  there is nothing to open.

## Install — Home Assistant side

1. Copy `custom_components/freepbx_assist/` into your Home Assistant
   `config/custom_components/` directory (or add this repository as a custom
   repository in HACS) and restart Home Assistant.
2. `Settings → Devices & Services → Add Integration → FreePBX Assist`.
   - **SIP port**: 5060, unless the core VoIP integration is also running (it
     owns 5060) — then pick e.g. 5061 and use that port in the PBX config too.
   - **FreePBX IP address**: strongly recommended — only calls from this IP
     are answered.
3. Optional, in the integration's **Configure** dialog:
   - **Allowed extensions**: pre-filled with `1000, 2000, 3000, 4000` (the
     four room phones); clear it to allow any caller.
   - **Per-extension pipelines**: one per line, `2000 = Lounge Pipeline`
     (empty = your default Assist pipeline).
   - **Command timeout**: seconds of silence before the call is dropped.

## Install — FreePBX side

Both snippets are in the [`freepbx/`](freepbx/) folder.

1. **PJSIP endpoint** — append [`pjsip_custom.conf`](freepbx/pjsip_custom.conf)
   to `pjsip.endpoint_custom.conf` (Admin → Config Edit). Change the
   `contact=` line to your Home Assistant IP and port.
2. **Dialplan** — append
   [`extensions_custom.conf`](freepbx/extensions_custom.conf) to
   `extensions_custom.conf`. It makes **400** the "talk to Home Assistant"
   number; change it if 400 collides with something.
3. Apply: `fwconsole reload`.

The dialplan deliberately leaves `CALLERID(num)` untouched — the extension
number in the SIP `From` header is what Home Assistant uses to identify the
room.

## First call

1. Dial **400** from a room phone (e.g. the Lounge phone, extension 2000).
   You should hear a short **beep**.
2. Say a command: *"turn off the lights"*, *"what's the temperature?"*.
3. Assist answers in the call — scoped to that phone's room — then beeps
   again for the next command.
4. Hang up (or stay silent and it hangs up for you).

The Office (1000), Lounge (2000), Bedroom (3000) and Attic (4000) devices
are created with their areas already assigned. If Home Assistant doesn't
already have areas with those exact names, they are created — check
`Settings → Areas` if you use different names (e.g. "Study" vs "Office") and
reassign the device to your existing area. Make sure the relevant entities
are exposed to Assist under `Settings → Voice assistants → Expose`.

## Troubleshooting

Enable debug logging in `configuration.yaml`:

```yaml
logger:
  logs:
    custom_components.freepbx_assist: debug
    voip_utils: debug
```

- **Call never connects / immediate hangup**: check `asterisk -rx "pjsip set
  logger on"` on the PBX; verify the contact IP/port and that nothing else is
  bound to the SIP port on the HA machine.
- **Call connects but no audio / "unsupported codec"**: OPUS isn't enabled.
  The endpoint must have `allow=opus` and the codec module must be loaded;
  the phones themselves may use any codec — Asterisk transcodes.
- **Rejected call from …** in the HA log: the caller IP or extension didn't
  match your PBX IP / allowed-extensions settings.
- **Commands work but aren't room-scoped**: the extension's device has no
  area assigned, or the target entities aren't exposed to Assist / not in
  that area.
- **No spoken reply**: check the pipeline's TTS engine works in
  `Settings → Voice assistants` (the integration asks the pipeline for 16 kHz
  mono WAV output; Piper and HA Cloud both support this).

## Notes and limitations

- This integration uses Home Assistant's internal `assist_pipeline` API (the
  same way the core VoIP integration does). Internal APIs can drift between
  HA releases; it targets 2024.12–2025.x. If a future release breaks it, the
  core `voip` component is the reference to diff against.
- One SIP call at a time per RTP session is supported per caller; concurrent
  calls from different extensions are fine.
- Home Assistant closes the media stream to end a call rather than sending a
  SIP BYE; the endpoint's `rtp_timeout=15` makes Asterisk clean up promptly.
