# Phoenix

The release this branch is aiming at, what counts as done, and what is
deliberately left until after it.

Phoenix is not a redesign. It is the old BlissFlixx, made solid: the same
jobs, on current Raspberry Pi OS, with nothing on screen that the person
watching cannot make sense of.

The measure of success is the one from the beginning. Installing BlissFlixx
should be easy enough for someone who has never used it: one command, and a
television. That was real once, and it stopped being true when omxplayer was
retired and the things built around it began to rot one at a time. Phoenix is
about making it work like it did.

That is why the documentation is part of this and not an afterthought. An
install that works for someone who already knows the project is not the same
thing as one that works for someone who has just found it.

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
  for it. `mpv` has the capability; VLC does not
  advertise it and the buttons are hidden for it rather than shown and
  ineffective.
- **The overlay is one line at one position.** A marquee reads one line from
  one file, so two stacked messages need two overlays, and that has not been
  shown to work.

## After Phoenix

Noted, agreed, not started. In the order they were raised.

### Let the television sleep

BlissFlixx has no way to put the TV into its own low-power state. The display
stays on, lit, for as long as the Pi is booted.

It used to work. `blank_screen.sh` ran `tvservice -p`, which returns the TV to
its default settings, and at least some televisions read that blank output as
permission to drop the backlight. The user reports this as having worked
reliably on their set for years, with the screen waking again when a film
started.

It cannot be reinstalled rather than merely being broken:

    $ apt-cache search --names-only tvservice
    (nothing)

`tvservice` belonged to a display stack Raspberry Pi OS no longer ships. The
other path in the script, `xset dpms`, only ever ran on a desktop install and
needs an X server that a 'lite' install does not run.

So this needs a different mechanism, not a reinstall. The obvious candidate is
`vcgencmd display_power 0` and `1`, which is present on both a 4K and a 1080p
install and turns the HDMI output off at the firmware level. **What is not known
is whether that produces the same signal the television reads as sleep.** The
`tvservice` behaviour was something learned from using it, and it cannot be
inferred from the code.

Two design points to carry over from the original script:

- Keep the two-path shape. One mechanism for a headless install and one for a
  desktop, each used only where it applies. The original was deliberate about
  this, not a leftover.
- The screen has to come back. A service that blanks and never unblanks leaves
  a black television until someone intervenes, so waking has to be tied to
  playback starting rather than left to the TV.

Watch out for one thing: a stream that is buffering looks identical to one that
is playing. Anything that decides to blank must be driven by the player's state,
not by the clock, or a slow torrent could blank the screen while it is still
coming.

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