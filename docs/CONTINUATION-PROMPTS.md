# Target-Machine Handoff Index

The builder produces exactly two self-contained Codex prompts. Their canonical copies are packaged with the target-machine launchers:

1. `handoff/broadcast-server/prompt.md`
2. `handoff/web-server/prompt.md`

Do not create another deployment prompt. The broadcast prompt owns target-machine broadcast staging; the web prompt owns target-machine API/UI/reverse-proxy staging. Both stop before production cutover unless the operator explicitly authorizes it.
