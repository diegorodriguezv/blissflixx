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
  - **Reddit posts come from an archive, and search is the weak part.** Six
    channels read their posts from Arctic Shift rather than from Reddit, which
    no longer answers. Feeds are reliable; search is not. Measured from here,
    a plain subreddit listing answered 6 times in 6, while a title search
    answered 4 in 10 -- with the same query returning results on one attempt
    and a timeout on the next. Search retries five times before giving up, so
    it usually answers, but it can take half a minute, and a term the archive
    cannot search will fail rather than return nothing. A channel whose only
    listing is a `top` feed is also an approximation now: the archive cannot
    rank by score, so it reads as the highest scoring posts within a date
    window instead. See the module docstring in `lib/chanutils/reddit`.

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
### Popular videos, from YouTube or an aggregator

There was a YouTube channel (`bfch_youtube`) built on Reddit's sitewide
`/domain/youtube.com` listing: whatever anyone posted anywhere on Reddit that
linked to YouTube. Retired rather than repaired.

It went the way the other Reddit channels went, and for the same reason -- see
"Reddit posts come from an archive now" below for how those work. But this one
does not come back with the others, because what it listed is not a subreddit.
It was everything across all of Reddit matching one domain, and the archive
those channels now read can only search within a named subreddit. Substituting
one would return a different thing that looked the same: posts from some
subreddit about YouTube, not posts from everywhere about YouTube. A channel
that is quietly wrong is worse than one that is absent, so it was removed
instead.

What is actually wanted is a way to play popular videos without going through
Reddit at all, which is a different feature with a different set of sources to
weigh:

- YouTube's own feeds and search, through the bundled yt-dlp. The plumbing is
  already there, since every video play goes through it. The question is
  whether YouTube's own endpoints are reachable and stable enough to depend on,
  which is the same question Reddit's were, once.
- An aggregator that serves a plain list of what's popular, with no account and
  no key. Arctic Shift is one of these for Reddit specifically; the equivalent
  for video is not settled.

Worth deciding on its own terms rather than as a repair. Not before Phoenix.
