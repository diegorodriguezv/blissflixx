# Raspberry Pi + TV = <3

Use a Raspberry Pi to turn any screen into a smart streaming experience!

Watch movies, tv shows, documentaries and almost any video in the Internet, instantly.

Control everything from a web browser. 

Built with awesome **free software** tools that respect your privacy and put you in control. No ads, no tracking.

Website: https://diegorodriguezv.github.io/blissflixx/

# Features

BlissFlixx allows you to stream various types of video and audio content directly from the internet to your television. Supported media sources include:

 - Youtube
 - Facebook
 - Twitch
 - Catchup TV services such as BBC iPlayer
 - Full movies and documentaries on Youtube
 - Torrents (including magnet links)
 - Local media files
 - Video/Audio from hundreds of other websites

All media (including torrents) are streamed on demand and you do not need to wait for a download to complete. 

BlissFlixx allows for easy discovery of media by installing channels which group related content by category and allow user defined searches. e.g Documentary channel, Youtube channel, BBC iPlayer channel etc. New channels are easily created.

Multilingual subtitles can automatically be added to Movies and TV shows.

The user may also create multiple playlists to organise their favourite content.

The media server can be controlled via a web interface from any computer, tablet or phone.

# Screenshots

![BlissFlixx Channels](https://diegorodriguezv.github.io/blissflixx/img/channels.png)

![Youtube Movies Channel](https://diegorodriguezv.github.io/blissflixx/img/movies_chan.png)

![Documentary Channel](https://diegorodriguezv.github.io/blissflixx/img/doc_chan.png)

![BBC iPlayer Channel](https://diegorodriguezv.github.io/blissflixx/img/iplayer_chan.png)

![Video Controls](https://diegorodriguezv.github.io/blissflixx/img/control.png)

# Prerequisites

 - Raspberry Pi 3 or later with at least 8Gb SD card running Raspberry Pi OS (trixie), desktop or lite. You should also have about 2 Gb of free space on the card.
 - `git`, which a desktop image has and a lite image does not. Without it, install it first with `sudo apt install git`.
 - Broadband connection with enough speed to support video streaming. Ideally you should have a wired ethernet connection into your Pi as some WiFi adapters may have problems with streaming.
 - Television plugged into Raspberry Pi.
 - Computer, tablet or smartphone for controlling server.

# Installation

**IMPORTANT: BlissFlixx installation requires Raspberry Pi OS 13 (trixie), desktop or lite**

BlissFlixx for current Raspberry Pi OS is developed on the `bookworm` branch:

    git clone -b bookworm https://github.com/diegorodriguezv/blissflixx.git

Once it has been tested it will be published to the main repository, and this
line will go back to being a plain `git clone`.

Now enter the BlissFlixx folder and run the configure script which will install all the required external dependencies:

    cd blissflixx
    sudo ./configure.sh

Expect about **45 minutes** on a Raspberry Pi 3. The script prints how long each
stage took as it goes, and the total at the end, so you can see where the time
went. The largest single step is npm, which downloads nodejs and takes about
ten minutes of it; installing VLC takes about eight and GStreamer about four.

Various warnings are reported during the install and this is expected.

**If an apt repository fails partway**, the script carries on and the machine
ends up looking installed without being installed. The stage timings are how
you tell: something that took no time at all is the step that failed. Run
`sudo ./configure.sh` again and it will pick up where it left off, finishing in
a few minutes because everything already installed is cached.

# configure.sh options

The `configure.sh` script will install BlissFlixx dependencies and set up the python environment.
But it can also perform other optional tasks which are invoked with the following arguments:

* `-boot`

Install a system service script to run BlissFlixx when the system starts up. The command executed at boot is `run.sh` in case you want to modify it.

* `-blank`

Install a system service script to blank the screen when the system starts up.

**On current Raspberry Pi OS this does nothing, on either kind of install.**
`blank_screen.sh` has two paths, one for each kind of install: `tvservice -p`
for a headless 'lite' image, which needs no X, and `xset dpms` for a desktop,
which only runs when `xinit` exists. Neither is available now. `tvservice` was
part of a display stack Raspberry Pi OS no longer ships, and is not in the
archive to be installed. `xset` is present on a desktop but needs an X server
that a 'lite' install does not run.

The flag is kept because the intent still holds and the mechanism has not been
replaced yet; see the note in `docs/phoenix.md`. The command executed at boot is
`blank_screen.sh` in case you want to modify it.

* `-uninstall`

Remove the system service scripts installed by BlissFlixx.

* `-only`

Performs only the options invoked. Useful to configure BlissFlixx after initial installation.

For example `sudo ./configure.sh -only -blank` will just install the blank screen screen service, skipping over installation.

* `-no-players`

Skips installing the player backends, for when they are already present or you
want a smaller install. Everything else is installed as usual.

## After installing

`configure.sh` finishes by running `bin/check_players.py`, which reports whether
the player can actually run rather than merely being installed. Worth knowing,
because the two are not the same thing and a missing codec only shows up when
something tries to play.
Run it again at any time:

```
./virtualenv/bin/python ./bin/check_players.py
```

# configure_py.sh

The `configure_py.sh` script will set up the python environment for BlissFlixx.

# Running the server

Once the configuration script is complete you can start the server by running:

    ./start.sh

The server must be run with the virtualenv in this checkout. `run.sh` does that
and is what the systemd unit starts; starting it any other way is refused with a
message saying so, rather than failing later on whichever module happens to be
missing from the system Python.

Nothing is downloaded on first run any more. `configure_py.sh` installs
everything Python needs -- including yt-dlp -- and clones the plugins listed in
`plugins.txt`, so the first start is a start rather than an install.

Once you see the message "ENGINE Bus STARTED" the server will be up and running.
Now open up a browser on your phone, tablet or PC and point it at:

    http://ip-address-of-your-pi

So for example if the IP address of the Raspberry Pi is 192.168.1.4:

    http://192.168.1.4

**The server runs on port 80**, so there is no port number in the address. Port
80 normally needs privilege to bind; the service installed by `configure.sh
-boot` asks for `CAP_NET_BIND_SERVICE` for itself, which is why `./start.sh`
needs no `sudo`.

To run on a different port, for instance behind a reverse proxy or to avoid
needing that capability at all:

    ./blissflixx.py --port 6969

and then point the browser at `http://192.168.1.4:6969`.

If you have netbios installed in your network, chances are you can access your raspberry pi using it's `hostname`:

*    http://raspberrypi
*    http://raspberrypi.lan
*    http://raspberrypi.local

If you have an Apple device (iPad, Mac etc) read [this article](http://www.howtogeek.com/167190/how-and-why-to-assign-the-.local-domain-to-your-raspberry-pi/) for details on how to get this to work with Windows devices.

## Driving it from a terminal

Everything the web interface does is an HTTP call to `/api/<module>`, so the
server can be exercised, and diagnosed, from a shell. Useful when a remote
machine has no browser on it, or when you want to know whether a channel is
broken or just slow.

The two parameters are always the same: `fn` names the function in the module,
and `data` is a JSON object of its keyword arguments. Give `data` as a URL
parameter rather than pasting raw JSON into the address bar.

Check what the player is doing:

    curl 'http://192.168.1.4/api/playr?fn=status'

Stop it:

    curl --get http://192.168.1.4/api/playr \
      --data-urlencode "fn=control" --data-urlencode 'data={"action":"stop"}'

The action travels **inside** `data`. Two plausible-looking alternatives do
nothing at all, and neither says so:

| Attempted | Result |
|---|---|
| `?fn=stop` | `fn` must name a function in the module; there is no `stop`, so this returns 404 |
| `?fn=control&action=stop` | `action` is not a request parameter; `control` is called with no arguments and raises "Action is undefined" |

Both fail as a 404 or a 400 with a JSON body, which is easy to miss if you are
watching the process list rather than the response. Pass the action in `data`,
where `control(action=...)` expects it.

Play something:

    curl --get http://192.168.1.4/api/playr \
      --data-urlencode "fn=play" \
      --data-urlencode 'data={"url":"https://www.youtube.com/watch?v=i6eNK1O-RWw","title":"A test"}'

List a channel's items:

    curl --get http://192.168.1.4/api/channels \
      --data-urlencode "fn=feed" \
      --data-urlencode 'data={"chid":"bfch_bbc_iplayer","idx":0}'

Errors come back as `{"error": "..."}`. The text is meant to be shown to the
person watching: a channel that depends on a remote archive answers `503` with a
sentence saying the archive is busy, rather than a 500 carrying a stack trace.

# User guide

When you first connect your browser to the BlissFlixx server you should be presented with the following page showing your installed channels:

![BlissFlixx Homepage](https://diegorodriguezv.github.io/blissflixx/img/channels.png)

This page will allow you to browse all the channels that you have installed and enabled.

To remove any channels you are not interested in click on the settings cog icon on the top menu bar and then select the 'MANAGE CHANNELS' option.

Now select a channel such as Youtube Movies. You will then see a screen like the following: 

![Youtube Movies Channel](https://diegorodriguezv.github.io/blissflixx/img/movies_chan.png)

The default view is to list all the latest movies submitted to the channel. However you can also select the categories at the top of the page to view movies of the selected genre.

To play a movie simply click on the title link and the movie will start playing.

You can also search for specific movies from the search box.

All the other channels behave in a similar fashion with a list of categories and an optional search facility.

You can also add any media item to a playlist which is covered in the following section.

## Playlists

You can organise your media by creating playlists:

![BlissFlixx playlists](https://diegorodriguezv.github.io/blissflixx/img/playlists.png)

To create a playlist click on the 'NEW PLAYLIST' button and specify a title. You can then go and add a thumbnail and description for the playlist by clicking on the 'Edit Playlist' button.

When you are browsing your channels you will have an option to add items to your created playlists.

You can also import an M3U playlist by URL with the 'ADD REMOTE PLAYLIST' button, which is how an IPTV channel list is added. The list is fetched again every hour, so it stays as current as the source. Its items cannot be edited here -- they come from the list.

## Play URL

If you want to play something that is not provided by any of your channels then you can use this page to play it. First select the play icon in the menu bar and then copy the URL of the page containing the video and paste it into the 'What to Play' box. You will then be able to play and/or add it to a playlist. 

Note that you can also play torrents and magnet links by pasting the link as documented above.

## Search

The search page allows you to search all of your channels simultaneously. Simply enter your query terms into the search box and you will be presented with the results grouped by channel.

## Subtitles

Movies and TV shows will have an option saying 'Play With Subtitles'. Selecting this option will allow you to specify your preferred language as well as other metadata required to find the correct subtitles.

## Settings

The settings page currently contains two options. The first - MANAGE CHANNELS - allows you to disable or enable any installed channels. 

The second option - RESTART & UPDATE - will restart and also update the server to the latest release. If you come across any problems make sure you first update the server to the latest version to check if the issue has already been fixed.

# Player backends

BlissFlixx plays through VLC. Other backends exist in the code and can still be
selected through `set_backend` below, but they are not part of this release and
are not supported.

| Backend name | Player | Control | Subtitles | Audio tracks |
|---|---|---|---|---|
| `vlc` | cvlc | cli interface | yes | no |

`vlc` decodes H.264 in hardware, sends audio to HDMI and burns in subtitles.
Video output is pinned to `drm_vout` through the `vc4` driver, which is what
makes the Pi decode in hardware.

VLC is controlled through its cli interface: commands are typed into the
player's stdin and the replies read from its stdout. The rc interface would be
the more usual choice, but Debian trixie's VLC 3.0.23 for armhf ships no
`librc_plugin.so`, so `--extraintf=rc` is silently ignored and no control socket
is ever created. Subtitles and audio tracks are selected through `strack` and `atrack`,
which list the available tracks and mark the active one with an asterisk,
so visibility can be turned off with `-1` and back on with a real id.
`--osd` is enabled so a volume change or a seek is visible on screen: without
it the only feedback is a log line nobody watching a film can see. VLC's own
OSD cannot be sent text, because that needs `marq`, which this build does not
have; Every control action says what it did on the picture, for `osd_timeout`
milliseconds: where a seek landed, that the film is paused, which subtitle or
audio track is now selected, and the new volume level. It is driven by the
actions themselves rather than a timer, because polling the player once a second
is what stopped it answering anything at all a few seconds into playback -- and
because a person watching a film needs to know the button worked, not to read a
clock. The position is shown when a seek happens, not every second.

The messages are drawn with VLC's `marq` marquee, enabled through `osd_overlay`,
placed and sized with `osd_position`, `osd_size` and `osd_opacity`. It sits at the
top of the frame, well clear of the subtitles, which are left where VLC puts them
by default -- `sub_margin` is there if you want to lift them, and is 0.

VLC has no command for setting a marquee's text, so it is given a file and
re-reads it every `osd_refresh` seconds; that file is what `get_time` and
`get_length` feed. Setting `report_progress` to a number of seconds also writes
the same figures to the log, which is off by default.

When a film starts, what is on screen is its name and how long it is, drawn by
the same overlay -- the total length, not the position, which at the start is
0:00. Nothing is shown if there is no name to show.

Nothing here polls the player on a timer. That is what stops its cli interface
answering at all, and the reply is taken within a second or so of the action
that wanted it, rather than continuously.

Subtitle delay is a separate capability from subtitle selection, and VLC's cli
interface has no verb for it. VLC does not advertise the capability, so a client
knows not to offer buttons that would do nothing.

## Choosing a backend

Which backend is active is a plain JSON file, `data/settings/player`:

```json
{"backend": "vlc"}
```

Delete the file, or leave it out, and the default is used. The choice is read on
each `play`, so it takes effect from the next item onwards rather than needing a
restart.

It can also be changed over the API, which writes the same file:

```
/api/playr?fn=backends                            # every backend, with the active one marked
/api/playr?fn=backend                             # just the active one
/api/playr?fn=set_backend&data={"name":"vlc"}     # select one
```

`backends` reports each backend's capabilities, so a client can hide the buttons
a backend would silently drop rather than offering controls that do nothing.

Switching does not disturb what is already playing: the current item keeps the
backend it started with.

## Configuring a backend

Each backend's settings live in `data/settings/player-<backend name>`, and only
need to contain what differs from the built-in defaults. This is where the parts
that vary between machines go: an ALSA card name, a KMS plane id.

```json
{"audio_device": "alsa/hdmi:CARD=hdmi_zero,DEV=0"}
```

A Pi 4 uses `CARD=vc4hdmi`, a Pi 5 uses `CARD=hdmi_zero`. Because the file is
per backend, the same checkout can hold a Pi 4 profile and a Pi 5 profile at once
by registering the second under another name.

The full set of keys, and their defaults, is each backend's `defaults` dict:

| Backend | Keys |
|---|---|
| `vlc` | `binary`, `extra_args`, `audio_device`, `video_output`, `video_output_module`, `source_modeset`, `fast_seek`, `subtitle_text_scale`, `start_timeout`, `report_progress`, `osd_overlay`, `osd_refresh`, `osd_position`, `osd_size`, `osd_opacity`,  `sub_margin`, `volume_step`, `volume_max`, `subtitle_delay_step` |

`extra_args` replaces the built-in flag list rather than adding to it, so a flag
the backend always passes can be removed by naming the list without it.

`source_modeset` matches the display to the film instead of scaling it. It is on,
because the scaling is not free: the display controller's scaler uses the HVS's
line-buffer entries, a small fixed hardware pool, and the on-screen text needs one
of its own. With a 1080p film on a 1366x768 mode the pool ran out and every frame
was rejected -- a decoded film and a black screen. Matching the mode costs a
flicker at the start of a film and removes the scaling entirely.

`fast_seek` is on here and off in VLC. It seeks to the nearest keyframe, which
costs a little precision — up to a GOP — and buys a picture that keeps moving
when two seeks arrive in quick succession. Without it the decoder wedges
(`Timestamp conversion failed`) and the film freezes on its last frame while the
clock keeps running, so the elapsed time advances and nothing is playing.

`subtitle_delay_step` is how far one press of the playbar's subtitle-delay
buttons moves the timing. VLC's floor is 50 ms and it cannot be asked for an
amount, so a step is rounded to whole multiples of that and the confirmation
shows the offset actually in force. The default quarter second matches mpv;
set it to `0.05` when a film needs an offset the quarter-second grid misses.

A key the backend does not recognise is logged and ignored, so a settings file
written for a different version stays usable.

## What still needs the Pi

The command for each backend, its readiness handling and its control commands
are all covered by the test suite on a development machine. What cannot be is
whether the video actually appears: `kmssink`, `drm_vout` and `v4l2h264dec` need
the Pi's kernel and hardware. `gst-launch` in particular cannot express a path
containing a space, and says so rather than failing obscurely.

# Help

If you find a bug or want to ask a question please use the [github issues page](https://github.com/blissland/blissflixx/issues).

More information can be found at the [wiki](https://github.com/blissland/blissflixx/wiki).

# Supported media sites

BlissFlixx relies on yt-dlp for extracting media so a list of supported sites can be found on it's project site: https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md

yt-dlp is the one dependency that is not pinned, because it follows the sites it
extracts from rather than this project: a pinned yt-dlp is a yt-dlp that has
stopped handling some of them. The UPDATE button therefore moves it, and then
asks it to resolve something before believing it -- if the new version cannot,
it is put straight back and the page says which version is installed. The number
`configure_py.sh` prints at the end of an install is the same one.

Everything else *is* pinned, in `requirements.txt`. The UPDATE button does not
install it; if a pull brings a changed `requirements.txt`, the page says so and
leaves it to you, because a pinned dependency is only pinned if nothing quietly
moves it.

That list is what yt-dlp supports, not what it can reach. A site that now
sits behind a bot check will refuse a Raspberry Pi even when the extractor for
it is listed. Vimeo is the case that matters: vimeo.com answers a Pi with a
Cloudflare challenge page, and the only client that then works needs a Vimeo
login, so the Vimeo channel was removed rather than left listing videos that
all fail. A channel is only worth having if its items play.

## YouTube

YouTube answers a Raspberry Pi with "sign in to confirm you're not a bot" often
enough that it is worth saying what does and does not help.

`configure.sh` unpacks a pinned node LTS into `data/node` so yt-dlp can run
YouTube's JavaScript challenges. Not the distribution's: yt-dlp refuses node
below 22 and Raspberry Pi OS ships 20, so asking for `node` bought nothing and
said so only in a warning. It is unpacked into the checkout rather than
installed system-wide, so the runtime npm and peerflix were installed with is
untouched. `quickjs` is installed too, as the fallback: it works, but it is an
interpreter and took four times as long to solve the same challenge — 79 s
against 19 s on a Pi 4.

Picture and sound are requested as **two streams**, which is what makes 1080p
reachable at all: one URL can only be a muxed stream, and YouTube's muxed
streams stop at 360p. The player takes them as VLC's input and its slave, which
measured as hardware-decoded 1080p with no dropped frames and in sync.

**H.264 only**, and that is not a preference. It is the only codec this hardware
both decodes and displays: AV1 and VP9 arrive as `I420` and are copied per frame,
which is about 1 fps at 1080p, while H.264 arrives as a DRM buffer and is scanned
out directly. So the ceiling is whatever YouTube still serves in H.264 — 1080p on
many videos, less on others.

Being signed in is the other half, and the box has no browser to read cookies
from. Export a Netscape-format `cookies.txt` from a browser that is signed in
(the "Get cookies.txt LOCALLY" extension does it in one click) and copy it to:

    data/cookies.txt

**Cookies are optional.** yt-dlp is handed `--cookies` whenever that file exists
and no flag at all when it does not, so a fresh install works without one: every
site that does not need an account plays, and the ones that do say as much in the
play error. The file is what extends that to YouTube and Reddit.

The extension exports one site at a time; the format is one cookie per line, so
appending two exports is enough to cover both. yt-dlp also writes the jar back as
it plays, refreshing sessions and keeping whatever a site hands it, so the file
has to stay writable by the service.

It is a session credential, which is worth removing rather than leaving on a card
that might change hands:

    rm -f data/cookies.txt

Nothing else refers to it -- the next play simply runs without cookies, and no
restart is needed. It lives under `data/`, which git ignores, so it cannot be
committed by accident.

Neither is a promise. YouTube rate-limits by address as well as by account, so a
house that has been streaming all evening can be told to sign in with cookies
that are perfectly good; then the answer is to wait. The updater's smoke test
resolves an archive.org video rather than a YouTube one for exactly this reason:
a test that fails because of YouTube says nothing about yt-dlp.

# Plugins

Channels that are separate projects live in `plugins/`, which is not part of this
repository -- they have their own histories and their own authors. `plugins.txt`
lists them, `configure_py.sh` clones whatever from that list is missing, and the
channel manager lists whatever it finds in `plugins/`. A fresh checkout is
therefore the same installation as an established one.

# Updating

The UPDATE button on the settings page moves three things and reports each one
separately:

  * the plugins, which are fast-forwarded to the newest commit on the branch each
    one is on;
  * yt-dlp, which is moved to the newest release and then checked;
  * BlissFlixx itself, fast-forwarded to the newest commit on the branch this
    checkout is on -- read from git rather than written down, so it cannot ask
    for a branch this project does not use.

Pulls are `--ff-only`: a checkout that has diverged fails with git's own reason
rather than being merged in silence. If nothing moved, nothing restarts and the
page says what was already current. If something did move, the server restarts
and every open tab says so while it does.

To go back, reinstall the version it names: `./configure_py.sh`, or

    ./virtualenv/bin/pip install yt-dlp==<the version you want>

## Moving an existing install onto this

Nothing above changes anything by itself. An install made before yt-dlp became a
package needs one step, because until it runs the virtualenv has no yt-dlp in it
and nothing can resolve a video:

    ./configure_py.sh

That installs yt-dlp, and leaves the old `lib/yt-dlp` checkout alone -- which is
deliberate, since deleting it is not something to do on somebody's machine
without asking. It is harmless: nothing reads it, `.gitignore` still ignores it,
and it can go whenever:

    rm -rf lib/yt-dlp

For information about what extra plugins are available please check the [Plugin Development](https://github.com/blissland/blissflixx/wiki/Plugin-Development) section of the wiki.

# Credits

BlissFlixx relies on the following projects to do all the heavy lifting:

 - yt-dlp for media extraction: https://github.com/yt-dlp/yt-dlp
 - peerflix for torrent streaming: https://github.com/mafintosh/peerflix
 - SinglePaged jekyll theme for the website: https://github.com/t413/SinglePaged

## Previously used software

BlissFlixx used to rely on the following software projects:

 - omxplayer for media playback: https://github.com/popcornmix/omxplayer
 - livestreamer for media streaming: https://github.com/chrippa/livestreamer
 - youtube-dl for media extraction: https://github.com/rg3/youtube-dl
