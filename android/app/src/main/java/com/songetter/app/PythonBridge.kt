package com.songetter.app

import android.content.Context
import android.os.Environment
import com.chaquo.python.PyObject
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.io.File

object PythonBridge {
    private const val MODULE_NAME = "songetter.android_bridge"
    private val initLock = Any()

    private fun ensureModule(context: Context): PyObject {
        synchronized(initLock) {
            if (!Python.isStarted()) {
                Python.start(AndroidPlatform(context.applicationContext))
            }
        }
        return Python.getInstance().getModule(MODULE_NAME)
    }

    fun defaultLibraryDir(context: Context): String {
        val sharedMusic = File("/storage/emulated/0/Music")
        if (sharedMusic.exists() && sharedMusic.isDirectory && sharedMusic.canWrite()) {
            return File(sharedMusic, "Songetter").absolutePath
        }
        val appMusic = context.getExternalFilesDir(Environment.DIRECTORY_MUSIC)
        if (appMusic != null) {
            return File(appMusic, "Songetter").absolutePath
        }
        return File(context.filesDir, "Music/Songetter").absolutePath
    }

    private fun parseJsonSafe(raw: String): JSONObject {
        return try {
            JSONObject(raw)
        } catch (exc: Exception) {
            JSONObject().apply {
                put("ok", false)
                put("error", "Invalid JSON returned from Python bridge: ${exc.message}")
                put("raw", raw)
            }
        }
    }

    private suspend fun invokeBridge(
        context: Context,
        block: (PyObject) -> String,
    ): JSONObject = withContext(Dispatchers.IO) {
        try {
            val module = ensureModule(context)
            val jsonStr = block(module)
            parseJsonSafe(jsonStr)
        } catch (exc: Throwable) {
            JSONObject().apply {
                put("ok", false)
                put("error", exc.message ?: exc.javaClass.simpleName)
            }
        }
    }

    suspend fun getStatus(
        context: Context,
        libraryDir: String = "",
        remoteUrl: String = "",
    ): JSONObject = invokeBridge(context) { module ->
        module.callAttr("bridge_get_status", libraryDir, remoteUrl).toString()
    }

    suspend fun listLibrary(
        context: Context,
        libraryDir: String = "",
    ): JSONObject = invokeBridge(context) { module ->
        module.callAttr("bridge_list_library", libraryDir).toString()
    }

    suspend fun startJob(
        context: Context,
        inputText: String,
        libraryDir: String = "",
        isCsv: Boolean = false,
        mode: String = "on_device",
        remoteUrl: String = "",
    ): JSONObject = invokeBridge(context) { module ->
        module.callAttr(
            "bridge_start_job",
            inputText,
            libraryDir,
            isCsv,
            mode,
            remoteUrl,
        ).toString()
    }

    suspend fun search(
        context: Context,
        query: String,
        sourcesCsv: String = "",
        limit: Int = 10,
    ): JSONObject = invokeBridge(context) { module ->
        module.callAttr("bridge_search", query, sourcesCsv, limit).toString()
    }

    suspend fun organizeLibrary(
        context: Context,
        libraryDir: String = "",
    ): JSONObject = invokeBridge(context) { module ->
        module.callAttr("bridge_organize_library", libraryDir).toString()
    }
}
