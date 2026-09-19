# STREAMING.md — Can playback start before the download finishes?

**Short answer: not with the current pipeline, and not worth changing for a fast
link.** One blocker is structural (the file being served does not exist until the
download is merged) and the rest are in how the file is served and consumed.

This document records the question, the evidence, the options, and one correction
to an earlier claim made during the investigation.

---

## How a file is obtained and served today

The pipeline is strictly sequential. `cli.py` calls the downloader and only then
the AirPlay stage, so the second stage cannot begin before the first returns:

1. `cli.py:85` prints `[1/2] Downloading ...` and calls
   `downloader.download(url)` (`cli.py:88` for `--keep`, `cli.py:96` otherwise).
2. `downloader.download()` (`downloader.py:281`) runs yt-dlp and returns only once
   a merged output exists. `_resolve_output` (`downloader.py:172`) looks for
   `base.mp4` and falls back to the newest media file in the destination.
3. `cli.py:106` prints `[2/2] Streaming ...` and calls
   `airplay.stream(media_path, ...)` (`cli.py:109`).
4. Inside pyatv, `play_url` notices that the argument is a local path
   (`pyatv/protocols/airplay/__init__.py:118`, `if os.path.exists(url)`) and
   starts a local HTTP server for it:

   ```python
   server = StaticFileWebServer(url, str(server_address))
   await server.start()
   url = server.file_address
   ```

5. `StaticFileWebServer` (`pyatv/support/http.py:608`) is a thin aiohttp
   `web.Application` with `add_static("/", self.path.parent)` and a middleware that
   allows only the one filename. aiohttp then serves byte ranges from the file on
   disk.

So the Apple TV is reading, over HTTP, a file that yt-dlp and ffmpeg produced
beforehand. There is no streaming path from the network source to the receiver.

---

## Blockers

### 1. During the download there is no file to serve (structural)

The format selector requests separate video and audio streams
(`downloader.py:47`):

```
bv[vcodec^=vp9]+ba[acodec^=mp4a]
/ bv[vcodec^=avc1]+ba[acodec^=mp4a]
/ bv+ba/b
```

yt-dlp downloads those as separate temporary files and merges them into one MP4
(`merge_output_format = "mp4"`, `downloader.py:65` and `downloader.py:151`). While
that runs, the destination directory holds only `.f<id>` parts and `.part` files.
The name the server would serve, `base.mp4`, does not exist yet. Playing early
would mean serving a file that is not there.

This is the real blocker. Even a perfect HTTP server cannot help until something
playable exists on disk.

### 2. The container itself is fine

An earlier claim in this investigation was that the merged MP4 is not
progressively playable because ffmpeg writes the `moov` index after `mdat`. **That
was wrong.** Inspecting the file produced by the real run:

```
file size: 362111397
  ftyp @ 0        size=28
  moov @ 28       size=137909
  free @ 137937   size=8
  mdat @ 137945   size=361973452
```

`moov` sits at offset 28, before `mdat`, so the index is available from the first
bytes and a player can begin on a partial file. The cause is that yt-dlp passes
`-movflags +faststart` to the merge
(`yt_dlp/postprocessor/ffmpeg.py:340`), so every merged output is faststart-ready
without any effort from this project.

This removes one suspected blocker. It does not remove blocker 1, because the
faststarted file still only appears after the merge.

### 3. aiohttp sizes each response from a single stat

aiohttp's file response reads the size once per request and fixes
`Content-Length` from it (`aiohttp/web_fileresponse.py:309` and `:401`):

```python
file_size: int = st.st_size
...
self.content_length = count
```

A file that keeps growing after that point is served only up to the size known
when the request started. The receiver would hit an early end of stream, or see a
`Content-Length` that no longer matches the resource. Serving a growing file needs
a handler that waits for more bytes instead of trusting one stat.

### 4. The receiver treats the resource as seekable

`AIRPLAY.md` records the Apple TV issuing repeated HTTP **206 partial-content**
range requests for the duration of playback. A range server that is following a
growing file has to answer ranges that may not exist yet, and has to cope with
probes to the tail for duration and seeking. Those probes are exactly the
operations a partially downloaded file cannot satisfy.

---

## What a working version would require

Any of these is a real change to the download or serving stage, not a flag:

- **Serve a progressive source.** Request a single pre-muxed format (for example
  H.264 + AAC `best[ext=mp4]`) and write it straight to the served path so bytes
  appear sequentially. The cost is quality: progressive renditions are typically
  capped around 720p and lose the VP9/4K selection that is this project's point
  (`downloader.py:47`, `downloader.py:55`).
- **Fragment the output.** Pipe the separate streams through ffmpeg with
  `-movflags frag_keyframe+empty_moov+default_base_moof` so playback can start on
  a growing fragmented MP4 while keeping VP9. This needs a custom download path
  alongside the existing yt-dlp merge.
- **Replace pyatv's file server.** `StaticFileWebServer` cannot serve a growing
  file (blocker 3). A replacement has to track file growth, block on reads past
  the current end, and answer ranges against a moving boundary.

All three have to be combined; none alone produces early playback.

---

## Is it worth doing?

Measured on the machine used for testing, on a real YouTube URL:

| Stage | Observed |
|---|---|
| Video stream | 342.00 MiB in 00:00:06 at 51.65 MiB/s |
| Audio stream | 3.29 MiB in under a second |
| Merged output on disk | 362,111,397 bytes (`dQw4w9WgXcQ.mp4`) |

The download wait is seconds. The latency a user actually notices is discovery,
pairing, and setting up the AirPlay session, not the download. Early playback
would save a few seconds on a fast link while adding a growth-aware HTTP server,
a second download path, and new failure modes around seeks into missing bytes.

Recommendation: leave the pipeline sequential. Revisit only if the target link is
slow enough that downloads take minutes, in which case the fragmented-output
option keeps the quality selection intact.

---

## Test status and caveats

- The `--keep` download path was broken before this test by an earlier refactor
  in this repository: `_build_opts` still called `_safe_video_id(url)` after its
  `url` parameter was removed, so it raised `NameError: name 'url' is not
  defined`. Fixed by passing the already-computed stem. Earlier verification
  claims did not cover a download, so this went unnoticed.
- After the fix the download and merge succeeded and produced
  `~/Videos/airplay-yt/dQw4w9WgXcQ.mp4` at the size above.
- **The streaming leg was not confirmed.** The log ends at
  `[2/2] Streaming dQw4w9WgXcQ.mp4 to Living Room Apple TV ...` with no `done`
  line, and no exit code was captured, because the run was superseded mid-stream.
  Playback should be re-tested before relying on this run as evidence that the
  `--keep` path works end to end. The download half is confirmed; the AirPlay half
  of that run is not.
- The measurements above are single-run figures from one machine and one network,
  not a benchmark.
- Unrelated files exist in the same directory (`watch_v=mQztu0HLIv4.mp4`,
  `oxhlMDmn6II.mp4`); the latter was created after this run started and is not from
  this test.

---

## References

- `src/airplay_yt/cli.py:85`, `:88`, `:96`, `:106`, `:109` — sequential stages.
- `src/airplay_yt/downloader.py:47`, `:55`, `:65`, `:151`, `:172`, `:281` — format
  selector, sort, merge format, output resolution, entry point.
- `pyatv/protocols/airplay/__init__.py:118` — local path triggers the web server.
- `pyatv/support/http.py:608` — `StaticFileWebServer`, aiohttp `add_static`.
- `aiohttp/web_fileresponse.py:309`, `:401` — size sampled once per request.
- `yt_dlp/postprocessor/ffmpeg.py:340` — unconditional `-movflags +faststart`.
- `AIRPLAY.md` — receiver behaviour, including the repeated 206 range requests.
