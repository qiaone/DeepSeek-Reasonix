package com.reasonix.app

import android.annotation.SuppressLint
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.ServiceConnection
import android.net.http.SslError
import android.os.Bundle
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.util.Log
import android.view.KeyEvent
import android.webkit.*
import androidx.appcompat.app.AppCompatActivity
import androidx.core.view.WindowCompat
import androidx.core.view.isVisible
import androidx.lifecycle.lifecycleScope
import com.reasonix.app.databinding.ActivityMainBinding
import kotlinx.coroutines.*

/**
 * Main activity: displays the Reasonix web UI in a full-screen WebView.
 *
 * On launch it:
 * 1. Checks if this is the first launch → shows SetupActivity
 * 2. Binds to ReasonixService (starts it if not running)
 * 3. Polls the HTTP server until it's ready, then loads the web UI
 */
class MainActivity : AppCompatActivity() {

    companion object {
        private const val TAG = "MainActivity"
        private const val SERVER_URL = "http://127.0.0.1:8787"
        private const val POLL_INTERVAL_MS = 1000L
        private const val POLL_MAX_ATTEMPTS = 120 // 2 minutes (bootstrap extraction on first launch can take 30-60s)
    }

    private lateinit var binding: ActivityMainBinding
    private var service: ReasonixService? = null
    private var serviceBound = false
    private var webViewLoaded = false
    private val handler = Handler(Looper.getMainLooper())
    private var pollAttempts = 0

    private val serviceConnection = object : ServiceConnection {
        override fun onServiceConnected(name: ComponentName?, binder: IBinder?) {
            val localBinder = binder as? ReasonixService.LocalBinder ?: return
            service = localBinder.getService()
            serviceBound = true
            Log.d(TAG, "Service connected, isRunning=${service?.isRunning}")

            if (service?.isRunning == true) {
                // Server already running — start loading immediately
                startPollingForServer()
            } else {
                // Server starting — show loading, service will start it
                startPollingForServer()
            }
        }

        override fun onServiceDisconnected(name: ComponentName?) {
            service = null
            serviceBound = false
            Log.d(TAG, "Service disconnected")
        }
    }

    // ── Lifecycle ──────────────────────────────────────────────────────────

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        // Keep content below status bar — prevents overlap with status bar icons
        WindowCompat.setDecorFitsSystemWindows(window, true)

        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        // First launch? → show setup
        if (ConfigHelper.isFirstLaunch(this)) {
            startActivity(Intent(this, SetupActivity::class.java))
            finish()
            return
        }

        setupWebView()
        bindService()
        setupRetryButton()
    }

    override fun onDestroy() {
        if (serviceBound) {
            try { unbindService(serviceConnection) } catch (_: Exception) { }
            serviceBound = false
        }
        handler.removeCallbacksAndMessages(null)
        super.onDestroy()
    }

    override fun onKeyDown(keyCode: Int, event: KeyEvent?): Boolean {
        // Handle back button in WebView
        if (keyCode == KeyEvent.KEYCODE_BACK && binding.webView.canGoBack()) {
            binding.webView.goBack()
            return true
        }
        return super.onKeyDown(keyCode, event)
    }

    // ── Service binding ────────────────────────────────────────────────────

    private fun bindService() {
        val intent = Intent(this, ReasonixService::class.java)
        bindService(intent, serviceConnection, Context.BIND_AUTO_CREATE)
        // Also start the service explicitly so it stays running if we unbind
        startService(intent)
    }

    // ── Server readiness polling ───────────────────────────────────────────

    private fun startPollingForServer() {
        pollAttempts = 0
        showLoading()
        pollServer()
    }

    private fun pollServer() {
        if (webViewLoaded) return

        lifecycleScope.launch(Dispatchers.IO) {
            val ok = try {
                val url = java.net.URL("$SERVER_URL/status")
                val conn = url.openConnection() as java.net.HttpURLConnection
                conn.connectTimeout = 2000
                conn.readTimeout = 2000
                conn.requestMethod = "GET"
                conn.responseCode == 200
            } catch (_: Exception) {
                false
            }

            withContext(Dispatchers.Main) {
                if (ok) {
                    Log.d(TAG, "Server is ready after ${pollAttempts + 1} polls")
                    loadWebView()
                } else if (pollAttempts < POLL_MAX_ATTEMPTS) {
                    pollAttempts++
                    handler.postDelayed({ pollServer() }, POLL_INTERVAL_MS)
                } else {
                    // Give up — show error
                    Log.e(TAG, "Server did not start after $POLL_MAX_ATTEMPTS attempts")
                    val err = service?.startupError
                    showError(err ?: getString(R.string.error_server_unreachable))
                }
            }
        }
    }

    // ── WebView setup ──────────────────────────────────────────────────────

    @SuppressLint("SetJavaScriptEnabled")
    private fun setupWebView() {
        val wv = binding.webView
        val settings = wv.settings

        with(settings) {
            javaScriptEnabled = true
            domStorageEnabled = true
            databaseEnabled = true
            allowFileAccess = false // security: don't let web UI read local files
            allowContentAccess = false
            mediaPlaybackRequiresUserGesture = false
            mixedContentMode = WebSettings.MIXED_CONTENT_NEVER_ALLOW
            // Prevent zooming — the Reasonix UI is responsive
            builtInZoomControls = false
            displayZoomControls = false
            loadWithOverviewMode = true
            useWideViewPort = true
            // Cache policy: prefer network while server is local
            cacheMode = WebSettings.LOAD_NO_CACHE
        }

        // Allow dark theme to match the web UI
        if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.TIRAMISU) {
            settings.isAlgorithmicDarkeningAllowed = false
        }

        wv.setWebViewClient(object : WebViewClient() {
            override fun onPageFinished(view: WebView?, url: String?) {
                super.onPageFinished(view, url)
                webViewLoaded = true
                handler.removeCallbacksAndMessages(null)
                showWebView()
            }

            override fun onReceivedError(
                view: WebView?,
                request: WebResourceRequest?,
                error: WebResourceError?
            ) {
                super.onReceivedError(view, request, error)
                if (request?.isForMainFrame == true) {
                    Log.e(TAG, "WebView error: ${error?.description} (code ${error?.errorCode})")
                    // Don't show error on the first load — polling will retry
                    if (webViewLoaded) {
                        showError("${error?.description}")
                    }
                }
            }

            override fun onReceivedSslError(
                view: WebView?,
                handler: SslErrorHandler?,
                error: SslError?
            ) {
                // Localhost doesn't use SSL, so this shouldn't happen
                Log.w(TAG, "SSL error (unexpected for localhost): ${error?.primaryError}")
                handler?.proceed() // proceed anyway for localhost
            }
        })

        // Chrome DevTools debugging (optional: uncomment for development)
        // if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.KITKAT) {
        //     WebView.setWebContentsDebuggingEnabled(true)
        // }
    }

    private fun loadWebView() {
        binding.webView.loadUrl(SERVER_URL)
        Log.d(TAG, "Loading WebView: $SERVER_URL")
    }

    // ── UI state helpers ───────────────────────────────────────────────────

    private fun showLoading() {
        binding.webView.isVisible = false
        binding.loadingOverlay.isVisible = true
        binding.errorOverlay.isVisible = false
    }

    private fun showWebView() {
        binding.webView.isVisible = true
        binding.loadingOverlay.isVisible = false
        binding.errorOverlay.isVisible = false
    }

    private fun showError(message: String) {
        binding.webView.isVisible = false
        binding.loadingOverlay.isVisible = false
        binding.errorOverlay.isVisible = true
        binding.errorText.text = message
    }

    private fun setupRetryButton() {
        binding.retryButton.setOnClickListener {
            showLoading()
            pollAttempts = 0
            pollServer()
        }
    }
}
