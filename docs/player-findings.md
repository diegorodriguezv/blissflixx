# Player findings

Recorded by the tests added in `test_player_commands.py`, `test_player_ready.py`
and `test_player_routing.py`.

Findings 1 to 4 below have since been acted on; see "Resolved" at the end. The
text is kept as written, because it is the record of what the tests exposed
before the abstraction existed.

## Reproducing

Everything here is checked by the suite. No Raspberry Pi, omxplayer, peerflix
or network required:

```
./virtualenv/bin/pytest test/test_player_commands.py test/test_player_ready.py test/test_player_routing.py
```

## What the tests cover

`_get_cmd()` does no I/O, and every `_ready()` reads solely through
`self._readline()`. Those two facts are the entire test seam:

| Stage | Command | Ready parsing | Control |
|---|---|---|---|
| yt-dlp | full argv | url / requested_formats / Destination / ERROR | — |
| omxplayer (dbus) | 3 branches in `start()` | shared | dbus, pause+resume only |
| omxplayer.bin (keys) | full string | shared | 17 actions |
| dlsrv | argv | rewrites outfile to `:9696` | — |
| peerflix | magnet + `-i` | rewrites outfile to `:9696` | — |
| subtitles | movie vs series argv | filename / error | — |
| localfile | none | emits path | — |

Plus `_Player.play()` pipeline composition across the `http`/`dlsrv`/`subs`
matrix, and `playr.play()` url-scheme routing.

## Findings

### 1. `OmxplayerProcess._get_cmd()` cannot be called on a fresh instance

```python
OmxplayerProcess()._get_cmd({"outfile": "http://x"})
# AttributeError: 'OmxplayerProcess' object has no attribute 'cmd'
```

Command construction is split across two methods: `start()` builds `self.cmd`
across four branches, then `super().start()` delegates to `_get_cmd()`, which
merely returns what `start()` already built. It is the only one of the seven
stages where the command depends on prior state, so it is the only one needing
a test helper. `omxproc2` builds its whole command inside `_get_cmd`.

Fixing this is folded into the abstraction rather than done separately: the
backend interface wants `build_command(args)` that works from scratch.

### 2. `omxproc`'s local-file branch raises a bare `KeyError` when `pid` is absent

```python
pid = args["pid"]   # omxproc.start(), local-file branch
```

`pid` is supplied only by the yt-dlp stage, which sets it in `_ready()`. Reaching
this branch without that upstream stage raises `KeyError`, not
`ProcessException`, and a `KeyError` escaping `start()` kills the stage thread
silently — the pipe then blocks forever with nothing reported to the parent.
This is the general hazard already pinned by
`test_processpipe.py::TestPipeErrors::test_unexpected_exception_in_start_leaves_the_pipe_hung`,
reachable here through ordinary mis-wiring rather than anything exotic.

`omxproc2` has no such requirement; it takes `outfile` alone.

### 3. The two omxplayer stages share `_ready()` verbatim

Compared as ASTs, `omxproc._ready` and `omxproc2._ready` are byte-for-byte
identical — 18 lines duplicated. The classes differ *only* in `control()`:

- `omxproc` shells out to `dbus.sh`, and handles `pause`/`resume` only
- `omxproc2` writes 17 different keys into a FIFO at `/tmp/cmdfifo`

So there are two control transports, not two players. Any new backend inherits
the duplicated `_ready()` unless the abstraction provides it.

### 4. Player selection is really control-transport selection

`lib/player/player.py` `play()`:

```python
if not http:
    if dlsrv: pipe.add_process(DlsrvProcess()); pipe.add_process(OmxplayerProcess2())
    else:     pipe.add_process(OmxplayerProcess())
else:
    pipe.add_process(OmxplayerProcess2())
```

The two booleans decide *how the player is controlled*, not which player runs.
There is no backend setting, no registry, and `lib/settings` has no player key.
Adding mpv or vlc today means another branch here and another `*proc.py`.

Exact current behaviour, now pinned by `TestPipelineComposition`:

| http | dlsrv | stages |
|---|---|---|
| False | True | src, dlsrv, omxplayer.bin |
| False | False | src, omxplayer |
| True | False | src, omxplayer.bin |
| True | True | src, omxplayer.bin |

A `subs` dict prepends a subtitles stage in every case.

### 5. 14 of the 17 control actions are unreachable from the frontend

The UI sends only `pause`, `resume` and `stop`. The rest — subtitle cycling,
±30s, ±600s, volume, audio-track switching — exist only as keys in
`omxproc2.control()`. Either the UI grew a keypad that was never wired to these,
or the actions were added server-side in anticipation. Worth knowing before an
abstraction decides which capabilities to expose per backend.

Note also that `stop` is a no-op on `omxproc` (finding 3), so on that path it
relies on `stop()` SIGKILLing the process group rather than a keypress.

## Still hardware-only

Three things cannot be checked without a Raspberry Pi, by construction:

1. whether omxplayer or mpv actually decodes and renders the stream
2. whether dbus or FIFO key injection reaches a running player
3. whether subtitles burn in correctly over the video

Everything upstream of the handover is covered. The point of a backend
abstraction is that those three become the *only* untested surface, rather than
being tangled up with command strings and readiness parsing.

## Proposed shape for the abstraction

As sketched above:

- `build_command(args)` -> argv or shell string, pure, callable on a fresh
  instance (finding 1)
- `parse_ready(lines)` or shared `_ready()` base (finding 3)
- `control(action)` transport, with a declared capability set per backend
  (finding 5)
- selection by name from settings, not by two booleans (finding 4)
- `omxplayer` and `omxplayer2` as two entries, then **mpv** as the first
  non-omxplayer backend, so the design is proven by a second implementation

## Resolved

### lib/player/backend.py, backends.py, mpvproc.py

`PlayerBackend` is the base: `build_command(args)` is pure, `control(action)` is
per-backend, and `declares()` reports a capability set so the UI can stop
offering controls that would be dropped. `OmxplayerBackend` holds the readiness
parsing the two variants shared.

- **Finding 1 fixed.** `OmxplayerProcess.build_command()` now builds the whole
  command from args. The `cmd` attribute and the `omxproc_cmd` test helper are
  gone. Verified byte-identical against the previous `start()` assembly for all
  four argument combinations.
- **Finding 2 fixed.** A missing `pid` raises `ProcessException` with a message
  naming the cause, so the failure surfaces instead of hanging the pipe. The
  test asserts the new behaviour rather than the old `KeyError`.
- **Finding 3 fixed.** The duplicated `_ready()` is one method on
  `OmxplayerBackend`. The AST-comparison test that recorded the duplication now
  guards it from re-diverging.
- **Finding 4 addressed.** `backends.py` registers `omxplayer`,
  `omxplayer-keys` and `mpv` by name; the backend comes from the `player`
  setting. `_legacy_backend()` keeps the old two-boolean choice byte-for-byte
  for unconfigured installs, verified across all four http/dlsrv combinations,
  so upgrading does not silently change anyone's player.
- **Finding 5 addressed.** Each backend advertises what it can do: the dbus
  variant declares only pause and subtitles, the FIFO and mpv variants declare
  everything. Still no UI for this; the capability set is available for it.
- **mpv implemented.** Different binary, JSON IPC over a unix socket instead of
  dbus or FIFO keystrokes, and readiness detected by polling for the socket
  because mpv emits no status line to parse. It is the implementation that
  proves the abstraction is not just omxplayer in three costumes.

### Not addressed

14 of 17 actions remain unreachable from the frontend. That is a UI question,
not a backend one, and the capability set is now there to answer it when
someone wants it.