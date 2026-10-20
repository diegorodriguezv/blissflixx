import re

from ..chanutils import get

BBC_URL = re.compile(
    r"https?://(?:www\.)?bbc\.co\.uk/(?:(?:(?:programmes|iplayer(?:/[^/]+)?/(?:episode|playlist))/)|music/clips[/#])(?P<id>[\da-z]{8})"
)

#: A TechCrunch video post. The article page is not playable -- yt-dlp has no
#: extractor for techcrunch.com and its generic one gives up with "Unsupported
#: URL" -- but the page carries the id of the JW Player playlist the video is
#: served from, and that playlist is directly playable. See resolve().
TECHCRUNCH_URL = re.compile(
    r"https?://(?:www\.)?techcrunch\.com/(?:video|20\d\d/\d\d/\d\d)/[^?#]*"
)

#: The JW Player playlist itself. cdn.jwplayer.com is not bot-protected and
#: yt-dlp has a first-class extractor for it.
JWPLAYER_URL = re.compile(r"https?://cdn\.jwplayer\.com/v2/media/(?P<id>[A-Za-z0-9]+)")

#: The id in the article page. It appears twice -- once as a meta itemprop and
#: once in the inline player setup -- so a single match is not proof of anything
#: and the pattern is deliberately narrow.
JW_ID_IN_PAGE = re.compile(r"cdn\.jwplayer\.com/v2/media/([A-Za-z0-9]+)")

YOUTUBE_URL = re.compile(
    r"""(?x)^
                     (
                         (?:https?://|//)                                    # http(s):// or protocol-independent URL
                         (?:(?:(?:(?:\w+\.)?[yY][oO][uU][tT][uU][bB][eE](?:-nocookie)?\.com/|
                            (?:www\.)?deturl\.com/www\.youtube\.com/|
                            (?:www\.)?pwnyoutube\.com/|
                            (?:www\.)?yourepeat\.com/|
                            tube\.majestyc\.net/|
                            youtube\.googleapis\.com/)                        # the various hostnames, with wildcard subdomains
                         (?:.*?\#/)?                                          # handle anchor (#/) redirect urls
                         (?:                                                  # the various things that can precede the ID:
                             (?:(?:v|embed|e)/(?!videoseries))                # v/ or embed/ or e/
                             |(?:                                             # or the v= param in all its forms
                                 (?:(?:watch|movie)(?:_popup)?(?:\.php)?/?)?  # preceding watch(_popup|.php) or nothing (like /?v=xxxx)
                                 (?:\?|\#!?)                                  # the params delimiter ? or # or #!
                                 (?:.*?&)?                                    # any other preceding param (like /?s=tuff&v=xxxx)
                                 v=
                             )
                         ))
                         |youtu\.be/                                          # just youtu.be/xxxx
                         |(?:www\.)?cleanvideosearch\.com/media/action/yt/watch\?videoId=
                         )
                     )?                                                       # all until now is optional -> you can pass the naked ID
                     ([0-9A-Za-z_-]{11})                                      # here is it! the YouTube video ID
                     (?!.*?&list=)                                            # combined list/video URLs are handled by the playlist IE
                     (?(1).+)?                                                # if we found the ID, everything can follow
                     $"""
)
VIMEO_URL = re.compile(
    r"""(?x)
                    https?://
                        (?:
                            (?:
                                www|
                                (?P<player>player)
                            )
                            \.
                        )?
                        vimeo(?P<pro>pro)?\.com/
                        (?!(?:channels|album)/[^/?#]+/?(?:$|[?#])|[^/]+/review/|ondemand/)
                        (?:.*?/)?
                        (?:
                            (?:
                                play_redirect_hls|
                                moogaloop\.swf)\?clip_id=
                            )?
                        (?:videos?/)?
                        (?P<id>[0-9]+)
                        (?:/[\da-f]+)?
                        /?(?:[?&].*)?(?:[#].*)?$
                    """
)
ITV_URL = re.compile(r"https?://www\.itv\.com/(.+?)")
OPENLOAD_URL = re.compile(
    r"https?://(?:www\.)?(?:openload\.(?:co|io|link)|oload\.(?:tv|stream))/(?:f|embed)/(?P<id>[a-zA-Z0-9-_]+)"
)


DL_URLS = [
    #  BBC_URL,
    ITV_URL,
    OPENLOAD_URL,
]


def skip_download(url):
    for url_re in DL_URLS:
        if url_re.match(url):
            return False
    return True


def resolve(url):
    """
    Turn a page URL into something the player can actually open.

    One case so far. A TechCrunch video post is an article page, and the article
    page is not media: yt-dlp has no extractor for techcrunch.com, its generic
    one gives up, and every item in the channel failed with

        ERROR: Unsupported URL: https://techcrunch.com/video/...

    The page embeds the video from JW Player, and names the playlist it comes
    from:

        <meta itemprop='contentUrl' content='https://cdn.jwplayer.com/v2/media/nAuWIDg0'/>

    cdn.jwplayer.com is not bot-protected, and yt-dlp resolves that URL to the
    file directly. So the article URL is exchanged for the playlist URL before
    yt-dlp ever sees it, and the item plays.

    Anything not recognised is returned unchanged, and a page that cannot be read
    or does not name a playlist is left alone too. This is a translation, not a
    gate: if it cannot help, the URL that came in is the URL that goes on to
    yt-dlp, which then fails exactly as it did before rather than worse.
    """
    if not TECHCRUNCH_URL.match(url):
        return url
    try:
        page = get(url).text
    except Exception:
        # A page we cannot read is not an error here. The URL that came in is
        # still the best guess, and yt-dlp's own failure is the one that gets
        # reported.
        return url
    found = JW_ID_IN_PAGE.search(page)
    if found is None:
        return url
    return "https://cdn.jwplayer.com/v2/media/" + found.group(1)


def get_format(url):
    if BBC_URL.match(url):
        # Don't download hd 1280 x 720 but the next best quality
        # (usaully 832 x 468). Sometimes rtmpdump aborts before downloading
        # all of hd quality. Lower quality seems more reliable.
        return "best[height<720]"
    elif JWPLAYER_URL.match(url):
        # Ask for a progressive file rather than an HLS variant playlist.
        #
        # Left to itself "best" ranks the m3u8 renditions above the plain ones,
        # so it hands back another playlist -- one more level of indirection
        # for something that is supposed to be a finished media URL. Naming the
        # protocol and the container gets the progressive MP4 instead, which is
        # a plain file on a CDN with no signature and no token.
        return "best[protocol=https][ext=mp4]"
    elif YOUTUBE_URL.match(url) or VIMEO_URL.match(url):
        # Picture and sound, separately, in H.264 -- which is what makes 1080p
        # possible at all here.
        #
        # This used to be "(mp4)", which picks the highest-resolution mp4. Above
        # 360p those are video-only, so the film played silently from a stream
        # the Pi could not present: the 1080p AV1 it selected dropped 668 of
        # 1358 frames while the clock kept running, which is the frozen picture
        # with a working seek bar this was reported as.
        #
        # H.264 is not a preference, it is the only codec this hardware both
        # decodes and displays. h264 comes out of the decoder as a DRM buffer and
        # is scanned out directly; VP9 and AV1 arrive as I420 and are copied per
        # frame, which measured at about 1fps at 1080p. So the ceiling is
        # whatever YouTube still serves in H.264: 1080p on many videos, 480p or
        # less on others.
        #
        # The "+" pair is the other half. One URL can only ever be a muxed
        # stream, and YouTube's muxed streams stop at 360p -- so the resolution
        # cap was structural. ytdlproc hands the two URLs to VLC as its input and
        # its slave, which measured as 1080p, hardware, zero dropped frames, and
        # in sync including after a seek.
        #
        # The fallbacks cover videos with no separate streams at all: prefer a
        # muxed H.264 one, then any muxed one, because a film that plays is
        # better than a format error.
        return (
            "bestvideo[height<=1080][vcodec^=avc1]+bestaudio[ext=m4a]"
            "/best[height<=1080][vcodec^=avc1]"
            "/best"
        )
    else:
        return None
