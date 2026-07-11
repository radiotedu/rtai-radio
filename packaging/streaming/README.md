# RadioTEDU independent streaming fallback

Install this artifact separately on `stream-a` and `stream-b`.  They must be
separate hosts: do not share a VM, disk, Icecast process, fallback process, or
power-failure domain.

Each host runs Icecast plus two music-and-imaging-only Liquidsoap sources:
`radiotedu-fallback-en` and `radiotedu-fallback-fr`.  Fallback playout never
creates speech and never contacts Qwen, the broadcasting computer, or the
public website.

## Host preparation

1. Create an unprivileged `radiotedu` account and install `icecast2` and
   Liquidsoap with MP3 encoding support.
2. Copy the station-specific, validated music/imaging assets to
   `/var/lib/radiotedu/fallback/en/assets` and
   `/var/lib/radiotedu/fallback/fr/assets`.
3. Copy `fallback-en.env.example` and `fallback-fr.env.example` to
   `/etc/radiotedu/fallback-en.env` and `/etc/radiotedu/fallback-fr.env`;
   set the per-host Icecast source password.
   Do not reuse the same password on both hosts.
4. Run `sudo ./install.sh` from this directory, then validate both local mounts
   with `systemctl status radiotedu-fallback-en radiotedu-fallback-fr`.

The broadcasting computer publishes primary sources to both independent hosts.
The edge health-check layer, not this artifact, selects the listener endpoint.
