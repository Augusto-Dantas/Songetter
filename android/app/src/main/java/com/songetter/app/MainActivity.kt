package com.songetter.app

import android.Manifest
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Environment
import android.provider.Settings
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.CheckCircle
import androidx.compose.material.icons.filled.CleaningServices
import androidx.compose.material.icons.filled.CloudSync
import androidx.compose.material.icons.filled.Download
import androidx.compose.material.icons.filled.Error
import androidx.compose.material.icons.filled.Folder
import androidx.compose.material.icons.filled.LibraryMusic
import androidx.compose.material.icons.filled.PhoneAndroid
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material.icons.filled.Search
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material.icons.filled.UploadFile
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilledTonalButton
import androidx.compose.material3.FilterChip
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.core.content.ContextCompat
import kotlinx.coroutines.launch
import org.json.JSONArray
import org.json.JSONObject
import java.util.Locale

private enum class AppTab(val label: String, val icon: ImageVector) {
    DOWNLOAD("Download", Icons.Default.Download),
    SEARCH("Search", Icons.Default.Search),
    LIBRARY("Library", Icons.Default.LibraryMusic),
    SETTINGS("Settings", Icons.Default.Settings),
}

private data class LibraryTrackUi(
    val filename: String,
    val artist: String,
    val title: String,
    val bitrate: Long,
    val fileSize: Long,
    val duration: Double,
    val format: String,
)

private data class SearchResultUi(
    val source: String,
    val title: String,
    val artist: String,
    val album: String,
    val url: String,
    val durationSeconds: Int,
)

private data class JobTrackUi(
    val url: String,
    val artist: String,
    val title: String,
    val status: String,
    val error: String,
    val lyricsEmbedded: Boolean,
)

private val SongetterColorScheme = darkColorScheme(
    primary = Color(0xFF1DB954),
    onPrimary = Color(0xFF00210B),
    secondary = Color(0xFF6DD58C),
    background = Color(0xFF101412),
    surface = Color(0xFF171D19),
    surfaceVariant = Color(0xFF222B25),
)

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            MaterialTheme(colorScheme = SongetterColorScheme) {
                SongetterApp()
            }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun SongetterApp() {
    val context = LocalContext.current
    val prefs = remember { context.getSharedPreferences("songetter_prefs", Context.MODE_PRIVATE) }
    val coroutineScope = rememberCoroutineScope()
    val snackbarHostState = remember { SnackbarHostState() }

    var activeTab by remember { mutableStateOf(AppTab.DOWNLOAD) }
    var libraryDir by remember {
        mutableStateOf(
            prefs.getString("library_dir", "")?.takeIf { it.isNotBlank() }
                ?: PythonBridge.defaultLibraryDir(context),
        )
    }
    var remoteUrl by remember {
        mutableStateOf(prefs.getString("remote_url", "http://192.168.1.100:8000") ?: "")
    }

    // Download tab state
    var downloadMode by remember { mutableStateOf("on_device") }
    var inputText by remember { mutableStateOf("") }
    var isCsvInput by remember { mutableStateOf(false) }
    var selectedCsvName by remember { mutableStateOf<String?>(null) }
    var isJobRunning by remember { mutableStateOf(false) }
    var jobSummary by remember { mutableStateOf<JSONObject?>(null) }
    var jobTracks by remember { mutableStateOf<List<JobTrackUi>>(emptyList()) }

    // Search tab state
    var searchQuery by remember { mutableStateOf("") }
    var searchSources by remember { mutableStateOf("deezer,itunes,musicbrainz") }
    var isSearching by remember { mutableStateOf(false) }
    var searchResults by remember { mutableStateOf<List<SearchResultUi>>(emptyList()) }
    var searchError by remember { mutableStateOf<String?>(null) }

    // Library tab state
    var isLibraryLoading by remember { mutableStateOf(false) }
    var libraryTracks by remember { mutableStateOf<List<LibraryTrackUi>>(emptyList()) }
    var libraryMessage by remember { mutableStateOf<String?>(null) }

    // Settings / diagnostics state
    var statusJson by remember { mutableStateOf<JSONObject?>(null) }
    var permissionRefreshTrigger by remember { mutableIntStateOf(0) }

    fun saveSettings(newLib: String, newRemote: String) {
        prefs.edit()
            .putString("library_dir", newLib.trim())
            .putString("remote_url", newRemote.trim())
            .apply()
    }

    fun parseLibraryTracks(resp: JSONObject): List<LibraryTrackUi> {
        val arr = resp.optJSONArray("tracks") ?: JSONArray()
        val list = ArrayList<LibraryTrackUi>(arr.length())
        for (i in 0 until arr.length()) {
            val obj = arr.optJSONObject(i) ?: continue
            list.add(
                LibraryTrackUi(
                    filename = obj.optString("filename", ""),
                    artist = obj.optString("artist", "Unknown Artist"),
                    title = obj.optString("title", "Unknown Title"),
                    bitrate = obj.optLong("bitrate", 0L),
                    fileSize = obj.optLong("file_size", 0L),
                    duration = obj.optDouble("duration", 0.0),
                    format = obj.optString("format", ""),
                ),
            )
        }
        return list
    }

    fun parseJobTracks(resp: JSONObject): List<JobTrackUi> {
        val arr = resp.optJSONArray("tracks") ?: JSONArray()
        val list = ArrayList<JobTrackUi>(arr.length())
        for (i in 0 until arr.length()) {
            val obj = arr.optJSONObject(i) ?: continue
            list.add(
                JobTrackUi(
                    url = obj.optString("url", ""),
                    artist = obj.optString("artist", ""),
                    title = obj.optString("title", ""),
                    status = obj.optString("status", ""),
                    error = obj.optString("error", ""),
                    lyricsEmbedded = obj.optBoolean("lyrics_embedded", false),
                ),
            )
        }
        return list
    }

    fun refreshLibrary() {
        coroutineScope.launch {
            isLibraryLoading = true
            val resp = PythonBridge.listLibrary(context, libraryDir)
            if (resp.optBoolean("ok", false)) {
                libraryTracks = parseLibraryTracks(resp)
                libraryMessage = if (resp.optBoolean("exists", false)) {
                    "${libraryTracks.size} track(s) in flat library"
                } else {
                    "Library directory will be created on first download"
                }
            } else {
                libraryMessage = resp.optString("error", "Unable to scan library")
            }
            isLibraryLoading = false
        }
    }

    fun refreshStatus() {
        coroutineScope.launch {
            statusJson = PythonBridge.getStatus(context, libraryDir, remoteUrl)
        }
    }

    val csvPickerLauncher = rememberLauncherForActivityResult(
        contract = ActivityResultContracts.OpenDocument(),
    ) { uri: Uri? ->
        if (uri != null) {
            try {
                val content = context.contentResolver.openInputStream(uri)
                    ?.bufferedReader()
                    ?.use { it.readText() }
                    ?: ""
                if (content.isNotBlank()) {
                    inputText = content
                    isCsvInput = true
                    selectedCsvName = uri.lastPathSegment ?: "selected.csv"
                    coroutineScope.launch {
                        snackbarHostState.showSnackbar("Loaded CSV (${content.lines().size} lines)")
                    }
                }
            } catch (exc: Exception) {
                coroutineScope.launch {
                    snackbarHostState.showSnackbar("Failed to read CSV: ${exc.message}")
                }
            }
        }
    }

    val permissionLauncher = rememberLauncherForActivityResult(
        contract = ActivityResultContracts.RequestMultiplePermissions(),
    ) {
        permissionRefreshTrigger++
    }

    LaunchedEffect(activeTab) {
        when (activeTab) {
            AppTab.LIBRARY -> refreshLibrary()
            AppTab.SETTINGS -> refreshStatus()
            else -> Unit
        }
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = {
                    Column {
                        Text("Songetter", fontWeight = FontWeight.Bold)
                        Text(
                            text = if (downloadMode == "remote") "Hybrid Android · Remote Sync" else "Hybrid Android · On-Device",
                            style = MaterialTheme.typography.labelSmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                    }
                },
            )
        },
        snackbarHost = { SnackbarHost(hostState = snackbarHostState) },
        bottomBar = {
            NavigationBar {
                AppTab.entries.forEach { tab ->
                    NavigationBarItem(
                        selected = activeTab == tab,
                        onClick = { activeTab = tab },
                        icon = { Icon(tab.icon, contentDescription = tab.label) },
                        label = { Text(tab.label) },
                    )
                }
            }
        },
    ) { innerPadding ->
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(innerPadding)
                .padding(horizontal = 16.dp, vertical = 12.dp),
        ) {
            when (activeTab) {
                AppTab.DOWNLOAD -> {
                    DownloadScreen(
                        downloadMode = downloadMode,
                        onModeChange = { downloadMode = it },
                        inputText = inputText,
                        onInputChange = {
                            inputText = it
                            if (it.isBlank()) {
                                selectedCsvName = null
                            }
                        },
                        isCsvInput = isCsvInput,
                        onToggleCsv = { isCsvInput = it },
                        selectedCsvName = selectedCsvName,
                        remoteUrl = remoteUrl,
                        libraryDir = libraryDir,
                        isRunning = isJobRunning,
                        jobSummary = jobSummary,
                        jobTracks = jobTracks,
                        onPickCsv = {
                            csvPickerLauncher.launch(
                                arrayOf("text/csv", "text/comma-separated-values", "text/plain", "*/*"),
                            )
                        },
                        onStartJob = {
                            if (inputText.isBlank()) {
                                coroutineScope.launch {
                                    snackbarHostState.showSnackbar("Enter Spotify/audio URLs or load a CSV file first.")
                                }
                            } else if (downloadMode == "remote" && remoteUrl.isBlank()) {
                                coroutineScope.launch {
                                    snackbarHostState.showSnackbar("Configure Remote Server URL in Settings first.")
                                }
                            } else {
                                coroutineScope.launch {
                                    isJobRunning = true
                                    val resp = PythonBridge.startJob(
                                        context = context,
                                        inputText = inputText,
                                        libraryDir = libraryDir,
                                        isCsv = isCsvInput,
                                        mode = downloadMode,
                                        remoteUrl = remoteUrl,
                                    )
                                    jobSummary = resp
                                    jobTracks = parseJobTracks(resp)
                                    isJobRunning = false
                                    val err = resp.optString("error", "")
                                    if (!resp.optBoolean("ok", false) && err.isNotBlank()) {
                                        snackbarHostState.showSnackbar("Job error: $err")
                                    } else {
                                        snackbarHostState.showSnackbar(
                                            "Done: ${resp.optInt("downloaded")} downloaded, " +
                                                "${resp.optInt("skipped")} skipped, ${resp.optInt("failed")} failed",
                                        )
                                    }
                                }
                            }
                        },
                    )
                }

                AppTab.SEARCH -> {
                    SearchScreen(
                        query = searchQuery,
                        onQueryChange = { searchQuery = it },
                        sources = searchSources,
                        onSourcesChange = { searchSources = it },
                        isSearching = isSearching,
                        results = searchResults,
                        errorMessage = searchError,
                        onSearch = {
                            if (searchQuery.isNotBlank()) {
                                coroutineScope.launch {
                                    isSearching = true
                                    searchError = null
                                    val resp = PythonBridge.search(context, searchQuery, searchSources, 15)
                                    if (resp.optBoolean("ok", false)) {
                                        val arr = resp.optJSONArray("results") ?: JSONArray()
                                        val parsed = ArrayList<SearchResultUi>(arr.length())
                                        for (i in 0 until arr.length()) {
                                            val obj = arr.optJSONObject(i) ?: continue
                                            parsed.add(
                                                SearchResultUi(
                                                    source = obj.optString("source", ""),
                                                    title = obj.optString("title", ""),
                                                    artist = obj.optString("artist", ""),
                                                    album = obj.optString("album", ""),
                                                    url = obj.optString("url", ""),
                                                    durationSeconds = obj.optInt("duration_seconds", 0),
                                                ),
                                            )
                                        }
                                        searchResults = parsed
                                    } else {
                                        searchError = resp.optString("error", "Search failed")
                                    }
                                    isSearching = false
                                }
                            }
                        },
                        onQueueTrack = { track ->
                            if (track.url.isNotBlank()) {
                                inputText = track.url
                                isCsvInput = false
                                activeTab = AppTab.DOWNLOAD
                                coroutineScope.launch {
                                    snackbarHostState.showSnackbar("Queued ${track.artist} - ${track.title}")
                                }
                            }
                        },
                    )
                }

                AppTab.LIBRARY -> {
                    LibraryScreen(
                        libraryDir = libraryDir,
                        tracks = libraryTracks,
                        isLoading = isLibraryLoading,
                        statusMessage = libraryMessage,
                        onRefresh = { refreshLibrary() },
                        onOrganize = {
                            coroutineScope.launch {
                                isLibraryLoading = true
                                val resp = PythonBridge.organizeLibrary(context, libraryDir)
                                if (resp.optBoolean("ok", false)) {
                                    val dups = resp.optInt("duplicates_count", 0)
                                    val org = resp.optInt("organized_count", 0)
                                    snackbarHostState.showSnackbar(
                                        "Organized $org track(s), resolved $dups duplicate group(s).",
                                    )
                                } else {
                                    snackbarHostState.showSnackbar(
                                        resp.optString("error", "Organize failed"),
                                    )
                                }
                                refreshLibrary()
                            }
                        },
                    )
                }

                AppTab.SETTINGS -> {
                    SettingsScreen(
                        libraryDir = libraryDir,
                        onLibraryDirChange = {
                            libraryDir = it
                            saveSettings(it, remoteUrl)
                        },
                        onResetDefaultDir = {
                            val defaultDir = PythonBridge.defaultLibraryDir(context)
                            libraryDir = defaultDir
                            saveSettings(defaultDir, remoteUrl)
                            refreshStatus()
                        },
                        remoteUrl = remoteUrl,
                        onRemoteUrlChange = {
                            remoteUrl = it
                            saveSettings(libraryDir, it)
                        },
                        statusJson = statusJson,
                        permissionTrigger = permissionRefreshTrigger,
                        onCheckStatus = { refreshStatus() },
                        onRequestMediaPermissions = {
                            val perms = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                                arrayOf(Manifest.permission.READ_MEDIA_AUDIO)
                            } else {
                                arrayOf(
                                    Manifest.permission.READ_EXTERNAL_STORAGE,
                                    Manifest.permission.WRITE_EXTERNAL_STORAGE,
                                )
                            }
                            permissionLauncher.launch(perms)
                        },
                        onOpenManageAllFiles = {
                            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                                val intent = try {
                                    Intent(
                                        Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION,
                                        Uri.parse("package:${context.packageName}"),
                                    )
                                } catch (_: Exception) {
                                    Intent(Settings.ACTION_MANAGE_ALL_FILES_ACCESS_PERMISSION)
                                }
                                context.startActivity(intent)
                            }
                        },
                    )
                }
            }
        }
    }
}

@Composable
private fun DownloadScreen(
    downloadMode: String,
    onModeChange: (String) -> Unit,
    inputText: String,
    onInputChange: (String) -> Unit,
    isCsvInput: Boolean,
    onToggleCsv: (Boolean) -> Unit,
    selectedCsvName: String?,
    remoteUrl: String,
    libraryDir: String,
    isRunning: Boolean,
    jobSummary: JSONObject?,
    jobTracks: List<JobTrackUi>,
    onPickCsv: () -> Unit,
    onStartJob: () -> Unit,
) {
    Column(
        modifier = Modifier.fillMaxSize(),
        verticalArrangement = Arrangement.spacedBy(10.dp),
    ) {
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            FilterChip(
                selected = downloadMode == "on_device",
                onClick = { onModeChange("on_device") },
                label = { Text("On-Device") },
                leadingIcon = {
                    Icon(Icons.Default.PhoneAndroid, contentDescription = null, modifier = Modifier.size(18.dp))
                },
            )
            FilterChip(
                selected = downloadMode == "remote",
                onClick = { onModeChange("remote") },
                label = { Text("Remote Sync") },
                leadingIcon = {
                    Icon(Icons.Default.CloudSync, contentDescription = null, modifier = Modifier.size(18.dp))
                },
            )
        }

        if (downloadMode == "remote") {
            Text(
                text = "Remote Server: ${remoteUrl.ifBlank { "(not configured in Settings)" }}",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        } else {
            Text(
                text = "Target Library: $libraryDir",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
            )
        }

        OutlinedTextField(
            value = inputText,
            onValueChange = onInputChange,
            label = {
                Text(if (isCsvInput) "CSV Payload" else "Spotify or Audio URLs (one per line)")
            },
            placeholder = {
                Text("https://open.spotify.com/track/... or paste CSV content")
            },
            modifier = Modifier
                .fillMaxWidth()
                .height(140.dp),
            maxLines = 8,
        )

        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.spacedBy(8.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            OutlinedButton(onClick = onPickCsv, enabled = !isRunning) {
                Icon(Icons.Default.UploadFile, contentDescription = null, modifier = Modifier.size(18.dp))
                Spacer(modifier = Modifier.width(6.dp))
                Text(selectedCsvName ?: "Pick CSV")
            }

            FilterChip(
                selected = isCsvInput,
                onClick = { onToggleCsv(!isCsvInput) },
                label = { Text("CSV Mode") },
            )

            Spacer(modifier = Modifier.weight(1f))

            Button(onClick = onStartJob, enabled = !isRunning) {
                if (isRunning) {
                    CircularProgressIndicator(modifier = Modifier.size(18.dp), strokeWidth = 2.dp)
                } else {
                    Icon(Icons.Default.Download, contentDescription = null, modifier = Modifier.size(18.dp))
                }
                Spacer(modifier = Modifier.width(6.dp))
                Text(if (isRunning) "Running..." else "Start")
            }
        }

        if (isRunning) {
            LinearProgressIndicator(modifier = Modifier.fillMaxWidth())
        }

        if (jobSummary != null) {
            Card(
                modifier = Modifier.fillMaxWidth(),
                colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceVariant),
            ) {
                Column(modifier = Modifier.padding(12.dp)) {
                    Text(
                        text = "Job Summary (${jobSummary.optString("mode", downloadMode)})",
                        style = MaterialTheme.typography.titleSmall,
                        fontWeight = FontWeight.SemiBold,
                    )
                    Text(
                        text = "Downloaded: ${jobSummary.optInt("downloaded")} · " +
                            "Skipped: ${jobSummary.optInt("skipped")} · " +
                            "Failed: ${jobSummary.optInt("failed")} · " +
                            "Lyrics Tagged: ${jobSummary.optInt("lyrics_embedded")}",
                        style = MaterialTheme.typography.bodySmall,
                    )
                    val err = jobSummary.optString("error", "")
                    if (err.isNotBlank() && err != "null") {
                        Text(
                            text = err,
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.error,
                        )
                    }
                }
            }
        }

        LazyColumn(
            modifier = Modifier.weight(1f),
            verticalArrangement = Arrangement.spacedBy(6.dp),
        ) {
            items(jobTracks) { track ->
                Card(modifier = Modifier.fillMaxWidth()) {
                    Row(
                        modifier = Modifier
                            .fillMaxWidth()
                            .padding(10.dp),
                        verticalAlignment = Alignment.CenterVertically,
                    ) {
                        Icon(
                            imageVector = if (track.status == "failed") Icons.Default.Error else Icons.Default.CheckCircle,
                            contentDescription = track.status,
                            tint = if (track.status == "failed") MaterialTheme.colorScheme.error else MaterialTheme.colorScheme.primary,
                        )
                        Spacer(modifier = Modifier.width(10.dp))
                        Column(modifier = Modifier.weight(1f)) {
                            val heading = listOf(track.artist, track.title)
                                .filter { it.isNotBlank() && it != "null" }
                                .joinToString(" - ")
                                .ifBlank { track.url }
                            Text(heading, fontWeight = FontWeight.Medium, maxLines = 1, overflow = TextOverflow.Ellipsis)
                            Text(
                                text = buildString {
                                    append(track.status.uppercase())
                                    if (track.lyricsEmbedded) append(" · USLT/Tag Lyrics Embedded")
                                    if (track.error.isNotBlank() && track.error != "null") append(" · ${track.error}")
                                },
                                style = MaterialTheme.typography.bodySmall,
                                color = MaterialTheme.colorScheme.onSurfaceVariant,
                                maxLines = 2,
                                overflow = TextOverflow.Ellipsis,
                            )
                        }
                    }
                }
            }
        }
    }
}

@Composable
private fun SearchScreen(
    query: String,
    onQueryChange: (String) -> Unit,
    sources: String,
    onSourcesChange: (String) -> Unit,
    isSearching: Boolean,
    results: List<SearchResultUi>,
    errorMessage: String?,
    onSearch: () -> Unit,
    onQueueTrack: (SearchResultUi) -> Unit,
) {
    Column(
        modifier = Modifier.fillMaxSize(),
        verticalArrangement = Arrangement.spacedBy(10.dp),
    ) {
        OutlinedTextField(
            value = query,
            onValueChange = onQueryChange,
            label = { Text("Search tracks, artists, or albums") },
            modifier = Modifier.fillMaxWidth(),
            singleLine = true,
        )
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.spacedBy(8.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            OutlinedTextField(
                value = sources,
                onValueChange = onSourcesChange,
                label = { Text("Sources (e.g. deezer,itunes,musicbrainz)") },
                modifier = Modifier.weight(1f),
                singleLine = true,
            )
            Button(onClick = onSearch, enabled = !isSearching && query.isNotBlank()) {
                Icon(Icons.Default.Search, contentDescription = null, modifier = Modifier.size(18.dp))
                Spacer(modifier = Modifier.width(6.dp))
                Text("Search")
            }
        }

        if (isSearching) {
            LinearProgressIndicator(modifier = Modifier.fillMaxWidth())
        }
        if (!errorMessage.isNullOrBlank()) {
            Text(errorMessage, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodySmall)
        }

        LazyColumn(
            modifier = Modifier.weight(1f),
            verticalArrangement = Arrangement.spacedBy(6.dp),
        ) {
            items(results) { item ->
                Card(modifier = Modifier.fillMaxWidth()) {
                    Row(
                        modifier = Modifier
                            .fillMaxWidth()
                            .padding(12.dp),
                        verticalAlignment = Alignment.CenterVertically,
                    ) {
                        Column(modifier = Modifier.weight(1f)) {
                            Text(
                                text = "${item.artist} - ${item.title}",
                                fontWeight = FontWeight.Medium,
                                maxLines = 1,
                                overflow = TextOverflow.Ellipsis,
                            )
                            Text(
                                text = buildString {
                                    append(item.source.uppercase())
                                    if (item.album.isNotBlank() && item.album != "null") append(" · ${item.album}")
                                    if (item.durationSeconds > 0) append(" · ${item.durationSeconds}s")
                                },
                                style = MaterialTheme.typography.bodySmall,
                                color = MaterialTheme.colorScheme.onSurfaceVariant,
                                maxLines = 1,
                                overflow = TextOverflow.Ellipsis,
                            )
                        }
                        if (item.url.isNotBlank()) {
                            IconButton(onClick = { onQueueTrack(item) }) {
                                Icon(Icons.Default.Download, contentDescription = "Queue Download")
                            }
                        }
                    }
                }
            }
        }
    }
}

@Composable
private fun LibraryScreen(
    libraryDir: String,
    tracks: List<LibraryTrackUi>,
    isLoading: Boolean,
    statusMessage: String?,
    onRefresh: () -> Unit,
    onOrganize: () -> Unit,
) {
    Column(
        modifier = Modifier.fillMaxSize(),
        verticalArrangement = Arrangement.spacedBy(10.dp),
    ) {
        Text(
            text = libraryDir,
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            maxLines = 1,
            overflow = TextOverflow.Ellipsis,
        )
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            OutlinedButton(onClick = onRefresh, enabled = !isLoading) {
                Icon(Icons.Default.Refresh, contentDescription = null, modifier = Modifier.size(18.dp))
                Spacer(modifier = Modifier.width(6.dp))
                Text("Refresh")
            }
            FilledTonalButton(onClick = onOrganize, enabled = !isLoading) {
                Icon(Icons.Default.CleaningServices, contentDescription = null, modifier = Modifier.size(18.dp))
                Spacer(modifier = Modifier.width(6.dp))
                Text("Deduplicate & Flatten")
            }
        }

        if (isLoading) {
            LinearProgressIndicator(modifier = Modifier.fillMaxWidth())
        }
        if (!statusMessage.isNullOrBlank()) {
            Text(statusMessage, style = MaterialTheme.typography.bodySmall)
        }

        LazyColumn(
            modifier = Modifier.weight(1f),
            verticalArrangement = Arrangement.spacedBy(6.dp),
        ) {
            items(tracks) { track ->
                Card(modifier = Modifier.fillMaxWidth()) {
                    Column(modifier = Modifier.padding(12.dp)) {
                        Text(
                            text = track.filename.ifBlank { "${track.artist} - ${track.title}" },
                            fontWeight = FontWeight.Medium,
                            maxLines = 1,
                            overflow = TextOverflow.Ellipsis,
                        )
                        val kbps = if (track.bitrate > 0) "${track.bitrate / 1000} kbps" else "? kbps"
                        val sizeMb = String.format(Locale.US, "%.2f MB", track.fileSize / (1024.0 * 1024.0))
                        Text(
                            text = "${track.artist} · ${track.title} · ${track.format.uppercase()} · $kbps · $sizeMb",
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                            maxLines = 1,
                            overflow = TextOverflow.Ellipsis,
                        )
                    }
                }
            }
        }
    }
}

@Composable
private fun SettingsScreen(
    libraryDir: String,
    onLibraryDirChange: (String) -> Unit,
    onResetDefaultDir: () -> Unit,
    remoteUrl: String,
    onRemoteUrlChange: (String) -> Unit,
    statusJson: JSONObject?,
    permissionTrigger: Int,
    onCheckStatus: () -> Unit,
    onRequestMediaPermissions: () -> Unit,
    onOpenManageAllFiles: () -> Unit,
) {
    val context = LocalContext.current
    val scrollState = rememberScrollState()

    val hasAudioPerm = remember(permissionTrigger) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            ContextCompat.checkSelfPermission(
                context,
                Manifest.permission.READ_MEDIA_AUDIO,
            ) == PackageManager.PERMISSION_GRANTED
        } else {
            ContextCompat.checkSelfPermission(
                context,
                Manifest.permission.WRITE_EXTERNAL_STORAGE,
            ) == PackageManager.PERMISSION_GRANTED
        }
    }
    val hasAllFilesAccess = remember(permissionTrigger) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            Environment.isExternalStorageManager()
        } else {
            true
        }
    }

    Column(
        modifier = Modifier
            .fillMaxSize()
            .verticalScroll(scrollState),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        OutlinedTextField(
            value = libraryDir,
            onValueChange = onLibraryDirChange,
            label = { Text("Local Music Library Path") },
            leadingIcon = { Icon(Icons.Default.Folder, contentDescription = null) },
            modifier = Modifier.fillMaxWidth(),
            singleLine = true,
        )

        OutlinedButton(onClick = onResetDefaultDir) {
            Text("Use Default Android Music Folder")
        }

        OutlinedTextField(
            value = remoteUrl,
            onValueChange = onRemoteUrlChange,
            label = { Text("Remote Songetter Server URL") },
            placeholder = { Text("http://192.168.1.100:8000") },
            leadingIcon = { Icon(Icons.Default.CloudSync, contentDescription = null) },
            modifier = Modifier.fillMaxWidth(),
            singleLine = true,
        )

        HorizontalDivider()

        Text("Storage Permissions", style = MaterialTheme.typography.titleSmall, fontWeight = FontWeight.SemiBold)
        Text(
            text = "Media Audio Permission: ${if (hasAudioPerm) "Granted" else "Not Granted"}",
            style = MaterialTheme.typography.bodySmall,
        )
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            Text(
                text = "All Files Access (Shared /Music): ${if (hasAllFilesAccess) "Granted" else "Not Granted"}",
                style = MaterialTheme.typography.bodySmall,
            )
        }

        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Button(onClick = onRequestMediaPermissions) {
                Text("Grant Audio Access")
            }
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                OutlinedButton(onClick = onOpenManageAllFiles) {
                    Text("All Files Access")
                }
            }
        }

        HorizontalDivider()

        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Text("Python Bridge Diagnostics", style = MaterialTheme.typography.titleSmall, fontWeight = FontWeight.SemiBold)
            OutlinedButton(onClick = onCheckStatus) {
                Text("Refresh Status")
            }
        }

        if (statusJson != null) {
            Card(modifier = Modifier.fillMaxWidth()) {
                Column(modifier = Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                    Text("Python: ${statusJson.optString("python_version", "?")}", style = MaterialTheme.typography.bodySmall)
                    Text("Library Exists: ${statusJson.optBoolean("library_exists")}", style = MaterialTheme.typography.bodySmall)
                    Text("Library Writable: ${statusJson.optBoolean("library_writable")}", style = MaterialTheme.typography.bodySmall)
                    Text("Local Tracks: ${statusJson.optInt("track_count")}", style = MaterialTheme.typography.bodySmall)
                    Text("yt-dlp Module: ${statusJson.optBoolean("yt_dlp_available")}", style = MaterialTheme.typography.bodySmall)
                    Text("Mornye Binary: ${statusJson.optBoolean("mornye_available")}", style = MaterialTheme.typography.bodySmall)
                    if (statusJson.has("remote_reachable") && !statusJson.isNull("remote_reachable")) {
                        Text(
                            text = "Remote Reachable: ${statusJson.optBoolean("remote_reachable")} " +
                                "(tracks: ${statusJson.opt("remote_track_count")})",
                            style = MaterialTheme.typography.bodySmall,
                        )
                    }
                }
            }
        }
    }
}
