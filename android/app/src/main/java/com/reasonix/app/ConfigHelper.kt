package com.reasonix.app

import android.content.Context
import android.content.SharedPreferences
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.File

/**
 * Manages the reasonix.toml config file and credential storage for Android.
 *
 * On Android the OS keychain is unavailable (go-keyring returns
 * ErrUnsupportedPlatform), so we force credentials_store = "file" and write a
 * self-contained config + credentials pair under the app's internal storage.
 */
object ConfigHelper {

    private const val PREFS_NAME = "reasonix_config"
    private const val KEY_FIRST_LAUNCH = "first_launch"
    private const val KEY_PROVIDER = "provider"
    private const val KEY_MODEL = "model"
    private const val KEY_API_KEY = "api_key"

    // ── SharedPreferences getters/setters ──────────────────────────────────

    private fun prefs(ctx: Context): SharedPreferences =
        ctx.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)

    fun isFirstLaunch(ctx: Context): Boolean =
        prefs(ctx).getBoolean(KEY_FIRST_LAUNCH, true)

    fun markLaunched(ctx: Context) {
        prefs(ctx).edit().putBoolean(KEY_FIRST_LAUNCH, false).apply()
    }

    fun getProvider(ctx: Context): String =
        prefs(ctx).getString(KEY_PROVIDER, "deepseek") ?: "deepseek"

    fun getModel(ctx: Context): String =
        prefs(ctx).getString(KEY_MODEL, "") ?: ""

    fun getApiKey(ctx: Context): String =
        prefs(ctx).getString(KEY_API_KEY, "") ?: ""

    fun saveCredentials(ctx: Context, provider: String, model: String, apiKey: String) {
        prefs(ctx).edit()
            .putString(KEY_PROVIDER, provider)
            .putString(KEY_MODEL, model)
            .putString(KEY_API_KEY, apiKey)
            .apply()
    }

    // ── Config file writers ────────────────────────────────────────────────

    /**
     * Returns the directory where reasonix stores config and credentials.
     */
    fun configDir(ctx: Context): File = ctx.filesDir

    /**
     * Returns the Go binary path on device (in nativeLibraryDir).
     */
    fun binaryPath(ctx: Context): File =
        File(ctx.applicationInfo.nativeLibraryDir, "libreasonix.so")

    // Provider presets matching the built-in Reasonix provider definitions.
    // These must match the names/URLs used in internal/config/config.go.
    private data class ProviderDef(
        val name: String,
        val kind: String,
        val baseUrl: String,
        val model: String,
        val apiKeyEnv: String
    )

    private fun providerDef(ctx: Context): ProviderDef {
        val provider = getProvider(ctx)
        val model = getModel(ctx).ifEmpty { defaultModelFor(provider) }
        return when (provider) {
            "deepseek" -> ProviderDef(
                name = "deepseek-pro",
                kind = "openai",
                baseUrl = "https://api.deepseek.com",
                model = model,
                apiKeyEnv = "DEEPSEEK_API_KEY"
            )
            "anthropic" -> ProviderDef(
                name = "anthropic",
                kind = "anthropic",
                baseUrl = "https://api.anthropic.com",
                model = model,
                apiKeyEnv = "ANTHROPIC_API_KEY"
            )
            "openai" -> ProviderDef(
                name = "openai",
                kind = "openai",
                baseUrl = "https://api.openai.com",
                model = model,
                apiKeyEnv = "OPENAI_API_KEY"
            )
            else -> ProviderDef(
                name = provider,
                kind = "openai",
                baseUrl = "",
                model = model,
                apiKeyEnv = "${provider.uppercase()}_API_KEY"
            )
        }
    }

    private fun defaultModelFor(provider: String): String = when (provider) {
        "deepseek" -> "deepseek-v4-pro"
        "anthropic" -> "claude-sonnet-4-20250514"
        "openai" -> "gpt-4o"
        else -> ""
    }

    /**
     * Writes reasonix.toml and a credentials file into [configDir].
     *
     * The toml forces `credentials_store = "file"` so the Go binary does not
     * attempt the OS keychain (which fails on Android).
     *
     * IMPORTANT: defining [[providers]] in the toml REPLACES all built-in
     * presets. The generated entry must match the naming conventions the
     * rest of the codebase expects (e.g. "deepseek-pro", kind="openai").
     */
    suspend fun writeConfig(ctx: Context): Result<Unit> = withContext(Dispatchers.IO) {
        try {
            val dir = configDir(ctx)
            dir.mkdirs()

            val p = providerDef(ctx)
            val apiKey = getApiKey(ctx)

            // ── reasonix.toml ──────────────────────────────────────────────
            val toml = buildString {
                appendLine("# Reasonix config — auto-generated for Android")
                appendLine("credentials_store = \"file\"")
                appendLine()
                appendLine("default_model = \"${p.name}\"")
                appendLine()
                appendLine("[agent]")
                appendLine("keep = [\"errors\"]")
                appendLine()
                appendLine("[[providers]]")
                appendLine("name = \"${p.name}\"")
                appendLine("kind = \"${p.kind}\"")
                if (p.baseUrl.isNotEmpty()) {
                    appendLine("base_url = \"${p.baseUrl}\"")
                }
                appendLine("model = \"${p.model}\"")
                appendLine("api_key_env = \"${p.apiKeyEnv}\"")
                appendLine()
                appendLine("[serve]")
                appendLine("auth_mode = \"none\"")
            }

            File(dir, "reasonix.toml").writeText(toml)

            // ── Credentials file ───────────────────────────────────────────
            val credsDir = File(dir, ".reasonix")
            credsDir.mkdirs()
            val creds = "${p.apiKeyEnv}=$apiKey\n"
            File(credsDir, "credentials").writeText(creds)

            Result.success(Unit)
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    /**
     * Returns the environment variables to set for the Go process.
     *
     * When the Termux bootstrap is installed under filesDir/usr/, we prepend its
     * bin/ and lib/ directories to PATH and LD_LIBRARY_PATH so the agent (and any
     * tool it spawns) finds bash, python, git, ripgrep, etc. from the Termux
     * environment.
     *
     * LD_PRELOAD loads libtermux-exec.so which:
     * 1. Intercepts libc execve() calls and uses system linker exec
     *    (/system/bin/linker64) to bypass SELinux app_data_file restrictions.
     * 2. Fixes shebang paths (e.g. /usr/bin/env → $PREFIX/bin/env).
     *
     * Go's own raw-syscall execve is handled by wrapArgv() in
     * internal/sandbox/android_exec.go, which also uses system linker exec.
     */
    fun goEnv(ctx: Context): Map<String, String> {
        val p = providerDef(ctx)
        val apiKey = getApiKey(ctx)
        val dir = configDir(ctx)
        val prefixDir = File(dir, "usr")
        val homeDir = File(dir, "home")

        // Android system PATH (the Go process inherits this)
        val systemPath = System.getenv("PATH") ?: "/system/bin"

        return buildMap {
            // ── Termux paths ───────────────────────────────────────────────
            if (File(prefixDir, "bin/bash").exists()) {
                val prefix = prefixDir.absolutePath
                val rootfs = dir.absolutePath           // filesDir → TERMUX__ROOTFS
                val appDataDir = dir.parentFile!!.absolutePath  // /data/.../<pkg> → TERMUX_APP__DATA_DIR
                // /data/data/<pkg> legacy format (for libtermux-exec compat)
                val legacyDataDir = "/data/data/" + appDataDir.substringAfterLast("/")

                put("PREFIX", prefix)                    // backward compat
                put("TERMUX_PREFIX", prefix)            // pkg/apt scripts use single underscore
                put("TERMUX__PREFIX", prefix)           // libtermux-exec shebang fix
                put("TERMUX__ROOTFS", rootfs)           // libtermux-exec rootfs
                put("TERMUX_APP__DATA_DIR", appDataDir) // libtermux-exec app data
                put("TERMUX_APP__LEGACY_DATA_DIR", legacyDataDir)
                put("TERMUX_ARCH", "aarch64")           // apt/dpkg need this
                // Prepend Termux bin so bash/python/apt shadow Android's toybox
                put("PATH", "$prefix/bin:$systemPath")
                put("LD_LIBRARY_PATH", "$prefix/lib")
                // libtermux-exec-ld-preload.so is bundled in the APK's
                // jniLibs/ and extracted by Android to nativeLibraryDir
                // at install time. It is NOT part of the Termux bootstrap
                // zip, so filesDir/usr/lib/ won't have it unless we copy
                // it there. Look in nativeLibraryDir first, then try the
                // bootstrap path (for when BootstrapInstaller copies it).
                val nativeLibDir = ctx.applicationInfo.nativeLibraryDir
                val ldPreloadPath = listOf(
                    "$nativeLibDir/libtermux-exec-ld-preload.so",
                    "$nativeLibDir/libtermux-exec.so",
                    "$prefix/lib/libtermux-exec-ld-preload.so",
                    "$prefix/lib/libtermux-exec.so"
                ).firstOrNull { File(it).exists() } ?: "$nativeLibDir/libtermux-exec-ld-preload.so"
                put("LD_PRELOAD", ldPreloadPath)
                put("HOME", homeDir.absolutePath)
                put("TMPDIR", "$prefix/tmp")
                put("SHELL", "$prefix/bin/bash")
            } else {
                // Bootstrap not installed yet — use system defaults
                put("HOME", dir.absolutePath)
                put("TMPDIR", dir.absolutePath + "/tmp")
            }

            // ── API key ────────────────────────────────────────────────────
            put(p.apiKeyEnv, apiKey)
            // ── Credentials file location ──────────────────────────────────
            put("REASONIX_CONFIG_DIR", dir.absolutePath)
            // Pin REASONIX_HOME to filesDir so config/credentials are always
            // found regardless of where HOME points (Termux sets HOME=filesDir/home/)
            put("REASONIX_HOME", dir.absolutePath)
            // ── Force file-based credential store ──────────────────────────
            put("REASONIX_CREDENTIALS_STORE", "file")
        }
    }
}
