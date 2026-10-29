# Temporary file cleanup

Design note. Not implemented, and nothing here is required for anything to work
— downloads are simply kept forever until this gets built.

## Why

Five places used to delete on stop, and one deleted on every startup:

| Site | What it removed |
|---|---|
| the old torrent source's `-r` flag | its own `--remove`: "remove files on exit" (both gone) |
| the old torrent stage | `/tmp/torrent-stream` (gone) |
| `blissflixx.py` `cleanup()` | `/tmp/torrent-stream` and all of `/tmp/blissflixx`, on every start |
| `lib/player/processpipe.py` `stop()` | `bf.out` |
| `lib/player/subsproc.py` `stop()` | the subtitle file |

The effect was that no download ever survived being stopped. For someone on a
slow connection or with poor seeds — which is exactly who is watching something
in the first place — a torrent could never finish, and a subtitle file was
refetched on every play.

Deleting is now never a side effect of stopping or restarting. Ephemeral files
live in `/tmp/blissflixx`; downloads live on the card.

## What is in there

```
/tmp/blissflixx/
  <name>.srt            downloaded subtitles, from bin/getsubs.py
  mpv.sock              mpv IPC
  vlc.sock              VLC, unused: this VLC has no rc module
  bf.out                yt-dlp output, while downloading
```

```
data/downloads/
  torrent-stream/       the download buffer, told to the torrent process with --dir
  torrent-stream/.bf-torrent/
                        <info hash>.torrent and <info hash>.fastresume
```

Nothing torrent-related is written to `/tmp` any more. The old source kept its
own `<hash>.torrent` metadata in `/tmp/torrent-stream`, a path its library
hardcoded; the torrent process keeps metadata and resume data with the download,
so a rewatch needs no network and no hashing, and `/tmp` holds only sockets and
fifos.

## Why downloads are not in /tmp

This was not a theory. The buffer sat in `/tmp/blissflixx/torrent-stream` and a
torrent filled it to 100% (371 MB of 371 MB), after which every write failed with
`ENOSPC` — including the file the on-screen confirmation is written to, so the
overlay silently disappeared. The subtitle overlay stopped too.

`/tmp` is a tmpfs here, and not by Debian policy: `tmp.mount` is a **static
systemd unit** shipped with `Options=mode=1777,...,size=50%%`, so it mounts
`tmpfs` on `/tmp` sized at half of RAM on any system where `/tmp` is a real
directory. On a 741 MB Pi that is 371 MB. There is no fstab entry and nothing
custom — a fresh install gets it too.

A 1080p film is 2–4 GB. Against 371 MB of RAM it cannot finish, and no amount
of tuning makes it fit. Downloads therefore go on the card, which has ~22 GB free.

The card is slower than tmpfs, but the comparison was never real: the alternative
is not a faster disk, it is not finishing the film.

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
- Should a torrent buffer be removed when the film is complete, or left alone
  because it doubles as a local copy?
- On a Pi, is tmpfs large enough that a swap file or a persistent mount under
  `~/` would be a better home than `/tmp` at all?