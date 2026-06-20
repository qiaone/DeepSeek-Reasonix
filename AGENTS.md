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
Go → bash (第一级): proot --link2symlink -0 (默认)               ✅
                       └─ 退退: system linker exec (/system/bin/linker64)
bash → 子命令 (proot 开):  由 proot ptrace 统一翻译路径            ✅
bash → 子命令 (proot 关):  libc execve → LD_PRELOAD = libtermux-exec  ✅
                       → system linker exec (SELinux bypass)
路径翻译 (脚本):       runShebangFix (安装时修复 shebang)        ✅
路径翻译 (ELF):         proot -b 运行时翻译                       ✅ (proot 开)
```

---

## proot 集成（默认通路 ✅）

**proot 版本**：libproot.so 5.1.107 from Termux apt repo，seccomp_filter=yes，无 userland-exec。

**目标**：用 proot `-b` 运行时翻译 ELF 二进制中硬编码的 `/data/data/com.termux/files/usr` → 实际 prefix。这样 apt/dpkg/python 等所有硬编码 Termux 前缀的二进制都能直接跑，**不再需要给每个命令写 wrapper**。

**结论**：proot 5.1.107 在 Android 15 / kernel 6.6 (OPPO 设备) 上**完全可用**。先前判定"不可用"的 12 次失败实验是配置缺漏 —— 漏掉了 Termux 自家 `proot-distro` 的若干关键开关，导致 proot 在 ptrace 路径解析时被 hardlink/symlink 绊倒，错把内部错误报成目标程序的 ENOENT。

### 真正能跑通的命令行（参照 termux/proot-distro 范式）

```
libproot.so \
  --kill-on-exit \
  --link2symlink \
  -0 \
  -b <ourFilesRoot>:/data/data/com.termux/files \
  -b <ourPrefix>:/data/data/com.termux/files/usr \
  -b <ourHome>:/data/data/com.termux/files/home \
  -b /dev -b /proc -b /sys -b /system -b /apex -b /linkerconfig \
  -b /vendor -b /product -b /data -b /storage -b /sdcard \
  -b /dev/urandom:/dev/random \
  -b /proc/self/fd:/dev/fd \
  -b /proc/self/fd/0:/dev/stdin \
  /data/data/com.termux/files/usr/bin/bash <args...>
```

（入口路径是 **guest 视角**的 Termux 路径，不是 host 视角的 `<ourPrefix>/bin/bash`，这样 proot 的 `-b` 翻译只走一次。）

**注意（Android 15 / OPPO ColorOS）**：

- **不**给 proot 传 `-w`：会触发 `chdir ... Function not implemented`（fortify_chdir hook，把 app 私有目录子路径的 chdir 当 ENOSYS 拒绝）。改由 [ReasonixService.kt](android/app/src/main/java/com/reasonix/app/ReasonixService.kt) 在 `ProcessBuilder.directory(filesDir/home)` 把 Go 进程 cwd 设到 home，proot 沿用继承 cwd。
- **不**绑 `/proc/self/fd/1` 与 `/proc/self/fd/2`：在 Foreground Service 里 stdout/stderr 已被 `redirectError/Output` 接到 log 文件，proot 启动期 stat 这俩 fd 会触发 "can't sanitize binding" warning 并干扰后续 path-translation。脚本要 stdout/stderr 时直接读 `/proc/self/fd/1` 即可。
- `PROOT_TMP_DIR` 改放 **`cacheDir/proot-tmp`**：默认值 `$PREFIX/tmp/proot` 同样落在 filesDir 子树，会因 `chdir ENOSYS` 让 proot 启动失败。Service 在启动时 `mkdir cacheDir/proot-tmp` 并通过 `REASONIX_PROOT_TMPDIR` 透传，[android_exec.go](internal/sandbox/android_exec.go) 的 `configureProotEnv` 优先用它。

### 关键开关 / 之前漏掉的盲点

| 开关 | 没它会怎样 | 为什么必须加 |
|------|-----------|-------------|
| `--link2symlink` | **execve ENOENT** | Android 11+ `/data/data/<pkg>` 是 `/data/user/0/<pkg>` 的 symlink；Termux 包大量用 hardlink；这个开关让 proot 把 hardlink 转成 symlink，绕开 Android 数据分区禁止 hardlink 的限制 — **#1~#12 全军覆没的真凶就是它**。 |
| `--kill-on-exit` | wait4 卡死 / 僵尸 | 部分 OEM kernel 在 ptrace 子进程退出时不发 SIGCHLD，proot 会无限等。 |
| `-0` (`--root-id`) | apt/dpkg 报 ENOENT | dpkg 强制要求 uid=0 才允许 chown；非 root 时它把 EPERM 包装成 ENOENT 链路上的故障。 |
| `/dev/random → /dev/urandom` | apt/openssl 卡 30s+ | Android 的 `/dev/random` 阻塞，Termux 标准做法。 |
| `/proc/self/fd:/dev/fd` + `fd/0:/dev/stdin` | shell 脚本 ENOENT | 很多 Android 设备没有 `/dev/stdin`。**只绑 fd 与 fd/0**，fd/1 / fd/2 在 Service 上下文会让 proot warn "can't sanitize binding" 并干扰 path-translation。 |
| **不**给 `-w` | `chdir ... Function not implemented` (ENOSYS) | OPPO/ColorOS Android 15 fortify_chdir hook 把 app 私有目录子路径 chdir 拒成 ENOSYS。改由 Service 启动 Go 进程时设 cwd=`filesDir/home`，proot 继承即可。 |
| `PROOT_TMP_DIR=cacheDir/proot-tmp` | `chdir … ENOSYS` | 同上 — `$PREFIX/tmp/proot` 也在 filesDir 子树，命中 fortify 黑名单。cacheDir 不在黑名单。 |
| host 路径预先 `EvalSymlinks` | 路径翻译双重展开 | 把 `/data/data/<pkg>` 提前展平成 `/data/user/0/<pkg>`，让 proot 的 `-b` 只翻译一次，避开 symlink-loop。 |
| **不**用 `-r` / `--rootfs` | 多余的 canonicalize 失败面 | 我们要的是"路径翻译"而非"chroot"；纯 `-b` 即可，且 `-r` 要求 rootfs 目录里有完整的 FHS，否则会触发隐性 ENOENT。 |
| **不**用 `/system/bin/linker64` 套娃入口 | ptrace 状态错乱 | proot 自己会处理 PT_INTERP；外面再套 linker64 反而让 ptrace 与 linker 私有 mmap 行为打架。 |
| **必须 unset `LD_PRELOAD`** | proot 自身 execve 被拦 | libtermux-exec 会拦截 proot 自己发出的 execve，把 ptrace 状态搞坏；走 proot 时它不需要存在。 |
| `PROOT_NO_SECCOMP=1` | 偶发 syscall 翻译错误 | Android 15 / kernel 6.6 的 seccomp_filter 行为变更让 proot 加速通路偶发失败；proot-distro 默认就是关掉的。 |

### 历史失败实验（保留作教训）

以下 12 次实验都至少缺了上表中的 `--link2symlink` 与 `-0`，因此全部 ENOENT。这并不能证明 proot 不可用，只能证明配置不完整。

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
| 11 | `PROOT_NO_SECCOMP=1` 单独打开 | 同上，无效 |
| 12 | 精准 bind（etc/var/lib 单独 bind，不 bind bin）| ENOENT |
| 13 | proot-distro 范式（--link2symlink/-0/--kill-on-exit/`-w guest-home`/`PROOT_TMP_DIR=$PREFIX/tmp/proot`）| `proot error: can't chdir to '/data/data/com.reasonix.app/files/usr/tmp/proot/proot-…': Function not implemented` —— OPPO/ColorOS Android 15 fortify_chdir 把 app 私有目录子路径 chdir 拒成 ENOSYS；解法见 #14 |
| 14 | 同 13 但 **去掉 `-w`** + `PROOT_TMP_DIR=cacheDir/proot-tmp` + Service 设 Go cwd=`filesDir/home` + 不绑 `/proc/self/fd/{1,2}` | 通过 ✅（chdir ENOSYS 全部消失） |

### 当前默认行为

- `KEY_USE_PROOT` 默认 `true`，UI 不开关也走 proot。
- 想关闭：SharedPreferences 设 `use_proot=false`，或 `export REASONIX_USE_PROOT=0` 后回退到 system-linker exec 通路。
- 走 proot 时 [ConfigHelper.kt](android/app/src/main/java/com/reasonix/app/ConfigHelper.kt) 自动 `remove("LD_PRELOAD")`、注入 `PROOT_NO_SECCOMP=1`、把 nativeLibraryDir 拼到 `LD_LIBRARY_PATH`（让 libproot.so 能 dlopen libtalloc.so / libandroid-shmem.so）。
- 命令行装配集中在 [internal/sandbox/android_exec.go](internal/sandbox/android_exec.go) 的 `wrapArgv`。

### apt/dpkg 路径 wrapper（可逐步淘汰）

proot 通了之后，apt/dpkg 的硬编码 `/data/data/com.termux/files/usr` 会被 `-b ourPrefix:termuxBuildPrefix` 自动翻译，**理论上不再需要 wrapper**。但当前仍保留 [BootstrapInstaller.installAptWrappers()](android/app/src/main/java/com/reasonix/app/BootstrapInstaller.kt) 作为 proot 关闭时的后备：

| 命令 | 注入选项 |
|------|---------|
| apt/apt-get/apt-cache/apt-mark | `-o Dir=/ -o Dir::Etc=$PREFIX/etc/apt ...` |
| dpkg/dpkg-deb/dpkg-split/dpkg-query | `--root=$PREFIX --admindir=$PREFIX/var/lib/dpkg` |

后续验证 proot 通路稳定后可以删除这部分。

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
| 8 | proot 不带 `--link2symlink` / `-0` | 12 次实验全 ENOENT；按 proot-distro 范式补上后即通 |
| 9 | proot 加上 `-w guest-home` 与 `PROOT_TMP_DIR=$PREFIX/tmp/proot` | OPPO/ColorOS Android 15 fortify_chdir 把 app 私有目录子路径的 chdir 拒成 ENOSYS。改成不传 `-w` + tmp 放 `cacheDir/proot-tmp` + Go 进程 cwd=`filesDir/home` 即通 |

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
