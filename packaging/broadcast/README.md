# RadioTEDU Windows broadcast-service beta package

This package installs four independently supervised Windows services:

- `RadioTEDU.SharedAI` — loopback Ollama and Qwen only; no station database or playout.
- `RadioTEDU.Station.EN` — EN-only database, queue, cache, Liquidsoap child, and source credentials.
- `RadioTEDU.Station.FR` — FR-only database, queue, cache, Liquidsoap child, and source credentials.
- `RadioTEDU.PublicSync` — the outbound-only, sanitized public-state service.

Run `install-services.ps1` in an elevated PowerShell after creating the four
environment files in `C:\ProgramData\RadioTEDU\config`.  The installer creates
services but deliberately does not start them unless `-Start` is supplied.
Starting is only permitted after the broadcast readiness and service checks have
passed on the target machine.

Do not add another station's source credentials, snapshot secret, queue path,
or database path to either station environment file.  The installer records
independent restart policies; failure of EN cannot restart FR, and vice versa.
