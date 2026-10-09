# Raspberry Pi + TV = <3

Use a Raspberry Pi to turn any screen into a smart streaming experience!

Watch movies, tv shows, documentaries and almost any video in the Internet, instantly.

Control everything from a web browser. 

Built with awesome **free software** tools that respect your privacy and put you in control. No ads, no tracking.

Website: https://diegorodriguezv.github.io/blissflixx/

# Features

BlissFlixx allows you to stream various types of video and audio content directly from the internet to your television. Supported media sources include:

 - Youtube
 - Vimeo
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

 - Raspberry Pi (Not model A) with at least 8Gb SD card running Raspbian. You should also have about 2 Gb of free space on the card.
 - Broadband connection with enough speed to support video streaming. Ideally you should have a wired ethernet connection into your Pi as some WiFi adapters may have problems with streaming.
 - Television plugged into Raspberry Pi.
 - Computer, tablet or smartphone for controlling server.

# Installation

**IMPORTANT: BlissFlixx installation requires Raspberry PI OS 10 (Buster) 32 bits - desktop or lite**

To install git clone this repository:

    git clone https://github.com/blissland/blissflixx.git

Now enter the BlissFlixx folder and run the configure script which will install all the required external dependencies:

    cd blissflixx
    sudo ./configure.sh

It will take about 1 hour for this script to complete so now might be a good time to wash the dishes or take the dog for a walk. Note that during the execution of the script various warnings will be reported but this is expected.

# configure.sh options

The `configure.sh` script will install BlissFlixx dependencies and set up the python environment.
But it can also perform other optional tasks which are invoked with the following arguments:

* `-boot`

Install a system service script to run BlissFlixx when the system starts up. The command executed at boot is `run.sh` in case you want to modify it.

* `-blank`

Install a system service script to blank the screen when the system starts up. It's particularly useful when using a 'lite' version without an X desktop as the screen is full of boot up messages. Blanking the screen allows the screen to enter in a power saving mode more easily. For installations with a desktop environment this can also be useful so the script configures the X screensaver to blank the screen after 10 seconds. The command executed at boot is `blank_screen.sh` in case you want to modify it.

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
each registered backend can actually run rather than merely be installed. It is
worth knowing, because two of them need attention on a current Raspberry Pi OS:

* omxplayer has no package, so it cannot be installed at all. The two omxplayer
  backends only work on the old OS.
* stock GStreamer has no `v4l2h264dec`; the v4l2 decoders were dropped from the
  main gst-plugins set. Set `video_decoder` to `avdec_h264` in
  `data/settings/player-gstreamer` for software decode, which costs CPU.

Run it again at any time:

```
./virtualenv/bin/python ./bin/check_players.py
```

# configure_py.sh

The `configure_py.sh` script will set up the python environment for BlissFlixx.

# Running the server

Once the configuration script is complete you can start the server by running:

    ./blissflixx.py
    
Note that the first time you start the server it will first take a few moments to complete the installation.

Once you see the message "ENGINE Bus STARTED" the server will be up and running. Now open up a browser on your phone, tablet or PC and point it at:

    http://ip-address-of-your-pi:6969
    
So for example if the IP address of the Raspberry Pi is  192.168.1.4. Then you need to point your browser to:

    http://192.168.1.4:6969

If you want to run the server on the default port 80 then you will need start the server with the --port flag:

    ./blissflixx.py --port 80
    
Finally if you want the server to continue running even after you log out of your session (which is usaully the case) then specify the --daemon flag:

    ./blissflixx.py --port 80 --daemon
    
Fortunately there is a script to run the above command:

    ./start.sh

If you have netbios installed in your network, chances are you can access your raspberry pi using it's `hostname`:

*    http://raspberrypi
*    http://raspberrypi.lan
*    http://raspberrypi.local

If you have an Apple device (iPad, Mac etc) read [this article](http://www.howtogeek.com/167190/how-and-why-to-assign-the-.local-domain-to-your-raspberry-pi/) for details on how to get this to work with Windows devices.

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

Playback used to go through omxplayer, which no longer runs on current Raspberry
Pi OS. VLC is now the default. The omxplayer backends remain selectable by name
for anyone still on the old OS.

| Backend name | Player | Control | Subtitles | Audio tracks |
|---|---|---|---|---|
| `omxplayer` | omxplayer | dbus, pause only | yes | no |
| `omxplayer-keys` | omxplayer.bin | FIFO keystrokes | yes | yes |
| `vlc` | cvlc | cli interface | yes | no |
| `mpv` | mpv | JSON IPC | yes | yes |
| `gstreamer` | gst-launch-1.0 | none | no | no |

`gstreamer` has no control surface at all: `gst-launch` is a one-shot pipeline
runner with no IPC, so it cannot be paused, seeked or adjusted. Stopping still
works, because the player SIGKILLs the process group. It is the most efficient
by CPU and the least capable.

`vlc` is the recommended player on current Raspberry Pi OS, and the default. It
decodes H.264 in hardware, sends audio to HDMI and burns in subtitles, without
the expensive GStreamer compositing path. Video output is pinned to `drm_vout`
through the `vc4` driver, which is what makes the Pi decode in hardware.

VLC is controlled through its cli interface: commands are typed into the
player's stdin and the replies read from its stdout. The rc interface would be
the more usual choice, but Debian trixie's VLC 3.0.23 for armhf ships no
`librc_plugin.so`, so `--extraintf=rc` is silently ignored and no control socket
is ever created. Subtitles and audio tracks are selected through `strack` and `atrack`,
which list the available tracks and mark the active one with an asterisk,
so visibility can be turned off with `-1` and back on with a real id. VLC
therefore declares the same capabilities as `mpv` and `omxplayer-keys`.
`--osd` is enabled so a pause or a seek is visible on screen: without it
the only feedback is a log line nobody watching a film can see.

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
/api/playr?fn=backend&data={"name":"mpv"}        # describe one without selecting it
/api/playr?fn=set_backend&data={"name":"mpv"}     # select one
```

`backends` reports each backend's capabilities. That matters because the playbar
renders one fixed set of controls whatever is playing: with `gstreamer` active,
`capabilities` is empty and a `note` says so, so a client can hide the buttons
that would be dropped rather than offering controls that do nothing.

The special name `legacy` selects the behaviour from before backends existed,
where the http and dlsrv flags chose between the two omxplayer variants. It is
reported with a note saying so, because its capabilities depend on those flags
rather than being fixed.

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
| `vlc` | `binary`, `extra_args`, `audio_device`, `video_output`, `video_output_module`, `subtitle_text_scale`, `start_timeout`, `volume_step`, `volume_max` |
| `mpv` | `binary`, `extra_args`, `audio_device`, `socket`, `start_timeout` |
| `gstreamer` | `binary`, `pipeline`, `http_source`, `plane_id`, `connector_id`, `audio_device`, `video_decoder`, `audio_decoder`, `start_timeout` |
| `omxplayer` | `binary`, `extra_args`, `start_timeout`, `input_timeout`, `dbus_path` |
| `omxplayer-keys` | `binary`, `extra_args`, `start_timeout`, `fifo` |

`extra_args` replaces the built-in flag list rather than adding to it, so a flag
the backend always passes can be removed by naming the list without it.

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

# Plugins

For information about what extra plugins are available please check the [Plugin Development](https://github.com/blissland/blissflixx/wiki/Plugin-Development) section of the wiki.

# Credits

BlissFlixx relies on the following projects to do all the heavy lifting:

 - yt-dlp for media extraction: https://github.com/yt-dlp/yt-dlp
 - peerflix for torrent streaming: https://github.com/mafintosh/peerflix
 - omxplayer for media playback: https://github.com/popcornmix/omxplayer
 - SinglePaged jekyll theme for the website: https://github.com/t413/SinglePaged

BlissFlixx used to rely on the following software projects:

 - livestreamer for media streaming: https://github.com/chrippa/livestreamer
 - youtube-dl for media extraction: https://github.com/rg3/youtube-dl
