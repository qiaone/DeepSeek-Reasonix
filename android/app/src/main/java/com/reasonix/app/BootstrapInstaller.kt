package com.reasonix.app

import android.content.Context
import android.os.Build
import android.util.Log
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.File
import java.io.FileOutputStream
import java.util.zip.ZipEntry
import java.util.zip.ZipInputStream

object BootstrapInstaller {

    private const val TAG = "BootstrapInstaller"
    private const val PREFIX_DIR = "usr"
    private const val BUILD_PREFIX = "/data/data/com.termux/files/usr"
    private const val SENTINEL = "usr/bin/bash"
    private const val CDN_BASE = "https://github.com/termux/termux-packages/releases/latest/download"

    suspend fun ensureInstalled(ctx: Context): Boolean = withContext(Dispatchers.IO) {
        val filesDir = ctx.filesDir
        val prefixDir = File(filesDir, PREFIX_DIR)
        val sentinel = File(filesDir, SENTINEL)

        if (sentinel.exists() && sentinel.canExecute()) {
            Log.d(TAG, "Bootstrap already installed at ${prefixDir.absolutePath}")
            // Fix shebangs on every boot — cheap idempotent pass
            runShebangFix(prefixDir)
            // Ensure apt/dpkg wrappers are in place BEFORE fixupPermissions
            // (which does chmod -R 555 and makes directories non-writable;
            //  installAptWrappers needs to move files between directories)
            installAptWrappers(prefixDir)
            // Fix permissions on every boot (dpkg needs writable dirs)
            fixupPermissions(prefixDir)
            // Refresh the SELinux-bypass library in case it was updated in the APK
            installTermuxExec(ctx, prefixDir)
            return@withContext true
        }

        Log.i(TAG, "Installing Termux bootstrap to ${prefixDir.absolutePath} ...")
        try {
            val zipStream = openBootstrapZip(ctx) ?: return@withContext false
            prefixDir.mkdirs()
            zipStream.use { zis -> extractBootstrap(zis, prefixDir) }
            createSymlinks(prefixDir)
            runShebangFix(prefixDir)
            // Wrap apt/dpkg ELF binaries BEFORE fixupPermissions locks directories
            installAptWrappers(prefixDir)
            fixupPermissions(prefixDir)
            installTermuxExec(ctx, prefixDir)
            File(filesDir, "home").mkdirs()
            setupResolvConf(prefixDir)
            if (sentinel.exists() && sentinel.canExecute()) {
                Log.i(TAG, "Bootstrap installed successfully")
                true
            } else {
                Log.e(TAG, "Bootstrap extraction completed but sentinel missing/invalid")
                false
            }
        } catch (e: Exception) {
            Log.e(TAG, "Failed to install bootstrap", e)
            false
        }
    }

    /**
     * Copies libtermux-exec-ld-preload.so from the APK's nativeLibraryDir
     * (where Android extracts bundled .so files) into filesDir/usr/lib/ so
     * that LD_PRELOAD can load it for SELinux execve bypass in subprocesses.
     *
     * The Termux bootstrap .zip does NOT include this library — it is a
     * Termux Android app component that we bundle directly in our APK.
     */
    private fun installTermuxExec(ctx: Context, prefixDir: File) {
        val nativeLibDir = ctx.applicationInfo.nativeLibraryDir
        val libDir = File(prefixDir, "lib")
        libDir.mkdirs()

        for (name in listOf("libtermux-exec-ld-preload.so", "libtermux-exec.so")) {
            val src = File(nativeLibDir, name)
            if (!src.exists()) continue
            val dst = File(libDir, name)
            if (dst.exists() && dst.length() == src.length()) continue
            try {
                src.copyTo(dst, overwrite = true)
                dst.setExecutable(true, false)
                Log.i(TAG, "Installed $name to ${dst.absolutePath}")
            } catch (e: Exception) {
                Log.w(TAG, "Failed to copy $name: ${e.message}")
            }
        }

        // Drop a "ld-android.so" stub in usr/lib/ that points at the system
        // dynamic linker.  Termux ELFs (bash, ls, apt, …) carry PT_INTERP =
        // /data/data/com.termux/files/usr/lib/ld-android.so .  Without this
        // stub the kernel returns ENOENT on every execve of a Termux ELF
        // (proot's "No such file or directory" error usually originates
        //  from the missing interpreter, not the target binary).
        installLdInterpreter(libDir)
    }

    /**
     * Creates filesDir/usr/lib/ld-android.so as a symlink (preferred) or
     * copy of /system/bin/linker64 — the Android bionic dynamic linker.
     *
     * Android's linker64 is itself a valid ELF interpreter, so any Termux
     * ELF whose PT_INTERP resolves to this file will load successfully.
     */
    private fun installLdInterpreter(libDir: File) {
        val linker = listOf("/system/bin/linker64", "/system/bin/linker")
            .map { File(it) }
            .firstOrNull { it.exists() } ?: run {
                Log.w(TAG, "No system linker found — Termux ELFs may fail PT_INTERP resolution")
                return
            }
        val ldStub = File(libDir, "ld-android.so")
        if (ldStub.exists()) {
            // Already installed — skip.  We don't try to keep it in sync;
            // the system linker's path is stable across boots on a given
            // device, so a one-time install is enough.
            return
        }
        try {
            java.nio.file.Files.createSymbolicLink(
                ldStub.toPath(),
                linker.toPath()
            )
            Log.i(TAG, "Symlinked ld-android.so -> ${linker.absolutePath}")
        } catch (e: Exception) {
            // Symlink may fail on some filesystems; fall back to copy.
            try {
                linker.copyTo(ldStub, overwrite = true)
                ldStub.setExecutable(true, false)
                Log.i(TAG, "Copied ld-android.so from ${linker.absolutePath}")
            } catch (e2: Exception) {
                Log.w(TAG, "Failed to install ld-android.so: ${e2.message}")
            }
        }
    }

    // ── Shebang fix ───────────────────────────────────────────────────────

    /**
     * Rewrites shebang lines in scripts that point to the build-time
     * Termux prefix.  Uses pure Java I/O since Android's toybox sed
     * may not support -i.
     */
    private fun runShebangFix(prefixDir: File) {
        val buildPrefix = BUILD_PREFIX
        val ourPrefix = prefixDir.absolutePath
        if (buildPrefix == ourPrefix) return
        var fixed = 0
        for (subdir in listOf("bin", "etc", "lib/apt")) {
            val dir = File(prefixDir, subdir)
            if (!dir.isDirectory) continue
            dir.listFiles()?.forEach { file ->
                if (!file.isFile) return@forEach
                try {
                    if (isElf(file)) return@forEach

                    val text = file.readText()
                    if (!text.contains(buildPrefix)) return@forEach
                    file.setWritable(true, false)
                    file.writeText(text.replace(buildPrefix, ourPrefix))
                    file.setWritable(false, false)
                    file.setExecutable(true, false)
                    fixed++
                    Log.d(TAG, "Fixed paths: ${file.absolutePath}")
                } catch (e: Exception) {
                    Log.d(TAG, "Path fix failed for ${file.name}: ${e.message}")
                }
            }
        }
        Log.i(TAG, "Path fix: $fixed files patched")
    }

    // ── ELF detection ──────────────────────────────────────────────────────

    /** Returns true if [file] is an ELF binary (starts with 0x7F E L F). */
    private fun isElf(file: File): Boolean {
        return try {
            val head = ByteArray(4)
            java.io.FileInputStream(file).use { it.read(head) }
            head[0] == 0x7f.toByte() && head[1] == 'E'.code.toByte() &&
                head[2] == 'L'.code.toByte() && head[3] == 'F'.code.toByte()
        } catch (_: Exception) { false }
    }

    // ── Apt / dpkg wrappers ────────────────────────────────────────────────

    /**
     * Wraps the apt and dpkg ELF binaries with shell scripts that inject
     * path-override options so they work with our custom prefix instead of
     * the compiled-in Termux prefix (/data/data/com.termux/files/usr).
     *
     * Without this, apt-get fails with "Unable to determine a suitable
     * packaging system type" because it looks for config at the hardcoded
     * Termux path which does not exist.
     *
     * The real binaries are moved to usr/libexec/; thin wrapper scripts
     * take their place in usr/bin/ and inject the Dir / root overrides.
     */
    private fun installAptWrappers(prefixDir: File) {
        val binDir = File(prefixDir, "bin")
        val libexecDir = File(prefixDir, "libexec")
        libexecDir.mkdirs()

        // Ensure directories are writable before we move files.
        // fixupPermissions does chmod -R 555 which makes dirs non-writable;
        // we run before it, but also self-defend here for any edge case.
        binDir.setWritable(true, false)
        libexecDir.setWritable(true, false)

        val ourPrefix = prefixDir.absolutePath

        // Apt commands need Dir overrides so they read config / state / cache
        // from our prefix instead of the compiled-in /data/data/com.termux.
        val aptOpts = "-o Dir=/ " +
            "-o \"Dir::Etc=$ourPrefix/etc/apt\" " +
            "-o \"Dir::Bin::methods=$ourPrefix/lib/apt/methods\" " +
            "-o \"Dir::State=$ourPrefix/var/lib/apt\" " +
            "-o \"Dir::Cache=$ourPrefix/var/cache/apt\" " +
            "-o \"DPkg::Options::=--root=$ourPrefix\" " +
            "-o \"DPkg::Options::=--admindir=$ourPrefix/var/lib/dpkg\""

        // Dpkg commands need --root and --admindir so they operate on our
        // prefix instead of the compiled-in Termux path.
        val dpkgOpts = "--root=\"$ourPrefix\" " +
            "--admindir=\"$ourPrefix/var/lib/dpkg\""

        val aptCommands = listOf("apt", "apt-get", "apt-cache", "apt-mark")
        val dpkgCommands = listOf("dpkg", "dpkg-deb", "dpkg-split", "dpkg-query")

        for ((cmds, opts) in listOf(aptCommands to aptOpts, dpkgCommands to dpkgOpts)) {
            for (cmd in cmds) {
                val cmdFile = File(binDir, cmd)
                if (!cmdFile.isFile) continue
                if (!isElf(cmdFile)) continue // already wrapped or is a symlink

                val realBin = File(libexecDir, cmd)
                if (!realBin.exists()) {
                    // Move the real ELF binary to libexec/
                    if (!cmdFile.renameTo(realBin)) {
                        Log.w(TAG, "Failed to move $cmd to libexec/ — skipping wrapper")
                        continue
                    }
                } else {
                    // Real binary already there (e.g. on re-run); just replace
                    // the stale wrapper / leftover ELF with a fresh wrapper.
                    cmdFile.delete()
                }
                realBin.setExecutable(true, false)

                val wrapper = "#!$ourPrefix/bin/bash\n" +
                    "# Auto-generated apt/dpkg wrapper — do not edit\n" +
                    "exec \"$realBin\" $opts \"\$@\"\n"
                cmdFile.writeText(wrapper)
                cmdFile.setExecutable(true, false)
                Log.i(TAG, "Installed wrapper: $cmd -> $realBin")
            }
        }
    }

    // ── Asset / CDN resolution ─────────────────────────────────────────────

    private fun openBootstrapZip(ctx: Context): ZipInputStream? {
        val abi = deviceAbi()
        val assetName = "termux-bootstrap-$abi.zip"
        try {
            val stream = ctx.assets.open(assetName)
            Log.d(TAG, "Using bundled bootstrap: $assetName")
            return ZipInputStream(stream)
        } catch (_: Exception) {
            Log.w(TAG, "Bootstrap not found in assets: $assetName, trying CDN...")
        }
        val termuxArch = abiToTermuxArch(abi)
        val url = "$CDN_BASE/bootstrap-$termuxArch.zip"
        Log.i(TAG, "Downloading bootstrap from $url ...")
        try {
            val conn = java.net.URL(url).openConnection() as java.net.HttpURLConnection
            conn.connectTimeout = 30_000
            conn.readTimeout = 120_000
            conn.requestMethod = "GET"
            if (conn.responseCode != 200) { Log.e(TAG, "CDN returned ${conn.responseCode}"); return null }
            return ZipInputStream(conn.inputStream)
        } catch (e: Exception) {
            Log.e(TAG, "Failed to download bootstrap from CDN", e)
            return null
        }
    }

    // ── Extraction ──────────────────────────────────────────────────────────

    private fun extractBootstrap(zis: ZipInputStream, prefixDir: File) {
        val buffer = ByteArray(8192)
        while (true) {
            val entry: ZipEntry = zis.nextEntry ?: break
            var name = entry.name
            if (name.startsWith("./")) name = name.substring(2)
            if (name.isEmpty()) continue
            if (name.endsWith("/")) { File(prefixDir, name).mkdirs(); continue }
            val target = File(prefixDir, name)
            if (name == "SYMLINKS.txt") {
                target.parentFile?.mkdirs()
                FileOutputStream(target).use { out -> zis.copyTo(out) }
                zis.closeEntry()
                continue
            }
            target.parentFile?.mkdirs()
            try {
                FileOutputStream(target).use { out ->
                    var count: Int
                    while (zis.read(buffer).also { count = it } > 0) { out.write(buffer, 0, count) }
                }
                target.setReadable(true, false)
                val needsExec = name.startsWith("bin/") || name.contains("/bin/") ||
                    name.startsWith("sbin/") || name.contains("/sbin/") ||
                    name.startsWith("lib/") || name.contains("/lib/")
                if (needsExec) {
                    target.setWritable(false, false)
                    target.setExecutable(true, false)
                    if (!target.canExecute()) {
                        for (cmd in arrayOf(arrayOf("/system/bin/toybox", "chmod", "555", target.absolutePath), arrayOf("/system/bin/chmod", "555", target.absolutePath), arrayOf("chmod", "555", target.absolutePath))) {
                            try { val p = Runtime.getRuntime().exec(cmd); if (p.waitFor(1, java.util.concurrent.TimeUnit.SECONDS) && p.exitValue() == 0) break } catch (_: Exception) { }
                        }
                    }
                } else { target.setWritable(false, false) }
            } catch (e: Exception) { Log.w(TAG, "Failed to extract $name: ${e.message}") }
            zis.closeEntry()
        }
    }

    // ── Symlinks ────────────────────────────────────────────────────────────

    private fun createSymlinks(prefixDir: File) {
        val symlinksFile = File(prefixDir, "SYMLINKS.txt")
        if (!symlinksFile.exists()) { Log.w(TAG, "SYMLINKS.txt not found — skipping symlinks"); return }
        symlinksFile.forEachLine { line ->
            val trimmed = line.trim()
            if (trimmed.isEmpty() || trimmed.startsWith("#")) return@forEachLine
            val arrowIdx = trimmed.indexOf('\u2190')
            if (arrowIdx < 0) { Log.w(TAG, "Malformed symlink line (no arrow): $trimmed"); return@forEachLine }
            createOneSymlink(prefixDir, trimmed.substring(0, arrowIdx).trim(), trimmed.substring(arrowIdx + 1).trim())
        }
    }

    private fun createOneSymlink(prefixDir: File, target: String, link: String) {
        val linkFile = File(prefixDir, link)
        if (linkFile.exists()) return
        linkFile.parentFile?.mkdirs()
        var resolvedTarget = target
        if (target.startsWith(BUILD_PREFIX + "/")) resolvedTarget = target.substring(BUILD_PREFIX.length + 1)
        else if (target.startsWith("/")) resolvedTarget = target.substringAfterLast('/')
        try {
            java.nio.file.Files.createSymbolicLink(linkFile.toPath(), java.nio.file.Paths.get(resolvedTarget))
            Log.d(TAG, "Symlink: $link -> $resolvedTarget")
        } catch (e: Exception) {
            val targetFile = File(prefixDir, resolvedTarget)
            if (targetFile.exists()) {
                try { targetFile.copyTo(linkFile, overwrite = false); linkFile.setExecutable(targetFile.canExecute(), false); Log.d(TAG, "Copied (symlink fallback): $link -> $resolvedTarget") }
                catch (e2: Exception) { Log.w(TAG, "Failed to copy symlink fallback $link: ${e2.message}") }
            } else { Log.w(TAG, "Symlink target not found: $resolvedTarget (original=$target, for $link)") }
        }
    }

    // ── Permission fixup ────────────────────────────────────────────────────

    private fun fixupPermissions(prefixDir: File) {
        val binDir = File(prefixDir, "bin"); val libDir = File(prefixDir, "lib")
        val busybox = File(prefixDir, "bin/busybox"); val chmodBin = File(prefixDir, "bin/chmod")
        val chmodCandidates = mutableListOf<List<String>>()
        chmodCandidates.add(listOf("/system/bin/toybox", "chmod", "-R", "555"))
        if (chmodBin.exists() && chmodBin.canExecute()) chmodCandidates.add(listOf(chmodBin.absolutePath, "-R", "555"))
        if (busybox.exists() && busybox.canExecute()) chmodCandidates.add(listOf(busybox.absolutePath, "chmod", "-R", "555"))
        chmodCandidates.add(listOf("/system/bin/chmod", "-R", "555"))
        for (chmodCmd in chmodCandidates) {
            try {
                val proc = Runtime.getRuntime().exec((chmodCmd + listOf(binDir.absolutePath, libDir.absolutePath)).toTypedArray())
                proc.waitFor(5, java.util.concurrent.TimeUnit.SECONDS)
                if (proc.exitValue() == 0) { Log.d(TAG, "Permissions fixed via ${chmodCmd[0]} (exit=0)"); break }
            } catch (e: Exception) { Log.d(TAG, "${chmodCmd[0]} failed: ${e.message}") }
        }
        for (dir in listOf(binDir, libDir)) {
            if (!dir.isDirectory) continue
            dir.walkTopDown().filter { it.isFile }.forEach { file ->
                file.setExecutable(true, false)
            }
        }

        // Make the entire prefix writable so dpkg can install packages.
        // The bootstrap zip extracts non-executable files as read-only,
        // but apt/dpkg need to replace/add files under usr/ at runtime.
        // We preserve the execute bits set above for bin/ and lib/.
        if (prefixDir.isDirectory) {
            prefixDir.walkTopDown().filter { it.isFile }.forEach { file ->
                file.setWritable(true, false)
            }
            Log.d(TAG, "Made prefix files writable")
        }
    }

    // ── DNS / resolv.conf ───────────────────────────────────────────────────

    private fun setupResolvConf(prefixDir: File) {
        val etcDir = File(prefixDir, "etc"); etcDir.mkdirs()
        val resolv = File(etcDir, "resolv.conf")
        if (resolv.exists()) return
        try { resolv.writeText("nameserver 8.8.8.8\nnameserver 8.8.4.4\n"); Log.d(TAG, "Wrote resolv.conf with public DNS") }
        catch (e: Exception) { Log.w(TAG, "Failed to write resolv.conf: ${e.message}") }
    }

    // ── ABI helpers ─────────────────────────────────────────────────────────

    @Suppress("DEPRECATION")
    private fun deviceAbi(): String {
        val abis = Build.SUPPORTED_ABIS ?: arrayOf(Build.CPU_ABI)
        for (abi in abis) { when { abi.startsWith("arm64") -> return "arm64"; abi.startsWith("armeabi") -> return "arm"; abi.startsWith("x86_64") -> return "x86_64"; abi.startsWith("x86") -> return "x86" } }
        return when { Build.CPU_ABI.startsWith("arm64") -> "arm64"; Build.CPU_ABI.startsWith("armeabi") -> "arm"; Build.CPU_ABI.startsWith("x86_64") -> "x86_64"; else -> "x86" }
    }

    private fun abiToTermuxArch(abi: String): String = when (abi) { "arm64" -> "aarch64"; "arm" -> "arm"; "x86" -> "i686"; "x86_64" -> "x86_64"; else -> "aarch64" }
}
