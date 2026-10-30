package com.via.meetkevin

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.swiperefreshlayout.widget.SwipeRefreshLayout

class MainActivity : Activity() {
    private lateinit var web: WebView
    private lateinit var refresh: SwipeRefreshLayout

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        web = WebView(this)
        refresh = SwipeRefreshLayout(this)
        refresh.addView(web)
        setContentView(refresh)

        web.settings.javaScriptEnabled = true
        web.settings.domStorageEnabled = true
        web.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                val url = request.url
                // Keep the tracker inside the app; open YouTube links in the YouTube app/browser.
                return if (url.host == Uri.parse(Config.SITE).host) false else {
                    startActivity(Intent(Intent.ACTION_VIEW, url)); true
                }
            }
            override fun onPageFinished(view: WebView, url: String) { refresh.isRefreshing = false }
        }
        refresh.setOnRefreshListener { web.reload() }
        refresh.setOnChildScrollUpCallback { _, _ -> web.scrollY > 0 }
        if (savedInstanceState == null) web.loadUrl(Config.SITE) else web.restoreState(savedInstanceState)

        Notifier.createChannel(this)
        CheckWorker.schedule(this)
        if (Build.VERSION.SDK_INT >= 33 &&
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 1)
        }
    }

    override fun onResume() { super.onResume(); web.reload() }
    override fun onSaveInstanceState(outState: Bundle) { super.onSaveInstanceState(outState); web.saveState(outState) }
    @Deprecated("Deprecated in Java")
    override fun onBackPressed() { if (web.canGoBack()) web.goBack() else super.onBackPressed() }
}
