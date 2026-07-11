# Music/imaging-only fallback source.  No TTS provider is reachable here.
set("log.stdout", true)
set("server.telnet", false)

station = getenv("RADIOTEDU_STATION")
playlist_path = getenv("FALLBACK_PLAYLIST")
host = getenv("ICECAST_HOST")
port = int_of_string(getenv("ICECAST_PORT"))
password = getenv("ICECAST_SOURCE_PASSWORD")
mount = getenv("ICECAST_MOUNT")

fallback_music = playlist(
  id="radiotedu-#{station}-fallback",
  mode="randomize",
  reload=1,
  reload_mode="watch",
  playlist_path
)

output.icecast(
  %mp3(bitrate=192, samplerate=44100, stereo=true),
  host=host,
  port=port,
  password=password,
  mount=mount,
  name="RadioTEDU #{station} independent fallback",
  description="Music and validated station imaging only",
  fallible=false,
  fallback_music
)
