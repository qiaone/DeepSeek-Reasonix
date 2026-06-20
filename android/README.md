# Reasonix for Android

Embedded Reasonix agent server wrapped in an Android WebView APK.

## Architecture

```
┌──────────────────────────────┐
│  Android APK                 │
│  ┌────────────────────────┐  │
│  │  WebView               │  │
│  │  http://127.0.0.1:8787 │  │
│  └──────────┬─────────────┘  │
│             │ HTTP + SSE     │
│  ┌──────────▼─────────────┐  │
│  │  ReasonixService       │  │
│  │  (foreground service)  │  │
│  │  ┌──────────────────┐  │  │
│  │  │ Go binary         │  │  │
│  │  │ `reasonix serve`  │  │  │
│  │  └──────────────────┘  │  │
│  └────────────────────────┘  │
└──────────────────────────────┘
```

The Go binary is cross-compiled with `CGO_ENABLED=0` for `GOOS=android` and
embedded in the APK assets. On first launch the app extracts the binary to the
device's internal storage, writes a minimal `reasonix.toml` (with
`credentials_store = "file"` because the OS keychain is unavailable on Android),
and starts the server as a foreground service.

The WebView then loads `http://127.0.0.1:8787` — the full Reasonix single-page
web UI served by the embedded Go server.

## Prerequisites

- **Go 1.25+** (for cross-compiling the binary)
- **Android Studio** or Android SDK command-line tools
- **JDK 17+**
- An Android device or emulator running **Android 8.0 (API 26)+**

## Build

### 1. Cross-compile Go + build APK (one command)

```bash
cd android
./gradlew assembleDebug
```

The Gradle build automatically cross-compiles the Go binary for
`android/arm64` before packaging. Make sure `go` is on your `PATH`.

If Go is not available, you can pre-build the binary manually:

```powershell
# On Windows (PowerShell)
$env:GOOS = "android"
$env:GOARCH = "arm64"
$env:CGO_ENABLED = "0"
go build -ldflags="-s -w" -o android/app/src/main/assets/reasonix-arm64 ./cmd/reasonix/
```

Then build the APK:

```bash
cd android
./gradlew assembleDebug
```

### 2. Install on device

```bash
adb install app/build/outputs/apk/debug/app-debug.apk
```

Or open the APK file directly on the device.

### 3. Multi-architecture

To include binaries for all supported architectures (arm64, arm, amd64):

```bash
cd android
./gradlew goBuildAll assembleDebug
```

Then update `ReasonixService.kt` to pick the right architecture binary at
runtime (already handled via `Build.SUPPORTED_ABIS`).

## First Launch — API Key Setup

On first launch the app shows a setup screen where you enter:

| Field    | Description                                  |
|----------|----------------------------------------------|
| Provider | `deepseek`, `anthropic`, or `openai`         |
| Model    | Model name (e.g. `deepseek-chat`)            |
| API Key  | Your provider API key                        |

The app writes a minimal `reasonix.toml` and credentials file, then starts the
server. On subsequent launches it skips straight to the WebView.

You can skip setup and configure later through the Reasonix web UI.

## How It Works

### Service lifecycle

1. `MainActivity` binds to `ReasonixService` (and starts it explicitly).
2. `ReasonixService` extracts the Go binary from `assets/` to
   `/data/data/com.reasonix.app/files/reasonix`.
3. The service writes `reasonix.toml` and a credentials file.
4. The Go process is started as a subprocess:
   ```
   reasonix serve --addr 127.0.0.1:8787 --auth none
   ```
5. The service registers as a *foreground service* with a persistent
   notification so Android doesn't kill it.
6. `MainActivity` polls `http://127.0.0.1:8787/status` until the server is
   ready, then loads the WebView.

### Shutdown

When the service is destroyed:
- `Process.destroy()` sends `SIGTERM` for graceful shutdown (the Go server
  handles this via `signal.NotifyContext`).
- If the process doesn't exit within 3 seconds, `destroyForcibly()` sends
  `SIGKILL`.

## Android-Specific Limitations

| Feature                    | Status                                        |
|----------------------------|-----------------------------------------------|
| OS Keychain (keyring)      | ❌ Unavailable → forced `credentials_store=file` |
| Desktop notifications      | ❌ No-op on Android                           |
| Shell sandbox (bubblewrap) | ❌ No sandbox available                       |
| Clipboard paste            | ⚠️ serve mode already disables this           |
| File system access         | ⚠️ Sandboxed to app internal storage          |
| Shell commands (`!`)       | ⚠️ Disabled over HTTP (serve mode default)    |

## Development

### Enable WebView debugging

Uncomment in `MainActivity.kt`:

```kotlin
WebView.setWebContentsDebuggingEnabled(true)
```

Then inspect via `chrome://inspect` on a connected desktop.

### View Go server logs

```bash
adb shell cat /data/data/com.reasonix.app/files/reasonix.log
```

### Rebuild Go binary only

```bash
cd android
./gradlew goBuildArm64
```

## License

Same as the parent Reasonix project.
