package com.via.meetkevin

import android.content.Context
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.NetworkType
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.util.Locale
import java.util.concurrent.TimeUnit

object Config {
    const val SITE = "https://irfan-via.github.io/meet-kevin-tracker/"
    const val STATUS = SITE + "data/status.json"
}

/** Every hour, checks the tracker's latest run and notifies about new videos, calls and big moves. */
class CheckWorker(ctx: Context, params: WorkerParameters) : CoroutineWorker(ctx, params) {

    override suspend fun doWork(): Result {
        return try {
            val conn = URL(Config.STATUS + "?t=" + System.currentTimeMillis()).openConnection() as HttpURLConnection
            conn.connectTimeout = 15000; conn.readTimeout = 15000
            val body = conn.inputStream.bufferedReader().use { it.readText() }
            check(JSONObject(body))
            Result.success()
        } catch (e: Exception) {
            Result.retry()
        }
    }

    private fun check(s: JSONObject) {
        val prefs = applicationContext.getSharedPreferences("tracker", Context.MODE_PRIVATE)
        val runAt = s.optString("run_at")
        if (runAt.isEmpty() || runAt == prefs.getString("last_run", "")) return

        val lines = mutableListOf<String>()
        val vids = s.optJSONArray("new_videos")
        val newCount = vids?.length() ?: 0
        if (newCount > 0) {
            for (i in 0 until newCount) lines += "▶ " + vids!!.getJSONObject(i).optString("title")
            val calls = s.optJSONArray("new_calls")
            if (calls != null && calls.length() > 0) {
                lines += "Calls: " + (0 until calls.length()).joinToString(", ") {
                    val c = calls.getJSONObject(it); c.optString("ticker") + " " + c.optString("stance")
                }
            }
        }
        // Only mention big moves that weren't in the last alert, so the same move doesn't repeat.
        val prevBig = prefs.getStringSet("last_big", emptySet()) ?: emptySet()
        val big = s.optJSONArray("big_moves")
        val nowBig = mutableSetOf<String>()
        val freshBig = mutableListOf<String>()
        if (big != null) for (i in 0 until big.length()) {
            val b = big.getJSONObject(i)
            val t = b.optString("ticker"); nowBig += t
            if (t !in prevBig) {
                val d = if (b.isNull("day_pct")) null else b.optDouble("day_pct")
                freshBig += t + (if (d != null) String.format(Locale.US, " %+.1f%%", d * 100) else "")
            }
        }
        if (freshBig.isNotEmpty()) lines += "Big moves: " + freshBig.joinToString(", ")

        prefs.edit().putString("last_run", runAt).putStringSet("last_big", nowBig).apply()
        if (lines.isEmpty()) return

        val title = if (newCount > 0) "New Meet Kevin video" + (if (newCount > 1) "s ($newCount)" else "") else "Meet Kevin tracker"
        Notifier.show(applicationContext, title, lines.joinToString("\n"))
    }

    companion object {
        fun schedule(ctx: Context) {
            val req = PeriodicWorkRequestBuilder<CheckWorker>(1, TimeUnit.HOURS)
                .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
                .build()
            WorkManager.getInstance(ctx).enqueueUniquePeriodicWork("tracker-check", ExistingPeriodicWorkPolicy.KEEP, req)
        }
    }
}
