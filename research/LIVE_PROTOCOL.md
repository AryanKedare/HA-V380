# V380 legacy cloud live protocol

This report describes static observations from the two APKs supplied by the user. No camera, Android application, vendor service, or user account was executed or contacted to establish these claims. Synthetic protocol tests establish implementation behavior against fixtures, not compatibility with a particular camera or current regional service.

## Artifact identity and evidence boundary

| Artifact | SHA-256 |
| --- | --- |
| `base.apk` | `1cb63bc47ec36f46b9764ecdd6cbdf9a535d3ce772b7a02b94e7f2f3a00cb91f` |
| `split_config.arm64_v8a.apk` | `0dd840fea287f1fb2900f01ad02383e4d00d3515146681a0398b821074650f2e` |
| `lib/arm64-v8a/libhsMediaLibrary.so` | `01c15be44bb841f38e2ea72cbcbee4914a21720bab25744c6bfc7e61cb62b558` |
| `lib/arm64-v8a/libaes.so` | `b2dc8d54427823c8a15c7f9767e2c52fce508732b6415be8e4df3fec0cdf9fe5` |

Java evidence was decoded from `classes9.dex` with Androguard. Decompiled local variable names and types are imperfect; constants and data accesses were cross-checked against DEX instructions when important. Native evidence is LLVM `objdump` ARM64 disassembly. Addresses below are ELF virtual addresses, not source line numbers. Native REA analysis was attempted, but its Hopper provider could not launch. Consequently these native observations have source hashes and addresses, not successful REA function Evidence IDs. No decompiled vendor code or native libraries are included in the integration.

## Routing and login

Observed Java sources:

- `com.macrovideo.sdk.tools.DispatchServerGetter.getDispatchServer(String,int,int[])` constructs a 144-byte routing request and parses a 64-byte result. `DispatchServerGetter.a(String,int,int[])` connects to TCP port **8000**. Its default hostname is **dispa1.av380.net**; a separate Iran zone and fallback IP arrays exist but are outside this implementation's retry coverage.
- `com.macrovideo.sdk.tools.DispatchUtils.getSubDispatchServer(int)` reads a device ID to hostname `Map<Integer,String>`; `setSubDispatchServer(Map)` copies that map. The implementation accepts an individual string `sub_server` returned with device metadata and otherwise uses the default hostname.
- `com.macrovideo.sdk.tools.Functions.<clinit>` initializes its dispatch selector string `m` to the empty string. Empty `zone` is therefore an observed initialization value. The implementation does not reconstruct all later app region selection, relay caching, or panoramic routing policy.
- `Functions.encrypt(byte[],byte[])` uses AES/ECB/PKCS5Padding. `LoginHelper.<clinit>` supplies the 16-byte static key `macrovideo+*#!^@`.

For each request a random 16-character ASCII key is generated. Define `E(data)` as padded AES-ECB under the static key, followed by a second padded AES-ECB operation under that random key. The dispatcher request is:

| Field | Encoding |
| --- | --- |
| command | LE uint16 `100` |
| encrypted selector length | LE uint16 |
| encrypted selector | `E(zone UTF-8)` |
| encrypted device record length | LE uint16 |
| encrypted device record | `E(LE uint32 device ID + random key16)` |
| random key | 16 raw ASCII bytes |
| remaining bytes | zeros to total 144 bytes |

The dispatcher response has command `200` at uint16 offset 0; normal and panoramic relay counts are bytes 6 and 7. IPv4 addresses occupy successive four-byte tuples starting at offset 8. The integration bounds the total count to the 64-byte response and tries returned relays in order. It does not probe local camera addresses.

`LoginHelper.a(Context,String,int,String,String,int,int,int,int,boolean)` is logged by the app as `LoginFromMRServerV30`. It uses `Functions.connectToMRServer2(null,8089,...)`, sends framed JSON, and requires the matching response request ID and `result.code == 1001`.

The frame is LE uint32 `0xfe000300`, LE uint32 UTF-8 byte length, then JSON. The login parameters observed are version `31`, phoneType `1012` for V380 Pro (`Defines.<clinit>`), deviceId, device domain and camera port, accountId `11`, device username, Base64 `E(device password)` truncated to at most 112 encrypted bytes, randomKey, connectType, securityLevel `1`, agora `0`, ectx and p2pIdx. This integration selects ordinary preview connectType `0`, event context `0` and P2P index from `p_id` (default `0`); these are selected ordinary-preview inputs, not values learned from a live device. It keeps device-account credentials separate from the user's V380 account credentials.

The app takes handle, session and communication version from the response `v380` object. Its `network.serverIP` and `network.lanIP` are stored as additional address information; the inspected login path retains the original camera domain and port. The implementation uses the normal device domain `<device_id>.nvdvr.net` and camera port 8800 unless explicit camera port metadata is present. `HSMediaPlayer.startPlayEX2` selects `Functions._nCurrentPanoMRPort`, initialized to 8800, for a normal relay session unless the login explicitly marks another relay port. Custom relay-port negotiation is not implemented.

## Native relay handshake

`HSLiveDataV2Transmitter::getDataFromServer`, entry **0x476f4**, constructs the cloud request at **0x47c14–0x47cc4**. The 256-byte packed request is:

| Offset | Value |
| --- | --- |
| 0 | uint32 command `301` |
| 4 | uint32 `1002` |
| 8 | camera domain in a zero-filled 50-byte region |
| 58 | uint32 camera port |
| 62 | uint32 device ID |
| 66 | uint32 login handle |
| 70 | uint32 login session |
| 74 | uint32 stream selection, ordinary default `0` |
| 78 | byte requested FPS `20` |
| 79 | uint32 audio/video mask `4096` or `4097` |
| 83 | uint32 additional selection, ordinary default `0` |
| 88 | uint16 `0x0101` |
| 90 | byte `1` |

Additional native request fields exist at offsets 91 and 95. Their complete semantic types have not been established; the selected implementation leaves these additional inputs zero. Special modes, higher quality selectors, multi-channel panorama and custom forwarding are not claimed supported.

At **0x482e8–0x4836c**, the reply is read as command `401` at offset 0, result `1001` at offset 4, another status at offset 8, and media protocol version byte at offset 12. For protocol version at least 21, the SDK reads 256 bytes overall. The implementation follows that branch and rejects older variants. Offset 8 is **not** interpreted as a successful-result code or feature mask.

At **0x48bbc–0x48c28**, ordinary preview starts with a 256-byte zero-filled command whose first two uint32 values are `303` and `0x3001`. An alternate mode uses command 333; it is not selected here.

Native relay port data flow is independently visible in `setPlayParamWithJavaObject` **0x9ae84–0x9aea4**, **0x9b450–0x9b468** (`nMRPort` to play parameter offset 0x154), `HSLiveDataV2Transmitter::initParam` **0x45370–0x45380** (to transmitter offset 0x16c), and `dataGetThreadFunc` **0x43c24–0x43c4c** (passed to `getDataFromServer`).

## Frames and encryption

The relay reads 12-byte packet headers. For marker **0x7f**, `getDataFromServer` **0x49130–0x49150** reads fragment count uint16 offset 3, fragment index uint16 offset 5 and payload length uint16 offset 7. Fragment bytes are reassembled before decoding. Marker **0x2f** has a parameter-body length uint16 at offset 2; the implementation consumes that body and updates the media-frame version from byte 1, matching **0x492bc–0x49304**. Fixed marker **0x1f** updates that version from byte 5, matching **0x49218–0x49264**. Parameter-selected versions below 20 require older frame metadata and are rejected. The fixed markers 0x3f, 0x4f, 0x5f and 0x6f do not produce video frames in this implementation.

`parseFrameDataV20`, **0x4ab38**, interprets the complete frame's first 16 bytes as uint32 sequence, uint16 frame type, uint16 channel or auxiliary value, and uint64 time value. Payload begins at offset 16. The time field's unit has not been established; the integration does not claim it is wall-clock time. Frame timing in the Home Assistant bridge is a separate implementation choice.

Video frame types 0/1 are I/P and AAC type 24 are named by `Defines` in `classes9.dex`. Native frame handling recognizes 40/41 and maps 50/51 to 0/1 and 52/53 to 40/41 at **0x4afa0–0x4afd0**. Treating 40/41 as H.265 is consistent with the SDK's H.265 support and the external reference, but was not verified by decoding a vendor stream. The emitted codec mapping should therefore be checked on the user's camera. Unsupported audio/control types are omitted. Home Assistant's initial live bridge requests video only.

For communication version at least 31, `updateAesKey`, **0x455d0–0x45604**, constructs:

- bytes 0–3: LE login handle;
- bytes 4–11: `5c79142c46238161`, read from original `.rodata` **0x31340**;
- bytes 12–15: `f00d8082`.

`libaes.so::media_data_decrypt`, **0x3ffc–0x4098**, decrypts 64-byte AES-ECB groups separated by 16 unchanged bytes for video. Incomplete final groups remain unchanged. Audio decrypts complete 16-byte blocks and keeps the tail unchanged. `media_data_decrypt_pre_2k`, **0x409c–0x4108**, instead decrypts at most the first 2048 video bytes, aligned down to complete blocks. `parseFrameDataV20` selects that latter mode when the relay media version equals 21, at **0x4aedc–0x4aef4**. No media padding is removed. The SDK also checks Annex B start codes for keyframes; the integration rejects keyframes whose decrypted payload lacks one.

At **0x49894–0x498fc**, the native code counts fragments and schedules an acknowledgement every 30 ordinary-mode fragments, or every 10 in the unselected alternate mode. `actionCtrlThreadFunc`, **0x43e70–0x43ed0**, sends a 16-byte packet containing uint32 command `5005`, uint16 fragment count, uint16 elapsed milliseconds and eight zero bytes. The integration sends the ordinary-mode acknowledgement. The often referenced command **0x2101** is a stream/audio selection command, visible at **0x44ac4–0x44b0c**; it is not used as a periodic keepalive here.

## Verification and remaining work

`tests/test_live.py` covers key derivation, selective AES boundaries, the first-2048 variant, ordered fragment reconstruction and missing fragments, malformed sizes, newer-platform rejection, the dispatcher request, credential encoding, login correlation and framing, command301 offsets, command303, TCP partial/coalesced reads, parameter packet skipping, command5005 acknowledgement, and session cleanup. Its cloud-session fixture runs through in-memory duplex streams and an optional real localhost TCP server. All credentials and frames in these tests are synthetic.

Required remaining real-world verification: authenticate using the user's own account in Home Assistant; confirm their camera is on the supported legacy platform; confirm regional dispatcher selection; receive and decode an actual stream; confirm picture timing, reconnect behavior and cloud limits. No claim of camera compatibility or successful live video viewing follows from static analysis or the synthetic tests alone.

Explicitly unsupported: device metadata `type >= 2` (newer MQTT/key-exchange/IOT transport), relay media versions below 21, special mode333, local camera streaming, LAN discovery, PTZ/control, custom relay redirects and channel selection, non-AAC audio formats, or uncertain signed service variants. Newer V3 transmitter symbols and JSON/key-exchange routines exist in the APK but have not been reconstructed into a portable implementation. Cloud recordings are a separate protocol described elsewhere in this package.

External source consulted: `j-bloggs/v380-4g-stream` (MIT) documents itself as AI-generated and uses a hard-coded regional server; `prsyahmi/v380` primarily implements local discovery/streaming. They were treated as investigation leads. The live implementation's request structure, authentication framing, keys and acknowledgements were cross-checked against the supplied APK; the external implementations were not treated as evidence of the user's camera working.
