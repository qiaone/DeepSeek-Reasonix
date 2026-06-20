//go:build android

package sandbox

import (
	"os"
	"path/filepath"
	"strings"

	"golang.org/x/sys/unix"
)

func wrapArgv(argv []string) []string {
	if len(argv) == 0 {
		return argv
	}

	shell := argv[0]
	if !needsProxy(shell) {
		return argv
	}

	// Default: system linker exec (/system/bin/linker64) so binaries from
	// filesDir can be executed despite SELinux on strict devices.
	//
	// Set REASONIX_USE_PROOT=1 to opt into proot for runtime path
	// translation of Termux-hardcoded paths.  proot is known to fail on
	// Android 15 / kernel 6.6 (see AGENTS.md § proot).
	if os.Getenv("REASONIX_USE_PROOT") != "1" {
		return fallback(argv)
	}

	proot := prootPath()
	if proot == "" {
		return fallback(argv)
	}

	ourPrefix := os.Getenv("TERMUX__PREFIX")
	ourHome := os.Getenv("HOME")
	if ourPrefix == "" {
		return fallback(argv)
	}
	if _, err := os.Stat(shell); err != nil {
		return fallback(argv)
	}

	buildPrefix := "/data/data/com.termux/files/usr"
	buildHome := "/data/data/com.termux/files/home"

	out := []string{proot}
	if ourPrefix != buildPrefix {
		out = append(out, "-b", ourPrefix+":"+buildPrefix)
	}
	if ourHome != "" && ourHome != buildHome {
		out = append(out, "-b", ourHome+":"+buildHome)
	}
	for _, p := range []string{"/dev", "/proc", "/sys", "/system", "/apex", "/storage"} {
		out = append(out, "-b", p+":"+p)
	}

	_ = os.Setenv("LD_LIBRARY_PATH",
		filepath.Dir(proot)+":"+os.Getenv("LD_LIBRARY_PATH"))
	os.Unsetenv("LD_PRELOAD")
	tmpDir := os.Getenv("TMPDIR") + "/proot"
	os.MkdirAll(tmpDir, 0755)
	os.Setenv("PROOT_TMP_DIR", tmpDir)

	out = append(out, argv...)
	return out
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
