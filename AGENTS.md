# Reasonix Android APK 开发总结

## 架构

```
┌─ Android APK ─────────────────────────────────────┐
│  WebView ← http://127.0.0.1:8787 ← SSE/fetch ────┐│
│                                                    ││
│  ReasonixService (Foreground Service)              ││
│  ├─ locateGoBinary() → nativeLibraryDir             ││
│  └─ ProcessBuilder → reasonix serve                 ││
│                                                     ││
│  libreasonix.so (Go 进程, CGO_ENABLED=1)           ││
│  ├─ Bionic getaddrinfo → real DNS from DHCP         ││
│  ├─ net/http → api.deepseek.com (TLS)              ││
│  └─ bash 工具 → wrapArgv() → 执行 shell 命令      ││
│                                                     ││
│  nativeLibraryDir (jniLibs/arm64-v8a/)              ││
│  ├─ libreasonix.so   Go 二进制 (~22MB)              ││
│  ├─ libproot.so      proot 5.1.0 (~228KB)          ││
│  ├─ libtalloc.so     proot 依赖                     ││
│  ├─ libandroid-shmem.so  proot 依赖                 ││
│  └─ libtermux-exec-ld-preload.so  LD_PRELOAD 库    ││
└────────────────────────────────────────────────────┘
```

## Go 交叉编译

### 基础编译

```powershell
$env:GOOS="android"; $env:GOARCH="arm64"; $env:CGO_ENABLED="0"
go build -ldflags="-s -w" -o libreasonix.so ./cmd/reasonix/
```

产物约 21.8 MB（stripped）。GOOS=android 等价于 GOOS=linux。

### CGO 是必须的（DNS）

- `CGO_ENABLED=0` → Go 读 `/etc/resolv.conf` → 文件不存在 → DNS 失败
- `CGO_ENABLED=1` → Go 通过 cgo 调 Bionic libc `getaddrinfo` → 真实 DNS ✅

需要 Android NDK（`sdkmanager "ndk;27.0.12077973"`）：

```powershell
$env:CC = "$NDK\toolchains\llvm\prebuilt\windows-x86_64\bin\aarch64-linux-android21-clang.cmd"
$env:CXX = "$NDK\toolchains\llvm\prebuilt\windows-x86_64\bin\aarch64-linux-android21-clang++.cmd"
```

## APK 打包

Go ELF 二进制改名为 `.so` 放入 `jniLibs/<abi>/`，Android 包管理器在安装时自动解压到
`nativeLibraryDir`（`/data/app/.../lib/arm64/`），该目录有执行权限。

`build.gradle.kts`:
```kotlin
packaging {
    jniLibs {
        useLegacyPackaging = true
    }
}
```

## 文件结构

```
android/
├── app/src/main/
│   ├── assets/
│   │   └── termux-bootstrap-<abi>.zip        ← Termux bootstrap
│   ├── jniLibs/arm64-v8a/
│   │   ├── libreasonix.so                    ← Go 二进制
│   │   ├── libproot.so                       ← proot ptrace 路径翻译器
│   │   ├── libtalloc.so                      ← proot 依赖
│   │   ├── libandroid-shmem.so               ← proot 依赖
│   │   └── libtermux-exec-ld-preload.so      ← LD_PRELOAD exec 拦截
│   ├── java/com/reasonix/app/
│   │   ├── ReasonixApp.kt                    ← Application
│   │   ├── ReasonixService.kt                ← Foreground Service
│   │   ├── MainActivity.kt                   ← WebView
│   │   ├── SetupActivity.kt                  ← 首次配置
│   │   ├── ConfigHelper.kt                   ← toml + 凭据 + 环境变量
│   │   └── BootstrapInstaller.kt             ← bootstrap 解压/修复
│   ├── res/                                  ← 布局/主题/字符串/图标
│   └── AndroidManifest.xml
└── build.gradle.kts
```

## Termux 集成

### Bootstrap 布局

```
filesDir/
├── usr/                    ← $PREFIX / TERMUX_PREFIX
│   ├── bin/                ← bash, busybox, apt, dpkg, python (via apt)...
│   ├── lib/                ← libtermux-exec*.so, liblzma, libc...
│   ├── etc/                ← apt sources, ca-certificates
│   ├── tmp/                ← $TMPDIR
│   └── var/                ← dpkg/apt state
├── home/                   ← $HOME
├── proot-guest/            ← proot -r 的 rootfs
│   └── data/data/com.termux/files/
│       ├── usr/            ← proot -b 绑定目标
│       └── home/
├── .reasonix/              ← credentials
└── reasonix.toml           ← config
```

### 环境变量（ConfigHelper.goEnv() 设置）

| 变量 | 值 | 作用 |
|------|-----|------|
| `PREFIX` | `filesDir/usr` | 向后兼容 |
| `TERMUX_PREFIX` | `filesDir/usr` | pkg/apt 脚本使用 |
| `TERMUX__PREFIX` | `filesDir/usr` | libtermux-exec shebang 翻译 |
| `TERMUX__ROOTFS` | `filesDir` | libtermux-exec rootfs 定位 |
| `TERMUX_APP__DATA_DIR` | `filesDir 的父目录` | libtermux-exec 判断文件归属 |
| `TERMUX_APP__LEGACY_DATA_DIR` | `/data/data/com.reasonix.app` | 兼容旧路径格式 |
| `TERMUX_ARCH` | `aarch64` | apt/dpkg 架构识别 |
| `PATH` | `$PREFIX/bin` + 系统 PATH | 优先 Termux 命令 |
| `LD_LIBRARY_PATH` | `$PREFIX/lib` | 共享库搜索 |
| `LD_PRELOAD` | nativeLibraryDir 下的 libtermux-exec-ld-preload.so | libc execve 拦截 → SELinux 绕过 |
| `HOME` | `filesDir/home` | Termux 家目录 |
| `TMPDIR` | `$PREFIX/tmp` | 临时文件 |
| `SHELL` | `$PREFIX/bin/bash` | 登录 shell |
| `REASONIX_HOME` | `filesDir` | config/credentials 位置 |

---

## SELinux / exec 问题

### 根因

Android 10+ 的 SELinux 策略（commit `0dd738d8`）移除了 `untrusted_app*` domain 对
`app_data_file` context 文件的 `execute` 权限。`filesDir` 下的所有文件（Termux bootstrap
的 bash、apt、python 等）都无法通过普通 `execve()` 执行。

Go 使用 raw `execve` syscall，不受 `libtermux-exec.so` 的 LD_PRELOAD 拦截（LD_PRELOAD
只拦截通过 libc 的 `execve` 调用，Go 绕过了 libc）。

### 当前解决方案

**Go → bash（第一级）**：system linker exec (`/system/bin/linker64`)
- `wrapArgv()` 检测到 filesDir 路径时，prepend `/system/bin/linker64`
- linker64 的 `system_linker_exec` context 允许被 untrusted_app 执行
- linker64 通过 `mmap` 加载目标 ELF，绕过 SELinux 的 `file:execute` 检查

**bash → 子命令（apt/ls/python...）**：LD_PRELOAD = libtermux-exec
- libtermux-exec 拦截 libc `execve()`，改写为 system linker exec
- 需要 `LD_PRELOAD` 指向正确的 .so 路径

### 已验证的修复

| 修复 | 文件 |
|------|------|
| Go raw execve → system linker exec（`wrapArgv` fallback） | `internal/sandbox/android_exec.go` |
| LD_PRELOAD 从 nativeLibraryDir 查找（之前指向 bootstrap 中不存在的路径） | `ConfigHelper.kt` |
| `installTermuxExec()` 把 .so 从 nativeLibraryDir 复制到 filesDir/usr/lib/ | `BootstrapInstaller.kt` |
| `fixupPermissions()` 每次启动运行，`usr/` 全部可写 | `BootstrapInstaller.kt` |
| 新增 `TERMUX_PREFIX` + `TERMUX_ARCH=aarch64` | `ConfigHelper.kt` |

### 当前架构

```
Go → bash (第一级): system linker exec (/system/bin/linker64)   ✅
bash → 子命令:        libc execve → LD_PRELOAD = libtermux-exec   ✅
                     → system linker exec (SELinux bypass)
路径翻译 (脚本):      runShebangFix (安装时修复 shebang)          ✅
路径翻译 (ELF):       ❌ apt/dpkg 的硬编码 Termux 前缀无法翻译
```

---

## proot 集成（不可用）

**proot 版本**：libproot.so 5.1.107 from Termux apt repo，seccomp_filter=yes，无 userland-exec。

**目标**：用 proot `-b` 运行时翻译 ELF 二进制中硬编码的 `/data/data/com.termux/files/usr` → 实际 prefix。

**结论**：proot 5.1.107 在 Android 15 / kernel 6.6 (OPPO 设备) 上**完全不可用**。

**失败现象**：无论什么配置（有/无 `-r`、有/无 binds、有/无 LD_PRELOAD、有/无 seccomp、linker64/sh/直接 bash 入口），proot 子进程 execve 均返回 ENOENT。即使文件确认存在（`ls -la`），proot 也会在 ptrace 层解析 symlink（如 `/system/bin/linker64` → `/apex/com.android.runtime/bin/linker64`、`/data/user/0/` → `/data/data/`），导致路径翻译后 kernel 无法找到文件。

**设备 SELinux 实际状态**：该设备 `untrusted_app` 可执行 `app_data_file`（audit log 显示 `granted`），不需要 linker64 绕路。

### 尝试过的配置

| # | 配置 | 结果 |
|---|------|------|
| 1 | `-r proot-guest -b prefix:buildPrefix -- guestBashPath` | ENOENT |
| 2 | 去掉 `-r`，`-b` only，exec host bash | EACCES（旧设备）/ ENOENT（本设备）|
| 3 | 去掉 `-r`，`-b` only，exec linker64 → mmap bash | ENOENT |
| 4 | 同 3，去掉所有系统 `-b` binds | ENOENT |
| 5 | 同 3，unset LD_PRELOAD | ENOENT |
| 6 | `-r` + 系统 binds + linker64 + unset LD_PRELOAD | ENOENT |
| 7 | `-r` + 系统 binds + linker64 + unset LD_PRELOAD + guest shell path | ENOENT |
| 8 | 零 binds，直接传 bash | ENOENT（路径被 canonicalize 到 `/data/data/...`）|
| 9 | 仅 prefix+home binds，去掉系统 binds | ENOENT |
| 10 | `/system/bin/sh` 入口（bind scope 外）| ENOENT |
| 11 | `PROOT_NO_SECCOMP=1` | 同上，无效 |
| 12 | 精准 bind（etc/var/lib 单独 bind，不 bind bin）| ENOENT |

**根本原因**：proot 5.1.107 的 ptrace 路径 canonicalization 与 Android 15/kernel 6.6 的 mount namespace 不兼容。子进程 execve 时路径经过 symlink 解析后被 kernel 拒绝，原因不明（可能涉及 `/apex/` 挂载点权限或 kernel 6.6 的 ptrace 行为变更）。

proot 代码保留在 `android_exec.go` 中，`REASONIX_USE_PROOT=1` 可 opt-in 调试（默认关闭）。

### apt/dpkg 硬编码路径 — wrapper 方案（在用）

**问题**：Termux bootstrap 的 apt/dpkg ELF 二进制编译时硬编码了前缀
`/data/data/com.termux/files/usr`。即使 `runShebangFix` 修复了 shell 脚本，
ELF 二进制仍然会去硬编码路径读取配置。

**方案**：`BootstrapInstaller.installAptWrappers()` 将 apt/dpkg ELF 二进制
移到 `usr/libexec/`，在原位创建 shell wrapper，自动注入路径覆盖选项。

| 命令 | 注入选项 |
|------|---------|
| apt/apt-get/apt-cache/apt-mark | `-o Dir=/ -o Dir::Etc=$PREFIX/etc/apt ...` |
| dpkg/dpkg-deb/dpkg-split/dpkg-query | `--root=$PREFIX --admindir=$PREFIX/var/lib/dpkg` |

**遗留问题**：wrapper 只修了 apt/dpkg。apt 安装的其他包（如 python）内部
可能也有硬编码路径，没有 proot 的情况下无法翻译。

### apt/dpkg workaround（手动，已被 wrapper 替代）

```bash
APT_OPTS="-o Dir=/ -o Dir::Etc=$PREFIX/etc/apt \
  -o Dir::Bin::methods=$PREFIX/lib/apt/methods \
  -o Dir::State=$PREFIX/var/lib/apt \
  -o Dir::Cache=$PREFIX/var/cache/apt \
  -o DPkg::Options::=--root=$PREFIX \
  -o DPkg::Options::=--admindir=$PREFIX/var/lib/dpkg"
apt-get $APT_OPTS update
apt-get $APT_OPTS install python
```

---

## 已废弃的方案

| # | 方案 | 失败原因 |
|---|------|---------|
| 1 | memfd_create + fexecve 自定义 C proxy | Termux 不用这个方案 |
| 2 | `/system/bin/sh` 引导层 | SELinux 阻止 untrusted_app → shell_exec domain transition |
| 3 | sed -i 修复 shebang | toybox sed 0.8.12 不支持 `-i` |
| 4 | 只修复 shebang 第一行 | 脚本内部（第 2+ 行）也有硬编码路径 |
| 5 | 全文替换未跳过 ELF | ELF 二进制被破坏（bash `unexpected e_version`） |
| 6 | adb shell 手动修复 | run-as 无写权限 |
| 7 | 用 linker64 executor 模式加载 proot 子进程 | proot 的 LD_PRELOAD 与 path bind 冲突 |

## apt/dpkg 硬编码路径解决方案

**问题**：Termux bootstrap 的 apt/dpkg ELF 二进制在编译时硬编码了前缀
`/data/data/com.termux/files/usr`。即使 `runShebangFix` 修复了 shell 脚本，
ELF 二进制仍然会去硬编码路径读取配置，导致：
```
W: Unable to read /data/data/com.termux/files/usr/etc/apt/apt.conf.d/
E: Unable to determine a suitable packaging system type
```

**解决方案**：`BootstrapInstaller.installAptWrappers()` 将 apt/dpkg 的 ELF
二进制移动到 `usr/libexec/`，在原位置创建 shell wrapper 脚本，自动注入路径
覆盖选项：

- apt/apt-get/apt-cache/apt-mark → wrapper 注入 `-o Dir=/ -o Dir::Etc=...`
- dpkg/dpkg-deb/dpkg-split/dpkg-query → wrapper 注入 `--root=... --admindir=...`

这样 AI agent 直接运行 `pkg install python` 就能正常工作，无需手动设置 APT_OPTS。

## 设备信息

- 设备：3B15CN0057200000
- SELinux context（推测）：`u:r:untrusted_app:s0`
- toybox sed：0.8.12，**不支持 `-i`**
- targetSdkVersion：35
- versionCode：2

## 构建

```powershell
# 编译 proot（一次性，已完成）
# 提取自 Termux apt repo 的 proot .deb

# 编译 Go 二进制
$env:CC = "$NDK\toolchains\llvm\prebuilt\windows-x86_64\bin\aarch64-linux-android21-clang.cmd"
$env:CXX = "$NDK\toolchains\llvm\prebuilt\windows-x86_64\bin\aarch64-linux-android21-clang++.cmd"
$env:GOOS = "android"; $env:GOARCH = "arm64"; $env:CGO_ENABLED = "1"
go build -ldflags="-s -w" -o android/app/src/main/jniLibs/arm64-v8a/libreasonix.so ./cmd/reasonix/

# 打包 APK
cd android; .\gradlew assembleDebug

# 安装
adb install -r app/build/outputs/apk/debug/app-debug.apk
```
