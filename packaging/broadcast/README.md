# RadioTEDU Windows broadcast services

This package installs two Windows services:

- `RadioTEDU.SharedAI` owns loopback-only Ollama and Qwen TTS.
- `RadioTEDU.BroadcastSupervisor` starts, monitors, and independently recovers the EN and FR station child processes. The same supervisor process owns the single outbound-only `PublicSyncService` and durable outbox.

Create the two protected environment files in `C:\ProgramData\RadioTEDU\config` from the examples. Restrict their ACLs to the service identity and administrators; never put source credentials or HMAC secrets in the repository, prompt, command history, or logs. `install-services.ps1 -InitializeConfig` creates missing files and applies SYSTEM/Administrators-only ACLs without overwriting an existing protected file.

Run `install-liquidsoap-windows.ps1` first to install and qualify the official native Windows Liquidsoap 2.4.5 build. Then run `install-services.ps1 -InitializeConfig` from elevated PowerShell. The installer uses native PyWin32 service hosts and creates Manual services without starting them unless another switch is supplied. `-Automatic` configures delayed automatic startup without starting live air. The service host then retries a failed long-running child with bounded backoff, while protected preflight rejects placeholder credentials and unapproved or incomplete Qwen voice assets. `-Start` first runs the strict dual-station smoke and refuses to start either service when any media, FFmpeg AAC, credential, voice, API, or isolation gate fails; a passing explicit start also switches the services to delayed automatic startup. Use `-Start` only on the target broadcast computer after staging preflight, separate `/ai` and `/event` mount checks, and API conformance tests pass.

Both stations intentionally share the Icecast host at `10.98.98.75:11154`. Station processes, queues, databases, and recovery remain isolated, but the Icecast host is one acknowledged failure domain.
