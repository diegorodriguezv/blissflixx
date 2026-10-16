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
- **Reddit posts come from an archive, and the narrowed feeds are the weak
  part.** Six channels read their posts from Arctic Shift rather than from
  Reddit, which no longer answers. A plain subreddit listing answers in about
  two seconds and has not failed. Queries that narrow the listing -- the
  thirty-odd flair feeds in the documentaries channel, the word-search feeds in
  the shortfilms channel, and the search box -- are much weaker: they take
  around seven seconds when they work, and roughly a third of them are refused
  outright with a `422` in about a second and a quarter.

  That is a property of the archive, not a bug that can be fixed here, so the
  work is in not letting it reach the person watching. Three things:

  - A failed read is never cached. It used to be, as an empty list for five
    minutes, which is how one feed sat there silently showing nothing.
  - The whole read is bounded. Retries are a flat second apart rather than
    backing off, and the ceiling is fifteen seconds rather than thirty. The
    worst case measured went from 31.8 seconds to 10.5.
  - A refused archive is reported as what it is. The channel answers `503` and
    the interface shows a sentence saying the archive is busy, rather than an
    empty list that looks like a channel with nothing in it.

  A channel with a `top` feed is also an approximation: the archive cannot rank
  by score, so it reads as the highest scoring posts within a date window
  instead. See the module docstring in `lib/chanutils/reddit`.

  One thing that was ours, and is fixed: flair filters were only being read in
  the quoted form. Written as `flair:Action` and not `flair:"Action"`, every
  one of the twenty-two movie feeds was being served unfiltered, and every feed
  in the channel showed the same thirty posts.

## Channels

Every channel on the test instance was swept: every feed and every search, each
one loaded and read, watching for empty lists, `500`s and stalls. That is the
part of Phoenix that cannot be automated away, because a channel is not broken
or working -- it answers with a list and you have to decide whether that list
is the thing it claims to be.

Most of what was wrong was quietly wrong rather than loudly wrong, which is why
it survived so long. A feed that silently ignored its own filter and showed
the same thirty posts every time looked like a working feed. So did a channel
whose selector no longer matched anything, returning an empty page that parsed
cleanly.

One channel and three smaller entries could not be repaired, and were taken out
rather than left in place:

| Removed | Why |
|---|---|
| **Vimeo** (whole channel) | vimeo.com answers a Raspberry Pi with a Cloudflare challenge page. The page returns `200`, so it reads as success. yt-dlp finds no player config, falls back to a client that needs a login, and every item fails. Its own API is reachable but no longer carries media URLs. Playable only with a Vimeo account. |
| **IPTV.org "NSFW"** | The whole world list holds exactly one such stream, and it did not play. A menu entry listing a single dead stream is worse than not offering it. |
| **TechCrunch "TCTV News"** | The page is a `404`. Search results for the term answer `200` and ignore the query, which is a way of failing that looks like success. |
| **EZTV search** | The API takes an IMDb id and nothing else. There is no title search to map a query onto. |

Two needed more than a selector. **TechCrunch** items are article pages, and
yt-dlp has no extractor for techcrunch.com, so every video in the channel
failed with `Unsupported URL`. The page names the JW Player playlist the video
is served from, and that playlist plays -- so the article URL is exchanged for
the playlist before yt-dlp ever sees it.

**Rotten Tomatoes**, **BBC iPlayer**, **EZTV** and **TechCrunch** had selectors
that no longer matched anything, and were re-read against markup that has since
moved. Rotten Tomatoes parses the structured item list the browse pages
publish, BBC iPlayer matches the current grid and falls back to the carousel
layout that replaced it, EZTV reads the site's own torrent API rather than
scraping a page, and TechCrunch matches the current card and falls back to the
one it replaced.

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

Note that the ten percent is now a real ten percent. It used to be a fixed step
in VLC's own units, which are 256 to the nominal 100%, so a step that was
labelled 10% was really 10.16% -- and the overlay drifted as it counted down,
showing 100, 89, 79, 69 rather than round numbers.

### Popular videos, from YouTube or an aggregator

There was a YouTube channel (`bfch_youtube`) built on Reddit's sitewide
`/domain/youtube.com` listing: whatever anyone posted anywhere on Reddit that
linked to YouTube. It was retired rather than repaired, and has since come back
as something narrower.

The sitewide listing is what could not be kept. The archive the Reddit channels
now read searches within a named subreddit, and "anywhere on Reddit" is the one
question it cannot be asked -- see "Reddit posts come from an archive, and the
narrowed feeds are the weak part" above. Substituting a subreddit would return
a different thing that looked the same: posts from some subreddit about YouTube,
not posts from everywhere about YouTube.

So the channel is now exactly that, and says so: /r/youtubevideos, two feeds,
New and Top. The second feed it used to have came from YouTube's Data API with a
key written into the file, and is not coming back -- that key was a credential
belonging to whoever published it, committed to a public repository. The posts
carry their own titles, thumbnails and links, so nothing here needs it.

Two things about the replacement worth knowing:

- **There is no Top worth having in this subreddit, because it does not
  upvote.** Measured back to 2024, 98 of 100 posts score 1 and 2 score 0, so
  Top here is a ranking of a constant and comes back in time order. It is kept
  because it is a real ranking of whatever scores exist, not because it is
  informative here. The subreddit posts about 32 times a day, so New is the
  feed to use.
- **Three posts in a hundred link a channel rather than a video** ("we are
  live", "new video out"), and those are dropped. yt-dlp answers a channel URL
  by walking the channel's whole video list -- measured on the Pi, no output and
  no exit after 100 seconds -- and the player reads yt-dlp in a loop with no
  timeout, so such an item does not fail and does not start. It sits there, and
  the only way out is pressing stop.

  The drop is in `lib.chanutils.reddit`, not in this channel. It was written in
  the channel first, on the measurement that /r/youtubevideos carries these most
  often, and that was the wrong place: /r/Shortfilms has them at about the same
  rate, and a channel added tomorrow gets the filter by going through the same
  `_extract()` every other Reddit channel goes through rather than by
  remembering to copy it.- **Three posts in a hundred link a channel rather than a video** ("we are
  live", "new video out"), and those are dropped. yt-dlp answers a channel URL
  by walking the channel's whole video list -- measured on the Pi, no output and
  no exit after 100 seconds -- and the player reads yt-dlp in a loop with no
  timeout, so such an item does not fail and does not start. It sits there, and
  the only way out is pressing stop.

  The drop is in `lib.chanutils.reddit`, not in this channel. It was written in
  the channel first, on the measurement that /r/youtubevideos carries these most
  often, and that was the wrong place: /r/Shortfilms has them at about the same
  rate, and a channel added tomorrow gets the filter by going through the same
  `_extract()` every other Reddit channel goes through rather than by
  remembering to copy it.

Three posts in a hundred also link something that is not YouTube at all (a
meme, a Twitch VOD, a Suno link). Those are left in. yt-dlp errors on them
quickly and the error is shown, which is a different outcome from the hang
above, and the Twitch one sometimes plays.

What is still wanted is a way to play popular videos without going through Reddit
at all, which is a different feature with a different set of sources to weigh:

- YouTube's own feeds and search, through the bundled yt-dlp. The plumbing is
  already there, since every video play goes through it. The question is
  whether YouTube's own endpoints are reachable and stable enough to depend on,
  which is the same question Reddit's were, once.
- An aggregator that serves a plain list of what's popular, with no account and
  no key. Arctic Shift is one of these for Reddit specifically; the equivalent
  for video is not settled.

Worth deciding on its own terms rather than as a repair. Not before Phoenix.
