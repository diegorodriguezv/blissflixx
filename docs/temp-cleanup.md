# Temporary file cleanup

Design note. Not implemented, and nothing here is required for anything to work
— downloads are simply kept forever until this gets built.

## Why

Five places used to delete on stop, and one deleted on every startup:

| Site | What it removed |
|---|---|
| `peerflix -r` | peerflix's own flag, `--remove`: "remove files on exit" |
| `lib/player/pflixproc.py` | `/tmp/torrent-stream` |
| `blissflixx.py` `cleanup()` | `/tmp/torrent-stream` and all of `/tmp/blissflixx`, on every start |
| `lib/player/processpipe.py` `stop()` | `bf.out` |
| `lib/player/subsproc.py` `stop()` | the subtitle file |

The effect was that no download ever survived being stopped. For someone on a
slow connection or with poor seeds — which is exactly who is watching something
in the first place — a torrent could never finish, and a subtitle file was
refetched on every play.

Deleting is now never a side effect of stopping or restarting. All of it lives
in `/tmp/blissflixx`.

## What is in there

```
/tmp/blissflixx/
  torrent-stream/       peerflix's buffer (-f), told not to delete it
  <name>.srt            downloaded subtitles, from bin/getsubs.py
  cmdfifo               omxplayer command fifo
  mpv.sock              mpv IPC
  vlc.sock              VLC, unused: this VLC has no rc module
  bf.out                yt-dlp output, while downloading
  omxplayerdbus.$USER   omxplayer dbus socket and pid
```

## The problem with keeping everything

`/tmp` is tmpfs on a Raspberry Pi, so retained downloads consume **RAM**, not
disk. A few 1080p torrents is enough to matter on a 1 GB Pi. Nothing here should
be cleaned up automatically by default, but it cannot stay unbounded either.

## Proposed behaviour

1. **`player.keep_downloads`, default true.** When false, a download is removed
   once the play ends normally — not when it is stopped part-way, and not on
   error.
2. **Manual clear.** An API action and a UI button that delete the lot, with the
   total size shown first. Explicit, never automatic.
3. **Age-based cleanup, opt-in.** Off by default. When on, removes downloads
   untouched for *N* days (default 7).
4. **Never delete an incomplete or errored play.** This is the case the whole
   change exists for. A torrent still seeding, or one that failed on a flaky
   connection, is exactly what must survive. Age alone should never be a reason
   to remove something that was never finished.
5. **Disk-space warning, not deletion.** Above a threshold, tell the user how
   much is held and that it can be cleared. Removing anything the user did not
   ask for is the thing that was just fixed; a warning is the right size of
   response.

## Open questions

- Should age-based cleanup consider *last access* or *download completion*? A
  film half-watched and finished is not the same as one abandoned.
- Should a peerflix buffer be removed when the torrent is verified complete, or
  left alone because it doubles as a local copy?
- On a Pi, is tmpfs large enough that a swap file or a persistent mount under
  `~/` would be a better home than `/tmp` at all?