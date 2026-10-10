# Phoenix

The release this branch is aiming at, what counts as done, and what is
deliberately left until after it.

Phoenix is not a redesign. It is the old BlissFlixx, made solid: the same
jobs, on current Raspberry Pi OS, with nothing on screen that the person
watching cannot make sense of.

## What counts as done

1. **Every control action is visible.** Pressing something says what happened,
   on the picture, in words. A button that does nothing is indistinguishable
   from a button that was not pressed.
2. **Nothing is asked of the player that it cannot keep answering.** See
   "the interface goes quiet" below; this has cost more time than anything
   else in the VLC work.
3. **Nothing is deleted that the person did not ask to have deleted.** Not on
   stop, not on restart, not automatically.
4. **A film fits on the disk.** It did not on tmpfs, and could not.
5. **Readable on a television from a sofa**, which is where it is watched.

## Known limits, agreed

- **Subtitle delay is not available on VLC.** Its cli interface has no verb
  for it. `mpv` and `omxplayer-keys` have the capability; VLC does not
  advertise it and the buttons are hidden for it rather than shown and
  ineffective.
- **`omxplayer` is not available** on current Raspberry Pi OS, so parity is
  judged from memory rather than by measurement.
- **The overlay is one line at one position.** A marquee reads one line from
  one file, so two stacked messages need two overlays, and that has not been
  shown to work.

## After Phoenix

Noted, agreed, not started. In the order they were raised.

### Say what is happening while a stream loads

A torrent can take the better part of a minute to start producing, and all
of that time the screen says `LOADING STREAM`. Long enough that it reads as
frozen rather than as waiting.

What it should say instead is open: what stage it has reached — connecting,
finding peers, downloading, opening — and, where the numbers are known, how
far along it is. The stages are already logged by `peerflix` and the download
stage already knows its size, so this is a matter of surfacing what exists
rather than measuring anything new.

The difficulty is the same one as everywhere else in the VLC work: this
wants to report progress over time, and continuous questioning is what stops
the interface answering. It has to be built so that a report is never worth
a stalled seek.

### Volume in finer steps

Ten percent per press is what the television wants. Ten presses to silence is
a lot of pressing, and the difference between 40% and 45% matters more at the
bottom of the range than it does at the top. Not before Phoenix.