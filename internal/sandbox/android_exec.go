//go:build android

package sandbox

import (
	"os"
	"path/filepath"
	"strings"

	"golang.org/x/sys/unix"
)

// Termux build-time hardcoded paths.  All ELF binaries in the bootstrap
// (bash, ls, apt, dpkg, …) carry these as PT_INTERP / RPATH / hardcoded
// strings.  proot's job is to translate these into our real prefix.
const (
	termuxBuildPrefix    = "/data/data/com.termux/files/usr"
	termuxBuildHome      = "/data/data/com.termux/files/home"
	termuxBuildFilesRoot = "/data/data/com.termux/files"
)

// wrapArgv 把对 Termux bootstrap 二进制的调用包装成一条 proot 命令行。
//
// 设计参照 termux/proot-distro 的 start_proot()。该实现是 Termux 官方在
// 数百万设备（含 Android 14/15）上验证过的范式，因此我们尽量贴合：
//
//	proot --kill-on-exit --link2symlink -0 \
//	      -b <ourPrefix>:<termuxBuildPrefix> \
//	      -b <ourHome>:<termuxBuildHome>     \
//	      -b /dev -b /proc -b /sys -b /system -b /apex -b /linkerconfig \
//	      -b /storage -b /sdcard -b /data \
//	      -b /dev/urandom:/dev/random \
//	      -b /proc/self/fd:/dev/fd \
//	      -b /proc/self/fd/0:/dev/stdin \
//	      <bash> <args...>
//
// 注意我们**不**给 proot 传 -w：Foreground Service 上下文中 chdir
// 到 app 私有目录（/data/data/<pkg>/files/...）在某些 OEM 内核
// (OPPO ColorOS / Android 15) 会被 fortify_chdir hook 成 ENOSYS
// (\"Function not implemented\")。Termux 终端进程本身 cwd 就在
// home 里，proot 沿用继承 cwd 就 OK。我们这边由 ReasonixService.kt
// 把 ProcessBuilder.directory(filesDir/home) 设好。
//
// 同样原因，PROOT_TMP_DIR 改用 cacheDir 下的目录（Kotlin 层透传
// REASONIX_PROOT_TMPDIR）；之前默认放在 filesDir/usr/tmp/proot
// 也会触发 chdir ENOSYS。
//
// 关键点（之前 AGENTS.md 里 #1~#12 全失败的真正原因）：
//
//   - --link2symlink :  Android 11+ /data/data/<pkg> 是 /data/user/0/<pkg> 的
//     符号链接，且许多 Termux 包用 hardlink。没有这个开关，
//     proot 在 ptrace 解析路径时会因为 hardlink/symlink 解析
//     失败而把 ENOENT 报到目标程序头上。
//   - --kill-on-exit :  避免子进程僵尸化，部分 OEM kernel 会因此让 ptrace
//     的 wait4 卡住或返回错误。
//   - -0 / --root-id :  Termux 的 apt/dpkg 强制要求 uid==0，否则 dpkg 会
//     在尝试 chown 时把 errno 当成 ENOENT 链路上的故障。
//   - 入口直接给 bash 而**不是** /system/bin/linker64：proot 自己会处理
//     PT_INTERP；用 linker64 套娃反而会让 proot 的 ptrace 状态和 linker
//     的私有 mmap 行为打架。
//   - 不使用 -r / --rootfs：我们要的是把 Termux 硬编码前缀**翻译**到当前
//     prefix，纯 -b 即可，-r 只会引入额外的 canonicalization 失败面。
func wrapArgv(argv []string) []string {
	if len(argv) == 0 {
		return argv
	}
	shell := argv[0]
	if !needsProxy(shell) {
		return argv
	}

	// REASONIX_USE_PROOT=0 显式关闭，可退回到 system linker exec 通路。
	// 默认（未设置 / 设置成任何非 0 值）走 proot —— Termux 自己就是这么干的。
	if os.Getenv("REASONIX_USE_PROOT") == "0" {
		return fallback(argv)
	}

	proot := prootPath()
	if proot == "" {
		return fallback(argv)
	}
	if _, err := os.Stat(proot); err != nil {
		return fallback(argv)
	}
	if _, err := os.Stat(shell); err != nil {
		return fallback(argv)
	}

	ourPrefix := os.Getenv("TERMUX__PREFIX")
	if ourPrefix == "" {
		ourPrefix = os.Getenv("PREFIX")
	}
	if ourPrefix == "" {
		return fallback(argv)
	}
	ourHome := os.Getenv("HOME")
	if ourHome == "" {
		ourHome = filepath.Dir(ourPrefix) + "/home"
	}
	ourFilesRoot := filepath.Dir(ourPrefix) // …/files

	// /data/data/<pkg> 在 Android 11+ 是 /data/user/0/<pkg> 的 symlink。
	// proot 在 ptrace canonicalize 时会把 /data/data/com.termux/files/...
	// 翻译到我们的 host 路径，但如果 host 路径本身经过 symlink，proot
	// 会再做一次解析；用 realpath 提前展平可以避免这一层不确定性。
	ourPrefix = canonical(ourPrefix)
	ourHome = canonical(ourHome)
	ourFilesRoot = canonical(ourFilesRoot)

	// ── proot 命令行装配 ─────────────────────────────────────────────
	out := []string{proot}
	out = append(out,
		"--kill-on-exit",
		"--link2symlink",
		"-0", // pretend uid/gid 0 — required by apt/dpkg
	)

	// 路径覆盖：硬编码 Termux 前缀 → 我们的 prefix。
	// 注意要同时绑定 prefix、home、以及它们的公共父目录 files，
	// 因为有些包会从 $TERMUX_FILES 读 var/log 等子目录。
	out = append(out, "-b", ourFilesRoot+":"+termuxBuildFilesRoot)
	if ourPrefix != termuxBuildPrefix {
		out = append(out, "-b", ourPrefix+":"+termuxBuildPrefix)
	}
	if ourHome != termuxBuildHome {
		out = append(out, "-b", ourHome+":"+termuxBuildHome)
	}

	// Android system bind — proot-distro 的标准清单。
	// 顺序：先 /dev 再单独覆盖 /dev/random，否则 random 会被 /dev 覆盖。
	for _, p := range []string{
		"/dev", "/proc", "/sys",
		"/system", "/apex", "/linkerconfig",
		"/vendor", "/product",
		"/data", "/storage", "/sdcard",
	} {
		if _, err := os.Stat(p); err == nil {
			out = append(out, "-b", p)
		}
	}

	// /dev/random 阻塞慢，apt/openssl 启动时会卡 30s+；映射到 urandom。
	if _, err := os.Stat("/dev/urandom"); err == nil {
		out = append(out, "-b", "/dev/urandom:/dev/random")
	}
	// /dev/std{in,out,err} 在很多 Android 设备上不存在或不可读，
	// 用 /proc/self/fd/N 兜底。
	//
	// 注意：在 Foreground Service 上下文里，fd 1/2 已经被
	// ProcessBuilder.redirectError/Output 重定向到 log 文件，对它们
	// 做 -b /proc/self/fd/{1,2}:/dev/std{out,err} 时 proot 在启动期
	// stat /proc/self/fd/{1,2} 会拿到一个普通文件 inode；这本身能 work，
	// 但部分 Android 内核（OPPO ColorOS/Android 15）在跨 fd 树 stat 时
	// 返回 ENOENT 触发 "can't sanitize binding" warning，进而干扰
	// 后续 path-translation。这里只保留 fd:/dev/fd 和 fd/0:/dev/stdin —
	// 真要 stdout/stderr 时脚本可以直接读 /proc/self/fd/1。
	out = append(out,
		"-b", "/proc/self/fd:/dev/fd",
		"-b", "/proc/self/fd/0:/dev/stdin",
	)

	// 工作目录：**不**给 proot 传 -w。
	//
	// 之前传 -w /data/data/com.termux/files/home 在 OPPO/ColorOS
	// (Android 15, kernel 6.6) 上会触发 "can't chdir … Function not
	// implemented" — 这是 fortify_chdir hook 对 app 私有目录子路径
	// 返回 ENOSYS（不是 SELinux 的 EACCES），Termux 自家进程绕过它
	// 的方式是让父进程（终端 shell）启动时就以 home 为 cwd，proot
	// 直接继承，从不调 chdir。
	//
	// 我们靠 ReasonixService.kt 在 ProcessBuilder.directory(filesDir/home)
	// 把 Go 进程 cwd 设到 home，proot 沿用即可，不需要 -w。

	// ── proot 自身运行环境 ───────────────────────────────────────────
	configureProotEnv(proot, ourPrefix)

	// ── 入口：直接给 bash（guest 视角）。proot 会自己处理 PT_INTERP。
	// 把 host 路径换成 guest 路径，以便 proot 的 path-translation 一次到位。
	guestArgv := make([]string, len(argv))
	for i, a := range argv {
		guestArgv[i] = hostToGuest(a, ourPrefix, ourHome, ourFilesRoot)
	}
	out = append(out, guestArgv...)
	return out
}

// hostToGuest 把含有 host prefix 的路径翻成 guest 视角的 Termux 路径。
// 仅对以 ourPrefix / ourHome / ourFilesRoot 开头的字符串做替换，否则原样返回。
func hostToGuest(s, ourPrefix, ourHome, ourFilesRoot string) string {
	switch {
	case strings.HasPrefix(s, ourPrefix):
		return termuxBuildPrefix + s[len(ourPrefix):]
	case strings.HasPrefix(s, ourHome):
		return termuxBuildHome + s[len(ourHome):]
	case strings.HasPrefix(s, ourFilesRoot):
		return termuxBuildFilesRoot + s[len(ourFilesRoot):]
	}
	return s
}

// configureProotEnv 设置 libproot.so 自身需要的运行时环境变量。
func configureProotEnv(proot, ourPrefix string) {
	prootDir := filepath.Dir(proot)

	// libproot.so dlopen 同目录的 libtalloc.so / libandroid-shmem.so。
	if cur := os.Getenv("LD_LIBRARY_PATH"); !strings.Contains(cur, prootDir) {
		if cur == "" {
			_ = os.Setenv("LD_LIBRARY_PATH", prootDir)
		} else {
			_ = os.Setenv("LD_LIBRARY_PATH", prootDir+":"+cur)
		}
	}

	// libtermux-exec 会拦截 proot 自己发出的 execve，导致 ptrace 状态错乱；
	// 必须禁用 LD_PRELOAD（proot 已经做了所有 exec 翻译，不需要它）。
	os.Unsetenv("LD_PRELOAD")

	// 在 Android 15 / kernel 6.6 上 seccomp_filter 会让 proot 的 sysno 翻译
	// 偶发出错。proot-distro 默认就是关掉的（PROOT_NO_SECCOMP=1）。
	if os.Getenv("PROOT_NO_SECCOMP") == "" {
		_ = os.Setenv("PROOT_NO_SECCOMP", "1")
	}

	// proot 需要一个可写的 tmpdir 存中间状态。
	//
	// 重要：在 OPPO/ColorOS (Android 15) 上对 filesDir/usr/tmp 子路径
	// 调用 chdir() 会返 ENOSYS（fortify_chdir hook），所以**优先**使用
	// Android 应用 cacheDir 下的目录（由 Kotlin 层透传 REASONIX_PROOT_TMPDIR）。
	// 实测 cacheDir 子路径不在 fortify 黑名单里。
	prootTmp := os.Getenv("REASONIX_PROOT_TMPDIR")
	if prootTmp == "" {
		tmpDir := os.Getenv("TMPDIR")
		if tmpDir == "" {
			tmpDir = ourPrefix + "/tmp"
		}
		prootTmp = filepath.Join(tmpDir, "proot")
	}
	_ = os.MkdirAll(prootTmp, 0755)
	_ = os.Setenv("PROOT_TMP_DIR", prootTmp)

	// 详细日志可选：REASONIX_PROOT_VERBOSE=1 → PROOT_VERBOSE=1
	if v := os.Getenv("REASONIX_PROOT_VERBOSE"); v != "" {
		_ = os.Setenv("PROOT_VERBOSE", v)
	}
}

// canonical 解析给定路径上的所有 symlink；失败时原样返回。
// 用来把 /data/data/<pkg> 提前展平到 /data/user/0/<pkg>，避免 proot
// 路径翻译里的 symlink-loop。
func canonical(p string) string {
	if r, err := filepath.EvalSymlinks(p); err == nil {
		return r
	}
	return p
}

func needsProxy(path string) bool {
	return strings.Contains(path, "/data/data/") ||
		strings.Contains(path, "/data/user/") ||
		strings.Contains(path, "/files/")
}

func prootPath() string {
	exe, err := os.Executable()
	if err != nil {
		return ""
	}
	return filepath.Join(filepath.Dir(exe), "libproot.so")
}

// nativeLibDir returns the directory containing libreasonix.so / libproot.so
// (Android nativeLibraryDir).
func nativeLibDir() string {
	exe, err := os.Executable()
	if err != nil {
		return ""
	}
	return filepath.Dir(exe)
}

// fallback：当 proot 不可用时退回到 system linker exec 路径。
func fallback(argv []string) []string {
	linker := systemLinker()
	if linker == "" {
		return argv
	}
	out := make([]string, 0, len(argv)+1)
	out = append(out, linker)
	out = append(out, argv...)
	return out
}

func systemLinker() string {
	if is64Bit() {
		if _, err := os.Stat("/system/bin/linker64"); err == nil {
			return "/system/bin/linker64"
		}
	}
	if _, err := os.Stat("/system/bin/linker"); err == nil {
		return "/system/bin/linker"
	}
	if is64Bit() {
		return "/system/bin/linker64"
	}
	return "/system/bin/linker"
}

func is64Bit() bool {
	var uts unix.Utsname
	if err := unix.Uname(&uts); err != nil {
		return true
	}
	machine := unix.ByteSliceToString(uts.Machine[:])
	return strings.Contains(machine, "64")
}
