# Investigation ledger

## Target and method

The supplied files are `base.apk` and `split_config.arm64_v8a.apk` from V380 Pro,
package `com.macrovideo.v380pro`, version **2.2.90**, version code **225**.
The manifest declares compile SDK35. They are a base/split installation set,
not two app versions.

| Artifact | SHA-256 |
| --- | --- |
| base.apk | 1cb63bc47ec36f46b9764ecdd6cbdf9a535d3ce772b7a02b94e7f2f3a00cb91f |
| split_config.arm64_v8a.apk | 0dd840fea287f1fb2900f01ad02383e4d00d3515146681a0398b821074650f2e |

REA `inspect_artifact` inventoried both packages. Base inventory parent Evidence:
`ev_7d8ce962dd584a93f1d79eeabc886dcb75be7d95466a60150eb26667e12bee99`.
Split inventory parent Evidence:
`ev_f65eee8efcfdc0981e80f2b7f9ff7b539671466527f039b28b941c3d2ec29343`.
The complete REA JSON is retained in the investigation workspace, separately
from the distributable integration. REA's Android provider could not run without
a supplied JADX MCP JAR; no Java/toolchain installation or REA registration
change was made. The existing CLI was used for supported inventory. DEX code
and annotations were inspected with Androguard. Native code was inspected
statically with LLVM objdump after the available native provider failed to start.
Shell-derived findings are not represented as successful REA code Evidence.
Neither APK nor its native code was executed.

## Account and device protocol observations

Sources are in `classes10.dex` unless stated otherwise. DEX instruction offsets
are relative to the named method, not source-code line numbers.

- `utils.OkHttpUtil.accountLogin` posts JSON to billing `user/login`.
  `username` is the entered cloud account, `phonetoken` is the string `100`, and
  `timestamp` is integer Unix seconds. Its `password` is lowercase hex of
  AES-ECB/PKCS5 ciphertext. `utils.GlobalDefines.encrypt` offsets0x0008–0x0022
  establish the cipher; `com.macrovideo.sdk.tools.HttpUtils.sEncryptionKey` in
  `classes9.dex` supplies `1234567890123456`.
- The login signature is lowercase UTF8 MD5 of
  `password=<encoded>&phonetoken=100&timestamp=<seconds>&username=<account>hsshop2016`.
  This is observed app behavior, not a new cryptographic design.
- `defines.HttpUrlDefines` declares `https://mapi.av380.net/` and an HTTP
  alternative. The integration selects the observed HTTPS endpoint.
- `activities.LoginActivity.handleMessage`, offsets0x00a2–0x00b6, checks
  `result==0` and `error_code==0`; it reads `data.access_token`, `data.user_id`,
  username and relay/account metadata. Password error codes21005/21006 are
  mapped by `handleLoginFailure`. No account was used to verify acceptance.
- `OkHttpUtil.updateUserDeviceList` posts `device/list` with `accesstoken`,
  `from_app=V380Pro`, `get_sub_server=10`, `language=en`, empty registration ID,
  timestamp, `type=all`, `ver=1008`, and its canonical-order MD5 signature.
  It does not establish a camera's online status; the integration keeps it
  unknown until an explicit status source exists.
- `OkHttpUtil.renewUserToken` uses `user/get-new-token` and a token/timestamp
  signature. `BaseActivity$56.onResponse` at0x0084–0x008e reads the replacement
  `access_token` from the response root, not `data`. Error21011 means account
  authorization must be refreshed.
- Owned device rows include `device_account`, `device_password`, `type`, `ver`,
  `rand_key`, routing and newer-platform key metadata. For legacy devices,
  `BaseActivity$55.handleGetDataSuccessful` sets registerv5 to **2** at0x0108,
  compares `getVer()` to it at0x08d4, and decrypts Base64 AES using
  `rand_key + hongshi1` at0x08e4–0x0922. Other legacy versions use plain Base64
  decoding at0x0938–0x096c. The implementation uses **ver==2**, not ver==1.
- Shared rows use a different model: `entities.network.ShareNewsResponse$ShareNewBean`.
  `sdk.manager.DeviceManager.parseShareDevice` Base64-decodes `str` at0x0090 to
  JSON. Legacy `username`/`password` values are already clear strings after
  that decode; defaults are admin/empty (0x00be–0x00e8). All three inner keys
  `public_key`, `permission`, `model` select platform2 and clear legacy
  credentials (0x0124–0x0168). Outer nickname overrides the inner nickname
  (0x0188–0x01aa), and routing/P2P metadata is retained (0x0250–0x025e).
  Malformed shares are skipped, matching the Android parser's null result.
- Login/owned device model fields have no alternate Gson wire names; the schema
  audit is saved separately. S3 model annotations do contain alternate names,
  which were applied to the recording client.

## Question checklist

| Question | Status | Evidence / remaining gap |
| --- | --- | --- |
| Identify APK set/version | Answered | ZIP inventory, SHA256, decoded binary manifest. |
| Recover regular account login/device listing | Answered statically; runtime unverified | DEX instructions and JSON fields; loopback HTTP tests. |
| Recover legacy device credentials and shared devices | Answered statically; runtime unverified | Owned AES/Base64 branch, parseShareDevice, synthetic tests. |
| Fetch remote live video through cloud | Partially answered | Legacy relay implemented and tested with synthetic TCP traffic; actual camera/regional compatibility unknown. |
| Support newer key-exchange live cameras | Unresolved | SDK/native routines exist, but portable transport is not implemented. Explicitly rejected. |
| Browse/retrieve saved cloud recordings | Partially answered | UCloud and S3 clients implemented from DEX/native evidence; no actual recording sample/account tested. |
| Display video through current HA APIs | Partially answered | Camera, Media Source, authenticated proxy and renewal implemented; independent MPEGTS decode and proxy tests pass; adapter test shutdown crash unresolved. |
| End-to-end user camera compatibility | Unresolved | Requires the user's own account/camera to validate; credentials were not provided or requested in chat. |

See the detailed live/recording reports for additional bounded searches,
implementation choices, raw addresses and unsupported branches. Static analysis
and fixture tests do not establish successful execution against a real camera.
