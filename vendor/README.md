# Vendored binaries

Executables committed to the repository because nothing here builds them.
They are not interchangeable with anything else, so each one says what it is
and where it came from.

## `dlsrv`

The download server. A small HTTP server that serves one *growing* file, with
Range support, so a player can open a file while it is still being downloaded
and seek into the part that exists. The player starts it as a pipeline stage
(`DlsrvProcess`) for a source that has to download to disk first -- today that
is the yt-dlp path that cannot stream.

- Source: `github.com/blissland/dlsrv` (Go), compiled on a Raspberry Pi in
  April 2015 (the git history has it as "New dlsrv + various fixes").
- Binary: 32-bit ARM (armhf), committed unstripped. It runs on 32-bit Pi OS
  directly, and on 64-bit Pi OS through 32-bit compatibility.
- It listens on `127.0.0.1:9696`, serves the file named on its command line,
  and is given the downloader's pid so it does not outlive the play.

Nothing here rebuilds it. If it ever has to change, replacing it with a small
range server inside the application is the better direction -- but only after
a real yt-dlp download-to-disk play has been tested on a Pi.
