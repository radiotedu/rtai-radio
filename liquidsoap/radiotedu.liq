# RadioTEDU station-local Liquidsoap template.
# Production configs are rendered per station; the source password is injected
# from a protected target-machine environment and is never written here.
set("log.stdout", true)
set("server.telnet", false)

queue_path = getenv(default="data/liquidsoap/queue.m3u", "LIQUIDSOAP_QUEUE_PATH")
host = getenv(default="10.98.98.75", "ICECAST_HOST")
port = int_of_string(getenv(default="11154", "ICECAST_PORT"))
password = getenv(default="", "ICECAST_PASSWORD")
mount = getenv(default="/ai", "ICECAST_MOUNT")

radio = playlist(id="RadioTEDU", mode="normal", reload=1, reload_mode="watch", queue_path)
radio = mksafe(radio)

output.icecast(
  %ffmpeg(format="adts", %audio(codec="aac", b="192k", ac=2, ar=48000)),
  host=host,
  port=port,
  user="source",
  password=password,
  mount=mount,
  public=true,
  name="RadioTEDU",
  description="RadioTEDU bilingual station stream",
  genre="RadioTEDU",
  radio
)
