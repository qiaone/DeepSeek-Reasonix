package com.reasonix.app

import android.app.Notification
import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import android.os.Binder
import android.os.IBinder
import android.util.Log
import androidx.core.app.NotificationCompat
import kotlinx.coroutines.*
import java.io.File
import java.util.concurrent.TimeUnit

/**
 * Foreground service that extracts and runs the Go reasonix binary.
 *
 * Lifecycle:
 * 1. onCreate  → extractGoBinary() + writeConfig()
 * 2. onStartCommand → startGoProcess() + move to foreground
 * 3. onDestroy → stopGoProcess()
 */
class ReasonixService : Service() {

    companion object {
        private const val TAG = "ReasonixService"
        private const val NOTIFICATION_ID = 1
        const val ACTION_STOP = "com.reasonix.app.action.STOP"

        /** Port the Go server listens on. */
        const val SERVER_PORT = 8787
        const val SERVER_ADDR = "127.0.0.1:$SERVER_PORT"
    }

    private val binder = LocalBinder()
    private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())
    private var goProcess: Process? = null

    // State observable by MainActivity
    @Volatile var isRunning: Boolean = false
        private set
    @Volatile var startupError: String? = null
        private set

    inner class LocalBinder : Binder() {
        fun getService(): ReasonixService = this@ReasonixService
    }

    override fun onBind(intent: Intent?): IBinder = binder

    // ── Lifecycle ──────────────────────────────────────────────────────────

    override fun onCreate() {
        super.onCreate()
        Log.d(TAG, "Service created")
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_STOP -> {
                stopSelf()
                return START_NOT_STICKY
            }
        }

        startForeground(NOTIFICATION_ID, buildNotification())
        scope.launch { startServer() }
        return START_STICKY
    }

    override fun onDestroy() {
        Log.d(TAG, "Service destroying")
        stopGoProcess()
        scope.cancel()
        super.onDestroy()
    }

    // ── Go binary location ──────────────────────────────────────────────────
    //
    // The Go binary is bundled as a "native library" (libreasonix.so) in
    // jniLibs/<abi>/.  Android's package manager extracts .so files to the
    // nativeLibraryDir, which has exec permission — unlike filesDir which is
    // mounted noexec since Android 10 (API 29).

    /**
     * Returns the path to the Go binary in the native library directory.
     * Android extracts .so files from jniLibs to this directory at install
     * time, and it is mounted with execute permission.
     */
    private fun locateGoBinary(): File {
        val libDir = applicationInfo.nativeLibraryDir
        val binary = File(libDir, "libreasonix.so")
        Log.d(TAG, "Go binary location: ${binary.absolutePath} (exists=${binary.exists()}, exec=${binary.canExecute()})")
        return binary
    }

    // ── Server start / stop ─────────────────────────────────────────────────

    private suspend fun startServer() {
        startupError = null

        // 1. Locate binary (in nativeLibraryDir, extracted from jniLibs by Android)
        val binary = locateGoBinary()
        if (!binary.exists()) {
            startupError = "Go binary not found: ${binary.absolutePath}"
            return
        }
        if (!binary.canExecute()) {
            startupError = "Go binary not executable: ${binary.absolutePath}"
            Log.w(TAG, "Binary not executable, trying chmod...")
            try {
                binary.setExecutable(true, false)
                Runtime.getRuntime().exec(arrayOf("chmod", "755", binary.absolutePath)).waitFor(2, TimeUnit.SECONDS)
            } catch (_: Exception) { }
            if (!binary.canExecute()) {
                startupError = "Cannot make binary executable: ${binary.absolutePath}"
                return
            }
        }

        // 2. Write config
        val configResult = ConfigHelper.writeConfig(this)
        if (configResult.isFailure) {
            startupError = "Failed to write config: ${configResult.exceptionOrNull()?.message}"
            Log.w(TAG, "Config write failed, continuing anyway", configResult.exceptionOrNull())
        }

        // 3. Install Termux bootstrap (fast no-op if already done)
        val bootstrapReady = BootstrapInstaller.ensureInstalled(this)
        if (bootstrapReady) {
            Log.i(TAG, "Termux bootstrap ready")
        } else {
            Log.w(TAG, "Termux bootstrap not available — shell tools will be limited to Android system commands")
        }

        // 4. Start Go process
        try {
            val env = ConfigHelper.goEnv(this)
            val dir = ConfigHelper.configDir(this)
            dir.mkdirs()
            // Create tmp dirs if they don't exist
            File(dir, "tmp").mkdirs()
            File(dir, "usr/tmp").mkdirs()
            val homeDir = File(dir, "home").apply { mkdirs() }
            // proot 自身的 tmpdir：放到 cacheDir 下，避开 OPPO/ColorOS
            // 对 filesDir 子路径 chdir 的 fortify ENOSYS 限制。
            val prootTmpDir = File(cacheDir, "proot-tmp").apply { mkdirs() }

            val pb = ProcessBuilder(
                binary.absolutePath,
                "serve",
                "--addr", SERVER_ADDR,
                "--auth", "none"
            )
            // 关键：把 Go 进程 cwd 设到 home 而不是 filesDir 根。
            // proot 会沿用这个 cwd，不再调 chdir，避开 ENOSYS。
            pb.directory(homeDir)
            pb.environment().putAll(env)
            // 透传 PROOT 用的 tmpdir（android_exec.go 的 wrapArgv 会读）
            pb.environment()["REASONIX_PROOT_TMPDIR"] = prootTmpDir.absolutePath
            // Redirect stderr to a log file for debugging
            val logFile = File(dir, "reasonix.log")
            pb.redirectError(ProcessBuilder.Redirect.appendTo(logFile))
            pb.redirectOutput(ProcessBuilder.Redirect.appendTo(logFile))

            Log.d(TAG, "Starting Go process: ${binary.absolutePath} serve --addr $SERVER_ADDR --auth none")
            Log.d(TAG, "Working dir: ${dir.absolutePath}")
            Log.d(TAG, "Env keys: ${env.keys}")

            goProcess = pb.start()
            isRunning = true
            Log.i(TAG, "Go process started, proc=$goProcess")

            // 5. Monitor the process in the background
            val proc = goProcess!!
            withContext(Dispatchers.IO) {
                val exitCode = proc.waitFor()
                Log.w(TAG, "Go process exited with code $exitCode")
                isRunning = false
                goProcess = null

                // Read tail of log for error diagnosis
                if (exitCode != 0 && logFile.exists()) {
                    val tail = logFile.readLines().takeLast(10).joinToString("\n")
                    startupError = "Server exited with code $exitCode:\n$tail"
                }
            }
        } catch (e: Exception) {
            Log.e(TAG, "Failed to start Go process", e)
            startupError = "Failed to start: ${e.message}"
            isRunning = false
        }
    }

    private fun stopGoProcess() {
        goProcess?.let { proc ->
            Log.d(TAG, "Stopping Go process")
            // Send SIGTERM first for graceful shutdown (the Go server handles it)
            try {
                // On Android, Process.destroy() sends SIGTERM on Unix.
                proc.destroy()
                // Wait up to 3 seconds for graceful exit
                val exited = proc.waitFor(3, TimeUnit.SECONDS)
                if (!exited) {
                    Log.w(TAG, "Go process did not exit gracefully, sending SIGKILL")
                    proc.destroyForcibly()
                }
            } catch (e: Exception) {
                Log.e(TAG, "Error stopping Go process", e)
                try { proc.destroyForcibly() } catch (_: Exception) { }
            }
            goProcess = null
        }
        isRunning = false
    }

    // ── Notification ───────────────────────────────────────────────────────

    private fun buildNotification(): Notification {
        val pendingIntent = PendingIntent.getActivity(
            this,
            0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )

        val stopIntent = PendingIntent.getService(
            this,
            1,
            Intent(this, ReasonixService::class.java).apply { action = ACTION_STOP },
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )

        return NotificationCompat.Builder(this, ReasonixApp.CHANNEL_ID)
            .setContentTitle(getString(R.string.app_name))
            .setContentText(getString(R.string.notification_running))
            .setSmallIcon(R.drawable.ic_notification)
            .setOngoing(true)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .setContentIntent(pendingIntent)
            .addAction(android.R.drawable.ic_media_pause, "Stop", stopIntent)
            .build()
    }
}
